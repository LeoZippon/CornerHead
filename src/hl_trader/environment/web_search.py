"""Host-side web search provider for the meta-learning Fold.

Only the meta-learning session may call this boundary. Regular Fold Agents and
NL scoring stay offline except for LLM provider calls routed through LLMProxy.
The default provider is Tavily because it is built for agent/RAG search and has
a small HTTP API; proxy settings, if needed, are inherited from the host
``HTTP_PROXY``/``HTTPS_PROXY`` environment variables.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from hl_trader.environment.runtime import sanitize_for_log, utc_now_iso


class WebSearchError(RuntimeError):
    """Explicit, agent-visible web-search failure."""


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    content: str = ""
    score: float | None = None

    def to_record(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "url": self.url,
            "content": self.content,
        }
        if self.score is not None:
            payload["score"] = self.score
        return payload


class WebSearchProvider:
    provider = "none"

    def search(self, query: str, *, max_results: int = 5, category: str = "general") -> list[WebSearchResult]:
        raise WebSearchError("web search provider is not configured")


class TavilySearchProvider(WebSearchProvider):
    provider = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.tavily.com/search",
        timeout_seconds: float = 30.0,
        search_depth: str = "basic",
    ) -> None:
        if not api_key:
            raise WebSearchError("TAVILY_API_KEY is not configured")
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.search_depth = search_depth

    @classmethod
    def from_env(cls, *, env_var: str = "TAVILY_API_KEY", env_file: str = ".env", **kwargs) -> "TavilySearchProvider":
        return cls(os.getenv(env_var, "") or _load_env_value(env_var, env_file), **kwargs)

    def search(self, query: str, *, max_results: int = 5, category: str = "general") -> list[WebSearchResult]:
        query = str(query or "").strip()
        if not query:
            raise WebSearchError("web_search query is empty")
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max(1, min(int(max_results), 10)),
            "search_depth": self.search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - configured endpoint
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            body = body.replace(self.api_key, "[redacted]")
            raise WebSearchError(f"tavily HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            message = str(exc).replace(self.api_key, "[redacted]")
            raise WebSearchError(f"tavily request failed: {message}") from exc
        results = []
        for item in decoded.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    content=str(item.get("content", "")),
                    score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                )
            )
        return results


class SemanticScholarSearchProvider(WebSearchProvider):
    """Semantic Scholar Academic Graph paper search provider.

    This provider is intended for the meta-learning Fold's academic/theory
    search. It queries paper metadata only and keeps the API key on the host.
    """

    provider = "semantic_scholar"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.semanticscholar.org/graph/v1/paper/search",
        timeout_seconds: float = 30.0,
        fields: str = (
            "paperId,title,url,abstract,year,authors,venue,publicationDate,"
            "publicationTypes,citationCount,influentialCitationCount,openAccessPdf"
        ),
        min_interval_seconds: float = 1.05,
    ) -> None:
        if not api_key:
            raise WebSearchError("SEMANTIC_SCHOLAR_API_KEY is not configured")
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.fields = fields
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at: float | None = None

    @classmethod
    def from_env(
        cls,
        *,
        env_var: str = "SEMANTIC_SCHOLAR_API_KEY",
        env_file: str = ".env",
        **kwargs,
    ) -> "SemanticScholarSearchProvider":
        return cls(os.getenv(env_var, "") or _load_env_value(env_var, env_file), **kwargs)

    def search(self, query: str, *, max_results: int = 5, category: str = "general") -> list[WebSearchResult]:
        query = str(query or "").strip()
        if not query:
            raise WebSearchError("web_search query is empty")
        limit = max(1, min(int(max_results), 10))
        params = urllib.parse.urlencode(
            {
                "query": query,
                "limit": limit,
                "fields": self.fields,
            }
        )
        request = urllib.request.Request(
            f"{self.endpoint}?{params}",
            headers={"x-api-key": self.api_key},
            method="GET",
        )
        self._respect_rate_limit()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - fixed API endpoint
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            body = body.replace(self.api_key, "[redacted]")
            raise WebSearchError(f"semantic_scholar HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            message = str(exc).replace(self.api_key, "[redacted]")
            raise WebSearchError(f"semantic_scholar request failed: {message}") from exc

        results: list[WebSearchResult] = []
        for item in decoded.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            paper_id = str(item.get("paperId") or "").strip()
            url = str(item.get("url") or "").strip()
            if not url and paper_id:
                url = f"https://www.semanticscholar.org/paper/{paper_id}"
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    content=_semantic_scholar_content(item),
                )
            )
        return results

    def _respect_rate_limit(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = self.min_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()


class WebSearchTool:
    """Trace-emitting wrapper used by AgentSessionRunner."""

    def __init__(self, provider: WebSearchProvider) -> None:
        self.provider = provider

    def run(self, query: str, *, max_results: int = 5, category: str = "general") -> dict[str, object]:
        started_at = utc_now_iso()
        results = self.provider.search(query, max_results=max_results, category=category)
        return sanitize_for_log(
            {
                "provider": self.provider.provider,
                "category": category,
                "query": query,
                "max_results": max_results,
                "results": [result.to_record() for result in results],
                "started_at": started_at,
                "completed_at": utc_now_iso(),
            }
        )


def _load_env_value(name: str, env_file: str) -> str:
    path = os.path.abspath(env_file)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _semantic_scholar_content(item: dict[str, object]) -> str:
    authors = item.get("authors") or []
    if isinstance(authors, list):
        names = []
        for author in authors[:4]:
            if isinstance(author, dict) and author.get("name"):
                names.append(str(author["name"]))
        author_text = ", ".join(names)
    else:
        author_text = ""
    parts = [
        f"year={item.get('year')}" if item.get("year") else "",
        f"date={item.get('publicationDate')}" if item.get("publicationDate") else "",
        f"venue={item.get('venue')}" if item.get("venue") else "",
        f"authors={author_text}" if author_text else "",
        f"citations={item.get('citationCount')}" if isinstance(item.get("citationCount"), int) else "",
        (
            f"influential_citations={item.get('influentialCitationCount')}"
            if isinstance(item.get("influentialCitationCount"), int)
            else ""
        ),
    ]
    abstract = str(item.get("abstract") or "").strip()
    if abstract:
        parts.append(f"abstract={abstract[:1200]}")
    return " | ".join(part for part in parts if part)
