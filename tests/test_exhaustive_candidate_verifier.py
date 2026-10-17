import asyncio
import json

import rag.openai_provider as provider


def test_exhaustive_verifier_keeps_semantic_match_without_literal_plural(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "documents": [
                {"index": 1, "status": "match", "reason": "invoice evidence", "relation_binding": "direct", "evidence_frame": {"entities": [], "relations": [], "constraints": [], "concepts": [], "mentioned_entities": []}},
                {"index": 2, "status": "reject", "reason": "wrong year", "relation_binding": "contradicted", "evidence_frame": {"entities": [], "relations": [], "constraints": [], "concepts": [], "mentioned_entities": []}},
            ],
            "reason": "checked",
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    results = [
        provider.SearchResult(
            index=1,
            title="RG-EX-2025-001.pdf",
            text=(
                "Nordstern GmbH ... Example Logistics GmbH ... "
                "10. September 2025 ... Rechnung EX-2025-001 ... Zahlungsbetrag 1.428,00"
            ),
            raw={"document_id": "files:66732"},
        ),
        provider.SearchResult(
            index=2,
            title="Alt.pdf",
            text="Nordstern GmbH ... Rechnung aus 2023",
            raw={"document_id": "files:1"},
        ),
    ]

    matches, uncertain, meta = asyncio.run(
        provider._verify_exhaustive_candidates(
            "Suche die Rechnungen der Nordstern GmbH an Example Logistics GmbH 2025",
            results,
        )
    )

    assert [item.title for item in matches] == ["RG-EX-2025-001.pdf"]
    assert uncertain == []
    assert meta["matches"] == 1
    assert results[0].raw["verification_status"] == "match"


def test_exhaustive_verifier_fails_closed_to_uncertain(monkeypatch):
    async def failing_complete(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(provider, "_ollama_complete", failing_complete)
    results = [
        provider.SearchResult(index=1, title="x.pdf", text="some text", raw={})
    ]
    matches, uncertain, meta = asyncio.run(
        provider._verify_exhaustive_candidates("find invoices 2025", results)
    )
    assert matches == []
    assert len(uncertain) == 1
    assert meta["uncertain"] == 1
    assert "error" in meta


def test_answer_candidates_use_uncertain_only_when_no_match():
    match = provider.SearchResult(index=1, title="match.pdf", text="confirmed", raw={})
    uncertain = provider.SearchResult(
        index=2,
        title="hint.pdf",
        text="Grundbuch von Friedrichstadt Blatt 3216",
        raw={"verification_status": "uncertain"},
    )

    selected, uncertain_only = provider._answer_candidates_from_verification(
        [match], [uncertain]
    )
    assert selected == [match]
    assert uncertain_only is False

    selected, uncertain_only = provider._answer_candidates_from_verification(
        [], [uncertain]
    )
    assert selected == [uncertain]
    assert uncertain_only is True

    selected, uncertain_only = provider._answer_candidates_from_verification([], [])
    assert selected == []
    assert uncertain_only is False


def test_uncertain_only_answer_prompt_preserves_groundbook_clues_without_upgrading_match():
    result = provider.SearchResult(
        index=1,
        title="Blatt-3216.pdf",
        text=(
            "Amtsgericht Tempelhof-Kreuzberg. Grundbuch von Friedrichstadt. "
            "Blatt 3216. Musterstraße 44, 10969 Berlin."
        ),
        raw={"document_id": "files:3216", "verification_status": "uncertain"},
    )
    context, included = provider._build_context([result])
    messages = provider._rag_answer_messages(
        "Grundbuchauszug Musterstraße 44 in Berlin",
        "Grundbuchauszug Musterstraße 44 in Berlin",
        context,
        uncertain_only=True,
    )

    assert included == [result]
    system = messages[0]["content"]
    assert "AUSSCHLIESSLICH UNSICHERE INTERNE TREFFER" in system
    assert "kein eindeutiger bzw." in system
    assert "könnte entsprechen" in system
    assert "Blatt 3216" in system
    assert "Musterstraße 44" in system


def test_uncertain_only_answer_guard_not_added_for_confirmed_matches():
    messages = provider._rag_answer_messages(
        "Grundbuchblatt 3216 Kreuzberg",
        "Grundbuchblatt 3216 Kreuzberg",
        "[]",
    )
    assert "AUSSCHLIESSLICH UNSICHERE INTERNE TREFFER" not in messages[0]["content"]


def test_hybrid_answer_keeps_internal_uncertainty_separate_from_web_evidence():
    messages = provider._hybrid_web_answer_messages(
        "Grundbuchauszug Musterstraße 44",
        "Grundbuchauszug Musterstraße 44",
        '[{"citation":"[1]","text":"Grundbuch von Friedrichstadt Blatt 3216"}]',
        '[{"citation":"[W1]","text":"öffentliche Quelle"}]',
        internal_uncertain_only=True,
    )
    system = messages[0]["content"]
    assert "AUSSCHLIESSLICH UNSICHERE INTERNE TREFFER" in system
    assert "Öffentliche Web-Evidence" in system
    assert "Blatt 3216" in messages[1]["content"]
