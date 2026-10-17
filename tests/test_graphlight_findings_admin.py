from __future__ import annotations

from pathlib import Path

from rag.graph import GraphStore


def test_finding_list_derives_graph_lite_states():
    store = object.__new__(GraphStore)
    rows = [
        {"finding_id": "f0", "entity_texts": [], "curated_entities": [], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 0},
        {"finding_id": "f1", "entity_texts": ["A"], "curated_entities": [], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 0},
        {"finding_id": "f2", "entity_texts": ["A", "B"], "curated_entities": [{"text": "A", "entity_id": "e1"}, {"text": "B", "entity_id": "e2"}], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 0},
        {"finding_id": "f3", "entity_texts": ["A", "B"], "curated_entities": [{"text": "A", "entity_id": "e1"}, {"text": "B", "entity_id": "e2"}], "suppressed_entity_texts": [], "claim_count": 1, "review_count": 0},
        {"finding_id": "f4", "entity_texts": ["A"], "curated_entities": [], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 0, "curator_status": "suppressed"},
        {"finding_id": "f5", "entity_texts": ["A", "B"], "curated_entities": [{"text": "A", "entity_id": "e1"}, {"text": "B", "entity_id": "e2"}], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 0, "curator_status": "mentions_only"},
        {"finding_id": "f6", "entity_texts": ["A", "B"], "curated_entities": [{"text": "A", "entity_id": "e1"}, {"text": "B", "entity_id": "e2"}], "suppressed_entity_texts": [], "claim_count": 0, "review_count": 1},
    ]
    store._run = lambda query, **params: rows  # type: ignore[method-assign]
    result = store.list_research_findings()
    assert [row["graph_state"] for row in result] == [
        "no_entity", "open", "entities_resolved", "claimed", "suppressed", "mentions_only", "review_required"
    ]


def test_finding_entity_options_separates_auto_curated_and_suppressed():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "finding": {"entity_texts": ["RAW", "B280", "Noise"], "entity_roles": ["source", "target", ""] , "suppressed_entity_texts": ["Noise"]},
        "curated_entities": [{"text": "RAW", "entity_id": "e1", "display_name": "R.A.w. Capital"}],
        "resolved_entities": [{"text": "B280", "entity_id": "e2", "display_name": "Beispiel 280 VV UG"}],
    }
    store.find_entities = lambda text, limit=6: [{"entity_id": f"s-{text}", "display_name": text}]  # type: ignore[method-assign]
    store._resolve_existing_query_entity = lambda text: None  # type: ignore[method-assign]
    store._entity_form_decision = lambda text: None  # type: ignore[method-assign]
    result = store.research_finding_entity_options("f")
    assert result[0]["curated"]["entity_id"] == "e1"
    assert result[1]["auto_resolved"]["entity_id"] == "e2"
    assert result[2]["suppressed"] is True


def test_claim_preview_requires_language_neutral_predicate_and_curated_endpoints():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        return [{
            "document_id": "files:1",
            "subject_name": "A",
            "subject_labels": ["Entity", "Organization"],
            "subject_kind": "Company",
            "object_name": "B",
            "object_labels": ["Entity", "Organization"],
            "object_kind": "Company",
            "intent": "A relationship B",
        }]

    store._run = fake_run  # type: ignore[method-assign]
    preview = store.research_finding_claim_preview(
        "f1", subject_entity_id="e1", predicate_id="shareholder_of", object_entity_id="e2"
    )
    assert preview["predicate_id"] == "SHAREHOLDER_OF"
    assert preview["predicate_label"] == "Gesellschafter/in von"
    assert preview["subject"]["display_name"] == "A"
    assert preview["object"]["display_name"] == "B"
    assert calls


