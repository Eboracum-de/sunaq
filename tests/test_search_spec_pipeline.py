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
        "elastic_query": "+examplehost +2025 +Rechnung",
        "semantic_query": "Rechnungen von examplehost aus dem Jahr 2025",
        "entities": ["examplehost"],
        "concepts": ["Rechnung"],
        "constraints": [{"kind": "Jahr", "value": "2025"}],
        "verification_requirements": [
            "Das Dokument ist selbst eine Rechnung von examplehost.",
            "Das relevante Jahr ist 2025.",
        ],
    })


def test_search_spec_builds_expected_plan_and_query_frame():
    spec = _examplehost_spec()
    plan = plan_from_search_spec(spec, {})
    assert plan.search_mode == "search_spec"
    assert plan.elastic_query == "+examplehost +2025 +Rechnung"
    assert plan.semantic_query == "Rechnungen von examplehost aus dem Jahr 2025"
    frame = query_frame_from_search_spec(spec, intent="Rechnungen finden")
    assert frame["entities"][0]["text"] == "examplehost"
    assert frame["concepts"] == ["Rechnung"]
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
    plan = plan_from_search_spec(spec, context)
    assert plan.elastic_query == "+examplehost +2025 +Rechnung"
    assert any(item["value"] == "ExampleHost GmbH" for item in plan.entity_should_phrases)


def test_elasticsearch_compiler_executes_llm_nextcloud_query(monkeypatch):
    captured = {}

    def fake_post(endpoint, *, json, timeout):
        captured["body"] = json
        return FakeResponse({"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}})

    monkeypatch.setattr(search, "_es_post", fake_post)
    spec = _examplehost_spec()
    plan = plan_from_search_spec(spec, {})
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
    # The natural sentence belongs only to semantic/vector retrieval.
    assert "Suche Rechnungen von examplehost aus dem Jahr 2025" not in rendered


def test_query_rewriter_accepts_llm_authored_elastic_query_and_analysis_metadata(monkeypatch):
    captured = {}

    async def fake_complete(messages, *args, **kwargs):
        captured["messages"] = messages
        return json.dumps({
            "stop": False,
            "reason": "",
            "elastic_query": "+examplehost +2025 +Rechnung",
            "semantic_query": "Rechnungen von examplehost aus dem Jahr 2025",
            "entities": ["examplehost"],
            "concepts": ["Rechnung"],
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
    assert decision["spec"]["elastic_query"] == "+examplehost +2025 +Rechnung"
    assert decision["spec"]["concepts"] == ["Rechnung"]
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


def test_query_rewriter_preserves_explicit_user_phrase(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "elastic_query": "+Project +Alpha 42",
            "semantic_query": "Project Alpha 42",
            "entities": ["Project Alpha"],
            "concepts": [],
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
    assert decision["spec"]["elastic_query"] == '"Project Alpha 42"'


def test_query_rewriter_never_needs_lexical_terms_for_vector_only(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "elastic_query": "",
            "semantic_query": "wo wird ein unberechtigter Firmenadressgebrauch beschrieben",
            "entities": [],
            "concepts": ["Firmenadresse"],
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


def test_query_rewriter_month_constraint_can_keep_month_out_of_must_syntax(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "elastic_query": "+examplehost +2021 +Rechnung Februar",
            "semantic_query": "Rechnungen von examplehost aus dem Februar 2021",
            "entities": ["examplehost"],
            "concepts": ["Rechnung"],
            "constraints": [{"kind": "Monat", "value": "Februar 2021"}],
            "verification_requirements": [
                "Das Dokument ist selbst eine Rechnung von examplehost.",
                "Das relevante Rechnungsdatum liegt im Februar 2021.",
            ],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Suche Rechnungen von examplehost aus dem Februar 2021",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files"},
    ))
    assert decision["valid"] is True
    assert decision["spec"]["elastic_query"] == "+examplehost +2021 +Rechnung Februar"
    assert "02-2021" not in decision["spec"]["elastic_query"]
    assert decision["spec"]["constraints"] == [{"kind": "Monat", "value": "Februar 2021"}]


def test_query_rewriter_removes_generated_numeric_month_must(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "stop": False,
            "reason": "",
            "elastic_query": "+Rechnung +examplehost +2023 +02",
            "semantic_query": "Rechnungen von examplehost aus dem Februar 2023",
            "entities": ["examplehost"],
            "concepts": ["Rechnung"],
            "constraints": [
                {"kind": "year", "value": "2023"},
                {"kind": "month", "value": "02"},
            ],
            "verification_requirements": [
                "Das Dokument ist selbst eine Rechnung von examplehost.",
                "Das relevante Rechnungsdatum liegt im Februar 2023.",
            ],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    decision = asyncio.run(provider._rewrite_search_spec(
        question="Suche Rechnungen von examplehost aus dem Februar 2023",
        round_no=1,
        results=[],
        previous_spec=None,
        retrieval_arms={"files"},
    ))
    assert decision["valid"] is True
    assert decision["spec"]["elastic_query"] == "+Rechnung +examplehost +2023 Februar"
    assert "+02" not in decision["spec"]["elastic_query"]
    assert decision["spec"]["constraints"][-1] == {"kind": "month", "value": "02"}


def test_numeric_only_month_query_is_not_rewritten_by_named_month_guard():
    query = "+Rechnung +examplehost +2023 +02"
    assert provider._guard_generated_numeric_month_must(
        query,
        original_query="Suche Rechnungen von examplehost aus 02/2023",
    ) == query
