"""Mail sidecar helpers for deterministic mail/thread metadata.

The sidecar is deliberately not a retrieval document.  Two layouts are
supported:

1. The built-in mail sync and directory-per-mail extractors use
   ``.mailmeta.json`` in the same message directory.
2. Legacy flat ``mail_sync`` archives remain readable via
   ``.<base>.mailmeta.json`` next to ``<base>_mail.txt`` / attachments.

The graph worker reads these files directly via WebDAV.  Elasticsearch is not
used as the metadata source, so hidden files may stay unindexed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from rag.logging_utils import get_logger
from rag.secret_env import secret_env
from rag.credential_store import CredentialStore

log = get_logger("worker")

def cfg_get(cfg: dict[str, Any], *paths: str, default=None):
    for path in paths:
        cur: Any = cfg
        ok = True
        for part in str(path).split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            return cur
    return default

MAILMETA_SCHEMA = "nextcloud-mailmeta-v1"
_FLAT_MAIL_RE = re.compile(
    r"^(?P<base>.+)_(?:mail\.txt|a\d{2}_.+|message\.eml)$",
    re.IGNORECASE,
)


def normalize_cloud_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def is_mail_metadata_sidecar(path: str) -> bool:
    name = PurePosixPath(normalize_cloud_path(path)).name
    if name == ".mailmeta.json":
        return True
    return bool(name.startswith(".") and name.endswith(".mailmeta.json"))


def sidecar_candidates(title: str) -> list[str]:
    """Return deterministic WebDAV sidecar paths for an indexed document."""
    clean = normalize_cloud_path(title)
    if not clean or is_mail_metadata_sidecar(clean):
        return []
    path = PurePosixPath(clean)
    parent = "" if str(path.parent) == "." else str(path.parent)
    names = [".mailmeta.json"]
    match = _FLAT_MAIL_RE.match(path.name)
    if match:
        names.append(f".{match.group('base')}.mailmeta.json")
    out: list[str] = []
    for name in names:
        candidate = f"{parent}/{name}" if parent else name
        if candidate not in out:
            out.append(candidate)
    return out


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def normalize_mail_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalize the intentionally small v1 sidecar contract."""
    if not isinstance(payload, dict):
        raise ValueError("mail metadata must be a JSON object")
    schema = str(payload.get("schema") or "").strip()
    if schema != MAILMETA_SCHEMA:
        raise ValueError(f"unsupported mail metadata schema: {schema!r}")
    if str(payload.get("content_kind") or "").strip().lower() != "email":
        raise ValueError("mail metadata content_kind must be 'email'")

    headers = payload.get("headers") or {}
    files = payload.get("files") or payload.get("representations") or {}
    if not isinstance(headers, dict) or not isinstance(files, dict):
        raise ValueError("headers/files must be objects")

    def text(name: str) -> str:
        return str(headers.get(name) or "").strip()

    normalized_files: dict[str, Any] = {
        "eml": str(files.get("eml") or "").strip() or None,
        "text": str(files.get("text") or files.get("plain_text") or "").strip() or None,
        "html_pdf": _string_list(files.get("html_pdf")),
        "readme": str(files.get("readme") or "").strip() or None,
        "attachments": [],
    }
    attachments = files.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [attachments]
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("filename") or "").strip()
                if name:
                    normalized_files["attachments"].append(name)
            else:
                name = str(item or "").strip()
                if name:
                    normalized_files["attachments"].append(name)

    source = payload.get("source") or {}
    technical_headers = payload.get("technical_headers") or {}
    integrity = payload.get("integrity") or {}
    if not isinstance(source, dict):
        source = {}
    if not isinstance(technical_headers, dict):
        technical_headers = {}
    if not isinstance(integrity, dict):
        integrity = {}

    return {
        "schema": MAILMETA_SCHEMA,
        "content_kind": "email",
        "headers": {
            "message_id": text("message_id"),
            "in_reply_to": _string_list(headers.get("in_reply_to")),
            "references": _string_list(headers.get("references")),
            "date_raw": text("date_raw"),
            "date_iso": text("date_iso"),
            "subject": text("subject"),
            "from": headers.get("from") or [],
            "to": headers.get("to") or [],
            "cc": headers.get("cc") or [],
            "bcc": headers.get("bcc") or [],
            "reply_to": headers.get("reply_to") or [],
        },
        "technical_headers": {
            "return_path": str(technical_headers.get("return_path") or "").strip(),
            "received": _string_list(technical_headers.get("received")),
            "authentication_results": _string_list(technical_headers.get("authentication_results")),
            "arc_authentication_results": _string_list(technical_headers.get("arc_authentication_results")),
            "dkim_signature": _string_list(technical_headers.get("dkim_signature")),
            "received_spf": _string_list(technical_headers.get("received_spf")),
        },
        "files": normalized_files,
        "source": {
            "account": str(source.get("account") or "").strip(),
            "mailbox": str(source.get("mailbox") or "").strip(),
            "imap_uid": source.get("imap_uid"),
            "imap_uidvalidity": source.get("imap_uidvalidity"),
            "imported_at": str(source.get("imported_at") or "").strip(),
        },
        "integrity": {
            "algorithm": str(integrity.get("algorithm") or "").strip().lower(),
            "raw_message_sha256": str(integrity.get("raw_message_sha256") or "").strip().lower(),
            "raw_message_bytes": integrity.get("raw_message_bytes"),
        },
        "generator": payload.get("generator") or {},
    }


