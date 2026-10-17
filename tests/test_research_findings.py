from __future__ import annotations

from rag.graph import GraphStore
from rag.research_findings import (
    PROVENANCE_LABEL,
    canonical_query_frame,
    curation_frame_hash,
    evidence_entity_candidates,
    evidence_frame_view,
    merge_entity_candidates,
    finding_id,
    query_frame_hash,
    query_frame_has_structure,
)


def _frame_a():
    return {
        "intent": "find invoices",
        "entities": [
            {"id": "e1", "text": "Eboracum GmbH", "role": "issuer"},
            {"id": "e2", "text": "Muster Leasing GmbH", "role": "recipient"},
        ],
        "relations": [
            {"source": "e1", "predicate": "invoice to", "target": "e2"},
        ],
        "constraints": [{"kind": "year", "value": "2025"}],
        "concepts": ["Rechnung"],
    }


def _frame_same_semantics_other_ids():
    return {
        "intent": "find invoices",
        "entities": [
            {"id": "z9", "text": "Muster Leasing GmbH", "role": "recipient"},
            {"id": "x4", "text": "Eboracum GmbH", "role": "issuer"},
        ],
        "relations": [
            {"source": "x4", "predicate": "invoice to", "target": "z9"},
        ],
        "constraints": [{"value": "2025", "kind": "year"}],
        "concepts": ["Rechnung"],
    }


def test_query_frame_hash_ignores_local_entity_ids_and_order():
    assert canonical_query_frame(_frame_a()) == canonical_query_frame(_frame_same_semantics_other_ids())
    assert query_frame_hash(_frame_a()) == query_frame_hash(_frame_same_semantics_other_ids())
    assert finding_id("files:66732", _frame_a()) == finding_id(
        "files:66732", _frame_same_semantics_other_ids()
    )


def test_query_frame_requires_reusable_structure():
    assert query_frame_has_structure(_frame_a()) is True
    assert query_frame_has_structure({"intent": "find documents"}) is False


def test_store_research_findings_persists_only_positive_direct_matches():
    store = object.__new__(GraphStore)
    calls: list[tuple[str, dict]] = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "RETURN e.entity_id AS entity_id" in query and "match_rank" in query:
            normalized = params.get("normalized")
            if normalized == "eboracum gmbh":
                return [{"entity_id": "org-eb", "display_name": "Eboracum GmbH", "labels": ["Entity", "Organization"]}]
            if normalized == "muster leasing gmbh":
                return [{"entity_id": "org-ex", "display_name": "Muster Leasing GmbH", "labels": ["Entity", "Organization"]}]
            return []
        return []

    store._run = fake_run  # type: ignore[method-assign]

    result = store.store_research_findings(
        query_id="q-test",
        query_frame=_frame_a(),
        documents=[
            {
                "document_id": "files:66732",
                "title": "RG-EX-2025-001.pdf",
                "verification_status": "match",
                "relation_binding": "direct",
                "evidence_frame": {
                    "constraints": [{"kind": "year", "value": "2025", "status": "match"}],
                },
            },
            {
                "document_id": "files:old",
                "title": "Old.pdf",
                "verification_status": "reject",
                "relation_binding": "direct",
            },
            {
                "document_id": "files:unclear",
                "title": "Unclear.pdf",
                "verification_status": "match",
                "relation_binding": "unclear",
            },
        ],
        canonical_user_id="user-alice",
        nextcloud_login="alice",
        nextcloud_server="https://nc.example",
        user_query="Welche Rechnungen gab es 2025?",
        retrieval_query="+Eboracum +2025 +Rechnung",
        software_version="0.8.3-rc11",
        planner_model="planner",
        verifier_model="verifier",
    )

    assert result["stored"] == 1
    assert result["skipped"] == 2
    assert result["provenance"] == PROVENANCE_LABEL
    assert result["resolved_query_entities"] == 2

    write_calls = [params for query, params in calls if "MERGE (f:ResearchFinding:AKIResearchFinding" in query]
    assert len(write_calls) == 1
    assert len(write_calls[0]["rows"]) == 1
    assert write_calls[0]["rows"][0]["document_id"] == "files:66732"
    assert write_calls[0]["provenance_label"] == "SunaQ Recherche"
    assert write_calls[0]["query_id"] == "q-test"
    assert "user_query" not in write_calls[0]

    run_calls = [params for query, params in calls if "MERGE (run:ResearchRun" in query]
    assert len(run_calls) == 1
    assert run_calls[0]["run_id"] == "q-test"
    assert run_calls[0]["canonical_user_id"] == "user-alice"
    assert run_calls[0]["nextcloud_login"] == "alice"
    assert run_calls[0]["user_query"] == "Welche Rechnungen gab es 2025?"
    assert run_calls[0]["retrieval_query"] == "+Eboracum +2025 +Rechnung"

    produced_calls = [query for query, params in calls if "MERGE (run)-[p:PRODUCED]->(f)" in query]
    assert len(produced_calls) == 1

    entity_links = [params for query, params in calls if "MERGE (f)-[r:QUERY_ENTITY" in query]
    assert len(entity_links) == 1
    assert {item["entity_id"] for item in entity_links[0]["links"]} == {"org-eb", "org-ex"}


