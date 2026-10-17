import pytest

from rag.retrieval.contracts import (
    AuthorizedDocumentEvidence,
    RetrievalBudgetGrant,
    RetrievalIdentity,
    SrcRetriever,
    SrcRetrievalRequest,
)
from rag.retrieval.middleware import (
    MiddlewareRetrievalClient,
    MiddlewareSearchRequest,
    MiddlewareSrcRetriever,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.raise_called = False

    def raise_for_status(self):
        self.raise_called = True

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payload):
        self.response = FakeResponse(payload)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


class FakeFactory:
    def __init__(self, payload):
        self.client = FakeClient(payload)
        self.timeouts = []

    def __call__(self, *, timeout):
        self.timeouts.append(timeout)
        return self.client


@pytest.mark.asyncio
async def test_transitional_client_preserves_search_wire_contract_and_parsing():
    factory = FakeFactory({
        "acl_applied": True,
        "retrieval_mode": "normal",
        "results": [
            {
                "document_id": "files:1",
                "document": "Fallback.pdf",
                "context_text": "  primary context  ",
                "text": "secondary",
                "source_origin": "documents",
                "path": "/Docs/Fallback.pdf",
            },
            {"title": "empty.pdf", "text": ""},
        ],
    })
    client = MiddlewareRetrievalClient(
        base_url="http://middleware:8765/",
        timeout=23,
        client_factory=factory,
    )

    response = await client.search(MiddlewareSearchRequest(
        query="question",
        model_id="research",
        limit=9,
        identity=RetrievalIdentity(
            user_id="client::alice",
            groups=("finance,board",),
            request_id="req-1",
        ),
        entity_recall=True,
        retrieval_arms=("vector", "files"),
        source_scopes=("documents",),
        search_spec={"elastic_query": "question"},
        query_context={"entities": []},
        raw_results=False,
        force_unspecific=True,
    ))

    assert factory.timeouts == [23]
    call = factory.client.calls[0]
    assert call["url"] == "http://middleware:8765/search"
    assert call["headers"] == {
        "X-RAG-User-ID": "client::alice",
        "X-RAG-User-Groups": "finance,board",
        "X-RAG-Request-ID": "req-1",
    }
    assert call["json"] == {
        "query": "question",
        "model": "research",
        "limit": 9,
        "entity_recall": True,
        "retrieval_arms": ["files", "vector"],
        "source_scopes": ["documents"],
        "search_spec": {"elastic_query": "question"},
        "query_context": {"entities": []},
        "raw_results": False,
        "force_unspecific": True,
    }
    assert factory.client.response.raise_called is True
    assert len(response.evidence) == 1
    assert response.evidence[0].title == "Fallback.pdf"
    assert response.evidence[0].text == "primary context"
    assert response.evidence[0].source == "src"
    assert response.evidence[0].metadata["document_id"] == "files:1"


@pytest.mark.asyncio
async def test_raw_results_keep_empty_evidence_for_compatibility():
    factory = FakeFactory({"results": [{"title": "empty.pdf", "text": ""}]})
    client = MiddlewareRetrievalClient(
        base_url="http://middleware:8765",
        timeout=2,
        client_factory=factory,
    )

    response = await client.search(MiddlewareSearchRequest(
        query="question",
        model_id="default",
        limit=1,
        identity=RetrievalIdentity(),
        raw_results=True,
    ))

    assert len(response.evidence) == 1
    assert response.evidence[0].text == ""


