from contextlib import asynccontextmanager
from typing import Any
import json
import os
import time
from urllib.parse import urlparse

import httpx
import requests

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse

from pydantic import (
    BaseModel,
    Field,
)

from rag.secret_env import secret_env
from rag.version import VERSION
from rag.credential_store import CredentialStore
from rag.logging_utils import get_logger, configure_third_party_logging
from rag.runtime_validation import require_secure_runtime_config
from rag.architecture_policy import (
    api_path_blocked,
    effective_retrieval_arms,
    is_src,
    request_capability_error,
)
from rag.tls_compat import configure_tls_compat
from rag.nextcloud_tls import nextcloud_verify_value
from rag.api_security import (
    ADMIN as SECURITY_ADMIN,
    INTERNAL as SECURITY_INTERNAL,
    PUBLIC as SECURITY_PUBLIC,
    TRUSTED_PROVIDER as SECURITY_TRUSTED_PROVIDER,
    USER as SECURITY_USER,
    ApiSecurity,
)
from rag.elasticsearch_client import requests_options as elastic_requests_options
from rag.planner import create_plan
from rag.graph_indexer import GraphEvidenceIndexer
from rag.graph_queue import GraphQueue
from rag.graph import GraphStore, cfg_get, load_config
from rag.admin_ui import create_admin_router
from rag.curation_ui import create_curation_router, cleanup_stale_curation_sessions
from rag.web_research import WebResearchArm, load_web_config
from rag.retrieval_planner import load_retrieval_planner_settings
from rag.sunaq_models import RuntimeModel, load_model_registry
from rag.reranker import get_reranker_status
from rag.source_origin import chat_archive_roots, path_is_under, classify_source_origin, source_scope_allows_record
from rag.source_registry import register_document, auto_mirror_registry_to_elasticsearch
from rag.acl import (
    NextcloudLiveAcl,
    AclBackendError,
    AclConfigurationError,
    AclIdentityError,
)
from rag.search import (
    EMBEDDING_BACKEND,
    EMBEDDING_MODEL,
    EMBEDDING_URL,
    ES_INDEX,
    ES_URL,
    FINAL_LIMIT,
    QDRANT_COLLECTION,
    QDRANT_URL,
    close_graph_store,
    document_ids_lookup,
    document_reference_candidates,
    elastic_exact_search,
    extract_filename,
    is_pure_filename_lookup,
    perform_multi_probe_search,
    perform_search,
    prepare_entity_context,
    use_runtime_model_config,
    resolve_document_references,
    strict_filename_lookup,
    store,
    UNSPECIFIC_RETRIEVAL_MESSAGE,
)


configure_third_party_logging()
log = get_logger("api")

app_config = load_config()
configure_tls_compat(app_config)
graph_queue = GraphQueue(app_config)
live_acl = NextcloudLiveAcl(app_config)
_chat_archive_default = "true" if bool(cfg_get(app_config, "chat_archive.enabled", default=False)) else "false"
CHAT_ARCHIVE_ENABLED = os.getenv("CHAT_ARCHIVE_ENABLED", _chat_archive_default).strip().lower() in {
    "1", "true", "yes", "on"
}
api_security = ApiSecurity(app_config, live_acl)
credential_store = CredentialStore(str(cfg_get(app_config, "auth.credential_store", default="runtime/users.sqlite") or "runtime/users.sqlite"))
_web_arm_instance: WebResearchArm | None = None
_research_finding_schema_ready = False

# /elastic scans only a bounded prefix for visible-list usefulness.  It no
# longer attempts to calculate an exact ACL-filtered count for huge ES fields.
# Broad fields are reported as unspecific instead of authorizing hundreds or
# thousands of documents merely to count them.
ELASTIC_ACL_RESULT_SCAN_LIMIT = max(20, int(os.getenv("ELASTIC_ACL_RESULT_SCAN_LIMIT", "200")))
RETRIEVAL_PLANNER_SETTINGS = load_retrieval_planner_settings(app_config)
SUNAQ_MODEL_REGISTRY = load_model_registry(app_config)


def _resolve_sunaq_model(model_id: str | None) -> RuntimeModel:
    try:
        return SUNAQ_MODEL_REGISTRY.get(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _profile_final_limit(model: RuntimeModel) -> int:
    search_cfg = model.section("search")
    try:
        return max(1, min(100, int(search_cfg.get("final_limit", FINAL_LIMIT))))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"SunaQ model {model.model_id} has invalid search.final_limit",
        ) from exc



