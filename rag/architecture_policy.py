"""SunaQ architecture-tier policy.

SRC (Secure RAG Core) is the deliberately narrow Nextcloud/Elasticsearch
boundary: mandatory live Nextcloud ACL, one retrieval round, no SunaQ-owned
persistent derived knowledge state and no external information acquisition.
ERG Internal adds SunaQ-owned persistent derived state generated exclusively
from the authoritative internal Nextcloud information space. ERG Full
additionally permits external information acquisition and research egress.
Model, reranker and embedding processing locality remains an orthogonal
administrator decision.

The architecture tier is independent from deployment packaging. Packaging may
select defaults, but runtime validation and request gates enforce the boundary.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from rag.retrieval_planner import load_retrieval_planner_settings


SRC = "src"
ERG_INTERNAL = "erg_internal"
ERG_FULL = "erg_full"

# Backwards-compatible symbol: historical "ERG" meant the full ERG envelope.
ERG = ERG_FULL

_TIER_ALIASES = {
    "erg": ERG_FULL,
    "erg-internal": ERG_INTERNAL,
    "erg-full": ERG_FULL,
}
VALID_TIERS = frozenset({SRC, ERG_INTERNAL, ERG_FULL})

LOCAL_RESOLVER_ADAPTERS = frozenset({"neo4j"})
EXTERNAL_RESOLVER_ADAPTERS = frozenset({"openplz", "openregister"})


SRC_ALLOWED_SOURCE_SCOPES = frozenset({"documents"})
SRC_ALLOWED_RETRIEVAL_ARMS = frozenset({"files"})

SRC_BLOCKED_API_PREFIXES = (
    "/web/",
    "/source-origin/register-chat",
    "/graph/",
    "/query-context",
)


def _get(cfg: Mapping[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def normalize_architecture_tier(value: Any) -> str:
    """Return the canonical architecture tier, preserving unknown values."""
    raw = str(value or ERG_FULL).strip().casefold()
    return _TIER_ALIASES.get(raw, raw)


def architecture_tier(cfg: Mapping[str, Any]) -> str:
    # Existing installations used "erg"; normalize that legacy value to the
    # new ERG Full envelope rather than silently reducing capabilities.
    return normalize_architecture_tier(_get(cfg, "architecture.tier", ERG_FULL))


def is_src(cfg: Mapping[str, Any]) -> bool:
    return architecture_tier(cfg) == SRC


def is_erg_internal(cfg: Mapping[str, Any]) -> bool:
    return architecture_tier(cfg) == ERG_INTERNAL


def is_erg_full(cfg: Mapping[str, Any]) -> bool:
    return architecture_tier(cfg) == ERG_FULL


def allows_external_research(cfg: Mapping[str, Any]) -> bool:
    """Whether public/external retrieval and resolver adapters are permitted."""
    return is_erg_full(cfg)


def allowed_resolver_sources(
    cfg: Mapping[str, Any],
    sources: Iterable[str] | None,
) -> tuple[str, ...]:
    requested = tuple(
        sorted(
            {
                str(value).strip().casefold()
                for value in (sources or ())
                if str(value).strip()
            }
        )
    )
    tier = architecture_tier(cfg)
    if tier == SRC:
        return ()
    if tier == ERG_INTERNAL:
        return tuple(
            value for value in requested if value in LOCAL_RESOLVER_ADAPTERS
        )
    return requested


def validate_architecture_config(cfg: Mapping[str, Any]) -> list[str]:
    tier = architecture_tier(cfg)
    if tier not in VALID_TIERS:
        return [
            "architecture.tier must be one of "
            f"{sorted(VALID_TIERS)!r} (legacy alias 'erg' is accepted), got {tier!r}"
        ]

    errors: list[str] = []
    if tier == ERG_FULL:
        return errors

    policy_web = str(
        _get(cfg, "retrieval_policy.web", "disabled") or ""
    ).strip().casefold()
    if tier == ERG_INTERNAL:
        if policy_web != "disabled":
            errors.append("ERG Internal requires retrieval_policy.web=disabled")
        if _truthy(_get(cfg, "mail.enabled", False), False):
            errors.append("ERG Internal requires mail.enabled=false")
        if _truthy(_get(cfg, "mail.worker.enabled", False), False):
            errors.append("ERG Internal requires mail.worker.enabled=false")
        if _truthy(_get(cfg, "delegated_research.enabled", False), False):
            errors.append("ERG Internal requires delegated_research.enabled=false")
        return errors

    # SRC: Elasticsearch/Nextcloud only; no SunaQ-owned secondary retrieval or
    # resolution state.
    if not _truthy(_get(cfg, "acl.enabled", False), False):
        errors.append("SRC requires acl.enabled=true")

    policy_files = str(
        _get(cfg, "retrieval_policy.internal.files", "required") or ""
    ).strip().casefold()
    policy_vector = str(
        _get(cfg, "retrieval_policy.internal.vector", "disabled") or ""
    ).strip().casefold()
    policy_graph = str(
        _get(cfg, "retrieval_policy.internal.graph", "disabled") or ""
    ).strip().casefold()
    if policy_files == "disabled":
        errors.append("SRC requires retrieval_policy.internal.files to remain enabled")
    if policy_vector != "disabled":
        errors.append("SRC requires retrieval_policy.internal.vector=disabled")
    if policy_graph != "disabled":
        errors.append("SRC requires retrieval_policy.internal.graph=disabled")
    if policy_web != "disabled":
        errors.append("SRC requires retrieval_policy.web=disabled")

    disabled_flags = (
        ("qdrant.enabled", True),
        ("neo4j.enabled", False),
        ("resolver.enabled", False),
        ("sync_worker.enabled", True),
        ("sync.graph_queue.enabled", False),
        ("graph_queue.enabled", True),
        ("graph_queue.auto_enqueue_cited_documents", False),
        ("graph_queue.worker.enabled", False),
        ("research_findings.enabled", True),
        ("graph_indexer.enabled", True),
        ("graph_entity_discovery.enabled", True),
        ("graph_relation_discovery.enabled", True),
        ("mail_metadata.enabled", True),
        ("mail.enabled", False),
        ("mail.worker.enabled", False),
        ("chat_archive.enabled", False),
    )
    for path, default in disabled_flags:
        if _truthy(_get(cfg, path, default), default):
            errors.append(f"SRC requires {path}=false")

    return errors


def request_capability_error(
    cfg: Mapping[str, Any],
    *,
    retrieval_arms: Iterable[str] | None = None,
    source_scopes: Iterable[str] | None = None,
    web_requested: bool = False,
) -> str | None:
    tier = architecture_tier(cfg)
    if tier == ERG_FULL:
        return None
    if tier == ERG_INTERNAL and web_requested:
        return (
            "Diese SunaQ-Installation läuft als ERG Internal; öffentliche "
            "Web-Recherche und andere externe Research-Arme erfordern ERG Full."
        )
    if tier == ERG_INTERNAL:
        return None

    requested_sources = {
        str(value).strip().casefold()
        for value in (source_scopes or ())
        if str(value).strip()
    }
    requested_arms = {
        str(value).strip().casefold()
        for value in (retrieval_arms or ())
        if str(value).strip()
    }

    denied_sources = sorted(requested_sources - SRC_ALLOWED_SOURCE_SCOPES)
    if denied_sources:
        return (
            "Diese SunaQ-Installation läuft als Secure RAG Core (SRC); "
            "nicht verfügbare Quellenbereiche: " + ", ".join(denied_sources) + "."
        )
    if web_requested:
        return (
            "Diese SunaQ-Installation läuft als Secure RAG Core (SRC); "
            "öffentliche Web-Recherche gehört zum Eboracum Research Gate (ERG)."
        )

    denied_arms = sorted(requested_arms - SRC_ALLOWED_RETRIEVAL_ARMS)
    if denied_arms:
        return (
            "Diese SunaQ-Installation läuft als Secure RAG Core (SRC); "
            "nicht verfügbare Retrieval-Arme: " + ", ".join(denied_arms) + "."
        )
    return None


def effective_retrieval_arms(
    cfg: Mapping[str, Any],
    retrieval_arms: Iterable[str] | None,
) -> list[str] | None:
    """Normalize omitted/empty arms at the SRC boundary.

    The search layer historically interprets omitted arms as "all available".
    SRC must never rely on downstream component state for that decision.
    """
    if is_src(cfg) and not list(retrieval_arms or ()):
        return ["files"]
    if retrieval_arms is None:
        return None
    return [str(value) for value in retrieval_arms]


def api_path_blocked(cfg: Mapping[str, Any], path: str) -> bool:
    normalized = "/" + str(path or "").lstrip("/")
    tier = architecture_tier(cfg)
    if tier == ERG_FULL:
        return False
    if tier == ERG_INTERNAL:
        return normalized == "/web" or normalized.startswith("/web/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in SRC_BLOCKED_API_PREFIXES
    )


def validate_role_backends(
    cfg: Mapping[str, Any],
    backends: Mapping[str, Any],
) -> list[str]:
    """Validate role routing against architecture-tier invariants.

    Architecture tiers constrain information sources, SunaQ-owned derived state
    and retrieval complexity. Processing locality is orthogonal: administrators
    may deliberately route planner/verifier/evidence/answer, reranker or
    embedding work to remote providers without changing the tier.
    """

    del cfg, backends
    return []


def validate_runtime_model(cfg: Mapping[str, Any], model: Any) -> list[str]:
    tier = architecture_tier(cfg)
    if tier == ERG_FULL:
        return []

    errors: list[str] = []
    if tier == ERG_INTERNAL:
        try:
            capabilities = dict(model.section("capabilities") or {})
        except Exception:
            capabilities = {}
        if capabilities.get("web") is True:
            errors.append(
                f"ERG Internal does not permit Web capability; model "
                f"{getattr(model, 'model_id', '<unknown>')!r} enables web"
            )
        try:
            research = dict(model.section("research") or {})
        except Exception:
            research = {}
        web_backend = str(research.get("web_backend") or "").strip().casefold()
        if web_backend and web_backend != "none":
            errors.append(
                f"ERG Internal does not permit external Web backends; model "
                f"{getattr(model, 'model_id', '<unknown>')!r} selects {web_backend!r}"
            )
        try:
            resolver_research = dict(model.section("resolver_research") or {})
        except Exception:
            resolver_research = {}
        resolver_sources = {
            str(value).strip().casefold()
            for value in (resolver_research.get("sources") or ())
            if str(value).strip()
        }
        external_sources = sorted(resolver_sources - LOCAL_RESOLVER_ADAPTERS)
        if external_sources:
            errors.append(
                f"ERG Internal permits only local resolver adapters; model "
                f"{getattr(model, 'model_id', '<unknown>')!r} requests "
                + ", ".join(external_sources)
            )
        return errors

    # SRC model constraints.
    try:
        model_config = getattr(model, "config", None)
        if isinstance(model_config, Mapping):
            effective_config = dict(model_config)
        else:
            effective_config = {
                "retrieval_planner": dict(model.section("retrieval_planner") or {})
            }
        rounds = int(
            load_retrieval_planner_settings(effective_config).max_retrieval_rounds
        )
    except (TypeError, ValueError, AttributeError, KeyError):
        rounds = 2
    if rounds > 1:
        errors.append(
            f"SRC permits at most one retrieval round; model "
            f"{getattr(model, 'model_id', '<unknown>')!r} requests {rounds}"
        )

    return errors
