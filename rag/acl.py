"""Live Nextcloud authorization for final RAG evidence.

The retrieval indices may contain stale ACL metadata.  This module deliberately
asks Nextcloud itself whether the current user can see the already-ranked file
IDs.  Authorization therefore happens *after* retrieval/reranking and never
pulls replacement candidates.

Two identity modes are supported:

``single_user``
    Useful for the current one-user deployment and for smoke/regression tests.
    Every RAG request is checked with the configured Nextcloud app password.

``credential_store``
    Uses a frontend-scoped identity to retrieve a JIT-created Nextcloud app
    credential.  The identity is additionally bound to a canonical
    (nextcloud_server, nextcloud_login) user for admin-managed user settings.

``mapped_users``
    Legacy static mapping. Missing identities/credentials fail closed.

No password is stored in config.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse
import xml.etree.ElementTree as ET

import httpx

from rag.credential_store import CredentialStore
from rag.secret_env import secret_env
from rag.nextcloud_tls import nextcloud_verify_value


DAV_NS = "DAV:"
OC_NS = "http://owncloud.org/ns"




def _cfg_get(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = cfg
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current

class AclError(RuntimeError):
    """Base class for live ACL failures."""


class AclConfigurationError(AclError):
    pass


class AclIdentityError(AclError):
    pass


class AclBackendError(AclError):
    pass


@dataclass(frozen=True)
class NextcloudCredential:
    username: str
    password: str


@dataclass(frozen=True)
class AclDecision:
    enabled: bool
    results: list[dict[str, Any]]
    checked: int
    authorized: int


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _file_id(item: dict[str, Any]) -> str | None:
    """Return the numeric Nextcloud fileid represented by a result."""
    raw = str(item.get("document_id") or item.get("id") or "").strip()
    match = re.fullmatch(r"files:(\d+)", raw)
    if match:
        return match.group(1)

    # Some legacy payloads carry a separate open-file id.
    for key in ("nextcloud_openfile_id", "fileid", "file_id"):
        value = str(item.get(key) or "").strip()
        if value.isdigit():
            return value
    return None


def _nested_or(expressions: list[ET.Element]) -> ET.Element:
    """Build a balanced RFC5323 binary OR tree.

    A left-deep tree reaches Python/XML recursion limits around ~1000 file ids.
    Pairwise folding keeps the tree depth logarithmic.  Live ACL additionally
    sends bounded batches, so neither the serializer nor Nextcloud receives a
    pathological boolean expression.
    """
    if not expressions:
        raise ValueError("at least one expression is required")
    level = list(expressions)
    while len(level) > 1:
        next_level: list[ET.Element] = []
        for index in range(0, len(level), 2):
            if index + 1 >= len(level):
                next_level.append(level[index])
                continue
            node = ET.Element(f"{{{DAV_NS}}}or")
            node.append(level[index])
            node.append(level[index + 1])
            next_level.append(node)
        level = next_level
    return level[0]


def build_fileid_search_xml(username: str, file_ids: list[str]) -> bytes:
    """Return a Nextcloud WebDAV SEARCH body for one or more file IDs."""
    clean_ids = []
    seen: set[str] = set()
    for value in file_ids:
        text = str(value).strip()
        if text.isdigit() and text not in seen:
            seen.add(text)
            clean_ids.append(text)
    if not clean_ids:
        raise ValueError("no numeric file ids")

    ET.register_namespace("d", DAV_NS)
    ET.register_namespace("oc", OC_NS)
    root = ET.Element(f"{{{DAV_NS}}}searchrequest")
    basic = ET.SubElement(root, f"{{{DAV_NS}}}basicsearch")
    select = ET.SubElement(basic, f"{{{DAV_NS}}}select")
    props = ET.SubElement(select, f"{{{DAV_NS}}}prop")
    ET.SubElement(props, f"{{{OC_NS}}}fileid")
    ET.SubElement(props, f"{{{DAV_NS}}}displayname")

    from_node = ET.SubElement(basic, f"{{{DAV_NS}}}from")
    scope = ET.SubElement(from_node, f"{{{DAV_NS}}}scope")
    href = ET.SubElement(scope, f"{{{DAV_NS}}}href")
    href.text = f"/files/{quote(username, safe='')}"
    depth = ET.SubElement(scope, f"{{{DAV_NS}}}depth")
    depth.text = "infinity"

    where = ET.SubElement(basic, f"{{{DAV_NS}}}where")
    comparisons: list[ET.Element] = []
    for file_id in clean_ids:
        eq = ET.Element(f"{{{DAV_NS}}}eq")
        prop = ET.SubElement(eq, f"{{{DAV_NS}}}prop")
        ET.SubElement(prop, f"{{{OC_NS}}}fileid")
        literal = ET.SubElement(eq, f"{{{DAV_NS}}}literal")
        literal.text = file_id
        comparisons.append(eq)
    where.append(_nested_or(comparisons) if len(comparisons) > 1 else comparisons[0])
    ET.SubElement(basic, f"{{{DAV_NS}}}orderby")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_authorized_file_paths(
    xml_body: str | bytes,
    username: str,
    dav_root_path: str = "/remote.php/dav/",
) -> dict[str, str]:
    """Map visible Nextcloud file IDs to canonical user-relative DAV paths."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise AclBackendError(f"invalid WebDAV SEARCH response: {exc}") from exc

    user = str(username or "").strip()
    if not user:
        return {}
    encoded_user = quote(user, safe="")
    dav_root = "/" + str(dav_root_path or "").strip("/") + "/"
    prefixes = (
        f"{dav_root}files/{encoded_user}/",
        f"{dav_root}files/{user}/",
        f"/files/{encoded_user}/",
        f"/files/{user}/",
    )
    result: dict[str, str] = {}
    for response in root.findall(f".//{{{DAV_NS}}}response"):
        href_node = response.find(f"{{{DAV_NS}}}href")
        if href_node is None or not href_node.text:
            continue
        parsed_path = urlparse(str(href_node.text)).path
        decoded_path = unquote(parsed_path)
        relative = ""
        for prefix in prefixes:
            decoded_prefix = unquote(prefix)
            if decoded_path.startswith(decoded_prefix):
                relative = decoded_path[len(decoded_prefix):].lstrip("/")
                break
        if not relative:
            continue
        for elem in response.iter(f"{{{OC_NS}}}fileid"):
            file_id = str(elem.text or "").strip()
            if file_id.isdigit():
                result[file_id] = relative
                break
    return result


