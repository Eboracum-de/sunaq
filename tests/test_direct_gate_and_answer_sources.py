import asyncio

from fastapi import BackgroundTasks

from starlette.requests import Request

import rag.openai_provider as provider


def _request(headers=None):
    raw = []
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode("latin1"), value.encode("latin1")))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": raw,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8766),
        "scheme": "http",
        "query_string": b"",
    })


def _result(index: int, title: str) -> provider.SearchResult:
    return provider.SearchResult(
        index=index,
        title=title,
        text=f"Text aus {title}",
        raw={"path": title, "document_id": f"files:{index}"},
    )


def test_source_suffix_lists_only_explicitly_cited_documents():
    results = [_result(1, "A.pdf"), _result(2, "B.pdf"), _result(3, "C.pdf")]
    rendered = provider._source_suffix("Aussage [2].", results)
    assert "B.pdf" in rendered
    assert "A.pdf" not in rendered
    assert "C.pdf" not in rendered


def test_source_suffix_without_citation_is_empty_even_with_candidates():
    results = [_result(1, "A.pdf"), _result(2, "B.pdf")]
    assert provider._source_suffix("Antwort ohne Quellenmarker.", results) == ""


def test_explicit_document_wording_is_accepted_as_citation_fallback():
    results = [_result(5, "E.pdf"), _result(6, "F.pdf"), _result(7, "G.pdf"), _result(8, "H.pdf")]
    answer = 'Die Adresse wird explizit genannt (Dokumente 5, 6 und 7).'
    cited = provider._cited_results(answer, results)
    assert [result.index for result in cited] == [5, 6, 7]
    rendered = provider._source_suffix(answer, results)
    assert "E.pdf" in rendered
    assert "F.pdf" in rendered
    assert "G.pdf" in rendered
    assert "H.pdf" not in rendered


def test_bare_numbers_do_not_become_sources():
    results = [_result(5, "E.pdf"), _result(6, "F.pdf"), _result(7, "G.pdf")]
    answer = 'Die Adresse lautet Musterstraße 9, 12345 Musterstadt.'
    assert provider._cited_results(answer, results) == []
    assert provider._source_suffix(answer, results) == ""


def test_direct_gate_is_deliberately_narrow():
    assert provider._direct_query_kind("Hallo") == "conversation"
    assert provider._direct_query_kind("Wie spät ist es?") == "time"
    assert provider._direct_query_kind("Welcher Tag ist heute?") == "date"
    assert provider._direct_query_kind("2 + 2") == "arithmetic"

    # These may have useful private-document evidence and therefore remain RAG.
    assert provider._direct_query_kind("Wer ist Frank Muster?") is None
    assert provider._direct_query_kind("Was steht über die Greenwich AG in den Unterlagen?") is None
    assert provider._direct_query_kind("Wie spät war es laut dem Protokoll?") is None
    assert provider._direct_query_kind("Was ist eine GmbH?") is None


def test_context_reset_guards_history_rewrite():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    # Both natural-control and ordinary retrieval paths must honor the already
    # resolved reset boundary before any history-aware rewrite can run.
    assert source.count("and not context_reset") >= 2


def test_greeting_does_not_call_retrieval_or_query_rewrite(monkeypatch):
    monkeypatch.setattr(provider, "_check_auth", lambda authorization: "test-client")

    async def connected(user_id):
        return {"status": "connected"}

    async def forbidden(*args, **kwargs):
        raise AssertionError("direct request must not run retrieval/query rewrite")

    async def answer(messages, *args, **kwargs):
        assert "ohne Dokumentrecherche" in messages[0]["content"]
        assert messages[-1]["content"] == "Hallo"
        return "Hallo!"

    monkeypatch.setattr(provider, "_ensure_nextcloud_binding", connected)
    monkeypatch.setattr(provider, "_rewrite_query_if_needed", forbidden)
    monkeypatch.setattr(provider, "_rag_search", forbidden)
    monkeypatch.setattr(provider, "_ollama_complete", answer)
    monkeypatch.setattr(provider, "_research_call", lambda *args, **kwargs: None)

    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hallo"}],
        stream=False,
    )
    response = asyncio.run(
        provider.chat_completions(
            body,
            _request({"x-openwebui-user-id": "user-1"}),
            BackgroundTasks(),
            authorization="Bearer ignored",
        )
    )
    assert response["choices"][0]["message"]["content"] == "Hallo!"


