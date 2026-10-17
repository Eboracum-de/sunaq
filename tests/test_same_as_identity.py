from pathlib import Path

from rag.graph import GraphStore
from rag.graph_entities import detect_known_entities


ROOT = Path(__file__).resolve().parents[1]


def _summary(entity_id: str):
    return {
        "entity_id": entity_id,
        "display_name": "John Doe" if entity_id == "A" else "John Alexander Doe",
        "labels": ["Entity", "Person"],
        "identity_status": "seeded",
        "contact_records": 1,
        "document_mentions": 0,
        "observations": 0,
    }


def test_same_as_is_non_destructive_and_does_not_change_entity_status():
    store = object.__new__(GraphStore)
    calls = []
    store._entity_curation_summary = _summary
    store.refresh_possible_same_as = lambda entity_id: []

    def fake_run(query, **params):
        calls.append((query, params))
        return []

    store._run = fake_run
    result = store.confirm_same_as("A", "B")

    assert result["status"] == "same_as"
    rendered = "\n".join(query for query, _ in calls)
    assert "MERGE (a)-[r:SAME_AS]->(b)" in rendered
    assert "identity_status='merged'" not in rendered
    assert "identity_status='confirmed'" not in rendered
    assert "MERGED_INTO" not in rendered


def test_reject_same_pair_supersedes_same_as():
    store = object.__new__(GraphStore)
    calls = []
    store._entity_curation_summary = _summary

    def fake_run(query, **params):
        calls.append((query, params))
        return []

    store._run = fake_run
    result = store.reject_merge_candidate("A", "B")

    assert result["status"] == "not_same_as"
    rendered = "\n".join(query for query, _ in calls)
    assert "type(c) IN ['POSSIBLE_SAME_AS','SAME_AS']" in rendered
    assert "MERGE (a)-[r:NOT_SAME_AS]->(b)" in rendered


def test_same_as_component_is_transitive_and_keeps_only_active_entities():
    store = object.__new__(GraphStore)

    def fake_run(query, **params):
        if "LIMIT 1" in query and "identity_status" in query:
            return [{"entity_id": params["entity_id"]}]
        if "UNWIND $frontier" in query:
            frontier = set(params["frontier"])
            if frontier == {"A"}:
                return [{"entity_id": "B"}]
            if frontier == {"B"}:
                return [{"entity_id": "A"}, {"entity_id": "C"}]
            if frontier == {"C"}:
                return [{"entity_id": "B"}]
        return []

    store._run = fake_run
    assert store.same_as_component_ids("A") == ["A", "B", "C"]


class _SameAsGraph:
    def name_forms(self, include_inactive_names=True):
        return [
            {
                "entity_id": "A", "labels": ["Entity", "Person"],
                "display_name": "John Doe", "value": "John Doe",
                "normalized": "john doe", "form_type": "name", "weight": 1.0,
            },
            {
                "entity_id": "B", "labels": ["Entity", "Person"],
                "display_name": "John Alexander Doe", "value": "John Doe",
                "normalized": "john doe", "form_type": "alias", "weight": 0.96,
            },
        ]

    def same_as_component_ids(self, entity_id):
        return ["A", "B"] if entity_id in {"A", "B"} else [entity_id]

    def entity_search_forms(self, entity_id):
        return [
            {"value": "John Doe", "normalized": "john doe", "weight": 1.0, "kind": "name", "active": True, "source": "name"},
            {"value": "John Alexander Doe", "normalized": "john alexander doe", "weight": 0.98, "kind": "same_as_name", "active": True, "source": "name"},
        ]

    def candidate_context_links(self, candidate_ids, resolved_ids):
        return {}


def test_query_exact_match_across_same_as_group_is_not_ambiguous():
    result = detect_known_entities("Was weißt Du über John Doe?", _SameAsGraph(), fuzzy=False)
    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.status == "resolved"
    assert entity.resolution_method == "same_as_equivalent_form"
    assert {c.entity_id for c in entity.candidates} == {"A", "B"}
    assert {f["normalized"] for f in entity.search_forms} == {
        "john doe", "john alexander doe"
    }


