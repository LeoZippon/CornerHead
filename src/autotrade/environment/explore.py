"""Read-only data-exploration Sub Agent (Claude-Code "Explore" pattern).

The Fold/meta-learning Agent delegates a concrete read-only investigation to a
cheaper-model sub-agent that may call ``shell``/``grep``/``glob`` over the
visible sandbox. It returns a compact evidence digest, so the expensive main
context stays small and routine probing runs on the cheaper model. It never
writes formal artifacts.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from autotrade.environment.llm.proxy import LLMProxy, LLMProxyError, ProviderResponse
from autotrade.environment.runtime import new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.tools.base import ToolError, ToolSchemaError

EXPLORE_SYSTEM_PROMPT = """\
# 角色
你是主 Agent 的只读调查员，只回答委托给你的具体问题。你可以用 shell / grep / glob \
读取与统计可见数据（snapshot、产物、结果、日志），但不要修改任何文件，不要写正式产物，\
不要替主 Agent 设计最终策略、写 Taste 或做全局综合判断。
# 方法
- 优先用 grep/glob 做定向搜索，用 shell 做目录、metadata、head/count/limit、轻量 Python/DuckDB 只读抽样；不要全量读取大表。
- shell 是轻量合同 guard，不是只读 Bash 解析器；不要写文件、不要重定向到文件、不要隐藏错误。只读约定由本提示约束，硬隔离和产物校验兜底。
- 一轮可并行发起多个相互独立的只读检索；工具错误要如实保留，不要猜测成功。
- shell 命令不要用 `2>/dev/null` 隐藏错误。
# 交付
信息足够后停止调用工具，直接用简洁中文返回四部分：结论、证据、风险与限制、建议主 Agent 下一步。\
证据要包含关键路径、字段、数字或日期覆盖；不要罗列原始长输出。
"""


@dataclass(frozen=True)
class ExploreSubAgentConfig:
    per_call_timeout_seconds: float = 120.0
    # Room for a tool-call round (long DuckDB/SQL arguments) plus a concise
    # digest; too small a cap makes a round stop on finish_reason=length.
    max_tokens: int = 6000
    max_rounds: int = 6


class ExploreSubAgentEngine:
    """Bounded native-tool exploration loop over read-only sandbox tools."""

    def __init__(
        self,
        proxy: LLMProxy,
        *,
        shell,
        search,
        trace,
        mode: str = "fold",
        step_id: str | None = None,
        deadline_at: datetime | None = None,
        config: ExploreSubAgentConfig | None = None,
    ) -> None:
        self.proxy = proxy
        self.shell = shell
        self.search = search
        self.trace = trace
        self.mode = mode
        self.step_id = step_id
        self.deadline_at = deadline_at
        self.config = config or ExploreSubAgentConfig()
        self._specs = {"shell": shell.spec, "grep": search.grep_spec, "glob": search.glob_spec}
        self._schemas = [spec.to_tool_schema() for spec in self._specs.values()]

    def run(self, *, task: str, max_rounds: int | None = None, parent_call_id: str | None = None) -> dict[str, object]:
        rounds_limit = max_rounds if isinstance(max_rounds, int) and max_rounds > 0 else self.config.max_rounds
        task_id = new_id("explore")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": EXPLORE_SYSTEM_PROMPT},
            {"role": "user", "content": (task or "").strip() or "调查可见数据并给出摘要。"},
        ]
        rounds = 0
        tool_calls_made = 0
        digest = ""
        status = "completed"
        error = ""
        try:
            while rounds < rounds_limit:
                rounds += 1
                if self._remaining_seconds() <= 0:
                    status = "timeout"
                    error = "explore deadline reached"
                    break
                try:
                    response = self._call(
                        messages, task_id, f"explore_round_{rounds}", parent_call_id, tool_choice="auto"
                    )
                except LLMProxyError as exc:
                    if exc.timeout:
                        raise
                    # A round cut off by output length (or a transient provider
                    # error) must not waste the whole task: stop looping and
                    # summarize what was already gathered below.
                    break
                calls = self._parse_calls(response.tool_calls)
                if not calls:
                    digest = response.content.strip()
                    break
                messages.append(
                    {"role": "assistant", "content": response.content or "", "tool_calls": list(response.tool_calls)}
                )
                for tool_call_id, _name, observation, attempted_tool in self._dispatch_calls(calls):
                    if attempted_tool:
                        tool_calls_made += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(sanitize_for_log(observation), ensure_ascii=False, default=str),
                        }
                    )
            # Force a concise final digest when the loop ended without one
            # (rounds exhausted, or a round was cut off by output length).
            if status == "completed" and not digest:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "请立即按“结论 / 证据 / 风险与限制 / 建议主 Agent 下一步”四部分"
                            "给出简洁中文摘要，不要再调用工具，也不要罗列原始长输出。"
                        ),
                    }
                )
                response = self._call(messages, task_id, "explore_final", parent_call_id, tool_choice="none")
                digest = response.content.strip()
        except LLMProxyError as exc:
            status = "timeout" if exc.timeout else "error"
            error = str(sanitize_for_log(str(exc)))
        except Exception as exc:  # noqa: BLE001 - a sub-agent failure must not kill the Fold
            status = "error"
            error = str(sanitize_for_log(str(exc)))
        result: dict[str, object] = {
            "task_id": task_id,
            "status": status,
            "rounds": rounds,
            "tool_calls": tool_calls_made,
            "digest": digest,
            "model": self.proxy.model,
        }
        if error:
            result["error"] = error
        self.trace.emit(
            "explore", {**result, "task": str(task)[:500]}, step_id=self.step_id, parent_call_id=parent_call_id
        )
        return result

    def _call(
        self,
        messages: list[dict[str, object]],
        task_id: str,
        purpose: str,
        parent_call_id: str | None,
        *,
        tool_choice: str,
    ) -> ProviderResponse:
        detail: dict[str, object] = {
            "sub_agent": "explore",
            "task_id": task_id,
            "purpose": purpose,
            "provider": self.proxy.provider,
            "model": self.proxy.model,
            "messages": sanitize_for_log(messages),
            "started_at": utc_now_iso(),
        }
        try:
            timeout = self.config.per_call_timeout_seconds
            remaining = self._remaining_seconds()
            if remaining <= 0:
                raise LLMProxyError("explore deadline reached", timeout=True)
            if self.deadline_at is not None:
                timeout = min(timeout, max(remaining, 1.0))
            response = self.proxy.complete_tools(
                messages,
                tools=self._schemas,
                tool_choice=tool_choice,
                timeout_seconds=timeout,
                max_tokens=self.config.max_tokens,
            )
        except Exception as exc:
            detail.update(status="error", error=sanitize_for_log(str(exc)), completed_at=utc_now_iso())
            self.trace.emit("explore_llm_call", detail, step_id=self.step_id, parent_call_id=parent_call_id)
            raise
        detail.update(
            status="ok",
            completed_at=utc_now_iso(),
            content=response.content,
            reasoning_content=response.reasoning_content,
            tool_calls=sanitize_for_log([dict(tc) for tc in response.tool_calls]),
            usage=response.usage,
        )
        self.trace.emit("explore_llm_call", detail, step_id=self.step_id, parent_call_id=parent_call_id)
        return response

    def _parse_calls(
        self, tool_calls: object
    ) -> list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]]:
        parsed: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = []
        for tool_call in tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = str(tool_call.get("id") or new_id("call"))
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = str(function.get("name", ""))
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, dict):
                arguments: dict[str, object] = dict(raw_arguments)
            elif isinstance(raw_arguments, str) and raw_arguments.strip():
                try:
                    decoded = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    parsed.append((tool_call_id, name, None, {"observation": "error", "error": f"invalid arguments: {exc}"}))
                    continue
                arguments = decoded if isinstance(decoded, dict) else {}
            else:
                arguments = {}
            spec = self._specs.get(name)
            if spec is None:
                parsed.append(
                    (tool_call_id, name, None, {"observation": "error", "error": f"explore can only call shell/grep/glob, not {name!r}"})
                )
                continue
            try:
                validated = spec.validate(arguments, mode=self.mode)
            except ToolSchemaError as exc:
                parsed.append((tool_call_id, name, None, {"observation": "error", "error": str(exc)}))
                continue
            parsed.append((tool_call_id, name, validated, None))
        return parsed

    def _dispatch(self, name: str, args: dict[str, object]) -> dict[str, object]:
        if name == "shell":
            result = self.shell.run(
                str(args["command"]),
                max_output_chars=int(args["max_output_chars"]),
                timeout_seconds=self._bounded_tool_timeout(int(args["timeout_seconds"])),
            )
            return {"observation": "shell", **result.to_record()}
        if name == "grep":
            return {"observation": "grep", **self.search.grep(**args, timeout_seconds=self._search_timeout())}
        if name == "glob":
            return {"observation": "glob", **self.search.glob(**args, deadline_monotonic=self._search_deadline())}
        return {"observation": "error", "error": f"unsupported explore tool: {name!r}"}

    def _dispatch_calls(
        self, calls: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]]
    ) -> list[tuple[str, str, dict[str, object], bool]]:
        def run_one(index: int) -> tuple[str, str, dict[str, object], bool]:
            tool_call_id, name, args, call_error = calls[index]
            if call_error is not None:
                return tool_call_id, name, call_error, False
            if self._remaining_seconds() <= 0:
                return tool_call_id, name, {"observation": "cancelled", "reason": "explore_deadline_reached"}, True
            try:
                return tool_call_id, name, self._dispatch(name, args or {}), True
            except ToolError as exc:
                return tool_call_id, name, _tool_error_observation(name, exc), True

        can_parallel = (
            len(calls) > 1
            and all(call_error is None for *_rest, call_error in calls)
            and all(name in {"grep", "glob"} for _tool_call_id, name, _args, _call_error in calls)
        )
        if not can_parallel:
            return [run_one(index) for index in range(len(calls))]
        results: list[tuple[str, str, dict[str, object], bool] | None] = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as executor:
            futures = {executor.submit(run_one, index): index for index in range(len(calls))}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [item for item in results if item is not None]

    def _remaining_seconds(self) -> float:
        if self.deadline_at is None:
            return float("inf")
        return (self.deadline_at - datetime.now(timezone.utc)).total_seconds()

    def _bounded_tool_timeout(self, requested: int) -> int:
        remaining = self._remaining_seconds()
        if remaining == float("inf"):
            return requested
        return max(1, min(requested, int(remaining)))

    def _search_timeout(self) -> float | None:
        remaining = self._remaining_seconds()
        if remaining == float("inf"):
            return None
        if remaining < 1.0:
            raise ToolError("explore deadline reached", error_type="timeout", reason="not enough time for grep")
        return min(float(getattr(self.search, "timeout_seconds", remaining)), remaining)

    def _search_deadline(self) -> float | None:
        remaining = self._remaining_seconds()
        if remaining == float("inf"):
            return None
        if remaining < 1.0:
            raise ToolError("explore deadline reached", error_type="timeout", reason="not enough time for glob")
        return time.monotonic() + remaining


def _tool_error_observation(action: str, exc: ToolError) -> dict[str, object]:
    observation: dict[str, object] = {
        "observation": "error",
        "action": action,
        "error": str(sanitize_for_log(str(exc))),
    }
    observation.update(exc.to_record())
    return {key: value for key, value in observation.items() if value not in (None, {}, "")}
