"""Configuration and adapter registry for delegated research."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from rag.research.contracts import DelegatedResearchProvider
from rag.research.openai import OpenAIResearchProvider
from rag.secret_env import secret_env


@dataclass(frozen=True, slots=True)
class DelegatedResearchSettings:
    enabled: bool = False
    provider: str = "native"
    delegate_web_only: bool = True
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LLM_API_KEY"
    verify_tls: bool = True
    ca_file: str = ""
    timeout: float = 180.0
    reasoning_effort: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and self.provider != "native"


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def load_delegated_research_settings(
    config: Mapping[str, Any],
    *,
    fallback_model: str,
) -> DelegatedResearchSettings:
    section = config.get("delegated_research") or {}
    section = dict(section) if isinstance(section, Mapping) else {}
    openai_cfg = section.get("openai") or {}
    openai_cfg = dict(openai_cfg) if isinstance(openai_cfg, Mapping) else {}

    env_provider = str(os.getenv("SUNAQ_RESEARCH_PROVIDER", "") or "").strip().casefold()
    configured_provider = str(section.get("provider") or "native").strip().casefold()
    provider = env_provider or configured_provider or "native"
    enabled = _truthy(section.get("enabled"), False)
    if env_provider:
        enabled = env_provider != "native"

    configured_model = str(
        os.getenv("SUNAQ_RESEARCH_MODEL", "")
        or openai_cfg.get("model")
        or ""
    ).strip()
    if configured_model:
        model = configured_model
    elif provider == "openai":
        # Keep hosted-research model selection independent of ANSWER_MODEL:
        # the latter may legitimately name a local Ollama/private model.
        model = "gpt-5.5"
    else:
        model = str(fallback_model or "").strip()
    base_url = str(
        os.getenv("SUNAQ_RESEARCH_BASE_URL", "")
        or openai_cfg.get("base_url")
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    api_key_env = str(openai_cfg.get("api_key_env") or "LLM_API_KEY").strip()

    return DelegatedResearchSettings(
        enabled=enabled,
        provider=provider,
        delegate_web_only=_truthy(section.get("delegate_web_only"), True),
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        verify_tls=_truthy(openai_cfg.get("verify_tls"), True),
        ca_file=str(openai_cfg.get("ca_file") or "").strip(),
        timeout=max(5.0, float(openai_cfg.get("timeout") or 180.0)),
        reasoning_effort=str(openai_cfg.get("reasoning_effort") or "").strip().casefold(),
    )


def build_delegated_research_provider(
    settings: DelegatedResearchSettings,
) -> DelegatedResearchProvider | None:
    if not settings.active:
        return None
    if settings.provider != "openai":
        raise RuntimeError(
            f"unsupported delegated research provider: {settings.provider}"
        )
    api_key = secret_env(settings.api_key_env, "")
    verify: bool | str = settings.ca_file or settings.verify_tls
    return OpenAIResearchProvider(
        api_key=api_key,
        model=settings.model,
        base_url=settings.base_url,
        timeout=settings.timeout,
        verify=verify,
        reasoning_effort=settings.reasoning_effort,
    )
