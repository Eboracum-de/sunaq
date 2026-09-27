"""Role-aware LLM backend routing.

Global LLM_* / <ROLE>_LLM_* environment variables remain the compatibility
baseline. SunaQ models may add request-model-specific role overrides at startup,
so two user-visible models can use the same or different underlying LLMs without
mutating process-global environment state.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from typing import Any
from urllib.parse import urlparse

from rag.llm_backend import LLMBackend, build_llm_backend
from rag.secret_env import secret_env


ROLES = ("default", "planner", "verifier", "evidence", "answer")


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _env(role: str, suffix: str, default: str = "") -> str:
    if role == "default":
        return str(os.getenv(f"LLM_{suffix}", default) or default).strip()
    value = str(os.getenv(f"{role.upper()}_LLM_{suffix}", "") or "").strip()
    if value:
        return value
    return str(os.getenv(f"LLM_{suffix}", default) or default).strip()


def _scope_for_url(base_url: str) -> str:
    """Classify a backend endpoint for trust-boundary display/budgets."""
    try:
        host = (urlparse(base_url).hostname or "").strip().casefold()
        if host in {"localhost", "localhost.localdomain"}:
            return "local"
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_private or address.is_link_local:
            return "local"
        return "remote"
    except ValueError:
        # A DNS name cannot be proven private without performing resolution.
        return "remote"


@dataclass(frozen=True)
class RoleBackend:
    role: str
    backend_name: str
    base_url: str
    model: str
    scope: str
    backend: LLMBackend

    @property
    def remote(self) -> bool:
        return self.scope == "remote"

    def info(self) -> dict[str, object]:
        data = dict(self.backend.info())
        data.update({"role": self.role, "scope": self.scope, "remote": self.remote})
        return data


def _profile_role_override(
    role_overrides: dict[str, dict[str, Any]] | None,
    role: str,
) -> dict[str, Any]:
    value = (role_overrides or {}).get(role) or {}
    if not isinstance(value, dict):
        raise RuntimeError(f"SunaQ role override {role!r} must be a mapping")
    return value


def build_role_backends(
    *,
    default_backend: str,
    default_base_url: str,
    default_model: str,
    default_api_key: str,
    default_verify_tls: bool,
    default_ca_file: str | None,
    models: dict[str, str] | None = None,
    role_overrides: dict[str, dict[str, Any]] | None = None,
    private_network_only: bool = False,
) -> dict[str, RoleBackend]:
    """Build immutable role backends for one SunaQ runtime model.

    Precedence is:
      SunaQ model role override -> existing role environment -> global LLM_*

    This keeps old deployments unchanged while allowing multiple user-visible
    models to route the same role differently in one provider process.
    """
    model_map = {str(k): str(v) for k, v in (models or {}).items()}
    result: dict[str, RoleBackend] = {}

    for role in ROLES:
        prefix = role.upper()
        override = _profile_role_override(role_overrides, role)

        if role == "default":
            backend_name = default_backend
            base_url = default_base_url
            api_key = default_api_key
            verify_tls = default_verify_tls
            ca_file = default_ca_file
            model = model_map.get(role) or default_model
            scope_override = str(os.getenv("LLM_SCOPE", "") or "").strip().lower()
        else:
            backend_name = (
                str(os.getenv(f"{prefix}_LLM_BACKEND", "") or "").strip()
                or default_backend
            )
            base_url = (
                str(os.getenv(f"{prefix}_LLM_BASE_URL", "") or "").strip().rstrip("/")
                or default_base_url
            )
            api_key_name = f"{prefix}_LLM_API_KEY"
            api_key_env_value = secret_env(api_key_name, "")
            api_key = default_api_key if not api_key_env_value else api_key_env_value
            verify_raw = os.getenv(f"{prefix}_LLM_VERIFY_TLS")
            verify_tls = (
                default_verify_tls
                if verify_raw is None or verify_raw == ""
                else _truthy(verify_raw, default_verify_tls)
            )
            ca_raw = os.getenv(f"{prefix}_LLM_CA_FILE")
            ca_file = (
                default_ca_file
                if ca_raw is None or str(ca_raw).strip() == ""
                else str(ca_raw).strip()
            )
            model = (
                str(os.getenv(f"{prefix}_LLM_MODEL", "") or "").strip()
                or model_map.get(role)
                or default_model
            )
            scope_override = str(
                os.getenv(f"{prefix}_LLM_SCOPE", "") or ""
            ).strip().lower()

        # A SunaQ model is an explicit administrator-defined routing package and
        # therefore overrides the compatibility environment for this one model.
        if override:
            if "api_key" in override:
                raise RuntimeError(
                    f"SunaQ model role {role!r} must use api_key_env; plaintext api_key is not allowed"
                )
            backend_name = str(override.get("backend") or backend_name).strip().lower()
            new_base_url = str(
                override.get("base_url") or override.get("url") or base_url
            ).strip().rstrip("/")
            if new_base_url != base_url and "scope" not in override:
                # A compatibility-environment scope describes that environment
                # endpoint only. Reclassify a profile-selected endpoint unless
                # the profile explicitly declares its scope.
                scope_override = ""
            base_url = new_base_url
            model = str(override.get("model") or model).strip()
            if "verify_tls" in override:
                verify_tls = _truthy(override.get("verify_tls"), verify_tls)
            if "ca_file" in override:
                ca_file = str(override.get("ca_file") or "").strip() or None
            key_env = str(override.get("api_key_env") or "").strip()
            if key_env:
                api_key = secret_env(key_env, "")
            if "scope" in override:
                scope_override = str(override.get("scope") or "").strip().lower()

        if scope_override and scope_override not in {"local", "remote"}:
            raise RuntimeError(f"{prefix}_LLM_SCOPE must be 'local' or 'remote'")
        scope = scope_override or _scope_for_url(base_url)

        # SRC may deliberately route a role to a remote LLM. The architecture
        # tier therefore cannot itself mean "private network only" for every
        # backend. Keep the stricter network pinning for roles classified as
        # local, while allowing administrator-selected remote roles to reach
        # their configured public endpoint.
        enforce_private_network = private_network_only and scope == "local"
        backend = build_llm_backend(
            backend_name,
            base_url=base_url,
            model=model,
            api_key=api_key,
            verify_tls=verify_tls,
            ca_file=ca_file,
            private_network_only=enforce_private_network,
            trust_env=not enforce_private_network,
        )
        result[role] = RoleBackend(
            role=role,
            backend_name=backend_name,
            base_url=base_url,
            model=model,
            scope=scope,
            backend=backend,
        )
    return result
