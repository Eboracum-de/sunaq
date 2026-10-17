from dataclasses import replace

import rag.openai_provider as provider
from rag.openai_provider import (
    _COMMAND_HELP,
    _effective_verification_candidate_limit,
    _is_help_alias,
    _verification_capacity_notice,
    _verification_limit_notice,
    _verification_notice_candidate_count,
)


def test_help_aliases_are_natural_and_health_is_not_listed():
    for text in ("help", "Help!", "hilfe", "Hilfe bitte", "aide", "ayuda", "aiuto", "pomoc"):
        assert _is_help_alias(text)
    assert not _is_help_alias("help me find invoices")
    assert "/health" not in _COMMAND_HELP
    assert "/use:1,2" in _COMMAND_HELP
    assert "/web" in _COMMAND_HELP
    assert "nur normale Dokumente" in _COMMAND_HELP
    assert "Dokumente + Mailarchiv" not in _COMMAND_HELP
    assert "gespeicherte SunaQ-Recherchen" in _COMMAND_HELP


def test_info_rewrite_logging_does_not_emit_query_content():
    source = provider.__file__
    text = __import__("pathlib").Path(source).read_text(encoding="utf-8")
    assert "query rewrite round 1: elastic=%r" not in text
    assert "query intent round 1: semantic_present=%s entities=%d concepts=%d concept_groups=%d" in text
    assert "retrieval round %d rewrite: elastic=%r" not in text


def test_verification_limit_notice_only_when_candidates_are_unchecked():
    assert _verification_limit_notice(6, 6, exhaustive=False) == ""
    exhaustive = _verification_limit_notice(7, 6, exhaustive=True)
    assert "vollständige Treffermenge" in exhaustive
    assert "unvollständig" in exhaustive
    notice = _verification_limit_notice(20, 6, exhaustive=False)
    assert "weitere mögliche Dokumenttreffer" in notice
    assert "grenzen Sie" in notice
    # Regression: the configured bounded window may be 30 while a remote
    # verifier is capped at 10.  The notice must use the actually reviewed
    # count, otherwise 22 ranked candidates incorrectly look complete.
    bounded = _verification_limit_notice(22, 10, exhaustive=False, bounded_document_set=True)
    assert "innerhalb der angegebenen Kriterien" in bounded
    assert "grenzen Sie" not in bounded


def test_legacy_remote_verifier_uses_exhaustive_budget_only_for_explicit_completeness(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "verifier")
    monkeypatch.setattr(provider, "_answer_context_budget", lambda: None)
    monkeypatch.setattr(provider, "REMOTE_VERIFIER_MAX_CANDIDATES", 10)
    monkeypatch.setattr(
        provider,
        "RETRIEVAL_PLANNER",
        replace(
            provider.RETRIEVAL_PLANNER,
            verification_candidate_limit=10,
            bounded_verification_candidate_limit=30,
            exhaustive_verification_candidate_limit=30,
        ),
    )

    assert _effective_verification_candidate_limit(10, exhaustive=False) == 10
    # A normal/bounded request remains protected by the remote safety cap.
    assert _effective_verification_candidate_limit(30, exhaustive=False) == 10
    # Explicit completeness intent may consume the configured exhaustive budget.
    assert _effective_verification_candidate_limit(30, exhaustive=True) == 30


def test_exhaustive_notice_count_uses_elasticsearch_total_beyond_return_window():
    payload = {"statistics": {"elasticsearch_total_hits": 47}}
    assert _verification_notice_candidate_count(30, payload, exhaustive=True) == 47
    assert _verification_notice_candidate_count(30, payload, exhaustive=False) == 30
    notice = _verification_limit_notice(
        _verification_notice_candidate_count(30, payload, exhaustive=True),
        30,
        exhaustive=True,
    )
    assert "unvollständig" in notice


def test_slash_hilfe_is_normalized_to_help_directive():
    parsed = provider._parse_retrieval_directives('/hilfe')
    assert parsed.seen == ['help']
    assert parsed.special_command == 'help'
    assert parsed.query == ''


def test_explicit_use_documentwise_completeness_is_narrow():
    assert provider._explicit_selection_needs_documentwise_completeness(
        'Liste Datum und Betrag tabellarisch auf.'
    ) is True
    assert provider._explicit_selection_needs_documentwise_completeness(
        'Fasse die wesentlichen Erkenntnisse zusammen.'
    ) is False


