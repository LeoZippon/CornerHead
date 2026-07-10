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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import normalize_date_key, read_many


DEFAULT_CONFIG = Path("configs/tushare_update_schedule.json")
RUNTIME_ROOT = Path(".runtime/tushare")
STATE_PATH = RUNTIME_ROOT / "cron_state.json"
DISPATCH_LOG_PATH = Path("logs/tushare_cron_dispatch.log")
DEFAULT_LOCK_WAIT_SECONDS = 900


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
            "base",
            "--include-text",
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
            "--text-start-date",
            ctx.start_date,
            "--text-end-date",
            ctx.end_date,
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


def append_dispatch(message: str) -> None:
    DISPATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCH_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def run_probe(command: list[str], log_handle) -> None:
    log_handle.write(f"\n$ {' '.join(command)}\n")
    log_handle.flush()
    subprocess.run(command, cwd=Path.cwd(), stdout=log_handle, stderr=subprocess.STDOUT, check=False)


def run_update(ctx: RunContext, commands: list[list[str]], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    returncodes: list[int] = []
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"started_at={started}\njob={ctx.job_name}\nstart_date={ctx.start_date}\nend_date={ctx.end_date}\ntimezone={ctx.timezone_name}\n")
        run_probe(["nvidia-smi"], log)
        run_probe(["free", "-h"], log)
        for index, command in enumerate(commands, start=1):
            log.write(f"\n$ {' '.join(command)}\n")
            log.flush()
            env = os.environ.copy()
            src_path = str(ctx.repo_root / "src")
            env["PYTHONPATH"] = src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
            env["PYTHONUNBUFFERED"] = "1"
            process = subprocess.run(command, cwd=ctx.repo_root, stdout=log, stderr=subprocess.STDOUT, check=False, env=env)
            returncodes.append(process.returncode)
            log.write(f"\ncommand_index={index}\nreturncode={process.returncode}\n")
            if process.returncode != 0 and ctx.job.get("fail_fast", True):
                log.write(f"fail_fast=true; skipped_remaining_commands={len(commands) - index}\n")
                break
        run_probe(["nvidia-smi"], log)
        run_probe(["free", "-h"], log)
        log.write(f"returncodes={returncodes}\n")
        log.write(f"finished_at={utc_now()}\n")
    return 1 if any(code != 0 for code in returncodes) else 0


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
    log_path = Path("logs") / f"tushare_cron_{ctx.job_name}_{ctx.end_date}_{timestamp}.log"
    payload = {
        "job": ctx.job_name,
        "start_date": ctx.start_date,
        "end_date": ctx.end_date,
        "commands": commands,
        "command_hash": stable_hash(commands),
        "config_hash": stable_hash(ctx.config),
        "log_path": str(log_path),
        "timezone": ctx.timezone_name,
    }
    if args.dry_run:
        print(json.dumps({"status": "dry_run", **payload}, ensure_ascii=False, indent=2))
        return 0

    os.chdir(ctx.repo_root)
    state = read_state()
    job_state = state.get(ctx.job_name, {})
    if should_skip_completed(ctx, args, job_state, payload):
        message = json.dumps({"status": "skipped_already_ok", **payload}, ensure_ascii=False)
        append_dispatch(f"{utc_now()} {message}")
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
        append_dispatch(f"{utc_now()} {message}")
        print(message)
        return 1

    returncode = 1
    try:
        state = read_state()
        job_state = state.get(ctx.job_name, {})
        if should_skip_completed(ctx, args, job_state, payload):
            message = json.dumps({"status": "skipped_already_ok_after_lock", **payload}, ensure_ascii=False)
            append_dispatch(f"{utc_now()} {message}")
            print(message)
            return 0
        returncode = run_update(ctx, commands, log_path)
        status = "ok" if returncode == 0 else "error"
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
        append_dispatch(f"{utc_now()} {message}")
        print(message)
        return returncode
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