def test_claim_creation_stays_document_grounded_relation_observation():
    store = object.__new__(GraphStore)
    calls = []
    store.research_finding_claim_preview = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "finding_id": "f1", "document_id": "files:1", "relation_id": "r1",
        "subject": {"entity_id": "e1", "display_name": "A"},
        "predicate_id": "SHAREHOLDER_OF", "predicate_label": "Gesellschafter/in von",
        "object": {"entity_id": "e2", "display_name": "B"}, "claim_text": "", "effect": "x", "note": "x",
        "ontology": {"name": "nextcloud-rag-relations-v1", "version": 1, "hash": "test"},
    }
    store.research_finding_detail = lambda finding_id: {"finding": {"intent": "invoice query"}}  # type: ignore[method-assign]
    store._run = lambda query, **params: calls.append((query, params)) or []  # type: ignore[method-assign]
    result = store.create_research_finding_claim(
        "f1", subject_entity_id="e1", predicate_id="SHAREHOLDER_OF", object_entity_id="e2"
    )
    assert result["status"] == "claim_created"
    query = calls[-1][0]
    assert "RelationObservation" in query
    assert "DERIVED_FROM_FINDING" in query
    assert "HAS_RELATION_OBSERVATION" in query


def test_relink_restores_durable_research_finding_mention_even_without_extractor_mention():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "MATCH (o:EntityObservation {document_id:$document_id})" in query and "research_finding_entity" in query:
            return [{
                "observation_id": "o1",
                "curator_status": "research_finding_entity",
                "target_entity_id": "e1",
                "observed_text": "Beispiel GmbH",
                "normalized": "beispiel gmbh",
            }]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    store.replace_document_mentions(document_id="files:1", mentions=[], content_hash="h", extractor="graph-v1")
    assert any("research_finding_curated=true" in query and params.get("entity_id") == "e1" for query, params in calls)


def test_relation_reindex_preserves_manual_research_finding_claims():
    store = object.__new__(GraphStore)
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or []  # type: ignore[method-assign]
    store.replace_document_relation_observations(document_id="files:1", observations=[], extractor="graph-v1")
    delete_query = calls[0][0]
    assert "curator_status" in delete_query
    assert "manual_claim" in delete_query
    assert "research_finding_curator" in delete_query


def test_entity_change_marks_manual_claim_for_review():
    store = object.__new__(GraphStore)
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or []  # type: ignore[method-assign]
    store._mark_research_finding_claims_for_review("f1", reason="finding_entity_reassigned")
    query, params = calls[0]
    assert "review_required" in query
    assert "DERIVED_FROM_FINDING" in query
    assert params["finding_id"] == "f1"


def test_bulk_no_entity_cleanup_suppresses_without_deleting_provenance():
    store = object.__new__(GraphStore)
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or [{"updated": 7}]  # type: ignore[method-assign]
    result = store.suppress_research_findings_without_entities()
    assert result["updated"] == 7
    query = calls[0][0]
    assert "size(coalesce(properties(f)['entity_texts'],[])) = 0" in query
    assert "DETACH DELETE" not in query
    assert "curator_status='suppressed'" in query


def test_manual_claim_prefers_evidence_frame_over_query_intent():
    store = object.__new__(GraphStore)
    calls = []
    store.research_finding_claim_preview = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "finding_id": "f1", "document_id": "files:1", "relation_id": "r1",
        "subject": {"entity_id": "e1", "display_name": "A"},
        "predicate_id": "SHAREHOLDER_OF", "predicate_label": "Gesellschafter/in von",
        "object": {"entity_id": "e2", "display_name": "B"}, "claim_text": "", "effect": "x", "note": "x",
        "ontology": {"name": "nextcloud-rag-relations-v1", "version": 1, "hash": "test"},
    }
    store.research_finding_detail = lambda finding_id: {"finding": {  # type: ignore[method-assign]
        "intent": "search intent must not become evidence",
        "evidence_frame_json": '{"evidence":"invoice passage"}',
    }}
    store._run = lambda query, **params: calls.append((query, params)) or []  # type: ignore[method-assign]
    store.create_research_finding_claim(
        "f1", subject_entity_id="e1", predicate_id="SHAREHOLDER_OF", object_entity_id="e2"
    )
    assert calls[-1][1]["evidence_text"] == '{"evidence":"invoice passage"}'


