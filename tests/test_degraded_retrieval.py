from types import SimpleNamespace

import rag.search as search


def _empty_entity_context():
    return {
        "enabled": False,
        "entities": [],
        "elastic_phrase_expansion": [],
        "error": None,
    }


def test_elasticsearch_failure_returns_degraded_payload_not_exception(monkeypatch):
    monkeypatch.setattr(search, "ELASTICSEARCH_ENABLED", True)
    monkeypatch.setattr(search, "QDRANT_ENABLED", False)
    monkeypatch.setattr(search, "GRAPH_RETRIEVAL_ENABLED", False)
    monkeypatch.setattr(search, "prepare_entity_context", lambda question: _empty_entity_context())
    monkeypatch.setattr(search, "create_plan", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(search, "elastic_search", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("ES down")))
    monkeypatch.setattr(search, "rerank_results", lambda *a, **k: [])

    payload = search.perform_search("Testanfrage", retrieval_arms={"files"})

    assert payload["retrieval_mode"] == "degraded_no_results"
    assert payload["results"] == []
    assert payload["backend_status"]["files"]["available"] is False
    assert "Dokumentensuche" in payload["retrieval_message"]
    assert "kein weiterer Suchdienst" in payload["retrieval_message"]
    assert "übrigen verfügbaren Suchdienste" not in payload["retrieval_message"]


def test_disabled_vector_is_not_reported_as_failed(monkeypatch):
    monkeypatch.setattr(search, "ELASTICSEARCH_ENABLED", False)
    monkeypatch.setattr(search, "QDRANT_ENABLED", False)
    monkeypatch.setattr(search, "GRAPH_RETRIEVAL_ENABLED", False)
    monkeypatch.setattr(search, "prepare_entity_context", lambda question: _empty_entity_context())
    monkeypatch.setattr(search, "create_plan", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(search, "rerank_results", lambda *a, **k: [])

    payload = search.perform_search("Testanfrage", retrieval_arms={"vector"})

    assert payload["backend_status"]["vector"]["enabled"] is False
    assert payload["backend_status"]["vector"]["available"] is False
    assert payload["retrieval_mode"] != "degraded_no_results"


def test_api_translates_elasticsearch_transport_failure_to_503():
    import httpx
    from fastapi import HTTPException
    import rag.api as api

    request = httpx.Request("POST", api.ES_URL.rstrip("/") + "/_search")
    exc = httpx.ConnectError("connection refused", request=request)
    assert api._is_elasticsearch_unavailable(exc) is True
    try:
        api._raise_retrieval_exception(exc, fallback="generic")
    except HTTPException as error:
        assert error.status_code == 503
        assert "Elasticsearch" in str(error.detail)
    else:
        raise AssertionError("Elasticsearch outage must become HTTP 503")


def test_provider_turns_elasticsearch_503_into_user_message():
    import httpx
    import rag.openai_provider as provider

    request = httpx.Request("POST", "http://127.0.0.1:8765/documents/resolve")
    response = httpx.Response(
        503,
        request=request,
        json={"detail": "Dokumentensuche derzeit nicht verfügbar (Elasticsearch nicht erreichbar)."},
    )
    exc = httpx.HTTPStatusError("503", request=request, response=response)
    text = provider._document_search_unavailable_text(exc)
    assert text is not None
    assert "Dokumentensuche" in text
    assert "Elasticsearch" in text


def test_degraded_message_mentions_other_services_only_when_one_remains_available():
    message = search._degraded_retrieval_message(
        {
            "files": {"requested": True, "enabled": True, "available": False},
            "vector": {"requested": True, "enabled": True, "available": True},
            "graph": {"requested": False, "enabled": False, "available": False},
        },
        ["files"],
    )
    assert "Dokumentensuche" in message
    assert "übrigen verfügbaren Suchdienste" in message
    assert "kein weiterer Suchdienst" not in message
