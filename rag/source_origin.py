"""Source-origin classification and source-scope policy.

The default internal RAG pool deliberately contains ordinary Nextcloud files
only. Mail and saved SunaQ research/chat dossiers are explicit opt-in source
scopes. Persisted public-web evidence belongs to the chat dossier lifecycle and
is therefore retrieved through chatarchive. Explicit source scopes remain
orthogonal to retrieval engines: the same policy is applied to Elasticsearch,
Qdrant and Graph candidates.
"""
from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path, PurePosixPath
import sqlite3
from typing import Any, Iterable

from rag.source_registry import SPECIAL_ORIGINS, registered_origin, register_document

import yaml

log = logging.getLogger("rag-source-origin")
BASE_DIR = Path(__file__).resolve().parent.parent
ALLOWED_SOURCE_SCOPES = frozenset({"documents", "mailarchive", "chatarchive"})
LEGACY_SOURCE_SCOPE_ALIASES = {"webarchive": "chatarchive"}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


@lru_cache(maxsize=1)
def _load_web_yaml() -> dict[str, Any]:
    path = BASE_DIR / "web.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("web.yaml could not be read for source-origin policy: %s", exc)
        return {}


@lru_cache(maxsize=1)
def _load_app_yaml() -> dict[str, Any]:
    path = BASE_DIR / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("config.yaml unavailable for source-origin policy: %s", exc)
        return {}


@lru_cache(maxsize=1)
def web_archive_root() -> str:
    cfg = _load_web_yaml()
    archive = cfg.get("archive", {}) or {}
    return normalize_path(str(archive.get("root") or "Webarchiv")) or "Webarchiv"


@lru_cache(maxsize=1)
def chat_archive_root() -> str:
    cfg = _load_app_yaml()
    chat = cfg.get("chat_archive", {}) or {}
    return normalize_path(str(chat.get("root") or "SunaQ-Chats")) or "SunaQ-Chats"


@lru_cache(maxsize=1)
def exclude_web_archive_from_internal() -> bool:
    cfg = _load_web_yaml()
    archive = cfg.get("archive", {}) or {}
    return _truthy(archive.get("exclude_from_internal_retrieval"), True)


def _credential_store_path() -> Path:
    cfg = _load_app_yaml()
    auth = cfg.get("auth") or {}
    acl = cfg.get("acl") or {}
    raw = str(auth.get("credential_store") or acl.get("credential_store") or "runtime/users.sqlite").strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _db_stamp(path: Path) -> tuple[int, ...] | None:
    """Return a cache stamp that also notices SQLite WAL changes.

    The credential store normally uses the default journal mode, but deployments
    may switch to WAL.  Archive-root discovery must then not keep a stale empty
    cache merely because the main database file has not been checkpointed yet.
    """
    values: list[int] = []
    found = False
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            values.extend((stat.st_mtime_ns, stat.st_size))
            found = True
        except OSError:
            values.extend((0, 0))
    return tuple(values) if found else None


@lru_cache(maxsize=16)
def _user_archive_roots_for_stamp(db_path: str, stamp: tuple[int, ...] | None) -> tuple[str, ...]:
    del stamp
    path = Path(db_path)
    if not path.exists():
        return ()
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            history_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_archive_roots'"
            ).fetchone()
            if history_exists:
                rows = con.execute(
                    "SELECT target_path FROM web_archive_roots WHERE target_path<>''"
                ).fetchall()
            else:
                settings_exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_web_settings'"
                ).fetchone()
                if not settings_exists:
                    return ()
                rows = con.execute(
                    "SELECT target_path FROM user_web_settings WHERE archive_enabled=1 AND target_path<>''"
                ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        log.debug("per-user web archive roots unavailable: %s", exc)
        return ()
    roots = {normalize_path(str(row[0] or "")) for row in rows}
    roots.discard("")
    return tuple(sorted(roots, key=str.casefold))


@lru_cache(maxsize=16)
def _chat_archive_roots_for_stamp(db_path: str, stamp: tuple[int, ...] | None) -> tuple[str, ...]:
    """Return configured chat archive roots as a conservative path fallback.

    Saved SunaQ chats register their concrete file IDs as `chat_archive`, which
    is authoritative. The path roots keep a failed/late registration from
    exposing a configured archive as ordinary implicit document evidence.
    """
    del stamp
    path = Path(db_path)
    if not path.exists():
        return ()
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            rows: list[tuple[Any, ...]] = []
            history_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_archive_roots'"
            ).fetchone()
            if history_exists:
                rows.extend(
                    con.execute(
                        "SELECT target_path FROM chat_archive_roots WHERE target_path<>''"
                    ).fetchall()
                )
            settings_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_chat_settings'"
            ).fetchone()
            if settings_exists:
                rows.extend(
                    con.execute(
                        "SELECT target_path FROM user_chat_settings WHERE target_path<>''"
                    ).fetchall()
                )
            if not rows:
                return ()
        finally:
            con.close()
    except Exception as exc:
        log.debug("per-user chat archive roots unavailable: %s", exc)
        return ()
    roots = {normalize_path(str(row[0] or "")) for row in rows}
    roots.discard("")
    return tuple(sorted(roots, key=str.casefold))


