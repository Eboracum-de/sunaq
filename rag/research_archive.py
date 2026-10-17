"""Server-side per-conversation research dossier writer.

The readable chat remains the dossier lifecycle anchor. Research runs are
written below the same admin-controlled Nextcloud chat-archive root using the
current user's WebDAV credential:

    <chat-root>/<conversation-id>/
        <date-title-id>.md      # readable chat, written by the NC client
        .conversation.json      # current client metadata
        runs/
            <run-id>/
                run.json
                trace.jsonl
                documents.json
                resolver/
                    request.json
                    response.json
                    <adapter>.json

The provider owns run/provenance writes. Resolver services never receive
Nextcloud credentials or archive paths.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
import xml.etree.ElementTree as ET

import httpx

from rag.acl import NextcloudCredential, NextcloudLiveAcl
from rag.credential_store import CredentialStore
from rag.logging_utils import get_logger
from rag.nextcloud_tls import nextcloud_verify_value
from rag.nextcloud_writer import NextcloudWriter
from rag.policy_hooks import PRE_PERSIST, apply_policy_hook

log = get_logger("provider")

_CONVERSATION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DAV_NS = "DAV:"
_OC_NS = "http://owncloud.org/ns"


@dataclass(frozen=True)
class ResearchRun:
    conversation_id: str
    run_id: str
    dossier_path: str
    run_path: str
    scoped_user_id: str
    created_at: str = ""


class ResearchArchive:
    def __init__(self, cfg: dict[str, Any], *, enabled: bool | None = None):
        self.cfg = cfg
        archive_cfg = cfg.get("chat_archive") or {}
        self.enabled = bool(
            archive_cfg.get("enabled", False)
            if enabled is None
            else enabled
        )
        self.default_root = str(archive_cfg.get("root") or "SunaQ-Chats").strip(" /")
        self.timeout = float(archive_cfg.get("timeout") or 20)
        self.acl = NextcloudLiveAcl(cfg)
        store_path = str(
            ((cfg.get("auth") or {}).get("credential_store"))
            or ((cfg.get("acl") or {}).get("credential_store"))
            or "runtime/users.sqlite"
        )
        self.store = CredentialStore(store_path)
        base = str((cfg.get("nextcloud") or {}).get("base_url") or "").strip().rstrip("/")
        self.dav_base = base + "/remote.php/dav/files" if base else ""
        self.verify = nextcloud_verify_value(cfg, "acl")
        self.nc_writer = NextcloudWriter(self.dav_base)

    @staticmethod
    def _clean_root(value: str) -> str:
        clean = str(value or "").strip(" /")
        if not clean or any(part in {"", ".", ".."} for part in clean.split("/")):
            raise ValueError("invalid research archive root")
        return clean

    @staticmethod
    def _conversation_key(value: str) -> str:
        clean = str(value or "").strip()
        if not _CONVERSATION_KEY_RE.fullmatch(clean):
            raise ValueError("conversation id is not safe for the dossier path")
        return clean

    @staticmethod
    def _run_id(request_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = _SAFE_FILE_RE.sub("-", str(request_id or "").strip()).strip(".-_")[:48]
        if not suffix:
            suffix = os.urandom(6).hex()
        return f"{stamp}-{suffix}"

    def _root_for_user(self, scoped_user_id: str) -> str:
        if not self.enabled:
            return ""
        identity = str(scoped_user_id or "").strip()
        if self.acl.identity_mode == "credential_store":
            if not identity:
                return ""
            canonical = self.store.get_canonical_user_for_identity(identity)
            if canonical is None or not canonical.enabled:
                return ""
            settings = self.store.get_chat_settings(canonical.canonical_user_id)
            if settings is not None:
                if not settings.enabled:
                    return ""
                return self._clean_root(settings.target_path or self.default_root)
        return self._clean_root(self.default_root)

    def _credential(self, scoped_user_id: str) -> NextcloudCredential:
        return self.acl.credential_for_user(scoped_user_id)

    def _writer(self) -> NextcloudWriter:
        writer = getattr(self, "nc_writer", None)
        if writer is None:
            writer = NextcloudWriter(self.dav_base)
            self.nc_writer = writer
        return writer

    def _url(self, credential: NextcloudCredential, relpath: str) -> str:
        return self._writer().url(credential, relpath)

    async def _dav_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        credential: NextcloudCredential,
        target: str,
        **kwargs: Any,
    ) -> httpx.Response:
        return await self._writer().request(
            client,
            method,
            credential,
            target,
            **kwargs,
        )

    async def _ensure_dir(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        relpath: str,
    ) -> None:
        await self._writer().ensure_dir(client, credential, relpath)
    async def _ensure_run_dir(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        dossier: str,
        run_path: str,
    ) -> None:
        """Create the per-run directory with a short common-case DAV path.

        The Nextcloud client normally created the conversation dossier already.
        Try only the runs directory plus the unique run directory first; fall
        back to the recursive creator when expected parents are missing.
        """
        runs_path = f"{dossier}/runs"
        response = await self._dav_request(
            client,
            "MKCOL",
            credential,
            runs_path,
        )
        if response.status_code == 409:
            await self._ensure_dir(client, credential, runs_path)
        elif response.status_code not in {201, 405}:
            response.raise_for_status()

        response = await self._dav_request(
            client,
            "MKCOL",
            credential,
            run_path,
        )
        if response.status_code not in {201, 405}:
            response.raise_for_status()

    async def _put(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        target: str,
        content: bytes,
        content_type: str,
    ) -> str | None:
        content = apply_policy_hook(
            PRE_PERSIST,
            content=bytes(content),
            metadata={
                "source": "research_archive",
                "target": target,
                "content_type": content_type,
            },
        )
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("pre_persist policy hook must return bytes content")
        response = await self._dav_request(
            client,
            "PUT",
            credential,
            target,
            content=bytes(content),
            headers={"Content-Type": content_type},
        )
        response.raise_for_status()

        for header in ("OC-FileId", "X-OC-FileId", "FileId"):
            value = str(response.headers.get(header) or "").strip()
            if value.isdigit():
                return value
        return None

    async def _propfind_file_id(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        target: str,
    ) -> str | None:
        """Resolve Nextcloud oc:fileid for an already-created DAV resource."""
        root = ET.Element(f"{{{_DAV_NS}}}propfind")
        prop = ET.SubElement(root, f"{{{_DAV_NS}}}prop")
        ET.SubElement(prop, f"{{{_OC_NS}}}fileid")
        try:
            response = await self._dav_request(
                client,
                "PROPFIND",
                credential,
                target,
                content=ET.tostring(root, encoding="utf-8", xml_declaration=True),
                headers={
                    "Depth": "0",
                    "Content-Type": "application/xml; charset=utf-8",
                },
            )
            response.raise_for_status()
            xml_root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError):
            return None
        for node in xml_root.iter(f"{{{_OC_NS}}}fileid"):
            value = str(node.text or "").strip()
            if value.isdigit():
                return value
        return None

    async def _append_jsonl(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        target: str,
        event: dict[str, Any],
    ) -> None:
        existing = b""
        response = await self._dav_request(
            client,
            "GET",
            credential,
            target,
        )
        if response.status_code == 200:
            existing = response.content
        elif response.status_code != 404:
            response.raise_for_status()
        line = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8") + b"\n"
        await self._put(
            client,
            credential,
            target,
            existing + line,
            "application/x-ndjson; charset=utf-8",
        )

    def reserve_run(
        self,
        *,
        scoped_user_id: str,
        conversation_id: str,
        request_id: str,
    ) -> ResearchRun | None:
        """Reserve the deterministic ResearchRun path without WebDAV I/O."""
        root = self._root_for_user(scoped_user_id)
        if not root or not self.dav_base:
            return None
        conversation = self._conversation_key(conversation_id)
        run_id = self._run_id(request_id)
        dossier = f"{root}/{conversation}"
        return ResearchRun(
            conversation_id=conversation,
            run_id=run_id,
            dossier_path=dossier,
            run_path=f"{dossier}/runs/{run_id}",
            scoped_user_id=str(scoped_user_id or ""),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def persist_run_start(
        self,
        run: ResearchRun,
        *,
        request_id: str,
        question: str,
        model_id: str = "",
    ) -> None:
        """Persist reserved run metadata from the dossier delivery path."""
        credential = self._credential(run.scoped_user_id)
        created_at = str(run.created_at or datetime.now(timezone.utc).isoformat())
        meta = {
            "schema": "sunaq.research_run.v1",
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "request_id": str(request_id or ""),
            "created_at": created_at,
            "question": str(question or ""),
            "model": str(model_id or ""),
        }
        started_line = (
            json.dumps(
                {
                    "type": "research.run.started",
                    "at": created_at,
                    "request_id": str(request_id or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            + b"\n"
        )

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_run_dir(
                client, credential, run.dossier_path, run.run_path
            )

            # Resolver/document steps may already have created trace.jsonl while
            # the run-start persistence was deferred. Preserve those entries and
            # put the logical start event first instead of overwriting the trace.
            trace_target = f"{run.run_path}/trace.jsonl"
            existing = b""
            response = await self._dav_request(
                client,
                "GET",
                credential,
                trace_target,
            )
            if response.status_code == 200:
                existing = response.content
            elif response.status_code != 404:
                response.raise_for_status()

            # Keep DAV writes inside one dossier job sequential. The outbox already
            # serializes jobs per dossier; parallel sibling writes would recreate
            # the same Nextcloud lock race inside a single delivery operation.
            await self._put(
                client,
                credential,
                f"{run.run_path}/run.json",
                json.dumps(
                    meta,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            await self._put(
                client,
                credential,
                trace_target,
                started_line + existing,
                "application/x-ndjson; charset=utf-8",
            )

    async def start_run(
        self,
        *,
        scoped_user_id: str,
        conversation_id: str,
        request_id: str,
        question: str,
        model_id: str = "",
    ) -> ResearchRun | None:
        """Compatibility path: reserve and persist the ResearchRun synchronously."""
        run = self.reserve_run(
            scoped_user_id=scoped_user_id,
            conversation_id=conversation_id,
            request_id=request_id,
        )
        if run is None:
            return None
        await self.persist_run_start(
            run,
            request_id=request_id,
            question=question,
            model_id=model_id,
        )
        return run

    async def write_resolver(
        self,
        run: ResearchRun,
        *,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None,
        error: str = "",
    ) -> None:
        credential = self._credential(run.scoped_user_id)
        resolver_path = f"{run.run_path}/resolver"
        now = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_dir(client, credential, resolver_path)
            await self._put(
                client,
                credential,
                f"{resolver_path}/request.json",
                json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "resolver.request",
                    "at": now,
                    "references": len(request_payload.get("references") or []),
                },
            )
            if response_payload is not None:
                await self._put(
                    client,
                    credential,
                    f"{resolver_path}/response.json",
                    json.dumps(response_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                by_source: dict[str, list[dict[str, Any]]] = {}
                for result in response_payload.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    for candidate in result.get("candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        source = _SAFE_FILE_RE.sub("-", str(candidate.get("source") or "unknown").strip().lower()).strip(".-_") or "unknown"
                        by_source.setdefault(source, []).append({
                            "reference": result.get("reference"),
                            "status": result.get("status"),
                            "candidate": candidate,
                        })
                for source, records in by_source.items():
                    await self._put(
                        client,
                        credential,
                        f"{resolver_path}/{source}.json",
                        json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8"),
                        "application/json; charset=utf-8",
                    )

            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "resolver.response" if not error else "resolver.error",
                    "at": now,
                    "references": len(request_payload.get("references") or []),
                    "status": "error" if error else "ok",
                    "error": str(error or ""),
                },
            )

    async def write_retrieval_decision(
        self,
        run: ResearchRun,
        decision: dict[str, Any],
    ) -> None:
        """Persist the pre-retrieval strategy audit record inside the ResearchRun."""
        payload = dict(decision or {})
        if str(payload.get("schema") or "") != "sunaq.retrieval_decision.v2":
            raise ValueError("unsupported retrieval decision schema")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) > 250_000:
            raise ValueError("retrieval decision record exceeds hard size limit")

        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        target = f"{run.run_path}/retrieval-decision.v2.json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            # A reserved ResearchRun has no DAV state yet. Every writer that
            # targets the run root must be able to materialize it independently.
            await self._ensure_run_dir(
                client, credential, run.dossier_path, run.run_path
            )
            await self._put(
                client,
                credential,
                target,
                encoded,
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "retrieval.decision",
                    "at": now,
                    "path": target,
                    "schema": payload["schema"],
                },
            )


    async def write_retrieval_execution(
        self,
        run: ResearchRun,
        execution: dict[str, Any],
    ) -> None:
        """Persist post-ACL retrieval execution provenance.

        The caller must supply only ACL-authorized candidates/documents.
        Pre-ACL hit counts, backend ranks and scores are deliberately excluded
        from this contract because they can reveal hidden repository state.
        """
        payload = dict(execution or {})
        if str(payload.get("schema") or "") != "sunaq.retrieval_execution.v1":
            raise ValueError("unsupported retrieval execution schema")
        if str(payload.get("authorization_boundary") or "") != "post_live_acl":
            raise ValueError("retrieval execution must be post_live_acl")

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) > 750_000:
            raise ValueError("retrieval execution record exceeds hard size limit")

        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        target = f"{run.run_path}/retrieval-execution.v1.json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_run_dir(
                client, credential, run.dossier_path, run.run_path
            )
            await self._put(
                client,
                credential,
                target,
                encoded,
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "retrieval.execution",
                    "at": now,
                    "path": target,
                    "schema": payload["schema"],
                },
            )


    async def write_external_source(
        self,
        run: ResearchRun,
        *,
        source: str,
        external_id: str,
        file_name: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one external research artifact below the ResearchRun.

        The artifact may be a primary-source document or an adapter-generated
        portable projection. Callers pass bytes only; transport credentials such
        as presigned URLs must never be included in metadata.
        """
        source_key = _SAFE_FILE_RE.sub("_", str(source or "").strip()).strip("._")
        external_key = _SAFE_FILE_RE.sub("_", str(external_id or "").strip()).strip("._")
        file_key = _SAFE_FILE_RE.sub("_", str(file_name or "").strip()).strip("._")
        if not source_key or not external_key or not file_key:
            raise ValueError("invalid external source path component")

        data = bytes(content)
        digest = hashlib.sha256(data).hexdigest()
        credential = self._credential(run.scoped_user_id)
        source_path = f"{run.run_path}/sources/{source_key}/{external_key}"
        target = f"{source_path}/{file_key}"
        manifest = {
            "schema": "sunaq.external_source.v1",
            "source": source_key,
            "external_id": str(external_id or ""),
            "file_name": file_key,
            "content_type": str(content_type or "application/octet-stream"),
            "size_bytes": len(data),
            "sha256": digest,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "metadata": dict(metadata or {}),
        }

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_dir(client, credential, source_path)
            file_id = await self._put(
                client,
                credential,
                target,
                data,
                manifest["content_type"],
            )
            if not file_id:
                file_id = await self._propfind_file_id(
                    client,
                    credential,
                    target,
                )
            if file_id:
                manifest["nextcloud_file_id"] = file_id
            await self._put(
                client,
                credential,
                f"{source_path}/{file_key}.source.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "external_source.archived",
                    "at": manifest["retrieved_at"],
                    "source": source_key,
                    "external_id": str(external_id or ""),
                    "path": target,
                    "file_id": file_id,
                    "sha256": digest,
                    "size_bytes": len(data),
                },
            )
        return {
            "path": target,
            "file_id": file_id,
            "sha256": digest,
            "size_bytes": len(data),
            "content_type": manifest["content_type"],
            "file_name": file_key,
        }



    async def write_research_markdown(
        self,
        run: ResearchRun,
        *,
        question: str,
        answer: str,
        source_records: list[dict[str, Any]] | None = None,
        heading: str = "Recherche",
        source_label: str = "",
        summary_sections: list[str] | None = None,
    ) -> None:
        """Write a human-readable per-run research summary.

        External transport URLs are never accepted here. Source links must
        already point at the archived Nextcloud copy.
        """
        archived = [
            item for item in (source_records or [])
            if isinstance(item, dict)
            and str(item.get("status") or "").strip().casefold() == "archived"
            and isinstance(item.get("response"), dict)
        ]
        lines = [
            f"# {str(heading or 'Recherche').strip()}",
            "",
            f"- Recherche-ID: `{run.run_id}`",
        ]
        if str(source_label or "").strip():
            lines.append(f"- Externe Quelle: {str(source_label).strip()}")
        lines.extend([
            "",
            "## Anfrage",
            "",
            str(question or "").strip(),
            "",
            "## Ergebnis",
            "",
            str(answer or "").strip(),
            "",
        ])
        for section in summary_sections or []:
            rendered = str(section or "").strip()
            if rendered:
                lines.extend([rendered, ""])
        lines.extend([
            "## Archivierte Research-Artefakte",
            "",
        ])
        if not archived:
            lines.append("Keine externen Research-Artefakte wurden in diesem Lauf archiviert.")
        else:
            for index, item in enumerate(archived, start=1):
                response = item.get("response") or {}
                file_name = str(response.get("file_name") or f"Artefakt {index}").strip()
                title = str(item.get("title") or file_name).strip()
                source_url = str(response.get("source_url") or "").strip()
                path = str(response.get("path") or "").strip()
                relative_path = ""
                prefix = run.run_path.rstrip("/") + "/"
                if path.startswith(prefix):
                    relative_path = path[len(prefix):]
                file_id = str(response.get("file_id") or "").strip()
                sha256 = str(response.get("sha256") or "").strip()
                category = str(item.get("category") or "provider_source").strip()
                lines.extend([
                    f"### [A{index}] {title}",
                    "",
                    (
                        f"- Datei: [{file_name}]({source_url})"
                        if source_url
                        else (
                            f"- Datei: [{file_name}]({relative_path})"
                            if relative_path
                            else f"- Datei in Nextcloud: `{path}`"
                        )
                    ),
                    f"- Content-Type: `{response.get('content_type') or 'unbekannt'}`",
                    f"- Nextcloud File-ID: `{file_id}`" if file_id else "- Nextcloud File-ID: nicht verfügbar",
                    f"- SHA-256: `{sha256}`" if sha256 else "- SHA-256: nicht verfügbar",
                    f"- Rolle: `{category}`",
                    "",
                ])
        content = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_run_dir(
                client, credential, run.dossier_path, run.run_path
            )
            await self._put(
                client,
                credential,
                f"{run.run_path}/recherche.md",
                content,
                "text/markdown; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "research.markdown",
                    "at": now,
                    "path": f"{run.run_path}/recherche.md",
                    "archived_sources": len(archived),
                },
            )


    async def write_web_evidence(
        self,
        run: ResearchRun,
        *,
        queries: list[str],
        sources: list[dict[str, Any]],
        searched: int = 0,
        fetched: int = 0,
        archive_run_paths: list[str] | None = None,
    ) -> None:
        """Persist the bounded web evidence actually supplied to the answer model.

        This dossier record is independent of the optional full Webarchiv snapshot.
        It keeps the research run auditable without duplicating raw HTML/PDF bodies.
        """
        safe_sources: list[dict[str, Any]] = []
        for source in (sources or [])[:20]:
            if not isinstance(source, dict):
                continue
            safe_sources.append({
                "title": str(source.get("title") or "")[:512],
                "url": str(source.get("url") or "")[:2048],
                "final_url": str(source.get("final_url") or "")[:2048],
                "publisher": str(source.get("publisher") or "")[:512],
                "published_at": str(source.get("published_at") or "")[:128],
                "retrieved_at": str(source.get("retrieved_at") or "")[:128],
                "content_type": str(source.get("content_type") or "")[:256],
                "content_hash": str(source.get("content_hash") or "")[:256],
                "relevance_score": source.get("relevance_score"),
                "relevance_reason": str(source.get("relevance_reason") or "")[:2000],
                "evidence_text": str(source.get("evidence_text") or "")[:16000],
                "archive_path": str(source.get("archive_path") or "")[:2048],
                "archive_raw_path": str(source.get("archive_raw_path") or "")[:2048],
                "archive_pdf_path": str(source.get("archive_pdf_path") or "")[:2048],
                "archive_metadata_path": str(source.get("archive_metadata_path") or "")[:2048],
                "search_query": str(source.get("search_query") or "")[:1200],
            })

        payload = {
            "schema": "sunaq.web_evidence.v1",
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "queries": [str(value or "")[:1200] for value in (queries or [])[:12] if str(value or "").strip()],
            "searched": int(searched or 0),
            "fetched": int(fetched or 0),
            "selected": len(safe_sources),
            "archive_run_paths": [
                str(value or "")[:2048]
                for value in (archive_run_paths or [])[:12]
                if str(value or "").strip()
            ],
            "sources": safe_sources,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) > 500_000:
            raise ValueError("web evidence dossier record exceeds hard size limit")

        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        web_path = f"{run.run_path}/web"
        target = f"{web_path}/evidence.json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_dir(client, credential, web_path)
            await self._put(
                client,
                credential,
                target,
                encoded,
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "web.evidence",
                    "at": now,
                    "path": target,
                    "searched": payload["searched"],
                    "fetched": payload["fetched"],
                    "selected": payload["selected"],
                },
            )

    async def write_delegated_research(
        self,
        run: ResearchRun,
        *,
        provider: str,
        model: str,
        backend_id: str = "",
        response_id: str,
        search_queries: list[str],
        citations: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Persist provider-neutral provenance for a delegated public research run.

        Raw provider responses are deliberately not archived.  The dossier keeps
        the normalized search/citation/source metadata needed to audit what the
        external provider did, while the user-visible answer remains part of the
        normal chat archive.
        """
        safe_citations = []
        for value in (citations or [])[:100]:
            if not isinstance(value, dict):
                continue
            safe_citations.append({
                "index": int(value.get("index") or 0),
                "url": str(value.get("url") or "")[:4096],
                "title": str(value.get("title") or "")[:512],
                "start_index": value.get("start_index"),
                "end_index": value.get("end_index"),
            })
        safe_sources = []
        for value in (sources or [])[:250]:
            if not isinstance(value, dict):
                continue
            safe_sources.append({
                "url": str(value.get("url") or "")[:4096],
                "title": str(value.get("title") or "")[:512],
                "cited": bool(value.get("cited", False)),
            })
        safe_usage = dict(usage or {})
        encoded_usage = json.dumps(
            safe_usage,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if len(encoded_usage) > 50_000:
            safe_usage = {"truncated": True}

        payload = {
            "schema": "sunaq.delegated_research.v1",
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "backend_id": str(backend_id or "")[:80],
            "provider": str(provider or "")[:80],
            "model": str(model or "")[:200],
            "response_id": str(response_id or "")[:300],
            "search_queries": [
                str(value or "")[:8000]
                for value in (search_queries or [])[:64]
                if str(value or "").strip()
            ],
            "citations": safe_citations,
            "sources": safe_sources,
            "usage": safe_usage,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        if len(encoded) > 500_000:
            raise ValueError("delegated research dossier record exceeds hard size limit")

        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        web_path = f"{run.run_path}/web"
        target = f"{web_path}/provider.json"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_dir(client, credential, web_path)
            await self._put(
                client,
                credential,
                target,
                encoded,
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "web.delegated_research",
                    "at": now,
                    "path": target,
                    "provider": payload["provider"],
                    "model": payload["model"],
                    "citations": len(safe_citations),
                    "sources": len(safe_sources),
                },
            )

    async def write_documents(
        self,
        run: ResearchRun,
        documents: list[dict[str, Any]],
    ) -> None:
        credential = self._credential(run.scoped_user_id)
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": "sunaq.documents.v1",
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "documents": documents,
        }
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
            await self._ensure_run_dir(
                client, credential, run.dossier_path, run.run_path
            )
            await self._put(
                client,
                credential,
                f"{run.run_path}/documents.json",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            await self._append_jsonl(
                client,
                credential,
                f"{run.run_path}/trace.jsonl",
                {
                    "type": "retrieval.documents",
                    "at": now,
                    "count": len(documents),
                },
            )
