"""Provider-neutral contracts for resolution, retrieval and research evidence.

The contracts intentionally contain no transport endpoints, credentials,
Elasticsearch DSL, Cypher or provider-specific response fields.  Planner output
may influence requests, but code-issued grants define the capability envelope.

A central invariant is that private retrieval candidates are not evidence until
an authoritative authorization path has hydrated and live-ACL-checked them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable


MAX_RETRIEVAL_RESULTS = 100
MAX_RETRIEVAL_ROUNDS = 8
MAX_EVIDENCE_CHARS = 2_000_000
MAX_RESEARCH_ARTIFACTS = 32
MAX_EVIDENCE_TEXT_CHARS = 500_000
MAX_METADATA_CHARS = 2_000_000
MAX_DIAGNOSTICS_CHARS = 500_000
MAX_QUERY_CHARS = 8_000
MAX_RESEARCH_GOAL_TERMS = 16
MAX_RESEARCH_TERM_CHARS = 200
MAX_RESOLVED_ALIASES = 20
MAX_RESOLVED_ALIAS_CHARS = 300
MAX_CONSTRAINT_VALUE_CHARS = 512
MAX_RESOLVER_CONTEXT_CHARS = 500
MAX_DECISION_QUERY_CHARS = 8_000
MAX_DECISION_TEXT_CHARS = 4_000
MAX_DECISION_ITEMS = 32
MAX_DECISION_REASON_CHARS = 80

KNOWN_ERG_CAPABILITIES = frozenset({
    "vector",
    "research-graph",
    "web",
    "openregister-research",
})
KNOWN_RESOLVER_ADAPTERS = frozenset({
    "neo4j",
    "openplz",
    "openregister",
})
KNOWN_ERG_CONSTRAINTS = frozenset({
    "country",
    "date_from",
    "date_to",
    "jurisdiction",
    "language",
    "record_type",
    "source",
})


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _require_bounded_mapping(
    value: Mapping[str, Any],
    *,
    label: str,
    max_chars: int,
) -> None:
    if _serialized_size(dict(value)) > max_chars:
        raise ValueError(f"{label} exceeds {max_chars} serialized characters")


@dataclass(frozen=True, slots=True)
class RetrievalIdentity:
    """Opaque request identity passed to an authorization-aware retriever."""

    user_id: str | None = None
    groups: tuple[str, ...] = ()
    request_id: str | None = None

    def __post_init__(self) -> None:
        normalized = tuple(
            value
            for value in (
                str(group).strip()
                for group in self.groups
            )
            if value
        )
        object.__setattr__(self, "groups", normalized)


@dataclass(frozen=True, slots=True)
class RetrievalBudgetGrant:
    """Code-issued hard upper bounds for one retrieval operation."""

    max_results: int
    max_rounds: int = 1
    max_evidence_chars: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= MAX_RETRIEVAL_RESULTS:
            raise ValueError(
                f"max_results must be between 1 and {MAX_RETRIEVAL_RESULTS}"
            )
        if not 1 <= self.max_rounds <= MAX_RETRIEVAL_ROUNDS:
            raise ValueError(
                f"max_rounds must be between 1 and {MAX_RETRIEVAL_ROUNDS}"
            )
        if self.max_evidence_chars is not None and not (
            1 <= self.max_evidence_chars <= MAX_EVIDENCE_CHARS
        ):
            raise ValueError(
                "max_evidence_chars must be between 1 and "
                f"{MAX_EVIDENCE_CHARS} when set"
            )


# Backwards-compatible name for the first architecture slice.  New code should
# use RetrievalBudgetGrant to make the authority semantics explicit.
RetrievalBudget = RetrievalBudgetGrant


def _bounded_string_tuple(
    values: tuple[str, ...],
    *,
    label: str,
    max_items: int = MAX_DECISION_ITEMS,
    max_chars: int = MAX_DECISION_TEXT_CHARS,
) -> tuple[str, ...]:
    cleaned = tuple(str(value or "").strip() for value in values if str(value or "").strip())
    if len(cleaned) > max_items:
        raise ValueError(f"{label} exceeds hard count limit")
    if any(len(value) > max_chars for value in cleaned):
        raise ValueError(f"{label} item exceeds hard length limit")
    return cleaned


def _bounded_concept_groups(
    values: tuple[tuple[bool, tuple[str, ...]], ...],
    *,
    label: str,
) -> tuple[tuple[bool, tuple[str, ...]], ...]:
    if len(values) > MAX_DECISION_ITEMS:
        raise ValueError(f"{label} exceeds hard count limit")
    cleaned: list[tuple[bool, tuple[str, ...]]] = []
    for required, terms in values:
        clean_terms = _bounded_string_tuple(
            tuple(terms),
            label=f"{label} terms",
            max_items=12,
            max_chars=300,
        )
        if clean_terms:
            cleaned.append((bool(required), clean_terms))
    return tuple(cleaned)


def _bounded_pair_tuple(
    values: tuple[tuple[str, str], ...],
    *,
    label: str,
    max_items: int = MAX_DECISION_ITEMS,
    max_chars: int = MAX_DECISION_TEXT_CHARS,
) -> tuple[tuple[str, str], ...]:
    if len(values) > max_items:
        raise ValueError(f"{label} exceeds hard count limit")
    cleaned: list[tuple[str, str]] = []
    for key, value in values:
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if not clean_key or not clean_value:
            continue
        if len(clean_key) > 120 or len(clean_value) > max_chars:
            raise ValueError(f"{label} item exceeds hard length limit")
        cleaned.append((clean_key, clean_value))
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class RetrievalPlannerProposal:
    """Inspectable planner output; never hidden model reasoning."""

    status: str = "planned"
    intent: str = ""
    elastic_query: str = ""
    semantic_query: str = ""
    entities: tuple[str, ...] = ()
    entity_hints: tuple[tuple[str, str], ...] = ()
    concepts: tuple[str, ...] = ()
    concept_groups: tuple[tuple[bool, tuple[str, ...]], ...] = ()
    constraints: tuple[tuple[str, str], ...] = ()
    verification_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = str(self.status or "").strip().casefold()
        if status not in {"planned", "not_run", "failed"}:
            raise ValueError("planner status must be planned, not_run, or failed")
        object.__setattr__(self, "status", status)
        for label, value in (
            ("planner intent", self.intent),
            ("planner elastic query", self.elastic_query),
            ("planner semantic query", self.semantic_query),
        ):
            if len(str(value or "")) > MAX_DECISION_TEXT_CHARS:
                raise ValueError(f"{label} exceeds hard length limit")
        object.__setattr__(
            self, "entities", _bounded_string_tuple(self.entities, label="planner entities")
        )
        object.__setattr__(
            self,
            "entity_hints",
            _bounded_pair_tuple(self.entity_hints, label="planner entity hints"),
        )
        object.__setattr__(
            self, "concepts", _bounded_string_tuple(self.concepts, label="planner concepts")
        )
        object.__setattr__(
            self,
            "concept_groups",
            _bounded_concept_groups(self.concept_groups, label="planner concept groups"),
        )
        object.__setattr__(
            self,
            "constraints",
            _bounded_pair_tuple(self.constraints, label="planner constraints"),
        )
        object.__setattr__(
            self,
            "verification_requirements",
            _bounded_string_tuple(
                self.verification_requirements,
                label="planner verification requirements",
            ),
        )


@dataclass(frozen=True, slots=True)
class RetrievalPolicyResolution:
    """Deterministic policy result applied to the planner proposal."""

    architecture_tier: str
    model_id: str
    retrieval_arms: tuple[str, ...] = ()
    source_scopes: tuple[str, ...] = ()
    requested_resolver_sources: tuple[str, ...] = ()
    resolver_sources: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    web_requested: bool = False
    web_enabled: bool = False
    resolver_research_requested: bool = False
    resolver_research_enabled: bool = False

    def __post_init__(self) -> None:
        tier = str(self.architecture_tier or "").strip().casefold()
        tier = {
            "erg": "erg_full",
            "erg-internal": "erg_internal",
            "erg-full": "erg_full",
        }.get(tier, tier)
        if tier not in {"src", "erg_internal", "erg_full"}:
            raise ValueError(
                "retrieval decision architecture tier must be "
                "src, erg_internal, or erg_full"
            )
        if not str(self.model_id or "").strip():
            raise ValueError("retrieval decision model_id is required")
        object.__setattr__(self, "architecture_tier", tier)
        for field_name, label in (
            ("retrieval_arms", "policy retrieval arms"),
            ("source_scopes", "policy source scopes"),
            ("requested_resolver_sources", "policy requested resolver sources"),
            ("resolver_sources", "policy resolver sources"),
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded_string_tuple(getattr(self, field_name), label=label),
            )
        reasons = _bounded_string_tuple(
            self.reason_codes,
            label="policy reason codes",
            max_chars=MAX_DECISION_REASON_CHARS,
        )
        object.__setattr__(
            self,
            "reason_codes",
            tuple(str(value).strip().casefold() for value in reasons),
        )


@dataclass(frozen=True, slots=True)
class EffectiveRetrievalPlan:
    """Exact bounded strategy handed to resolver/retriever execution."""

    retrieval_arms: tuple[str, ...] = ()
    source_scopes: tuple[str, ...] = ()
    resolver_sources: tuple[str, ...] = ()
    elastic_query: str = ""
    semantic_query: str = ""
    exhaustive: bool = False
    bounded_document_set: bool = False
    max_rounds: int = 1
    max_queries_per_round: int = 1
    verification_candidate_limit: int = 1
    max_complete_documents: int = 1

    def __post_init__(self) -> None:
        for field_name, label in (
            ("retrieval_arms", "effective retrieval arms"),
            ("source_scopes", "effective source scopes"),
            ("resolver_sources", "effective resolver sources"),
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded_string_tuple(getattr(self, field_name), label=label),
            )
        if len(self.elastic_query) > MAX_DECISION_TEXT_CHARS:
            raise ValueError("effective elastic query exceeds hard length limit")
        if len(self.semantic_query) > MAX_DECISION_TEXT_CHARS:
            raise ValueError("effective semantic query exceeds hard length limit")
        if not 1 <= int(self.max_rounds) <= MAX_RETRIEVAL_ROUNDS:
            raise ValueError("effective max_rounds exceeds retrieval hard limits")
        for label, value in (
            ("max_queries_per_round", self.max_queries_per_round),
            ("verification_candidate_limit", self.verification_candidate_limit),
            ("max_complete_documents", self.max_complete_documents),
        ):
            if int(value) < 1 or int(value) > 1000:
                raise ValueError(f"effective {label} exceeds hard limits")


@dataclass(frozen=True, slots=True)
class RetrievalDecisionRecord:
    """Versioned pre-retrieval audit record safe for status/persistence surfaces."""

    query_id: str
    original_query: str
    normalized_query: str
    planner: RetrievalPlannerProposal
    policy: RetrievalPolicyResolution
    effective_plan: EffectiveRetrievalPlan
    created_at: str = ""
    schema: str = "sunaq.retrieval_decision.v2"

    def __post_init__(self) -> None:
        if not self.query_id.strip() or len(self.query_id) > 160:
            raise ValueError("retrieval decision query_id is invalid")
        if len(self.original_query) > MAX_DECISION_QUERY_CHARS:
            raise ValueError("retrieval decision original query exceeds hard length limit")
        if len(self.normalized_query) > MAX_DECISION_QUERY_CHARS:
            raise ValueError("retrieval decision normalized query exceeds hard length limit")
        if self.schema != "sunaq.retrieval_decision.v2":
            raise ValueError("unsupported retrieval decision schema")
        if len(self.created_at) > 80:
            raise ValueError("retrieval decision timestamp exceeds hard length limit")

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted v2 contract without legacy backend-syntax fields."""
        payload = asdict(self)
        planner = payload.get("planner") or {}
        planner.pop("intent", None)
        planner.pop("elastic_query", None)
        effective = payload.get("effective_plan") or {}
        effective.pop("elastic_query", None)
        effective.pop("semantic_query", None)
        return payload


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """Immutable, code-known ERG capability grant."""

    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        cleaned = frozenset(
            str(value).strip().casefold()
            for value in self.capabilities
            if str(value).strip()
        )
        if not cleaned:
            raise ValueError("capability grant must not be empty")
        unknown = cleaned - KNOWN_ERG_CAPABILITIES
        if unknown:
            raise ValueError(
                "unknown ERG capabilities: " + ", ".join(sorted(unknown))
            )
        object.__setattr__(self, "capabilities", cleaned)

    def allows(self, capability: str) -> bool:
        return str(capability or "").strip().casefold() in self.capabilities


