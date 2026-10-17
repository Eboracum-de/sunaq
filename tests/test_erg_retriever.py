import asyncio

import pytest

from rag.retrieval.contracts import (
    CapabilityGrant,
    ErgRetrievalRequest,
    RetrievalBudgetGrant,
    RetrievalIdentity,
)
from rag.retrieval.erg import MiddlewareErgRetriever
from rag.retrieval.middleware import MiddlewareSearchHit, MiddlewareSearchResponse


class FakeClient:
    def __init__(self, *, acl_applied=True):
        self.requests = []
        self.acl_applied = acl_applied

    async def search(self, request):
        self.requests.append(request)
        return MiddlewareSearchResponse(
            payload={
                "acl_applied": self.acl_applied,
                "results": [{"document_id": "files:101"}],
            },
            evidence=(
                MiddlewareSearchHit(
                    index=1,
                    title="A.pdf",
                    text="Evidence",
                    document_id="files:101",
                    metadata={"document_id": "files:101", "source_origin": "documents"},
                    provenance={"source_origin": "documents"},
                ),
            ),
        )


def _request(*capabilities):
    return ErgRetrievalRequest(
        query="Example GmbH",
        identity=RetrievalIdentity(user_id="client::alice", request_id="r1"),
        budget=RetrievalBudgetGrant(max_results=10),
        grant=CapabilityGrant(frozenset(capabilities)),
    )


def test_internal_erg_retriever_maps_granted_capabilities_to_existing_arms():
    client = FakeClient()
    batch = asyncio.run(
        MiddlewareErgRetriever(client, model_id="sunaq-standard").retrieve(
            _request("vector", "research-graph")
        )
    )

    assert client.requests[0].retrieval_arms == ("graph", "vector")
    assert client.requests[0].source_scopes == ("documents",)
    assert batch.document_evidence[0].document_id == "files:101"
    assert batch.document_evidence[0].source == "erg-internal"


def test_internal_erg_retriever_preserves_structured_planner_inputs():
    client = FakeClient()
    request = ErgRetrievalRequest(
        query="Example GmbH",
        identity=RetrievalIdentity(user_id="client::alice", request_id="r2"),
        budget=RetrievalBudgetGrant(max_results=10),
        grant=CapabilityGrant(frozenset({"vector"})),
        search_spec={
            "elastic_query": "Example GmbH",
            "semantic_query": "Example GmbH Beteiligung",
            "entities": ["Example GmbH"],
        },
        query_context={"entities": [{"mention": "Example GmbH"}]},
        entity_recall=True,
        raw_results=True,
        force_unspecific=True,
    )
    asyncio.run(
        MiddlewareErgRetriever(client, model_id="sunaq-standard").retrieve(request)
    )
    sent = client.requests[0]
    assert sent.retrieval_arms == ("vector",)
    assert sent.search_spec == request.search_spec
    assert sent.query_context == request.query_context
    assert sent.entity_recall is True
    assert sent.raw_results is True
    assert sent.force_unspecific is True


def test_internal_erg_retriever_rejects_external_capabilities():
    client = FakeClient()
    with pytest.raises(ValueError, match="does not execute external ERG capabilities"):
        asyncio.run(
            MiddlewareErgRetriever(client, model_id="sunaq-standard").retrieve(
                _request("web")
            )
        )


def test_internal_erg_retriever_requires_live_acl_confirmation():
    client = FakeClient(acl_applied=False)
    with pytest.raises(RuntimeError, match="live Nextcloud ACL"):
        asyncio.run(
            MiddlewareErgRetriever(client, model_id="sunaq-standard").retrieve(
                _request("vector")
            )
        )