def test_passive_source_scopes_do_not_disable_direct_gate(monkeypatch):
    monkeypatch.setattr(provider, "_check_auth", lambda authorization: "test-client")

    async def connected(user_id):
        return {"status": "connected"}

    async def forbidden(*args, **kwargs):
        raise AssertionError("passive source scopes must not force retrieval/query rewrite")

    async def answer(messages, *args, **kwargs):
        assert messages[-1]["content"] == "Hallo"
        return "Hallo!"

    monkeypatch.setattr(provider, "_ensure_nextcloud_binding", connected)
    monkeypatch.setattr(provider, "_rewrite_query_if_needed", forbidden)
    monkeypatch.setattr(provider, "_rag_search", forbidden)
    monkeypatch.setattr(provider, "_ollama_complete", answer)
    monkeypatch.setattr(provider, "_research_call", lambda *args, **kwargs: None)

    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "/documents /mailarchive Hallo"}],
        stream=False,
    )
    response = asyncio.run(
        provider.chat_completions(
            body,
            _request({"x-openwebui-user-id": "user-1"}),
            BackgroundTasks(),
            authorization="Bearer ignored",
        )
    )
    assert response["choices"][0]["message"]["content"] == "Hallo!"


def test_time_question_gets_runtime_context_without_retrieval(monkeypatch):
    monkeypatch.setattr(provider, "_check_auth", lambda authorization: "test-client")

    async def connected(user_id):
        return {"status": "connected"}

    async def forbidden(*args, **kwargs):
        raise AssertionError("time request must not run retrieval")

    async def answer(messages, *args, **kwargs):
        assert "Aktuelle lokale Serverzeit:" in messages[0]["content"]
        assert "Direktmodus: time" in messages[0]["content"]
        return "Es ist 12:34 Uhr."

    monkeypatch.setattr(provider, "_ensure_nextcloud_binding", connected)
    monkeypatch.setattr(provider, "_rag_search", forbidden)
    monkeypatch.setattr(provider, "_ollama_complete", answer)
    monkeypatch.setattr(provider, "_research_call", lambda *args, **kwargs: None)

    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Wie spät ist es?"}],
        stream=False,
    )
    response = asyncio.run(
        provider.chat_completions(
            body,
            _request({"x-openwebui-user-id": "user-1"}),
            BackgroundTasks(),
            authorization="Bearer ignored",
        )
    )
    content = response["choices"][0]["message"]["content"]
    assert content == "Es ist 12:34 Uhr."
    assert "**Quellen:**" not in content


def test_conversation_id_prefers_sunaq_header_over_openwebui_header():
    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hallo"}],
        stream=False,
        user="user-identity",
    )
    request = _request({
        "x-rag-conversation-id": "sunaq-chat-1",
        "x-openwebui-chat-id": "openwebui-chat-2",
    })
    assert provider._conversation_id(request, body) == "sunaq-chat-1"


def test_conversation_id_accepts_native_openwebui_header():
    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hallo"}],
        stream=False,
        user="user-identity",
    )
    request = _request({"x-openwebui-chat-id": "550e8400-e29b-41d4-a716-446655440000"})
    assert provider._conversation_id(request, body) == "550e8400-e29b-41d4-a716-446655440000"


def test_conversation_id_does_not_reuse_openai_user_field():
    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Hallo"}],
        stream=False,
        user="user-identity",
    )
    assert provider._conversation_id(_request(), body) is None
