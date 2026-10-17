"""Build inspectable pre-retrieval strategy records from normalized provider state."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Mapping

from rag.retrieval.contracts import (
    EffectiveRetrievalPlan,
    RetrievalDecisionRecord,
    RetrievalPlannerProposal,
    RetrievalPolicyResolution,
)
from rag.architecture_policy import (
    ERG_FULL,
    ERG_INTERNAL,
    SRC,
    LOCAL_RESOLVER_ADAPTERS,
    normalize_architecture_tier,
)
from rag.search_spec import normalize_search_spec


_QUOTED_PHRASE_RE = re.compile(r'["“”„][^"“”„]{2,}["“”„]')


def build_retrieval_decision_record(
    *,
    query_id: str,
    original_query: str,
    normalized_query: str,
    search_spec: Mapping[str, Any],
    architecture_tier: str,
    model_id: str,
    retrieval_arms: set[str] | tuple[str, ...] | list[str] | None,
    source_scopes: set[str] | tuple[str, ...] | list[str] | None,
    explicit_arm_selection: bool,
    web_requested: bool,
    resolver_research_requested: bool,
    resolver_gateway_enabled: bool,
    resolver_sources: list[str] | tuple[str, ...],
    exhaustive: bool,
    bounded_document_set: bool,
    max_rounds: int,
    max_queries_per_round: int,
    verification_candidate_limit: int,
    max_complete_documents: int,
    planner_status: str = "planned",
    block_retrieval: bool = False,
) -> RetrievalDecisionRecord:
    """Return a safe audit record of proposal, policy resolution and effective plan."""
    planner_status = str(planner_status or "planned").strip().casefold()
    normalized_spec = normalize_search_spec(
        dict(search_spec or {}) if planner_status == "planned" else {},
        original_query=(normalized_query if planner_status == "planned" else ""),
    )
    policy_arms = tuple(sorted(
        str(value).strip().casefold()
        for value in (retrieval_arms or ())
        if str(value).strip()
    ))
    policy_scopes = tuple(sorted(
        str(value).strip().casefold()
        for value in (
            source_scopes
            if source_scopes is not None
            else (("documents",) if policy_arms else ())
        )
        if str(value).strip()
    ))
    tier = normalize_architecture_tier(architecture_tier)
    policy_web_enabled = bool(web_requested and tier == ERG_FULL)
    effective_arms = (
        ()
        if block_retrieval
        else tuple(sorted(set(policy_arms) | ({"web"} if policy_web_enabled else set())))
    )
    effective_scopes = () if block_retrieval else policy_scopes

    requested_resolver_sources = tuple(sorted(
        str(value).strip().casefold()
        for value in resolver_sources
        if str(value).strip()
    ))
    allowed_resolver_sources = requested_resolver_sources
    if tier == SRC:
        allowed_resolver_sources = ()
    elif tier == ERG_INTERNAL:
        allowed_resolver_sources = tuple(
            value for value in requested_resolver_sources
            if value in LOCAL_RESOLVER_ADAPTERS
        )
    resolver_research_enabled = bool(
        resolver_research_requested
        and resolver_gateway_enabled
        and allowed_resolver_sources
        and tier in {ERG_INTERNAL, ERG_FULL}
    )
    effective_resolver_sources = (
        allowed_resolver_sources if resolver_research_enabled else ()
    )

    reasons = [
        {
            SRC: "src_tier",
            ERG_INTERNAL: "erg_internal_tier",
            ERG_FULL: "erg_full_tier",
        }.get(tier, "unknown_tier"),
        (
            "explicit_retrieval_arms"
            if explicit_arm_selection
            else "policy_default_retrieval_arms"
        ),
    ]
    if source_scopes is not None:
        reasons.append("explicit_source_scopes")
    if web_requested:
        reasons.append("web_requested")
        if policy_web_enabled:
            reasons.append("web_enabled")
        elif tier == SRC:
            reasons.append("web_disallowed_in_src")
        else:
            reasons.append("web_disallowed_in_erg_internal")
    if planner_status == "failed":
        reasons.append("planner_rewrite_failed")
    elif planner_status == "not_run":
        reasons.append("planner_not_run")
    if resolver_research_requested:
        reasons.append("resolver_research_requested")
        if resolver_research_enabled:
            reasons.append("resolver_research_enabled")
        elif tier == SRC:
            reasons.append("resolver_disallowed_in_src")
        elif requested_resolver_sources and not allowed_resolver_sources:
            reasons.append("resolver_source_disallowed_by_tier")
        else:
            reasons.append("resolver_disabled_by_admin")
    if exhaustive:
        reasons.append("exhaustive_intent")
    elif bounded_document_set:
        reasons.append("bounded_document_set")
    if _QUOTED_PHRASE_RE.search(normalized_query):
        reasons.append("quoted_phrase")

    return RetrievalDecisionRecord(
        query_id=query_id,
        original_query=original_query,
        normalized_query=normalized_query,
        planner=RetrievalPlannerProposal(
            status=planner_status,
            intent=str(normalized_query or "")[:4000],
            # The planner does not own backend query syntax. The legacy field
            # remains empty; compiled ES details belong to execution telemetry.
            elastic_query="",
            semantic_query=str(normalized_spec.get("semantic_query") or ""),
            entities=tuple(
                str(value)
                for value in (normalized_spec.get("entities") or [])
            ),
            entity_hints=tuple(
                (
                    str(item.get("text") or ""),
                    str(item.get("kind") or ""),
                )
                for item in (normalized_spec.get("entity_hints") or [])
                if isinstance(item, dict)
            ),
            concepts=tuple(
                str(value)
                for value in (normalized_spec.get("concepts") or [])
            ),
            concept_groups=tuple(
                (
                    bool(item.get("required")),
                    tuple(str(term) for term in (item.get("terms") or [])),
                )
                for item in (normalized_spec.get("concept_groups") or [])
                if isinstance(item, dict)
            ),
            constraints=tuple(
                (
                    str(item.get("kind") or ""),
                    str(item.get("value") or ""),
                )
                for item in (normalized_spec.get("constraints") or [])
                if isinstance(item, dict)
            ),
            verification_requirements=tuple(
                str(value)
                for value in (
                    normalized_spec.get("verification_requirements") or []
                )
            ),
        ),
        policy=RetrievalPolicyResolution(
            architecture_tier=tier,
            model_id=model_id,
            retrieval_arms=policy_arms,
            source_scopes=policy_scopes,
            requested_resolver_sources=requested_resolver_sources,
            resolver_sources=effective_resolver_sources,
            reason_codes=tuple(reasons),
            web_requested=bool(web_requested),
            web_enabled=bool(policy_web_enabled),
            resolver_research_requested=bool(resolver_research_requested),
            resolver_research_enabled=bool(resolver_research_enabled),
        ),
        effective_plan=EffectiveRetrievalPlan(
            retrieval_arms=effective_arms,
            source_scopes=effective_scopes,
            resolver_sources=effective_resolver_sources,
            elastic_query="",
            semantic_query=str(normalized_spec.get("semantic_query") or ""),
            exhaustive=bool(exhaustive),
            bounded_document_set=bool(bounded_document_set),
            max_rounds=int(max_rounds),
            max_queries_per_round=int(max_queries_per_round),
            verification_candidate_limit=int(verification_candidate_limit),
            max_complete_documents=int(max_complete_documents),
        ),
        created_at=datetime.now().astimezone().isoformat(),
    )
