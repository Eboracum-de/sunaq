"""OpenAI-compatible provider for SunaQ.

Architecture:
    OpenAI-compatible UI/client -> this provider (:8766) -> RAG middleware (:8765)
                                                       -> configured LLM backend

The provider intentionally owns retrieval/web/evidence orchestration. UI-specific
helper requests are treated only as compatibility traffic and never as control
input for the RAG pipeline.

v0.5.8 changes:
- /force bypasses only the broad/unspecific early retrieval stop
- /list is now post-reranker browse output; /list:raw preserves the old raw list
- /use selects documents explicitly by prior source number, filename or exact path
- prior source numbers are recoverable from provider markers/openfile links

v0.4.3 changes:
- enriched final-document context from middleware
- compact evidence-review context + strict JSON schema
- stronger relation-direction / neutral-keyword answer rules

v0.4 changes:
- OpenWebUI/Ollama generation-parameter bridge
- SQLite research provenance (ranked/review_context/answer_context/cited)
- optional evidence control: answer/retry/clarify/insufficient
- up to MAX_RETRIEVAL_ROUNDS search rounds

v0.3 changes:
- deterministic Nextcloud source links for cited documents
- source links are built from path + nextcloud_openfile_id, never by the LLM

v0.2 changes:
- external prompt files
- raw chat history is no longer passed to the RAG answer model
- optional history-aware follow-up rewrite produces a standalone retrieval query
- explicit retrieval-query logging for planner debugging
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
import re
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from typing import Any, AsyncIterator

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from rag.version import VERSION
from rag.search_text import normalize_query_quotes, preserve_explicit_quoted_phrases
from rag.research_log import ResearchLog
from rag.llm_backend import build_llm_backend
from rag.llm_roles import build_role_backends
from rag.architecture_policy import (
    architecture_tier,
    is_src,
    request_capability_error,
    validate_role_backends,
    validate_runtime_model,
)
from rag.sunaq_models import RuntimeModel, load_model_registry
from rag.logging_utils import get_logger
from rag.credential_store import CredentialStore, scope_identity
from rag.internal_auth import provider_api_headers
from rag.tls_compat import configure_tls_compat
from rag.retrieval_policy import configured_internal_arms, load_retrieval_policy
from rag.retrieval_planner import (
    detect_bounded_document_set,
    detect_exhaustive_intent,
    extract_complete_filename,
    extract_safe_hard_constraints,
    load_retrieval_planner_settings,
    normalize_generated_probes,
    normalize_query_frame,
    original_probe,
    safe_constraint_summary,
    semanticize_query,
)
from rag.elastic_query import nextcloud_query_tokens
from rag.search_spec import (
    fingerprint as search_spec_fingerprint,
    normalize_search_spec,
    positive_lexical_terms,
    query_frame_from_search_spec,
)
from rag.source_origin import source_scope_allows_record
from rag.evidence_boundary import (
    fit_evidence_line_records,
    fit_evidence_records,
    guarded_evidence_prompt,
    serialize_evidence_records,
)
from rag.secret_env import secret_env


log = get_logger("provider")


def _load_provider_config() -> dict[str, Any]:
    config_path = Path(os.getenv("RAG_CONFIG_FILE", "config.yaml"))
    try:
        with config_path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle) or {}
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        log.warning("Could not read provider config %s: %s", config_path, exc)
        return {}


PROVIDER_CONFIG = _load_provider_config()
configure_tls_compat(PROVIDER_CONFIG)
SUNAQ_MODEL_REGISTRY = load_model_registry(PROVIDER_CONFIG)
RETRIEVAL_PLANNER = load_retrieval_planner_settings(PROVIDER_CONFIG)
RETRIEVAL_POLICY = load_retrieval_policy(PROVIDER_CONFIG)
CONFIGURED_INTERNAL_ARMS = configured_internal_arms(PROVIDER_CONFIG)


def _config_truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


_RETRIEVAL_RECORD_CONFIG = dict(PROVIDER_CONFIG.get("retrieval_record") or {})
RETRIEVAL_RECORD_ENABLED = _config_truthy(
    _RETRIEVAL_RECORD_CONFIG.get("enabled"), False
)
RETRIEVAL_RECORD_DIRECTORY = Path(
    str(_RETRIEVAL_RECORD_CONFIG.get("directory") or "runtime/retrieval-records")
)

MODEL_ID = os.getenv("PROVIDER_MODEL_ID", "nextcloud-hybrid-rag")
MODEL_NAME = os.getenv("PROVIDER_MODEL_NAME", "SunaQ")
RAG_MIDDLEWARE_URL = os.getenv("RAG_MIDDLEWARE_URL", "http://127.0.0.1:8765").rstrip("/")


def _middleware_client(*, timeout: float) -> httpx.AsyncClient:
    """Create an authenticated client for the internal middleware API."""
    return httpx.AsyncClient(timeout=timeout, headers=provider_api_headers())
# Answer-/control-LLM backend.  New LLM_* variables are canonical; the old
# OLLAMA_* variables remain a backwards-compatible fallback.
_LEGACY_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
_LEGACY_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")

LLM_BACKEND_TYPE = os.getenv("LLM_BACKEND", "ollama").strip().lower()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", _LEGACY_OLLAMA_URL).rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", _LEGACY_OLLAMA_MODEL)

# Role-specific models.  LLM_MODEL remains the general/default model and is
# also used for auxiliary/direct helper tasks unless overridden by
# future dedicated settings.
ANSWER_MODEL = os.getenv("ANSWER_MODEL", LLM_MODEL)
FOLLOWUP_MODEL = os.getenv("FOLLOWUP_MODEL", LLM_MODEL)

LLM_API_KEY = secret_env("LLM_API_KEY", "")
LLM_VERIFY_TLS = os.getenv("LLM_VERIFY_TLS", "true").lower() in {
    "1", "true", "yes", "on"
}
LLM_CA_FILE = os.getenv("LLM_CA_FILE", "").strip() or None

# Deprecated aliases kept inside this file so old diagnostics/config remain
# understandable while the implementation moves to a generic backend.
OLLAMA_URL = LLM_BASE_URL
OLLAMA_MODEL = LLM_MODEL

PROVIDER_API_KEY = secret_env("PROVIDER_API_KEY", "")  # deprecated compatibility variable; not trusted directly
_PROVIDER_CLIENT_STORE: CredentialStore | None = None

def _provider_credential_store_path() -> str:
    config_path = Path(os.getenv("RAG_CONFIG_FILE", "config.yaml"))
    try:
        with config_path.open(encoding="utf-8") as handle:
            provider_cfg = yaml.safe_load(handle) or {}
        return str(((provider_cfg.get("auth") or {}).get("credential_store") or "runtime/users.sqlite"))
    except Exception:
        return "runtime/users.sqlite"

def _provider_client_store() -> CredentialStore:
    global _PROVIDER_CLIENT_STORE
    if _PROVIDER_CLIENT_STORE is None:
        _PROVIDER_CLIENT_STORE = CredentialStore(_provider_credential_store_path())
    return _PROVIDER_CLIENT_STORE
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "20"))
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "40000"))
PER_RESULT_MAX_CHARS = int(os.getenv("PER_RESULT_MAX_CHARS", "6000"))
USE_PER_RESULT_MAX_CHARS = int(os.getenv("USE_PER_RESULT_MAX_CHARS", "24000"))
USE_CONTEXT_MAX_CHARS = int(os.getenv("USE_CONTEXT_MAX_CHARS", str(CONTEXT_MAX_CHARS)))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "300"))
_graph_queue_cfg = dict(PROVIDER_CONFIG.get("graph_queue") or {})
_graph_auto_default = "true" if bool(_graph_queue_cfg.get("auto_enqueue_cited_documents", False)) else "false"
GRAPH_EVIDENCE_HOOK_ENABLED = os.getenv("GRAPH_EVIDENCE_HOOK_ENABLED", _graph_auto_default).lower() in {
    "1", "true", "yes", "on"
}
GRAPH_EVIDENCE_HOOK_TIMEOUT = float(os.getenv("GRAPH_EVIDENCE_HOOK_TIMEOUT", "5"))
GRAPH_EVIDENCE_HOOK_MAX_DOCUMENTS = max(1, int(os.getenv("GRAPH_EVIDENCE_HOOK_MAX_DOCUMENTS", "8")))
_research_findings_cfg = dict(PROVIDER_CONFIG.get("research_findings") or {})
_research_findings_default = "true" if _config_truthy(_research_findings_cfg.get("enabled"), False) else "false"
RESEARCH_FINDINGS_ENABLED = os.getenv("RESEARCH_FINDINGS_ENABLED", _research_findings_default).lower() in {
    "1", "true", "yes", "on"
}
RESEARCH_FINDINGS_TIMEOUT = float(os.getenv("RESEARCH_FINDINGS_TIMEOUT", str(_research_findings_cfg.get("timeout", 5))))
RESEARCH_FINDINGS_MAX_DOCUMENTS = max(1, min(60, int(os.getenv(
    "RESEARCH_FINDINGS_MAX_DOCUMENTS", str(_research_findings_cfg.get("max_documents_per_request", 30))
))))
_chat_archive_cfg = dict(PROVIDER_CONFIG.get("chat_archive") or {})
_chat_archive_default = "true" if _config_truthy(_chat_archive_cfg.get("enabled"), False) else "false"
CHAT_ARCHIVE_ENABLED = os.getenv("CHAT_ARCHIVE_ENABLED", _chat_archive_default).lower() in {
    "1", "true", "yes", "on"
}


def _provider_web_config() -> dict[str, Any]:
    """Return the static Web capability configuration from web.yaml."""
    try:
        path = Path(__file__).resolve().parent.parent / "web.yaml"
        with path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _chat_archive_state(scoped_user_id: str | None) -> tuple[str, bool]:
    """Return the selected path and effective global+per-user archive policy."""
    target_path = "SunaQ-Chats"
    user_enabled = False
    identity = str(scoped_user_id or "").strip()
    if identity:
        store = _provider_client_store()
        user = store.get_canonical_user_for_identity(identity)
        if user is not None and user.enabled:
            user_enabled = True
            settings = store.get_chat_settings(user.canonical_user_id)
            if settings is not None:
                target_path = settings.target_path
                user_enabled = bool(settings.enabled)
    return target_path, bool(CHAT_ARCHIVE_ENABLED and user_enabled)


def _source_capabilities_for_identity(scoped_user_id: str | None) -> dict[str, bool]:
    """Return the optional source capabilities actually enabled for this user.

    Capability-off is a SunaQ boundary, not merely a producer/worker switch:
    archived material may still exist in Nextcloud and remain discoverable there,
    while SunaQ deliberately stops offering and accepting that source scope.
    """
    if is_src(PROVIDER_CONFIG):
        return {
            "documents": True,
            "mailarchive": False,
            "webarchive": False,
            "chatarchive": False,
            "web": False,
        }
    identity = str(scoped_user_id or "").strip()
    store = _provider_client_store()
    user = store.get_canonical_user_for_identity(identity) if identity else None

    mail_enabled = False
    web_user_enabled = False
    web_archive_user_enabled = False
    if user is not None and user.enabled:
        mail_enabled = bool(
            _config_truthy((PROVIDER_CONFIG.get("mail") or {}).get("enabled"), False)
            and any(
                account.enabled and account.has_secret
                for account in store.list_mail_accounts(
                    user.canonical_user_id,
                    enabled_only=True,
                )
            )
        )
        web_settings = store.get_web_settings(user.canonical_user_id)
        web_user_enabled = bool(web_settings is not None and web_settings.enabled)
        web_archive_user_enabled = bool(
            web_settings is not None
            and web_settings.enabled
            and web_settings.archive_enabled
        )

    web_cfg = _provider_web_config()
    web_global_enabled = _config_truthy(web_cfg.get("enabled"), False)
    web_archive_global_enabled = bool(
        web_global_enabled
        and _config_truthy((web_cfg.get("archive") or {}).get("enabled"), False)
    )
    _, chat_enabled = _chat_archive_state(identity)
    return {
        "documents": True,
        "mailarchive": mail_enabled,
        "webarchive": bool(web_archive_global_enabled and web_archive_user_enabled),
        "chatarchive": chat_enabled,
        "web": bool(
            RETRIEVAL_POLICY.web != "disabled"
            and web_global_enabled
            and web_user_enabled
        ),
    }


_SOURCE_CAPABILITY_LABELS = {
    "mailarchive": "Mailarchiv",
    "webarchive": "Webarchiv",
    "chatarchive": "Chatarchiv",
    "web": "Web-Recherche",
}


def _disabled_requested_sources(
    source_scopes: set[str] | None,
    *,
    web_requested: bool,
    scoped_user_id: str | None,
) -> list[str]:
    capabilities = _source_capabilities_for_identity(scoped_user_id)
    requested = set(source_scopes or ())
    if web_requested:
        requested.add("web")
    return sorted(
        source
        for source in requested
        if source in capabilities and not capabilities[source]
    )


def _provider_config_nextcloud_base_url() -> str:
    """Resolve Nextcloud base URL from env override or canonical config.yaml."""
    env_value = os.getenv("NEXTCLOUD_BASE_URL", "").strip().rstrip("/")
    if env_value:
        return env_value

    config_path = Path(os.getenv("RAG_CONFIG_FILE", "config.yaml"))
    try:
        with config_path.open(encoding="utf-8") as handle:
            provider_cfg = yaml.safe_load(handle) or {}
        return str((provider_cfg.get("nextcloud") or {}).get("base_url", "") or "").strip().rstrip("/")
    except Exception as exc:
        log.warning("Could not read Nextcloud base URL from %s: %s", config_path, exc)
        return ""


NEXTCLOUD_BASE_URL = _provider_config_nextcloud_base_url()
ALLOW_GENERAL_KNOWLEDGE = os.getenv("ALLOW_GENERAL_KNOWLEDGE", "false").lower() in {
    "1", "true", "yes", "on"
}

PROMPT_DIR = Path(os.getenv("PROMPT_DIR", "./prompts"))
RAG_ANSWER_PROMPT_FILE = os.getenv("RAG_ANSWER_PROMPT_FILE", "rag_answer.txt")
DIRECT_PROMPT_FILE = os.getenv("DIRECT_PROMPT_FILE", "direct.txt")
DIRECT_ANSWER_PROMPT_FILE = os.getenv("DIRECT_ANSWER_PROMPT_FILE", "direct_answer.txt")
FOLLOWUP_REWRITE_PROMPT_FILE = os.getenv("FOLLOWUP_REWRITE_PROMPT_FILE", "followup_rewrite.txt")
NATURAL_INSTRUCTION_PROMPT_FILE = os.getenv("NATURAL_INSTRUCTION_PROMPT_FILE", "natural_instruction.txt")
WEB_AFTER_QUERY_PROMPT_FILE = os.getenv("WEB_AFTER_QUERY_PROMPT_FILE", "web_after_queries.txt")
WEB_ANSWER_PROMPT_FILE = os.getenv("WEB_ANSWER_PROMPT_FILE", "web_answer.txt")
HYBRID_WEB_ANSWER_PROMPT_FILE = os.getenv("HYBRID_WEB_ANSWER_PROMPT_FILE", "hybrid_web_answer.txt")
WEB_GATE_PROMPT_FILE = os.getenv("WEB_GATE_PROMPT_FILE", "web_gate.txt")
RETRIEVAL_PLANNER_PROMPT_FILE = os.getenv("RETRIEVAL_PLANNER_PROMPT_FILE", "retrieval_planner.txt")
QUERY_REWRITER_PROMPT_FILE = os.getenv("QUERY_REWRITER_PROMPT_FILE", "query_rewriter.txt")
CANDIDATE_VERIFIER_PROMPT_FILE = os.getenv("CANDIDATE_VERIFIER_PROMPT_FILE", "candidate_verifier.txt")

# Cosmetic source rendering for normal answers. Kept out of config.yaml on
# purpose; these are provider/UI presentation knobs rather than retrieval policy.
SOURCE_SNIPPETS = os.getenv("SOURCE_SNIPPETS", "true").lower() in {"1", "true", "yes", "on"}
SOURCE_SNIPPET_CHARS = max(120, int(os.getenv("SOURCE_SNIPPET_CHARS", "340")))

# Deterministic expert-mode full-text search. /elastic deliberately trusts the
# user's Nextcloud query syntax and refuses automatic analysis when the visible
# result set is too large.
ELASTIC_ANALYZE_LIMIT = max(1, int(os.getenv("ELASTIC_ANALYZE_LIMIT", "20")))
ELASTIC_LIST_LIMIT = max(1, int(os.getenv("ELASTIC_LIST_LIMIT", "50")))
ELASTIC_MAX_EVIDENCE_CHARS = max(8000, int(os.getenv("ELASTIC_MAX_EVIDENCE_CHARS", "120000")))
ELASTIC_STREAM_MAX_SECONDS = float(os.getenv("ELASTIC_STREAM_MAX_SECONDS", "360"))

# Optional public-web capability. UI-specific tools are deliberately ignored: a
# client may only grant this capability through our neutral API header. This
# keeps the provider independent from OpenWebUI/AnythingLLM/etc. and prevents
# a UI's own hidden web-orchestration requests from steering the RAG pipeline.
WEB_GATE_MODEL = os.getenv("WEB_GATE_MODEL", LLM_MODEL)
WEB_GATE_MAX_TOKENS = max(128, int(os.getenv("WEB_GATE_MAX_TOKENS", "240")))

QUERY_REWRITE_MODE = os.getenv("QUERY_REWRITE_MODE", "followup").strip().lower()
NATURAL_INSTRUCTION_MODE = os.getenv("NATURAL_INSTRUCTION_MODE", "on").strip().lower()
NATURAL_INSTRUCTION_MODEL = os.getenv("NATURAL_INSTRUCTION_MODEL", FOLLOWUP_MODEL)
NATURAL_INSTRUCTION_MAX_TOKENS = max(256, int(os.getenv("NATURAL_INSTRUCTION_MAX_TOKENS", "600")))
WEB_AFTER_QUERY_MODEL = os.getenv("WEB_AFTER_QUERY_MODEL", NATURAL_INSTRUCTION_MODEL)
WEB_AFTER_QUERY_MAX_TOKENS = max(192, int(os.getenv("WEB_AFTER_QUERY_MAX_TOKENS", "500")))
WEB_AFTER_CONTEXT_MAX_CHARS = max(2000, int(os.getenv("WEB_AFTER_CONTEXT_MAX_CHARS", "12000")))
WEB_AFTER_MAX_QUERIES = min(3, max(1, int(os.getenv("WEB_AFTER_MAX_QUERIES", "3"))))
FOLLOWUP_HISTORY_MESSAGES = int(os.getenv("FOLLOWUP_HISTORY_MESSAGES", "0"))
FOLLOWUP_HISTORY_MAX_CHARS = int(os.getenv("FOLLOWUP_HISTORY_MAX_CHARS", "8000"))
FOLLOWUP_EVIDENCE_DOCUMENTS = min(
    5, max(0, int(os.getenv("FOLLOWUP_EVIDENCE_DOCUMENTS", "3")))
)
LOG_RETRIEVAL_QUERY = os.getenv("LOG_RETRIEVAL_QUERY", "true").lower() in {
    "1", "true", "yes", "on"
}

# User-facing generation parameters. The provider accepts a deliberate subset of
# OpenAI/OpenWebUI parameters and translates them to Ollama runtime options.
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.2"))
MAX_NUM_PREDICT = int(os.getenv("MAX_NUM_PREDICT", "8192"))
MAX_NUM_CTX = int(os.getenv("MAX_NUM_CTX", "32768"))
# Auch wenn OpenWebUI kein max_tokens mitsendet, bekommt jede Antwort ein
# serverseitiges Tokenbudget. 0 deaktiviert den Default und stellt das alte
# Verhalten wieder her.
DEFAULT_NUM_PREDICT = int(os.getenv("DEFAULT_NUM_PREDICT", "2048"))
# Streaming hatte bisher timeout=None und konnte daher unbegrenzt offenbleiben.
# 0 deaktiviert jeweils den Watchdog.
LLM_STREAM_MAX_SECONDS = float(
    os.getenv(
        "LLM_STREAM_MAX_SECONDS",
        os.getenv("OLLAMA_STREAM_MAX_SECONDS", "180"),
    )
)
LLM_STREAM_READ_TIMEOUT = float(
    os.getenv(
        "LLM_STREAM_READ_TIMEOUT",
        os.getenv("OLLAMA_STREAM_READ_TIMEOUT", "90"),
    )
)

# Development/diagnostic switch.  When true, structured backend reasoning
# is forwarded to OpenWebUI as delta.reasoning_content.  It is deliberately
# independent from whether the model itself is allowed to think.
STREAM_REASONING = os.getenv("STREAM_REASONING", "false").lower() in {
    "1", "true", "yes", "on"
}

# Live reasoning transport:
# - tags:              <think>...</think> through delta.content
# - reasoning_content: structured delta.reasoning_content
REASONING_STREAM_FORMAT = os.getenv(
    "REASONING_STREAM_FORMAT",
    "tags",
).strip().lower()

if REASONING_STREAM_FORMAT not in {"tags", "reasoning_content"}:
    raise RuntimeError(
        "REASONING_STREAM_FORMAT must be 'tags' or 'reasoning_content'"
    )

# Qwen3 can consume a client-provided 1000-token output budget entirely in
# reasoning. For normal RAG answers, keep enough room for final answer text.
ANSWER_MIN_NUM_PREDICT_THINKING = int(
    os.getenv("ANSWER_MIN_NUM_PREDICT_THINKING", "2048")
)

# The answer layer receives evidence that has already been reviewed and selected.
# Default: do not let it start a second research/reasoning pass.
# Set true only for deliberate diagnostics/experiments.
ANSWER_THINKING = os.getenv("ANSWER_THINKING", "false").lower() in {
    "1", "true", "yes", "on"
}

# OpenWebUI helper jobs do not need long reasoning.
AUX_MAX_NUM_PREDICT = int(os.getenv("AUX_MAX_NUM_PREDICT", "400"))


# Research provenance. This is deliberately independent of sync state.sqlite.
RESEARCH_LOG_ENABLED = os.getenv("RESEARCH_LOG_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
RESEARCH_LOG_DB = Path(os.getenv("RESEARCH_LOG_DB", "research.sqlite"))
RESEARCH_LOG_STORE_ANSWER = os.getenv("RESEARCH_LOG_STORE_ANSWER", "false").lower() in {
    "1", "true", "yes", "on"
}

# Optional post-ACL/post-verifier evidence-control pass. config.yaml is
# canonical for the mode. EVIDENCE_DECISION_MODE remains a legacy fallback for
# installations whose preserved config.yaml predates the evidence_control block.
_evidence_control_cfg = dict(PROVIDER_CONFIG.get("evidence_control") or {})
_configured_evidence_decision_mode = str(
    _evidence_control_cfg.get("mode") or ""
).strip().lower()
EVIDENCE_DECISION_MODE = (
    _configured_evidence_decision_mode
    or os.getenv("EVIDENCE_DECISION_MODE", "off").strip().lower()
)
EVIDENCE_DECISION_PROMPT_FILE = os.getenv("EVIDENCE_DECISION_PROMPT_FILE", "evidence_decision.txt")
# Evidence already had its own model switch; keep LLM_MODEL as fallback.
EVIDENCE_MODEL = os.getenv("EVIDENCE_MODEL", LLM_MODEL)
EVIDENCE_MAX_TOKENS = int(os.getenv("EVIDENCE_MAX_TOKENS", "700"))
EVIDENCE_CONTEXT_MAX_CHARS = int(os.getenv("EVIDENCE_CONTEXT_MAX_CHARS", "14000"))
EVIDENCE_PER_RESULT_MAX_CHARS = int(os.getenv("EVIDENCE_PER_RESULT_MAX_CHARS", "2600"))

# Remote trust-boundary budgets. They apply only when a role resolves to a
# public/remote endpoint (or is explicitly marked remote with *_LLM_SCOPE).
# Legacy remote limits remain the compatibility fallback for installations that
# do not use packaged SunaQ answer-context budgets.
REMOTE_LLM_MAX_CHARS_PER_DOCUMENT = max(500, int(os.getenv("REMOTE_LLM_MAX_CHARS_PER_DOCUMENT", "3000")))
REMOTE_LLM_MAX_TOTAL_CHARS = max(2000, int(os.getenv("REMOTE_LLM_MAX_TOTAL_CHARS", "20000")))
REMOTE_VERIFIER_MAX_CANDIDATES = max(1, int(os.getenv("REMOTE_VERIFIER_MAX_CANDIDATES", "10")))
REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT = max(500, int(os.getenv("REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT", "2000")))
REMOTE_ANSWER_MAX_DOCUMENTS = max(1, int(os.getenv("REMOTE_ANSWER_MAX_DOCUMENTS", "8")))

# Packaged SunaQ profiles may deliberately spend larger budgets, but never
# beyond these administrator-controlled trust-boundary ceilings.
SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT = max(
    500, int(os.getenv("SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT", "8000"))
)
SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS = max(
    2000, int(os.getenv("SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS", "250000"))
)
SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES = max(
    1, int(os.getenv("SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES", "50"))
)
SUNAQ_REMOTE_HARD_VERIFIER_MAX_CHARS_PER_DOCUMENT = max(
    500, int(os.getenv("SUNAQ_REMOTE_HARD_VERIFIER_MAX_CHARS_PER_DOCUMENT", "6000"))
)
SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS = max(
    1, int(os.getenv("SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS", "50"))
)


def _sunaq_remote_cap(hard_cap: int, legacy_cap: int, legacy_env_name: str) -> int:
    """Apply a preserved explicit 0.8.5 REMOTE_* cap without shrinking fresh profiles.

    Fresh 0.8.6 templates leave legacy REMOTE_* variables unset. Upgraded
    installations may retain them in provider.env; in that case the smaller
    legacy value remains an administrator-controlled data-exposure cap.
    """
    if legacy_env_name in os.environ:
        return min(int(hard_cap), int(legacy_cap))
    return int(hard_cap)


@dataclass(frozen=True)
class ProviderRuntimeModel:
    model: RuntimeModel
    retrieval_planner: Any
    evidence_decision_mode: str
    role_backends: dict[str, Any]


def _build_provider_runtime_model(model: RuntimeModel) -> ProviderRuntimeModel:
    planner = load_retrieval_planner_settings(model.config)
    evidence_cfg = dict(model.config.get("evidence_control") or {})
    evidence_mode = str(
        evidence_cfg.get("mode")
        or EVIDENCE_DECISION_MODE
        or "off"
    ).strip().lower()
    if evidence_mode not in {"off", "review"}:
        raise RuntimeError(
            f"SunaQ model {model.model_id}: evidence_control.mode must be off or review"
        )
    role_backends = build_role_backends(
        default_backend=LLM_BACKEND_TYPE,
        default_base_url=LLM_BASE_URL,
        default_model=LLM_MODEL,
        default_api_key=LLM_API_KEY,
        default_verify_tls=LLM_VERIFY_TLS,
        default_ca_file=LLM_CA_FILE,
        models={
            "default": LLM_MODEL,
            "planner": planner.model or FOLLOWUP_MODEL or LLM_MODEL,
            "verifier": planner.model or LLM_MODEL,
            "evidence": EVIDENCE_MODEL,
            "answer": ANSWER_MODEL,
        },
        role_overrides=model.roles,
        private_network_only=is_src(PROVIDER_CONFIG),
    )
    architecture_errors = [
        *validate_runtime_model(PROVIDER_CONFIG, model),
        *validate_role_backends(PROVIDER_CONFIG, role_backends),
    ]
    if architecture_errors:
        raise RuntimeError(
            f"SunaQ model {model.model_id}: incompatible with architecture tier "
            f"{architecture_tier(PROVIDER_CONFIG)!r}: "
            + "; ".join(architecture_errors)
        )
    return ProviderRuntimeModel(
        model=model,
        retrieval_planner=planner,
        evidence_decision_mode=evidence_mode,
        role_backends=role_backends,
    )


SUNAQ_PROVIDER_MODELS = {
    model.model_id: _build_provider_runtime_model(model)
    for model in SUNAQ_MODEL_REGISTRY.list()
}
_DEFAULT_RUNTIME_MODEL = SUNAQ_PROVIDER_MODELS[SUNAQ_MODEL_REGISTRY.default_model_id]
_ACTIVE_RUNTIME_MODEL: ContextVar[ProviderRuntimeModel | None] = ContextVar(
    "sunaq_provider_runtime_model", default=None
)

_PROGRESS_TTL_SECONDS = max(60.0, float(os.getenv("SUNAQ_PROGRESS_TTL_SECONDS", "900")))
_PROGRESS_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_STATES: dict[str, dict[str, Any]] = {}
_ACTIVE_PROGRESS_ID: ContextVar[str] = ContextVar("sunaq_progress_id", default="")
_ACTIVE_PROGRESS_OWNER: ContextVar[str] = ContextVar("sunaq_progress_owner", default="")
_ACTIVE_SUGGESTIONS: ContextVar[tuple[dict[str, str], ...]] = ContextVar(
    "sunaq_suggestions", default=()
)
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _set_suggestions(items: list[dict[str, str]] | tuple[dict[str, str], ...]) -> None:
    clean: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in list(items or [])[:4]:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or "").strip()[:120]
        action = str(raw.get("action") or "").strip().lower()
        query = str(raw.get("query") or "").strip()[:4000]
        model = str(raw.get("model") or "").strip()[:96]
        if not label or action not in {"query", "rerun", "focus"}:
            continue
        if action in {"query", "rerun"} and not query:
            continue
        key = (label, action, query, model)
        if key in seen:
            continue
        seen.add(key)
        item = {"label": label, "action": action}
        if query:
            item["query"] = query
        if model:
            item["model"] = model
        clean.append(item)
    _ACTIVE_SUGGESTIONS.set(tuple(clean))


def _suggestions() -> list[dict[str, str]]:
    return [dict(item) for item in _ACTIVE_SUGGESTIONS.get()]


def _stronger_model_suggestion(
    question: str,
    user_id: str | None,
    current_model_id: str | None = None,
) -> dict[str, str] | None:
    """Offer a stronger allowed SunaQ model when a verification window was hit."""
    try:
        allowed, _ = _model_access_for_identity(user_id)
    except PermissionError:
        return None
    current = (
        SUNAQ_PROVIDER_MODELS.get(str(current_model_id or "").strip())
        or _active_runtime_model()
    )
    current_search = current.model.section("search")
    current_score = (
        int(current.retrieval_planner.max_retrieval_rounds),
        int(current.retrieval_planner.exhaustive_verification_candidate_limit),
        int(current.retrieval_planner.bounded_verification_candidate_limit),
        int(current.retrieval_planner.verification_candidate_limit),
        int(current_search.get("rerank_candidates", 0) or 0),
    )
    candidates: list[tuple[tuple[int, int, int, int, int], ProviderRuntimeModel]] = []
    for model_id in allowed:
        runtime = SUNAQ_PROVIDER_MODELS.get(model_id)
        if runtime is None or runtime.model.model_id == current.model.model_id:
            continue
        search_cfg = runtime.model.section("search")
        score = (
            int(runtime.retrieval_planner.max_retrieval_rounds),
            int(runtime.retrieval_planner.exhaustive_verification_candidate_limit),
            int(runtime.retrieval_planner.bounded_verification_candidate_limit),
            int(runtime.retrieval_planner.verification_candidate_limit),
            int(search_cfg.get("rerank_candidates", 0) or 0),
        )
        if score > current_score:
            candidates.append((score, runtime))
    if not candidates:
        return None
    _, target = max(candidates, key=lambda pair: pair[0])
    return {
        "label": f"Mit {target.model.name} erneut suchen",
        "action": "rerun",
        "query": str(question or "").strip(),
        "model": target.model.model_id,
    }



def _refinement_suggestions(
    question: str,
    user_id: str | None,
    current_model_id: str | None = None,
) -> list[dict[str, str]]:
    """Deterministic next actions for broad/limited retrieval outcomes."""
    items: list[dict[str, str]] = []
    stronger = _stronger_model_suggestion(
        question,
        user_id,
        current_model_id=current_model_id,
    )
    if stronger is not None:
        items.append(stronger)
    items.append({
        "label": "Anfrage präzisieren",
        "action": "focus",
    })
    return items


def _cleanup_progress_locked(now: float) -> None:
    stale = [
        request_id
        for request_id, state in _PROGRESS_STATES.items()
        if now - float(state.get("updated_at") or 0.0) > _PROGRESS_TTL_SECONDS
    ]
    for request_id in stale:
        _PROGRESS_STATES.pop(request_id, None)


def _set_progress(
    request_id: str,
    owner: str,
    stage: str,
    label: str,
) -> None:
    request_id = str(request_id or "").strip()
    if not request_id or not _PROGRESS_ID_RE.fullmatch(request_id):
        return
    now = time.time()
    with _PROGRESS_LOCK:
        _cleanup_progress_locked(now)
        previous = _PROGRESS_STATES.get(request_id) or {}
        if previous and str(previous.get("owner") or "") != str(owner or ""):
            return
        _PROGRESS_STATES[request_id] = {
            "request_id": request_id,
            "owner": str(owner or ""),
            "stage": str(stage or "").strip(),
            "label": str(label or "").strip(),
            "model": _active_model_id(),
            "created_at": float(previous.get("created_at") or now),
            "updated_at": now,
        }


def _progress(stage: str, label: str) -> None:
    request_id = _ACTIVE_PROGRESS_ID.get()
    if request_id:
        _set_progress(request_id, _ACTIVE_PROGRESS_OWNER.get(), stage, label)


def _read_progress(request_id: str, owner: str) -> dict[str, Any] | None:
    request_id = str(request_id or "").strip()
    if not _PROGRESS_ID_RE.fullmatch(request_id):
        return None
    now = time.time()
    with _PROGRESS_LOCK:
        _cleanup_progress_locked(now)
        state = _PROGRESS_STATES.get(request_id)
        if state is None or str(state.get("owner") or "") != str(owner or ""):
            return None
        return {
            key: value
            for key, value in state.items()
            if key != "owner"
        }


# Compatibility aliases remain available to diagnostics/tests outside a request.
llm_role_backends = _DEFAULT_RUNTIME_MODEL.role_backends
llm_backend = llm_role_backends["default"].backend


def _active_runtime_model() -> ProviderRuntimeModel:
    return _ACTIVE_RUNTIME_MODEL.get() or _DEFAULT_RUNTIME_MODEL


def _model_access_for_identity(
    user_id: str | None,
) -> tuple[list[str], str]:
    """Return canonical allowed model ids and the effective default.

    Standard is the safe default entitlement. Additional SunaQ profiles are
    opt-in per user through RAG Admin. A persisted setting is fail-closed if it
    references no currently configured model.
    """
    default_model = SUNAQ_MODEL_REGISTRY.default_model_id
    if not user_id:
        return [default_model], default_model

    user = _provider_client_store().get_canonical_user_for_identity(str(user_id))
    if user is None:
        return [default_model], default_model

    settings = _provider_client_store().get_model_settings(user.canonical_user_id)
    if settings is None:
        return [default_model], default_model

    allowed: list[str] = []
    for raw_id in settings.allowed_model_ids:
        try:
            canonical = SUNAQ_MODEL_REGISTRY.canonical_id(raw_id)
        except KeyError:
            continue
        if canonical not in allowed:
            allowed.append(canonical)
    if not allowed:
        raise PermissionError(
            "No configured SunaQ model remains allowed for this user; contact an administrator"
        )

    try:
        preferred = SUNAQ_MODEL_REGISTRY.canonical_id(settings.default_model_id)
    except KeyError:
        preferred = ""
    if preferred not in allowed:
        preferred = (
            SUNAQ_MODEL_REGISTRY.default_model_id
            if SUNAQ_MODEL_REGISTRY.default_model_id in allowed
            else allowed[0]
        )
    return allowed, preferred


def _select_runtime_model(
    model_id: str | None,
    *,
    user_id: str | None = None,
) -> ProviderRuntimeModel:
    allowed, preferred = _model_access_for_identity(user_id)
    requested = str(model_id or "").strip()
    model = SUNAQ_MODEL_REGISTRY.get(requested or preferred)
    if model.model_id not in allowed:
        raise PermissionError(
            f"SunaQ model {model.model_id!r} is not allowed for this user"
        )
    return SUNAQ_PROVIDER_MODELS[model.model_id]


def _retrieval_planner():
    active = _ACTIVE_RUNTIME_MODEL.get()
    return active.retrieval_planner if active is not None else RETRIEVAL_PLANNER


def _evidence_decision_mode() -> str:
    active = _ACTIVE_RUNTIME_MODEL.get()
    return active.evidence_decision_mode if active is not None else EVIDENCE_DECISION_MODE


def _active_model_id() -> str:
    return _active_runtime_model().model.model_id


def _profile_section(name: str) -> dict[str, Any]:
    return _active_runtime_model().model.section(name)


def _role_backend(role: str):
    active = _ACTIVE_RUNTIME_MODEL.get()
    selected = active.role_backends if active is not None else llm_role_backends
    return selected.get(role) or selected["default"]


def _role_model(role: str, requested: str | None = None) -> str:
    # During a packaged SunaQ request the precompiled role backend is
    # authoritative. The per-task "requested" model is a legacy/default fallback
    # only and must not override a selected profile's role routing.
    # Outside requests retain the established environment/legacy behaviour.
    active = _ACTIVE_RUNTIME_MODEL.get()
    if active is not None:
        selected = active.role_backends.get(role) or active.role_backends["default"]
        return str(selected.model or requested or LLM_MODEL)
    explicit = (
        str(os.getenv(f"{role.upper()}_LLM_MODEL", "") or "").strip()
        if role != "default"
        else ""
    )
    if explicit:
        return explicit
    return str(requested or _role_backend(role).model or LLM_MODEL)


def _role_remote(role: str) -> bool:
    return bool(_role_backend(role).remote)


def _answer_context_budget() -> dict[str, int] | None:
    """Return the active packaged model's answer-context budget, if configured."""
    cfg = _active_runtime_model().model.section("answer_context")
    if not cfg:
        return None
    return {
        "max_documents": max(1, int(cfg.get("max_documents") or 1)),
        "max_chars_per_document": max(
            500, int(cfg.get("max_chars_per_document") or PER_RESULT_MAX_CHARS)
        ),
        "max_total_chars": max(
            2000, int(cfg.get("max_total_chars") or CONTEXT_MAX_CHARS)
        ),
    }


