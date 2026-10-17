"""Deterministic outbound egress policy shared by SunaQ data-flow boundaries.

The policy is intentionally small and non-LLM-based. Research/search queries can
be guarded without silently rewriting full model evidence or embedding payloads.
Model processing and embeddings use the same classification contract but default
to non-destructive modes.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

log = logging.getLogger("sunaq.egress")

RESEARCH_QUERY = "research_query"
MODEL_PROCESSING = "model_processing"
EMBEDDING = "embedding"
EGRESS_CLASSES = frozenset({RESEARCH_QUERY, MODEL_PROCESSING, EMBEDDING})
EGRESS_MODES = frozenset({"allow", "audit", "guard", "block"})

_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|secret)"
        r"\s*[:=]\s*([^\s,;]{6,})"
    ),
)


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    enabled: bool = True
    research_query: str = "guard"
    model_processing: str = "audit"
    embedding: str = "allow"
    redact_credentials: bool = True
    internal_identifiers: tuple[str, ...] = ()
    replacement: str = "[REDACTED]"

    def mode_for(self, egress_class: str) -> str:
        value = str(getattr(self, egress_class, "allow") or "allow").strip().casefold()
        if value not in EGRESS_MODES:
            raise ValueError(f"invalid egress mode {value!r} for {egress_class}")
        return value


@dataclass(frozen=True, slots=True)
class EgressResult:
    action: str
    content: Any
    mode: str
    reason: str = ""
    redactions: int = 0


_runtime_policy: EgressPolicy | None = None


def _truthy(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def load_egress_policy(config: Mapping[str, Any] | None = None) -> EgressPolicy:
    section = dict((config or {}).get("egress") or {})
    guard = dict(section.get("guard") or {})
    identifiers = guard.get("internal_identifiers") or []
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    if not isinstance(identifiers, list):
        raise ValueError("egress.guard.internal_identifiers must be a list")

    values = {
        "enabled": _truthy(section.get("enabled"), True),
        "research_query": str(
            os.getenv("SUNAQ_EGRESS_RESEARCH_QUERY_MODE")
            or (section.get("research_query") or {}).get("mode")
            or "guard"
        ).strip().casefold(),
        "model_processing": str(
            os.getenv("SUNAQ_EGRESS_MODEL_PROCESSING_MODE")
            or (section.get("model_processing") or {}).get("mode")
            or "audit"
        ).strip().casefold(),
        "embedding": str(
            os.getenv("SUNAQ_EGRESS_EMBEDDING_MODE")
            or (section.get("embedding") or {}).get("mode")
            or "allow"
        ).strip().casefold(),
        "redact_credentials": _truthy(guard.get("redact_credentials"), True),
        "internal_identifiers": tuple(str(value) for value in identifiers if str(value)),
        "replacement": str(guard.get("replacement") or "[REDACTED]"),
    }
    for name in (RESEARCH_QUERY, MODEL_PROCESSING, EMBEDDING):
        if values[name] not in EGRESS_MODES:
            raise ValueError(
                f"egress.{name}.mode must be one of {sorted(EGRESS_MODES)!r}"
            )
    for pattern in values["internal_identifiers"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid egress internal identifier regex {pattern!r}: {exc}") from exc
    return EgressPolicy(**values)


def configure_runtime_egress_policy(config: Mapping[str, Any]) -> EgressPolicy:
    global _runtime_policy
    _runtime_policy = load_egress_policy(config)
    return _runtime_policy


def reset_runtime_egress_policy() -> None:
    global _runtime_policy
    _runtime_policy = None


def runtime_egress_policy() -> EgressPolicy:
    global _runtime_policy
    if _runtime_policy is not None:
        return _runtime_policy
    config: Mapping[str, Any] = {}
    path = Path(os.getenv("RAG_CONFIG_FILE", "config.yaml"))
    try:
        import yaml
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(parsed, Mapping):
            config = parsed
    except Exception:
        # Standalone helpers (notably the resolver prototype) may deliberately
        # run without the main config file. Safe class defaults still apply.
        config = {}
    _runtime_policy = load_egress_policy(config)
    return _runtime_policy


def _external_target(metadata: Mapping[str, Any], egress_class: str) -> bool:
    explicit = metadata.get("external")
    if isinstance(explicit, bool):
        return explicit
    target = str(
        metadata.get("target") or metadata.get("base_url") or ""
    ).strip()
    if not target:
        # A research query without a target is still intended for a public
        # search/research service. Other classes should normally provide URL
        # metadata, but treating an unknown target as external is conservative.
        return True
    host = (urlparse(target).hostname or "").strip().casefold()
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local)


def _redact_text(text: str, policy: EgressPolicy) -> tuple[str, int]:
    result = text
    count = 0
    if policy.redact_credentials:
        for index, pattern in enumerate(_CREDENTIAL_PATTERNS):
            if index == 1:
                result, changed = pattern.subn("Bearer " + policy.replacement, result)
            elif index == 2:
                result, changed = pattern.subn(
                    lambda match: f"{match.group(1)}={policy.replacement}",
                    result,
                )
            else:
                result, changed = pattern.subn(policy.replacement, result)
            count += changed
    for raw_pattern in policy.internal_identifiers:
        result, changed = re.subn(raw_pattern, policy.replacement, result)
        count += changed
    return result, count


def _redact_content(content: Any, policy: EgressPolicy) -> tuple[Any, int]:
    if isinstance(content, str):
        return _redact_text(content, policy)
    if isinstance(content, list):
        output, total = [], 0
        for value in content:
            cleaned, changed = _redact_content(value, policy)
            output.append(cleaned)
            total += changed
        return output, total
    if isinstance(content, tuple):
        output, total = [], 0
        for value in content:
            cleaned, changed = _redact_content(value, policy)
            output.append(cleaned)
            total += changed
        return tuple(output), total
    if isinstance(content, Mapping):
        output, total = {}, 0
        for key, value in content.items():
            cleaned, changed = _redact_content(value, policy)
            output[key] = cleaned
            total += changed
        return output, total
    return content, 0


def evaluate_egress(
    content: Any,
    *,
    egress_class: str,
    metadata: Mapping[str, Any] | None = None,
    policy: EgressPolicy | None = None,
) -> EgressResult:
    egress_class = str(egress_class or "").strip().casefold()
    if egress_class not in EGRESS_CLASSES:
        raise ValueError(f"unknown egress class: {egress_class!r}")
    metadata = dict(metadata or {})
    policy = policy or runtime_egress_policy()
    if not policy.enabled or not _external_target(metadata, egress_class):
        return EgressResult("ALLOW", content, "allow")

    mode = policy.mode_for(egress_class)
    destination = str(
        metadata.get("provider") or metadata.get("backend")
        or metadata.get("target") or metadata.get("base_url") or "external"
    )
    if mode == "block":
        log.warning("egress class=%s mode=block destination=%s action=BLOCK", egress_class, destination)
        return EgressResult("BLOCK", content, mode, "blocked by administrator egress policy")
    if mode in {"allow", "audit"}:
        log.info("egress class=%s mode=%s destination=%s action=ALLOW", egress_class, mode, destination)
        return EgressResult("ALLOW", content, mode)

    cleaned, redactions = _redact_content(content, policy)
    action = "MODIFY" if redactions else "ALLOW"
    log.info(
        "egress class=%s mode=guard destination=%s action=%s redactions=%d",
        egress_class, destination, action, redactions,
    )
    return EgressResult(action, cleaned, mode, redactions=redactions)
