"""Transitional explicit ERG retriever over the existing middleware boundary.

This adapter does not create a second retrieval implementation. It grants only
internal ERG retrieval capabilities that the current middleware can already
execute and that still finish with authoritative live Nextcloud ACL. External
Web/register capabilities belong to dedicated ERG research adapters.
"""
from __future__ import annotations

from rag.retrieval.contracts import (
    AuthorizedDocumentEvidence,
    ErgRetrievalRequest,
    RetrievalBatch,
)
from rag.retrieval.middleware import MiddlewareRetrievalClient, MiddlewareSearchRequest


_CAPABILITY_ARMS = {
    "vector": "vector",
    "research-graph": "graph",
}
_EXTERNAL_CAPABILITIES = frozenset({"web", "openregister-research"})


class MiddlewareErgRetriever:
    """Explicit internal-ERG adapter backed by the combined middleware endpoint."""

    def __init__(self, client: MiddlewareRetrievalClient, *, model_id: str) -> None:
        self.client = client
        self.model_id = str(model_id or "").strip()

    async def retrieve(self, request: ErgRetrievalRequest) -> RetrievalBatch:
        external = request.capabilities & _EXTERNAL_CAPABILITIES
        if external:
            raise ValueError(
                "MiddlewareErgRetriever does not execute external ERG capabilities: "
                + ", ".join(sorted(external))
            )

        arms = tuple(
            sorted(
                _CAPABILITY_ARMS[capability]
                for capability in request.capabilities
                if capability in _CAPABILITY_ARMS
            )
        )
        if not arms:
            raise ValueError("MiddlewareErgRetriever requires vector or research-graph capability")

        response = await self.client.search(
            MiddlewareSearchRequest(
                query=request.query,
                model_id=self.model_id,
                limit=request.budget.max_results,
                identity=request.identity,
                entity_recall=request.entity_recall,
                retrieval_arms=arms,
                source_scopes=("documents",),
                search_spec=request.search_spec,
                query_context=request.query_context,
                raw_results=request.raw_results,
                force_unspecific=request.force_unspecific,
            )
        )
        if response.payload.get("acl_applied") is not True:
            raise RuntimeError(
                "internal ERG retrieval requires confirmed live Nextcloud ACL"
            )

        evidence: list[AuthorizedDocumentEvidence] = []
        for hit in response.evidence:
            if not hit.document_id.strip():
                raise RuntimeError(
                    "internal ERG retrieval returned evidence without document_id"
                )
            evidence.append(
                AuthorizedDocumentEvidence(
                    index=hit.index,
                    title=hit.title,
                    text=hit.text,
                    document_id=hit.document_id,
                    source="erg-internal",
                    metadata=hit.metadata,
                    provenance=hit.provenance,
                )
            )

        batch = RetrievalBatch(
            document_evidence=tuple(evidence),
            diagnostics={
                key: value
                for key, value in response.payload.items()
                if key != "results"
            },
        )
        batch.validate_budget(request.budget)
        return batch
