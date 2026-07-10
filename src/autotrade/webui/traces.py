"""Agent-trace access: byte-offset pagination and live SSE tailing.

agent_trace.jsonl is appended one fully-flushed JSON line per event (see
AgentTraceWriter), so tailing by byte offset and splitting on newlines is
lossless. During a session the live file sits under the experiment work root;
after collection the canonical copy lives in experiments/<id>/artifacts/<run>/.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from autotrade.pipelines.hitl_state import HITL_DIR_NAME, STATUS_NAME, read_status, status_pid_alive

DEFAULT_PAGE_BYTES = 512 * 1024
STREAM_POLL_SECONDS = 1.0
STREAM_IDLE_HEARTBEAT_EVERY = 15


def resolve_trace_path(experiment_dir: Path, run_id: str | None) -> Path | None:
    """Prefer the collected canonical trace; fall back to the live one."""
    experiment_dir = Path(experiment_dir)
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


def trace_stats(path: Path) -> dict[str, object]:
    """Aggregate per-event-type counts and headline totals from one trace file.

    Powers the live operations dashboard: LLM/tool call counts, cumulative
    backtest wall-time (which is credited back to the reasoning deadline), and
    whether a backtest is currently in flight (started without a terminal event).
    """
    path = Path(path)
    counts: dict[str, int] = {}
    backtest_wall = 0.0
    llm_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    last_ts: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
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
            if kind == "backtest":
                try:
                    backtest_wall += float(event.get("replay_wall_seconds") or 0.0)
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
    return {
        "counts": counts,
        "total_events": sum(counts.values()),
        "backtest_wall_seconds": round(backtest_wall, 1),
        "llm_total_tokens": llm_tokens,
        "llm_prompt_tokens": prompt_tokens,
        "llm_completion_tokens": completion_tokens,
        "in_backtest": counts.get("backtest_start", 0) > counts.get("backtest", 0),
        "last_event_ts": last_ts,
        "trace_bytes": path.stat().st_size,
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
            for event in page["events"]:
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            position = int(page["next_offset"])
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
