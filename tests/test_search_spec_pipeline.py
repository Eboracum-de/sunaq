from pathlib import Path
import asyncio
import json

import rag.openai_provider as provider
import rag.search as search
from rag.planner import plan_from_search_spec
from rag.search_spec import normalize_search_spec, query_frame_from_search_spec


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.is_error = False
        self.status_code = 200
        self.reason_phrase = "OK"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _examplehost_spec():
    return normalize_search_spec({
        "semantic_query": "Rechnungen von examplehost aus dem Jahr 2025",
        "entities": ["examplehost"],
        "concepts": ["Rechnung"],
        "concept_groups": [
            {"required": True, "terms": ["Rechnung", "Invoice"]},
        ],
        "constraints": [{"kind": "Jahr", "value": "2025"}],
        "verification_requirements": [
            "Das Dokument ist selbst eine Rechnung von examplehost.",
            "Das relevante Jahr ist 2025.",
        ],
    })


def test_api_search_spec_transport_preserves_structured_intent():
    from rag.api import SearchSpecRequest

    request = SearchSpecRequest(
        semantic_query="Jane Doe CV",
        entities=["Jane Doe"],
        entity_hints=[{"text": "Jane Doe", "kind": "person"}],
        concepts=["CV"],
        concept_groups=[{
            "required": True,
            "terms": ["CV", "résumé", "curriculum vitae", "professional background"],
        }],
        verification_requirements=["The requested document is a CV."],
    )
    dumped = request.model_dump()

    assert dumped["entities"] == ["Jane Doe"]
    assert dumped["entity_hints"] == [
        {"text": "Jane Doe", "kind": "person"}
    ]
    assert dumped["concept_groups"] == [{
        "required": True,
        "terms": ["CV", "résumé", "curriculum vitae", "professional background"],
    }]


def test_search_spec_builds_expected_plan_and_query_frame():
    spec = _examplehost_spec()
    plan = plan_from_search_spec(
        spec, {}, original_query="Suche Rechnungen von examplehost aus dem Jahr 2025"
    )
    assert plan.search_mode == "search_spec"
    assert plan.elastic_query == ""
    assert plan.semantic_query == "Rechnungen von examplehost aus dem Jahr 2025"
    assert [group["kind"] for group in plan.lexical_groups] == [
        "entity", "concept", "constraint:years"
    ]
    frame = query_frame_from_search_spec(spec, intent="Rechnungen finden")
    assert frame["entities"][0]["text"] == "examplehost"
    assert frame["concepts"] == ["Rechnung"]
    assert frame["concept_groups"] == [
        {"required": True, "terms": ["Rechnung", "Invoice"]}
    ]
    assert frame["constraints"] == [{"kind": "Jahr", "value": "2025"}]


def test_search_spec_keeps_neo4j_alias_expansion_outside_llm_backend_syntax():
    spec = _examplehost_spec()
    context = {
        "entities": [{
            "mention": "examplehost",
            "status": "resolved",
            "entity_id": "org-1",
            "entity_type": "ORGANIZATION",
            "resolution_method": "exact",
        }],
        "elastic_phrase_expansion": [{
            "value": "ExampleHost GmbH",
            "weight": 1.0,
            "source": "alias",
            "kind": "alias",
            "entity_id": "org-1",
            "entity_type": "ORGANIZATION",
            "resolution": "exact",
            "original_mention": "examplehost",
        }],
    }
    plan = plan_from_search_spec(
        spec, context, original_query="Suche Rechnungen von examplehost aus dem Jahr 2025"
    )
    assert plan.elastic_query == ""
    assert any(item["value"] == "ExampleHost GmbH" for item in plan.entity_should_phrases)


