"""Agent session runner: the main conversation loop for one Fold or meta-learning run.

docs/agent_design.md + docs/environment_design.md 3.4: one Agent session per
Fold (one conversation_id), Steps share the session, only the four documented
entrypoints are callable, the fold deadline is the master constraint, and a
fixed wrap-up prompt fires at most once inside the finalize window. Every main
conversation provider call is logged in full as an ``llm_call`` event in
agent_trace.jsonl (docs/environment_design.md 6.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from hl_trader.environment.llm.proxy import LLMProxy, LLMProxyError
from hl_trader.environment.nl.extraction import ExtractionError, extract_json_object
from hl_trader.environment.runtime import sanitize_for_log, utc_now_iso
from hl_trader.environment.tools import (
    BacktestTool,
    FinishFoldTool,
    ModificationCheckTool,
    SandboxShellTool,
    ToolContext,
    ToolError,
)
from hl_trader.environment.web_search import WebSearchError, WebSearchProvider, WebSearchTool

from .prompts import WRAP_UP_PROMPT, build_meta_learning_prompt, build_system_prompt

SESSION_MODES = ("fold", "meta_learning")


@dataclass(frozen=True)
class AgentSessionConfig:
    fold_deadline_at: datetime
    finalize_before_deadline_seconds: int = 300
    per_call_timeout_seconds: float = 300.0
    max_llm_calls: int = 200
    max_steps: int = 10
    max_history_messages: int = 60
    # Code-writing turns need room; reasoning tokens also count against this.
    max_response_tokens: int = 8000


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
        mode: str = "fold",
        web_search_provider: WebSearchProvider | None = None,
    ) -> None:
        if mode not in SESSION_MODES:
            raise ValueError(f"unsupported session mode: {mode}")
        self.ctx = ctx
        self.proxy = proxy
        self.config = config
        self.mode = mode
        if mode == "meta_learning":
            self.system_prompt = build_meta_learning_prompt(fold_info)
        else:
            prompt_kwargs: dict[str, object] = {
                "fold_info": fold_info,
                "acceptance_rules": acceptance_rules,
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
        self.modification_check = ModificationCheckTool(ctx)
        self.backtest = BacktestTool(ctx)
        self.finish_fold = FinishFoldTool(ctx)
        self.web_search = WebSearchTool(web_search_provider) if web_search_provider is not None else None

    def run(self) -> dict[str, object]:
        messages: list[dict[str, str]] = [
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

            try:
                payload = self._next_action(messages, remaining)
                llm_calls += 1
            except (LLMProxyError, ExtractionError) as exc:
                llm_calls += 1
                messages.append({"role": "user", "content": f'{{"observation": "invalid_action", "error": {json.dumps(str(exc))}}}'})
                continue

            messages.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False, default=str)})
            action = str(payload.get("action", ""))
            observation = self._dispatch(action, payload)
            messages.append({"role": "user", "content": json.dumps(sanitize_for_log(observation), ensure_ascii=False, default=str)})
            messages = self._trim(messages)

            if self.mode == "meta_learning" and action == "done":
                finish_status = "meta_learning_done"
                break
            if action == "finish_fold" and observation.get("status") == "fold_finished":
                finish_status = "fold_finished"
                break
            # Only complete validations (nl=on) are Steps; off/sample runs are
            # debugging per docs/environment_design.md 4.4 and stay free.
            if action == "backtest" and observation.get("status") == "ok" and observation.get("complete_validation"):
                if step_index >= self.config.max_steps:
                    finish_status = "step_limit_reached"
                    break
                step_index += 1
                self.ctx.current_step_id = f"step_{step_index:03d}"

        summary = {
            "finish_status": finish_status,
            "llm_calls": llm_calls,
            "steps_used": step_index,
            "wrap_up_sent": wrap_up_sent,
            "write_locked": self.ctx.write_locked,
            "ended_at": utc_now_iso(),
        }
        self.ctx.trace.emit("session_end", summary, step_id=self.ctx.current_step_id)
        return summary

    # ---- internals ----

    def _initial_user_message(self) -> str:
        if self.mode == "meta_learning":
            return (
                "Begin meta-learning. Read development_history and meta_learning_memory if present, "
                "run the required web_search categories, write /mnt/agent/workspace/taste.md, "
                "optionally simplify factor/nl_prior within constraints, then call done."
            )
        return "Begin the fold. Inspect the data, improve the strategy artifact, validate, and finish."

    def _next_action(self, messages: list[dict[str, str]], remaining: float) -> dict[str, object]:
        timeout = min(self.config.per_call_timeout_seconds, max(remaining, 1.0))
        detail: dict[str, object] = {
            "purpose": "agent_main_conversation",
            "provider": self.proxy.provider,
            "model": self.proxy.model,
            "messages": sanitize_for_log(messages),
            "started_at": utc_now_iso(),
        }
        try:
            response = self.proxy.complete(
                messages, json_mode=True, timeout_seconds=timeout, max_tokens=self.config.max_response_tokens
            )
        except LLMProxyError as exc:
            detail.update(status="timeout" if exc.timeout else "error", error=str(exc), completed_at=utc_now_iso())
            self.ctx.trace.emit("llm_call", detail, step_id=self.ctx.current_step_id)
            raise
        extracted = extract_json_object(response.content)
        detail.update(
            status="ok",
            completed_at=utc_now_iso(),
            raw_content=response.content,
            reasoning_content=response.reasoning_content or extracted.stripped_think,
            parsed=sanitize_for_log(extracted.payload),
            usage=response.usage,
        )
        self.ctx.trace.emit("llm_call", detail, step_id=self.ctx.current_step_id)
        return extracted.payload

    def _dispatch(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        # No preemptive kill of in-flight work: per-call timeouts bound every
        # provider/tool call, and no NEW work is dispatched once the deadline
        # has passed, so the worst overrun is one bounded call.
        if self._remaining_seconds() <= 0 and action not in {"note", "done"}:
            return {"observation": "error", "action": action, "error": "fold deadline reached; no new calls"}
        try:
            if action == "shell":
                result = self.shell.run(str(payload.get("command", "")))
                return {"observation": "shell", **result.to_record()}
            if action == "modification_check":
                return {"observation": "modification_check", **self.modification_check.run()}
            if action == "backtest":
                if self.mode == "meta_learning":
                    return {"observation": "error", "action": action, "error": "backtests are not allowed in this session"}
                nl_mode = str(payload.get("nl_mode", "on"))
                return {"observation": "backtest", **self.backtest.run(mode="valid", nl_mode=nl_mode)}
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
                    str(payload.get("query", "")),
                    max_results=int(payload.get("max_results", 5)),
                    category=str(payload.get("category", "general")),
                )
                self.ctx.trace.emit("web_search", result, step_id=self.ctx.current_step_id)
                return {"observation": "web_search", **result}
            if action == "note":
                return {"observation": "note_recorded"}
            if action == "done" and self.mode == "meta_learning":
                return {"observation": "meta_learning_done"}
            return {"observation": "error", "error": f"unknown action: {action!r}"}
        except ToolError as exc:
            return {"observation": "error", "action": action, "error": str(exc)}
        except WebSearchError as exc:
            return {"observation": "error", "action": action, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - an agent action must never kill the fold
            self.ctx.trace.emit(
                "error",
                {"action": action, "error": f"{type(exc).__name__}: {exc}"},
                step_id=self.ctx.current_step_id,
            )
            return {"observation": "error", "action": action, "error": f"internal tool failure: {type(exc).__name__}: {exc}"}

    def _remaining_seconds(self) -> float:
        return (self.config.fold_deadline_at - datetime.now(timezone.utc)).total_seconds()

    def _trim(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if len(messages) <= self.config.max_history_messages:
            return messages
        # Keep the system prompt and the most recent turns inside one session.
        keep = self.config.max_history_messages - 1
        return [messages[0], *messages[-keep:]]
