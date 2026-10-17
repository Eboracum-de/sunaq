import sys
import time
import builtins
from contextlib import contextmanager
from contextvars import ContextVar
import re
import json
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

import httpx
import yaml

from rag.planner import create_plan, plan_from_search_spec
from rag.embeddings import build_embedding_backend_from_config
from rag.elastic_query import nextcloud_query_tokens
from rag.elasticsearch_client import httpx_options as elastic_httpx_options
from rag.graph import GraphStore
from rag.ontology import load_relation_ontology, relation_names_with_role
from rag.graph_entities import detect_known_entities, elastic_entity_phrases
from rag.vector import VectorStore
from rag.reranker import rerank_results
from rag.source_origin import (
    chat_archive_roots,
    internal_exclude_paths,
    mail_archive_roots,
    normalize_source_scopes,
    source_scope_allows_path,
    source_scope_allows_record,
    source_origins_for_scopes,
    web_archive_roots,
    maybe_register_path_origin,
)
from rag.source_registry import auto_mirror_registry_to_elasticsearch, SPECIAL_ORIGINS
from rag.logging_utils import get_logger
from rag.query_specificity import is_broad_entity_query
from rag.retrieval_planner import extract_safe_hard_constraints
from rag.tls_compat import configure_tls_compat
from rag.nextcloud_links import NextcloudVersionProbe, build_nextcloud_source_url
from rag.retrieval_signal import (
    analyze_score_curve,
    choose_retrieval_strategy,
    overlap_at_k,
)


log = get_logger("api")


def _module_print(*args, **kwargs):
    """Historic diagnostics become DEBUG when imported by the API.

    Keep human CLI output when search.py is executed directly.
    """
    if __name__ == "__main__":
        return builtins.print(*args, **kwargs)
    text = " ".join(str(x) for x in args)
    if text.strip():
        log.debug(text)


print = _module_print

# ------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

with (BASE_DIR / "config.yaml").open("r") as f:
    config = yaml.safe_load(f)
configure_tls_compat(config)


ELASTICSEARCH_ENABLED = bool((config.get("elasticsearch") or {}).get("enabled", True))
ES_URL = (
    config["elasticsearch"]["url"]
    .rstrip("/")
)

ES_INDEX = (
    config["elasticsearch"]["index"]
)

ES_TIMEOUT = (
    config["elasticsearch"]
    .get("timeout", 60)
)
ES_HTTPX_OPTIONS = elastic_httpx_options(config)

def _es_post(url: str, **kwargs):
    return httpx.post(url, **ES_HTTPX_OPTIONS, **kwargs)


EMBEDDING_CONFIG = config.get("embedding", {}) or {}
EMBEDDING_BACKEND = str(EMBEDDING_CONFIG.get("backend", "ollama")).strip().lower()
EMBEDDING_URL = str(
    EMBEDDING_CONFIG.get("url")
    or (config.get("ollama", {}) or {}).get("url")
    or "http://127.0.0.1:11434"
).rstrip("/")
EMBEDDING_MODEL = str(
    EMBEDDING_CONFIG.get("model")
    or (config.get("ollama", {}) or {}).get("embedding_model")
    or "qwen3-embedding:4b"
)


QDRANT_ENABLED = bool((config.get("qdrant") or {}).get("enabled", True))
NEO4J_ENABLED = bool((config.get("neo4j") or {}).get("enabled", True))
RERANKER_CONFIG = config.get("reranker", {}) or {}
RERANKER_ENABLED = str(RERANKER_CONFIG.get("backend", "none") or "none").strip().lower() not in {"none", "off", "disabled"}
QDRANT_URL = (
    config["qdrant"]["url"]
)

QDRANT_COLLECTION = (
    config["qdrant"]["collection"]
)

NEXTCLOUD_BASE_URL = (
    str(
        config.get("nextcloud", {})
        .get("base_url", "")
    )
    .strip()
    .rstrip("/")
)

_NEXTCLOUD_VERSION_PROBE = NextcloudVersionProbe(NEXTCLOUD_BASE_URL, config)

# Public web snapshots remain in Nextcloud for provenance, but by default they
# are not candidates in the ordinary internal evidence arms.
INTERNAL_EXCLUDE_PATHS = internal_exclude_paths()


def _es_root_clause(root: str) -> dict:
    clean_root = str(root or "").strip(" /")
    return {
        "bool": {
            "should": [
                {"prefix": {"title.keyword": clean_root + "/"}},
                {"match_phrase": {"title": clean_root}},
            ],
            "minimum_should_match": 1,
        }
    }


def _es_origin_clause(origins) -> dict:
    values = [str(value or "").strip() for value in origins if str(value or "").strip()]
    if len(values) == 1:
        clauses = [
            {"term": {"source_origin.keyword": values[0]}},
            {"term": {"source_origin": values[0]}},
        ]
    else:
        clauses = [
            {"terms": {"source_origin.keyword": values}},
            {"terms": {"source_origin": values}},
        ]
    return {"bool": {"should": clauses, "minimum_should_match": 1}}


def _es_special_scope_clause(scope: str, roots_by_scope: dict[str, tuple[str, ...]]) -> dict:
    origins_by_scope = {
        "mailarchive": ("mail_archive",),
        # Legacy web_archive records are now part of the Chats source class.
        "chatarchive": ("chat_archive", "web_archive"),
    }
    should = [_es_origin_clause(origins_by_scope[scope])]
    # Path matching is deliberately only a recovery/legacy fallback. Native
    # source_origin is the normal retrieval filter; roots keep scopes usable
    # after an ES rebuild until the admin runs source-origin reconcile.
    should.extend(_es_root_clause(root) for root in roots_by_scope.get(scope, ()) if str(root or "").strip(" /"))
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _apply_source_scope_to_es(bool_query: dict, source_scopes=None) -> set[str] | None:
    """Apply source scopes before the Elasticsearch candidate window.

    ``source_origin`` is mirrored into ES from the middleware registry. Archive
    roots are retained only as a graceful-degradation fallback for fresh/rebuilt
    indexes whose mirror has not yet been reconciled.
    """
    scopes = normalize_source_scopes(source_scopes)
    # Keep the ES mirror of the middleware-owned archive registry current before
    # candidate limits are applied.  This is a lightweight id-based repair, not
    # the full admin reconcile, and is throttled internally.
    try:
        auto_mirror_registry_to_elasticsearch()
    except Exception as exc:
        log.warning("source_origin auto-mirror failed; continuing with path fallback: %s", exc)
    roots_by_scope = {
        "mailarchive": tuple(mail_archive_roots()),
        "chatarchive": tuple(sorted(
            set(chat_archive_roots()) | set(web_archive_roots()),
            key=str.casefold,
        )),
    }

    if scopes is None:
        # Client-neutral implicit default: ordinary documents only.
        for scope in ("mailarchive", "chatarchive"):
            bool_query["must_not"].append(_es_special_scope_clause(scope, roots_by_scope))
        return None

    selected_special = {scope for scope in scopes if scope != "documents"}
    all_special = {"mailarchive", "chatarchive"}

    if "documents" in scopes:
        # Ordinary documents are the complement of special origins.  Exclude
        # only archive classes not selected alongside /documents.
        for scope in sorted(all_special - selected_special):
            bool_query["must_not"].append(_es_special_scope_clause(scope, roots_by_scope))
    else:
        # Archive-only searches must match one of the selected special origins.
        # The native field remains useful even when no root is currently known.
        include = [_es_special_scope_clause(scope, roots_by_scope) for scope in sorted(selected_special)]
        if include:
            bool_query["filter"].append({"bool": {"should": include, "minimum_should_match": 1}})
        else:
            bool_query["filter"].append({"term": {"_id": "__rag_no_selected_source_scope__"}})
    return scopes


def _es_acl_identity_clauses(field: str, value: str) -> list[dict]:
    """Mapping-tolerant ACL identity clauses.

    False positives are acceptable because live WebDAV ACL remains mandatory.
    The prefilter must never be treated as authorization.
    """
    clean = str(value or "").strip()
    if not clean:
        return []
    return [
        {"term": {field: clean}},
        {"term": {field + ".keyword": clean}},
        {"match": {field: clean}},
    ]


def _apply_acl_prefilter_to_es(
    bool_query: dict,
    *,
    user: str | None,
    groups: list[str] | tuple[str, ...] | None,
) -> bool:
    """Prefilter by Nextcloud owner/direct-user/group metadata.

    groups=None means group context is unavailable, so the request deliberately
    falls back to the unfiltered retrieval path. An empty list is valid known
    context for a user with no groups.
    """
    username = str(user or "").strip()
    if not username or groups is None:
        return False

    should: list[dict] = []
    should.extend(_es_acl_identity_clauses("owner", username))
    should.extend(_es_acl_identity_clauses("users", username))
    for group in groups:
        should.extend(_es_acl_identity_clauses("groups", str(group or "").strip()))

    if not should:
        return False
    bool_query.setdefault("filter", []).append({
        "bool": {
            "should": should,
            "minimum_should_match": 1,
        }
    })
    return True

def _heal_source_origin_from_hit(document_id: str, title: str, indexed_origin: str | None) -> str | None:
    """Register archive origin discovered through a legacy/path fallback.

    The current request can continue using the fallback.  The registry stamp
    change causes the next scoped ES query to mirror the durable classification
    into Elasticsearch before retrieval.
    """
    origin = str(indexed_origin or "").strip()
    if origin in SPECIAL_ORIGINS:
        return origin
    try:
        inferred = maybe_register_path_origin(
            str(document_id or ""), str(title or ""),
            classification_source="search_path_fallback",
        )
        return inferred if inferred in SPECIAL_ORIGINS else origin or None
    except Exception as exc:
        log.debug("source_origin path self-heal skipped for %s: %s", document_id, exc)
        return origin or None


# ------------------------------------------------------------
# Entity Resolution / Neo4j query preparation
#
# Fail-open by design: if Neo4j is unavailable, the historic ES/Qdrant
# pipeline continues unchanged. Neo4j first prepares query entities and ES
# phrase expansions; v0.5.5 additionally uses already-graphified documents as
# a conservative third retrieval arm.
# ------------------------------------------------------------

ENTITY_CONFIG = config.get("entity_resolution", {}) or {}
ENTITY_RESOLUTION_ENABLED = NEO4J_ENABLED and bool(
    ENTITY_CONFIG.get("enabled", bool(config.get("neo4j")))
)
ENTITY_FAIL_OPEN = bool(ENTITY_CONFIG.get("fail_open", True))
ENTITY_FUZZY_ENABLED = bool(ENTITY_CONFIG.get("fuzzy", True))
ENTITY_FUZZY_THRESHOLD = float(ENTITY_CONFIG.get("fuzzy_threshold", 86.0))
ENTITY_FUZZY_MAX_CANDIDATES = int(ENTITY_CONFIG.get("fuzzy_max_candidates", 5))
ENTITY_FUZZY_EXPANSION_MIN_SIMILARITY = float(
    ENTITY_CONFIG.get("fuzzy_expansion_min_similarity", 90.0)
)
ENTITY_RETRY_SECONDS = float(ENTITY_CONFIG.get("retry_seconds", 30.0))

# ------------------------------------------------------------
# Graph Retrieval v1
#
# Neo4j is a third *document retrieval* arm. It contributes only documents
# already graphified by the evidence indexer. Graph structure itself is not
# passed as answer evidence.
# ------------------------------------------------------------

GRAPH_RETRIEVAL_CONFIG = config.get("graph_retrieval", {}) or {}
GRAPH_RETRIEVAL_ENABLED = NEO4J_ENABLED and bool(GRAPH_RETRIEVAL_CONFIG.get("enabled", True))
GRAPH_RETRIEVAL_LIMIT = int(GRAPH_RETRIEVAL_CONFIG.get("limit", 30) or 30)
GRAPH_RETRIEVAL_PAIR_WEIGHT = float(GRAPH_RETRIEVAL_CONFIG.get("pair_weight", 0.65) or 0.65)
GRAPH_RETRIEVAL_RELATION_WEIGHT = float(GRAPH_RETRIEVAL_CONFIG.get("relation_weight", 0.90) or 0.90)
GRAPH_RETRIEVAL_DIRECT_WEIGHT = float(GRAPH_RETRIEVAL_CONFIG.get("direct_relation_weight", 0.75) or 0.75)
GRAPH_RETRIEVAL_SINGLE_WEIGHT = float(GRAPH_RETRIEVAL_CONFIG.get("single_entity_weight", 0.30) or 0.30)
GRAPH_RETRIEVAL_HYDRATE_LIMIT = int(GRAPH_RETRIEVAL_CONFIG.get("hydrate_limit", 15) or 15)
GRAPH_RETRIEVAL_SNIPPET_CHARS = int(GRAPH_RETRIEVAL_CONFIG.get("snippet_chars", 2400) or 2400)
GRAPH_RETRIEVAL_SNIPPET_BEFORE_CHARS = int(
    GRAPH_RETRIEVAL_CONFIG.get("snippet_before_chars", 350) or 350
)
GRAPH_RETRIEVAL_SNIPPET_AFTER_CHARS = int(
    GRAPH_RETRIEVAL_CONFIG.get("snippet_after_chars", 1450) or 1450
)
GRAPH_RETRIEVAL_RELATIONS = relation_names_with_role(
    load_relation_ontology(),
    "retrieval_bridge",
)

_graph_store: GraphStore | None = None
_graph_store_failed_at: float | None = None


def _get_graph_store() -> GraphStore | None:
    global _graph_store, _graph_store_failed_at

    if not _entity_resolution_enabled():
        return None
    if _graph_store is not None:
        return _graph_store
    fail_open = _entity_fail_open()
    retry_seconds = _entity_retry_seconds()
    if (
        _graph_store_failed_at is not None
        and fail_open
        and (time.monotonic() - _graph_store_failed_at) < retry_seconds
    ):
        return None

    try:
        graph = GraphStore.from_config(config)
        graph.verify_connectivity()
        # Lightweight, idempotent compatibility migration for older CardDAV
        # seeds.  This prevents Neo4j's UnknownPropertyKeyWarning for
        # Entity.entity_kind before the first explicit contact sync/schema init
        # after an upgrade.
        graph.ensure_entity_kind_schema()
        _graph_store = graph
        _graph_store_failed_at = None
        return graph
    except Exception as exc:
        _graph_store_failed_at = time.monotonic()
        print(
            f"[RAG] WARNUNG Entity Resolution/Neo4j: {type(exc).__name__}: {exc}",
            flush=True,
        )
        if fail_open:
            return None
        raise


def close_graph_store() -> None:
    global _graph_store
    if _graph_store is not None:
        try:
            _graph_store.close()
        finally:
            _graph_store = None


