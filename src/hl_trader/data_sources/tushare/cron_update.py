#!/usr/bin/env python3
"""Cron-safe TuShare update runner for MacroQuant."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_CONFIG = Path("configs/tushare_update_schedule.json")
RUNTIME_ROOT = Path(".runtime/tushare")
STATE_PATH = RUNTIME_ROOT / "cron_state.json"
DISPATCH_LOG_PATH = Path("logs/tushare_cron_dispatch.log")


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


def build_context(args: argparse.Namespace) -> RunContext:
    config = load_config(Path(args.config))
    jobs = config.get("jobs", {})
    if args.job not in jobs:
        raise KeyError(f"unknown job {args.job!r}; available={sorted(jobs)}")
    timezone_name = config.get("timezone", "Asia/Shanghai")
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    job = jobs[args.job]
    offset_days = int(job.get("end_date_offset_days", 0))
    end_date = args.end_date or (now.date() - timedelta(days=offset_days)).strftime("%Y%m%d")
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
    repo_root = Path(config.get("repo_root", ".")).resolve()
    python = config.get("python") or sys.executable
    return RunContext(config, repo_root, python, args.job, job, start_date, end_date, timezone_name)


def build_audit_full_commands(ctx: RunContext) -> list[list[str]]:
    raw_dir = ctx.config.get("default_raw_dir", "data/raw")
    return [
        [
            ctx.python,
            "scripts/tushare/audit.py",
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
            "scripts/tushare/audit.py",
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
            "scripts/tushare/audit.py",
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
            "scripts/tushare/audit.py",
            "event-flow",
            "--start-date",
            ctx.start_date,
            "--end-date",
            ctx.end_date,
            "--raw-dir",
            raw_dir,
        ],
        [
            ctx.python,
            "scripts/tushare/audit.py",
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
            "scripts/tushare/audit.py",
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
            "scripts/tushare/download.py",
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
            "scripts/tushare/download.py",
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
            "scripts/tushare/download.py",
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
    if operation == "audit_full":
        commands = build_audit_full_commands(ctx)
        commands.extend(ctx.job.get("extra_commands", []))
        return commands
    raise ValueError(f"unsupported cron operation: {operation}")


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


def acquire_lock(lock_name: str) -> Path:
    lock = RUNTIME_ROOT / "locks" / f"{lock_name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock, flags)
    except FileExistsError as exc:
        raise RuntimeError(f"lock exists, another run may be active: {lock}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\nstarted_at={utc_now()}\n")
    return lock


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
            process = subprocess.run(command, cwd=ctx.repo_root, stdout=log, stderr=subprocess.STDOUT, check=False)
            returncodes.append(process.returncode)
            log.write(f"\ncommand_index={index}\nreturncode={process.returncode}\n")
        run_probe(["nvidia-smi"], log)
        run_probe(["free", "-h"], log)
        log.write(f"returncodes={returncodes}\n")
        log.write(f"finished_at={utc_now()}\n")
    return 1 if any(code != 0 for code in returncodes) else 0


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
        "log_path": str(log_path),
        "timezone": ctx.timezone_name,
    }
    if args.dry_run:
        print(json.dumps({"status": "dry_run", **payload}, ensure_ascii=False, indent=2))
        return 0

    os.chdir(ctx.repo_root)
    state = read_state()
    job_state = state.get(ctx.job_name, {})
    if (
        ctx.job.get("skip_if_already_ok", True)
        and not args.force_run
        and job_state.get("end_date") == ctx.end_date
        and job_state.get("status") == "ok"
    ):
        message = json.dumps({"status": "skipped_already_ok", **payload}, ensure_ascii=False)
        append_dispatch(f"{utc_now()} {message}")
        print(message)
        return 0

    try:
        lock = acquire_lock("tushare_update")
    except RuntimeError as exc:
        message = json.dumps({"status": "skipped_lock_exists", "job": ctx.job_name, "end_date": ctx.end_date, "error": str(exc)}, ensure_ascii=False)
        append_dispatch(f"{utc_now()} {message}")
        print(message)
        return 0

    returncode = 1
    try:
        returncode = run_update(ctx, commands, log_path)
        status = "ok" if returncode == 0 else "error"
        state[ctx.job_name] = {
            "status": status,
            "returncode": returncode,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
            "log_path": str(log_path),
            "updated_at": utc_now(),
        }
        write_state(state)
        message = json.dumps({**state[ctx.job_name], "job": ctx.job_name}, ensure_ascii=False)
        append_dispatch(f"{utc_now()} {message}")
        print(message)
        return returncode
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
