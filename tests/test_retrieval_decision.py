from pathlib import Path
import json

import pytest

import rag.openai_provider as provider
from rag.retrieval.contracts import (
    EffectiveRetrievalPlan,
    RetrievalDecisionRecord,
    RetrievalPlannerProposal,
    RetrievalPolicyResolution,
)
from rag.retrieval.decision import build_retrieval_decision_record


def test_decision_builder_separates_proposal_policy_and_effective_plan():
    record = build_retrieval_decision_record(
        query_id="req-strategy-1",
        original_query='Suche "Musterweg 280"',
        normalized_query='Suche "Musterweg 280"',
        search_spec={
            "semantic_query": "Musterweg 280",
            "entities": ["Musterweg 280"],
            "concepts": ["Adresse"],
            "concept_groups": [
                {"required": False, "terms": ["Adresse", "Anschrift"]},
            ],
            "constraints": [{"kind": "year", "value": "2025"}],
        },
        architecture_tier="src",
        model_id="sunaq-standard",
        retrieval_arms={"files"},
        source_scopes=None,
        explicit_arm_selection=False,
        web_requested=False,
        resolver_research_requested=False,
        resolver_gateway_enabled=False,
        resolver_sources=[],
        exhaustive=False,
        bounded_document_set=True,
        max_rounds=1,
        max_queries_per_round=3,
        verification_candidate_limit=10,
        max_complete_documents=15,
    )
    payload = record.to_dict()

    assert "elastic_query" not in payload["planner"]
    assert "intent" not in payload["planner"]
    assert "elastic_query" not in payload["effective_plan"]
    assert "semantic_query" not in payload["effective_plan"]
    assert payload["planner"]["concept_groups"] == (
        (False, ("Adresse", "Anschrift")),
    )
    assert payload["policy"]["source_scopes"] == ("documents",)
    assert "src_tier" in payload["policy"]["reason_codes"]
    assert "policy_default_retrieval_arms" in payload["policy"]["reason_codes"]
    assert "bounded_document_set" in payload["policy"]["reason_codes"]
    assert "quoted_phrase" in payload["policy"]["reason_codes"]
    assert payload["effective_plan"]["verification_candidate_limit"] == 10


def test_execution_projection_drops_backend_ranks_and_filters_verifier_to_authorized_docs():
    result = provider.SearchResult(
        index=1,
        title="Jane-Doe-CV.odt",
        text="",
        raw={
            "document_id": "files:62556",
            "path": "Examples/Jane-Doe/Jane-Doe-CV.odt",
            "es_rank": 7,
            "rrf_rank": 4,
            "es_score": 131.34,
            "source_origin": "",
        },
    )
    records = provider._authorized_document_records([result])
    assert records == [{
        "authorized_rank": 1,
        "document_id": "files:62556",
        "title": "Jane-Doe-CV.odt",
        "path": "Examples/Jane-Doe/Jane-Doe-CV.odt",
        "source_url": "",
        "source_origin": "",
        "document_date": None,
    }]

    verification = {
        "reviewed_documents": [
            {
                "document_id": "files:62556",
                "title": "Jane-Doe-CV.odt",
                "path": "Examples/Jane-Doe/Jane-Doe-CV.odt",
                "status": "match",
                "relation_binding": "direct",
                "reason": "CV",
            },
            {
                "document_id": "files:hidden",
                "title": "hidden",
                "path": "hidden",
                "status": "reject",
            },
        ]
    }
    verifier = provider._authorized_verifier_records(verification, [result])
    assert [item["document_id"] for item in verifier] == ["files:62556"]
    assert verifier[0]["authorized_rank"] == 1


