from rag.ontology import (
    compatible_document_predicates,
    load_relation_ontology,
    predicate_label,
    validate_relation_semantics,
)


ONTOLOGY = load_relation_ontology()


def test_flg_footer_rejects_free_predicate():
    reason = validate_relation_semantics(
        ONTOLOGY,
        predicate="USES_ADDRESS_OF",
        subject_type="Organization",
        subject_kind="Company",
        object_type="Organization",
        object_kind="Court",
        relation_text="Amtsgericht Frankfurt am Main",
        evidence_text="Beispiel Automation AG ... Amtsgericht Frankfurt am Main ... Handelsregister HRB 72567",
    )
    assert reason == "predicate_not_in_ontology"


def test_flg_footer_accepts_registered_at_semantics():
    reason = validate_relation_semantics(
        ONTOLOGY,
        predicate="REGISTERED_AT",
        subject_type="Organization",
        subject_kind="Company",
        object_type="Organization",
        object_kind="Court",
        relation_text="Amtsgericht Frankfurt am Main\nHandelsregister HRB 72567",
        evidence_text="Beispiel Automation AG ... Amtsgericht Frankfurt am Main ... Handelsregister HRB 72567",
    )
    assert reason is None


def test_registered_at_requires_court_kind():
    reason = validate_relation_semantics(
        ONTOLOGY,
        predicate="REGISTERED_AT",
        subject_type="Organization",
        subject_kind="Company",
        object_type="Organization",
        object_kind="Bank",
        relation_text="Postbank Handelsregister",
        evidence_text="Beispiel Automation AG Postbank Handelsregister",
    )
    assert reason == "object_kind_not_allowed:Bank"


def test_board_relation_needs_explicit_cue():
    reason = validate_relation_semantics(
        ONTOLOGY,
        predicate="BOARD_MEMBER_OF",
        subject_type="Person",
        subject_kind="Person",
        object_type="Organization",
        object_kind="Company",
        relation_text="Max Mustermann",
        evidence_text="Max Mustermann Beispiel Automation AG",
    )
    assert reason == "missing_explicit_relation_cue"


def test_structural_retrieval_relations_live_in_ontology():
    from rag.ontology import relation_names_with_role
    assert relation_names_with_role(ONTOLOGY, "retrieval_bridge") == ["WORKS_AT", "WORKS_IN", "PART_OF"]


def test_seed_only_relation_is_not_document_extractable():
    from rag.ontology import relation_schema, ontology_prompt
    schema = relation_schema(ONTOLOGY)
    allowed = schema["properties"]["relations"]["items"]["properties"]["predicate"]["enum"]
    assert "WORKS_IN" not in allowed
    assert "WORKS_IN:" not in ontology_prompt(ONTOLOGY)


def test_ontology_exposes_curator_labels_and_pair_compatibility():
    predicates = compatible_document_predicates(
        ONTOLOGY,
        subject_type="Organization",
        subject_kind="Company",
        object_type="Organization",
        object_kind="Company",
    )
    assert "SHAREHOLDER_OF" in predicates
    assert "REGISTERED_AT" not in predicates
    assert predicate_label("SHAREHOLDER_OF", predicates["SHAREHOLDER_OF"], language="de") == "Gesellschafter/in von"


def test_registered_at_is_offered_only_for_court_object_kind():
    predicates = compatible_document_predicates(
        ONTOLOGY,
        subject_type="Organization",
        subject_kind="Company",
        object_type="Organization",
        object_kind="Court",
    )
    assert "REGISTERED_AT" in predicates


def test_supervisory_board_membership_requires_explicit_membership_cue():
    allowed = validate_relation_semantics(
        ONTOLOGY,
        predicate="SUPERVISORY_BOARD_MEMBER_OF",
        subject_type="Person",
        subject_kind="Person",
        object_type="Organization",
        object_kind="Company",
        relation_text="Aufsichtsratsmitglied",
        evidence_text="Bob Example ist Aufsichtsratsmitglied der Example Automation AG.",
    )
    assert allowed is None

    insufficient = validate_relation_semantics(
        ONTOLOGY,
        predicate="SUPERVISORY_BOARD_MEMBER_OF",
        subject_type="Person",
        subject_kind="Person",
        object_type="Organization",
        object_kind="Company",
        relation_text="Nacharbeit eines Aufsichtsratschreibens",
        evidence_text="Alice Example, bitte um Nacharbeit des Aufsichtsratschreibens von Bob Example.",
    )
    assert insufficient == "missing_explicit_relation_cue"
