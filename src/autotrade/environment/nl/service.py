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
import re
import time
from pathlib import Path
from typing import Callable

from autotrade.environment.main_ctx_engine import BacktestError
from autotrade.environment.nl.context import CompanyContextStore
from autotrade.environment.nl.engine import NLSubAgentConfig, NLSubAgentEngine, company_terms
from autotrade.environment.nl.retrieval import TextRetriever, validate_pattern
from autotrade.environment.runtime import new_id, sanitize_for_log


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
        self.nl_wall_seconds = 0.0  # cumulative LLM-service wall, reported as a replay phase
        self.outcome_counts: dict[str, int] = {}
        self.withhold_response = bool(withhold_response)
        self.activity_callback = activity_callback
        # Set per tick by the replay engine; rolls ctx.nl() text on the same nodes as
        # the Timeview. None (Timeview off / no replay) keeps the frozen PIT corpus.
        self.current_when = None
        # Absolute monotonic deadline of the decision currently being served
        # (set by MainPolicyRunner). NL provider calls are clamped to it.
        self.deadline_at: float | None = None
        self._analysis_cache: dict[
            tuple[str, str, str], tuple[str, tuple[str, ...], dict[str, object]]
        ] = {}
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
        if self.proxy is None:
            if self.failure_policy == "return_error_with_audit":
                result = _error_result(ts_code, state="failed_with_policy", error="nl proxy is not configured")
                self._write_result(request, result)
                return result
            raise BacktestError("strategy called nl() but no LLM proxy is configured")
        if self.deadline_at is not None and self.deadline_at - time.monotonic() <= 1.0:
            # The decision's wall cap is already spent: fail the request without
            # starting a provider round (the runner will kill the decision at
            # its next deadline check regardless).
            result = _error_result(
                ts_code, state="timeout", error="decision wall-clock deadline exhausted before the NL task"
            )
            self._write_result(request, result)
            if self.failure_policy == "fail":
                raise BacktestError(f"nl() deadline exhausted for {ts_code or 'general'}")
            return result
        _nl_t0 = time.monotonic()
        activity_cache_status = "bypass" if not ts_code else "miss"
        self._emit_activity("running", call_index=self.calls, elapsed_seconds=0.0, cache_status="checking")
        try:
            context = self.company_context_store.context(ts_code) if ts_code else {}
            cache_key: tuple[str, str, str] | None = None
            revision: str | None = None
            event_patterns: tuple[str, ...] = ()
            if ts_code:
                candidate_terms = company_terms(context, ts_code)
                cache_key = _analysis_cache_key(ts_code, prompt, kwargs, context)
                cached = self._analysis_cache.get(cache_key)
                if cached is not None:
                    cached_revision, event_patterns, cached_record = cached
                    revision, event_patterns = _candidate_revision(
                        self.retriever, ts_code, candidate_terms, event_patterns
                    )
                    if cached_revision == revision:
                        self.cache_hits += 1
                        activity_cache_status = "hit"
                        record = copy.deepcopy(cached_record)
                        source_task_id = str(record.get("task_id") or "")
                        record["task_id"] = new_id("nlreuse")
                        record["cache"] = {
                            "status": "hit",
                            "evidence_revision": revision,
                            "event_filter_count": len(event_patterns),
                            "source_task_id": source_task_id,
                        }
                        self._write_result(request, record)
                        return record
                self.cache_misses += 1

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
            )
            result = engine.run(ts_code=ts_code, prompt=prompt, request_kwargs=kwargs, config=config)
            record = result.to_record()
            if cache_key is not None and result.state == "completed":
                event_patterns = _event_patterns(result.tool_calls, candidate_terms)
                revision, event_patterns = _candidate_revision(
                    self.retriever, ts_code, candidate_terms, event_patterns
                )
            record["cache"] = {
                "status": "miss" if cache_key is not None else "bypass",
                "evidence_revision": revision,
                "event_filter_count": len(event_patterns),
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
                self._analysis_cache[cache_key] = (
                    revision,
                    event_patterns,
                    copy.deepcopy(record),
                )
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


def _candidate_revision(
    retriever: TextRetriever,
    ts_code: str,
    candidate_terms: list[str],
    patterns: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Use event-scoped validity when possible, otherwise conservatively hash all evidence."""
    try:
        revision = retriever.candidate_revision(
            ts_code, company_terms=candidate_terms, patterns=patterns
        )
        return revision, patterns
    except ValueError:
        revision = retriever.candidate_revision(ts_code, company_terms=candidate_terms)
        return revision, ()


def _event_patterns(
    tool_calls: list[dict[str, object]], candidate_terms: list[str]
) -> tuple[str, ...]:
    """Keep the sub-agent's substantive queries as its next-call event filter."""
    entity_terms = {term.strip() for term in candidate_terms if term.strip()}
    entity_terms.update(term.split(".", 1)[0] for term in tuple(entity_terms) if "." in term)
    patterns: list[str] = []
    seen: set[str] = set()
    for call in tool_calls:
        if call.get("name") != "text_retrieve" or call.get("status") == "error":
            continue
        arguments = call.get("arguments")
        pattern = str(arguments.get("pattern") or "").strip() if isinstance(arguments, dict) else ""
        pattern = _candidate_event_pattern(pattern, entity_terms)
        if pattern is None:
            # An entity-qualified expression outside the small safe prefix
            # grammar cannot be rewritten without interpreting arbitrary RE2.
            # Hash the whole candidate corpus instead of risking stale reuse.
            return ()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        patterns.append(pattern)
    return tuple(patterns)


def _candidate_event_pattern(pattern: str, entity_terms: set[str]) -> str | None:
    """Drop discovery-only queries and safely broaden a leading entity qualifier.

    Arbitrary regex surgery can change semantics or create invalid RE2. Only the
    common ``entity.*event`` / ``entity event`` prefix is removed; any remaining
    entity reference makes the cache fall back to the full candidate revision.
    """
    spellings = {
        spelling.casefold()
        for term in entity_terms
        for spelling in (term, re.escape(term))
        if spelling
    }
    branches = [part.strip().casefold() for part in pattern.split("|") if part.strip()]
    if branches and all(branch in spellings for branch in branches):
        return ""

    normalized = pattern
    for spelling in sorted(spellings, key=len, reverse=True):
        prefix = re.compile(
            rf"^\s*{re.escape(spelling)}(?:\s*\.\*\s*|\s+)",
            flags=re.IGNORECASE,
        )
        normalized, removed = prefix.subn("", normalized, count=1)
        if removed:
            break
    folded = normalized.casefold()
    if any(spelling in folded for spelling in spellings):
        return None
    normalized = "|".join(part.strip() for part in normalized.split("|") if part.strip())
    if not re.search(r"[\w\u4e00-\u9fff]", normalized):
        return ""
    try:
        return validate_pattern(normalized)
    except ValueError:
        return None


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
    "failed_with_policy": "本运行未配置 NL 代理：nl() 在此环境不可用。请走无文本的退化路径（纯数值信号），不要重试。",
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
