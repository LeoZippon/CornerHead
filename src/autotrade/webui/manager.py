"""Experiment lifecycle for the HITL console: create, spawn, control, delete.

The web server never runs pipeline code in-process. Each experiment executes
in a detached worker (scripts/experiments/run_interactive_experiment.py) whose
lifetime is independent of the server; control flows exclusively through the
single-writer JSON files under experiments/<id>/hitl/.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from autotrade.environment.runtime import utc_now_iso
from autotrade.pipelines.interactive import (
    CONTROL_NAME,
    HITL_DIR_NAME,
    PARAMS_NAME,
    STATUS_NAME,
    ControlState,
    read_control,
    read_json,
    read_status,
    resolve_options,
    status_pid_alive,
    write_control,
    write_json_atomic,
)

from .registry import ACTIVE_STATES, experiment_state, resolve_experiment_dir

MAX_RUNNING_EXPERIMENTS = 4
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_TERMINAL_RESUMABLE_STATES = ("stopped", "failed", "interrupted", "created")


class ManagerError(RuntimeError):
    """User-facing lifecycle error (mapped to HTTP 4xx by the server)."""


class ExperimentManager:
    def __init__(self, repo_root: Path, experiments_root: Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.experiments_root = Path(experiments_root or self.repo_root / "experiments").resolve()
        self.worker_script = self.repo_root / "scripts" / "experiments" / "run_interactive_experiment.py"
        self.log_dir = self.repo_root / "logs" / "webui"
        # Serializes all lifecycle mutations across the server's request threads.
        # Without it, concurrent requests race on control.json read-modify-write
        # (lost approvals/directives), on the check-then-spawn in start_worker
        # (double workers on one experiment, cap breach), and on delete-vs-resume.
        # RLock: create/control re-enter start_worker. Mutations are rare and
        # fast (JSON writes + Popen), so one process-wide lock is proportionate.
        self._mutate = threading.RLock()

    # ---- queries -----------------------------------------------------------
    def running_experiments(self) -> list[str]:
        running: list[str] = []
        if not self.experiments_root.is_dir():
            return running
        for entry in self.experiments_root.iterdir():
            if not entry.is_dir():
                continue
            state = experiment_state(entry)
            if state.get("worker_alive") and state.get("state") in ACTIVE_STATES:
                running.append(entry.name)
        return running

    # ---- creation ----------------------------------------------------------
    def create_experiment(self, params: dict[str, object]) -> dict[str, object]:
        with self._mutate:
            return self._create_experiment(params)

    def _create_experiment(self, params: dict[str, object]) -> dict[str, object]:
        experiment_id = str(params.get("experiment_id") or "").strip()
        if not _ID_PATTERN.match(experiment_id):
            raise ManagerError(
                "experiment_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,99} (letters, digits, _ and -)"
            )
        experiment_dir = self.experiments_root / experiment_id
        if experiment_dir.exists():
            raise ManagerError(f"experiment {experiment_id!r} already exists")
        merged = dict(params)
        merged["experiment_id"] = experiment_id
        merged.setdefault("experiments_root", str(self.experiments_root))
        # Per-experiment sandbox work root so the live run dir (and its
        # agent_trace.jsonl) can be discovered without cross-experiment noise.
        merged.setdefault("work_root", str(self.repo_root / ".runtime" / "sandboxes" / experiment_id))
        merged = {key: value for key, value in merged.items() if value is not None}
        options = resolve_options(merged, self.repo_root)  # fail-fast validation before any mkdir
        hitl_dir = experiment_dir / HITL_DIR_NAME
        hitl_dir.mkdir(parents=True)
        merged["_created_at"] = utc_now_iso()
        write_json_atomic(hitl_dir / PARAMS_NAME, merged)
        write_control(hitl_dir / CONTROL_NAME, ControlState(mode=str(options.initial_control_mode)))
        spawn = self.start_worker(experiment_id)
        return {"experiment_id": experiment_id, "experiment_dir": str(experiment_dir), **spawn}

    # ---- worker lifecycle ---------------------------------------------------
    def start_worker(self, experiment_id: str) -> dict[str, object]:
        with self._mutate:
            return self._start_worker(experiment_id)

    def _start_worker(self, experiment_id: str) -> dict[str, object]:
        experiment_dir = resolve_experiment_dir(self.experiments_root, experiment_id)
        state = experiment_state(experiment_dir)
        if state.get("kind") != "hitl":
            raise ManagerError(f"experiment {experiment_id!r} is legacy/read-only; it cannot be started")
        if state.get("worker_alive"):
            raise ManagerError(f"experiment {experiment_id!r} already has a live worker")
        if state.get("state") == "completed":
            raise ManagerError(f"experiment {experiment_id!r} is already completed")
        running = self.running_experiments()
        if len(running) >= MAX_RUNNING_EXPERIMENTS:
            raise ManagerError(
                f"parallel experiment cap reached ({MAX_RUNNING_EXPERIMENTS}); running: {', '.join(sorted(running))}"
            )
        # A stop request left behind by a previous run would immediately re-stop
        # the resumed worker; clear it (mode and approvals are preserved).
        control_path = experiment_dir / HITL_DIR_NAME / CONTROL_NAME
        control = read_control(control_path)
        if control.request == "stop":
            control.request = None
            write_control(control_path, control)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{experiment_id}.log"
        with log_path.open("ab") as log:
            log.write(f"\n===== spawn {utc_now_iso()} =====\n".encode("utf-8"))
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(self.worker_script),
                    "--experiment-dir",
                    str(experiment_dir),
                ],
                cwd=str(self.repo_root),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # survives server restarts
            )
        return {"spawned_pid": process.pid, "log_path": str(log_path)}

    def control(self, experiment_id: str, action: str, *, session_key: str | None = None, directive: str | None = None, mode: str | None = None) -> dict[str, object]:
        with self._mutate:
            return self._control(experiment_id, action, session_key=session_key, directive=directive, mode=mode)

    def _control(self, experiment_id: str, action: str, *, session_key: str | None = None, directive: str | None = None, mode: str | None = None) -> dict[str, object]:
        experiment_dir = resolve_experiment_dir(self.experiments_root, experiment_id)
        hitl_dir = experiment_dir / HITL_DIR_NAME
        if not hitl_dir.is_dir():
            raise ManagerError(f"experiment {experiment_id!r} is legacy/read-only")
        control_path = hitl_dir / CONTROL_NAME
        control = read_control(control_path)
        if action == "pause":
            control.request = "pause"
        elif action == "resume":
            # Clears a pause; if the worker died, relaunch it (ledger resume).
            control.request = None
            write_control(control_path, control)
            state = experiment_state(experiment_dir)
            if not state.get("worker_alive") and state.get("state") in _TERMINAL_RESUMABLE_STATES:
                return {"control": control.to_record(), **self.start_worker(experiment_id)}
            return {"control": control.to_record()}
        elif action == "stop":
            control.request = "stop"
        elif action == "set_mode":
            if mode not in ("auto", "step"):
                raise ManagerError("set_mode requires mode auto|step")
            control.mode = mode
        elif action == "approve":
            if not session_key:
                raise ManagerError("approve requires session_key")
            if directive is not None:
                control.directives[session_key] = directive
            control.approved_sessions = tuple(dict.fromkeys([*control.approved_sessions, session_key]))
        elif action == "set_directive":
            if not session_key:
                raise ManagerError("set_directive requires session_key")
            if directive:
                control.directives[session_key] = directive
            else:
                control.directives.pop(session_key, None)
        elif action == "set_prompt_override":
            if not session_key:
                raise ManagerError("set_prompt_override requires session_key")
            if directive and directive.strip():
                control.prompt_overrides[session_key] = directive
            else:
                control.prompt_overrides.pop(session_key, None)
        elif action == "rerun_fold":
            if not session_key:
                raise ManagerError("rerun_fold requires session_key")
            self._validate_rerun_target(experiment_dir, session_key)
            state = experiment_state(experiment_dir)
            if state.get("worker_alive"):
                raise ManagerError("先停止运行中的 worker（停止/强制终止）再重跑该 Fold")
            control.rerun_sessions[session_key] = uuid.uuid4().hex[:12]
            # Step-mode gating: the re-run must be re-approved (prompt edits land first).
            control.approved_sessions = tuple(k for k in control.approved_sessions if k != session_key)
            control.request = None
            write_control(control_path, control)
            return {"control": control.to_record(), **self.start_worker(experiment_id)}
        elif action == "terminate":
            status = read_status(hitl_dir / STATUS_NAME)
            if not status_pid_alive(status):
                raise ManagerError("no live worker to terminate")
            os.kill(int(status["pid"]), signal.SIGTERM)
            return {"terminated_pid": status["pid"]}
        else:
            raise ManagerError(f"unknown control action: {action!r}")
        write_control(control_path, control)
        return {"control": control.to_record()}

    def _validate_rerun_target(self, experiment_dir: Path, session_key: str) -> None:
        """Only the LATEST recorded fold may be re-run: earlier folds already
        fed their frozen artifacts into successors, so re-running them would
        break the parent chain the later records were built on."""
        from .registry import latest_fold_records, _read_ledger_records

        schedule = read_json(experiment_dir / HITL_DIR_NAME / "schedule.json")
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
        fold_keys = [str(s.get("key")) for s in sessions if s.get("kind") == "fold"]
        if session_key not in fold_keys:
            raise ManagerError(f"{session_key!r} is not a fold session")
        recorded = latest_fold_records(_read_ledger_records(experiment_dir))
        recorded_keys = [key for key in fold_keys if tuple(key.split("/", 1)) in recorded]
        if not recorded_keys:
            raise ManagerError("该实验还没有已完成的 Fold 可重跑")
        if session_key != recorded_keys[-1]:
            raise ManagerError(f"只能重跑最新完成的 Fold（{recorded_keys[-1]}）——更早的 Fold 已被后续继承")

    # ---- deletion ------------------------------------------------------------
    def delete_experiment(self, experiment_id: str) -> dict[str, object]:
        with self._mutate:
            return self._delete_experiment(experiment_id)

    def _delete_experiment(self, experiment_id: str) -> dict[str, object]:
        experiment_dir = resolve_experiment_dir(self.experiments_root, experiment_id)
        state = experiment_state(experiment_dir)
        if state.get("worker_alive"):
            raise ManagerError(
                f"experiment {experiment_id!r} has a live worker; stop or terminate it before deleting"
            )
        removed_work_root: str | None = None
        params = read_json(experiment_dir / HITL_DIR_NAME / PARAMS_NAME)
        work_root = params.get("work_root")
        if work_root:
            work_path = Path(str(work_root)).resolve()
            # Only remove a work root that is unambiguously this experiment's own
            # per-experiment sandbox dir, never a shared root.
            expected = (self.repo_root / ".runtime" / "sandboxes" / experiment_id).resolve()
            if work_path == expected and work_path.is_dir():
                shutil.rmtree(work_path, ignore_errors=True)
                removed_work_root = str(work_path)
        shutil.rmtree(experiment_dir)
        return {"deleted": experiment_id, "removed_work_root": removed_work_root}