@lru_cache(maxsize=16)
def _mail_archive_roots_for_stamp(db_path: str, stamp: tuple[int, ...] | None) -> tuple[str, ...]:
    """Return every known mail-archive root, including historical roots.

    Long-lived installations may have a mail_accounts table from before
    eml_target_path existed.  Read its actual schema instead of assuming the
    newest column set.  A separate history table preserves roots when an account
    is later renamed or removed.
    """
    del stamp
    path = Path(db_path)
    if not path.exists():
        return ()
    roots: set[str] = set()
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            history_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_archive_roots'"
            ).fetchone()
            if history_exists:
                for row in con.execute(
                    "SELECT target_path FROM mail_archive_roots WHERE target_path<>''"
                ).fetchall():
                    value = normalize_path(str(row[0] or ""))
                    if value:
                        roots.add(value)

            accounts_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_accounts'"
            ).fetchone()
            if accounts_exists:
                columns = {str(row[1]) for row in con.execute("PRAGMA table_info(mail_accounts)").fetchall()}
                select_columns = [name for name in ("target_path", "eml_target_path") if name in columns]
                if select_columns:
                    query = "SELECT " + ",".join(select_columns) + " FROM mail_accounts"
                    for row in con.execute(query).fetchall():
                        for raw in row:
                            value = normalize_path(str(raw or ""))
                            if value:
                                roots.add(value)
        finally:
            con.close()
    except Exception as exc:
        log.warning("mail archive roots unavailable from %s: %s", path, exc)
        return tuple(sorted(roots, key=str.casefold))
    return tuple(sorted(roots, key=str.casefold))


def _user_archive_roots() -> tuple[str, ...]:
    path = _credential_store_path()
    return _user_archive_roots_for_stamp(str(path), _db_stamp(path))


def mail_archive_roots() -> tuple[str, ...]:
    path = _credential_store_path()
    return _mail_archive_roots_for_stamp(str(path), _db_stamp(path))


def _configured_chat_archive_roots() -> tuple[str, ...]:
    path = _credential_store_path()
    return _chat_archive_roots_for_stamp(str(path), _db_stamp(path))


def web_archive_roots() -> tuple[str, ...]:
    roots = {web_archive_root(), *_user_archive_roots()}
    roots.discard("")
    return tuple(sorted(roots, key=str.casefold))


def chat_archive_roots() -> tuple[str, ...]:
    root = chat_archive_root()
    roots = {root, *_configured_chat_archive_roots()} if root else set(_configured_chat_archive_roots())
    # Conservative compatibility fallback for pre-setting RC archives. This is
    # source classification only; the Nextcloud app never scans two archive
    # folders in parallel.
    roots.add("AKI-Chats")
    roots.discard("")
    return tuple(sorted(roots, key=str.casefold))


