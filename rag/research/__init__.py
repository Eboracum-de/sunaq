"""Delegated external research providers.

This package is deliberately separate from retrieval adapters.  A research
provider owns public-web search plus answer synthesis and returns only the
portable result contract defined here.  Private Nextcloud evidence must never
enter this path unless a future, separately granted hybrid contract explicitly
allows it.
"""

from rag.research.contracts import (
    DelegatedResearchProvider,
    DelegatedResearchRequest,
    DelegatedResearchResult,
    ResearchCitation,
    ResearchSource,
)
from rag.research.registry import (
    DelegatedResearchSettings,
    build_delegated_research_provider,
    load_delegated_research_settings,
)

__all__ = [
    "DelegatedResearchProvider",
    "DelegatedResearchRequest",
    "DelegatedResearchResult",
    "ResearchCitation",
    "ResearchSource",
    "DelegatedResearchSettings",
    "build_delegated_research_provider",
    "load_delegated_research_settings",
]