@dataclass(frozen=True, slots=True)
class ResolverGrant:
    """Immutable, code-known resolver adapter grant."""

    adapters: frozenset[str]

    def __post_init__(self) -> None:
        cleaned = frozenset(
            str(value).strip().casefold()
            for value in self.adapters
            if str(value).strip()
        )
        if not cleaned:
            raise ValueError("resolver adapter grant must not be empty")
        unknown = cleaned - KNOWN_RESOLVER_ADAPTERS
        if unknown:
            raise ValueError(
                "unknown resolver adapters: " + ", ".join(sorted(unknown))
            )
        object.__setattr__(self, "adapters", cleaned)

    def allows(self, adapter: str) -> bool:
        return str(adapter or "").strip().casefold() in self.adapters


@dataclass(frozen=True, slots=True)
class PrivateCandidate:
    """Private retrieval signal that is explicitly *not* user-visible evidence.

    Qdrant/private-graph adapters may return only an opaque document locator and
    bounded ranking/trace signals. There is deliberately no free-form metadata,
    text, snippet or content channel. The candidate must traverse authoritative
    Nextcloud hydration/live ACL before document content becomes evidence.
    """

    document_id: str
    source: str
    rank: int | None = None
    score: float | None = None
    candidate_kind: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("private candidate document_id is required")
        if not self.source.strip():
            raise ValueError("private candidate source is required")
        if self.rank is not None and self.rank < 1:
            raise ValueError("private candidate rank must be positive")
        if len(self.candidate_kind) > 80 or len(self.trace_id) > 200:
            raise ValueError("private candidate trace metadata exceeds hard limits")


