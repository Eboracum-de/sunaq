from fastapi import BackgroundTasks
from starlette.requests import Request

from rag.openai_provider import (
    ChatCompletionRequest,
    SearchResult,
    _auxiliary_task_kind,
    _merge_followup_evidence,
    _previous_source_map,
    _previous_supporting_document_ids,
    _request_allows_web,
    _source_suffix,
)


def _request(headers=None):
    raw = []
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode("latin1"), value.encode("latin1")))
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": raw})


def test_source_capabilities_follow_global_and_per_user_service_gates(monkeypatch, tmp_path):
    import rag.openai_provider as provider
    from rag.credential_store import CredentialStore, scope_identity

    store = CredentialStore(tmp_path / "users.sqlite")
    identity = scope_identity("frontend-a", "alice-ext")
    user = store.bind_identity(identity, "https://cloud.example", "alice")
    store.set_chat_settings(
        user.canonical_user_id,
        enabled=True,
        target_path="SunaQ-Chats",
    )
    store.set_web_settings(
        user.canonical_user_id,
        enabled=True,
        archive_enabled=True,
        target_path="Research/Web",
    )
    store.save_mail_account(
        user.canonical_user_id,
        name="primary",
        host="imap.example",
        username="alice@example.org",
        password="secret",
        enabled=True,
        target_path="Mailarchiv",
    )

    monkeypatch.setattr(provider, "_PROVIDER_CLIENT_STORE", store)
    monkeypatch.setattr(provider, "CHAT_ARCHIVE_ENABLED", True)
    monkeypatch.setitem(provider.PROVIDER_CONFIG, "mail", {"enabled": True})
    monkeypatch.setattr(provider, "_provider_web_config", lambda: {
        "enabled": True,
        "archive": {"enabled": True},
    })

    assert provider._source_capabilities_for_identity(identity) == {
        "documents": True,
        "mailarchive": True,
        "chatarchive": True,
        "web": True,
    }

    monkeypatch.setitem(provider.PROVIDER_CONFIG, "mail", {"enabled": False})
    monkeypatch.setattr(provider, "_provider_web_config", lambda: {
        "enabled": False,
        "archive": {"enabled": False},
    })
    store.set_chat_settings(
        user.canonical_user_id,
        enabled=False,
        target_path="SunaQ-Chats",
    )

    assert provider._source_capabilities_for_identity(identity) == {
        "documents": True,
        "mailarchive": False,
        "chatarchive": False,
        "web": False,
    }


def test_delegated_web_engine_satisfies_web_capability_without_native_engine(monkeypatch, tmp_path):
    import rag.openai_provider as provider
    from rag.credential_store import CredentialStore, scope_identity
    from rag.research.registry import DelegatedResearchSettings

    store = CredentialStore(tmp_path / "users.sqlite")
    identity = scope_identity("frontend-a", "alice-ext")
    user = store.bind_identity(identity, "https://cloud.example", "alice")
    store.set_web_settings(
        user.canonical_user_id,
        enabled=True,
        archive_enabled=False,
        target_path="Research/Web",
    )

    monkeypatch.setattr(provider, "_PROVIDER_CLIENT_STORE", store)
    monkeypatch.setattr(provider, "_provider_web_config", lambda: {"enabled": False})
    monkeypatch.setattr(
        provider,
        "DELEGATED_RESEARCH_SETTINGS",
        DelegatedResearchSettings(
            enabled=True,
            provider="openai",
            delegate_web_only=True,
            model="gpt-test",
        ),
    )

    capabilities = provider._source_capabilities_for_identity(identity)
    assert capabilities["web"] is True


def test_disabled_requested_sources_are_rejected_as_server_policy(monkeypatch):
    import rag.openai_provider as provider

    monkeypatch.setattr(provider, "_source_capabilities_for_identity", lambda identity: {
        "documents": True,
        "mailarchive": False,
        "chatarchive": False,
        "web": False,
    })
    assert provider._disabled_requested_sources(
        {"documents", "mailarchive", "chatarchive"},
        web_requested=True,
        scoped_user_id="frontend::alice",
    ) == ["chatarchive", "mailarchive", "web"]


def test_ui_search_web_function_does_not_grant_capability():
    body = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Wer ist heute Vorstand?"}],
        tools=[{"type": "function", "function": {"name": "search_web", "parameters": {"type": "object"}}}],
    )
    assert _request_allows_web(body, _request()) is False


def test_native_web_search_tool_does_not_grant_capability():
    body = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Test"}],
        tools=[{"type": "web_search"}],
    )
    assert _request_allows_web(body, _request()) is False


def test_unknown_tool_does_not_grant_web_capability():
    body = ChatCompletionRequest(
        messages=[{"role": "user", "content": "Test"}],
        tools=[{"type": "function", "function": {"name": "calculator"}}],
    )
    assert _request_allows_web(body, _request()) is False


def test_neutral_header_can_grant_web_capability():
    body = ChatCompletionRequest(messages=[{"role": "user", "content": "Test"}])
    assert _request_allows_web(body, _request({"x-rag-web-allowed": "true"})) is True


