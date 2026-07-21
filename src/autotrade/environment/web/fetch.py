"""Host-side public web fetch for meta-learning sessions."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from autotrade.environment.runtime import sanitize_for_log, utc_now_iso

MAX_URL_LENGTH = 2000
MAX_QUERY_LENGTH = 1500
MAX_FETCH_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 5
MAX_MARKDOWN_CHARS = 100_000
USER_AGENT = "MacroQuant-WebFetch/1.0"
TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)


class WebFetchError(RuntimeError):
    """Explicit, agent-visible web-fetch failure."""


@dataclass(frozen=True)
class WebFetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    bytes_read: int
    content_hash: str
    markdown: str
    markdown_truncated: bool
    body_truncated: bool
    use_proxy: bool
    redirect_chain: tuple[str, ...]
    started_at: str
    completed_at: str
    duration_ms: int

    def to_record(self) -> dict[str, object]:
        return sanitize_for_log(
            {
                "url": self.url,
                "final_url": self.final_url,
                "status_code": self.status_code,
                "content_type": self.content_type,
                "bytes": self.bytes_read,
                "content_hash": self.content_hash,
                "markdown_truncated": self.markdown_truncated,
                "body_truncated": self.body_truncated,
                "use_proxy": self.use_proxy,
                "redirect_chain": list(self.redirect_chain),
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_ms": self.duration_ms,
            }
        )


class WebFetchService:
    """Small deterministic HTTP GET client used by the Agent-facing tool."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_FETCH_BYTES,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())

    def fetch(
        self,
        url: str,
        *,
        use_proxy: bool = False,
        proxy_env: dict[str, str] | None = None,
    ) -> WebFetchResult:
        current_url = _validate_url(url)
        original_url = current_url
        redirect_chain: list[str] = []
        started_at = utc_now_iso()
        started = time.monotonic()
        opener = _build_proxy_opener(proxy_env) if use_proxy else self._direct_opener

        for _attempt in range(self.max_redirects + 1):
            _validate_public_host(current_url)
            request = urllib.request.Request(
                current_url,
                headers={
                    "Accept": "text/html,text/plain,application/json,application/xml;q=0.9,*/*;q=0.1",
                    "User-Agent": USER_AGENT,
                },
                method="GET",
            )
            try:
                with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - URL is guarded
                    return self._read_response(
                        response,
                        original_url=original_url,
                        final_url=current_url,
                        use_proxy=use_proxy,
                        redirect_chain=tuple(redirect_chain),
                        started_at=started_at,
                        started=started,
                    )
            except urllib.error.HTTPError as exc:
                if 300 <= int(exc.code) < 400:
                    location = exc.headers.get("Location")
                    if not location:
                        raise WebFetchError(f"redirect response missing Location header: HTTP {exc.code}") from exc
                    next_url = urllib.parse.urljoin(current_url, location)
                    next_url = _validate_url(next_url)
                    if not _is_same_host_redirect(current_url, next_url):
                        raise WebFetchError(
                            "cross-host redirect is not followed automatically: "
                            f"{_redacted_url(current_url)} -> {_redacted_url(next_url)}"
                        ) from exc
                    redirect_chain.append(next_url)
                    current_url = next_url
                    continue
                body = exc.read(500).decode("utf-8", errors="replace")
                raise WebFetchError(f"web_fetch HTTP {exc.code}: {sanitize_for_log(body)}") from exc
            except WebFetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize request failures
                raise WebFetchError(f"web_fetch request failed: {sanitize_for_log(str(exc))}") from exc

        raise WebFetchError(f"too many redirects: limit is {self.max_redirects}")

    def _read_response(
        self,
        response: object,
        *,
        original_url: str,
        final_url: str,
        use_proxy: bool,
        redirect_chain: tuple[str, ...],
        started_at: str,
        started: float,
    ) -> WebFetchResult:
        headers = response.headers  # type: ignore[attr-defined]
        content_type = str(headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
        if not _is_text_content_type(content_type):
            raise WebFetchError(f"unsupported content type for web_fetch: {content_type or 'unknown'}")
        body = response.read(self.max_bytes + 1)  # type: ignore[attr-defined]
        body_truncated = len(body) > self.max_bytes
        if body_truncated:
            body = body[: self.max_bytes]
        charset = _charset_from_headers(headers) or "utf-8"
        text = body.decode(charset, errors="replace")
        markdown = _to_markdown(text, content_type=content_type)
        markdown_truncated = len(markdown) > MAX_MARKDOWN_CHARS
        if markdown_truncated:
            markdown = markdown[:MAX_MARKDOWN_CHARS]
        completed_at = utc_now_iso()
        return WebFetchResult(
            url=original_url,
            final_url=final_url,
            status_code=int(response.status),  # type: ignore[attr-defined]
            content_type=content_type,
            bytes_read=len(body),
            content_hash="sha256:" + hashlib.sha256(body).hexdigest(),
            markdown=markdown,
            markdown_truncated=markdown_truncated,
            body_truncated=body_truncated,
            use_proxy=use_proxy,
            redirect_chain=redirect_chain,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class _MarkdownHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._pending_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "tr", "br"}:
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._newline()
            self.parts.append("#" * int(tag[1]))
            self.parts.append(" ")
        elif tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag == "a":
            attrs_dict = {key.lower(): value for key, value in attrs}
            href = attrs_dict.get("href")
            self._pending_href = href if href and href.startswith(("http://", "https://")) else None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._pending_href:
            self.parts.append(f" ({self._pending_href})")
            self._pending_href = None
        if tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self.parts.append(text)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return html.unescape(text).strip()

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


def _validate_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise WebFetchError("web_fetch url is empty")
    if len(value) > MAX_URL_LENGTH:
        raise WebFetchError(f"web_fetch url is too long: max {MAX_URL_LENGTH} characters")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise WebFetchError("web_fetch only supports http/https URLs")
    if parsed.username or parsed.password:
        raise WebFetchError("web_fetch rejects URLs with username or password")
    if not parsed.hostname:
        raise WebFetchError("web_fetch URL must include a hostname")
    if len(parsed.query) > MAX_QUERY_LENGTH:
        raise WebFetchError(f"web_fetch query is too long: max {MAX_QUERY_LENGTH} characters")
    return urllib.parse.urlunsplit(parsed)


def _validate_public_host(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if host.lower() in {"localhost", "host.docker.internal"}:
        raise WebFetchError(f"web_fetch rejects local host: {host}")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        _validate_hostname(host)
        try:
            infos = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise WebFetchError(f"web_fetch cannot resolve host: {host}") from exc
        for info in infos:
            address = info[4][0]
            _reject_private_ip(address)
        return
    _reject_private_ip(str(ip))


def _validate_hostname(host: str) -> None:
    if "." not in host:
        raise WebFetchError("web_fetch hostname must be a public fully qualified name")
    labels = host.split(".")
    if any(not label for label in labels):
        raise WebFetchError("web_fetch hostname is invalid")


def _reject_private_ip(address: str) -> None:
    ip = ipaddress.ip_address(address)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise WebFetchError(f"web_fetch rejects non-public address: {address}")


def _is_same_host_redirect(current_url: str, next_url: str) -> bool:
    current = urllib.parse.urlsplit(current_url)
    target = urllib.parse.urlsplit(next_url)
    if target.scheme != current.scheme:
        return False
    current_host = (current.hostname or "").lower().removeprefix("www.")
    target_host = (target.hostname or "").lower().removeprefix("www.")
    return current_host == target_host and _effective_port(current) == _effective_port(target)


def _effective_port(parsed: urllib.parse.SplitResult) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _is_text_content_type(content_type: str) -> bool:
    if not content_type:
        return True
    return any(content_type.startswith(prefix) for prefix in TEXT_CONTENT_TYPES)


def _charset_from_headers(headers: object) -> str | None:
    get_content_charset = getattr(headers, "get_content_charset", None)
    if callable(get_content_charset):
        return get_content_charset()
    return None


def _to_markdown(text: str, *, content_type: str) -> str:
    if content_type in {"text/html", "application/xhtml+xml"} or "<html" in text[:1000].lower():
        parser = _MarkdownHTMLParser()
        parser.feed(text)
        return parser.markdown()
    return text.strip()


def _redacted_url(url: str) -> str:
    return str(sanitize_for_log(url))


def _build_proxy_opener(proxy_env: dict[str, str] | None) -> urllib.request.OpenerDirector:
    proxies = _proxy_mapping(proxy_env or {})
    if not proxies:
        raise WebFetchError("web_fetch proxy requested but no active proxy is configured")
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _NoRedirectHandler())


def _proxy_mapping(proxy_env: dict[str, str]) -> dict[str, str]:
    http = str(proxy_env.get("HTTP_PROXY") or proxy_env.get("http_proxy") or "").strip()
    https = str(proxy_env.get("HTTPS_PROXY") or proxy_env.get("https_proxy") or "").strip()
    all_proxy = str(proxy_env.get("ALL_PROXY") or proxy_env.get("all_proxy") or "").strip()
    proxies: dict[str, str] = {}
    if http or all_proxy:
        proxies["http"] = http or all_proxy
    if https or all_proxy:
        proxies["https"] = https or all_proxy
    return proxies
