#!/usr/bin/env python3
"""Cron-safe TuShare update runner for AutoTrade."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import NO_MUTATION_RETRY_EXIT_CODE, normalize_date_key, read_many


DEFAULT_CONFIG = Path("configs/tushare_update_schedule.json")
RUNTIME_ROOT = Path(".runtime/tushare")
STATE_PATH = RUNTIME_ROOT / "cron_state.json"
RUN_LOG_ROOT = Path("logs/tushare/cron")
RUN_LOG_RETENTION_DAYS = 14
DEFAULT_LOCK_WAIT_SECONDS = 900
# Job operations that mutate the raw/PIT lake and therefore publish a new
# generation on success; audit-only jobs must not churn snapshot cache keys.
MUTATING_OPERATIONS = {"update", "download_tier", "download_event_flow", "pit_event_pipeline", "auction_capture"}
GENERATION_SCHEMA_VERSION = 2
GENERATION_COMMITTED = "committed"
GENERATION_IN_PROGRESS = {"updating", "dirty"}


@dataclass
class RunContext:
    config: dict
    repo_root: Path
    python: str
    job_name: str
    job: dict
    start_date: str
    end_date: str
    timezone_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a locked scheduled TuShare update job.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to tushare_update_schedule.json.")
    parser.add_argument("--job", required=True, help="Job name from the schedule config.")
    parser.add_argument("--start-date", help="Override update lower bound. Defaults to TUSHARE_UPDATE_START_DATE or config default_start_date.")
    parser.add_argument("--end-date", help="Override update end date. Defaults to job offset from current Asia/Shanghai date.")
    parser.add_argument("--dry-run", action="store_true", help="Print the computed command without running it.")
    parser.add_argument("--force-run", action="store_true", help="Run even if this job/date already has an ok state.")
    return parser.parse_args()


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"schedule config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_sse_open_on_or_before(repo_root: Path, raw_dir: str, target_date: str) -> str:
    trade_cal_dir = repo_root / raw_dir / "trade_cal" / "exchange=SSE"
    files = sorted(trade_cal_dir.glob("year=*.parquet"))
    if not files:
        raise RuntimeError(f"SSE trade_cal partitions are missing under {trade_cal_dir}; run reference download first")
    calendar = read_many(files, columns=["cal_date", "is_open"])
    if calendar.empty:
        raise RuntimeError(f"SSE trade_cal is empty under {trade_cal_dir}; refresh reference trade_cal first")
    calendar["cal_date"] = calendar["cal_date"].map(normalize_date_key)
    open_dates = sorted(
        calendar.loc[
            (calendar["is_open"].astype(str) == "1")
            & (calendar["cal_date"] != "")
            & (calendar["cal_date"] <= target_date),
            "cal_date",
        ].tolist()
    )
    if not open_dates:
        raise RuntimeError(f"no SSE open date found on or before {target_date}")
    return str(open_dates[-1])


def resolve_job_end_date(job: dict, repo_root: Path, raw_dir: str, target_date: str) -> str:
    mode = str(job.get("end_date_mode", "calendar_date"))
    if mode == "calendar_date":
        return target_date
    if mode == "sse_open_on_or_before":
        return resolve_sse_open_on_or_before(repo_root, raw_dir, target_date)
    raise ValueError(f"unsupported end_date_mode: {mode}")


def is_sse_open_date(repo_root: Path, raw_dir: str, target_date: str) -> bool:
    """Whether ``target_date`` itself is open, without silently rolling backward."""
    files = sorted((repo_root / raw_dir / "trade_cal" / "exchange=SSE").glob("year=*.parquet"))
    if not files:
        raise RuntimeError(f"SSE trade_cal partitions are missing under {repo_root / raw_dir}")
    calendar = read_many(files, columns=["cal_date", "is_open"])
    dates = calendar["cal_date"].map(normalize_date_key)
    exact = dates == target_date
    if not exact.any():
        raise RuntimeError(f"SSE trade_cal does not cover target date {target_date}")
    return bool((exact & (calendar["is_open"].astype(str) == "1")).any())


def resolve_event_flow_audit_end_date(ctx: RunContext, raw_dir: str) -> str:
    mode = ctx.job.get("event_flow_end_date_mode")
    if mode:
        return resolve_job_end_date({"end_date_mode": mode}, ctx.repo_root, raw_dir, ctx.end_date)
    event_flow_end_date = ctx.end_date
    event_extra_offset = int(ctx.job.get("event_flow_end_extra_offset_days", 0))
    if event_extra_offset:
        event_flow_end_date = (
            datetime.strptime(ctx.end_date, "%Y%m%d").date() - timedelta(days=event_extra_offset)
        ).strftime("%Y%m%d")
    return event_flow_end_date


def build_context(args: argparse.Namespace) -> RunContext:
    config = load_config(Path(args.config))
    jobs = config.get("jobs", {})
    if args.job not in jobs:
        raise KeyError(f"unknown job {args.job!r}; available={sorted(jobs)}")
    timezone_name = config.get("timezone", "Asia/Shanghai")
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    job = jobs[args.job]
    repo_root = Path(config.get("repo_root", ".")).resolve()
    python = config.get("python") or sys.executable
    raw_dir = config.get("default_raw_dir", "data/raw")
    offset_days = int(job.get("end_date_offset_days", 0))
    target_date = args.end_date or (now.date() - timedelta(days=offset_days)).strftime("%Y%m%d")
    end_date = resolve_job_end_date(job, repo_root, raw_dir, target_date)
    env_start_date = os.environ.get("TUSHARE_UPDATE_START_DATE")
    if args.start_date or env_start_date:
        start_date = args.start_date or env_start_date or config["default_start_date"]
    elif job.get("operation") == "download_event_flow":
        start_date = end_date
    elif "start_date_lookback_days" in job:
        end_day = datetime.strptime(end_date, "%Y%m%d").date()
        start_date = (end_day - timedelta(days=int(job["start_date_lookback_days"]))).strftime("%Y%m%d")
    else:
        start_date = config["default_start_date"]
    return RunContext(config, repo_root, python, args.job, job, start_date, end_date, timezone_name)


def build_audit_full_commands(ctx: RunContext) -> list[list[str]]:
    raw_dir = ctx.config.get("default_raw_dir", "data/raw")
    event_flow_end_date = resolve_event_flow_audit_end_date(ctx, raw_dir)
    text_end_date = (
        datetime.strptime(ctx.end_date, "%Y%m%d").date()
        - timedelta(days=int(ctx.job.get("text_end_extra_offset_days", 0)))
    ).strftime("%Y%m%d")
    return [
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "base",
            "--start-date",
            ctx.start_date,
            "--bak-start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--fundamental-start-date",
            ctx.start_date,
            "--fundamental-end-date",
            ctx.end_date,
            "--include-limit-list",
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "macro",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "intraday-by-date",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--expected-codes-source",
            "minute",
            "--min-rows-per-day",
            "1",
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "event-flow",
            "--start-date",
            ctx.start_date,
            "--end-date",
            event_flow_end_date,
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "board-trading",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "text",
            "--start-date",
            ctx.start_date,
            "--end-date",
            text_end_date,
            "--raw-dir",
            raw_dir,
        ],
    ]


def build_job_commands(ctx: RunContext) -> list[list[str]]:
    raw_dir = ctx.config.get("default_raw_dir", "data/raw")
    operation = ctx.job.get("operation", "update")
    if operation == "update":
        command = [
            ctx.python,
            "scripts/data/tushare_download.py",
            "update",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        command.extend(ctx.config.get("default_update_args", []))
        command.extend(ctx.job.get("extra_args", []))
        return [command]
    if operation == "download_event_flow":
        command = [
            ctx.python,
            "scripts/data/tushare_download.py",
            "download",
            "--tier",
            "event_flow",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        command.extend(ctx.config.get("default_update_args", []))
        command.extend(ctx.job.get("extra_args", []))
        return [command]
    if operation == "download_tier":
        tier = ctx.job.get("tier")
        if not tier:
            raise ValueError("download_tier job requires a tier")
        command = [
            ctx.python,
            "scripts/data/tushare_download.py",
            "download",
            "--tier",
            tier,
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        command.extend(ctx.config.get("default_update_args", []))
        command.extend(ctx.job.get("extra_args", []))
        return [command]
    if operation == "auction_capture":
        command = [
            ctx.python,
            "scripts/data/tushare_download.py",
            "capture-open-auction",
            "--trade-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        command.extend(ctx.config.get("default_update_args", []))
        command.extend(ctx.job.get("extra_args", []))
        return [command]
    if operation == "audit_event_flow":
        command = [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "event-flow",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        command.extend(ctx.job.get("extra_args", []))
        return [command]
    if operation == "pit_event_pipeline":
        raw_dir = ctx.config.get("default_raw_dir", "data/raw")
        pit_root = ctx.config.get("default_pit_root", "data/pit")
        fundamental_root = ctx.job.get("fundamental_events_root", f"{pit_root}/fundamental_events")
        event_start_date = pit_event_start_date(ctx, fundamental_root)
        commands = [
            [
                ctx.python,
                "scripts/data/build_pit_events.py",
                "build-fundamental-events",
                "--raw-dir",
                raw_dir,
                "--output-root",
                fundamental_root,
                "--start-date",
                event_start_date,
                "--end-date",
                ctx.end_date,
            ],
            [
                ctx.python,
                "scripts/data/build_pit_events.py",
                "audit-fundamental-events",
                "--events-root",
                fundamental_root,
                "--start-date",
                event_start_date,
                "--end-date",
                ctx.end_date,
                "--output",
                ctx.job.get("fundamental_events_status", "results/data_quality/fundamental_events_status.json"),
                "--require-partitions",
            ],
        ]
        commands[0].extend(ctx.job.get("fundamental_events_extra_args", []))
        commands[1].extend(ctx.job.get("fundamental_events_audit_extra_args", []))
        return commands
    if operation == "revision_sentinel":
        revision_config = ctx.config.get("revision_monitor", {})
        command = [
            ctx.python,
            "scripts/data/tushare_audit.py",
            "revision-sentinel",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ]
        if revision_config.get("ledger_path"):
            command.extend(["--revision-ledger", str(revision_config["ledger_path"])])
        if revision_config.get("summary_path"):
            command.extend(["--output", str(revision_config["summary_path"])])
        command.extend(ctx.config.get("default_update_args", []))
        extra_args = list(ctx.job.get("extra_args", []))
        if "--sample-size" not in extra_args and revision_config.get("sentinel_sample_size") is not None:
            extra_args.extend(["--sample-size", str(revision_config["sentinel_sample_size"])])
        if "--datasets" not in extra_args and revision_config.get("sentinel_datasets"):
            extra_args.append("--datasets")
            extra_args.extend(str(dataset) for dataset in revision_config["sentinel_datasets"])
        command.extend(extra_args)
        return [command]
    if operation == "audit_full":
        commands = build_audit_full_commands(ctx)
        commands.extend(ctx.job.get("extra_commands", []))
        return commands
    raise ValueError(f"unsupported cron operation: {operation}")


def pit_event_start_date(ctx: RunContext, fundamental_root: str) -> str:
    if not ctx.job.get("initialize_from_default_start_date_when_missing", True):
        return month_start(ctx.start_date)
    root = ctx.repo_root / fundamental_root
    has_partitions = root.exists() and any(root.glob("*/available_month=*.parquet"))
    if has_partitions:
        return month_start(ctx.start_date)
    return str(ctx.config.get("default_start_date", ctx.start_date))


def month_start(date_text: str) -> str:
    normalized = str(date_text)
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError(f"expected YYYYMMDD date, got {date_text!r}")
    return f"{normalized[:6]}01"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def job_config_hash(ctx: RunContext) -> str:
    """Hash only schedule inputs that can affect this job.

    Hashing the complete schedule made an edit to any unrelated job invalidate
    every successful job/date record. Commands already have a separate hash;
    this identity retains the shared settings read by this operation plus the
    selected job's own configuration.
    """
    operation = str(ctx.job.get("operation", "update"))
    shared_keys = {
        "schema_version",
        "timezone",
        "repo_root",
        "python",
        "default_start_date",
        "default_raw_dir",
        "default_lock_wait_seconds",
    }
    if operation in {
        "update",
        "download_tier",
        "download_event_flow",
        "auction_capture",
        "revision_sentinel",
    }:
        shared_keys.add("default_update_args")
    if operation == "pit_event_pipeline":
        shared_keys.add("default_pit_root")
    if operation == "revision_sentinel":
        shared_keys.add("revision_monitor")
    return stable_hash({
        "shared": {key: ctx.config.get(key) for key in sorted(shared_keys)},
        "job": ctx.job,
    })


@dataclass
class FileLock:
    """A held kernel flock; the file itself is never deleted (unlinking a
    flock-backed lock file races a concurrent opener onto a dead inode)."""

    path: Path
    fd: int

    def release(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def acquire_lock(lock_name: str, wait_seconds: int) -> FileLock:
    """Exclusive kernel flock: released automatically when the holder exits,
    so a crashed/killed run can never leave a permanently stale lock (the old
    PID-file scheme broke on PID reuse). pid/started_at are diagnostics only."""
    lock = RUNTIME_ROOT / "locks" / f"{lock_name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.close(fd)
                raise RuntimeError(
                    f"lock is held after waiting {wait_seconds}s, another run may be active: {lock}"
                ) from None
            time.sleep(min(15.0, remaining))
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()}\nstarted_at={utc_now()}\n".encode("utf-8"))
    return FileLock(lock, fd)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_raw_generation_file(raw_dir: Path) -> dict:
    path = raw_dir / ".raw_generation.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid raw generation record: {path}: {exc}") from exc


def _write_raw_generation_file(raw_dir: Path, payload: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / ".raw_generation.json"
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _restore_raw_generation_file(raw_dir: Path, payload: dict) -> None:
    """Undo an updating fence after a child proves it performed no mutation."""
    if payload:
        _write_raw_generation_file(raw_dir, payload)
    else:
        (raw_dir / ".raw_generation.json").unlink(missing_ok=True)


def write_raw_generation(raw_dir: Path, *, transaction: dict | None = None) -> dict:
    """Publish a committed generation after one fully-successful mutating job."""
    payload = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "state": GENERATION_COMMITTED,
        "generation_id": uuid.uuid4().hex,
        "completed_at": utc_now(),
    }
    if transaction:
        payload["transaction"] = dict(transaction)
    _write_raw_generation_file(raw_dir, payload)
    print(f"raw generation {payload['generation_id']} published at {raw_dir / '.raw_generation.json'}")
    return payload


def begin_raw_generation_update(raw_dir: Path, transaction: dict) -> dict:
    """Mark the lake unavailable before the first child process can mutate it.

    A dirty/updating transaction may only be recovered by an exact rerun of the
    same job window and command. An unrelated successful job must never bless a
    partially-updated lake left by an earlier failure.
    """
    previous = _read_raw_generation_file(raw_dir)
    previous_state = str(previous.get("state", GENERATION_COMMITTED))
    previous_transaction = previous.get("transaction") or {}
    identity_keys = ("job", "start_date", "end_date", "command_hash")
    if previous_state in GENERATION_IN_PROGRESS and any(
        str(previous_transaction.get(key, "")) != str(transaction.get(key, ""))
        for key in identity_keys
    ):
        failed_job = str(previous_transaction.get("job", "unknown"))
        raise RuntimeError(
            "raw lake has an unfinished mutation; rerun the original job before any other mutation: "
            f"job={failed_job} state={previous_state}"
        )
    recovering = previous_state in GENERATION_IN_PROGRESS
    transaction = dict(transaction)
    transaction["transaction_id"] = str(
        previous_transaction.get("transaction_id") if recovering else uuid.uuid4().hex
    )
    transaction["started_at"] = str(
        previous_transaction.get("started_at") if recovering else utc_now()
    )
    payload = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "state": "updating",
        "generation_id": str(previous.get("generation_id", "")),
        "completed_at": str(previous.get("completed_at", "")),
        "updated_at": utc_now(),
        "transaction": transaction,
    }
    _write_raw_generation_file(raw_dir, payload)
    return transaction


def mark_raw_generation_dirty(raw_dir: Path, transaction: dict, *, error: str) -> None:
    previous = _read_raw_generation_file(raw_dir)
    payload = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "state": "dirty",
        "generation_id": str(previous.get("generation_id", "")),
        "completed_at": str(previous.get("completed_at", "")),
        "updated_at": utc_now(),
        "transaction": dict(transaction),
        "error": str(error)[:1000],
    }
    _write_raw_generation_file(raw_dir, payload)


def prune_run_logs(state: dict, *, now: float | None = None) -> None:
    """Bound dedicated cron-run logs while retaining every state-linked run."""
    if not RUN_LOG_ROOT.exists():
        return
    referenced = {
        Path(str(item.get("log_path"))).resolve()
        for item in state.values()
        if isinstance(item, dict) and item.get("log_path")
    }
    cutoff = (time.time() if now is None else now) - RUN_LOG_RETENTION_DAYS * 86400
    for path in RUN_LOG_ROOT.glob("tushare_cron_*.log"):
        try:
            if path.is_file() and path.resolve() not in referenced and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            # Retention is best-effort and must never block a market-data job.
            continue


def run_probe(command: list[str], log_handle) -> None:
    log_handle.write(f"\n$ {' '.join(command)}\n")
    log_handle.flush()
    subprocess.run(command, cwd=Path.cwd(), stdout=log_handle, stderr=subprocess.STDOUT, check=False)


def run_update(ctx: RunContext, commands: list[list[str]], log_path: Path, *, lock_fd: int | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    returncodes: list[int] = []
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"started_at={started}\njob={ctx.job_name}\nstart_date={ctx.start_date}\nend_date={ctx.end_date}\ntimezone={ctx.timezone_name}\n")
        run_probe(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            log,
        )
        run_probe(["free", "-h"], log)
        for index, command in enumerate(commands, start=1):
            log.write(f"\n$ {' '.join(command)}\n")
            log.flush()
            env = os.environ.copy()
            src_path = str(ctx.repo_root / "src")
            env["PYTHONPATH"] = src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
            env["PYTHONUNBUFFERED"] = "1"
            if lock_fd is not None:
                # The child inherits the held updater flock via pass_fds; the
                # marker stops download.py from re-acquiring it (deadlock-safe).
                env["TUSHARE_UPDATE_LOCK_HELD"] = "1"
            process = subprocess.run(
                command,
                cwd=ctx.repo_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
                pass_fds=(lock_fd,) if lock_fd is not None else (),
            )
            returncodes.append(process.returncode)
            log.write(f"\ncommand_index={index}\nreturncode={process.returncode}\n")
            if process.returncode != 0 and ctx.job.get("fail_fast", True):
                log.write(f"fail_fast=true; skipped_remaining_commands={len(commands) - index}\n")
                break
        run_probe(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            log,
        )
        run_probe(["free", "-h"], log)
        log.write(f"returncodes={returncodes}\n")
        log.write(f"finished_at={utc_now()}\n")
    if returncodes and all(code in {0, NO_MUTATION_RETRY_EXIT_CODE} for code in returncodes):
        return NO_MUTATION_RETRY_EXIT_CODE if NO_MUTATION_RETRY_EXIT_CODE in returncodes else 0
    return 1


def should_skip_completed(ctx: RunContext, args: argparse.Namespace, job_state: dict, payload: dict) -> bool:
    return bool(
        ctx.job.get("skip_if_already_ok", True)
        and not args.force_run
        and job_state.get("start_date") == ctx.start_date
        and job_state.get("end_date") == ctx.end_date
        and job_state.get("status") == "ok"
        and job_state.get("command_hash") == payload["command_hash"]
        and job_state.get("config_hash") == payload["config_hash"]
    )


def main() -> int:
    args = parse_args()
    ctx = build_context(args)
    commands = build_job_commands(ctx)
    timestamp = datetime.now(ZoneInfo(ctx.timezone_name)).strftime("%Y%m%d_%H%M%S")
    log_path = RUN_LOG_ROOT / f"tushare_cron_{ctx.job_name}_{ctx.end_date}_{timestamp}.log"
    payload = {
        "job": ctx.job_name,
        "start_date": ctx.start_date,
        "end_date": ctx.end_date,
        "commands": commands,
        "command_hash": stable_hash(commands),
        "config_hash": job_config_hash(ctx),
        "log_path": str(log_path),
        "timezone": ctx.timezone_name,
    }
    mutates_lake = ctx.job.get("operation", "update") in MUTATING_OPERATIONS
    raw_dir = ctx.repo_root / ctx.config.get("default_raw_dir", "data/raw")
    if args.dry_run:
        print(json.dumps({"status": "dry_run", **payload}, ensure_ascii=False, indent=2))
        return 0

    os.chdir(ctx.repo_root)
    state = read_state()
    prune_run_logs(state)
    if ctx.job.get("only_if_sse_open_date") and not is_sse_open_date(
        ctx.repo_root,
        ctx.config.get("default_raw_dir", "data/raw"),
        ctx.end_date,
    ):
        state[ctx.job_name] = {
            "status": "ok",
            "returncode": 0,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
            "command_hash": payload["command_hash"],
            "config_hash": payload["config_hash"],
            "log_path": str(log_path),
            "skipped_non_trading_day": True,
            "updated_at": utc_now(),
        }
        write_state(state)
        message = json.dumps({**state[ctx.job_name], "job": ctx.job_name}, ensure_ascii=False)
        print(message)
        return 0
    job_state = state.get(ctx.job_name, {})
    generation = _read_raw_generation_file(raw_dir) if mutates_lake else {}
    generation_committed = str(generation.get("state", GENERATION_COMMITTED)) == GENERATION_COMMITTED
    if should_skip_completed(ctx, args, job_state, payload) and generation_committed:
        message = json.dumps({"status": "skipped_already_ok", **payload}, ensure_ascii=False)
        print(message)
        return 0

    try:
        lock = acquire_lock(
            "tushare_update",
            int(ctx.job.get("lock_wait_seconds", ctx.config.get("default_lock_wait_seconds", DEFAULT_LOCK_WAIT_SECONDS))),
        )
    except RuntimeError as exc:
        state[ctx.job_name] = {
            "status": "error",
            "returncode": 1,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
            "command_hash": payload["command_hash"],
            "config_hash": payload["config_hash"],
            "log_path": str(log_path),
            "error": str(exc),
            "updated_at": utc_now(),
        }
        write_state(state)
        message = json.dumps({**state[ctx.job_name], "job": ctx.job_name}, ensure_ascii=False)
        print(message)
        return 1

    returncode = 1
    try:
        state = read_state()
        job_state = state.get(ctx.job_name, {})
        generation = _read_raw_generation_file(raw_dir) if mutates_lake else {}
        generation_committed = str(generation.get("state", GENERATION_COMMITTED)) == GENERATION_COMMITTED
        if should_skip_completed(ctx, args, job_state, payload) and generation_committed:
            message = json.dumps({"status": "skipped_already_ok_after_lock", **payload}, ensure_ascii=False)
            print(message)
            return 0
        transaction = None
        generation_before = generation
        if mutates_lake:
            transaction = begin_raw_generation_update(
                raw_dir,
                {
                    "job": ctx.job_name,
                    "start_date": ctx.start_date,
                    "end_date": ctx.end_date,
                    "command_hash": payload["command_hash"],
                    "config_hash": payload["config_hash"],
                },
            )
        try:
            returncode = run_update(ctx, commands, log_path, lock_fd=lock.fd)
        except Exception as exc:
            if transaction is not None:
                mark_raw_generation_dirty(raw_dir, transaction, error=f"runner_exception: {exc}")
            raise
        # Exit 75 asserts "no lake mutation happened"; only operations whose
        # download paths enforce that contract may restore the prior generation
        # (auction polling, and event_flow runs with --zero-rows-not-ready).
        no_mutation_retry = bool(
            returncode == NO_MUTATION_RETRY_EXIT_CODE
            and ctx.job.get("operation") in {"auction_capture", "download_event_flow"}
            and len(commands) == 1
        )
        if transaction is not None:
            if no_mutation_retry:
                _restore_raw_generation_file(raw_dir, generation_before)
            elif returncode == 0:
                write_raw_generation(raw_dir, transaction=transaction)
            else:
                mark_raw_generation_dirty(raw_dir, transaction, error=f"job_returncode={returncode}")
        status = "ok" if returncode == 0 else (
            "not_ready" if no_mutation_retry else "error"
        )
        state[ctx.job_name] = {
            "status": status,
            "returncode": returncode,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
            "command_hash": payload["command_hash"],
            "config_hash": payload["config_hash"],
            "log_path": str(log_path),
            "updated_at": utc_now(),
        }
        write_state(state)
        message = json.dumps({**state[ctx.job_name], "job": ctx.job_name}, ensure_ascii=False)
        print(message)
        return returncode
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
