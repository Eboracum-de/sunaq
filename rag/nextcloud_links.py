"""Version-compatible Nextcloud Files links.

Nextcloud changed the Files app route used for direct file opening between the
legacy releases and the current frontend.  Keep route selection in one module
so API search results, provider source lists, archives, and external clients all
see the same link shape.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from rag.nextcloud_tls import nextcloud_verify_value


def build_nextcloud_source_url(
    base_url: str,
    directory: str,
    openfile: str | int | None,
    *,
    major_version: int | None,
) -> str:
    base = str(base_url or "").strip().rstrip("/")
    file_id = str(openfile or "").strip()
    if not base or not file_id.isdigit():
        return ""

    clean_dir = str(directory or "/").strip()
    if clean_dir != "/":
        clean_dir = "/" + clean_dir.strip("/")

    if major_version is not None and major_version >= 26:
        params = urlencode({
            "dir": clean_dir,
            "editing": "false",
            "openfile": "true",
        })
        return f"{base}/apps/files/files/{file_id}?{params}"

    params = urlencode({
        "dir": clean_dir,
        "openfile": file_id,
    })
    return f"{base}/index.php/apps/files/?{params}"


def document_id_from_nextcloud_url(url: str) -> str | None:
    try:
        parsed = urlparse(str(url))
    except Exception:
        return None

    modern = re.search(r"/(?:index\.php/)?apps/files/files/(\d+)/?$", parsed.path)
    if modern:
        return f"files:{modern.group(1)}"

    values = parse_qs(parsed.query).get("openfile") or []
    if not values:
        return None
    value = str(values[0]).strip()
    if value.isdigit():
        return f"files:{value}"
    return None


class NextcloudVersionProbe:
    """Best-effort cached server-version probe used only for UI compatibility."""

    def __init__(self, base_url: str, config: dict):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.config = config
        self._major: int | None = None
        self._probed = False
        self._lock = threading.Lock()

    def major_version(self) -> int | None:
        if self._probed:
            return self._major
        with self._lock:
            if self._probed:
                return self._major
            self._probed = True
            if not self.base_url:
                return None
            try:
                response = httpx.get(
                    self.base_url + "/status.php",
                    timeout=3.0,
                    verify=nextcloud_verify_value(self.config),
                    follow_redirects=False,
                )
                response.raise_for_status()
                payload = response.json()
                raw = ""
                if isinstance(payload, dict):
                    raw = payload.get("version") or payload.get("versionstring") or ""
                match = re.match(r"^\s*(\d+)", str(raw))
                if match:
                    self._major = int(match.group(1))
            except Exception:
                # Link rendering must never become a retrieval/availability
                # dependency. Unknown versions deliberately use the legacy URL.
                self._major = None
            return self._major
