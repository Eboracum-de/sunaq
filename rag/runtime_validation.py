"""Fail-fast validation for security-critical beta configuration."""
from __future__ import annotations

import os
from typing import Any

from rag.credential_store import CredentialStore
from rag.secret_env import secret_env
from rag.config_security import validate_static_security_config
from rag.internal_auth import (
    ENV_NAME as INTERNAL_API_KEY_ENV,
    PROVIDER_ENV_NAME,
    MIN_KEY_LENGTH,
)


def _get(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def validate_security_config(cfg: dict[str, Any]) -> list[str]:
    """Return startup errors including static invariants and runtime secrets/state."""
    errors: list[str] = list(validate_static_security_config(cfg))
    acl_enabled = _truthy(_get(cfg, "acl.enabled", True), True)
    mode = str(_get(cfg, "acl.identity_mode", "credential_store") or "").strip().lower()

    internal_key = secret_env(INTERNAL_API_KEY_ENV, "").strip()
    if len(internal_key) < MIN_KEY_LENGTH:
        errors.append(
            f"{INTERNAL_API_KEY_ENV} must be configured with at least {MIN_KEY_LENGTH} characters"
        )
    provider_internal_key = secret_env(PROVIDER_ENV_NAME, "").strip()
    if len(provider_internal_key) < MIN_KEY_LENGTH:
        errors.append(
            f"{PROVIDER_ENV_NAME} must be configured with at least {MIN_KEY_LENGTH} characters"
        )

    if mode == "credential_store" and acl_enabled:
        auth_store = str(
            _get(cfg, "auth.credential_store", "runtime/users.sqlite")
            or "runtime/users.sqlite"
        ).strip()
        try:
            store = CredentialStore(auth_store)
            store.validate_writable()
            secret_status = store.secret_security_status()
            if str(secret_status.get("encryption_mode") or "") == "required":
                plaintext = (
                    int(secret_status.get("credentials_plaintext") or 0)
                    + int(secret_status.get("flows_plaintext") or 0)
                )
                if plaintext:
                    errors.append(
                        "credential encryption is required but "
                        f"{plaintext} plaintext secret(s) remain; "
                        "run rag.secret_admin migrate"
                    )
        except Exception as exc:
            errors.append(f"credential store unavailable: {exc}")

    es_enabled = _truthy(_get(cfg, "elasticsearch.enabled", True), True)
    if es_enabled:
        es_user = str(
            _get(cfg, "elasticsearch.username", _get(cfg, "elasticsearch.user", ""))
            or ""
        ).strip()
        es_password_env = str(
            _get(cfg, "elasticsearch.password_env", "ELASTICSEARCH_PASSWORD")
            or ""
        ).strip()
        es_legacy_password = str(_get(cfg, "elasticsearch.password", "") or "")
        if es_user and not (
            (es_password_env and secret_env(es_password_env, ""))
            or es_legacy_password
        ):
            errors.append(
                "elasticsearch.username is set but password is missing "
                f"({es_password_env or 'elasticsearch.password'})"
            )

    return errors


def require_secure_runtime_config(cfg: dict[str, Any]) -> None:
    errors = validate_security_config(cfg)
    if errors:
        raise RuntimeError("Unsafe/invalid RAG configuration:\n - " + "\n - ".join(errors))
