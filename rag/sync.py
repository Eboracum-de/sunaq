#!/usr/bin/env python3
"""
Incremental Elasticsearch -> Qdrant sync for Nextcloud Hybrid RAG.

v2 additions:
- Preserve Nextcloud Elasticsearch document id as nextcloud_openfile_id.
- Preserve Nextcloud path/directory/filename.
- Preserve ACL-related metadata: owner, users, groups, circles, share_names.
- Preserve document metadata: date, content type, language, source/provider.
- Keep stable UUIDv5 Qdrant point ids.
- Keep incremental state in SQLite.
- v0.6.5: optional include/exclude path scopes and background GraphQueue enqueue.
- v0.6.8a: per-document index signature invalidates stale chunks when embedding/chunk settings change.
- v0.6.8b: Ollama and OpenAI-compatible embedding backends share one adapter.
- 0.8.3-rc11: shared core also supports super-light deployment; research findings are provider/graph-side.
- 0.8.3-rc9: explicit model-agnostic embedding prefixes; configurable dimensions and basename-aware document embeddings.

The Elasticsearch _id "files:<n>" is deliberately treated as an
openfile reference, not as the canonical Nextcloud fileid. On the
user's Nextcloud, ?openfile=<n> resolves correctly to the current file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import requests
import yaml

from rag.embeddings import EmbeddingContextLengthError, build_embedding_backend_from_config
from rag.elasticsearch_client import requests_options as elastic_requests_options
from rag.logging_utils import get_logger
from rag.runtime_control import worker_enabled as runtime_worker_enabled
from rag.source_origin import classify_source_origin, is_machine_sidecar_path, maybe_register_path_origin
from rag.source_registry import SPECIAL_ORIGINS, mirror_updates_to_elasticsearch

UUID_NAMESPACE = uuid.NAMESPACE_URL
log = get_logger("sync")
SYNC_SCHEMA_VERSION = "sync-v3"
CHUNKER_VERSION = "sync-char-boundary-v1"


DEFAULT_SEMANTIC_EXCLUDE_EXTENSIONS = {
    # Raw mail/container formats. Normalized mail text (*.txt) remains eligible.
    "eml", "msg", "mbox",
    # Archives are never useful as one semantic document even if an extractor
    # happens to expose container metadata as text.
    "zip", "7z", "rar", "gz", "bz2", "xz", "tar", "tgz",
}
DEFAULT_SEMANTIC_EXCLUDE_MIME_PREFIXES: tuple[str, ...] = ()
DEFAULT_SEMANTIC_EXCLUDE_MIME_TYPES = {
    # MIME is a fallback only for extensionless/ambiguous objects. A known text
    # representation such as *.txt must not be rejected because Nextcloud still
    # reports the source mail MIME type (message/rfc822).
    "message/rfc822",
    "application/zip", "application/x-7z-compressed", "application/x-rar-compressed",
    "application/gzip", "application/x-gzip", "application/x-tar",
}

# Extensions that make an extracted text representation authoritative when it
# conflicts with attachment.content_type. This is not a whitelist: unknown
# extensions with useful FullTextSearch content are still eligible unless they
# hit a hard exclusion.
KNOWN_TEXT_DOCUMENT_EXTENSIONS = {
    "txt", "text", "md", "markdown", "csv", "tsv", "html", "htm", "xml", "json",
    "pdf", "rtf", "doc", "docx", "odt", "xls", "xlsx", "ods", "ppt", "pptx", "odp",
}

def _config_string_set(cfg: dict[str, Any], path: str, default: Iterable[str]) -> set[str]:
    value = cfg_get(cfg, path, default=list(default))
    if isinstance(value, str):
        value = [value]
    return {str(item).strip().casefold().lstrip(".") for item in (value or []) if str(item).strip()}

def semantic_document_allowed(cfg: dict[str, Any], hit: dict[str, Any]) -> tuple[bool, str]:
    """Return whether an ES document should enter Qdrant/GraphQueue.

    The sync embeds only Elasticsearch's extracted ``content``; it never asks an
    LLM to decode the source file. Raw mail/container formats and mail metadata
    sidecars are hard exclusions. Images/audio/video are *not* rejected by file
    type: if FullTextSearch produced useful ``content``, that text may be indexed.
    """
    enabled = bool(cfg_get(cfg, "sync.document_filter.enabled", default=True))
    if not enabled:
        return True, "filter_disabled"

    src = hit.get("_source") or {}
    title = str(src.get("title") or hit.get("_id") or "")
    suffix = PurePosixPath(title).suffix.casefold().lstrip(".")
    attachment = src.get("attachment") or {}
    content_type = str(attachment.get("content_type") or "").split(";", 1)[0].strip().casefold()
    text = str(src.get("content") or "").strip()

    include_extensions = _config_string_set(cfg, "sync.document_filter.include_extensions", [])
    exclude_extensions = _config_string_set(
        cfg, "sync.document_filter.exclude_extensions", DEFAULT_SEMANTIC_EXCLUDE_EXTENSIONS
    )
    exclude_mime_types = _config_string_set(
        cfg, "sync.document_filter.exclude_mime_types", DEFAULT_SEMANTIC_EXCLUDE_MIME_TYPES
    )
    raw_prefixes = cfg_get(
        cfg, "sync.document_filter.exclude_mime_prefixes",
        default=list(DEFAULT_SEMANTIC_EXCLUDE_MIME_PREFIXES),
    ) or []
    if isinstance(raw_prefixes, str):
        raw_prefixes = [raw_prefixes]
    exclude_mime_prefixes = tuple(str(x).strip().casefold() for x in raw_prefixes if str(x).strip())
    min_text_chars = max(0, int(cfg_get(cfg, "sync.document_filter.min_text_chars", default=0) or 0))

    lower_title = title.casefold()
    # Hidden sidecars are already caught before this function, but also reject
    # non-hidden variants such as foo.mailmeta.json. These are machine metadata
    # for the canonical mail document, never a standalone semantic source.
    if lower_title.endswith(".mailmeta.json"):
        return False, "machine_mailmeta"

    if include_extensions and suffix not in include_extensions:
        return False, f"extension_not_allowed:{suffix or '<none>'}"
    if suffix and suffix in exclude_extensions:
        return False, f"excluded_extension:{suffix}"

    # MIME is deliberately secondary to a known text/document extension. This
    # fixes Nextcloud/Elasticsearch cases where normalized *.txt mail exports are
    # still labelled message/rfc822. For extensionless or unknown objects MIME
    # remains a useful safety fallback for raw mail/archive containers.
    mime_can_veto = not suffix or suffix not in KNOWN_TEXT_DOCUMENT_EXTENSIONS
    if mime_can_veto and content_type and content_type in exclude_mime_types:
        return False, f"excluded_mime:{content_type}"
    if mime_can_veto and content_type and any(content_type.startswith(prefix) for prefix in exclude_mime_prefixes):
        return False, f"excluded_mime_prefix:{content_type}"
    if min_text_chars and len(text) < min_text_chars:
        return False, f"text_too_short:{len(text)}<{min_text_chars}"
    return True, "allowed"


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be a mapping")
    from rag.tls_compat import configure_tls_compat
    configure_tls_compat(cfg)
    return cfg


def cfg_get(cfg: dict[str, Any], *paths: str, default=None):
    """Read first matching dotted config path."""
    for path in paths:
        cur: Any = cfg
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            return cur
    return default


def resolve_sync_state_path(cfg: dict[str, Any]) -> str:
    """Resolve the persistent sync state path, protecting Dockerized upgrades.

    Older configs may still carry a top-level state_db: state.sqlite.
    In Dockerized mode that historical default points into the ephemeral
    container filesystem, so treat it as the old default and use the current
    persistent runtime mount instead.
    """
    canonical = cfg_get(cfg, "sync.state_db", default=None)
    if canonical not in (None, ""):
        return str(canonical)

    legacy = cfg_get(cfg, "state_db", default=None)
    if legacy not in (None, ""):
        value = str(legacy)
        deployment_mode = str(
            cfg_get(cfg, "deployment.mode", default="native") or "native"
        ).strip().lower()
        if deployment_mode == "dockerized" and value.strip() == "state.sqlite":
            return "runtime/state.sqlite"
        return value

    return "runtime/state.sqlite"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_index_signature(
    *,
    embedding_backend: str,
    embedding_model: str,
    embedding_profile: str,
    embedding_document_prefix: str,
    chunk_size: int,
    chunk_overlap: int,
    embedding_dimensions: int | None = None,
    embedding_include_basename: bool = False,
) -> str:
    """Stable fingerprint of settings that affect Qdrant vector contents."""
    payload = {
        "sync_schema": SYNC_SCHEMA_VERSION,
        "chunker": CHUNKER_VERSION,
        "embedding_backend": str(embedding_backend).strip().lower(),
        "embedding_model": str(embedding_model).strip(),
        "embedding_document_prefix": str(embedding_document_prefix),
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(chunk_overlap),
        "embedding_dimensions": int(embedding_dimensions) if embedding_dimensions else None,
        "embedding_include_basename": bool(embedding_include_basename),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def graph_reindex_needed(old_state: tuple | None, *, content_unchanged: bool) -> bool:
    """Graph discovery depends on document content, not vector/index profile.

    A changed embedding model/document formatting/chunker may require rebuilding Qdrant while
    the underlying evidence text is identical.  Re-queueing those documents for
    entity/relation discovery would only burn LLM/GPU time.
    """
    return old_state is None or not content_unchanged


def content_hash(hit: dict[str, Any]) -> str:
    src = hit.get("_source") or {}
    es_hash = src.get("hash")
    if es_hash:
        return str(es_hash)
    raw = json.dumps(
        {
            "_id": hit.get("_id"),
            "title": src.get("title"),
            "content": src.get("content"),
            "attachment": src.get("attachment"),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 5)

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_size)
        if end < n:
            lower = max(start + chunk_size // 2, start)
            candidates = [
                text.rfind("\n\n", lower, end),
                text.rfind("\n", lower, end),
                text.rfind(". ", lower, end),
                text.rfind(" ", lower, end),
            ]
            split = max(candidates)
            if split > start:
                end = split + 1 if text[split:split + 2] == ". " else split
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(start + 1, end - overlap)
    return chunks



def _adaptive_embedding_split(text: str, overlap: int) -> list[str]:
    """Split one rejected embedding chunk while preserving useful overlap.

    The normal configured chunk size remains authoritative. This helper is only
    used after the embedding backend explicitly reports a context-length error.
    """
    text = str(text or "").strip()
    if len(text) < 2:
        return []
    target = max(256, len(text) // 2)
    adaptive_overlap = min(max(0, int(overlap or 0)), max(0, target // 4))
    parts = chunk_text(text, target, adaptive_overlap)
    if len(parts) >= 2 and all(part != text for part in parts):
        return parts
    # Last-resort deterministic split when no natural boundary was found.
    mid = len(text) // 2
    if mid <= 0 or mid >= len(text):
        return []
    ov = min(adaptive_overlap, mid // 2)
    left = text[: min(len(text), mid + ov)].strip()
    right = text[max(0, mid - ov):].strip()
    return [part for part in (left, right) if part and part != text]


def embedding_document_input(text: str, title: str, *, include_basename: bool) -> str:
    """Build the text sent to the embedding model without changing evidence payloads.

    The basename is useful semantic metadata for invoices, protocols and OCR-heavy
    files, while the stored Qdrant payload remains the raw chunk.
    """
    if not include_basename:
        return text
    basename = PurePosixPath(str(title or "")).name.strip()
    if not basename:
        return text
    return f"Document: {basename}\n\n{text}"


def embed_chunks_resilient(
    embedding_client,
    chunks: list[str],
    *,
    batch_size: int,
    document_id: str,
    title: str,
    overlap: int,
    include_basename: bool = False,
    min_split_chars: int = 256,
) -> list[tuple[str, list[float]]]:
    """Embed all chunks before Qdrant mutation, isolating context failures.

    A context-length error in a multi-item batch is first isolated by recursively
    splitting the batch. Only the offending text chunk is then split into smaller
    overlapping pieces. Other HTTP/backend failures remain fatal and are never
    silently converted into chunking changes.
    """
    batch_size = max(1, int(batch_size or 1))

    def embed_group(group: list[str], *, depth: int = 0) -> list[tuple[str, list[float]]]:
        if not group:
            return []
        try:
            embedding_inputs = [
                embedding_document_input(chunk, title, include_basename=include_basename)
                for chunk in group
            ]
            embed_documents = getattr(embedding_client, "embed_documents", None)
            vectors = (
                embed_documents(embedding_inputs)
                if callable(embed_documents)
                else embedding_client.embed(embedding_inputs)
            )
        except EmbeddingContextLengthError as exc:
            if len(group) > 1:
                middle = max(1, len(group) // 2)
                return embed_group(group[:middle], depth=depth) + embed_group(group[middle:], depth=depth)

            chunk = group[0]
            if len(chunk) <= min_split_chars:
                raise EmbeddingContextLengthError(
                    f"Embedding context overflow persisted for {document_id} at {len(chunk)} chars",
                    status_code=getattr(exc, "status_code", 400),
                    response_text=getattr(exc, "response_text", ""),
                ) from exc
            parts = _adaptive_embedding_split(chunk, overlap)
            if len(parts) < 2:
                raise EmbeddingContextLengthError(
                    f"Could not split embedding chunk for {document_id} after context overflow",
                    status_code=getattr(exc, "status_code", 400),
                    response_text=getattr(exc, "response_text", ""),
                ) from exc
            log.warning(
                "embedding context exceeded document=%s title=%r chars=%s depth=%s action=adaptive_split parts=%s",
                document_id, title, len(chunk), depth, len(parts),
            )
            return embed_group(parts, depth=depth + 1)

        if len(vectors) != len(group):
            raise RuntimeError(
                f"Embedding count mismatch for {document_id}: {len(vectors)} != {len(group)}"
            )
        return list(zip(group, vectors))

    result: list[tuple[str, list[float]]] = []
    for group in batched(chunks, batch_size):
        result.extend(embed_group(list(group)))
    return result

def append_embedding_deferred(path: str, payload: dict[str, Any]) -> None:
    """Append one failed document to the persistent embedding retry queue."""
    queue_path = Path(str(path or "embedding-deferred.jsonl"))
    if queue_path.parent and str(queue_path.parent) not in {"", "."}:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("deferred_at", utc_now())
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def nextcloud_meta(hit: dict[str, Any]) -> dict[str, Any]:
    src = hit.get("_source") or {}
    title = str(src.get("title") or "").strip()
    p = PurePosixPath(title) if title else PurePosixPath(".")
    filename = p.name if title else ""
    parent = str(p.parent) if title else ""
    directory = "/" + parent.lstrip("/") if parent not in ("", ".") else "/"

    es_id = str(hit.get("_id") or "")
    openfile_id = es_id.split(":", 1)[1] if es_id.startswith("files:") else None
    attachment = src.get("attachment") or {}

    return {
        "nextcloud_es_id": es_id or None,
        "nextcloud_openfile_id": openfile_id,
        "path": title or None,
        "directory": directory,
        "filename": filename or None,
        "owner": src.get("owner"),
        "users": src.get("users") or [],
        "groups": src.get("groups") or [],
        "circles": src.get("circles") or [],
        "share_names": src.get("share_names") or {},
        "nextcloud_source": src.get("source"),
        "provider": src.get("provider"),
        "document_date": attachment.get("date"),
        "content_type": attachment.get("content_type"),
        "language": attachment.get("language"),
        "tags": src.get("tags") or [],
        "metatags": src.get("metatags") or [],
        "source_hash": src.get("hash"),
        "source_origin": maybe_register_path_origin(es_id, title, classification_source="sync_path"),
    }


def normalize_scope_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def path_matches_scope(title: str, include_paths: Iterable[str], exclude_paths: Iterable[str]) -> bool:
    title_norm = normalize_scope_path(title)
    includes = [normalize_scope_path(x) for x in include_paths if normalize_scope_path(x)]
    excludes = [normalize_scope_path(x) for x in exclude_paths if normalize_scope_path(x)]

    def under(path: str, root: str) -> bool:
        return path == root or path.startswith(root + "/")

    if includes and not any(under(title_norm, root) for root in includes):
        return False
    if any(under(title_norm, root) for root in excludes):
        return False
    return True


def config_path_list(cfg: dict[str, Any], path: str) -> list[str]:
    value = cfg_get(cfg, path, default=[]) or []
    if isinstance(value, str):
        value = [value]
    return [str(x) for x in value if str(x or "").strip()]


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    if target.parent and str(target.parent) not in {"", "."}:
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{__import__('os').getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


class StateDB:
    def __init__(self, path: str):
        db_path = Path(path)
        if db_path.parent and str(db_path.parent) not in {"", "."}:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                title TEXT,
                updated_at TEXT NOT NULL,
                index_signature TEXT
            )
            """
        )
        self._ensure_column("documents", "index_signature", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_type: str):
        columns = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def get(self, document_id: str):
        return self.conn.execute(
            "SELECT content_hash, chunk_count, title, index_signature FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()

    def put(
        self,
        document_id: str,
        digest: str,
        chunk_count: int,
        title: str | None,
        index_signature: str,
    ):
        self.conn.execute(
            """
            INSERT INTO documents(document_id, content_hash, chunk_count, title, updated_at, index_signature)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                chunk_count=excluded.chunk_count,
                title=excluded.title,
                updated_at=excluded.updated_at,
                index_signature=excluded.index_signature
            """,
            (document_id, digest, chunk_count, title, utc_now(), index_signature),
        )
        self.conn.commit()

    def ids(self) -> set[str]:
        return {row[0] for row in self.conn.execute("SELECT document_id FROM documents")}

    def stats(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN chunk_count > 0 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN chunk_count = 0 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(chunk_count), 0)
            FROM documents
            """
        ).fetchone()
        return {
            "documents": int((row or (0, 0, 0, 0))[0] or 0),
            "vectorized_documents": int((row or (0, 0, 0, 0))[1] or 0),
            "empty_documents": int((row or (0, 0, 0, 0))[2] or 0),
            "chunks": int((row or (0, 0, 0, 0))[3] or 0),
        }

    def delete(self, document_id: str):
        self.conn.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()


def es_scroll(es_url: str, index: str, page_size: int, scroll: str,
              request_options: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
    request_options = dict(request_options or {})
    r = requests.post(
        f"{es_url.rstrip('/')}/{index}/_search",
        params={"scroll": scroll},
        json={"size": page_size, "sort": ["_doc"], "_source": True, "query": {"match_all": {}}},
        timeout=120, **request_options,
    )
    r.raise_for_status()
    data = r.json()
    scroll_id = data.get("_scroll_id")
    try:
        while True:
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            yield from hits
            r = requests.post(
                f"{es_url.rstrip('/')}/_search/scroll",
                json={"scroll": scroll, "scroll_id": scroll_id},
                timeout=120, **request_options,
            )
            r.raise_for_status()
            data = r.json()
            scroll_id = data.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            try:
                requests.delete(
                    f"{es_url.rstrip('/')}/_search/scroll",
                    json={"scroll_id": [scroll_id]},
                    timeout=30, **request_options,
                )
            except requests.RequestException:
                pass


def qdrant_collection_exists(qdrant_url: str, collection: str) -> bool:
    r = requests.get(f"{qdrant_url.rstrip('/')}/collections/{collection}", timeout=30)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def ensure_qdrant_collection(qdrant_url: str, collection: str, vector_size: int):
    endpoint = f"{qdrant_url.rstrip('/')}/collections/{collection}"
    r = requests.get(endpoint, timeout=30)
    if r.status_code == 404:
        created = requests.put(
            endpoint,
            json={"vectors": {"size": vector_size, "distance": "Cosine"}},
            timeout=60,
        )
        created.raise_for_status()
        return
    r.raise_for_status()

    # A changed embedding model may use a different vector dimension. Qdrant
    # cannot mix dimensions in an existing unnamed-vector collection. Fail
    # before an upsert rather than leaving a half-migrated collection.
    data = r.json()
    vectors_cfg = (
        (((data.get("result") or {}).get("config") or {}).get("params") or {}).get("vectors")
    )
    current_size = None
    if isinstance(vectors_cfg, dict) and "size" in vectors_cfg:
        try:
            current_size = int(vectors_cfg.get("size"))
        except (TypeError, ValueError):
            current_size = None
    if current_size is not None and current_size != int(vector_size):
        raise RuntimeError(
            f"Qdrant collection {collection!r} has vector size {current_size}, "
            f"but the configured embedding backend returned {vector_size}. "
            "Use a new collection or reset/recreate the vector collection before reindexing."
        )


def point_id(document_id: str, chunk_no: int) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, f"nextcloud-rag:{document_id}:{chunk_no}"))


QDRANT_DELETE_BATCH_SIZE = 256


def qdrant_delete_chunk_range(
    qdrant_url: str,
    collection: str,
    document_id: str,
    start_chunk: int,
    end_chunk: int,
    *,
    batch_size: int = QDRANT_DELETE_BATCH_SIZE,
) -> int:
    """Delete deterministic chunk point IDs without Qdrant payload filters.

    Qdrant point IDs are UUIDv5(document_id, chunk_no), so the sync already knows
    every point belonging to a previously committed document from ``chunk_count``
    in StateDB. Deleting explicit IDs avoids the more expensive payload-filter
    resolver and keeps lifecycle mutations deterministic.

    ``end_chunk`` is exclusive. A missing collection is treated as already clean.
    """
    start = max(0, int(start_chunk or 0))
    end = max(start, int(end_chunk or 0))
    size = max(1, int(batch_size or QDRANT_DELETE_BATCH_SIZE))
    if end <= start:
        return 0

    endpoint = f"{qdrant_url.rstrip('/')}/collections/{collection}/points/delete"
    deleted = 0
    for offset in range(start, end, size):
        stop = min(end, offset + size)
        ids = [point_id(document_id, chunk_no) for chunk_no in range(offset, stop)]
        r = requests.post(
            endpoint,
            json={"points": ids},
            params={"wait": "true"},
            timeout=120,
        )
        if r.status_code == 404:
            return deleted
        if not r.ok:
            body = (r.text or "").strip().replace("\n", " ")[:1000]
            raise RuntimeError(
                f"Qdrant explicit-id delete failed for {document_id} "
                f"chunks={offset}:{stop} status={r.status_code} body={body!r}"
            )
        deleted += len(ids)
    return deleted


def _state_chunk_count(old_state: tuple | None) -> int:
    if not old_state or len(old_state) < 2:
        return 0
    try:
        return max(0, int(old_state[1] or 0))
    except (TypeError, ValueError):
        return 0


def qdrant_upsert(qdrant_url: str, collection: str, points: list[dict[str, Any]]):
    if not points:
        return
    r = requests.put(
        f"{qdrant_url.rstrip('/')}/collections/{collection}/points",
        params={"wait": "true"}, json={"points": points}, timeout=300,
    )
    r.raise_for_status()


def batched(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--include-path", action="append", default=None,
                    help="Only sync this Nextcloud path prefix; repeatable. CLI overrides sync.include_paths")
    ap.add_argument("--exclude-path", action="append", default=None,
                    help="Exclude this Nextcloud path prefix; repeatable. CLI overrides sync.exclude_paths")
    ap.add_argument("--max-documents", type=int, default=None,
                    help="Override sync.max_documents for this run; 0=unlimited")
    ap.add_argument("--dry-run", action="store_true", help="Scan scope and state only; do not change Qdrant/state/queue")
    ap.add_argument(
        "--embedding-url",
        default=None,
        help="One-run override for embedding.url (useful for a GPU-assisted initial sync)",
    )
    ap.add_argument("--log-level", default=None, help="override SYNC_LOG_LEVEL/LOG_LEVEL")
    ap.add_argument(
        "--runtime-control-worker",
        default="",
        help="Internal worker name for cooperative pause checks; empty disables runtime-control checks",
    )
    ap.add_argument(
        "--pause-check-documents",
        type=int,
        default=0,
        help="Check runtime worker control after each N scanned documents; 0 disables",
    )
    graph_group = ap.add_mutually_exclusive_group()
    graph_group.add_argument("--enqueue-graph", action="store_true", help="Queue new/changed synced documents for background graph discovery")
    graph_group.add_argument("--no-enqueue-graph", action="store_true", help="Disable graph enqueue for this run")
    args = ap.parse_args()
    if args.log_level:
        log.setLevel(getattr(logging, str(args.log_level).upper(), log.level))
    runtime_control_worker = str(args.runtime_control_worker or "").strip()
    pause_check_documents = max(0, int(args.pause_check_documents or 0))
    cfg = load_config(args.config)
    if args.embedding_url:
        embedding_cfg = cfg.setdefault("embedding", {})
        if not isinstance(embedding_cfg, dict):
            raise RuntimeError("embedding configuration must be a mapping")
        embedding_cfg["url"] = str(args.embedding_url).rstrip("/")

    es_url = str(cfg_get(cfg, "elasticsearch.url", "es.url", "elasticsearch_url", default="http://127.0.0.1:9200"))
    es_index = str(cfg_get(cfg, "elasticsearch.index", "es.index", "elasticsearch_index", default="my_index"))
    es_request_options = elastic_requests_options(cfg)
    es_page_size = int(cfg_get(cfg, "elasticsearch.page_size", "es.page_size", default=50))
    es_scroll_ttl = str(cfg_get(cfg, "elasticsearch.scroll", "es.scroll", default="5m"))

    qdrant_url = str(cfg_get(cfg, "qdrant.url", "qdrant_url", default="http://127.0.0.1:6333"))
    collection = str(cfg_get(cfg, "qdrant.collection", "qdrant.collection_name", "qdrant_collection", default="nextcloud_rag"))
    embedding_client = build_embedding_backend_from_config(cfg)
    embedding_backend = embedding_client.kind
    embedding_model = embedding_client.model
    embedding_profile = str(getattr(embedding_client, "profile", "plain") or "plain")
    embedding_document_prefix = str(getattr(embedding_client, "document_prefix", "") or "")
    embedding_dimensions = getattr(embedding_client, "dimensions", None)

    chunk_size = int(cfg_get(cfg, "sync.chunk_size", "chunk_size", default=700))
    chunk_overlap = int(cfg_get(cfg, "sync.chunk_overlap", "chunk_overlap", default=100))
    embedding_batch_size = int(cfg_get(cfg, "sync.embedding_batch_size", "embedding_batch_size", default=8))
    embedding_include_basename = bool(cfg_get(cfg, "sync.embedding_include_basename", default=True))
    max_documents = int(cfg_get(cfg, "sync.max_documents", "max_documents", default=500))
    if args.max_documents is not None:
        max_documents = max(0, int(args.max_documents))
    state_path = resolve_sync_state_path(cfg)
    if (
        cfg_get(cfg, "sync.state_db", default=None) in (None, "")
        and str(cfg_get(cfg, "state_db", default="") or "").strip() == "state.sqlite"
        and str(cfg_get(cfg, "deployment.mode", default="native") or "native").strip().lower() == "dockerized"
    ):
        log.warning(
            "Ignoring legacy dockerized state_db=state.sqlite because it would be ephemeral; "
            "using runtime/state.sqlite"
        )
    embedding_deferred_file = str(
        cfg_get(cfg, "sync.embedding_deferred_file", default="runtime/embedding-deferred.jsonl")
        or "runtime/embedding-deferred.jsonl"
    )
    status_file = str(
        cfg_get(cfg, "sync.status_file", default="runtime/sync-status.json")
        or "runtime/sync-status.json"
    )
    index_signature = build_index_signature(
        embedding_backend=embedding_backend,
        embedding_model=embedding_model,
        embedding_profile=embedding_profile,
        embedding_document_prefix=embedding_document_prefix,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        embedding_dimensions=embedding_dimensions,
        embedding_include_basename=embedding_include_basename,
    )

    include_paths = (list(args.include_path) if args.include_path is not None else config_path_list(cfg, "sync.include_paths"))
    exclude_paths = (list(args.exclude_path) if args.exclude_path is not None else config_path_list(cfg, "sync.exclude_paths"))
    scoped_run = bool(include_paths or exclude_paths)

    graph_enqueue_enabled = bool(cfg_get(cfg, "sync.graph_queue.enabled", default=False))
    if args.enqueue_graph:
        graph_enqueue_enabled = True
    if args.no_enqueue_graph:
        graph_enqueue_enabled = False
    graph_priority = str(cfg_get(cfg, "sync.graph_queue.priority", default="background") or "background")
    graph_queue = None
    if graph_enqueue_enabled and not args.dry_run:
        from rag.graph_queue import GraphQueue
        graph_queue = GraphQueue(cfg)

    log.info(
        "Index signature: %s (backend=%s model=%s profile=%s dimensions=%s chunker=%s chunk=%s/%s basename=%s graph_enqueue=%s)",
        index_signature[:16], embedding_backend, embedding_model, embedding_profile, embedding_dimensions, CHUNKER_VERSION,
        chunk_size, chunk_overlap, embedding_include_basename, graph_enqueue_enabled,
    )
    state = StateDB(state_path)
    seen: set[str] = set()
    scanned = in_scope = out_of_scope = excluded_semantic = 0
    new = updated = unchanged = empty = would_change = embedding_deferred = 0
    graph_queued = graph_coalesced = 0
    source_origin_mirror_updates: list[tuple[str, str]] = []
    source_origin_mirrored = 0
    runtime_paused = False

    def pause_requested() -> bool:
        return bool(
            runtime_control_worker
            and pause_check_documents > 0
            and not runtime_worker_enabled(
                runtime_control_worker,
                config_path=args.config,
                default=False,
            )
        )

    try:
        for hit in es_scroll(es_url, es_index, es_page_size, es_scroll_ttl, es_request_options):
            if (
                runtime_control_worker
                and pause_check_documents > 0
                and scanned > 0
                and scanned % pause_check_documents == 0
                and pause_requested()
            ):
                runtime_paused = True
                log.info(
                    "Runtime pause observed worker=%s after scanned=%d batch_size=%d; "
                    "stopping before next document",
                    runtime_control_worker,
                    scanned,
                    pause_check_documents,
                )
                break
            document_id = str(hit.get("_id") or "")
            if not document_id:
                continue
            scanned += 1
            src = hit.get("_source") or {}
            title = str(src.get("title") or document_id)
            source_origin = maybe_register_path_origin(document_id, title, classification_source="sync_path")
            if (not args.dry_run and source_origin in SPECIAL_ORIGINS
                    and str(src.get("source_origin") or "") != source_origin):
                source_origin_mirror_updates.append((document_id, source_origin))
                if len(source_origin_mirror_updates) >= 250:
                    source_origin_mirrored += mirror_updates_to_elasticsearch(
                        es_url, es_index, source_origin_mirror_updates, cfg
                    )
                    source_origin_mirror_updates.clear()

            # Scope is intentionally the first classification step.  A scoped
            # sync may still have to scroll the whole ES index, but out-of-scope
            # documents must not trigger semantic-filter logs or look like work
            # performed by this run.
            if not path_matches_scope(title, include_paths, exclude_paths):
                out_of_scope += 1
                continue
            in_scope += 1

            # Hidden mail sidecars are control-plane metadata, never semantic
            # documents.  Leaving them out here also prevents GraphQueue jobs.
            # On a complete unscoped run, any legacy Qdrant/state entry is
            # removed by the normal stale-document cleanup because the id is not
            # added to ``seen``.
            if is_machine_sidecar_path(title):
                excluded_semantic += 1
                log.info("filtered semantic document reason=machine_sidecar document=%s title=%s", document_id, title)
                continue
            semantic_allowed, semantic_reason = semantic_document_allowed(cfg, hit)
            if not semantic_allowed:
                excluded_semantic += 1
                log.info("filtered semantic document reason=%s document=%s title=%s", semantic_reason, document_id, title)
                continue
            seen.add(document_id)
            text = str(src.get("content") or "")
            digest = content_hash(hit)
            old = state.get(document_id)

            content_unchanged = bool(old and old[0] == digest)
            signature_unchanged = bool(old and len(old) >= 4 and old[3] == index_signature)
            if content_unchanged and signature_unchanged:
                unchanged += 1
                if max_documents and (new + updated + unchanged + empty + embedding_deferred) >= max_documents:
                    break
                continue

            if args.dry_run:
                would_change += 1
                reasons = []
                if not content_unchanged:
                    reasons.append("content")
                if not signature_unchanged:
                    reasons.append("index-signature")
                reason = ",".join(reasons) or "unknown"
                log.info("DRY-RUN would sync %s reason=%s title=%s", document_id, reason, title)
                if max_documents and (would_change + unchanged) >= max_documents:
                    break
                continue

            chunks = chunk_text(text, chunk_size, chunk_overlap)
            if not chunks:
                empty += 1
                old_chunk_count = _state_chunk_count(old)
                if old_chunk_count:
                    qdrant_delete_chunk_range(
                        qdrant_url, collection, document_id, 0, old_chunk_count
                    )
                state.put(document_id, digest, 0, title, index_signature)
                if max_documents and (new + updated + unchanged + empty + embedding_deferred) >= max_documents:
                    break
                continue

            meta = nextcloud_meta(hit)
            # Transaction-like ordering for one document: do not delete the old
            # Qdrant representation until every new embedding has succeeded.
            try:
                embedded = embed_chunks_resilient(
                    embedding_client,
                    chunks,
                    batch_size=embedding_batch_size,
                    document_id=document_id,
                    title=title,
                    overlap=chunk_overlap,
                    include_basename=embedding_include_basename,
                )
            except EmbeddingContextLengthError as exc:
                embedding_deferred += 1
                append_embedding_deferred(
                    embedding_deferred_file,
                    {
                        "document_id": document_id,
                        "title": title,
                        "reason": "context_length",
                        "status_code": getattr(exc, "status_code", None),
                        "error": str(exc),
                        "text_chars": len(text),
                        "configured_chunks": len(chunks),
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "embedding_backend": embedding_backend,
                        "embedding_model": embedding_model,
                        "embedding_profile": embedding_profile,
                        "embedding_dimensions": embedding_dimensions,
                        "embedding_include_basename": embedding_include_basename,
                    },
                )
                log.warning(
                    "embedding deferred document=%s title=%r reason=context_length queue=%s; existing Qdrant/state left unchanged",
                    document_id, title, embedding_deferred_file,
                )
                if max_documents and (new + updated + unchanged + empty + embedding_deferred) >= max_documents:
                    break
                continue
            if embedded:
                ensure_qdrant_collection(qdrant_url, collection, len(embedded[0][1]))

            final_chunk_count = len(embedded)
            adaptive_split = final_chunk_count != len(chunks)
            old_chunk_count = _state_chunk_count(old)

            # Stable UUIDv5 point IDs make a pre-delete unnecessary. Upsert the
            # new representation first; existing chunk IDs are overwritten in
            # place. Only a shrinking document needs explicit cleanup afterward.
            for chunk_offset in range(0, final_chunk_count, embedding_batch_size):
                points = []
                for chunk_no, (chunk, vector) in enumerate(
                    embedded[chunk_offset:chunk_offset + embedding_batch_size],
                    start=chunk_offset,
                ):
                    payload = {
                        "document_id": document_id,
                        "chunk_index": chunk_no,
                        "chunk_count": final_chunk_count,
                        "title": title,
                        "text": chunk,
                        "index_signature": index_signature,
                        "sync_schema_version": SYNC_SCHEMA_VERSION,
                        "chunker_version": CHUNKER_VERSION,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "adaptive_embedding_split": adaptive_split,
                        "actual_chunk_chars": len(chunk),
                        "embedding_backend": embedding_backend,
                        "embedding_model": embedding_model,
                        "embedding_profile": embedding_profile,
                        "embedding_dimensions": embedding_dimensions,
                        "embedding_include_basename": embedding_include_basename,
                        **meta,
                    }
                    points.append({"id": point_id(document_id, chunk_no), "vector": vector, "payload": payload})
                qdrant_upsert(qdrant_url, collection, points)

            if old_chunk_count > final_chunk_count:
                qdrant_delete_chunk_range(
                    qdrant_url,
                    collection,
                    document_id,
                    final_chunk_count,
                    old_chunk_count,
                )

            state.put(document_id, digest, final_chunk_count, title, index_signature)
            # Graph ingestion is intentionally origin-agnostic: archived web sources
            # may contain valuable current/historical entity and relation observations.
            # Separation from ordinary internal evidence retrieval is enforced later by
            # source_origin/path filters, not by suppressing graph discovery here.
            if graph_queue is not None and graph_reindex_needed(old, content_unchanged=content_unchanged):
                queued = graph_queue.enqueue_evidence({
                    "query_id": "sync",
                    "user_query": "",
                    "retrieval_query": "",
                    "evidence_action": "sync_background",
                    "queue_priority": graph_priority,
                    "entity_discovery": True,
                    "force_reindex": False,
                    "count_evidence": False,
                    "documents": [{
                        "document_id": document_id,
                        "title": title,
                        "path": meta.get("path") or title,
                        "source_origin": meta.get("source_origin"),
                        "text_chars": len(text),
                        "sync_chunk_count": len(chunks),
                        "file_bytes": (((src.get("attachment") or {}).get("content_length")) or ((src.get("attachment") or {}).get("size")) or src.get("size") or 0),
                    }],
                })
                graph_queued += int(queued.get("queued") or 0)
                graph_coalesced += int(queued.get("coalesced") or 0)
            if old:
                updated += 1
            else:
                new += 1
            log.info("document=%s chunks=%d openfile=%s title=%s", document_id, len(chunks), meta.get("nextcloud_openfile_id"), title)

            if max_documents and (new + updated + unchanged + empty + embedding_deferred) >= max_documents:
                break

        if source_origin_mirror_updates and not args.dry_run:
            source_origin_mirrored += mirror_updates_to_elasticsearch(
                es_url, es_index, source_origin_mirror_updates, cfg
            )
            source_origin_mirror_updates.clear()

        # Re-check once after traversal so an operator pause can also suppress a
        # potentially long stale-delete phase. A cooperatively paused run is not
        # complete and must never infer that unseen documents vanished.
        if (
            not runtime_paused
            and runtime_control_worker
            and pause_check_documents > 0
            and pause_requested()
        ):
            runtime_paused = True
            log.info(
                "Runtime pause observed worker=%s after traversal; stale cleanup skipped",
                runtime_control_worker,
            )

        # Only a genuinely complete, unscoped ES traversal can safely identify deletions.
        # A path-scoped, bounded or cooperatively paused run must never infer that
        # unseen documents vanished.
        if max_documents == 0 and not scoped_run and not args.dry_run and not runtime_paused:
            stale = state.ids() - seen
            for document_id in sorted(stale):
                old = state.get(document_id)
                old_chunk_count = _state_chunk_count(old)
                log.info(
                    "delete stale document=%s chunks=%d",
                    document_id, old_chunk_count,
                )
                if old_chunk_count:
                    qdrant_delete_chunk_range(
                        qdrant_url, collection, document_id, 0, old_chunk_count
                    )
                state.delete(document_id)

        if not args.dry_run:
            state_stats = state.stats()
            _atomic_write_json(
                status_file,
                {
                    "schema": "sunaq.sync_status.v1",
                    "completed_at": utc_now(),
                    "complete_full_scan": bool(max_documents == 0 and not scoped_run and not runtime_paused),
                    "runtime_paused": bool(runtime_paused),
                    "scan": {
                        "scanned": scanned,
                        "in_scope": in_scope,
                        "eligible_seen": len(seen),
                        "excluded_semantic": excluded_semantic,
                        "out_of_scope": out_of_scope,
                        "embedding_deferred": embedding_deferred,
                        "new": new,
                        "updated": updated,
                        "unchanged": unchanged,
                        "empty": empty,
                    },
                    "state": state_stats,
                    "scope": {
                        "include_paths": include_paths,
                        "exclude_paths": exclude_paths,
                        "max_documents": max_documents,
                    },
                    "embedding": {
                        "backend": embedding_backend,
                        "model": embedding_model,
                        "dimensions": embedding_dimensions,
                        "index_signature": index_signature,
                    },
                },
            )

        log.info(
            "Done: scanned=%d in_scope=%d new=%d updated=%d unchanged=%d empty=%d embedding_deferred=%d excluded_semantic=%d out_of_scope=%d graph_queued=%d graph_coalesced=%d source_origin_mirrored=%d",
            scanned, in_scope, new, updated, unchanged, empty, embedding_deferred, excluded_semantic, out_of_scope, graph_queued, graph_coalesced, source_origin_mirrored,
        )
        if scoped_run:
            log.info("Scope: include=%s exclude=%s; deletion cleanup outside scope disabled", include_paths or ["<all>"], exclude_paths or [])
        elif max_documents != 0:
            log.info("max_documents != 0; deletion cleanup not run")
        elif runtime_paused:
            log.info("Runtime pause: traversal/cleanup incomplete; stale deletion cleanup not run")
        if args.dry_run:
            log.info("Dry-run: would_change=%d; no Qdrant/state/GraphQueue mutation", would_change)
        return 0
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