def test_decision_contract_has_no_reasoning_or_credentials_channel():
    record = RetrievalDecisionRecord(
        query_id="req-decision-1",
        original_query="find evidence",
        normalized_query="find evidence",
        planner=RetrievalPlannerProposal(intent="find evidence"),
        policy=RetrievalPolicyResolution(
            architecture_tier="erg",
            model_id="sunaq-standard",
            retrieval_arms=("files",),
        ),
        effective_plan=EffectiveRetrievalPlan(
            retrieval_arms=("files",),
            max_rounds=1,
            max_queries_per_round=1,
            verification_candidate_limit=1,
            max_complete_documents=1,
        ),
    )
    payload = record.to_dict()

    assert "reasoning" not in payload
    assert "credentials" not in payload
    with pytest.raises(ValueError, match="architecture tier"):
        RetrievalPolicyResolution(
            architecture_tier="invalid",
            model_id="sunaq-standard",
        )


def test_decision_record_variant_does_not_overwrite_outcome_record(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "RETRIEVAL_RECORD_ENABLED", True)
    monkeypatch.setattr(provider, "RETRIEVAL_RECORD_DIRECTORY", tmp_path)

    provider._write_retrieval_record(
        {"query_id": "chatcmpl-decision-test", "stage": "outcome"}
    )
    provider._write_retrieval_record(
        {
            "schema": "sunaq.retrieval_decision.v2",
            "query_id": "chatcmpl-decision-test",
            "stage": "decision",
        },
        kind="decision",
    )

    outcome = list(tmp_path.rglob("chatcmpl-decision-test.json"))
    decision = list(tmp_path.rglob("chatcmpl-decision-test.decision.json"))
    assert len(outcome) == 1
    assert len(decision) == 1
    assert json.loads(decision[0].read_text(encoding="utf-8"))["stage"] == "decision"


def test_progress_state_preserves_identity_scoped_decision(monkeypatch):
    request_id = "req-strategy-1234"
    owner = "client-a|user-a"
    with provider._PROGRESS_LOCK:
        provider._PROGRESS_STATES.pop(request_id, None)

    monkeypatch.setattr(provider, "_active_model_id", lambda: "sunaq-standard")
    provider._set_progress(request_id, owner, "analysis", "Analysiere")
    provider._set_progress_retrieval_decision(
        request_id,
        owner,
        {
            "schema": "sunaq.retrieval_decision.v2",
            "policy": {"architecture_tier": "src"},
        },
    )
    provider._set_progress(request_id, owner, "retrieval", "Suche")

    state = provider._read_progress(request_id, owner)
    assert state is not None
    assert state["retrieval_decision"]["schema"] == "sunaq.retrieval_decision.v2"
    assert provider._read_progress(request_id, "other-client|user-a") is None

def test_provider_publishes_decision_on_client_progress_id():
    source = (Path(__file__).resolve().parent.parent / "rag" / "openai_provider.py").read_text(
        encoding="utf-8"
    )
    assert "_set_progress_retrieval_decision(\n                progress_id," in source
    assert "_set_progress_retrieval_decision(\n                completion_id," not in source

def test_decision_record_distinguishes_requested_and_effective_resolver():
    record = build_retrieval_decision_record(
        query_id="req-register-policy-1",
        original_query="Example Machinery AG",
        normalized_query="Example Machinery AG",
        search_spec={
            "elastic_query": "Example Machinery AG",
            "semantic_query": "Example Machinery AG",
            "entities": ["Example Machinery AG"],
            "entity_hints": [{"text": "Example Machinery AG", "kind": "organization"}],
        },
        architecture_tier="erg",
        model_id="sunaq-register",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        explicit_arm_selection=False,
        web_requested=False,
        resolver_research_requested=True,
        resolver_gateway_enabled=False,
        resolver_sources=["openregister"],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=3,
        verification_candidate_limit=10,
        max_complete_documents=15,
    )
    payload = record.to_dict()

    assert payload["policy"]["requested_resolver_sources"] == ("openregister",)
    assert payload["policy"]["resolver_research_requested"] is True
    assert payload["policy"]["resolver_research_enabled"] is False
    assert payload["policy"]["resolver_sources"] == ()
    assert payload["effective_plan"]["resolver_sources"] == ()
    assert "resolver_research_requested" in payload["policy"]["reason_codes"]
    assert "resolver_disabled_by_admin" in payload["policy"]["reason_codes"]
    assert "resolver_research_enabled" not in payload["policy"]["reason_codes"]

    granted = build_retrieval_decision_record(
        query_id="req-register-policy-2",
        original_query="Example Machinery AG",
        normalized_query="Example Machinery AG",
        search_spec={
            "elastic_query": "Example Machinery AG",
            "semantic_query": "Example Machinery AG",
            "entities": ["Example Machinery AG"],
        },
        architecture_tier="erg",
        model_id="sunaq-register",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        explicit_arm_selection=False,
        web_requested=False,
        resolver_research_requested=True,
        resolver_gateway_enabled=True,
        resolver_sources=["openregister"],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=3,
        verification_candidate_limit=10,
        max_complete_documents=15,
    ).to_dict()
    assert granted["policy"]["resolver_sources"] == ("openregister",)
    assert granted["policy"]["resolver_research_enabled"] is True
    assert granted["effective_plan"]["resolver_sources"] == ("openregister",)
    assert "resolver_research_enabled" in granted["policy"]["reason_codes"]