def representation_role(metadata: dict[str, Any], title: str) -> str | None:
    """Return a deterministic role for a file listed by the sidecar."""
    name = PurePosixPath(normalize_cloud_path(title)).name
    files = metadata.get("files") or {}
    if name and name == str(files.get("text") or ""):
        return "plain_text"
    if name and name == str(files.get("readme") or ""):
        return "readme"
    eml = str(files.get("eml") or "")
    if name and (name == eml or (eml and name == PurePosixPath(eml).name)):
        return "eml"
    if name in _string_list(files.get("html_pdf")):
        return "html_pdf"
    if name in _string_list(files.get("attachments")):
        return "attachment"
    return None


def message_key(message_id: str, sidecar_path: str) -> str:
    message_id = str(message_id or "").strip()
    if message_id:
        return "message-id:" + message_id
    # The sidecar path is stable enough to bind representations of a mail that
    # lacks a Message-ID without pretending to infer identity from subject/date.
    return "sidecar:" + normalize_cloud_path(sidecar_path)


def reply_parent_id(metadata: dict[str, Any]) -> str:
    headers = metadata.get("headers") or {}
    in_reply_to = _string_list(headers.get("in_reply_to"))
    if in_reply_to:
        return in_reply_to[-1]
    references = _string_list(headers.get("references"))
    return references[-1] if references else ""


def _cfg_value_or_env(cfg: dict[str, Any], *paths: str) -> str:
    for path in paths:
        value = cfg_get(cfg, path, default=None)
        if value is None or str(value).strip() == "":
            continue
        if path.endswith("_env"):
            return secret_env(str(value), "").strip()
        return str(value).strip()
    return ""


