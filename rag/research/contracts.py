"""Provider-neutral contracts for delegated public research."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


MAX_QUERY_CHARS = 8_000
MAX_ANSWER_CHARS = 500_000
MAX_CITATIONS = 100
MAX_SOURCES = 250
MAX_URL_CHARS = 4_096
MAX_TITLE_CHARS = 512
MAX_QUERY_COUNT = 64


@dataclass(frozen=True, slots=True)
class DelegatedResearchRequest:
    """A public-only research task.

    There is intentionally no document/evidence/history field.  The first
    delegated-research slice cannot carry private Nextcloud evidence across the
    egress boundary.
    """

    query: str
    request_id: str = ""

    def __post_init__(self) -> None:
        query = str(self.query or "").strip()
        if not query:
            raise ValueError("delegated research query is required")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError("delegated research query exceeds hard length limit")
        if len(str(self.request_id or "")) > 200:
            raise ValueError("delegated research request_id exceeds hard length limit")
        object.__setattr__(self, "query", query)


@dataclass(frozen=True, slots=True)
class ResearchCitation:
    index: int
    url: str
    title: str = ""
    start_index: int | None = None
    end_index: int | None = None

    def __post_init__(self) -> None:
        if self.index < 1 or self.index > MAX_CITATIONS:
            raise ValueError("research citation index exceeds hard limits")
        url = str(self.url or "").strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError("research citation URL must be HTTP(S)")
        if len(url) > MAX_URL_CHARS or len(str(self.title or "")) > MAX_TITLE_CHARS:
            raise ValueError("research citation metadata exceeds hard limits")
        object.__setattr__(self, "url", url)


@dataclass(frozen=True, slots=True)
class ResearchSource:
    url: str
    title: str = ""
    cited: bool = False

    def __post_init__(self) -> None:
        url = str(self.url or "").strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError("research source URL must be HTTP(S)")
        if len(url) > MAX_URL_CHARS or len(str(self.title or "")) > MAX_TITLE_CHARS:
            raise ValueError("research source metadata exceeds hard limits")
        object.__setattr__(self, "url", url)


@dataclass(frozen=True, slots=True)
class DelegatedResearchResult:
    provider: str
    model: str
    answer_markdown: str
    citations: tuple[ResearchCitation, ...] = ()
    sources: tuple[ResearchSource, ...] = ()
    search_queries: tuple[str, ...] = ()
    response_id: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().casefold()
        model = str(self.model or "").strip()
        if not provider or not model:
            raise ValueError("delegated research provider and model are required")
        answer = str(self.answer_markdown or "").strip()
        if not answer:
            raise ValueError("delegated research provider returned no answer")
        if len(answer) > MAX_ANSWER_CHARS:
            raise ValueError("delegated research answer exceeds hard length limit")
        if len(self.citations) > MAX_CITATIONS:
            raise ValueError("delegated research citations exceed hard count limit")
        if len(self.sources) > MAX_SOURCES:
            raise ValueError("delegated research sources exceed hard count limit")
        if len(self.search_queries) > MAX_QUERY_COUNT:
            raise ValueError("delegated research search queries exceed hard count limit")
        if len(str(self.response_id or "")) > 300:
            raise ValueError("delegated research response_id exceeds hard length limit")
        if any(len(str(value or "")) > MAX_QUERY_CHARS for value in self.search_queries):
            raise ValueError("delegated research search query exceeds hard length limit")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "answer_markdown", answer)


@runtime_checkable
class DelegatedResearchProvider(Protocol):
    async def research(
        self,
        request: DelegatedResearchRequest,
    ) -> DelegatedResearchResult: ...