@pytest.mark.asyncio
async def test_src_adapter_fixes_files_and_documents_capability_envelope():
    factory = FakeFactory({
        "acl_applied": True,
        "results": [{
            "document_id": "files:7",
            "title": "Authorized.pdf",
            "context_text": "Evidence",
            "source_origin": "documents",
        }],
    })
    transport = MiddlewareRetrievalClient(
        base_url="http://middleware:8765",
        timeout=5,
        client_factory=factory,
    )
    retriever = MiddlewareSrcRetriever(transport)

    assert isinstance(retriever, SrcRetriever)
    batch = await retriever.retrieve(SrcRetrievalRequest(
        query="question",
        model_id="core",
        identity=RetrievalIdentity(user_id="client::alice"),
        budget=RetrievalBudgetGrant(max_results=4),
    ))

    request = factory.client.calls[0]["json"]
    assert request["retrieval_arms"] == ["files"]
    assert request["source_scopes"] == ["documents"]
    assert request["limit"] == 4
    assert isinstance(batch.evidence[0], AuthorizedDocumentEvidence)
    assert batch.document_evidence[0].evidence_id == "files:7"
    assert batch.private_candidates == ()
    assert batch.diagnostics["acl_applied"] is True
    assert "results" not in batch.diagnostics


@pytest.mark.asyncio
async def test_provider_search_wrapper_preserves_existing_result_contract(monkeypatch):
    from rag import openai_provider as provider

    factory = FakeFactory({
        "acl_applied": True,
        "retrieval_mode": "normal",
        "results": [
            {
                "document_id": "files:3",
                "title": "Board.pdf",
                "context_text": "authorized evidence",
            },
            {"title": "empty.pdf", "context_text": ""},
        ],
    })
    monkeypatch.setattr(provider, "_middleware_client", factory)
    monkeypatch.setattr(provider, "_active_model_id", lambda: "test-profile")
    monkeypatch.setattr(provider, "_progress", lambda *args, **kwargs: None)

    payload, results = await provider._rag_search(
        "question",
        "client::alice",
        "finance,board",
        request_id="req-3",
        retrieval_arms={"files"},
        source_scopes={"documents"},
        search_spec={"elastic_query": "question"},
        query_context={"entities": []},
        limit=7,
    )

    assert payload["retrieval_mode"] == "normal"
    assert len(results) == 1
    assert isinstance(results[0], provider.SearchResult)
    assert results[0].title == "Board.pdf"
    assert results[0].raw["document_id"] == "files:3"
    request = factory.client.calls[0]
    assert request["json"]["model"] == "test-profile"
    assert request["json"]["retrieval_arms"] == ["files"]
    assert request["json"]["source_scopes"] == ["documents"]
    assert request["headers"]["X-RAG-User-Groups"] == "finance,board"
    assert payload["acl_applied"] is True
    assert payload["results"][0]["document_id"] == "files:3"


@pytest.mark.asyncio
async def test_provider_search_wrapper_keeps_mixed_requests_on_erg_compatibility_path(monkeypatch):
    from rag import openai_provider as provider

    factory = FakeFactory({
        "retrieval_mode": "normal",
        "results": [{
            "document_id": "files:4",
            "title": "Hybrid.pdf",
            "context_text": "hybrid evidence",
        }],
    })
    monkeypatch.setattr(provider, "_middleware_client", factory)
    monkeypatch.setattr(provider, "_active_model_id", lambda: "test-profile")
    monkeypatch.setattr(provider, "_progress", lambda *args, **kwargs: None)

    payload, results = await provider._rag_search(
        "question",
        "client::alice",
        "finance",
        request_id="req-4",
        retrieval_arms={"files", "vector"},
        source_scopes={"documents"},
        limit=7,
    )

    assert payload["retrieval_mode"] == "normal"
    assert len(results) == 1
    request = factory.client.calls[0]["json"]
    assert request["retrieval_arms"] == ["files", "vector"]
    assert request["source_scopes"] == ["documents"]


@pytest.mark.asyncio
async def test_provider_search_wrapper_keeps_archive_scopes_out_of_src(monkeypatch):
    from rag import openai_provider as provider

    factory = FakeFactory({
        "retrieval_mode": "normal",
        "results": [{
            "document_id": "files:5",
            "title": "Archive.pdf",
            "context_text": "archive evidence",
        }],
    })
    monkeypatch.setattr(provider, "_middleware_client", factory)
    monkeypatch.setattr(provider, "_active_model_id", lambda: "test-profile")
    monkeypatch.setattr(provider, "_progress", lambda *args, **kwargs: None)

    await provider._rag_search(
        "question",
        "client::alice",
        "",
        retrieval_arms={"files"},
        source_scopes={"mailarchive"},
        limit=7,
    )

    request = factory.client.calls[0]["json"]
    assert request["retrieval_arms"] == ["files"]
    assert request["source_scopes"] == ["mailarchive"]