def test_elasticsearch_compiler_executes_structured_intent(monkeypatch):
    captured = {}

    def fake_post(endpoint, *, json, timeout):
        captured["body"] = json
        return FakeResponse({"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}})

    monkeypatch.setattr(search, "_es_post", fake_post)
    spec = _examplehost_spec()
    plan = plan_from_search_spec(
        spec, {}, original_query="Suche Rechnungen von examplehost aus dem Jahr 2025"
    )
    search.elastic_search(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        plan,
        {},
    )
    query = captured["body"]["query"]["bool"]
    rendered = json.dumps(query, ensure_ascii=False)
    assert "Rechnung" in rendered
    assert "examplehost" in rendered
    assert "2025" in rendered
    assert len(query["must"]) == 3
    assert query["should"] == []
    assert query["must"][1]["bool"]["minimum_should_match"] == 1
    # The natural sentence belongs only to semantic/vector retrieval.
    assert "Suche Rechnungen von examplehost aus dem Jahr 2025" not in rendered


def test_query_rewriter_returns_backend_neutral_analysis_metadata(monkeypatch):
    captured = {}

    async def fake_complete(messages, *args, **kwargs):
        captured["messages"] = messages
        return json.dumps({
            "stop": False,
            "reason": "",
            "semantic_query": "Rechnungen von examplehost aus dem Jahr 2025",
            "entities": ["examplehost"],
            "concepts": ["Rechnung"],
            "concept_groups": [{"required": True, "terms": ["Rechnung", "Invoice"]}],
            "constraints": [{"kind": "Jahr", "value": "2025"}],
            "verification_requirements": ["Das Dokument ist selbst eine Rechnung von examplehost."],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Suche Rechnungen von examplehost aus dem Jahr 2025",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files"},
        seed_context={
            "entities": [{
                "mention": "examplehost",
                "status": "resolved",
                "display_name": "ExampleHost GmbH",
                "entity_type": "ORGANIZATION",
            }],
            "elastic_phrase_expansion": [{
                "value": "ExampleHost GmbH",
                "kind": "alias",
                "original_mention": "examplehost",
            }],
        },
    ))
    assert decision["valid"] is True
    assert decision["spec"]["elastic_query"] == ""
    assert decision["spec"]["concepts"] == ["Rechnung"]
    assert decision["spec"]["concept_groups"] == [
        {"required": True, "terms": ["Rechnung", "Invoice"]}
    ]
    assert decision["spec"]["verification_requirements"]
    assert "ExampleHost GmbH" in captured["messages"][1]["content"]


def test_followup_rewrite_restores_explicit_user_phrase(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "use_history": True,
            "standalone_query": "+Project +Alpha +42 Vertrag",
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    monkeypatch.setattr(provider, "QUERY_REWRITE_MODE", "followup")
    current = 'Und was ist mit (+"Project Alpha 42")?'
    rewritten, used_history = asyncio.run(provider._rewrite_query_with_context(
        [
            {"role": "user", "content": "Vorheriger Kontext"},
            {"role": "assistant", "content": "Vorherige Antwort"},
            {"role": "user", "content": current},
        ],
        current,
    ))

    assert used_history is True
    assert rewritten == 'Vertrag +"Project Alpha 42"'


def test_query_rewriter_preserves_explicit_user_phrase_as_entity_intent(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "semantic_query": "Project Alpha 42",
            "entities": ["Project Alpha 42"],
            "concepts": [],
            "concept_groups": [],
            "constraints": [],
            "verification_requirements": [],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question='"Project Alpha 42"',
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files"},
    ))

    assert decision["valid"] is True
    assert decision["spec"]["entities"] == ["Project Alpha 42"]
    assert decision["spec"]["elastic_query"] == ""


def test_query_rewriter_retries_concepts_without_concept_groups(monkeypatch):
    calls = {"count": 0}

    async def fake_complete(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps({
                "stop": False,
                "reason": "",
                "semantic_query": "Jane Doe CV",
                "entities": ["Jane Doe"],
                "concepts": ["CV"],
                "concept_groups": [],
                "constraints": [],
                "verification_requirements": ["The requested document is a CV."],
            })
        return json.dumps({
            "stop": False,
            "reason": "",
            "semantic_query": "Jane Doe CV",
            "entities": ["Jane Doe"],
            "concepts": ["CV"],
            "concept_groups": [{
                "required": True,
                "terms": ["CV", "résumé", "curriculum vitae", "professional background"],
            }],
            "constraints": [],
            "verification_requirements": ["The requested document is a CV."],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Jane Doe CV",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files"},
    ))

    assert calls["count"] == 2
    assert decision["valid"] is True
    assert decision["spec"]["concepts"] == ["CV"]
    assert decision["spec"]["concept_groups"][0]["required"] is True
    assert "CV" in decision["spec"]["concept_groups"][0]["terms"]


def test_query_rewriter_never_needs_lexical_terms_for_vector_only(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "semantic_query": "wo wird ein unberechtigter Firmenadressgebrauch beschrieben",
            "entities": [],
            "concepts": ["Firmenadresse"],
            "concept_groups": [],
            "constraints": [],
            "verification_requirements": [],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Wo geht es um die unberechtigte Nutzung einer Firmenadresse?",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"vector"},
    ))
    assert decision["valid"] is True
    assert decision["spec"]["elastic_query"] == ""