def test_admin_findings_javascript_is_external_and_csp_allows_only_self_scripts():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    findings = (root / "rag/templates/admin/findings.html").read_text()
    entity = (root / "rag/templates/admin/entity.html").read_text()
    base = (root / "rag/templates/admin/base.html").read_text()
    admin_ui = (root / "rag/admin_ui.py").read_text()
    admin_js = (root / "rag/static/admin/admin.js").read_text()

    assert "<script>" not in findings
    assert "onsubmit=" not in findings
    assert "onsubmit=" not in entity
    assert "admin_js" in base
    assert "script-src 'self'" in admin_ui
    assert "script-src 'none'" not in admin_ui
    assert '.bulk-toggle' in admin_js
    assert 'input[name="finding_id"]' in admin_js
    assert "confirm-submit" in admin_js


def test_admin_layout_warns_when_live_acl_is_disabled():
    root = Path(__file__).resolve().parents[1]
    base = (root / "rag/templates/admin/base.html").read_text()
    admin_ui = (root / "rag/admin_ui.py").read_text()
    assert "acl_disabled_warning" in admin_ui
    assert "Live Nextcloud ACL ist deaktiviert." in base
    assert "aktuellen Nextcloud-Berechtigungen" in base


def test_admin_js_asset_is_registered_and_backed_by_static_file():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    admin_ui = (root / "rag/admin_ui.py").read_text()
    assert 'JS_FILE = HERE / "static" / "admin" / "admin.js"' in admin_ui
    assert '@router.get("/assets/admin.js"' in admin_ui
    assert (root / "rag/static/admin/admin.js").is_file()


def test_claim_preview_rejects_free_predicate_even_for_curated_endpoints():
    store = object.__new__(GraphStore)
    store._run = lambda query, **params: [{  # type: ignore[method-assign]
        "document_id": "files:1",
        "subject_name": "A",
        "subject_labels": ["Entity", "Organization"],
        "subject_kind": "Company",
        "object_name": "B",
        "object_labels": ["Entity", "Organization"],
        "object_kind": "Company",
    }]
    try:
        store.research_finding_claim_preview(
            "f1", subject_entity_id="e1", predicate_id="made_up_relation", object_entity_id="e2"
        )
    except ValueError as exc:
        assert "nicht Teil der Relation-Ontologie" in str(exc)
    else:
        raise AssertionError("free predicate unexpectedly accepted")


def test_claim_options_are_filtered_by_entity_type_and_kind():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "curated_entities": [
            {
                "text": "Company",
                "entity_id": "org",
                "display_name": "Example GmbH",
                "labels": ["Entity", "Organization"],
                "entity_kind": "Company",
            },
            {
                "text": "Court",
                "entity_id": "court",
                "display_name": "Amtsgericht Bonn",
                "labels": ["Entity", "Organization"],
                "entity_kind": "Court",
            },
        ]
    }
    pairs = store.research_finding_claim_options("f1")
    org_to_court = next(
        pair for pair in pairs
        if pair["subject"]["entity_id"] == "org" and pair["object"]["entity_id"] == "court"
    )
    predicate_ids = {item["id"] for item in org_to_court["predicates"]}
    assert "REGISTERED_AT" in predicate_ids


def test_mentions_only_requires_completed_entity_decisions_and_no_active_claim():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "finding": {"entity_texts": ["A", "B"], "suppressed_entity_texts": []},
        "curated_entities": [
            {"text": "A", "entity_id": "e1"},
            {"text": "B", "entity_id": "e2"},
        ],
        "claims": [],
    }
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or [{  # type: ignore[method-assign]
        "finding_id": "f1", "curator_status": params["status"], "curator_reason": params["reason"]
    }]
    result = store.curate_research_finding("f1", status="mentions_only", reason="no semantic relation")
    assert result["curator_status"] == "mentions_only"
    assert calls[-1][1]["status"] == "mentions_only"


