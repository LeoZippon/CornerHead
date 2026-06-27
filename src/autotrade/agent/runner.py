"""Agent session runner: the main conversation loop for one Fold or meta-learning run.

docs/agent_design.md + docs/environment_design.md 3.4: one Agent session per
Fold (one conversation_id), Steps share the session, only the four documented
entrypoints are callable, the fold deadline is the master constraint, and a
fixed wrap-up prompt fires at most once inside the finalize window. Main
conversation calls and semantic compactions are logged in agent_trace.jsonl
(docs/environment_design.md 6.3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from autotrade.environment.explore import ExploreSubAgentEngine
from autotrade.environment.llm.proxy import LLMProxy, LLMProxyError, ProviderResponse
from autotrade.environment.runtime import new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.tools import (
    AgentWebSearchTool,
    BacktestTool,
    FinishFoldTool,
    ModificationCheckTool,
    SandboxShellTool,
    StructuredSearchTool,
    ToolContext,
    ToolError,
)
from autotrade.environment.tools.artifact_io import ArtifactIOTool
from autotrade.environment.tools.base import ActionField, ActionSpec, ToolSchemaError
from autotrade.environment.tools.web_search import META_SEARCH_PERSPECTIVES, build_web_search_spec
from autotrade.environment.web_search import WebSearchError, WebSearchProvider

from .compact import (
    ContextCompactionConfig,
    ContextCompactor,
    estimate_messages_tokens,
    is_compaction_message,
    is_llm_compaction_message,
    safe_error_summary,
)
from .prompts import WRAP_UP_PROMPT, build_experiment_facts, build_meta_learning_prompt, build_system_prompt

SESSION_MODES = ("fold", "meta_learning")
TERMINAL_ACTIONS = {"done", "finish_fold"}
_CLEARED_TOOL_RESULT = json.dumps(
    {
        "observation": "cleared",
        "note": "旧工具原始结果已清理以节省上下文；完整结果见 /mnt/artifacts/agent_trace.jsonl 与结果产物。",
    },
    ensure_ascii=False,
)
@dataclass(frozen=True)
class AgentSessionConfig:
    fold_deadline_at: datetime
    finalize_before_deadline_seconds: int = 300
    per_call_timeout_seconds: float = 300.0
    max_llm_calls: int = 200
    max_steps: int = 10
    # Estimated prompt tokens are the primary trigger for trim/clear; the message
    # counts are high safety caps so a many-small-turn session still stays bounded
    # without rewriting the cacheable prefix every turn (which resets the cache).
    max_history_messages: int = 150
    trim_token_threshold: int = 60000
    # Code-writing turns need room; reasoning tokens also count against this.
    max_response_tokens: int = 8000
    context_summary_max_items: int = 30
    context_summary_max_chars: int = 6000
    # Context editing: clear (not summarize) large old raw tool-result bodies in
    # place, keeping the most recent ones, to defer the costlier compaction.
    clear_tool_results: bool = True
    tool_result_keep_recent: int = 8
    tool_result_clear_min_chars: int = 4000
    tool_result_clear_token_threshold: int = 24000
    context_compaction: ContextCompactionConfig = field(default_factory=ContextCompactionConfig)


class AgentSessionRunner:
    """Drives one Agent session against the Environment tools.

    ``mode="fold"`` is the normal validation session; ``mode="meta_learning"``
    runs the Epoch-start Taste/regularization session, where backtest/finish_fold
    are rejected and the session ends with a ``done`` action.
    """

    def __init__(
        self,
        ctx: ToolContext,
        proxy: LLMProxy,
        config: AgentSessionConfig,
        *,
        fold_info: dict[str, object],
        acceptance_rules: dict[str, object],
        anti_overfit_prompt: str | None = None,
        convergence_prompt: str | None = None,
        phase: str = "exploration",
        step_tree_enabled: bool = False,
        taste_prompt: str = "",
        meta_learning_directive: str = "",
        mode: str = "fold",
        web_search_providers: Mapping[str, WebSearchProvider] | None = None,
        compact_proxy: LLMProxy | None = None,
        explore_proxy: LLMProxy | None = None,
    ) -> None:
        if mode not in SESSION_MODES:
            raise ValueError(f"unsupported session mode: {mode}")
        self.ctx = ctx
        self.proxy = proxy
        # Read-only Explore sub-agent runs on a cheaper proxy when wired, else the
        # main proxy (still saves main-context tokens via the returned digest).
        self.explore_proxy = explore_proxy or proxy
        self.config = config
        self.mode = mode
        providers: dict[str, WebSearchProvider] = {}
        if web_search_providers:
            for engine, provider in web_search_providers.items():
                providers[str(engine or provider.provider)] = provider
        self.web_search = AgentWebSearchTool(ctx, providers) if providers else None
        self.web_search_engines = self.web_search.engines if self.web_search is not None else ()
        self.compactor = ContextCompactor(compact_proxy, config.context_compaction) if compact_proxy is not None else None
        experiment_facts = self._experiment_facts()
        if mode == "meta_learning":
            self.system_prompt = build_meta_learning_prompt(
                experiment_directive=meta_learning_directive,
                experiment_facts=experiment_facts,
            )
        else:
            prompt_kwargs: dict[str, object] = {
                "fold_info": fold_info,
                "acceptance_rules": acceptance_rules,
                "experiment_facts": experiment_facts,
                "phase": phase,
                "step_tree_enabled": step_tree_enabled,
            }
            if anti_overfit_prompt:
                prompt_kwargs["anti_overfit_prompt"] = anti_overfit_prompt
            if convergence_prompt:
                prompt_kwargs["convergence_prompt"] = convergence_prompt
            if taste_prompt:
                prompt_kwargs["taste_prompt"] = taste_prompt
            self.system_prompt = build_system_prompt(**prompt_kwargs)
        self.shell = SandboxShellTool(ctx)
        self.artifact_io = ArtifactIOTool(ctx)
        self.modification_check = ModificationCheckTool(ctx)
        self.backtest = BacktestTool(ctx)
        self.finish_fold = FinishFoldTool(ctx)
        self.search = StructuredSearchTool(ctx)
        self.action_specs = self._build_action_specs()
        self._tool_schemas_cache: list[dict[str, object]] | None = None
        self._observation_digests: list[dict[str, object]] = []
        self._message_seq = 0
        self._meta_search_perspectives: set[str] = set()
        self._token_totals: dict[str, int] = {
            key: 0
            for key in (
                "llm_calls_with_usage",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "reasoning_tokens",
                "cache_hit_tokens",
                "cache_miss_tokens",
            )
        }

    def _experiment_facts(self) -> dict[str, object]:
        runtime_env = _read_json_if_exists(self.ctx.paths.runtime_env)
        data_summary = _read_json_if_exists(self.ctx.paths.data_summary)
        compaction = self.config.context_compaction
        model_empty = not any(self.ctx.paths.model_artifacts.iterdir()) if self.ctx.paths.model_artifacts.exists() else True
        return build_experiment_facts(
            manifest=dict(self.ctx.manifest.data),
            runtime_env=runtime_env,
            data_summary=data_summary,
            max_llm_calls=self.config.max_llm_calls,
            context_compaction={
                "enabled": self.compactor is not None,
                "token_threshold": compaction.token_threshold,
                "max_calls": compaction.max_calls,
            },
            model_artifacts_empty=model_empty,
        )

    def run(self) -> dict[str, object]:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": self._initial_user_message(),
            },
        ]
        llm_calls = 0
        step_index = 1
        self.ctx.current_step_id = f"step_{step_index:03d}"
        wrap_up_sent = False
        finish_status = "deadline_timeout"

        while llm_calls < self.config.max_llm_calls:
            remaining = self._remaining_seconds()
            if remaining <= 0:
                finish_status = "deadline_timeout"
                break
            if self.ctx.write_locked:
                finish_status = "fold_finished"
                break
            if not wrap_up_sent and remaining <= self.config.finalize_before_deadline_seconds:
                messages.append({"role": "user", "content": WRAP_UP_PROMPT})
                wrap_up_sent = True
            messages = self._compact_if_needed(messages, remaining)
            remaining = self._remaining_seconds()
            if remaining <= 0:
                finish_status = "deadline_timeout"
                break

            try:
                response = self._next_turn(messages, remaining)
                llm_calls += 1
            except LLMProxyError as exc:
                llm_calls += 1
                observation = {
                    "observation": "llm_error",
                    "error": safe_error_summary(exc),
                    "retry_hint": "Call a tool to proceed; do not repeat the same failing request verbatim.",
                }
                messages.append({"role": "user", "content": json.dumps(observation, ensure_ascii=False)})
                self._remember_observation("llm_call", observation)
                messages = self._trim(messages)
                continue

            tool_calls = list(response.tool_calls)
            assistant_message: dict[str, object] = {"role": "assistant", "content": response.content or ""}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)

            if not tool_calls:
                # The model spoke without acting; only tool calls advance the Fold.
                nudge = {
                    "observation": "no_tool_call",
                    "retry_hint": (
                        "用工具行动（grep/glob/shell/modification_check/backtest/finish_fold 等）；"
                        "仅输出文字不会推进本 Fold。"
                    ),
                }
                messages.append({"role": "user", "content": json.dumps(nudge, ensure_ascii=False)})
                self._remember_observation("llm_call", nudge)
                messages = self._trim(messages)
                continue

            # Every tool_call in the assistant turn must get exactly one tool result,
            # or the next request is rejected for an unmatched tool_call_id.
            results = self._dispatch_tool_calls(tool_calls)
            first_new_tool_index = len(messages)
            for tool_call_id, action, observation in results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(sanitize_for_log(observation), ensure_ascii=False, default=str),
                    }
                )
                self._remember_observation(action, observation)
            messages = self._clear_stale_tool_results(messages, protect_from_index=first_new_tool_index)
            messages = self._trim(messages)

            terminal = False
            for _tool_call_id, action, observation in results:
                if (
                    self.mode == "meta_learning"
                    and action == "done"
                    and observation.get("observation") == "meta_learning_done"
                ):
                    finish_status = "meta_learning_done"
                    terminal = True
                    break
                if action == "finish_fold" and observation.get("status") == "fold_finished":
                    finish_status = "fold_finished"
                    terminal = True
                    break
                # Only successful complete validations are counted as formal Steps.
                if action == "backtest" and observation.get("status") == "ok" and observation.get("complete_validation"):
                    if step_index >= self.config.max_steps:
                        finish_status = "step_limit_reached"
                        terminal = True
                        break
                    step_index += 1
                    self.ctx.current_step_id = f"step_{step_index:03d}"
            if terminal:
                break

        summary = {
            "finish_status": finish_status,
            "llm_calls": llm_calls,
            "steps_used": step_index,
            "wrap_up_sent": wrap_up_sent,
            "write_locked": self.ctx.write_locked,
            "context_compactions": self.compactor.compaction_count if self.compactor is not None else 0,
            "context_compaction_calls": self.compactor.compaction_attempts if self.compactor is not None else 0,
            "token_usage": self._token_usage_summary(),
            "ended_at": utc_now_iso(),
        }
        self.ctx.extra["agent_session_summary"] = summary
        if finish_status == "meta_learning_done":
            self.ctx.extra["meta_learning_done"] = True
        self.ctx.trace.emit("session_end", summary, step_id=self.ctx.current_step_id)
        return summary

    # ---- internals ----

    def _build_action_specs(self) -> dict[str, ActionSpec]:
        specs = [
            self.shell.spec,
            self.artifact_io.write_spec,
            self.artifact_io.edit_spec,
            self.search.grep_spec,
            self.search.glob_spec,
            self.modification_check.spec,
            self.backtest.spec,
            self.finish_fold.spec,
            self.web_search.spec if self.web_search is not None else build_web_search_spec(self.web_search_engines),
            ActionSpec(
                action="note",
                tool_name="runner_note",
                description="Record a short reasoning note without executing tools.",
                fields=(ActionField("text", "string", default="", description="Short note to keep in the trace."),),
                read_only=True,
                destructive=False,
                concurrency_safe=True,
                allowed_modes=("fold", "meta_learning"),
            ),
            ActionSpec(
                action="explore",
                tool_name="explore_subagent",
                description=(
                    "委托只读数据探查 Sub Agent（更便宜模型）调查一个具体问题，返回简洁结论摘要；"
                    "只读、不能修改产物，适合把大量 shell/grep 数据探查从主上下文转移出去。"
                ),
                fields=(
                    ActionField(
                        "task",
                        "string",
                        required=True,
                        description=(
                            "Concrete read-only investigation request. Ask for evidence and concise findings, "
                            "not final strategy design."
                        ),
                    ),
                    ActionField(
                        "max_rounds",
                        "integer",
                        default=0,
                        min_value=0,
                        max_value=12,
                        description="Optional cap on Explore sub-agent tool-call rounds; 0 uses the default.",
                    ),
                ),
                read_only=True,
                destructive=False,
                concurrency_safe=False,
                allowed_modes=("fold", "meta_learning"),
            ),
            ActionSpec(
                action="done",
                tool_name="meta_learning_done",
                description=(
                    "End a meta-learning session after workspace/taste.md is non-empty and required "
                    "web_search perspectives have successful calls when web search is configured."
                ),
                read_only=True,
                destructive=False,
                concurrency_safe=False,
                allowed_modes=("meta_learning",),
            ),
        ]
        return {spec.action: spec for spec in specs}

    def _initial_user_message(self) -> str:
        if self.mode == "meta_learning":
            return (
                "开始元学习。先读取 development_history、meta_learning_memory、run_manifest、runtime_env 和 data_summary；"
                "用 shell/Python 只读检查并分析可见 snapshot 的文件清单、字段、行数、日期覆盖、关键空值和单位；"
                "需要时反复使用 shell、grep/glob 和 web_search；配置联网检索时完成三类 perspective 的非空成功检索；"
                "把简洁可执行的中文 Taste 写入 /mnt/agent/workspace/taste.md；"
                "如需正则化，可在约束内简化 output/models；最后调用 done。"
            )
        return (
            "开始本 Fold。先读取 run_manifest、runtime_env、data_summary 和可见数据；"
            "改进策略产物，通过 modification_check 与 backtest 验证，满足条件后调用 finish_fold。"
        )

    def _tool_schemas(self) -> list[dict[str, object]]:
        """Mode-filtered native tool definitions (built once per session)."""
        if self._tool_schemas_cache is None:
            self._tool_schemas_cache = [
                spec.to_tool_schema()
                for spec in self.action_specs.values()
                if self.mode in spec.allowed_modes
            ]
        return self._tool_schemas_cache

    def _next_turn(self, messages: list[dict[str, object]], remaining: float) -> ProviderResponse:
        timeout = min(self.config.per_call_timeout_seconds, max(remaining, 1.0))
        detail: dict[str, object] = {
            "purpose": "agent_main_conversation",
            "provider": self.proxy.provider,
            "model": self.proxy.model,
            # Log only messages first seen this turn (the delta), not the whole
            # growing history each call. Concatenating every call's new_messages
            # with its content/tool_calls reconstructs the full conversation.
            "new_messages": sanitize_for_log(self._log_new_messages(messages)),
            "message_count": len(messages),
            "started_at": utc_now_iso(),
        }
        try:
            response = self.proxy.complete_tools(
                messages,
                tools=self._tool_schemas(),
                tool_choice="auto",
                timeout_seconds=timeout,
                max_tokens=self.config.max_response_tokens,
            )
        except LLMProxyError as exc:
            detail.update(
                status="timeout" if exc.timeout else "error",
                error=safe_error_summary(exc),
                completed_at=utc_now_iso(),
            )
            self.ctx.trace.emit("llm_call", detail, step_id=self.ctx.current_step_id)
            raise
        detail.update(
            status="ok",
            completed_at=utc_now_iso(),
            content=response.content,
            reasoning_content=response.reasoning_content,
            tool_calls=sanitize_for_log([dict(tc) for tc in response.tool_calls]),
            usage=response.usage,
        )
        self._accumulate_usage(response.usage)
        self.ctx.trace.emit("llm_call", detail, step_id=self.ctx.current_step_id)
        return response

    def _log_new_messages(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        """Tag unseen messages with a sequence id and return the new ones to log.

        Each message is logged exactly once (when first seen by a turn), so the
        trace keeps the complete conversation without re-embedding the full
        history every call. Assistant messages are tagged but not returned here:
        their content/tool_calls are already recorded on the producing llm_call.
        """
        new: list[dict[str, object]] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("_seq") is not None:
                continue
            message["_seq"] = self._message_seq
            self._message_seq += 1
            if message.get("role") != "assistant":
                new.append(message)
        return new

    def _accumulate_usage(self, usage: object) -> None:
        """Sum prompt/completion/reasoning/cache tokens across main-conversation calls.

        Cache hits are only realized while the request prefix (system prompt +
        tool schemas + early history) stays byte-stable; ``_trim`` and compaction
        rewrite history and reset that prefix, so the session ``cache_hit_ratio``
        is the lever for tuning how aggressively to trim/compact.
        """
        if not isinstance(usage, dict):
            return
        totals = self._token_totals
        totals["llm_calls_with_usage"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += int(value)
        hit = usage.get("prompt_cache_hit_tokens")
        if not isinstance(hit, (int, float)) or isinstance(hit, bool):
            prompt_details = usage.get("prompt_tokens_details")
            hit = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        if isinstance(hit, (int, float)) and not isinstance(hit, bool):
            totals["cache_hit_tokens"] += int(hit)
        miss = usage.get("prompt_cache_miss_tokens")
        if isinstance(miss, (int, float)) and not isinstance(miss, bool):
            totals["cache_miss_tokens"] += int(miss)
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning = completion_details.get("reasoning_tokens")
            if isinstance(reasoning, (int, float)) and not isinstance(reasoning, bool):
                totals["reasoning_tokens"] += int(reasoning)

    def _token_usage_summary(self) -> dict[str, object]:
        totals = dict(self._token_totals)
        prompt = totals.get("prompt_tokens", 0)
        totals["cache_hit_ratio"] = round(totals["cache_hit_tokens"] / prompt, 4) if prompt else 0.0
        return totals

    def _parse_tool_arguments(
        self, name: str, raw_arguments: object
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        """Decode a tool_call ``arguments`` field into a dict, or an error observation."""
        if raw_arguments is None or raw_arguments == "":
            return {}, None
        if isinstance(raw_arguments, dict):
            return dict(raw_arguments), None
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                return None, {
                    "observation": "error",
                    "action": name,
                    "error": f"invalid tool arguments JSON: {exc}",
                    "error_type": "schema_error",
                    "retry_hint": "Send tool arguments as a single valid JSON object.",
                }
            if not isinstance(parsed, dict):
                return None, {
                    "observation": "error",
                    "action": name,
                    "error": "tool arguments must be a JSON object",
                    "error_type": "schema_error",
                }
            return parsed, None
        return None, {
            "observation": "error",
            "action": name,
            "error": "unsupported tool arguments payload",
            "error_type": "schema_error",
        }

    def _dispatch_tool_calls(
        self, tool_calls: list[dict[str, object]]
    ) -> list[tuple[str, str, dict[str, object]]]:
        """Run every tool_call in one assistant turn; one result per call.

        A turn whose calls are all concurrency-safe (grep/glob/note/web_search)
        runs in parallel; any stateful tool (shell/backtest/finish_fold/...)
        forces deterministic sequential execution.
        """
        plan: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = []
        for tool_call in tool_calls:
            tool_call_id = str(tool_call.get("id") or new_id("call")) if isinstance(tool_call, dict) else new_id("call")
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            name = str(function.get("name")) if isinstance(function, dict) else ""
            raw_arguments = function.get("arguments") if isinstance(function, dict) else None
            args, error = self._parse_tool_arguments(name, raw_arguments)
            plan.append((tool_call_id, name, args, error))

        specs = [self.action_specs.get(name) for _id, name, _args, error in plan if error is None]
        can_parallel = (
            len(plan) > 1
            and all(error is None for *_, error in plan)
            and all(spec is not None and spec.concurrency_safe for spec in specs)
        )

        def run_one(index: int) -> tuple[str, str, dict[str, object]]:
            tool_call_id, name, args, error = plan[index]
            if error is not None:
                return (tool_call_id, name, error)
            return (tool_call_id, name, self._dispatch(name, args or {}))

        results: list[tuple[str, str, dict[str, object]] | None] = [None] * len(plan)
        if can_parallel:
            with ThreadPoolExecutor(max_workers=min(len(plan), 4)) as executor:
                futures = {executor.submit(run_one, index): index for index in range(len(plan))}
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
        else:
            terminal_seen = False
            for index in range(len(plan)):
                tool_call_id, name, _args, _error = plan[index]
                if terminal_seen:
                    results[index] = (
                        tool_call_id,
                        name,
                        {
                            "observation": "cancelled",
                            "action": name,
                            "status": "cancelled",
                            "reason": "terminal_tool_already_called",
                            "retry_hint": "Start a new turn before making further changes after done/finish_fold.",
                        },
                    )
                    continue
                result = run_one(index)
                results[index] = result
                if name in TERMINAL_ACTIONS:
                    terminal_seen = True
        return [item for item in results if item is not None]

    def _dispatch(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        spec = self.action_specs.get(action)
        if spec is None:
            return {"observation": "error", "error": f"unknown action: {action!r}"}
        try:
            args = spec.validate(payload, mode=self.mode)
        except ToolSchemaError as exc:
            return {
                "observation": "error",
                "action": action,
                "error": str(exc),
                "error_type": "schema_error",
                "reason": str(exc),
                "retry_hint": "Retry with exactly the fields declared in tool_spec.",
                "tool_spec": spec.to_record(),
            }
        # No preemptive kill of in-flight work: per-call timeouts bound every
        # provider/tool call, and no NEW work is dispatched once the deadline
        # has passed, so the worst overrun is one bounded call.
        if self._remaining_seconds() <= 0 and action not in {"note", "done"}:
            cancellation = {
                "observation": "cancelled",
                "action": action,
                "status": "cancelled",
                "reason": "fold_deadline_reached",
                "tool_spec": spec.to_record(),
            }
            self.ctx.trace.emit("tool_cancelled", cancellation, step_id=self.ctx.current_step_id)
            return cancellation
        try:
            if action == "shell":
                result = self.shell.run(
                    str(args["command"]),
                    max_output_chars=int(args["max_output_chars"]),
                    timeout_seconds=int(args["timeout_seconds"]),
                )
                return {"observation": "shell", **result.to_record()}
            if action == "write_file":
                return {"observation": "write_file", **self.artifact_io.write_file(**args)}
            if action == "edit_file":
                return {"observation": "edit_file", **self.artifact_io.edit_file(**args)}
            if action == "grep":
                return {"observation": "grep", **self.search.grep(**args)}
            if action == "glob":
                return {"observation": "glob", **self.search.glob(**args)}
            if action == "modification_check":
                return {"observation": "modification_check", **self.modification_check.run()}
            if action == "backtest":
                if self.mode == "meta_learning":
                    return {"observation": "error", "action": action, "error": "backtests are not allowed in this session"}
                return {"observation": "backtest", **self.backtest.run(mode="valid", replay_window=(args or {}).get("replay_window"))}
            if action == "finish_fold":
                if self.mode == "meta_learning":
                    return {"observation": "error", "action": action, "error": "use done to end this session"}
                return {"observation": "finish_fold", **self.finish_fold.run()}
            if action == "web_search":
                if self.mode != "meta_learning":
                    return {"observation": "error", "action": action, "error": "web_search is only available in meta_learning"}
                if self.web_search is None:
                    return {"observation": "error", "action": action, "error": "web search provider is not configured"}
                result = self.web_search.run(
                    engine=str(args["engine"]),
                    perspective=str(args["perspective"]),
                    query=str(args["query"]),
                    max_results=int(args["max_results"]),
                )
                result_count = int(result.get("result_count") or 0)
                if result_count > 0:
                    self._meta_search_perspectives.add(str(args["perspective"]))
                return {"observation": "web_search", **result}
            if action == "explore":
                engine = ExploreSubAgentEngine(
                    self.explore_proxy,
                    shell=self.shell,
                    search=self.search,
                    trace=self.ctx.trace,
                    mode=self.mode,
                    step_id=self.ctx.current_step_id,
                    deadline_at=self.config.fold_deadline_at,
                )
                return {"observation": "explore", **engine.run(task=str(args["task"]), max_rounds=int(args.get("max_rounds") or 0))}
            if action == "note":
                return {"observation": "note_recorded", "text": str(args.get("text", ""))}
            if action == "done" and self.mode == "meta_learning":
                if self.web_search is not None:
                    missing = [item for item in META_SEARCH_PERSPECTIVES if item not in self._meta_search_perspectives]
                    if missing:
                        return {
                            "observation": "error",
                            "action": action,
                            "error": (
                                "complete successful web_search calls for all required perspectives before done: "
                                + ", ".join(missing)
                            ),
                        }
                taste_violation = self._taste_policy_violation()
                if taste_violation:
                    return {
                        "observation": "error",
                        "action": action,
                        "error": taste_violation,
                    }
                return {"observation": "meta_learning_done"}
            return {"observation": "error", "error": f"unknown action: {action!r}"}
        except ToolError as exc:
            return _tool_error_observation(action, exc)
        except WebSearchError as exc:
            return {"observation": "error", "action": action, "error": safe_error_summary(exc)}
        except Exception as exc:  # noqa: BLE001 - an agent action must never kill the fold
            error = safe_error_summary(exc)
            self.ctx.trace.emit(
                "error",
                {"action": action, "error": error},
                step_id=self.ctx.current_step_id,
            )
            return {"observation": "error", "action": action, "error": f"internal tool failure: {error}"}

    def _remaining_seconds(self) -> float:
        return (self.config.fold_deadline_at - datetime.now(timezone.utc)).total_seconds()

    def _compact_if_needed(self, messages: list[dict[str, str]], remaining: float) -> list[dict[str, str]]:
        if self.compactor is None:
            return messages
        result = self.compactor.compact(messages, remaining_seconds=remaining, step_id=self.ctx.current_step_id)
        if result is None:
            return messages
        self.ctx.trace.emit("context_compaction", result.event, step_id=self.ctx.current_step_id)
        return result.messages

    def _clear_stale_tool_results(
        self,
        messages: list[dict[str, object]],
        *,
        protect_from_index: int | None = None,
    ) -> list[dict[str, object]]:
        """Prune (not summarize) large old raw tool-result bodies in place.

        Keeps the most recent ``tool_result_keep_recent`` tool results raw, and
        replaces older oversized ones with a stub while preserving role and
        ``tool_call_id`` so the assistant/tool grouping (and the provider's
        tool_call_id matching) stays valid. Full results remain in the trace.
        """
        if not self.config.clear_tool_results:
            return messages
        # Token budget is the primary trigger: skip clearing (and the prefix
        # rewrite that resets the cache) while the context is still small.
        if estimate_messages_tokens(messages) < self.config.tool_result_clear_token_threshold:
            return messages
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "tool"]
        keep_recent = self.config.tool_result_keep_recent
        protected = set(tool_indices[-keep_recent:]) if keep_recent > 0 else set()
        if protect_from_index is not None:
            protected.update(index for index in tool_indices if index >= protect_from_index)
        cleared = 0
        chars_freed = 0
        for index in tool_indices:
            if index in protected:
                continue
            message = messages[index]
            content = message.get("content")
            if (
                isinstance(content, str)
                and len(content) >= self.config.tool_result_clear_min_chars
                and content != _CLEARED_TOOL_RESULT
            ):
                chars_freed += len(content)
                messages[index] = {**message, "content": _CLEARED_TOOL_RESULT}
                cleared += 1
        if cleared:
            self.ctx.trace.emit(
                "context_edit",
                {
                    "cleared_tool_results": cleared,
                    "chars_freed": chars_freed,
                    "kept_recent": keep_recent,
                    "protected_from_index": protect_from_index,
                    "min_chars": self.config.tool_result_clear_min_chars,
                },
                step_id=self.ctx.current_step_id,
            )
        return messages

    @staticmethod
    def _drop_leading_orphan_tools(seq: list[dict[str, object]]) -> list[dict[str, object]]:
        """Drop leading ``tool`` messages whose ``assistant`` turn was trimmed away.

        A ``tool`` message must follow the ``assistant`` ``tool_calls`` that
        produced it; a window that starts with one would be rejected.
        """
        index = 0
        while index < len(seq) and isinstance(seq[index], dict) and seq[index].get("role") == "tool":
            index += 1
        return list(seq[index:])

    def _trim(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        # Primary trigger is the estimated token budget; the message count is a
        # high safety cap. Trimming rewrites the prefix and resets the provider
        # cache, so it should fire on real size, not on every small-turn batch.
        if (
            len(messages) <= self.config.max_history_messages
            and estimate_messages_tokens(messages) < self.config.trim_token_threshold
        ):
            return messages
        if self.config.max_history_messages <= 2:
            keep = max(self.config.max_history_messages - 1, 0)
            tail = self._drop_leading_orphan_tools(messages[-keep:]) if keep else []
            return [messages[0], *tail]
        non_summary = [message for message in messages[1:] if not is_compaction_message(message)]
        latest_llm_summary = next(
            (message for message in reversed(messages[1:]) if is_llm_compaction_message(message)),
            None,
        )
        summary = self._context_summary_payload()
        summary_message = {"role": "user", "content": json.dumps(summary, ensure_ascii=False, default=str)}
        kept_llm_compaction = latest_llm_summary is not None and self.config.max_history_messages >= 4
        if kept_llm_compaction:
            keep = self.config.max_history_messages - 3
            tail = self._drop_leading_orphan_tools(non_summary[-keep:])
            trimmed = [messages[0], latest_llm_summary, summary_message, *tail]
        else:
            keep = self.config.max_history_messages - 2
            tail = self._drop_leading_orphan_tools(non_summary[-keep:])
            trimmed = [messages[0], summary_message, *tail]
        self.ctx.trace.emit(
            "context_summary",
            {
                "summary_items": len(summary["items"]),
                "kept_llm_compaction": kept_llm_compaction,
                "kept_messages": len(trimmed),
                "dropped_messages": max(len(messages) - len(trimmed), 0),
                "max_history_messages": self.config.max_history_messages,
            },
            step_id=self.ctx.current_step_id,
        )
        return trimmed

    def _remember_observation(self, action: str, observation: dict[str, object]) -> None:
        item: dict[str, object] = {
            "action": action,
            "observation": observation.get("observation"),
        }
        if observation.get("status") is not None:
            item["status"] = observation.get("status")
        if observation.get("error") is not None:
            item["error"] = _shorten(observation.get("error"), 240)
        if action == "shell":
            item.update(
                exit_code=observation.get("exit_code"),
                timed_out=observation.get("timed_out"),
                stdout_truncated=observation.get("stdout_truncated"),
                stderr_truncated=observation.get("stderr_truncated"),
                stdout_path=observation.get("stdout_path"),
                stderr_path=observation.get("stderr_path"),
            )
        elif action in {"grep", "glob"}:
            item.update(
                root=observation.get("root"),
                path=observation.get("path"),
                pattern=_shorten(observation.get("pattern"), 160),
                mode=observation.get("mode"),
                returned=observation.get("returned"),
                total=observation.get("total"),
                truncated=observation.get("truncated") or observation.get("truncated_by_chars"),
            )
        elif action == "modification_check":
            item.update(
                allowed_to_backtest=observation.get("allowed_to_backtest"),
                artifact_hash=observation.get("artifact_hash"),
                reasons=_shorten(observation.get("reasons"), 240),
            )
        elif action == "backtest":
            item.update(
                complete_validation=observation.get("complete_validation"),
                result_name=observation.get("result_name"),
                total_return=observation.get("total_return"),
                sharpe=observation.get("sharpe"),
                max_drawdown=observation.get("max_drawdown"),
            )
        elif action == "web_search":
            item.update(
                engine=observation.get("engine"),
                perspective=observation.get("perspective"),
                provider=observation.get("provider"),
                query=_shorten(observation.get("query"), 160),
                result_count=observation.get("result_count"),
            )
        elif action == "explore":
            item.update(
                explore_status=observation.get("status"),
                rounds=observation.get("rounds"),
                tool_calls=observation.get("tool_calls"),
                digest=_shorten(observation.get("digest"), 240),
            )
        elif action == "finish_fold":
            item.update(fold_status=observation.get("fold_status"), write_locked=observation.get("write_locked"))
        self._observation_digests.append({key: value for key, value in item.items() if value is not None})
        if len(self._observation_digests) > 120:
            self._observation_digests = self._observation_digests[-120:]

    def _context_summary_payload(self) -> dict[str, object]:
        items = self._observation_digests[-self.config.context_summary_max_items :]
        payload: dict[str, object] = {
            "observation": "context_summary",
            "summary_kind": "deterministic_runner_summary",
            "note": "Raw Shell/Tool/LLM details remain in /mnt/artifacts/agent_trace.jsonl and result artifacts.",
            "items": items,
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= self.config.context_summary_max_chars:
            return payload
        compact_items: list[dict[str, object]] = []
        for item in reversed(items):
            compact_items.insert(0, item)
            compact_payload = {**payload, "items": compact_items}
            if len(json.dumps(compact_payload, ensure_ascii=False, default=str)) > self.config.context_summary_max_chars:
                compact_items.pop(0)
                break
        return {**payload, "items": compact_items, "truncated": True}

    def _taste_policy_violation(self) -> str:
        if self.mode != "meta_learning":
            return ""
        taste_path = self.ctx.paths.workspace / "taste.md"
        if not taste_path.exists():
            return "write /mnt/agent/workspace/taste.md before done"
        text = taste_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return "taste.md must be non-empty before done"
        return ""

def _shorten(value: object, max_chars: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _read_json_if_exists(path) -> dict[str, object]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_error_observation(action: str, exc: ToolError) -> dict[str, object]:
    observation: dict[str, object] = {
        "observation": "error",
        "action": action,
        "error": safe_error_summary(exc),
    }
    observation.update(exc.to_record())
    return {key: value for key, value in observation.items() if value not in (None, {}, "")}