def test_web_query_generation_helper_is_auxiliary():
    prompt = '''### Task:\nAnalyze the chat history to determine the necessity of generating search queries.\nReturn JSON with a "queries" array.\n\nChat History:\nUSER: Frank Muster'''
    assert _auxiliary_task_kind(_request(), prompt) == "ui:web_query_generation"


def test_llm_reference_resolution_handles_elliptic_followup(monkeypatch):
    import asyncio
    import json
    import rag.openai_provider as provider

    messages = [
        {"role": "user", "content": "Welche Beziehung besteht zwischen RAE und V280?"},
        {
            "role": "assistant",
            "content": "RAE ist Gesellschafterin der V280. [1]\n\n"
                       "**Quellen:**\n- [1] Gesellschafterbeschluss",
        },
        {"role": "user", "content": "Wer sind die anderen Gesellschafter?"},
    ]

    async def fake_complete(messages, **kwargs):
        assert kwargs["response_format"] == provider.FOLLOWUP_REWRITE_RESPONSE_SCHEMA
        prompt = messages[-1]["content"]
        assert "Wer sind die anderen Gesellschafter?" in prompt
        assert "RAE ist Gesellschafterin der V280" in prompt
        return json.dumps({
            "use_history": True,
            "standalone_query": "Wer sind die anderen Gesellschafter der V280?",
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    query, used = asyncio.run(
        provider._rewrite_query_with_context(
            messages,
            "Wer sind die anderen Gesellschafter?",
        )
    )
    assert used is True
    assert query == "Wer sind die anderen Gesellschafter der V280?"


def test_llm_reference_resolution_keeps_new_topic_exact(monkeypatch):
    import asyncio
    import json
    import rag.openai_provider as provider

    question = "Frank Muster"
    messages = [
        {"role": "user", "content": "Welche Beziehung besteht zwischen RAE und V280?"},
        {"role": "assistant", "content": "Antwort zur V280."},
        {"role": "user", "content": question},
    ]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "use_history": False,
            "standalone_query": "Frank Muster V280",
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    query, used = asyncio.run(
        provider._rewrite_query_with_context(messages, question)
    )
    assert used is False
    assert query == question


def test_followup_prompt_is_language_neutral():
    import rag.openai_provider as provider

    prompt = provider.FOLLOWUP_REWRITE_SYSTEM_PROMPT
    assert "any language" in prompt
    assert "Preserve the user's language" in prompt
    assert "the others" in prompt


def test_previous_supporting_documents_prefer_cited_sources():
    messages = [
        {"role": "user", "content": "Frage"},
        {
            "role": "assistant",
            "content": (
                "Antwort [2] und danach [1].\n\n"
                "**Quellen:**\n"
                "- [1] A\n- [2] B\n- [3] C\n"
                "<!--rag-source:1:files:101-->"
                "<!--rag-source:2:files:202-->"
                "<!--rag-source:3:files:303-->"
            ),
        },
        {"role": "user", "content": "Folgefrage"},
    ]
    assert _previous_supporting_document_ids(messages, limit=3) == [
        "files:202", "files:101", "files:303"
    ]


def test_followup_evidence_merge_prioritizes_prior_and_deduplicates():
    prior = [
        SearchResult(index=1, title="Beschluss.pdf", text="A", raw={
            "document_id": "files:101", "_followup_evidence": True
        })
    ]
    ranked = [
        SearchResult(index=1, title="Beschluss.pdf", text="A", raw={
            "document_id": "files:101"
        }),
        SearchResult(index=2, title="Andere.pdf", text="B", raw={
            "document_id": "files:202"
        }),
    ]
    merged = _merge_followup_evidence(prior, ranked)
    assert [item.raw["document_id"] for item in merged] == ["files:101", "files:202"]
    assert [item.index for item in merged] == [1, 2]
    assert merged[0].raw["_followup_evidence"] is True


def test_followup_evidence_respects_current_source_scope(monkeypatch):
    import asyncio
    import rag.openai_provider as provider

    messages = [
        {"role": "assistant", "content": (
            "Antwort [1] [2]\n\n**Quellen:**\n"
            "<!--rag-source:1:files:101-->"
            "<!--rag-source:2:files:202-->"
        )},
        {"role": "user", "content": "Who are the other shareholders?"},
    ]

    async def fake_resolve(question, references, user_id, user_groups, request_id=None):
        assert references == ["files:101", "files:202"]
        return {}, [
            SearchResult(index=1, title="A.pdf", text="A", raw={
                "document_id": "files:101",
                "path": "Dokumente/A.pdf",
                "source_origin": "internal",
            }),
            SearchResult(index=2, title="Chat.html", text="B", raw={
                "document_id": "files:202",
                "path": "AKI-Chats/Chat.html",
                "source_origin": "chat_archive",
            }),
        ]

    monkeypatch.setattr(provider, "_rag_resolve_documents", fake_resolve)
    resolved = asyncio.run(provider._resolve_followup_evidence(
        messages,
        query="Who are the other shareholders of V280?",
        user_id="u1",
        user_groups="g1",
        source_scopes={"documents"},
        request_id="r1",
    ))
    assert [item.raw["document_id"] for item in resolved] == ["files:101"]
    assert resolved[0].raw["_followup_evidence"] is True


def test_normal_source_suffix_contains_snippet():
    result = SearchResult(
        index=1,
        title="Test.pdf",
        text="Fallback text",
        raw={
            "path": "Ordner/Test.pdf",
            "document_id": "files:123",
            "vector_snippet": "Hier steht Greenwich AG als relevante Passage im Dokument.",
        },
    )
    rendered = _source_suffix("Antwort [1]", [result], query="Greenwich AG")
    assert "**Quellen:**" in rendered
    assert "> " in rendered
    assert "**Greenwich**" in rendered


def test_internal_sources_remain_resolvable_in_mixed_answer():
    messages = [
        {"role": "user", "content": "Frage"},
        {
            "role": "assistant",
            "content": (
                "Antwort\n\n**Interne Quellen:**\n"
                "- [1] [Test.pdf](https://cloud.example/index.php/f/123?openfile=123)\n"
                "  > Passage\n\n"
                "**Öffentliche Quellen:**\n"
                "- [W1] [Web](https://example.org)\n"
                "  Archiv: `Webarchiv/2026-08/x/01.txt`\n"
            ),
        },
        {"role": "user", "content": "/use:1 Prüfe"},
    ]
    assert _previous_source_map(messages) == {1: "files:123"}


def test_unbound_openwebui_user_gets_nextcloud_login_link(monkeypatch):
    import asyncio
    import rag.openai_provider as provider

    async def fake_ensure(user_id):
        assert user_id == "openwebui-local::owui-user-123"
        return {
            "status": "pending",
            "flow_id": "flow-1",
            "login_url": "https://cloud.example/index.php/login/flow/abc",
        }

    monkeypatch.setattr(provider, "_ensure_nextcloud_binding", fake_ensure)
    monkeypatch.setattr(provider, "_check_auth", lambda authorization: "openwebui-local")
    body = provider.ChatCompletionRequest(
        messages=[{"role": "user", "content": "Darlehensvertrag"}],
        stream=False,
    )
    response = asyncio.run(
        provider.chat_completions(
            body,
            _request({"x-openwebui-user-id": "owui-user-123"}),
            BackgroundTasks(),
            authorization=None,
        )
    )
    content = response["choices"][0]["message"]["content"]
    assert "Nextcloud-Anmeldung erforderlich" in content
    assert "https://cloud.example/index.php/login/flow/abc" in content


def test_same_external_user_id_is_isolated_by_provider_client(monkeypatch, tmp_path):
    import rag.openai_provider as provider
    from rag.credential_store import CredentialStore, scope_identity

    store = CredentialStore(tmp_path / "users.sqlite")
    store.register_client("frontend-a", "A" * 32, name="A")
    store.register_client("frontend-b", "B" * 32, name="B")
    monkeypatch.setattr(provider, "_PROVIDER_CLIENT_STORE", store)

    client_a = provider._check_auth("Bearer " + "A" * 32)
    client_b = provider._check_auth("Bearer " + "B" * 32)
    assert client_a == "frontend-a"
    assert client_b == "frontend-b"
    assert scope_identity(client_a, "same-user") != scope_identity(client_b, "same-user")


def test_followup_helper_signature_is_detected_for_server_side_suppression():
    prompt = "### Task: Suggest 3-5 relevant follow-up questions based on the chat history."
    assert _auxiliary_task_kind(_request(), prompt) == "ui:follow_ups"


def test_followup_helper_returns_empty_list_without_llm(monkeypatch):
    import asyncio
    from starlette.requests import Request
    import rag.openai_provider as provider

    monkeypatch.setattr(provider, "_check_auth", lambda authorization: "test-client")

    async def forbidden_llm(*args, **kwargs):
        raise AssertionError("follow-up helper must not invoke an LLM")

    monkeypatch.setattr(provider, "_ollama_complete", forbidden_llm)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8766),
        "scheme": "http",
        "query_string": b"",
    })
    body = provider.ChatCompletionRequest(
        messages=[{
            "role": "user",
            "content": "### Task: Suggest 3-5 relevant follow-up questions based on the chat history.",
        }],
        stream=False,
    )
    response = asyncio.run(
        provider.chat_completions(
            body, request, BackgroundTasks(), authorization="Bearer ignored"
        )
    )
    assert response["choices"][0]["message"]["content"] == '{"follow_ups":[]}'


def test_streaming_followup_suggestions_are_exposed_via_request_status():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "rag" / "openai_provider.py").read_text(
        encoding="utf-8"
    )
    setter = source[
        source.index("def _set_suggestions("):
        source.index("def _suggestions(", source.index("def _set_suggestions("))
    ]
    assert 'state["suggestions"] = [dict(item) for item in clean]' in setter
    assert 'state["updated_at"] = time.time()' in setter