# Default/legacy diagnostic value. Runtime requests use _retrieval_planner().
MAX_RETRIEVAL_ROUNDS = RETRIEVAL_PLANNER.max_retrieval_rounds
ENTITY_RECALL_BACKOFF_ENABLED = os.getenv(
    "ENTITY_RECALL_BACKOFF_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}

if QUERY_REWRITE_MODE not in {"off", "followup", "always"}:
    raise RuntimeError("QUERY_REWRITE_MODE must be off, followup, or always")
if NATURAL_INSTRUCTION_MODE not in {"off", "on"}:
    raise RuntimeError("NATURAL_INSTRUCTION_MODE must be off or on")
if EVIDENCE_DECISION_MODE not in {"off", "review"}:
    raise RuntimeError("EVIDENCE_DECISION_MODE must be off or review")


def _load_prompt(filename: str) -> str:
    path = PROMPT_DIR / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Cannot read prompt file {path}: {exc}") from exc


RAG_ANSWER_TEMPLATE = _load_prompt(RAG_ANSWER_PROMPT_FILE)
DIRECT_SYSTEM_PROMPT = _load_prompt(DIRECT_PROMPT_FILE)
DIRECT_ANSWER_SYSTEM_PROMPT = _load_prompt(DIRECT_ANSWER_PROMPT_FILE)
FOLLOWUP_REWRITE_SYSTEM_PROMPT = _load_prompt(FOLLOWUP_REWRITE_PROMPT_FILE)
NATURAL_INSTRUCTION_SYSTEM_PROMPT = _load_prompt(NATURAL_INSTRUCTION_PROMPT_FILE)
WEB_AFTER_QUERY_SYSTEM_PROMPT = _load_prompt(WEB_AFTER_QUERY_PROMPT_FILE)
EVIDENCE_DECISION_SYSTEM_PROMPT = _load_prompt(EVIDENCE_DECISION_PROMPT_FILE)
WEB_ANSWER_SYSTEM_PROMPT = _load_prompt(WEB_ANSWER_PROMPT_FILE)
HYBRID_WEB_ANSWER_SYSTEM_PROMPT = _load_prompt(HYBRID_WEB_ANSWER_PROMPT_FILE)
WEB_GATE_SYSTEM_PROMPT = _load_prompt(WEB_GATE_PROMPT_FILE)
RETRIEVAL_PLANNER_SYSTEM_PROMPT = _load_prompt(RETRIEVAL_PLANNER_PROMPT_FILE)
QUERY_REWRITER_SYSTEM_PROMPT = _load_prompt(QUERY_REWRITER_PROMPT_FILE)
CANDIDATE_VERIFIER_SYSTEM_PROMPT = _load_prompt(CANDIDATE_VERIFIER_PROMPT_FILE)

def _prompt(name: str, default: str) -> str:
    """Return the active SunaQ model prompt override or the global default."""
    value = _active_runtime_model().model.prompts.get(str(name or "").strip().lower())
    return value if value is not None else default


FOLLOWUP_REWRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_history": {"type": "boolean"},
        "standalone_query": {"type": "string"},
    },
    "required": ["use_history", "standalone_query"],
    "additionalProperties": False,
}

NATURAL_INSTRUCTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_mode": {
            "type": "string", "enum": ["none", "all_previous", "selected"],
            "description": "selected for any concrete source, including ordinal singular references such as the first/second document; all_previous only for a plural/set of previous sources",
        },
        "use_references": {
            "type": "array", "items": {"type": "string"},
            "description": "For selected: numeric source references like 1/2/3 or explicit filenames; empty for all_previous",
        },
        "internal_search": {"type": "string", "enum": ["auto", "none", "arms", "elastic"]},
        "retrieval_arms": {
            "type": "array",
            "items": {"type": "string", "enum": ["files", "vector", "graph"]},
            "uniqueItems": True,
        },
        "web": {"type": "boolean"},
        "web_timing": {"type": "string", "enum": ["parallel", "after"]},
        "web_query": {"type": "string"},
        "list_mode": {"type": "string", "enum": ["none", "ranked", "raw"]},
        "force": {"type": "boolean"},
        "context_reset": {"type": "boolean"},
    },
    "required": [
        "use_mode", "use_references", "internal_search", "retrieval_arms",
        "web", "web_timing", "web_query",
        "list_mode", "force", "context_reset",
    ],
}

WEB_AFTER_QUERY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": WEB_AFTER_MAX_QUERIES,
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}


QUERY_FRAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entities": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["id", "text", "role"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "predicate": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["source", "predicate", "target"],
                "additionalProperties": False,
            },
        },
        "constraints": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            },
        },
        "concepts": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string"},
        },
    },
    "required": ["intent", "entities", "relations", "constraints", "concepts"],
    "additionalProperties": False,
}


SEARCH_SPEC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stop": {"type": "boolean"},
        "reason": {"type": "string"},
        "elastic_query": {"type": "string"},
        "semantic_query": {"type": "string"},
        "entities": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
        "concepts": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
        "constraints": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            },
        },
        "verification_requirements": {
            "type": "array", "maxItems": 16, "items": {"type": "string"}
        },
    },
    "required": [
        "stop", "reason", "elastic_query", "semantic_query", "entities",
        "concepts", "constraints", "verification_requirements"
    ],
    "additionalProperties": False,
}


RETRIEVAL_PLANNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stop": {"type": "boolean"},
        "reason": {"type": "string"},
        "exhaustive": {"type": "boolean"},
        "query_frame": QUERY_FRAME_SCHEMA,
        "retrieval_arms": {
            "type": "array",
            "items": {"type": "string", "enum": ["files", "vector", "graph"]},
            "maxItems": 3,
            "uniqueItems": True,
        },
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["strict_lexical", "lexical", "semantic"],
                    },
                    "query": {"type": "string"},
                },
                "required": ["kind", "query"],
                "additionalProperties": False,
            },
            "maxItems": _retrieval_planner().max_queries_per_round,
        },
    },
    "required": ["stop", "reason", "exhaustive", "query_frame", "retrieval_arms", "probes"],
    "additionalProperties": False,
}

EVIDENCE_FRAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": QUERY_FRAME_SCHEMA["properties"]["entities"],
        "relations": QUERY_FRAME_SCHEMA["properties"]["relations"],
        "constraints": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "value": {"type": "string"},
                    "status": {"type": "string", "enum": ["match", "conflict", "unclear"]},
                },
                "required": ["kind", "value", "status"],
                "additionalProperties": False,
            },
        },
        "concepts": QUERY_FRAME_SCHEMA["properties"]["concepts"],
        "mentioned_entities": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string"},
        },
    },
    "required": ["entities", "relations", "constraints", "concepts", "mentioned_entities"],
    "additionalProperties": False,
}


EXHAUSTIVE_VERIFY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["match", "uncertain", "reject"],
                    },
                    "reason": {"type": "string"},
                    "relation_binding": {
                        "type": "string",
                        "enum": ["direct", "reference_only", "contradicted", "unclear"],
                    },
                    "evidence_frame": EVIDENCE_FRAME_SCHEMA,
                },
                "required": ["index", "status", "reason", "relation_binding", "evidence_frame"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["documents", "reason"],
    "additionalProperties": False,
}


NORMAL_VERIFY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 1},
                    "status": {
                        "type": "string",
                        "enum": ["match", "uncertain", "reject"],
                    },
                    "relation_binding": {
                        "type": "string",
                        "enum": ["direct", "reference_only", "contradicted", "unclear"],
                    },
                    "relations": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "predicate": {"type": "string"},
                                "target": {"type": "string"},
                            },
                            "required": ["source", "predicate", "target"],
                            "additionalProperties": False,
                        },
                    },
                    "constraints": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "value": {"type": "string"},
                                "status": {"type": "string", "enum": ["match", "conflict", "unclear"]},
                            },
                            "required": ["kind", "value", "status"],
                            "additionalProperties": False,
                        },
                    },
                    "mentioned_entities": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "index", "status", "relation_binding",
                    "relations", "constraints", "mentioned_entities"
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["documents"],
    "additionalProperties": False,
}

CITATION_REPAIR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "indexes": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "reason": {"type": "string"},
    },
    "required": ["indexes", "reason"],
    "additionalProperties": False,
}


# Ollama structured-output schema.  If an older runtime rejects a schema object,
# _evidence_decision falls back once to plain JSON mode and still validates the
# returned object itself.
EVIDENCE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["answer", "retry", "clarify", "insufficient", "conflict"],
        },
        "reason": {"type": "string"},
        "next_query": {"type": "string"},
        "clarification_options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["label", "query"],
            },
        },
        "conflict_sources": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "answer_sources": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "maxItems": 4,
        },
    },
    "required": [
        "action",
        "reason",
        "next_query",
        "clarification_options",
        "conflict_sources",
        "answer_sources",
    ],
}

research_log = ResearchLog(
    RESEARCH_LOG_DB,
    enabled=RESEARCH_LOG_ENABLED,
    store_answer=RESEARCH_LOG_STORE_ANSWER,
)

# Role-aware backend registry is initialized above after all legacy model
# overrides are loaded. ``llm_backend`` remains the default-backend alias.
app = FastAPI(title="SunaQ", version=VERSION)


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]]
    stream: bool = False

    # OpenAI/OpenWebUI-compatible generation parameters that have a useful
    # Ollama equivalent. Unknown request fields remain harmlessly ignored by
    # Pydantic, preserving compatibility with richer clients.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    repeat_penalty: float | None = None
    num_ctx: int | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None

    # Ollama supports a top-level think field. OpenWebUI may expose either a
    # native think value or OpenAI-style reasoning_effort.
    think: bool | str | None = None
    reasoning_effort: str | None = None

    user: str | None = None

    # OpenAI-compatible function/tool declarations. Unknown tools are ignored;
    # recognised web-search tools only grant a capability and are never invoked
    # by OpenWebUI on our behalf.
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None


class ChatArchiveRegisterRequest(BaseModel):
    document_id: str
    path: str


class SearchResult(BaseModel):
    index: int
    title: str
    text: str
    raw: dict[str, Any] = Field(default_factory=dict)


