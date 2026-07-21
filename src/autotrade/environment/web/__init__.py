"""Host-side public web services for meta-learning sessions.

Transport/provider implementations only; the Agent-facing tool contracts that
wrap these services live in ``autotrade.environment.tools.web_fetch`` and
``autotrade.environment.tools.web_search``.
"""

from .fetch import WebFetchError, WebFetchResult, WebFetchService
from .search import (
    SemanticScholarSearchProvider,
    TavilySearchProvider,
    WebSearchError,
    WebSearchProvider,
    WebSearchResult,
    WebSearchService,
)

__all__ = [
    "SemanticScholarSearchProvider",
    "TavilySearchProvider",
    "WebFetchError",
    "WebFetchResult",
    "WebFetchService",
    "WebSearchError",
    "WebSearchProvider",
    "WebSearchResult",
    "WebSearchService",
]
