"""OpenAI Responses API adapter for delegated public-web research."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Mapping

import httpx

from rag.policy_hooks import OUTBOUND_QUERY, PRE_MODEL_EGRESS, apply_policy_hook
from rag.research.contracts import (
    DelegatedResearchRequest,
    DelegatedResearchResult,
    ResearchCitation,
    ResearchSource,
)


ClientFactory = Callable[..., httpx.AsyncClient]


def _citation_fields(annotation: Mapping[str, Any]) -> tuple[str, str, int | None, int | None]:
    """Accept both current Responses and legacy/nested citation shapes."""
    if str(annotation.get("type") or "") != "url_citation":
        return "", "", None, None
    value: Mapping[str, Any] = annotation
    nested = annotation.get("url_citation")
    if isinstance(nested, Mapping):
        value = nested
    url = str(value.get("url") or "").strip()
    title = str(value.get("title") or "").strip()
    try:
        start = int(value.get("start_index")) if value.get("start_index") is not None else None
    except (TypeError, ValueError):
        start = None
    try:
        end = int(value.get("end_index")) if value.get("end_index") is not None else None
    except (TypeError, ValueError):
        end = None
    return url, title, start, end


def _render_cited_text(
    text: str,
    annotations: list[Mapping[str, Any]],
    *,
    citation_index_by_url: dict[str, int],
) -> tuple[str, list[ResearchCitation]]:
    citations: list[ResearchCitation] = []
    insertions: dict[int, list[str]] = defaultdict(list)
    for annotation in annotations:
        url, title, start, end = _citation_fields(annotation)
        if not url.startswith(("https://", "http://")):
            continue
        key = url.casefold()
        index = citation_index_by_url.setdefault(key, len(citation_index_by_url) + 1)
        citations.append(
            ResearchCitation(
                index=index,
                url=url,
                title=title,
                start_index=start,
                end_index=end,
            )
        )
        if end is not None and 0 <= end <= len(text):
            marker = f" [W{index}]({url})"
            if marker not in insertions[end]:
                insertions[end].append(marker)

    rendered = text
    for position in sorted(insertions, reverse=True):
        rendered = rendered[:position] + "".join(insertions[position]) + rendered[position:]
    return rendered, citations


def parse_openai_research_response(
    payload: Mapping[str, Any],
    *,
    requested_model: str,
) -> DelegatedResearchResult:
    """Normalize a Responses API payload without persisting provider raw output."""
    citation_index_by_url: dict[str, int] = {}
    citations_by_url: dict[str, ResearchCitation] = {}
    source_titles: dict[str, str] = {}
    consulted_urls: list[str] = []
    search_queries: list[str] = []
    answer_parts: list[str] = []
    web_search_calls = 0

    for output in payload.get("output") or []:
        if not isinstance(output, Mapping):
            continue
        output_type = str(output.get("type") or "")
        if output_type == "web_search_call":
            web_search_calls += 1
            action = output.get("action")
            if isinstance(action, Mapping):
                queries = action.get("queries") or []
                if isinstance(queries, list):
                    for query in queries:
                        value = str(query or "").strip()
                        if value and value not in search_queries:
                            search_queries.append(value)
                legacy_query = str(action.get("query") or "").strip()
                if legacy_query and legacy_query not in search_queries:
                    search_queries.append(legacy_query)
                for source in action.get("sources") or []:
                    if not isinstance(source, Mapping):
                        continue
                    url = str(source.get("url") or "").strip()
                    if url.startswith(("https://", "http://")) and url.casefold() not in {
                        value.casefold() for value in consulted_urls
                    }:
                        consulted_urls.append(url)
            continue

        if output_type != "message":
            continue
        for content in output.get("content") or []:
            if not isinstance(content, Mapping) or str(content.get("type") or "") != "output_text":
                continue
            text = str(content.get("text") or "")
            raw_annotations = [
                value for value in (content.get("annotations") or [])
                if isinstance(value, Mapping)
            ]
            rendered, block_citations = _render_cited_text(
                text,
                raw_annotations,
                citation_index_by_url=citation_index_by_url,
            )
            answer_parts.append(rendered)
            for citation in block_citations:
                key = citation.url.casefold()
                citations_by_url.setdefault(key, citation)
                if citation.title:
                    source_titles.setdefault(key, citation.title)
                if key not in {value.casefold() for value in consulted_urls}:
                    consulted_urls.append(citation.url)

    if web_search_calls == 0:
        raise RuntimeError("OpenAI delegated research returned no web_search_call")

    citations = tuple(
        sorted(citations_by_url.values(), key=lambda value: value.index)
    )
    cited_urls = {value.url.casefold() for value in citations}
    sources = tuple(
        ResearchSource(
            url=url,
            title=source_titles.get(url.casefold(), ""),
            cited=url.casefold() in cited_urls,
        )
        for url in consulted_urls
    )
    usage = payload.get("usage")
    return DelegatedResearchResult(
        provider="openai",
        model=str(payload.get("model") or requested_model),
        answer_markdown="\n\n".join(part.strip() for part in answer_parts if part.strip()),
        citations=citations,
        sources=sources,
        search_queries=tuple(search_queries),
        response_id=str(payload.get("id") or ""),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


class OpenAIResearchProvider:
    """Delegate public research to OpenAI hosted web search."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 180.0,
        verify: bool | str = True,
        reasoning_effort: str = "",
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("OpenAI delegated research API key is required")
        if not str(model or "").strip():
            raise ValueError("OpenAI delegated research model is required")
        self.api_key = str(api_key).strip()
        self.model = str(model).strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self.verify = verify
        self.reasoning_effort = str(reasoning_effort or "").strip().casefold()
        self.client_factory = client_factory

    async def research(
        self,
        request: DelegatedResearchRequest,
    ) -> DelegatedResearchResult:
        query = apply_policy_hook(
            OUTBOUND_QUERY,
            content=request.query,
            metadata={
                "provider": "openai",
                "target": self.base_url,
                "kind": "delegated_research",
                "egress_class": "research_query",
                "external": True,
            },
        )
        if not isinstance(query, str):
            raise TypeError("outbound_query policy hook must return str content")

        checked_messages = apply_policy_hook(
            PRE_MODEL_EGRESS,
            content=[{"role": "user", "content": query}],
            metadata={
                "kind": "delegated_research",
                "backend": "openai",
                "base_url": self.base_url,
                "model": self.model,
                "public_only": True,
                "egress_class": "research_query",
                "external": True,
            },
        )
        if (
            not isinstance(checked_messages, list)
            or len(checked_messages) != 1
            or not isinstance(checked_messages[0], Mapping)
            or str(checked_messages[0].get("role") or "") != "user"
            or not isinstance(checked_messages[0].get("content"), str)
        ):
            raise TypeError(
                "pre_model_egress policy hook must preserve one user message "
                "for delegated research"
            )
        query = str(checked_messages[0]["content"]).strip()
        if not query:
            raise ValueError("policy hook removed delegated research query")

        body: dict[str, Any] = {
            "model": self.model,
            "instructions": (
                "Perform public-web research for the user's request. "
                "Use the web search tool. Answer in the user's language. "
                "Use sourced factual claims and do not claim access to private "
                "SunaQ or Nextcloud data."
            ),
            "input": query,
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "store": False,
        }
        if self.reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            body["reasoning"] = {"effort": self.reasoning_effort}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if request.request_id:
            headers["X-Client-Request-Id"] = request.request_id[:200]

        async with self.client_factory(
            timeout=self.timeout,
            verify=self.verify,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("OpenAI delegated research returned a non-object response")
        return parse_openai_research_response(payload, requested_model=self.model)
