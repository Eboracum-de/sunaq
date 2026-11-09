from rag.openai_provider import _initial_retrieval_views, _planner_executor_probe
from rag.planner import create_plan


def test_round1_compiles_files_from_frame_but_keeps_natural_query_for_vector_graph():
    question = "Suche Rechnungen von examplehost aus dem Jahr 2025"
    frame = {
        "intent": "Rechnungen finden",
        "entities": [{"id": "q1", "text": "examplehost", "role": "Anbieter"}],
        "relations": [],
        "constraints": [{"kind": "Jahr", "value": "2025"}],
        "concepts": ["Rechnung"],
    }
    views = _initial_retrieval_views(
        question,
        retrieval_arms=None,
        query_frame=frame,
        bounded_document_set=True,
    )
    assert [v["retrieval_arms"] for v in views] == [["files"], ["vector"], ["graph"]]
    assert views[0]["kind"] == "strict_lexical"
    assert views[0]["query"] == "+Rechnung +examplehost +2025"
    assert views[1]["query"] == question
    assert views[2]["query"] == question


def test_super_light_bounded_files_view_never_sends_full_sentence_to_elasticsearch():
    question = "Suche Rechnungen von examplehost aus dem Jahr 2025"
    views = _initial_retrieval_views(
        question,
        retrieval_arms={"files"},
        query_frame={
            "entities": [{"id": "q1", "text": "examplehost", "role": "Anbieter"}],
            "constraints": [{"kind": "Jahr", "value": "2025"}],
            "concepts": ["Rechnung"],
        },
        bounded_document_set=True,
    )
    assert views == [{
        "kind": "strict_lexical",
        "query": "+Rechnung +examplehost +2025",
        "semantic_query": "Rechnung examplehost 2025",
        "retrieval_arms": ["files"],
    }]


def test_nonbounded_files_view_is_compact_lexical_terms_not_user_sentence():
    question = "Wo geht es darum, dass Musterperson die Firmenadresse unberechtigt benutzt?"
    views = _initial_retrieval_views(
        question,
        retrieval_arms={"files"},
        query_frame={
            "entities": [{"id": "q1", "text": "Musterperson", "role": "Person"}],
            "concepts": ["Firmenadresse", "unberechtigt benutzt"],
        },
        bounded_document_set=False,
    )
    assert views[0]["kind"] == "lexical"
    assert views[0]["query"] == "Firmenadresse unberechtigt benutzt Musterperson"
    assert views[0]["query"] != question


def test_generated_lexical_and_semantic_probes_do_not_rehybridize():
    lexical = _planner_executor_probe(
        {"kind": "lexical", "query": "Nordstern Lombard 2025"},
        retrieval_arms=None,
        original_query="original",
    )
    semantic = _planner_executor_probe(
        {"kind": "semantic", "query": "invoice from Nordstern to Lombard in 2025"},
        retrieval_arms=None,
        original_query="original",
    )
    assert lexical["retrieval_arms"] == ["files"]
    assert semantic["retrieval_arms"] == ["vector"]


def test_nextcloud_is_not_a_global_stopword_any_more():
    plan = create_plan(
        'Nach meiner Erinnerung hat die Nordstern der DL 2025 eine Rechnung über "Bereitstellung Nextcloud" gestellt'
    )
    assert any("nextcloud" in term.casefold() for term in (plan.should + plan.phrases))


def test_insufficient_wording_is_search_scoped_not_global():
    from rag.openai_provider import _format_insufficient
    text = _format_insufficient({"reason": "Die Treffer sind uneindeutig."})
    assert text.startswith("Ich habe in den vorliegenden Dokumenttreffern")
    assert "gefunden" in text
    assert "Es besteht kein Beleg" not in text


def test_bounded_strict_recall_probe_uses_entity_plus_year_and_files_only():
    from rag.openai_provider import _bounded_strict_recall_probe

    probe = _bounded_strict_recall_probe(
        "suche rechnungen von examplehost aus dem Jahr 2025",
        {
            "entities": [
                {"id": "q1", "text": "examplehost", "role": "Rechnungssteller oder Anbieter"}
            ],
            "constraints": [{"kind": "Jahr", "value": "2025"}],
        },
        retrieval_arms=None,
    )

    assert probe is not None
    assert probe["kind"] == "strict_lexical"
    assert probe["retrieval_arms"] == ["files"]
    assert "+examplehost" in probe["query"]
    assert "+2025" in probe["query"]


def test_bounded_strict_recall_probe_requires_two_independent_anchors():
    from rag.openai_provider import _bounded_strict_recall_probe

    probe = _bounded_strict_recall_probe(
        "suche rechnungen aus dem Jahr 2025",
        {"entities": []},
        retrieval_arms=None,
    )
    assert probe is None


def test_recovered_examplehost_probe_executes_as_files_only_strict_view():
    from rag.retrieval_planner import normalize_generated_probes

    normalized = normalize_generated_probes(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        [{"kind": "semantic", "query": "+examplehost +(Rechnung) +2025"}],
        max_additional=1,
        retrieval_arms={"files"},
    )
    assert normalized == [
        {"kind": "strict_lexical", "query": "+examplehost +Rechnung +2025"}
    ]
    executor = _planner_executor_probe(
        normalized[0],
        retrieval_arms={"files"},
        original_query="Suche Rechnungen von examplehost aus dem Jahr 2025",
    )
    assert executor["kind"] == "strict_lexical"
    assert executor["query"] == "+examplehost +Rechnung +2025"
    assert executor["semantic_query"] == "examplehost Rechnung 2025"
    assert executor["retrieval_arms"] == ["files"]


def test_semantic_probe_is_not_executed_on_files_when_vector_is_unavailable():
    probe = _planner_executor_probe(
        {"kind": "semantic", "query": "Suche Rechnungen von examplehost aus 2025"},
        retrieval_arms={"files"},
        original_query="Suche Rechnungen von examplehost aus 2025",
    )
    assert probe is None


def test_valid_empty_query_frame_does_not_fall_back_to_natural_language_files_query():
    views = _initial_retrieval_views(
        "Suche Rechnungen von examplehost aus dem Jahr 2025",
        retrieval_arms={"files"},
        query_frame={},
        bounded_document_set=True,
    )
    assert views == []


def test_missing_query_frame_keeps_legacy_files_fallback_for_planner_failure():
    question = "Suche Rechnungen von examplehost aus dem Jahr 2025"
    views = _initial_retrieval_views(
        question,
        retrieval_arms={"files"},
        query_frame=None,
        bounded_document_set=True,
    )
    assert views == [{
        "kind": "lexical",
        "query": question,
        "semantic_query": question,
        "retrieval_arms": ["files"],
    }]


def test_candidate_verifier_requires_requested_document_type_itself():
    from rag.openai_provider import CANDIDATE_VERIFIER_SYSTEM_PROMPT

    assert "candidate document itself must be of that type" in CANDIDATE_VERIFIER_SYSTEM_PROMPT
    assert "mere mention, accounting entry, attachment reference" in CANDIDATE_VERIFIER_SYSTEM_PROMPT