def parse_authorized_fileids(xml_body: str | bytes) -> set[str]:
    """Extract successfully returned oc:fileid values from DAV multistatus."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        raise AclBackendError(f"invalid WebDAV SEARCH response: {exc}") from exc
    result: set[str] = set()
    for elem in root.iter(f"{{{OC_NS}}}fileid"):
        if elem.text and elem.text.strip().isdigit():
            result.add(elem.text.strip())
    return result


def _promote_authorized_duplicate(
    representative: dict[str, Any],
    variant: dict[str, Any],
    remaining_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    """Promote one ACL-visible duplicate without reusing denied evidence text."""
    ranking_keys = (
        "rank", "final_rank", "score",
        "es_rank", "es_score", "elasticsearch_rank", "elasticsearch_score",
        "vector_rank", "vector_score", "graph_rank", "graph_score",
        "rrf", "rrf_rank", "rrf_score",
        "probe_rrf_score", "probe_hits",
        "reranker_score", "reranker_raw_score",
        "chunk_no", "exhaustive_required",
    )
    # Never begin with the denied representative.  Only ranking/provenance
    # metadata that cannot contain document evidence crosses the ACL boundary.
    promoted = {
        key: representative.get(key)
        for key in ranking_keys
        if key in representative
    }
    identity_keys = (
        "document_id", "id", "fileid", "file_id",
        "title", "path", "directory", "filename",
        "nextcloud_es_id", "nextcloud_openfile_id", "source_url",
        "document_date", "content_type", "content_hash", "source", "provider",
        "share_names", "owner", "users", "groups", "circles", "source_origin",
    )
    # Promotion crosses an authorization boundary: never retain identity or ACL
    # metadata from the denied representative merely because the visible legacy
    # variant omitted that field.
    for key in identity_keys:
        promoted.pop(key, None)
    for key in identity_keys:
        if key in variant:
            promoted[key] = variant.get(key)
    variant_file_id = _file_id(variant)
    if variant_file_id and not promoted.get("document_id"):
        promoted["document_id"] = f"files:{variant_file_id}"

    snippet = str(
        variant.get("snippet")
        or variant.get("es_snippet")
        or variant.get("vector_snippet")
        or variant.get("graph_snippet")
        or ""
    )
    promoted["snippet"] = snippet
    promoted["es_snippet"] = str(variant.get("es_snippet") or "")
    promoted["vector_snippet"] = str(variant.get("vector_snippet") or "")
    promoted["graph_snippet"] = str(variant.get("graph_snippet") or "")
    promoted["context_text"] = snippet or str(variant.get("title") or variant.get("path") or "")
    promoted["context_enriched"] = False
    promoted["acl_promoted_duplicate"] = True

    # Graph/extraction evidence belongs to the denied representative unless it
    # was explicitly stored on this concrete variant, which current dedup does
    # not do. Do not carry it over merely because ranking provenance is shared.
    for key in (
        "graph_reason", "graph_entities", "graph_direct_relations",
        "graph_indirect_chains", "graph_relation_observations",
        "graph_relation_evidence",
    ):
        promoted.pop(key, None)

    promoted["duplicate_variants"] = [dict(item) for item in remaining_variants]
    promoted["duplicate_count"] = 1 + len(remaining_variants)
    return promoted


class NextcloudLiveAcl:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.enabled = _truthy(_cfg_get(cfg, "acl.enabled", default=False))
        self.identity_mode = str(_cfg_get(cfg, "acl.identity_mode", default="single_user") or "single_user").strip().lower()
        self.timeout = float(_cfg_get(cfg, "acl.timeout", default=15.0) or 15.0)
        self.batch_size = max(1, min(500, int(_cfg_get(cfg, "acl.batch_size", default=100) or 100)))
        verify = nextcloud_verify_value(cfg, "acl", "carddav")
        self.verify_tls = verify if isinstance(verify, bool) else True
        self.ca_file = verify if isinstance(verify, str) else None
        self.nextcloud_base_url = str(
            _cfg_get(cfg, "nextcloud.base_url", default="") or ""
        ).strip().rstrip("/")
        explicit_url = str(_cfg_get(cfg, "acl.webdav_url", default="") or "").strip()
        if explicit_url:
            self.webdav_url = explicit_url.rstrip("/") + "/"
        else:
            self.webdav_url = (
                self.nextcloud_base_url + "/remote.php/dav/"
                if self.nextcloud_base_url else ""
            )

    def _credential(self, rag_user_id: str | None) -> NextcloudCredential:
        if self.identity_mode == "single_user":
            username_env = str(_cfg_get(self.cfg, "acl.username_env", default="NEXTCLOUD_USERNAME") or "NEXTCLOUD_USERNAME")
            password_env = str(_cfg_get(self.cfg, "acl.password_env", default="NEXTCLOUD_APP_PASSWORD") or "NEXTCLOUD_APP_PASSWORD")
            username = os.getenv(username_env, "").strip()
            password = secret_env(password_env, "")
            if not username or not password:
                raise AclConfigurationError(
                    f"live ACL credentials missing ({username_env}/{password_env})"
                )
            return NextcloudCredential(username, password)

        user_id = str(rag_user_id or "").strip()
        if not user_id:
            raise AclIdentityError("RAG user identity missing")

        if self.identity_mode == "credential_store":
            store_path = str(_cfg_get(self.cfg, "acl.credential_store", default="runtime/users.sqlite") or "runtime/users.sqlite")
            store = CredentialStore(store_path)
            credential = store.get_credential(user_id, "nextcloud")
            if credential is None:
                raise AclIdentityError("no Nextcloud credential stored for current RAG user")
            canonical = store.get_canonical_user_for_identity(user_id)
            if canonical is not None and not canonical.enabled:
                raise AclIdentityError("Nextcloud user is disabled by RAG admin")
            return NextcloudCredential(credential.username, credential.secret)

        if self.identity_mode != "mapped_users":
            raise AclConfigurationError(f"unknown acl.identity_mode={self.identity_mode!r}")
        mapping = _cfg_get(self.cfg, "acl.user_map", default={}) or {}
        if not isinstance(mapping, dict):
            raise AclConfigurationError("acl.user_map must be a mapping")
        entry = mapping.get(user_id)
        if not isinstance(entry, dict):
            raise AclIdentityError("no Nextcloud credential mapping for current RAG user")
        username = str(entry.get("username") or "").strip()
        username_env = str(entry.get("username_env") or "").strip()
        if username_env:
            username = os.getenv(username_env, "").strip()
        password_env = str(entry.get("password_env") or "").strip()
        password = secret_env(password_env, "") if password_env else ""
        if not username or not password:
            raise AclIdentityError("Nextcloud credential mapping is incomplete")
        return NextcloudCredential(username, password)

    def credential_for_user(self, rag_user_id: str | None = None) -> NextcloudCredential:
        """Return the server-side Nextcloud credential for the current RAG user.

        This is shared by live ACL checks and other user-scoped WebDAV actions
        such as archival of selected public web evidence. Credentials never
        leave the middleware response.
        """
        return self._credential(rag_user_id)

    def prefilter_identity(self, rag_user_id: str | None = None) -> tuple[str, list[str]]:
        """Resolve the authenticated Nextcloud UID and current groups via OCS.

        Prefilter metadata is an optimization only.  The app credential is the
        same server-side credential used by live ACL; no client-supplied group
        header is trusted for this lookup.  Callers may fail open to the normal
        retrieval path if OCS is unavailable, while live WebDAV ACL remains the
        final authorization boundary.
        """
        if not self.enabled:
            raise AclConfigurationError("live ACL is disabled")
        if not self.nextcloud_base_url:
            raise AclConfigurationError("nextcloud.base_url is not configured")

        credential = self._credential(rag_user_id)
        verify: bool | str = self.ca_file if self.ca_file else self.verify_tls
        url = self.nextcloud_base_url + "/ocs/v1.php/cloud/user"
        try:
            response = httpx.get(
                url,
                params={"format": "json"},
                headers={
                    "Accept": "application/json",
                    "OCS-APIRequest": "true",
                },
                auth=(credential.username, credential.password),
                timeout=self.timeout,
                verify=verify,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise AclIdentityError(
                    f"Nextcloud rejected OCS identity credentials (HTTP {status})"
                ) from exc
            raise AclBackendError(
                f"Nextcloud OCS identity lookup failed (HTTP {status})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AclBackendError(
                f"Nextcloud OCS identity lookup failed: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            payload = response.json()
            ocs = payload.get("ocs") if isinstance(payload, dict) else None
            meta = ocs.get("meta") if isinstance(ocs, dict) else None
            data = ocs.get("data") if isinstance(ocs, dict) else None
            if not isinstance(data, dict):
                raise ValueError("missing ocs.data object")

            status_code = meta.get("statuscode") if isinstance(meta, dict) else None
            status = str(meta.get("status") or "").strip().casefold() if isinstance(meta, dict) else ""
            if status_code not in {None, 100} or (status and status != "ok"):
                message = str(meta.get("message") or "") if isinstance(meta, dict) else ""
                raise ValueError(
                    f"OCS status={status or '?'} statuscode={status_code!r} {message}".strip()
                )

            uid = str(data.get("id") or "").strip()
            if not uid:
                raise ValueError("missing authenticated Nextcloud user id")

            raw_groups = data.get("groups")
            if raw_groups is None:
                raw_groups = []
            if not isinstance(raw_groups, list):
                raise ValueError("ocs.data.groups is not a list")
            groups: list[str] = []
            for item in raw_groups[:256]:
                group = str(item or "").strip()
                if group and len(group) <= 256 and group not in groups:
                    groups.append(group)
            return uid, groups
        except (TypeError, ValueError) as exc:
            raise AclBackendError(
                f"invalid Nextcloud OCS identity response: {exc}"
            ) from exc

    def authorize_with_credential(
        self,
        results: list[dict[str, Any]],
        *,
        username: str,
        password: str,
    ) -> AclDecision:
        """Authorize a bounded result set with an explicitly supplied temporary credential.

        Used by ephemeral curation sessions. The credential is never written to
        the normal provider credential namespace.
        """
        if not self.enabled:
            return AclDecision(False, list(results), len(results), len(results))
        if not results:
            return AclDecision(True, [], 0, 0)
        if not self.webdav_url:
            raise AclConfigurationError("acl.webdav_url/nextcloud.base_url is not configured")
        credential = NextcloudCredential(str(username or "").strip(), str(password or ""))
        if not credential.username or not credential.password:
            raise AclIdentityError("temporary Nextcloud credential is incomplete")
        return self._authorize_with_credential(results, credential)

    def _authorize_with_credential(
        self,
        results: list[dict[str, Any]],
        credential: NextcloudCredential,
    ) -> AclDecision:
        # Dedupe runs before live ACL for ranking efficiency, so one result may
        # represent several concrete Nextcloud files. Authorize every file ID in
        # the group: a denied representative must never hide an accessible copy,
        # and unauthorized duplicate metadata must never leave this boundary.
        groups: list[tuple[dict[str, Any], str | None, list[tuple[dict[str, Any], str | None]]]] = []
        file_ids: list[str] = []
        for item in results:
            representative_id = _file_id(item)
            variants: list[tuple[dict[str, Any], str | None]] = []
            for raw_variant in item.get("duplicate_variants") or []:
                if not isinstance(raw_variant, dict):
                    continue
                variant = dict(raw_variant)
                variant_id = _file_id(variant)
                variants.append((variant, variant_id))
                if variant_id:
                    file_ids.append(variant_id)
            groups.append((item, representative_id, variants))
            if representative_id:
                file_ids.append(representative_id)

        if not file_ids:
            # Fail closed for results that cannot be tied to a Nextcloud file.
            return AclDecision(True, [], len(results), 0)

        # Preserve order while avoiding repeated file ids across batches.
        unique_file_ids = list(dict.fromkeys(file_ids))
        verify: bool | str = self.ca_file if self.ca_file else self.verify_tls
        authorized_ids: set[str] = set()
        for offset in range(0, len(unique_file_ids), self.batch_size):
            batch = unique_file_ids[offset:offset + self.batch_size]
            body = build_fileid_search_xml(credential.username, batch)
            try:
                response = httpx.request(
                    "SEARCH",
                    self.webdav_url,
                    content=body,
                    headers={"Content-Type": "text/xml; charset=utf-8"},
                    auth=(credential.username, credential.password),
                    timeout=self.timeout,
                    verify=verify,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {401, 403}:
                    raise AclIdentityError(f"Nextcloud rejected live ACL credentials (HTTP {status})") from exc
                raise AclBackendError(f"Nextcloud live ACL SEARCH failed (HTTP {status})") from exc
            except httpx.HTTPError as exc:
                raise AclBackendError(f"Nextcloud live ACL SEARCH failed: {type(exc).__name__}: {exc}") from exc
            authorized_ids.update(parse_authorized_fileids(response.content))
        filtered: list[dict[str, Any]] = []
        for item, representative_id, variants in groups:
            visible_variants = [
                variant for variant, variant_id in variants
                if variant_id is not None and variant_id in authorized_ids
            ]
            if representative_id is not None and representative_id in authorized_ids:
                kept = dict(item)
                kept["duplicate_variants"] = [dict(variant) for variant in visible_variants]
                kept["duplicate_count"] = 1 + len(visible_variants)
                filtered.append(kept)
                continue
            if visible_variants:
                filtered.append(
                    _promote_authorized_duplicate(
                        item,
                        visible_variants[0],
                        visible_variants[1:],
                    )
                )
        return AclDecision(True, filtered, len(results), len(filtered))

    def resolve_visible_file_path(
        self,
        document_id: str,
        *,
        rag_user_id: str | None = None,
    ) -> str | None:
        """Return the server-derived user-relative path for one visible file id."""
        if not self.enabled:
            raise AclConfigurationError("live ACL is disabled")
        if not self.webdav_url:
            raise AclConfigurationError("acl.webdav_url/nextcloud.base_url is not configured")
        file_id = _file_id({"document_id": document_id})
        if not file_id:
            return None

        credential = self._credential(rag_user_id)
        verify: bool | str = self.ca_file if self.ca_file else self.verify_tls
        body = build_fileid_search_xml(credential.username, [file_id])
        try:
            response = httpx.request(
                "SEARCH",
                self.webdav_url,
                content=body,
                headers={"Content-Type": "text/xml; charset=utf-8"},
                auth=(credential.username, credential.password),
                timeout=self.timeout,
                verify=verify,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise AclIdentityError(
                    f"Nextcloud rejected live ACL credentials (HTTP {status})"
                ) from exc
            raise AclBackendError(
                f"Nextcloud live ACL SEARCH failed (HTTP {status})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AclBackendError(
                f"Nextcloud live ACL SEARCH failed: {type(exc).__name__}: {exc}"
            ) from exc

        return parse_authorized_file_paths(
            response.content,
            credential.username,
            urlparse(self.webdav_url).path or "/remote.php/dav/",
        ).get(file_id)


    def authorize(self, results: list[dict[str, Any]], *, rag_user_id: str | None = None) -> AclDecision:
        """Filter *already final* results against current Nextcloud visibility."""
        if not self.enabled:
            return AclDecision(False, list(results), len(results), len(results))
        if not results:
            return AclDecision(True, [], 0, 0)
        if not self.webdav_url:
            raise AclConfigurationError("acl.webdav_url/nextcloud.base_url is not configured")
        return self._authorize_with_credential(results, self._credential(rag_user_id))
