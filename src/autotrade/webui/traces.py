"""Agent-trace access: byte-offset pagination and live SSE tailing.

agent_trace.jsonl is appended one fully-flushed JSON line per event (see
AgentTraceWriter), so tailing by byte offset and splitting on newlines is
lossless. During a session the live file sits under the experiment work root;
after collection the canonical copy lives in experiments/<id>/artifacts/<run>/.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import AsyncIterator

from autotrade.pipelines.hitl_state import HITL_DIR_NAME, STATUS_NAME, read_status, status_pid_alive

DEFAULT_PAGE_BYTES = 512 * 1024
DEFAULT_TAIL_EVENTS = 200
MAX_TAIL_BYTES = 4 * 1024 * 1024
STREAM_POLL_SECONDS = 1.0
STREAM_IDLE_HEARTBEAT_EVERY = 15


def resolve_trace_path(experiment_dir: Path, run_id: str | None) -> Path | None:
    """Prefer the collected canonical trace; fall back to the live one."""
    experiment_dir = Path(experiment_dir)
    if run_id and ("/" in run_id or "\\" in run_id or run_id.startswith(".")):
        # Same guard as the style endpoint: run_id must stay one path segment
        # so it cannot traverse outside this experiment's artifacts/.
        return None
    if run_id:
        collected = experiment_dir / "artifacts" / run_id / "agent_trace.jsonl"
        if collected.exists():
            return collected
    status = read_status(experiment_dir / HITL_DIR_NAME / STATUS_NAME)
    live = status.get("trace_path")
    if live and (run_id is None or status.get("run_id") == run_id):
        live_path = Path(str(live))
        if live_path.exists():
            return live_path
    return None


def read_trace_page(path: Path, *, offset: int = 0, max_bytes: int = DEFAULT_PAGE_BYTES) -> dict[str, object]:
    """Read complete JSONL events from ``offset``; a partial tail line stays unread."""
    path = Path(path)
    size = path.stat().st_size
    offset = max(0, min(int(offset), size))
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(max_bytes)
    consumed = chunk.rfind(b"\n") + 1
    if consumed <= 0:
        return {"events": [], "next_offset": offset, "eof": offset + len(chunk) >= size and not chunk}
    events: list[dict[str, object]] = []
    for line in chunk[:consumed].splitlines():
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            event = json.loads(text)
            events.append(event if isinstance(event, dict) else {"raw": text})
        except json.JSONDecodeError:
            events.append({"raw": text})
    next_offset = offset + consumed
    return {"events": events, "next_offset": next_offset, "eof": next_offset >= size}


def read_trace_tail(
    path: Path,
    *,
    max_events: int = DEFAULT_TAIL_EVENTS,
    max_bytes: int = MAX_TAIL_BYTES,
) -> dict[str, object]:
    """Read a bounded event tail and return the offset where live tailing starts."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        return {"events": [], "next_offset": 0, "eof": True, "history_truncated": False}

    read_size = min(size, max(1, int(max_bytes)))
    start = size - read_size
    with path.open("rb") as handle:
        handle.seek(start)
        blob = handle.read(read_size)

    # The first bytes may be the suffix of an oversized JSONL event. Never
    # parse or expose that fragment; the live stream resumes after the last
    # complete line returned below.
    if start:
        first_newline = blob.find(b"\n")
        if first_newline < 0:
            return {"events": [], "next_offset": size, "eof": True, "history_truncated": True}
        start += first_newline + 1
        blob = blob[first_newline + 1 :]

    complete_bytes = blob.rfind(b"\n") + 1
    complete = blob[:complete_bytes]
    lines = complete.splitlines(keepends=True)
    selected = lines[-max(1, int(max_events)) :]
    selected_start = start + sum(len(line) for line in lines[: len(lines) - len(selected)])
    events: list[dict[str, object]] = []
    for raw in selected:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            event = json.loads(text)
            events.append(event if isinstance(event, dict) else {"raw": text})
        except json.JSONDecodeError:
            events.append({"raw": text})
    next_offset = start + complete_bytes
    return {
        "events": events,
        "next_offset": next_offset,
        "eof": next_offset >= size,
        "history_truncated": selected_start > 0,
    }