def test_admin_claim_ui_uses_ontology_select_not_free_predicate_input():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    finding = (root / "rag/templates/admin/finding.html").read_text()
    assert '<select name="predicate_id"' in finding
    assert '<input name="predicate_id"' not in finding
    assert "Mentions speichern – keinen Claim anlegen" in finding
    assert "Review nötig" in finding


def test_curated_finding_claims_are_not_graph_retrieval_signals():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append(query)
        return []

    store._run = fake_run  # type: ignore[method-assign]
    store.retrieve_documents_for_entities(["e1", "e2"], limit=10)
    relation_query = next(query for query in calls if "HAS_RELATION_OBSERVATION" in query)
    assert "research_finding_curator" in relation_query


def test_review_dismiss_keeps_claim_as_superseded_provenance():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "claims": [{
            "relation_id": "r1",
            "curator_status": "review_required",
            "predicate": "SHAREHOLDER_OF",
            "subject_entity_id": "e1",
            "object_entity_id": "e2",
        }]
    }
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or [{  # type: ignore[method-assign]
        "relation_id": "r1", "curator_status": params["status"]
    }]
    result = store.review_research_finding_claim(
        "f1", relation_id="r1", action="dismiss", reason="entity changed"
    )
    assert result["curator_status"] == "superseded"
    query, params = calls[-1]
    assert "DETACH DELETE" not in query
    assert params["status"] == "superseded"


def test_research_run_dismissal_uses_run_context_not_every_raw_finding():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    admin_ui = (root / "rag/admin_ui.py").read_text()
    start = admin_ui.index('@router.post("/findings/run/{run_id}/dismiss"')
    end = admin_ui.index('@router.post("/findings/run/{run_id}/dismiss-findings"', start)
    handler = admin_ui[start:end]

    assert "_require_admin_research_run_context(canonical_user_id, run_id)" in handler
    assert "_require_admin_finding_context" not in handler
    assert "except HTTPException:" in handler


def test_research_run_list_supports_single_and_bulk_dismissal():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    findings = (root / "rag/templates/admin/findings.html").read_text()
    admin_ui = (root / "rag/admin_ui.py").read_text()
    admin_js = (root / "rag/static/admin/admin.js").read_text()

    assert "admin_research_run_dismiss" in findings
    assert "admin_research_runs_dismiss" in findings
    assert 'name="run_id"' in findings
    assert "bulk-run-toggle" in findings
    assert '@router.post("/findings/runs/dismiss"' in admin_ui
    assert ".bulk-run-form" in admin_js
    assert 'input[name="run_id"].bulk-run-item:checked' in admin_js


def test_finding_detail_renders_readable_evidence_and_entity_search():
    root = Path(__file__).resolve().parents[1]
    finding = (root / "rag/templates/admin/finding.html").read_text()
    admin_ui = (root / "rag/admin_ui.py").read_text()
    research = (root / "rag/research_findings.py").read_text()

    assert "Verifier-Beobachtung · noch kein Ontologie-Claim" in finding
    assert "Rohdaten anzeigen" in finding
    assert 'name="entity_q"' in finding
    assert 'name="target_entity_id"' in finding
    assert "evidence_frame_view" in admin_ui
    assert "evidence_entity_candidates" in research


def test_evidence_relation_endpoint_uses_unique_exact_seed_resolution():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "finding": {
            "entity_texts": ["Alice Example"],
            "entity_roles": ["betroffene Person"],
            "evidence_frame_json": (
                '{"relations":[{"source":"Bob Example",'
                '"predicate":"adressiert ein Schreiben an","target":"Alice Example"}]}'
            ),
        },
        "curated_entities": [],
        "resolved_entities": [
            {"text": "Alice Example", "entity_id": "person-alice", "display_name": "Alice Example"}
        ],
    }
    store._resolve_existing_query_entity = lambda text: (  # type: ignore[method-assign]
        {"entity_id": "person-bob", "display_name": "Bob Example", "labels": ["Entity", "Person"]}
        if text == "Bob Example"
        else None
    )
    store.find_entities = lambda text, limit=6: []  # type: ignore[method-assign]
    store._entity_form_decision = lambda text: None  # type: ignore[method-assign]

    result = store.research_finding_entity_options("f")

    eichner = next(item for item in result if item["text"] == "Bob Example")
    assert eichner["role"] == "Relationsquelle"
    assert eichner["auto_resolved"]["entity_id"] == "person-bob"