def _clamp_float(value: float | None, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    return max(minimum, min(maximum, float(value)))


def _clamp_int(value: int | None, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return max(minimum, min(maximum, int(value)))


def _normalize_stop(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in items[:8]:
        text = str(item)
        if text:
            result.append(text[:256])
    return result or None


def _normalize_think(think: bool | str | None, reasoning_effort: str | None) -> bool | str | None:
    if think is not None:
        if isinstance(think, bool):
            return think
        value = str(think).strip().lower()
        if value in {"true", "on", "yes", "1"}:
            return True
        if value in {"false", "off", "no", "0"}:
            return False
        if value in {"low", "medium", "high", "max"}:
            return value
        return None

    if reasoning_effort:
        value = reasoning_effort.strip().lower()
        if value in {"low", "medium", "high"}:
            return value
    return None


def _generation_parameters(body: ChatCompletionRequest) -> tuple[dict[str, Any], bool | str | None, dict[str, Any]]:
    options: dict[str, Any] = {
        "temperature": DEFAULT_TEMPERATURE if body.temperature is None else _clamp_float(body.temperature, 0.0, 2.0),
    }

    optional = {
        "top_p": _clamp_float(body.top_p, 0.0, 1.0),
        "top_k": _clamp_int(body.top_k, 1, 1000),
        "min_p": _clamp_float(body.min_p, 0.0, 1.0),
        "seed": body.seed,
        "repeat_penalty": _clamp_float(body.repeat_penalty, 0.0, 4.0),
        "num_ctx": _clamp_int(body.num_ctx, 256, MAX_NUM_CTX),
    }
    for key, value in optional.items():
        if value is not None:
            options[key] = value

    num_predict = body.max_completion_tokens if body.max_completion_tokens is not None else body.max_tokens
    if num_predict is None and DEFAULT_NUM_PREDICT > 0:
        num_predict = DEFAULT_NUM_PREDICT
    num_predict = _clamp_int(num_predict, 1, MAX_NUM_PREDICT)
    if num_predict is not None:
        options["num_predict"] = num_predict

    stop = _normalize_stop(body.stop)
    if stop:
        options["stop"] = stop

    think = _normalize_think(body.think, body.reasoning_effort)

    logged = dict(options)
    if think is not None:
        logged["think"] = think
    return options, think, logged


def _research_call(method: str, *args: Any, **kwargs: Any) -> Any:
    if not RESEARCH_LOG_ENABLED:
        return None
    try:
        return getattr(research_log, method)(*args, **kwargs)
    except Exception as exc:  # Logging must never break document answering.
        log.warning("Research log %s failed: %s", method, exc)
        return None


def _write_retrieval_record(record: dict[str, Any]) -> None:
    """Persist one query/evidence record atomically without document bodies.

    Query frames are search hypotheses; evidence frames are document-derived.
    Keeping them in separate fields preserves provenance for a later curated
    Neo4j import without turning user questions into facts.
    """
    if not RETRIEVAL_RECORD_ENABLED:
        return
    try:
        now = datetime.now().astimezone()
        directory = RETRIEVAL_RECORD_DIRECTORY / now.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        query_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(record.get("query_id") or uuid.uuid4()))[:160]
        target = directory / f"{query_id}.json"
        temporary = directory / f".{query_id}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
        log.info("RetrievalRecord archived: %s", target)
    except Exception as exc:
        log.warning("RetrievalRecord archive failed: %s: %s", type(exc).__name__, exc)


def _conversation_id(request: Request, body: ChatCompletionRequest) -> str | None:
    return (
        request.headers.get("x-openwebui-chat-id")
        or request.headers.get("x-chat-id")
        or request.headers.get("x-rag-conversation-id")
        or body.user
    )


def _raw_documents(results: list[SearchResult]) -> list[dict[str, Any]]:
    """Return only metadata fields persisted by ResearchLog.log_documents()."""
    documents: list[dict[str, Any]] = []
    for result in results:
        raw = result.raw
        documents.append({
            "document_id": raw.get("document_id"),
            "title": raw.get("title") or result.title,
            "path": raw.get("path"),
            "source_url": raw.get("source_url"),
            "rank": raw.get("rank") if raw.get("rank") is not None else result.index,
            "rrf_rank": raw.get("rrf_rank"),
            "elasticsearch_rank": (
                raw.get("elasticsearch_rank")
                if raw.get("elasticsearch_rank") is not None
                else raw.get("es_rank")
            ),
            "vector_rank": raw.get("vector_rank"),
            "chunk_no": raw.get("chunk_no"),
            "rrf_score": raw.get("rrf_score") if raw.get("rrf_score") is not None else raw.get("rrf"),
            "elasticsearch_score": (
                raw.get("elasticsearch_score")
                if raw.get("elasticsearch_score") is not None
                else raw.get("es_score")
            ),
            "vector_score": raw.get("vector_score"),
            "reranker_score": raw.get("reranker_score"),
            "reranker_raw_score": raw.get("reranker_raw_score"),
            "document_date": raw.get("document_date"),
        })
    return documents


def _cited_numbers(answer: str, results: list[SearchResult]) -> set[int]:
    """Return document numbers explicitly referenced by the answer.

    Canonical citations use ``[n]``.  Small/local answer models occasionally
    spell the same reference as ``Dokument 5`` or ``Dokumente 5, 6 und 7``
    despite the prompt.  Those phrases are still explicit document references
    and therefore count as answer evidence.  Bare numbers never count.
    """
    valid = {result.index for result in results}
    cited = {
        int(match.group(1))
        for match in re.finditer(r"\[(\d+)\]", answer)
        if int(match.group(1)) in valid
    }

    document_list_pattern = re.compile(
        r"\bDokument(?:e|en)?\s+"
        r"((?:\[?\d+\]?)(?:\s*(?:,|und|&)\s*\[?\d+\]?)+|\[?\d+\]?)",
        flags=re.IGNORECASE,
    )
    for match in document_list_pattern.finditer(answer):
        for raw_number in re.findall(r"\d+", match.group(1)):
            number = int(raw_number)
            if number in valid:
                cited.add(number)

    return cited


def _cited_results(answer: str, results: list[SearchResult]) -> list[SearchResult]:
    cited = _cited_numbers(answer, results)
    return [result for result in results if result.index in cited]


def _check_auth(authorization: str | None) -> str:
    """Authenticate a trusted frontend and return its server-side client_id.

    The external user-id header is only meaningful inside this client scope.
    A copied/guessed user id from another registered frontend therefore cannot
    address an existing Nextcloud credential binding.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing provider client API key")
    api_key = authorization[7:].strip()
    client = _provider_client_store().authenticate_client(api_key)
    if client is None:
        raise HTTPException(status_code=401, detail="Invalid provider client API key")
    return client.client_id


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                text = item.get("text") or item.get("input_text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def _latest_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content")).strip()
    return ""


def _is_pure_complete_filename_query(question: str, filename: str | None) -> bool:
    """Return True only when the user message is effectively just the filename.

    A complete filename embedded in a larger instruction (e.g. "analyse X.pdf")
    is a deterministic document selection and should use the direct /use-style
    path.  A bare filename keeps the compact filename-navigation response.
    """
    name = str(filename or "").strip()
    if not name:
        return False
    text = normalize_query_quotes(str(question or "")).strip()
    text = text.strip(" \t\r\n`'\"“”„‚‘’.,;:()[]{}")
    return text.casefold() == name.casefold()


def _log_answer_context_stats(label: str, context: str, results: list[SearchResult]) -> None:
    """Log what reached the answer model without logging document contents."""
    docs = [
        {
            "index": result.index,
            "file": _source_filename(result),
            "source_chars": len(result.text or ""),
            "enriched": bool((result.raw or {}).get("context_enriched")),
        }
        for result in results
    ]
    log.info(
        "Answer context %s: context_chars=%d docs=%s",
        label,
        len(context or ""),
        docs,
    )


WEB_GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_web": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["use_web", "reason"],
    "additionalProperties": False,
}


def _request_allows_web(body: ChatCompletionRequest, request: Request) -> bool:
    """Return whether our UI-independent public-web capability is granted.

    Tool declarations are intentionally ignored. Several chat UIs inject their
    own web-search tools and helper prompts; treating those as RAG control input
    couples the middleware to UI internals and can create recursive/irrelevant
    web searches. Clients that want automatic *fallback* web evidence may set
    ``X-RAG-Web-Allowed: true`` through a trusted integration layer. ``/web``
    remains the explicit user-controlled web path.
    """
    del body  # API compatibility; capability is intentionally header-only.
    header = str(request.headers.get("x-rag-web-allowed") or "").strip().casefold()
    return header in {"1", "true", "yes", "on"}


async def _decide_web_use(question: str, retrieval_query: str) -> dict[str, Any]:
    """Conservative request-level gate for the optional public-web arm.

    This decision happens before any public request, so a false result creates
    neither traffic nor WebDAV archive material. Failure is fail-closed.
    """
    user = (
        f"NUTZERFRAGE:\n{question}\n\n"
        f"INTERNE SUCHANFRAGE:\n{retrieval_query}\n\n"
        "Entscheide, ob fuer diese konkrete Frage zusaetzliche aktuelle oder "
        "oeffentliche Web-Evidence wirklich erforderlich ist."
    )
    try:
        raw = await _ollama_complete(
            [
                {"role": "system", "content": _prompt("web_gate", WEB_GATE_SYSTEM_PROMPT)},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=WEB_GATE_MAX_TOKENS,
            think=False,
            model=WEB_GATE_MODEL,
            role="planner",
            response_format=WEB_GATE_SCHEMA,
        )
        value = _extract_json_object(raw)
        return {
            "use_web": bool(value.get("use_web", False)),
            "reason": str(value.get("reason") or "").strip()[:500],
        }
    except Exception as exc:
        log.warning("Web capability gate failed closed: %s", exc)
        return {"use_web": False, "reason": f"gate_error:{type(exc).__name__}"}


_CONTROL_DIRECTIVES = {"documents", "mailarchive", "webarchive", "chatarchive", "web", "files", "vector", "graph", "elastic", "list", "list:raw", "new", "force", "use", "health", "help"}


@dataclass
class ParsedDirectives:
    query: str
    retrieval_arms: set[str] | None = None
    source_scopes: set[str] | None = None
    source_scopes_explicit: bool = False
    web_requested: bool = False
    list_mode: str | None = None  # None | "ranked" | "raw"
    context_reset: bool = False
    force_unspecific: bool = False
    web_only: bool = False
    elastic_mode: bool = False
    use_references: list[str] = field(default_factory=list)
    special_command: str | None = None  # "health" | "help"
    seen: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class NaturalInstructionWorkflow:
    instruction: str
    query: str
    retrieval_query: str
    use_references: list[str] = field(default_factory=list)
    retrieval_arms: set[str] | None = None
    list_mode: str | None = None
    context_reset: bool = False
    force_unspecific: bool = False
    web_requested: bool = False
    web_query: str = ""
    web_timing: str = "parallel"
    web_only: bool = False
    elastic_mode: bool = False


def _split_leading_natural_instruction(text: str) -> tuple[str, str] | None:
    """Return ``(instruction, query)`` only for a leading parenthesized block.

    Slash directives deliberately have their own syntax and are never interpreted
    by the LLM compiler. Parentheses later in a normal prompt remain normal text.
    A colon after the closing parenthesis is optional.
    """
    raw = str(text or "").lstrip()
    if not raw.startswith("("):
        return None
    close = raw.find(")", 1)
    if close < 0:
        return None
    instruction = raw[1:close].strip()
    rest = raw[close + 1:].lstrip()
    if rest.startswith(":"):
        rest = rest[1:].lstrip()
    if not instruction or not rest:
        return None
    return instruction, normalize_query_quotes(rest).strip()


def _natural_instruction_forces_all_previous(instruction: str) -> bool:
    """Return True for an explicit request to reuse the complete previous set.

    The phrase ``Nutze alle Dokumente`` is semantically ambiguous to a generic
    instruction LLM: it can be misread as "search all documents".  Inside the
    leading control block, however, ``nutze``/``verwende`` denotes direct reuse
    of the documents handed off by the immediately preceding assistant turn.
    Keep this small set of high-confidence aliases deterministic so a follow-up
    can never accidentally widen back out to the whole repository.
    """
    text = normalize_query_quotes(str(instruction or "")).casefold().strip()
    text = re.sub(r"[.!?;:]+$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return bool(re.match(
        r"^(?:nutze|verwende)\s+(?:bitte\s+)?(?:alle|sämtliche)\s+"
        r"(?:(?:diese|bisherigen|vorherigen|obigen)\s+)?"
        r"(?:dokumente|quellen|dateien)(?:$|\s+und\b|\s*,)",
        text,
    )) or bool(re.match(
        r"^use\s+(?:please\s+)?all\s+(?:these\s+|previous\s+)?"
        r"(?:documents|sources|files)(?:$|\s+and\b|\s*,)",
        text,
    ))


def _normalize_natural_workflow(
    instruction: str,
    query: str,
    value: dict[str, Any],
) -> NaturalInstructionWorkflow:
    forced_all_previous = _natural_instruction_forces_all_previous(instruction)
    use_mode = str(value.get("use_mode") or "none").strip().lower()
    if forced_all_previous:
        use_mode = "all_previous"
    refs = [str(x).strip() for x in (value.get("use_references") or []) if str(x).strip()]
    if use_mode == "all_previous":
        refs = ["all"]
    elif use_mode != "selected":
        refs = []

    internal_search = str(value.get("internal_search") or "auto").strip().lower()
    if forced_all_previous:
        # Reuse of the previous document set is closed-world.  Do not allow a
        # model misclassification (typically internal_search=arms/files) to
        # widen the follow-up into a fresh repository search.
        internal_search = "none"
        log.info("Natural instruction deterministic all-previous override: %r", instruction)
    arms = {str(x).strip().lower() for x in (value.get("retrieval_arms") or [])}
    arms &= {"files", "vector", "graph"}
    retrieval_arms: set[str] | None = arms if internal_search == "arms" and arms else None
    elastic_mode = internal_search == "elastic"

    list_raw = str(value.get("list_mode") or "none").strip().lower()
    list_mode = {"ranked": "ranked", "raw": "raw"}.get(list_raw)
    web_requested = bool(value.get("web", False))
    web_timing = str(value.get("web_timing") or "parallel").strip().lower()
    if web_timing not in {"parallel", "after"}:
        web_timing = "parallel"

    # Directly selected documents do not trigger another internal retrieval unless
    # the instruction explicitly asks for it. The first release deliberately keeps
    # that mixed internal workflow out of scope; /use+web is supported.
    if refs and internal_search in {"auto", "none"}:
        internal_search = "none"
        retrieval_arms = None
        elastic_mode = False
    elif refs and internal_search in {"arms", "elastic"}:
        raise ValueError("Natural instruction cannot combine direct document selection with a new internal search yet")

    web_only = web_requested and not refs and internal_search == "none"
    # The compiler controls workflow only. The user task stays authoritative.
    retrieval_query = normalize_query_quotes(query).strip() or query
    web_query = normalize_query_quotes(str(value.get("web_query") or "")).strip()
    if web_requested and not web_query:
        web_query = retrieval_query

    return NaturalInstructionWorkflow(
        instruction=instruction,
        query=query,
        retrieval_query=retrieval_query,
        use_references=refs,
        retrieval_arms=retrieval_arms,
        list_mode=list_mode,
        context_reset=bool(value.get("context_reset", False)),
        force_unspecific=bool(value.get("force", False)),
        web_requested=web_requested,
        web_query=web_query,
        web_timing=web_timing,
        web_only=web_only,
        elastic_mode=elastic_mode,
    )


async def _compile_natural_instruction(
    request_messages: list[dict[str, Any]],
    instruction: str,
    query: str,
) -> NaturalInstructionWorkflow | None:
    """Compile a leading natural-language control block to a closed workflow.

    The model may interpret language flexibly, but execution accepts only the
    fields/enums in NATURAL_INSTRUCTION_RESPONSE_SCHEMA. On any parse/validation
    failure the caller falls back to a normal internal query without control actions.
    """
    if NATURAL_INSTRUCTION_MODE == "off":
        return None
    history = _history_as_text(request_messages)
    user_prompt = (
        f"ANWEISUNG:\n{instruction}\n\n"
        f"AUFGABE:\n{query}\n\n"
        f"CHATVERLAUF (nur falls fuer Verweise noetig):\n{history or '(leer)'}"
    )
    try:
        raw = await _ollama_complete(
            [
                {"role": "system", "content": _prompt("natural_instruction", NATURAL_INSTRUCTION_SYSTEM_PROMPT)},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=NATURAL_INSTRUCTION_MAX_TOKENS,
            think=False,
            model=NATURAL_INSTRUCTION_MODEL,
            role="planner",
            response_format=NATURAL_INSTRUCTION_RESPONSE_SCHEMA,
        )
        value = _extract_json_object(raw)
        workflow = _normalize_natural_workflow(instruction, query, value)
        log.info(
            "Natural instruction compiled: use=%s arms=%s elastic=%s web=%s timing=%s list=%s force=%s reset=%s web_query=%r retrieval_query=%r",
            workflow.use_references or [],
            sorted(workflow.retrieval_arms) if workflow.retrieval_arms else [],
            workflow.elastic_mode, workflow.web_requested, workflow.web_timing,
            workflow.list_mode, workflow.force_unspecific, workflow.context_reset,
            workflow.web_query, workflow.retrieval_query,
        )
        return workflow
    except Exception as exc:
        log.warning(
            "Natural instruction compiler failed closed; using normal query without control actions: %s: %s",
            type(exc).__name__, exc,
        )
        return None


def _normalize_after_web_queries(
    value: dict[str, Any],
    fallback_query: str,
) -> list[str]:
    queries: list[str] = []
    raw_queries = value.get("queries") or []
    if isinstance(raw_queries, list):
        for item in raw_queries:
            query = normalize_query_quotes(str(item or "")).strip()
            query = re.sub(r"\s+", " ", query)
            if not query:
                continue
            if query.casefold() in {existing.casefold() for existing in queries}:
                continue
            queries.append(query)
            if len(queries) >= WEB_AFTER_MAX_QUERIES:
                break
    fallback = normalize_query_quotes(str(fallback_query or "")).strip()
    if not queries and fallback:
        queries.append(fallback)
    return queries


async def _derive_after_web_queries(
    *,
    instruction: str,
    question: str,
    initial_web_query: str,
    internal_context: str,
) -> list[str]:
    """Derive 1..3 web queries *after* internal evidence is available.

    This is intentionally a closed helper task, not an agent loop.  The model
    sees the user's explicit target plus a bounded slice of the already selected
    evidence and may only return a short list of search-engine queries.
    """
    evidence = str(internal_context or "").strip()[:WEB_AFTER_CONTEXT_MAX_CHARS]
    fallback = normalize_query_quotes(str(initial_web_query or question)).strip() or question
    if not evidence:
        return [fallback] if fallback else []

    bounded_evidence = serialize_evidence_records(
        [{"citation": "[INTERNAL]", "text": evidence}],
        kind="web_after_internal_context",
    )
    prompt = (
        f"STEUERANWEISUNG:\n{instruction or '(keine)'}\n\n"
        f"BENUTZERAUFTRAG:\n{question}\n\n"
        f"VORLAEUFIGE WEB-SUCHANFRAGE (nur Hinweis, nicht verbindlich):\n{fallback or '(leer)'}\n\n"
        f"INTERNE_EVIDENCE_JSON:\n{bounded_evidence}"
    )
    try:
        raw = await _ollama_complete(
            [
                {
                    "role": "system",
                    "content": guarded_evidence_prompt(
                        _prompt("web_after_query", WEB_AFTER_QUERY_SYSTEM_PROMPT)
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=WEB_AFTER_QUERY_MAX_TOKENS,
            think=False,
            model=WEB_AFTER_QUERY_MODEL,
            role="planner",
            response_format=WEB_AFTER_QUERY_RESPONSE_SCHEMA,
        )
        value = _extract_json_object(raw)
        queries = _normalize_after_web_queries(value, fallback)
        log.info("Derived after-web queries: %s", queries)
        return queries
    except Exception as exc:
        # Web-after is an enrichment step.  If query derivation fails, preserve
        # the explicit/initial query instead of failing the whole user request.
        log.warning(
            "After-web query derivation failed; using initial web query: %s: %s",
            type(exc).__name__, exc,
        )
        return [fallback] if fallback else []


def _consume_use_directive(text: str) -> tuple[list[str], str, str | None]:
    """Consume ``/use:...`` and return references, remaining text and error.

    Supported examples::

        /use:1,2 Fasse die Dokumente zusammen.
        /use:"Wagner.pdf","Vertrag 2024.pdf" Vergleiche beide.
        /use:1,"Ordner/Datei mit Leerzeichen.pdf" Prüfe die Aussage.

    Quoted values may contain spaces and commas.  Backslash escapes the next
    character inside a quoted value.  Unquoted values end at comma/whitespace.
    """
    prefix = "/use:"
    if not text.casefold().startswith(prefix):
        return [], text, "Interner Parserfehler: /use-Prefix fehlt."

    pos = len(prefix)
    length = len(text)
    items: list[str] = []

    while True:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break

        if text[pos] == '"':
            pos += 1
            buf: list[str] = []
            closed = False
            while pos < length:
                char = text[pos]
                if char == "\\" and pos + 1 < length:
                    buf.append(text[pos + 1])
                    pos += 2
                    continue
                if char == '"':
                    pos += 1
                    closed = True
                    break
                buf.append(char)
                pos += 1
            if not closed:
                return [], "", "Nicht geschlossenes Anführungszeichen in /use."
            value = "".join(buf).strip()
        else:
            start = pos
            while pos < length and text[pos] not in {",", " ", "\t", "\r", "\n"}:
                pos += 1
            value = text[start:pos].strip()

        if not value:
            return [], "", "Leerer Eintrag in /use."
        items.append(value)

        # Whitespace may occur around commas.  Without a comma, whitespace ends
        # the directive and begins the natural-language prompt.
        space_start = pos
        while pos < length and text[pos].isspace():
            pos += 1
        if pos < length and text[pos] == ",":
            pos += 1
            continue

        if pos > space_start:
            return items, text[pos:].lstrip(), None

        # A non-comma character directly after an item is invalid rather than
        # silently becoming part of the prompt.
        if pos < length:
            return [], "", "Zwischen /use-Einträgen ist ein Komma erforderlich."
        break

    if not items:
        return [], "", "/use benötigt mindestens eine Quellen-Nummer oder Datei."
    return items, "", None


def _parse_retrieval_directives(text: str) -> ParsedDirectives:
    """Parse the contiguous leading slash-command prefix.

    Retrieval selectors remain orthogonal to conversation control.  ``/list``
    now means post-reranker browse output, while ``/list:raw`` preserves the
    historic retrieval-only view.  ``/force`` only bypasses the broad/unspecific
    early-stop heuristic.  ``/use`` is an explicit document-selection mode and
    may only be combined with ``/new``.
    """
    rest = str(text or "").lstrip()
    seen: list[str] = []
    selectors: set[str] = set()
    source_scopes: set[str] = set()
    source_scopes_explicit = False
    web_requested = False
    list_mode: str | None = None
    context_reset = False
    force_unspecific = False
    web_only = False
    elastic_mode = False
    use_references: list[str] = []
    special_command: str | None = None
    error: str | None = None

    while rest.startswith("/"):
        lowered = rest.casefold()

        if lowered.startswith("/use:"):
            if use_references:
                error = "/use darf pro Anfrage nur einmal angegeben werden."
                break
            refs, remainder, use_error = _consume_use_directive(rest)
            if use_error:
                error = use_error
                break
            use_references = refs
            seen.append("use")
            rest = remainder
            continue

        match = re.match(
            r"^/(list:raw|documents|mailarchive|webarchive|chatarchive|files|vector|graph|elastic|web|list|new|force|health|help|hilfe)\b",
            rest,
            flags=re.IGNORECASE,
        )
        if not match:
            break

        command = match.group(1).lower()
        if command == "hilfe":
            command = "help"
        seen.append(command)
        rest = rest[match.end():].lstrip()

        if command in {"documents", "mailarchive", "webarchive", "chatarchive"}:
            source_scopes.add(command)
            source_scopes_explicit = True
        elif command in {"files", "vector", "graph"}:
            selectors.add(command)
        elif command == "elastic":
            elastic_mode = True
        elif command == "web":
            web_requested = True
        elif command == "list":
            list_mode = "ranked"
        elif command == "list:raw":
            list_mode = "raw"
        elif command == "new":
            context_reset = True
        elif command == "force":
            force_unspecific = True
        elif command in {"health", "help"}:
            if special_command and special_command != command:
                error = "/health und /help können nicht kombiniert werden."
                break
            special_command = command

    active_arms = selectors if selectors else None
    active_source_scopes = source_scopes if source_scopes else None
    # /web is a source scope. Alone it is web-only; with an internal source scope
    # or an explicit internal retrieval arm it becomes a mixed internal+web task.
    web_only = bool(web_requested and not active_source_scopes and not active_arms and not elastic_mode and not use_references)

    if elastic_mode and (active_arms or force_unspecific or use_references or special_command):
        error = (
            "/elastic ist eine direkte Volltextsuche und kann nur mit /new sowie "
            "optional /list bzw. /list:raw kombiniert werden."
        )

    if use_references and (active_arms or active_source_scopes or list_mode or force_unspecific or special_command or web_requested or elastic_mode):
        error = (
            "/use ist eine direkte Dokumentauswahl und kann nur mit /new kombiniert werden; "
            "Quellen- und Retrieval-Directives sind dabei nicht zulässig."
        )

    if special_command and (active_arms or active_source_scopes or list_mode or force_unspecific or use_references or context_reset or web_requested or elastic_mode):
        error = f"/{special_command} ist ein eigenständiger Befehl und kann nicht mit anderen Directives kombiniert werden."
    if special_command and rest.strip():
        error = f"/{special_command} erwartet keine zusätzliche Frage."

    return ParsedDirectives(
        query=(rest.strip() if use_references else normalize_query_quotes(rest).strip()),
        retrieval_arms=active_arms,
        source_scopes=active_source_scopes,
        source_scopes_explicit=source_scopes_explicit or web_requested,
        web_requested=web_requested,
        list_mode=list_mode,
        context_reset=context_reset,
        force_unspecific=force_unspecific,
        web_only=web_only,
        elastic_mode=elastic_mode,
        use_references=use_references,
        special_command=special_command,
        seen=seen,
        error=error,
    )

def _sanitize_followup_history(role: str, text: str) -> str:
    """Keep semantic chat content, but remove provider presentation artifacts."""
    cleaned = str(text or "").strip()

    if role == "user":
        # Retrieval/control directives are operational metadata, not semantic
        # content for a later rewrite. Natural-instruction parentheses are likewise
        # control metadata when (and only when) they lead the user message.
        natural = _split_leading_natural_instruction(cleaned)
        if natural is not None:
            cleaned = natural[1]
        else:
            cleaned = _parse_retrieval_directives(cleaned).query

    if role == "assistant":
        cleaned = re.split(
            r"\n\s*\n\*\*Quellen:\*\*",
            cleaned,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        cleaned = re.sub(r"\[(\d+)\]", "", cleaned)

    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _context_boundary(messages: list[dict[str, Any]]) -> tuple[int, str]:
    """Return start index and label for the active conversation segment.

    The latest leading /new is a hard boundary.  If the CURRENT user message
    carries /new, no previous message belongs to the active segment.
    """
    if not messages:
        return 0, "chat_start"

    latest_user_index = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            latest_user_index = i
            break

    if latest_user_index is not None:
        latest_text = _content_to_text(messages[latest_user_index].get("content"))
        parsed = _parse_retrieval_directives(latest_text)
        if parsed.context_reset:
            return latest_user_index, "current:/new"

    end = latest_user_index if latest_user_index is not None else len(messages)
    for i in range(end - 1, -1, -1):
        message = messages[i]
        if message.get("role") != "user":
            continue
        text = _content_to_text(message.get("content"))
        parsed = _parse_retrieval_directives(text)
        if parsed.context_reset:
            return i, "previous:/new"

    return 0, "chat_start"


def _prior_conversation(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the user/assistant context since the latest /new boundary."""
    usable: list[dict[str, str]] = []
    start, boundary = _context_boundary(messages)

    # Exclude the latest user message; it is supplied separately as the current
    # question.  If that message itself is /new, the slice is intentionally empty.
    previous = messages[start:-1]
    if FOLLOWUP_HISTORY_MESSAGES > 0:
        previous = previous[-FOLLOWUP_HISTORY_MESSAGES:]

    for message in previous:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _content_to_text(message.get("content")).strip()
        text = _sanitize_followup_history(str(role), text)
        if text:
            usable.append({"role": str(role), "content": text})
    return usable


def _history_as_text(messages: list[dict[str, Any]]) -> str:
    # Preserve the most recent part of the active segment if the character
    # budget is reached; recent turns are more useful for reference resolution.
    blocks: list[str] = []
    for item in _prior_conversation(messages):
        role = "BENUTZER" if item["role"] == "user" else "ASSISTENT"
        blocks.append(f"{role}: {item['content']}")

    if FOLLOWUP_HISTORY_MAX_CHARS <= 0:
        return "\n\n".join(blocks)

    selected: list[str] = []
    used = 0
    for block in reversed(blocks):
        cost = len(block) + (2 if selected else 0)
        if selected and used + cost > FOLLOWUP_HISTORY_MAX_CHARS:
            break
        if not selected and len(block) > FOLLOWUP_HISTORY_MAX_CHARS:
            block = block[-FOLLOWUP_HISTORY_MAX_CHARS:]
            cost = len(block)
        selected.append(block)
        used += cost
    selected.reverse()
    return "\n\n".join(selected)


# Follow-up/reference resolution is deliberately delegated to the configured
# planner LLM. Conversation language is therefore not constrained by a
# language-specific trigger regex; /new remains the deterministic context
# boundary and invalid/failed rewrites fall back to the current query.


def _auxiliary_task_kind(request: Request, question: str) -> str | None:
    """Detect OpenWebUI helper requests that must bypass document retrieval.

    OpenWebUI does not reliably attach a task header to all helper calls.
    Current versions may instead send a single user message beginning with
    ``### Task:`` (follow-up suggestions, title generation, tag generation).
    Sending those prompts through RAG pollutes retrieval and, because the
    embedded chat history can be very long, may also create an Elasticsearch
    query with excessive boolean clauses.
    """
    task = (
        request.headers.get("x-rag-task")
        or request.headers.get("x-openwebui-task")
        or request.headers.get("x-task")
        or ""
    ).strip().lower()

    if task and task not in {"chat", "conversation", "default", "none"}:
        return f"header:{task}"

    text = question.lstrip()
    folded = text.casefold()

    # Known OpenWebUI background-task prompt signatures.  Keep these
    # deliberately specific so an ordinary user message is not classified
    # as auxiliary merely because it contains the word 'task'.
    if folded.startswith("### task:"):
        if "suggest 3-5 relevant follow-up questions" in folded:
            return "ui:follow_ups"
        if "generate a concise" in folded and "title" in folded and "chat history" in folded:
            return "ui:title"
        if "generate 1-3 broad tags" in folded and "chat history" in folded:
            return "ui:tags"
        # OpenWebUI and other frontends may ask a model to synthesize web-search
        # queries in a hidden helper call. It must never enter document retrieval.
        if (
            "search quer" in folded
            and ("chat history" in folded or "conversation" in folded)
            and ("json" in folded or '"queries"' in folded or "queries" in folded)
        ):
            return "ui:web_query_generation"
        if "analyze the chat history" in folded and "generating search queries" in folded:
            return "ui:web_query_generation"

    return None


def _is_auxiliary_task(request: Request, question: str) -> bool:
    return _auxiliary_task_kind(request, question) is not None


async def _ollama_complete(
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    *,
    options: dict[str, Any] | None = None,
    think: bool | str | None = None,
    model: str | None = None,
    response_format: str | dict[str, Any] | None = None,
    role: str = "default",
) -> str:
    """Compatibility wrapper around the configured generic LLM backend.

    The old function name is deliberately retained so the rest of the mature
    provider/evidence code does not need a risky rewrite.
    """
    runtime_options = dict(options or {})
    if "temperature" not in runtime_options:
        runtime_options["temperature"] = (
            DEFAULT_TEMPERATURE if temperature is None else temperature
        )
    if max_tokens and "num_predict" not in runtime_options:
        runtime_options["num_predict"] = max_tokens

    selected = _role_backend(role)
    selected_model = _role_model(role, model)
    result = await selected.backend.complete(
        messages,
        options=runtime_options,
        think=think,
        model=selected_model,
        response_format=response_format,
        timeout=HTTP_TIMEOUT,
    )

    content = str(result.get("content") or "").strip()
    if not content:
        thinking = str(result.get("thinking") or "")
        log.warning(
            "LLM backend returned empty content: backend=%s model=%s "
            "think=%r thinking_chars=%d done_reason=%s eval_count=%s",
            selected.backend_name,
            selected_model,
            think,
            len(thinking),
            result.get("done_reason"),
            result.get("eval_count"),
        )
    return content


async def _rewrite_query_with_context(
    request_messages: list[dict[str, Any]],
    question: str,
) -> tuple[str, bool]:
    """Return a standalone retrieval query and whether prior chat was required.

    In followup mode the model is also the reference classifier. If it says
    the current query is standalone, the original user text is kept byte-for-byte
    rather than accepting an unsolicited paraphrase. This avoids topic bleed
    while keeping reference resolution language-neutral.
    """
    history = _history_as_text(request_messages)
    if not history or QUERY_REWRITE_MODE not in {"always", "followup"}:
        return question, False

    user_prompt = (
        f"MODE: {QUERY_REWRITE_MODE}\n\n"
        "CHAT_HISTORY:\n"
        f"{history}\n\n"
        "CURRENT_QUERY:\n"
        f"{question}"
    )
    try:
        raw = await _ollama_complete(
            [
                {"role": "system", "content": _prompt("followup", FOLLOWUP_REWRITE_SYSTEM_PROMPT)},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=320,
            think=False,
            model=FOLLOWUP_MODEL,
            role="planner",
            response_format=FOLLOWUP_REWRITE_RESPONSE_SCHEMA,
        )
        value = _extract_json_object(raw)
    except Exception as exc:
        log.warning(
            "Follow-up reference resolution failed; using original question: %s: %s",
            type(exc).__name__,
            exc,
        )
        return question, False

    use_history = bool(value.get("use_history"))
    rewritten = normalize_query_quotes(
        str(value.get("standalone_query") or "")
    ).strip()
    rewritten = re.sub(r"<!--.*?-->", "", rewritten, flags=re.DOTALL)
    rewritten = re.sub(r"\[(?:W)?\d+\]", "", rewritten)
    rewritten = re.sub(r"\s+", " ", rewritten).strip()

    if QUERY_REWRITE_MODE == "followup" and not use_history:
        return question, False
    if not rewritten:
        return question, False

    guarded_rewritten = preserve_explicit_quoted_phrases(question, rewritten)
    if guarded_rewritten != rewritten:
        log.info(
            "Follow-up query guard: restored explicit quoted phrase syntax: before=%r after=%r",
            rewritten[:240],
            guarded_rewritten[:240],
        )
        rewritten = guarded_rewritten

    log.info(
        "Follow-up reference resolution: use_history=%s original=%r standalone=%r",
        use_history,
        question[:180],
        rewritten[:240],
    )
    return rewritten, use_history


async def _rewrite_query_if_needed(
    request_messages: list[dict[str, Any]],
    question: str,
    *,
    allow_short_acronym_context: bool = False,
) -> str:
    """Compatibility wrapper returning only the standalone query."""
    del allow_short_acronym_context
    rewritten, _ = await _rewrite_query_with_context(request_messages, question)
    return rewritten


async def _rag_search(
    question: str,
    user_id: str | None,
    user_groups: str | None,
    request_id: str | None = None,
    *,
    entity_recall: bool = False,
    retrieval_arms: set[str] | None = None,
    source_scopes: set[str] | None = None,
    raw_results: bool = False,
    force_unspecific: bool = False,
    search_spec: dict[str, Any] | None = None,
    query_context: dict[str, Any] | None = None,
    limit: int | None = None,
) -> tuple[dict[str, Any], list[SearchResult]]:
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if user_groups:
        headers["X-RAG-User-Groups"] = user_groups
    if request_id:
        headers["X-RAG-Request-ID"] = request_id

    _progress("retrieving", "Dokumente werden gesucht und Berechtigungen geprüft …")
    async with _middleware_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/search",
            json={
                "query": question,
                "model": _active_model_id(),
                "limit": int(limit if limit is not None else SEARCH_LIMIT),
                "entity_recall": bool(entity_recall),
                "retrieval_arms": sorted(retrieval_arms) if retrieval_arms else None,
                "source_scopes": sorted(source_scopes) if source_scopes else None,
                "search_spec": search_spec,
                "query_context": query_context,
                "raw_results": bool(raw_results),
                "force_unspecific": bool(force_unspecific),
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    parsed: list[SearchResult] = []
    for i, item in enumerate(payload.get("results", []), start=1):
        title = str(item.get("title") or item.get("document") or f"Dokument {i}")
        text = str(
            item.get("context_text")
            or item.get("text")
            or item.get("chunk")
            or ""
        ).strip()
        if text or raw_results:
            parsed.append(SearchResult(index=i, title=title, text=text, raw=item))
    return payload, parsed


async def _rag_query_context(
    question: str,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Fetch compact Neo4j seed/alias context before query rewriting.

    Failure is intentionally fail-open: seeds improve rewriting, but a graph
    outage must not disable ordinary Elasticsearch/Qdrant retrieval.
    """
    headers: dict[str, str] = {}
    if request_id:
        headers["X-RAG-Request-ID"] = request_id
    try:
        async with _middleware_client(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                f"{RAG_MIDDLEWARE_URL}/query-context",
                json={"query": question, "model": _active_model_id()},
                headers=headers,
            )
            response.raise_for_status()
            value = response.json()
            return value if isinstance(value, dict) else {}
    except Exception as exc:
        log.warning("Neo4j query-seed context unavailable; rewriting without seeds: %s", exc)
        return {}


def _compact_query_seed_context(value: dict[str, Any] | None) -> dict[str, Any]:
    raw = value or {}
    entities: list[dict[str, Any]] = []
    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        compact = {
            "mention": str(item.get("mention") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "display_name": str(item.get("display_name") or "").strip(),
            "matched_form": str(item.get("matched_form") or "").strip(),
            "entity_type": str(item.get("entity_type") or "").strip(),
        }
        candidates = []
        for candidate in (item.get("candidates") or [])[:3]:
            if not isinstance(candidate, dict):
                continue
            candidates.append({
                "display_name": str(candidate.get("display_name") or "").strip(),
                "form_value": str(candidate.get("form_value") or "").strip(),
                "similarity": candidate.get("similarity"),
            })
        if candidates:
            compact["candidates"] = candidates
        if compact["mention"]:
            entities.append(compact)

    aliases = []
    for item in raw.get("elastic_phrase_expansion") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("value") or "").strip()
        if not text:
            continue
        aliases.append({
            "value": text,
            "kind": str(item.get("kind") or item.get("source") or "").strip(),
            "original_mention": str(item.get("original_mention") or "").strip(),
        })
        if len(aliases) >= 12:
            break
    return {"entities": entities[:8], "aliases": aliases}


_NAMED_MONTH_RE = re.compile(
    r"(?<!\w)(Januar|Jan\.?|Februar|Feb\.?|März|Maerz|Mrz\.?|April|Apr\.?|Mai|"
    r"Juni|Jun\.?|Juli|Jul\.?|August|Aug\.?|September|Sept?\.?|Oktober|Okt\.?|"
    r"November|Nov\.?|Dezember|Dez\.?)(?!\w)",
    re.IGNORECASE,
)
_NAMED_MONTH_NUMBER = {
    "januar": 1, "jan": 1,
    "februar": 2, "feb": 2,
    "märz": 3, "maerz": 3, "mrz": 3,
    "april": 4, "apr": 4,
    "mai": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
}


def _guard_generated_numeric_month_must(elastic_query: str, *, original_query: str) -> str:
    """Do not turn a named user month into a fabricated numeric hard anchor.

    A model may correctly understand ``Februar 2023`` as month=02 but then
    over-constrain files retrieval with ``+02``.  The document may spell the
    date naturally and contain no standalone ``02`` token.  Keep the named
    month as a soft lexical hint and leave the normalized month value to the
    SearchSpec constraint/verifier instead.

    This guard is intentionally narrow: it only acts when the *user* supplied
    a named month and the LLM emitted the corresponding standalone numeric
    value as a required token.  Years and user-supplied numeric-only dates are
    untouched.
    """
    query = normalize_query_quotes(str(elastic_query or "")).strip()
    original = str(original_query or "")
    if not query or not original:
        return query

    month_mentions: dict[int, str] = {}
    for match in _NAMED_MONTH_RE.finditer(original):
        raw = match.group(0).strip()
        key = raw.rstrip(".").casefold()
        number = _NAMED_MONTH_NUMBER.get(key)
        if number is not None and number not in month_mentions:
            month_mentions[number] = raw
    if not month_mentions:
        return query

    raw_tokens = re.findall(r'[+-]?"(?:\\.|[^\\"])*"|\S+', query)
    kept: list[str] = []
    removed_months: set[int] = set()
    for raw_token in raw_tokens:
        token = raw_token.strip()
        if not token.startswith("+"):
            kept.append(raw_token)
            continue
        value = token[1:].strip().strip('"').strip()
        if not re.fullmatch(r"0?[1-9]|1[0-2]", value):
            kept.append(raw_token)
            continue
        number = int(value)
        if number not in month_mentions:
            kept.append(raw_token)
            continue
        removed_months.add(number)

    if not removed_months:
        return query

    existing_texts = {
        str(item.get("text") or "").rstrip(".").casefold()
        for item in nextcloud_query_tokens(" ".join(kept))
    }
    for number in sorted(removed_months):
        month_text = month_mentions[number]
        if month_text.rstrip(".").casefold() not in existing_texts:
            kept.append(month_text)

    return re.sub(r"\s+", " ", " ".join(kept)).strip()


async def _rewrite_search_spec(
    *,
    question: str,
    round_no: int,
    results: list[SearchResult],
    previous_spec: dict[str, Any] | None,
    retrieval_arms: set[str],
    seed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rewrite one user request into one small SearchSpec.

    The LLM writes the human-style Nextcloud full-text expression for files and
    the natural semantic query for vectors.  Entity/concept/constraint fields
    are analytical side-products used for verification and Graph-Lite.
    """
    files_requested = "files" in retrieval_arms
    vector_requested = "vector" in retrieval_arms
    prior = _planner_result_context(results) if results else "(erste Retrieval-Runde)"
    previous_text = json.dumps(previous_spec or {}, ensure_ascii=False)
    prompt = (
        f"ORIGINALFRAGE:\n{question}\n\n"
        f"RETRIEVAL-RUNDE: {round_no}\n"
        f"AKTIVE ARME: files={str(files_requested).lower()} vector={str(vector_requested).lower()}\n\n"
        f"NEO4J-SEEDS (nur bekannte Namens-/Alias-Hinweise):\n"
        f"{json.dumps(_compact_query_seed_context(seed_context), ensure_ascii=False)}\n\n"
        f"VORHERIGER SEARCHSPEC:\n{previous_text}\n\n"
        f"BISHERIGES SICHTBARES TREFFERBILD:\n{prior}\n\n"
        "Erzeuge den SearchSpec für diese Runde. In Runde 1 ist stop=false. "
        "Wenn files aktiv ist, muss elastic_query eine sinnvolle positive Volltextsuche enthalten. "
        "Explizite unterscheidende Namen/Kennungen nicht verlieren."
    )
    model = _retrieval_planner().model or ANSWER_MODEL

    async def run_once(extra: str = "") -> dict[str, Any]:
        raw = await _ollama_complete(
            [
                {"role": "system", "content": guarded_evidence_prompt(_prompt("query_rewriter", QUERY_REWRITER_SYSTEM_PROMPT))},
                {"role": "user", "content": prompt + extra},
            ],
            temperature=0.0,
            max_tokens=_retrieval_planner().max_tokens,
            think=False,
            model=model,
            role="planner",
            response_format=SEARCH_SPEC_RESPONSE_SCHEMA,
        )
        if not raw:
            raise ValueError("Query rewriter returned empty content")
        value = _extract_json_object(raw)
        spec = normalize_search_spec(value, original_query=question)
        if files_requested:
            phrase_guarded_query = preserve_explicit_quoted_phrases(
                question,
                spec.get("elastic_query") or "",
            )
            if phrase_guarded_query != spec.get("elastic_query"):
                log.info(
                    "query rewrite guard: restored explicit quoted phrase syntax: before=%r after=%r",
                    spec.get("elastic_query") or "",
                    phrase_guarded_query,
                )
                spec["elastic_query"] = phrase_guarded_query

            guarded_elastic_query = _guard_generated_numeric_month_must(
                spec.get("elastic_query") or "",
                original_query=question,
            )
            if guarded_elastic_query != spec.get("elastic_query"):
                log.info(
                    "query rewrite guard: removed generated numeric month must: before=%r after=%r",
                    spec.get("elastic_query") or "",
                    guarded_elastic_query,
                )
                spec["elastic_query"] = guarded_elastic_query
            tokens = nextcloud_query_tokens(spec.get("elastic_query") or "")
            if not tokens or not any(token.get("occur") in {"must", "should"} for token in tokens):
                raise ValueError("Query rewriter omitted a positive elastic_query")
        if vector_requested and not str(spec.get("semantic_query") or "").strip():
            raise ValueError("Query rewriter omitted semantic_query")

        return {
            "valid": True,
            "stop": bool(value.get("stop", False)) if round_no > 1 else False,
            "reason": str(value.get("reason") or "").strip()[:1000],
            "spec": spec,
            "model": model,
        }

    try:
        return await run_once()
    except Exception as first_exc:
        log.warning(
            "Query rewrite invalid; retrying once: %s: %s",
            type(first_exc).__name__, first_exc,
        )
        try:
            return await run_once(
                "\n\nFEHLERHINWEIS: Die vorige Ausgabe war unbrauchbar. "
                "Behalte alle expliziten Suchanker und liefere ausschließlich gültiges JSON."
            )
        except Exception as second_exc:
            log.warning(
                "Query rewrite failed after retry: %s: %s",
                type(second_exc).__name__, second_exc,
            )
            return {
                "valid": False,
                "stop": True,
                "reason": f"rewrite_error:{type(second_exc).__name__}",
                "spec": normalize_search_spec({}, original_query=question),
                "model": model,
            }


def _planner_result_context(results: list[SearchResult]) -> str:
    """Bounded, ACL-safe JSON hit picture for a later planner round."""
    if not results:
        return serialize_evidence_records([], kind="planner_visible_results")

    records: list[dict[str, Any]] = []
    for result in results[:20]:
        raw = result.raw or {}
        snippet = str(
            result.text
            or raw.get("context_text")
            or raw.get("es_snippet")
            or raw.get("vector_snippet")
            or raw.get("graph_snippet")
            or ""
        ).strip()
        records.append(
            {
                "citation": f"[{result.index}]",
                "document_id": str(raw.get("document_id") or "")[:512],
                "title": str(result.title or "")[:512],
                "graph_entities": [
                    str(value)[:300]
                    for value in (raw.get("graph_entities") or [])[:8]
                ],
                "graph_direct_relations": list(
                    (raw.get("graph_direct_relations") or [])[:4]
                ),
                "graph_indirect_chains": list(
                    (raw.get("graph_indirect_chains") or [])[:2]
                ),
                "text": snippet,
            }
        )

    rendered, _ = fit_evidence_records(
        records,
        kind="planner_visible_results",
        max_total_chars=_retrieval_planner().context_max_chars,
        per_record_max_chars=1800,
        min_partial_chars=200,
    )
    return rendered

def _planner_executor_probe(
    probe: dict[str, str],
    *,
    retrieval_arms: set[str] | None,
    original_query: str,
) -> dict[str, Any] | None:
    """Bind a generated probe to the backend that understands its semantics.

    Lexical/strict probes belong to files/Elasticsearch. Semantic probes belong
    to vector retrieval. A semantic probe is never silently executed against
    files when Qdrant/vector is unavailable; callers simply omit it.
    """
    selected = set(retrieval_arms or {"files", "vector", "graph"})
    query = str(probe.get("query") or "").strip()
    kind = str(probe.get("kind") or "semantic").strip().casefold()
    if not query:
        return None

    if kind in {"lexical", "strict_lexical"}:
        if "files" not in selected:
            return None
        probe_arms = {"files"}
    elif kind == "semantic":
        if "vector" not in selected:
            return None
        query = semanticize_query(query) or original_query
        probe_arms = {"vector"}
    else:
        return None

    semantic_query = query
    if kind == "strict_lexical":
        # Boolean control characters belong to ES. Keep an equivalent plain
        # semantic representation only as metadata; this probe still executes
        # exclusively on files.
        semantic_query = semanticize_query(query) or original_query
    return {
        "kind": kind,
        "query": query,
        "semantic_query": semantic_query,
        "retrieval_arms": sorted(probe_arms),
    }

def _query_frame_file_anchors(
    question: str,
    query_frame: dict[str, Any],
) -> list[str]:
    """Compile conservative sparse anchors from a planner QueryFrame.

    The planner describes intent; it does not own Elasticsearch syntax. File
    retrieval therefore consumes only structured concepts/entities plus safe
    literal constraints that are independently recoverable from the user's
    wording. This avoids sending a natural-language sentence to Elasticsearch
    merely because the files arm is the only available retrieval backend.
    """
    values: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        folded = text.casefold()
        if folded in seen:
            return
        seen.add(folded)
        values.append(text)

    # Concepts come first deliberately. For bounded document-set requests the
    # first concept is commonly the user-requested document type (e.g. Rechnung).
    for concept in query_frame.get("concepts") or []:
        add(concept)
    for entity in query_frame.get("entities") or []:
        if isinstance(entity, dict):
            add(entity.get("text"))

    # A bare literal year/date is not a useful files query by itself. If the
    # structured frame contains no semantic anchor at all, let the caller use
    # its explicit planner-failure fallback instead of producing e.g. ``2025``.
    if not values:
        return []

    # Model-generated free-form constraints are not sufficient for a hard ES
    # term. Only values deterministically observed in the user's question are
    # eligible here.
    constraints = extract_safe_hard_constraints(question)
    for kind in ("filenames", "identifiers", "dates", "years"):
        for value in constraints.get(kind) or []:
            add(value)
    return values


def _files_probe_from_query_frame(
    question: str,
    query_frame: dict[str, Any],
    *,
    retrieval_arms: set[str] | None,
    bounded_document_set: bool,
) -> dict[str, Any] | None:
    """Compile one Elasticsearch-native view from the structured QueryFrame.

    Bounded document sets use a conjunctive Nextcloud-compatible query. Other
    file searches use a compact lexical term view. Natural-language prose is
    only a last-resort fallback when the planner produced no usable structure.
    """
    selected = set(retrieval_arms or {"files", "vector", "graph"})
    if "files" not in selected:
        return None

    anchors = _query_frame_file_anchors(question, query_frame)
    if bounded_document_set and len(anchors) >= 2:
        tokens: list[str] = []
        for text in anchors:
            escaped = text.replace('"', '\\"')
            tokens.append(f'+"{escaped}"' if any(ch.isspace() for ch in text) else f'+{escaped}')
        query = " ".join(tokens)
        return _planner_executor_probe(
            {"kind": "strict_lexical", "query": query},
            retrieval_arms={"files"},
            original_query=question,
        )

    if anchors:
        query = " ".join(anchors)
        return _planner_executor_probe(
            {"kind": "lexical", "query": query},
            retrieval_arms={"files"},
            original_query=question,
        )
    return None


def _bounded_strict_recall_probe(
    question: str,
    query_frame: dict[str, Any],
    *,
    retrieval_arms: set[str] | None,
) -> dict[str, Any] | None:
    """Compatibility wrapper returning only a true strict bounded probe."""
    probe = _files_probe_from_query_frame(
        question,
        query_frame,
        retrieval_arms=retrieval_arms,
        bounded_document_set=True,
    )
    if not probe or probe.get("kind") != "strict_lexical":
        return None
    return probe


def _initial_retrieval_views(
    query: str,
    *,
    retrieval_arms: set[str] | None,
    query_frame: dict[str, Any] | None = None,
    bounded_document_set: bool = False,
) -> list[dict[str, Any]]:
    """Build deterministic arm-native round-1 views.

    Files receive an Elasticsearch-oriented query compiled from the structured
    QueryFrame. Vector retrieval receives the original natural-language query.
    Graph keeps the original wording for entity/relation resolution. If the
    QueryFrame is unexpectedly empty, files retain the old original-query path
    as a fail-open fallback rather than silently dropping retrieval.
    """
    selected = set(retrieval_arms or {"files", "vector", "graph"})
    frame_supplied = query_frame is not None
    frame = normalize_query_frame(query_frame or {})
    views: list[dict[str, Any]] = []
    if "files" in selected:
        files_probe = _files_probe_from_query_frame(
            query,
            frame,
            retrieval_arms={"files"},
            bounded_document_set=bounded_document_set,
        )
        if files_probe is not None:
            views.append(files_probe)
        elif not frame_supplied:
            # Only a failed/unavailable planner may fall back to the legacy
            # original-query files path. A valid (even empty) QueryFrame must
            # never cause natural-language prose to be sent to Elasticsearch.
            views.append({
                "kind": "lexical",
                "query": query,
                "semantic_query": query,
                "retrieval_arms": ["files"],
            })
    if "vector" in selected:
        views.append({
            "kind": "semantic",
            "query": query,
            "semantic_query": query,
            "retrieval_arms": ["vector"],
        })
    if "graph" in selected:
        views.append({
            "kind": "graph",
            "query": query,
            "semantic_query": query,
            "retrieval_arms": ["graph"],
        })
    return views

async def _retrieval_planner_decision(
    *,
    question: str,
    round_no: int,
    results: list[SearchResult],
    seen_probe_queries: list[str],
    retrieval_arms: set[str] | None,
    initial: bool = False,
    initial_probe_count: int = 1,
) -> dict[str, Any]:
    """Ask the thinking model only for additional conservative probes."""
    deterministic_exhaustive = detect_exhaustive_intent(question)
    if not _retrieval_planner().enabled:
        return {
            "stop": True,
            "reason": "retrieval_planner_disabled",
            "exhaustive": deterministic_exhaustive,
            "query_frame": normalize_query_frame({}),
            "retrieval_arms": None,
            "planner_valid": False,
            "probes": [],
        }

    selected = sorted(retrieval_arms or {"files", "vector", "graph"})
    prior = _planner_result_context(results)
    prompt = (
        f"ORIGINALFRAGE:\n{question}\n\n"
        f"RETRIEVAL-RUNDE: {round_no} von {_retrieval_planner().max_retrieval_rounds}\n"
        f"ERLAUBTE ARME (verbindlich): {', '.join(selected)}\n"
        f"SPRACHNEUTRALE SICHERE CONSTRAINTS: {safe_constraint_summary(question)}\n"
        f"BEREITS VERWENDETE PROBES:\n" + ("\n".join(seen_probe_queries) or "(nur Originalfrage)") + "\n\n"
        f"BISHERIGES SICHTBARES TREFFERBILD:\n{prior}\n\n"
        "Erzeuge nur zusaetzliche Probes. Die Originalfrage wird vom System immer beibehalten."
    )
    planner_messages = [
        {"role": "system", "content": guarded_evidence_prompt(_prompt("planner", RETRIEVAL_PLANNER_SYSTEM_PROMPT))},
        {"role": "user", "content": prompt},
    ]
    planner_model = _retrieval_planner().model or ANSWER_MODEL
    # Initial query expansion may use configured thinking.  Follow-up rounds are
    # a bounded stop/continue decision over already retrieved evidence and do
    # not benefit from a long hidden reasoning pass.
    planner_think = _retrieval_planner().thinking if initial else False

    async def run_planner(*, think: bool | str | None) -> dict[str, Any]:
        raw = await _ollama_complete(
            planner_messages,
            temperature=0.0,
            max_tokens=_retrieval_planner().max_tokens,
            think=think,
            model=planner_model,
            role="planner",
            response_format=RETRIEVAL_PLANNER_RESPONSE_SCHEMA,
        )
        if not raw:
            raise ValueError("Retrieval planner returned empty content")
        value = _extract_json_object(raw)
        if not isinstance(value, dict):
            raise ValueError("Retrieval planner did not return a JSON object")
        return value

    planner_mode = "schema_think" if planner_think else "schema_no_think"
    try:
        try:
            value = await run_planner(think=planner_think)
        except (ValueError, json.JSONDecodeError) as first_exc:
            if not planner_think:
                raise
            # Qwen/Ollama can spend the entire num_predict budget in the
            # thinking channel and return empty content. Retry the small
            # structured task once without thinking instead of increasing the
            # budget indefinitely.
            log.warning(
                "Retrieval planner produced no usable structured content with "
                "thinking; retrying once without think: %s: %s",
                type(first_exc).__name__, first_exc,
            )
            planner_mode = "schema_no_think_retry"
            value = await run_planner(think=False)
        except httpx.HTTPStatusError as first_exc:
            if first_exc.response.status_code not in {400, 415, 422} or not planner_think:
                raise
            # Capability-safe fallback for an explicitly configured model that
            # does not accept thinking together with structured output.
            log.warning(
                "Retrieval planner thinking request rejected by LLM backend "
                "(HTTP %d); retrying schema without think",
                first_exc.response.status_code,
            )
            planner_mode = "schema_no_think_retry"
            value = await run_planner(think=False)
    except Exception as exc:
        log.warning(
            "Retrieval planner failed; keeping deterministic original-query retrieval: %s: %s",
            type(exc).__name__, exc,
        )
        return {
            "stop": False if initial else True,
            "reason": f"planner_error:{type(exc).__name__}",
            "exhaustive": deterministic_exhaustive,
            "query_frame": normalize_query_frame({}),
            "retrieval_arms": None,
            "planner_valid": False,
            "probes": [],
        }

    log.info("RC8 retrieval planner backend mode: %s model=%s", planner_mode, planner_model)

    # max_queries_per_round is the budget for *additional* planner rewrites.
    # The deterministic per-arm views of the original wording are technical
    # retrieval views and do not consume this budget.
    max_additional = _retrieval_planner().max_queries_per_round
    probes = normalize_generated_probes(
        question,
        value.get("probes") or [],
        max_additional=max_additional,
        retrieval_arms=retrieval_arms,
        already_seen=seen_probe_queries,
    )
    return {
        "stop": bool(value.get("stop", False)),
        "reason": str(value.get("reason") or "").strip()[:1000],
        "exhaustive": bool(value.get("exhaustive", False)) or deterministic_exhaustive,
        "query_frame": normalize_query_frame(value.get("query_frame")),
        "retrieval_arms": (
            [
                str(arm).strip().casefold()
                for arm in value.get("retrieval_arms")
                if str(arm).strip().casefold() in {"files", "vector", "graph"}
            ]
            if isinstance(value.get("retrieval_arms"), list)
            else None
        ),
        "planner_valid": isinstance(value.get("retrieval_arms"), list),
        "probes": probes,
    }



def _verification_context(
    results: list[SearchResult],
    *,
    start_index: int = 1,
) -> str:
    per_doc = _retrieval_planner().verification_max_chars_per_document
    total_limit = 10**9
    if _role_remote("verifier"):
        if _answer_context_budget() is not None:
            per_doc = min(
                per_doc,
                _sunaq_remote_cap(
                    SUNAQ_REMOTE_HARD_VERIFIER_MAX_CHARS_PER_DOCUMENT,
                    REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT,
                    "REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT",
                ),
            )
            total_limit = _sunaq_remote_cap(
                SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS,
                REMOTE_LLM_MAX_TOTAL_CHARS,
                "REMOTE_LLM_MAX_TOTAL_CHARS",
            )
        else:
            per_doc = min(per_doc, REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT)
            total_limit = REMOTE_LLM_MAX_TOTAL_CHARS

    records: list[dict[str, Any]] = []
    for offset, result in enumerate(results):
        raw = result.raw or {}
        index = start_index + offset
        records.append(
            {
                "citation": f"[DOKUMENT {index}]",
                "index": index,
                "document_id": str(raw.get("document_id") or "")[:512],
                "title": str(result.title or "")[:512],
                "path": str(raw.get("path") or raw.get("file_path") or "")[:1024],
                "source_origin": str(raw.get("source_origin") or "")[:128],
                # Preserve OCR/document line structure explicitly inside the
                # JSON evidence record.
                "text_lines": str(result.text or "").strip().split("\n"),
            }
        )
    rendered, _ = fit_evidence_line_records(
        records,
        kind="verification_candidates",
        max_total_chars=total_limit,
        per_record_max_chars=per_doc,
        min_partial_chars=400,
    )
    return rendered

def _observed_document_years(result: SearchResult) -> list[str]:
    """Return four-digit years visibly present in the reviewed title/body."""
    evidence = f"{result.title}\n{result.text or ''}"
    return sorted(set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", evidence)))


def _empty_evidence_frame() -> dict[str, Any]:
    return {"entities": [], "relations": [], "constraints": [], "concepts": [], "mentioned_entities": []}


def _normalize_evidence_frame(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_evidence_frame()
    raw_entities = value.get("entities") or []
    raw_relations = value.get("relations") or []
    base = normalize_query_frame({
        "intent": "",
        "entities": raw_entities,
        # Full EvidenceFrames use local entity IDs. Compact normal-mode frames
        # intentionally omit the duplicate entity table and carry relation
        # endpoints as document-grounded free text instead.
        "relations": raw_relations if raw_entities else [],
        "constraints": [],
        "concepts": value.get("concepts") or [],
    })
    relations = list(base["relations"])
    if not raw_entities:
        relations = []
        for item in raw_relations[:12]:
            if not isinstance(item, dict):
                continue
            source = re.sub(r"\s+", " ", str(item.get("source") or "")).strip()[:300]
            predicate = re.sub(r"\s+", " ", str(item.get("predicate") or "")).strip()[:300]
            target = re.sub(r"\s+", " ", str(item.get("target") or "")).strip()[:300]
            if source and predicate and target:
                relations.append({"source": source, "predicate": predicate, "target": target})
    constraints: list[dict[str, str]] = []
    for item in (value.get("constraints") or [])[:12]:
        if not isinstance(item, dict):
            continue
        kind = re.sub(r"\s+", " ", str(item.get("kind") or "")).strip()[:120]
        val = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()[:300]
        status = str(item.get("status") or "unclear").strip().lower()
        if status not in {"match", "conflict", "unclear"}:
            status = "unclear"
        if kind and val:
            constraints.append({"kind": kind, "value": val, "status": status})
    mentioned: list[str] = []
    seen: set[str] = set()
    for item in (value.get("mentioned_entities") or [])[:16]:
        text = re.sub(r"\s+", " ", str(item or "")).strip()[:300]
        folded = text.casefold()
        if text and folded not in seen:
            seen.add(folded); mentioned.append(text)
    return {
        "entities": base["entities"],
        "relations": relations,
        "constraints": constraints,
        "concepts": base["concepts"],
        "mentioned_entities": mentioned,
    }


def _audit_verifier_relation_binding(
    status: str,
    reason: str,
    relation_binding: str,
) -> tuple[str, str]:
    """Fail closed unless a claimed match is directly supported by the document.

    The binding vocabulary deliberately has no ``not_applicable`` escape hatch.
    Even for non-relational topical searches, ``direct`` means that the document
    itself directly satisfies the requested subject, while ``reference_only``
    means it merely mentions or cites it.
    """
    if status != "match":
        return status, reason

    binding = (relation_binding or "unclear").strip().lower()
    if binding == "reference_only":
        audit = "gesuchte Beziehung bzw. Gegenstand nur referenziert, nicht selbst durch das Dokument belegt"
        combined = (reason + "; " + audit).strip("; ") if reason else audit
        return "reject", combined[:500]
    if binding == "contradicted":
        audit = "Dokument belegt eine widersprechende Rollen-/Beziehungszuordnung"
        combined = (reason + "; " + audit).strip("; ") if reason else audit
        return "reject", combined[:500]
    if binding == "unclear":
        audit = "gesuchte Beziehung bzw. Gegenstandsbindung im Dokument nicht sicher zuordenbar"
        combined = (reason + "; " + audit).strip("; ") if reason else audit
        return "uncertain", combined[:500]
    if binding != "direct":
        audit = "unbekannte Relationseinstufung; fail closed"
        combined = (reason + "; " + audit).strip("; ") if reason else audit
        return "uncertain", combined[:500]
    return status, reason


def _audit_verifier_safe_constraints(
    question: str,
    result: SearchResult,
    status: str,
    reason: str = "",
) -> tuple[str, str]:
    """Fail closed on explicit language-neutral constraints after LLM review.

    Retrieval stays recall-oriented.  This audit only prevents a candidate from
    becoming a confirmed exhaustive match when an explicit four-digit year
    requested by the user is not evidenced anywhere in the reviewed document
    text/title.  Missing evidence becomes ``uncertain`` rather than a hard
    reject; the LLM may still reject documents with clearly conflicting dates.
    """
    if status != "match":
        return status, reason

    constraints = extract_safe_hard_constraints(question)
    requested_years = constraints.get("years") or []
    if not requested_years:
        return status, reason

    evidence = f"{result.title}\n{result.text or ''}"
    missing = [
        year for year in requested_years
        if not re.search(rf"(?<!\d){re.escape(year)}(?!\d)", evidence)
    ]
    if not missing:
        return status, reason

    audit = "explizites Jahreskriterium nicht im geprüften Dokumenttext belegt: " + ", ".join(missing)
    combined = (reason + "; " + audit).strip("; ") if reason else audit
    return "uncertain", combined[:500]


def _effective_verification_candidate_limit(
    candidate_limit: int | None,
    *,
    exhaustive: bool = False,
) -> int:
    """Return the request-local verifier budget.

    Remote verifier backends keep the small ordinary-request safety cap.  An
    explicit completeness request (``alle``, ``sämtliche`` ...) may instead
    consume the administrator-configured exhaustive verification budget.  The
    latter remains hard bounded by ``RetrievalPlannerSettings`` (currently
    max. 60), so exhaustive intent cannot turn into an unbounded corpus scan.
    """
    limit = int(candidate_limit or _retrieval_planner().verification_candidate_limit)
    if _role_remote("verifier") and not exhaustive:
        remote_cap = (
            _sunaq_remote_cap(
                SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES,
                REMOTE_VERIFIER_MAX_CANDIDATES,
                "REMOTE_VERIFIER_MAX_CANDIDATES",
            )
            if _answer_context_budget() is not None
            else REMOTE_VERIFIER_MAX_CANDIDATES
        )
        limit = min(limit, remote_cap)
    return max(1, limit)


async def _verify_exhaustive_candidates(
    question: str,
    results: list[SearchResult],
    *,
    query_frame: dict[str, Any] | None = None,
    verification_requirements: list[str] | None = None,
    candidate_limit: int | None = None,
    compact: bool = False,
    exhaustive: bool = False,
) -> tuple[list[SearchResult], list[SearchResult], dict[str, Any]]:
    """Verify an ACL-filtered recall pool against the user's document-set intent.

    Verification is deliberately batched. Structured-output capabilities differ
    across Ollama versions/models, so the verifier negotiates output mode once
    per verification run: JSON schema -> plain JSON -> prompt-only JSON. The
    chosen compatible mode is then reused for all remaining batches.
    """
    if not results:
        return [], [], {"checked": 0, "matches": 0, "uncertain": 0, "elapsed_ms": 0}

    _progress("verifying", "Dokumente werden inhaltlich geprüft …")
    verify_started = time.perf_counter()
    limit = _effective_verification_candidate_limit(
        candidate_limit, exhaustive=exhaustive
    )
    bounded = list(results[:limit])
    batch_size = max(1, int(_retrieval_planner().verification_batch_size))
    by_index: dict[int, str] = {}
    reasons: dict[int, str] = {}
    relation_bindings: dict[int, str] = {}
    evidence_frames: dict[int, dict[str, Any]] = {}
    batch_errors: list[str] = []
    # Request-local capability negotiation. Do not repeat a known-incompatible
    # structured-output request for every batch.
    verifier_format_mode = "schema"

    async def run_verifier_once(
        messages: list[dict[str, str]],
        mode: str,
    ) -> dict[str, Any]:
        response_format: str | dict[str, Any] | None
        call_messages = messages
        if mode == "schema":
            response_format = NORMAL_VERIFY_RESPONSE_SCHEMA if compact else EXHAUSTIVE_VERIFY_RESPONSE_SCHEMA
        elif mode == "json":
            response_format = "json"
        else:
            response_format = None
            call_messages = [
                messages[0],
                {
                    "role": "user",
                    "content": (
                        messages[1]["content"]
                        + "\n\nKOMPATIBILITAETS-HINWEIS: Gib ausschließlich ein einzelnes "
                          "gültiges JSON-Objekt entsprechend dem verlangten Schema aus; "
                          "kein Markdown und keinen Begleittext."
                    ),
                },
            ]

        raw = await _ollama_complete(
            call_messages,
            temperature=0.0,
            max_tokens=_retrieval_planner().verification_max_tokens,
            # Candidate verification is a bounded classification task. Do not
            # inherit planner thinking: some Ollama/model combinations reject
            # `think` for structured-output calls.
            think=False,
            model=_retrieval_planner().model or LLM_MODEL,
            role="verifier",
            response_format=response_format,
        )
        return _extract_json_object(raw)

    batch_retries: list[str] = []

    async def process_batch(batch: list[SearchResult], first_index: int) -> None:
        nonlocal verifier_format_mode
        context = _verification_context(batch, start_index=first_index)
        compact_instruction = ""
        if compact:
            compact_instruction = (
                "\n\nKOMPAKTER NORMALMODUS:\n"
                "Gib pro Dokument nur index, status, relation_binding, relations, constraints "
                "und mentioned_entities zurück. Kein reason-Feld, keine separaten entities- oder "
                "concepts-Listen. relations enthält nur unmittelbar im Dokument belegte Beziehungen; "
                "mentioned_entities nur bloße Erwähnungen/Referenzen."
            )
        prompt = (
            f"BENUTZERANFRAGE:\n{question}\n\n"
            f"QUERY-FRAME (Suchhypothese; keine behauptete Tatsache):\n"
            f"{json.dumps(normalize_query_frame(query_frame), ensure_ascii=False)}\n\n"
            f"SICHERE EXPLIZITE CONSTRAINTS (bei match verbindlich):\n"
            f"{safe_constraint_summary(question)}\n\n"
            f"VERIFIKATIONSANFORDERUNGEN AUS DEM QUERY-REWRITE:\n"
            f"{json.dumps(list(verification_requirements or []), ensure_ascii=False)}"
            f"{compact_instruction}\n\n"
            f"KANDIDATEN (bereits Live-ACL-geprueft):\n{context}"
        )
        messages = [
            {"role": "system", "content": guarded_evidence_prompt(_prompt("verifier", CANDIDATE_VERIFIER_SYSTEM_PROMPT))},
            {"role": "user", "content": prompt},
        ]

        try:
            while True:
                try:
                    value = await run_verifier_once(messages, verifier_format_mode)
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status not in {400, 415, 422}:
                        raise
                    if verifier_format_mode == "schema":
                        verifier_format_mode = "json"
                        log.warning(
                            "Candidate verifier JSON schema rejected by LLM backend "
                            "(HTTP %s); downgrading this verification run to plain JSON",
                            status,
                        )
                        continue
                    if verifier_format_mode == "json":
                        verifier_format_mode = "prompt"
                        log.warning(
                            "Candidate verifier plain JSON mode rejected by LLM backend "
                            "(HTTP %s); downgrading this verification run to prompt-only JSON",
                            status,
                        )
                        continue
                    raise
        except Exception as exc:
            message = f"batch {first_index}-{first_index + len(batch) - 1}: {type(exc).__name__}: {exc}"
            # Normal mode should usually fit in one compact call. If the model
            # nevertheless truncates/mangles the JSON, retry only the failed
            # batch as two smaller calls instead of turning every candidate
            # into `uncertain`. This is a rare-path latency cost, not the
            # steady-state behavior.
            if len(batch) > 1:
                split = max(1, len(batch) // 2)
                batch_retries.append(message)
                log.warning(
                    "Candidate verification batch failed; retrying split: %s",
                    message,
                )
                await process_batch(batch[:split], first_index)
                await process_batch(batch[split:], first_index + split)
                return

            batch_errors.append(message)
            log.warning("Candidate verification batch failed; fail closed: %s", message)
            for index in range(first_index, first_index + len(batch)):
                by_index[index] = "uncertain"
            return

        for item in value.get("documents") or []:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if first_index <= index < first_index + len(batch):
                status = str(item.get("status") or "uncertain").strip().lower()
                if status not in {"match", "uncertain", "reject"}:
                    status = "uncertain"
                by_index[index] = status
                reasons[index] = str(item.get("reason") or "").strip()[:500]
                binding = str(item.get("relation_binding") or "unclear").strip().lower()
                if binding not in {"direct", "reference_only", "contradicted", "unclear"}:
                    binding = "unclear"
                relation_bindings[index] = binding
                if compact:
                    evidence_frames[index] = _normalize_evidence_frame({
                        "relations": item.get("relations") or [],
                        "constraints": item.get("constraints") or [],
                        "mentioned_entities": item.get("mentioned_entities") or [],
                    })
                else:
                    evidence_frames[index] = _normalize_evidence_frame(item.get("evidence_frame"))

        # Missing entries stay fail-closed, but only within this small batch.
        for index in range(first_index, first_index + len(batch)):
            by_index.setdefault(index, "uncertain")

    for batch_start in range(0, len(bounded), batch_size):
        batch = bounded[batch_start: batch_start + batch_size]
        first = batch_start + 1
        last = batch_start + len(batch)
        if first == last:
            label = f"Prüfe Dokument {first} von {len(bounded)} …"
        else:
            label = f"Prüfe Dokumente {first}–{last} von {len(bounded)} …"
        _progress("verifying", label)
        await process_batch(batch, first)

    matches: list[SearchResult] = []
    uncertain: list[SearchResult] = []
    rejected = 0
    decision_log: list[tuple[int, str, str]] = []
    evidence_log: list[dict[str, Any]] = []
    for index, result in enumerate(bounded, start=1):
        llm_status = by_index.get(index, "uncertain")
        status = llm_status
        reason = reasons.get(index, "")
        relation_binding = relation_bindings.get(index, "unclear")
        status, reason = _audit_verifier_relation_binding(
            status, reason, relation_binding
        )
        status, reason = _audit_verifier_safe_constraints(
            question, result, status, reason
        )
        result.raw["verification_status"] = status
        result.raw["verification_relation_binding"] = relation_binding
        result.raw["verification_evidence_frame"] = evidence_frames.get(index, _empty_evidence_frame())
        if reason:
            result.raw["verification_reason"] = reason
        decision_log.append((index, _source_filename(result), status))
        if llm_status == "match" or status == "uncertain":
            evidence_log.append({
                "index": index,
                "file": _source_filename(result),
                "llm": llm_status,
                "final": status,
                "binding": relation_binding,
                "years": _observed_document_years(result),
                "reason": reason[:220],
            })
        if status == "match":
            matches.append(result)
        elif status == "uncertain":
            uncertain.append(result)
        else:
            rejected += 1

    log.info("RC8 candidate verifier decisions: %s", decision_log)
    if evidence_log:
        log.info("RC8 candidate verifier evidence: %s", evidence_log)
    reviewed_documents = []
    for index, result in enumerate(bounded, start=1):
        raw = result.raw or {}
        reviewed_documents.append({
            "index": index,
            "document_id": str(raw.get("document_id") or ""),
            "title": result.title,
            "path": str(raw.get("path") or raw.get("file_path") or ""),
            "status": str(raw.get("verification_status") or "uncertain"),
            "relation_binding": str(raw.get("verification_relation_binding") or "unclear"),
            "reason": str(raw.get("verification_reason") or "")[:500],
            "observed_years": _observed_document_years(result),
            "evidence_frame": raw.get("verification_evidence_frame") or _empty_evidence_frame(),
        })

    meta = {
        "checked": len(bounded),
        "matches": len(matches),
        "uncertain": len(uncertain),
        "rejected": rejected,
        "batch_size": batch_size,
        "format_mode": verifier_format_mode,
        "batch_errors": batch_errors,
        "batch_retries": batch_retries,
        "compact": compact,
        "elapsed_ms": round((time.perf_counter() - verify_started) * 1000),
        "reviewed_documents": reviewed_documents,
    }
    if batch_errors:
        meta["error"] = "; ".join(batch_errors)[:2000]
    return matches, uncertain, meta


async def _rag_multi_search(
    original_query: str,
    probes: list[dict[str, Any]],
    user_id: str | None,
    user_groups: str | None,
    request_id: str | None = None,
    *,
    exhaustive: bool = False,
    bounded_document_set: bool = False,
    strict_query: str = "",
    strict_required_document_ids: list[str] | None = None,
    force_unspecific: bool = False,
) -> tuple[dict[str, Any], list[SearchResult]]:
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if user_groups:
        headers["X-RAG-User-Groups"] = user_groups
    if request_id:
        headers["X-RAG-Request-ID"] = request_id

    if exhaustive:
        verification_pool = _retrieval_planner().exhaustive_verification_candidate_limit
    elif bounded_document_set:
        verification_pool = _retrieval_planner().bounded_verification_candidate_limit
    else:
        verification_pool = _retrieval_planner().verification_candidate_limit
    result_limit = max(SEARCH_LIMIT, verification_pool)
    _progress("retrieving", "Dokumente werden gesucht und Berechtigungen geprüft …")
    async with _middleware_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/multi-search",
            json={
                "original_query": original_query,
                "model": _active_model_id(),
                "probes": probes,
                "limit": result_limit,
                "exhaustive": bool(exhaustive),
                "strict_query": strict_query,
                "strict_required_document_ids": list(strict_required_document_ids or []),
                "force_unspecific": bool(force_unspecific),
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    parsed: list[SearchResult] = []
    for i, item in enumerate(payload.get("results", []), start=1):
        title = str(item.get("title") or item.get("document") or f"Dokument {i}")
        text = str(
            item.get("context_text")
            or item.get("text")
            or item.get("chunk")
            or ""
        ).strip()
        if text:
            parsed.append(SearchResult(index=i, title=title, text=text, raw=item))
    return payload, parsed


async def _elastic_search(
    query: str,
    user_id: str | None,
    user_groups: str | None,
    request_id: str | None = None,
    *,
    limit: int,
    include_content: bool = False,
    source_scopes: set[str] | None = None,
) -> tuple[dict[str, Any], list[SearchResult]]:
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if user_groups:
        headers["X-RAG-User-Groups"] = user_groups
    if request_id:
        headers["X-RAG-Request-ID"] = request_id

    _progress("retrieving", "Dokumente werden gesucht und Berechtigungen geprüft …")
    async with _middleware_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/elastic/search",
            json={
                "query": query,
                "model": _active_model_id(),
                "limit": int(limit),
                "include_content": bool(include_content),
                "source_scopes": sorted(source_scopes) if source_scopes else None,
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    parsed: list[SearchResult] = []
    for i, item in enumerate(payload.get("results", []), start=1):
        title = str(item.get("title") or item.get("document") or f"Dokument {i}")
        text = str(item.get("context_text") or item.get("es_snippet") or item.get("snippet") or "").strip()
        parsed.append(SearchResult(index=i, title=title, text=text, raw=item))
    return payload, parsed


async def _web_search(
    question: str,
    user_id: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run the separate public-web arm in the middleware."""
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if request_id:
        headers["X-RAG-Request-ID"] = request_id
    _progress("web", "Webquellen werden recherchiert …")
    async with _middleware_client(timeout=max(HTTP_TIMEOUT, 300.0)) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/web/search",
            json={"query": question},
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


async def _web_search_many(
    queries: list[str],
    user_id: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run a bounded set of web queries once and merge their evidence.

    Duplicate URLs are collapsed. A failed secondary query does not discard
    evidence already obtained from other queries; if every query fails, the
    first exception is re-raised so the caller keeps the existing error path.
    """
    cleaned: list[str] = []
    for item in queries:
        query = normalize_query_quotes(str(item or "")).strip()
        if query and query.casefold() not in {q.casefold() for q in cleaned}:
            cleaned.append(query)
        if len(cleaned) >= WEB_AFTER_MAX_QUERIES:
            break
    if not cleaned:
        return {"enabled": True, "queries": [], "sources": [], "searched": 0, "fetched": 0}
    if len(cleaned) == 1:
        payload = await _web_search(cleaned[0], user_id, request_id)
        payload = dict(payload)
        payload["queries"] = cleaned
        run_path = str(payload.get("archive_run_path") or "").strip()
        payload["archive_run_paths"] = [run_path] if run_path else []
        return payload

    merged_sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    archive_paths: list[str] = []
    archive_errors: list[dict[str, Any]] = []
    searched = 0
    fetched = 0
    enabled_seen = False
    successful_calls = 0
    first_exc: Exception | None = None

    for query in cleaned:
        try:
            payload = await _web_search(query, user_id, request_id)
        except Exception as exc:
            if first_exc is None:
                first_exc = exc
            archive_errors.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
            log.warning("Web subquery failed query=%r: %s: %s", query, type(exc).__name__, exc)
            continue

        successful_calls += 1
        enabled_seen = enabled_seen or bool(payload.get("enabled", True))
        try:
            searched += int(payload.get("searched") or 0)
        except (TypeError, ValueError):
            pass
        try:
            fetched += int(payload.get("fetched") or 0)
        except (TypeError, ValueError):
            pass
        run_path = str(payload.get("archive_run_path") or "").strip()
        if run_path and run_path not in archive_paths:
            archive_paths.append(run_path)
        archive_errors.extend(list(payload.get("archive_errors") or []))

        for source in list(payload.get("sources") or []):
            item = dict(source)
            url = str(item.get("final_url") or item.get("url") or "").strip()
            key = url.casefold() if url else f"title:{str(item.get('title') or '').strip().casefold()}"
            if key and key in seen_urls:
                continue
            if key:
                seen_urls.add(key)
            item.setdefault("search_query", query)
            merged_sources.append(item)

    if successful_calls == 0 and first_exc is not None:
        raise first_exc

    return {
        "enabled": enabled_seen,
        "queries": cleaned,
        "sources": merged_sources,
        "searched": searched,
        "fetched": fetched,
        "archive_run_path": archive_paths[0] if len(archive_paths) == 1 else "",
        "archive_run_paths": archive_paths,
        "archive_errors": archive_errors,
    }


async def _web_finalize_archive(
    run_path: str,
    answer_text: str,
    user_id: str | None,
    *,
    answer_model: str,
    answer_parameters: dict[str, Any],
    request_id: str | None = None,
) -> dict[str, Any]:
    """Write the actual answer-model output into the archived recherche.md."""
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if request_id:
        headers["X-RAG-Request-ID"] = request_id
    async with _middleware_client(timeout=max(HTTP_TIMEOUT, 120.0)) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/web/archive/finalize",
            json={
                "run_path": run_path,
                "answer_text": answer_text,
                "answer_model": answer_model,
                "answer_parameters": answer_parameters,
            },
            headers=headers,
        )
        response.raise_for_status()
        return response.json()



def _build_web_context(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    sources = list(payload.get("sources") or [])
    records: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for source in sources:
        text = str(source.get("evidence_text") or "").strip()
        if not text:
            continue
        index = len(candidates) + 1
        item = dict(source)
        item["index"] = index
        candidates.append(item)
        records.append(
            {
                "citation": f"[W{index}]",
                "index": index,
                "title": str(source.get("title") or source.get("final_url") or "Webquelle")[:512],
                "url": str(source.get("final_url") or source.get("url") or "")[:2048],
                "publisher": str(source.get("publisher") or "")[:512],
                "published_at": str(source.get("published_at") or "")[:128],
                "retrieved_at": str(source.get("retrieved_at") or "")[:128],
                "text": text,
            }
        )

    rendered, included_count = fit_evidence_records(
        records,
        kind="public_web",
        max_total_chars=CONTEXT_MAX_CHARS,
        per_record_max_chars=PER_RESULT_MAX_CHARS,
        min_partial_chars=400,
    )
    return rendered, candidates[:included_count]

def _web_answer_messages(question: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": guarded_evidence_prompt(_prompt("web_answer", WEB_ANSWER_SYSTEM_PROMPT))},
        {
            "role": "user",
            "content": f"FRAGE:\n{question}\n\nWEB-EVIDENCE:\n{context}",
        },
    ]


def _hybrid_web_answer_messages(
    question: str,
    retrieval_query: str,
    internal_context: str,
    web_context: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": guarded_evidence_prompt(_prompt("hybrid_web_answer", HYBRID_WEB_ANSWER_SYSTEM_PROMPT))},
        {
            "role": "user",
            "content": (
                f"FRAGE:\n{question}\n\n"
                f"INTERNE SUCHANFRAGE:\n{retrieval_query}\n\n"
                f"INTERNE EVIDENCE:\n{internal_context or '(keine hinreichende interne Evidence)'}\n\n"
                f"OEFFENTLICHE WEB-EVIDENCE:\n{web_context}"
            ),
        },
    ]


def _web_source_suffix(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return ""
    lines = ["", "", "**Öffentliche Quellen:**"]
    for source in sources:
        idx = int(source.get("index") or 0)
        title = str(source.get("title") or source.get("final_url") or "Webquelle").replace("[", "\\[").replace("]", "\\]")
        url = str(source.get("final_url") or source.get("url") or "").strip()
        line = f"- [W{idx}] [{title}]({url})" if url else f"- [W{idx}] {title}"
        meta: list[str] = []
        if source.get("published_at"):
            meta.append(f"veröffentlicht {source.get('published_at')}")
        if source.get("retrieved_at"):
            meta.append(f"abgerufen {str(source.get('retrieved_at'))[:19]}")
        if meta:
            line += " — " + ", ".join(meta)
        lines.append(line)
        archive = str(source.get("archive_path") or "").strip()
        raw_archive = str(source.get("archive_raw_path") or "").strip()
        pdf_archive = str(source.get("archive_pdf_path") or "").strip()
        metadata_archive = str(source.get("archive_metadata_path") or "").strip()
        if archive:
            lines.append(f"  Archiv: `{archive}`")
        if pdf_archive and pdf_archive != raw_archive:
            lines.append(f"  PDF: `{pdf_archive}`")
        if raw_archive:
            lines.append(f"  Original: `{raw_archive}`")
        if metadata_archive:
            lines.append(f"  Metadaten: `{metadata_archive}`")
    return "\n".join(lines)


async def _rag_resolve_documents(
    question: str,
    references: list[str],
    user_id: str | None,
    user_groups: str | None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], list[SearchResult]]:
    """Resolve explicit /use references without running normal retrieval."""
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = user_id
    if user_groups:
        headers["X-RAG-User-Groups"] = user_groups
    if request_id:
        headers["X-RAG-Request-ID"] = request_id

    _progress("retrieving", "Ausgewählte Dokumente werden geladen und Berechtigungen geprüft …")
    async with _middleware_client(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/documents/resolve",
            json={
                "query": question,
                "model": _active_model_id(),
                "references": references,
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    parsed: list[SearchResult] = []
    for i, item in enumerate(payload.get("results", []), start=1):
        title = str(item.get("title") or item.get("document") or f"Dokument {i}")
        text = str(
            item.get("context_text")
            or item.get("text")
            or item.get("chunk")
            or ""
        ).strip()
        parsed.append(SearchResult(index=i, title=title, text=text, raw=item))
    return payload, parsed


async def _resolve_followup_evidence(
    messages: list[dict[str, Any]],
    *,
    query: str,
    user_id: str | None,
    user_groups: str | None,
    source_scopes: set[str] | None,
    request_id: str | None,
) -> list[SearchResult]:
    """Re-resolve a small previous-answer evidence set through current live ACL."""
    references = _previous_supporting_document_ids(
        messages,
        limit=FOLLOWUP_EVIDENCE_DOCUMENTS,
    )
    if not references:
        return []
    try:
        _, resolved = await _rag_resolve_documents(
            query,
            references,
            user_id,
            user_groups,
            request_id=request_id,
        )
    except Exception as exc:
        # Continuity is a recall aid, never an authorization or availability
        # dependency. Normal retrieval continues if the optional lane fails.
        log.warning(
            "Follow-up evidence continuity unavailable; normal retrieval continues: %s: %s",
            type(exc).__name__,
            exc,
        )
        return []

    allowed: list[SearchResult] = []
    for result in resolved:
        raw = dict(result.raw)
        document_id = str(raw.get("document_id") or "").strip()
        path = str(raw.get("path") or raw.get("title") or result.title or "").strip()
        if not source_scope_allows_record(
            document_id,
            path,
            source_scopes,
            indexed_origin=str(raw.get("source_origin") or "").strip() or None,
        ):
            continue
        raw["_followup_evidence"] = True
        allowed.append(
            SearchResult(
                index=result.index,
                title=result.title,
                text=result.text,
                raw=raw,
            )
        )
    return allowed


def _merge_followup_evidence(
    continuity: list[SearchResult],
    ranked: list[SearchResult],
) -> list[SearchResult]:
    """Put prior authorized evidence in front without duplicating document IDs."""
    merged: list[SearchResult] = []
    seen: set[str] = set()
    for result in [*continuity, *ranked]:
        document_id = str(result.raw.get("document_id") or "").strip()
        fallback_key = f"{result.title}\n{result.text[:200]}"
        key = document_id or fallback_key
        if key in seen:
            continue
        seen.add(key)
        merged.append(result)
    return [
        SearchResult(index=i, title=result.title, text=result.text, raw=result.raw)
        for i, result in enumerate(merged, start=1)
    ]


def _eligible_entity_recall_backoff(payload: dict[str, Any]) -> bool:
    """Return True only for a single-entity, non-relation retrieval plan."""
    if not ENTITY_RECALL_BACKOFF_ENABLED:
        return False

    plan = payload.get("plan") or {}
    if bool(plan.get("entity_relation_query")):
        return False

    entities = [
        entity
        for entity in (plan.get("entity_mentions") or [])
        if str(entity.get("status") or "")
        in {"resolved", "ambiguous", "fuzzy_candidate"}
        and str(entity.get("mention") or "").strip()
    ]
    return len(entities) == 1


async def _graph_enqueue_evidence(
    *,
    query_id: str,
    user_query: str,
    retrieval_query: str,
    evidence_action: str,
    results: list[SearchResult],
    rag_user_id: str = "",
) -> None:
    """Fail-open hook: enqueue selected/cited answer evidence for graph construction."""
    if not GRAPH_EVIDENCE_HOOK_ENABLED or not results:
        return

    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        raw = result.raw or {}
        document_id = str(raw.get("document_id") or "").strip()
        if not document_id or document_id in seen:
            continue
        # Immediate /use:Wn can fall back to a synthetic ID if Nextcloud did
        # not expose oc:fileid in PROPFIND.  Do not create a duplicate graph
        # document; the normal sync will enqueue the archived source later.
        if raw.get("webarchive_direct") and document_id.startswith("webarchive:"):
            continue
        seen.add(document_id)
        documents.append({
            "document_id": document_id,
            "title": result.title,
            "path": raw.get("path"),
            "source_url": raw.get("source_url"),
            "document_date": raw.get("document_date"),
            # Middleware fetches the full ES content. This is only a fallback if
            # Elasticsearch cannot return the source document temporarily.
            "context_text": result.text,
            "rag_user_id": str(rag_user_id or ""),
        })

    if not documents:
        return

    # The middleware API deliberately limits one graph batch.  In degraded
    # Evidence-controller fallback paths the answer context can contain more
    # documents than that (e.g. the whole reranker pool).  Never let this
    # optional, fail-open hook turn that into a 422 request.
    documents = documents[:GRAPH_EVIDENCE_HOOK_MAX_DOCUMENTS]

    payload = {
        "query_id": query_id,
        "user_query": user_query,
        "retrieval_query": retrieval_query,
        "evidence_action": evidence_action,
        "documents": documents,
    }
    try:
        async with _middleware_client(timeout=GRAPH_EVIDENCE_HOOK_TIMEOUT) as client:
            response = await client.post(
                f"{RAG_MIDDLEWARE_URL}/graph/enqueue-evidence",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        log.info(
            "Graph evidence queued: action=%s docs=%d queued=%s coalesced=%s jobs=%s",
            evidence_action,
            len(documents),
            data.get("queued", 0),
            data.get("coalesced", 0),
            data.get("job_ids") or [],
        )
    except Exception as exc:
        # Graph construction is an enrichment layer. It must never prevent the
        # already-reviewed document answer from reaching the user.
        log.warning(
            "Graph evidence enqueue failed; answer continues unchanged: %s: %s",
            type(exc).__name__,
            exc,
        )


async def _store_use_research_findings(
    *,
    query_id: str,
    question: str,
    retrieval_query: str,
    results: list[SearchResult],
    canonical_user_id: str = "",
    nextcloud_login: str = "",
    nextcloud_server: str = "",
) -> None:
    """Persist /use evidence through the same planner/verifier contract as search Findings.

    Explicit document selection bypasses normal retrieval, so it otherwise never
    reaches the planner/verifier Findings hook.  Only when Findings are enabled do
    we pay for a structured rewrite and verifier pass.  Work on copies so a
    verifier classification cannot remove or reorder the user's explicitly chosen
    answer context.
    """
    if not RESEARCH_FINDINGS_ENABLED or not results:
        return

    try:
        rewrite = await _rewrite_search_spec(
            question=retrieval_query,
            round_no=1,
            results=[],
            previous_spec=None,
            retrieval_arms={"files"},
            seed_context=None,
        )
        if not bool(rewrite.get("valid")):
            log.info("Research Findings skipped for /use: structured rewrite invalid")
            return

        spec = dict(rewrite.get("spec") or {})
        query_frame = normalize_query_frame(
            query_frame_from_search_spec(spec, intent=retrieval_query)
        )
        review_results = [
            SearchResult(
                index=result.index,
                title=result.title,
                text=result.text,
                raw=dict(result.raw or {}),
            )
            for result in results
        ]
        verified, _uncertain, verification = await _verify_exhaustive_candidates(
            retrieval_query,
            review_results,
            query_frame=query_frame,
            verification_requirements=list(spec.get("verification_requirements") or []),
            candidate_limit=min(
                len(review_results),
                _retrieval_planner().exhaustive_verification_candidate_limit,
            ),
            compact=True,
            # /use is an explicitly bounded document set.  The administrator has
            # already opted into Finding persistence, so review the configured
            # bounded set rather than applying the ordinary remote small-window cap.
            exhaustive=True,
        )
        await _store_positive_research_findings(
            query_id=query_id,
            query_frame=query_frame,
            results=verified,
            canonical_user_id=canonical_user_id,
            nextcloud_login=nextcloud_login,
            nextcloud_server=nextcloud_server,
            user_query=question,
            retrieval_query=retrieval_query,
            source_scopes=None,
        )
        log.info(
            "Research Findings /use review: selected=%d checked=%d matches=%d",
            len(review_results),
            int(verification.get("checked") or 0),
            int(verification.get("matches") or 0),
        )
    except Exception as exc:
        # Findings are an optional enrichment layer and must never break /use.
        log.warning(
            "Research Findings /use review failed; answer continues unchanged: %s: %s",
            type(exc).__name__,
            exc,
        )


async def _run_use_research_findings_background(**kwargs: Any) -> None:
    _ACTIVE_PROGRESS_ID.set("")
    _ACTIVE_PROGRESS_OWNER.set("")
    await _store_use_research_findings(**kwargs)


def _schedule_use_research_findings(**kwargs: Any) -> None:
    if not RESEARCH_FINDINGS_ENABLED or not kwargs.get("results"):
        return
    task = asyncio.create_task(_run_use_research_findings_background(**kwargs))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _store_positive_research_findings(
    *,
    query_id: str,
    query_frame: dict[str, Any],
    results: list[SearchResult],
    canonical_user_id: str = "",
    nextcloud_login: str = "",
    nextcloud_server: str = "",
    user_query: str = "",
    retrieval_query: str = "",
    source_scopes: set[str] | None = None,
) -> None:
    """Fail-open persistence of already-paid planner/verifier work.

    This hook performs no model call. Only final Candidate-Verifier matches with
    direct document binding are sent to the middleware. Negative/uncertain
    candidates are intentionally omitted and therefore never become graph facts.
    """
    if not RESEARCH_FINDINGS_ENABLED or not results:
        return

    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_special_origins = {
        "mailarchive": "mail_archive",
        "webarchive": "web_archive",
        "chatarchive": "chat_archive",
    }
    selected_special_origins = {
        origin
        for scope, origin in allowed_special_origins.items()
        if source_scopes and scope in source_scopes
    }
    for result in results:
        raw = result.raw or {}
        document_id = str(raw.get("document_id") or "").strip()
        status = str(raw.get("verification_status") or "").strip().lower()
        binding = str(raw.get("verification_relation_binding") or "").strip().lower()
        source_origin = str(raw.get("source_origin") or "").strip()
        if (
            source_scopes
            and source_origin in set(allowed_special_origins.values())
            and source_origin not in selected_special_origins
        ):
            log.warning(
                "Research Finding skipped outside explicit source scope: document_id=%s origin=%s scopes=%s",
                document_id, source_origin, sorted(source_scopes),
            )
            continue
        if not document_id or document_id in seen or status != "match" or binding != "direct":
            continue
        seen.add(document_id)
        documents.append({
            "document_id": document_id,
            "title": result.title,
            "path": raw.get("path") or raw.get("file_path"),
            "source_url": raw.get("source_url"),
            "document_date": raw.get("document_date"),
            "source_origin": raw.get("source_origin"),
            "verification_status": status,
            "relation_binding": binding,
            "evidence_frame": raw.get("verification_evidence_frame") or {},
        })
        if len(documents) >= RESEARCH_FINDINGS_MAX_DOCUMENTS:
            break

    if not documents:
        return

    payload = {
        "query_id": query_id,
        "canonical_user_id": canonical_user_id,
        "nextcloud_login": nextcloud_login,
        "nextcloud_server": nextcloud_server,
        "user_query": user_query,
        "retrieval_query": retrieval_query,
        "source_scopes": sorted(source_scopes) if source_scopes else None,
        "provenance_code": "aki_research",
        "provenance_label": "SunaQ Recherche",
        "query_frame": normalize_query_frame(query_frame),
        "software_version": VERSION,
        "planner_model": _retrieval_planner().model or ANSWER_MODEL,
        "verifier_model": _retrieval_planner().model or LLM_MODEL,
        "documents": documents,
    }
    try:
        async with _middleware_client(timeout=RESEARCH_FINDINGS_TIMEOUT) as client:
            response = await client.post(
                f"{RAG_MIDDLEWARE_URL}/graph/research-findings",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        log.info(
            "AKI research findings persisted: docs=%d stored=%s skipped=%s frame=%s",
            len(documents),
            data.get("stored", 0),
            data.get("skipped", 0),
            str(data.get("frame_hash") or "")[:12],
        )
    except Exception as exc:
        # Re-use is an enrichment feature. Retrieval and the user answer must
        # remain available when Neo4j is disabled or temporarily unavailable.
        log.warning(
            "AKI research finding persistence failed; answer continues unchanged: %s: %s",
            type(exc).__name__,
            exc,
        )



def _build_context(
    results: list[SearchResult],
    *,
    per_result_max_chars: int | None = None,
    context_max_chars: int | None = None,
    preserve_all_results: bool = False,
) -> tuple[str, list[SearchResult]]:
    """Build structured JSON evidence and return exactly the included results.

    The existing local/remote/profile budgets remain authoritative. Evidence is
    serialized as JSON records so document text or metadata cannot syntactically
    impersonate SunaQ-generated source boundaries.
    """
    effective_per_result = int(
        PER_RESULT_MAX_CHARS if per_result_max_chars is None else per_result_max_chars
    )
    effective_total = int(
        CONTEXT_MAX_CHARS if context_max_chars is None else context_max_chars
    )
    effective_results = results
    profile_budget = _answer_context_budget()
    if profile_budget is not None:
        effective_per_result = (
            profile_budget["max_chars_per_document"]
            if per_result_max_chars is None
            else min(
                effective_per_result,
                profile_budget["max_chars_per_document"],
            )
        )
        effective_total = (
            profile_budget["max_total_chars"]
            if context_max_chars is None
            else min(
                effective_total,
                profile_budget["max_total_chars"],
            )
        )
        if not preserve_all_results:
            effective_results = effective_results[:profile_budget["max_documents"]]

    remote_answer = _role_remote("answer")
    if remote_answer:
        if profile_budget is not None:
            effective_per_result = min(
                effective_per_result,
                _sunaq_remote_cap(
                    SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT,
                    REMOTE_LLM_MAX_CHARS_PER_DOCUMENT,
                    "REMOTE_LLM_MAX_CHARS_PER_DOCUMENT",
                ),
            )
            effective_total = min(
                effective_total,
                _sunaq_remote_cap(
                    SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS,
                    REMOTE_LLM_MAX_TOTAL_CHARS,
                    "REMOTE_LLM_MAX_TOTAL_CHARS",
                ),
            )
            if not preserve_all_results:
                effective_results = effective_results[
                    :_sunaq_remote_cap(
                        SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS,
                        REMOTE_ANSWER_MAX_DOCUMENTS,
                        "REMOTE_ANSWER_MAX_DOCUMENTS",
                    )
                ]
        else:
            effective_per_result = min(
                effective_per_result,
                REMOTE_LLM_MAX_CHARS_PER_DOCUMENT,
            )
            effective_total = min(effective_total, REMOTE_LLM_MAX_TOTAL_CHARS)
            if not preserve_all_results:
                effective_results = results[:REMOTE_ANSWER_MAX_DOCUMENTS]

    records: list[dict[str, Any]] = []
    for result in effective_results:
        raw = result.raw or {}
        record: dict[str, Any] = {
            "citation": f"[{result.index}]",
            "title": str(result.title or "")[:512],
            "text": str(result.text or ""),
        }
        optional_metadata = {
            "document_id": str(raw.get("document_id") or "")[:512],
            "path": str(raw.get("path") or raw.get("file_path") or "")[:1024],
            "source_origin": str(raw.get("source_origin") or "")[:128],
            "source_date": str(raw.get("source_date") or "")[:128],
            "document_date": str(raw.get("document_date") or "")[:128],
        }
        record.update({key: value for key, value in optional_metadata.items() if value})
        records.append(record)

    context, included_count = fit_evidence_records(
        records,
        kind="documents",
        max_total_chars=effective_total,
        per_record_max_chars=effective_per_result,
        preserve_all=preserve_all_results,
        # JSON metadata consumes part of the historic character budget. Keep
        # the final admitted ranked document when at least a useful excerpt fits.
        min_partial_chars=200,
    )
    return context, list(effective_results[:included_count])


def _build_review_context(
    results: list[SearchResult],
) -> tuple[str, list[SearchResult]]:
    """Compact structured evidence for the control model."""

    effective_results = results
    effective_per_result = EVIDENCE_PER_RESULT_MAX_CHARS
    effective_total = EVIDENCE_CONTEXT_MAX_CHARS
    if _role_remote("evidence"):
        effective_results = results[:REMOTE_ANSWER_MAX_DOCUMENTS]
        effective_per_result = min(effective_per_result, REMOTE_LLM_MAX_CHARS_PER_DOCUMENT)
        effective_total = min(effective_total, REMOTE_LLM_MAX_TOTAL_CHARS)

    records: list[dict[str, Any]] = []
    for result in effective_results:
        raw = result.raw or {}
        pieces: list[str] = []
        es_text = str(raw.get("es_snippet") or "").strip()
        vector_text = str(raw.get("vector_snippet") or "").strip()
        graph_text = str(raw.get("graph_snippet") or "").strip()
        graph_rank = raw.get("graph_rank")
        graph_reason = str(raw.get("graph_reason") or "").strip()
        graph_entities = [
            str(x).strip() for x in (raw.get("graph_entities") or [])
            if str(x).strip()
        ]

        if graph_rank is not None and graph_text:
            graph_meta = []
            if graph_reason:
                graph_meta.append(f"Graph-Signal: {graph_reason}")
            if graph_entities:
                graph_meta.append("Graph-Entities: " + ", ".join(graph_entities[:6]))
            prefix = ("; ".join(graph_meta) + "\n") if graph_meta else ""
            pieces.append("GRAPH:\n" + prefix + graph_text[:1200])

        if es_text:
            pieces.append("ES: " + es_text[:700])
        if vector_text and vector_text.casefold() != es_text.casefold():
            pieces.append("Vektor: " + vector_text[:700])

        enriched_text = str(result.text or "").strip()
        if bool(raw.get("context_enriched")) and enriched_text:
            review_text = enriched_text
        else:
            review_text = "\n".join(pieces).strip() or enriched_text

        records.append(
            {
                "citation": f"[{result.index}]",
                "document_id": str(raw.get("document_id") or "")[:512],
                "title": str(result.title or "")[:512],
                "path": str(raw.get("path") or raw.get("file_path") or "")[:1024],
                "source_origin": str(raw.get("source_origin") or "")[:128],
                "source_date": str(raw.get("source_date") or "")[:128],
                "document_date": str(raw.get("document_date") or "")[:128],
                "text": review_text,
            }
        )

    rendered, included_count = fit_evidence_records(
        records,
        kind="evidence_review",
        max_total_chars=effective_total,
        per_record_max_chars=effective_per_result,
        min_partial_chars=400,
    )
    return rendered, list(effective_results[:included_count])

def _select_results_by_indexes(
    results: list[SearchResult],
    indexes: list[int],
) -> list[SearchResult]:
    """Return the requested evidence documents, preserving controller order.

    Source numbers remain the original retrieval numbers, so citations such as
    [1] or [4] continue to map to the correct source even when only a subset is
    sent to the answer model.
    """
    by_index = {result.index: result for result in results}
    selected: list[SearchResult] = []

    for index in indexes:
        result = by_index.get(index)
        if result is not None and result not in selected:
            selected.append(result)

    return selected


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Evidence decision is not a JSON object")
    return value


async def _evidence_decision(
    question: str,
    retrieval_query: str,
    context: str,
    *,
    force_broad: bool = False,
) -> dict[str, Any]:
    _progress("evidence", "Evidenz wird geprüft …")
    force_instruction = ""
    if force_broad:
        force_instruction = (
            "\n\nFORCE-HINWEIS (verbindlich): Der Benutzer hat /force gesetzt. "
            "Ein breites, heterogenes oder umfangreiches Trefferbild ist FUER SICH ALLEIN "
            "kein Grund fuer retry, clarify oder insufficient. Pruefe, ob die konkrete "
            "Frage aus einzelnen oder mehreren der gelieferten Treffer beantwortbar ist, "
            "und waehle dann gezielt answer_sources. Wenn die verlangte Information in den "
            "Treffern tatsaechlich nicht belegt ist, bleibt insufficient weiterhin erlaubt."
        )

    prompt = (
        "Prüfe ausschließlich die Qualität des Trefferbilds. Beantworte die Sachfrage NICHT "
        "und fasse die Dokumente NICHT zusammen.\n\n"
        "AKTUELLE BENUTZERFRAGE:\n"
        f"{question}\n\n"
        "AKTUELLE SUCHANFRAGE:\n"
        f"{retrieval_query}\n\n"
        "DOKUMENTTREFFER:\n"
        f"{context}"
        f"{force_instruction}\n\n"
        "Gib ausschließlich das verlangte Kontroll-JSON aus."
    )

    async def run_once(
        user_prompt: str,
        max_tokens: int,
        response_format: str | dict[str, Any],
    ) -> dict[str, Any]:
        raw = await _ollama_complete(
            [
                {"role": "system", "content": guarded_evidence_prompt(_prompt("evidence", EVIDENCE_DECISION_SYSTEM_PROMPT))},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            think=False,
            model=EVIDENCE_MODEL,
            role="evidence",
            response_format=response_format,
        )
        if not raw:
            raise ValueError("Evidence model returned empty content")
        value = _extract_json_object(raw)
        action = str(value.get("action") or "").strip().lower()
        if action not in {"answer", "retry", "clarify", "insufficient", "conflict"}:
            raise ValueError(f"Evidence JSON has no valid action: {raw[:240]!r}")
        value["action"] = action
        return value

    try:
        value = await run_once(prompt, EVIDENCE_MAX_TOKENS, EVIDENCE_RESPONSE_SCHEMA)
    except httpx.HTTPStatusError as exc:
        # Compatibility path for Ollama versions that accept format="json" but
        # not a JSON-schema object.
        if exc.response.status_code != 400:
            raise
        log.warning(
            "Evidence JSON schema rejected by LLM backend; retrying in plain JSON mode: %s",
            exc,
        )
        value = await run_once(prompt + "\n/no_think", max(EVIDENCE_MAX_TOKENS, 1000), "json")
    except (ValueError, json.JSONDecodeError) as first_exc:
        log.warning(
            "Evidence decision invalid; retrying once with stricter instruction: %s",
            first_exc,
        )
        retry_prompt = (
            prompt
            + "\n\nFEHLERHINWEIS: Deine vorige Ausgabe entsprach nicht dem Schema. "
              "Keine Zusammenfassung, keine neue Aufgabe, kein Feld 'input'. "
              "Beginne direkt mit {\\\"action\\\": ...}.\n/no_think"
        )
        value = await run_once(
            retry_prompt,
            max(EVIDENCE_MAX_TOKENS, 1000),
            EVIDENCE_RESPONSE_SCHEMA,
        )

    action = str(value["action"]).strip().lower()

    options: list[dict[str, str]] = []
    raw_options = value.get("clarification_options") or []
    if isinstance(raw_options, list):
        for option in raw_options[:4]:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            query = str(option.get("query") or "").strip()
            if label and query:
                options.append({"label": label, "query": query})

    conflict_sources: list[int] = []
    raw_conflict_sources = value.get("conflict_sources") or []
    if isinstance(raw_conflict_sources, list):
        for source_index in raw_conflict_sources:
            try:
                number = int(source_index)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in conflict_sources:
                conflict_sources.append(number)

    answer_sources: list[int] = []
    raw_answer_sources = value.get("answer_sources") or []
    if isinstance(raw_answer_sources, list):
        for source_index in raw_answer_sources:
            try:
                number = int(source_index)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in answer_sources:
                answer_sources.append(number)

    return {
        "action": action,
        "reason": str(value.get("reason") or "").strip(),
        "next_query": str(value.get("next_query") or "").strip(),
        "clarification_options": options,
        "conflict_sources": conflict_sources[:8],
        "answer_sources": answer_sources[:4],
    }


def _format_conflict(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason") or "").strip()
    sources = [
        int(value)
        for value in (decision.get("conflict_sources") or [])
        if isinstance(value, int) and value > 0
    ]

    lines = [
        "Die Dokumenttreffer widersprechen sich in einem wesentlichen Punkt."
    ]
    if reason:
        lines.append(reason)
    if sources:
        lines.append(
            "Betroffene Belege: "
            + ", ".join(f"[{number}]" for number in sources)
            + "."
        )
    lines.append(
        "Ich löse diesen Widerspruch nicht spekulativ auf."
    )
    return " ".join(lines)


def _format_clarification(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason") or "Die Treffer lassen mehrere getrennte Suchrichtungen zu.").strip()
    lines = [reason, "", "Welche Richtung meinst Du?"]
    for index, option in enumerate(decision.get("clarification_options") or [], start=1):
        lines.append(f"{index}. **{option['label']}** — Suchanfrage: `{option['query']}`")
    if len(lines) == 3:
        lines.append("Bitte präzisiere den gemeinten Vorgang oder nenne einen zusätzlichen Suchbegriff.")
    return "\n".join(lines)


def _format_insufficient(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason") or "").strip()
    if reason:
        return (
            "Ich habe in den vorliegenden Dokumenttreffern keine hinreichenden Belege "
            f"für eine belastbare Antwort gefunden. {reason}"
        )
    return (
        "Ich habe in den vorliegenden Dokumenttreffern keine hinreichenden Belege "
        "für eine belastbare Antwort gefunden."
    )



ANSWER_GROUNDING_GUARD = """

ZUSÄTZLICHE EVIDENZREGELN (verbindlich):
- Behaupte nur konkrete Rollen, Funktionen oder Beziehungen, die im bereitgestellten
  Dokumentkontext unmittelbar gestützt werden. Gemeinsames Vorkommen zweier Namen
  ist KEIN Beleg für Geschäftsführerstellung, Beschäftigung, Beteiligung, Eigentum
  oder eine andere organisatorische Rolle.
- Bei Fragen nach der Beziehung zwischen A und B beschreibe ausschließlich die
  Beziehung, die die ausgewählten Unterlagen tatsächlich zwischen genau A und B
  erkennen lassen. Ergänze keine plausibel klingende Rolle aus Vorwissen oder aus
  dem bloßen Dokumentkontext.
- OCR-/PDF-Text kann räumliche Layoutinformation verlieren. Insbesondere bei
  Briefen darf aus der linearen Nachbarschaft von Firmenname, Personenname und
  Anschrift NICHT geschlossen werden, wer Absender, Empfänger, Geschäftsführer
  oder sonstiger Funktionsträger ist. Nutze dafür nur eindeutige Formulierungen
  im Dokument (z. B. Anrede, "Geschäftsführer:", "vertreten durch").
- Formuliere Vorwürfe, Absichten, Bewertungen oder Behauptungen aus einem Dokument
  als solche (z. B. "in dem Schreiben wird A vorgeworfen ..."), nicht als
  objektiv feststehende Tatsache.
- Wenn die Unterlagen eine bestimmte behauptete Rolle nicht belegen, sage das
  ausdrücklich. Erfinde keine fehlende Verbindung.
- Formuliere negative Rechercheergebnisse epistemisch und suchbezogen: z. B.
  "Ich habe in den vorliegenden Dokumenten keinen Beleg für ... gefunden".
  Behaupte aus einer begrenzten Trefferliste niemals pauschal "es gibt keinen
  Beleg" oder "es existiert kein Dokument", sofern dies nicht anderweitig
  vollständig nachgewiesen ist.
""".strip()


DOCUMENT_ANALYSIS_GUARD = """

ZUSÄTZLICHE REGELN FÜR EXPLIZIT AUSGEWÄHLTE DOKUMENTE (verbindlich):
- Der bereitgestellte Kontext stammt aus den vom Benutzer ausdrücklich ausgewählten
  Dokumenten. Werte den tatsächlich gelieferten Dokumenttext vollständig aus.
- Wenn der Benutzer die ausgewählten Dokumente auflisten, tabellarisch darstellen,
  sortieren, vergleichen oder pro Dokument Felder extrahieren lässt, ist JEDES
  ausgewählte Dokument ein eigener Datensatz. Lass kein Dokument wegen ähnlicher,
  redundanter oder fehlender Einzelangaben weg. Gib in solchen Aufgaben genau eine
  Zeile bzw. einen eindeutig zuordenbaren Eintrag pro ausgewähltem Dokument aus.
- Fehlt ein verlangtes Feld in einem ausgewählten Dokument oder ist es nicht sicher
  erkennbar, behalte das Dokument trotzdem in der Ausgabe und kennzeichne das Feld
  als "nicht eindeutig erkennbar". Verdichte mehrere ausgewählte Dokumente niemals
  zu einem gemeinsamen Eintrag, wenn der Benutzer eine dokumentweise Auswertung will.
- Zitiere bei dokumentweiser Liste/Tabelle jeden Eintrag mit dem Quellenmarker des
  zugehörigen Dokuments [n], damit die Vollständigkeit überprüfbar bleibt.
- Übernimm wörtlich vorhandene Sachangaben wie Datum, Rechnungsnummer, Firmennamen,
  postalische Anschriften, Beträge, Umsatzsteuer, Leistungsbeschreibung und Zeitraum
  zuverlässig. Behaupte nicht, eine solche Angabe fehle oder sei "nicht explizit
  genannt", wenn der Wert im Dokumenttext tatsächlich vorkommt.
- PDF-/OCR-/Office-Extraktion kann räumliches Layout linearisieren. Verwechsle fehlende
  Layoutinformation nicht mit fehlendem Inhalt. Ein unmittelbar zusammenhängender
  Firmen-/Adressblock darf als im Dokument gemeinsam auftretender Adressblock
  beschrieben werden.
- Leite dennoch keine organisatorische Rolle allein aus bloßer Nachbarschaft ab. Bei
  Briefen/Rechnungen darf die Dokumentstruktur als schwaches Layoutsignal genutzt
  werden, wenn mehrere eindeutige Merkmale zusammenpassen (z. B. eigener Briefkopf
  und Signatur für den Aussteller; separater Adressblock vor Betreff/Rechnung für den
  Adressaten). Formuliere im Zweifel dokumentbezogen statt absolut.
- Ein unlabeled Datumswert im typischen Dokumentkopf ist nicht "fehlend". Wenn die
  genaue Feldbezeichnung nicht im Text steht, formuliere z. B. "Das Dokument ist auf
  den 10. September 2025 datiert" statt eine nicht vorhandene Feldüberschrift zu
  erfinden.
- Erfinde weiterhin keine Tatsachen oder Beziehungen, die im ausgewählten Dokumenttext
  nicht gestützt sind.
""".strip()


def _render_rag_prompt(
    context: str,
    retrieval_query: str,
    *,
    document_analysis: bool = False,
    selected_document_count: int | None = None,
) -> str:
    base = (
        _prompt("answer", RAG_ANSWER_TEMPLATE)
        .replace("{{context}}", context)
        .replace("{{retrieval_query}}", retrieval_query)
    )
    guard = DOCUMENT_ANALYSIS_GUARD if document_analysis else ANSWER_GROUNDING_GUARD
    if document_analysis and selected_document_count:
        guard += (
            "\n- Der bereitgestellte explizite Dokumentensatz enthält genau "
            f"{int(selected_document_count)} Dokument(e). Wenn die Benutzeraufgabe eine "
            "dokumentweise Liste/Tabelle/Sortierung/Extraktion verlangt, muss die Ausgabe "
            f"genau {int(selected_document_count)} Dokumenteinträge enthalten; fehlende Felder "
            "werden markiert, nicht durch Weglassen des Dokuments behandelt."
        )
    return guarded_evidence_prompt(base + "\n\n" + guard)


def _rag_answer_messages(
    question: str,
    retrieval_query: str,
    context: str,
    *,
    document_analysis: bool = False,
    selected_document_count: int | None = None,
) -> list[dict[str, str]]:
    # Critical design choice: NO raw prior chat history here.
    # The rewriter may resolve references, but the answerer sees only the current
    # question, the resolved retrieval query in the system prompt, and documents.
    return [
        {
            "role": "system",
            "content": _render_rag_prompt(
                context,
                retrieval_query,
                document_analysis=document_analysis,
                selected_document_count=selected_document_count,
            ),
        },
        {"role": "user", "content": question},
    ]


def _direct_messages(request_messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    # UI helper tasks may legitimately depend on the immediate chat, so keep a
    # small recent slice here. Do not inherit OpenWebUI/client system prompts.
    result: list[dict[str, str]] = [{"role": "system", "content": _prompt("direct", DIRECT_SYSTEM_PROMPT)}]
    for item in _prior_conversation(request_messages):
        result.append(item)
    latest = _latest_user_message(request_messages)
    if latest:
        result.append({"role": "user", "content": latest})
    return result


_DIRECT_GREETING_PATTERNS = (
    re.compile(r"^(?:hallo|hi|hey|moin|servus|guten\s+(?:morgen|tag|abend))[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:danke|vielen\s+dank|besten\s+dank|dankesch(?:ö|oe)n)[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:tsch(?:ü|u)ss|auf\s+wiedersehen|bis\s+(?:später|spaeter|dann))[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^wie\s+geht(?:'s|\s+es)\s+dir[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:wer|was)\s+bist\s+du[!.?\s]*$", re.IGNORECASE),
)

_DIRECT_TIME_PATTERNS = (
    re.compile(r"^wie\s+spät\s+ist\s+es[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^wie\s+spaet\s+ist\s+es[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^wie\s*(?:viel|viele)\s+uhr\s+ist\s+es[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^wieviel\s+uhr\s+ist\s+es[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^welche\s+uhrzeit\s+ist\s+es[!.?\s]*$", re.IGNORECASE),
)

_DIRECT_DATE_PATTERNS = (
    re.compile(r"^welches\s+datum\s+haben\s+wir[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^welcher\s+tag\s+ist\s+heute[!.?\s]*$", re.IGNORECASE),
    re.compile(r"^was\s+ist\s+(?:heute\s+für\s+ein|das\s+heutige)\s+datum[!.?\s]*$", re.IGNORECASE),
)

_DIRECT_ARITHMETIC = re.compile(
    r"^(?:was\s+ist\s+)?[0-9\s.,()+\-*/%^]+[!?\s]*$",
    re.IGNORECASE,
)


def _direct_query_kind(question: str) -> str | None:
    """Conservatively classify queries that clearly need no document retrieval.

    This gate is deliberately narrow.  A false negative only costs a normal RAG
    lookup; a false positive could suppress useful private-document evidence.
    Therefore ordinary knowledge questions, names, organisations and ambiguous
    short prompts continue through retrieval.
    """
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    if not text or len(text) > 160:
        return None

    for pattern in _DIRECT_GREETING_PATTERNS:
        if pattern.fullmatch(text):
            return "conversation"
    for pattern in _DIRECT_TIME_PATTERNS:
        if pattern.fullmatch(text):
            return "time"
    for pattern in _DIRECT_DATE_PATTERNS:
        if pattern.fullmatch(text):
            return "date"

    # Only a bare arithmetic expression (optionally prefixed by "Was ist") is
    # direct.  Any words beyond that keep the conservative RAG path.
    if _DIRECT_ARITHMETIC.fullmatch(text) and re.search(r"\d", text):
        return "arithmetic"
    return None


def _direct_answer_messages(question: str, kind: str) -> list[dict[str, str]]:
    now = datetime.now().astimezone()
    runtime_context = (
        f"Aktuelle lokale Serverzeit: {now.isoformat(timespec='seconds')}\n"
        f"Direktmodus: {kind}"
    )
    system = _prompt("direct_answer", DIRECT_ANSWER_SYSTEM_PROMPT).replace("{{runtime_context}}", runtime_context)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


async def _ollama_stream(
    messages: list[dict[str, str]],
    *,
    options: dict[str, Any],
    think: bool | str | None = None,
    model: str | None = None,
    max_seconds: float | None = None,
    role: str = "answer",
) -> AsyncIterator[dict[str, str]]:
    """Compatibility wrapper around the configured backend stream.

    Each yielded event may contain ``content`` and/or ``reasoning``.
    The watchdog measures the whole upstream stream, including reasoning.
    """

    selected = _role_backend(role)
    selected_model = _role_model(role, model)

    async def consume() -> AsyncIterator[dict[str, str]]:
        async for event in selected.backend.stream(
            messages,
            options=dict(options),
            think=think,
            model=selected_model,
            read_timeout=(
                LLM_STREAM_READ_TIMEOUT
                if LLM_STREAM_READ_TIMEOUT > 0
                else None
            ),
        ):
            if not isinstance(event, dict):
                # Defensive compatibility with a custom/older backend.
                text = str(event or "")
                if text:
                    yield {"content": text}
                continue

            content = str(event.get("content") or "")
            reasoning = str(event.get("reasoning") or "")
            if content or reasoning:
                yield {
                    "content": content,
                    "reasoning": reasoning,
                }

    effective_max_seconds = LLM_STREAM_MAX_SECONDS if max_seconds is None else float(max_seconds)
    if effective_max_seconds > 0:
        try:
            async with asyncio.timeout(effective_max_seconds):
                async for event in consume():
                    yield event
        except TimeoutError as exc:
            raise RuntimeError(
                f"LLM-Stream nach {effective_max_seconds:g}s abgebrochen"
            ) from exc
    else:
        async for event in consume():
            yield event


def _markdown_escape(text: str) -> str:
    """Escape the few characters that can break a Markdown link label."""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _source_path(result: SearchResult) -> str:
    raw = result.raw
    return str(raw.get("path") or result.title or "").strip()


def _source_filename(result: SearchResult) -> str:
    raw = result.raw
    filename = str(raw.get("filename") or "").strip()
    if filename:
        return filename
    path = _source_path(result).rstrip("/")
    if "/" in path:
        return path.rsplit("/", 1)[-1]
    return path or result.title


def _source_directory(result: SearchResult) -> str:
    raw = result.raw
    directory = str(raw.get("directory") or "").strip()
    if directory:
        return "/" + directory.strip("/") if directory != "/" else "/"

    path = _source_path(result).strip()
    if "/" not in path:
        return "/"
    parent = path.rsplit("/", 1)[0]
    return "/" + parent.strip("/")


def _nextcloud_source_url(result: SearchResult) -> str:
    """Build a deterministic Nextcloud Files URL from search metadata."""
    raw = result.raw

    # Allow the middleware to provide an already constructed URL in the future.
    source_url = str(raw.get("source_url") or "").strip()
    if source_url:
        return source_url

    if not NEXTCLOUD_BASE_URL:
        return ""

    openfile_id = raw.get("nextcloud_openfile_id")
    if openfile_id in (None, ""):
        return ""

    params = urlencode({
        "dir": _source_directory(result),
        "openfile": str(openfile_id),
    })
    return f"{NEXTCLOUD_BASE_URL}/index.php/apps/files/?{params}"


def _display_date_value(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        year, month, day = match.groups()
        return f"{day}.{month}.{year}"
    return value


def _source_date(result: SearchResult) -> str:
    return _display_date_value(str(result.raw.get("source_date") or ""))


def _technical_document_date(result: SearchResult) -> str:
    return _display_date_value(str(result.raw.get("document_date") or ""))


def _source_marker(result: SearchResult) -> str:
    """Return the legacy hidden source marker.

    0.7 no longer emits these comments because /use:N can resolve the visible
    deterministic Nextcloud ``openfile=`` links.  The helper and parser remain
    for backwards compatibility with older chat turns.
    """
    document_id = str(result.raw.get("document_id") or "").strip()
    if not document_id:
        return ""
    safe_id = document_id.replace("--", "-").replace(">", "")
    return f"<!--rag-source:{result.index}:{safe_id}-->"


def _source_handoff_suffix(results: list[SearchResult]) -> str:
    """Legacy helper retained for tests/compatibility; new answers do not emit it."""
    markers = [_source_marker(result) for result in results]
    markers = [marker for marker in markers if marker]
    return ("\n" + "".join(markers)) if markers else ""


def _previous_assistant_text(messages: list[dict[str, Any]]) -> str:
    latest_user = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            latest_user = i
            break
    if latest_user is None:
        return ""
    for i in range(latest_user - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return _content_to_text(messages[i].get("content")).strip()
    return ""


def _document_id_from_url(url: str) -> str | None:
    try:
        values = parse_qs(urlparse(str(url)).query).get("openfile") or []
    except Exception:
        return None
    if not values:
        return None
    value = str(values[0]).strip()
    if value.isdigit():
        return f"files:{value}"
    return None


def _previous_source_map(messages: list[dict[str, Any]]) -> dict[int, str]:
    """Map the immediately preceding assistant source numbers to document IDs.

    New 0.7 answers use deterministic Nextcloud ``openfile=`` links. Legacy
    invisible provider markers are still parsed so older chat turns remain
    usable. Numbered /list output is supported as well.
    """
    text = _previous_assistant_text(messages)
    if not text:
        return {}

    result: dict[int, str] = {}
    for match in re.finditer(
        r"<!--\s*rag-source:(\d+):([^>]+?)\s*-->",
        text,
        flags=re.IGNORECASE,
    ):
        result[int(match.group(1))] = match.group(2).strip()

    # Parse visible source/list lines as a fallback.  Restrict the [n] syntax
    # to the Quellen block; numbered ``n.`` lines are used by /list.
    source_block = text
    source_match = re.search(r"\*\*(?:Interne\s+)?Quellen:\*\*\s*(.*?)(?=\n\*\*Öffentliche Quellen:\*\*|$)", text, flags=re.IGNORECASE | re.DOTALL)
    if source_match:
        source_block = source_match.group(1)

    for line in source_block.splitlines():
        number_match = re.match(r"\s*-\s*\[(\d+)\]\s+", line)
        if not number_match:
            number_match = re.match(r"\s*(\d+)\.\s+", line)
        if not number_match:
            continue
        number = int(number_match.group(1))
        if number in result:
            continue
        for url_match in re.finditer(r"\]\(([^)]+)\)", line):
            document_id = _document_id_from_url(url_match.group(1))
            if document_id:
                result[number] = document_id
                break

    return result


def _previous_supporting_document_ids(
    messages: list[dict[str, Any]],
    *,
    limit: int,
) -> list[str]:
    """Prefer cited documents from the immediately preceding answer.

    Hidden source handoff markers may include every document that reached the
    answer model. Explicit citation numbers are therefore preferred, then the
    remaining handoff documents fill the small continuity budget.
    """
    if limit <= 0:
        return []
    mapping = _previous_source_map(messages)
    if not mapping:
        return []

    text = _previous_assistant_text(messages)
    answer_text = re.split(
        r"\n\s*\n\*\*(?:Interne\s+)?Quellen:\*\*",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    ordered_numbers: list[int] = []
    seen_numbers: set[int] = set()
    for match in re.finditer(r"\[(\d+)\]", answer_text):
        number = int(match.group(1))
        if number in mapping and number not in seen_numbers:
            seen_numbers.add(number)
            ordered_numbers.append(number)
    for number in sorted(mapping):
        if number not in seen_numbers:
            ordered_numbers.append(number)

    result: list[str] = []
    for number in ordered_numbers:
        document_id = str(mapping.get(number) or "").strip()
        if document_id and document_id not in result:
            result.append(document_id)
        if len(result) >= limit:
            break
    return result


def _previous_web_source_map(messages: list[dict[str, Any]]) -> dict[int, str]:
    """Map W1/W2/... in the previous web answer to archived text paths.

    The archived snapshot is the durable hand-off for /use:Wn.  We deliberately
    do not refetch the public URL because it may already have changed.
    """
    text = _previous_assistant_text(messages)
    if not text:
        return {}

    result: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        source_match = re.match(r"\s*-\s*\[W(\d+)\]\s+", line, flags=re.IGNORECASE)
        if source_match:
            current = int(source_match.group(1))
            continue
        if current is None:
            continue
        archive_match = re.match(
            r"\s*Archiv:\s*`([^`]+)`\s*$", line, flags=re.IGNORECASE
        )
        if archive_match:
            path = archive_match.group(1).strip()
            if path:
                result[current] = path
            current = None
    return result


def _resolve_use_references(
    references: list[str],
    messages: list[dict[str, Any]],
) -> tuple[list[str], list[int], list[int]]:
    """Resolve internal [n] and archived web [Wn] references.

    W references are converted to an internal ``webarchive:<path>`` reference.
    The middleware then reads that snapshot directly through authenticated
    WebDAV, so /use:W1 works before the next Nextcloud/ES synchronization.
    """
    previous = _previous_source_map(messages)
    previous_web = _previous_web_source_map(messages)
    resolved: list[str] = []
    missing_numbers: list[int] = []
    requested_numbers: list[int] = []

    for reference in references:
        value = str(reference or "").strip()
        if value.casefold() == "all":
            for number in sorted(previous):
                document_id = previous.get(number)
                if document_id and document_id not in resolved:
                    resolved.append(document_id)
            continue
        if re.fullmatch(r"\d+", value):
            number = int(value)
            requested_numbers.append(number)
            document_id = previous.get(number)
            if document_id:
                resolved.append(document_id)
            else:
                missing_numbers.append(number)
            continue

        web_match = re.fullmatch(r"[Ww](\d+)", value)
        if web_match:
            archive_path = previous_web.get(int(web_match.group(1)))
            if archive_path:
                resolved.append("webarchive:" + archive_path)
            else:
                # Fail closed.  The resolver will report this as unavailable;
                # never silently switch to a live refetch of the public URL.
                resolved.append(value)
            continue

        resolved.append(value)

    return resolved, missing_numbers, sorted(previous)



def _format_use_resolution_error(
    payload: dict[str, Any],
    *,
    missing_previous_numbers: list[int] | None = None,
    available_previous_numbers: list[int] | None = None,
) -> str:
    lines: list[str] = []

    missing_previous_numbers = missing_previous_numbers or []
    if missing_previous_numbers:
        numbers = ", ".join(str(value) for value in missing_previous_numbers)
        available = ", ".join(str(value) for value in (available_previous_numbers or []))
        lines.append(
            f"Die Quellen-Nummer(n) {numbers} kann ich in der unmittelbar vorherigen "
            "Antwort nicht auflösen."
        )
        if available:
            lines.append(f"Dort verfügbar sind: {available}.")
        else:
            lines.append("Die unmittelbar vorherige Antwort enthält keine auflösbare Quellenliste.")

    for reference in payload.get("not_found") or []:
        lines.append(f"Die Datei `{reference}` wurde nicht gefunden.")

    for entry in payload.get("ambiguous") or []:
        reference = str(entry.get("reference") or "").strip()
        matches = entry.get("matches") or []
        lines.append(
            f"`{reference}` ist nicht eindeutig. Bitte gib den vollständigen Pfad an."
        )
        for match in matches[:20]:
            path = str(match.get("path") or match.get("title") or "").strip()
            if path:
                lines.append(f"- `{path}`")

    return "\n".join(lines).strip()



_LIST_HIGHLIGHT_STOPWORDS = {
    "aber", "alle", "alles", "auch", "dann", "dass", "dem", "den", "der",
    "des", "die", "dies", "diese", "dieser", "dieses", "doch", "durch",
    "eine", "einem", "einen", "einer", "eines", "für", "geht", "haben",
    "hat", "hier", "ist", "kann", "mit", "nach", "nicht", "oder", "sich",
    "sind", "über", "und", "unter", "vom", "von", "war", "waren", "was",
    "welche", "welcher", "welches", "wenn", "wer", "werden", "wie", "wird",
    "wurde", "wurden", "zwischen", "zum", "zur",
}


def _list_highlight_terms(query: str) -> list[str]:
    """Distinctive query tokens used only for cosmetic /list highlighting."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\wÄÖÜäöüß-]{4,}", str(query or ""), flags=re.UNICODE):
        folded = token.casefold()
        if folded in _LIST_HIGHLIGHT_STOPWORDS or folded in seen:
            continue
        seen.add(folded)
        terms.append(token)
    return sorted(terms, key=len, reverse=True)


def _highlight_list_snippet(text: str, query: str) -> str:
    """Bold matching query anchors without changing retrieval semantics."""
    result = text
    for term in _list_highlight_terms(query):
        pattern = re.compile(rf"(?<!\w)({re.escape(term)})(?!\w)", flags=re.IGNORECASE)
        result = pattern.sub(r"**\1**", result)
    return result


def _compact_list_snippet(
    result: SearchResult,
    max_chars: int = 340,
    *,
    query: str = "",
) -> str:
    raw = result.raw
    candidates: list[str] = []
    if raw.get("graph_rank") is not None:
        candidates.append(str(raw.get("graph_snippet") or ""))
    candidates.extend([
        str(raw.get("es_snippet") or ""),
        str(raw.get("vector_snippet") or ""),
        str(raw.get("graph_snippet") or ""),
        str(result.text or ""),
    ])
    for candidate in candidates:
        candidate = re.sub(r"<[^>]+>", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if not candidate:
            continue
        if len(candidate) > max_chars:
            candidate = candidate[: max_chars - 1].rstrip() + "…"
        return _highlight_list_snippet(candidate, query)
    return ""


def _format_retrieval_list(
    results: list[SearchResult],
    active_arms: set[str] | None,
    *,
    raw_mode: bool,
    query: str = "",
) -> str:
    labels = {
        "files": "Elasticsearch",
        "vector": "Qdrant",
        "graph": "Neo4j",
    }
    selected = active_arms or {"files", "vector", "graph"}
    mode_label = " + ".join(labels[key] for key in ("files", "vector", "graph") if key in selected)

    if not results:
        return f"Keine Treffer ({mode_label})."

    if raw_mode:
        lines = [f"Treffer ({mode_label}; roh vor Reranker/Evidence/Antwortmodell):"]
    else:
        lines = [f"Treffer ({mode_label}; nach Reranking, ohne Evidence/Antwortmodell):"]

    for result in results:
        raw = result.raw
        filename = _source_filename(result)
        display = str(result.title or filename).strip() or filename
        url = _nextcloud_source_url(result)
        title = f"[{_markdown_escape(display)}]({url})" if url else display

        profile: list[str] = []
        if not raw_mode:
            score = raw.get("reranker_score")
            if score is not None:
                try:
                    profile.append(f"Reranker-Score {float(score):.4f}")
                except (TypeError, ValueError):
                    profile.append(f"Reranker-Score {score}")
            elif raw.get("rrf_rank") is not None:
                profile.append(f"RRF-Fallback #{raw['rrf_rank']}")

        if raw.get("es_rank") is not None:
            profile.append(f"Files #{raw['es_rank']}")
        if raw.get("vector_rank") is not None:
            profile.append(f"Vector #{raw['vector_rank']}")
        if raw.get("graph_rank") is not None:
            profile.append(f"Graph #{raw['graph_rank']}")
        if raw_mode and raw.get("rrf_rank") is not None and len(selected) > 1:
            profile.append(f"RRF #{raw['rrf_rank']}")

        suffix = " — " + ", ".join(profile) if profile else ""
        lines.append(f"{result.index}. {title}{suffix}")

        snippet = _compact_list_snippet(result, query=query)
        if snippet:
            lines.append(f"   > {snippet}")

    return "\n".join(lines)


def _explicit_selection_needs_documentwise_completeness(question: str) -> bool:
    """Whether a closed /use set must yield one output record per document."""
    text = re.sub(r"\s+", " ", str(question or "")).strip().casefold()
    if not text:
        return False
    patterns = (
        r"\btabell",
        r"\blist(?:e|en|et|ung)",
        r"\bauflist",
        r"\bsortier",
        r"\breihenfolge",
        r"\bchronolog",
        r"\bpro dokument\b",
        r"\bfür jedes dokument\b",
        r"\bfuer jedes dokument\b",
        r"\bje dokument\b",
        r"\balle dokument",
        r"\bsämtliche dokument",
        r"\bsaemtliche dokument",
    )
    return any(re.search(pattern, text) for pattern in patterns)


async def _repair_incomplete_explicit_selection(
    answer: str,
    messages: list[dict[str, str]],
    results: list[SearchResult],
    *,
    question: str,
    options: dict[str, Any] | None,
    model: str,
) -> str:
    """Retry once when a documentwise /use answer omits selected documents."""
    if not answer.strip() or not results:
        return answer
    cited = _cited_results(answer, results)
    if len(cited) >= len(results):
        return answer

    expected = ", ".join(f"[{result.index}]" for result in results)
    missing = [result.index for result in results if result not in cited]
    instruction = f"""Die vorige Antwort ist für den explizit ausgewählten Dokumentensatz unvollständig.
Es wurden genau {len(results)} Dokumente ausgewählt ({expected}), aber die Antwort deckt nur {len(cited)} davon ab.
Fehlende Quellenmarker: {', '.join(f'[{index}]' for index in missing)}.

Erstelle die Antwort vollständig neu. Für diese dokumentweise Aufgabe gilt zwingend:
- Verarbeite alle {len(results)} ausgewählten Dokumente genau einmal.
- Bei Tabelle/Liste/Sortierung: genau eine Zeile bzw. ein Eintrag pro Dokument.
- Wenn Datum, Betrag oder ein anderes verlangtes Feld nicht sicher erkennbar ist, schreibe "nicht eindeutig erkennbar" statt das Dokument wegzulassen.
- Zitiere jeden Dokumenteintrag mit seinem Quellenmarker [n].
- Füge keine anderen Dokumente hinzu und führe keine neue Suche durch."""
    try:
        repaired = await _ollama_complete(
            messages
            + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": instruction},
            ],
            options=options,
            think=False,
            model=model,
            role="answer",
        )
    except Exception as exc:
        log.warning(
            "Explicit /use completeness repair failed; keeping first answer: %s: %s",
            type(exc).__name__,
            exc,
        )
        return answer
    if not repaired:
        return answer
    repaired_cited = _cited_results(repaired, results)
    log.info(
        "Explicit /use completeness repair: selected=%d first_cited=%d repaired_cited=%d",
        len(results),
        len(cited),
        len(repaired_cited),
    )
    return repaired


async def _repair_missing_citations(
    answer: str,
    results: list[SearchResult],
    *,
    question: str,
) -> tuple[str, list[SearchResult]]:
    """Select supporting internal sources when an answer omitted all markers.

    The repair step never rewrites factual answer text. It only selects source
    numbers that directly support statements already present in the answer and
    appends a compact ``Belege: [n]`` marker line. This also works after a
    streamed answer, where inline citation insertion is no longer possible.
    """
    if not answer.strip() or not results or _cited_numbers(answer, results):
        return answer, _cited_results(answer, results)

    review_context, review_results = _build_review_context(results)
    if not review_context or not review_results:
        return answer, []

    valid_indexes = {result.index for result in review_results}
    prompt = (
        "Wähle ausschließlich Quellen, die konkrete Tatsachenaussagen der bereits "
        "formulierten Antwort direkt stützen. Schreibe die Antwort nicht um und "
        "ergänze keine Tatsachen. Wenn keine Quelle sicher passt, gib indexes=[] zurück.\n\n"
        f"BENUTZERFRAGE:\n{question}\n\n"
        f"ANTWORT:\n{answer}\n\n"
        f"QUELLENKANDIDATEN:\n{review_context}"
    )

    async def run_once(response_format: str | dict[str, Any] | None) -> dict[str, Any]:
        raw = await _ollama_complete(
            [
                {
                    "role": "system",
                    "content": guarded_evidence_prompt(
                        "Du bist ein Citation-Integrity-Controller. Wähle nur bereits "
                        "vorhandene interne Quellenindizes aus. Antworte ausschließlich als JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=350,
            think=False,
            model=EVIDENCE_MODEL,
            role="evidence",
            response_format=response_format,
        )
        if not raw:
            raise ValueError("Citation repair returned empty content")
        return _extract_json_object(raw)

    try:
        try:
            value = await run_once(CITATION_REPAIR_RESPONSE_SCHEMA)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {400, 415, 422}:
                raise
            value = await run_once("json")
    except Exception as exc:
        log.warning("Citation repair failed; leaving answer unchanged: %s: %s", type(exc).__name__, exc)
        return answer, []

    selected_indexes: list[int] = []
    for raw_index in value.get("indexes") or []:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if index in valid_indexes and index not in selected_indexes:
            selected_indexes.append(index)

    selected = _select_results_by_indexes(review_results, selected_indexes)
    if not selected:
        return answer, []

    marker_line = "Belege: " + ", ".join(f"[{result.index}]" for result in selected)
    repaired = answer.rstrip() + "\n\n" + marker_line
    log.info("Citation repair selected internal sources: %s", selected_indexes)
    return repaired, selected


def _source_suffix(
    answer: str,
    results: list[SearchResult],
    *,
    query: str = "",
    heading: str = "Quellen",
    include_all_visible: bool = False,
) -> str:
    """Render only documents explicitly cited by the answer as ``[n]``.

    Retrieval candidates and answer-context documents are telemetry, not
    automatically answer evidence. If the answer model cites no document, the
    user-facing source list is empty.
    """
    cited_numbers = _cited_numbers(answer, results)

    lines: list[str] = []

    for result in results:
        number = result.index
        if not include_all_visible and number not in cited_numbers:
            continue

        filename = _source_filename(result)
        url = _nextcloud_source_url(result)
        source_date = _source_date(result)
        technical_date = _technical_document_date(result)

        label = _markdown_escape(filename)
        source = f"[{label}]({url})" if url else label

        if source_date:
            suffix = f" · Quelle {source_date}"
            if technical_date and technical_date != source_date:
                suffix += f" · Datei {technical_date}"
            lines.append(f"- [{number}] {source}{suffix}")
        elif technical_date:
            lines.append(f"- [{number}] {source} · Datei {technical_date}")
        else:
            lines.append(f"- [{number}] {source}")

        if SOURCE_SNIPPETS:
            snippet = _compact_list_snippet(
                result,
                max_chars=SOURCE_SNIPPET_CHARS,
                query=query,
            )
            if snippet:
                lines.append(f"  > {snippet}")

    if not lines:
        return ""
    safe_heading = str(heading or "Quellen").strip() or "Quellen"
    return f"\n\n**{safe_heading}:**\n" + "\n".join(lines)


def _filename_lookup_response(
    filename: str,
    results: list[SearchResult],
) -> str:
    """Deterministische Antwort für reine Dateinamen-Navigation."""

    filename = str(filename or "").strip()

    if not results:
        if filename:
            return (
                f"Die Datei `{filename}` wurde unter diesem exakten "
                "Dateinamen nicht gefunden."
            )
        return "Unter dem angegebenen Dateinamen wurde keine Datei gefunden."

    references = ", ".join(f"[{result.index}]" for result in results)

    if len(results) == 1:
        answer = f"Die Datei `{filename}` wurde gefunden: {references}."
    else:
        answer = (
            f"Ich habe {len(results)} Dateien mit dem Namen `{filename}` "
            f"gefunden: {references}."
        )

    return answer + _source_suffix(answer, results)


def _completion_response(content: str, completion_id: str) -> dict[str, Any]:
    _progress("complete", "Fertig.")
    response = {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _active_model_id(),
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
    }
    suggestions = _suggestions()
    if suggestions:
        # OpenAI-compatible extension. Clients that do not know it ignore it;
        # the bundled Nextcloud client renders it as action buttons.
        response["suggestions"] = suggestions
    return response


def _sse_chunk(
    completion_id: str,
    content: str = "",
    finish_reason: str | None = None,
    *,
    reasoning: str = "",
) -> str:
    delta: dict[str, Any] = {}
    if content:
        delta["content"] = content
    if reasoning:
        # OpenWebUI detects structured reasoning in streaming Chat Completions
        # via reasoning_content / reasoning / thinking deltas.
        delta["reasoning_content"] = reasoning
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": _active_model_id(),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"



def _static_response(content: str, completion_id: str, stream: bool) -> Any:
    if not stream:
        return _completion_response(content, completion_id)

    async def generator() -> AsyncIterator[str]:
        initial = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": _active_model_id(),
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        if content:
            yield _sse_chunk(completion_id, content)
        yield _sse_chunk(completion_id, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


_COMMAND_HELP = """SunaQ – Kurzreferenz

Natürliche Steueranweisungen können in genau EINEM führenden Klammerblock stehen, z. B.
(Nutze diese Dokumente und suche anschließend im Web): Fasse den Vorgang zusammen und recherchiere den aktuellen Stand.

Ein führendes / aktiviert immer ausschließlich den Directive-Modus; spätere Klammern sind normaler Text.

/new
  Neuer Gesprächskontext für die RAG-Auflösung.

Quellenbereiche (untereinander kombinierbar):
/documents
  Normale Nextcloud-Dokumente, ohne Mail-, Web- und Chatarchiv.
/mailarchive
  Nur konfigurierte Mailarchive.
/webarchive
  Nur bereits archivierte Webquellen.
/chatarchive
  Nur gespeicherte SunaQ-Recherchen.
/web
  Aktuelle öffentliche Web-Recherche. Allein = Web-only; zusammen mit internen Quellen = gemischte Recherche.

Retrieval-Technik (orthogonal zu den Quellenbereichen):
/files
  Elasticsearch/Nextcloud-Volltextarm.
/vector
  Qdrant/Vektorarm, falls verfügbar.
/graph
  Expliziter Graph-Retrievalarm, falls verfügbar.
/elastic
  Direkte Nextcloud-kompatible Elasticsearch-Volltextsuche.

/list
  Trefferliste nach Cross-Encoder-Reranking mit kurzer relevanter Passage.

/force
  Breite/unspezifische Retrieval-Felder nicht früh abbrechen.

/use:1,2
  Interne Quellen der unmittelbar vorherigen Antwort direkt verwenden.

/use:all
  Alle internen Quellen der unmittelbar vorherigen Antwort direkt verwenden.

/use:W1,W2
  Archivierte Webquellen der unmittelbar vorherigen /web-Antwort direkt verwenden.

/use:\"Datei.pdf\"
  Dokument per eindeutigem Dateinamen oder vollständigem Pfad direkt verwenden.

/help
  Diese Kurzreferenz.

Quellenbereiche und Retrieval-Technik können kombiniert werden, z. B.
/documents /mailarchive /vector Rechnung 2025
/documents /web Aktueller Stand des Vorgangs
/webarchive /web Entwicklung seit der letzten Recherche

Ohne Quellen-Directive werden nur normale Dokumente durchsucht; Mail-, Web- und Chatarchiv sind opt-in."""

_HELP_ALIASES = {
    "help", "help me", "help please",
    "hilfe", "hilfe bitte",
    "aide", "aide svp",
    "ayuda", "ayuda por favor",
    "aiuto", "aiuto per favore",
    "ajuda", "ajuda por favor",
    "pomoc",
    "hjælp", "hjelp", "hjälp",
}

def _is_help_alias(text: str) -> bool:
    value = re.sub(r"[.!?;:]+$", "", str(text or "").strip().casefold()).strip()
    value = re.sub(r"\s+", " ", value)
    return value in _HELP_ALIASES


def _verification_notice_candidate_count(
    preverification_count: int,
    payload: dict[str, Any] | None,
    *,
    exhaustive: bool,
) -> int:
    """Count candidates conservatively for completeness notices.

    Exhaustive searches can be response-limited to the verifier budget while
    Elasticsearch still reports a larger exact hit count.  In that case the
    larger value must drive the notice or the answer would falsely look
    complete.  Ordinary searches keep using the actually ranked pool.
    """
    count = max(0, int(preverification_count))
    if not exhaustive:
        return count
    try:
        es_total = int(
            ((payload or {}).get("statistics") or {}).get(
                "elasticsearch_total_hits"
            )
            or 0
        )
    except (TypeError, ValueError, AttributeError):
        es_total = 0
    return max(count, es_total)


def _verification_capacity_notice(
    candidate_count: int,
    verified_count: int,
    configured_window: int,
    *,
    exhaustive: bool,
) -> str:
    """Warn near the ranked-candidate ceiling without claiming hidden hits exist.

    This uses only the ACL-visible/ranked pool.  It deliberately does not inspect
    or expose raw backend totals, which may include documents the caller cannot
    access.  The notice is therefore a coverage caution, not a statement that
    additional matching documents are known to exist.
    """
    if exhaustive:
        return ""
    candidate_count = max(0, int(candidate_count))
    verified_count = max(0, int(verified_count))
    configured_window = max(1, int(configured_window))
    if candidate_count != verified_count:
        return ""
    # 95% keeps the notice focused on runs that are genuinely close to the
    # profile ceiling (e.g. Deep 48/50) rather than routine medium-sized runs.
    if candidate_count * 20 < configured_window * 19:
        return ""
    return (
        "*Hinweis: Alle aktuell gerankten Kandidaten wurden geprüft, das "
        "Kandidatenfenster dieses Profils ist jedoch nahezu ausgeschöpft. "
        "Weitere relevante Dokumente außerhalb dieses Fensters können daher "
        "nicht ausgeschlossen werden.*"
    )


def _verification_limit_notice(
    candidate_count: int,
    verified_count: int,
    *,
    exhaustive: bool,
    bounded_document_set: bool = False,
) -> str:
    if int(candidate_count) <= int(verified_count):
        return ""
    if exhaustive:
        return (
            "*Hinweis: Die Anfrage verlangt eine vollständige Treffermenge. "
            "Es wurden weitere Kandidaten gefunden, die wegen des konfigurierten "
            "Prüfmaximums nicht mehr inhaltlich geprüft wurden. Das Ergebnis "
            "kann daher unvollständig sein.*"
        )
    if bounded_document_set:
        return (
            "*Hinweis: Es wurden weitere Kandidaten innerhalb der angegebenen "
            "Kriterien gefunden, die wegen der Mengenbegrenzung nicht mehr "
            "inhaltlich geprüft wurden.*"
        )
    return (
        "*Hinweis: Es wurden weitere mögliche Dokumenttreffer gefunden, die im "
        "normalen Lauf wegen der Mengenbegrenzung nicht mehr inhaltlich geprüft "
        "wurden. Wenn Sie eine vollständigere Treffermenge erwarten, grenzen Sie "
        "die Suche bitte weiter ein.*"
    )


def _short_async_health_error(exc: Exception, url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.hostname or parsed.netloc or str(url or "")
    endpoint = f"{host}:{parsed.port}" if parsed.port else host
    folded = str(exc or "").casefold()
    if isinstance(exc, httpx.TimeoutException) or "timed out" in folded or "timeout" in folded:
        return f"Timeout ({endpoint})"
    if isinstance(exc, httpx.ConnectError):
        if "refused" in folded or "all connection attempts failed" in folded:
            return f"Connection refused ({endpoint})"
        return f"Connection failed ({endpoint})"
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) is not None:
        return f"HTTP {response.status_code} ({endpoint})"
    return f"{type(exc).__name__} ({endpoint})"


async def _probe_role_backend(role: str, timeout: float = 4.0) -> dict[str, Any]:
    selected = _role_backend(role)
    info = selected.info()
    started = time.perf_counter()
    try:
        headers = selected.backend.headers()
        verify = getattr(selected.backend, "_verify", True)
        async with httpx.AsyncClient(timeout=timeout, headers=headers, verify=verify) as client:
            if selected.backend_name in {"ollama", "native_ollama"}:
                response = await client.get(f"{selected.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
                names = {
                    str(item.get("name") or item.get("model") or "")
                    for item in (data.get("models") or []) if isinstance(item, dict)
                }
                base = selected.model.split(":", 1)[0]
                present = selected.model in names or any(name.split(":", 1)[0] == base for name in names if name)
                status = "ok" if present else "degraded"
                result = {**info, "status": status, "model": selected.model, "model_present": bool(present)}
                if not present:
                    result["error"] = f"Modell nicht gefunden: {selected.model}"
            else:
                response = await client.get(f"{selected.base_url}/models")
                response.raise_for_status()
                result = {**info, "status": "ok", "model": selected.model}
        result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        return result
    except Exception as exc:
        log.debug("LLM role health probe failed role=%s: %s: %s", role, type(exc).__name__, exc)
        return {
            **info,
            "status": "down",
            "model": selected.model,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": _short_async_health_error(exc, selected.base_url),
        }


async def _llm_health(timeout: float = 4.0) -> dict[str, Any]:
    """Probe all configured role backends, deduplicating is unnecessary at this scale."""
    roles = {}
    for role in ("planner", "verifier", "evidence", "answer"):
        roles[role] = await _probe_role_backend(role, timeout=timeout)
    statuses = {str(v.get("status")) for v in roles.values()}
    status = "down" if statuses == {"down"} else ("ok" if statuses == {"ok"} else "degraded")
    return {
        "status": status,
        "roles": roles,
        "remote_limits": {
            "legacy": {
                "max_chars_per_document": REMOTE_LLM_MAX_CHARS_PER_DOCUMENT,
                "max_total_chars": REMOTE_LLM_MAX_TOTAL_CHARS,
                "verifier_max_candidates": REMOTE_VERIFIER_MAX_CANDIDATES,
                "verifier_max_chars_per_document": REMOTE_VERIFIER_MAX_CHARS_PER_DOCUMENT,
                "answer_max_documents": REMOTE_ANSWER_MAX_DOCUMENTS,
            },
            "sunaq_profile_hard_caps": {
                "max_chars_per_document": SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT,
                "max_total_chars": SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS,
                "verifier_max_candidates": SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES,
                "verifier_max_chars_per_document": SUNAQ_REMOTE_HARD_VERIFIER_MAX_CHARS_PER_DOCUMENT,
                "answer_max_documents": SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS,
            },
        },
    }


def _document_search_unavailable_text(exc: httpx.HTTPError) -> str | None:
    """Translate middleware ES outages into a user-facing temporary failure."""
    response = getattr(exc, "response", None)
    if response is None or int(getattr(response, "status_code", 0) or 0) != 503:
        return None
    text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            text = str(payload.get("detail") or "")
    except Exception:
        try:
            text = str(response.text or "")
        except Exception:
            text = ""
    if "elasticsearch" not in text.casefold():
        return None
    return (
        "Die Dokumentensuche ist derzeit nicht verfügbar, weil Elasticsearch nicht erreichbar ist. "
        "Bitte versuchen Sie es später erneut."
    )


def _http_error_looks_like_model_backend(exc: httpx.HTTPError) -> bool:
    response = getattr(exc, "response", None)
    text = ""
    if response is not None:
        try:
            text = str(response.text or "")
        except Exception:
            text = ""
    haystack = (str(exc) + " " + text).casefold()
    markers = (
        "connection refused", "connecterror", "connect error", "ollama",
        "embedding", "llm", "api/chat", "11434",
    )
    return any(marker in haystack for marker in markers)


def _llm_unavailable_text(exc: Exception | str, *, found: int = 0) -> str:
    if exc:
        log.debug("LLM unavailable detail: %s", exc)
    base = (
        "LLM nicht verfügbar. "
        f"Backend: {_role_backend('answer').backend_name}; Modell: {_role_backend('answer').model}; URL: {_role_backend('answer').base_url}."
    )
    if found:
        base += f" Die Dokumentensuche selbst war erfolgreich ({found} sichtbare Treffer)."
    return base


async def _middleware_health() -> dict[str, Any]:
    async with _middleware_client(timeout=min(float(HTTP_TIMEOUT), 15.0)) as client:
        response = await client.get(f"{RAG_MIDDLEWARE_URL}/health")
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {"status": "down", "error": "Ungültige Health-Antwort"}


async def _ensure_nextcloud_binding(user_id: str | None) -> dict[str, Any]:
    """Ask the middleware whether this external identity is bound to Nextcloud.

    Credentials and Login Flow state stay entirely inside the middleware.  The
    provider only forwards the stable identity supplied by the frontend.
    """
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RAG-User-ID"] = str(user_id)
    async with _middleware_client(timeout=min(float(HTTP_TIMEOUT), 20.0)) as client:
        response = await client.post(
            f"{RAG_MIDDLEWARE_URL}/auth/nextcloud/ensure",
            json={},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {"status": "error"}


def _format_health(payload: dict[str, Any], llm: dict[str, Any] | None = None) -> str:
    def state(item: dict[str, Any] | None) -> str:
        return str((item or {}).get("status") or "unknown").upper()

    arms = payload.get("retrieval_arms") or {}
    files = arms.get("files") or {}
    vector = arms.get("vector") or {}
    graph = arms.get("graph") or {}
    worker = payload.get("graph_worker") or {}
    queue = payload.get("graph_queue") or {}
    web = payload.get("web_evidence") or {}
    llm = llm or {}
    overall = str(payload.get("status") or "unknown").lower()
    if str(llm.get("status") or "unknown").lower() not in {"ok", "unknown"} and overall == "ok":
        overall = "degraded"

    web_label = str(web.get("provider") or "Web Search")
    lines = [
        f"SunaQ – Health: {overall.upper()}",
        "",
        f"Provider            OK",
        f"LLM roles           {state(llm)}",
        f"Elasticsearch       {state(files)}" + (f"   {files.get('latency_ms')} ms" if files.get("latency_ms") is not None else ""),
        f"Qdrant/Embedding    {state(vector)}",
        f"Neo4j               {state(graph)}" + (f"   {graph.get('latency_ms')} ms" if graph.get("latency_ms") is not None else ""),
        f"Graph Worker        {state(worker)}" + (f"   state={worker.get('state')}" if worker.get("state") else ""),
        f"Web Search/{web_label:<9} {state(web) if web.get('enabled') else 'DISABLED'}" + (f"   {web.get('latency_ms')} ms" if web.get("latency_ms") is not None else ""),
        f"Web Archive         {'ENABLED' if web.get('archive_enabled') else 'DISABLED'}",
        "",
        "LLM role routing",
    ]
    for role in ("planner", "verifier", "evidence", "answer"):
        info = (llm.get("roles") or {}).get(role) or {}
        lines.append(
            f"  {role:<16} {state(info):<9} {str(info.get('backend') or '—'):<8} "
            f"{str(info.get('scope') or '—'):<6} {str(info.get('model') or '—')}"
        )
    lines.extend([
        "",
        "Graph Queue",
        f"  pending           {int(queue.get('pending') or 0)}",
        f"  running           {int(queue.get('running') or 0)}",
        f"  errors            {int(queue.get('errors') or 0)}",
        f"  skipped oversize  {int(queue.get('skipped_oversize') or 0)}",
    ])
    if worker.get("heartbeat_age_seconds") is not None:
        lines.append(f"  worker heartbeat  {worker.get('heartbeat_age_seconds')} s ago")
    if files.get("error"):
        lines.append(f"\nElasticsearch: {files.get('error')}")
    qdrant = vector.get("qdrant") or {}
    embedding = vector.get("embedding") or {}
    if qdrant.get("error"):
        lines.append(f"\nQdrant: {qdrant.get('error')}")
    if embedding.get("error"):
        lines.append(f"\nEmbedding: {embedding.get('error')}")
    if graph.get("error"):
        lines.append(f"\nNeo4j: {graph.get('error')}")
    if llm.get("error"):
        lines.append(f"\nLLM: {llm.get('error')}")
    if web.get("error"):
        lines.append(f"\nWeb Search: {web.get('error')}")
    return "\n".join(lines)


@app.get("/live")
async def live() -> dict[str, Any]:
    return {"status": "ok", "version": VERSION, "model": MODEL_ID}


@app.get("/health")
async def health() -> dict[str, Any]:
    llm_status = await _llm_health(timeout=min(float(HTTP_TIMEOUT), 4.0))
    try:
        middleware = await _middleware_health()
    except Exception as exc:
        middleware = {"status": "down", "error": f"{type(exc).__name__}: {exc}"}
    overall = "ok"
    if middleware.get("status") == "down":
        overall = "down"
    elif middleware.get("status") != "ok" or llm_status.get("status") != "ok":
        overall = "degraded"
    return {
        "status": overall,
        "version": VERSION,
        "model": _active_model_id(),
        "architecture_tier": architecture_tier(PROVIDER_CONFIG),
        "rag_middleware": RAG_MIDDLEWARE_URL,
        "middleware": middleware,
        "llm": llm_status,
        "models": {
            "default_auxiliary": LLM_MODEL,
            "planner": _role_backend("planner").model,
            "verifier": _role_backend("verifier").model,
            "answer": _role_backend("answer").model,
            "evidence": _role_backend("evidence").model,
            "legacy_followup_default": FOLLOWUP_MODEL,
            "legacy_natural_instruction_default": NATURAL_INSTRUCTION_MODEL,
        },
        "llm_roles": {role: selected.info() for role, selected in llm_role_backends.items() if role != "default"},
        "stream_reasoning": STREAM_REASONING,
        "reasoning_stream_format": REASONING_STREAM_FORMAT,
        "answer_thinking": ANSWER_THINKING,
        "answer_min_num_predict_thinking": ANSWER_MIN_NUM_PREDICT_THINKING,
        "aux_max_num_predict": AUX_MAX_NUM_PREDICT,
        "query_rewrite_mode": QUERY_REWRITE_MODE,
        "natural_instruction_mode": NATURAL_INSTRUCTION_MODE,
        "nextcloud_base_url": NEXTCLOUD_BASE_URL,
        "prompt_dir": str(PROMPT_DIR),
        "evidence_decision_mode": _evidence_decision_mode(),
        "max_retrieval_rounds": _retrieval_planner().max_retrieval_rounds,
        "retrieval_planner": {
            "enabled": _retrieval_planner().enabled,
            "thinking": _retrieval_planner().thinking,
            "max_retrieval_rounds": _retrieval_planner().max_retrieval_rounds,
            "max_queries_per_round": _retrieval_planner().max_queries_per_round,
            "model": _retrieval_planner().model or LLM_MODEL,
            "max_tokens": _retrieval_planner().max_tokens,
            "context_max_chars": _retrieval_planner().context_max_chars,
            "max_complete_documents": _retrieval_planner().max_complete_documents,
            "overflow_acl_scan_limit": _retrieval_planner().overflow_acl_scan_limit,
        },
        "research_log_enabled": RESEARCH_LOG_ENABLED,
        "research_log_db": str(RESEARCH_LOG_DB),
        "use_per_result_max_chars": USE_PER_RESULT_MAX_CHARS,
        "use_context_max_chars": USE_CONTEXT_MAX_CHARS,
    }


@app.get("/v1/status/{request_id}")
async def request_status(
    request_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    client_id = _check_auth(authorization)
    external_user_id = (
        request.headers.get("x-rag-user-id")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
    )
    user_id = None
    if external_user_id:
        try:
            user_id = scope_identity(client_id, str(external_user_id))
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid RAG user identity") from exc
    owner = f"{client_id}|{user_id or ''}"
    state = _read_progress(request_id, owner)
    if state is None:
        # Deliberately indistinguishable from an unknown/expired request.
        raise HTTPException(status_code=404, detail="Request status not found")
    return state


@app.get("/v1/models")
async def list_models(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    client_id = _check_auth(authorization)
    external_user_id = (
        request.headers.get("x-rag-user-id")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
    )
    user_id = None
    if external_user_id:
        try:
            user_id = scope_identity(client_id, str(external_user_id))
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid RAG user identity") from exc
    try:
        allowed, default_id = _model_access_for_identity(user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    data: list[dict[str, Any]] = []
    for model in SUNAQ_MODEL_REGISTRY.list():
        if model.model_id not in allowed:
            continue
        item = model.public_info()
        item["default"] = model.model_id == default_id
        data.append(item)
    capabilities = _source_capabilities_for_identity(user_id)
    return {
        "object": "list",
        "data": data,
        "architecture_tier": architecture_tier(PROVIDER_CONFIG),
        "capabilities": {
            "source_scopes": [
                name for name, enabled in capabilities.items() if enabled
            ],
        },
    }


@app.get("/v1/user-settings")
async def user_settings(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    client_id = _check_auth(authorization)
    external_user_id = str(
        request.headers.get("x-rag-user-id")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
        or ""
    ).strip()
    if not external_user_id:
        raise HTTPException(status_code=403, detail="RAG user identity missing")
    try:
        scoped_user_id = scope_identity(client_id, external_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid RAG user identity") from exc

    target_path, chat_enabled = _chat_archive_state(scoped_user_id)
    return {
        "chat_archive_path": target_path,
        "chat_archive_enabled": chat_enabled,
        "source_capabilities": _source_capabilities_for_identity(scoped_user_id),
    }


@app.post("/v1/archive/chat/register")
async def register_chat_archive(
    body: ChatArchiveRegisterRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    if is_src(PROVIDER_CONFIG):
        raise HTTPException(
            status_code=404,
            detail="Chat archive registration is not available in Secure RAG Core (SRC)",
        )
    client_id = _check_auth(authorization)
    external_user_id = str(request.headers.get("x-rag-user-id") or "").strip()
    if not external_user_id:
        raise HTTPException(status_code=403, detail="RAG user identity missing")
    try:
        user_id = scope_identity(client_id, external_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid RAG user identity") from exc
    if not _chat_archive_state(user_id)[1]:
        return {
            "ok": False,
            "enabled": False,
            "reason": "chat_archive_disabled",
        }
    document_id = str(body.document_id or "").strip()
    path = str(body.path or "").strip()
    if not document_id or not path:
        raise HTTPException(status_code=400, detail="document_id and path are required")

    try:
        async with _middleware_client(timeout=min(float(HTTP_TIMEOUT), 30.0)) as client:
            response = await client.post(
                f"{RAG_MIDDLEWARE_URL}/source-origin/register-chat",
                json={"document_id": document_id, "path": path},
                headers={"X-RAG-User-ID": user_id},
            )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": True}
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            value = exc.response.json()
            detail = str(value.get("detail") or "") if isinstance(value, dict) else ""
        except Exception:
            detail = ""
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=detail or "chat archive registration failed",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"chat archive registration failed: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    client_id = _check_auth(authorization)

    raw_question = _latest_user_message(body.messages)
    if not raw_question:
        raise HTTPException(status_code=400, detail="No user message found")

    completion_id = "chatcmpl-" + uuid.uuid4().hex
    # Prefer the explicit RAG headers, but accept common OpenWebUI identity
    # headers and the OpenAI-compatible ``user`` request field as fallbacks.
    # The middleware never trusts these values for authorization by itself;
    # they are only a lookup key for server-side Nextcloud app credentials.
    external_user_id = (
        request.headers.get("x-rag-user-id")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
        or body.user
    )
    user_id = None
    if external_user_id:
        try:
            user_id = scope_identity(client_id, str(external_user_id))
        except ValueError:
            user_id = None

    try:
        runtime_model = _select_runtime_model(body.model, user_id=user_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _ACTIVE_RUNTIME_MODEL.set(runtime_model)
    _set_suggestions([])

    raw_progress_id = str(request.headers.get("x-rag-request-id") or "").strip()
    progress_id = (
        raw_progress_id
        if _PROGRESS_ID_RE.fullmatch(raw_progress_id)
        else completion_id
    )
    progress_owner = f"{client_id}|{user_id or ''}"
    _ACTIVE_PROGRESS_ID.set(progress_id)
    _ACTIVE_PROGRESS_OWNER.set(progress_owner)
    _progress("received", "Anfrage wird vorbereitet …")

    log.info(
        "SunaQ model: requested=%r selected=%s",
        body.model,
        runtime_model.model.model_id,
    )
    log.info(
        "identity: client=%r external_user=%r scoped=%r source_ip=%r",
        client_id, external_user_id, user_id,
        request.client.host if request.client else None,
    )
    user_groups = (
        request.headers.get("x-rag-user-groups")
        or request.headers.get("x-openwebui-user-groups")
        or request.headers.get("x-open-webui-user-groups")
    )
    auxiliary_kind = _auxiliary_task_kind(request, raw_question)

    # Beta security boundary: OpenWebUI follow-up suggestions are a separate
    # helper request containing client-supplied chat history. They must not
    # become a secondary disclosure channel after live ACL removed all document
    # evidence. Return the format OpenWebUI expects without invoking any LLM.
    if auxiliary_kind == "ui:follow_ups":
        log.info("Request %s: suppressed OpenWebUI follow-up helper", completion_id)
        return _static_response('{"follow_ups":[]}', completion_id, body.stream)

    auxiliary = auxiliary_kind is not None
    client_web_capability = (not auxiliary) and _request_allows_web(body, request)
    # Policy controls automatic public-web fallback. Explicit /web remains a
    # user action unless web is administratively disabled.
    web_capability = client_web_capability and RETRIEVAL_POLICY.web == "planner"

    natural_workflow: NaturalInstructionWorkflow | None = None
    direct_kind: str | None = None
    retrieval_limit_notice = ""
    planner_exhaustive = False

    if auxiliary:
        question = raw_question
        retrieval_arms: set[str] | None = None
        source_scopes: set[str] | None = None
        source_scopes_explicit = False
        web_requested = False
        list_mode: str | None = None
        context_reset = False
        force_unspecific = False
        web_only = False
        elastic_mode = False
        use_references: list[str] = []
        special_command: str | None = None
        retrieval_directives: list[str] = []
    else:
        if _is_help_alias(raw_question):
            return _static_response(_COMMAND_HELP, completion_id, body.stream)

        leading = str(raw_question or "").lstrip()
        natural_parts = (
            None
            if leading.startswith("/") or NATURAL_INSTRUCTION_MODE == "off"
            else _split_leading_natural_instruction(raw_question)
        )

        if natural_parts is not None:
            instruction_text, natural_query = natural_parts
            natural_workflow = await _compile_natural_instruction(
                body.messages, instruction_text, natural_query
            )
            question = natural_query
            special_command = None
            if natural_workflow is None:
                # Fail closed: do not guess control actions. The parenthesized
                # instruction is dropped and the task runs as a normal query.
                retrieval_arms = None
                source_scopes = None
                source_scopes_explicit = False
                web_requested = False
                list_mode = None
                context_reset = False
                force_unspecific = False
                web_only = False
                elastic_mode = False
                use_references = []
                retrieval_directives = ["natural:fallback"]
            else:
                retrieval_arms = natural_workflow.retrieval_arms
                source_scopes = None
                source_scopes_explicit = bool(natural_workflow.web_requested)
                web_requested = bool(natural_workflow.web_requested)
                list_mode = natural_workflow.list_mode
                context_reset = natural_workflow.context_reset
                force_unspecific = natural_workflow.force_unspecific
                web_only = natural_workflow.web_only
                elastic_mode = natural_workflow.elastic_mode
                use_references = natural_workflow.use_references
                retrieval_directives = ["natural"]
        else:
            parsed_directives = _parse_retrieval_directives(raw_question)
            if parsed_directives.error:
                return _static_response(parsed_directives.error, completion_id, body.stream)

            question = parsed_directives.query
            retrieval_arms = parsed_directives.retrieval_arms
            source_scopes = parsed_directives.source_scopes
            source_scopes_explicit = parsed_directives.source_scopes_explicit
            web_requested = parsed_directives.web_requested
            list_mode = parsed_directives.list_mode
            context_reset = parsed_directives.context_reset
            force_unspecific = parsed_directives.force_unspecific
            web_only = parsed_directives.web_only
            elastic_mode = parsed_directives.elastic_mode
            use_references = parsed_directives.use_references
            special_command = parsed_directives.special_command
            retrieval_directives = parsed_directives.seen

        if special_command == "help":
            return _static_response(_COMMAND_HELP, completion_id, body.stream)
        if special_command == "health":
            return _static_response(
                "Der Systemstatus ist nur für die Administration verfügbar.",
                completion_id,
                body.stream,
            )

        architecture_error = request_capability_error(
            PROVIDER_CONFIG,
            retrieval_arms=retrieval_arms,
            source_scopes=source_scopes,
            web_requested=web_requested,
        )
        if architecture_error:
            return _static_response(
                architecture_error,
                completion_id,
                body.stream,
            )

        if retrieval_arms is not None:
            requested_policy_arms = {str(value).strip().casefold() for value in retrieval_arms}
            blocked_policy_arms = requested_policy_arms & RETRIEVAL_POLICY.disabled_arms
            if blocked_policy_arms:
                return _static_response(
                    "Die angeforderte Suchquelle wurde durch die Retrieval-Policy der Administration deaktiviert: "
                    + ", ".join(sorted(blocked_policy_arms)) + ".",
                    completion_id,
                    body.stream,
                )

        if source_scopes_explicit and not web_requested:
            # Explicit source selection is authoritative. A UI choosing only
            # internal scopes must not silently fall back to the live web arm.
            web_capability = False

        if source_scopes is None:
            log.info(
                "Request %s source scopes: implicit default documents",
                completion_id,
            )
        else:
            log.info(
                "Request %s source scopes: explicit=%s",
                completion_id,
                ",".join(sorted(source_scopes)),
            )

        if retrieval_directives and not question:
            content = (
                "Bitte hinter den Befehlen eine Frage bzw. Arbeitsanweisung angeben. "
                "Beispiele: `/force Welche Beteiligungen hat die Nordstern GmbH?`, "
                "`/list Musterfall Hausverbot`, `/elastic +Novak +VEW`, `/web Beispiel Automation`, `/list:raw Musterfall Hausverbot` oder "
                "`/use:1,2 Fasse die beiden Dokumente zusammen.`"
            )
            return _static_response(content, completion_id, body.stream)

        # Multi-user just-in-time binding.  Auxiliary OpenWebUI helper requests
        # deliberately bypass RAG/ACL, but every normal task is checked before
        # retrieval so an unbound identity does not consume ES/reranker work.
        try:
            auth_state = await _ensure_nextcloud_binding(user_id)
        except Exception as exc:
            log.warning("Request %s auth preflight failed: %s: %s", completion_id, type(exc).__name__, exc)
            return _static_response(
                "Die Benutzerautorisierung ist derzeit nicht erreichbar. "
                "Bitte versuchen Sie es erneut oder wenden Sie sich an den Administrator.",
                completion_id,
                body.stream,
            )

        auth_status = str(auth_state.get("status") or "").strip().lower()
        if auth_status == "identity_missing":
            return _static_response(
                "Anmeldung erforderlich. Der Client hat keine verwertbare Benutzeridentität mitgesendet.",
                completion_id,
                body.stream,
            )
        if auth_status == "pending":
            login_url = str(auth_state.get("login_url") or "").strip()
            if login_url:
                content = (
                    "Nextcloud-Anmeldung erforderlich. "
                    f"[Jetzt bei Nextcloud anmelden]({login_url}). "
                    "Nach erfolgreicher Anmeldung senden Sie die Anfrage bitte erneut."
                )
            else:
                content = "Nextcloud-Anmeldung erforderlich. Bitte führen Sie den Login-Flow aus und senden Sie die Anfrage danach erneut."
            return _static_response(content, completion_id, body.stream)
        if auth_status not in {"connected", "not_required"}:
            return _static_response(
                "Die Benutzerautorisierung konnte nicht bestätigt werden.",
                completion_id,
                body.stream,
            )

        # Conservative direct-answer gate. Pure internal source-scope
        # directives may be injected by a frontend checkbox selection and must
        # not turn greetings/time/date/arithmetic into document searches.
        # Explicit workflow/retrieval controls (/web, /files, /vector, /graph,
        # /elastic, /list, /use, /force, /new, ...) still win.
        direct_blocking_directives = [
            value
            for value in retrieval_directives
            if value not in {"documents", "mailarchive", "webarchive", "chatarchive"}
        ]
        if (
            not direct_blocking_directives
            and natural_workflow is None
            and retrieval_arms is None
            and list_mode is None
            and not use_references
            and not web_only
            and not elastic_mode
            and not force_unspecific
        ):
            direct_kind = _direct_query_kind(question)
            if direct_kind:
                log.info(
                    "Request %s direct-answer gate matched kind=%s question=%r",
                    completion_id, direct_kind, question[:180],
                )

        # Source capabilities govern retrieval/evidence use, not trivial direct
        # replies. This also makes a stale frontend checkbox harmless while the
        # UI refreshes its authenticated capability list.
        if direct_kind is None:
            disabled_sources = _disabled_requested_sources(
                source_scopes,
                web_requested=web_requested,
                scoped_user_id=user_id,
            )
            if disabled_sources:
                labels = [
                    _SOURCE_CAPABILITY_LABELS.get(source, source)
                    for source in disabled_sources
                ]
                return _static_response(
                    "Die angeforderte Quelle ist für diesen Benutzer nicht freigeschaltet: "
                    + ", ".join(labels) + ".",
                    completion_id,
                    body.stream,
                )

    options, think, logged_parameters = _generation_parameters(body)

    # Purpose-specific generation policy.
    if auxiliary:
        think = False
        if AUX_MAX_NUM_PREDICT > 0:
            current = int(options.get("num_predict") or AUX_MAX_NUM_PREDICT)
            options["num_predict"] = min(current, AUX_MAX_NUM_PREDICT)
    elif direct_kind:
        # Trivial/direct requests should be cheap as well as retrieval-free.
        think = False
        if AUX_MAX_NUM_PREDICT > 0:
            current = int(options.get("num_predict") or AUX_MAX_NUM_PREDICT)
            options["num_predict"] = min(current, AUX_MAX_NUM_PREDICT)
    else:
        # The retrieval/evidence stages have already made the decision.
        # The answer layer should normally formulate, not reopen the investigation.
        if not ANSWER_THINKING:
            think = False

        # Retain the v0.4.7a budget protection when answer-thinking is explicitly
        # enabled for diagnostics.
        if think is not False and ANSWER_MIN_NUM_PREDICT_THINKING > 0:
            current = int(options.get("num_predict") or DEFAULT_NUM_PREDICT or 0)
            options["num_predict"] = max(
                current,
                ANSWER_MIN_NUM_PREDICT_THINKING,
            )
            options["num_predict"] = min(
                options["num_predict"],
                MAX_NUM_PREDICT,
            )

    logged_parameters = dict(options)
    if think is not None:
        logged_parameters["think"] = think

    generation_model = LLM_MODEL if auxiliary else ANSWER_MODEL

    log.info(
        "Request %s: stream=%s auxiliary=%s auxiliary_kind=%s "
        "client_model=%s generation_model=%s think=%r directives=%s question=%r",
        completion_id,
        body.stream,
        auxiliary,
        auxiliary_kind or "-",
        body.model,
        generation_model,
        think,
        retrieval_directives if not auxiliary else [],
        question[:180],
    )

    if not auxiliary:
        _, context_boundary = _context_boundary(body.messages)
        context_items = _prior_conversation(body.messages)
        log.info(
            "Conversation context: boundary=%s prior_messages_used=%d reset=%s",
            context_boundary,
            len(context_items),
            context_reset,
        )

    retrieval_query = question
    followup_uses_history = False
    explicit_filename = extract_complete_filename(question)
    if explicit_filename:
        # A complete filename is already a deterministic document selector.
        # Do not let history-aware follow-up rewriting paraphrase or drop it.
        log.info("Exact filename anchor preserved before retrieval: %s", explicit_filename)

    if direct_kind:
        retrieval_query = question
    elif natural_workflow is not None:
        # Natural instructions compile only the leading control block. The task
        # itself remains byte/quote-semantically authoritative and is not rewritten
        # by the instruction compiler. A normal query rewriter may still run for
        # ordinary internal search, exactly as without a leading instruction.
        if (
            not auxiliary
            and not context_reset
            and list_mode is None
            and not use_references
            and not elastic_mode
            and not explicit_filename
        ):
            retrieval_query, followup_uses_history = await _rewrite_query_with_context(
                body.messages, question
            )
        else:
            retrieval_query = question
    elif (
        not auxiliary
        and not context_reset
        and list_mode is None
        and not use_references
        and not elastic_mode
        and not explicit_filename
    ):
        retrieval_query, followup_uses_history = await _rewrite_query_with_context(
            body.messages, question
        )

    policy_default_arms = RETRIEVAL_POLICY.fallback_arms(CONFIGURED_INTERNAL_ARMS)
    files_arm_available = (
        (retrieval_arms is None and "files" in policy_default_arms)
        or (retrieval_arms is not None and "files" in {str(value).strip().lower() for value in retrieval_arms})
    )
    implicit_filename_use = bool(
        explicit_filename
        and files_arm_available
        and not use_references
        and not elastic_mode
        and list_mode is None
        and not _is_pure_complete_filename_query(question, explicit_filename)
    )
    if implicit_filename_use:
        log.info(
            "Exact filename analysis uses direct document path: %s",
            explicit_filename,
        )

    # Generic UI capability is fallback-only. Internal evidence is always tried
    # first; only an insufficient/empty/unspecific internal result may trigger
    # our own web gate. Explicit /web bypasses this fallback policy.
    auto_web_decision = {"use_web": False, "reason": "not_evaluated"}
    auto_web = False
    web_fallback_pending = False
    explicit_natural_web = bool(
        natural_workflow is not None
        and natural_workflow.web_requested
        and not natural_workflow.web_only
    )
    explicit_source_web = bool(web_requested and not web_only)
    explicit_mixed_web = bool(explicit_natural_web or explicit_source_web)
    web_search_query = (
        natural_workflow.web_query
        if natural_workflow is not None and natural_workflow.web_requested
        else retrieval_query
    ) or retrieval_query
    web_search_queries = [web_search_query] if web_search_query else []

    if (web_only or explicit_mixed_web) and RETRIEVAL_POLICY.web == "disabled":
        return _static_response(
            "Die Web-Recherche wurde durch die Retrieval-Policy der Administration deaktiviert.",
            completion_id,
            body.stream,
        )

    _research_call(
        "start_query",
        query_id=completion_id,
        conversation_id=_conversation_id(request, body),
        user_id=user_id,
        user_groups=user_groups,
        user_question=question,
        initial_retrieval_query=retrieval_query,
        model=generation_model,
        model_parameters=logged_parameters,
        auxiliary=auxiliary,
    )

    if (not auxiliary) and web_only:
        try:
            web_payload = await _web_search(web_search_query, user_id, completion_id)
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = str(exc.response.json().get("detail") or "")
            except Exception:
                detail = exc.response.text[:500]
            if _http_error_looks_like_model_backend(exc):
                content = _llm_unavailable_text(detail or exc)
            else:
                content = f"Web-Recherche fehlgeschlagen (HTTP {exc.response.status_code})."
                if detail:
                    content += " " + detail
            _research_call(
                "finish_query", query_id=completion_id, status="web_error",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)
        except Exception as exc:
            content = f"Web-Recherche fehlgeschlagen: {type(exc).__name__}: {exc}"
            _research_call(
                "finish_query", query_id=completion_id, status="web_error",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)

        if not bool(web_payload.get("enabled", True)):
            content = "Die Web-Recherche ist derzeit nicht verfügbar. Wenden Sie sich bei Bedarf an die Administration."
            _research_call(
                "finish_query", query_id=completion_id, status="web_disabled",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)

        web_context, web_sources = _build_web_context(web_payload)
        if not web_sources:
            details = []
            if web_payload.get("searched") is not None:
                details.append(f"gesucht: {web_payload.get('searched')}")
            if web_payload.get("fetched") is not None:
                details.append(f"geladen: {web_payload.get('fetched')}")
            content = "Es wurde keine hinreichend relevante, tatsächlich abrufbare Webquelle gefunden."
            if details:
                content += " (" + ", ".join(details) + ")"
            _research_call(
                "finish_query", query_id=completion_id, status="web_insufficient",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)

        messages = _web_answer_messages(question, web_context)
        try:
            answer = await _ollama_complete(
                messages, options=options, think=False, model=ANSWER_MODEL, role="answer"
            )
        except httpx.HTTPError as exc:
            content = _llm_unavailable_text(exc, found=len(web_sources)) + _web_source_suffix(web_sources)
            log.warning("Request %s web answer LLM unavailable: %s", completion_id, exc)
            return _static_response(content, completion_id, body.stream)
        if not answer:
            answer = "Die Webquellen wurden ausgewählt, aber das Antwortmodell hat keinen Text geliefert."
        content = answer + _web_source_suffix(web_sources)
        archive_errors = list(web_payload.get("archive_errors") or [])
        archive_run_path = str(web_payload.get("archive_run_path") or "").strip()
        if archive_run_path:
            try:
                await _web_finalize_archive(
                    archive_run_path,
                    answer,
                    user_id,
                    answer_model=ANSWER_MODEL,
                    answer_parameters=dict(options or {}),
                    request_id=completion_id,
                )
            except Exception as exc:
                archive_errors.append({
                    "url": "recherche.md",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning("Request %s web archive finalization failed: %s", completion_id, exc)
        if archive_errors:
            content += f"\n\n*Archivierung: {len(archive_errors)} Webarchiv-Schritt(e) konnten nicht vollständig abgeschlossen werden; die Web-Evidence selbst wurde trotzdem ausgewertet.*"
        _research_call(
            "finish_query", query_id=completion_id, status="web_answered",
            final_retrieval_query=retrieval_query, answer_text=content,
        )
        log.info(
            "Request %s finished: web_answered searched=%s fetched=%s selected=%s",
            completion_id, web_payload.get("searched"), web_payload.get("fetched"), len(web_sources),
        )
        return _static_response(content, completion_id, body.stream)

    auto_web_payload: dict[str, Any] | None = None
    auto_web_sources: list[dict[str, Any]] = []
    auto_web_context = ""
    auto_web_archive_run_paths: list[str] = []
    auto_web_archive_errors: list[dict[str, Any]] = []
    internal_fallback_message = ""
    deferred_use_findings: dict[str, Any] | None = None

    if auxiliary:
        messages = _direct_messages(body.messages)
        results: list[SearchResult] = []
        final_round_id: int | None = None
    elif direct_kind:
        messages = _direct_answer_messages(question, direct_kind)
        results = []
        final_round_id = None
        log.info(
            "Request %s answering directly without ES/Qdrant/Neo4j retrieval",
            completion_id,
        )
    else:
        if LOG_RETRIEVAL_QUERY:
            if retrieval_query == question:
                log.info("RAG query: %s", retrieval_query)
            else:
                log.info("RAG query: %s | rewritten from: %s", retrieval_query, question)

        results = []
        messages: list[dict[str, str]] = []
        final_round_id = None
        entity_recall_backoff = False

        if use_references or implicit_filename_use:
            if implicit_filename_use:
                resolved_references = [str(explicit_filename)]
                missing_numbers: list[int] = []
                available_numbers: list[int] = []
            else:
                resolved_references, missing_numbers, available_numbers = _resolve_use_references(
                    use_references,
                    body.messages,
                )
            if missing_numbers:
                content = _format_use_resolution_error(
                    {},
                    missing_previous_numbers=missing_numbers,
                    available_previous_numbers=available_numbers,
                )
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="use_reference_error",
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            try:
                use_payload, use_results = await _rag_resolve_documents(
                    question,
                    resolved_references,
                    user_id,
                    user_groups,
                    request_id=completion_id,
                )
            except httpx.HTTPError as exc:
                unavailable = _document_search_unavailable_text(exc)
                if unavailable:
                    _research_call(
                        "finish_query", query_id=completion_id, status="search_unavailable",
                        final_retrieval_query=retrieval_query, answer_text=unavailable,
                    )
                    return _static_response(unavailable, completion_id, body.stream)
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="rag_error",
                    final_retrieval_query=retrieval_query,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"RAG document resolver error: {exc}",
                ) from exc

            resolution_error = _format_use_resolution_error(use_payload)
            if resolution_error:
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="use_resolution_error",
                    final_retrieval_query=retrieval_query,
                    answer_text=resolution_error,
                )
                return _static_response(resolution_error, completion_id, body.stream)

            if not use_results:
                content = "Über /use konnten keine Dokumente aufgelöst werden."
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="use_no_results",
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            use_log_payload = dict(use_payload)
            use_log_payload["results"] = [
                {
                    "document_id": result.raw.get("document_id"),
                    "title": result.title,
                    "path": result.raw.get("path"),
                }
                for result in use_results
            ]
            final_round_id = _research_call(
                "start_round",
                query_id=completion_id,
                round_no=1,
                search_query="/use " + ", ".join(resolved_references),
                payload=use_log_payload,
            )
            context, results = _build_context(
                use_results,
                per_result_max_chars=USE_PER_RESULT_MAX_CHARS,
                context_max_chars=USE_CONTEXT_MAX_CHARS,
                preserve_all_results=True,
            )
            _research_call(
                "log_documents",
                round_id=final_round_id,
                stage="answer_context",
                documents=_raw_documents(results),
            )
            messages = _rag_answer_messages(
                question,
                question,
                context,
                document_analysis=True,
                selected_document_count=len(results),
            )
            _log_answer_context_stats(
                "implicit_filename" if implicit_filename_use else "explicit_use",
                context,
                results,
            )
            deferred_use_findings = {
                "query_id": completion_id,
                "question": question,
                "retrieval_query": retrieval_query,
                "results": list(results),
                "canonical_user_id": str(auth_state.get("canonical_user_id") or ""),
                "nextcloud_login": str(auth_state.get("nextcloud_login") or ""),
                "nextcloud_server": str(auth_state.get("server") or ""),
            }
            log.info(
                "Request %s %s: references=%s resolved_documents=%s",
                completion_id,
                "implicit filename use" if implicit_filename_use else "explicit /use",
                resolved_references,
                [str(result.raw.get("document_id") or result.title) for result in results],
            )

        if elastic_mode:
            elastic_limit = ELASTIC_LIST_LIMIT if list_mode is not None else ELASTIC_ANALYZE_LIMIT
            try:
                elastic_payload, elastic_results = await _elastic_search(
                    retrieval_query,
                    user_id,
                    user_groups,
                    request_id=completion_id,
                    limit=elastic_limit,
                    include_content=(list_mode is None),
                    source_scopes=source_scopes,
                )
            except httpx.HTTPError as exc:
                content = (
                    "Die direkte Nextcloud/Elasticsearch-Volltextsuche ist derzeit nicht erreichbar. "
                    "Der Dienst kann später erneut versucht werden."
                )
                _research_call(
                    "finish_query", query_id=completion_id, status="elastic_unavailable",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                log.warning("Request %s /elastic unavailable: %s", completion_id, exc)
                return _static_response(content, completion_id, body.stream)

            final_round_id = _research_call(
                "start_round",
                query_id=completion_id,
                round_no=1,
                search_query="/elastic " + retrieval_query,
                payload=elastic_payload,
            )
            _research_call(
                "log_documents",
                round_id=final_round_id,
                stage="elastic_raw",
                documents=_raw_documents(elastic_results),
            )

            count_complete = bool(elastic_payload.get("count_complete"))
            total_hits = elastic_payload.get("total_hits")
            visible_scanned = int(elastic_payload.get("visible_scanned") or len(elastic_results))

            if list_mode is not None:
                if count_complete:
                    header = f"Elasticsearch-Volltextsuche: {int(total_hits or 0)} Treffer."
                else:
                    header = (
                        "Elasticsearch-Volltextsuche: Die Anfrage überschreitet die Anzahl "
                        "zulässiger Treffer; die Ausgabe ist begrenzt."
                    )
                rendered = _format_retrieval_list(
                    elastic_results,
                    {"files"},
                    raw_mode=True,
                    query=retrieval_query,
                )
                content = header + "\n\n" + rendered
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="elastic_list",
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            if not count_complete:
                content = (
                    "Die Anfrage ist zu unspezifisch, um zielführend beantwortet werden zu können. "
                    "Die Volltextsuche überschreitet die Anzahl zulässiger Treffer. "
                    "Bitte grenzen Sie die Suche enger ein, z. B. nach Zeitraum, Person, Organisation, "
                    "Vorgang oder mit verpflichtenden Begriffen (+Begriff)."
                )
                _research_call(
                    "finish_query", query_id=completion_id, status="elastic_too_broad",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            total_hits = int(total_hits or 0)
            if total_hits > ELASTIC_ANALYZE_LIMIT:
                content = (
                    "Die Anfrage ist zu unspezifisch, um zielführend beantwortet werden zu können. "
                    "Die Volltextsuche überschreitet die Anzahl zulässiger Treffer. "
                    "Bitte grenzen Sie die Suche enger ein, z. B. nach Zeitraum, Person, Organisation, "
                    "Vorgang oder mit verpflichtenden Begriffen (+Begriff), Phrasen (\"...\") oder Ausschlüssen (-Begriff)."
                )
                _research_call(
                    "finish_query", query_id=completion_id, status="elastic_too_many",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            if total_hits == 0 or not elastic_results:
                content = "Die direkte Elasticsearch-Volltextsuche lieferte keine sichtbaren Treffer."
                _research_call(
                    "finish_query", query_id=completion_id, status="elastic_no_results",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            # The middleware has already hydrated the complete visible field
            # when it fit inside the analysis limit. No ranking/re-selection is
            # inserted here.
            hydrated = elastic_results
            if len(hydrated) != total_hits:
                content = (
                    f"Die Volltextsuche lieferte {total_hits} Treffer, aber nur {len(hydrated)} Dokumente "
                    "konnten vollständig für die Analyse bereitgestellt werden. Bitte wiederholen Sie die Suche "
                    "oder verwenden Sie /elastic /list."
                )
                _research_call(
                    "finish_query", query_id=completion_id, status="elastic_hydration_incomplete",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            per_doc_budget = max(
                2000,
                min(USE_PER_RESULT_MAX_CHARS, ELASTIC_MAX_EVIDENCE_CHARS // max(1, len(hydrated))),
            )
            context, results = _build_context(
                hydrated,
                per_result_max_chars=per_doc_budget,
                context_max_chars=ELASTIC_MAX_EVIDENCE_CHARS,
            )
            _research_call(
                "log_documents",
                round_id=final_round_id,
                stage="answer_context",
                documents=_raw_documents(results),
            )
            elastic_analysis_question = (
                "Analysiere die vollständige Treffermenge der direkten Volltextsuche. "
                "Fasse die für den Suchausdruck relevanten Informationen aus allen bereitgestellten "
                "Dokumenten zusammen; lasse kein Dokument allein wegen vermuteter geringerer Relevanz weg "
                "und belege Tatsachen mit den Quellen-Nummern. Suchausdruck: "
                + retrieval_query
            )
            messages = _rag_answer_messages(elastic_analysis_question, retrieval_query, context)
            log.info(
                "Request %s /elastic complete field: visible_hits=%d context_docs=%d",
                completion_id, total_hits, len(results),
            )

        # ------------------------------------------------------------
        # Simple retrieval contract
        #
        # One structured SearchSpec drives every normal retrieval round.
        # Neo4j remains available inside the API for entity/alias expansion,
        # but it is not an automatic document-retrieval arm.  Qdrant receives
        # only semantic_query; Elasticsearch receives only the lexical fields.
        # ------------------------------------------------------------
        explicit_arm_selection = retrieval_arms is not None
        if retrieval_arms is None:
            automatic_arms = RETRIEVAL_POLICY.fallback_arms(CONFIGURED_INTERNAL_ARMS)
            retrieval_arms = automatic_arms & {"files", "vector"}
            if not retrieval_arms:
                # Compatibility for an explicitly graph-only installation.
                retrieval_arms = automatic_arms
            log.info(
                "retrieval policy: automatic files/vector arms=%s (Neo4j remains query-expansion only)",
                sorted(retrieval_arms),
            )

        rewrite_enabled_task = bool(
            not use_references
            and not implicit_filename_use
            and not elastic_mode
            and bool(set(retrieval_arms or set()) & {"files", "vector"})
        )
        # Keep the historic variable name below so verifier/evidence code stays
        # small; it now means "structured rewrite path", not RC8 multi-probe.
        planner_enabled_task = rewrite_enabled_task
        planner_probes: list[dict[str, Any]] = []  # legacy retrieval-record field
        planner_query_frame: dict[str, Any] = normalize_query_frame({})
        planner_exhaustive = detect_exhaustive_intent(retrieval_query)
        planner_bounded_document_set = (
            detect_bounded_document_set(retrieval_query) and not planner_exhaustive
        )
        planner_previous_doc_ids: set[str] = set()
        active_search_spec: dict[str, Any] | None = None
        rewrite_model = _retrieval_planner().model or ANSWER_MODEL
        followup_evidence_results: list[SearchResult] = []
        followup_evidence_loaded = False

        query_seed_context: dict[str, Any] = {}
        if rewrite_enabled_task:
            query_seed_context = await _rag_query_context(
                retrieval_query,
                request_id=completion_id,
            )
            initial_rewrite = await _rewrite_search_spec(
                question=retrieval_query,
                round_no=1,
                results=[],
                previous_spec=None,
                retrieval_arms=set(retrieval_arms or set()) & {"files", "vector"},
                seed_context=query_seed_context,
            )
            if not bool(initial_rewrite.get("valid")):
                content = (
                    "Die Suchanfrage konnte nicht zuverlässig in eine interne "
                    "Dokumentensuche übersetzt werden. Bitte formulieren Sie die "
                    "Anfrage etwas konkreter."
                )
                _research_call(
                    "finish_query", query_id=completion_id, status="query_rewrite_failed",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            active_search_spec = dict(initial_rewrite.get("spec") or {})
            planner_query_frame = normalize_query_frame(
                query_frame_from_search_spec(active_search_spec, intent=retrieval_query)
            )
            log.info(
                "query rewrite round 1: elastic_present=%s semantic_present=%s entities=%d concepts=%d constraints=%d verification_requirements=%d seed_keys=%d",
                bool(str(active_search_spec.get("elastic_query") or "").strip()),
                bool(str(active_search_spec.get("semantic_query") or "").strip()),
                len(active_search_spec.get("entities") or []),
                len(active_search_spec.get("concepts") or []),
                len(active_search_spec.get("constraints") or []),
                len(active_search_spec.get("verification_requirements") or []),
                len(query_seed_context),
            )

        retrieval_rounds = (
            range(0)
            if (use_references or implicit_filename_use or elastic_mode)
            else range(1, _retrieval_planner().max_retrieval_rounds + 1)
        )

        for round_no in retrieval_rounds:
            try:
                if planner_enabled_task:
                    if planner_exhaustive:
                        retrieval_candidate_limit = _retrieval_planner().exhaustive_verification_candidate_limit
                    elif planner_bounded_document_set:
                        retrieval_candidate_limit = _retrieval_planner().bounded_verification_candidate_limit
                    else:
                        retrieval_candidate_limit = max(
                            SEARCH_LIMIT, _retrieval_planner().verification_candidate_limit
                        )
                    payload, ranked_results = await _rag_search(
                        retrieval_query,
                        user_id,
                        user_groups,
                        request_id=completion_id,
                        entity_recall=entity_recall_backoff,
                        retrieval_arms=retrieval_arms,
                        source_scopes=source_scopes,
                        raw_results=(list_mode == "raw"),
                        force_unspecific=(force_unspecific or list_mode == "ranked"),
                        search_spec=active_search_spec,
                        query_context=query_seed_context,
                        limit=retrieval_candidate_limit,
                    )
                else:
                    payload, ranked_results = await _rag_search(
                        retrieval_query,
                        user_id,
                        user_groups,
                        request_id=completion_id,
                        entity_recall=entity_recall_backoff,
                        retrieval_arms=retrieval_arms,
                        source_scopes=source_scopes,
                        raw_results=(list_mode == "raw"),
                        force_unspecific=(force_unspecific or list_mode == "ranked"),
                    )
            except httpx.HTTPError as exc:
                unavailable = _document_search_unavailable_text(exc)
                if unavailable:
                    _research_call(
                        "finish_query", query_id=completion_id, status="search_unavailable",
                        final_retrieval_query=retrieval_query, answer_text=unavailable,
                    )
                    log.warning("Request %s Elasticsearch unavailable during retrieval: %s", completion_id, exc)
                    return _static_response(unavailable, completion_id, body.stream)
                if _http_error_looks_like_model_backend(exc):
                    content = _llm_unavailable_text(exc)
                    _research_call(
                        "finish_query", query_id=completion_id, status="llm_unavailable",
                        final_retrieval_query=retrieval_query, answer_text=content,
                    )
                    log.warning("Request %s model/embedding backend unavailable during retrieval: %s", completion_id, exc)
                    return _static_response(content, completion_id, body.stream)
                _research_call(
                    "finish_query", query_id=completion_id, status="rag_error",
                    final_retrieval_query=retrieval_query,
                )
                raise HTTPException(status_code=502, detail=f"RAG middleware error: {exc}") from exc

            if followup_uses_history and not followup_evidence_loaded:
                followup_evidence_results = await _resolve_followup_evidence(
                    body.messages,
                    query=retrieval_query,
                    user_id=user_id,
                    user_groups=user_groups,
                    source_scopes=source_scopes,
                    request_id=completion_id,
                )
                followup_evidence_loaded = True
                if followup_evidence_results:
                    log.info(
                        "Follow-up evidence continuity: reauthorized=%d documents",
                        len(followup_evidence_results),
                    )
            if followup_evidence_results:
                ranked_results = _merge_followup_evidence(
                    followup_evidence_results,
                    ranked_results,
                )

            round_id = _research_call(
                "start_round",
                query_id=completion_id,
                round_no=round_no,
                search_query=retrieval_query,
                payload=payload,
            )
            final_round_id = round_id
            _research_call(
                "log_documents",
                round_id=round_id,
                stage="ranked",
                documents=_raw_documents(ranked_results),
            )

            retrieval_mode = str(payload.get("retrieval_mode") or "")
            lookup_filename = str(payload.get("lookup_filename") or "").strip()

            if planner_enabled_task:
                current_doc_ids = {
                    str(result.raw.get("document_id") or "").strip()
                    for result in ranked_results
                    if str(result.raw.get("document_id") or "").strip()
                }
                diminishing_returns = bool(
                    round_no > 1
                    and current_doc_ids
                    and not (current_doc_ids - planner_previous_doc_ids)
                )
                if diminishing_returns:
                    log.info(
                        "retrieval rounds stop: diminishing returns in round %d",
                        round_no,
                    )

                if (
                    _retrieval_planner().enabled
                    and round_no < _retrieval_planner().max_retrieval_rounds
                    and not diminishing_returns
                ):
                    next_rewrite = await _rewrite_search_spec(
                        question=retrieval_query,
                        round_no=round_no + 1,
                        results=ranked_results,
                        previous_spec=active_search_spec,
                        retrieval_arms=set(retrieval_arms or set()) & {"files", "vector"},
                        seed_context=query_seed_context,
                    )
                    next_spec = dict(next_rewrite.get("spec") or {})
                    changed = (
                        bool(next_rewrite.get("valid"))
                        and search_spec_fingerprint(next_spec)
                        != search_spec_fingerprint(active_search_spec)
                    )
                    if not bool(next_rewrite.get("stop")) and changed:
                        active_search_spec = next_spec
                        planner_query_frame = normalize_query_frame(
                            query_frame_from_search_spec(active_search_spec, intent=retrieval_query)
                        )
                        planner_previous_doc_ids = current_doc_ids
                        log.info(
                            "retrieval round %d rewrite: elastic_present=%s semantic_present=%s entities=%d concepts=%d constraints=%d",
                            round_no + 1,
                            bool(str(active_search_spec.get("elastic_query") or "").strip()),
                            bool(str(active_search_spec.get("semantic_query") or "").strip()),
                            len(active_search_spec.get("entities") or []),
                            len(active_search_spec.get("concepts") or []),
                            len(active_search_spec.get("constraints") or []),
                        )
                        continue
                    log.info(
                        "retrieval rounds stop after round %d: stop=%s changed=%s valid=%s",
                        round_no,
                        bool(next_rewrite.get("stop")),
                        changed,
                        bool(next_rewrite.get("valid")),
                    )
                planner_previous_doc_ids = current_doc_ids

            if planner_enabled_task and ranked_results and list_mode is None:
                if planner_exhaustive:
                    verification_limit = _retrieval_planner().exhaustive_verification_candidate_limit
                elif planner_bounded_document_set:
                    verification_limit = _retrieval_planner().bounded_verification_candidate_limit
                else:
                    verification_limit = _retrieval_planner().verification_candidate_limit
                preverification_count = len(ranked_results)
                continuity_extra = sum(
                    1
                    for result in ranked_results
                    if bool(result.raw.get("_followup_evidence"))
                )
                effective_verification_limit = min(
                    60, verification_limit + continuity_extra
                )
                verified_results, uncertain_results, verification = await _verify_exhaustive_candidates(
                    retrieval_query,
                    ranked_results,
                    query_frame=planner_query_frame,
                    verification_requirements=list((active_search_spec or {}).get("verification_requirements") or []),
                    candidate_limit=effective_verification_limit,
                    # Completeness changes the candidate budget, not the verifier
                    # output vocabulary.  Keep the compact classification schema
                    # for exhaustive runs as well: they review more documents and
                    # are therefore the mode most vulnerable to truncated JSON.
                    compact=True,
                    exhaustive=planner_exhaustive,
                )
                verified_count = int(verification.get("checked") or 0)
                # ``ranked_results`` is bounded by the request limit.  For an
                # exhaustive files request Elasticsearch may know that more
                # lexical candidates exist beyond that window.  Include that
                # count conservatively so a capped exhaustive run never claims
                # completeness merely because the response list itself stopped
                # at the configured verifier budget.
                notice_candidate_count = _verification_notice_candidate_count(
                    preverification_count,
                    payload,
                    exhaustive=planner_exhaustive,
                )
                hard_limit_notice = _verification_limit_notice(
                    notice_candidate_count,
                    verified_count,
                    exhaustive=planner_exhaustive,
                    bounded_document_set=planner_bounded_document_set,
                )
                capacity_notice = ""
                if not hard_limit_notice:
                    capacity_notice = _verification_capacity_notice(
                        preverification_count,
                        verified_count,
                        effective_verification_limit,
                        exhaustive=planner_exhaustive,
                    )
                retrieval_limit_notice = hard_limit_notice or capacity_notice
                if retrieval_limit_notice:
                    _set_suggestions(_refinement_suggestions(
                        question,
                        user_id,
                        current_model_id=runtime_model.model.model_id,
                    ))
                    if hard_limit_notice:
                        log.info(
                            "Request %s candidate verification window limited: ranked=%d checked=%d configured_window=%d",
                            completion_id, preverification_count, verified_count, effective_verification_limit,
                        )
                    else:
                        log.info(
                            "Request %s candidate verification window near capacity: ranked=%d checked=%d configured_window=%d",
                            completion_id, preverification_count, verified_count, effective_verification_limit,
                        )
                log.info(
                    "RC8 candidate verify: mode=%s checked=%d match=%d uncertain=%d rejected=%d format=%s errors=%d retries=%d elapsed_ms=%d",
                    ("exhaustive" if planner_exhaustive else ("bounded" if planner_bounded_document_set else "normal")),
                    int(verification.get("checked") or 0),
                    int(verification.get("matches") or 0),
                    int(verification.get("uncertain") or 0),
                    int(verification.get("rejected") or 0),
                    str(verification.get("format_mode") or "-"),
                    len(verification.get("batch_errors") or []),
                    len(verification.get("batch_retries") or []),
                    int(verification.get("elapsed_ms") or 0),
                )
                _write_retrieval_record({
                    "schema_version": 1,
                    "query_id": completion_id,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "software_version": VERSION,
                    "query": {"original": question, "normalized": retrieval_query},
                    "query_frame": planner_query_frame,
                    "retrieval": {
                        "search_spec": dict(active_search_spec or {}),
                        "exhaustive": planner_exhaustive,
                        "bounded_document_set": planner_bounded_document_set,
                        "arms": sorted(retrieval_arms or {"files", "vector"}),
                        "source_scopes": sorted(source_scopes) if source_scopes else None,
                    },
                    "verification": {
                        key: value for key, value in verification.items()
                        if key != "reviewed_documents"
                    },
                    "documents": verification.get("reviewed_documents") or [],
                    "provenance": {
                        "query_rewriter_model": rewrite_model,
                        "verifier_model": _retrieval_planner().model or LLM_MODEL,
                        "answer_model": generation_model,
                    },
                })
                if planner_exhaustive and len(verified_results) > _retrieval_planner().max_complete_documents:
                    _set_suggestions(_refinement_suggestions(
                        question,
                        user_id,
                        current_model_id=runtime_model.model.model_id,
                    ))
                    content = (
                        "Die Suche ergibt mehr passende Dokumente, als vollständig und zuverlässig "
                        "in einer Antwort verarbeitet werden können. Bitte grenzen Sie die Suche "
                        "weiter ein, beispielsweise nach Zeitraum, Dokumenttyp, Beteiligten oder "
                        "einem zusätzlichen Sachkriterium."
                    )
                    _research_call(
                        "finish_query", query_id=completion_id, status="exhaustive_verified_overflow",
                        final_retrieval_query=retrieval_query, answer_text=content,
                    )
                    return _static_response(content, completion_id, body.stream)
                if planner_exhaustive and not verified_results and uncertain_results:
                    content = (
                        "Die Suche hat mögliche Kandidaten gefunden, aber keinen davon anhand des "
                        "Dokumentinhalts hinreichend sicher als passenden Treffer verifizieren können. "
                        "Bitte präzisieren Sie die Anfrage leicht; ich verwende keine lediglich ähnlich "
                        "erscheinenden Dokumente als Ersatztreffer."
                    )
                    _research_call(
                        "finish_query", query_id=completion_id, status="exhaustive_verification_uncertain",
                        final_retrieval_query=retrieval_query, answer_text=content,
                    )
                    return _static_response(content, completion_id, body.stream)
                ranked_results = verified_results

            if list_mode is not None:
                content = _format_retrieval_list(
                    ranked_results,
                    retrieval_arms,
                    raw_mode=(list_mode == "raw"),
                    query=retrieval_query,
                )
                if retrieval_limit_notice:
                    content += "\n\n" + retrieval_limit_notice
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status=("retrieval_list_raw" if list_mode == "raw" else "retrieval_list_ranked"),
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                log.info(
                    "Request %s finished as /list%s: arms=%s results=%d",
                    completion_id,
                    ":raw" if list_mode == "raw" else "",
                    sorted(retrieval_arms) if retrieval_arms else ["files", "vector", "graph"],
                    len(ranked_results),
                )
                return _static_response(content, completion_id, body.stream)

            if retrieval_mode in {"filename_exact", "filename_not_found"}:
                content = _filename_lookup_response(
                    lookup_filename,
                    ranked_results,
                )
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status=retrieval_mode,
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            if retrieval_mode == "too_unspecific":
                content = str(
                    payload.get("retrieval_message")
                    or "Die Anfrage ist zu unspezifisch, um zielführend beantwortet werden zu können. "
                       "Bitte spezifizieren Sie die Anfrage."
                ).strip()
                if bool(payload.get("graph_orientation_available")):
                    content += (
                        " Zu der erkannten Entität liegen Informationen im für Sie zugänglichen "
                        "Datenbestand vor."
                    )
                # A guard in one explicitly requested arm must not cancel another
                # requested capability. Preserve the internal warning and continue
                # with the web arm when web was explicitly/implicitly allowed.
                if explicit_mixed_web or web_capability:
                    results = []
                    context = ""
                    messages = []
                    internal_fallback_message = content
                    web_fallback_pending = not explicit_mixed_web
                    log.info("Request %s internal retrieval too_unspecific; continuing with web=%s", completion_id, True)
                    break
                _set_suggestions(_refinement_suggestions(
                    question,
                    user_id,
                    current_model_id=runtime_model.model.model_id,
                ))
                _research_call(
                    "finish_query", query_id=completion_id, status="too_unspecific",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                log.info("Request %s stopped as too_unspecific; graph_orientation_available=%s", completion_id, bool(payload.get("graph_orientation_available")))
                return _static_response(content, completion_id, body.stream)

            if retrieval_mode == "unspecific":
                content = str(
                    payload.get("retrieval_message")
                    or "Die Anfrage ist zu unspezifisch, um zielführend beantwortet werden zu können. "
                       "Bitte spezifizieren Sie die Anfrage."
                ).strip()
                if bool(payload.get("graph_orientation_available")):
                    content += (
                        " Zu der erkannten Entität liegen Informationen im für Sie zugänglichen "
                        "Datenbestand vor."
                    )
                if web_capability:
                    results = []
                    context = ""
                    messages = []
                    internal_fallback_message = content
                    web_fallback_pending = True
                    log.info("Request %s internal retrieval unspecific; web fallback may be evaluated", completion_id)
                    break
                _set_suggestions(_refinement_suggestions(
                    question,
                    user_id,
                    current_model_id=runtime_model.model.model_id,
                ))
                _research_call(
                    "finish_query",
                    query_id=completion_id,
                    status="unspecific",
                    final_retrieval_query=retrieval_query,
                    answer_text=content,
                )
                log.info(
                    "Request %s stopped before evidence/answer: "
                    "retrieval_strategy=%s signal=%s",
                    completion_id,
                    payload.get("retrieval_strategy"),
                    payload.get("retrieval_signal"),
                )
                return _static_response(content, completion_id, body.stream)

            if retrieval_mode == "degraded_no_results":
                content = str(payload.get("retrieval_message") or "Ein interner Suchdienst ist derzeit nicht verfügbar.").strip()
                if explicit_mixed_web or web_capability:
                    results = []
                    context = ""
                    messages = []
                    internal_fallback_message = content
                    web_fallback_pending = not explicit_mixed_web
                    break
                _research_call(
                    "finish_query", query_id=completion_id, status="degraded_no_results",
                    final_retrieval_query=retrieval_query, answer_text=content,
                )
                return _static_response(content, completion_id, body.stream)

            if not ranked_results:
                no_results_text = "Ich habe in den internen Unterlagen keine passenden Dokumenttreffer gefunden."
                if retrieval_limit_notice:
                    no_results_text += "\n\n" + retrieval_limit_notice
                if web_capability:
                    results = []
                    context = ""
                    messages = []
                    internal_fallback_message = no_results_text
                    web_fallback_pending = True
                    break
                if not ALLOW_GENERAL_KNOWLEDGE:
                    content = no_results_text
                    _research_call(
                        "finish_query",
                        query_id=completion_id,
                        status="no_results",
                        final_retrieval_query=retrieval_query,
                        answer_text=content,
                    )
                    return _static_response(content, completion_id, body.stream)

                results = []
                context = ""
                messages = _rag_answer_messages(question, retrieval_query, context)
                break

            context, evaluated_results = _build_context(
                ranked_results,
                preserve_all_results=planner_exhaustive,
            )
            review_context, review_results = _build_review_context(ranked_results)

            if _evidence_decision_mode() == "review":
                _research_call(
                    "log_documents",
                    round_id=round_id,
                    stage="review_context",
                    documents=_raw_documents(review_results),
                )

                try:
                    decision = await _evidence_decision(
                        question,
                        retrieval_query,
                        review_context,
                        force_broad=force_unspecific,
                    )
                except Exception as exc:
                    log.warning("Evidence decision failed; continuing with answer: %s", exc)
                    decision = {
                        "action": "answer",
                        "reason": f"evidence decision failed: {type(exc).__name__}",
                        "next_query": "",
                        "clarification_options": [],
                        "conflict_sources": [],
                        "answer_sources": [],
                    }

                _research_call("set_evidence_decision", round_id, decision)
                action = decision["action"]

                if action == "retry":
                    if (
                        round_no < _retrieval_planner().max_retrieval_rounds
                        and not planner_enabled_task
                        and not entity_recall_backoff
                        and _eligible_entity_recall_backoff(payload)
                    ):
                        entity_recall_backoff = True
                        log.info(
                            "Evidence requested retry; starting entity-recall "
                            "backoff with unchanged retrieval query: %s",
                            retrieval_query,
                        )
                        continue

                    reason = str(decision.get("reason") or "").strip()
                    content = (
                        "Die Suche liefert kein hinreichend eindeutiges Trefferbild, "
                        "um die Anfrage zuverlässig zu beantworten. "
                        "Bitte spezifizieren Sie die Anfrage."
                    )
                    if reason:
                        content += " " + reason
                    if web_capability:
                        results = []
                        context = ""
                        messages = []
                        internal_fallback_message = content
                        web_fallback_pending = True
                        log.info("Evidence requested retry; no internal pass left, web fallback may be evaluated")
                        break
                    _set_suggestions(_refinement_suggestions(
                        question,
                        user_id,
                        current_model_id=runtime_model.model.model_id,
                    ))
                    _research_call(
                        "finish_query",
                        query_id=completion_id,
                        status="needs_user_refinement",
                        final_retrieval_query=retrieval_query,
                        answer_text=content,
                    )
                    log.info(
                        "Evidence requested retry; no further automatic recall pass available."
                    )
                    return _static_response(content, completion_id, body.stream)

                if action == "conflict":
                    content = _format_conflict(decision)
                    conflict_results = _select_results_by_indexes(
                        ranked_results,
                        decision.get("conflict_sources") or [],
                    )
                    await _graph_enqueue_evidence(
                        query_id=completion_id,
                        user_query=question,
                        retrieval_query=retrieval_query,
                        evidence_action="conflict",
                        results=conflict_results,
                        rag_user_id=user_id,
                    )
                    _research_call(
                        "log_documents",
                        round_id=round_id,
                        stage="cited",
                        documents=_raw_documents(
                            _cited_results(content, ranked_results)
                        ),
                    )
                    content += _source_suffix(content, ranked_results, query=retrieval_query)
                    _research_call(
                        "finish_query",
                        query_id=completion_id,
                        status="conflict",
                        final_retrieval_query=retrieval_query,
                        answer_text=content,
                    )
                    log.info(
                        "Request %s finished: status=conflict sources=%s",
                        completion_id,
                        decision.get("conflict_sources"),
                    )
                    return _static_response(content, completion_id, body.stream)

                if action == "clarify":
                    _set_suggestions([
                        {
                            "label": str(option.get("label") or "").strip(),
                            "action": "query",
                            "query": str(option.get("query") or "").strip(),
                        }
                        for option in (decision.get("clarification_options") or [])
                        if isinstance(option, dict)
                    ])
                    content = _format_clarification(decision)
                    _research_call(
                        "finish_query",
                        query_id=completion_id,
                        status="clarify",
                        final_retrieval_query=retrieval_query,
                        answer_text=content,
                    )
                    return _static_response(content, completion_id, body.stream)

                if action == "insufficient":
                    if (
                        round_no < _retrieval_planner().max_retrieval_rounds
                        and not planner_enabled_task
                        and not entity_recall_backoff
                        and _eligible_entity_recall_backoff(payload)
                    ):
                        entity_recall_backoff = True
                        log.info(
                            "Evidence insufficient; starting entity-recall backoff "
                            "with unchanged retrieval query: %s",
                            retrieval_query,
                        )
                        continue

                    content = _format_insufficient(decision)
                    if web_capability:
                        results = []
                        context = ""
                        messages = []
                        internal_fallback_message = content
                        web_fallback_pending = True
                        log.info("Request %s internal evidence insufficient; web fallback may be evaluated", completion_id)
                        break
                    _research_call(
                        "finish_query",
                        query_id=completion_id,
                        status="insufficient",
                        final_retrieval_query=retrieval_query,
                        answer_text=content,
                    )
                    return _static_response(content, completion_id, body.stream)

                if action == "answer":
                    requested_sources = decision.get("answer_sources") or []
                    selected_results = _select_results_by_indexes(
                        ranked_results,
                        requested_sources,
                    )

                    if selected_results:
                        context, results = _build_context(selected_results)
                        log.info(
                            "Evidence control ANSWER: selected sources=%s "
                            "(%d of %d ranked results); files=%s",
                            [result.index for result in results],
                            len(results),
                            len(ranked_results),
                            [
                                f"[{result.index}] {_source_filename(result)}"
                                for result in results
                            ],
                        )
                    else:
                        # Robust fallback for an older/weak control-model response:
                        # preserve previous behaviour rather than fail the request.
                        results = evaluated_results
                        log.warning(
                            "Evidence control ANSWER returned no usable "
                            "answer_sources; using evaluated answer context."
                        )
                else:
                    results = evaluated_results
            else:
                results = evaluated_results

            # Rebuild context from exactly the documents that will reach the answerer.
            # This is intentionally separate from the larger review context.
            context, results = _build_context(
                results,
                preserve_all_results=planner_exhaustive,
            )
            _log_answer_context_stats("retrieval", context, results)

            _research_call(
                "log_documents",
                round_id=round_id,
                stage="answer_context",
                documents=_raw_documents(results),
            )
            # A ResearchRun should describe evidence that actually reached the
            # answer model, not the wider verifier candidate pool. This keeps
            # curation aligned with the user-visible research and avoids
            # persisting verified-but-unused candidates as run Findings.
            await _store_positive_research_findings(
                query_id=completion_id,
                query_frame=planner_query_frame,
                results=results,
                canonical_user_id=str(auth_state.get("canonical_user_id") or ""),
                nextcloud_login=str(auth_state.get("nextcloud_login") or ""),
                nextcloud_server=str(auth_state.get("server") or ""),
                user_query=question,
                retrieval_query=retrieval_query,
                source_scopes=source_scopes,
            )
            messages = _rag_answer_messages(question, retrieval_query, context)
            break

    if (not auxiliary) and explicit_mixed_web:
        if (
            natural_workflow is not None
            and natural_workflow.web_timing == "after"
            and results
        ):
            web_search_queries = await _derive_after_web_queries(
                instruction=natural_workflow.instruction,
                question=question,
                initial_web_query=web_search_query,
                internal_context=context,
            )
            if web_search_queries:
                web_search_query = web_search_queries[0]

        auto_web = True
        auto_web_decision = {
            "use_web": True,
            "reason": f"explicit_natural_instruction:{natural_workflow.web_timing if natural_workflow else 'parallel'}",
        }
        web_fallback_pending = False
        log.info(
            "Request %s explicit natural web workflow: timing=%s queries=%r internal_results=%d",
            completion_id,
            natural_workflow.web_timing if natural_workflow else "parallel",
            web_search_queries,
            len(results),
        )

    # Evaluate automatic web only after the internal pipeline has explicitly
    # ended without sufficient evidence. This keeps ordinary entity/document
    # questions local even when a UI has granted web capability.
    if (not auxiliary) and web_fallback_pending:
        auto_web_decision = await _decide_web_use(question, retrieval_query)
        auto_web = bool(auto_web_decision.get("use_web"))
        log.info(
            "Request %s web fallback: allowed=true use_web=%s reason=%r",
            completion_id, auto_web, auto_web_decision.get("reason"),
        )
        if not auto_web:
            content = internal_fallback_message or (
                "Die internen Unterlagen liefern keine hinreichende Evidence; "
                "die Web-Recherche wurde für diese Anfrage nicht als erforderlich bewertet."
            )
            _research_call(
                "finish_query", query_id=completion_id, status="internal_insufficient",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)

    # Optional public-web fallback. The client only grants permission; our own
    # gate/search/fetch/relevance/archive pipeline remains authoritative.
    if (not auxiliary) and auto_web:
        try:
            if explicit_natural_web:
                auto_web_payload = await _web_search_many(web_search_queries, user_id, completion_id)
            else:
                auto_web_payload = await _web_search(web_search_query, user_id, completion_id)
            auto_web_context, auto_web_sources = _build_web_context(auto_web_payload)
            auto_web_archive_run_paths = [
                str(path).strip()
                for path in (auto_web_payload.get("archive_run_paths") or [])
                if str(path).strip()
            ]
            if not auto_web_archive_run_paths:
                single_path = str(auto_web_payload.get("archive_run_path") or "").strip()
                if single_path:
                    auto_web_archive_run_paths = [single_path]
            auto_web_archive_errors = list(auto_web_payload.get("archive_errors") or [])
        except Exception as exc:
            log.warning("Request %s automatic web arm failed; internal answer continues: %s", completion_id, exc)
            auto_web_payload = None
            auto_web_sources = []
            auto_web_context = ""

        if auto_web_sources:
            if results:
                messages = _hybrid_web_answer_messages(
                    question, retrieval_query, context, auto_web_context
                )
            else:
                messages = _web_answer_messages(question, auto_web_context)
        elif not results and internal_fallback_message:
            content = (
                internal_fallback_message
                + " Auch die freigegebene Web-Recherche hat keine hinreichend relevante, tatsächlich abrufbare Quelle geliefert."
            )
            _research_call(
                "finish_query", query_id=completion_id, status="web_insufficient",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            return _static_response(content, completion_id, body.stream)

    _progress("answering", "Antwort wird erstellt …")
    if not body.stream:
        try:
            answer = await _ollama_complete(
                messages,
                options=options,
                think=think,
                model=generation_model,
                role="answer",
            )
            if not answer and not auxiliary:
                log.warning("Request %s: empty answer content; retrying once with think=false", completion_id)
                answer = await _ollama_complete(
                    messages,
                    options=options,
                    think=False,
                    model=generation_model,
                    role="answer",
                )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            # No model answer means no document was actually used in an answer.
            # Do not expose the retrieval candidate list as if it were evidence.
            content = _llm_unavailable_text(exc, found=len(results))
            _research_call(
                "finish_query", query_id=completion_id, status="llm_unavailable",
                final_retrieval_query=retrieval_query, answer_text=content,
            )
            log.warning("Request %s LLM unavailable: %s", completion_id, exc)
            return _static_response(content, completion_id, body.stream)

        if not answer and not auxiliary:
            if direct_kind:
                answer = (
                    "Das LLM-Backend hat keinen Antworttext geliefert. "
                    "Bitte prüfe die Modell-/Token-Einstellungen des Backends."
                )
            else:
                answer = (
                    "Die Dokumenttreffer wurden gefunden, aber das LLM-Backend hat keinen Antworttext geliefert. "
                    "Bitte prüfe die Thinking-/Token-Einstellungen des Backends."
                )

        if not auxiliary:
            if (
                use_references
                and len(results) > 1
                and _explicit_selection_needs_documentwise_completeness(question)
            ):
                answer = await _repair_incomplete_explicit_selection(
                    answer,
                    messages,
                    results,
                    question=question,
                    options=options,
                    model=generation_model,
                )
            explicit_cited_results = _cited_results(answer, results)
            if not explicit_cited_results and results:
                answer, explicit_cited_results = await _repair_missing_citations(
                    answer, results, question=question
                )
            if explicit_cited_results:
                await _graph_enqueue_evidence(
                    query_id=completion_id,
                    user_query=question,
                    retrieval_query=retrieval_query,
                    evidence_action="answer_cited",
                    results=explicit_cited_results,
                    rag_user_id=user_id,
                )
            cited_results = explicit_cited_results
            _research_call(
                "log_documents",
                round_id=final_round_id,
                stage="cited",
                documents=_raw_documents(cited_results),
            )
            internal_heading = "Interne Quellen" if auto_web_sources else "Quellen"
            content = answer + _source_suffix(
                answer,
                results,
                query=retrieval_query,
                heading=internal_heading,
                include_all_visible=planner_exhaustive,
            )
            if auto_web_sources:
                content += _web_source_suffix(auto_web_sources)
            if retrieval_limit_notice:
                content += "\n\n" + retrieval_limit_notice
            if auto_web_sources:
                for auto_web_archive_run_path in auto_web_archive_run_paths:
                    try:
                        await _web_finalize_archive(
                            auto_web_archive_run_path,
                            answer,
                            user_id,
                            answer_model=ANSWER_MODEL,
                            answer_parameters=dict(options or {}),
                            request_id=completion_id,
                        )
                    except Exception as exc:
                        auto_web_archive_errors.append({
                            "url": "recherche.md",
                            "run_path": auto_web_archive_run_path,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                        log.warning("Request %s automatic web archive finalization failed for %s: %s", completion_id, auto_web_archive_run_path, exc)
                if auto_web_archive_errors:
                    content += f"\n\n*Webarchiv: {len(auto_web_archive_errors)} Archivierungsschritt(e) konnten nicht vollständig abgeschlossen werden.*"
        else:
            content = answer

        _research_call(
            "finish_query",
            query_id=completion_id,
            status="answered",
            final_retrieval_query=retrieval_query,
            answer_text=content,
        )
        log.info("Request %s finished: status=answered answer_chars=%d", completion_id, len(content))
        if deferred_use_findings is not None:
            _schedule_use_research_findings(**deferred_use_findings)
        return _completion_response(content, completion_id)

    async def event_stream() -> AsyncIterator[str]:
        accumulated: list[str] = []
        initial = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": _active_model_id(),
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"

        status = "answered"
        reasoning_open = False
        reasoning_chunks = 0
        reasoning_chars = 0
        content_chunks = 0

        try:
            async for event in _ollama_stream(
                messages,
                options=options,
                think=think,
                model=generation_model,
                max_seconds=(ELASTIC_STREAM_MAX_SECONDS if elastic_mode else None),
                role="answer",
            ):
                reasoning = str(event.get("reasoning") or "")
                content_token = str(event.get("content") or "")

                if reasoning:
                    reasoning_chunks += 1
                    reasoning_chars += len(reasoning)

                    if STREAM_REASONING:
                        if REASONING_STREAM_FORMAT == "reasoning_content":
                            yield _sse_chunk(
                                completion_id,
                                reasoning=reasoning,
                            )
                        else:
                            if not reasoning_open:
                                yield _sse_chunk(completion_id, "<think>\n")
                                reasoning_open = True
                            yield _sse_chunk(completion_id, reasoning)

                if content_token:
                    if reasoning_open:
                        yield _sse_chunk(completion_id, "\n</think>\n")
                        reasoning_open = False

                    content_chunks += 1
                    accumulated.append(content_token)
                    yield _sse_chunk(completion_id, content_token)

        except Exception as exc:
            status = "stream_error"
            if reasoning_open:
                yield _sse_chunk(completion_id, "\n</think>\n")
                reasoning_open = False
            if not accumulated:
                friendly = _llm_unavailable_text(exc, found=len(results))
                accumulated.append(friendly)
                yield _sse_chunk(completion_id, friendly)
                log.warning("Request %s LLM unavailable during stream: %s", completion_id, exc)
            else:
                note = f"\n\n[LLM-Ausgabe unterbrochen: {exc}]"
                accumulated.append(note)
                yield _sse_chunk(completion_id, note)
                log.warning("Request %s LLM stream interrupted: %s", completion_id, exc)

        if reasoning_open:
            yield _sse_chunk(completion_id, "\n</think>\n")
            reasoning_open = False

        log.info(
            "Request %s stream stats: reasoning_chunks=%d reasoning_chars=%d "
            "content_chunks=%d content_chars=%d format=%s visible=%s",
            completion_id,
            reasoning_chunks,
            reasoning_chars,
            content_chunks,
            sum(len(part) for part in accumulated),
            REASONING_STREAM_FORMAT,
            STREAM_REASONING,
        )

        answer = "".join(accumulated)
        if not answer and not auxiliary and status == "answered":
            log.warning("Request %s: empty streamed answer; retrying once with think=false", completion_id)
            try:
                fallback = await _ollama_complete(
                    messages,
                    options=options,
                    think=False,
                    model=generation_model,
                    role="answer",
                )
            except Exception as exc:
                fallback = ""
                log.warning("Request %s: no-think fallback failed: %s", completion_id, exc)
            if fallback:
                accumulated.append(fallback)
                answer = fallback
                yield _sse_chunk(completion_id, fallback)
            else:
                if direct_kind:
                    answer = (
                        "Das LLM-Backend hat keinen Antworttext geliefert. "
                        "Bitte prüfe die Modell-/Token-Einstellungen des Backends."
                    )
                else:
                    answer = (
                        "Die Dokumenttreffer wurden gefunden, aber das LLM-Backend hat keinen Antworttext geliefert. "
                        "Bitte prüfe die Thinking-/Token-Einstellungen des Backends."
                    )
                yield _sse_chunk(completion_id, answer)

        if not auxiliary:
            explicit_cited_results = _cited_results(answer, results)
            if not explicit_cited_results and results:
                repaired_answer, explicit_cited_results = await _repair_missing_citations(
                    answer, results, question=question
                )
                if repaired_answer != answer:
                    repair_delta = repaired_answer[len(answer):]
                    if repair_delta:
                        yield _sse_chunk(completion_id, repair_delta)
                    answer = repaired_answer
            if explicit_cited_results:
                await _graph_enqueue_evidence(
                    query_id=completion_id,
                    user_query=question,
                    retrieval_query=retrieval_query,
                    evidence_action="answer_cited",
                    results=explicit_cited_results,
                    rag_user_id=user_id,
                )
            cited_results = explicit_cited_results
            _research_call(
                "log_documents",
                round_id=final_round_id,
                stage="cited",
                documents=_raw_documents(cited_results),
            )
            internal_heading = "Interne Quellen" if auto_web_sources else "Quellen"
            suffix = _source_suffix(
                answer,
                results,
                query=retrieval_query,
                heading=internal_heading,
                include_all_visible=planner_exhaustive,
            )
            if auto_web_sources:
                suffix += _web_source_suffix(auto_web_sources)
            if suffix:
                yield _sse_chunk(completion_id, suffix)
                answer += suffix
            if retrieval_limit_notice:
                notice_delta = "\n\n" + retrieval_limit_notice
                yield _sse_chunk(completion_id, notice_delta)
                answer += notice_delta

            if auto_web_sources:
                for auto_web_archive_run_path in auto_web_archive_run_paths:
                    try:
                        await _web_finalize_archive(
                            auto_web_archive_run_path,
                            "".join(accumulated),
                            user_id,
                            answer_model=ANSWER_MODEL,
                            answer_parameters=dict(options or {}),
                            request_id=completion_id,
                        )
                    except Exception as exc:
                        log.warning(
                            "Request %s automatic web archive finalization failed for %s: %s",
                            completion_id, auto_web_archive_run_path, exc,
                        )

        _research_call(
            "finish_query",
            query_id=completion_id,
            status=status,
            final_retrieval_query=retrieval_query,
            answer_text=answer,
        )
        log.info("Request %s finished: status=%s answer_chars=%d", completion_id, status, len(answer))
        if deferred_use_findings is not None and status == "answered":
            _schedule_use_research_findings(**deferred_use_findings)

        yield _sse_chunk(completion_id, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