def test_sunaq_remote_verifier_uses_profile_window_below_admin_hard_cap(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "verifier")
    monkeypatch.setattr(
        provider,
        "_answer_context_budget",
        lambda: {
            "max_documents": 30,
            "max_chars_per_document": 4000,
            "max_total_chars": 100000,
        },
    )
    monkeypatch.delenv("REMOTE_VERIFIER_MAX_CANDIDATES", raising=False)
    monkeypatch.setattr(provider, "SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES", 50)

    assert _effective_verification_candidate_limit(10, exhaustive=False) == 10
    assert _effective_verification_candidate_limit(30, exhaustive=False) == 30
    assert _effective_verification_candidate_limit(60, exhaustive=False) == 50


def test_sunaq_remote_verifier_preserves_explicit_legacy_cap(monkeypatch):
    monkeypatch.setattr(provider, "_role_remote", lambda role: role == "verifier")
    monkeypatch.setattr(
        provider,
        "_answer_context_budget",
        lambda: {
            "max_documents": 30,
            "max_chars_per_document": 4000,
            "max_total_chars": 100000,
        },
    )
    monkeypatch.setenv("REMOTE_VERIFIER_MAX_CANDIDATES", "10")
    monkeypatch.setattr(provider, "REMOTE_VERIFIER_MAX_CANDIDATES", 10)
    monkeypatch.setattr(provider, "SUNAQ_REMOTE_HARD_VERIFIER_MAX_CANDIDATES", 50)

    assert _effective_verification_candidate_limit(30, exhaustive=False) == 10


def test_progress_owner_cannot_be_replaced():
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    with provider._PROGRESS_LOCK:
        provider._PROGRESS_STATES.clear()

    provider._set_progress(request_id, "alice", "search", "Alice search")
    provider._set_progress(request_id, "bob", "answer", "Bob answer")

    with provider._PROGRESS_LOCK:
        state = dict(provider._PROGRESS_STATES[request_id])
    assert state["owner"] == "alice"
    assert state["stage"] == "search"
    assert state["label"] == "Alice search"


def test_capacity_notice_warns_near_profile_ceiling_without_claiming_known_overflow():
    assert _verification_capacity_notice(48, 48, 50, exhaustive=False)
    assert _verification_capacity_notice(47, 47, 50, exhaustive=False) == ""
    assert _verification_capacity_notice(30, 29, 30, exhaustive=False) == ""
    assert _verification_capacity_notice(48, 48, 50, exhaustive=True) == ""


def test_candidate_verifier_reports_batch_progress():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    provider_source = (root / "rag" / "openai_provider.py").read_text(encoding="utf-8")
    assert 'Prüfe Dokument {first} von {len(bounded)}' in provider_source
    assert 'Prüfe Dokumente {first}–{last} von {len(bounded)}' in provider_source


def test_stronger_model_suggestion_respects_current_and_allowed_models(monkeypatch):
    monkeypatch.setattr(
        provider,
        "_model_access_for_identity",
        lambda _user_id: (["sunaq-standard", "sunaq-thorough"], "sunaq-standard"),
    )

    # Regression: when Thorough is already active and Deep is not allowed,
    # never offer Thorough again as a supposedly stronger model.
    assert provider._stronger_model_suggestion(
        "Musterweg 280",
        "client:user",
        current_model_id="sunaq-thorough",
    ) is None

    monkeypatch.setattr(
        provider,
        "_model_access_for_identity",
        lambda _user_id: (
            ["sunaq-standard", "sunaq-thorough", "sunaq-deep"],
            "sunaq-standard",
        ),
    )
    suggestion = provider._stronger_model_suggestion(
        "Musterweg 280",
        "client:user",
        current_model_id="sunaq-thorough",
    )
    assert suggestion is not None
    assert suggestion["model"] == "sunaq-deep"
    assert suggestion["label"] == "Mit Tief erneut suchen"


def test_refinement_suggestions_always_offer_manual_refinement(monkeypatch):
    monkeypatch.setattr(
        provider,
        "_model_access_for_identity",
        lambda _user_id: (["sunaq-standard"], "sunaq-standard"),
    )
    suggestions = provider._refinement_suggestions(
        "Eboracum GmbH",
        "client:user",
        current_model_id="sunaq-standard",
    )
    assert suggestions == [{"label": "Anfrage präzisieren", "action": "focus"}]


def test_explicit_use_can_persist_findings_without_changing_answer_selection():
    source = __import__("pathlib").Path(provider.__file__).read_text(encoding="utf-8")
    assert "async def _store_use_research_findings(" in source
    assert "if not RESEARCH_FINDINGS_ENABLED or not results:" in source
    assert "review_results = [" in source
    assert "raw=dict(result.raw or {})" in source
    assert "await _verify_exhaustive_candidates(" in source
    assert "await _store_positive_research_findings(" in source
    assert "await _store_use_research_findings(" in source