def prepare_entity_context(question: str) -> dict:
    """Detect/resolve query entities before the planner.

    The original query is never rewritten.  Resolved entities contribute
    weighted phrase expansions to Elasticsearch; fuzzy candidates only add a
    conservative canonical phrase when string similarity is sufficiently high.
    """
    if not _entity_resolution_enabled():
        return {
            "enabled": False,
            "entities": [],
            "elastic_phrase_expansion": [],
            "error": None,
        }

    graph = _get_graph_store()
    if graph is None:
        return {
            "enabled": True,
            "entities": [],
            "elastic_phrase_expansion": [],
            "error": "neo4j_unavailable",
        }

    try:
        entity_query = detect_known_entities(
            question,
            graph,
            fuzzy=_entity_fuzzy_enabled(),
            fuzzy_threshold=_entity_fuzzy_threshold(),
            fuzzy_max_candidates=_entity_fuzzy_max_candidates(),
        )
        payload = entity_query.to_dict()
        expansions = elastic_entity_phrases(entity_query)

        # v5 intentionally used a higher threshold for fuzzy expansion.
        # At integration time we allow a configurable lower threshold, while
        # still exporting only the matched known form, never candidate-specific
        # aliases that could silently disambiguate an ambiguous person.
        existing = {
            (str(item.get("entity_id") or ""), str(item.get("value") or "").casefold())
            for item in expansions
        }
        for entity in entity_query.fuzzy_entities:
            if not entity.candidates:
                continue
            best = entity.candidates[0]
            if best.similarity < _entity_fuzzy_expansion_min_similarity():
                continue
            value = str(best.form_value or "").strip()
            if not value:
                continue
            key = (best.entity_id, value.casefold())
            if key in existing:
                continue
            existing.add(key)
            expansions.append({
                "entity_id": best.entity_id,
                "entity_type": best.entity_type,
                "value": value,
                "weight": round(min(0.76, best.candidate_score * 0.82), 4),
                "kind": "fuzzy_candidate",
                "active": True,
                "source": "fuzzy",
                "similarity": best.similarity,
                "original_mention": entity.mention,
                "resolution": "fuzzy_candidate",
            })

        payload.update({
            "enabled": True,
            "elastic_phrase_expansion": expansions,
            "error": None,
        })
        return payload

    except Exception as exc:
        print(
            f"[RAG] WARNUNG Entity-Vorbereitung: {type(exc).__name__}: {exc}",
            flush=True,
        )
        if not _entity_fail_open():
            raise
        return {
            "enabled": True,
            "entities": [],
            "elastic_phrase_expansion": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _resolved_query_entity_ids(entity_context: dict) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for entity in entity_context.get("entities") or []:
        if str(entity.get("status") or "") != "resolved":
            continue
        entity_id = str(entity.get("entity_id") or "").strip()
        if entity_id and entity_id not in seen:
            seen.add(entity_id)
            ids.append(entity_id)
    return ids




def graph_search(entity_context: dict, diagnostics: dict | None = None, source_scopes=None) -> list[dict]:
    """Retrieve already-graphified documents for resolved query entities."""
    graph_enabled = _graph_retrieval_enabled()
    graph_limit = _graph_retrieval_limit()
    snippet_chars = _graph_retrieval_snippet_chars()
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({
            "enabled": graph_enabled,
            "available": False,
            "mode": "disabled" if not graph_enabled else "no_entities",
            "resolved_entity_ids": [],
            "direct_relations": [],
            "indirect_relation_chains": [],
            "count": 0,
        })

    if not graph_enabled:
        return []

    entity_ids = _resolved_query_entity_ids(entity_context)
    if diagnostics is not None:
        diagnostics["resolved_entity_ids"] = entity_ids
    if not entity_ids:
        return []

    graph = _get_graph_store()
    if graph is None:
        if diagnostics is not None:
            diagnostics["mode"] = "neo4j_unavailable"
        return []

    try:
        payload = graph.retrieve_documents_for_entities(
            entity_ids,
            limit=graph_limit * (3 if INTERNAL_EXCLUDE_PATHS else 1),
            structural_relations=GRAPH_RETRIEVAL_RELATIONS,
        )
    except Exception as exc:
        print(
            f"[RAG] WARNUNG Graph Retrieval: {type(exc).__name__}: {exc}",
            flush=True,
        )
        if diagnostics is not None:
            diagnostics.update({
                "mode": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
        return []

    mode = str(payload.get("mode") or "none")
    relations = list(payload.get("direct_relations") or [])
    indirect_chains = list(payload.get("indirect_relation_chains") or [])
    results: list[dict] = []
    for rank, raw in enumerate(payload.get("documents") or [], start=1):
        item = dict(raw)
        document_id = str(item.get("document_id") or "").strip()
        if not document_id:
            continue
        title = str(item.get("title") or item.get("path") or "").strip()
        meta = path_metadata(title) if title else {"path": "", "directory": "/", "filename": ""}
        path_value = str(item.get("path") or meta["path"] or "")
        if not source_scope_allows_record(document_id, path_value or title, source_scopes, item.get("source_origin")):
            continue
        if path_value:
            path_meta = path_metadata(path_value)
        else:
            path_meta = meta

        relation_evidence = [
            str(x or "").strip()
            for x in item.get("graph_relation_evidence") or []
            if str(x or "").strip()
        ]
        item.update({
            "document_id": document_id,
            "title": title or path_value,
            "path": path_value or path_meta["path"],
            "directory": path_meta["directory"],
            "filename": path_meta["filename"],
            "nextcloud_openfile_id": openfile_id(document_id),
            "rank": int(item.get("rank") or rank),
            "score": float(item.get("graph_score") or 0.0),
            "snippet": "",
            "graph_snippet": "\n\n".join(relation_evidence)[:snippet_chars],
            "graph_mode": mode,
            "graph_direct_relations": relations,
            "graph_indirect_chains": list(item.get("graph_indirect_chains") or indirect_chains),
        })
        if not item.get("source_url"):
            item["source_url"] = nextcloud_source_url(
                item.get("directory") or "/",
                item.get("nextcloud_openfile_id"),
            )
        results.append(item)
        if len(results) >= graph_limit:
            break

    if diagnostics is not None:
        diagnostics.update({
            "available": bool(results or relations),
            "mode": mode,
            "direct_relations": relations,
            "indirect_relation_chains": indirect_chains,
            "count": len(results),
        })
    return results


def graph_retrieval_weight(diagnostics: dict, results: list[dict]) -> float:
    if not results:
        return 0.0
    mode = str(diagnostics.get("mode") or "")
    if mode == "explicit_relation_observation":
        return _graph_retrieval_relation_weight()
    if mode == "direct_relation_context":
        return _graph_retrieval_direct_weight()
    if mode == "all_query_entities":
        # A direct seed relation plus co-mention documents is the strongest v1
        # structural signal; still leave the reranker/evidence controller in
        # charge of the final answer.
        if diagnostics.get("direct_relations"):
            return max(_graph_retrieval_pair_weight(), _graph_retrieval_direct_weight())
        return _graph_retrieval_pair_weight()
    if mode == "indirect_relation_chain":
        # Two independently document-grounded hops are useful orientation, but
        # intentionally weaker than direct pair evidence.
        return min(_graph_retrieval_pair_weight(), 0.55)
    if mode == "single_entity_documents":
        return _graph_retrieval_single_weight()
    return 0.0


# ------------------------------------------------------------
# Clients
# ------------------------------------------------------------

embeddings = build_embedding_backend_from_config(config)

store = None
if QDRANT_ENABLED:
    try:
        store = VectorStore(QDRANT_URL, QDRANT_COLLECTION)
    except RuntimeError as exc:
        # Keep lexical/graph retrieval available when the optional Qdrant
        # client is absent; the vector arm reports degraded only if requested.
        print(f"[RAG] WARNUNG Vector-Backend nicht initialisiert: {exc}", flush=True)


# ------------------------------------------------------------
# Suchparameter
#
# Alle Werte sind optional über config.yaml überschreibbar. Ohne einen
# search:-Block bleibt das bisherige Verhalten exakt erhalten.
#
# search:
#   es_limit: 50
#   vector_limit: 80
#   vector_threshold: 0.55
#   rrf_k: 60
#   rerank_candidates: 10
#   final_limit: 8
#   rerank_min_score: null
# ------------------------------------------------------------

SEARCH_CONFIG = config.get("search", {}) or {}


def _search_int(name: str, default: int, minimum: int = 1) -> int:
    value = SEARCH_CONFIG.get(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Ungültiger search.{name}-Wert: {value!r}; erwartet Ganzzahl"
        ) from exc
    if parsed < minimum:
        raise RuntimeError(
            f"Ungültiger search.{name}-Wert: {parsed}; Minimum ist {minimum}"
        )
    return parsed


def _search_float(name: str, default: float) -> float:
    value = SEARCH_CONFIG.get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Ungültiger search.{name}-Wert: {value!r}; erwartet Zahl"
        ) from exc


def _search_optional_float(
    name: str,
    default: float | None = None,
) -> float | None:
    value = SEARCH_CONFIG.get(name, default)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Ungültiger search.{name}-Wert: {value!r}; erwartet Zahl oder null"
        ) from exc


ES_LIMIT = _search_int("es_limit", 50)
VECTOR_LIMIT = _search_int("vector_limit", 80)
VECTOR_THRESHOLD = _search_float("vector_threshold", 0.55)
RRF_K = _search_int("rrf_k", 60)
RERANK_CANDIDATES = _search_int("rerank_candidates", 10)
FINAL_LIMIT = _search_int("final_limit", 8)
RERANK_MIN_SCORE = _search_optional_float("rerank_min_score", None)

print(
    "[RAG] Suchparameter: "
    f"ES_LIMIT={ES_LIMIT}, "
    f"VECTOR_LIMIT={VECTOR_LIMIT}, "
    f"VECTOR_THRESHOLD={VECTOR_THRESHOLD}, "
    f"RRF_K={RRF_K}, "
    f"RERANK_CANDIDATES={RERANK_CANDIDATES}, "
    f"FINAL_LIMIT={FINAL_LIMIT}, "
    f"RERANK_MIN_SCORE={RERANK_MIN_SCORE}",
    flush=True,
)


# ------------------------------------------------------------
# Retrieval-Signal / dynamische Armgewichtung
#
# Harte Grenzwerte dienen nur als Fangnetz für breite, flache
# Trefferfelder. Die eigentliche Entscheidung ES/Vector/Fusion
# wird aus der Form der jeweils deduplizierten Scorekurve abgeleitet.
# ------------------------------------------------------------

RETRIEVAL_SIGNAL_CONFIG = config.get("retrieval_signal", {}) or {}
RETRIEVAL_SIGNAL_ENABLED = bool(
    RETRIEVAL_SIGNAL_CONFIG.get("enabled", True)
)
RETRIEVAL_SIGNAL_REJECT_UNSPECIFIC = bool(
    RETRIEVAL_SIGNAL_CONFIG.get("reject_unspecific", True)
)
RETRIEVAL_SIGNAL_HEAD_SIZE = int(
    RETRIEVAL_SIGNAL_CONFIG.get("head_size", 3)
)
RETRIEVAL_SIGNAL_TAIL_FRACTION = float(
    RETRIEVAL_SIGNAL_CONFIG.get("tail_fraction", 0.33)
)
RETRIEVAL_SIGNAL_MIN_CURVE_CANDIDATES = int(
    RETRIEVAL_SIGNAL_CONFIG.get("min_curve_candidates", 8)
)
RETRIEVAL_SIGNAL_SMALL_FIELD = float(
    RETRIEVAL_SIGNAL_CONFIG.get("small_field_signal", 0.35)
)
RETRIEVAL_SIGNAL_EXPLICIT_ANCHOR = float(
    RETRIEVAL_SIGNAL_CONFIG.get("explicit_anchor_signal", 0.45)
)
RETRIEVAL_SIGNAL_FLOOR = float(
    RETRIEVAL_SIGNAL_CONFIG.get("unspecific_signal_floor", 0.08)
)
RETRIEVAL_SIGNAL_DOMINANCE_RATIO = float(
    RETRIEVAL_SIGNAL_CONFIG.get("dominance_ratio", 1.50)
)
RETRIEVAL_SIGNAL_DOMINANCE_MARGIN = float(
    RETRIEVAL_SIGNAL_CONFIG.get("dominance_margin", 0.10)
)
RETRIEVAL_SIGNAL_SECONDARY_WEIGHT_FLOOR = float(
    RETRIEVAL_SIGNAL_CONFIG.get("secondary_weight_floor", 0.20)
)
RETRIEVAL_SIGNAL_OVERLAP_K = int(
    RETRIEVAL_SIGNAL_CONFIG.get("overlap_k", 10)
)

UNSPECIFIC_RETRIEVAL_MESSAGE = (
    "Die Suche liefert ein zu unspezifisches Trefferbild, aus dem sich "
    "keine eindeutige dokumentengestützte Antwort ableiten lässt. "
    "Bitte spezifizieren Sie die Anfrage."
)


# ------------------------------------------------------------
# Kontext-Anreicherung nach dem Reranking
#
# Die breite Suche arbeitet weiterhin nur mit ES-Highlights und
# Qdrant-Chunks. Erst für die finalen Treffer wird der extrahierte
# Elasticsearch-Volltext per _mget nachgeladen und zu einem kompakten
# Antwortkontext verdichtet. Fehler in dieser Komfortstufe sind fail-open:
# die Suche bleibt mit den vorhandenen Snippets funktionsfähig.
# ------------------------------------------------------------

CONTEXT_ENRICH_CONFIG = config.get("context_enrichment", {})

CONTEXT_ENRICH_ENABLED = bool(
    CONTEXT_ENRICH_CONFIG.get("enabled", True)
)

CONTEXT_ENRICH_HEAD_CHARS = int(
    CONTEXT_ENRICH_CONFIG.get("head_chars", 2200)
)

CONTEXT_ENRICH_WINDOW_CHARS = int(
    CONTEXT_ENRICH_CONFIG.get("window_chars", 1800)
)

CONTEXT_ENRICH_MAX_WINDOWS = int(
    CONTEXT_ENRICH_CONFIG.get("max_windows", 2)
)

CONTEXT_ENRICH_MAX_CHARS = int(
    CONTEXT_ENRICH_CONFIG.get("max_chars", 7000)
)

DOCUMENT_USE_CONFIG = config.get("document_use", {}) or {}
DOCUMENT_USE_MAX_CHARS = int(
    DOCUMENT_USE_CONFIG.get("max_chars", 24000)
)


# ------------------------------------------------------------
# Dubletten-Gruppierung vor dem Reranker
#
# Ziel:
# - ODT/PDF-Fassungen desselben Dokuments sollen nur einen
#   Reranker-Slot belegen.
# - Die Dateien selbst bleiben erhalten; weitere Fassungen
#   werden als duplicate_variants am Repräsentanten mitgeführt.
#
# Alle Werte sind optional über config.yaml überschreibbar:
#
# dedup:
#   enabled: true
#   near_text_ratio: 0.96
#   min_text_chars: 500
#   min_length_ratio: 0.80
# ------------------------------------------------------------

DEDUP_CONFIG = config.get("dedup", {})

DEDUP_ENABLED = bool(
    DEDUP_CONFIG.get("enabled", True)
)

DEDUP_NEAR_TEXT_RATIO = float(
    DEDUP_CONFIG.get("near_text_ratio", 0.96)
)

DEDUP_MIN_TEXT_CHARS = int(
    DEDUP_CONFIG.get("min_text_chars", 500)
)

DEDUP_MIN_LENGTH_RATIO = float(
    DEDUP_CONFIG.get("min_length_ratio", 0.80)
)

DEDUP_EXACT_TEXT_MIN_CHARS = int(
    DEDUP_CONFIG.get("exact_text_min_chars", 120)
)

DEDUP_VARIANT_EXTENSIONS = {
    ".odt",
    ".doc",
    ".docx",
    ".rtf",
    ".txt",
    ".html",
    ".htm",
    ".md",
    ".pdf",
}


# ------------------------------------------------------------
# Request-local SunaQ model configuration
# ------------------------------------------------------------

_ACTIVE_RUNTIME_CONFIG: ContextVar[dict | None] = ContextVar(
    "sunaq_search_runtime_config", default=None
)


@contextmanager
def use_runtime_model_config(runtime_config: dict | None):
    """Apply one prevalidated SunaQ model config to the current request only."""
    token = _ACTIVE_RUNTIME_CONFIG.set(runtime_config)
    try:
        yield
    finally:
        _ACTIVE_RUNTIME_CONFIG.reset(token)


def _runtime_section(name: str, fallback: dict) -> dict:
    active = _ACTIVE_RUNTIME_CONFIG.get()
    if active is None:
        return fallback
    value = active.get(name)
    return value if isinstance(value, dict) else fallback


def _runtime_value(section: str, key: str, fallback: object):
    default_section = {
        "search": SEARCH_CONFIG,
        "retrieval_signal": RETRIEVAL_SIGNAL_CONFIG,
        "context_enrichment": CONTEXT_ENRICH_CONFIG,
        "graph_retrieval": GRAPH_RETRIEVAL_CONFIG,
        "entity_resolution": ENTITY_CONFIG,
        "reranker": RERANKER_CONFIG,
    }[section]
    return _runtime_section(section, default_section).get(key, fallback)


def _es_limit() -> int:
    return int(_runtime_value("search", "es_limit", ES_LIMIT))


def _vector_limit() -> int:
    return int(_runtime_value("search", "vector_limit", VECTOR_LIMIT))


def _vector_threshold() -> float:
    return float(_runtime_value("search", "vector_threshold", VECTOR_THRESHOLD))


def _rrf_k() -> int:
    return int(_runtime_value("search", "rrf_k", RRF_K))


def _rerank_candidates() -> int:
    return int(_runtime_value("search", "rerank_candidates", RERANK_CANDIDATES))


def _rerank_min_score() -> float | None:
    value = _runtime_value("search", "rerank_min_score", RERANK_MIN_SCORE)
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "none", "null"}):
        return None
    return float(value)


def _retrieval_signal_enabled() -> bool:
    return bool(_runtime_value("retrieval_signal", "enabled", RETRIEVAL_SIGNAL_ENABLED))


def _retrieval_signal_reject_unspecific() -> bool:
    return bool(_runtime_value("retrieval_signal", "reject_unspecific", RETRIEVAL_SIGNAL_REJECT_UNSPECIFIC))


def _retrieval_signal_head_size() -> int:
    return int(_runtime_value("retrieval_signal", "head_size", RETRIEVAL_SIGNAL_HEAD_SIZE))


def _retrieval_signal_tail_fraction() -> float:
    return float(_runtime_value("retrieval_signal", "tail_fraction", RETRIEVAL_SIGNAL_TAIL_FRACTION))


def _retrieval_signal_min_curve_candidates() -> int:
    return int(_runtime_value("retrieval_signal", "min_curve_candidates", RETRIEVAL_SIGNAL_MIN_CURVE_CANDIDATES))


def _retrieval_signal_small_field() -> float:
    return float(_runtime_value("retrieval_signal", "small_field_signal", RETRIEVAL_SIGNAL_SMALL_FIELD))


def _retrieval_signal_explicit_anchor() -> float:
    return float(_runtime_value("retrieval_signal", "explicit_anchor_signal", RETRIEVAL_SIGNAL_EXPLICIT_ANCHOR))


def _retrieval_signal_floor() -> float:
    return float(_runtime_value("retrieval_signal", "unspecific_signal_floor", RETRIEVAL_SIGNAL_FLOOR))


def _retrieval_signal_dominance_ratio() -> float:
    return float(_runtime_value("retrieval_signal", "dominance_ratio", RETRIEVAL_SIGNAL_DOMINANCE_RATIO))


def _retrieval_signal_dominance_margin() -> float:
    return float(_runtime_value("retrieval_signal", "dominance_margin", RETRIEVAL_SIGNAL_DOMINANCE_MARGIN))


def _retrieval_signal_secondary_weight_floor() -> float:
    return float(_runtime_value("retrieval_signal", "secondary_weight_floor", RETRIEVAL_SIGNAL_SECONDARY_WEIGHT_FLOOR))


def _retrieval_signal_overlap_k() -> int:
    return int(_runtime_value("retrieval_signal", "overlap_k", RETRIEVAL_SIGNAL_OVERLAP_K))


def _context_enrich_enabled() -> bool:
    return bool(_runtime_value("context_enrichment", "enabled", CONTEXT_ENRICH_ENABLED))


def _context_enrich_head_chars() -> int:
    return int(_runtime_value("context_enrichment", "head_chars", CONTEXT_ENRICH_HEAD_CHARS))


def _context_enrich_window_chars() -> int:
    return int(_runtime_value("context_enrichment", "window_chars", CONTEXT_ENRICH_WINDOW_CHARS))


def _context_enrich_max_windows() -> int:
    return int(_runtime_value("context_enrichment", "max_windows", CONTEXT_ENRICH_MAX_WINDOWS))


def _context_enrich_max_chars() -> int:
    return int(_runtime_value("context_enrichment", "max_chars", CONTEXT_ENRICH_MAX_CHARS))


def _graph_retrieval_enabled() -> bool:
    return NEO4J_ENABLED and bool(_runtime_value("graph_retrieval", "enabled", GRAPH_RETRIEVAL_ENABLED))


def _graph_retrieval_limit() -> int:
    return int(_runtime_value("graph_retrieval", "limit", GRAPH_RETRIEVAL_LIMIT))


def _graph_retrieval_pair_weight() -> float:
    return float(_runtime_value("graph_retrieval", "pair_weight", GRAPH_RETRIEVAL_PAIR_WEIGHT))


def _graph_retrieval_relation_weight() -> float:
    return float(_runtime_value("graph_retrieval", "relation_weight", GRAPH_RETRIEVAL_RELATION_WEIGHT))


def _graph_retrieval_direct_weight() -> float:
    return float(_runtime_value("graph_retrieval", "direct_relation_weight", GRAPH_RETRIEVAL_DIRECT_WEIGHT))


def _graph_retrieval_single_weight() -> float:
    return float(_runtime_value("graph_retrieval", "single_entity_weight", GRAPH_RETRIEVAL_SINGLE_WEIGHT))


def _graph_retrieval_hydrate_limit() -> int:
    return int(_runtime_value("graph_retrieval", "hydrate_limit", GRAPH_RETRIEVAL_HYDRATE_LIMIT))


def _graph_retrieval_snippet_chars() -> int:
    return int(_runtime_value("graph_retrieval", "snippet_chars", GRAPH_RETRIEVAL_SNIPPET_CHARS))


def _entity_resolution_enabled() -> bool:
    return NEO4J_ENABLED and bool(_runtime_value("entity_resolution", "enabled", ENTITY_RESOLUTION_ENABLED))


def _entity_fail_open() -> bool:
    return bool(_runtime_value("entity_resolution", "fail_open", ENTITY_FAIL_OPEN))


def _entity_fuzzy_enabled() -> bool:
    return bool(_runtime_value("entity_resolution", "fuzzy", ENTITY_FUZZY_ENABLED))


def _entity_fuzzy_threshold() -> float:
    return float(_runtime_value("entity_resolution", "fuzzy_threshold", ENTITY_FUZZY_THRESHOLD))


def _entity_fuzzy_max_candidates() -> int:
    return int(_runtime_value("entity_resolution", "fuzzy_max_candidates", ENTITY_FUZZY_MAX_CANDIDATES))


def _entity_fuzzy_expansion_min_similarity() -> float:
    return float(_runtime_value(
        "entity_resolution",
        "fuzzy_expansion_min_similarity",
        ENTITY_FUZZY_EXPANSION_MIN_SIMILARITY,
    ))


def _entity_retry_seconds() -> float:
    return float(_runtime_value("entity_resolution", "retry_seconds", ENTITY_RETRY_SECONDS))


def _runtime_reranker_config() -> dict:
    merged = dict(RERANKER_CONFIG)
    active = _ACTIVE_RUNTIME_CONFIG.get()
    section = active.get("reranker") if isinstance(active, dict) else None
    if isinstance(section, dict):
        merged.update(section)
    return merged


def _reranker_enabled() -> bool:
    backend = str(_runtime_reranker_config().get("backend", "none") or "none").strip().lower()
    return backend not in {"none", "off", "disabled"}


# ------------------------------------------------------------
# Fehlerklasse für eine bestimmte Pipeline-Stufe
# ------------------------------------------------------------

class PipelineStageError(RuntimeError):

    def __init__(
        self,
        stage: str,
        elapsed: float,
        original_exception: Exception,
    ):

        self.stage = stage
        self.elapsed = elapsed
        self.original_exception = original_exception

        message = (
            f"Fehler in Pipeline-Stufe "
            f"'{stage}' nach {elapsed:.3f} s: "
            f"{type(original_exception).__name__}: "
            f"{original_exception}"
        )

        super().__init__(message)


# ------------------------------------------------------------
# Timing-Helfer
# ------------------------------------------------------------

