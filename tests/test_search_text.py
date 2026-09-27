from rag.elastic_query import nextcloud_query_tokens
from rag.planner import parse_search_syntax
from rag.search_text import normalize_query_quotes, preserve_explicit_quoted_phrases


def test_normalize_common_double_quote_variants():
    assert normalize_query_quotes('“a” „b“ »c«') == '"a" "b" "c"'


def test_planner_accepts_german_typographic_phrase_quotes():
    parsed = parse_search_syntax('„38 M 8076/17“')
    assert parsed.phrases == ['38 M 8076/17']
    assert parsed.free_words == []


def test_explicit_multiword_phrase_survives_rewriter_token_split():
    assert preserve_explicit_quoted_phrases(
        '"Project Alpha 42"',
        "+Project +Alpha 42",
    ) == '"Project Alpha 42"'


def test_explicit_phrase_occurrence_marker_is_preserved():
    assert preserve_explicit_quoted_phrases(
        '+"Project Alpha 42" invoice',
        '+"Project Alpha" +42 +invoice',
    ) == '+"Project Alpha" +invoice +"Project Alpha 42"'


def test_phrase_component_written_outside_quotes_may_remain_separate():
    assert preserve_explicit_quoted_phrases(
        '"Project Alpha" Alpha',
        "+Project +Alpha",
    ) == '+Alpha "Project Alpha"'


def test_parenthesized_explicit_phrases_keep_occurrence_semantics():
    assert preserve_explicit_quoted_phrases(
        '(+"Project Alpha 42")',
        "+Project +Alpha +42",
    ) == '+"Project Alpha 42"'
    assert preserve_explicit_quoted_phrases(
        '(-"Project Alpha 42")',
        "-Project -Alpha -42",
    ) == '-"Project Alpha 42"'
    assert preserve_explicit_quoted_phrases(
        '("Project Alpha 42")',
        "Project Alpha 42",
    ) == '"Project Alpha 42"'


def test_separately_quoted_single_word_keeps_positive_component():
    assert preserve_explicit_quoted_phrases(
        '-"Project Alpha" +"Alpha"',
        "+Project +Alpha",
    ) == '+Alpha -"Project Alpha"'


def test_parenthesized_rewritten_phrase_is_not_split_into_lexical_fragments():
    assert preserve_explicit_quoted_phrases(
        '+"Project Alpha"',
        '(+"Project Alpha")',
    ) == '+"Project Alpha"'


def test_preserved_phrase_compiles_as_one_nextcloud_query_token():
    tokens = nextcloud_query_tokens('"Project Alpha 42"')
    assert len(tokens) == 1
    assert tokens[0]["text"] == "Project Alpha 42"
    assert tokens[0]["phrase"] is True
    assert tokens[0]["occur"] == "should"
    assert tokens[0]["match"] == "match_phrase_prefix"
