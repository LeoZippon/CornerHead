"""Host-side NL service for formal replays (``ctx.nl()`` over the JSONL RPC).

The replay engine pumps sandbox NL requests to :class:`StrategyNLService`,
which composes the NL Sub Agent engine, the PIT text retriever, and the
company-context store, enforcing the per-backtest call budget and the calling
decision's wall-clock deadline. The RPC file helpers own the host↔sandbox
channel permissions (host-only response writes).
"""

from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autotrade.environment.main_ctx_engine import BacktestError
from autotrade.environment.nl.context import CompanyContextStore
from autotrade.environment.nl.engine import (
    ENUM_MAX_RESULTS,
    ENUM_MAX_TOKENS,
    ENUM_SNIPPET_CHARS,
    MAX_TOOL_ROUNDS,
    NLSubAgentConfig,
    NLSubAgentEngine,
    company_terms,
)
from autotrade.environment.nl.retrieval import TextRetriever, validate_pattern
from autotrade.environment.runtime import new_id, sanitize_for_log


@dataclass(frozen=True)
class _EventFilter:
    patterns: tuple[str, ...]
    lookback_days: int


@dataclass(frozen=True)
class _ResponseFormat:
    choices: tuple[str, ...]


class StrategyNLService:
    def __init__(
        self,
        *,
        proxy,
        snapshot_dir: Path,
        log_dir: Path,
        failure_policy: str,
        per_call_timeout_seconds: float,
        max_calls: int | None = None,
        replay_dir: Path | None = None,
        withhold_response: bool = False,
        activity_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.proxy = proxy
        self.snapshot_dir = snapshot_dir
        self.log_dir = log_dir
        self.failure_policy = failure_policy
        self.per_call_timeout_seconds = per_call_timeout_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.executed_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.no_evidence_skips = 0
        self.nl_wall_seconds = 0.0  # cumulative LLM-service wall, reported as a replay phase
        self.provider_calls = 0
        self.provider_wall_seconds = 0.0
        self.provider_prompt_tokens = 0
        self.provider_completion_tokens = 0
        self.retrieval_calls = 0
        self.retrieval_wall_seconds = 0.0
        self.event_filter_calls = 0
        self.event_filter_wall_seconds = 0.0
        self.evidence_items = 0
        self.structural_provider_call_limit = 0
        self.outcome_counts: dict[str, int] = {}
        self.withhold_response = bool(withhold_response)
        self.activity_callback = activity_callback
        # Set per tick by the replay engine; rolls ctx.nl() text on the same nodes as
        # the Timeview. None (Timeview off / no replay) keeps the frozen PIT corpus.
        self.current_when = None
        # Absolute monotonic deadline of the decision currently being served
        # (set by MainPolicyRunner). NL provider calls are clamped to it.
        self.deadline_at: float | None = None
        self._analysis_cache: dict[tuple[str, str, str], tuple[str, dict[str, object]]] = {}
        # Per-ts_code company context is constant for the frozen snapshot, so load the
        # universe/fundamentals parquet once and memoize rather than re-reading both on
        # every nl() call.
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.withhold_response:
            self.company_context_store = None
            self.retriever = None
        else:
            self.company_context_store = CompanyContextStore(snapshot_dir)
            self.retriever = TextRetriever(
                snapshot_dir / "text_index.parquet",
                snapshot_dir / "text_library",
                replay_index_path=(replay_dir / "text_index.parquet") if replay_dir is not None else None,
                replay_library_dir=(replay_dir / "text_library") if replay_dir is not None else None,
            )

    def close(self) -> None:
        if self.retriever is not None:
            self.retriever.close()

    def run(
        self,
        ts_code: str,
        *,
        prompt: str,
        kwargs: dict[str, object],
        request: dict[str, object],
    ) -> dict[str, object]:
        ts_code = str(ts_code or "").strip()
        scope = "stock" if ts_code else "general"
        self.calls += 1
        self.structural_provider_call_limit = max(
            self.structural_provider_call_limit,
            _provider_call_limit(kwargs),
        )
        if self.max_calls is not None and self.calls > self.max_calls:
            # The budget also applies to Probe-withheld calls: otherwise an
            # erroneous strategy can still flood the RPC/audit files.
            result = _error_result(
                ts_code, state="budget_exhausted", error=f"nl call budget exhausted (max {self.max_calls})"
            )
            self._write_result(request, result)
            return result
        if self.withhold_response:
            # A Probe replays a known future window. Returning NL content to
            # strategy code would let it encode that content into substep names,
            # order counts, errors or timing and hand it back to the Agent. Full
            # validation remains representative; Probe exercises the explicit
            # no-NL fallback and records only this generic outcome.
            result = _error_result(
                ts_code,
                state="withheld_probe",
                error="nl content is unavailable in short future-window probes",
            )
            self._write_result(request, result)
            return result
        # Bind the retriever to the requesting tick's sim clock so announcements/news
        # become visible to ctx.nl() only once their refresh node has completed.
        assert self.retriever is not None
        assert self.company_context_store is not None
        self.retriever.as_of = self.current_when
        _nl_t0 = time.monotonic()
        activity_cache_status = "bypass" if not ts_code else "miss"
        self._emit_activity("running", call_index=self.calls, elapsed_seconds=0.0, cache_status="checking")
        try:
            try:
                event_filter = _parse_event_filter(kwargs.get("event_filter"), ts_code=ts_code)
                response_format = _parse_response_format(kwargs.get("response_format"))
            except ValueError as exc:
                return self._invalid_request(ts_code, request, str(exc))

            context = self.company_context_store.context(ts_code) if ts_code else {}
            cache_key: tuple[str, str, str] | None = None
            revision: str | None = None
            matching_evidence_count: int | None = None
            if ts_code:
                candidate_terms = company_terms(context, ts_code)
                cache_key = _analysis_cache_key(ts_code, prompt, kwargs, context)
                filter_started = time.monotonic()
                try:
                    if event_filter is not None:
                        state = self.retriever.candidate_evidence_state(
                            ts_code,
                            company_terms=candidate_terms,
                            patterns=event_filter.patterns,
                            lookback_days=event_filter.lookback_days,
                        )
                        matching_evidence_count = state.match_count
                        self.event_filter_calls += 1
                    else:
                        state = self.retriever.candidate_evidence_state(
                            ts_code,
                            company_terms=candidate_terms,
                        )
                    revision = state.revision
                except ValueError as exc:
                    return self._request_failure(ts_code, request, str(exc))
                finally:
                    if event_filter is not None:
                        self.event_filter_wall_seconds += time.monotonic() - filter_started
                if event_filter is None:
                    # Without a declared validity predicate, reuse within one
                    # simulated date and conservatively refresh on the next date.
                    revision = f"{revision}|date:{_cache_epoch(self.current_when)}"
                cached = self._analysis_cache.get(cache_key)
                if cached is not None:
                    cached_revision, cached_record = cached
                    if cached_revision == revision:
                        self.cache_hits += 1
                        activity_cache_status = "hit"
                        record = copy.deepcopy(cached_record)
                        if record.get("state") == "no_matching_evidence":
                            self.no_evidence_skips += 1
                        source_task_id = str(record.get("task_id") or "")
                        record["task_id"] = new_id("nlreuse")
                        record["cache"] = {
                            "status": "hit",
                            "evidence_revision": revision,
                            "event_filter_count": len(event_filter.patterns) if event_filter else 0,
                            "lookback_days": event_filter.lookback_days if event_filter else None,
                            "matching_evidence_count": matching_evidence_count,
                            "source_task_id": source_task_id,
                        }
                        self._write_result(request, record)
                        return record
                self.cache_misses += 1

                if event_filter is not None and matching_evidence_count == 0:
                    self.no_evidence_skips += 1
                    record = _no_evidence_result(ts_code, context=context)
                    record["cache"] = {
                        "status": "miss",
                        "evidence_revision": revision,
                        "event_filter_count": len(event_filter.patterns),
                        "lookback_days": event_filter.lookback_days,
                        "matching_evidence_count": 0,
                    }
                    self._analysis_cache[cache_key] = (revision, copy.deepcopy(record))
                    self._write_result(request, record)
                    return record

            if self.proxy is None:
                return self._request_failure(ts_code, request, "nl proxy is not configured")
            if self.deadline_at is not None and self.deadline_at - time.monotonic() <= 1.0:
                result = _error_result(
                    ts_code,
                    state="timeout",
                    error="decision wall-clock deadline exhausted before the NL task",
                )
                self._write_result(request, result)
                if self.failure_policy == "fail":
                    raise BacktestError(f"nl() deadline exhausted for {ts_code or 'general'}")
                return result

            self.executed_calls += 1
            engine = NLSubAgentEngine(
                self.proxy,
                self.retriever,
                company_contexts={ts_code: context} if ts_code else {},
            )
            config = NLSubAgentConfig(
                per_call_timeout_seconds=self.per_call_timeout_seconds,
                failure_policy=self.failure_policy,
                deadline_at=self.deadline_at,
                max_tokens=ENUM_MAX_TOKENS if response_format else 3000,
                max_tool_rounds=1 if response_format else MAX_TOOL_ROUNDS,
                response_choices=response_format.choices if response_format else (),
                max_results_per_search=ENUM_MAX_RESULTS if response_format else None,
                max_evidence_snippet_chars=ENUM_SNIPPET_CHARS if response_format else None,
                lookback_days=event_filter.lookback_days if event_filter else None,
            )
            result = engine.run(ts_code=ts_code, prompt=prompt, request_kwargs=kwargs, config=config)
            self._record_execution_stats(result.llm_calls, result.tool_calls, result.evidence)
            record = result.to_record()
            record["cache"] = {
                "status": "miss" if cache_key is not None else "bypass",
                "evidence_revision": revision,
                "event_filter_count": len(event_filter.patterns) if event_filter else 0,
                "lookback_days": event_filter.lookback_days if event_filter else None,
                "matching_evidence_count": matching_evidence_count,
            }
            if record.get("status") == "error":
                record["feedback"] = failure_feedback(str(record.get("state")), str(record.get("error") or ""))
            self._write_result(request, record)
            _append_jsonl(
                self.log_dir / "search_requests.jsonl",
                [{"ts_code": ts_code, "scope": scope, **r} for r in result.tool_calls],
            )
            _append_jsonl(
                self.log_dir / "evidence.jsonl",
                [{"ts_code": ts_code, "scope": scope, **e} for e in result.evidence],
            )
            _append_jsonl(self.log_dir / "nl_llm_calls.jsonl", result.llm_calls)
            if cache_key is not None and revision is not None and result.state == "completed":
                self._analysis_cache[cache_key] = (revision, copy.deepcopy(record))
            if result.state in {"failed", "timeout"} and self.failure_policy == "fail":
                raise BacktestError(f"nl() failed for {ts_code}: {result.error}")
            return record
        finally:
            elapsed = time.monotonic() - _nl_t0
            self.nl_wall_seconds += elapsed
            self._emit_activity(
                "finished",
                call_index=self.calls,
                elapsed_seconds=elapsed,
                cache_status=activity_cache_status,
            )

    def cost_summary(self) -> dict[str, object]:
        return {
            "logical_calls": int(self.calls),
            "executed_tasks": int(self.executed_calls),
            "cache_hits": int(self.cache_hits),
            "cache_misses": int(self.cache_misses),
            "no_evidence_skips": int(self.no_evidence_skips),
            "provider_calls": int(self.provider_calls),
            "provider_wall_seconds": round(self.provider_wall_seconds, 6),
            "provider_prompt_tokens": int(self.provider_prompt_tokens),
            "provider_completion_tokens": int(self.provider_completion_tokens),
            "retrieval_calls": int(self.retrieval_calls),
            "retrieval_wall_seconds": round(self.retrieval_wall_seconds, 6),
            "event_filter_calls": int(self.event_filter_calls),
            "event_filter_wall_seconds": round(self.event_filter_wall_seconds, 6),
            "evidence_items": int(self.evidence_items),
            "max_provider_calls_per_logical_call": int(self.structural_provider_call_limit),
        }

    def _record_execution_stats(
        self,
        llm_calls: list[dict[str, object]],
        tool_calls: list[dict[str, object]],
        evidence: list[dict[str, object]],
    ) -> None:
        self.provider_calls += len(llm_calls)
        self.provider_wall_seconds += sum(_number(call.get("duration_seconds")) for call in llm_calls)
        for call in llm_calls:
            usage = call.get("usage")
            if not isinstance(usage, dict):
                continue
            self.provider_prompt_tokens += int(
                _number(usage.get("prompt_tokens") or usage.get("input_tokens"))
            )
            self.provider_completion_tokens += int(
                _number(usage.get("completion_tokens") or usage.get("output_tokens"))
            )
        searches = [call for call in tool_calls if call.get("name") == "text_retrieve"]
        self.retrieval_calls += len(searches)
        self.retrieval_wall_seconds += sum(_number(call.get("duration_seconds")) for call in searches)
        self.evidence_items += len(evidence)

    def _invalid_request(
        self,
        ts_code: str,
        request: dict[str, object],
        error: str,
    ) -> dict[str, object]:
        result = _error_result(ts_code, state="invalid_request", error=error)
        self._write_result(request, result)
        if self.failure_policy == "fail":
            raise BacktestError(f"invalid nl() request for {ts_code or 'general'}: {error}")
        return result

    def _request_failure(
        self,
        ts_code: str,
        request: dict[str, object],
        error: str,
    ) -> dict[str, object]:
        state = "failed_with_policy" if self.failure_policy == "return_error_with_audit" else "failed"
        result = _error_result(ts_code, state=state, error=error)
        self._write_result(request, result)
        if self.failure_policy == "fail":
            raise BacktestError(f"nl() failed for {ts_code or 'general'}: {error}")
        return result

    def _emit_activity(
        self,
        status: str,
        *,
        call_index: int,
        elapsed_seconds: float,
        cache_status: str,
    ) -> None:
        if self.activity_callback is None:
            return
        try:
            self.activity_callback(
                {
                    "activity": "nl",
                    "activity_status": status,
                    "nl_call_index": int(call_index),
                    "activity_elapsed_seconds": round(float(elapsed_seconds), 3),
                    "nl_cache_status": cache_status,
                }
            )
        except Exception:
            # Progress reporting must never alter strategy/replay semantics.
            pass

    def _write_result(
        self,
        request: dict[str, object],
        result: dict[str, object],
    ) -> None:
        state = str(result.get("state") or result.get("status") or "unknown")
        self.outcome_counts[state] = self.outcome_counts.get(state, 0) + 1
        _append_jsonl(
            self.log_dir / "nl_requests.jsonl",
            {"request": request, "result": result},
        )


def _append_jsonl(path: Path, records: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = records if isinstance(records, list) else [records]
    with path.open("a", encoding="utf-8") as handle:
        for record in items:
            handle.write(json.dumps(sanitize_for_log(record), ensure_ascii=False, default=str) + "\n")


def _analysis_cache_key(
    ts_code: str,
    prompt: str,
    kwargs: dict[str, object],
    context: dict[str, object],
) -> tuple[str, str, str]:
    return (
        str(ts_code),
        str(prompt),
        json.dumps(
            {"kwargs": kwargs, "company_context": context},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )


def _parse_event_filter(raw: object, *, ts_code: str) -> _EventFilter | None:
    if raw is None:
        return None
    if not ts_code:
        raise ValueError("event_filter is supported only for stock-scoped nl() calls")
    if not isinstance(raw, dict):
        raise ValueError("event_filter must be an object with patterns and lookback_days")
    unknown = set(raw) - {"patterns", "lookback_days"}
    if unknown:
        raise ValueError(f"event_filter has unsupported fields: {', '.join(sorted(unknown))}")
    raw_patterns = raw.get("patterns")
    if not isinstance(raw_patterns, list) or not 1 <= len(raw_patterns) <= 16:
        raise ValueError("event_filter.patterns must contain 1 to 16 strings")
    patterns: list[str] = []
    seen: set[str] = set()
    for raw_pattern in raw_patterns:
        if not isinstance(raw_pattern, str):
            raise ValueError("event_filter.patterns must contain only strings")
        pattern = validate_pattern(raw_pattern.strip())
        if pattern not in seen:
            seen.add(pattern)
            patterns.append(pattern)
    if not patterns:
        raise ValueError("event_filter.patterns must contain at least one non-empty pattern")
    # The candidate scan uses one RE2 alternation. Validate the aggregate too so
    # the existing bounded-pattern contract remains true regardless of list size.
    validate_pattern("|".join(f"(?:{pattern})" for pattern in patterns))
    lookback_days = raw.get("lookback_days")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
        raise ValueError("event_filter.lookback_days must be an integer")
    if not 1 <= lookback_days <= 3660:
        raise ValueError("event_filter.lookback_days must be between 1 and 3660")
    return _EventFilter(tuple(patterns), lookback_days)


def _parse_response_format(raw: object) -> _ResponseFormat | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("response_format must be an object")
    unknown = set(raw) - {"type", "values"}
    if unknown:
        raise ValueError(f"response_format has unsupported fields: {', '.join(sorted(unknown))}")
    if raw.get("type") != "enum":
        raise ValueError("response_format.type must be 'enum'")
    raw_values = raw.get("values")
    if not isinstance(raw_values, list) or not 1 <= len(raw_values) <= 16:
        raise ValueError("response_format.values must contain 1 to 16 strings")
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ValueError("response_format.values must contain only strings")
        value = raw_value.strip()
        if not value or len(value) > 64 or any(char in value for char in "\r\n"):
            raise ValueError("response_format values must be 1 to 64 characters on one line")
        folded = value.casefold()
        if folded in seen:
            raise ValueError("response_format values must be unique ignoring case")
        seen.add(folded)
        values.append(value)
    return _ResponseFormat(tuple(values))


def _provider_call_limit(kwargs: dict[str, object]) -> int:
    try:
        return 2 if _parse_response_format(kwargs.get("response_format")) else MAX_TOOL_ROUNDS + 1
    except ValueError:
        return MAX_TOOL_ROUNDS + 1


def _cache_epoch(when: object) -> str:
    if when is None:
        return "frozen"
    date = getattr(when, "date", None)
    return str(date() if callable(date) else when)[:10]


def _number(value: object) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0.0 else 0.0


def _no_evidence_result(ts_code: str, *, context: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": new_id("nlskip"),
        "ts_code": ts_code,
        "scope": "stock",
        "status": "ok",
        "state": "no_matching_evidence",
        "content": "",
        "error": "",
        "rounds": 0,
        "tool_calls": [],
        "evidence": [],
        "company_context": dict(context),
    }


def prepare_nl_rpc_files(agent_root: Path) -> tuple[Path, Path]:
    runtime_dir = agent_root / ".runtime"
    rpc_dir = runtime_dir / "nl_rpc"
    if not runtime_dir.exists():
        raise BacktestError("agent runtime directory is missing; prepare the sandbox layout before backtest")
    runtime_dir.chmod(0o755)
    rpc_dir.mkdir(parents=True, exist_ok=True)
    rpc_dir.chmod(0o755)
    requests_host = rpc_dir / f"{new_id('nl_requests')}.jsonl"
    responses_host = rpc_dir / f"{new_id('nl_responses')}.jsonl"
    requests_host.write_text("", encoding="utf-8")
    responses_host.write_text("", encoding="utf-8")
    # The sandbox agent may append requests, but only the host may write
    # responses. Locked parent dirs prevent delete/replace attacks.
    requests_host.chmod(0o622)
    responses_host.chmod(0o644)
    rpc_dir.chmod(0o555)
    runtime_dir.chmod(0o555)
    return requests_host, responses_host


def cleanup_nl_rpc_files(requests_host: Path, responses_host: Path) -> None:
    rpc_dir = requests_host.parent
    runtime_dir = rpc_dir.parent
    if runtime_dir.exists():
        runtime_dir.chmod(0o755)
    if rpc_dir.exists():
        rpc_dir.chmod(0o755)
    requests_host.unlink(missing_ok=True)
    responses_host.unlink(missing_ok=True)
    if rpc_dir.exists():
        try:
            rpc_dir.rmdir()
        except OSError:
            rpc_dir.chmod(0o555)
    if runtime_dir.exists():
        runtime_dir.chmod(0o555)


# Failed nl() calls return EXPLANATORY feedback, not a bare error: the strategy
# (and the Agent debugging it) sees why the call failed and which degrade path
# to take, while status/state/error stay stable for programmatic branching.
_FAILURE_FEEDBACK = {
    "withheld_probe": "短窗口 Probe 不返回 NL 内容，防止未来窗口文本经策略输出反馈给 Agent。请在 Probe 中走无文本退化路径；完整 Valid 仍按正常合同执行 NL。",
    "budget_exhausted": "本次回测的 nl() 配额已用完：本条无结论。请降低 NL 调用频率（批量合并问题、缓存已得结论），改用数值信号继续本次回放。",
    "invalid_request": "nl() 的事件过滤或结构化输出参数不符合合同：本条未执行。请按 error 修正参数，不要无条件重试。",
    "failed_with_policy": "NL 服务在执行前或执行中不可用：本条无结论。请查看 error，并走无文本的退化路径（纯数值信号）。",
    "timeout": "本决策 tick 的墙钟剩余不足，NL 请求未执行或未完成：本条无结论。请减少该 tick 的计算量或降低 nl() 频率，稍后 tick 可重试。",
    "failed": "NL 服务调用失败（见 error 详情）：本条无结论。偶发失败可在后续 tick 重试一次；连续失败请走数值信号退化路径。",
}


def failure_feedback(state: str, error: str) -> str:
    base = _FAILURE_FEEDBACK.get(str(state), _FAILURE_FEEDBACK["failed"])
    return f"{base}（state={state}，error={error}）" if error else f"{base}（state={state}）"


def _error_result(ts_code: str, *, state: str, error: str) -> dict[str, object]:
    code = str(ts_code or "").strip()
    return {
        "task_id": "",
        "ts_code": code,
        "scope": "stock" if code else "general",
        "status": "error",
        "state": state,
        "content": "",
        "error": error,
        "feedback": failure_feedback(state, error),
        "rounds": 0,
        "tool_calls": [],
        "evidence": [],
        "company_context": {},
    }
