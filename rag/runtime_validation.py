"""Fail-fast validation for security-critical beta configuration."""
from __future__ import annotations

from pathlib import Path
import os
from typing import Any
from urllib.parse import urlsplit

from rag.credential_store import CredentialStore
from rag.secret_env import secret_env
from rag.architecture_policy import validate_architecture_config
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
    """Return human-readable configuration errors; no network access is used."""
    errors: list[str] = []
    errors.extend(validate_architecture_config(cfg))
    acl_enabled = _truthy(_get(cfg, "acl.enabled", True), True)
    mode = str(_get(cfg, "acl.identity_mode", "credential_store") or "").strip().lower()
    allow_insecure = _truthy(_get(cfg, "security.allow_insecure_nextcloud", False), False)
    base_url = str(_get(cfg, "nextcloud.base_url", "") or "").strip().rstrip("/")

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

    if acl_enabled:
        if mode not in {"credential_store", "single_user", "mapped_users"}:
            errors.append(f"unknown acl.identity_mode={mode!r}")
        if not base_url:
            errors.append("nextcloud.base_url is required when live ACL is enabled")
        else:
            parsed = urlsplit(base_url)
            if not parsed.scheme or not parsed.netloc:
                errors.append("nextcloud.base_url must be an absolute URL")
            elif parsed.scheme.lower() != "https" and not allow_insecure:
                errors.append(
                    "nextcloud.base_url must use HTTPS; set security.allow_insecure_nextcloud=true only for an explicit lab exception"
                )

        nextcloud_ca_file = str(_get(cfg, "nextcloud.ca_file", "") or "").strip()
        canonical_verify = _get(cfg, "nextcloud.verify_tls", None)
        if nextcloud_ca_file:
            if not Path(nextcloud_ca_file).exists():
                errors.append(f"nextcloud.ca_file does not exist: {nextcloud_ca_file}")
        elif canonical_verify is not None:
            if not _truthy(canonical_verify, True) and not allow_insecure:
                errors.append(
                    "nextcloud.verify_tls=false requires security.allow_insecure_nextcloud=true"
                )
        else:
            # Upgrade compatibility: old installations may still carry
            # component-local TLS switches until config.yaml is migrated.
            for path in ("acl.verify_tls", "auth.verify_tls", "carddav.verify_tls"):
                if not _truthy(_get(cfg, path, True), True) and not allow_insecure:
                    errors.append(
                        f"{path}=false requires security.allow_insecure_nextcloud=true"
                    )

    if mode == "credential_store" and acl_enabled:
        acl_store = str(_get(cfg, "acl.credential_store", "runtime/users.sqlite") or "runtime/users.sqlite").strip()
        auth_store = str(_get(cfg, "auth.credential_store", "runtime/users.sqlite") or "runtime/users.sqlite").strip()
        if Path(acl_store) != Path(auth_store):
            errors.append(
                "acl.credential_store and auth.credential_store must reference the same database in credential_store mode"
            )
        if not _truthy(_get(cfg, "auth.nextcloud_login_flow_enabled", True), True):
            # Pre-seeded credentials are possible, so this is not intrinsically unsafe,
            # but a fresh multi-user beta would otherwise have no supported onboarding path.
            errors.append(
                "auth.nextcloud_login_flow_enabled must be true for the default multi-user beta mode"
            )
        try:
            store = CredentialStore(auth_store)
            store.validate_writable()
            secret_status = store.secret_security_status()
            if str(secret_status.get("encryption_mode") or "") == "required":
                plaintext = int(secret_status.get("credentials_plaintext") or 0) + int(secret_status.get("flows_plaintext") or 0)
                if plaintext:
                    errors.append(
                        f"credential encryption is required but {plaintext} plaintext secret(s) remain; run rag.secret_admin migrate"
                    )
        except Exception as exc:
            errors.append(f"credential store unavailable: {exc}")


    es_enabled = _truthy(_get(cfg, "elasticsearch.enabled", True), True)
    if es_enabled:
        es_user = str(_get(cfg, "elasticsearch.username", _get(cfg, "elasticsearch.user", "")) or "").strip()
        es_password_env = str(_get(cfg, "elasticsearch.password_env", "ELASTICSEARCH_PASSWORD") or "").strip()
        es_legacy_password = str(_get(cfg, "elasticsearch.password", "") or "")
        if es_user and not ((es_password_env and secret_env(es_password_env, "")) or es_legacy_password):
            errors.append(f"elasticsearch.username is set but password is missing ({es_password_env or 'elasticsearch.password'})")
        ca_file = str(_get(cfg, "elasticsearch.ca_file", "") or "").strip()
        if _truthy(_get(cfg, "elasticsearch.verify_tls", True), True) and ca_file and not Path(ca_file).exists():
            errors.append(f"elasticsearch.ca_file does not exist: {ca_file}")

    if mode == "single_user" and acl_enabled:
        username_env = str(_get(cfg, "acl.username_env", "NEXTCLOUD_USERNAME") or "").strip()
        password_env = str(_get(cfg, "acl.password_env", "NEXTCLOUD_APP_PASSWORD") or "").strip()
        if not username_env or not password_env:
            errors.append("single_user ACL requires acl.username_env and acl.password_env")

    return errors


def require_secure_runtime_config(cfg: dict[str, Any]) -> None:
    errors = validate_security_config(cfg)
    if errors:
        raise RuntimeError("Unsafe/invalid RAG configuration:\n - " + "\n - ".join(errors))