def test_relation_observation_optional_finding_reads_are_property_safe():
    store = object.__new__(GraphStore)
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or []  # type: ignore[method-assign]

    store.ensure_schema()

    rendered = "\n".join(query for query, _params in calls)
    assert "RAGSchemaMarker" not in rendered
    graph_source = (Path(__file__).resolve().parents[1] / "rag/graph.py").read_text()
    for field in ("predicate_text", "relation_text", "evidence_text", "curator_note", "review_reason"):
        assert f"properties(claim)['{field}']" in graph_source
        assert f"{field}: claim.{field}" not in graph_source


def test_active_manual_claim_can_be_withdrawn_without_deleting_provenance():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "claims": [{
            "relation_id": "r1",
            "curator_status": "manual_claim",
            "predicate": "REPRESENTS",
            "subject_entity_id": "e1",
            "object_entity_id": "e2",
        }]
    }
    calls = []
    store._run = lambda query, **params: calls.append((query, params)) or [{  # type: ignore[method-assign]
        "relation_id": "r1", "curator_status": params["status"]
    }]

    result = store.review_research_finding_claim(
        "f1", relation_id="r1", action="withdraw", reason="falsch zugeordnet"
    )

    assert result["curator_status"] == "withdrawn"
    query, params = calls[-1]
    assert "DETACH DELETE" not in query
    assert params["status"] == "withdrawn"
    assert params["reason"] == "falsch zugeordnet"


def test_finding_claim_ui_uses_one_filtered_triple_selector_and_mentions_completion():
    root = Path(__file__).resolve().parents[1]
    finding = (root / "rag/templates/admin/finding.html").read_text()
    admin_js = (root / "rag/static/admin/admin.js").read_text()
    admin_ui = (root / "rag/admin_ui.py").read_text()

    assert finding.count('data-claim-builder') == 1
    assert 'select name="subject_entity_id"' in finding
    assert 'select name="predicate_id"' in finding
    assert 'select name="object_entity_id"' in finding
    assert 'data-subject="{{ pair.subject.entity_id }}"' in finding
    assert "Mentions speichern – keinen Claim anlegen" in finding
    assert "Mention gespeichert" in finding
    assert 'value="withdraw"' in finding
    assert "Claim zurücknehmen" in finding
    assert "applyCompatibility" in admin_js
    assert '{"confirm", "dismiss", "withdraw"}' in admin_ui



def test_observation_review_queue_defaults_and_manual_filter_are_explicit():
    store = object.__new__(GraphStore)
    calls = []
    raw_rows = [
        {"observation_id": "pending", "status": "unresolved", "curator_status": ""},
        {"observation_id": "confirmed", "status": "resolved_existing",
         "curator_status": "research_finding_entity"},
        {"observation_id": "non-entity", "status": "rejected",
         "curator_status": "manual_not_entity"},
    ]
    store._run = lambda query, **params: calls.append((query, params)) or raw_rows  # type: ignore[method-assign]

    rows = store.list_observations()
    query, params = calls[-1]
    assert params["status"] == "needs_review"
    assert "$status='needs_review'" in query
    assert "coalesce(properties(o)['curator_status'],'')=''" in query
    assert "['created_provisional','ambiguous','unresolved']" in query
    assert "$status <> 'all' AND $status <> 'needs_review'" in query
    assert [row["observation_id"] for row in rows] == ["pending"]
    assert rows[0]["needs_review"] is True
    assert rows[0]["status_label"] == "nicht aufgelöst"

    rows = store.list_observations(status="manual_not_entity")
    query, params = calls[-1]
    assert params["status"] == "manual_not_entity"
    assert "coalesce(properties(o)['curator_status'],'')=$status" in query
    assert [row["observation_id"] for row in rows] == ["non-entity"]