def test_search_spec_files_only_keeps_requested_bounded_candidate_window(monkeypatch):
    monkeypatch.setattr(search, "RERANKER_ENABLED", False)
    monkeypatch.setattr(search, "ELASTICSEARCH_ENABLED", True)
    monkeypatch.setattr(search, "RETRIEVAL_SIGNAL_ENABLED", False)
    monkeypatch.setattr(
        search,
        "prepare_entity_context",
        lambda question: (_ for _ in ()).throw(AssertionError("query context should be reused")),
    )

    def fake_es(question, plan, diagnostics):
        diagnostics.update({"total_hits": 30, "total_relation": "eq", "available": True})
        return [
            {
                "document_id": f"files:{i}",
                "title": f"invoice-{i}.pdf",
                "path": f"invoice-{i}.pdf",
                "score": float(100 - i),
                "text": f"unique invoice evidence {i}",
                "snippet": f"unique invoice evidence {i}",
            }
            for i in range(30)
        ]

    monkeypatch.setattr(search, "elastic_search", fake_es)
    payload = search.perform_search(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        limit=30,
        retrieval_arms={"files"},
        force_unspecific=True,
        search_spec=_examplehost_spec(),
        entity_context_override={"enabled": True, "entities": [], "elastic_phrase_expansion": [], "error": None},
    )

    assert payload["reranker_used"] is False
    assert len(payload["results"]) == 30
    assert payload["results"][0]["document_id"] == "files:0"
    assert payload["results"][-1]["document_id"] == "files:29"


def test_month_constraint_keeps_named_month_out_of_hard_compiler_terms():
    spec = normalize_search_spec({
        "semantic_query": "Rechnungen von examplehost aus dem Februar 2021",
        "entities": ["examplehost"],
        "concepts": ["Rechnung"],
        "concept_groups": [{"required": True, "terms": ["Rechnung", "Invoice"]}],
        "constraints": [{"kind": "Monat", "value": "Februar 2021"}],
        "verification_requirements": [
            "Das relevante Rechnungsdatum liegt im Februar 2021.",
        ],
    })
    plan = plan_from_search_spec(
        spec,
        {},
        original_query="Suche Rechnungen von examplehost aus dem Februar 2021",
    )

    constraint_groups = [
        group for group in plan.lexical_groups
        if str(group.get("kind") or "").startswith("constraint:")
    ]
    assert constraint_groups == [
        {"required": True, "kind": "constraint:years", "terms": ["2021"]}
    ]
    assert all("02" not in group["terms"] for group in plan.lexical_groups)


def test_query_rewriter_rejects_structured_semantic_query_value(monkeypatch):
    calls = {"count": 0}

    async def fake_complete(*args, **kwargs):
        calls["count"] += 1
        return json.dumps({
            "stop": False,
            "reason": "",
            "semantic_query": {"query": "Example Machinery AG"},
            "entities": ["Example Machinery AG"],
            "entity_hints": [{"text": "Example Machinery AG", "kind": "organization"}],
            "concepts": [],
            "concept_groups": [],
            "constraints": [],
            "verification_requirements": [],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Example Machinery AG",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files", "vector"},
        require_entity_hints=True,
    ))

    assert calls["count"] == 2
    assert decision["valid"] is False
    assert decision["spec"]["elastic_query"] == ""
    assert decision["spec"]["semantic_query"] == "Example Machinery AG"


def test_search_spec_normalization_never_stringifies_legacy_query_values():
    spec = normalize_search_spec({
        "elastic_query": {"query": "Example Machinery AG"},
        "semantic_query": ["Example Machinery AG"],
    }, original_query="Example Machinery AG")

    assert spec["elastic_query"] == ""
    assert spec["semantic_query"] == "Example Machinery AG"


