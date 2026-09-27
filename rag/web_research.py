"""Ephemeral public-web evidence arm with optional Nextcloud archival.

Design constraints:
- search-engine snippets are discovery hints only, never answer evidence;
- only successfully fetched HTML/PDF/text becomes WebEvidence;
- an LLM relevance gate selects fetched sources before answer use/archive;
- selected sources can be snapshotted into the current user's Nextcloud via
  WebDAV as one timestamped research run below a monthly folder;
- each run gets recherche.md with query, parameters, selected sources and the
  final answer-model output;
- web evidence stays separate from ES/Qdrant/Neo4j during the live request.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx
import yaml

from rag.acl import NextcloudLiveAcl, NextcloudCredential
from rag.credential_store import CredentialStore, UserWebSettings
from rag.llm_backend import build_llm_backend
from rag.logging_utils import get_logger
from rag.nextcloud_tls import nextcloud_verify_value
from rag.policy_hooks import (
    OUTBOUND_QUERY,
    POST_FETCH,
    PRE_FETCH,
    PRE_PERSIST,
    apply_policy_hook,
)
from rag.source_registry import register_document
from rag.evidence_boundary import guarded_evidence_prompt, serialize_evidence_records
from rag.secret_env import secret_env

log = get_logger("web")

BASE_DIR = Path(__file__).resolve().parent.parent


def cfg_get(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = cfg
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else BASE_DIR / "config.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml root must be a mapping")
    return data


WEB_CONFIG_FILE = BASE_DIR / "web.yaml"


def load_web_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else WEB_CONFIG_FILE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("web.yaml root must be a mapping")
    return data


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_component(value: str, fallback: str = "source", max_len: int = 100) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"https?://", "", value, flags=re.I)
    value = value.replace("/", "_").replace("\\", "_")
    value = re.sub(r"[^0-9A-Za-zÄÖÜäöüß._ -]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value or fallback)[:max_len]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SearchHit:
    rank: int
    title: str
    url: str
    snippet: str = ""   # discovery-only; never evidence
    provider: str = ""


@dataclass
class FetchedSource:
    rank: int
    title: str
    url: str
    final_url: str
    text: str
    content_type: str
    raw: bytes
    retrieved_at: str
    published_at: str = ""
    publisher: str = ""
    content_hash: str = ""
    fetch_error: str = ""
    search_provider: str = ""
    search_rank: int = 0
    http_status: int = 0
    redirect_count: int = 0


@dataclass
class RenderedSnapshot:
    pdf: bytes
    requested_url: str
    final_url: str
    title: str
    sha256: str
    elapsed_ms: int = 0
    media: str = "screen"
    video_posters: int = 0
    cookie_consent: str = "none"
    cookie_actions: int = 0
    overlays_dismissed: int = 0
    overlays_removed: int = 0
    dom_modified: bool = False
    landscape: bool = True
    viewport: str = "1440x900"
    state_reused: bool = False
    state_persisted: bool = False


@dataclass
class WebEvidence:
    index: int
    title: str
    url: str
    final_url: str
    publisher: str
    published_at: str
    retrieved_at: str
    content_type: str
    content_hash: str
    evidence_text: str
    relevance_score: float
    relevance_reason: str
    archive_path: str = ""
    archive_raw_path: str = ""
    archive_pdf_path: str = ""
    archive_metadata_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _PageParser(HTMLParser):
    BLOCKS = {"p", "div", "li", "tr", "td", "br", "h1", "h2", "h3", "h4", "h5", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.skip = 0
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or attrs_dict.get("itemprop") or "").strip().lower()
            value = attrs_dict.get("content", "").strip()
            if key and value and key not in self.meta:
                self.meta[key] = value
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
            return
        if tag == "title":
            self.in_title = False
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def title(self) -> str:
        return re.sub(r"\s+", " ", html.unescape("".join(self.title_parts))).strip()

    def published(self) -> str:
        for key in (
            "article:published_time", "datepublished", "date", "publishdate",
            "pubdate", "dc.date", "dc.date.issued", "citation_publication_date",
        ):
            if self.meta.get(key):
                return self.meta[key]
        return ""

    def publisher(self) -> str:
        for key in ("og:site_name", "publisher", "application-name"):
            if self.meta.get(key):
                return self.meta[key]
        return ""


def _decode_html(data: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type or "", flags=re.I)
    charset = match.group(1).strip('"\'') if match else "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf ist für PDF-Webquellen nicht installiert") from exc
    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            value = page.extract_text() or ""
        except Exception:
            value = ""
        if value.strip():
            parts.append(value)
    return "\n\n".join(parts).strip()


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolve_host_ips(host: str, port: int) -> list[str]:
    host = str(host or "").strip().rstrip(".")
    if not host:
        return []
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return []
    return sorted({str(item[4][0]) for item in infos if item and item[4]})


def _validated_connect_targets(
    url: str,
    allow_private: bool = False,
    *,
    allowed_ports: set[int] | None = None,
) -> list[tuple[str, str, str]]:
    """Return IP-pinned request targets for one validated HTTP(S) URL.

    DNS is resolved exactly here. The caller connects to the returned IP
    literal, while retaining the original Host header and TLS SNI hostname.
    This closes the validation/connect DNS-rebinding window.
    """
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeError("invalid URL") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("unsupported/non-HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("userinfo in URLs is not allowed")
    if allowed_ports is not None and port not in allowed_ports:
        raise RuntimeError(f"destination port {port} is not allowed")

    folded = parsed.hostname.casefold().rstrip(".")
    if folded == "localhost" or folded.endswith(".localhost"):
        if not allow_private:
            raise RuntimeError("private/local destination blocked")

    addresses = _resolve_host_ips(parsed.hostname, port)
    if not addresses:
        raise RuntimeError("DNS resolution failed")
    if not allow_private and any(not _is_public_ip(value) for value in addresses):
        raise RuntimeError("private/local destination blocked")

    default_port = 443 if parsed.scheme == "https" else 80
    display_host = parsed.hostname
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    host_header = display_host if port == default_port else f"{display_host}:{port}"

    original = httpx.URL(str(url))
    targets: list[tuple[str, str, str]] = []
    for address in addresses:
        pinned = original.copy_with(host=address)
        targets.append((str(pinned), host_header, parsed.hostname))
    return targets


def _is_public_host(host: str) -> bool:
    values = _resolve_host_ips(host, 443)
    return bool(values) and all(_is_public_ip(value) for value in values)


def _validate_public_url(
    url: str,
    allow_private: bool = False,
    *,
    allowed_ports: set[int] | None = None,
) -> None:
    _validated_connect_targets(
        url,
        allow_private,
        allowed_ports=allowed_ports,
    )


class WebSearchProvider:
    def __init__(self, cfg: dict[str, Any]):
        search = cfg.get("search", {}) or {}
        self.provider = str(search.get("provider") or "searxng").strip().lower()
        self.url = str(search.get("url") or "").strip().rstrip("/")
        self.api_key_env = str(search.get("api_key_env") or "WEB_SEARCH_API_KEY").strip()
        self.timeout = float(search.get("timeout") or 20)
        self.verify_tls = _truthy(search.get("verify_tls"), True)
        self.max_results = max(1, min(int(search.get("max_results") or 10), 30))

    def _key(self) -> str:
        return secret_env(self.api_key_env, "") if self.api_key_env else ""

    def readiness(self) -> tuple[bool, str]:
        if self.provider == "brave":
            if not self._key():
                return False, f"Brave API key missing in {self.api_key_env}"
            return True, ""
        if self.provider == "searxng":
            if not self.url:
                return False, "web.search.url is required for searxng"
            return True, ""
        return False, f"unknown web search provider {self.provider!r}"

    async def search(self, query: str) -> list[SearchHit]:
        query = apply_policy_hook(
            OUTBOUND_QUERY,
            content=query,
            metadata={"provider": self.provider, "target": self.url},
        )
        if not isinstance(query, str):
            raise TypeError("outbound_query policy hook must return str content")
        if self.provider == "brave":
            return await self._brave(query)
        if self.provider == "searxng":
            return await self._searxng(query)
        raise RuntimeError(f"unknown web search provider {self.provider!r}")

    async def _brave(self, query: str) -> list[SearchHit]:
        key = self._key()
        if not key:
            raise RuntimeError(f"Brave API key missing in {self.api_key_env}")
        url = self.url or "https://api.search.brave.com/res/v1/web/search"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as client:
            response = await client.get(
                url,
                params={"q": query, "count": self.max_results, "safesearch": "moderate"},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
            )
            response.raise_for_status()
            data = response.json()
        rows = ((data.get("web") or {}).get("results") or [])
        hits: list[SearchHit] = []
        for row in rows[: self.max_results]:
            url_value = str(row.get("url") or "").strip()
            if not url_value:
                continue
            hits.append(SearchHit(len(hits) + 1, str(row.get("title") or url_value), url_value, str(row.get("description") or ""), "brave"))
        return hits

    async def _searxng(self, query: str) -> list[SearchHit]:
        if not self.url:
            raise RuntimeError("web.search.url is required for searxng")
        endpoint = self.url if self.url.endswith("/search") else self.url + "/search"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as client:
            response = await client.get(endpoint, params={"q": query, "format": "json"})
            response.raise_for_status()
            data = response.json()
        hits: list[SearchHit] = []
        for row in (data.get("results") or [])[: self.max_results]:
            url_value = str(row.get("url") or "").strip()
            if not url_value:
                continue
            hits.append(SearchHit(len(hits) + 1, str(row.get("title") or url_value), url_value, str(row.get("content") or ""), "searxng"))
        return hits


class WebFetcher:
    def __init__(self, cfg: dict[str, Any]):
        fetch = cfg.get("fetch", {}) or {}
        self.timeout = float(fetch.get("timeout") or 25)
        self.verify_tls = _truthy(fetch.get("verify_tls"), True)
        self.max_bytes = max(100_000, int(fetch.get("max_bytes") or 15_000_000))
        self.max_text_chars = max(10_000, int(fetch.get("max_text_chars") or 300_000))
        self.user_agent = str(fetch.get("user_agent") or "Nextcloud-RAG-WebEvidence/0.8")
        self.max_redirects = max(0, min(int(fetch.get("max_redirects") or 5), 10))
        self.allow_private = _truthy(fetch.get("allow_private"), False)
        raw_ports = fetch.get("allowed_ports")
        if raw_ports is None:
            raw_ports = [80, 443]
        if isinstance(raw_ports, (str, int)):
            raw_ports = [raw_ports]
        self.allowed_ports = {
            int(value)
            for value in raw_ports
            if str(value).strip() and 1 <= int(value) <= 65535
        }
        if not self.allowed_ports:
            raise ValueError("web.fetch.allowed_ports must contain at least one valid TCP port")

    async def fetch(self, hit: SearchHit) -> FetchedSource:
        retrieved = _utc_now().isoformat()
        url = hit.url
        current = url
        final_url = current
        content_type = ""
        data = b""
        status_code = 0
        redirect_count = 0
        try:
            # No environment proxy is allowed on untrusted public-Web fetches.
            # Every connect is made to an IP returned by the validation DNS
            # lookup; original Host/SNI is retained for virtual hosting/TLS.
            # A fresh HTTPX client is used for every redirect hop so a pooled
            # TLS connection authenticated for host A can never be reused for
            # host B merely because both names resolve to the same pinned IP.
            for _ in range(self.max_redirects + 1):
                current = apply_policy_hook(
                    PRE_FETCH,
                    content=current,
                    metadata={
                        "source": "web",
                        "search_provider": hit.provider,
                        "original_url": url,
                        "redirect_count": redirect_count,
                    },
                )
                if not isinstance(current, str):
                    raise TypeError("pre_fetch policy hook must return str content")

                targets = _validated_connect_targets(
                    current,
                    self.allow_private,
                    allowed_ports=self.allowed_ports,
                )
                redirect_target = ""
                last_transport_error: Exception | None = None

                for pinned_url, host_header, sni_hostname in targets:
                    headers = {
                        "Host": host_header,
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5",
                    }
                    extensions = (
                        {"sni_hostname": sni_hostname}
                        if urlparse(current).scheme == "https"
                        else None
                    )
                    try:
                        async with httpx.AsyncClient(
                            timeout=self.timeout,
                            verify=self.verify_tls,
                            follow_redirects=False,
                            trust_env=False,
                        ) as client:
                            async with client.stream(
                                "GET",
                                pinned_url,
                                headers=headers,
                                extensions=extensions,
                            ) as response:
                                status_code = int(response.status_code)
                                # User-visible/archive provenance keeps the
                                # original hostname URL, never the pinned IP.
                                final_url = current

                                if response.status_code in {301, 302, 303, 307, 308}:
                                    location = response.headers.get("location")
                                    if not location:
                                        raise RuntimeError("redirect response has no location")
                                    if redirect_count >= self.max_redirects:
                                        raise RuntimeError(
                                            f"source exceeds max_redirects={self.max_redirects}"
                                        )
                                    redirect_target = urljoin(current, location)
                                    break

                                response.raise_for_status()
                                content_type = str(
                                    response.headers.get("content-type")
                                    or "application/octet-stream"
                                ).lower()
                                content_length = response.headers.get("content-length")
                                if content_length:
                                    try:
                                        if int(content_length) > self.max_bytes:
                                            raise RuntimeError(
                                                f"source exceeds max_bytes={self.max_bytes}"
                                            )
                                    except ValueError:
                                        pass

                                chunks = bytearray()
                                async for chunk in response.aiter_bytes():
                                    if len(chunks) + len(chunk) > self.max_bytes:
                                        raise RuntimeError(
                                            f"source exceeds max_bytes={self.max_bytes}"
                                        )
                                    chunks.extend(chunk)
                                data = bytes(chunks)
                                break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        last_transport_error = exc
                        continue

                if redirect_target:
                    current = redirect_target
                    redirect_count += 1
                    continue
                if data:
                    break
                if last_transport_error is not None:
                    raise last_transport_error
                raise RuntimeError("no reachable validated destination")

            if not data:
                raise RuntimeError("no HTTP response body")

            data = apply_policy_hook(
                POST_FETCH,
                content=data,
                metadata={
                    "source": "web",
                    "requested_url": current,
                    "final_url": final_url,
                    "content_type": content_type,
                    "http_status": status_code,
                },
            )
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError("post_fetch policy hook must return bytes content")
            data = bytes(data)
            if len(data) > self.max_bytes:
                raise RuntimeError(f"source exceeds max_bytes={self.max_bytes}")

            title = hit.title
            published = ""
            publisher = urlparse(final_url).hostname or ""
            if "pdf" in content_type or final_url.casefold().endswith(".pdf"):
                text = _extract_pdf_text(data)
                normalized_type = "application/pdf"
            elif "html" in content_type or data[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
                decoded = _decode_html(data, content_type)
                parser = _PageParser()
                parser.feed(decoded)
                parser.close()
                text = parser.text()
                title = parser.title() or title
                published = parser.published()
                publisher = parser.publisher() or publisher
                normalized_type = "text/html"
            elif content_type.startswith("text/"):
                text = _decode_html(data, content_type)
                normalized_type = content_type.split(";", 1)[0]
            else:
                raise RuntimeError(f"unsupported content type {content_type}")

            text = text.strip()[: self.max_text_chars]
            if len(text) < 120:
                raise RuntimeError("fetched source contains too little extractable text")
            digest = hashlib.sha256(data).hexdigest()
            return FetchedSource(
                rank=hit.rank,
                title=title,
                url=hit.url,
                final_url=final_url,
                text=text,
                content_type=normalized_type,
                raw=data,
                retrieved_at=retrieved,
                published_at=published,
                publisher=publisher,
                content_hash=digest,
                search_provider=hit.provider,
                search_rank=hit.rank,
                http_status=status_code,
                redirect_count=redirect_count,
            )
        except Exception as exc:
            return FetchedSource(
                rank=hit.rank,
                title=hit.title,
                url=hit.url,
                final_url=current,
                text="",
                content_type="",
                raw=b"",
                retrieved_at=retrieved,
                content_hash="",
                fetch_error=f"{type(exc).__name__}: {exc}",
                search_provider=hit.provider,
                search_rank=hit.rank,
                http_status=status_code,
                redirect_count=redirect_count,
            )


_RELEVANCE_SCHEMA: dict[str, Any] = {
    # Keep this schema inside the strict Structured Outputs subset used by
    # native OpenAI. Range/ID validation remains application-side below.
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "relevant", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sources"],
    "additionalProperties": False,
}


class RelevanceGate:
    def __init__(self, cfg: dict[str, Any]):
        rel = cfg.get("relevance", {}) or {}
        backend_name = str(rel.get("backend") or os.getenv("WEB_LLM_BACKEND") or os.getenv("LLM_BACKEND") or "ollama").strip().lower()
        base_url = str(rel.get("url") or os.getenv("WEB_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        model = str(rel.get("model") or os.getenv("WEB_LLM_MODEL") or os.getenv("EVIDENCE_MODEL") or os.getenv("LLM_MODEL") or "qwen3:8b")
        key_env = str(rel.get("api_key_env") or "WEB_LLM_API_KEY").strip()
        api_key = secret_env(key_env, "") or secret_env("LLM_API_KEY", "")
        self.backend = build_llm_backend(
            backend_name,
            base_url=base_url,
            model=model,
            api_key=api_key,
            verify_tls=_truthy(rel.get("verify_tls"), True),
            ca_file=str(rel.get("ca_file") or "").strip() or None,
        )
        self.model = model
        self.timeout = float(rel.get("timeout") or 180)
        self.num_ctx = int(rel.get("num_ctx") or 16384)
        self.num_predict = int(rel.get("num_predict") or 800)
        self.max_sources = max(1, min(int(rel.get("max_sources") or 5), 12))
        self.min_score = float(rel.get("min_score") or 0.58)
        self.preview_chars = max(800, int(rel.get("preview_chars") or 4200))

    async def evaluate(self, query: str, sources: list[FetchedSource]) -> list[tuple[FetchedSource, float, str]]:
        selected, _ = await self.evaluate_with_decisions(query, sources)
        return selected

    async def evaluate_with_decisions(
        self, query: str, sources: list[FetchedSource]
    ) -> tuple[list[tuple[FetchedSource, float, str]], list[dict[str, Any]]]:
        usable = [s for s in sources if s.text and not s.fetch_error]
        if not usable:
            return [], []
        records: list[dict[str, Any]] = []
        for i, source in enumerate(usable, start=1):
            # The relevance model sees only fetched text, never search snippets.
            # JSON serialization prevents fetched content from impersonating
            # server-generated source boundaries or metadata fields.
            preview = best_passage(query, source.text, self.preview_chars)
            records.append(
                {
                    "citation": f"[W{i}]",
                    "id": i,
                    "title": str(source.title or "")[:512],
                    "url": str(source.final_url or source.url or "")[:2048],
                    "published_at": str(source.published_at or "")[:128],
                    "text": preview,
                }
            )
        system = (
            "Du bewertest bereits tatsächlich abgerufene Webquellen für eine Recherche. "
            "Suchmaschinen-Snippets sind nicht Teil dieser Eingabe. Bewerte die Relevanz zur erkennbaren "
            "Informationsabsicht der Suchanfrage, nicht nur dazu, ob eine grammatisch ausformulierte Frage "
            "beantwortet wird. Die Suchanfrage kann auch nur aus einem Namen oder kurzen Suchbegriff bestehen. "
            "In diesem Fall ist eine Quelle relevant, wenn ihr abgerufener Text den gesuchten Gegenstand "
            "substanziell behandelt oder zu seiner eindeutigen Identifikation beiträgt. Verwirf eine Quelle "
            "nicht allein deshalb, weil die Suchanfrage keine vollständige Frage ist. Bevorzuge Primärquellen "
            "und konkrete Tatsachenbelege. Gib für JEDE vorgelegte SOURCE-ID genau eine Entscheidung zurück; "
            "auch irrelevante Quellen dürfen nicht ausgelassen werden. Das JSON-Wurzelobjekt hat genau das Feld "
            "sources; jedes Element enthält genau id, relevant, score und reason. Gib ausschließlich JSON zurück."
        )
        system = guarded_evidence_prompt(system)
        user = (
            "FRAGE:\n"
            + query
            + "\n\nQUELLEN_JSON:\n"
            + serialize_evidence_records(records, kind="public_web_relevance")
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        expected_ids = set(range(1, len(usable) + 1))

        def parse_json(raw: str) -> dict[str, Any]:
            raw = str(raw or "").strip()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                start, end = raw.find("{"), raw.rfind("}")
                if start < 0 or end <= start:
                    raise
                value = json.loads(raw[start:end + 1])
            if not isinstance(value, dict):
                raise ValueError("web relevance JSON root must be an object")
            return value

        def validate_coverage(value: dict[str, Any]) -> dict[str, Any]:
            raw_sources = value.get("sources")
            if not isinstance(raw_sources, list):
                raise ValueError("web relevance response has no sources array")
            returned_ids: list[int] = []
            for item in raw_sources:
                if not isinstance(item, dict):
                    continue
                try:
                    returned_ids.append(int(item.get("id")))
                except (TypeError, ValueError):
                    continue
            returned_set = set(returned_ids)
            missing = sorted(expected_ids - returned_set)
            unexpected = sorted(returned_set - expected_ids)
            duplicates = sorted({idx for idx in returned_ids if returned_ids.count(idx) > 1})
            if missing or unexpected or duplicates or len(returned_ids) != len(expected_ids):
                raise ValueError(
                    "web relevance incomplete source decisions: "
                    f"expected={sorted(expected_ids)} returned={returned_ids} "
                    f"missing={missing} unexpected={unexpected} duplicates={duplicates}"
                )
            return value

        try:
            response = await self.backend.complete(
                messages,
                options={"temperature": 0.0, "num_ctx": self.num_ctx, "num_predict": self.num_predict},
                think=False,
                response_format=_RELEVANCE_SCHEMA,
                timeout=self.timeout,
            )
            data = validate_coverage(parse_json(str(response.get("content") or "")))
        except (json.JSONDecodeError, ValueError) as first_exc:
            # A relevance decision is required for every fetched source. An
            # empty or partial array is not equivalent to "all irrelevant";
            # it is an incomplete model response and must be retried/failed.
            log.warning(
                "web relevance returned invalid/incomplete JSON; retrying once: %s; finish_reason=%r",
                first_exc,
                response.get("done_reason") if 'response' in locals() else None,
            )
            retry_messages = [
                {
                    "role": "system",
                    "content": (
                        system
                        + " Du MUSST genau eine Entscheidung für JEDE vorgelegte SOURCE-ID zurückgeben. "
                          "Lasse keine Quelle aus. Auch irrelevante Quellen erhalten einen Eintrag mit "
                          "relevant=false, score=0 und kurzem reason. Antworte ausschließlich mit syntaktisch gültigem JSON."
                    ),
                },
                {"role": "user", "content": user},
            ]
            response = await self.backend.complete(
                retry_messages,
                options={
                    "temperature": 0.0,
                    "num_ctx": self.num_ctx,
                    "num_predict": max(self.num_predict * 2, self.num_predict + 512),
                },
                think=False,
                response_format=_RELEVANCE_SCHEMA,
                timeout=self.timeout,
            )
            try:
                data = validate_coverage(parse_json(str(response.get("content") or "")))
            except (json.JSONDecodeError, ValueError) as second_exc:
                raise RuntimeError(
                    "web relevance model returned invalid/incomplete JSON twice: "
                    f"{second_exc}; finish_reason={response.get('done_reason')!r}"
                ) from second_exc
        decisions: dict[int, tuple[bool, float, str]] = {}
        for item in data.get("sources") or []:
            try:
                idx = int(item.get("id"))
                score = max(0.0, min(1.0, float(item.get("score") or 0.0)))
            except Exception:
                log.debug("Ignoring malformed web relevance decision: %r", item, exc_info=True)
                continue
            decisions[idx] = (bool(item.get("relevant")), score, str(item.get("reason") or "").strip())
        selected: list[tuple[FetchedSource, float, str]] = []
        decision_log: list[dict[str, Any]] = []
        for idx, source in enumerate(usable, start=1):
            relevant, score, reason = decisions.get(idx, (False, 0.0, "no model decision"))
            accepted = bool(relevant and score >= self.min_score)
            decision_log.append({
                "id": idx,
                "search_rank": source.search_rank,
                "requested_url": source.url,
                "final_url": source.final_url,
                "title": source.title[:120],
                "relevant": bool(relevant),
                "score": round(score, 3),
                "accepted": accepted,
                "selected": False,
                "reason": reason[:180],
            })
            if accepted:
                selected.append((source, score, reason))
        selected.sort(key=lambda x: x[1], reverse=True)
        selected = selected[: self.max_sources]
        selected_keys = {(src.search_rank, src.url, src.final_url) for src, _, _ in selected}
        for item in decision_log:
            item["selected"] = (
                item.get("search_rank"), item.get("requested_url"), item.get("final_url")
            ) in selected_keys
        log.info(
            "web relevance decisions: query=%r min_score=%.2f decisions=%s",
            query[:160],
            self.min_score,
            decision_log,
        )
        return selected, decision_log


def _chunk_text(text: str, size: int = 2200, overlap: int = 300, max_chunks: int = 40) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", start + int(size * 0.65), end), text.rfind(". ", start + int(size * 0.65), end))
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [c for c in chunks if c]


def _lexical_passage(query: str, chunks: list[str], max_chars: int) -> str:
    """Cheap language-agnostic fallback for passage selection.

    This is deliberately not a document/domain taxonomy. It only rewards the
    literal query phrase and token overlap inside text that was actually
    fetched, so CPU-only installations remain useful when no reranker is
    available.
    """
    if not chunks:
        return ""
    normalized_query = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
    query_terms = {
        token for token in re.findall(r"[0-9A-Za-zÄÖÜäöüß]+", normalized_query)
        if len(token) >= 2
    }

    def score(chunk: str) -> tuple[float, int]:
        folded = chunk.casefold()
        terms = set(re.findall(r"[0-9A-Za-zÄÖÜäöüß]+", folded))
        overlap = len(query_terms & terms)
        coverage = overlap / max(1, len(query_terms))
        phrase_bonus = 2.0 if normalized_query and normalized_query in folded else 0.0
        return (phrase_bonus + coverage, overlap)

    ranked = sorted(chunks, key=score, reverse=True)
    selected = [ranked[0]]
    if len(ranked) > 1 and len(selected[0]) < max_chars * 0.7:
        selected.append(ranked[1])
    return "\n\n".join(selected)[:max_chars].strip()


def best_passage(query: str, text: str, max_chars: int = 4200) -> str:
    chunks = _chunk_text(text)
    if not chunks:
        return ""
    # Prefer the configured CrossEncoder/TEI reranker. If it is unavailable,
    # fall back to lightweight lexical passage selection over fetched text.
    # Search-engine snippets are never used as answer evidence.
    try:
        from rag.reranker import reranker
        if getattr(reranker, "backend", "") == "none":
            return _lexical_passage(query, chunks, max_chars)
        scored = reranker.score(query, chunks)
        ranked = sorted(zip(chunks, scored), key=lambda x: float(x[1].get("score") or 0.0), reverse=True)
        selected = [ranked[0][0]]
        if len(ranked) > 1 and len(selected[0]) < max_chars * 0.7:
            selected.append(ranked[1][0])
        return "\n\n".join(selected)[:max_chars].strip()
    except Exception as exc:
        log.warning("web passage reranker failed, using lexical fetched-text fallback: %s", exc)
        return _lexical_passage(query, chunks, max_chars)


class PlaywrightRenderer:
    """Optional fail-open client for the local hardened Playwright helper."""

    def __init__(self, cfg: dict[str, Any]):
        archive = cfg.get("archive", {}) or {}
        render_cfg = archive.get("renderer", {}) or {}
        self.enabled = bool(
            _truthy(archive.get("write_rendered_pdf"), True)
            and _truthy(render_cfg.get("enabled"), False)
        )
        self.url = str(render_cfg.get("url") or "http://127.0.0.1:8090/render").strip()
        self.timeout = float(render_cfg.get("timeout") or 45)
        self.verify_tls = _truthy(render_cfg.get("verify_tls"), True)
        self.max_bytes = max(1_000_000, int(render_cfg.get("max_bytes") or 52_428_800))
        self.landscape = _truthy(render_cfg.get("landscape"), True)
        self.prefer_css_page_size = _truthy(render_cfg.get("prefer_css_page_size"), False)
        viewport_cfg = render_cfg.get("viewport", {}) or {}
        self.viewport_width = max(800, min(3840, int(viewport_cfg.get("width") or 1440)))
        self.viewport_height = max(600, min(2160, int(viewport_cfg.get("height") or 900)))
        self.persist_state = _truthy(render_cfg.get("persist_state"), True)
        cleanup_cfg = render_cfg.get("cleanup", {}) or {}
        self.cleanup_cookie_consent = str(cleanup_cfg.get("cookie_consent") or "off").strip().lower()
        self.cleanup_dismiss_overlays = _truthy(cleanup_cfg.get("dismiss_overlays"), False)
        self.cleanup_remove_overlays = _truthy(cleanup_cfg.get("remove_overlays"), False)

    async def render(self, source: FetchedSource) -> RenderedSnapshot | None:
        if not self.enabled or source.content_type != "text/html":
            return None
        render_url = apply_policy_hook(
            PRE_FETCH,
            content=source.final_url,
            metadata={"source": "playwright", "original_url": source.url},
        )
        if not isinstance(render_url, str):
            raise TypeError("pre_fetch policy hook must return str content")
        html_snapshot = _decode_html(source.raw, source.content_type)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            verify=self.verify_tls,
            trust_env=False,
        ) as client:
            response = await client.post(
                self.url,
                json={
                    "url": render_url,
                    "html": html_snapshot,
                    "landscape": self.landscape,
                    "prefer_css_page_size": self.prefer_css_page_size,
                    "viewport_width": self.viewport_width,
                    "viewport_height": self.viewport_height,
                    # rc1.2 renderer is deliberately network-free. Persistent
                    # cookies/state only make sense for live navigation.
                    "persist_state": False,
                    "cleanup_cookie_consent": self.cleanup_cookie_consent,
                    "cleanup_dismiss_overlays": self.cleanup_dismiss_overlays,
                    "cleanup_remove_overlays": self.cleanup_remove_overlays,
                },
            )
            response.raise_for_status()
            pdf = response.content
            if not pdf.startswith(b"%PDF-"):
                raise RuntimeError("renderer returned non-PDF payload")
            if len(pdf) > self.max_bytes:
                raise RuntimeError(f"rendered PDF exceeds max_bytes={self.max_bytes}")
            headers = response.headers
            return RenderedSnapshot(
                pdf=pdf,
                requested_url=unquote(str(headers.get("x-render-requested-url") or source.final_url)),
                final_url=unquote(str(headers.get("x-render-final-url") or source.final_url)),
                title=unquote(str(headers.get("x-render-title") or source.title)),
                sha256=str(headers.get("x-render-sha256") or hashlib.sha256(pdf).hexdigest()),
                elapsed_ms=int(headers.get("x-render-elapsed-ms") or 0),
                media=str(headers.get("x-render-media") or "screen"),
                video_posters=int(headers.get("x-render-video-posters") or 0),
                cookie_consent=str(headers.get("x-render-cookie-consent") or "none"),
                cookie_actions=int(headers.get("x-render-cookie-actions") or 0),
                overlays_dismissed=int(headers.get("x-render-overlays-dismissed") or 0),
                overlays_removed=int(headers.get("x-render-overlays-removed") or 0),
                dom_modified=str(headers.get("x-render-dom-modified") or "false").strip().lower() in {"1", "true", "yes", "on"},
                landscape=str(headers.get("x-render-landscape") or "true").strip().lower() in {"1", "true", "yes", "on"},
                viewport=str(headers.get("x-render-viewport") or f"{self.viewport_width}x{self.viewport_height}"),
                state_reused=str(headers.get("x-render-state-reused") or "false").strip().lower() in {"1", "true", "yes", "on"},
                state_persisted=str(headers.get("x-render-state-persisted") or "false").strip().lower() in {"1", "true", "yes", "on"},
            )


class NextcloudWebArchive:
    """Per-user WebDAV archive for selected public evidence.

    Layout:
        <root>/YYYY-MM/DD-HHMMSS-xxxx/
            recherche.md
            fetch-log.jsonl
            01-source.txt
            .01-source.metadata.json
            01-source.pdf        (original PDF or optional Playwright HTML render)
            01-source.html       (optional raw HTML snapshot)

    One directory is one web-research run.  The answer model output is written
    in a second, fail-open finalize call after the provider has generated it.
    """

    LLM_START = "<!-- LLM_OUTPUT_START -->"
    LLM_END = "<!-- LLM_OUTPUT_END -->"
    META_START = "<!-- ANSWER_META_START -->"
    META_END = "<!-- ANSWER_META_END -->"

    def __init__(self, cfg: dict[str, Any], app_cfg: dict[str, Any]):
        archive = cfg.get("archive", {}) or {}
        self.cfg = cfg
        self.enabled = _truthy(archive.get("enabled"), True)
        # Multi-user archive targets are admin-managed in runtime/users.sqlite.
        # archive.root is retained only as an explicit single-user/upgrade fallback.
        self.legacy_root = str(archive.get("root") or "").strip(" /")
        self.write_text_snapshot = _truthy(archive.get("write_text_snapshot"), True)
        self.write_research_markdown = _truthy(archive.get("write_research_markdown"), True)
        self.write_raw_pdf = _truthy(archive.get("write_raw_pdf"), True)
        self.write_raw_html = _truthy(archive.get("write_raw_html"), False)
        self.write_fetch_log = _truthy(archive.get("write_fetch_log"), True)
        self.write_metadata_json = _truthy(archive.get("write_metadata_json"), True)
        self.renderer = PlaywrightRenderer(cfg)
        render_cfg = archive.get("renderer", {}) or {}
        # Rendering HTML pages to PDFs is archival enrichment, not answer evidence.
        # Run it after the synchronous archive write by default so slow Playwright
        # pages never hold the chat response open.
        self.render_in_background = _truthy(render_cfg.get("background"), True)
        self.render_background_runs = max(1, min(int(render_cfg.get("background_runs") or 1), 4))
        self._render_gate = asyncio.Semaphore(self.render_background_runs)
        self._render_tasks: set[asyncio.Task[Any]] = set()
        self.timeout = float(archive.get("timeout") or 30)
        self.nextcloud_verify = nextcloud_verify_value(app_cfg, archive, "acl")
        self.acl = NextcloudLiveAcl(app_cfg)
        store_path = str(
            cfg_get(app_cfg, "auth.credential_store", default=cfg_get(app_cfg, "acl.credential_store", default="runtime/users.sqlite"))
            or "runtime/users.sqlite"
        )
        self.store = CredentialStore(store_path)
        base = str(cfg_get(app_cfg, "nextcloud.base_url", default="") or "").rstrip("/")
        self.dav_base = base + "/remote.php/dav/files" if base else ""

    def user_settings(self, rag_user_id: str | None) -> UserWebSettings | None:
        if self.acl.identity_mode != "credential_store":
            return None
        identity = str(rag_user_id or "").strip()
        if not identity:
            return None
        canonical = self.store.get_canonical_user_for_identity(identity)
        if canonical is None or not canonical.enabled:
            return None
        return self.store.get_web_settings(canonical.canonical_user_id)

    def _root_for_user(self, rag_user_id: str | None) -> str:
        if self.acl.identity_mode == "credential_store":
            settings = self.user_settings(rag_user_id)
            if settings is None or not settings.enabled or not settings.archive_enabled:
                return ""
            return settings.target_path.strip(" /")
        return self.legacy_root

    def _credential(self, rag_user_id: str | None) -> NextcloudCredential:
        return self.acl.credential_for_user(rag_user_id)

    def _url(self, credential: NextcloudCredential, relpath: str) -> str:
        pieces = [quote(credential.username, safe="")] + [quote(p, safe="") for p in relpath.strip("/").split("/") if p]
        return self.dav_base.rstrip("/") + "/" + "/".join(pieces)

    async def _ensure_dir(self, client: httpx.AsyncClient, credential: NextcloudCredential, relpath: str) -> None:
        accum: list[str] = []
        for part in [p for p in relpath.strip("/").split("/") if p]:
            accum.append(part)
            url = self._url(credential, "/".join(accum))
            response = await client.request("MKCOL", url, auth=(credential.username, credential.password))
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
            content=content,
            metadata={
                "source": "web_archive",
                "target": target,
                "content_type": content_type,
            },
        )
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("pre_persist policy hook must return bytes content")
        content = bytes(content)
        url = self._url(credential, target)
        response = await client.put(
            url,
            content=content,
            headers={"Content-Type": content_type},
            auth=(credential.username, credential.password),
        )
        response.raise_for_status()
        file_id = ""
        for header in ("OC-FileId", "X-OC-FileId", "FileId"):
            value = str(response.headers.get(header) or "").strip()
            if value.isdigit():
                file_id = value
                break
        if not file_id:
            body = (
                '<?xml version="1.0" encoding="utf-8" ?>\n'
                '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                '<d:prop><oc:fileid/></d:prop></d:propfind>'
            ).encode("utf-8")
            try:
                prop = await client.request(
                    "PROPFIND", url, content=body,
                    headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                    auth=(credential.username, credential.password),
                )
                if prop.status_code == 207:
                    tree = ET.fromstring(prop.content)
                    node = tree.find(".//{http://owncloud.org/ns}fileid")
                    if node is not None and str(node.text or "").strip().isdigit():
                        file_id = str(node.text).strip()
            except Exception as exc:
                log.debug("web archive fileid lookup failed for %s: %s", target, exc)
        if file_id:
            register_document(
                f"files:{file_id}", "web_archive", source_path=target,
                classification_source="web_archive",
            )
            return file_id
        return None

    def _validated_archive_path(self, relpath: str, rag_user_id: str | None) -> str:
        clean = str(relpath or "").strip(" /")
        root = self._root_for_user(rag_user_id).strip(" /")
        if not clean or any(part in {".", ".."} for part in clean.split("/")):
            raise ValueError("invalid web archive path")
        if not root:
            raise ValueError("no web archive target configured for current canonical user")
        if not (clean == root or clean.startswith(root + "/")):
            raise ValueError("web archive path is outside current user's configured archive root")
        return clean

    async def read_text_snapshot(
        self, relpath: str, *, rag_user_id: str | None
    ) -> dict[str, Any] | None:
        """Read an archived web text snapshot directly with user WebDAV.

        This supports immediate /use:Wn without waiting for the next ES sync.
        The archived snapshot is used deliberately instead of refetching the URL.
        """
        if not self.enabled or not self.dav_base:
            return None
        clean = self._validated_archive_path(relpath, rag_user_id)
        if not clean.lower().endswith(".txt"):
            return None
        credential = self._credential(rag_user_id)
        url = self._url(credential, clean)
        verify = self.nextcloud_verify
        async with httpx.AsyncClient(timeout=self.timeout, verify=verify) as client:
            response = await client.get(
                url,
                auth=(credential.username, credential.password),
                headers={"Accept": "text/plain,*/*;q=0.8"},
            )
            if response.status_code in {401, 403, 404}:
                return None
            response.raise_for_status()
            raw = response.content

            file_id = ""
            propfind_body = (
                '<?xml version="1.0" encoding="utf-8" ?>\n'
                '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">\n'
                '  <d:prop><oc:fileid/></d:prop>\n'
                '</d:propfind>'
            ).encode("utf-8")
            try:
                prop = await client.request(
                    "PROPFIND",
                    url,
                    content=propfind_body,
                    headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                    auth=(credential.username, credential.password),
                )
                if prop.status_code == 207:
                    tree = ET.fromstring(prop.content)
                    node = tree.find(".//{http://owncloud.org/ns}fileid")
                    if node is not None and str(node.text or "").strip().isdigit():
                        file_id = str(node.text).strip()
            except Exception as exc:
                log.debug("web archive fileid lookup failed for %s: %s", clean, exc)

        text = raw.decode("utf-8", errors="replace")
        title = Path(clean).name
        source_url = ""
        content_text = text
        header, sep, body = text.partition("\n\nTEXT:\n")
        if sep:
            content_text = body.strip()
            for line in header.splitlines():
                key, colon, value = line.partition(":")
                if not colon:
                    continue
                key = key.strip().upper()
                value = value.strip()
                if key == "TITLE" and value:
                    title = value
                elif key == "SOURCE-URL" and value:
                    source_url = value

        document_id = (
            f"files:{file_id}"
            if file_id
            else "webarchive:" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:24]
        )
        return {
            "document_id": document_id,
            "title": title,
            "path": clean,
            "source_url": source_url,
            "context_text": content_text,
            "content_available": bool(content_text),
            "context_enriched": True,
            "source_origin": "web_archive",
            "webarchive_direct": True,
            "acl_verified_by": "webdav_get",
        }

    def _new_run_path(self, query: str, root: str) -> tuple[str, datetime, str]:
        # Folder timestamps intentionally use host-local time: they are for
        # human chronology in the user's Nextcloud, while source metadata keeps
        # precise timezone-aware retrieved_at values.
        now = datetime.now().astimezone()
        salt = hashlib.sha256(f"{query}\0{now.isoformat()}\0{os.getpid()}".encode("utf-8")).hexdigest()[:4]
        run_id = f"{now:%d-%H%M%S}-{salt}"
        return f"{root.strip(' /')}/{now:%Y-%m}/{run_id}", now, run_id

    @staticmethod
    def _snapshot_bytes(query: str, source: FetchedSource, score: float, reason: str) -> bytes:
        return (
            "CONTENT-KIND: WEB_SOURCE\n"
            f"SOURCE-URL: {source.final_url}\n"
            f"ORIGINAL-URL: {source.url}\n"
            f"TITLE: {source.title}\n"
            f"PUBLISHER: {source.publisher}\n"
            f"PUBLISHED-AT: {source.published_at}\n"
            f"RETRIEVED-AT: {source.retrieved_at}\n"
            f"CONTENT-TYPE: {source.content_type}\n"
            f"CONTENT-HASH: {source.content_hash}\n"
            f"SEARCH-PROVIDER: {source.search_provider}\n"
            f"SEARCH-RANK: {source.search_rank}\n"
            f"RELEVANCE-SCORE: {score:.4f}\n"
            f"RELEVANCE-REASON: {reason}\n"
            f"QUERY: {query}\n\n"
            "TEXT:\n" + source.text.rstrip() + "\n"
        ).encode("utf-8")

    def _research_markdown(
        self,
        *,
        query: str,
        run_id: str,
        created_at: datetime,
        rag_user_id: str | None,
        selected: list[tuple[FetchedSource, float, str]],
        source_records: list[dict[str, str]],
        stats: dict[str, Any],
        relevance_model: str = "",
        fetch_log_path: str = "",
    ) -> str:
        search = self.cfg.get("search", {}) or {}
        fetch = self.cfg.get("fetch", {}) or {}
        relevance = self.cfg.get("relevance", {}) or {}
        lines = [
            "# Web-Recherche",
            "",
            f"- Recherche-ID: `{run_id}`",
            f"- Zeitpunkt: {created_at.isoformat(timespec='seconds')}",
            f"- Benutzer: {rag_user_id or '(single-user/default)' }",
            "",
            "## Suchanfrage",
            "",
            str(query or "").strip(),
            "",
            "## Parameter",
            "",
            f"- Search provider: `{stats.get('search_provider') or search.get('provider') or ''}`",
            f"- Max. Suchtreffer: {int(search.get('max_results') or 10)}",
            f"- Search timeout: {search.get('timeout') or 20}s",
            f"- Fetch timeout: {fetch.get('timeout') or 25}s",
            f"- Max. Fetch-Größe: {int(fetch.get('max_bytes') or 15000000)} Bytes",
            f"- Relevance model: `{relevance_model or relevance.get('model') or os.getenv('WEB_LLM_MODEL') or os.getenv('EVIDENCE_MODEL') or os.getenv('LLM_MODEL') or ''}`",
            f"- Min. Relevance score: {float(relevance.get('min_score') or 0.58):.3f}",
            f"- Max. relevante Quellen: {int(relevance.get('max_sources') or 5)}",
            f"- Playwright-PDF: {'aktiv' if self.renderer.enabled else 'inaktiv'}",
            "",
            "## Laufstatistik",
            "",
            f"- Suchtreffer: {int(stats.get('searched') or 0)}",
            f"- Tatsächlich geladen: {int(stats.get('fetched') or 0)}",
            f"- Als relevant ausgewählt: {len(selected)}",
            f"- Fetch-Log: `{fetch_log_path or '-'}`",
            "",
            "## Verwendete Quellen",
            "",
        ]
        for idx, ((source, score, reason), record) in enumerate(zip(selected, source_records), start=1):
            lines.extend([
                f"### [W{idx}] {source.title or source.final_url}",
                "",
                f"- URL: {source.final_url}",
                f"- Ursprüngliche URL: {source.url}",
                f"- Publisher: {source.publisher or '-'}",
                f"- Veröffentlicht: {source.published_at or '-'}",
                f"- Abgerufen: {source.retrieved_at}",
                f"- Search rank: {source.search_rank}",
                f"- Relevance score: {score:.4f}",
                f"- Relevance reason: {reason or '-'}",
                f"- Content hash: `{source.content_hash}`",
                f"- Textsnapshot: `{record.get('text_path') or '-'}`",
                f"- PDF-Snapshot: `{record.get('pdf_path') or '-'}`",
                f"- Originalsnapshot: `{record.get('raw_path') or '-'}`",
                f"- Metadaten: `{record.get('metadata_path') or '-'}`",
                "",
            ])
        lines.extend([
            "## Antwortmodell",
            "",
            self.META_START,
            "*(wird nach der Antwort ergänzt)*",
            self.META_END,
            "",
            "## LLM-Ausgabe",
            "",
            self.LLM_START,
            "*(wird nach der Antwort ergänzt)*",
            self.LLM_END,
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _decision_map(decisions: list[dict[str, Any]]) -> dict[tuple[int, str, str], dict[str, Any]]:
        result: dict[tuple[int, str, str], dict[str, Any]] = {}
        for item in decisions:
            key = (
                int(item.get("search_rank") or 0),
                str(item.get("requested_url") or ""),
                str(item.get("final_url") or ""),
            )
            result[key] = item
        return result

    @classmethod
    def _fetch_log_bytes(
        cls,
        fetched: list[FetchedSource],
        decisions: list[dict[str, Any]],
    ) -> bytes:
        decision_map = cls._decision_map(decisions)
        lines: list[str] = []
        for source in fetched:
            key = (source.search_rank, source.url, source.final_url)
            decision = decision_map.get(key, {})
            if source.fetch_error:
                outcome = "fetch_error"
            elif decision.get("selected"):
                outcome = "selected"
            elif decision.get("accepted"):
                outcome = "relevance_pass_not_selected"
            elif decision:
                outcome = "rejected"
            else:
                outcome = "not_evaluated"
            record = {
                "search_rank": source.search_rank,
                "search_provider": source.search_provider,
                "requested_url": source.url,
                "final_url": source.final_url,
                "title": source.title,
                "retrieved_at": source.retrieved_at,
                "http_status": source.http_status,
                "redirect_count": source.redirect_count,
                "fetch_ok": bool(source.text and not source.fetch_error),
                "fetch_error": source.fetch_error,
                "content_type": source.content_type,
                "content_hash": source.content_hash,
                "outcome": outcome,
                "relevance": {
                    "relevant": bool(decision.get("relevant")) if decision else None,
                    "score": decision.get("score") if decision else None,
                    "accepted": bool(decision.get("accepted")) if decision else None,
                    "selected": bool(decision.get("selected")) if decision else None,
                    "reason": str(decision.get("reason") or ""),
                },
            }
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
        return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")

    @staticmethod
    def _metadata_bytes(
        *,
        query: str,
        source: FetchedSource,
        score: float,
        reason: str,
        record: dict[str, str],
        rendered: RenderedSnapshot | None,
        render_status: str = "",
        render_error: str = "",
        render_target_path: str = "",
    ) -> bytes:
        payload: dict[str, Any] = {
            "content_kind": "WEB_SOURCE",
            "query": query,
            "requested_url": source.url,
            "final_url": source.final_url,
            "title": source.title,
            "publisher": source.publisher,
            "published_at": source.published_at,
            "retrieved_at": source.retrieved_at,
            "http_status": source.http_status,
            "redirect_count": source.redirect_count,
            "content_type": source.content_type,
            "content_sha256": source.content_hash,
            "search_provider": source.search_provider,
            "search_rank": source.search_rank,
            "relevance_score": round(float(score), 6),
            "relevance_reason": reason,
            "archive": {
                "text_path": record.get("text_path") or "",
                "raw_path": record.get("raw_path") or "",
                "pdf_path": record.get("pdf_path") or "",
            },
            "render": None,
        }
        if rendered is not None:
            payload["render"] = {
                "kind": "playwright_pdf",
                "status": "complete",
                "requested_url": rendered.requested_url,
                "final_url": rendered.final_url,
                "title": rendered.title,
                "pdf_sha256": rendered.sha256,
                "elapsed_ms": rendered.elapsed_ms,
                "media": rendered.media,
                "video_posters": rendered.video_posters,
                "landscape": rendered.landscape,
                "viewport": rendered.viewport,
                "browser_state": {
                    "reused": rendered.state_reused,
                    "persisted": rendered.state_persisted,
                },
                "cleanup": {
                    "cookie_consent": rendered.cookie_consent,
                    "cookie_actions": rendered.cookie_actions,
                    "overlays_dismissed": rendered.overlays_dismissed,
                    "overlays_removed": rendered.overlays_removed,
                    "dom_modified": rendered.dom_modified,
                },
            }
        elif source.content_type == "application/pdf" and record.get("pdf_path"):
            payload["render"] = {
                "kind": "original_pdf",
                "status": "complete",
                "pdf_sha256": source.content_hash,
            }
        elif render_status:
            payload["render"] = {
                "kind": "playwright_pdf",
                "status": render_status,
                "target_path": render_target_path,
            }
            if render_error:
                payload["render"]["error"] = render_error
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")

    def _schedule_render_batch(
        self,
        jobs: list[tuple[str, FetchedSource, float, str, dict[str, str], str]],
        *,
        rag_user_id: str | None,
    ) -> None:
        if not jobs:
            return
        task = asyncio.create_task(
            self._render_batch_background(jobs, rag_user_id=rag_user_id),
            name=f"web-render-{hashlib.sha256(jobs[0][0].encode('utf-8')).hexdigest()[:10]}",
        )
        self._render_tasks.add(task)
        task.add_done_callback(self._render_task_done)

    def _render_task_done(self, task: asyncio.Task[Any]) -> None:
        self._render_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            log.info("web archive background render cancelled")
        except Exception as exc:
            log.warning("web archive background render batch failed: %s: %s", type(exc).__name__, exc)

    async def _render_batch_background(
        self,
        jobs: list[tuple[str, FetchedSource, float, str, dict[str, str], str]],
        *,
        rag_user_id: str | None,
    ) -> None:
        async with self._render_gate:
            started = time.perf_counter()
            completed = 0
            failed = 0
            credential = self._credential(rag_user_id)
            verify = self.nextcloud_verify
            async with httpx.AsyncClient(timeout=self.timeout, verify=verify) as client:
                for query, source, score, reason, base_record, target_pdf in jobs:
                    record = dict(base_record)
                    metadata_path = str(record.get("metadata_path") or "")
                    try:
                        rendered = await self.renderer.render(source)
                        if rendered is None:
                            continue
                        await self._put(client, credential, target_pdf, rendered.pdf, "application/pdf")
                        record["pdf_path"] = target_pdf
                        if self.write_metadata_json and metadata_path:
                            await self._put(
                                client, credential, metadata_path,
                                self._metadata_bytes(
                                    query=query, source=source, score=score, reason=reason,
                                    record=record, rendered=rendered, render_status="complete",
                                    render_target_path=target_pdf,
                                ),
                                "application/json; charset=utf-8",
                            )
                        completed += 1
                    except Exception as exc:
                        failed += 1
                        message = f"{type(exc).__name__}: {exc}"
                        log.warning("web archive background render failed for %s: %s", source.final_url, exc)
                        if self.write_metadata_json and metadata_path:
                            try:
                                await self._put(
                                    client, credential, metadata_path,
                                    self._metadata_bytes(
                                        query=query, source=source, score=score, reason=reason,
                                        record=record, rendered=None, render_status="failed",
                                        render_error=message, render_target_path=target_pdf,
                                    ),
                                    "application/json; charset=utf-8",
                                )
                            except Exception as meta_exc:
                                log.warning(
                                    "web archive background render metadata update failed for %s: %s",
                                    source.final_url, meta_exc,
                                )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            log.info(
                "web archive background render finished jobs=%d completed=%d failed=%d elapsed_ms=%.1f",
                len(jobs), completed, failed, elapsed_ms,
            )

    async def archive_run(
        self,
        query: str,
        selected: list[tuple[FetchedSource, float, str]],
        *,
        fetched: list[FetchedSource],
        relevance_decisions: list[dict[str, Any]],
        rag_user_id: str | None,
        stats: dict[str, Any],
        relevance_model: str = "",
    ) -> dict[str, Any]:
        if not self.enabled or (not selected and not fetched):
            return {"run_path": "", "source_records": [], "errors": [], "fetch_log_path": ""}
        root = self._root_for_user(rag_user_id)
        if not root:
            reason = "archive_disabled_or_target_unconfigured"
            log.info(
                "web archive skipped: reason=%s rag_user_id=%r",
                reason, str(rag_user_id or "")[:160],
            )
            return {
                "run_path": "", "source_records": [], "errors": [],
                "fetch_log_path": "", "skipped": True,
                "skipped_reason": reason,
            }
        if not self.dav_base:
            raise RuntimeError("nextcloud.base_url missing for web archive")
        credential = self._credential(rag_user_id)
        directory, created_at, run_id = self._new_run_path(query, root)
        records: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        fetch_log_path = ""
        render_jobs: list[tuple[str, FetchedSource, float, str, dict[str, str], str]] = []

        verify = self.nextcloud_verify
        async with httpx.AsyncClient(timeout=self.timeout, verify=verify) as client:
            await self._ensure_dir(client, credential, directory)

            if self.write_fetch_log and fetched:
                fetch_log_path = f"{directory}/fetch-log.jsonl"
                try:
                    await self._put(
                        client, credential, fetch_log_path,
                        self._fetch_log_bytes(fetched, relevance_decisions),
                        "application/x-ndjson; charset=utf-8",
                    )
                except Exception as exc:
                    errors.append({"url": "fetch-log.jsonl", "error": f"{type(exc).__name__}: {exc}"})
                    log.warning("web archive fetch-log failed: %s", exc)
                    fetch_log_path = ""

            for idx, (source, score, reason) in enumerate(selected, start=1):
                stem = f"{idx:02d}-" + _safe_component(
                    source.title or urlparse(source.final_url).hostname or "source", max_len=70
                )
                stem += "-" + source.content_hash[:10]
                record: dict[str, str] = {
                    "text_path": "",
                    "raw_path": "",
                    "pdf_path": "",
                    "metadata_path": "",
                }
                rendered: RenderedSnapshot | None = None
                try:
                    if self.write_text_snapshot:
                        record["text_path"] = f"{directory}/{stem}.txt"
                        await self._put(
                            client, credential, record["text_path"],
                            self._snapshot_bytes(query, source, score, reason),
                            "text/plain; charset=utf-8",
                        )

                    write_raw = (
                        (source.content_type == "application/pdf" and self.write_raw_pdf) or
                        (source.content_type == "text/html" and self.write_raw_html)
                    )
                    if write_raw and source.raw:
                        raw_ext = ".pdf" if source.content_type == "application/pdf" else ".html"
                        record["raw_path"] = f"{directory}/{stem}{raw_ext}"
                        await self._put(client, credential, record["raw_path"], source.raw, source.content_type)
                        if source.content_type == "application/pdf":
                            record["pdf_path"] = record["raw_path"]

                    render_target = ""
                    render_status = ""
                    if source.content_type == "text/html" and self.renderer.enabled:
                        render_target = f"{directory}/{stem}.pdf"
                        if self.render_in_background:
                            render_status = "pending"
                        else:
                            try:
                                rendered = await self.renderer.render(source)
                                if rendered is not None:
                                    record["pdf_path"] = render_target
                                    await self._put(
                                        client, credential, record["pdf_path"],
                                        rendered.pdf, "application/pdf",
                                    )
                            except Exception as exc:
                                errors.append({
                                    "url": source.final_url,
                                    "stage": "render",
                                    "error": f"{type(exc).__name__}: {exc}",
                                })
                                log.warning("web archive Playwright render failed for %s: %s", source.final_url, exc)

                    if self.write_metadata_json:
                        record["metadata_path"] = f"{directory}/.{stem}.metadata.json"
                        await self._put(
                            client, credential, record["metadata_path"],
                            self._metadata_bytes(
                                query=query, source=source, score=score, reason=reason,
                                record=record, rendered=rendered, render_status=render_status,
                                render_target_path=render_target,
                            ),
                            "application/json; charset=utf-8",
                        )
                    if render_status == "pending":
                        render_jobs.append((query, source, score, reason, dict(record), render_target))
                except Exception as exc:
                    errors.append({"url": source.final_url, "error": f"{type(exc).__name__}: {exc}"})
                    log.warning("web archive source failed for %s: %s", source.final_url, exc)
                records.append(record)

            if self.write_research_markdown:
                md = self._research_markdown(
                    query=query,
                    run_id=run_id,
                    created_at=created_at,
                    rag_user_id=rag_user_id,
                    selected=selected,
                    source_records=records,
                    stats=stats,
                    relevance_model=relevance_model,
                    fetch_log_path=fetch_log_path,
                )
                try:
                    await self._put(
                        client, credential, f"{directory}/recherche.md",
                        md.encode("utf-8"), "text/markdown; charset=utf-8",
                    )
                except Exception as exc:
                    errors.append({"url": "recherche.md", "error": f"{type(exc).__name__}: {exc}"})
                    log.warning("web archive recherche.md failed: %s", exc)

        if render_jobs:
            self._schedule_render_batch(render_jobs, rag_user_id=rag_user_id)
            log.info(
                "web archive queued background render jobs=%d run_path=%s",
                len(render_jobs), directory,
            )

        return {
            "run_path": directory,
            "source_records": records,
            "errors": errors,
            "fetch_log_path": fetch_log_path,
            "render_pending": len(render_jobs),
        }

    async def finalize_run(
        self,
        run_path: str,
        answer_text: str,
        *,
        rag_user_id: str | None,
        answer_model: str = "",
        answer_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled or not self.write_research_markdown or not run_path:
            return {"updated": False, "run_path": run_path}
        clean = str(run_path or "").strip(" /")
        root = self._root_for_user(rag_user_id).strip(" /")
        if not root or not (clean == root or clean.startswith(root + "/")):
            raise RuntimeError("archive run path is outside current user's configured web archive root")
        if not self.dav_base:
            raise RuntimeError("nextcloud.base_url missing for web archive")
        credential = self._credential(rag_user_id)
        target = f"{clean}/recherche.md"
        verify = self.nextcloud_verify
        async with httpx.AsyncClient(timeout=self.timeout, verify=verify) as client:
            response = await client.get(
                self._url(credential, target),
                auth=(credential.username, credential.password),
            )
            response.raise_for_status()
            text = response.text
            params = json.dumps(answer_parameters or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            meta = f"- Modell: `{answer_model or '-'}`\n\n```json\n{params}\n```"
            text = self._replace_marked(text, self.META_START, self.META_END, meta)
            text = self._replace_marked(text, self.LLM_START, self.LLM_END, str(answer_text or "").strip())
            await self._put(
                client, credential, target,
                text.encode("utf-8"), "text/markdown; charset=utf-8",
            )
        return {"updated": True, "run_path": clean, "research_path": target}

    @staticmethod
    def _replace_marked(text: str, start: str, end: str, body: str) -> str:
        a = text.find(start)
        b = text.find(end)
        if a < 0 or b < a:
            return text.rstrip() + f"\n\n{start}\n{body}\n{end}\n"
        return text[: a + len(start)] + "\n" + body.rstrip() + "\n" + text[b:]


class WebResearchArm:
    def __init__(self, app_cfg: dict[str, Any] | None = None, web_cfg: dict[str, Any] | None = None):
        self.app_cfg = app_cfg or load_app_config()
        self.cfg = web_cfg if web_cfg is not None else load_web_config()
        self.enabled = _truthy(self.cfg.get("enabled"), False)
        self.searcher = WebSearchProvider(self.cfg)
        self.fetcher = WebFetcher(self.cfg)
        self.gate = RelevanceGate(self.cfg)
        self.archive_store = NextcloudWebArchive(self.cfg, self.app_cfg)
        self.fetch_concurrency = max(1, min(int((self.cfg.get("fetch", {}) or {}).get("concurrency") or 4), 8))

    async def research(self, query: str, *, rag_user_id: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "query": query, "sources": [], "errors": ["web arm disabled"]}
        ready, readiness_error = self.searcher.readiness()
        if not ready:
            return {
                "enabled": False,
                "query": query,
                "sources": [],
                "errors": [readiness_error],
                "reason": "search_backend_unconfigured",
            }
        if self.archive_store.acl.identity_mode == "credential_store":
            settings = self.archive_store.user_settings(rag_user_id)
            if settings is None or not settings.enabled:
                return {
                    "enabled": False,
                    "query": query,
                    "sources": [],
                    "errors": ["web research is not enabled by admin for current canonical Nextcloud user"],
                }
        total_started = time.perf_counter()
        stage_started = total_started
        hits = await self.searcher.search(query)
        search_ms = round((time.perf_counter() - stage_started) * 1000.0, 1)
        semaphore = asyncio.Semaphore(self.fetch_concurrency)

        async def fetch_one(hit: SearchHit) -> FetchedSource:
            async with semaphore:
                return await self.fetcher.fetch(hit)

        stage_started = time.perf_counter()
        fetched = await asyncio.gather(*(fetch_one(hit) for hit in hits))
        fetch_ms = round((time.perf_counter() - stage_started) * 1000.0, 1)
        stage_started = time.perf_counter()
        selected, relevance_decisions = await self.gate.evaluate_with_decisions(query, fetched)
        relevance_ms = round((time.perf_counter() - stage_started) * 1000.0, 1)
        fetched_count = sum(1 for source in fetched if source.text and not source.fetch_error)

        archive_run_path = ""
        archive_fetch_log_path = ""
        archive_records: list[dict[str, str]] = []
        archive_errors: list[dict[str, str]] = []
        archive_render_pending = 0
        archive_skipped_reason = ""
        archive_started = time.perf_counter()
        if fetched:
            try:
                archived = await self.archive_store.archive_run(
                    query,
                    selected,
                    fetched=fetched,
                    relevance_decisions=relevance_decisions,
                    rag_user_id=rag_user_id,
                    stats={
                        "search_provider": self.searcher.provider,
                        "searched": len(hits),
                        "fetched": fetched_count,
                    },
                    relevance_model=self.gate.model,
                )
                archive_run_path = str(archived.get("run_path") or "")
                archive_fetch_log_path = str(archived.get("fetch_log_path") or "")
                archive_records = list(archived.get("source_records") or [])
                archive_errors.extend(list(archived.get("errors") or []))
                archive_render_pending = int(archived.get("render_pending") or 0)
                archive_skipped_reason = str(archived.get("skipped_reason") or "")
            except Exception as exc:
                archive_errors.append({"url": "archive-run", "error": f"{type(exc).__name__}: {exc}"})
                log.warning("web archive run failed: %s", exc)
        archive_ms = round((time.perf_counter() - archive_started) * 1000.0, 1)

        evidence_started = time.perf_counter()
        evidence: list[WebEvidence] = []
        for idx, (source, score, reason) in enumerate(selected, start=1):
            record = archive_records[idx - 1] if idx - 1 < len(archive_records) else {}
            evidence.append(WebEvidence(
                index=idx,
                title=source.title,
                url=source.url,
                final_url=source.final_url,
                publisher=source.publisher,
                published_at=source.published_at,
                retrieved_at=source.retrieved_at,
                content_type=source.content_type,
                content_hash=source.content_hash,
                evidence_text=best_passage(query, source.text),
                relevance_score=score,
                relevance_reason=reason,
                archive_path=str(record.get("text_path") or ""),
                archive_raw_path=str(record.get("raw_path") or ""),
                archive_pdf_path=str(record.get("pdf_path") or ""),
                archive_metadata_path=str(record.get("metadata_path") or ""),
            ))
        passage_ms = round((time.perf_counter() - evidence_started) * 1000.0, 1)
        total_ms = round((time.perf_counter() - total_started) * 1000.0, 1)
        timings = {
            "search_ms": search_ms,
            "fetch_ms": fetch_ms,
            "relevance_ms": relevance_ms,
            "archive_ms": archive_ms,
            "passage_ms": passage_ms,
            "total_ms": total_ms,
        }
        log.info(
            "web timings query=%r searched=%d fetched=%d selected=%d search_ms=%.1f fetch_ms=%.1f relevance_ms=%.1f archive_ms=%.1f passage_ms=%.1f total_ms=%.1f",
            query, len(hits), fetched_count, len(evidence), search_ms, fetch_ms, relevance_ms, archive_ms, passage_ms, total_ms,
        )
        return {
            "enabled": True,
            "query": query,
            "search_provider": self.searcher.provider,
            "searched": len(hits),
            "fetched": fetched_count,
            "selected": len(evidence),
            "sources": [e.to_dict() for e in evidence],
            "archive_run_path": archive_run_path,
            "archive_fetch_log_path": archive_fetch_log_path,
            "archive_render_pending": archive_render_pending,
            "archive_skipped_reason": archive_skipped_reason,
            "fetch_errors": [
                {"url": source.url, "error": source.fetch_error}
                for source in fetched if source.fetch_error
            ],
            "archive_errors": archive_errors,
            "timings": timings,
        }

    async def finalize_archive(
        self,
        run_path: str,
        answer_text: str,
        *,
        rag_user_id: str | None = None,
        answer_model: str = "",
        answer_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.archive_store.finalize_run(
            run_path,
            answer_text,
            rag_user_id=rag_user_id,
            answer_model=answer_model,
            answer_parameters=answer_parameters,
        )

    async def read_archive_source(
        self, relpath: str, *, rag_user_id: str | None = None
    ) -> dict[str, Any] | None:
        return await self.archive_store.read_text_snapshot(
            relpath, rag_user_id=rag_user_id
        )