def _acl_prefilter_context(http_request: Request) -> tuple[str | None, list[str] | None]:
    """Return a best-effort server-resolved owner/user/group prefilter identity.

    The authenticated Nextcloud UID and current group list are resolved via the
    OCS current-user endpoint using the stored app credential.  Client-supplied
    group headers are deliberately not trusted.  Failure disables only the
    metadata prefilter for this request; live WebDAV ACL remains mandatory.
    """
    if not bool(cfg_get(app_config, "acl.prefilter.enabled", default=False)):
        return None, None
    if not live_acl.enabled:
        log.info("ACL prefilter configured but skipped: live ACL disabled")
        return None, None

    rag_user_id = str(http_request.headers.get("x-rag-user-id") or "").strip()
    try:
        uid, groups = live_acl.prefilter_identity(
            None if live_acl.identity_mode == "single_user" else rag_user_id
        )
    except (AclIdentityError, AclConfigurationError, AclBackendError) as exc:
        log.warning(
            "ACL prefilter configured but skipped: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None, None

    log.info(
        "ACL prefilter: applied user=%r groups=%d source=nextcloud_ocs",
        uid,
        len(groups),
    )
    return uid, groups


def _get_web_arm() -> WebResearchArm:
    global _web_arm_instance
    if _web_arm_instance is None:
        _web_arm_instance = WebResearchArm(app_config)
    return _web_arm_instance



def _is_elasticsearch_unavailable(exc: Exception) -> bool:
    """Recognize transport failures against the configured Elasticsearch endpoint."""
    transport_types = (
        httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
        requests.ConnectionError, requests.Timeout,
    )
    if not isinstance(exc, transport_types):
        return False
    request = getattr(exc, "request", None)
    url = str(getattr(request, "url", "") or "")
    es_base = str(ES_URL or "").rstrip("/")
    # Some nested transport errors do not retain the request object. In this API
    # the affected call sites below are Elasticsearch-backed; an empty URL is
    # therefore still treated as a temporary ES outage.
    return (not url) or (bool(es_base) and url.startswith(es_base))


def _raise_retrieval_exception(exc: Exception, *, fallback: str) -> None:
    if _is_elasticsearch_unavailable(exc):
        raise HTTPException(
            status_code=503,
            detail="Dokumentensuche derzeit nicht verfügbar (Elasticsearch nicht erreichbar).",
        ) from exc
    raise HTTPException(status_code=500, detail=fallback) from exc

# ------------------------------------------------------------
# Lebenszyklus des API-Servers
# ------------------------------------------------------------

def _initialize_neo4j_schema() -> bool:
    """Apply the idempotent Neo4j schema upgrade without making API startup fail-open/closed."""
    global _research_finding_schema_ready
    if not bool(cfg_get(app_config, "neo4j.enabled", default=True)):
        return False
    try:
        with GraphStore.from_config(app_config) as graph:
            graph.verify_connectivity()
            graph.ensure_schema()
        _research_finding_schema_ready = True
        return True
    except Exception as exc:
        # Neo4j is an optional/degradable backend. The installer treats a failed
        # selected local Neo4j as fatal; an independent API start only defers the
        # migration until Neo4j becomes reachable.
        log.warning("Neo4j schema initialization deferred: %s", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):

    # --------------------------------------------------------
    # Startup
    #
    # Die globalen Objekte sind beim Import bereits erzeugt.
    # Insbesondere bleibt der Reranker während der gesamten
    # Laufzeit im RAM. Security-critical misconfiguration fails before
    # the service starts accepting requests.
    # --------------------------------------------------------

    require_secure_runtime_config(app_config)
    cleanup_stale_curation_sessions(app_config)
    _initialize_neo4j_schema()
    yield


    # --------------------------------------------------------
    # Shutdown
    #
    # Qdrant-Verbindung explizit sauber schließen.
    # --------------------------------------------------------

    if store is not None:
        store.close()
    close_graph_store()


# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------

app = FastAPI(

    title="SunaQ",

    description=(
        "Hybride Dokumentensuche über Nextcloud/Elasticsearch, Qdrant und Neo4j "
        "mit Cross-Encoder-Reranking sowie deterministischer Dokumentauswahl."
    ),

    version=VERSION,

    lifespan=lifespan,
)

# Browser-admin UI shares the same FastAPI/uvicorn process and backend curation logic.
app.include_router(create_admin_router(app_config, graph_queue, load_web_config()))
app.include_router(create_curation_router(app_config))


def _zone(zone: str, dependency: Any | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"openapi_extra": {"x-aki-security-zone": zone}}
    if dependency is not None:
        value["dependencies"] = [Depends(dependency)]
    return value


ZONE_PUBLIC = _zone(SECURITY_PUBLIC)
ZONE_INTERNAL = _zone(SECURITY_INTERNAL, api_security.require_internal_client)
ZONE_TRUSTED_PROVIDER = _zone(
    SECURITY_TRUSTED_PROVIDER, api_security.require_trusted_provider
)
ZONE_USER = _zone(SECURITY_USER, api_security.require_current_user)
ZONE_ADMIN = _zone(SECURITY_ADMIN, api_security.require_admin)


def _enforce_architecture_request(
    *,
    retrieval_arms: list[str] | set[str] | None = None,
    source_scopes: list[str] | set[str] | None = None,
    web_requested: bool = False,
) -> list[str] | None:
    """Reject requests outside the architecture tier and return effective arms."""
    error = request_capability_error(
        app_config,
        retrieval_arms=retrieval_arms,
        source_scopes=source_scopes,
        web_requested=web_requested,
    )
    if error:
        raise HTTPException(status_code=403, detail=error)
    return effective_retrieval_arms(app_config, retrieval_arms)


def _src_documents_only(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prevent archive records from entering SRC through deterministic lookups."""
    if not is_src(app_config):
        return items
    return [
        item
        for item in items
        if source_scope_allows_record(
            str(item.get("document_id") or ""),
            str(item.get("path") or item.get("title") or ""),
            {"documents"},
            indexed_origin=str(item.get("source_origin") or "").strip() or None,
        )
    ]


@app.middleware("http")
async def require_internal_api_auth(request: Request, call_next):
    """Defense-in-depth default deny for middleware routes.

    Route dependencies below define the finer PUBLIC/TRUSTED_PROVIDER/INTERNAL/
    ADMIN/USER zones. This middleware remains the coarse baseline so a future
    endpoint cannot become reachable merely because its zone dependency was
    accidentally omitted.
    """
    if api_security.is_baseline_exempt(request.url.path):
        return await call_next(request)
    if api_path_blocked(app_config, request.url.path):
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Capability not available in the configured SunaQ architecture tier"
            },
        )
    try:
        api_security.require_internal_client(request)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )
    return await call_next(request)


# ------------------------------------------------------------
# Request-Modelle
# ------------------------------------------------------------



class NextcloudAuthStartRequest(BaseModel):
    rag_user_id: str = Field(default="", description="RAG/OpenWebUI user id. Prefer x-rag-user-id in integrated clients.")


class NextcloudAuthEnsureRequest(BaseModel):
    rag_user_id: str = Field(default="", description="External client identity lookup key.")


def _auth_user_id(request: Request, explicit: str = "") -> str:
    return str(
        explicit
        or request.headers.get("x-rag-user-id")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
        or ""
    ).strip()


def _nextcloud_login_base() -> str:
    return str(cfg_get(app_config, "nextcloud.base_url", default="") or "").strip().rstrip("/")


def _nextcloud_auth_verify() -> bool | str:
    return nextcloud_verify_value(app_config, "auth", "acl", "carddav")


def _nextcloud_flow_ttl_seconds() -> float:
    return max(60.0, float(cfg_get(app_config, "auth.login_flow_ttl_seconds", default=1200) or 1200))


def _nextcloud_flow_post(url: str, *, data: dict[str, str] | None = None) -> requests.Response:
    response = requests.post(
        url,
        data=data,
        timeout=15,
        verify=_nextcloud_auth_verify(),
        allow_redirects=False,
        headers={
            "User-Agent": "SunaQ",
            "Accept": "application/json",
        },
    )
    if response.status_code in {301, 302, 303, 307, 308}:
        location = str(response.headers.get("Location") or "").strip()
        raise RuntimeError(
            "Nextcloud Login Flow endpoint redirected "
            f"({response.status_code}) to {location or '<unknown>'}; "
            "configure nextcloud.base_url with the canonical HTTPS URL"
        )
    return response


def _start_nextcloud_flow_for_user(user_id: str) -> dict[str, Any]:
    base = _nextcloud_login_base()
    if not base:
        raise HTTPException(status_code=503, detail="nextcloud.base_url is not configured")
    response = _nextcloud_flow_post(base + "/index.php/login/v2")
    response.raise_for_status()
    payload = response.json()
    poll = payload.get("poll") or {}
    login_url = str(payload.get("login") or "").strip()
    endpoint = str(poll.get("endpoint") or "").strip()
    token = str(poll.get("token") or "").strip()
    if not (login_url and endpoint and token):
        raise RuntimeError("incomplete Nextcloud Login Flow response")
    flow_id = credential_store.create_nextcloud_flow(user_id, endpoint, token, login_url)
    return {"flow_id": flow_id, "login_url": login_url, "status": "pending", "rag_user_id": user_id}


def _poll_nextcloud_flow(flow: Any) -> dict[str, Any]:
    flow_id = str(flow["flow_id"])
    response = _nextcloud_flow_post(
        str(flow["poll_endpoint"]),
        data={"token": str(flow["poll_token"])},
    )
    if response.status_code in {404, 425}:
        return {
            "flow_id": flow_id,
            "status": "pending",
            "rag_user_id": str(flow["rag_user_id"]),
            "login_url": str(flow["login_url"]),
        }
    response.raise_for_status()
    payload = response.json()
    login_name = str(payload.get("loginName") or "").strip()
    app_password = str(payload.get("appPassword") or "")
    server = str(payload.get("server") or _nextcloud_login_base()).strip().rstrip("/")
    if not login_name or not app_password:
        raise RuntimeError("incomplete Nextcloud login poll response")
    rag_user_id = str(flow["rag_user_id"])
    credential_store.set_credential(rag_user_id, "nextcloud", login_name, app_password, server=server)
    canonical_user = credential_store.bind_identity(rag_user_id, server, login_name)
    credential_store.delete_nextcloud_flows_for_user(rag_user_id)
    return {
        "flow_id": flow_id,
        "status": "connected",
        "rag_user_id": rag_user_id,
        "canonical_user_id": canonical_user.canonical_user_id,
        "nextcloud_login": login_name,
        "server": canonical_user.nextcloud_server,
    }


class ElasticSearchRequest(BaseModel):

    model: str | None = Field(
        default=None,
        description="SunaQ model/profile id. Omitted = configured default.",
    )

    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Direkte Nextcloud-kompatible Volltextsyntax. Kein Planner, "
            "kein Vector/Graph, kein RRF und kein Reranker."
        ),
    )

    limit: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximal zurückzugebende sichtbare Treffer.",
    )

    source_scopes: list[str] | None = Field(
        default=None,
        description="Optionale Quellenbereiche: documents, mailarchive, webarchive, chatarchive.",
    )

    include_content: bool = Field(
        default=False,
        description=(
            "Wenn die sichtbare Treffermenge vollständig in das Limit passt, "
            "werden die Dokumenttexte für eine nachgeschaltete Analyse geladen."
        ),
    )


class SearchSpecRequest(BaseModel):
    elastic_query: str = Field(default="", max_length=1200)
    semantic_query: str = Field(default="", max_length=1200)
    entities: list[str] = Field(default_factory=list, max_length=16)
    concepts: list[str] = Field(default_factory=list, max_length=16)
    constraints: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    verification_requirements: list[str] = Field(default_factory=list, max_length=16)
    # Legacy rc2 fields remain accepted for compatibility with stored records
    # and external callers; the rc3 provider no longer emits them.
    must: list[str] = Field(default_factory=list, max_length=24)
    should: list[str] = Field(default_factory=list, max_length=24)
    must_not: list[str] = Field(default_factory=list, max_length=24)
    phrases: list[str] = Field(default_factory=list, max_length=24)
    must_not_phrases: list[str] = Field(default_factory=list, max_length=24)


class SearchRequest(BaseModel):

    model: str | None = Field(
        default=None,
        description="SunaQ model/profile id. Omitted = configured default.",
    )

    query: str = Field(
        ...,
        description=(
            "Natürlich formulierte Suchanfrage."
        ),
        min_length=1,
    )


    limit: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Optionale maximale Anzahl zurückgegebener Dokumente nach dem "
            "Reranking. Wenn nicht gesetzt, gilt search.final_limit aus "
            "config.yaml."
        ),
    )

    entity_recall: bool = Field(
        default=False,
        description=(
            "Gezielter zweiter Retrieval-Pass: erlaubt bei genau einer "
            "erkannten Entity schwache Token-Anker zusätzlich zu den "
            "präzisen Namensphrasen."
        ),
    )

    retrieval_arms: list[str] | None = Field(
        default=None,
        description=(
            "Optionale harte Auswahl der Dokument-Retrieval-Arme. Zulässig: "
            "files (Elasticsearch), vector (Qdrant), graph (Neo4j). "
            "Nicht gesetzt = normaler Hybridmodus mit allen Armen."
        ),
    )

    source_scopes: list[str] | None = Field(
        default=None,
        description=(
            "Optionale Quellenbereiche, orthogonal zu retrieval_arms: documents, "
            "mailarchive, webarchive, chatarchive. Nicht gesetzt = historischer "
            "interner Pool (Dokumente + Mail; Web-/Chatarchiv ausgeschlossen)."
        ),
    )

    search_spec: SearchSpecRequest | None = Field(
        default=None,
        description=(
            "Optionaler Query-Rewrite mit Nextcloud-kompatibler elastic_query, "
            "semantic_query und Analysemetadaten."
        ),
    )

    query_context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Interner, bereits vor dem Rewrite ermittelter Neo4j Seed-/Alias-Kontext. "
            "Verhindert eine zweite identische Entity-Auflösung im selben Request."
        ),
    )

    raw_results: bool = Field(
        default=False,
        description=(
            "Retrieval-only-Ausgabe: kein Cross-Encoder-Reranker; Ergebnisse "
            "bleiben in nativer/RRF-Reihenfolge. Für /list:raw im Provider."
        ),
    )

    force_unspecific: bool = Field(
        default=False,
        description=(
            "Überspringt ausschließlich den frühen Abbruch bei einem sehr breiten/"
            "unspezifischen Trefferbild. Reranker und Evidence Controller bleiben aktiv."
        ),
    )


class MultiSearchProbe(BaseModel):
    query: str = Field(..., min_length=1)
    kind: str = Field(default="semantic")
    semantic_query: str = Field(default="")
    retrieval_arms: list[str] | None = Field(default=None)


class MultiSearchRequest(BaseModel):
    model: str | None = Field(
        default=None,
        description="SunaQ model/profile id. Omitted = configured default.",
    )
    original_query: str = Field(..., min_length=1)
    probes: list[MultiSearchProbe] = Field(default_factory=list, max_length=32)
    limit: int | None = Field(default=None, ge=1, le=100)
    exhaustive: bool = Field(default=False)
    strict_query: str = Field(default="")
    strict_required_document_ids: list[str] = Field(default_factory=list, max_length=100)
    force_unspecific: bool = Field(default=False)


class DocumentResolveRequest(BaseModel):

    model: str | None = Field(
        default=None,
        description="SunaQ model/profile id. Omitted = configured default.",
    )

    query: str = Field(
        default="",
        description="Arbeitsanweisung für die kontextbezogene Dokumentnutzung.",
    )

    references: list[str] = Field(
        ...,
        min_length=1,
        max_length=60,
        description=(
            "Explizite Dokumentreferenzen: files:<id>, vollständiger Nextcloud-Pfad, "
            "exakter Dateiname oder interner webarchive:<Pfad>-Verweis."
        ),
    )


class PlanRequest(BaseModel):

    model: str | None = Field(
        default=None,
        description="SunaQ model/profile id. Omitted = configured default.",
    )

    query: str = Field(
        ...,
        description=(
            "Natürlich formulierte Suchanfrage."
        ),
        min_length=1,
    )

    entity_recall: bool = False


class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Öffentliche Web-Recherche. Suchsnippets werden nie als Evidence verwendet.")


class WebArchiveFinalizeRequest(BaseModel):
    run_path: str = Field(..., min_length=1, description="Von /web/search zurückgegebener Webarchiv-Laufpfad.")
    answer_text: str = Field(default="", description="Tatsächliche LLM-Ausgabe der Web-Antwort.")
    answer_model: str = Field(default="", description="Für die Web-Antwort verwendetes Modell.")
    answer_parameters: dict[str, Any] = Field(default_factory=dict)


class GraphEvidenceDocument(BaseModel):

    document_id: str = Field(..., min_length=1)
    title: str | None = None
    path: str | None = None
    source_url: str | None = None
    document_date: str | None = None
    context_text: str | None = None


class GraphEvidenceRequest(BaseModel):

    query_id: str | None = None
    user_query: str | None = None
    retrieval_query: str | None = None
    evidence_action: str = "answer"
    entity_discovery: bool | None = None
    relation_discovery: bool | None = None
    documents: list[GraphEvidenceDocument] = Field(default_factory=list, max_length=8)


class ChatArchiveRegisterRequest(BaseModel):

    document_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class ResearchFindingDocument(BaseModel):

    document_id: str = Field(..., min_length=1)
    title: str | None = None
    path: str | None = None
    source_url: str | None = None
    document_date: str | None = None
    source_origin: str | None = None
    verification_status: str = "match"
    relation_binding: str = "direct"
    evidence_frame: dict[str, Any] = Field(default_factory=dict)


class ResearchFindingRequest(BaseModel):

    query_id: str | None = None
    canonical_user_id: str = ""
    nextcloud_login: str = ""
    nextcloud_server: str = ""
    user_query: str = ""
    retrieval_query: str = ""
    source_scopes: list[str] | None = None
    provenance_code: str = "aki_research"
    provenance_label: str = "SunaQ Recherche"
    query_frame: dict[str, Any] = Field(default_factory=dict)
    software_version: str = ""
    planner_model: str = ""
    verifier_model: str = ""
    documents: list[ResearchFindingDocument] = Field(default_factory=list, max_length=60)


class GraphDocumentRequest(BaseModel):

    document_id: str = Field(..., min_length=1)


# ------------------------------------------------------------
# Ergebnis für API vereinfachen
# ------------------------------------------------------------

def result_to_dict(
    item: dict[str, Any],
) -> dict[str, Any]:

    es_snippet = str(item.get("es_snippet") or "").strip()
    vector_snippet = str(item.get("vector_snippet") or "").strip()
    graph_snippet = str(item.get("graph_snippet") or "").strip()
    context_text = str(item.get("context_text") or "").strip()

    if not context_text:
        parts = []
        if graph_snippet:
            parts.append("Graph-Ausschnitt:\n" + graph_snippet)
        if es_snippet and es_snippet.casefold() != graph_snippet.casefold():
            parts.append("Elasticsearch-Ausschnitt:\n" + es_snippet)
        if (
            vector_snippet
            and vector_snippet.casefold() != es_snippet.casefold()
            and vector_snippet.casefold() != graph_snippet.casefold()
        ):
            parts.append("Semantischer Chunk:\n" + vector_snippet)
        context_text = "\n\n".join(parts).strip()


    return {

        "rank":
            item.get(
                "final_rank"
            ),

        "rrf_rank":
            item.get(
                "rrf_rank"
            ),

        "document_id":
            item.get(
                "document_id"
            ),

        "title":
            item.get(
                "title"
            ),

        "reranker_score":
            item.get(
                "reranker_score"
            ),

        "reranker_raw_score":
            item.get(
                "reranker_raw_score"
            ),

        "rrf_score":
            item.get(
                "rrf"
            ),

        "elasticsearch_rank":
            item.get(
                "es_rank"
            ),

        "elasticsearch_score":
            item.get(
                "es_score"
            ),

        "vector_rank":
            item.get(
                "vector_rank"
            ),

        "vector_score":
            item.get(
                "vector_score"
            ),

        "graph_rank":
            item.get(
                "graph_rank"
            ),

        "graph_score":
            item.get(
                "graph_score"
            ),

        "graph_reason":
            item.get(
                "graph_reason"
            ),

        "graph_entities":
            item.get(
                "graph_entities",
                [],
            ),

        "graph_direct_relations":
            item.get(
                "graph_direct_relations",
                [],
            ),

        "graph_indirect_chains":
            item.get(
                "graph_indirect_chains",
                [],
            ),

        # Kurze Diagnose-Aliase. Die ausführlichen Feldnamen oben bleiben
        # der stabile API-Vertrag; diese Aliase erleichtern CLI/jq-Tests.
        "es_rank": item.get("es_rank"),
        "es_score": item.get("es_score"),
        "rerank_score": item.get("reranker_score"),
        "score": (
            item.get("reranker_score")
            if item.get("reranker_score") is not None
            else item.get("rrf")
        ),

        "chunk_no":
            item.get(
                "chunk_no"
            ),

        # Dokument-Metadaten aus search.py unverändert durchreichen.
        # Diese Felder werden u. a. vom OpenWebUI-Provider für
        # deterministische Nextcloud-Links und Datumsangaben genutzt.
        "path":
            item.get(
                "path"
            ),

        "directory":
            item.get(
                "directory"
            ),

        "filename":
            item.get(
                "filename"
            ),

        "nextcloud_openfile_id":
            item.get(
                "nextcloud_openfile_id"
            ),

        "document_date":
            item.get(
                "document_date"
            ),

        "source_date": item.get("source_date"),
        "source_date_precision": item.get("source_date_precision"),
        "source_date_confidence": item.get("source_date_confidence"),
        "source_date_basis": item.get("source_date_basis"),
        "source_origin": item.get("source_origin"),
        "webarchive_direct": bool(item.get("webarchive_direct", False)),
        "acl_verified_by": item.get("acl_verified_by"),

        "content_kind":
            item.get(
                "content_kind"
            ),

        "content_available":
            item.get(
                "content_available"
            ),

        "source_url":
            item.get(
                "source_url"
            ),

        "owner":
            item.get(
                "owner"
            ),

        "users":
            item.get(
                "users"
            ),

        "groups":
            item.get(
                "groups"
            ),

        "circles":
            item.get(
                "circles"
            ),

        "duplicate_variants":
            item.get(
                "duplicate_variants",
                [],
            ),

        # Beide Retrieval-Sichten separat erhalten.  Der Provider kann damit
        # nachvollziehen, was aus ES bzw. Qdrant stammt.
        "es_snippet":
            es_snippet,

        "vector_snippet":
            vector_snippet,

        "graph_snippet":
            str(item.get("graph_snippet") or "").strip(),

        # Antwortkontext nach der gezielten Vertiefung der finalen Treffer.
        "context_text":
            context_text,

        "context_enriched":
            bool(item.get("context_enriched", False)),

        # Abwärtskompatibilität: ältere Provider lesen weiterhin "text".
        "text":
            context_text,
    }


# ------------------------------------------------------------
# Health
# ------------------------------------------------------------

def _health_endpoint_label(url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.hostname or parsed.netloc or str(url or "")
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def _short_http_error(exc: Exception, url: str) -> str:
    """Compact UI-safe health error; full exception remains available in DEBUG."""
    endpoint = _health_endpoint_label(url)
    text = str(exc or "")
    folded = text.casefold()
    if isinstance(exc, requests.Timeout) or "timed out" in folded or "timeout" in folded:
        return f"Timeout ({endpoint})"
    if isinstance(exc, requests.ConnectionError):
        if "connection refused" in folded or "errno 111" in folded:
            return f"Connection refused ({endpoint})"
        return f"Connection failed ({endpoint})"
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) is not None:
        return f"HTTP {response.status_code} ({endpoint})"
    return f"{type(exc).__name__} ({endpoint})"


def _probe_http(url: str, *, auth=None, verify=True, timeout: float = 4.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = requests.get(url, auth=auth, verify=verify, timeout=timeout)
        response.raise_for_status()
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "http_status": int(response.status_code),
            "response": response,
        }
    except Exception as exc:
        log.debug("Health HTTP probe failed url=%s: %s: %s", url, type(exc).__name__, exc)
        return {
            "status": "down",
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": _short_http_error(exc, url),
        }


def _health_elasticsearch(timeout: float) -> dict[str, Any]:
    if not bool(cfg_get(app_config, "elasticsearch.enabled", default=True)):
        return {"status": "disabled", "backend": "elasticsearch", "index": ES_INDEX}
    options = elastic_requests_options(app_config)
    result = _probe_http(
        f"{ES_URL.rstrip('/')}/{ES_INDEX}/_count",
        timeout=timeout,
        **options,
    )
    result.pop("response", None)
    result.update({"backend": "elasticsearch", "index": ES_INDEX})
    return result


def _health_embedding(timeout: float) -> dict[str, Any]:
    # Ollama exposes a cheap model-list endpoint which lets health verify both
    # connectivity and model presence without generating an embedding.
    if EMBEDDING_BACKEND in {"ollama", "native_ollama"}:
        result = _probe_http(f"{EMBEDDING_URL.rstrip('/')}/api/tags", timeout=timeout)
        response = result.pop("response", None)
        result.update({"backend": "ollama", "model": EMBEDDING_MODEL})
        if response is not None:
            try:
                payload = response.json()
                names = {
                    str(item.get("name") or item.get("model") or "")
                    for item in (payload.get("models") or [])
                    if isinstance(item, dict)
                }
                base = EMBEDDING_MODEL.split(":", 1)[0]
                present = EMBEDDING_MODEL in names or any(
                    name.split(":", 1)[0] == base for name in names if name
                )
                result["model_present"] = bool(present)
                if not present:
                    result["status"] = "down"
                    result["error"] = f"Embedding-Modell nicht installiert: {EMBEDDING_MODEL}"
            except Exception as exc:
                result["status"] = "down"
                result["error"] = f"Ungültige Ollama-Antwort: {type(exc).__name__}: {exc}"
        return result

    # OpenAI-compatible embedding APIs do not define a universally available,
    # side-effect-free health endpoint. Do not create billable embeddings from
    # /health. Configuration is reported as ready; real request failures still
    # fail the vector arm at query time.
    return {
        "status": "ok",
        "backend": EMBEDDING_BACKEND,
        "model": EMBEDDING_MODEL,
        "url": EMBEDDING_URL,
        "probe": "configuration-only",
    }


def _health_qdrant(timeout: float) -> dict[str, Any]:
    """Distinguish an absent first-sync collection from a dead Qdrant service."""
    if not bool(cfg_get(app_config, "qdrant.enabled", default=True)):
        return {"status": "disabled", "backend": "qdrant", "collection": QDRANT_COLLECTION}
    started = time.perf_counter()
    collection_url = f"{QDRANT_URL.rstrip('/')}/collections/{QDRANT_COLLECTION}"
    try:
        response = requests.get(collection_url, timeout=timeout)
        latency = round((time.perf_counter() - started) * 1000.0, 1)
        if response.status_code == 404:
            # A blank installation legitimately has no collection until the
            # first non-empty sync determines the embedding vector dimension.
            server = requests.get(f"{QDRANT_URL.rstrip('/')}/collections", timeout=timeout)
            server.raise_for_status()
            return {
                "status": "uninitialized",
                "backend": "qdrant",
                "collection": QDRANT_COLLECTION,
                "latency_ms": latency,
                "http_status": 404,
                "message": "Collection noch nicht angelegt; erster Dokument-Sync initialisiert Qdrant.",
            }
        response.raise_for_status()
        return {
            "status": "ok",
            "backend": "qdrant",
            "collection": QDRANT_COLLECTION,
            "latency_ms": latency,
            "http_status": int(response.status_code),
        }
    except Exception as exc:
        return {
            "status": "down",
            "backend": "qdrant",
            "collection": QDRANT_COLLECTION,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _health_neo4j() -> dict[str, Any]:
    if not bool(cfg_get(app_config, "neo4j.enabled", default=True)):
        return {"status": "disabled", "backend": "neo4j"}
    started = time.perf_counter()
    try:
        with GraphStore.from_config(app_config) as graph:
            graph.verify_connectivity()
        return {
            "status": "ok",
            "backend": "neo4j",
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    except Exception as exc:
        return {
            "status": "down",
            "backend": "neo4j",
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _health_web(timeout: float) -> dict[str, Any]:
    try:
        cfg = load_web_config() or {}
    except Exception as exc:
        return {
            "enabled": True,
            "status": "down",
            "provider": "unknown",
            "archive_enabled": False,
            "error": f"web.yaml: {type(exc).__name__}: {exc}",
        }
    enabled = bool(cfg.get("enabled", False))
    search_cfg = cfg.get("search") or {}
    archive_cfg = cfg.get("archive") or {}
    provider = str(search_cfg.get("provider") or "").strip().lower() or "unknown"
    result: dict[str, Any] = {
        "enabled": enabled,
        "status": "disabled" if not enabled else "configured",
        "provider": provider,
        "archive_enabled": bool(archive_cfg.get("enabled", False)),
    }
    if not enabled:
        return result
    if provider == "searxng":
        url = str(search_cfg.get("url") or "").strip().rstrip("/")
        result["url"] = url
        if not url:
            result.update({"status": "unconfigured", "error": "web.search.url fehlt"})
            return result
        probe = _probe_http(url + "/", timeout=timeout)
        probe.pop("response", None)
        result.update(probe)
        return result
    if provider == "brave":
        key_env = str(search_cfg.get("api_key_env") or "WEB_SEARCH_API_KEY").strip()
        if not key_env or not secret_env(key_env, "").strip():
            result.update({"status": "unconfigured", "error": f"{key_env or 'WEB_SEARCH_API_KEY'} fehlt"})
            return result
        # Avoid billable external search requests from /health.
        result["status"] = "configured"
        result["probe"] = "configuration-only"
        return result
    result.update({"status": "unconfigured", "error": f"unbekannter Web-Provider: {provider}"})
    return result




@app.post("/auth/nextcloud/start", tags=["auth"], **ZONE_TRUSTED_PROVIDER)
def nextcloud_auth_start(body: NextcloudAuthStartRequest, request: Request) -> dict[str, Any]:
    if not bool(cfg_get(app_config, "auth.nextcloud_login_flow_enabled", default=True)):
        raise HTTPException(status_code=404, detail="Nextcloud Login Flow is disabled")
    user_id = _auth_user_id(request, body.rag_user_id)
    if not user_id:
        raise HTTPException(status_code=400, detail="RAG user identity missing")
    try:
        credential_store.delete_nextcloud_flows_for_user(user_id)
        return _start_nextcloud_flow_for_user(user_id)
    except Exception as exc:
        log.warning("Nextcloud login flow start failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"Nextcloud Login Flow start failed: {type(exc).__name__}: {exc}") from exc


@app.post("/auth/nextcloud/ensure", tags=["auth"], **ZONE_TRUSTED_PROVIDER)
def nextcloud_auth_ensure(body: NextcloudAuthEnsureRequest, request: Request) -> dict[str, Any]:
    """Ensure that the external request identity is bound to Nextcloud.

    This is the provider-facing just-in-time onboarding endpoint.  It is safe to
    call on every normal chat request: single-user deployments return
    ``not_required``; already-bound users return ``connected``; an unfinished
    Login Flow is polled/reused; otherwise one new Nextcloud Login Flow is
    created for the supplied external identity.
    """
    if not live_acl.enabled or live_acl.identity_mode != "credential_store":
        return {"status": "not_required"}
    if not bool(cfg_get(app_config, "auth.nextcloud_login_flow_enabled", default=True)):
        raise HTTPException(status_code=503, detail="Nextcloud Login Flow is disabled")

    user_id = _auth_user_id(request, body.rag_user_id)
    if not user_id:
        return {"status": "identity_missing"}

    credential = credential_store.get_credential(user_id, "nextcloud")
    if credential is not None:
        # 0.8.2d canonicalizes the fachliche Nextcloud identity.  Existing r1
        # credentials are lazily backfilled when first seen after an upgrade.
        canonical_user = credential_store.get_canonical_user_for_identity(user_id)
        if canonical_user is None:
            canonical_user = credential_store.bind_identity(
                user_id, credential.server or _nextcloud_login_base(), credential.username
            )
        if not canonical_user.enabled:
            raise HTTPException(status_code=403, detail="Nextcloud user is disabled by RAG admin")
        return {
            "status": "connected",
            "rag_user_id": user_id,
            "canonical_user_id": canonical_user.canonical_user_id,
            "nextcloud_login": credential.username,
            "server": canonical_user.nextcloud_server,
        }

    try:
        flow = credential_store.get_recent_nextcloud_flow(
            user_id,
            max_age_seconds=_nextcloud_flow_ttl_seconds(),
        )
        if flow is not None:
            result = _poll_nextcloud_flow(flow)
            if result.get("status") == "connected":
                return result
            return result

        # Drop stale flows before creating the new reusable one.
        credential_store.delete_nextcloud_flows_for_user(user_id)
        return _start_nextcloud_flow_for_user(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Nextcloud login flow ensure failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"Nextcloud Login Flow ensure failed: {type(exc).__name__}: {exc}") from exc


@app.get("/auth/nextcloud/status/{flow_id}", tags=["auth"], **ZONE_TRUSTED_PROVIDER)
def nextcloud_auth_status(flow_id: str) -> dict[str, Any]:
    flow = credential_store.get_nextcloud_flow(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Unknown or expired login flow")
    try:
        return _poll_nextcloud_flow(flow)
    except Exception as exc:
        log.warning("Nextcloud login flow poll failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"Nextcloud Login Flow poll failed: {type(exc).__name__}: {exc}") from exc


@app.delete("/auth/nextcloud/{rag_user_id}", tags=["auth"], **ZONE_ADMIN)
def nextcloud_auth_disconnect(rag_user_id: str) -> dict[str, Any]:
    deleted = credential_store.delete_credential(rag_user_id, "nextcloud")
    return {"rag_user_id": rag_user_id, "deleted": bool(deleted)}


@app.get("/live", include_in_schema=False, **ZONE_PUBLIC)
def live():
    """Cheap local liveness endpoint; performs no external probes."""
    return {"ok": True, "service": "nextcloud-hybrid-rag-api", "version": VERSION}


@app.get(
    "/health",
    summary="Status der RAG-Middleware und Retrieval-Arme",
    **ZONE_INTERNAL
)
def health():
    timeout = float(cfg_get(app_config, "health.timeout_seconds", default=4.0) or 4.0)

    files = _health_elasticsearch(timeout)
    qdrant = _health_qdrant(timeout)
    if qdrant.get("status") == "disabled":
        embedding = {
            "status": "disabled",
            "backend": EMBEDDING_BACKEND,
            "model": EMBEDDING_MODEL,
            "reason": "vector arm disabled",
        }
        vector_status = "disabled"
    else:
        embedding = _health_embedding(timeout)
    if qdrant.get("status") == "disabled":
        vector_status = "disabled"
    elif qdrant.get("status") == "ok" and embedding.get("status") == "ok":
        vector_status = "ok"
    elif qdrant.get("status") == "uninitialized" and embedding.get("status") == "ok":
        vector_status = "uninitialized"
    else:
        vector_status = "down"
    vector = {
        "status": vector_status,
        "qdrant": qdrant,
        "embedding": embedding,
    }
    graph = _health_neo4j()
    web = _health_web(timeout)
    queue = graph_queue.stats()
    worker = dict(queue.get("worker") or {})

    arms = {"files": files, "vector": vector, "graph": graph}
    configured_arms = [item for item in arms.values() if item.get("status") != "disabled"]
    healthy_states = {"ok", "uninitialized"}
    arm_ok = sum(1 for item in configured_arms if item.get("status") in healthy_states)
    if not configured_arms:
        overall = "degraded"
    elif arm_ok == 0:
        overall = "down"
    elif arm_ok < len(configured_arms):
        overall = "degraded"
    else:
        overall = "ok"

    # A missing worker should degrade the system only if work is waiting or a
    # supposedly running job exists. An empty queue can legitimately have no
    # worker in small/manual installations.
    if graph_queue.enabled and (int(queue.get("pending") or 0) > 0 or int(queue.get("running") or 0) > 0):
        if worker.get("status") != "ok" and overall == "ok":
            overall = "degraded"
    if web.get("enabled") and web.get("status") == "down" and overall == "ok":
        overall = "degraded"

    return {
        "status": overall,
        "service": "nextcloud-hybrid-rag",
        "version": VERSION,
        "retrieval_arms": arms,
        "graph_worker": worker,
        "graph_queue": {
            "enabled": queue.get("enabled"),
            "pending": queue.get("pending", 0),
            "running": queue.get("running", 0),
            "errors": (queue.get("counts") or {}).get("error", 0),
            "skipped_oversize": (queue.get("counts") or {}).get("skipped_oversize", 0),
            "oldest_pending": queue.get("oldest_pending"),
            "coalesced_total": queue.get("coalesced_total", 0),
        },
        "live_acl": {
            "enabled": bool(live_acl.enabled),
            "identity_mode": live_acl.identity_mode,
            "webdav_url_configured": bool(live_acl.webdav_url),
        },
        "web_evidence": {**web, "config_file": "web.yaml"},
        "reranker": {"status": "configured", **get_reranker_status()},
    }


# ------------------------------------------------------------
# Public Web Evidence (separate from internal retrieval)
# ------------------------------------------------------------

@app.post(
    "/web/search",
    summary="Separate öffentliche Web-Recherche",
    description=(
        "Sucht öffentlich, lädt Treffer tatsächlich ab, lässt nur relevante "
        "Quellen als Evidence zu und archiviert ausgewählte Quellen optional "
        "benutzerspezifisch in Nextcloud. Suchmaschinen-Snippets sind nie Evidence."
    ),
    **ZONE_USER
)
async def web_search(request: WebSearchRequest, http_request: Request):
    try:
        user_id = (
            http_request.headers.get("x-rag-user-id")
            or http_request.headers.get("x-openwebui-user-id")
            or http_request.headers.get("x-open-webui-user-id")
        )
        return await _get_web_arm().research(request.query, rag_user_id=user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Web-Recherche-Fehler: {type(exc).__name__}: {exc}",
        ) from exc


@app.post(
    "/web/archive/finalize",
    summary="Web-Rechercheakte mit LLM-Ausgabe abschließen",
    **ZONE_USER
)
async def web_archive_finalize(request: WebArchiveFinalizeRequest, http_request: Request):
    try:
        user_id = (
            http_request.headers.get("x-rag-user-id")
            or http_request.headers.get("x-openwebui-user-id")
            or http_request.headers.get("x-open-webui-user-id")
        )
        return await _get_web_arm().finalize_archive(
            request.run_path,
            request.answer_text,
            rag_user_id=user_id,
            answer_model=request.answer_model,
            answer_parameters=request.answer_parameters,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Webarchiv-Finalisierung fehlgeschlagen: {type(exc).__name__}: {exc}",
        ) from exc


# ------------------------------------------------------------
# Archive provenance registration
# ------------------------------------------------------------

@app.post(
    "/source-origin/register-chat",
    summary="Register a Nextcloud chat archive document",
    **ZONE_USER
)
def register_chat_archive_source(
    body: ChatArchiveRegisterRequest,
    http_request: Request,
) -> dict[str, Any]:
    rag_user_id = str(http_request.headers.get("x-rag-user-id") or "").strip()
    if not rag_user_id:
        raise HTTPException(status_code=403, detail="RAG user identity missing")
    document_id = str(body.document_id or "").strip()
    path = str(body.path or "").strip().replace("\\", "/").strip("/")
    if not document_id.startswith("files:") or not document_id[6:].isdigit():
        raise HTTPException(status_code=400, detail="document_id must be files:<numeric-id>")
    roots = tuple(chat_archive_roots())
    if not path or not any(path_is_under(path, root) for root in roots):
        raise HTTPException(status_code=400, detail="path is outside the configured chat archive root")

    if not live_acl.enabled:
        raise HTTPException(
            status_code=503,
            detail="Live Nextcloud ACL is required for chat archive registration",
        )
    try:
        canonical_path = live_acl.resolve_visible_file_path(
            document_id,
            rag_user_id=rag_user_id,
        )
    except AclIdentityError as exc:
        raise HTTPException(status_code=403, detail=f"Live ACL denied: {exc}") from exc
    except (AclBackendError, AclConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Live ACL unavailable: {exc}") from exc
    if not canonical_path:
        raise HTTPException(
            status_code=403,
            detail="Live ACL denied chat archive document",
        )
    canonical_path = str(canonical_path).replace("\\", "/").strip("/")
    if not any(path_is_under(canonical_path, root) for root in roots):
        raise HTTPException(
            status_code=400,
            detail="server-derived path is outside the configured chat archive root",
        )
    if canonical_path != path:
        raise HTTPException(
            status_code=400,
            detail="submitted chat archive path does not match Nextcloud",
        )

    changed = register_document(
        document_id,
        "chat_archive",
        source_path=canonical_path,
        classification_source="chat_archive_write",
    )
    mirror = {"checked": 0, "updated": 0, "missing": 0}
    try:
        mirror = auto_mirror_registry_to_elasticsearch(retry_seconds=1.0)
    except Exception as exc:
        log.warning(
            "chat archive source_origin mirror deferred for %s: %s: %s",
            document_id,
            type(exc).__name__,
            exc,
        )
    return {
        "ok": True,
        "document_id": document_id,
        "source_origin": "chat_archive",
        "registered": bool(changed),
        "mirror": mirror,
    }


# ------------------------------------------------------------
# Query rewrite seed context
# ------------------------------------------------------------

@app.post(
    "/query-context",
    summary="Liefere Neo4j Seed-/Alias-Kontext fuer das Query-Rewriting",
    **ZONE_TRUSTED_PROVIDER
)
def query_context(request: PlanRequest):
    """Return query-side entity/alias hints without performing retrieval."""
    runtime_model = _resolve_sunaq_model(request.model)
    try:
        graph_queue.mark_activity("query_context")
        with use_runtime_model_config(runtime_model.config):
            return prepare_entity_context(request.query)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Query-Kontext konnte nicht erzeugt werden: {type(exc).__name__}: {exc}",
        ) from exc


# ------------------------------------------------------------
# Planner
# ------------------------------------------------------------

@app.post(
    "/plan",
    summary="Erzeuge einen Suchplan",
    description=(
        "Analysiert eine natürliche Benutzerfrage "
        "und erzeugt daraus einen strukturierten "
        "Suchplan. Es wird noch keine Dokumentensuche "
        "ausgeführt."
    ),
    **ZONE_TRUSTED_PROVIDER
)
def plan(
    request: PlanRequest,
):

    runtime_model = _resolve_sunaq_model(request.model)
    try:

        graph_queue.mark_activity("plan")
        with use_runtime_model_config(runtime_model.config):
            entity_context = prepare_entity_context(
                request.query
            )

            search_plan = create_plan(
                request.query,
                entity_context=entity_context,
                entity_recall=request.entity_recall,
            )


        return {

            "query":
                request.query,

            "entity_resolution":
                entity_context,

            "plan":
                search_plan.model_dump(),
        }


    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Planner-Fehler: {exc}"
            ),
        )


# ------------------------------------------------------------
# Evidence -> Dokumentgraph
# ------------------------------------------------------------

@app.get(
    "/graph/stats",
    summary="Graph-Statistik",
    **ZONE_ADMIN
)
def graph_stats():
    try:
        cfg = load_config()
        with GraphStore.from_config(cfg) as graph:
            graph.verify_connectivity()
            return graph.stats()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Stats-Fehler: {type(exc).__name__}: {exc}",
        )


@app.post(
    "/graph/document",
    summary="Zeige Dokumentgraph-Diagnose",
    **ZONE_ADMIN
)
def graph_document(request: GraphDocumentRequest):
    try:
        cfg = load_config()
        with GraphStore.from_config(cfg) as graph:
            graph.verify_connectivity()
            return graph.document_summary(request.document_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Dokument-Fehler: {type(exc).__name__}: {exc}",
        )


@app.post(
    "/graph/enqueue-evidence",
    summary="Stelle Evidence-Dokumente in die Graph-Queue",
    description=(
        "Persistiert die vom Evidence Controller ausgewählten Dokumente nur in "
        "SQLite. Die eigentliche Neo4j-Graphifizierung erledigt rag.graph_worker "
        "später außerhalb des Chat-Antwortpfades."
    ),
    **ZONE_INTERNAL
)
def graph_enqueue_evidence(request: GraphEvidenceRequest):
    try:
        payload = request.model_dump()
        return graph_queue.enqueue_evidence(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Queue-Fehler: {type(exc).__name__}: {exc}",
        )


@app.post(
    "/graph/research-findings",
    summary="Persistiere positive SunaQ-Recherche-Findings",
    description=(
        "Speichert ausschließlich bereits vom Retrieval-Planner strukturierten und "
        "vom Candidate-Verifier positiv bestätigten Recherche-Nutzen in Neo4j. "
        "Es wird kein zusätzlicher LLM-/Graph-Extraktionslauf gestartet."
    ),
    **ZONE_INTERNAL
)
def graph_research_findings(request: ResearchFindingRequest):
    global _research_finding_schema_ready

    def enabled_value(path: str, default: bool = False) -> bool:
        value = cfg_get(app_config, path, default=default)
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"1", "true", "yes", "on"}

    enabled = enabled_value("research_findings.enabled", False)
    neo4j_enabled = enabled_value("neo4j.enabled", False)
    if not enabled or not neo4j_enabled:
        return {
            "enabled": False,
            "stored": 0,
            "skipped": len(request.documents),
            "reason": "research_findings_disabled" if not enabled else "neo4j_disabled",
        }

    try:
        with GraphStore.from_config(app_config) as graph:
            graph.verify_connectivity()
            if not _research_finding_schema_ready:
                # Startup may have deferred schema initialization while Neo4j was
                # unavailable. Recovery must apply the complete idempotent upgrade,
                # not only the ResearchFinding subset.
                graph.ensure_schema()
                _research_finding_schema_ready = True
            result = graph.store_research_findings(
                query_id=str(request.query_id or ""),
                query_frame=request.query_frame,
                documents=[item.model_dump() for item in request.documents],
                canonical_user_id=str(request.canonical_user_id or ""),
                nextcloud_login=str(request.nextcloud_login or ""),
                nextcloud_server=str(request.nextcloud_server or ""),
                user_query=str(request.user_query or ""),
                retrieval_query=str(request.retrieval_query or ""),
                source_scopes=request.source_scopes,
                provenance_code=str(request.provenance_code or "aki_research"),
                provenance_label=str(request.provenance_label or "SunaQ Recherche"),
                software_version=str(request.software_version or ""),
                planner_model=str(request.planner_model or ""),
                verifier_model=str(request.verifier_model or ""),
            )
        return {"enabled": True, **result}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Research-Finding-Fehler: {type(exc).__name__}: {exc}",
        )


@app.get(
    "/graph/queue/stats",
    summary="Graph-Queue-Statistik",
    **ZONE_ADMIN
)
def graph_queue_stats():
    try:
        return graph_queue.stats()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Queue-Stats-Fehler: {type(exc).__name__}: {exc}",
        )


@app.get(
    "/graph/queue/jobs",
    summary="Letzte Graph-Queue-Jobs",
    **ZONE_ADMIN
)
def graph_queue_jobs(limit: int = 20):
    try:
        return {"jobs": graph_queue.recent_jobs(limit)}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Queue-Jobs-Fehler: {type(exc).__name__}: {exc}",
        )


@app.post(
    "/graph/index-evidence",
    summary="Graphifiziere Evidence-Dokumente",
    description=(
        "Indexiert ausschließlich die vom Evidence Controller ausgewählten "
        "Dokumente inkrementell in Neo4j. Fuzzy/mehrdeutige Namensformen "
        "bleiben Kandidaten und werden nicht als Identität aufgelöst."
    ),
    **ZONE_INTERNAL
)
def graph_index_evidence(request: GraphEvidenceRequest):

    try:
        cfg = load_config()
        indexer = GraphEvidenceIndexer(cfg)
        return indexer.index_evidence(
            [item.model_dump() for item in request.documents],
            query_id=str(request.query_id or ""),
            user_query=str(request.user_query or ""),
            retrieval_query=str(request.retrieval_query or ""),
            evidence_action=str(request.evidence_action or "answer"),
        )
    except Exception as exc:
        # Endpoint reports the concrete error. The provider hook itself is
        # fail-open and will not abort the user answer.
        raise HTTPException(
            status_code=500,
            detail=f"Graph-Indexer-Fehler: {type(exc).__name__}: {exc}",
        )


# ------------------------------------------------------------
# Explizite Dokumentauswahl (/use)
# ------------------------------------------------------------

@app.post(
    "/documents/resolve",
    summary="Löse explizit ausgewählte Dokumente auf",
    description=(
        "Kein normaler Retrieval-Pfad: löst files:<id>, vollständige Pfade, "
        "exakte Dateinamen sowie archivierte Webquellen deterministisch auf und "
        "liefert Dokumenttext für /use. Mehrdeutige Dateinamen werden nicht geraten."
    ),
    **ZONE_USER
)
async def documents_resolve(body: DocumentResolveRequest, http_request: Request):
    try:
        graph_queue.mark_activity("document_resolve")
        user_id = (
            http_request.headers.get("x-rag-user-id")
            or http_request.headers.get("x-openwebui-user-id")
            or http_request.headers.get("x-open-webui-user-id")
        )

        normal_refs: list[str] = []
        web_refs: list[tuple[str, str]] = []
        if is_src(app_config) and any(
            str(reference or "").strip().casefold().startswith("webarchive:")
            for reference in body.references
        ):
            raise HTTPException(
                status_code=403,
                detail="Web archive references are not available in Secure RAG Core (SRC)",
            )
        for reference in body.references:
            value = str(reference or "").strip()
            if value.casefold().startswith("webarchive:"):
                web_refs.append((value, value.split(":", 1)[1].strip()))
            elif value:
                normal_refs.append(value)

        # Resolve explicit references in two phases.  Elasticsearch may retain
        # stale FullTextSearch rows for files that have already been removed
        # from Nextcloud.  Candidate discovery therefore happens first, then
        # Live-ACL/existence filtering, and only *after that* do we decide
        # unique vs. ambiguous.  This also prevents stale rows from making an
        # otherwise unique /use:<filename> look ambiguous.
        if normal_refs:
            candidate_sets: dict[str, list[dict[str, Any]]] = {}
            candidate_by_id: dict[str, dict[str, Any]] = {}
            for reference in normal_refs:
                matches = _src_documents_only(
                    document_reference_candidates(reference, limit=50)
                )
                deduped: list[dict[str, Any]] = []
                local_seen: set[str] = set()
                for item in matches:
                    document_id = str(item.get("document_id") or "").strip()
                    if not document_id or document_id in local_seen:
                        continue
                    candidate_path = str(
                        item.get("path") or item.get("title") or ""
                    ).strip()
                    if (
                        not CHAT_ARCHIVE_ENABLED
                        and classify_source_origin(
                            candidate_path,
                            document_id=document_id,
                        ) == "chat_archive"
                    ):
                        continue
                    local_seen.add(document_id)
                    deduped.append(item)
                    candidate_by_id.setdefault(document_id, item)
                candidate_sets[reference] = deduped

            candidate_acl = live_acl.authorize(
                list(candidate_by_id.values()),
                rag_user_id=user_id,
            )
            visible_ids = {
                str(item.get("document_id") or "").strip()
                for item in candidate_acl.results
                if str(item.get("document_id") or "").strip()
            }
            if candidate_acl.enabled:
                log.info(
                    "live_acl document_resolve_candidates: checked=%d authorized=%d",
                    candidate_acl.checked, candidate_acl.authorized,
                )

            selected_ids: list[str] = []
            ambiguous: list[dict[str, Any]] = []
            not_found: list[str] = []
            selected_seen: set[str] = set()
            for reference in normal_refs:
                visible = [
                    item
                    for item in candidate_sets.get(reference, [])
                    if (not candidate_acl.enabled)
                    or str(item.get("document_id") or "").strip() in visible_ids
                ]
                if not visible:
                    not_found.append(reference)
                    continue
                if len(visible) > 1:
                    ambiguous.append({
                        "reference": reference,
                        "matches": [
                            {
                                "document_id": item.get("document_id"),
                                "path": item.get("path") or item.get("title"),
                                "title": item.get("title"),
                                "source_url": item.get("source_url"),
                            }
                            for item in visible[:20]
                        ],
                    })
                    continue
                document_id = str(visible[0].get("document_id") or "").strip()
                if document_id and document_id not in selected_seen:
                    selected_seen.add(document_id)
                    selected_ids.append(document_id)

            if selected_ids:
                runtime_model = _resolve_sunaq_model(body.model)
                with use_runtime_model_config(runtime_model.config):
                    payload = resolve_document_references(selected_ids, question=body.query)
            else:
                payload = {
                    "references": [],
                    "results": [],
                    "ambiguous": [],
                    "not_found": [],
                    "resolved_count": 0,
                }
            payload["references"] = list(normal_refs)
            payload["ambiguous"] = ambiguous
            payload["not_found"] = not_found
        else:
            payload = {
                "references": [],
                "results": [],
                "ambiguous": [],
                "not_found": [],
                "resolved_count": 0,
            }

        normal_results = _src_documents_only(list(payload.get("results", [])))
        acl = live_acl.authorize(normal_results, rag_user_id=user_id)
        if acl.enabled:
            print(
                f"[RAG] live_acl document_resolve: checked={acl.checked} authorized={acl.authorized}",
                flush=True,
            )
        final_results = [result_to_dict(item) for item in acl.results]

        web_not_visible: list[str] = []
        for original_ref, archive_path in web_refs:
            item = await _get_web_arm().read_archive_source(
                archive_path, rag_user_id=user_id
            )
            if item is None:
                web_not_visible.append(original_ref)
                continue
            final_results.append(result_to_dict(item))

        for rank, item in enumerate(final_results, start=1):
            item["final_rank"] = rank
            item["rank"] = rank

        payload["references"] = list(body.references)
        payload["results"] = final_results
        payload["resolved_count"] = len(final_results)
        payload["acl_applied"] = bool(acl.enabled)
        # Ambiguity/not-found decisions for normal references were already made
        # from ACL-visible candidates only.  Do not erase them here; denied or
        # stale files are represented simply as unavailable.
        if web_not_visible:
            payload.setdefault("not_found", []).extend(web_not_visible)
        return payload
    except AclIdentityError as exc:
        raise HTTPException(status_code=403, detail=f"Live-ACL verweigert: {exc}")
    except (AclBackendError, AclConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Live-ACL nicht verfügbar: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        _raise_retrieval_exception(
            exc, fallback=f"Dokument-Auflösung fehlgeschlagen: {type(exc).__name__}: {exc}"
        )


# ------------------------------------------------------------
# Direkte Elasticsearch-/Nextcloud-Volltextsuche
# ------------------------------------------------------------

@app.post(
    "/elastic/search",
    summary="Direkte Nextcloud-kompatible Volltextsuche",
    description=(
        "Übernimmt die kleine Suchsyntax der Nextcloud Full Text Search (+/-/Phrasen) "
        "ohne Query Planner, Entity Expansion, Vector/Graph, RRF oder Reranker. "
        "Live-ACL bleibt die Sicherheitsgrenze."
    ),
    **ZONE_USER
)
def elastic_search_endpoint(body: ElasticSearchRequest, http_request: Request):
    try:
        _enforce_architecture_request(source_scopes=body.source_scopes)
        graph_queue.mark_activity("elastic_search")

        # With ACL enabled, inspect only a bounded prefix.  Exact counting of a
        # huge visible field is intentionally not a goal: once the field exceeds
        # the analysis/list window the user must narrow the query.
        scan_limit = body.limit
        if live_acl.enabled:
            scan_limit = min(
                ELASTIC_ACL_RESULT_SCAN_LIMIT,
                max(body.limit, body.limit * 4),
            )

        diagnostics: dict[str, Any] = {}
        prefilter_user, prefilter_groups = _acl_prefilter_context(http_request)
        raw_results = elastic_exact_search(
            body.query,
            limit=scan_limit,
            diagnostics=diagnostics,
            source_scopes=body.source_scopes,
            acl_prefilter_user=prefilter_user,
            acl_prefilter_groups=prefilter_groups,
        )

        acl = live_acl.authorize(
            list(raw_results),
            rag_user_id=http_request.headers.get("x-rag-user-id"),
        )
        visible = [result_to_dict(item) for item in acl.results]
        if acl.enabled:
            log.info("live_acl elastic: checked=%d authorized=%d", acl.checked, acl.authorized)

        es_total = int(diagnostics.get("total_hits") or 0)
        scanned_complete = es_total <= scan_limit
        if not acl.enabled:
            visible_total = es_total
            count_complete = True
        elif scanned_complete:
            visible_total = len(visible)
            count_complete = True
        else:
            visible_total = None
            count_complete = False

        returned = visible[: body.limit]
        if (
            body.include_content
            and count_complete
            and visible_total is not None
            and int(visible_total) <= body.limit
            and returned
        ):
            ids = [str(item.get("document_id") or "") for item in returned]
            runtime_model = _resolve_sunaq_model(body.model)
            with use_runtime_model_config(runtime_model.config):
                hydrated_payload = resolve_document_references(ids, question=body.query)
            hydrated_by_id = {
                str(item.get("document_id") or ""): item
                for item in (hydrated_payload.get("results") or [])
            }
            merged: list[dict[str, Any]] = []
            for item in returned:
                doc_id = str(item.get("document_id") or "")
                hydrated = dict(hydrated_by_id.get(doc_id) or {})
                if hydrated:
                    # Preserve the direct-search rank/score/snippet while adding
                    # the hydrated document text and content flags.
                    hydrated["rank"] = item.get("rank")
                    hydrated["final_rank"] = item.get("rank")
                    hydrated["es_rank"] = item.get("es_rank") or item.get("rank")
                    hydrated["es_score"] = item.get("es_score")
                    hydrated["score"] = item.get("es_score")
                    hydrated["es_snippet"] = item.get("es_snippet") or hydrated.get("es_snippet")
                    merged.append(result_to_dict(hydrated))
                else:
                    merged.append(item)
            returned = merged

        # Never expose the pre-ACL ES total while live ACL is active.
        return {
            "query": body.query,
            "mode": "elastic_exact",
            "count_complete": count_complete,
            "total_hits": visible_total,
            "total_relation": "eq" if count_complete else "gte",
            "visible_scanned": len(visible),
            "scan_limit": scan_limit if acl.enabled else body.limit,
            "acl_applied": bool(acl.enabled),
            "content_included": bool(body.include_content),
            "results": returned,
        }
    except AclIdentityError as exc:
        raise HTTPException(status_code=403, detail=f"Live-ACL verweigert: {exc}")
    except (AclBackendError, AclConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Live-ACL nicht verfügbar: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        _raise_retrieval_exception(
            exc, fallback=f"Direkte Elasticsearch-Suche fehlgeschlagen: {type(exc).__name__}: {exc}"
        )


# ------------------------------------------------------------
# RC8 Multi-Probe-Suche
# ------------------------------------------------------------

def _dedupe_acl_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        document_id = str(item.get("document_id") or "").strip()
        key = document_id or str(item.get("title") or item.get("path") or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


@app.post(
    "/multi-search",
    summary="RC8 Multi-Probe Retrieval mit gemeinsamem Reranking",
    description=(
        "Führt mehrere konservative Retrieval-Probes über die bestehenden "
        "ES/Qdrant/Graph-Arme aus, fusioniert die Probe-Ranglisten per RRF und "
        "rerankt die gemeinsame Kandidatenmenge einmal gegen die Originalfrage."
    ),
    **ZONE_USER
)
def multi_search(body: MultiSearchRequest, http_request: Request):
    try:
        graph_queue.mark_activity("multi_search")
        rag_user_id = http_request.headers.get("x-rag-user-id")
        if live_acl.enabled and live_acl.identity_mode == "credential_store":
            live_acl.credential_for_user(rag_user_id)

        runtime_model = _resolve_sunaq_model(body.model)
        effective_limit = (
            body.limit if body.limit is not None else _profile_final_limit(runtime_model)
        )
        profile_planner = load_retrieval_planner_settings(runtime_model.config)
        probes = [probe.model_dump() for probe in body.probes]
        if not probes:
            probes = [{
                "query": body.original_query,
                "kind": "original",
                "semantic_query": body.original_query,
                "retrieval_arms": None,
            }]
        for probe in probes:
            probe["retrieval_arms"] = _enforce_architecture_request(
                retrieval_arms=probe.get("retrieval_arms"),
            )

        files_requested = any(
            probe.get("retrieval_arms") is None
            or "files" in {str(value).strip().lower() for value in (probe.get("retrieval_arms") or [])}
            for probe in probes
        )

        max_complete = profile_planner.max_complete_documents
        required_results: list[dict[str, Any]] = []

        # RC8 hotfix: an explicitly named complete filename is a deterministic
        # document selector, not merely another relevance signal.  Resolve it
        # before multi-probe fusion and apply Live-ACL before exposing or
        # seeding anything.  Pure filename navigation returns immediately; a
        # filename embedded in an analysis request is hydrated and made a
        # mandatory candidate so joint reranking cannot discard it.
        explicit_filename = extract_filename(body.original_query) if files_requested else None
        if explicit_filename:
            filename_matches = _src_documents_only(
                strict_filename_lookup(
                    explicit_filename,
                    limit=max(20, effective_limit),
                )
            )
            filename_acl = live_acl.authorize(
                filename_matches,
                rag_user_id=rag_user_id,
            )
            visible_filename_matches = list(filename_acl.results)
            if filename_acl.enabled:
                log.info(
                    "RC8 filename ACL: checked=%d authorized=%d filename=%r",
                    filename_acl.checked, filename_acl.authorized, explicit_filename,
                )

            if is_pure_filename_lookup(body.original_query, explicit_filename):
                exact_results = [result_to_dict(item) for item in visible_filename_matches]
                return {
                    "query": body.original_query,
                    "model": runtime_model.model_id,
                    "plan": {},
                    "entity_resolution": {},
                    "retrieval_mode": "filename_exact" if exact_results else "filename_not_found",
                    "retrieval_strategy": "filename_exact",
                    "retrieval_message": "",
                    "lookup_filename": explicit_filename,
                    "retrieval_arms": ["files"],
                    "raw_results": False,
                    "force_unspecific": bool(body.force_unspecific),
                    "retrieval_signal": {},
                    "reranker_used": False,
                    "reranker_error": None,
                    "timings": {},
                    "statistics": {"filename_matches": len(exact_results), "returned_results": len(exact_results)},
                    "acl_applied": bool(filename_acl.enabled),
                    "graph_orientation_available": False,
                    "planner_probes": [],
                    "strict_gate_checked": False,
                    "strict_required_document_ids": [],
                    "results": exact_results,
                }

            # For "analyse <filename>" only seed an unambiguous visible file.
            # Hydration reuses the established /use path and therefore fetches
            # the actual Elasticsearch document content rather than a filename
            # snippet.
            if len(visible_filename_matches) == 1:
                visible_id = str(visible_filename_matches[0].get("document_id") or "").strip()
                with use_runtime_model_config(runtime_model.config):
                    resolved = resolve_document_references(
                        [explicit_filename], question=body.original_query
                    )
                for item in resolved.get("results") or []:
                    if str(item.get("document_id") or "").strip() == visible_id:
                        required_results.append(dict(item))
                        break

        strict_required_ids = list(dict.fromkeys(
            str(value or "").strip()
            for value in body.strict_required_document_ids
            if str(value or "").strip()
        ))[:max_complete]
        if strict_required_ids:
            # Later planner rounds reuse the already authorized strict set from
            # round 1.  No second broad ACL preflight is performed.  These
            # exact ids are still checked by the normal final Live-ACL below.
            required_results.extend(
                _src_documents_only(document_ids_lookup(strict_required_ids))
            )
            required_results = _dedupe_acl_candidates(required_results)

        # Recall-draft1: natural-language exhaustive search no longer creates
        # a mandatory Boolean completeness query.  Candidate generation remains
        # broad; Live-ACL is applied to the bounded candidate pool below and
        # the provider performs document-based verification afterwards.
        # The legacy request fields stay accepted for wire compatibility but
        # are intentionally not used as a natural-language completeness gate.

        prefilter_user, prefilter_groups = _acl_prefilter_context(http_request)
        with use_runtime_model_config(runtime_model.config):
            search_result = perform_multi_probe_search(
                original_question=body.original_query,
                probes=probes,
                limit=effective_limit,
                required_results=required_results,
                force_unspecific=body.force_unspecific,
                acl_prefilter_user=prefilter_user,
                acl_prefilter_groups=prefilter_groups,
            )

        # Final security boundary remains live Nextcloud authorization.  The
        # strict-query preflight is only a bounded completeness gate, never a
        # replacement for the normal final ACL.
        acl = live_acl.authorize(
            list(search_result.get("results") or []),
            rag_user_id=rag_user_id,
        )
        if acl.enabled:
            log.info("live_acl multi_search: checked=%d authorized=%d", acl.checked, acl.authorized)
        results = [result_to_dict(item) for item in acl.results]

        plan = search_result.get("plan")
        return {
            "query": body.original_query,
            "model": runtime_model.model_id,
            "plan": plan.model_dump() if plan is not None else {},
            "entity_resolution": search_result.get("entity_resolution", {}),
            "search_spec": search_result.get("search_spec", {}),
            "retrieval_mode": search_result.get("retrieval_mode"),
            "retrieval_strategy": search_result.get("retrieval_strategy"),
            "retrieval_message": search_result.get("retrieval_message", ""),
            "retrieval_arms": search_result.get("retrieval_arms"),
            "raw_results": False,
            "force_unspecific": bool(body.force_unspecific),
            "retrieval_signal": {} if acl.enabled else search_result.get("retrieval_signal", {}),
            "reranker_used": search_result.get("reranker_used"),
            "reranker_error": search_result.get("reranker_error"),
            "timings": search_result.get("timings", {}),
            "statistics": (
                {"returned_results": len(results)}
                if acl.enabled
                else search_result.get("statistics", {})
            ),
            "acl_applied": bool(acl.enabled),
            "graph_orientation_available": False,
            "planner_probes": search_result.get("planner_probes", []),
            "strict_gate_checked": False,
            "strict_required_document_ids": [],
            "results": results,
        }
    except AclIdentityError as exc:
        raise HTTPException(status_code=403, detail=f"Live-ACL verweigert: {exc}")
    except (AclBackendError, AclConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Live-ACL nicht verfügbar: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        _raise_retrieval_exception(
            exc, fallback=f"Multi-Probe-Suche fehlgeschlagen: {type(exc).__name__}: {exc}"
        )


# ------------------------------------------------------------
# Suche
# ------------------------------------------------------------

@app.post(
    "/search",
    summary=(
        "Durchsuche die Nextcloud-Dokumente"
    ),
    description=(
        "Plant die Anfrage, sucht über die gewählten Retrieval-Arme, kombiniert "
        "Kandidaten per Reciprocal Rank Fusion und bewertet die besten Treffer "
        "anschließend mit einem Cross-Encoder-Reranker."
    ),
    **ZONE_USER
)
def search(
    body: SearchRequest,
    http_request: Request,
):

    try:

        effective_arms = _enforce_architecture_request(
            retrieval_arms=body.retrieval_arms,
            source_scopes=body.source_scopes,
        )
        graph_queue.mark_activity("search")

        # Multi-user fail-fast boundary: do not spend retrieval/reranker work on
        # a request that cannot possibly pass the live ACL.  The provider's
        # /auth/nextcloud/ensure preflight normally handles onboarding before
        # reaching this endpoint; direct API callers still fail closed here.
        if live_acl.enabled and live_acl.identity_mode == "credential_store":
            live_acl.credential_for_user(http_request.headers.get("x-rag-user-id"))

        runtime_model = _resolve_sunaq_model(body.model)
        effective_limit = (
            body.limit
            if body.limit is not None
            else _profile_final_limit(runtime_model)
        )

        prefilter_user, prefilter_groups = _acl_prefilter_context(http_request)
        with use_runtime_model_config(runtime_model.config):
            search_result = perform_search(
                question=body.query,
                limit=effective_limit,
                entity_recall=body.entity_recall,
                retrieval_arms=effective_arms,
                source_scopes=body.source_scopes,
                raw_results=body.raw_results,
                # With live ACL enabled the broad-field stop must not happen before
                # authorization: otherwise "unspecific" itself becomes an oracle
                # for hidden repository contents. Continue internally, then decide
                # the user-visible outcome from the ACL-visible candidate set.
                force_unspecific=(body.force_unspecific or live_acl.enabled),
                search_spec=(body.search_spec.model_dump() if body.search_spec is not None else None),
                entity_context_override=body.query_context,
                acl_prefilter_user=prefilter_user,
                acl_prefilter_groups=prefilter_groups,
            )

        # Security boundary: authorization is deliberately applied only after
        # the retrieval/ranking decision.  Denied results are removed; lower
        # ranked candidates are never pulled in as replacements.
        acl = live_acl.authorize(
            list(search_result["results"]),
            rag_user_id=http_request.headers.get("x-rag-user-id"),
        )
        if acl.enabled:
            log.info("live_acl search: checked=%d authorized=%d", acl.checked, acl.authorized)

        results = [result_to_dict(item) for item in acl.results]

        # The retrieval signal is computed on the index-wide candidate field.
        # When live ACL is active, never expose a broad/unspecific judgment until
        # at least one of the bounded final candidates is visible to this user.
        # A user with zero authorized candidates gets the ordinary no-results
        # path instead, so repository breadth cannot be inferred through wording.
        signal_decision = dict(
            (search_result.get("retrieval_signal") or {}).get("decision") or {}
        )
        acl_guarded_unspecific = bool(
            acl.enabled
            and not body.force_unspecific
            and str(signal_decision.get("strategy") or "") == "unspecific"
            and str(signal_decision.get("applied_strategy") or "") == "forced_fusion"
        )
        response_retrieval_mode = search_result.get("retrieval_mode")
        response_retrieval_message = search_result.get("retrieval_message", "")
        if acl_guarded_unspecific:
            if results:
                response_retrieval_mode = "unspecific"
                response_retrieval_message = UNSPECIFIC_RETRIEVAL_MESSAGE
                results = []
                log.info(
                    "ACL-gated unspecific feedback: authorized=%d -> unspecific",
                    acl.authorized,
                )
            else:
                response_retrieval_mode = "no_results"
                response_retrieval_message = ""
                log.info(
                    "ACL-gated unspecific feedback: authorized=0 -> no_results"
                )

        # Broad entity queries may carry Graph document ids only as private ACL
        # probes.  Never expose the candidate list itself: one authorized linked
        # document is enough for the safe existence hint returned to the provider.
        graph_orientation_available = False
        orientation_candidates = list(search_result.get("orientation_candidates") or [])
        if str(search_result.get("retrieval_mode") or "") in {"unspecific", "too_unspecific"} and orientation_candidates:
            orientation_acl = live_acl.authorize(
                orientation_candidates,
                rag_user_id=http_request.headers.get("x-rag-user-id"),
            )
            graph_orientation_available = bool(orientation_acl.results)
            if orientation_acl.enabled:
                log.info(
                    "live_acl graph_orientation: checked=%d authorized=%d",
                    orientation_acl.checked, orientation_acl.authorized,
                )

        return {

            "query":
                body.query,

            "model":
                runtime_model.model_id,

            "plan":
                (
                    search_result["plan"].model_dump()
                    if search_result.get("plan") is not None
                    else {}
                ),

            "entity_resolution":
                search_result.get(
                    "entity_resolution",
                    {},
                ),

            "search_spec":
                search_result.get(
                    "search_spec",
                    {},
                ),

            "retrieval_mode":
                response_retrieval_mode,

            "retrieval_strategy":
                search_result.get(
                    "retrieval_strategy"
                ),

            "retrieval_message":
                response_retrieval_message,

            "retrieval_arms":
                search_result.get("retrieval_arms", effective_arms),

            "source_scopes":
                search_result.get("source_scopes", body.source_scopes),

            "raw_results":
                bool(search_result.get("raw_results", body.raw_results)),

            # Report the caller's explicit control flag, not the internal
            # ACL-safety override used to defer broad-field feedback.
            "force_unspecific":
                bool(body.force_unspecific),

            "retrieval_signal": (
                {}
                if acl.enabled
                else search_result.get("retrieval_signal", {})
            ),

            "lookup_filename":
                search_result.get(
                    "lookup_filename"
                ),

            "reranker_used":
                search_result.get(
                    "reranker_used"
                ),

            "reranker_error":
                search_result.get(
                    "reranker_error"
                ),

            "timings":
                search_result.get(
                    "timings",
                    {},
                ),

            # Do not expose pre-ACL candidate counts to a normal caller.
            # Server logs retain the diagnostic counts, but the response only
            # describes the evidence this user is actually allowed to receive.
            "statistics": (
                {"returned_results": len(results)}
                if acl.enabled
                else search_result["statistics"]
            ),

            "acl_applied": bool(acl.enabled),

            # Safe post-ACL boolean only; no hidden Graph relationship/document
            # metadata is exposed by the broad-query orientation path.
            "graph_orientation_available": graph_orientation_available,

            "results":
                results,
        }


    except AclIdentityError as exc:
        raise HTTPException(status_code=403, detail=f"Live-ACL verweigert: {exc}")
    except (AclBackendError, AclConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=f"Live-ACL nicht verfügbar: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        _raise_retrieval_exception(exc, fallback=f"Suchfehler: {exc}")
