from pathlib import Path

import pytest

from rag.egress_policy import (
    EMBEDDING,
    MODEL_PROCESSING,
    RESEARCH_QUERY,
    EgressPolicy,
    evaluate_egress,
    load_egress_policy,
)


def test_research_query_guard_redacts_credentials_and_admin_identifiers():
    policy = EgressPolicy(
        internal_identifiers=(r"\bDL-[0-9]{4}-[0-9]+\b",),
    )
    result = evaluate_egress(
        "Suche DL-2026-4711 mit token=abcdef123456",
        egress_class=RESEARCH_QUERY,
        metadata={"external": True, "provider": "brave"},
        policy=policy,
    )
    assert result.action == "MODIFY"
    assert result.redactions == 2
    assert "DL-2026-4711" not in result.content
    assert "abcdef123456" not in result.content
    assert result.content.count("[REDACTED]") == 2


def test_guard_recurses_through_delegated_or_resolver_payloads():
    policy = EgressPolicy(internal_identifiers=(r"CASE-[0-9]+",))
    payload = {
        "references": [{"text": "CASE-42 Beispiel GmbH", "context": ["local"]}],
        "sources": ["openregister"],
    }
    result = evaluate_egress(
        payload,
        egress_class=RESEARCH_QUERY,
        metadata={"external": True, "provider": "resolver"},
        policy=policy,
    )
    assert result.action == "MODIFY"
    assert result.content["references"][0]["text"] == "[REDACTED] Beispiel GmbH"
    assert result.content["references"][0]["context"] == ["local"]


def test_model_processing_audit_and_embedding_allow_are_non_destructive_defaults():
    model_payload = [{"role": "user", "content": "DL-2026-4711 secret"}]
    model = evaluate_egress(
        model_payload,
        egress_class=MODEL_PROCESSING,
        metadata={"external": True, "backend": "openai"},
        policy=EgressPolicy(internal_identifiers=(r"DL-[0-9-]+",)),
    )
    embedding = evaluate_egress(
        ["DL-2026-4711 secret"],
        egress_class=EMBEDDING,
        metadata={"external": True, "backend": "openai"},
        policy=EgressPolicy(internal_identifiers=(r"DL-[0-9-]+",)),
    )
    assert model.action == "ALLOW"
    assert model.content is model_payload
    assert embedding.action == "ALLOW"
    assert embedding.content == ["DL-2026-4711 secret"]


def test_private_target_does_not_apply_guard_even_for_research_query():
    policy = EgressPolicy(internal_identifiers=(r"CASE-[0-9]+",))
    result = evaluate_egress(
        "CASE-42",
        egress_class=RESEARCH_QUERY,
        metadata={"target": "http://127.0.0.1:8080"},
        policy=policy,
    )
    assert result.action == "ALLOW"
    assert result.content == "CASE-42"


def test_block_mode_fails_policy_before_network_layer():
    policy = EgressPolicy(research_query="block")
    result = evaluate_egress(
        "public query",
        egress_class=RESEARCH_QUERY,
        metadata={"external": True},
        policy=policy,
    )
    assert result.action == "BLOCK"
    assert "administrator egress policy" in result.reason


def test_invalid_modes_and_regexes_are_rejected():
    with pytest.raises(ValueError, match="egress.research_query.mode"):
        load_egress_policy({"egress": {"research_query": {"mode": "magic"}}})
    with pytest.raises(ValueError, match="invalid egress internal identifier regex"):
        load_egress_policy({"egress": {"guard": {"internal_identifiers": ["("]}}})


def test_outbound_call_sites_use_egress_classes():
    root = Path(__file__).resolve().parent.parent
    web = (root / "rag" / "web_research.py").read_text(encoding="utf-8")
    delegated = (root / "rag" / "research" / "openai.py").read_text(encoding="utf-8")
    resolver = (root / "rag" / "resolver_client.py").read_text(encoding="utf-8")
    llm = (root / "rag" / "llm_backend.py").read_text(encoding="utf-8")
    embeddings = (root / "rag" / "embeddings.py").read_text(encoding="utf-8")

    assert '"egress_class": "research_query"' in web
    assert '"egress_class": "research_query"' in delegated
    assert '"egress_class": "research_query"' in resolver
    assert '"egress_class": "model_processing"' in llm
    assert '"egress_class": "embedding"' in embeddings
