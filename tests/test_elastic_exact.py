from rag.elastic_query import nextcloud_query_tokens
from rag.openai_provider import _parse_retrieval_directives


def test_nextcloud_expression_parser_matches_querycontent_for_22_07():
    assert nextcloud_query_tokens('+Novak +VEW +Berufung +"22-07"') == [
        {"occur": "must", "text": "Novak", "phrase": False, "match": "match_phrase_prefix"},
        {"occur": "must", "text": "VEW", "phrase": False, "match": "match_phrase_prefix"},
        {"occur": "must", "text": "Berufung", "phrase": False, "match": "match_phrase_prefix"},
        {"occur": "must", "text": "22-07", "phrase": True, "match": "match"},
    ]


def test_nextcloud_expression_parser_optional_and_excluded():
    assert nextcloud_query_tokens('Novak -Nordstern "alte Brauerei"') == [
        {"occur": "should", "text": "Novak", "phrase": False, "match": "match_phrase_prefix"},
        {"occur": "must_not", "text": "Nordstern", "phrase": False, "match": "match_phrase_prefix"},
        {"occur": "should", "text": "alte Brauerei", "phrase": True, "match": "match_phrase_prefix"},
    ]


def test_elastic_directive_is_separate_mode():
    parsed = _parse_retrieval_directives('/elastic +Novak +VEW')
    assert parsed.error is None
    assert parsed.elastic_mode is True
    assert parsed.query == '+Novak +VEW'
    assert parsed.list_mode is None


def test_elastic_list_is_allowed_and_never_selects_hybrid_arm():
    parsed = _parse_retrieval_directives('/elastic /list +Novak +VEW')
    assert parsed.error is None
    assert parsed.elastic_mode is True
    assert parsed.list_mode == 'ranked'
    assert parsed.retrieval_arms is None


def test_elastic_cannot_mix_with_vector():
    parsed = _parse_retrieval_directives('/elastic /vector Novak')
    assert parsed.error is not None


def test_required_quoted_phrase_with_spaces_stays_one_token():
    assert nextcloud_query_tokens('+"alte Brauerei" -"falsche Firma"') == [
        {"occur": "must", "text": "alte Brauerei", "phrase": True, "match": "match_phrase_prefix"},
        {"occur": "must_not", "text": "falsche Firma", "phrase": True, "match": "match_phrase_prefix"},
    ]


def test_files_raw_parser_shape_stays_files_mode():
    parsed = _parse_retrieval_directives('/files /list:raw +Novak +VEW')
    assert parsed.error is None
    assert parsed.retrieval_arms == {'files'}
    assert parsed.list_mode == 'raw'


def test_direct_elastic_query_does_not_require_provider_field(monkeypatch):
    import rag.search as search

    captured = {}

    class FakeResponse:
        is_error = False
        status_code = 200
        reason_phrase = "OK"

        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}

    def fake_post(url, json=None, timeout=None, **kwargs):
        captured["body"] = json
        return FakeResponse()

    monkeypatch.setattr(search.httpx, "post", fake_post)
    search.elastic_exact_search('+Novak +"22-07"', limit=10)
    body = captured["body"]
    filters = body["query"]["bool"].get("filter", [])
    assert not any("provider" in str(item) for item in filters)

    must = body["query"]["bool"]["must"]
    assert "match_phrase_prefix" in str(must[0])
    assert "match" in str(must[1])
    assert "match_phrase" not in str(must[1])


def test_typographic_quotes_are_search_syntax_equivalent():
    assert nextcloud_query_tokens('+“22-07” „alte Brauerei“') == [
        {"occur": "must", "text": "22-07", "phrase": True, "match": "match"},
        {"occur": "should", "text": "alte Brauerei", "phrase": True, "match": "match_phrase_prefix"},
    ]


def test_provider_normalizes_typographic_quotes_in_query():
    parsed = _parse_retrieval_directives('/files /list:raw “38 M 8076/17”')
    assert parsed.error is None
    assert parsed.retrieval_arms == {'files'}
    assert parsed.list_mode == 'raw'
    assert parsed.elastic_mode is False
    assert parsed.query == '"38 M 8076/17"'


def test_rag_files_explicit_phrase_is_strict_not_nextcloud_or():
    import rag.search as search

    clause = search.phrase_clause('22-07')
    body = clause['multi_match']
    assert body['type'] == 'phrase'
    assert body['slop'] == 0
    assert 'operator' not in body

    multi = search.phrase_clause('38 M 8076/17')['multi_match']
    assert multi['type'] == 'phrase'
    assert multi['slop'] == 0

def test_required_company_phrase_and_required_role_are_both_must():
    tokens = nextcloud_query_tokens(
        '+"Example Real Estate GmbH" +Geschaeftsfuehrer'
    )
    assert [item["occur"] for item in tokens] == ["must", "must"]
    assert [item["text"] for item in tokens] == [
        "Example Real Estate GmbH",
        "Geschaeftsfuehrer",
    ]