# Policy denial must remain observable even when no planner call is allowed.
def test_blocked_register_plan_records_policy_without_running_planner():
    payload = build_retrieval_decision_record(
        query_id="req-register-denied",
        original_query="Example Machinery AG",
        normalized_query="Example Machinery AG",
        search_spec={},
        architecture_tier="erg",
        model_id="sunaq-register",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        explicit_arm_selection=False,
        web_requested=False,
        resolver_research_requested=True,
        resolver_gateway_enabled=False,
        resolver_sources=["openregister"],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=3,
        verification_candidate_limit=10,
        max_complete_documents=15,
        planner_status="not_run",
        block_retrieval=True,
    ).to_dict()

    assert payload["planner"]["status"] == "not_run"
    assert "elastic_query" not in payload["planner"]
    assert payload["planner"]["semantic_query"] == ""
    assert payload["policy"]["retrieval_arms"] == ("files", "vector")
    assert payload["policy"]["requested_resolver_sources"] == ("openregister",)
    assert payload["policy"]["resolver_sources"] == ()
    assert payload["effective_plan"]["retrieval_arms"] == ()
    assert payload["effective_plan"]["source_scopes"] == ()
    assert payload["effective_plan"]["resolver_sources"] == ()
    assert "planner_not_run" in payload["policy"]["reason_codes"]
    assert "resolver_disabled_by_admin" in payload["policy"]["reason_codes"]


def test_provider_resolves_direct_resolver_policy_deny_before_query_rewrite():
    source = (Path(__file__).resolve().parent.parent / "rag" / "openai_provider.py").read_text(
        encoding="utf-8"
    )
    start = source.index("resolver_settings = load_resolver_settings(runtime_model.model.config)")
    exhaustive = source.index("planner_exhaustive = detect_exhaustive_intent(retrieval_query)", start)
    bounded = source.index("planner_bounded_document_set = (", start)
    deny = source.index("if resolver_direct_answer and not resolver_settings.enabled:", start)
    rewrite = source.index("initial_rewrite = await _rewrite_search_spec(", start)

    assert exhaustive < deny
    assert bounded < deny
    assert deny < rewrite
    blocked = source[deny:rewrite]
    assert 'planner_status="not_run"' in blocked
    assert "block_retrieval=True" in blocked
    assert "Die Resolver-Recherche ist administrativ nicht freigeschaltet." in blocked

def test_provider_gates_erg_resolver_by_profile_and_explicit_sources():
    source = (Path(__file__).resolve().parent.parent / "rag" / "openai_provider.py").read_text(
        encoding="utf-8"
    )
    marker = "resolver_outcome = await resolve_search_spec("
    call = source.index(marker)
    gate_start = source.rfind("if (", 0, call)
    gate = source[gate_start:call]

    assert "resolver_research_enabled" in gate
    assert "resolver_settings.enabled" in gate
    assert "effective_resolver_sources" in gate
    assert 'active_search_spec.get("entities")' in gate

    call_block = source[call:call + 500]
    assert "sources=effective_resolver_sources," in call_block
    assert "sources=resolver_sources," not in call_block


