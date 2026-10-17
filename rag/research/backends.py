"""Administrator-owned registry of Web Retrieval and Delegated Research backends.

Model profiles may only refer to an already registered ID. Neither a planner nor
an incoming request can create a provider, change a destination or enable Web.
The legacy global delegated_research switch remains the default for old models.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from rag.research.registry import DelegatedResearchSettings

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CAPABILITIES = frozenset({"documents", "mailarchive", "chatarchive", "web"})
_IMPLEMENTED = {"web_retrieval": frozenset({"brave_search"}), "delegated_research": frozenset({"openai"})}


@dataclass(frozen=True, slots=True)
class WebBackend:
    backend_id: str
    kind: str
    adapter: str
    enabled: bool = True
    settings: DelegatedResearchSettings | None = None

    @property
    def delegated(self) -> bool:
        return self.kind == "delegated_research"


class ResearchBackendRegistry:
    def __init__(self, backends: Mapping[str, WebBackend]) -> None:
        self._backends = dict(backends)

    def get(self, backend_id: str) -> WebBackend:
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise ValueError(f"unknown research backend {backend_id!r}") from exc

    def resolve(
        self,
        model: Any,
        *,
        legacy: DelegatedResearchSettings,
        native_enabled: bool,
    ) -> WebBackend | None:
        caps = model.section("capabilities")
        for key, value in caps.items():
            if key not in _CAPABILITIES or not isinstance(value, bool):
                raise ValueError(f"model {model.model_id}: invalid capability {key!r}={value!r}")
        if caps.get("web") is False:
            return None

        research = model.section("research")
        unsupported = set(research) - {"web_backend"}
        if unsupported:
            raise ValueError(f"model {model.model_id}: unknown research fields {sorted(unsupported)!r}")
        selected = research.get("web_backend")
        if selected is None or selected == "":
            # Upgrade compatibility: no per-model choice retains global routing.
            if legacy.active and legacy.delegate_web_only:
                return WebBackend("legacy-delegated", "delegated_research",
                                  legacy.provider, settings=legacy)
            if native_enabled:
                return WebBackend("legacy-native", "web_retrieval", "brave_search")
            return None

        if not isinstance(selected, str):
            raise ValueError(f"model {model.model_id}: research.web_backend must be a string")
        selected = selected.strip()
        if selected == "none":
            return None
        backend = self.get(selected)
        if not backend.enabled:
            raise ValueError(f"model {model.model_id}: research backend {selected!r} disabled by administrator")
        if backend.kind == "web_retrieval" and not native_enabled:
            raise ValueError(f"model {model.model_id}: native Web engine is disabled")
        if backend.kind == "delegated_research" and backend.settings is None:
            raise ValueError(f"model {model.model_id}: research backend {selected!r} lacks delegated settings")
        return backend


def load_research_backend_registry(config: Mapping[str, Any]) -> ResearchBackendRegistry:
    raw = config.get("research_backends") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("research_backends must be a mapping")
    result: dict[str, WebBackend] = {}
    for key, val in raw.items():
        name = str(key or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ValueError(f"invalid research backend id {name!r}")
        if not isinstance(val, Mapping):
            raise ValueError(f"research_backends.{name} must be a mapping")
        allowed = {"kind", "adapter", "enabled", "model", "base_url",
                   "api_key_env", "verify_tls", "ca_file", "timeout", "reasoning_effort"}
        extra = set(val) - allowed
        if extra:
            raise ValueError(f"research_backends.{name}: unknown fields {sorted(extra)!r}")
        kind = str(val.get("kind") or "").strip().lower()
        adapter = str(val.get("adapter") or "").strip().lower()
        if kind not in _IMPLEMENTED or adapter not in _IMPLEMENTED[kind]:
            if val.get("enabled") is False:
                # Keep disabled future adapter declarations in an admin config,
                # but never let a model select them.
                result[name] = WebBackend(name, kind, adapter, False)
                continue
            raise ValueError(f"research_backends.{name}: unsupported kind/adapter {kind}/{adapter}")
        enabled = val.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"research_backends.{name}.enabled must be a boolean")
        if kind == "web_retrieval":
            if any(k in val for k in allowed - {"kind", "adapter", "enabled"}):
                raise ValueError(f"research_backends.{name}: Web Retrieval backend cannot configure LLM settings")
            result[name] = WebBackend(name, kind, adapter, enabled)
            continue

        api_key_env = str(val.get("api_key_env") or "LLM_API_KEY").strip()
        if not _ENV_RE.fullmatch(api_key_env):
            raise ValueError(f"research_backends.{name}: invalid api_key_env")
        base_url = str(val.get("base_url") or "https://api.openai.com/v1").strip().rstrip("/")
        if not base_url.startswith("https://") and not base_url.startswith("http://"):
            raise ValueError(f"research_backends.{name}: base_url must be HTTP(S)")
        verify = val.get("verify_tls", True)
        if not isinstance(verify, bool):
            raise ValueError(f"research_backends.{name}.verify_tls must be boolean")
        timeout = float(val.get("timeout") or 180)
        if not 5 <= timeout <= 600:
            raise ValueError(f"research_backends.{name}.timeout outside 5-600s")
        settings = DelegatedResearchSettings(
            enabled=enabled,
            provider=adapter,
            delegate_web_only=True,
            model=str(val.get("model") or "gpt-5.5").strip(),
            base_url=base_url,
            api_key_env=api_key_env,
            verify_tls=verify,
            ca_file=str(val.get("ca_file") or "").strip(),
            timeout=timeout,
            reasoning_effort=str(val.get("reasoning_effort") or "").strip().lower(),
        )
        result[name] = WebBackend(name, kind, adapter, enabled, settings)
    return ResearchBackendRegistry(result)
