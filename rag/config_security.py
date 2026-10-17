"""Pure static security/configuration invariants shared by startup and diagnostics.

This module deliberately performs no network calls, secret lookups or database
writes. It is therefore safe for installer preflight and smoke/config checks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rag.architecture_policy import validate_architecture_config


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


def validate_static_security_config(
    cfg: dict[str, Any],
    *,
    require_complete: bool = True,
) -> list[str]:
    """Return security-critical errors that require configuration only.

    Runtime startup uses require_complete=True. Static tooling may pass False
    so deliberately partial config fragments are still checkable; explicitly
    configured unsafe values are validated in either mode.
    """
    errors: list[str] = []
    errors.extend(validate_architecture_config(cfg))

    acl_default = True if require_complete else False
    acl_enabled = _truthy(_get(cfg, "acl.enabled", acl_default), acl_default)
    mode = str(_get(cfg, "acl.identity_mode", "credential_store") or "").strip().lower()
    allow_insecure = _truthy(
        _get(cfg, "security.allow_insecure_nextcloud", False), False
    )
    base_url = str(_get(cfg, "nextcloud.base_url", "") or "").strip().rstrip("/")

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
                    "nextcloud.base_url must use HTTPS; set "
                    "security.allow_insecure_nextcloud=true only for an explicit lab exception"
                )

        nextcloud_ca_file = str(_get(cfg, "nextcloud.ca_file", "") or "").strip()
        canonical_verify = _get(cfg, "nextcloud.verify_tls", None)
        if nextcloud_ca_file:
            if not Path(nextcloud_ca_file).exists():
                errors.append(f"nextcloud.ca_file does not exist: {nextcloud_ca_file}")
        elif canonical_verify is not None:
            if not _truthy(canonical_verify, True) and not allow_insecure:
                errors.append(
                    "nextcloud.verify_tls=false requires "
                    "security.allow_insecure_nextcloud=true"
                )
        else:
            for path in ("acl.verify_tls", "auth.verify_tls", "carddav.verify_tls"):
                if not _truthy(_get(cfg, path, True), True) and not allow_insecure:
                    errors.append(
                        f"{path}=false requires security.allow_insecure_nextcloud=true"
                    )

    if mode == "credential_store" and acl_enabled:
        acl_store = str(
            _get(cfg, "acl.credential_store", "runtime/users.sqlite")
            or "runtime/users.sqlite"
        ).strip()
        auth_store = str(
            _get(cfg, "auth.credential_store", "runtime/users.sqlite")
            or "runtime/users.sqlite"
        ).strip()
        if Path(acl_store) != Path(auth_store):
            errors.append(
                "acl.credential_store and auth.credential_store must reference "
                "the same database in credential_store mode"
            )
        if not _truthy(
            _get(cfg, "auth.nextcloud_login_flow_enabled", True), True
        ):
            errors.append(
                "auth.nextcloud_login_flow_enabled must be true for the default "
                "multi-user beta mode"
            )

    es_enabled = _truthy(_get(cfg, "elasticsearch.enabled", True), True)
    if es_enabled:
        ca_file = str(_get(cfg, "elasticsearch.ca_file", "") or "").strip()
        if (
            _truthy(_get(cfg, "elasticsearch.verify_tls", True), True)
            and ca_file
            and not Path(ca_file).exists()
        ):
            errors.append(f"elasticsearch.ca_file does not exist: {ca_file}")

    if mode == "single_user" and acl_enabled:
        username_env = str(
            _get(cfg, "acl.username_env", "NEXTCLOUD_USERNAME") or ""
        ).strip()
        password_env = str(
            _get(cfg, "acl.password_env", "NEXTCLOUD_APP_PASSWORD") or ""
        ).strip()
        if not username_env or not password_env:
            errors.append(
                "single_user ACL requires acl.username_env and acl.password_env"
            )

    return errors
