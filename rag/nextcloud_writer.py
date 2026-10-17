"""Shared Nextcloud WebDAV write transport for SunaQ dossier/archive code."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from rag.acl import NextcloudCredential
from rag.logging_utils import get_logger

log = get_logger("nextcloud_writer")


class NextcloudWriter:
    """Small storage boundary for Nextcloud DAV write coordination.

    It owns URL construction, recursive collection materialization and the one
    retry policy that is intentionally special-cased for Nextcloud's transient
    423 locks. Callers retain their own artifact serialization and policy hooks.
    """

    def __init__(self, dav_base: str):
        self.dav_base = str(dav_base or "").rstrip("/")

    def url(self, credential: NextcloudCredential, relpath: str) -> str:
        parts = [quote(credential.username, safe="")] + [
            quote(part, safe="")
            for part in str(relpath or "").strip("/").split("/")
            if part
        ]
        return self.dav_base + "/" + "/".join(parts)

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        credential: NextcloudCredential,
        target: str,
        **kwargs: Any,
    ) -> httpx.Response:
        delays = (0.15, 0.35, 0.75, 1.50, 2.50)
        response: httpx.Response | None = None
        for attempt in range(len(delays) + 1):
            response = await client.request(
                method,
                self.url(credential, target),
                auth=(credential.username, credential.password),
                **kwargs,
            )
            if response.status_code != 423 or attempt >= len(delays):
                return response

            delay = delays[attempt]
            retry_after = str(response.headers.get("Retry-After") or "").strip()
            if retry_after:
                try:
                    delay = max(delay, min(5.0, float(retry_after)))
                except ValueError:
                    pass
            log.info(
                "Nextcloud DAV lock retry: method=%s target=%s attempt=%d delay=%.2fs",
                method.upper(),
                target,
                attempt + 1,
                delay,
            )
            await asyncio.sleep(delay)

        assert response is not None
        return response

    async def ensure_dir(
        self,
        client: httpx.AsyncClient,
        credential: NextcloudCredential,
        relpath: str,
    ) -> None:
        accum: list[str] = []
        for part in [p for p in str(relpath or "").strip("/").split("/") if p]:
            accum.append(part)
            response = await self.request(
                client,
                "MKCOL",
                credential,
                "/".join(accum),
            )
            if response.status_code not in {201, 405}:
                response.raise_for_status()
