"""SunaQ architecture-tier policy.

SRC (Secure RAG Core) is a deliberately narrow retrieval/persistence boundary:
Nextcloud/Elasticsearch documents, mandatory live Nextcloud ACL, at most one
retrieval round, and no archive/web/vector/document-graph side channels. LLM roles may be
local or remote according to administrator policy; remote evidence remains
subject to the provider's hard egress caps. ERG is the broader Research Gate
envelope.

The architecture tier is independent from deployment packaging. Packaging may
select defaults, but runtime validation and request gates enforce the boundary.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from rag.retrieval_planner import load_retrieval_planner_settings


SRC = "src"
ERG = "erg"
VALID_TIERS = frozenset({SRC, ERG})

SRC_ALLOWED_SOURCE_SCOPES = frozenset({"documents"})
SRC_ALLOWED_RETRIEVAL_ARMS = frozenset({"files"})

SRC_BLOCKED_API_PREFIXES = (
    "/web/",
    "/source-origin/register-chat",
    "/graph/document",
    "/graph/enqueue-evidence",
    "/graph/research-findings",
    "/graph/queue/",
    "/graph/index-evidence",
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


def architecture_tier(cfg: Mapping[str, Any]) -> str:
    # Existing installations predate this field and are ERG-capable, so ERG is
    # the compatibility default. Fresh core presets set SRC explicitly.
    return str(_get(cfg, "architecture.tier", ERG) or ERG).strip().casefold()


def is_src(cfg: Mapping[str, Any]) -> bool:
    return architecture_tier(cfg) == SRC


def validate_architecture_config(cfg: Mapping[str, Any]) -> list[str]:
    tier = architecture_tier(cfg)
    if tier not in VALID_TIERS:
        return [
            f"architecture.tier must be one of {sorted(VALID_TIERS)!r}, got {tier!r}"
        ]
    if tier != SRC:
        return []

    errors: list[str] = []
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
    policy_web = str(
        _get(cfg, "retrieval_policy.web", "disabled") or ""
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
    if not is_src(cfg):
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
    if not is_src(cfg):
        return False
    normalized = "/" + str(path or "").lstrip("/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in SRC_BLOCKED_API_PREFIXES
    )


def _remote_endpoint(url: str) -> bool:
    """Return True unless the endpoint resolves exclusively inside local/private space."""
    host = (urlparse(str(url or "")).hostname or "").strip().casefold()
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return True
        addresses = []
        for item in infos:
            try:
                addresses.append(ipaddress.ip_address(item[4][0]))
            except (ValueError, IndexError):
                return True
        if not addresses:
            return True

    return any(
        not (address.is_loopback or address.is_private or address.is_link_local)
        for address in addresses
    )


def validate_role_backends(
    cfg: Mapping[str, Any],
    backends: Mapping[str, Any],
) -> list[str]:
    """Validate role routing against architecture-tier invariants.

    SRC constrains retrieval sources, derived state and retrieval complexity, but
    it does not require zero model egress. Administrators may deliberately route
    planner/verifier/evidence/answer roles to remote providers; the provider's
    remote evidence caps remain the data-minimization boundary for those calls.
    """

    del cfg, backends
    return []


def validate_runtime_model(cfg: Mapping[str, Any], model: Any) -> list[str]:
    if not is_src(cfg):
        return []
    errors: list[str] = []
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

    try:
        reranker = dict(model.section("reranker") or {})
    except Exception:
        reranker = {}
    backend = str(reranker.get("backend") or "none").strip().casefold()
    if backend == "tei":
        tei_url = str(reranker.get("tei_url") or "").strip()
        if not tei_url or _remote_endpoint(tei_url):
            errors.append(
                f"SRC requires a local reranker; model "
                f"{getattr(model, 'model_id', '<unknown>')!r} uses TEI endpoint "
                f"{tei_url or '<unset>'!r}"
            )
    return errors