@dataclass(frozen=True, slots=True)
class AuthorizedDocumentEvidence:
    """Private document content returned only after authoritative live ACL."""

    index: int
    title: str
    text: str
    document_id: str
    source: str = "src"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("authorized document evidence index must be positive")
        if not self.document_id.strip():
            raise ValueError("authorized document evidence document_id is required")
        if not self.source.strip():
            raise ValueError("authorized document evidence source is required")
        if len(self.text) > MAX_EVIDENCE_TEXT_CHARS:
            raise ValueError("authorized document evidence text exceeds hard limit")
        _require_bounded_mapping(
            self.metadata,
            label="authorized document evidence metadata",
            max_chars=MAX_METADATA_CHARS,
        )
        _require_bounded_mapping(
            self.provenance,
            label="authorized document evidence provenance",
            max_chars=MAX_METADATA_CHARS,
        )

    @property
    def evidence_id(self) -> str:
        return self.document_id


@dataclass(frozen=True, slots=True)
class ExternalEvidence:
    """Evidence from an explicitly granted external/public ERG capability."""

    index: int
    title: str
    text: str
    source: str
    evidence_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("external evidence index must be positive")
        if not self.source.strip():
            raise ValueError("external evidence source is required")
        if len(self.text) > MAX_EVIDENCE_TEXT_CHARS:
            raise ValueError("external evidence text exceeds hard limit")
        _require_bounded_mapping(
            self.metadata,
            label="external evidence metadata",
            max_chars=MAX_METADATA_CHARS,
        )
        _require_bounded_mapping(
            self.provenance,
            label="external evidence provenance",
            max_chars=MAX_METADATA_CHARS,
        )


