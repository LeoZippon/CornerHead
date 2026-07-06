"""Agent-facing web_fetch_tool for meta-learning sessions."""

from __future__ import annotations

from autotrade.environment.runtime import new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.web_fetch import WebFetchError, WebFetchService

from .base import ActionField, ActionSpec, ToolContext

DEFAULT_MAX_CHARS = 12_000
MAX_RESULT_CHARS = 30_000


def build_web_fetch_spec() -> ActionSpec:
    return ActionSpec(
        action="web_fetch",
        tool_name="web_fetch_tool",
        description=(
            "Fetch one public http/https page for meta-learning only. Host-side, read-only, "
            "GET-only, no cookies/auth/custom headers/POST/browser rendering/JS/PDF parsing."
        ),
        fields=(
            ActionField(
                "url",
                "string",
                required=True,
                description="Public http/https URL to fetch. Credentials, localhost and private addresses are rejected.",
            ),
            ActionField(
                "max_chars",
                "integer",
                default=DEFAULT_MAX_CHARS,
                min_value=1000,
                max_value=MAX_RESULT_CHARS,
                description="Maximum markdown characters returned inline; the full bounded extraction is stored in logs.",
            ),
            ActionField(
                "use_proxy",
                "boolean",
                default=False,
                description="Whether this fetch may use the Runner's active HTTP_PROXY/HTTPS_PROXY/ALL_PROXY environment.",
            ),
        ),
        read_only=True,
        destructive=False,
        concurrency_safe=True,
        max_result_chars=MAX_RESULT_CHARS,
        result_policy="bounded_inline_with_artifact",
        allowed_modes=("meta_learning",),
    )


class AgentWebFetchTool:
    name = "web_fetch_tool"

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.service = WebFetchService()
        self.spec = build_web_fetch_spec()

    def run(self, *, url: str, max_chars: int = DEFAULT_MAX_CHARS, use_proxy: bool = False) -> dict[str, object]:
        max_chars = max(1000, min(int(max_chars), MAX_RESULT_CHARS))
        try:
            result = self.service.fetch(
                url,
                use_proxy=bool(use_proxy),
                proxy_env=self._proxy_env() if use_proxy else None,
            )
        except WebFetchError as exc:
            failure = sanitize_for_log(
                {
                    "tool": self.name,
                    "tool_spec": self.spec.to_record(),
                    "url": url,
                    "max_chars": max_chars,
                    "use_proxy": bool(use_proxy),
                    "status": "error",
                    "error": str(exc),
                    "completed_at": utc_now_iso(),
                }
            )
            self.ctx.trace.emit("web_fetch", failure, step_id=self.ctx.current_step_id)
            raise

        markdown_path = self._write_markdown(result.markdown)
        inline = result.markdown[:max_chars]
        payload = result.to_record()
        payload.update(
            {
                "tool": self.name,
                "tool_spec": self.spec.to_record(),
                "status": "ok",
                "markdown_path": markdown_path["markdown_path"],
                "host_markdown_path": markdown_path["host_markdown_path"],
                "content": inline,
                "truncated": len(result.markdown) > len(inline) or result.markdown_truncated,
                "max_chars": max_chars,
                "use_proxy": bool(use_proxy),
            }
        )
        payload = sanitize_for_log(payload)
        trace_payload = {key: value for key, value in payload.items() if key != "content"}
        self.ctx.trace.emit("web_fetch", trace_payload, step_id=self.ctx.current_step_id)
        return payload

    def _write_markdown(self, markdown: str) -> dict[str, str | None]:
        result_id = new_id("webfetch")
        target_dir = self.ctx.paths.logs / "web_fetch"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{result_id}.md"
        path.write_text(str(sanitize_for_log(markdown)), encoding="utf-8", errors="replace")
        mapped_path: str | None
        try:
            mapped_path = self.ctx.executor.map_path(path) if self.ctx.executor is not None else str(path)
        except Exception:  # noqa: BLE001 - host path still supports audit
            mapped_path = None
        return {"markdown_path": mapped_path, "host_markdown_path": str(path)}

    def _proxy_env(self) -> dict[str, str]:
        raw = self.ctx.extra.get("web_fetch_proxy_env")
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value is not None}
