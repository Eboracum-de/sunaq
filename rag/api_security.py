"""Central FastAPI security-zone dependencies for rag.api."""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from rag.acl import AclConfigurationError, AclIdentityError, NextcloudLiveAcl
from rag.secret_env import secret_env
from rag.internal_auth import (
    ENV_NAME as INTERNAL_API_KEY_ENV,
    HEADER_NAME as INTERNAL_API_KEY_HEADER,
    PROVIDER_ENV_NAME,
    PROVIDER_HEADER_NAME,
    internal_api_configured,
    provider_api_configured,
    valid_internal_api_key,
    valid_provider_internal_key,
)


PUBLIC = "PUBLIC"
TRUSTED_PROVIDER = "TRUSTED_PROVIDER"
INTERNAL = "INTERNAL"
ADMIN = "ADMIN"
USER = "USER"


@dataclass
class ApiSecurity:
    cfg: dict[str, Any]
    live_acl: NextcloudLiveAcl

    @staticmethod
    def is_baseline_exempt(path: str) -> bool:
        clean = str(path or "")
        return (
            clean == "/live"
            or clean.startswith("/rag-admin")
            or clean.startswith("/curation")
        )

    @staticmethod
    def _header(request: Request, name: str) -> str:
        return str(request.headers.get(name) or "").strip()

    def require_internal_client(self, request: Request) -> None:
        if not internal_api_configured():
            raise HTTPException(
                status_code=503,
                detail=f"{INTERNAL_API_KEY_ENV} is not configured",
            )
        if not valid_internal_api_key(self._header(request, INTERNAL_API_KEY_HEADER)):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing internal API credential",
            )

    def require_trusted_provider(self, request: Request) -> None:
        self.require_internal_client(request)
        if not provider_api_configured():
            raise HTTPException(
                status_code=503,
                detail=f"{PROVIDER_ENV_NAME} is not configured",
            )
        if not valid_provider_internal_key(self._header(request, PROVIDER_HEADER_NAME)):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing trusted-provider credential",
            )

    def require_current_user(self, request: Request) -> str:
        """Require a trusted provider and a usable user identity when needed.

        In single-user mode the server-side static Nextcloud credential is the
        identity, so no X-RAG-User-ID is required. In credential-store/mapped
        modes the provider must already have scoped the external identity and
        the referenced server-side credential must exist.
        """
        self.require_trusted_provider(request)
        user_id = str(request.headers.get("x-rag-user-id") or "").strip()

        if not self.live_acl.enabled:
            return user_id
        if self.live_acl.identity_mode == "single_user":
            try:
                self.live_acl.credential_for_user(None)
            except AclConfigurationError as exc:
                raise HTTPException(status_code=503, detail=f"Live ACL unavailable: {exc}") from exc
            return user_id

        if not user_id:
            raise HTTPException(status_code=403, detail="RAG user identity missing")
        try:
            self.live_acl.credential_for_user(user_id)
        except AclIdentityError as exc:
            raise HTTPException(status_code=403, detail=f"Live ACL denied: {exc}") from exc
        except AclConfigurationError as exc:
            raise HTTPException(status_code=503, detail=f"Live ACL unavailable: {exc}") from exc
        return user_id

    def require_admin(self, request: Request) -> None:
        self.require_internal_client(request)

        username = str(os.getenv("RAG_ADMIN_USER", "") or "").strip()
        password = secret_env("RAG_ADMIN_PASSWORD", "")
        if not username or not password:
            raise HTTPException(status_code=503, detail="RAG Admin credentials are not configured")

        header = str(request.headers.get("authorization") or "")
        if not header.lower().startswith("basic "):
            raise HTTPException(
                status_code=401,
                detail="Admin authentication required",
                headers={"WWW-Authenticate": 'Basic realm="RAG Admin"'},
            )
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid admin authentication",
                headers={"WWW-Authenticate": 'Basic realm="RAG Admin"'},
            ) from exc

        if not (
            secrets.compare_digest(supplied_user, username)
            and secrets.compare_digest(supplied_password, password)
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid admin authentication",
                headers={"WWW-Authenticate": 'Basic realm="RAG Admin"'},
            )