def test_candidate_queue_collapses_same_as_components_and_exposes_user_provenance():
    store = object.__new__(GraphStore)

    def fake_run(query, **params):
        if "type(r)='POSSIBLE_SAME_AS'" in query:
            return [
                {
                    "left_entity_id": "A", "left_name": "John Doe", "left_labels": ["Entity", "Person"],
                    "right_entity_id": "B", "right_name": "John Doe", "right_labels": ["Entity", "Person"],
                    "score": 1.0, "reason": "exact_shared_form", "status": "candidate",
                    "suggested_by": "identity_similarity_v1", "matched_left_form": "John Doe",
                    "matched_right_form": "John Doe", "carried_from_merge_entity_id": None,
                },
                {
                    "left_entity_id": "A", "left_name": "John Doe", "left_labels": ["Entity", "Person"],
                    "right_entity_id": "C", "right_name": "John Doe", "right_labels": ["Entity", "Person"],
                    "score": 1.0, "reason": "exact_shared_form", "status": "candidate",
                    "suggested_by": "identity_similarity_v1", "matched_left_form": "John Doe",
                    "matched_right_form": "John Doe", "carried_from_merge_entity_id": None,
                },
                {
                    "left_entity_id": "B", "left_name": "John Doe", "left_labels": ["Entity", "Person"],
                    "right_entity_id": "C", "right_name": "John Doe", "right_labels": ["Entity", "Person"],
                    "score": 1.0, "reason": "exact_shared_form", "status": "candidate",
                    "suggested_by": "identity_similarity_v1", "matched_left_form": "John Doe",
                    "matched_right_form": "John Doe", "carried_from_merge_entity_id": None,
                },
            ]
        if "type(r) IN ['SAME_AS','NOT_SAME_AS']" in query:
            return [{"left_entity_id": "A", "right_entity_id": "C", "relation_type": "SAME_AS"}]
        if "OPTIONAL MATCH (c:ContactRecord)-[cr]->(e)" in query and "type(cr)='DESCRIBES'" in query:
            return [
                {
                    "entity_id": "A", "display_name": "John Doe", "labels": ["Entity", "Person"],
                    "sources": [{"contact_id": "ca", "cloud_id": "nextcloud", "source_user_id": "user-a", "addressbook_name": "Kontakte", "addressbook_slug": "kontakte"}],
                },
                {
                    "entity_id": "B", "display_name": "John Doe", "labels": ["Entity", "Person"],
                    "sources": [{"contact_id": "cb", "cloud_id": "nextcloud", "source_user_id": "user-b", "addressbook_name": "Kontakte", "addressbook_slug": "kontakte"}],
                },
                {
                    "entity_id": "C", "display_name": "John Doe", "labels": ["Entity", "Person"],
                    "sources": [{"contact_id": "cc", "cloud_id": "nextcloud", "source_user_id": "user-a", "addressbook_name": "Geschäftlich", "addressbook_slug": "geschaeftlich"}],
                },
            ]
        return []

    store._run = fake_run

    rows = store.list_merge_candidates()
    assert len(rows) == 1
    row = rows[0]
    assert set(row["left_component_ids"]) in ({"A", "C"}, {"B"})
    assert {source["source_user_id"] for source in row["left_sources"] + row["right_sources"]} == {"user-a", "user-b"}
    assert len(store.list_merge_candidates(source_user_id="user-a")) == 1
    assert len(store.list_merge_candidates(source_user_id="user-b")) == 1
    assert store.list_merge_candidates(source_user_id="user-c") == []


def test_component_level_not_same_hides_redundant_cross_candidate():
    store = object.__new__(GraphStore)

    def fake_run(query, **params):
        if "type(r)='POSSIBLE_SAME_AS'" in query:
            return [
                {
                    "left_entity_id": "A", "left_name": "John Doe", "left_labels": ["Entity", "Person"],
                    "right_entity_id": "B", "right_name": "John Doe", "right_labels": ["Entity", "Person"],
                    "score": 1.0, "reason": "exact_shared_form", "status": "candidate",
                    "suggested_by": "identity_similarity_v1", "matched_left_form": "John Doe",
                    "matched_right_form": "John Doe", "carried_from_merge_entity_id": None,
                },
                {
                    "left_entity_id": "C", "left_name": "John Doe", "left_labels": ["Entity", "Person"],
                    "right_entity_id": "B", "right_name": "John Doe", "right_labels": ["Entity", "Person"],
                    "score": 1.0, "reason": "exact_shared_form", "status": "candidate",
                    "suggested_by": "identity_similarity_v1", "matched_left_form": "John Doe",
                    "matched_right_form": "John Doe", "carried_from_merge_entity_id": None,
                },
            ]
        if "type(r) IN ['SAME_AS','NOT_SAME_AS']" in query:
            return [
                {"left_entity_id": "A", "right_entity_id": "C", "relation_type": "SAME_AS"},
                {"left_entity_id": "A", "right_entity_id": "B", "relation_type": "NOT_SAME_AS"},
            ]
        if "OPTIONAL MATCH (c:ContactRecord)-[cr]->(e)" in query:
            return [
                {"entity_id": "A", "display_name": "John Doe", "labels": ["Entity", "Person"], "sources": []},
                {"entity_id": "B", "display_name": "John Doe", "labels": ["Entity", "Person"], "sources": []},
                {"entity_id": "C", "display_name": "John Doe", "labels": ["Entity", "Person"], "sources": []},
            ]
        return []

    store._run = fake_run
    assert store.list_merge_candidates() == []


def test_candidate_ui_uses_same_as_not_survivor_buttons():
    template = (ROOT / "rag/templates/admin/candidates.html").read_text(encoding="utf-8")
    base = (ROOT / "rag/templates/admin/base.html").read_text(encoding="utf-8")
    admin = (ROOT / "rag/admin_ui.py").read_text(encoding="utf-8")

    assert "Identitäts-Kandidaten" in template
    assert "Identisch …" in template
    assert "Verschieden …" in template
    assert "A behalten" not in template
    assert "B behalten" not in template
    assert "admin_candidate_same" in template
    assert "Identitäts-Kandidaten" in base
    assert 'name="admin_candidate_same"' in admin
    assert "Nutzerkontext" in template
    assert "source.source_user_id" in template
    assert "canonical_user_id" in template