class MailMetadataReader:
    """Fetch hidden mail sidecars directly from Nextcloud WebDAV, fail-open.

    In the multi-user beta path no global mail/WebDAV credential is used.  The
    answer hook passes the frontend-scoped identity that cited the document;
    this reader then uses exactly that user's current Nextcloud app password.
    """

    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg_get(cfg, "mail_metadata.enabled", default=True))
        self.timeout = float(cfg_get(cfg, "mail_metadata.timeout", default=10) or 10)
        self.verify_tls = bool(cfg_get(cfg, "mail_metadata.verify_tls", "acl.verify_tls", default=True))
        self.ca_file = str(cfg_get(cfg, "mail_metadata.ca_file", "acl.ca_file", default="") or "").strip() or None
        self.identity_mode = str(cfg_get(cfg, "acl.identity_mode", default="credential_store") or "credential_store").strip().lower()
        store_path = str(cfg_get(cfg, "auth.credential_store", "acl.credential_store", default="runtime/users.sqlite") or "runtime/users.sqlite")
        self.store = CredentialStore(store_path)
        self.nextcloud_base = str(cfg_get(cfg, "nextcloud.base_url", default="") or "").strip().rstrip("/")

        # Explicit single-user compatibility fallback; no mail-specific secret in config.yaml.
        self.legacy_username = _cfg_value_or_env(cfg, "mail_metadata.username_env", "mail_metadata.username", "acl.username_env", "acl.username")
        password_env = str(cfg_get(cfg, "mail_metadata.password_env", "acl.password_env", default="") or "").strip()
        self.legacy_password = secret_env(password_env, "").strip() if password_env else ""
        raw_url = str(cfg_get(cfg, "mail_metadata.webdav_url", default="") or "").strip().rstrip("/")
        self.legacy_base_url = raw_url

        self._clients: dict[str, httpx.Client] = {}
        self._cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self.identity_mode == "credential_store":
            return bool(self.nextcloud_base)
        return bool(self._legacy_context())

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def _legacy_context(self) -> tuple[str, str, str] | None:
        if not (self.legacy_username and self.legacy_password):
            return None
        base = self.legacy_base_url
        if not base and self.nextcloud_base:
            base = self.nextcloud_base + "/remote.php/dav/files"
        if not base:
            return None
        encoded = quote(self.legacy_username, safe="")
        if "{username}" in base:
            base = base.replace("{username}", encoded)
        elif base.endswith("/remote.php/dav/files"):
            base += "/" + encoded
        return "single-user", base, self.legacy_password

    def _context(self, rag_user_id: str | None) -> tuple[str, str, str] | None:
        if not self.enabled:
            return None
        identity = str(rag_user_id or "").strip()
        if self.identity_mode == "credential_store":
            if not identity or not self.nextcloud_base:
                return None
            canonical = self.store.get_canonical_user_for_identity(identity)
            credential = self.store.get_credential(identity, "nextcloud")
            if canonical is None or not canonical.enabled or credential is None:
                return None
            server = canonical.nextcloud_server.rstrip("/") or self.nextcloud_base
            base = f"{server}/remote.php/dav/files/{quote(credential.username, safe='')}"
            return identity, base, credential.secret
        return self._legacy_context()

    def _get_client(self, key: str, username: str, password: str) -> httpx.Client:
        client = self._clients.get(key)
        if client is None:
            verify: bool | str = self.ca_file if self.ca_file else self.verify_tls
            client = httpx.Client(
                auth=httpx.BasicAuth(username, password),
                timeout=self.timeout,
                verify=verify,
                follow_redirects=True,
            )
            self._clients[key] = client
        return client

    def fetch_for_title(self, title: str, *, rag_user_id: str | None = None) -> tuple[dict[str, Any], str] | None:
        context = self._context(rag_user_id)
        if context is None:
            return None
        key, base_url, password = context
        if self.identity_mode == "credential_store":
            credential = self.store.get_credential(str(rag_user_id or "").strip(), "nextcloud")
            if credential is None:
                return None
            username = credential.username
        else:
            username = self.legacy_username
        client = self._get_client(key, username, password)
        for candidate in sidecar_candidates(title):
            cache_key = (key, candidate)
            if cache_key in self._cache:
                cached = self._cache[cache_key]
                if cached is not None and representation_role(cached, title):
                    return cached, candidate
                continue
            url = base_url + "/" + "/".join(quote(part, safe="") for part in candidate.split("/"))
            try:
                response = client.get(url)
            except Exception as exc:
                log.debug("mail sidecar fetch failed path=%s user=%s: %s", candidate, key, exc)
                self._cache[cache_key] = None
                return None
            if response.status_code == 404:
                self._cache[cache_key] = None
                continue
            if response.status_code in {401, 403}:
                log.warning("mail sidecar not readable path=%s user=%s HTTP=%s", candidate, key, response.status_code)
                self._cache[cache_key] = None
                return None
            if response.is_error:
                log.debug("mail sidecar HTTP error path=%s HTTP=%s", candidate, response.status_code)
                self._cache[cache_key] = None
                continue
            try:
                payload = normalize_mail_metadata(response.json())
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("invalid mail sidecar path=%s: %s", candidate, exc)
                self._cache[cache_key] = None
                return None
            self._cache[cache_key] = payload
            if representation_role(payload, title):
                return payload, candidate
        return None
