"""Host-side NL service for formal replays (``ctx.nl()`` over the JSONL RPC).

The replay engine pumps sandbox NL requests to :class:`StrategyNLService`,
which composes the NL Sub Agent engine, the PIT text retriever, and the
company-context store, enforcing the per-backtest call budget and the calling
decision's wall-clock deadline. The RPC file helpers own the host↔sandbox
channel permissions (host-only response writes).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from autotrade.environment.main_ctx_engine import BacktestError
from autotrade.environment.nl.context import CompanyContextStore
from autotrade.environment.nl.engine import NLSubAgentConfig, NLSubAgentEngine
from autotrade.environment.nl.retrieval import TextRetriever
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
    ) -> None:
        self.proxy = proxy
        self.snapshot_dir = snapshot_dir
        self.log_dir = log_dir
        self.failure_policy = failure_policy
        self.per_call_timeout_seconds = per_call_timeout_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.nl_wall_seconds = 0.0  # cumulative LLM-service wall, reported as a replay phase
        # Set per tick by the replay engine; rolls ctx.nl() text on the same nodes as
        # the Timeview. None (Timeview off / no replay) keeps the frozen PIT corpus.
        self.current_when = None
        # Absolute monotonic deadline of the decision currently being served
        # (set by MainPolicyRunner). NL provider calls are clamped to it.
        self.deadline_at: float | None = None
        # Per-ts_code company context is constant for the frozen snapshot, so load the
        # universe/fundamentals parquet once and memoize rather than re-reading both on
        # every nl() call.
        self.company_context_store = CompanyContextStore(snapshot_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retriever = TextRetriever(
            snapshot_dir / "text_index.parquet",
            snapshot_dir / "text_library",
            replay_index_path=(replay_dir / "text_index.parquet") if replay_dir is not None else None,
            replay_library_dir=(replay_dir / "text_library") if replay_dir is not None else None,
        )

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
        # Bind the retriever to the requesting tick's sim clock so announcements/news
        # become visible to ctx.nl() only once their refresh node has completed.
        self.retriever.as_of = self.current_when
        if self.max_calls is not None and self.calls > self.max_calls:
            # Hard backstop on API spend; strategy code sees an audited error and
            # degrades (the prompt asks it to keep NL frequency low to begin with).
            result = _error_result(
                ts_code, state="budget_exhausted", error=f"nl call budget exhausted (max {self.max_calls})"
            )
            self._write_result(request, result)
            return result
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
        engine = NLSubAgentEngine(
            self.proxy,
            self.retriever,
            company_contexts={ts_code: self.company_context_store.context(ts_code)} if ts_code else {},
        )
        config = NLSubAgentConfig(
            per_call_timeout_seconds=self.per_call_timeout_seconds,
            failure_policy=self.failure_policy,
            deadline_at=self.deadline_at,
        )
        _nl_t0 = time.monotonic()
        result = engine.run(ts_code=ts_code, prompt=prompt, request_kwargs=kwargs, config=config)
        self.nl_wall_seconds += time.monotonic() - _nl_t0
        record = result.to_record()
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
        if result.state in {"failed", "timeout"} and self.failure_policy == "fail":
            raise BacktestError(f"nl() failed for {ts_code}: {result.error}")
        return record

    def _write_result(
        self,
        request: dict[str, object],
        result: dict[str, object],
    ) -> None:
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
