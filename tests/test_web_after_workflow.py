import asyncio
import json

import rag.openai_provider as provider


def test_mixed_ui_web_query_uses_structured_entities_not_full_work_instruction():
    query = provider._web_query_from_search_spec(
        {
            "entities": ["Example Culture+Product GmbH", "Nicholas Example"],
            "concepts": ["Verbindungen"],
            "semantic_query": "Example Culture Nicholas Example Verbindungen",
        },
        "Such in den internen Dokumenten nach Example Culture und Nicholas Example und recherchiere anschliessend im Web",
    )
    assert query == "Example Culture+Product GmbH Nicholas Example Verbindungen"
    assert "internen Dokumenten" not in query
    assert "recherchiere" not in query


def test_mixed_ui_web_query_falls_back_to_semantic_query_without_entities():
    query = provider._web_query_from_search_spec(
        {"entities": [], "concepts": [], "semantic_query": "Example Culture Nicholas Example"},
        "search internal documents and then the web",
    )
    assert query == "Example Culture Nicholas Example"


def test_web_only_query_compiler_removes_unavailable_internal_workflow(monkeypatch):
    captured = {}

    async def fake_complete(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps({
            "mode": "focused",
            "queries": ["Example Culture+Product GmbH Nicholas Example"],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    queries = asyncio.run(provider._derive_web_queries(
        question="Such in den internen Dokumenten nach Example Culture und Nicholas Example und recherchiere anschliessend im Web",
        fallback_query="Such in den internen Dokumenten nach Example Culture und Nicholas Example und recherchiere anschliessend im Web",
    ))
    assert queries == ["Example Culture+Product GmbH Nicholas Example"]
    assert captured["kwargs"]["response_format"] is provider.WEB_QUERY_RESPONSE_SCHEMA
    assert "internen Dokumenten" in captured["messages"][1]["content"]


def test_web_only_exploratory_query_contract_repairs_single_query_once(monkeypatch):
    calls = []

    async def fake_complete(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        if len(calls) == 1:
            return json.dumps({
                "mode": "exploratory",
                "queries": ['"Robert Alexander Wilke"'],
            })
        return json.dumps({
            "mode": "exploratory",
            "queries": [
                '"Robert Alexander Wilke"',
                '"Robert Alexander Wilke" Karriere OR Unternehmen',
                '"Robert Alexander Wilke" Kontakt OR Biografie',
            ],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    monkeypatch.setattr(provider, "WEB_AFTER_MAX_QUERIES", 3)

    queries = asyncio.run(provider._derive_web_queries(
        question="Findest Du weitere Informationen zu Robert Alexander Wilke?",
        fallback_query='"Robert Alexander Wilke"',
    ))

    assert queries == [
        '"Robert Alexander Wilke"',
        '"Robert Alexander Wilke" Karriere OR Unternehmen',
        '"Robert Alexander Wilke" Kontakt OR Biografie',
    ]
    assert len(calls) == 2
    assert calls[0]["kwargs"]["response_format"] is provider.WEB_QUERY_RESPONSE_SCHEMA
    repair_schema = calls[1]["kwargs"]["response_format"]
    assert repair_schema["properties"]["mode"]["enum"] == ["exploratory"]
    assert repair_schema["properties"]["queries"]["minItems"] == 3
    assert repair_schema["properties"]["queries"]["maxItems"] == 3
    assert "VERTRAGS-HINWEIS" in calls[1]["messages"][1]["content"]
    assert "genau 3" in calls[1]["messages"][1]["content"]


def test_web_only_focused_query_does_not_force_fanout(monkeypatch):
    calls = []

    async def fake_complete(messages, **kwargs):
        calls.append(messages)
        return json.dumps({
            "mode": "focused",
            "queries": ['"Beispiel GmbH" Gründungsdatum'],
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    monkeypatch.setattr(provider, "WEB_AFTER_MAX_QUERIES", 3)

    queries = asyncio.run(provider._derive_web_queries(
        question="Wann wurde die Beispiel GmbH gegründet?",
        fallback_query='"Beispiel GmbH" Gründungsdatum',
    ))

    assert queries == ['"Beispiel GmbH" Gründungsdatum']
    assert len(calls) == 1


def test_native_web_query_prompt_defines_exploratory_example_and_three_query_contract():
    prompt = provider.WEB_QUERY_SYSTEM_PROMPT
    assert 'mode="exploratory"' in prompt
    assert "Findest Du weitere Informationen zu <Person>?" in prompt
    assert "genau drei" in prompt


def test_web_only_answer_prompt_states_internal_sources_were_not_searched():
    messages = provider._web_answer_messages(
        "Suche intern und im Web nach Beispiel",
        "WEB",
        source_scope_note="Nur Web; interne Dokumente wurden nicht durchsucht.",
    )
    assert "QUELLENUMFANG" in messages[1]["content"]
    assert "interne Dokumente wurden nicht durchsucht" in messages[1]["content"]


def test_after_web_query_normalization_deduplicates_and_caps(monkeypatch):
    monkeypatch.setattr(provider, "WEB_AFTER_MAX_QUERIES", 3)
    value = {
        "queries": [
            "Cornelius Muster Berlin",
            " cornelius   muster berlin ",
            '"Cornelius Muster" "R.A.W. Capital GmbH"',
            '"Cornelius Muster" "Musterhof 280 VV UG"',
            "ignored fourth query",
        ]
    }
    queries = provider._normalize_after_web_queries(value, "fallback")
    assert queries == [
        "Cornelius Muster Berlin",
        '"Cornelius Muster" "R.A.W. Capital GmbH"',
        '"Cornelius Muster" "Musterhof 280 VV UG"',
    ]


def test_after_web_query_derivation_sees_internal_evidence_and_uses_closed_schema(monkeypatch):
    captured = {}

    async def fake_complete(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps({
            "queries": [
                "Cornelius Muster Rechtsanwaltsgesellschaft mbH & Co. KG Berlin",
                '"Cornelius Muster" "R.A.W. Capital GmbH"',
            ]
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    queries = asyncio.run(provider._derive_after_web_queries(
        instruction="Nutze das erste Dokument und suche anschliessend im Web nach weiteren Informationen",
        question="Analysiere und suche dann im Web nach erkannten Sachverhalten",
        initial_web_query="weitere Informationen",
        internal_context=(
            "[1] DATEI: Schreiben.pdf\n"
            "Cornelius Muster Rechtsanwaltsgesellschaft mbH & Co. KG vertritt die R.A.W. Capital GmbH."
        ),
    ))
    assert queries[0].startswith("Cornelius Muster Rechtsanwaltsgesellschaft")
    assert captured["kwargs"]["response_format"] is provider.WEB_AFTER_QUERY_RESPONSE_SCHEMA
    prompt = captured["messages"][1]["content"]
    assert "Cornelius Muster Rechtsanwaltsgesellschaft" in prompt
    assert "erkannten Sachverhalten" in prompt


def test_web_search_many_sends_one_logical_research_request(monkeypatch):
    captured = []

    async def fake_search(
        question,
        user_id,
        request_id=None,
        research_run_path="",
        *,
        research_goal="",
        queries=None,
    ):
        captured.append({
            "question": question,
            "user_id": user_id,
            "request_id": request_id,
            "research_run_path": research_run_path,
            "research_goal": research_goal,
            "queries": list(queries or []),
        })
        return {
            "enabled": True,
            "research_goal": research_goal,
            "queries": list(queries or []),
            "searched": 9,
            "unique_hits": 7,
            "fetched": 5,
            "sources": [],
        }

    monkeypatch.setattr(provider, "_web_search", fake_search)
    payload = asyncio.run(
        provider._web_search_many(
            ["first query", "second query", "third query"],
            "u",
            "r",
            "SunaQ-Chats/chat/runs/run",
            research_goal="stable research goal",
        )
    )

    assert len(captured) == 1
    assert captured[0] == {
        "question": "first query",
        "user_id": "u",
        "request_id": "r",
        "research_run_path": "SunaQ-Chats/chat/runs/run",
        "research_goal": "stable research goal",
        "queries": ["first query", "second query", "third query"],
    }
    assert payload["searched"] == 9
    assert payload["unique_hits"] == 7


def test_web_search_many_deduplicates_and_caps_probes_before_retriever(monkeypatch):
    captured = []

    async def fake_search(
        question,
        user_id,
        request_id=None,
        research_run_path="",
        *,
        research_goal="",
        queries=None,
    ):
        captured.append(list(queries or []))
        return {"enabled": True, "queries": list(queries or []), "sources": []}

    monkeypatch.setattr(provider, "_web_search", fake_search)
    monkeypatch.setattr(provider, "WEB_AFTER_MAX_QUERIES", 3)

    asyncio.run(
        provider._web_search_many(
            ["one", " ONE ", "two", "three", "four"],
            "u",
            research_goal="goal",
        )
    )

    assert captured == [["one", "two", "three"]]


def test_web_search_many_preserves_retriever_disabled_state(monkeypatch):
    async def fake_search(
        question,
        user_id,
        request_id=None,
        research_run_path="",
        *,
        research_goal="",
        queries=None,
    ):
        return {
            "enabled": False,
            "research_goal": research_goal,
            "queries": list(queries or []),
            "sources": [],
            "searched": 0,
            "fetched": 0,
        }

    monkeypatch.setattr(provider, "_web_search", fake_search)
    payload = asyncio.run(
        provider._web_search_many(["one", "two"], "u", research_goal="goal")
    )
    assert payload["enabled"] is False
    assert payload["queries"] == ["one", "two"]


def test_hybrid_prompt_does_not_treat_missing_web_evidence_as_fiction():
    prompt = provider.HYBRID_WEB_ANSWER_SYSTEM_PROMPT
    assert "KEIN Beleg" in prompt
    assert "fiktiv" in prompt
    assert "Loese den Konflikt nicht spekulativ" in prompt


def test_native_web_only_streams_answer_model_when_client_requests_streaming():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    start = source.index("if (not auxiliary) and web_only:")
    end = source.index("auto_web_payload:", start)
    block = source[start:end]

    assert "if body.stream:" in block
    assert "async def web_answer_stream()" in block
    assert "async for event in _ollama_stream(" in block
    assert 'return StreamingResponse(web_answer_stream(), media_type="text/event-stream")' in block
    assert "web_answer_model_ms=%.1f stream=true" in block
    assert "web_answer_model_ms=%.1f stream=false" in block


def test_web_only_path_logs_fetch_diagnostics_without_auto_broadening():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    start = source.index("if (not auxiliary) and web_only:")
    end = source.index("auto_web_payload:", start)
    block = source[start:end]

    assert "Web fetch failures=%d sample=%s" in block
    assert "_web_recovery_queries" not in block
    assert "_merge_web_payloads" not in block
    assert "needs_recovery" not in block


def test_web_query_repair_schema_respects_configured_lower_cap(monkeypatch):
    monkeypatch.setattr(provider, "WEB_AFTER_MAX_QUERIES", 2)
    schema = provider._web_query_repair_schema(3)
    assert schema["properties"]["queries"]["minItems"] == 2
    assert schema["properties"]["queries"]["maxItems"] == 2