def test_search_spec_response_schema_is_backend_neutral():
    properties = provider.SEARCH_SPEC_RESPONSE_SCHEMA["properties"]
    assert "elastic_query" not in properties
    assert properties["semantic_query"]["type"] == "string"
    assert "concept_groups" in properties
    assert "concept_groups" in provider.SEARCH_SPEC_RESPONSE_SCHEMA["required"]


def test_shipped_model_query_rewriters_forbid_backend_query_syntax():
    root = Path(__file__).resolve().parent.parent
    for model in ("standard", "thorough", "deep"):
        prompt = (
            root / "models" / model / "prompts" / "query_rewriter.txt"
        ).read_text(encoding="utf-8")
        assert "The planner describes intent; it does not own Elasticsearch syntax." in prompt
        assert "erzeugst keine Elasticsearch-, Nextcloud- oder andere Backend-Syntax" in prompt
        assert "MUST(OR(term1, term2, ...))" in prompt


def test_general_query_rewriter_prompt_keeps_planner_backend_neutral():
    prompt = (
        Path(__file__).resolve().parent.parent
        / "prompts" / "query_rewriter.txt"
    ).read_text(encoding="utf-8")
    assert "The planner describes intent; it does not own Elasticsearch syntax." in prompt
    assert "MUST(OR(term1, term2, ...))" in prompt
    assert "Jane Doe CV" in prompt
    assert "erzeugst keine Elasticsearch-, Nextcloud- oder andere Backend-Syntax" in prompt


def test_general_query_rewriter_prompt_preserves_unicode_orthography():
    prompt = (
        Path(__file__).resolve().parent.parent
        / "prompts" / "query_rewriter.txt"
    ).read_text(encoding="utf-8")
    assert "Bewahre die Orthografie" in prompt
    assert "`Geschäftsführer`" in prompt
    assert "`München`" in prompt
    assert "Geschaeftsfuehrer" not in prompt
    assert "Muenchen" not in prompt


def test_short_user_query_uses_structured_intent_in_first_round():
    rewritten = normalize_search_spec({
        "semantic_query": "Jane Doe CV",
        "entities": ["Jane Doe"],
        "concepts": ["CV"],
        "concept_groups": [{
            "required": True,
            "terms": ["CV", "résumé", "curriculum vitae", "professional background"],
        }],
        "verification_requirements": ["The requested document is a CV."],
    }, original_query="Jane Doe CV")

    assert provider._execution_search_spec(
        "Jane Doe CV",
        round_no=1,
        search_spec=rewritten,
    ) == rewritten
    assert provider._execution_search_spec(
        "Jane Doe CV",
        round_no=2,
        search_spec=rewritten,
    ) == rewritten


def test_jane_doe_cv_compiles_entity_and_required_alternative_group(monkeypatch):
    captured = {}

    def fake_post(endpoint, *, json, timeout):
        captured["body"] = json
        return FakeResponse({"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}})

    monkeypatch.setattr(search, "_es_post", fake_post)
    spec = normalize_search_spec({
        "semantic_query": "Jane Doe CV",
        "entities": ["Jane Doe"],
        "concepts": ["CV"],
        "concept_groups": [{
            "required": True,
            "terms": ["CV", "résumé", "curriculum vitae", "professional background"],
        }],
        "verification_requirements": ["The requested document is a CV."],
    })
    plan = plan_from_search_spec(
        spec, {}, original_query="Jane Doe CV"
    )
    search.elastic_search("Jane Doe CV", plan, {})

    query = captured["body"]["query"]["bool"]
    rendered = json.dumps(query, ensure_ascii=False)
    assert plan.elastic_query == ""
    assert len(query["must"]) == 2
    assert query["should"] == []
    assert "Jane Doe" in rendered
    assert "CV" in rendered
    assert "résumé" in rendered
    assert "curriculum vitae" in rendered
    assert "professional background" in rendered
    assert query["must"][1]["bool"]["minimum_should_match"] == 1


def test_longer_query_also_executes_structured_search_spec_in_first_round():
    rewritten = _examplehost_spec()
    assert provider._execution_search_spec(
        "Suche Rechnungen examplehost 2025",
        round_no=1,
        search_spec=rewritten,
    ) == rewritten
