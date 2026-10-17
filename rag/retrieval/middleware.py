"""HTTP adapter for the existing combined retrieval middleware.

``MiddlewareRetrievalClient`` is a transitional adapter: the current
``/search`` endpoint can still execute mixed files/vector/graph retrieval.  It
keeps that wire schema out of the provider while the SRC and ERG execution
paths are separated incrementally.

``MiddlewareSrcRetriever`` is the first strict contract implementation.  Its
request type has no capability selectors and this adapter always asks for the
``files`` arm and ordinary ``documents`` scope.  The middleware remains
responsible for the mandatory final live Nextcloud ACL check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from rag.retrieval.contracts import (
    AuthorizedDocumentEvidence,
    RetrievalBatch,
    RetrievalIdentity,
    SrcRetrievalRequest,
)


ClientFactory = Callable[..., httpx.AsyncClient]


@dataclass(frozen=True, slots=True)
class MiddlewareSearchHit:
    """Compatibility representation; not evidence until a strict adapter authorizes it."""

    index: int
    title: str
    text: str
    document_id: str = ""
    source: str = "internal"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)


def _identity_headers(identity: RetrievalIdentity) -> dict[str, str]:
    headers: dict[str, str] = {}
    if identity.user_id:
        headers["X-RAG-User-ID"] = identity.user_id
    if identity.groups:
        headers["X-RAG-User-Groups"] = ",".join(identity.groups)
    if identity.request_id:
        headers["X-RAG-Request-ID"] = identity.request_id
    return headers


def _parse_evidence(
    payload: Mapping[str, Any],
    *,
    include_empty: bool,
) -> tuple[MiddlewareSearchHit, ...]:
    parsed: list[MiddlewareSearchHit] = []
    for index, value in enumerate(payload.get("results") or (), start=1):
        if not isinstance(value, Mapping):
            continue
        raw = dict(value)
        title = str(raw.get("title") or raw.get("document") or f"Dokument {index}")
        text = str(
            raw.get("context_text")
            or raw.get("text")
            or raw.get("chunk")
            or ""
        ).strip()
        if text or include_empty:
            parsed.append(
                MiddlewareSearchHit(
                    index=index,
                    title=title,
                    text=text,
                    document_id=str(raw.get("document_id") or ""),
                    source="src" if raw.get("source_origin") == "documents" else "internal",
                    metadata=raw,
                    provenance={
                        "source_origin": str(raw.get("source_origin") or ""),
                        "path": str(raw.get("path") or raw.get("file_path") or ""),
                    },
                )
            )
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class MiddlewareSearchRequest:
    """Exact compatibility input for the current middleware ``/search`` API."""

    query: str
    model_id: str
    limit: int
    identity: RetrievalIdentity
    entity_recall: bool = False
    retrieval_arms: tuple[str, ...] | None = None
    source_scopes: tuple[str, ...] | None = None
    search_spec: Mapping[str, Any] | None = None
    query_context: Mapping[str, Any] | None = None
    raw_results: bool = False
    force_unspecific: bool = False


@dataclass(frozen=True, slots=True)
class MiddlewareSearchResponse:
    payload: Mapping[str, Any]
    evidence: tuple[MiddlewareSearchHit, ...] = field(default_factory=tuple)


class MiddlewareRetrievalClient:
    """Transport adapter preserving the current provider/middleware behavior."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        client_factory: ClientFactory,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.client_factory = client_factory

    async def search(self, request: MiddlewareSearchRequest) -> MiddlewareSearchResponse:
        body = {
            "query": request.query,
            "model": request.model_id,
            "limit": int(request.limit),
            "entity_recall": bool(request.entity_recall),
            "retrieval_arms": (
                sorted(request.retrieval_arms)
                if request.retrieval_arms
                else None
            ),
            "source_scopes": (
                sorted(request.source_scopes)
                if request.source_scopes
                else None
            ),
            "search_spec": request.search_spec,
            "query_context": request.query_context,
            "raw_results": bool(request.raw_results),
            "force_unspecific": bool(request.force_unspecific),
        }
        async with self.client_factory(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/search",
                json=body,
                headers=_identity_headers(request.identity),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("retrieval middleware returned a non-object response")
        return MiddlewareSearchResponse(
            payload=dict(payload),
            evidence=_parse_evidence(payload, include_empty=request.raw_results),
        )


class MiddlewareSrcRetriever:
    """Strict files/documents adapter for the canonical :class:`SrcRetriever`."""

    def __init__(self, client: MiddlewareRetrievalClient) -> None:
        self.client = client

    async def retrieve(self, request: SrcRetrievalRequest) -> RetrievalBatch:
        response = await self.client.search(
            MiddlewareSearchRequest(
                query=request.query,
                model_id=request.model_id,
                limit=request.budget.max_results,
                identity=request.identity,
                entity_recall=request.entity_recall,
                retrieval_arms=("files",),
                source_scopes=("documents",),
                search_spec=request.search_spec,
                query_context=request.query_context,
                raw_results=request.raw_results,
                force_unspecific=request.force_unspecific,
            )
        )
        if response.payload.get("acl_applied") is not True:
            raise RuntimeError(
                "strict SRC retrieval requires confirmed live Nextcloud ACL"
            )
        document_evidence: list[AuthorizedDocumentEvidence] = []
        for hit in response.evidence:
            if not hit.document_id.strip():
                raise RuntimeError(
                    "strict SRC retrieval returned evidence without document_id"
                )
            document_evidence.append(
                AuthorizedDocumentEvidence(
                    index=hit.index,
                    title=hit.title,
                    text=hit.text,
                    document_id=hit.document_id,
                    source="src",
                    metadata=hit.metadata,
                    provenance=hit.provenance,
                )
            )
        batch = RetrievalBatch(
            document_evidence=tuple(document_evidence),
            diagnostics={
                key: value
                for key, value in response.payload.items()
                if key != "results"
            },
        )
        batch.validate_budget(request.budget)
        return batch