EvidenceRecord = AuthorizedDocumentEvidence | ExternalEvidence


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    """Retriever output with trust classes kept structurally separate."""

    private_candidates: tuple[PrivateCandidate, ...] = ()
    document_evidence: tuple[AuthorizedDocumentEvidence, ...] = ()
    external_evidence: tuple[ExternalEvidence, ...] = ()
    artifacts: tuple["ResearchArtifact", ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.artifacts) > MAX_RESEARCH_ARTIFACTS:
            raise ValueError(
                f"at most {MAX_RESEARCH_ARTIFACTS} research artifacts are allowed per batch"
            )
        evidence = (*self.document_evidence, *self.external_evidence)
        indices = [item.index for item in evidence]
        if len(indices) != len(set(indices)):
            raise ValueError("retrieval evidence indices must be globally unique")
        _require_bounded_mapping(
            self.diagnostics,
            label="retrieval diagnostics",
            max_chars=MAX_DIAGNOSTICS_CHARS,
        )

    @property
    def evidence(self) -> tuple[EvidenceRecord, ...]:
        """Return evidence in the final joint ranking order, never private candidates."""
        return tuple(
            sorted(
                (*self.document_evidence, *self.external_evidence),
                key=lambda item: item.index,
            )
        )

    def validate_budget(self, budget: RetrievalBudgetGrant) -> None:
        """Fail closed if this result exceeds the code-issued retrieval budget."""
        result_count = (
            len(self.private_candidates)
            + len(self.document_evidence)
            + len(self.external_evidence)
        )
        if result_count > budget.max_results:
            raise ValueError("retrieval batch exceeds max_results budget")
        if budget.max_evidence_chars is not None:
            evidence_chars = sum(len(item.text) for item in self.evidence)
            if evidence_chars > budget.max_evidence_chars:
                raise ValueError("retrieval batch exceeds max_evidence_chars budget")