def test_suppressed_finding_entity_is_hidden_only_from_curated_evidence_view():
    from rag.admin_ui import _filter_suppressed_evidence

    raw_view = {
        "concepts": [],
        "constraints": [],
        "entities": [],
        "mentioned_entities": [
            {"value": "Bob Example"},
            {"value": "Office t-m"},
        ],
        "relations": [
            {"source": "Alice Example", "predicate": "schreibt an", "target": "Bob Example"},
            {"source": "Office t-m", "predicate": "ist", "target": "Absender"},
        ],
        "has_content": True,
    }

    curated = _filter_suppressed_evidence(raw_view, ["Office t-m"])

    assert [item["value"] for item in curated["mentioned_entities"]] == ["Bob Example"]
    assert len(curated["relations"]) == 1
    assert raw_view["mentioned_entities"][1]["value"] == "Office t-m"


def test_observation_and_relation_admin_views_are_document_acl_scoped():
    root = Path(__file__).resolve().parents[1]
    admin_ui = (root / "rag/admin_ui.py").read_text()
    observations = (root / "rag/templates/admin/observations.html").read_text()
    relations = (root / "rag/templates/admin/relations.html").read_text()
    observation = (root / "rag/templates/admin/observation.html").read_text()
    base = (root / "rag/templates/admin/base.html").read_text()

    assert "def _acl_filter_document_rows_for_canonical_user" in admin_ui
    assert "rows, acl_error = _acl_filter_document_rows_for_canonical_user(" in admin_ui
    assert 'status: str = "needs_review"' in admin_ui
    assert "Observation is not visible in the selected Nextcloud user context" in admin_ui
    assert 'name="canonical_user_id"' in observations
    assert 'name="canonical_user_id"' in relations
    assert 'name="canonical_user_id"' in observation
    assert "Verifier-Rohdaten anzeigen" in relations
    assert "request.query_params.get('canonical_user_id', '')" in base



def test_global_non_entity_form_is_not_offered_again_in_other_finding():
    store = object.__new__(GraphStore)
    store.research_finding_detail = lambda finding_id: {  # type: ignore[method-assign]
        "finding": {
            "entity_texts": ["Office t-m"],
            "entity_roles": ["Erwähnung"],
            "evidence_frame_json": "{}",
            "suppressed_entity_texts": [],
        },
        "curated_entities": [],
        "resolved_entities": [],
    }
    store._entity_form_decision = lambda text: {  # type: ignore[method-assign]
        "normalized": "office t m", "status": "not_entity", "value": "Office t-m"
    }
    store._resolve_existing_query_entity = lambda text: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("globally rejected form must not be resolved")
    )
    store.find_entities = lambda text, limit=6: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("globally rejected form must not be suggested")
    )

    option = store.research_finding_entity_options("other-finding")[0]

    assert option["suppressed"] is True
    assert option["suppression_scope"] == "global"
    assert option["auto_resolved"] is None
    assert option["suggestions"] == []


def test_historic_non_entity_observation_is_promoted_on_demand():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "MATCH (d:EntityFormDecision" in query:
            return []
        if "MATCH (o:EntityObservation" in query:
            return [{
                "value": "Office t-m",
                "curator_status": "manual_not_entity",
                "target_entity_id": "",
                "reason": "not a person or organization",
                "decided_at": "2026-09-19T12:00:00Z",
            }]
        if "MERGE (d:EntityFormDecision" in query:
            return []
        raise AssertionError(query)

    store._run = fake_run  # type: ignore[method-assign]
    decision = store._entity_form_decision("Office t-m")

    assert decision is not None
    assert decision["normalized"] == "office t m"
    assert decision["status"] == "not_entity"
    assert decision["decision_kind"] == "historic_observation"
    merge_query, merge_params = calls[-1]
    assert "MERGE (d:EntityFormDecision" in merge_query
    assert merge_params["status"] == "not_entity"


