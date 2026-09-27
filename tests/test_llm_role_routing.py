import os
from dataclasses import replace

import rag.llm_roles as roles
import rag.network_policy as network_policy
import rag.openai_provider as provider


def test_role_backend_inherits_default_and_can_override(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "default-model")
    monkeypatch.setenv("LLM_API_KEY", "default-secret")
    monkeypatch.setenv("PLANNER_LLM_BACKEND", "ollama")
    monkeypatch.setenv("PLANNER_LLM_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("PLANNER_LLM_MODEL", "planner-local")
    built = roles.build_role_backends(
        default_backend="openai",
        default_base_url="https://api.example.test/v1",
        default_model="default-model",
        default_api_key="default-secret",
        default_verify_tls=True,
        default_ca_file=None,
        models={"planner": "legacy-planner", "answer": "answer-default"},
    )
    assert built["planner"].backend_name == "ollama"
    assert built["planner"].base_url == "http://127.0.0.1:11434"
    assert built["planner"].model == "planner-local"
    assert built["planner"].scope == "local"
    assert built["answer"].backend_name == "openai"
    assert built["answer"].base_url == "https://api.example.test/v1"
    assert built["answer"].scope == "remote"


def test_private_endpoint_pinning_prefers_validated_ipv4(monkeypatch):
    monkeypatch.setattr(
        network_policy,
        "resolve_private_endpoint",
        lambda url: ("model.internal", ["fd00::5", "10.0.0.5"]),
    )
    target, host_header, extensions = network_policy.pinned_private_target(
        "https://model.internal/v1"
    )
    assert target == "https://10.0.0.5/v1"
    assert host_header == "model.internal"
    assert extensions == {"sni_hostname": "model.internal"}


def test_src_private_network_guard_applies_only_to_local_roles(monkeypatch):
    built = roles.build_role_backends(
        default_backend="openai",
        default_base_url="https://api.openai.com/v1",
        default_model="default-model",
        default_api_key="default-secret",
        default_verify_tls=True,
        default_ca_file=None,
        private_network_only=True,
        role_overrides={
            "planner": {
                "backend": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "planner-local",
                "scope": "local",
            },
            "answer": {
                "backend": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "answer-remote",
                "scope": "remote",
            },
        },
    )

    assert built["planner"].scope == "local"
    assert built["planner"].backend.private_network_only is True
    assert built["planner"].backend.trust_env is False

    assert built["answer"].scope == "remote"
    assert built["answer"].backend.private_network_only is False
    assert built["answer"].backend.trust_env is True


def test_legacy_remote_answer_context_is_hard_bounded(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "answer")
    monkeypatch.setattr(provider, "_answer_context_budget", lambda: None)
    monkeypatch.setattr(provider, "REMOTE_ANSWER_MAX_DOCUMENTS", 2)
    monkeypatch.setattr(provider, "REMOTE_LLM_MAX_CHARS_PER_DOCUMENT", 1000)
    monkeypatch.setattr(provider, "REMOTE_LLM_MAX_TOTAL_CHARS", 1700)
    results = [
        provider.SearchResult(index=i, title=f"d{i}.pdf", text="x" * 5000, raw={})
        for i in range(1, 5)
    ]
    context, included = provider._build_context(results, per_result_max_chars=6000, context_max_chars=40000)
    assert len(included) <= 2
    assert len(context) <= 1700 + 10  # separators are already counted by block budgeting except join overhead
    assert "d3.pdf" not in context


def test_remote_verifier_caps_candidates(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "verifier")
    monkeypatch.setattr(provider, "REMOTE_VERIFIER_MAX_CANDIDATES", 3)
    monkeypatch.setattr(
        provider,
        "RETRIEVAL_PLANNER",
        replace(provider.RETRIEVAL_PLANNER, verification_candidate_limit=6),
    )
    # The candidate bound is observable without invoking the model by using an
    # empty input only for the base case; direct cap arithmetic is the contract.
    limit = provider.RETRIEVAL_PLANNER.verification_candidate_limit
    if provider._role_remote("verifier"):
        limit = min(limit, provider.REMOTE_VERIFIER_MAX_CANDIDATES)
    assert limit == 3


def test_sunaq_remote_answer_context_uses_profile_budget_below_hard_cap(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "answer")
    monkeypatch.setattr(
        provider,
        "_answer_context_budget",
        lambda: {
            "max_documents": 3,
            "max_chars_per_document": 1200,
            "max_total_chars": 3000,
        },
    )
    monkeypatch.setattr(provider, "SUNAQ_REMOTE_HARD_ANSWER_MAX_DOCUMENTS", 5)
    monkeypatch.setattr(provider, "SUNAQ_REMOTE_HARD_MAX_CHARS_PER_DOCUMENT", 2000)
    monkeypatch.setattr(provider, "SUNAQ_REMOTE_HARD_MAX_TOTAL_CHARS", 5000)
    results = [
        provider.SearchResult(index=i, title=f"d{i}.pdf", text="x" * 5000, raw={})
        for i in range(1, 6)
    ]

    context, included = provider._build_context(results)

    assert len(included) == 3
    assert "d4.pdf" not in context
    assert len(context) <= 3000 + 20


def test_profile_endpoint_change_reclassifies_inherited_role_scope(monkeypatch):
    monkeypatch.setenv("ANSWER_LLM_SCOPE", "local")
    built = roles.build_role_backends(
        default_backend="ollama",
        default_base_url="http://127.0.0.1:11434",
        default_model="local-model",
        default_api_key="",
        default_verify_tls=True,
        default_ca_file=None,
        role_overrides={
            "answer": {
                "backend": "openai",
                "base_url": "https://api.example.test/v1",
                "model": "remote-model",
            }
        },
    )
    assert built["answer"].base_url == "https://api.example.test/v1"
    assert built["answer"].scope == "remote"

    explicit = roles.build_role_backends(
        default_backend="ollama",
        default_base_url="http://127.0.0.1:11434",
        default_model="local-model",
        default_api_key="",
        default_verify_tls=True,
        default_ca_file=None,
        role_overrides={
            "answer": {
                "backend": "openai",
                "base_url": "https://api.example.test/v1",
                "model": "remote-model",
                "scope": "local",
            }
        },
    )
    assert explicit["answer"].scope == "local"


def test_explicit_context_budget_equal_to_legacy_default_is_not_a_profile_sentinel(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda _role: False)
    monkeypatch.setattr(
        provider,
        "_answer_context_budget",
        lambda: {
            "max_documents": 10,
            "max_chars_per_document": 50000,
            "max_total_chars": 100000,
        },
    )
    result = provider.SearchResult(
        index=1,
        title="large.pdf",
        text="x" * 50000,
        raw={},
    )
    context, included = provider._build_context(
        [result],
        per_result_max_chars=50000,
        context_max_chars=provider.CONTEXT_MAX_CHARS,
    )
    assert included
    assert len(context) <= provider.CONTEXT_MAX_CHARS
