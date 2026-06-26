"""Agent-facing web_search_tool for meta-learning sessions.

Provider implementations live in ``hl_trader.environment.web_search``. This
module keeps the Tool contract beside the other Agent-visible tools.
"""

from __future__ import annotations

from collections.abc import Mapping

from hl_trader.environment.runtime import sanitize_for_log, utc_now_iso
from hl_trader.environment.web_search import WebSearchError, WebSearchProvider, WebSearchService

from .base import ActionField, ActionSpec, ToolContext

META_SEARCH_PERSPECTIVES = (
    "finance_quant_econ",
    "natural_science_engineering",
    "philosophy_methodology",
)


def build_web_search_spec(engines: tuple[str, ...]) -> ActionSpec:
    return ActionSpec(
        action="web_search",
        tool_name="web_search_tool",
        description=(
            "Run host-side web search for meta-learning only. Use multiple engines/perspectives "
            "to form transferable research Taste; results are evidence, not trading labels."
        ),
        fields=(
            ActionField(
                "engine",
                "string",
                required=True,
                choices=engines,
                description="Search backend to use, chosen from the configured engines.",
            ),
            ActionField(
                "perspective",
                "string",
                required=True,
                choices=META_SEARCH_PERSPECTIVES,
                description="Research lens; successful meta-learning must cover all required perspectives before done.",
            ),
            ActionField(
                "query",
                "string",
                required=True,
                description="Focused search query. Avoid using future validation/test dates or stock outcomes as terms.",
            ),
            ActionField(
                "max_results",
                "integer",
                default=5,
                min_value=1,
                max_value=10,
                description="Maximum search results to return.",
            ),
        ),
        read_only=True,
        destructive=False,
        concurrency_safe=True,
        result_policy="bounded_by_max_results",
        allowed_modes=("meta_learning",),
    )


class AgentWebSearchTool:
    name = "web_search_tool"

    def __init__(self, ctx: ToolContext, providers: Mapping[str, WebSearchProvider]) -> None:
        self.ctx = ctx
        self.service = WebSearchService(providers)
        self.spec = build_web_search_spec(self.engines)

    @property
    def engines(self) -> tuple[str, ...]:
        return self.service.engines

    def run(self, *, engine: str, perspective: str, query: str, max_results: int = 5) -> dict[str, object]:
        try:
            result = self.service.run(query, max_results=max_results, engine=engine, perspective=perspective)
        except WebSearchError as exc:
            failure = sanitize_for_log(
                {
                    "tool": self.name,
                    "tool_spec": self.spec.to_record(),
                    "engine": engine,
                    "perspective": perspective,
                    "query": query,
                    "max_results": max_results,
                    "status": "error",
                    "error": str(exc),
                    "completed_at": utc_now_iso(),
                }
            )
            self.ctx.trace.emit("web_search", failure, step_id=self.ctx.current_step_id)
            raise
        result["tool_spec"] = self.spec.to_record()
        result_count = int(result.get("result_count") or 0)
        result["status"] = "ok" if result_count > 0 else "empty_results"
        self.ctx.trace.emit("web_search", result, step_id=self.ctx.current_step_id)
        return result