def test_unique_contextual_alias_is_a_default_finding_resolution():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "EntityFormDecision" in query or "MATCH (o:EntityObservation" in query:
            return []
        return [{
            "entity_id": "org-example-machinery",
            "display_name": "Example Machinery AG",
            "labels": ["Entity", "Organization"],
            "match_kind": "alias",
        }]

    store._run = fake_run  # type: ignore[method-assign]

    result = store._resolve_existing_query_entity("Example Machinery")

    assert result is not None
    assert result["entity_id"] == "org-example-machinery"
    assert result["match_kind"] == "alias"
    assert "HAS_SEARCH_ALIAS" in calls[-1][0]


def test_bulk_entity_defaults_applies_only_pending_unique_matches():
    store = object.__new__(GraphStore)
    store.research_finding_entity_options = lambda finding_id, limit=1: [  # type: ignore[method-assign]
        {
            "text": "Example Machinery",
            "curated": None,
            "suppressed": False,
            "auto_resolved": {
                "entity_id": "org-z",
                "display_name": "Example Machinery AG",
                "match_kind": "alias",
            },
        },
        {
            "text": "Office t-m",
            "curated": None,
            "suppressed": True,
            "auto_resolved": None,
        },
    ]
    calls = []
    store.curate_research_finding_entity = lambda finding_id, **kwargs: (  # type: ignore[method-assign]
        calls.append((finding_id, kwargs))
        or {"target_entity_id": kwargs["target_entity_id"], "target_display_name": "Example Machinery AG"}
    )

    result = store.accept_research_finding_entity_defaults("f1", curator_actor="tester")

    assert result["applied_count"] == 1
    assert calls[0][1]["entity_text"] == "Example Machinery"
    assert calls[0][1]["reason"] == "unique_alias_default"


def test_finding_entity_ui_is_direct_with_optional_details_and_alias_action():
    root = Path(__file__).resolve().parents[1]
    admin_ui = (root / "rag/admin_ui.py").read_text()
    finding = (root / "rag/templates/admin/finding.html").read_text()
    graph = (root / "rag/graph.py").read_text()

    assert "apply=not detail_requested" in admin_ui
    assert 'action == "accept_defaults"' in admin_ui
    assert 'name="action" value="assign_alias"' in finding
    assert "Als Alias anlegen" in finding
    assert 'name="detail" value="yes"' in finding
    assert "eindeutigen Treffer speichern" in finding
    assert "EntityFormDecision" in graph
    assert "entity_form_decision_normalized" in graph


def test_research_run_listing_fetches_exact_produced_findings_not_global_window():
    store = object.__new__(GraphStore)

    def fake_run(query: str, **params):
        if "MATCH (run:ResearchRun)" in query:
            return [{
                "run": {"run_id": "old-run", "curation_status": ""},
                "produced": [{"finding_id": "finding-2501", "disposition": "pending"}],
            }]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    seen = {}

    def fake_findings(*, query="", limit=200, finding_ids=None):
        seen["ids"] = finding_ids
        return [{"finding_id": "finding-2501", "graph_state": "open", "entity_texts": ["A"]}]

    store.list_research_findings = fake_findings  # type: ignore[method-assign]
    result = store.list_research_runs(state="all")

    assert seen["ids"] == ["finding-2501"]
    assert result[0]["finding_count"] == 1
    assert result[0]["findings"][0]["finding_id"] == "finding-2501"


def test_research_run_detail_fetches_only_its_produced_findings():
    store = object.__new__(GraphStore)
    store._run = lambda query, **params: [{  # type: ignore[method-assign]
        "run": {"run_id": "old-run"},
        "produced": [{"finding_id": "finding-2501", "disposition": "pending"}],
    }]
    seen = {}

    def fake_findings(*, query="", limit=200, finding_ids=None):
        seen["ids"] = finding_ids
        return [{"finding_id": "finding-2501", "graph_state": "open"}]

    store.list_research_findings = fake_findings  # type: ignore[method-assign]
    result = store.research_run_detail("old-run")

    assert result is not None
    assert seen["ids"] == ["finding-2501"]
    assert result["findings"][0]["finding_id"] == "finding-2501"


