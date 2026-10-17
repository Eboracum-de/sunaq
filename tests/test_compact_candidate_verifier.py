import asyncio
import json
from dataclasses import replace

import rag.openai_provider as provider


def _result(i: int, title: str, text: str):
    return provider.SearchResult(index=i, title=title, text=text, raw={"document_id": f"files:{i}"})


def test_normal_compact_verifier_schema_and_evidence(monkeypatch):
    calls = []

    async def fake_complete(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps({
            "documents": [
                {
                    "index": 1,
                    "status": "match",
                    "relation_binding": "direct",
                    "relations": [{"source": "Nordstern GmbH", "predicate": "stellt Rechnung an", "target": "Example Logistics GmbH"}],
                    "constraints": [{"kind": "year", "value": "2025", "status": "match"}],
                    "mentioned_entities": [],
                },
                {
                    "index": 2,
                    "status": "reject",
                    "relation_binding": "contradicted",
                    "relations": [{"source": "Example Logistics GmbH", "predicate": "stellt Rechnung an", "target": "Doris Muster"}],
                    "constraints": [{"kind": "year", "value": "2025", "status": "match"}],
                    "mentioned_entities": ["Nordstern GmbH"],
                },
            ]
        })

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    results = [
        _result(1, "RG-EX-2025-001.odt", "Nordstern GmbH Rechnung an Example Logistics GmbH 2025"),
        _result(2, "RG-SAG-2025-001.pdf", "Example Logistics GmbH Rechnung an Doris Muster 2025; Nordstern GmbH erwähnt"),
    ]
    matches, uncertain, meta = asyncio.run(provider._verify_exhaustive_candidates(
        "Suche die Rechnungen der Nordstern GmbH an Example Logistics GmbH 2025",
        results,
        query_frame={
            "intent": "find evidence",
            "entities": [
                {"id": "q1", "text": "Nordstern GmbH", "role": "source"},
                {"id": "q2", "text": "Example Logistics GmbH", "role": "target"},
            ],
            "relations": [{"source": "q1", "predicate": "stellt Rechnung an", "target": "q2"}],
            "constraints": [{"kind": "year", "value": "2025"}],
            "concepts": ["Rechnung"],
        },
        candidate_limit=2,
        compact=True,
    ))

    assert [r.title for r in matches] == ["RG-EX-2025-001.odt"]
    assert uncertain == []
    assert meta["compact"] is True
    assert meta["batch_errors"] == []
    assert meta["reviewed_documents"][1]["status"] == "reject"
    ef = meta["reviewed_documents"][1]["evidence_frame"]
    assert ef["relations"][0]["target"] == "Doris Muster"
    assert ef["mentioned_entities"] == ["Nordstern GmbH"]
    schema = calls[0]["response_format"]
    props = schema["properties"]["documents"]["items"]["properties"]
    assert "reason" in props
    assert "evidence_frame" not in props


def test_candidate_verifier_prompt_treats_substantive_topical_cross_references_as_direct_evidence():
    prompt = provider.CANDIDATE_VERIFIER_SYSTEM_PROMPT
    assert 'breite Entitäts-/Informationsanfrage' in prompt
    assert 'substanzielle Tatsachen über genau diese Entität' in prompt
    assert 'inhaltlich aussagekräftige Querverweise' in prompt
    assert 'lediglich genannt oder zitiert' in prompt


def test_common_verifier_policy_is_appended_after_preserved_profile_override(monkeypatch):
    monkeypatch.setattr(
        provider,
        "_prompt",
        lambda name, default: "ALTES PROFIL: Zitate sind immer reference_only",
    )
    prompt = provider._candidate_verifier_prompt()
    assert prompt.startswith("ALTES PROFIL")
    assert "GEMEINSAMER VERIFIER-VERTRAG" in prompt
    assert "source_origin=chat_archive" in prompt
    assert "Elasticsearch-Ausschnitte" in prompt
    assert "NICHT in eine Forderung umgedeutet" in prompt


def test_normal_compact_verifier_splits_malformed_batch(monkeypatch):
    calls = []

    async def fake_complete(messages, *args, **kwargs):
        calls.append(messages[-1]["content"])
        # First 4-document call is malformed; split calls return valid 2-doc JSON.
        if "[DOKUMENT 4]" in messages[-1]["content"] and len(calls) == 1:
            return '{"documents": ['
        if "[DOKUMENT 1]" in messages[-1]["content"] and "[DOKUMENT 2]" in messages[-1]["content"]:
            return json.dumps({"documents": [
                {"index": 1, "status": "reject", "relation_binding": "contradicted", "relations": [], "constraints": [], "mentioned_entities": []},
                {"index": 2, "status": "match", "relation_binding": "direct", "relations": [], "constraints": [], "mentioned_entities": []},
            ]})
        return json.dumps({"documents": [
            {"index": 3, "status": "reject", "relation_binding": "contradicted", "relations": [], "constraints": [], "mentioned_entities": []},
            {"index": 4, "status": "match", "relation_binding": "direct", "relations": [], "constraints": [], "mentioned_entities": []},
        ]})

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    results = [
        _result(1, "a.pdf", "2019"),
        _result(2, "RG-EX-2025-001.odt", "2025"),
        _result(3, "c.pdf", "2020"),
        _result(4, "RG-EX-2025-001.pdf", "2025"),
    ]
    monkeypatch.setattr(
        provider, "RETRIEVAL_PLANNER",
        replace(provider.RETRIEVAL_PLANNER, verification_batch_size=4),
    )
    matches, uncertain, meta = asyncio.run(provider._verify_exhaustive_candidates(
        "Rechnungen 2025", results, candidate_limit=4, compact=True,
    ))

    assert [r.title for r in matches] == ["RG-EX-2025-001.odt", "RG-EX-2025-001.pdf"]
    assert uncertain == []
    assert len(meta["batch_retries"]) == 1
    assert meta["batch_errors"] == []
    assert len(calls) == 3


def test_verifier_prompt_receives_query_rewrite_requirements(monkeypatch):
    captured = {}

    async def fake_complete(messages, *args, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        return json.dumps({"documents": [{
            "index": 1,
            "status": "match",
            "relation_binding": "direct",
            "relations": [],
            "constraints": [],
            "mentioned_entities": [],
        }]})

    monkeypatch.setattr(provider, "_ollama_complete", fake_complete)
    result = _result(1, "invoice.pdf", "examplehost Rechnung Februar 2021")
    matches, uncertain, meta = asyncio.run(provider._verify_exhaustive_candidates(
        "Suche Rechnungen von examplehost aus dem Februar 2021",
        [result],
        query_frame={
            "intent": "Rechnung finden",
            "entities": [{"id": "q1", "text": "examplehost", "role": ""}],
            "relations": [],
            "constraints": [{"kind": "Monat", "value": "Februar 2021"}],
            "concepts": ["Rechnung"],
        },
        verification_requirements=[
            "Das Dokument ist selbst eine Rechnung von examplehost.",
            "Das relevante Rechnungsdatum liegt im Februar 2021.",
        ],
        candidate_limit=1,
        compact=True,
    ))
    assert len(matches) == 1
    assert uncertain == []
    assert "VERIFIKATIONSANFORDERUNGEN AUS DEM QUERY-REWRITE" in captured["prompt"]
    assert "Februar 2021" in captured["prompt"]
    assert "selbst eine Rechnung" in captured["prompt"]