def test_provider_logs_query_context_rewrite_and_resolver_timings():
    source = (Path(__file__).resolve().parent.parent / "rag" / "openai_provider.py").read_text(
        encoding="utf-8"
    )
    assert "timing: query_context_ms=%.1f" in source
    assert "timing: query_rewrite_ms=%.1f" in source
    assert "timing: resolver_ms=%.1f sources=%s references=%d" in source

def test_explicit_web_is_visible_as_granted_effective_retrieval_arm():
    payload = build_retrieval_decision_record(
        query_id="req-web-mixed",
        original_query="Recherchiere intern und im Web",
        normalized_query="Recherchiere intern und im Web",
        search_spec={
            "elastic_query": "+Example",
            "semantic_query": "Example",
            "entities": ["Example GmbH"],
        },
        architecture_tier="erg",
        model_id="sunaq-standard",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        explicit_arm_selection=False,
        web_requested=True,
        resolver_research_requested=False,
        resolver_gateway_enabled=False,
        resolver_sources=[],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=3,
        verification_candidate_limit=10,
        max_complete_documents=15,
    ).to_dict()

    assert payload["policy"]["retrieval_arms"] == ("files", "vector")
    assert payload["policy"]["web_requested"] is True
    assert payload["policy"]["web_enabled"] is True
    assert payload["effective_plan"]["retrieval_arms"] == ("files", "vector", "web")
    assert payload["effective_plan"]["source_scopes"] == ("documents",)
    assert "web_requested" in payload["policy"]["reason_codes"]
    assert "web_enabled" in payload["policy"]["reason_codes"]


def test_erg_internal_decision_denies_web_and_external_resolver_but_keeps_local_resolver():
    external = build_retrieval_decision_record(
        query_id="req-internal-external",
        original_query="Example",
        normalized_query="Example",
        search_spec={"semantic_query": "Example", "entities": ["Example GmbH"]},
        architecture_tier="erg_internal",
        model_id="sunaq-standard",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        explicit_arm_selection=False,
        web_requested=True,
        resolver_research_requested=True,
        resolver_gateway_enabled=True,
        resolver_sources=["openregister"],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=1,
        verification_candidate_limit=10,
        max_complete_documents=10,
    ).to_dict()

    assert external["policy"]["architecture_tier"] == "erg_internal"
    assert external["policy"]["web_enabled"] is False
    assert "web_disallowed_in_erg_internal" in external["policy"]["reason_codes"]
    assert external["policy"]["resolver_sources"] == ()
    assert "resolver_source_disallowed_by_tier" in external["policy"]["reason_codes"]

    local = build_retrieval_decision_record(
        query_id="req-internal-local",
        original_query="Example",
        normalized_query="Example",
        search_spec={"semantic_query": "Example", "entities": ["Example GmbH"]},
        architecture_tier="erg_internal",
        model_id="sunaq-standard",
        retrieval_arms={"vector"},
        source_scopes={"documents"},
        explicit_arm_selection=True,
        web_requested=False,
        resolver_research_requested=True,
        resolver_gateway_enabled=True,
        resolver_sources=["neo4j"],
        exhaustive=False,
        bounded_document_set=False,
        max_rounds=1,
        max_queries_per_round=1,
        verification_candidate_limit=10,
        max_complete_documents=10,
    ).to_dict()
    assert local["policy"]["resolver_sources"] == ("neo4j",)
    assert local["policy"]["resolver_research_enabled"] is True
    assert "erg_internal_tier" in local["policy"]["reason_codes"]


def test_retrieval_strategy_ui_surfaces_web_grant():
    source = (
        Path(__file__).resolve().parent.parent
        / "clients" / "nextcloud" / "sunaq" / "js" / "app.js"
    ).read_text(encoding="utf-8")
    assert "Web angefordert: ja" in source
    assert "Web freigegeben: " in source