def run_timed(
    name,
    function,
    timings,
):

    started = time.perf_counter()

    try:

        result = function()

    except Exception as exc:

        elapsed = (
            time.perf_counter()
            - started
        )

        timings[name] = elapsed

        print(
            f"[RAG] FEHLER "
            f"{name}: "
            f"{elapsed:.3f} s - "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise PipelineStageError(
            stage=name,
            elapsed=elapsed,
            original_exception=exc,
        ) from exc


    elapsed = (
        time.perf_counter()
        - started
    )

    timings[name] = elapsed

    print(
        f"[RAG] {name}: "
        f"{elapsed:.3f} s",
        flush=True,
    )

    return result


# ------------------------------------------------------------
# Dokument-Metadaten
#
# Diese Felder werden durch die Retrieval-Pipeline mitgeführt.
# Nur title/path/document_date werden später als Relevanzsignal
# für den Reranker verwendet. ACL-/technische Felder bleiben
# reine Metadaten und dienen später Filterung bzw. Quellenlinks.
# ------------------------------------------------------------

RESULT_METADATA_KEYS = (
    "path",
    "directory",
    "filename",
    "nextcloud_es_id",
    "nextcloud_openfile_id",
    "source_url",
    "document_date",
    "source_date",
    "source_date_precision",
    "source_date_confidence",
    "source_date_basis",
    "content_type",
    "content_kind",
    "content_available",
    "content_hash",
    "source",
    "provider",
    "share_names",
    "owner",
    "users",
    "groups",
    "circles",
)


def path_metadata(title: str) -> dict:

    title = (title or "").strip()

    if not title:
        return {
            "path": "",
            "directory": "/",
            "filename": "",
        }

    path = PurePosixPath(
        "/" + title.lstrip("/")
    )

    return {
        "path": str(path),
        "directory": str(path.parent),
        "filename": path.name,
    }


def openfile_id(document_id: str) -> str:

    if document_id.startswith("files:"):
        return document_id.split(":", 1)[1]

    return ""


def nextcloud_source_url(
    directory: str,
    openfile: str | int | None,
) -> str:
    """Build the version-compatible Nextcloud Files URL for a retrieval result."""
    return build_nextcloud_source_url(
        NEXTCLOUD_BASE_URL,
        directory,
        openfile,
        major_version=_NEXTCLOUD_VERSION_PROBE.major_version(),
    )


def add_source_urls(results: list[dict]) -> None:
    """Attach deterministic source_url values to final search results."""

    for item in results:
        item["source_url"] = nextcloud_source_url(
            item.get("directory") or "/",
            item.get("nextcloud_openfile_id"),
        )


def merge_metadata(
    target: dict,
    source: dict,
):
    """Nicht-leere Metadaten aus source in target übernehmen."""

    for key in RESULT_METADATA_KEYS:

        value = source.get(key)

        if value is None:
            continue

        if value == "":
            continue

        if value == []:
            # Leere ACL-Listen sind semantisch relevant: sie bedeuten
            # "keine expliziten Einträge" und dürfen gesetzt werden.
            if key in {"users", "groups", "circles"}:
                target[key] = value
            continue

        if value == {}:
            if key == "share_names":
                target[key] = value
            continue

        target[key] = value


# ------------------------------------------------------------
# Elasticsearch-Klauseln
# ------------------------------------------------------------

# ------------------------------------------------------------
# Deterministischer Dateinamen-Lookup
#
# Nextcloud indexiert "title" in der vorliegenden ES-Mapping-Konfiguration
# mit dem keyword-Analyzer. Ein kompletter Pfad ist damit ein einzelnes
# Token. Normale match/multi_match-Abfragen auf nur den Basename sind für
# Navigationsfragen ungeeignet. Vollständige Dateinamen werden daher bei
# expliziten Lookup-Fragen vor Planner/RRF/Reranker direkt per suffix-
# wildcard auf title gesucht.
# ------------------------------------------------------------

_FILENAME_EXTENSIONS = (
    "pdf|odt|ods|odp|doc|docx|xls|xlsx|ppt|pptx|rtf|txt|csv|eml|msg|"
    "jpg|jpeg|png|tif|tiff|gif|webp|zip|7z|rar|xml|json|yaml|yml"
)

_QUOTED_FILENAME_RE = re.compile(
    rf'''["']([^"']+\.(?:{_FILENAME_EXTENSIONS}))["']''',
    re.IGNORECASE,
)

_BARE_FILENAME_RE = re.compile(
    rf'''(?<!\S)([^\s"'<>|]+\.(?:{_FILENAME_EXTENSIONS}))(?=$|[\s,;:!?])''',
    re.IGNORECASE,
)

_FILENAME_LOOKUP_HINT_RE = re.compile(
    r"\b(?:suche|such|finde|find|finden|gefunden|zeige|zeig|wo\s+ist|"
    r"wo\s+liegt|datei\s+mit\s+(?:dem\s+)?namen|datei\s+namens|"
    r"gibt\s+es\s+(?:die|eine)\s+datei)\b",
    re.IGNORECASE,
)


def extract_filename(question: str) -> str | None:
    """Extrahiert einen explizit genannten vollständigen Dateinamen."""

    text = str(question or "").strip()
    if not text:
        return None

    match = _QUOTED_FILENAME_RE.search(text)
    if not match:
        match = _BARE_FILENAME_RE.search(text)

    if not match:
        return None

    filename = match.group(1).strip().rstrip(".,;:!?")
    return filename or None


def is_pure_filename_lookup(question: str, filename: str | None = None) -> bool:
    """
    True nur für reine Navigations-/Existenzfragen.

    Der Dateiname wird aus der Frage entfernt; der verbleibende Text muss
    vollständig einer kleinen Menge eindeutiger Lookup-Formulierungen
    entsprechen. Dadurch wird z.B. "Suche in X.pdf nach Darlehen" NICHT
    kurzgeschlossen.
    """

    filename = filename or extract_filename(question)
    if not filename:
        return False

    text = str(question or "").strip()
    remainder = re.sub(
        re.escape(filename),
        " ",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    remainder = remainder.strip().strip('"\'`.,;:!?()[]{} ').strip()
    remainder = re.sub(r"\s+", " ", remainder)

    if not remainder:
        return True

    patterns = (
        r"(?:suche|such|finde|find|zeige(?: mir)?|zeig(?: mir)?) "
        r"(?:(?:eine|die) )?datei(?: mit (?:dem )?namen| namens)?",
        r"(?:wo ist|wo liegt) (?:(?:die|eine) )?datei",
        r"gibt es (?:(?:die|eine) )?datei(?: mit (?:dem )?namen| namens)?",
        r"datei(?: mit (?:dem )?namen| namens)?",
    )

    return any(
        re.fullmatch(pattern, remainder, flags=re.IGNORECASE)
        for pattern in patterns
    )

def _escape_wildcard_literal(value: str) -> str:
    """Escaped Elasticsearch wildcard metacharacters in a literal filename."""

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("?", "\\?")
    )


def filename_lookup(
    filename: str,
    limit: int = 20,
    source_scopes=None,
    *,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
) -> list[dict]:
    """Sucht einen Basename deterministisch am Ende des Nextcloud-Titels."""

    filename = str(filename or "").strip()
    if not filename:
        return []

    escaped = _escape_wildcard_literal(filename)
    body = {
        "size": max(1, min(int(limit), 50)),
        "_source": [
            "title",
            "source",
            "provider",
            "share_names",
            "owner",
            "users",
            "groups",
            "circles",
            "attachment.date",
            "attachment.content_type",
            "hash",
            "source_origin",
        ],
        "query": {
            "bool": {
                "should": [
                    # Preferred on mappings that expose the usual keyword
                    # subfield.  This preserves punctuation in file names.
                    {"wildcard": {"title.keyword": "*" + escaped}},
                    # Compatibility with older Nextcloud mappings where title
                    # itself is keyword-like.
                    {"wildcard": {"title": "*" + escaped}},
                    # Mapping-tolerant fallback for analyzed text fields.  The
                    # Python-side basename equality check below remains strict,
                    # so this only broadens candidate discovery, never /use
                    # selection semantics.
                    {"match_phrase": {"title": filename}},
                    # Nextcloud FullTextSearch commonly stores the searchable
                    # title contribution in ``combined`` even when ``title``
                    # itself is not searchable for this document.  Candidate
                    # discovery may therefore use ``combined`` as well; the
                    # strict Python-side basename equality below still decides
                    # whether /use may select the document.
                    {"match_phrase": {"combined": filename}},
                    {
                        "match": {
                            "combined": {
                                "query": filename,
                                "operator": "and",
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
    }

    filename_bool = body["query"]["bool"]
    filename_bool.setdefault("must_not", [])
    filename_bool.setdefault("filter", [])
    _apply_source_scope_to_es(filename_bool, source_scopes)
    _apply_acl_prefilter_to_es(
        filename_bool,
        user=acl_prefilter_user,
        groups=acl_prefilter_groups,
    )

    endpoint = f"{ES_URL}/{ES_INDEX}/_search"
    log.debug(
        "filename lookup request: index=%s size=%d query=%s",
        ES_INDEX,
        int(body.get("size") or 0),
        json.dumps(body.get("query") or {}, ensure_ascii=False, separators=(",", ":")),
    )
    response = _es_post(endpoint, json=body, timeout=ES_TIMEOUT)

    if response.is_error:
        print(
            f"[RAG] Filename-Lookup HTTP-Fehler: "
            f"{response.status_code} {response.reason_phrase}",
            flush=True,
        )
        print(
            "[RAG] Filename-Lookup Query: "
            + json.dumps(body, ensure_ascii=False, indent=2),
            flush=True,
        )
        print(
            "[RAG] Filename-Lookup Response: " + response.text,
            flush=True,
        )

    response.raise_for_status()
    data = response.json()

    results: list[dict] = []

    for rank, hit in enumerate(data.get("hits", {}).get("hits", []), start=1):
        source = hit.get("_source", {}) or {}
        title = str(source.get("title") or "")
        path_info = path_metadata(title)

        stored_filename = path_info.get("filename", "").casefold()
        wanted_filename = filename.casefold()
        if not (
            stored_filename == wanted_filename
            or stored_filename.endswith("_" + wanted_filename)
        ):
            continue

        document_id = str(hit.get("_id") or "")
        attachment = source.get("attachment") or {}

        results.append({
            "document_id": document_id,
            "rank": rank,
            "score": hit.get("_score"),
            "title": title,
            "snippet": f"Exakter Dateiname: {path_info.get('filename', filename)}",
            **path_info,
            "nextcloud_es_id": document_id,
            "nextcloud_openfile_id": openfile_id(document_id),
            "document_date": attachment.get("date"),
            "content_type": attachment.get("content_type"),
            "content_hash": source.get("hash"),
            "source": source.get("source", ""),
            "provider": source.get("provider", ""),
            "share_names": source.get("share_names") or {},
            "owner": source.get("owner"),
            "users": source.get("users") or [],
            "groups": source.get("groups") or [],
            "circles": source.get("circles") or [],
            "es_rank": rank,
            "es_score": hit.get("_score"),
            "vector_rank": None,
            "vector_score": None,
            "rrf": None,
            "rrf_rank": None,
            "chunk_no": None,
            "es_snippet": f"Exakter Dateiname: {path_info.get('filename', filename)}",
            "vector_snippet": "",
            "context_text": f"Exakter Dateitreffer: {title}",
            "context_enriched": False,
            "reranker_score": None,
            "reranker_raw_score": None,
            "duplicate_variants": [],
            "duplicate_count": 1,
        })

    add_source_urls(results)

    for rank, item in enumerate(results, start=1):
        item["final_rank"] = rank

    return results


_DOCUMENT_LOOKUP_SOURCE_FIELDS = [
    "title",
    "source",
    "provider",
    "share_names",
    "owner",
    "users",
    "groups",
    "circles",
    "attachment.date",
    "attachment.content_type",
    "hash",
]


def _document_lookup_result(
    document_id: str,
    source: dict,
    *,
    rank: int = 1,
    score=None,
    snippet: str = "",
) -> dict:
    title = str(source.get("title") or document_id)
    path_info = path_metadata(title)
    attachment = source.get("attachment") or {}
    item = {
        "document_id": document_id,
        "rank": rank,
        "score": score,
        "title": title,
        "snippet": snippet,
        **path_info,
        "nextcloud_es_id": document_id,
        "nextcloud_openfile_id": openfile_id(document_id),
        "document_date": attachment.get("date"),
        "content_type": attachment.get("content_type"),
        "content_hash": source.get("hash"),
        "source": source.get("source", ""),
        "provider": source.get("provider", ""),
        "share_names": source.get("share_names") or {},
        "owner": source.get("owner"),
        "users": source.get("users") or [],
        "groups": source.get("groups") or [],
        "circles": source.get("circles") or [],
        "es_rank": None,
        "es_score": None,
        "vector_rank": None,
        "vector_score": None,
        "graph_rank": None,
        "graph_score": None,
        "rrf": None,
        "rrf_rank": None,
        "chunk_no": None,
        "es_snippet": snippet,
        "vector_snippet": "",
        "graph_snippet": "",
        "context_text": snippet,
        "context_enriched": False,
        "reranker_score": None,
        "reranker_raw_score": None,
        "duplicate_variants": [],
        "duplicate_count": 1,
    }
    add_source_urls([item])
    return item


def document_ids_lookup(document_ids: list[str]) -> list[dict]:
    """Resolve exact Elasticsearch/Nextcloud document ids in one bounded mget."""
    clean_ids = list(dict.fromkeys(
        str(value or "").strip() for value in document_ids if str(value or "").strip()
    ))
    if not clean_ids:
        return []

    response = _es_post(
        f"{ES_URL}/{ES_INDEX}/_mget",
        json={
            "docs": [
                {"_id": document_id, "_source": _DOCUMENT_LOOKUP_SOURCE_FIELDS}
                for document_id in clean_ids
            ]
        },
        timeout=ES_TIMEOUT,
    )
    response.raise_for_status()
    docs_by_id = {
        str(doc.get("_id") or ""): doc
        for doc in response.json().get("docs", [])
        if doc.get("found")
    }
    results: list[dict] = []
    for document_id in clean_ids:
        doc = docs_by_id.get(document_id)
        if not doc:
            continue
        results.append(
            _document_lookup_result(
                document_id,
                doc.get("_source") or {},
                snippet=f"Direkt ausgewähltes Dokument: {document_id}",
            )
        )
    return results


def document_id_lookup(document_id: str) -> list[dict]:
    """Resolve one exact Elasticsearch/Nextcloud document id."""
    return document_ids_lookup([document_id])


def exact_path_lookup(path_value: str, limit: int = 50) -> list[dict]:
    """Resolve an exact Nextcloud path; leading slash is optional."""
    wanted = path_metadata(str(path_value or "").strip()).get("path", "")
    if not wanted or wanted == "/":
        return []

    basename = PurePosixPath(wanted).name
    candidates = filename_lookup(basename, limit=limit)
    return [
        item
        for item in candidates
        if str(item.get("path") or "").casefold() == wanted.casefold()
    ]


def strict_filename_lookup(filename: str, limit: int = 50) -> list[dict]:
    """Resolve a basename exactly; unlike legacy navigation, no suffix alias."""
    wanted = str(filename or "").strip().casefold()
    if not wanted:
        return []
    return [
        item
        for item in filename_lookup(filename, limit=limit)
        if str(item.get("filename") or "").casefold() == wanted
    ]


def document_reference_candidates(reference: str, limit: int = 50) -> list[dict]:
    """Return deterministic candidates for one explicit /use reference.

    This deliberately does *not* decide uniqueness.  The API applies the
    current Nextcloud Live-ACL/existence check first, because Elasticsearch can
    contain stale FullTextSearch entries for files that no longer exist.
    """
    value = str(reference or "").strip()
    if not value:
        return []
    if value.startswith("files:"):
        return document_id_lookup(value)
    if "/" in value:
        return exact_path_lookup(value, limit=limit)
    return strict_filename_lookup(value, limit=limit)


def normal_clause(term):
    """Recall-friendly lexical anchor for standard Nextcloud ES mappings.

    ``combined`` is the standard analyzed full-text field fed by title+content.
    The raw ``title`` field is commonly keyword-analyzed and must therefore not
    dominate natural-language relevance.  ``content`` remains as a compatible
    fallback/secondary field.
    """
    return {
        "multi_match": {
            "query": term,
            "fields": ["combined^2", "content"],
            "type": "best_fields",
            "operator": "or",
        }
    }


def weighted_normal_clause(term, boost: float = 1.0):
    return {
        "multi_match": {
            "query": term,
            "fields": ["combined^2", "content"],
            "type": "best_fields",
            "operator": "or",
            "boost": float(boost),
        }
    }


def phrase_clause(term):
    """Strict phrase/identifier clause for the normal RAG Files arm.

    Explicit user/planner phrases are precision anchors.  Elasticsearch may
    still analyze punctuation (e.g. ``22-07`` -> tokens ``22`` and ``07``),
    but those analyzed tokens must occur adjacently and in order.  The direct
    ``/elastic`` compatibility path intentionally keeps Nextcloud's broader
    QueryContent semantics instead.
    """
    value = str(term or "").strip()
    if not value:
        return {"match_none": {}}
    return {
        "multi_match": {
            "query": value,
            "fields": ["combined^2", "content"],
            "type": "phrase",
            "slop": 0,
        }
    }


def weighted_phrase_clause(term, boost: float = 1.0, slop: int = 0):
    value = str(term or "").strip()
    if not value:
        return {"match_none": {}}
    if not any(ch.isspace() for ch in value):
        return {
            "multi_match": {
                "query": value,
                "fields": ["combined^2", "content"],
                "operator": "or",
                "boost": float(boost),
            }
        }
    # Preserve deliberate positional tolerance used for person names such as
    # "Max Mustermann" -> "Max Alexander Mustermann".  Otherwise prefer the
    # broader phrase-prefix candidate generation.
    if int(slop) > 0:
        return {
            "multi_match": {
                "query": value,
                "fields": ["combined^2", "content"],
                "type": "phrase",
                "slop": max(0, int(slop)),
                "boost": float(boost),
            }
        }
    return {
        "multi_match": {
            "query": value,
            "fields": ["combined^2", "content"],
            "type": "phrase_prefix",
            "boost": float(boost),
        }
    }


def entity_group_clause(group: dict) -> dict | None:
    """Build one ES bool clause for one resolved/recognized Entity mention.

    All known forms of the same entity are alternatives (OR). For persons we
    additionally allow a one-position phrase slop on the original mention so
    that e.g. "Max Mustermann" can still match "Max Alexander Mustermann".
    This is only a retrieval fallback and receives a lower boost.
    """

    phrases = list(group.get("phrases") or [])
    if not phrases:
        return None

    should: list[dict] = []
    seen: set[tuple[str, int]] = set()
    entity_type = str(group.get("entity_type") or "").upper()
    mention = str(group.get("mention") or "").strip()

    for item in phrases:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        boost = float(item.get("boost", 1.0) or 1.0)
        key = (value.casefold(), 0)
        if key not in seen:
            seen.add(key)
            should.append(weighted_phrase_clause(value, boost, slop=0))

    # Middle-name tolerance for persons.  Keep this deliberately narrow:
    # one positional gap only, and lower than the exact phrase.
    if entity_type == "PERSON" and mention and len(mention.split()) >= 2:
        key = (mention.casefold(), 1)
        if key not in seen:
            seen.add(key)
            should.append(weighted_phrase_clause(mention, 2.4, slop=1))

    if not should:
        return None

    return {
        "bool": {
            "should": should,
            "minimum_should_match": 1,
        }
    }


def original_question_clause(question):
    """Language-neutral broad lexical view of the original user wording.

    Exact analyzed matching is primary. A lower-weight fuzzy sibling is a
    standard ES recall mechanism for spelling/OCR/inflection variation; it is
    never a hard filter and therefore cannot by itself prove relevance.
    """
    return {
        "bool": {
            "should": [
                {
                    "multi_match": {
                        "query": question,
                        "fields": ["combined^2", "content"],
                        "type": "best_fields",
                        "operator": "or",
                        "minimum_should_match": "25%",
                        "boost": 1.0,
                    }
                },
                {
                    "match": {
                        "combined": {
                            "query": question,
                            "operator": "or",
                            "minimum_should_match": "25%",
                            "fuzziness": "AUTO",
                            "prefix_length": 2,
                            "max_expansions": 50,
                            "boost": 0.35,
                        }
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    }


def _nextcloud_query_tokens(query: str) -> list[dict]:
    # Backwards-local alias; parser itself lives in a dependency-light module
    # so it can be regression-tested without importing the full RAG stack.
    return nextcloud_query_tokens(query)


def _nextcloud_field_clauses(token: dict) -> list[dict]:
    # Use the same match type selected by Nextcloud's QueryContent model.
    # In particular, a quoted token such as "22-07" is *plain match*, not
    # match_phrase; ordinary +terms use match_phrase_prefix.
    match_type = str(token.get("match") or "").strip()
    if match_type not in {"match", "match_phrase_prefix", "match_phrase"}:
        # Compatibility fallback for callers/tests that still pass the old
        # token shape.
        match_type = "match_phrase" if bool(token.get("phrase")) else "match"
    value = str(token.get("text") or "")
    return [
        {match_type: {"content": value}},
        {match_type: {"title": value}},
    ]


def elastic_exact_search(
    query: str,
    *,
    limit: int = 50,
    diagnostics: dict | None = None,
    source_scopes=None,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
) -> list[dict]:
    """Direct Nextcloud-style Elasticsearch Files search, without RAG planning.

    This path intentionally performs no entity expansion, vector/graph lookup,
    RRF or reranking.  It is the deterministic substrate for ``/elastic`` and
    ``/elastic /list``.
    """
    tokens = _nextcloud_query_tokens(query)
    if not tokens:
        if diagnostics is not None:
            diagnostics.update({"total_hits": 0, "total_relation": "eq", "tokens": []})
        return []

    bool_query: dict = {"must": [], "should": [], "must_not": [], "filter": []}
    # Do not add an explicit ``provider=files`` filter here.  The configured
    # Elasticsearch index used by older Nextcloud Full Text Search releases
    # may not expose ``provider`` as an exact searchable field (and the normal
    # middleware ES arm does not rely on it either).  The Nextcloud document
    # ids (``files:<fileid>``) plus the existing result normalization are the
    # stable substrate for this direct compatibility path.

    _apply_source_scope_to_es(bool_query, source_scopes)
    prefilter_applied = _apply_acl_prefilter_to_es(
        bool_query,
        user=acl_prefilter_user,
        groups=acl_prefilter_groups,
    )

    # Hidden mail metadata sidecars are machine-control data.  Exclude them at
    # query time as well as in the local post-filter so they cannot consume the
    # limited ES candidate window.  The generated filenames are deliberately
    # lowercase and exact, so old Elasticsearch versions do not need the newer
    # case_insensitive wildcard option.
    bool_query["must_not"].append({
        "bool": {
            "should": [
                {"wildcard": {"title.keyword": "*/.mailmeta.json"}},
                {"wildcard": {"title.keyword": "*/.*.mailmeta.json"}},
                {"wildcard": {"title.keyword": "*/.*.akirag.json"}},
                {"wildcard": {"title.keyword": "*/.*.sunaq.json"}},
                {"wildcard": {"title.keyword": "*/.*.metadata.json"}},
            ],
            "minimum_should_match": 1,
        }
    })

    for token in tokens:
        fields = _nextcloud_field_clauses(token)
        occur = str(token.get("occur") or "should")
        if occur == "must":
            bool_query["must"].append({"bool": {"should": fields, "minimum_should_match": 1}})
        elif occur == "must_not":
            # One excluded search term means: exclude a document when it matches
            # in either content OR title.
            bool_query["must_not"].append({"bool": {"should": fields, "minimum_should_match": 1}})
        else:
            bool_query["should"].extend(fields)

    if not bool_query["must"] and bool_query["should"]:
        bool_query["minimum_should_match"] = 1

    size = max(1, int(limit))
    body = {
        "size": size,
        "track_total_hits": True,
        "_source": [
            "title", "source", "provider", "share_names", "owner", "users",
            "groups", "circles", "attachment.date", "attachment.content_type", "hash",
        ],
        "query": {"bool": bool_query},
        "highlight": {
            "pre_tags": [""],
            "post_tags": [""],
            "fields": {
                "content": {"fragment_size": 1600, "number_of_fragments": 1},
            },
            "max_analyzed_offset": 1000000,
        },
    }

    endpoint = f"{ES_URL}/{ES_INDEX}/_search"
    response = _es_post(endpoint, json=body, timeout=ES_TIMEOUT)
    if response.is_error:
        log.warning("/elastic Elasticsearch HTTP error: %s %s", response.status_code, response.reason_phrase)
        log.debug("/elastic query=%s", json.dumps(body, ensure_ascii=False))
        try:
            log.debug("/elastic response=%s", json.dumps(response.json(), ensure_ascii=False))
        except Exception:
            log.debug("/elastic response=%s", response.text)
    response.raise_for_status()
    data = response.json()

    total = (data.get("hits") or {}).get("total")
    if isinstance(total, dict):
        total_hits = int(total.get("value") or 0)
        total_relation = str(total.get("relation") or "eq")
    elif isinstance(total, int):
        total_hits = int(total)
        total_relation = "eq"
    else:
        total_hits = 0
        total_relation = "eq"

    log.info(
        "elastic exact: es_total=%d size=%d tokens=%s",
        total_hits, size, json.dumps(tokens, ensure_ascii=False),
    )

    if diagnostics is not None:
        diagnostics.update({
            "total_hits": total_hits,
            "total_relation": total_relation,
            "tokens": tokens,
            "requested_size": size,
            "acl_prefilter_applied": prefilter_applied,
        })

    raw_hits = list((data.get("hits") or {}).get("hits", []))
    if raw_hits:
        # Titles and scores are sufficient to understand ranking without
        # writing potentially sensitive source snippets to the application log.
        log.info(
            "elastic exact hits: %s",
            json.dumps([
                {
                    "rank": index,
                    "score": hit.get("_score"),
                    "document_id": str(hit.get("_id") or ""),
                    "title": str((hit.get("_source") or {}).get("title") or ""),
                    "highlight_fields": sorted((hit.get("highlight") or {}).keys()),
                }
                for index, hit in enumerate(raw_hits[: min(len(raw_hits), 30)], start=1)
            ], ensure_ascii=False),
        )

    results: list[dict] = []
    for hit in raw_hits:
        source = hit.get("_source") or {}
        document_id = str(hit.get("_id") or "")
        title = str(source.get("title") or "")
        indexed_origin = _heal_source_origin_from_hit(document_id, title, source.get("source_origin"))
        if not document_id or not source_scope_allows_record(document_id, title, source_scopes, indexed_origin):
            continue
        attachment = source.get("attachment") or {}
        path_info = path_metadata(title)
        highlight = (hit.get("highlight") or {}).get("content") or []
        direct_rank = len(results) + 1
        direct_snippet = str(highlight[0]) if highlight else ""
        results.append({
            "document_id": document_id,
            "rank": direct_rank,
            "final_rank": direct_rank,
            "es_rank": direct_rank,
            "score": hit.get("_score"),
            "es_score": hit.get("_score"),
            "title": title,
            "snippet": direct_snippet,
            "es_snippet": direct_snippet,
            **path_info,
            "nextcloud_es_id": document_id,
            "nextcloud_openfile_id": openfile_id(document_id),
            "document_date": attachment.get("date"),
            "content_type": attachment.get("content_type"),
            "content_hash": source.get("hash"),
            "source": source.get("source", ""),
            "provider": source.get("provider", ""),
            "share_names": source.get("share_names") or {},
            "owner": source.get("owner"),
            "users": source.get("users") or [],
            "groups": source.get("groups") or [],
            "circles": source.get("circles") or [],
            "source_origin": indexed_origin,
        })
    add_source_urls(results)
    return results


# ------------------------------------------------------------
# Elasticsearch
# ------------------------------------------------------------

def elastic_search(
    question,
    plan,
    diagnostics: dict | None = None,
    source_scopes=None,
    *,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
):

    bool_query = {
        "must": [],
        "should": [],
        "must_not": [],
        "filter": [],
    }

    _apply_source_scope_to_es(bool_query, source_scopes)
    _apply_acl_prefilter_to_es(
        bool_query,
        user=acl_prefilter_user,
        groups=acl_prefilter_groups,
    )

    # Hidden mail metadata sidecars are machine-control data.  Exclude them at
    # query time as well as in the local post-filter so they cannot consume the
    # limited ES candidate window.  The generated filenames are deliberately
    # lowercase and exact, so old Elasticsearch versions do not need the newer
    # case_insensitive wildcard option.
    bool_query["must_not"].append({
        "bool": {
            "should": [
                {"wildcard": {"title.keyword": "*/.mailmeta.json"}},
                {"wildcard": {"title.keyword": "*/.*.mailmeta.json"}},
                {"wildcard": {"title.keyword": "*/.*.akirag.json"}},
                {"wildcard": {"title.keyword": "*/.*.sunaq.json"}},
                {"wildcard": {"title.keyword": "*/.*.metadata.json"}},
            ],
            "minimum_should_match": 1,
        }
    })


    search_spec_mode = str(getattr(plan, "search_mode", "")) == "search_spec"

    def lexical_term_clause(term: str) -> dict:
        if not search_spec_mode:
            return normal_clause(term)
        # SearchSpec files retrieval deliberately follows the proven
        # Nextcloud QueryContent field semantics: every explicit lexical
        # anchor may occur in content OR title.  Older Nextcloud indexes do
        # not necessarily expose a `combined` field, and filenames often
        # carry important identifiers that are absent from extracted content.
        return {
            "bool": {
                "should": _nextcloud_field_clauses({
                    "text": str(term or ""),
                    "match": "match_phrase_prefix",
                }),
                "minimum_should_match": 1,
            }
        }

    def lexical_phrase_clause(term: str) -> dict:
        if not search_spec_mode:
            return phrase_clause(term)
        value = str(term or "").strip()
        return {
            "bool": {
                "should": [
                    {"match_phrase": {"content": value}},
                    {"match_phrase": {"title": value}},
                ],
                "minimum_should_match": 1,
            }
        }

    elastic_query = str(getattr(plan, "elastic_query", "") or "").strip()
    if search_spec_mode and elastic_query:
        # Legacy compatibility path only. New planner output is structured
        # intent and reaches Elasticsearch through lexical_groups below.
        for token in nextcloud_query_tokens(elastic_query):
            clause = {
                "bool": {
                    "should": _nextcloud_field_clauses(token),
                    "minimum_should_match": 1,
                }
            }
            occur = str(token.get("occur") or "should")
            if occur not in {"must", "should", "must_not"}:
                occur = "should"
            bool_query[occur].append(clause)
    else:
        for term in plan.must:
            bool_query["must"].append(lexical_term_clause(term))

        for term in plan.phrases:
            bool_query["must"].append(lexical_phrase_clause(term))

        for term in plan.should:
            bool_query["should"].append(lexical_term_clause(term))

    # Structured planner intent is compiled here, never by the LLM. Each group
    # is an OR over lexical alternatives. Required groups become MUST(OR(...));
    # optional groups remain ranking signals.
    for group in getattr(plan, "lexical_groups", []) or []:
        if not isinstance(group, dict):
            continue
        alternatives = [
            lexical_term_clause(str(term).strip())
            for term in (group.get("terms") or [])
            if str(term).strip()
        ]
        if not alternatives:
            continue
        clause = {
            "bool": {
                "should": alternatives,
                "minimum_should_match": 1,
            }
        }
        if bool(group.get("required")):
            bool_query["must"].append(clause)
        else:
            bool_query["should"].append(clause)


    required_entity_groups = [
        group
        for group in (getattr(plan, "entity_groups", []) or [])
        if bool(group.get("required"))
    ]

    if required_entity_groups:
        for group in required_entity_groups:
            clause = entity_group_clause(group)
            if clause is not None:
                bool_query["must"].append(clause)
    else:
        # Für normale Entity-Suchen bleiben die Formen weiche Boosts. Im
        # Recall-Backoff werden sie weiter unten bereits innerhalb der
        # verpflichtenden Recall-Gruppe verwendet und daher hier nicht doppelt
        # gewichtet.
        if not bool(getattr(plan, "entity_recall_backoff", False)):
            for item in getattr(plan, "entity_should_phrases", []) or []:

                value = str(item.get("value") or "").strip()
                if not value:
                    continue

                bool_query["should"].append(
                    weighted_phrase_clause(
                        value,
                        float(item.get("boost", 1.0) or 1.0),
                    )
                )


    # Im gezielten zweiten Recall-Pass muss weiterhin mindestens ein
    # lexikalischer Anker der Entity vorkommen. Die exakten/kanonischen
    # Phrasen bleiben starke Alternativen; die einzelnen Tokens sind bewusst
    # niedrig gewichtet. So wird der Pass breiter, aber nicht völlig global.
    if bool(getattr(plan, "entity_recall_backoff", False)):
        recall_should: list[dict] = []
        recall_seen: set[str] = set()

        for item in getattr(plan, "entity_should_phrases", []) or []:
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            key = "p:" + value.casefold()
            if key in recall_seen:
                continue
            recall_seen.add(key)
            recall_should.append(
                weighted_phrase_clause(
                    value,
                    float(item.get("boost", 1.0) or 1.0),
                )
            )

        for item in getattr(plan, "entity_recall_terms", []) or []:
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            key = "t:" + value.casefold()
            if key in recall_seen:
                continue
            recall_seen.add(key)
            recall_should.append(
                weighted_normal_clause(
                    value,
                    float(item.get("boost", 0.5) or 0.5),
                )
            )

        if recall_should:
            bool_query["must"].append({
                "bool": {
                    "should": recall_should,
                    "minimum_should_match": 1,
                }
            })

    # In SearchSpec mode the natural-language question belongs to the
    # semantic/vector arm.  Do not feed it back into Elasticsearch as a broad
    # scoring clause; the files arm is defined solely by the structured
    # lexical rewrite.  Legacy planner mode keeps the historical boost.
    if not search_spec_mode:
        bool_query["should"].append(
            original_question_clause(question)
        )

    for term in plan.must_not:
        bool_query["must_not"].append(lexical_term_clause(term))

    for term in plan.must_not_phrases:
        bool_query["must_not"].append(lexical_phrase_clause(term))


    if (
        plan.date_from
        or plan.date_to
    ):

        date_range = {}


        if plan.date_from:

            date_range["gte"] = (
                plan.date_from
            )


        if plan.date_to:

            date_range["lte"] = (
                plan.date_to
            )


        bool_query["filter"].append(
            {
                "range": {
                    "attachment.date":
                        date_range
                }
            }
        )


    if (
        not bool_query["must"]
        and bool_query["should"]
    ):

        bool_query[
            "minimum_should_match"
        ] = 1


    body = {

        "size": _es_limit(),

        "_source": [
            "title",
            "source",
            "provider",
            "share_names",
            "owner",
            "users",
            "groups",
            "circles",
            "attachment.date",
            "attachment.content_type",
            "hash",
            "source_origin",
        ],

        "query": {
            "bool": bool_query
        },

        "highlight": {

            "pre_tags": [""],
            "post_tags": [""],

            "fields": {

                "content": {
                    "fragment_size": 1600,
                    "number_of_fragments": 1,
                }
            },
        },
    }

    if diagnostics is not None:
        # Query text/DSL is safe execution provenance. Candidate counts/ranks
        # remain server-internal until after live ACL.
        diagnostics["compiled_query"] = body.get("query") or {}


    es_endpoint = (
        f"{ES_URL}/{ES_INDEX}/_search"
    )

    if str(getattr(plan, "search_mode", "")) == "search_spec":
        log.info(
            "elasticsearch request: index=%s size=%s query=%s",
            ES_INDEX,
            body.get("size"),
            json.dumps(body.get("query") or {}, ensure_ascii=False, separators=(",", ":")),
        )
    else:
        log.debug(
            "elasticsearch request: index=%s size=%s query=%s",
            ES_INDEX,
            body.get("size"),
            json.dumps(body.get("query") or {}, ensure_ascii=False, separators=(",", ":")),
        )

    response = _es_post(
        es_endpoint,
        json=body,
        timeout=ES_TIMEOUT,
    )

    # --------------------------------------------------------
    # Elasticsearch-Fehlerdiagnose
    #
    # httpx.raise_for_status() zeigt bei HTTP 400 nur den
    # Statuscode. Die eigentliche Elasticsearch-Erklärung
    # (error.type / error.reason / root_cause) steckt jedoch
    # im Response-Body. Gerade für automatisch erzeugte
    # Hybrid-/Retry-Queries ist dieser Body entscheidend.
    # --------------------------------------------------------

    if response.is_error:

        print(
            f"[RAG] Elasticsearch HTTP-Fehler: "
            f"{response.status_code} "
            f"{response.reason_phrase}",
            flush=True,
        )

        print(
            f"[RAG] Elasticsearch Endpoint: "
            f"{es_endpoint}",
            flush=True,
        )

        print(
            f"[RAG] Planner-Modus: "
            f"{getattr(plan, 'search_mode', '')}",
            flush=True,
        )

        try:
            plan_dump = plan.model_dump()
        except AttributeError:
            plan_dump = str(plan)

        print(
            "[RAG] Suchplan: "
            + (
                json.dumps(
                    plan_dump,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                if isinstance(plan_dump, (dict, list))
                else str(plan_dump)
            ),
            flush=True,
        )

        print(
            "[RAG] Elasticsearch Query: "
            + json.dumps(
                body,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            flush=True,
        )

        try:
            error_body = response.json()
            error_text = json.dumps(
                error_body,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            error_text = response.text

        print(
            "[RAG] Elasticsearch Response: "
            + error_text,
            flush=True,
        )

    response.raise_for_status()

    data = response.json()

    if diagnostics is not None:
        total = (data.get("hits") or {}).get("total")
        if isinstance(total, dict):
            diagnostics["total_hits"] = total.get("value")
            diagnostics["total_relation"] = total.get("relation")
        elif isinstance(total, int):
            diagnostics["total_hits"] = total
            diagnostics["total_relation"] = "eq"
        else:
            diagnostics["total_hits"] = None
            diagnostics["total_relation"] = None

    raw_hits = list((data.get("hits") or {}).get("hits") or [])
    if str(getattr(plan, "search_mode", "")) == "search_spec" and raw_hits:
        log.info(
            "elasticsearch raw hits (before source-scope post-filter): %s",
            json.dumps([
                {
                    "rank": index,
                    "score": hit.get("_score"),
                    "document_id": str(hit.get("_id") or ""),
                    "title": str((hit.get("_source") or {}).get("title") or ""),
                }
                for index, hit in enumerate(raw_hits[:30], start=1)
            ], ensure_ascii=False),
        )

    results = []


    for rank, hit in enumerate(
        raw_hits,
        start=1,
    ):

        source = hit.get(
            "_source",
            {},
        )

        document_id = hit["_id"]
        title = source.get("title", "") or ""
        indexed_origin = _heal_source_origin_from_hit(str(document_id), title, source.get("source_origin"))
        if not source_scope_allows_record(str(document_id), title, source_scopes, indexed_origin):
            continue
        attachment = source.get("attachment") or {}
        path_info = path_metadata(title)


        highlight = (
            hit
            .get("highlight", {})
            .get("content", [])
        )


        snippet = ""

        if highlight:

            snippet = highlight[0]


        results.append(
            {
                "document_id":
                    document_id,

                "rank":
                    len(results) + 1,

                "score":
                    hit.get("_score"),

                "title":
                    title,

                "snippet":
                    snippet,

                **path_info,

                "nextcloud_es_id":
                    document_id,

                "nextcloud_openfile_id":
                    openfile_id(document_id),

                "document_date":
                    attachment.get("date"),

                "content_type":
                    attachment.get("content_type"),

                "content_hash":
                    source.get("hash"),

                "source":
                    source.get("source", ""),

                "provider":
                    source.get("provider", ""),

                "share_names":
                    source.get("share_names") or {},

                "owner":
                    source.get("owner"),

                "users":
                    source.get("users") or [],

                "groups":
                    source.get("groups") or [],

                "circles":
                    source.get("circles") or [],

                "source_origin":
                    indexed_origin,
            }
        )
        if len(results) >= _es_limit():
            break


    return results


# ------------------------------------------------------------
# Qdrant / Vektorsuche
#
# Embedding und Qdrant werden absichtlich separat gemessen.
# ------------------------------------------------------------

def vector_search(
    plan,
    timings,
    diagnostics: dict | None = None,
    source_scopes=None,
    *,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
):

    if not plan.semantic_query:

        timings["embedding"] = 0.0
        timings["qdrant"] = 0.0
        timings["vector_processing"] = 0.0

        return []


    if store is None:
        raise RuntimeError("Qdrant vector store is unavailable")

    # --------------------------------------------------------
    # Embedding über konfiguriertes Backend
    # --------------------------------------------------------

    query_vector = run_timed(
        "embedding",
        lambda: embeddings.embed_query(plan.semantic_query),
        timings,
    )


    # --------------------------------------------------------
    # Qdrant-Abfrage
    # --------------------------------------------------------

    normalized_scopes = normalize_source_scopes(source_scopes)
    vector_exclude_origins: list[str] | None = None
    vector_include_origins: list[str] | None = None
    if normalized_scopes is None:
        # Client-neutral implicit default: ordinary documents only. Keep legacy
        # untagged document chunks eligible; path classification below remains
        # the authoritative fallback.
        vector_exclude_origins = ["mail_archive", "web_archive", "chat_archive"]
    elif "documents" in normalized_scopes:
        # Mirror the Elasticsearch semantics: ordinary documents are the
        # complement of unselected archive classes. Using exclusions rather
        # than a positive origin filter keeps older untagged document chunks
        # recoverable.
        origin_by_scope = {
            "mailarchive": "mail_archive",
            "webarchive": "web_archive",
            "chatarchive": "chat_archive",
        }
        vector_exclude_origins = [
            origin
            for scope, origin in origin_by_scope.items()
            if scope not in normalized_scopes
        ]
    else:
        # Archive-only requests can safely use the positive payload filter.
        vector_include_origins = sorted(
            source_origins_for_scopes(normalized_scopes) or []
        )

    raw_results = run_timed(
        "qdrant",
        lambda: store.search(  # type: ignore[union-attr]
            query_vector,
            limit=_vector_limit(),
            exclude_source_origins=vector_exclude_origins,
            include_source_origins=vector_include_origins,
            acl_user=acl_prefilter_user,
            acl_groups=acl_prefilter_groups,
        ),
        timings,
    )

    if diagnostics is not None:
        diagnostics["embedding_backend"] = getattr(embeddings, "kind", "")
        diagnostics["embedding_model"] = getattr(embeddings, "model", "")
        diagnostics["embedding_profile"] = getattr(embeddings, "profile", "plain")
        diagnostics["raw_results"] = len(raw_results)
        diagnostics["above_threshold"] = sum(
            1 for result in raw_results
            if float(getattr(result, "score", 0.0)) >= _vector_threshold()
        )


    # --------------------------------------------------------
    # Resultate filtern / pro Dokument deduplizieren
    # --------------------------------------------------------

    started = time.perf_counter()

    documents = []
    seen_documents = set()


    for result in raw_results:

        if (
            result.score
            < _vector_threshold()
        ):

            continue


        payload = (
            result.payload
            or {}
        )

        # Path classification remains authoritative for legacy chunks whose
        # source_origin predates mail/chat archive tagging.
        if not source_scope_allows_record(
            str(payload.get("document_id") or ""),
            str(payload.get("path") or payload.get("title") or ""),
            source_scopes,
            payload.get("source_origin"),
        ):
            continue


        document_id = payload.get(
            "document_id"
        )


        if not document_id:

            continue


        if document_id in seen_documents:

            continue


        seen_documents.add(
            document_id
        )

        title = payload.get("title", "") or ""
        path_info = path_metadata(title)

        # Neue Sync-Versionen speichern path/directory/filename direkt.
        # Für ältere Bestände bleiben die aus title abgeleiteten Werte
        # ein sauberer Fallback.
        for key in (
            "path",
            "directory",
            "filename",
        ):
            if payload.get(key):
                path_info[key] = payload[key]


        documents.append(
            {
                "document_id":
                    document_id,

                "score":
                    result.score,

                "title":
                    title,

                "chunk_no":
                    payload.get(
                        "chunk_no",
                        payload.get("chunk_index"),
                    ),

                "snippet":
                    payload.get(
                        "text",
                        "",
                    ),

                **path_info,

                "nextcloud_es_id":
                    payload.get(
                        "nextcloud_es_id",
                        document_id,
                    ),

                "nextcloud_openfile_id":
                    payload.get(
                        "nextcloud_openfile_id",
                        openfile_id(document_id),
                    ),

                "document_date":
                    payload.get("document_date"),

                "content_type":
                    payload.get("content_type"),

                "content_kind":
                    payload.get("content_kind"),

                "content_available":
                    payload.get("content_available"),

                "content_hash":
                    payload.get("content_hash") or payload.get("hash"),

                "source":
                    payload.get(
                        "source",
                        payload.get("nextcloud_source", ""),
                    ),

                "provider":
                    payload.get("provider", ""),

                "share_names":
                    payload.get("share_names") or {},

                "owner":
                    payload.get("owner"),

                "users":
                    payload.get("users") or [],

                "groups":
                    payload.get("groups") or [],

                "circles":
                    payload.get("circles") or [],
            }
        )
        if len(documents) >= _vector_limit():
            break


    for rank, item in enumerate(
        documents,
        start=1,
    ):

        item["rank"] = rank


    elapsed = (
        time.perf_counter()
        - started
    )

    timings[
        "vector_processing"
    ] = elapsed


    if diagnostics is not None:
        diagnostics["unique_documents"] = len(documents)

    print(
        f"[RAG] vector_processing: "
        f"{elapsed:.3f} s",
        flush=True,
    )


    return documents


# ------------------------------------------------------------
# Reciprocal Rank Fusion
# ------------------------------------------------------------

def fuse_results(
    elastic_results,
    vector_results,
    graph_results=None,
    es_weight: float = 1.0,
    vector_weight: float = 1.0,
    graph_weight: float = 0.0,
):

    graph_results = graph_results or []

    combined = {}


    def new_record(item):

        record = {
            "document_id":
                item["document_id"],

            "title":
                item.get("title", ""),

            "rrf":
                0.0,

            "es_rank":
                None,

            "es_score":
                None,

            "vector_rank":
                None,

            "vector_score":
                None,

            "graph_rank":
                None,

            "graph_score":
                None,

            "graph_reason":
                None,

            "graph_entities":
                [],

            "graph_direct_relations":
                [],

            "graph_snippet":
                "",

            "chunk_no":
                None,

            "es_snippet":
                "",

            "vector_snippet":
                "",

            "duplicate_variants":
                [],

            "duplicate_count":
                1,
        }

        merge_metadata(
            record,
            item,
        )
        _merge_duplicate_variants(
            record,
            item.get("duplicate_variants", []),
        )

        return record


    # --------------------------------------------------------
    # Elasticsearch
    # --------------------------------------------------------

    for item in elastic_results:

        document_id = (
            item["document_id"]
        )


        record = combined.setdefault(
            document_id,
            new_record(item),
        )

        merge_metadata(
            record,
            item,
        )
        _merge_duplicate_variants(
            record,
            item.get("duplicate_variants", []),
        )


        if not record["title"]:

            record["title"] = (
                item.get("title", "")
            )


        record["es_rank"] = (
            item["rank"]
        )

        record["es_score"] = (
            item["score"]
        )

        record["es_snippet"] = (
            item["snippet"]
        )


        record["rrf"] += (
            float(es_weight)
            / (
                _rrf_k()
                + item["rank"]
            )
        )


    # --------------------------------------------------------
    # Qdrant
    # --------------------------------------------------------

    for item in vector_results:

        document_id = (
            item["document_id"]
        )


        record = combined.setdefault(
            document_id,
            new_record(item),
        )

        merge_metadata(
            record,
            item,
        )
        _merge_duplicate_variants(
            record,
            item.get("duplicate_variants", []),
        )


        if not record["title"]:

            record["title"] = (
                item.get("title", "")
            )


        record["vector_rank"] = (
            item["rank"]
        )

        record["vector_score"] = (
            item["score"]
        )

        record["chunk_no"] = (
            item["chunk_no"]
        )

        record["vector_snippet"] = (
            item["snippet"]
        )


        record["rrf"] += (
            float(vector_weight)
            / (
                _rrf_k()
                + item["rank"]
            )
        )



    # --------------------------------------------------------
    # Neo4j document graph
    # --------------------------------------------------------

    for item in graph_results:
        document_id = item["document_id"]
        record = combined.setdefault(document_id, new_record(item))
        merge_metadata(record, item)
        _merge_duplicate_variants(record, item.get("duplicate_variants", []))

        if not record["title"]:
            record["title"] = item.get("title", "")

        record["graph_rank"] = item.get("rank")
        record["graph_score"] = item.get("score")
        record["graph_reason"] = item.get("graph_reason")
        record["graph_entities"] = list(item.get("graph_matched_entity_names") or [])
        record["graph_direct_relations"] = list(item.get("graph_direct_relations") or [])
        record["graph_snippet"] = str(item.get("graph_snippet") or "")

        if item.get("rank"):
            record["rrf"] += (
                float(graph_weight)
                / (_rrf_k() + int(item["rank"]))
            )

    return sorted(
        combined.values(),
        key=lambda item: item["rrf"],
        reverse=True,
    )



# ------------------------------------------------------------
# Dubletten-Gruppierung
# ------------------------------------------------------------

def _dedup_filename_signature(item: dict):
    """
    Liefert (Verzeichnis, normalisierter Stamm, Endung).

    Damit werden z.B.
        Schreiben.odt
        Schreiben.pdf
    im selben Verzeichnis sicher als Fassungen desselben
    Dokuments erkannt. Gleiche Stämme in verschiedenen
    Verzeichnissen werden nicht zusammengelegt.
    """

    raw_path = (
        item.get("path")
        or item.get("title")
        or ""
    )

    if not raw_path:
        return None

    path = PurePosixPath(
        "/" + str(raw_path).lstrip("/")
    )

    suffix = path.suffix.casefold()

    if suffix not in DEDUP_VARIANT_EXTENSIONS:
        return None

    stem = re.sub(
        r"[^\wäöüß]+",
        "",
        path.stem.casefold(),
        flags=re.UNICODE,
    )

    if not stem:
        return None

    return (
        str(path.parent).casefold(),
        stem,
        suffix,
    )


def _normalize_dedup_text(text: str) -> str:
    """
    Normalisierung nur für Dublettenerkennung.

    - HTML-Highlight-Tags aus Elasticsearch entfernen
    - Groß/Kleinschreibung ignorieren
    - Zeilen-/Mehrfachabstände normalisieren
    - Interpunktion ignorieren
    - einfache OCR-Zeilentrennung ("Ge-\nsellschaft") glätten
    """

    text = str(text or "")

    if not text:
        return ""

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    # Typische OCR-/Layout-Trennung am Zeilenende.
    text = re.sub(
        r"(?<=\w)-\s+(?=\w)",
        "",
        text,
    )

    text = text.casefold()

    text = re.sub(
        r"[^\wäöüß]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def _dedup_candidate_texts(item: dict) -> list[str]:
    """
    Für die Dublettenerkennung beide verfügbaren Textsichten
    verwenden. Der Vektor-Chunk und der ES-Ausschnitt können
    wegen unterschiedlicher Grenzen leicht versetzt sein.
    """

    values = []

    for key in (
        "vector_snippet",
        "es_snippet",
        "graph_snippet",
    ):
        normalized = _normalize_dedup_text(
            item.get(key) or ""
        )

        if (
            normalized
            and normalized not in values
        ):
            values.append(normalized)

    return values


def _same_filename_variant(
    left_signature,
    right_signature,
) -> bool:
    """
    Gleicher Dateistamm + gleiches Verzeichnis + verschiedene
    Dokumentformate => sehr starker Dublettenhinweis.
    """

    if (
        left_signature is None
        or right_signature is None
    ):
        return False

    left_dir, left_stem, left_suffix = (
        left_signature
    )

    right_dir, right_stem, right_suffix = (
        right_signature
    )

    return (
        left_dir == right_dir
        and left_stem == right_stem
        and left_suffix != right_suffix
    )


def _dedup_content_hash(item: dict) -> str:
    """Return a trusted exact extracted-content hash, or empty string.

    Nextcloud FullTextSearch currently exposes a 32-hex MD5 over extracted
    indexed content.  Treat only that exact shape as an equality signal; an
    absent or differently shaped field falls back to the existing conservative
    filename/text duplicate checks.
    """
    value = str(item.get("content_hash") or item.get("hash") or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def _near_duplicate_text(
    left_texts: list[str],
    right_texts: list[str],
) -> tuple[bool, str | None, float | None]:
    """
    Konservative Textprüfung.

    Exakt normalisierte Treffertexte werden direkt gruppiert.
    Für OCR-Abweichungen wird SequenceMatcher verwendet, aber
    nur bei langen und ähnlich langen Texten und mit hoher
    Standardschwelle (0.96).
    """

    for left in left_texts:

        for right in right_texts:

            shortest = min(
                len(left),
                len(right),
            )

            if (
                shortest
                >= DEDUP_EXACT_TEXT_MIN_CHARS
                and left == right
            ):
                return (
                    True,
                    "exact_text",
                    1.0,
                )

            if (
                shortest
                < DEDUP_MIN_TEXT_CHARS
            ):
                continue

            longest = max(
                len(left),
                len(right),
            )

            if not longest:
                continue

            length_ratio = (
                shortest
                / longest
            )

            if (
                length_ratio
                < DEDUP_MIN_LENGTH_RATIO
            ):
                continue

            matcher = SequenceMatcher(
                None,
                left,
                right,
                autojunk=False,
            )

            # Billige Vorprüfungen vermeiden die teure ratio()
            # für offensichtlich verschiedene Texte.
            if (
                matcher.real_quick_ratio()
                < DEDUP_NEAR_TEXT_RATIO
            ):
                continue

            if (
                matcher.quick_ratio()
                < DEDUP_NEAR_TEXT_RATIO
            ):
                continue

            ratio = matcher.ratio()

            if (
                ratio
                >= DEDUP_NEAR_TEXT_RATIO
            ):
                return (
                    True,
                    "near_text",
                    ratio,
                )

    return (
        False,
        None,
        None,
    )


def _duplicate_variant(
    item: dict,
    reason: str,
    similarity: float | None,
) -> dict:
    """
    Kleine, aber vollständige Referenz auf eine unterdrückte
    Fassung. Sie bleibt am Repräsentanten erhalten.
    """

    directory = (
        item.get("directory")
        or path_metadata(
            item.get("title", "")
        )["directory"]
    )

    openfile = (
        item.get("nextcloud_openfile_id")
        or openfile_id(
            item.get("document_id", "")
        )
    )

    return {
        "document_id":
            item.get("document_id"),

        "title":
            item.get("title", ""),

        "path":
            item.get("path")
            or path_metadata(
                item.get("title", "")
            )["path"],

        "directory":
            directory,

        "filename":
            item.get("filename")
            or path_metadata(
                item.get("title", "")
            )["filename"],

        "nextcloud_es_id":
            item.get("nextcloud_es_id") or item.get("document_id"),

        "nextcloud_openfile_id":
            openfile,

        "source_url":
            nextcloud_source_url(
                directory,
                openfile,
            ),

        "document_date":
            item.get("document_date"),

        "content_type":
            item.get("content_type"),

        "content_hash":
            item.get("content_hash"),

        "source":
            item.get("source"),

        "provider":
            item.get("provider"),

        "share_names":
            item.get("share_names") or {},

        "owner":
            item.get("owner"),

        "users":
            item.get("users") or [],

        "groups":
            item.get("groups") or [],

        "circles":
            item.get("circles") or [],

        "source_origin":
            item.get("source_origin"),

        "snippet":
            item.get("snippet", ""),

        "es_snippet":
            item.get("es_snippet", ""),

        "vector_snippet":
            item.get("vector_snippet", ""),

        "graph_snippet":
            item.get("graph_snippet", ""),

        "reason":
            reason,

        "similarity":
            similarity,
    }


def _merge_duplicate_variants(
    target: dict,
    variants: list[dict],
) -> None:
    existing = target.setdefault("duplicate_variants", [])
    seen = {
        str(item.get("document_id") or "")
        for item in existing
        if isinstance(item, dict)
    }
    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        document_id = str(variant.get("document_id") or "")
        if document_id and document_id in seen:
            continue
        existing.append(dict(variant))
        if document_id:
            seen.add(document_id)
    target["duplicate_count"] = 1 + len(existing)


def _merge_retrieval_provenance(target: dict, source: dict) -> None:
    """Merge arm-specific retrieval metadata when two document variants dedupe.

    A representative may have won the RRF ordering via Elasticsearch while a
    suppressed variant was the graph #1 (or vector #1).  Losing that metadata
    makes the final result look as if the arm never found the document.  Keep
    the best rank from every arm and the corresponding snippets/signals.
    """

    for arm in ("es", "vector", "graph"):
        rank_key = f"{arm}_rank"
        score_key = f"{arm}_score"
        target_rank = target.get(rank_key)
        source_rank = source.get(rank_key)

        if source_rank is not None and (
            target_rank is None or int(source_rank) < int(target_rank)
        ):
            target[rank_key] = source_rank
            if source.get(score_key) is not None:
                target[score_key] = source.get(score_key)

    if not target.get("es_snippet") and source.get("es_snippet"):
        target["es_snippet"] = source.get("es_snippet")
    if not target.get("vector_snippet") and source.get("vector_snippet"):
        target["vector_snippet"] = source.get("vector_snippet")
    if not target.get("graph_snippet") and source.get("graph_snippet"):
        target["graph_snippet"] = source.get("graph_snippet")

    if source.get("graph_rank") is not None:
        if source.get("graph_reason") and not target.get("graph_reason"):
            target["graph_reason"] = source.get("graph_reason")

        names = list(target.get("graph_entities") or [])
        for value in source.get("graph_entities") or []:
            if value not in names:
                names.append(value)
        target["graph_entities"] = names

        relations = list(target.get("graph_direct_relations") or [])
        for relation in source.get("graph_direct_relations") or []:
            if relation not in relations:
                relations.append(relation)
        target["graph_direct_relations"] = relations

        chains = list(target.get("graph_indirect_chains") or [])
        for chain in source.get("graph_indirect_chains") or []:
            if chain not in chains:
                chains.append(chain)
        target["graph_indirect_chains"] = chains


def _arm_head_document_ids(
    fused_results: list[dict],
    preserve_per_arm: int = 1,
) -> list[str]:
    """Return document ids that must reach the reranker.

    The RRF score is a fusion metric, not a licence to discard the best result
    of a retrieval arm before the cross-encoder can inspect it.  Preserve the
    first N results from each active arm; overlapping ids are kept only once.
    """

    if preserve_per_arm <= 0:
        return []

    protected: list[str] = []
    seen: set[str] = set()

    for rank_key in ("es_rank", "vector_rank", "graph_rank"):
        arm_items = [
            item for item in fused_results
            if item.get(rank_key) is not None
        ]
        arm_items.sort(key=lambda item: int(item[rank_key]))
        for item in arm_items[:preserve_per_arm]:
            document_id = str(item.get("document_id") or "")
            if document_id and document_id not in seen:
                protected.append(document_id)
                seen.add(document_id)

    return protected


def deduplicate_for_reranker(
    fused_results: list[dict],
    max_unique: int | None = None,
    preserve_per_arm: int = 1,
) -> list[dict]:
    """Build the cross-encoder candidate pool with arm-head preservation.

    Rules:
    - At least the #1 document of each active retrieval arm is admitted to the
      reranker (configurable through ``preserve_per_arm``).
    - Remaining slots are filled in normal RRF order.
    - Duplicates/alternate file variants consume only one slot.
    - Retrieval provenance from suppressed variants is merged into the kept
      representative, so a graph #1 cannot become invisible after dedup.
    """

    if max_unique is None:
        max_unique = _rerank_candidates()
    if not fused_results or max_unique <= 0:
        return []

    protected_ids = _arm_head_document_ids(
        fused_results,
        preserve_per_arm=preserve_per_arm,
    )
    by_id = {
        str(item.get("document_id") or ""): item
        for item in fused_results
        if item.get("document_id")
    }

    # Protected heads first, then the ordinary RRF ordering.  This ordering is
    # only for admission to the reranker; the cross-encoder decides final rank.
    ordered: list[dict] = []
    queued: set[str] = set()
    for document_id in protected_ids:
        item = by_id.get(document_id)
        if item is not None and document_id not in queued:
            ordered.append(item)
            queued.add(document_id)
    for item in fused_results:
        document_id = str(item.get("document_id") or "")
        if document_id and document_id not in queued:
            ordered.append(item)
            queued.add(document_id)

    if not DEDUP_ENABLED:
        return [dict(item) for item in ordered[:max_unique]]

    representatives: list[dict] = []

    for original in ordered:
        candidate = dict(original)
        candidate_signature = _dedup_filename_signature(candidate)
        candidate_texts = _dedup_candidate_texts(candidate)
        candidate_hash = _dedup_content_hash(candidate)
        matched = False

        for representative in representatives:
            reason = None
            similarity = None
            representative_signature = representative.get(
                "_dedup_filename_signature"
            )
            representative_hash = representative.get("_dedup_content_hash", "")

            if candidate_hash and candidate_hash == representative_hash:
                reason = "exact_extracted_content_hash"
                similarity = 1.0
            elif _same_filename_variant(
                candidate_signature,
                representative_signature,
            ):
                reason = "same_filename_variant"
            else:
                is_duplicate, reason, similarity = _near_duplicate_text(
                    candidate_texts,
                    representative.get("_dedup_texts", []),
                )
                if not is_duplicate:
                    reason = None

            if reason is None:
                continue

            _merge_retrieval_provenance(representative, candidate)
            _merge_duplicate_variants(
                representative,
                [_duplicate_variant(candidate, reason, similarity)],
            )
            _merge_duplicate_variants(
                representative,
                candidate.get("duplicate_variants", []),
            )
            matched = True
            break

        if matched:
            continue

        candidate["duplicate_variants"] = list(
            candidate.get("duplicate_variants") or []
        )
        candidate["duplicate_count"] = 1 + len(
            candidate["duplicate_variants"]
        )
        candidate["_dedup_filename_signature"] = candidate_signature
        candidate["_dedup_texts"] = candidate_texts
        candidate["_dedup_content_hash"] = candidate_hash
        representatives.append(candidate)

        if len(representatives) >= max_unique:
            break

    for representative in representatives:
        representative.pop("_dedup_filename_signature", None)
        representative.pop("_dedup_texts", None)
        representative.pop("_dedup_content_hash", None)

    return representatives


def deduplicate_retrieval_arm(
    results: list[dict],
    arm: str,
) -> list[dict]:
    """Dedupliziert ES oder Vector bereits VOR der Signalmessung.

    Die Originalreihenfolge entscheidet über den Repräsentanten; nach dem
    Entfernen physischer/nahezu identischer Fassungen werden die Ränge neu
    durchnummeriert. Damit misst die Scorekurve nicht die Ablagestruktur.
    """

    if arm not in {"es", "vector"}:
        raise ValueError(f"Unbekannter Retrieval-Arm: {arm}")

    if not results:
        return []

    if not DEDUP_ENABLED:
        copied = [dict(item) for item in results]
        for rank, item in enumerate(copied, start=1):
            item["rank"] = rank
            item.setdefault("duplicate_variants", [])
            item["duplicate_count"] = 1 + len(item["duplicate_variants"])
        return copied

    representatives: list[dict] = []

    for original in results:
        candidate = dict(original)
        probe = dict(candidate)
        probe["es_snippet"] = candidate.get("snippet", "") if arm == "es" else ""
        probe["vector_snippet"] = candidate.get("snippet", "") if arm == "vector" else ""

        candidate_signature = _dedup_filename_signature(probe)
        candidate_texts = _dedup_candidate_texts(probe)
        candidate_hash = _dedup_content_hash(candidate)
        matched = False

        for representative in representatives:
            reason = None
            similarity = None
            representative_hash = representative.get("_dedup_content_hash", "")

            if candidate_hash and candidate_hash == representative_hash:
                reason = "exact_extracted_content_hash"
                similarity = 1.0
            elif _same_filename_variant(
                candidate_signature,
                representative.get("_dedup_filename_signature"),
            ):
                reason = "same_filename_variant"
            else:
                is_duplicate, reason, similarity = _near_duplicate_text(
                    candidate_texts,
                    representative.get("_dedup_texts", []),
                )
                if not is_duplicate:
                    reason = None

            if reason is None:
                continue

            _merge_duplicate_variants(
                representative,
                [_duplicate_variant(candidate, reason, similarity)],
            )
            _merge_duplicate_variants(
                representative,
                candidate.get("duplicate_variants", []),
            )
            matched = True
            break

        if matched:
            continue

        candidate["duplicate_variants"] = list(
            candidate.get("duplicate_variants") or []
        )
        candidate["duplicate_count"] = 1 + len(candidate["duplicate_variants"])
        candidate["_dedup_filename_signature"] = candidate_signature
        candidate["_dedup_texts"] = candidate_texts
        candidate["_dedup_content_hash"] = candidate_hash
        representatives.append(candidate)

    for rank, representative in enumerate(representatives, start=1):
        representative["rank"] = rank
        representative.pop("_dedup_filename_signature", None)
        representative.pop("_dedup_texts", None)
        representative.pop("_dedup_content_hash", None)

    return representatives


# ------------------------------------------------------------
# RRF-Fallback
# ------------------------------------------------------------

def rrf_fallback(
    fused_results,
    limit,
):

    fallback = []


    for item in fused_results[:limit]:

        new_item = dict(item)

        new_item["reranker_score"] = None
        new_item["reranker_raw_score"] = None

        fallback.append(
            new_item
        )


    return fallback


# ------------------------------------------------------------
# Antwortkontext für finale Dokumente anreichern
# ------------------------------------------------------------

_CONTEXT_STOPWORDS = {
    "aber", "alle", "auch", "dass", "eine", "einem", "einen", "einer",
    "eines", "für", "gegen", "gegeben", "gibt", "haben", "hat", "ihre",
    "ihren", "ihres", "ist", "mit", "nach", "oder", "sind", "über",
    "und", "von", "was", "welche", "welcher", "welches", "wer", "wie",
    "wurde", "wurden", "zum", "zur",
}


def _clean_context_text(text: str) -> str:
    """Normalize extracted text without erasing useful document boundaries.

    PDF/OCR extractors often use form-feed characters between pages and blank
    lines between layout blocks.  Collapsing all whitespace with ``\\s+`` made
    the tail of one letter run directly into the recipient block of the next
    page.  That is especially dangerous for role inference.  Preserve those
    boundaries as explicit textual markers while still normalising line noise.
    """
    text = str(text or "")
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "", text)

    # Protect page/block boundaries before ordinary whitespace normalisation.
    page_marker = " __RAG_PAGE_BREAK__ "
    block_marker = " __RAG_BLOCK_BREAK__ "
    text = text.replace("\f", page_marker)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)*", block_marker, text)
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)

    text = text.replace(page_marker.strip(), "\n[SEITENWECHSEL]\n")
    text = text.replace(block_marker.strip(), "\n\n")
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _context_terms(question: str, plan) -> list[str]:
    """Kurze Liste markanter Begriffe für Fenster im Volltext."""
    values: list[str] = []

    for attr in ("must", "phrases", "should"):
        for value in getattr(plan, attr, []) or []:
            value = str(value or "").strip()
            if value:
                values.append(value)

    for item in getattr(plan, "entity_should_phrases", []) or []:
        value = str(item.get("value") or "").strip()
        if value:
            values.append(value)

    values.extend(re.findall(r"[\wÄÖÜäöüß-]{4,}", question or "", flags=re.UNICODE))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        folded = value.casefold()
        if folded in _CONTEXT_STOPWORDS or folded in seen:
            continue
        seen.add(folded)
        result.append(value)

    # Längere/eindeutigere Begriffe zuerst; maximal 12 Suchanker.
    result.sort(key=len, reverse=True)
    return result[:12]


def _relevant_windows(text: str, terms: list[str]) -> list[str]:
    if not text or not terms or _context_enrich_max_windows() <= 0:
        return []

    folded = text.casefold()
    half = max(300, _context_enrich_window_chars() // 2)
    candidates: list[tuple[int, int]] = []

    for term in terms:
        needle = term.casefold().strip()
        if not needle:
            continue
        start_at = 0
        # Pro Begriff höchstens zwei Fundstellen, damit ein häufiger Name nicht
        # alle Fenster belegt.
        for _ in range(2):
            pos = folded.find(needle, start_at)
            if pos < 0:
                break
            start = max(0, pos - half)
            end = min(len(text), pos + len(needle) + half)
            candidates.append((start, end))
            start_at = pos + len(needle)

    candidates.sort()
    selected: list[tuple[int, int]] = []
    for start, end in candidates:
        # Fenster, die weitgehend schon vom Dokumentanfang oder einem bereits
        # ausgewählten Fenster abgedeckt sind, nicht doppelt aufnehmen.
        if start < _context_enrich_head_chars() and end <= _context_enrich_head_chars() + 250:
            continue
        overlap = False
        for old_start, old_end in selected:
            common = max(0, min(end, old_end) - max(start, old_start))
            if common >= 0.55 * min(end - start, old_end - old_start):
                overlap = True
                break
        if overlap:
            continue
        selected.append((start, end))
        if len(selected) >= _context_enrich_max_windows():
            break

    return [text[start:end].strip() for start, end in selected if text[start:end].strip()]


def _graph_relevant_windows(
    text: str,
    terms: list[str],
    *,
    before_chars: int = GRAPH_RETRIEVAL_SNIPPET_BEFORE_CHARS,
    after_chars: int = GRAPH_RETRIEVAL_SNIPPET_AFTER_CHARS,
    max_windows: int | None = None,
) -> list[str]:
    """Select graph passages around the most discriminating entity mentions.

    Unlike the generic context windows, graph passages should not simply take
    the earliest occurrence in the document: company names often repeat in every
    header/footer while the person or relation-bearing mention occurs only once.
    Rare/long anchors are therefore preferred.  The window is deliberately
    asymmetric because letters commonly name the recipient first and express the
    relevant action afterwards.  If preserved page markers are present, a window
    is clipped to the current page rather than crossing into the next/previous
    letter.
    """
    if max_windows is None:
        max_windows = _context_enrich_max_windows()
    if not text or not terms or max_windows <= 0:
        return []

    # Use lower(), not casefold(): casefold can change string length (e.g. ß -> ss),
    # which would make occurrence offsets invalid for slicing the original text.
    folded = text.lower()
    page_marker = "[seitenwechsel]"
    candidates: list[tuple[float, int, int, str]] = []

    seen_terms: set[str] = set()
    for raw_term in terms:
        term = str(raw_term or "").strip()
        needle = term.casefold()
        if not needle or needle in seen_terms:
            continue
        seen_terms.add(needle)

        positions: list[int] = []
        start_at = 0
        while len(positions) < 12:
            pos = folded.find(needle, start_at)
            if pos < 0:
                break
            positions.append(pos)
            start_at = pos + max(1, len(needle))
        if not positions:
            continue

        # Rare and longer phrases are more discriminating than a company name
        # repeated in every page header/footer.
        frequency = len(positions)
        specificity = (100.0 / frequency) + min(len(needle), 60) * 0.35

        for pos in positions[:4]:
            start = max(0, pos - max(0, before_chars))
            end = min(len(text), pos + len(term) + max(0, after_chars))

            # Do not join two independent pages/letters if the extractor retained
            # a form-feed boundary which _clean_context_text converted to a marker.
            prev_page = folded.rfind(page_marker, start, pos)
            if prev_page >= 0:
                start = prev_page + len(page_marker)
            next_page = folded.find(page_marker, pos, end)
            if next_page >= 0:
                end = next_page

            candidates.append((specificity, start, end, needle))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    selected: list[tuple[int, int]] = []
    windows: list[str] = []

    for _score, start, end, _needle in candidates:
        if end <= start:
            continue
        overlap = False
        for old_start, old_end in selected:
            common = max(0, min(end, old_end) - max(start, old_start))
            if common >= 0.55 * min(end - start, old_end - old_start):
                overlap = True
                break
        if overlap:
            continue

        window = text[start:end].strip()
        if not window:
            continue
        selected.append((start, end))
        windows.append(window)
        if len(windows) >= max_windows:
            break

    return windows


def _fallback_context(item: dict) -> str:
    parts: list[str] = []
    if item.get("graph_indirect_chains"):
        parts.append(
            "Graph-Hinweis: indirekte dokumentgestützte Belegkette; "
            "kein Beleg für eine direkte Beziehung der Ausgangsentitäten."
        )
    es_text = _clean_context_text(item.get("es_snippet") or "")
    vector_text = _clean_context_text(item.get("vector_snippet") or "")

    if es_text:
        parts.append("Elasticsearch-Ausschnitt:\n" + es_text)
    if vector_text and vector_text.casefold() != es_text.casefold():
        parts.append("Semantischer Chunk:\n" + vector_text)

    return "\n\n".join(parts).strip()


def _build_enriched_context(item: dict, full_content: str, question: str, plan) -> str:
    """Build final answer context without hiding a graph-relevant passage.

    For ordinary hits the document head remains useful.  For a graph-ranked hit,
    however, the structural passage is the reason the document survived retrieval
    and must reach the answer model before a potentially ambiguous letterhead.
    """
    parts: list[str] = []

    content = _clean_context_text(full_content)
    graph_text = str(item.get("graph_snippet") or "").strip()
    graph_rank = item.get("graph_rank")
    indirect_chains = list(item.get("graph_indirect_chains") or [])

    if graph_rank is not None and indirect_chains:
        parts.append(
            "Graph-Hinweis: Die folgenden Treffer bilden ausschließlich eine indirekte, "
            "dokumentgestützte Belegkette über mindestens einen Brückenknoten. "
            "Sie sind KEIN Beleg für eine direkte Beziehung der beiden Ausgangsentitäten."
        )

    if graph_rank is not None and graph_text:
        parts.append("Graph-relevante Evidenz:\n" + graph_text)

    fallback = _fallback_context(item)
    if fallback:
        parts.append(fallback)

    head = ""
    if content:
        head = content[:_context_enrich_head_chars()].strip()

        # For graph hits, keep the head as secondary context.  For ordinary hits
        # preserve the historical behaviour and put it first.
        if head:
            if graph_rank is None or not graph_text:
                parts.insert(0, "Dokumentanfang:\n" + head)
            else:
                parts.append("Dokumentanfang (ergänzend):\n" + head)

        for window in _relevant_windows(content, _context_terms(question, plan)):
            if head and window.casefold() in head.casefold():
                continue
            if graph_text and window.casefold() in graph_text.casefold():
                continue
            parts.append("Weitere relevante Stelle:\n" + window)

    result = "\n\n".join(parts).strip()
    return result[:_context_enrich_max_chars()]



def _build_graph_retrieval_snippet(
    item: dict,
    full_content: str,
    question: str,
    plan,
) -> str:
    """Build a graph-focused snippet with relevant windows *before* the head.

    Graph retrieval is useful precisely because a structurally relevant passage
    may not be the passage Elasticsearch highlighted.  The generic enriched
    context starts with a large document head, which is good for the final answer
    but can consume almost the entire graph snippet budget.  For the retrieval
    and Evidence-Control stages we therefore put windows around query/entity
    mentions first and use the head only as a fallback.
    """
    content = _clean_context_text(full_content)
    if not content:
        return ""

    terms = list(_context_terms(question, plan))
    for entity_name in item.get("graph_entities") or []:
        value = str(entity_name or "").strip()
        if value:
            terms.append(value)
            # OCR/vCard display names can contain titles or punctuation.  Add
            # distinctive tokens as secondary anchors without creating any
            # identity assertion from them.
            terms.extend(
                token
                for token in re.findall(r"[\wÄÖÜäöüß-]{4,}", value, flags=re.UNICODE)
                if token.casefold() not in _CONTEXT_STOPWORDS
            )

    # Stable de-duplication, longer phrases first.
    seen: set[str] = set()
    unique_terms: list[str] = []
    for value in sorted(terms, key=lambda x: len(str(x)), reverse=True):
        value = str(value or "").strip()
        folded = value.casefold()
        if not value or folded in seen:
            continue
        seen.add(folded)
        unique_terms.append(value)

    parts: list[str] = []
    for window in _graph_relevant_windows(
        content,
        unique_terms[:16],
        max_windows=_context_enrich_max_windows(),
    ):
        parts.append("Graph-relevante Stelle:\n" + window)

    # Only if no useful window exists do we fall back to the beginning.  This
    # avoids the old 2200-char-head / 2400-char-snippet truncation pathology.
    if not parts:
        parts.append("Dokumentanfang:\n" + content[:_context_enrich_head_chars()])

    return "\n\n".join(parts).strip()[:_graph_retrieval_snippet_chars()]


def hydrate_graph_results(graph_results: list[dict], question: str, plan, timings: dict) -> None:
    """Give graph candidates focused document text for reranking/evidence review.

    Neo4j intentionally stores no document body. For the top graph candidates
    fetch ES content by document ID.  Unlike the final answer context, the graph
    snippet prioritises windows around the query entities so the structural
    reason for retrieval survives the compact review budget.
    """
    started = time.perf_counter()
    if not graph_results or _graph_retrieval_hydrate_limit() <= 0:
        timings["graph_hydration"] = time.perf_counter() - started
        return

    selected = graph_results[:_graph_retrieval_hydrate_limit()]
    ids = [str(item.get("document_id") or "") for item in selected]
    ids = [value for value in ids if value]
    if not ids:
        timings["graph_hydration"] = time.perf_counter() - started
        return

    try:
        response = _es_post(
            f"{ES_URL}/{ES_INDEX}/_mget",
            json={
                "docs": [
                    {"_id": document_id, "_source": ["content"]}
                    for document_id in ids
                ]
            },
            timeout=ES_TIMEOUT,
        )
        response.raise_for_status()
        docs = {
            str(doc.get("_id")): doc
            for doc in response.json().get("docs", [])
            if doc.get("found")
        }
        for item in selected:
            doc = docs.get(str(item.get("document_id") or "")) or {}
            content = str((doc.get("_source") or {}).get("content") or "")
            if not content:
                continue
            hydrated_snippet = _build_graph_retrieval_snippet(
                item,
                content,
                question,
                plan,
            )
            existing_snippet = str(item.get("graph_snippet") or "").strip()
            if existing_snippet and hydrated_snippet:
                item["graph_snippet"] = (
                    "Explizit extrahierte Relationspassage:\n" + existing_snippet
                    + "\n\n" + hydrated_snippet
                )[:_graph_retrieval_snippet_chars()]
            elif hydrated_snippet:
                item["graph_snippet"] = hydrated_snippet
    except Exception as exc:
        print(
            "[RAG] WARNUNG graph_hydration: "
            f"{type(exc).__name__}: {exc}; Graph-Kandidaten bleiben titelbasiert",
            flush=True,
        )

    elapsed = time.perf_counter() - started
    timings["graph_hydration"] = elapsed
    hydrated = sum(1 for item in selected if item.get("graph_snippet"))
    print(
        f"[RAG] graph_hydration: {elapsed:.3f} s "
        f"({hydrated}/{len(selected)} Kandidaten mit Text)",
        flush=True,
    )

def enrich_final_results(final_results: list[dict], question: str, plan, timings: dict) -> None:
    started = time.perf_counter()

    # Immer einen brauchbaren Fallback setzen, auch wenn Anreicherung deaktiviert
    # oder Elasticsearch kurzzeitig nicht verfügbar ist.
    for item in final_results:
        item["context_text"] = _fallback_context(item)
        item["context_enriched"] = False

    if not _context_enrich_enabled() or not final_results:
        timings["context_enrichment"] = time.perf_counter() - started
        return

    ids = [str(item.get("document_id") or "") for item in final_results]
    ids = [value for value in ids if value]
    if not ids:
        timings["context_enrichment"] = time.perf_counter() - started
        return

    endpoint = f"{ES_URL}/{ES_INDEX}/_mget"

    try:
        response = _es_post(
            endpoint,
            json={
                "docs": [
                    {"_id": document_id, "_source": ["content"]}
                    for document_id in ids
                ]
            },
            timeout=ES_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        docs = {
            str(doc.get("_id")): doc
            for doc in data.get("docs", [])
            if doc.get("found")
        }

        for item in final_results:
            doc = docs.get(str(item.get("document_id") or "")) or {}
            source = doc.get("_source") or {}
            content = source.get("content") or ""
            if content:
                item["context_text"] = _build_enriched_context(
                    item,
                    str(content),
                    question,
                    plan,
                )
                item["context_enriched"] = True

    except Exception as exc:
        print(
            "[RAG] WARNUNG context_enrichment: "
            f"{type(exc).__name__}: {exc}; verwende Snippet-Fallback",
            flush=True,
        )

    elapsed = time.perf_counter() - started
    timings["context_enrichment"] = elapsed
    enriched = sum(1 for item in final_results if item.get("context_enriched"))
    print(
        f"[RAG] context_enrichment: {elapsed:.3f} s "
        f"({enriched}/{len(final_results)} Dokumente vertieft)",
        flush=True,
    )



def resolve_document_references(
    references: list[str],
    *,
    question: str = "",
) -> dict:
    """Resolve explicit /use references without planner/RRF/reranking.

    ``files:<id>`` is exact by document id.  A value containing ``/`` is an
    exact path (leading slash optional).  Every other value is an exact basename.
    Ambiguous basename/path matches are returned to the caller instead of being
    guessed.
    """
    resolved: list[dict] = []
    ambiguous: list[dict] = []
    not_found: list[str] = []
    seen_ids: set[str] = set()

    for raw_reference in references:
        reference = str(raw_reference or "").strip()
        if not reference:
            continue

        if reference.startswith("files:"):
            matches = document_id_lookup(reference)
        elif "/" in reference:
            matches = exact_path_lookup(reference)
        else:
            matches = strict_filename_lookup(reference)

        if not matches:
            not_found.append(reference)
            continue

        if len(matches) > 1:
            ambiguous.append({
                "reference": reference,
                "matches": [
                    {
                        "document_id": item.get("document_id"),
                        "path": item.get("path") or item.get("title"),
                        "title": item.get("title"),
                        "source_url": item.get("source_url"),
                    }
                    for item in matches[:20]
                ],
            })
            continue

        item = dict(matches[0])
        document_id = str(item.get("document_id") or "").strip()
        if not document_id or document_id in seen_ids:
            continue
        seen_ids.add(document_id)
        resolved.append(item)

    # Explicitly selected documents get a generous direct-text context.  The
    # provider still applies its own /use per-document and total context caps.
    if resolved:
        ids = [str(item.get("document_id") or "") for item in resolved]
        try:
            response = _es_post(
                f"{ES_URL}/{ES_INDEX}/_mget",
                json={
                    "docs": [
                        {"_id": document_id, "_source": ["content"]}
                        for document_id in ids
                    ]
                },
                timeout=ES_TIMEOUT,
            )
            response.raise_for_status()
            docs = {
                str(doc.get("_id")): doc
                for doc in response.json().get("docs", [])
                if doc.get("found")
            }
            for item in resolved:
                doc = docs.get(str(item.get("document_id") or "")) or {}
                content = str((doc.get("_source") or {}).get("content") or "")
                cleaned = _clean_context_text(content)
                if cleaned:
                    item["context_text"] = cleaned[:DOCUMENT_USE_MAX_CHARS]
                    item["context_enriched"] = True
                    item["content_available"] = True
                else:
                    item["content_available"] = False
        except Exception as exc:
            print(
                "[RAG] WARNUNG document_use_hydration: "
                f"{type(exc).__name__}: {exc}; verwende Lookup-Metadaten",
                flush=True,
            )

    for rank, item in enumerate(resolved, start=1):
        item["final_rank"] = rank
        item["rank"] = rank
    add_source_urls(resolved)

    return {
        "references": list(references),
        "resolved_count": len(resolved),
        "ambiguous": ambiguous,
        "not_found": not_found,
        "results": resolved,
    }


# ------------------------------------------------------------
# Gesamte Suchpipeline
# ------------------------------------------------------------

_BACKEND_DISPLAY_NAMES = {
    "files": "Dokumentensuche",
    "vector": "Vektorsuche",
    "graph": "Graph-Suche",
}


def _degraded_retrieval_message(
    backend_status: dict[str, dict],
    degraded: list[str],
) -> str:
    """Describe degraded retrieval without implying nonexistent fallback arms."""
    unavailable = ", ".join(_BACKEND_DISPLAY_NAMES.get(name, name) for name in degraded)
    healthy_requested = [
        name
        for name, state in backend_status.items()
        if state.get("requested")
        and state.get("enabled", True)
        and state.get("available")
    ]
    if healthy_requested:
        return (
            f"Nicht verfügbar: {unavailable}. "
            "Die übrigen verfügbaren Suchdienste wurden weiterhin verwendet, "
            "lieferten für diese Anfrage jedoch keinen verwertbaren Treffer."
        )
    return (
        f"Nicht verfügbar: {unavailable}. "
        "Für diese Anfrage stand kein weiterer Suchdienst zur Verfügung."
    )


def perform_search(
    question: str,
    limit: int = FINAL_LIMIT,
    *,
    entity_recall: bool = False,
    retrieval_arms: list[str] | set[str] | tuple[str, ...] | None = None,
    raw_results: bool = False,
    force_unspecific: bool = False,
    semantic_query_override: str | None = None,
    search_spec: dict | None = None,
    entity_context_override: dict | None = None,
    source_scopes=None,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
):

    # ``retrieval_arms`` controls only the document-retrieval arms. Entity
    # resolution/planning still runs because /files may profit from known aliases
    # and /graph requires resolved entity ids.  Omitting the parameter preserves
    # the normal hybrid behaviour.
    allowed_arms = {"files", "vector", "graph"}
    if retrieval_arms is None:
        active_arms = set(allowed_arms)
        explicit_arm_selection = False
    else:
        active_arms = {str(value).strip().lower() for value in retrieval_arms if str(value).strip()}
        invalid_arms = active_arms - allowed_arms
        if invalid_arms:
            raise ValueError(
                "Unbekannte Retrieval-Arme: " + ", ".join(sorted(invalid_arms))
            )
        if not active_arms:
            active_arms = set(allowed_arms)
            explicit_arm_selection = False
        else:
            explicit_arm_selection = True

    normalized_source_scopes = normalize_source_scopes(source_scopes)
    acl_prefilter_active = bool(acl_prefilter_user and acl_prefilter_groups is not None)

    total_started = (
        time.perf_counter()
    )

    timings = {}


    print()
    print(
        "[RAG] ========================================",
        flush=True,
    )

    print(
        f"[RAG] Anfrage: {question}",
        flush=True,
    )
    print(
        f"[RAG] Retrieval-Arme: {','.join(sorted(active_arms))}; "
        f"raw_results={bool(raw_results)}; force_unspecific={bool(force_unspecific)}; "
        f"source_scopes={sorted(normalized_source_scopes) if normalized_source_scopes else 'default'}",
        flush=True,
    )


    try:

        # ----------------------------------------------------
        # 0. Exakter Dateinamen-Lookup für reine Navigationsfragen
        # ----------------------------------------------------

        requested_filename = extract_filename(question)

        if "files" in active_arms and ELASTICSEARCH_ENABLED and is_pure_filename_lookup(question, requested_filename):

            exact_results = run_timed(
                "filename_lookup",
                lambda: (
                    filename_lookup(
                        requested_filename,
                        limit=limit,
                        source_scopes=normalized_source_scopes,
                        acl_prefilter_user=acl_prefilter_user,
                        acl_prefilter_groups=acl_prefilter_groups,
                    )
                    if acl_prefilter_active
                    else filename_lookup(
                        requested_filename,
                        limit=limit,
                        source_scopes=normalized_source_scopes,
                    )
                ),
                timings,
            )
            exact_results = [
                item for item in exact_results
                if source_scope_allows_record(
                    str(item.get("document_id") or ""),
                    str(item.get("path") or item.get("title") or ""),
                    normalized_source_scopes,
                    item.get("source_origin"),
                )
            ][:limit]

            total_elapsed = time.perf_counter() - total_started
            timings["total"] = total_elapsed

            retrieval_mode = (
                "filename_exact"
                if exact_results
                else "filename_not_found"
            )

            print(
                f"[RAG] filename_lookup: "
                f"{requested_filename} -> "
                f"{len(exact_results)} Treffer",
                flush=True,
            )
            print(
                f"[RAG] GESAMT: {total_elapsed:.3f} s",
                flush=True,
            )
            print(
                "[RAG] ========================================",
                flush=True,
            )
            print()

            return {
                "question": question,
                "plan": None,
                "entity_resolution": {
                    "enabled": _entity_resolution_enabled(),
                    "entities": [],
                    "elastic_phrase_expansion": [],
                    "error": None,
                },
                "retrieval_mode": retrieval_mode,
                "retrieval_strategy": "filename_exact",
                "source_scopes": sorted(normalized_source_scopes) if normalized_source_scopes else None,
                "retrieval_message": "",
                "retrieval_signal": {},
                "lookup_filename": requested_filename,
                "reranker_used": False,
                "reranker_error": None,
                "timings": timings,
                "statistics": {
                    "filename_matches": len(exact_results),
                    "elasticsearch_candidates": len(exact_results),
                    "vector_candidates": 0,
                    "rrf_candidates": 0,
                    "deduplicated_candidates": len(exact_results),
                    "duplicate_variants_suppressed": 0,
                    "reranker_candidates": 0,
                    "returned": len(exact_results),
                },
                "results": exact_results,
            }

        # ----------------------------------------------------
        # 1. Entity Detection / Resolution (Neo4j)
        # ----------------------------------------------------

        if entity_context_override is not None:
            entity_context = dict(entity_context_override)
            timings["entity_resolution"] = 0.0
            log.debug("[RAG] entity_resolution reused provider query context")
        else:
            entity_context = run_timed(
                "entity_resolution",
                lambda: prepare_entity_context(question),
                timings,
            )

        entity_count = len(entity_context.get("entities") or [])
        expansion_count = len(entity_context.get("elastic_phrase_expansion") or [])
        if entity_count or entity_context.get("error"):
            print(
                f"[RAG] Entities: {entity_count}; "
                f"ES-Expansionen: {expansion_count}; "
                f"error={entity_context.get('error')}",
                flush=True,
            )


        # ----------------------------------------------------
        # 2. Planner
        # ----------------------------------------------------

        if search_spec is not None:
            plan = run_timed(
                "planner",
                lambda: plan_from_search_spec(
                    search_spec,
                    entity_context=entity_context,
                    original_query=question,
                    entity_recall=entity_recall,
                ),
                timings,
            )
            log.info(
                "query intent effective: lexical_groups=%d legacy_elastic_present=%s semantic_present=%s neo4j_expansions=%d",
                len(getattr(plan, "lexical_groups", []) or []),
                bool(str(getattr(plan, "elastic_query", "") or "").strip()),
                bool(str(getattr(plan, "semantic_query", "") or "").strip()),
                len(getattr(plan, "entity_should_phrases", []) or []),
            )
        else:
            plan = run_timed(
                "planner",
                lambda: create_plan(
                    question,
                    entity_context=entity_context,
                    entity_recall=entity_recall,
                ),
                timings,
            )
        if semantic_query_override is not None:
            semantic_override = str(semantic_query_override or "").strip()
            if semantic_override:
                # RC8 multi-probe retrieval may use strict Boolean syntax for ES,
                # while Qdrant must continue to receive natural semantic text.
                plan.semantic_query = semantic_override


        # ----------------------------------------------------
        # 3. Elasticsearch
        # ----------------------------------------------------

        es_diagnostics: dict = {}
        vector_diagnostics: dict = {}

        if "files" in active_arms and ELASTICSEARCH_ENABLED:
            try:
                es_results = run_timed(
                    "elasticsearch",
                    lambda: (
                        (
                            elastic_search(
                                question,
                                plan,
                                es_diagnostics,
                                acl_prefilter_user=acl_prefilter_user,
                                acl_prefilter_groups=acl_prefilter_groups,
                            )
                            if normalized_source_scopes is None
                            else elastic_search(
                                question,
                                plan,
                                es_diagnostics,
                                source_scopes=normalized_source_scopes,
                                acl_prefilter_user=acl_prefilter_user,
                                acl_prefilter_groups=acl_prefilter_groups,
                            )
                        )
                        if acl_prefilter_active
                        else (
                            elastic_search(question, plan, es_diagnostics)
                            if normalized_source_scopes is None
                            else elastic_search(
                                question, plan, es_diagnostics, source_scopes=normalized_source_scopes
                            )
                        )
                    ),
                    timings,
                )
                es_diagnostics.setdefault("available", True)
            except Exception as exc:
                es_results = []
                es_diagnostics = {
                    "available": False,
                    "mode": "backend_unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                timings.setdefault("elasticsearch", 0.0)
                remaining_arms = sorted(active_arms - {"files"})
                suffix = (
                    f"; fahre mit verbleibenden Retrieval-Armen fort: {','.join(remaining_arms)}"
                    if remaining_arms
                    else "; kein weiterer Retrieval-Arm aktiv"
                )
                print(
                    f"[RAG] WARNUNG Elasticsearch nicht verfügbar: {type(exc).__name__}: {exc}{suffix}",
                    flush=True,
                )
        else:
            es_results = []
            es_diagnostics = {
                "disabled_by_request": "files" not in active_arms,
                "disabled_by_config": not ELASTICSEARCH_ENABLED,
                "available": False,
            }
            timings["elasticsearch"] = 0.0

        # ----------------------------------------------------
        # 2b. Broad entity navigation guard
        #
        # A bare entity whose ES total exceeds the normal candidate window is
        # not a meaningful top-k ranking task.  Stop before vector/RRF/reranker.
        # Neo4j may contribute document ids solely as ACL probes; the API turns
        # those into a boolean "accessible information exists" signal.
        # ----------------------------------------------------
        es_total_hits = int(es_diagnostics.get("total_hits") or 0)
        broad_entity_guard = (
            "files" in active_arms
            and not raw_results
            and not force_unspecific
            and bool(es_diagnostics.get("available", True))
            and es_total_hits > _es_limit()
            and is_broad_entity_query(question, entity_context)
        )
        if broad_entity_guard:
            graph_diagnostics: dict = {}
            if "graph" in active_arms:
                orientation_candidates = run_timed(
                    "graph_orientation",
                    lambda: (
                        graph_search(entity_context, graph_diagnostics)
                        if normalized_source_scopes is None
                        else graph_search(
                            entity_context, graph_diagnostics, source_scopes=normalized_source_scopes
                        )
                    ),
                    timings,
                )
            else:
                orientation_candidates = []
                graph_diagnostics = {"enabled": False, "mode": "disabled_by_request", "count": 0}
                timings["graph_orientation"] = 0.0

            timings["embedding"] = 0.0
            timings["qdrant"] = 0.0
            timings["retrieval_signal"] = 0.0
            timings["reranker"] = 0.0
            total_elapsed = time.perf_counter() - total_started
            timings["total"] = total_elapsed
            message = (
                "Die Anfrage ist zu unspezifisch, um zielführend beantwortet werden zu können. "
                "Bitte grenzen Sie die Suche nach Zeitraum, Person, Organisation oder Vorgang ein."
            )
            print("[RAG] Abbruch vor Vector/RRF/Reranker: breite Entity-Anfrage.", flush=True)
            return {
                "question": question,
                "plan": plan,
                "entity_resolution": entity_context,
                "search_spec": dict(search_spec or {}),
                "retrieval_mode": "too_unspecific",
                "retrieval_strategy": "too_unspecific",
                "source_scopes": sorted(normalized_source_scopes) if normalized_source_scopes else None,
                "retrieval_message": message,
                "retrieval_signal": {},
                "orientation_candidates": orientation_candidates,
                "reranker_used": False,
                "reranker_error": None,
                "timings": timings,
                "statistics": {
                    "elasticsearch_candidates": len(es_results),
                    "elasticsearch_total_hits": es_total_hits,
                    "graph_orientation_candidates": len(orientation_candidates),
                    "returned": 0,
                },
                "results": [],
            }


        # ----------------------------------------------------
        # 3. Embedding + Qdrant
        # ----------------------------------------------------

        if "vector" in active_arms and QDRANT_ENABLED:
            try:
                vector_results = (
                    (
                        vector_search(
                            plan,
                            timings,
                            vector_diagnostics,
                            acl_prefilter_user=acl_prefilter_user,
                            acl_prefilter_groups=acl_prefilter_groups,
                        )
                        if normalized_source_scopes is None
                        else vector_search(
                            plan,
                            timings,
                            vector_diagnostics,
                            source_scopes=normalized_source_scopes,
                            acl_prefilter_user=acl_prefilter_user,
                            acl_prefilter_groups=acl_prefilter_groups,
                        )
                    )
                    if acl_prefilter_active
                    else (
                        vector_search(plan, timings, vector_diagnostics)
                        if normalized_source_scopes is None
                        else vector_search(
                            plan, timings, vector_diagnostics, source_scopes=normalized_source_scopes
                        )
                    )
                )
                vector_diagnostics.setdefault("available", True)
            except Exception as exc:
                vector_results = []
                vector_diagnostics = {
                    "available": False,
                    "mode": "backend_unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                    "raw_results": 0,
                    "above_threshold": 0,
                }
                timings.setdefault("embedding", 0.0)
                timings.setdefault("qdrant", 0.0)
                print(
                    f"[RAG] WARNUNG Vector-Arm nicht verfügbar: {type(exc).__name__}: {exc}; "
                    "fahre mit verbleibenden Retrieval-Armen fort",
                    flush=True,
                )
        else:
            vector_results = []
            vector_diagnostics = {
                "disabled_by_request": "vector" not in active_arms,
                "disabled_by_config": not QDRANT_ENABLED,
                "available": False,
            }
            timings["embedding"] = 0.0
            timings["qdrant"] = 0.0


        # ----------------------------------------------------
        # 3b. Neo4j als dritter Retrieval-Arm
        # ----------------------------------------------------

        graph_diagnostics: dict = {}
        if "graph" in active_arms:
            graph_results = run_timed(
                "graph_retrieval",
                lambda: (
                    graph_search(entity_context, graph_diagnostics)
                    if normalized_source_scopes is None
                    else graph_search(
                        entity_context, graph_diagnostics, source_scopes=normalized_source_scopes
                    )
                ),
                timings,
            )

            if raw_results:
                # /list /graph must remain a genuine graph-only document lookup.
                # Hydration through ES is needed only when a downstream reranker
                # or answer model needs document text.
                timings["graph_hydration"] = 0.0
            else:
                hydrate_graph_results(
                    graph_results,
                    question,
                    plan,
                    timings,
                )
        else:
            graph_results = []
            graph_diagnostics = {
                "enabled": bool(_graph_retrieval_enabled()),
                "available": False,
                "mode": "disabled_by_request",
                "count": 0,
            }
            timings["graph_retrieval"] = 0.0
            timings["graph_hydration"] = 0.0


        # ----------------------------------------------------
        # 4. Dubletten VOR der Signalmessung
        #
        # Die Scorekurve soll die Relevanzstruktur messen und nicht die
        # Ablagestruktur. ODT/PDF-Fassungen oder wiederholt abgelegte
        # identische Dokumente zählen daher pro Retrieval-Arm nur einmal.
        # ----------------------------------------------------

        es_unique, vector_unique = run_timed(
            "retrieval_dedup",
            lambda: (
                deduplicate_retrieval_arm(es_results, "es"),
                deduplicate_retrieval_arm(vector_results, "vector"),
            ),
            timings,
        )

        es_arm_duplicates = sum(
            len(item.get("duplicate_variants", []))
            for item in es_unique
        )
        vector_arm_duplicates = sum(
            len(item.get("duplicate_variants", []))
            for item in vector_unique
        )


        # ----------------------------------------------------
        # 5. Retrieval-Signal messen und Strategie wählen
        # ----------------------------------------------------

        explicit_es_anchor = bool(
            getattr(plan, "must", None)
            or getattr(plan, "phrases", None)
            or getattr(plan, "entity_should_phrases", None)
        )

        if _retrieval_signal_enabled():

            def _measure_retrieval_signal():
                es_profile = analyze_score_curve(
                    (item.get("score") for item in es_unique),
                    total_hits=es_diagnostics.get("total_hits"),
                    total_relation=es_diagnostics.get("total_relation"),
                    explicit_anchor=explicit_es_anchor,
                    head_size=_retrieval_signal_head_size(),
                    tail_fraction=_retrieval_signal_tail_fraction(),
                    min_curve_candidates=_retrieval_signal_min_curve_candidates(),
                    small_field_signal=_retrieval_signal_small_field(),
                    explicit_anchor_signal=_retrieval_signal_explicit_anchor(),
                )
                vector_profile = analyze_score_curve(
                    (item.get("score") for item in vector_unique),
                    explicit_anchor=False,
                    head_size=_retrieval_signal_head_size(),
                    tail_fraction=_retrieval_signal_tail_fraction(),
                    min_curve_candidates=_retrieval_signal_min_curve_candidates(),
                    small_field_signal=_retrieval_signal_small_field(),
                    explicit_anchor_signal=_retrieval_signal_explicit_anchor(),
                )
                overlap = overlap_at_k(
                    (item.get("document_id") for item in es_unique),
                    (item.get("document_id") for item in vector_unique),
                    _retrieval_signal_overlap_k(),
                )
                decision = choose_retrieval_strategy(
                    es_profile,
                    vector_profile,
                    unspecific_signal_floor=_retrieval_signal_floor(),
                    dominance_ratio=_retrieval_signal_dominance_ratio(),
                    dominance_margin=_retrieval_signal_dominance_margin(),
                    secondary_weight_floor=_retrieval_signal_secondary_weight_floor(),
                )
                return {
                    "elasticsearch": es_profile,
                    "vector": vector_profile,
                    "graph": dict(graph_diagnostics),
                    "overlap": overlap,
                    "decision": decision,
                }

            retrieval_signal = run_timed(
                "retrieval_signal",
                _measure_retrieval_signal,
                timings,
            )

        else:
            retrieval_signal = {
                "elasticsearch": {
                    "available": bool(es_unique),
                    "count": len(es_unique),
                    "total_hits": es_diagnostics.get("total_hits"),
                    "total_relation": es_diagnostics.get("total_relation"),
                },
                "vector": {
                    "available": bool(vector_unique),
                    "count": len(vector_unique),
                },
                "graph": dict(graph_diagnostics),
                "overlap": overlap_at_k(
                    (item.get("document_id") for item in es_unique),
                    (item.get("document_id") for item in vector_unique),
                    _retrieval_signal_overlap_k(),
                ),
                "decision": {
                    "strategy": "fusion",
                    "es_weight": 1.0,
                    "vector_weight": 1.0,
                    "reason": "Retrieval-Signalanalyse ist deaktiviert.",
                },
            }
            timings["retrieval_signal"] = 0.0

        strategy_decision = retrieval_signal["decision"]
        observed_strategy = str(strategy_decision.get("strategy") or "fusion")
        retrieval_strategy = observed_strategy
        es_weight = float(strategy_decision.get("es_weight") or 0.0)
        vector_weight = float(strategy_decision.get("vector_weight") or 0.0)
        graph_weight = graph_retrieval_weight(graph_diagnostics, graph_results)
        retrieval_signal.setdefault("graph", dict(graph_diagnostics))
        retrieval_signal["graph"]["weight"] = graph_weight

        # Ein belastbarer Graph-Treffer darf ein ansonsten flaches ES/Vector-
        # Feld retten. Die klassischen Arme bleiben als schwache Ergänzung
        # erhalten; der Graph ersetzt aber niemals Reranker/Evidence Control.
        if observed_strategy == "unspecific" and graph_results:
            retrieval_strategy = "graph_supported"
            es_weight = max(es_weight, 0.20 if es_unique else 0.0)
            vector_weight = max(vector_weight, 0.15 if vector_unique else 0.0)
            strategy_decision["applied_strategy"] = "graph_supported"
            strategy_decision["observe_only"] = False

        # Beobachtungsmodus: ein als unspezifisch erkanntes Feld wird geloggt,
        # aber noch nicht abgebrochen. In diesem Fall die bestehende neutrale
        # Fusion anwenden statt versehentlich mit Gewicht 0/0 zu fusionieren.
        elif (
            observed_strategy == "unspecific"
            and (force_unspecific or not _retrieval_signal_reject_unspecific())
        ):
            retrieval_strategy = "forced_fusion" if force_unspecific else "fusion"
            es_weight = 1.0 if es_unique else 0.0
            vector_weight = 1.0 if vector_unique else 0.0
            strategy_decision["applied_strategy"] = retrieval_strategy
            strategy_decision["observe_only"] = not force_unspecific
            strategy_decision["forced"] = bool(force_unspecific)
        else:
            strategy_decision["applied_strategy"] = retrieval_strategy
            strategy_decision["observe_only"] = False

        # /list is intentionally diagnostic: no quality model should decide
        # which retrieval arm is more important.  Use equal-weight RRF among
        # the explicitly active arms; with one arm this preserves native rank.
        if raw_results:
            retrieval_strategy = "raw_fusion"
            es_weight = 1.0 if ("files" in active_arms and es_unique) else 0.0
            vector_weight = 1.0 if ("vector" in active_arms and vector_unique) else 0.0
            graph_weight = 1.0 if ("graph" in active_arms and graph_results) else 0.0
            strategy_decision["applied_strategy"] = retrieval_strategy
            strategy_decision["observe_only"] = True

        # Explicit selectors are hard retrieval boundaries.  The normal
        # signal analysis may choose weights freely among active arms, but an
        # arm that the user disabled must never contribute to fusion.
        if "files" not in active_arms:
            es_weight = 0.0
        if "vector" not in active_arms:
            vector_weight = 0.0
        if "graph" not in active_arms:
            graph_weight = 0.0
        retrieval_signal.setdefault("selection", {})
        retrieval_signal["selection"] = {
            "explicit": explicit_arm_selection,
            "active_arms": sorted(active_arms),
            "raw_results": bool(raw_results),
            "force_unspecific": bool(force_unspecific),
        }

        print(
            "[RAG] Retrieval-Signal ES: "
            + json.dumps(
                retrieval_signal["elasticsearch"],
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        print(
            "[RAG] Retrieval-Signal Vector: "
            + json.dumps(
                retrieval_signal["vector"],
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        print(
            "[RAG] Retrieval-Signal Graph: "
            + json.dumps(
                retrieval_signal.get("graph", {}),
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        print(
            "[RAG] Retrieval-Strategie: "
            f"{retrieval_strategy}; "
            f"weights=ES:{es_weight:.3f}/Vector:{vector_weight:.3f}/Graph:{graph_weight:.3f}; "
            f"overlap={retrieval_signal['overlap']}; "
            f"reason={strategy_decision.get('reason', '')}",
            flush=True,
        )

        if (
            _retrieval_signal_reject_unspecific()
            and not raw_results
            and not force_unspecific
            and observed_strategy == "unspecific"
            and not graph_results
        ):
            total_elapsed = time.perf_counter() - total_started
            timings["total"] = total_elapsed
            print(
                "[RAG] Abbruch vor RRF/Reranker: unspezifisches Trefferbild.",
                flush=True,
            )
            print(
                f"[RAG] GESAMT: {total_elapsed:.3f} s",
                flush=True,
            )
            print(
                "[RAG] ========================================",
                flush=True,
            )
            print()
            return {
                "question": question,
                "plan": plan,
                "entity_resolution": entity_context,
                "search_spec": dict(search_spec or {}),
                "retrieval_mode": "unspecific",
                "retrieval_strategy": retrieval_strategy,
                "retrieval_message": UNSPECIFIC_RETRIEVAL_MESSAGE,
                "retrieval_signal": retrieval_signal,
                "reranker_used": False,
                "reranker_error": None,
                "timings": timings,
                "statistics": {
                    "elasticsearch_candidates": len(es_results),
                    "elasticsearch_unique": len(es_unique),
                    "elasticsearch_total_hits": es_diagnostics.get("total_hits"),
                    "elasticsearch_total_relation": es_diagnostics.get("total_relation"),
                    "vector_candidates": len(vector_results),
                    "vector_unique": len(vector_unique),
                    "vector_raw_results": vector_diagnostics.get("raw_results", 0),
                    "vector_above_threshold": vector_diagnostics.get("above_threshold", 0),
                    "graph_candidates": len(graph_results),
                    "graph_mode": graph_diagnostics.get("mode"),
                    "retrieval_duplicates_suppressed": (
                        es_arm_duplicates + vector_arm_duplicates
                    ),
                    "rrf_candidates": 0,
                    "deduplicated_candidates": 0,
                    "duplicate_variants_suppressed": 0,
                    "reranker_candidates": 0,
                    "returned": 0,
                },
                "results": [],
            }


        # ----------------------------------------------------
        # 6. Dynamisch gewichtete RRF-Fusion
        # ----------------------------------------------------

        fused_results = run_timed(
            "rrf",
            lambda: fuse_results(
                es_unique,
                vector_unique,
                graph_results,
                es_weight=es_weight,
                vector_weight=vector_weight,
                graph_weight=graph_weight,
            ),
            timings,
        )

        for rank, item in enumerate(
            fused_results,
            start=1,
        ):
            item["rrf_rank"] = rank


        # ----------------------------------------------------
        # 7. Cross-Arm-Dubletten-Gruppierung vor dem Reranker
        #
        # Die Einzelarme wurden bereits vor der Signalmessung bereinigt.
        # Dieser zweite, billige Durchlauf fängt unterschiedliche
        # Repräsentanten derselben Dokumentfamilie zwischen ES und Vector ab.
        # ----------------------------------------------------

        candidate_pool_limit = (
            max(1, int(limit))
            if (raw_results or search_spec is not None)
            else _rerank_candidates()
        )
        rerank_candidates = run_timed(
            "dedup",
            lambda: deduplicate_for_reranker(
                fused_results,
                candidate_pool_limit,
                preserve_per_arm=0 if raw_results else 1,
            ),
            timings,
        )

        duplicate_variants_suppressed = sum(
            len(item.get("duplicate_variants", []))
            for item in rerank_candidates
        )

        print(
            f"[RAG] dedup: "
            f"ES {len(es_results)} -> {len(es_unique)}, "
            f"Vector {len(vector_results)} -> {len(vector_unique)}, "
            f"Graph {len(graph_results)}, "
            f"RRF {len(fused_results)} -> "
            f"{len(rerank_candidates)} eindeutige Reranker-Kandidaten; "
            f"{duplicate_variants_suppressed} Dublettenvarianten mitgeführt",
            flush=True,
        )


        # ----------------------------------------------------
        # 6. Reranker
        #
        # Nur diese Stufe ist optional.
        # Bei Fehler erfolgt RRF-Fallback.
        # ----------------------------------------------------

        reranker_used = False
        reranker_error = None

        reranker_started = (
            time.perf_counter()
        )


        if raw_results:
            # /list: retrieval-only diagnostics.  Do not invoke the cross-encoder
            # or any evidence/answer model.  The order is pure RRF (or the native
            # arm order when only one arm is active, which is equivalent).
            final_results = [dict(item) for item in rerank_candidates[:limit]]
            reranker_used = False
            reranker_error = None
            retrieval_mode = "raw_list"
        elif not _reranker_enabled():
            # super-light profile: preserve deterministic retrieval order and do
            # not import/load a local CrossEncoder or call TEI at all.
            final_results = rrf_fallback(rerank_candidates, limit)
            reranker_used = False
            reranker_error = None
            retrieval_mode = "rrf_no_reranker"
        else:
            try:

                rerank_limit = (
                    max(_rerank_candidates(), int(limit))
                    if search_spec is not None
                    else _rerank_candidates()
                )
                final_results = rerank_results(
                    query=question,
                    results=rerank_candidates,
                    candidate_limit=rerank_limit,
                    top_k=limit,
                    min_score=_rerank_min_score(),
                    config_override=_runtime_reranker_config(),
                )


                if (
                    rerank_candidates
                    and not final_results
                ):

                    raise RuntimeError(
                        "Reranker lieferte keine "
                        "verwendbaren Ergebnisse"
                    )


                reranker_used = True
                retrieval_mode = "reranked"


            except Exception as exc:

                reranker_error = (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )


                print(
                    f"[RAG] WARNUNG Reranker: "
                    f"{reranker_error}",
                    flush=True,
                )

                print(
                    "[RAG] Verwende RRF-Fallback.",
                    flush=True,
                )


                final_results = rrf_fallback(
                    rerank_candidates,
                    limit,
                )


                retrieval_mode = (
                    "rrf_fallback"
                )


        reranker_elapsed = (
            time.perf_counter()
            - reranker_started
        )

        timings[
            "reranker"
        ] = reranker_elapsed


        print(
            f"[RAG] reranker: "
            f"{reranker_elapsed:.3f} s "
            f"(used={reranker_used})",
            flush=True,
        )


        # ----------------------------------------------------
        # Endgültige Ränge
        # ----------------------------------------------------

        for rank, item in enumerate(
            final_results,
            start=1,
        ):

            item["final_rank"] = rank


        # ----------------------------------------------------
        # Deterministische Nextcloud-Quellenlinks
        # ----------------------------------------------------

        add_source_urls(final_results)

        # ----------------------------------------------------
        # 7. Kontext-Anreicherung nur für finale Dokumente
        # ----------------------------------------------------

        if raw_results:
            timings["context_enrichment"] = 0.0
        else:
            enrich_final_results(
                final_results,
                question,
                plan,
                timings,
            )


        # ----------------------------------------------------
        # Gesamtzeit
        # ----------------------------------------------------

        total_elapsed = (
            time.perf_counter()
            - total_started
        )

        timings["total"] = (
            total_elapsed
        )


        print(
            "[RAG] ----------------------------------------",
            flush=True,
        )

        print(
            f"[RAG] GESAMT: "
            f"{total_elapsed:.3f} s",
            flush=True,
        )

        print(
            "[RAG] ========================================",
            flush=True,
        )

        print()


        backend_status = {
            "files": {
                "requested": "files" in active_arms,
                "enabled": ELASTICSEARCH_ENABLED,
                "available": bool(es_diagnostics.get("available", "error" not in es_diagnostics)),
                "error": es_diagnostics.get("error"),
            },
            "vector": {
                "requested": "vector" in active_arms,
                "enabled": QDRANT_ENABLED,
                "available": bool(vector_diagnostics.get("available", "error" not in vector_diagnostics)),
                "error": vector_diagnostics.get("error"),
            },
            "graph": {
                "requested": "graph" in active_arms,
                "enabled": _graph_retrieval_enabled(),
                "available": str(graph_diagnostics.get("mode") or "") not in {"error", "neo4j_unavailable"},
                "error": graph_diagnostics.get("error"),
            },
        }
        degraded = [
            name for name, state in backend_status.items()
            if state["requested"] and state.get("enabled", True) and not state["available"]
        ]
        if not final_results and degraded:
            retrieval_mode = "degraded_no_results"
            retrieval_message = _degraded_retrieval_message(backend_status, degraded)
        else:
            retrieval_message = ""

        execution_queries: dict[str, dict] = {}
        if "files" in active_arms and es_diagnostics.get("compiled_query"):
            execution_queries["files"] = {
                "backend": "elasticsearch",
                "query": es_diagnostics.get("compiled_query"),
            }
        if "vector" in active_arms and QDRANT_ENABLED and str(getattr(plan, "semantic_query", "") or "").strip():
            execution_queries["vector"] = {
                "backend": "qdrant",
                "query": str(getattr(plan, "semantic_query", "") or "").strip(),
            }
        if "graph" in active_arms and graph_diagnostics.get("resolved_entity_ids"):
            execution_queries["graph"] = {
                "backend": "neo4j",
                "resolved_entity_ids": list(graph_diagnostics.get("resolved_entity_ids") or []),
            }

        return {

            "question":
                question,

            "plan":
                plan,

            "entity_resolution":
                entity_context,

            "search_spec":
                dict(search_spec or {}),

            "retrieval_mode":
                retrieval_mode,

            "retrieval_strategy":
                retrieval_strategy,

            "retrieval_message":
                retrieval_message,

            "backend_status":
                backend_status,

            "execution_queries":
                execution_queries,

            "retrieval_signal":
                retrieval_signal,

            "reranker_used":
                reranker_used,

            "reranker_error":
                reranker_error,

            "retrieval_arms":
                sorted(active_arms),

            "source_scopes":
                sorted(normalized_source_scopes) if normalized_source_scopes else None,

            "raw_results":
                bool(raw_results),

            "force_unspecific":
                bool(force_unspecific),

            # ------------------------------------------------
            # Neu: detaillierte Laufzeiten
            # ------------------------------------------------

            "timings":
                timings,

            "statistics": {

                "elasticsearch_candidates":
                    len(es_results),

                "elasticsearch_unique":
                    len(es_unique),

                "elasticsearch_total_hits":
                    es_diagnostics.get("total_hits"),

                "elasticsearch_total_relation":
                    es_diagnostics.get("total_relation"),

                "vector_candidates":
                    len(vector_results),

                "vector_unique":
                    len(vector_unique),

                "vector_raw_results":
                    vector_diagnostics.get("raw_results", 0),

                "vector_above_threshold":
                    vector_diagnostics.get("above_threshold", 0),

                "graph_candidates":
                    len(graph_results),

                "graph_mode":
                    graph_diagnostics.get("mode"),

                "graph_direct_relations":
                    len(graph_diagnostics.get("direct_relations") or []),

                "retrieval_duplicates_suppressed":
                    es_arm_duplicates + vector_arm_duplicates,

                "rrf_candidates":
                    len(fused_results),

                "deduplicated_candidates":
                    len(rerank_candidates),

                "duplicate_variants_suppressed":
                    duplicate_variants_suppressed,

                "reranker_candidates":
                    len(rerank_candidates),

                "returned":
                    len(final_results),
            },

            "results":
                final_results,
        }


    except PipelineStageError:

        total_elapsed = (
            time.perf_counter()
            - total_started
        )

        timings["total"] = (
            total_elapsed
        )


        print(
            f"[RAG] GESAMT bis Fehler: "
            f"{total_elapsed:.3f} s",
            flush=True,
        )

        print(
            "[RAG] ========================================",
            flush=True,
        )

        print()

        raise


    except Exception as exc:

        total_elapsed = (
            time.perf_counter()
            - total_started
        )

        timings["total"] = (
            total_elapsed
        )


        print(
            "[RAG] UNERWARTETER FEHLER: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        print(
            f"[RAG] GESAMT bis Fehler: "
            f"{total_elapsed:.3f} s",
            flush=True,
        )

        print(
            "[RAG] ========================================",
            flush=True,
        )

        print()

        raise


# ------------------------------------------------------------
# CLI-Ausgabe
# ------------------------------------------------------------


# ------------------------------------------------------------
# RC8: Multi-Probe Retrieval
# ------------------------------------------------------------

def _multi_probe_document_key(item: dict) -> str:
    document_id = str(item.get("document_id") or "").strip()
    if document_id:
        return "id:" + document_id
    title = str(item.get("title") or item.get("path") or "").strip().casefold()
    return "title:" + title


def _merge_probe_rankings(
    probe_rankings: list[tuple[str, list[dict]]],
    *,
    rrf_k: int | None = None,
) -> list[dict]:
    """Fuse complete probe rankings with a second, probe-level RRF.

    Arm-level RRF remains inside ``perform_search(raw_results=True)``.  This
    second RRF treats each retrieval probe as one independent ranked list and
    preserves the arm provenance from every occurrence of a document.
    """
    if rrf_k is None:
        rrf_k = _rrf_k()

    by_key: dict[str, dict] = {}
    scores: dict[str, float] = {}
    hits: dict[str, list[dict]] = {}

    for probe_query, ranking in probe_rankings:
        seen_in_probe: set[str] = set()
        for rank, source in enumerate(ranking, start=1):
            key = _multi_probe_document_key(source)
            if not key or key in seen_in_probe:
                continue
            seen_in_probe.add(key)
            if key not in by_key:
                by_key[key] = dict(source)
                scores[key] = 0.0
                hits[key] = []
            else:
                _merge_retrieval_provenance(by_key[key], source)
            scores[key] += 1.0 / (float(rrf_k) + float(rank))
            hits[key].append({"query": probe_query, "rank": rank})

    fused: list[dict] = []
    for key, item in by_key.items():
        item["rrf"] = scores[key]
        item["probe_rrf_score"] = scores[key]
        item["probe_hits"] = hits[key]
        fused.append(item)
    fused.sort(key=lambda item: float(item.get("probe_rrf_score") or 0.0), reverse=True)
    for rank, item in enumerate(fused, start=1):
        item["rrf_rank"] = rank
    return fused


def _merge_required_results(fused: list[dict], required_results: list[dict]) -> tuple[list[dict], set[str]]:
    """Inject post-ACL strict-query documents into an exhaustive candidate set."""
    if not required_results:
        return fused, set()
    by_key = {_multi_probe_document_key(item): item for item in fused}
    required_keys: set[str] = set()
    for source in required_results:
        key = _multi_probe_document_key(source)
        if not key:
            continue
        required_keys.add(key)
        existing = by_key.get(key)
        if existing is not None:
            _merge_retrieval_provenance(existing, source)
            existing["exhaustive_required"] = True
            continue
        item = dict(source)
        item["exhaustive_required"] = True
        item.setdefault("probe_rrf_score", 0.0)
        item.setdefault("rrf", 0.0)
        item.setdefault("probe_hits", [{"query": "strict_acl_seed", "rank": None}])
        fused.append(item)
        by_key[key] = item
    return fused, required_keys


def _preserve_required_after_rerank(
    ranked: list[dict],
    candidates: list[dict],
    required_keys: set[str],
    limit: int,
) -> list[dict]:
    if not required_keys:
        return ranked[:limit]

    candidate_by_key = {_multi_probe_document_key(item): item for item in candidates}
    selected = list(ranked[:limit])
    selected_keys = {_multi_probe_document_key(item) for item in selected}
    for key in required_keys:
        if key in selected_keys:
            continue
        required = candidate_by_key.get(key)
        if required is not None:
            selected.append(dict(required))
            selected_keys.add(key)

    # If mandatory strict-query documents pushed us beyond the result budget,
    # remove the lowest-ranked non-mandatory items first.  The API preflight
    # guarantees that the mandatory set itself fits the configured capacity.
    while len(selected) > limit:
        drop_index = None
        for index in range(len(selected) - 1, -1, -1):
            if _multi_probe_document_key(selected[index]) not in required_keys:
                drop_index = index
                break
        if drop_index is None:
            break
        selected.pop(drop_index)
    return selected[:limit]


def _hard_constraint_values(question: str) -> list[tuple[str, str]]:
    """Return safe literal constraints used to prune the recall pool.

    Filenames have their own deterministic resolver.  Years, explicit dates and
    structured identifiers are language-neutral enough to become mandatory
    recall-pool constraints before RRF/reranking.
    """
    constraints = extract_safe_hard_constraints(question)
    values: list[tuple[str, str]] = []
    for kind in ("years", "dates", "identifiers"):
        for value in constraints.get(kind) or []:
            values.append((kind, str(value)))
    return values


def _filter_probe_rankings_by_hard_constraints(
    question: str,
    probe_rankings: list[tuple[str, list[dict]]],
) -> tuple[list[tuple[str, list[dict]]], dict]:
    """Intersect bounded probe candidates with explicit literal constraints.

    The lookup is performed in Elasticsearch against the candidate document
    IDs, so vector/graph candidates cannot bypass an explicit year/date/id.
    Failure is recall-safe: if Elasticsearch cannot perform the audit, the
    unfiltered rankings are returned and the later verifier remains fail-closed.
    """
    values = _hard_constraint_values(question)
    if not values or not probe_rankings:
        return probe_rankings, {"applied": False, "constraints": []}

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()
    for _probe, ranking in probe_rankings:
        for item in ranking:
            document_id = str(item.get("document_id") or "").strip()
            if document_id and document_id not in seen_ids:
                seen_ids.add(document_id)
                candidate_ids.append(document_id)
    if not candidate_ids:
        return probe_rankings, {"applied": False, "constraints": values}

    must: list[dict] = []
    for _kind, value in values:
        must.append({
            "multi_match": {
                "query": value,
                "fields": ["combined^3", "content", "title"],
                "operator": "and",
            }
        })

    try:
        response = _es_post(
            f"{ES_URL}/{ES_INDEX}/_search",
            json={
                "size": min(len(candidate_ids), 1000),
                "_source": False,
                "query": {
                    "bool": {
                        "filter": [{"ids": {"values": candidate_ids}}],
                        "must": must,
                    }
                },
            },
            timeout=ES_TIMEOUT,
        )
        response.raise_for_status()
        allowed = {
            str(hit.get("_id") or "").strip()
            for hit in (response.json().get("hits", {}).get("hits", []) or [])
            if str(hit.get("_id") or "").strip()
        }
    except Exception as exc:
        log.warning(
            "hard-constraint candidate audit failed; preserving recall: %s: %s",
            type(exc).__name__, exc,
        )
        return probe_rankings, {
            "applied": False,
            "constraints": values,
            "error": f"{type(exc).__name__}: {exc}",
        }

    filtered: list[tuple[str, list[dict]]] = []
    before = 0
    after = 0
    for probe, ranking in probe_rankings:
        before += len(ranking)
        kept = [
            item for item in ranking
            if str(item.get("document_id") or "").strip() in allowed
        ]
        after += len(kept)
        filtered.append((probe, kept))

    log.info(
        "hard-constraint recall filter: constraints=%s candidates=%d kept=%d",
        values, before, after,
    )
    return filtered, {
        "applied": True,
        "constraints": values,
        "candidate_entries_before": before,
        "candidate_entries_after": after,
        "allowed_document_ids": len(allowed),
    }


def _probe_uses_nextcloud_exact_files(probe: dict) -> bool:
    """Return whether one planner probe should use Nextcloud-style exact ES.

    ``strict_lexical`` is intended to mean the same small Boolean vocabulary
    as Nextcloud FullTextSearch (+term, -term, quoted phrases).  The provider
    normally labels such probes correctly, but recover a model/provider label
    mismatch when a files-only probe still carries at least two explicit MUST
    tokens.  This keeps strict planner views aligned with the operator-visible
    /elastic compatibility path instead of silently routing them through the
    broader RAG lexical planner.
    """
    query = str(probe.get("query") or "").strip()
    if not query:
        return False

    arms = probe.get("retrieval_arms")
    if arms is not None:
        selected = {str(value).strip().casefold() for value in arms if str(value).strip()}
        if selected != {"files"}:
            return False

    kind = str(probe.get("kind") or "").strip().casefold()
    if kind == "strict_lexical":
        return True

    tokens = _nextcloud_query_tokens(query)
    must_count = sum(1 for token in tokens if str(token.get("occur") or "") == "must")
    return must_count >= 2


def perform_multi_probe_search(
    original_question: str,
    probes: list[dict],
    limit: int = FINAL_LIMIT,
    *,
    required_results: list[dict] | None = None,
    force_unspecific: bool = False,
    acl_prefilter_user: str | None = None,
    acl_prefilter_groups: list[str] | None = None,
) -> dict:
    """Execute bounded RC8 probes, fuse them, then rerank once jointly.

    Each probe still traverses the mature ES/Qdrant/Graph retrieval code.  The
    per-probe calls are retrieval-only (no CrossEncoder). Their rankings are
    fused by probe-level RRF, Graph candidates are hydrated, and only then is
    the combined candidate field reranked against the *original* user question.
    """
    started = time.perf_counter()
    acl_prefilter_active = bool(acl_prefilter_user and acl_prefilter_groups is not None)
    clean_probes: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw_probe in probes:
        query = str(raw_probe.get("query") or "").strip()
        if not query:
            continue
        arms = raw_probe.get("retrieval_arms")
        arm_tuple = tuple(sorted(str(value).strip().lower() for value in (arms or []) if str(value).strip()))
        key = (query.casefold(), arm_tuple)
        if key in seen:
            continue
        seen.add(key)
        clean_probes.append({
            "query": query,
            "kind": str(raw_probe.get("kind") or "semantic"),
            "semantic_query": str(raw_probe.get("semantic_query") or query).strip(),
            "retrieval_arms": list(arm_tuple) if arm_tuple else None,
        })

    if not clean_probes:
        clean_probes = [{
            "query": original_question,
            "kind": "original",
            "semantic_query": original_question,
            "retrieval_arms": None,
        }]

    hard_constraint_values = _hard_constraint_values(original_question)
    per_probe_limit = max(int(limit), _rerank_candidates(), 20)
    if hard_constraint_values:
        # Pull a wider recall field first; the cheap literal audit below prunes
        # it before RRF and CrossEncoder reranking.
        per_probe_limit = max(per_probe_limit, min(80, max(40, int(limit) * 2)))
    probe_rankings: list[tuple[str, list[dict]]] = []
    probe_diagnostics: list[dict] = []
    original_plan = None
    original_entity_context: dict = {}

    for probe_index, probe in enumerate(clean_probes, start=1):
        if _probe_uses_nextcloud_exact_files(probe):
            exact_diagnostics: dict = {}
            ranking = (
                elastic_exact_search(
                    probe["query"],
                    limit=per_probe_limit,
                    diagnostics=exact_diagnostics,
                    acl_prefilter_user=acl_prefilter_user,
                    acl_prefilter_groups=acl_prefilter_groups,
                )
                if acl_prefilter_active
                else elastic_exact_search(
                    probe["query"],
                    limit=per_probe_limit,
                    diagnostics=exact_diagnostics,
                )
            )
            run = {
                "plan": None,
                "entity_resolution": {},
                "results": ranking,
                "retrieval_mode": "strict_lexical_exact",
                "retrieval_strategy": "nextcloud_exact",
                "backend_status": {
                    "files": {
                        "requested": True,
                        "enabled": True,
                        "available": True,
                        "error": None,
                    }
                },
            }
            log.info(
                "multi-probe strict lexical exact: query=%r es_total=%s returned=%d kind=%s",
                probe["query"],
                exact_diagnostics.get("total_hits"),
                len(ranking),
                probe.get("kind"),
            )
        else:
            run = (
                perform_search(
                    probe["query"],
                    limit=per_probe_limit,
                    retrieval_arms=probe.get("retrieval_arms"),
                    raw_results=True,
                    force_unspecific=True,
                    semantic_query_override=probe.get("semantic_query"),
                    acl_prefilter_user=acl_prefilter_user,
                    acl_prefilter_groups=acl_prefilter_groups,
                )
                if acl_prefilter_active
                else perform_search(
                    probe["query"],
                    limit=per_probe_limit,
                    retrieval_arms=probe.get("retrieval_arms"),
                    raw_results=True,
                    force_unspecific=True,
                    semantic_query_override=probe.get("semantic_query"),
                )
            )
            ranking = [dict(item) for item in (run.get("results") or [])]

        if original_plan is None and run.get("plan") is not None:
            original_plan = run.get("plan")
            original_entity_context = dict(run.get("entity_resolution") or {})
        probe_rankings.append((probe["query"], ranking))
        probe_diagnostics.append({
            "index": probe_index,
            "kind": probe["kind"],
            "query": probe["query"],
            "semantic_query": probe.get("semantic_query"),
            "retrieval_arms": probe.get("retrieval_arms"),
            "retrieval_mode": run.get("retrieval_mode"),
            "retrieval_strategy": run.get("retrieval_strategy"),
            "returned": len(ranking),
            "backend_status": run.get("backend_status") or {},
        })

    probe_rankings, hard_constraint_stats = _filter_probe_rankings_by_hard_constraints(
        original_question, probe_rankings
    )
    fused = _merge_probe_rankings(probe_rankings)
    fused, required_keys = _merge_required_results(fused, list(required_results or []))

    # Keep the complete bounded probe field for the one joint reranking pass.
    # Three default probes x 20 candidates is still a small CrossEncoder batch.
    candidate_limit = max(_rerank_candidates(), int(limit), len(required_keys))
    candidate_limit = min(max(candidate_limit, len(fused)), 80)
    rerank_candidates = deduplicate_for_reranker(
        fused,
        candidate_limit,
        preserve_per_arm=0,
    )

    if original_plan is None:
        original_entity_context = prepare_entity_context(original_question)
        original_plan = create_plan(original_question, entity_context=original_entity_context)

    graph_candidates = [item for item in rerank_candidates if item.get("graph_rank") is not None]
    graph_timings: dict = {}
    hydrate_graph_results(graph_candidates, original_question, original_plan, graph_timings)

    reranker_used = False
    reranker_error = None
    if not _reranker_enabled():
        joint_ranked = rrf_fallback(rerank_candidates, max(1, int(limit)))
    else:
        try:
            joint_ranked = rerank_results(
                query=original_question,
                results=rerank_candidates,
                candidate_limit=len(rerank_candidates),
                top_k=max(1, int(limit)),
                min_score=_rerank_min_score(),
                config_override=_runtime_reranker_config(),
            )
            if rerank_candidates and not joint_ranked:
                raise RuntimeError("Reranker lieferte keine verwendbaren Ergebnisse")
            reranker_used = True
        except Exception as exc:
            reranker_error = f"{type(exc).__name__}: {exc}"
            joint_ranked = rrf_fallback(rerank_candidates, max(1, int(limit)))

    final_results = _preserve_required_after_rerank(
        joint_ranked,
        rerank_candidates,
        required_keys,
        max(1, int(limit)),
    )
    for rank, item in enumerate(final_results, start=1):
        item["final_rank"] = rank

    add_source_urls(final_results)
    enrichment_timings: dict = {}
    enrich_final_results(final_results, original_question, original_plan, enrichment_timings)

    backend_status: dict[str, dict] = {}
    for arm in ("files", "vector", "graph"):
        states = [
            (probe.get("backend_status") or {}).get(arm)
            for probe in probe_diagnostics
            if (probe.get("backend_status") or {}).get(arm) is not None
        ]
        if states:
            backend_status[arm] = {
                "requested": any(bool(state.get("requested")) for state in states),
                "enabled": any(bool(state.get("enabled", True)) for state in states),
                "available": any(bool(state.get("available")) for state in states),
                "error": next((state.get("error") for state in states if state.get("error")), None),
            }

    degraded = [
        arm for arm, state in backend_status.items()
        if state.get("requested") and state.get("enabled", True) and not state.get("available")
    ]
    if not final_results and degraded:
        retrieval_mode = "degraded_no_results"
        retrieval_message = _degraded_retrieval_message(backend_status, degraded)
    else:
        retrieval_mode = (
            "multi_probe_reranked" if reranker_used
            else ("multi_probe_rrf_no_reranker" if not _reranker_enabled() else "multi_probe_rrf_fallback")
        )
        retrieval_message = ""

    return {
        "question": original_question,
        "plan": original_plan,
        "entity_resolution": original_entity_context,
        "retrieval_mode": retrieval_mode,
        "retrieval_strategy": "multi_probe_rrf",
        "retrieval_message": retrieval_message,
        "backend_status": backend_status,
        "retrieval_signal": {
            "decision": {
                "strategy": "multi_probe_rrf",
                "reason": "Probe-Ranglisten per RRF fusioniert; gemeinsames Reranking gegen Originalfrage.",
            },
            "probe_count": len(clean_probes),
        },
        "reranker_used": reranker_used,
        "reranker_error": reranker_error,
        "retrieval_arms": sorted({
            arm
            for probe in clean_probes
            for arm in (probe.get("retrieval_arms") or ["files", "vector", "graph"])
        }),
        "raw_results": False,
        "force_unspecific": bool(force_unspecific),
        "planner_probes": probe_diagnostics,
        "timings": {
            "multi_probe_total": time.perf_counter() - started,
            "graph_hydration": graph_timings.get("graph_hydration", 0.0),
            "context_enrichment": enrichment_timings.get("context_enrichment", 0.0),
        },
        "statistics": {
            "probe_count": len(clean_probes),
            "probe_rrf_candidates": len(fused),
            "hard_constraint_filter": hard_constraint_stats,
            "deduplicated_candidates": len(rerank_candidates),
            "exhaustive_required": len(required_keys),
            "returned": len(final_results),
        },
        "results": final_results,
    }

def display_plan(plan):

    print()
    print("=" * 80)
    print("SUCHPLAN")
    print("=" * 80)


    print(
        f"Modus:     {plan.search_mode}"
    )


    if plan.semantic_query:

        print(
            f"Semantik:  "
            f"{plan.semantic_query}"
        )


    if plan.must:

        print(
            "MUSS:      "
            + ", ".join(plan.must)
        )


    if plan.phrases:

        print(
            "PHRASE:    "
            + ", ".join(plan.phrases)
        )


    if plan.should:

        print(
            "SOLLTE:    "
            + ", ".join(plan.should)
        )


    if plan.must_not:

        print(
            "NICHT:     "
            + ", ".join(plan.must_not)
        )


    if plan.must_not_phrases:

        print(
            "NICHT-PHRASE: "
            + ", ".join(
                plan.must_not_phrases
            )
        )


    if (
        plan.date_from
        or plan.date_to
    ):

        print(
            "ZEIT:      "
            f"{plan.date_from or '*'}"
            " bis "
            f"{plan.date_to or '*'}"
        )


    if plan.notes:

        print(
            f"Notiz:     "
            f"{plan.notes}"
        )


def display_timings(
    timings,
):

    print()
    print("=" * 80)
    print("LAUFZEITEN")
    print("=" * 80)


    order = [
        "planner",
        "elasticsearch",
        "embedding",
        "qdrant",
        "vector_processing",
        "rrf",
        "reranker",
        "total",
    ]


    for name in order:

        if name not in timings:
            continue


        print(
            f"{name:20s} "
            f"{timings[name]:8.3f} s"
        )


def display_results(
    search_result,
):

    results = (
        search_result["results"]
    )


    print()
    print("=" * 80)
    print("SUCHERGEBNISSE")
    print("=" * 80)


    print(
        "Retrieval-Modus: "
        f"{search_result['retrieval_mode']}"
    )


    print(
        "Reranker benutzt: "
        f"{search_result['reranker_used']}"
    )


    if search_result["reranker_error"]:

        print(
            "Reranker-Fehler: "
            f"{search_result['reranker_error']}"
        )


    if not results:

        print()
        print("Keine Treffer.")

        return


    for item in results:

        print()
        print("=" * 80)


        print(
            "Final-Rang:     "
            f"{item.get('final_rank', '-')}"
        )


        print(
            "Alter RRF-Rang: "
            f"{item.get('rrf_rank', '-')}"
        )


        print(
            "RRF:            "
            f"{item.get('rrf', 0.0):.6f}"
        )


        es_rank = (
            item["es_rank"]
            if item["es_rank"] is not None
            else "-"
        )


        vector_rank = (
            item["vector_rank"]
            if item["vector_rank"] is not None
            else "-"
        )


        print(
            f"ES-Rang:        "
            f"{es_rank}"
        )


        print(
            f"Vector-Rang:    "
            f"{vector_rank}"
        )


        if (
            item["vector_score"]
            is not None
        ):

            print(
                "Vector-Score:   "
                f"{item['vector_score']:.4f}"
            )


        if (
            item.get("reranker_score")
            is not None
        ):

            print(
                "Reranker:       "
                f"{item['reranker_score']:.6f}"
            )


        print(
            "Datei:           "
            f"{item['title']}"
        )


        print(
            "Dokument-ID:     "
            f"{item['document_id']}"
        )


        if item.get("document_date"):

            print(
                "Dokumentdatum:   "
                f"{item['document_date']}"
            )


        if item.get("content_kind"):

            print(
                "Inhaltstyp:      "
                f"{item['content_kind']}"
            )


        if item.get("nextcloud_openfile_id"):

            print(
                "Openfile-ID:     "
                f"{item['nextcloud_openfile_id']}"
            )


        if item.get("source_url"):

            print(
                "Quelle:          "
                f"{item['source_url']}"
            )


        if (
            item["chunk_no"]
            is not None
        ):

            print(
                "Chunk:          "
                f"{item['chunk_no']}"
            )


        snippet = (
            item["vector_snippet"]
            or item["es_snippet"]
        )


        if snippet:

            print()
            print(
                snippet[:1600]
            )


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():

    if len(sys.argv) < 2:

        print()
        print(
            "Bitte eine Suchanfrage angeben."
        )

        sys.exit(1)


    question = " ".join(
        sys.argv[1:]
    )


    print()
    print("Benutzeranfrage:")
    print(question)


    try:

        result = perform_search(
            question
        )


        display_plan(
            result["plan"]
        )


        display_timings(
            result["timings"]
        )


        print()

        print(
            "ES-Kandidaten:       "
            f"{result['statistics']['elasticsearch_candidates']}"
        )

        print(
            "Vector-Kandidaten:   "
            f"{result['statistics']['vector_candidates']}"
        )

        print(
            "RRF-Kandidaten:      "
            f"{result['statistics']['rrf_candidates']}"
        )

        print(
            "Davon rerankt:       "
            f"{result['statistics']['reranker_candidates']}"
        )


        display_results(
            result
        )


    except PipelineStageError as exc:

        print()
        print("PIPELINE-FEHLER:")
        print(exc)

        sys.exit(1)


    except httpx.HTTPError as exc:

        print()
        print("HTTP-Fehler:")
        print(exc)

        sys.exit(1)


    except Exception as exc:

        print()
        print("Fehler:")
        print(exc)

        sys.exit(1)


    finally:

        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