def test_targeted_finding_list_does_not_apply_global_2000_cap():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "MATCH (f:ResearchFinding)-[s:SUPPORTED_BY]->(d:Document)" in query:
            return [{
                "finding_id": "finding-2501",
                "document_id": "files:2501",
                "entity_texts": [],
                "entity_roles": [],
                "curated_entities": [],
                "suppressed_entity_texts": [],
                "claim_count": 0,
                "review_count": 0,
            }]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    result = store.list_research_findings(finding_ids=["finding-2501"])

    main_query, params = calls[0]
    assert "f.finding_id IN $finding_ids" in main_query
    assert params["finding_ids"] == ["finding-2501"]
    assert params["limit"] == 1
    assert result[0]["finding_id"] == "finding-2501"


def test_admin_and_self_service_guards_do_not_use_global_2000_finding_scan():
    root = Path(__file__).resolve().parents[1]
    admin_source = (root / "rag/admin_ui.py").read_text()
    curation_source = (root / "rag/curation_ui.py").read_text()
    assert "list_research_findings(limit=2000)" not in admin_source
    assert "list_research_findings(limit=2000)" not in curation_source
    assert "_require_admin_findings_context(canonical_user_id, finding_ids)" in admin_source


def test_acl_denied_uncurated_finding_cleanup_detaches_only_user_provenance_and_gc_orphans():
    store = object.__new__(GraphStore)
    calls = []

    def fake_run(query: str, **params):
        calls.append((query, params))
        if "RETURN count(p) AS edge_count" in query:
            return [{"edge_count": 2, "finding_ids": ["f1"]}]
        if "RETURN f.finding_id AS finding_id" in query:
            return [{"finding_id": "f1"}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    result = store.purge_denied_uncurated_research_findings_for_user(
        "user-a", ["f1", "f1"]
    )

    assert result == {
        "requested": 1,
        "detached_edges": 2,
        "detached_findings": 1,
        "deleted_findings": 1,
    }
    eligibility_query, eligibility_params = calls[0]
    detach_query, detach_params = calls[1]
    orphan_query, orphan_params = calls[2]
    delete_query, delete_params = calls[3]
    assert "canonical_user_id:$canonical_user_id" in eligibility_query
    assert "NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }" in eligibility_query
    assert "NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }" in eligibility_query
    assert "size(coalesce(properties(f)['suppressed_entity_texts'],[]))=0" in eligibility_query
    assert eligibility_params["canonical_user_id"] == "user-a"
    assert eligibility_params["finding_ids"] == ["f1"]
    assert "DELETE p" in detach_query
    assert "curator_status" in detach_query
    assert "suppressed_entity_texts" in detach_query
    assert "NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }" in detach_query
    assert "NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }" in detach_query
    assert detach_params["finding_ids"] == ["f1"]
    assert "NOT EXISTS { MATCH (:ResearchRun)-[:PRODUCED]->(f) }" in orphan_query
    assert orphan_params["finding_ids"] == ["f1"]
    assert "DETACH DELETE f" in delete_query
    assert "curator_status" in delete_query
    assert "suppressed_entity_texts" in delete_query
    assert "NOT EXISTS { MATCH (f)-[:CURATED_ENTITY]->(:Entity) }" in delete_query
    assert "NOT EXISTS { MATCH (:RelationObservation)-[:DERIVED_FROM_FINDING]->(f) }" in delete_query
    assert "NOT EXISTS { MATCH (:ResearchRun)-[:PRODUCED]->(f) }" in delete_query
    assert delete_params["finding_ids"] == ["f1"]


def test_acl_denied_cleanup_is_noop_without_user_or_findings():
    store = object.__new__(GraphStore)
    store._run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not query"))  # type: ignore[method-assign]
    assert store.purge_denied_uncurated_research_findings_for_user("", ["f1"])["detached_edges"] == 0
    assert store.purge_denied_uncurated_research_findings_for_user("user-a", [])["deleted_findings"] == 0