@pytest.mark.asyncio
async def test_provider_src_route_fails_closed_without_acl_confirmation(monkeypatch):
    from rag import openai_provider as provider

    factory = FakeFactory({
        "acl_applied": False,
        "retrieval_mode": "normal",
        "results": [{
            "document_id": "files:6",
            "title": "Denied.pdf",
            "context_text": "must not become evidence",
        }],
    })
    monkeypatch.setattr(provider, "_middleware_client", factory)
    monkeypatch.setattr(provider, "_active_model_id", lambda: "test-profile")
    monkeypatch.setattr(provider, "_progress", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="confirmed live Nextcloud ACL"):
        await provider._rag_search(
            "question",
            "client::alice",
            "",
            retrieval_arms={"files"},
            source_scopes={"documents"},
            limit=7,
        )


def test_provider_src_route_selector_is_strictly_documents_files():
    from rag.openai_provider import _use_src_documents_retriever

    assert _use_src_documents_retriever({"files"}, None)
    assert _use_src_documents_retriever({"files"}, {"documents"})
    assert not _use_src_documents_retriever({"files", "vector"}, {"documents"})
    assert not _use_src_documents_retriever({"files"}, {"mailarchive"})
    assert not _use_src_documents_retriever(None, {"documents"})


@pytest.mark.asyncio
@pytest.mark.parametrize("acl_value", [False, None])
async def test_strict_src_adapter_requires_positive_acl_confirmation(acl_value):
    payload = {
        "results": [{
            "document_id": "files:11",
            "title": "Document.pdf",
            "context_text": "Evidence",
            "source_origin": "documents",
        }],
    }
    if acl_value is not None:
        payload["acl_applied"] = acl_value
    retriever = MiddlewareSrcRetriever(
        MiddlewareRetrievalClient(
            base_url="http://middleware:8765",
            timeout=5,
            client_factory=FakeFactory(payload),
        )
    )

    with pytest.raises(RuntimeError, match="confirmed live Nextcloud ACL"):
        await retriever.retrieve(SrcRetrievalRequest(
            query="question",
            identity=RetrievalIdentity(user_id="client::alice"),
            budget=RetrievalBudgetGrant(max_results=4),
        ))


@pytest.mark.asyncio
async def test_strict_src_adapter_rejects_missing_document_locator():
    retriever = MiddlewareSrcRetriever(
        MiddlewareRetrievalClient(
            base_url="http://middleware:8765",
            timeout=5,
            client_factory=FakeFactory({
                "acl_applied": True,
                "results": [{
                    "title": "No locator.pdf",
                    "context_text": "Evidence",
                    "source_origin": "documents",
                }],
            }),
        )
    )

    with pytest.raises(RuntimeError, match="without document_id"):
        await retriever.retrieve(SrcRetrievalRequest(
            query="question",
            identity=RetrievalIdentity(user_id="client::alice"),
            budget=RetrievalBudgetGrant(max_results=4),
        ))


@pytest.mark.asyncio
async def test_strict_src_adapter_enforces_evidence_character_budget():
    retriever = MiddlewareSrcRetriever(
        MiddlewareRetrievalClient(
            base_url="http://middleware:8765",
            timeout=5,
            client_factory=FakeFactory({
                "acl_applied": True,
                "results": [{
                    "document_id": "files:12",
                    "title": "Large.pdf",
                    "context_text": "0123456789",
                    "source_origin": "documents",
                }],
            }),
        )
    )

    with pytest.raises(ValueError, match="max_evidence_chars"):
        await retriever.retrieve(SrcRetrievalRequest(
            query="question",
            identity=RetrievalIdentity(user_id="client::alice"),
            budget=RetrievalBudgetGrant(max_results=4, max_evidence_chars=5),
        ))