@dataclass(frozen=True, slots=True)
class SrcRetrievalRequest:
    """Request for the secure internal document-retrieval core.

    There is deliberately no arm, endpoint or source selector.  The SRC
    implementation is fixed to ordinary Nextcloud documents via
    Elasticsearch/FullTextSearch and must finish with live Nextcloud ACL.
    """

    query: str
    identity: RetrievalIdentity
    budget: RetrievalBudgetGrant
    model_id: str = ""
    search_spec: Mapping[str, Any] | None = None
    query_context: Mapping[str, Any] | None = None
    entity_recall: bool = False
    raw_results: bool = False
    force_unspecific: bool = False

    def __post_init__(self) -> None:
        if self.budget.max_rounds != 1:
            raise ValueError("SRC retrieval is limited to exactly one round")


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """Provider-neutral identity supplied to research retrieval."""

    entity_id: str
    canonical_name: str
    entity_kind: str = ""
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.entity_id.strip() or not self.canonical_name.strip():
            raise ValueError("resolved entity id and canonical_name are required")
        if len(self.entity_id) > 500 or len(self.canonical_name) > 500:
            raise ValueError("resolved entity identity fields exceed hard limits")
        aliases = tuple(str(value).strip() for value in self.aliases if str(value).strip())
        if len(aliases) > MAX_RESOLVED_ALIASES:
            raise ValueError("resolved entity aliases exceed hard count limit")
        if any(len(value) > MAX_RESOLVED_ALIAS_CHARS for value in aliases):
            raise ValueError("resolved entity alias exceeds hard length limit")
        object.__setattr__(self, "aliases", aliases)


@dataclass(frozen=True, slots=True)
class ResearchGoal:
    """Provider-neutral research intent, not a provider query schema."""

    name: str
    terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = str(self.name or "").strip().casefold()
        if not normalized:
            raise ValueError("research goal name is required")
        if len(normalized) > 80:
            raise ValueError("research goal name exceeds 80 characters")
        terms = tuple(str(value).strip() for value in self.terms if str(value).strip())
        if len(terms) > MAX_RESEARCH_GOAL_TERMS:
            raise ValueError("research goal terms exceed hard count limit")
        if any(len(value) > MAX_RESEARCH_TERM_CHARS for value in terms):
            raise ValueError("research goal term exceeds hard length limit")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(self, "terms", terms)