# Incremental aggregates per trace path: the live panel polls every 5 s while
# the (append-only) trace grows into the megabytes; re-reading the whole file
# each poll is O(size). Keyed by resolved path; value carries the byte offset
# of the last COMPLETE line already folded into the aggregates.
_STATS_CACHE: dict[str, dict[str, object]] = {}
_STATS_CACHE_MAX = 32
_STATS_CACHE_LOCK = threading.Lock()


def trace_stats(path: Path) -> dict[str, object]:
    """Return one atomic incremental aggregate for a trace path."""
    with _STATS_CACHE_LOCK:
        return _trace_stats_locked(path)


def _trace_stats_locked(path: Path) -> dict[str, object]:
    """Aggregate per-event-type counts and headline totals from one trace file.

    Powers the live operations dashboard: LLM/tool call counts, cumulative
    backtest wall-time (which is credited back to the reasoning deadline), and
    whether a backtest is currently in flight (started without a terminal event).
    Incremental: only bytes appended since the previous call are scanned.
    """
    path = Path(path)
    size = path.stat().st_size
    key = str(path)
    cached = _STATS_CACHE.get(key)
    if cached is None or size < int(cached["offset"]):  # rewritten/truncated: rescan
        cached = {"offset": 0, "counts": {}, "backtest_wall": 0.0, "llm_tokens": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "last_ts": None,
                  "active_backtest_started_at": None, "active_backtest_progress": None,
                  "pending_backtest_credit": 0.0}
    counts: dict[str, int] = dict(cached["counts"])
    backtest_wall = float(cached["backtest_wall"])
    llm_tokens = int(cached["llm_tokens"])
    prompt_tokens = int(cached["prompt_tokens"])
    completion_tokens = int(cached["completion_tokens"])
    last_ts: str | None = cached["last_ts"]
    active_backtest_started_at: str | None = cached.get("active_backtest_started_at")
    active_backtest_progress = cached.get("active_backtest_progress")
    pending_backtest_credit = float(cached.get("pending_backtest_credit") or 0.0)
    offset = int(cached["offset"])
    with path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read(size - offset)
    tail = blob.rfind(b"\n") + 1  # a partial trailing line stays unread
    for raw in blob[:tail].splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or "event")
        counts[kind] = counts.get(kind, 0) + 1
        last_ts = str(event.get("ts") or last_ts or "")
        if kind == "backtest_start":
            pending_backtest_credit = 0.0
            active_backtest_started_at = str(event.get("ts") or event.get("started_at") or "") or None
            active_backtest_progress = {
                "status": "initializing",
                "mode": event.get("mode"),
                "result_name": event.get("result_name"),
                "day_index": 0,
                "total_days": event.get("total_trade_days"),
                "percent": 0.0,
                "trade_date": None,
                "elapsed_seconds": 0.0,
                "orders_so_far": 0,
                "activity": None,
                "activity_status": None,
                "activity_started_at": None,
            }
        elif kind == "backtest_progress":
            previous = active_backtest_progress if isinstance(active_backtest_progress, dict) else {}
            active_backtest_progress = {
                **previous,
                "status": "running",
                **{
                    field: event.get(field)
                    for field in (
                        "mode", "result_name", "day_index", "total_days", "percent",
                        "trade_date", "elapsed_seconds", "orders_so_far",
                    )
                    if field in event
                },
            }
        elif kind == "backtest_activity":
            previous = active_backtest_progress if isinstance(active_backtest_progress, dict) else {}
            running = event.get("activity_status") == "running"
            active_backtest_progress = {
                **previous,
                "activity": event.get("activity"),
                "activity_status": event.get("activity_status"),
                "activity_started_at": str(event.get("ts") or "") if running else None,
                "activity_elapsed_seconds": event.get("activity_elapsed_seconds"),
                "nl_call_index": event.get("nl_call_index"),
            }
        elif kind == "backtest":
            try:
                pending_backtest_credit = float(event.get("replay_wall_seconds") or 0.0)
                backtest_wall += pending_backtest_credit  # legacy fallback until exact exclusion arrives
            except (TypeError, ValueError):
                pending_backtest_credit = 0.0
            active_backtest_started_at = None
            active_backtest_progress = None
        elif kind == "budget_exclusion":
            try:
                seconds = float(event.get("seconds") or 0.0)
                if event.get("reason") == "backtest" and pending_backtest_credit:
                    backtest_wall -= pending_backtest_credit
                    pending_backtest_credit = 0.0
                backtest_wall += seconds
            except (TypeError, ValueError):
                pass
        elif kind == "step_gate":
            # Step-gate holds are deadline-credited like backtest wall-time;
            # counting them keeps the console countdown truthful during holds.
            try:
                backtest_wall += float(event.get("waited_seconds") or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind == "ask_user":
            # Researcher reply waits are excluded from the same active-session
            # budget; count the completed hold exactly like a Step gate.
            try:
                backtest_wall += float(event.get("waited_seconds") or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind == "llm_call":
            usage = event.get("usage")
            if isinstance(usage, dict):
                try:
                    llm_tokens += int(usage.get("total_tokens") or 0)
                    prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    completion_tokens += int(usage.get("completion_tokens") or 0)
                except (TypeError, ValueError):
                    pass
    if len(_STATS_CACHE) >= _STATS_CACHE_MAX and key not in _STATS_CACHE:
        _STATS_CACHE.pop(next(iter(_STATS_CACHE)))
    _STATS_CACHE[key] = {
        "offset": offset + tail,
        "counts": dict(counts),
        "backtest_wall": backtest_wall,
        "llm_tokens": llm_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "last_ts": last_ts,
        "active_backtest_started_at": active_backtest_started_at,
        "active_backtest_progress": active_backtest_progress,
        "pending_backtest_credit": pending_backtest_credit,
    }
    return {
        "counts": counts,
        "total_events": sum(counts.values()),
        "backtest_wall_seconds": round(backtest_wall, 1),
        "llm_total_tokens": llm_tokens,
        "llm_prompt_tokens": prompt_tokens,
        "llm_completion_tokens": completion_tokens,
        "in_backtest": active_backtest_started_at is not None,
        "active_backtest_started_at": active_backtest_started_at,
        "backtest_progress": active_backtest_progress,
        "last_event_ts": last_ts,
        "trace_bytes": size,
    }


async def stream_trace(experiment_dir: Path, run_id: str | None, *, offset: int = 0) -> AsyncIterator[str]:
    """SSE generator: replay from ``offset``, then live-tail until the run ends.

    Emits ``event: trace`` per JSONL line, ``event: waiting`` while the trace
    file does not exist yet, and a final ``event: eof`` when the trace can no
    longer grow (worker gone or a newer run started).

    Async so an idle stream costs no worker thread: a sync generator would hold
    one of anyio's ~40 threadpool tokens through every poll sleep, and a few
    lingering tabs could starve every other endpoint. The file reads here are
    small local-disk operations, safe to run on the event loop.
    """
    experiment_dir = Path(experiment_dir)
    position = int(offset)
    idle_rounds = 0
    yield "retry: 5000\n\n"  # reconnect backoff hint for the browser
    while True:
        path = resolve_trace_path(experiment_dir, run_id)
        if path is None:
            status = read_status(experiment_dir / HITL_DIR_NAME / STATUS_NAME)
            if not status_pid_alive(status):
                yield "event: eof\ndata: {}\n\n"
                return
            yield 'event: waiting\ndata: {"reason": "trace not started"}\n\n'
            await asyncio.sleep(STREAM_POLL_SECONDS)
            continue
        page = read_trace_page(path, offset=position)
        if page["events"]:
            idle_rounds = 0
            position = int(page["next_offset"])
            # SSE id = byte offset AFTER this page: the browser echoes it as
            # Last-Event-ID on auto-reconnect, so a dropped connection resumes
            # near the tail instead of restreaming the whole trace from 0.
            # (Page-boundary ids may re-send at most one page — never lose.)
            events = page["events"]
            for event in events[:-1]:
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            yield f"id: {position}\ndata: {json.dumps(events[-1], ensure_ascii=False, default=str)}\n\n"
            continue
        status = read_status(experiment_dir / HITL_DIR_NAME / STATUS_NAME)
        trace_is_current = str(status.get("trace_path") or "") == str(path)
        worker_alive = status_pid_alive(status)
        if not worker_alive or (run_id is not None and not trace_is_current and status.get("run_id") not in (None, run_id)):
            yield f'event: eof\ndata: {{"offset": {position}}}\n\n'
            return
        idle_rounds += 1
        if idle_rounds % STREAM_IDLE_HEARTBEAT_EVERY == 0:
            yield ": keep-alive\n\n"
        await asyncio.sleep(STREAM_POLL_SECONDS)
