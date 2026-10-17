"""Fail-open provider client for the optional ERG resolver gateway."""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

import httpx

from rag.policy_hooks import OUTBOUND_QUERY, apply_policy_hook

_ORG_CUE_RE = re.compile(
    r"\b(?:gmbh|ag|ug|kg|ohg|gbr|se|eg|kgaa|gmbh\s*&\s*co\.?\s*kg)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolverSettings:
    enabled: bool
    url: str
    timeout: float


@dataclass(frozen=True)
class ResolverOutcome:
    request: dict[str, Any]
    response: dict[str, Any] | None
    error: str = ""


@dataclass(frozen=True)
class ResolverEnrichmentOutcome:
    request: dict[str, Any]
    response: dict[str, Any] | None
    error: str = ""



def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def load_resolver_settings(cfg: dict[str, Any]) -> ResolverSettings:
    section = cfg.get("resolver") or {}
    enabled = _truthy(
        os.getenv("RESOLVER_GATEWAY_ENABLED", section.get("enabled")),
        False,
    )
    tier = str((cfg.get("architecture") or {}).get("tier") or "erg").strip().casefold()
    if tier == "src":
        # External/entity resolver gateways are an ERG capability. SRC retains
        # only its existing administrator-owned Neo4j seed/alias context.
        enabled = False
    url = str(
        os.getenv("RESOLVER_GATEWAY_URL")
        or section.get("url")
        or "http://127.0.0.1:8770"
    ).strip().rstrip("/")
    timeout = max(
        0.5,
        float(
            os.getenv("RESOLVER_GATEWAY_TIMEOUT")
            or section.get("timeout")
            or 8
        ),
    )
    return ResolverSettings(
        enabled=enabled,
        url=url,
        timeout=timeout,
    )


def build_resolve_request(
    search_spec: dict[str, Any] | None,
    *,
    limit_per_reference: int = 8,
    sources: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    spec = search_spec or {}
    context = []
    for item in spec.get("constraints") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if value and value not in context:
            context.append(value)
        if len(context) >= 8:
            break

    entity_hints = {
        str(item.get("text") or "").strip().casefold(): str(item.get("kind") or "").strip().casefold()
        for item in (spec.get("entity_hints") or [])
        if isinstance(item, dict)
        and str(item.get("text") or "").strip()
        and str(item.get("kind") or "").strip()
    }

    references = []
    seen: set[str] = set()
    for raw in spec.get("entities") or []:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        folded = text.casefold()
        if not text or folded in seen:
            continue
        seen.add(folded)
        hints: dict[str, str] = {}
        explicit_kind = entity_hints.get(folded, "")
        if explicit_kind:
            hints["kind"] = explicit_kind
        elif _ORG_CUE_RE.search(text):
            hints["kind"] = "organization"
        references.append({
            "text": text,
            "context": list(context),
            "hints": hints,
        })
        if len(references) >= 20:
            break

    payload: dict[str, Any] = {
        "references": references,
        "limit_per_reference": max(1, min(int(limit_per_reference), 20)),
    }
    cleaned_sources = [
        str(value or "").strip().casefold()
        for value in (sources or [])
        if str(value or "").strip()
    ]
    if cleaned_sources:
        payload["sources"] = list(dict.fromkeys(cleaned_sources))[:8]
    return payload


async def resolve_search_spec(
    settings: ResolverSettings,
    search_spec: dict[str, Any] | None,
    *,
    sources: list[str] | tuple[str, ...] | None = None,
) -> ResolverOutcome:
    request = build_resolve_request(search_spec, sources=sources)
    if not settings.enabled or not request["references"]:
        return ResolverOutcome(request=request, response=None)

    try:
        selected_sources = {
            str(value or "").strip().casefold()
            for value in (sources or ())
            if str(value or "").strip()
        }
        external_resolution = (
            not selected_sources
            or any(source != "neo4j" for source in selected_sources)
        )
        checked_request = dict(request)
        checked_references: list[dict[str, Any]] = []
        for reference in request.get("references") or []:
            checked_reference = dict(reference)
            checked_text = apply_policy_hook(
                OUTBOUND_QUERY,
                content=str(checked_reference.get("text") or ""),
                metadata={
                    "provider": "resolver",
                    "target": settings.url,
                    "kind": "resolver",
                    "egress_class": "research_query",
                    # Only reference.text crosses the external adapter boundary;
                    # reference.context stays local for candidate ranking.
                    "external": external_resolution,
                },
            )
            if not isinstance(checked_text, str):
                raise TypeError("resolver egress policy must return reference text")
            checked_reference["text"] = checked_text
            checked_references.append(checked_reference)
        checked_request["references"] = checked_references
        async with httpx.AsyncClient(timeout=settings.timeout) as client:
            response = await client.post(
                settings.url + "/resolve",
                json=checked_request,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("resolver returned a non-object response")
        return ResolverOutcome(request=checked_request, response=payload)
    except Exception as exc:
        return ResolverOutcome(
            request=request,
            response=None,
            error=f"{type(exc).__name__}: {exc}",
        )


async def enrich_candidate(
    settings: ResolverSettings,
    *,
    source: str,
    candidate_id: str,
    kind: str,
    include_holdings: bool = False,
) -> ResolverEnrichmentOutcome:
    request = {
        "source": str(source or "").strip(),
        "candidate_id": str(candidate_id or "").strip(),
        "kind": str(kind or "").strip(),
        "include_holdings": bool(include_holdings),
    }
    if not settings.enabled or not all(
        request.get(key) for key in ("source", "candidate_id", "kind")
    ):
        return ResolverEnrichmentOutcome(request=request, response=None)

    try:
        checked_candidate_id = apply_policy_hook(
            OUTBOUND_QUERY,
            content=request["candidate_id"],
            metadata={
                "provider": "resolver",
                "target": settings.url,
                "kind": "resolver_enrichment",
                "egress_class": "research_query",
                "external": str(source or "").strip().casefold() != "neo4j",
            },
        )
        if not isinstance(checked_candidate_id, str):
            raise TypeError("resolver enrichment egress policy must return candidate id")
        checked_request = dict(request)
        checked_request["candidate_id"] = checked_candidate_id
        async with httpx.AsyncClient(timeout=settings.timeout) as http:
            response = await http.post(
                settings.url + "/enrich",
                json=checked_request,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("resolver enrichment returned a non-object response")
        return ResolverEnrichmentOutcome(request=checked_request, response=payload)
    except Exception as exc:
        return ResolverEnrichmentOutcome(
            request=request,
            response=None,
            error=f"{type(exc).__name__}: {exc}",
        )



def entity_context_from_resolver(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Translate resolved gateway results into the mature planner entity contract.

    Ambiguous/candidate-only resolver output is kept out of query expansion. The
    gateway may inform provenance without silently selecting one external entity.
    """
    entities: list[dict[str, Any]] = []
    expansions: list[dict[str, Any]] = []

    for result in (payload or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        reference = str(result.get("reference") or "").strip()
        status = str(result.get("status") or "").strip().casefold()
        candidates = [
            item for item in (result.get("candidates") or [])
            if isinstance(item, dict)
        ]
        if not reference:
            continue

        if status != "resolved" or not candidates:
            entities.append({
                "mention": reference,
                "status": "ambiguous" if candidates else "unresolved",
                "entity_id": "",
                "display_name": "",
                "matched_form": reference,
                "entity_type": "",
                "resolution_method": "erg_resolver",
                "candidates": [
                    {
                        "entity_id": str(item.get("candidate_id") or ""),
                        "display_name": str(item.get("canonical") or ""),
                        "entity_type": str(item.get("kind") or ""),
                        "similarity": item.get("confidence"),
                    }
                    for item in candidates[:3]
                ],
            })
            continue

        candidate = candidates[0]
        source = str(candidate.get("source") or "resolver").strip()
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        canonical = str(candidate.get("canonical") or reference).strip()
        kind = str(candidate.get("kind") or "").strip()
        confidence = float(candidate.get("confidence") or 0.0)
        entity_id = f"{source}:{candidate_id}" if candidate_id else f"{source}:{canonical}"

        entities.append({
            "mention": reference,
            "status": "resolved",
            "entity_id": entity_id,
            "display_name": canonical,
            "matched_form": reference,
            "entity_type": kind,
            "resolution_method": f"erg_resolver:{source}",
            "candidates": [],
        })
        expansions.append({
            "entity_id": entity_id,
            "entity_type": kind,
            "value": canonical,
            "weight": max(0.5, min(confidence or 0.9, 1.0)),
            "kind": "canonical_name",
            "active": True,
            "source": source,
            "resolution": "erg_resolver",
            "original_mention": reference,
        })
        for alias in candidate.get("aliases") or []:
            value = str(alias or "").strip()
            if not value:
                continue
            expansions.append({
                "entity_id": entity_id,
                "entity_type": kind,
                "value": value,
                "weight": max(0.45, min((confidence or 0.9) * 0.9, 0.95)),
                "kind": "alias",
                "active": True,
                "source": source,
                "resolution": "erg_resolver",
                "original_mention": reference,
            })

    return {
        "enabled": True,
        "entities": entities,
        "elastic_phrase_expansion": expansions,
        "error": None,
    }


def merge_entity_context(
    base: dict[str, Any] | None,
    additional: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge resolver output without creating two identities for one mention.

    Existing locally resolved Neo4j identity wins. A newly resolved gateway
    result may replace only an unresolved/ambiguous local mention.
    """
    merged = dict(base or {})
    merged.setdefault("enabled", True)
    merged["entities"] = [
        dict(item)
        for item in (merged.get("entities") or [])
        if isinstance(item, dict)
    ]
    merged["elastic_phrase_expansion"] = [
        dict(item)
        for item in (merged.get("elastic_phrase_expansion") or [])
        if isinstance(item, dict)
    ]
    merged.setdefault("error", None)

    accepted_entity_ids: set[str] = set()
    for item in (additional or {}).get("entities") or []:
        if not isinstance(item, dict):
            continue
        mention = str(item.get("mention") or "").strip()
        mention_key = mention.casefold()
        if not mention_key:
            continue
        new_status = str(item.get("status") or "").strip().casefold()

        same_indexes = [
            index
            for index, current in enumerate(merged["entities"])
            if str(current.get("mention") or "").strip().casefold() == mention_key
        ]
        if same_indexes:
            if any(
                str(merged["entities"][index].get("status") or "").strip().casefold()
                == "resolved"
                for index in same_indexes
            ):
                # The administrator-owned/local identity is already definitive
                # for query expansion. Keep the external result as archive
                # provenance only.
                continue
            if new_status == "resolved":
                for index in reversed(same_indexes):
                    del merged["entities"][index]
            else:
                # Do not multiply unresolved candidate sets for one mention.
                continue

        merged["entities"].append(dict(item))
        entity_id = str(item.get("entity_id") or "").strip()
        if entity_id:
            accepted_entity_ids.add(entity_id)

    expansion_keys = {
        (
            str(item.get("entity_id") or ""),
            str(item.get("value") or "").casefold(),
        )
        for item in merged["elastic_phrase_expansion"]
        if isinstance(item, dict)
    }
    for item in (additional or {}).get("elastic_phrase_expansion") or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        if entity_id not in accepted_entity_ids:
            continue
        key = (
            entity_id,
            str(item.get("value") or "").casefold(),
        )
        if key not in expansion_keys:
            merged["elastic_phrase_expansion"].append(dict(item))
            expansion_keys.add(key)

    return merged