@dataclass(frozen=True, slots=True)
class ErgRetrievalRequest:
    """Request for research arms inside an authoritative ERG capability grant."""

    query: str
    identity: RetrievalIdentity
    budget: RetrievalBudgetGrant
    grant: CapabilityGrant
    entities: tuple[ResolvedEntity, ...] = ()
    goals: tuple[ResearchGoal, ...] = ()
    constraints: tuple[tuple[str, str], ...] = ()
    # Transitional SunaQ-normalized planner inputs used by the existing
    # combined middleware adapter. They are internal schemas, not provider
    # endpoints or arbitrary backend query languages.
    search_spec: Mapping[str, Any] | None = None
    query_context: Mapping[str, Any] | None = None
    entity_recall: bool = False
    raw_results: bool = False
    force_unspecific: bool = False

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("ERG retrieval query is required")
        if len(self.query) > MAX_QUERY_CHARS:
            raise ValueError("ERG retrieval query exceeds hard length limit")
        if len(self.entities) > 20:
            raise ValueError("ERG retrieval is limited to 20 resolved entities")
        if len(self.goals) > 32:
            raise ValueError("ERG retrieval is limited to 32 research goals")
        if len(self.constraints) > 32:
            raise ValueError("ERG retrieval is limited to 32 constraints")
        normalized_constraints: list[tuple[str, str]] = []
        for key, value in self.constraints:
            normalized_key = str(key or "").strip().casefold()
            normalized_value = str(value or "").strip()
            if normalized_key not in KNOWN_ERG_CONSTRAINTS:
                raise ValueError(f"unknown ERG research constraint: {key}")
            if not normalized_value:
                raise ValueError("ERG research constraint value is required")
            if len(normalized_value) > MAX_CONSTRAINT_VALUE_CHARS:
                raise ValueError("ERG research constraint value exceeds hard length limit")
            normalized_constraints.append((normalized_key, normalized_value))
        object.__setattr__(self, "constraints", tuple(normalized_constraints))
        if self.search_spec is not None:
            _require_bounded_mapping(
                self.search_spec,
                label="ERG search_spec",
                max_chars=MAX_DIAGNOSTICS_CHARS,
            )
        if self.query_context is not None:
            _require_bounded_mapping(
                self.query_context,
                label="ERG query_context",
                max_chars=MAX_DIAGNOSTICS_CHARS,
            )

    @property
    def capabilities(self) -> frozenset[str]:
        """Compatibility/read-only view of the authoritative grant."""
        return self.grant.capabilities


@dataclass(frozen=True, slots=True)
class ResolverReference:
    text: str
    context: tuple[str, ...] = ()
    hints: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("resolver reference text is required")
        if len(self.text) > 300:
            raise ValueError("resolver reference text exceeds 300 characters")
        if len(self.context) > 8:
            raise ValueError("resolver reference context is limited to 8 items")
        context = tuple(str(value).strip() for value in self.context if str(value).strip())
        if any(len(value) > MAX_RESOLVER_CONTEXT_CHARS for value in context):
            raise ValueError("resolver reference context item exceeds hard length limit")
        object.__setattr__(self, "context", context)
        if len(self.hints) > 8:
            raise ValueError("resolver reference hints are limited to 8 items")
        _require_bounded_mapping(
            self.hints,
            label="resolver reference hints",
            max_chars=10_000,
        )


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    references: tuple[ResolverReference, ...]
    grant: ResolverGrant
    limit_per_reference: int = 8

    def __post_init__(self) -> None:
        if not self.references:
            raise ValueError("at least one resolver reference is required")
        if len(self.references) > 20:
            raise ValueError("at most 20 resolver references are allowed")
        if not 1 <= self.limit_per_reference <= 20:
            raise ValueError("limit_per_reference must be between 1 and 20")

    @property
    def allowed_adapters(self) -> frozenset[str]:
        """Compatibility/read-only view of the authoritative resolver grant."""
        return self.grant.adapters


