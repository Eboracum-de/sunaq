import asyncio

import pytest

from rag.research.contracts import DelegatedResearchRequest
from rag.research.openai import parse_openai_research_response
from rag.research.registry import load_delegated_research_settings


def _payload():
    text = "Die Example Holdings GmbH hat öffentlich dokumentierte Geschäftsführer."
    url = "https://example.org/holdings"
    return {
        "id": "resp_test",
        "model": "gpt-test",
        "usage": {"input_tokens": 120, "output_tokens": 42, "total_tokens": 162},
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "action": {
                    "type": "search",
                    "queries": ["Example Holdings GmbH Geschäftsführer"],
                    "sources": [
                        {"type": "url", "url": url},
                        {"type": "url", "url": "https://example.net/real-estate"},
                    ],
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": url,
                                "title": "Example Holdings",
                                "start_index": 0,
                                "end_index": len(text),
                            }
                        ],
                    }
                ],
            },
        ],
    }


def test_openai_research_response_normalizes_queries_sources_and_citations():
    result = parse_openai_research_response(_payload(), requested_model="fallback")
    assert result.provider == "openai"
    assert result.model == "gpt-test"
    assert result.response_id == "resp_test"
    assert result.search_queries == ("Example Holdings GmbH Geschäftsführer",)
    assert len(result.citations) == 1
    assert result.citations[0].title == "Example Holdings"
    assert "[W1](https://example.org/holdings)" in result.answer_markdown
    assert len(result.sources) == 2
    assert result.sources[0].cited is True
    assert result.sources[1].cited is False
    assert result.usage["total_tokens"] == 162


def test_openai_research_requires_actual_web_search_call():
    payload = _payload()
    payload["output"] = [value for value in payload["output"] if value["type"] != "web_search_call"]
    with pytest.raises(RuntimeError, match="no web_search_call"):
        parse_openai_research_response(payload, requested_model="gpt-test")


def test_delegated_request_has_no_private_evidence_channel():
    request = DelegatedResearchRequest(query="Example Holdings GmbH", request_id="req-1")
    assert request.query == "Example Holdings GmbH"
    assert not hasattr(request, "documents")
    assert not hasattr(request, "evidence")
    assert not hasattr(request, "history")


def test_openai_delegated_research_runs_query_and_model_egress_hooks(monkeypatch):
    import rag.research.openai as openai_adapter

    stages = []

    def fake_hook(stage, *, content=None, metadata=None):
        stages.append((stage, content, dict(metadata or {})))
        if stage == openai_adapter.PRE_MODEL_EGRESS:
            raise RuntimeError("blocked by test policy")
        return content

    monkeypatch.setattr(openai_adapter, "apply_policy_hook", fake_hook)
    provider = openai_adapter.OpenAIResearchProvider(
        api_key="test-key",
        model="gpt-test",
    )
    with pytest.raises(RuntimeError, match="blocked by test policy"):
        asyncio.run(
            provider.research(
                DelegatedResearchRequest(
                    query="Example Holdings GmbH",
                    request_id="req-policy",
                )
            )
        )

    assert [value[0] for value in stages] == [
        openai_adapter.OUTBOUND_QUERY,
        openai_adapter.PRE_MODEL_EGRESS,
    ]
    assert stages[1][2]["public_only"] is True
    assert stages[1][1] == [{
        "role": "user",
        "content": "Example Holdings GmbH",
    }]


def test_environment_provider_opt_in_enables_openai_without_config_mutation(monkeypatch):
    monkeypatch.setenv("SUNAQ_RESEARCH_PROVIDER", "openai")
    monkeypatch.setenv("SUNAQ_RESEARCH_MODEL", "gpt-research")
    settings = load_delegated_research_settings({}, fallback_model="gpt-answer")
    assert settings.active is True
    assert settings.provider == "openai"
    assert settings.model == "gpt-research"
    assert settings.delegate_web_only is True


def test_openai_research_model_does_not_inherit_local_answer_model(monkeypatch):
    monkeypatch.delenv("SUNAQ_RESEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("SUNAQ_RESEARCH_MODEL", raising=False)
    settings = load_delegated_research_settings(
        {"delegated_research": {"enabled": True, "provider": "openai"}},
        fallback_model="qwen3:8b",
    )
    assert settings.model == "gpt-5.5"


def test_native_environment_override_disables_delegation(monkeypatch):
    monkeypatch.setenv("SUNAQ_RESEARCH_PROVIDER", "native")
    settings = load_delegated_research_settings(
        {"delegated_research": {"enabled": True, "provider": "openai"}},
        fallback_model="gpt-answer",
    )
    assert settings.active is False
    assert settings.provider == "native"
