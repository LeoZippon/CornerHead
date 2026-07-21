"""Host-side web search provider for the meta-learning Fold.

Only the meta-learning session may call this boundary. Regular Fold Agents and
NL Sub Agent calls stay offline except for LLM provider calls routed through LLMProxy.
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
import fcntl
import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from autotrade.environment.runtime import sanitize_for_log, utc_now_iso

_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
_RATE_LOCKS: dict[str, threading.Lock] = {}
_RATE_LOCKS_GUARD = threading.Lock()


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
        max_retries: int = 2,
        retry_initial_seconds: float = 1.0,
        retry_max_seconds: float = 8.0,
    ) -> None:
        if not api_key:
            raise WebSearchError("TAVILY_API_KEY is not configured")
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.search_depth = search_depth
        self.max_retries = max(0, int(max_retries))
        self.retry_initial_seconds = max(0.0, float(retry_initial_seconds))
        self.retry_max_seconds = max(0.0, float(retry_max_seconds))

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
        decoded = self._request_json(request)
        results = []
        for item in decoded.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                WebSearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    content=str(item.get("content", ""))[:1500],
                    score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                )
            )
        return results

    def _request_json(self, request: urllib.request.Request) -> dict[str, object]:
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - configured endpoint
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = _redact_body(exc, self.api_key)
                if _should_retry(exc, attempt, self.max_retries):
                    _sleep_before_retry(exc, attempt, self.retry_initial_seconds, self.retry_max_seconds)
                    continue
                raise WebSearchError(f"tavily HTTP {exc.code} after {attempt + 1} attempt(s): {body}") from exc
            except Exception as exc:  # noqa: BLE001 - normalize provider failures
                message = str(exc).replace(self.api_key, "[redacted]")
                raise WebSearchError(f"tavily request failed: {message}") from exc
        raise WebSearchError("tavily request failed after retries")


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
        fields: str = "paperId,title,url,abstract,year,authors,venue,citationCount",
        min_interval_seconds: float = 1.25,
        max_retries: int = 3,
        retry_initial_seconds: float = 2.0,
        retry_max_seconds: float = 20.0,
        rate_limit_dir: str | Path | None = None,
    ) -> None:
        if not api_key:
            raise WebSearchError("SEMANTIC_SCHOLAR_API_KEY is not configured")
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.fields = fields
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max(0, int(max_retries))
        self.retry_initial_seconds = max(0.0, float(retry_initial_seconds))
        self.retry_max_seconds = max(0.0, float(retry_max_seconds))
        self.rate_limit_dir = _default_rate_limit_dir() if rate_limit_dir is None else Path(rate_limit_dir)
        key_digest = hashlib.sha256(f"{self.provider}:{self.api_key}".encode("utf-8")).hexdigest()[:20]
        self._rate_limit_key = f"{self.provider}_{key_digest}"

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
        decoded = self._request_json(request)

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

    def _request_json(self, request: urllib.request.Request) -> dict[str, object]:
        for attempt in range(self.max_retries + 1):
            self._respect_rate_limit()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - fixed API endpoint
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = _redact_body(exc, self.api_key)
                if _should_retry(exc, attempt, self.max_retries):
                    _sleep_before_retry(exc, attempt, self.retry_initial_seconds, self.retry_max_seconds)
                    continue
                raise WebSearchError(
                    f"semantic_scholar HTTP {exc.code} after {attempt + 1} attempt(s): {body}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - normalize provider failures
                message = str(exc).replace(self.api_key, "[redacted]")
                raise WebSearchError(f"semantic_scholar request failed: {message}") from exc
        raise WebSearchError("semantic_scholar request failed after retries")

    def _respect_rate_limit(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        lock = _rate_lock(self._rate_limit_key)
        with lock:
            _respect_file_rate_limit(
                self._rate_limit_key,
                min_interval_seconds=self.min_interval_seconds,
                rate_limit_dir=self.rate_limit_dir,
            )


class WebSearchService:
    """Multi-engine provider wrapper used by the Agent-facing web_search_tool."""

    def __init__(self, providers: Mapping[str, WebSearchProvider]) -> None:
        cleaned: dict[str, WebSearchProvider] = {}
        for engine, provider in providers.items():
            name = str(engine or provider.provider).strip()
            if not name:
                continue
            cleaned[name] = provider
        if not cleaned:
            raise WebSearchError("web search providers are not configured")
        self.providers = cleaned

    @property
    def engines(self) -> tuple[str, ...]:
        return tuple(self.providers)

    def run(self, query: str, *, engine: str, perspective: str, max_results: int = 5) -> dict[str, object]:
        engine = str(engine or "").strip()
        if engine not in self.providers:
            available = sorted(self.providers)
            raise WebSearchError(f"unsupported web_search engine: {engine!r}; available engines: {available}")
        perspective = str(perspective or "").strip()
        provider = self.providers[engine]
        started_at = utc_now_iso()
        results = provider.search(query, max_results=max_results, category=perspective)
        return sanitize_for_log(
            {
                "engine": engine,
                "perspective": perspective,
                "provider": provider.provider,
                "query": query,
                "max_results": max_results,
                "result_count": len(results),
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


def _default_rate_limit_dir() -> Path:
    return Path(os.getenv("MACROQUANT_API_RATE_LIMIT_DIR", ".runtime/api_rate_limits"))


def _rate_lock(key: str) -> threading.Lock:
    with _RATE_LOCKS_GUARD:
        lock = _RATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RATE_LOCKS[key] = lock
        return lock


def _respect_file_rate_limit(key: str, *, min_interval_seconds: float, rate_limit_dir: Path) -> None:
    rate_limit_dir.mkdir(parents=True, exist_ok=True)
    lock_path = rate_limit_dir / f"{key}.lock"
    with lock_path.open("a+", encoding="ascii") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read().strip()
            last_started_at = float(raw) if raw else 0.0
            wait = min_interval_seconds - (time.time() - last_started_at)
            if wait > 0:
                time.sleep(wait)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{time.time():.6f}")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _redact_body(exc: urllib.error.HTTPError, api_key: str) -> str:
    body = exc.read().decode("utf-8", errors="replace")[:500]
    return body.replace(api_key, "[redacted]")


def _should_retry(exc: urllib.error.HTTPError, attempt: int, max_retries: int) -> bool:
    return attempt < max_retries and int(exc.code) in _RETRYABLE_HTTP_STATUS


def _sleep_before_retry(
    exc: urllib.error.HTTPError,
    attempt: int,
    retry_initial_seconds: float,
    retry_max_seconds: float,
) -> None:
    retry_after = _retry_after_seconds(exc)
    if retry_after is None:
        retry_after = retry_initial_seconds * (2**attempt)
    wait = min(float(retry_after), retry_max_seconds) if retry_max_seconds > 0 else float(retry_after)
    if wait > 0:
        time.sleep(wait)


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


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