def internal_exclude_paths() -> list[str]:
    """Roots excluded from ordinary implicit document retrieval.

    All archive sources are opt-in. Mail is deliberately explicit as well, so
    generic OpenAI-compatible clients have one simple provider-side default.
    """
    roots: set[str] = set(chat_archive_roots())
    roots.update(mail_archive_roots())
    # Historical Webarchiv roots are now part of the Chatarchive source class
    # and must never fall back into ordinary document retrieval.
    roots.update(web_archive_roots())
    roots.discard("")
    return sorted(roots, key=str.casefold)


def path_is_under(path: str, root: str) -> bool:
    p = normalize_path(path).casefold()
    r = normalize_path(root).casefold()
    return bool(r) and (p == r or p.startswith(r + "/"))


def is_machine_sidecar_path(path: str) -> bool:
    """Hidden Nextcloud files are control/metadata space, not RAG evidence.

    SunaQ sidecars are deliberately dot-prefixed, but this rule is broader on
    purpose: any file whose basename begins with "." is excluded from normal
    document retrieval and derived semantic corpora.  Directory components are
    not considered here, so a normal document below a dot-directory is not
    rejected merely because of its parent path.
    """
    name = PurePosixPath(normalize_path(path)).name.casefold()
    return bool(name.startswith("."))


def normalize_source_scopes(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    scopes = {
        LEGACY_SOURCE_SCOPE_ALIASES.get(
            str(value or "").strip().casefold(),
            str(value or "").strip().casefold(),
        )
        for value in values
        if str(value or "").strip()
    }
    invalid = scopes - ALLOWED_SOURCE_SCOPES
    if invalid:
        raise ValueError("Unbekannte Quellenbereiche: " + ", ".join(sorted(invalid)))
    return scopes or None



def source_origins_for_scopes(scopes: Iterable[str] | None) -> set[str] | None:
    normalized = normalize_source_scopes(scopes)
    if normalized is None:
        return None
    origins: set[str] = set()
    if "documents" in normalized:
        origins.add("internal")
    if "mailarchive" in normalized:
        origins.add("mail_archive")
    if "chatarchive" in normalized:
        # web_archive remains a legacy provenance value for snapshots created
        # before Web Evidence moved into the chat dossier lifecycle.
        origins.update({"chat_archive", "web_archive"})
    return origins

def classify_source_origin(path: str, document_id: str | None = None) -> str:
    registered = registered_origin(str(document_id or "").strip()) if document_id else None
    if registered in SPECIAL_ORIGINS:
        return registered
    if is_machine_sidecar_path(path):
        return "machine_metadata"
    if any(path_is_under(path, root) for root in web_archive_roots()):
        return "web_archive"
    if any(path_is_under(path, root) for root in chat_archive_roots()):
        return "chat_archive"
    if any(path_is_under(path, root) for root in mail_archive_roots()):
        return "mail_archive"
    return "internal"


def source_scope_allows_record(document_id: str, path: str, scopes: Iterable[str] | None, indexed_origin: str | None = None) -> bool:
    if is_machine_sidecar_path(path):
        return False
    normalized = normalize_source_scopes(scopes)
    origin = str(indexed_origin or "").strip()
    if origin not in SPECIAL_ORIGINS:
        origin = classify_source_origin(path, document_id=document_id)
    if normalized is None:
        # Client-neutral implicit default: ordinary Nextcloud documents only.
        # Archive sources, including mail, require explicit opt-in.
        return origin == "internal"
    mapping = {
        "internal": "documents",
        "mail_archive": "mailarchive",
        # Legacy web_archive provenance is intentionally folded into Chats.
        "web_archive": "chatarchive",
        "chat_archive": "chatarchive",
    }
    scope = mapping.get(origin)
    return bool(scope and scope in normalized)


def source_scope_allows_path(path: str, scopes: Iterable[str] | None) -> bool:
    """Compatibility wrapper for callers without a stable document id."""
    return source_scope_allows_record("", path, scopes)


def maybe_register_path_origin(document_id: str, path: str, *, classification_source: str = "path") -> str:
    origin = classify_source_origin(path, document_id=document_id)
    if origin in SPECIAL_ORIGINS and document_id:
        register_document(document_id, origin, source_path=path, classification_source=classification_source)
    return origin


def is_internal_excluded_path(path: str) -> bool:
    return not source_scope_allows_path(path, None)
