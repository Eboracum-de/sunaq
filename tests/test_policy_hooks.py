import pytest

from rag.egress_policy import configure_runtime_egress_policy, reset_runtime_egress_policy
from rag.policy_hooks import (
    ALLOW,
    BLOCK,
    MODIFY,
    OUTBOUND_QUERY,
    PRE_MODEL_EGRESS,
    PRE_PERSIST,
    PolicyHookDecision,
    PolicyHookRejected,
    apply_policy_hook,
    install_policy_evaluator,
    reset_policy_evaluator,
)


@pytest.fixture(autouse=True)
def _reset_policy_hook_evaluator():
    reset_policy_evaluator()
    reset_runtime_egress_policy()
    yield
    reset_policy_evaluator()
    reset_runtime_egress_policy()


def test_default_policy_hooks_are_noop_allow():
    payload = {"value": "unchanged"}
    assert apply_policy_hook(PRE_PERSIST, content=payload) is payload


def test_builtin_research_egress_guard_runs_before_optional_evaluator():
    configure_runtime_egress_policy({
        "egress": {
            "research_query": {"mode": "guard"},
            "guard": {"internal_identifiers": [r"CASE-[0-9]+"]},
        }
    })
    seen = []

    def evaluator(request):
        seen.append(request.content)
        return PolicyHookDecision(action=ALLOW, content=request.content)

    install_policy_evaluator(evaluator)
    assert apply_policy_hook(
        OUTBOUND_QUERY,
        content="CASE-42 Beispiel GmbH",
        metadata={"external": True, "provider": "brave"},
    ) == "[REDACTED] Beispiel GmbH"
    assert seen == ["[REDACTED] Beispiel GmbH"]


def test_policy_hook_can_modify_content():
    def evaluator(request):
        assert request.stage == OUTBOUND_QUERY
        assert request.content == "private query"
        return PolicyHookDecision(action=MODIFY, content="sanitized query")

    install_policy_evaluator(evaluator)
    assert (
        apply_policy_hook(OUTBOUND_QUERY, content="private query")
        == "sanitized query"
    )


def test_policy_hook_block_fails_closed():
    install_policy_evaluator(
        lambda _request: PolicyHookDecision(action=BLOCK, reason="blocked")
    )
    with pytest.raises(PolicyHookRejected) as exc:
        apply_policy_hook(PRE_PERSIST, content=b"payload")
    assert exc.value.action == BLOCK
    assert exc.value.reason == "blocked"


def test_policy_hook_evaluator_failure_is_not_swallowed():
    def evaluator(_request):
        raise RuntimeError("scanner unavailable")

    install_policy_evaluator(evaluator)
    with pytest.raises(RuntimeError, match="scanner unavailable"):
        apply_policy_hook(PRE_PERSIST, content=b"payload")


def test_model_backend_boundary_uses_policy_hook():
    from rag.llm_backend import OllamaBackend

    def evaluator(request):
        if request.stage == PRE_MODEL_EGRESS:
            return PolicyHookDecision(
                action=MODIFY,
                content=[{"role": "user", "content": "redacted"}],
            )
        return PolicyHookDecision(action=ALLOW, content=request.content)

    install_policy_evaluator(evaluator)
    backend = OllamaBackend(base_url="http://127.0.0.1:11434", model="test")
    assert backend._policy_messages(
        [{"role": "user", "content": "secret"}],
        model="test",
    ) == [{"role": "user", "content": "redacted"}]


def test_policy_hook_call_sites_are_wired():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    web = (root / "rag" / "web_research.py").read_text(encoding="utf-8")
    mail = (root / "rag" / "mail_sync.py").read_text(encoding="utf-8")
    llm = (root / "rag" / "llm_backend.py").read_text(encoding="utf-8")
    embeddings = (root / "rag" / "embeddings.py").read_text(encoding="utf-8")

    assert "OUTBOUND_QUERY" in web
    assert "PRE_FETCH" in web
    assert "POST_FETCH" in web
    assert "PRE_PERSIST" in web
    assert "POST_FETCH" in mail
    assert "PRE_PERSIST" in mail
    assert "PRE_MODEL_EGRESS" in llm
    assert "PRE_MODEL_EGRESS" in embeddings
