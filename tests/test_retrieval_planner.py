from pathlib import Path

from rag.retrieval_planner import (
    detect_exhaustive_intent,
    is_strict_lexical_query,
    load_retrieval_planner_settings,
    normalize_generated_probes,
    semanticize_query,
)


def test_query_rewriter_keeps_bare_entity_recall_oriented():
    prompt = (
        Path(__file__).resolve().parent.parent / "prompts" / "query_rewriter.txt"
    ).read_text(encoding="utf-8")
    assert "breite Informationssuche zu dieser Entität" in prompt
    assert "Erfinde dann kein zusätzliches zwingendes Sachkonzept" in prompt
    assert "The planner describes intent; it does not own Elasticsearch syntax." in prompt


def test_config_yaml_rounds_override_legacy_env():
    settings = load_retrieval_planner_settings(
        {"retrieval_planner": {"max_retrieval_rounds": 3}},
        env={"MAX_RETRIEVAL_ROUNDS": "7"},
    )
    assert settings.max_retrieval_rounds == 3


def test_legacy_rounds_used_only_when_yaml_key_missing():
    settings = load_retrieval_planner_settings(
        {"retrieval_planner": {}},
        env={"MAX_RETRIEVAL_ROUNDS": "4"},
    )
    assert settings.max_retrieval_rounds == 4


def test_exhaustive_document_set_intent_requires_explicit_completeness():
    assert not detect_exhaustive_intent("Suche mir Rechnungen von XY GmbH an Z GmbH aus 2025")
    assert not detect_exhaustive_intent("Welche Dokumente gibt es zum Hausverbot Musterfall?")
    assert detect_exhaustive_intent("Suche alle Rechnungen von XY GmbH an Z GmbH aus 2025")
    assert detect_exhaustive_intent("Wie viele Dokumente gibt es zum Hausverbot Musterfall?")
    assert not detect_exhaustive_intent("Was geschah beim Hausverbot Musterfall?")


def test_strict_query_requires_at_least_two_mandatory_anchors():
    assert is_strict_lexical_query('+Rechnung +"XY GmbH" +"Z GmbH" +2025')
    assert not is_strict_lexical_query('+Rechnung 2025')
    assert not is_strict_lexical_query('Rechnung "XY GmbH" 2025')


def test_vector_only_probe_never_keeps_boolean_control_syntax():
    probes = normalize_generated_probes(
        "Rechnungen XY GmbH",
        [{"kind": "strict_lexical", "query": '+Rechnung +"XY GmbH" -Entwurf'}],
        max_additional=2,
        retrieval_arms={"vector"},
    )
    assert probes == [{"kind": "semantic", "query": "Rechnung XY GmbH Entwurf"}]
    assert semanticize_query('+Rechnung +"XY GmbH" +2025') == "Rechnung XY GmbH 2025"


def test_generated_probe_dedup_preserves_original_query():
    probes = normalize_generated_probes(
        "Originalfrage",
        [
            {"kind": "semantic", "query": "Originalfrage"},
            {"kind": "lexical", "query": "andere Probe"},
            {"kind": "semantic", "query": "Andere Probe"},
        ],
        max_additional=3,
        retrieval_arms=None,
    )
    assert probes == [{"kind": "lexical", "query": "andere Probe"}]


def test_overflow_acl_scan_limit_can_observe_capacity_plus_one():
    settings = load_retrieval_planner_settings(
        {
            "retrieval_planner": {
                "max_complete_documents": 15,
                "overflow_acl_scan_limit": 8,
            }
        },
        env={},
    )
    assert settings.overflow_acl_scan_limit == 16


def test_strict_overflow_is_definitive_after_acl_capacity_is_exceeded():
    from rag.retrieval_planner import strict_overflow_reason

    assert strict_overflow_reason(
        es_total=50,
        scanned_documents=20,
        authorized_documents=16,
        max_complete_documents=15,
        scan_limit=20,
    ) == "authorized_overflow"


def test_strict_overflow_fails_closed_when_bounded_acl_window_is_exhausted():
    from rag.retrieval_planner import strict_overflow_reason

    assert strict_overflow_reason(
        es_total=200,
        scanned_documents=80,
        authorized_documents=8,
        max_complete_documents=15,
        scan_limit=80,
    ) == "acl_scan_limit_reached"


def test_strict_overflow_allows_complete_visible_set_within_capacity():
    from rag.retrieval_planner import strict_overflow_reason

    assert strict_overflow_reason(
        es_total=12,
        scanned_documents=12,
        authorized_documents=9,
        max_complete_documents=15,
        scan_limit=80,
    ) is None


def test_semantic_boolean_probe_is_recovered_as_strict_files_probe():
    probes = normalize_generated_probes(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        [{"kind": "semantic", "query": "+examplehost +(Rechnung) +2025"}],
        max_additional=1,
        retrieval_arms={"files"},
    )
    assert probes == [
        {"kind": "strict_lexical", "query": "+examplehost +Rechnung +2025"}
    ]


def test_semantic_boolean_probe_is_plain_text_when_files_arm_is_unavailable():
    probes = normalize_generated_probes(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        [{"kind": "semantic", "query": "+examplehost +(Rechnung) +2025"}],
        max_additional=1,
        retrieval_arms={"vector"},
    )
    assert probes == [
        {"kind": "semantic", "query": "examplehost Rechnung 2025"}
    ]


def test_weak_boolean_probe_cannot_leak_controls_into_non_strict_view():
    probes = normalize_generated_probes(
        "Suche examplehost",
        [{"kind": "lexical", "query": "+examplehost Rechnung"}],
        max_additional=1,
        retrieval_arms={"files"},
    )
    assert probes == [{"kind": "lexical", "query": "examplehost Rechnung"}]