def test_research_finding_observed_by_user_uses_research_run_provenance():
    store = object.__new__(GraphStore)
    seen: list[dict] = []

    def fake_run(query: str, **params):
        seen.append(params)
        if "MATCH (run:ResearchRun" in query and "PRODUCED" in query:
            return [{"count": 1 if params["canonical_user_id"] == "alice-id" else 0}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    assert store.research_finding_observed_by_user("alice-id", "f1") is True
    assert store.research_finding_observed_by_user("bob-id", "f1") is False


def test_finding_curation_fingerprint_ignores_free_form_intent():
    a = _frame_a()
    b = _frame_same_semantics_other_ids()
    b["intent"] = "show me the 2025 invoices between these companies"
    assert query_frame_hash(a) != query_frame_hash(b)
    assert curation_frame_hash(a) == curation_frame_hash(b)
    # Finding IDs retain the legacy full-frame formula for upgrade compatibility.
    assert finding_id("files:66732", a) != finding_id("files:66732", b)


def test_store_reuses_existing_finding_by_curation_hash_even_when_intent_differs():
    store = object.__new__(GraphStore)
    calls: list[tuple[str, dict]] = []
    prior_id = "legacy-finding-id"

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "RETURN DISTINCT e.entity_id AS entity_id" in query:
            return []
        if "properties(f)['curation_hash']" in query and "ORDER BY properties(f)['created_at'] ASC" in query:
            return [{"finding_id": prior_id}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    frame = _frame_a()
    frame["intent"] = "different wording of the same structured research"
    result = store.store_research_findings(
        query_id="q-second",
        query_frame=frame,
        documents=[{
            "document_id": "files:66732",
            "title": "same.pdf",
            "verification_status": "match",
            "relation_binding": "direct",
            "evidence_frame": {},
        }],
    )
    assert result["finding_ids"] == [prior_id]
    write = next(params for query, params in calls if "MERGE (f:ResearchFinding:AKIResearchFinding" in query)
    assert write["rows"][0]["finding_id"] == prior_id
    assert write["curation_hash"] == curation_frame_hash(frame)


def test_evidence_frame_view_and_candidates_surface_relation_endpoints():
    evidence = {
        "constraints": [{"kind": "betroffene Person", "status": "match", "value": "Alice Example"}],
        "entities": [],
        "mentioned_entities": [],
        "relations": [
            {"source": "Bob Example", "predicate": "adressiert ein Schreiben an", "target": "Alice Example"},
            {"source": "Alice Example", "predicate": "wird in einer E-Mail angesprochen", "target": "Bob Example"},
        ],
    }
    view = evidence_frame_view(evidence)
    assert view["has_content"] is True
    assert view["relations"][0]["predicate"] == "adressiert ein Schreiben an"
    candidates = evidence_entity_candidates(evidence)
    assert [item["text"] for item in candidates] == ["Alice Example", "Bob Example"]
    assert merge_entity_candidates(
        [{"text": "Alice Example", "role": "query"}], candidates
    ) == [
        {"text": "Alice Example", "role": "query"},
        {"text": "Bob Example", "role": "Relationsquelle"},
    ]


def test_relation_targets_are_not_promoted_to_entity_candidates():
    evidence = {
        "relations": [{
            "source": "Alice Example",
            "predicate": "klärt",
            "target": "Belege und deren Buchung durch eine Kollegin",
        }]
    }
    assert evidence_entity_candidates(evidence) == [
        {"text": "Alice Example", "role": "Relationsquelle"}
    ]
    assert merge_entity_candidates([
        {"text": "alter Zieltext", "role": "Relationsziel"},
        {"text": "Example Automation AG", "role": "Erwähnung"},
    ]) == [{"text": "Example Automation AG", "role": "Erwähnung"}]


def test_evidence_candidates_ignore_planner_local_relation_ids():
    evidence = {"relations": [{"source": "e1", "predicate": "works at", "target": "q2"}]}
    assert evidence_entity_candidates(evidence) == []