@dataclass(frozen=True, slots=True)
class ResolverCandidate:
    adapter: str
    candidate_id: str
    canonical_name: str
    entity_kind: str = ""
    aliases: tuple[str, ...] = ()
    confidence: float | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.adapter.strip() or not self.candidate_id.strip():
            raise ValueError("resolver adapter and candidate_id are required")
        if not self.canonical_name.strip():
            raise ValueError("resolver canonical_name is required")
        if self.adapter.strip().casefold() not in KNOWN_RESOLVER_ADAPTERS:
            raise ValueError(f"unknown resolver adapter: {self.adapter}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("resolver confidence must be between 0 and 1")
        _require_bounded_mapping(
            self.provenance,
            label="resolver candidate provenance",
            max_chars=MAX_METADATA_CHARS,
        )


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    reference: ResolverReference
    status: str
    candidates: tuple[ResolverCandidate, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = self.status.strip().casefold()
        if normalized not in {"resolved", "candidates", "unresolved", "error"}:
            raise ValueError(f"unsupported resolution status: {self.status}")
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True, slots=True)
class ResolutionBatch:
    results: tuple[ResolutionResult, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_bounded_mapping(
            self.diagnostics,
            label="resolver diagnostics",
            max_chars=MAX_DIAGNOSTICS_CHARS,
        )


class ArtifactMode(str, Enum):
    INLINE_TEXT = "inline_text"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class ResearchArtifact:
    """Portable artifact descriptor emitted by an ERG adapter.

    Remote artifacts contain only an HTTPS URL; credentials and arbitrary
    request headers are intentionally not part of the contract.  The provider
    remains responsible for SSRF-safe fetch and persistence policy.
    """

    source: str
    external_id: str
    file_name: str
    content_type: str
    mode: ArtifactMode
    title: str = ""
    content: str = ""
    url: str = ""
    include_in_summary: bool = False
    summary_markdown: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            normalized_mode = ArtifactMode(self.mode)
        except ValueError as exc:
            raise ValueError(f"unsupported research artifact mode: {self.mode}") from exc
        object.__setattr__(self, "mode", normalized_mode)
        if not self.source.strip() or not self.external_id.strip():
            raise ValueError("research artifact source and external_id are required")
        if not self.file_name.strip() or not self.content_type.strip():
            raise ValueError("research artifact file_name and content_type are required")
        if any(separator in self.file_name for separator in ("/", "\\")):
            raise ValueError("research artifact file_name must be a path-free leaf name")
        if self.file_name.strip() in {".", ".."}:
            raise ValueError("research artifact file_name is not safe")
        if len(self.file_name) > 240 or len(self.content_type) > 120:
            raise ValueError("research artifact file metadata exceeds its hard limit")
        if len(self.title) > 300 or len(self.url) > 4096:
            raise ValueError("research artifact title or URL exceeds its hard limit")
        if len(self.content) > 2_000_000 or len(self.summary_markdown) > 200_000:
            raise ValueError("research artifact content exceeds its hard limit")
        _require_bounded_mapping(
            self.metadata,
            label="research artifact metadata",
            max_chars=MAX_METADATA_CHARS,
        )
        if self.mode is ArtifactMode.INLINE_TEXT:
            if not self.content:
                raise ValueError("inline_text artifacts require content")
            if self.url:
                raise ValueError("inline_text artifacts must not contain a URL")
        elif self.mode is ArtifactMode.REMOTE:
            if not self.url.lower().startswith("https://"):
                raise ValueError("remote artifacts require an HTTPS URL")
            if self.content:
                raise ValueError("remote artifacts must not contain inline content")


@dataclass(frozen=True, slots=True)
class MaterializedResearchArtifact:
    """Artifact bytes after provider-side fetch and policy validation."""

    descriptor: ResearchArtifact
    content: bytes

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("materialized research artifact content is empty")


@dataclass(frozen=True, slots=True)
class PersistedResearchArtifact:
    source: str
    external_id: str
    path: str
    sha256: str
    nextcloud_file_id: str = ""


@runtime_checkable
class Resolver(Protocol):
    async def resolve(self, request: ResolutionRequest) -> ResolutionBatch: ...


@runtime_checkable
class SrcRetriever(Protocol):
    async def retrieve(self, request: SrcRetrievalRequest) -> RetrievalBatch: ...


@runtime_checkable
class ErgRetriever(Protocol):
    async def retrieve(self, request: ErgRetrievalRequest) -> RetrievalBatch: ...


@runtime_checkable
class ResearchArtifactSink(Protocol):
    async def persist(
        self,
        run: Any,
        artifact: MaterializedResearchArtifact,
    ) -> PersistedResearchArtifact: ...
