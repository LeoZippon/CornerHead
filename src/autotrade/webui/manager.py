"""Experiment lifecycle for the HITL console: create, spawn, control, delete.

The web server never runs pipeline code in-process. Each experiment executes
in a detached worker (scripts/experiments/run_interactive_experiment.py) whose
lifetime is independent of the server; control flows exclusively through the
single-writer JSON files under experiments/<id>/hitl/.
"""

from __future__ import annotations

import json
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
from autotrade.pipelines.hitl_state import (
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

from .params_schema import HIDDEN_KEYS
from .registry import ACTIVE_STATES, experiment_state, resolve_experiment_dir

MAX_RUNNING_EXPERIMENTS = 5
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_TERMINAL_RESUMABLE_STATES = ("stopped", "failed", "interrupted", "terminated", "created")


def _remove_sandbox_tree(path: Path) -> bool:
    """Remove a per-experiment sandbox dir, escalating through docker when
    plain rmtree leaves residue: under rootless docker the container agent's
    files map to a host subuid, so directories it created cannot be removed
    by the host user directly. A root-in-userns container maps those subuids
    and can delete them. Returns True when the tree is gone."""
    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return True
    try:
        from autotrade.environment.sandbox import DEFAULT_IMAGE

        subprocess.run(
            ["docker", "run", "--rm", "--user", "0", "--network=none",
             "-v", f"{path}:/purge", DEFAULT_IMAGE,
             "sh", "-c", "rm -rf /purge/* /purge/.[!.]* /purge/..?*"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _reclaim_sandbox_containers(experiment_id: str) -> list[str]:
    """Force-remove sandbox containers labelled for this experiment.

    A SIGKILLed worker skips its finally-block docker.stop(); the labels are
    set at container start (DockerSandbox). Best-effort: no docker on PATH or
    an empty listing simply reclaims nothing."""
    try:
        listing = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=mq.experiment={experiment_id}"],
            capture_output=True, text=True, timeout=30,
        )
        containers = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], capture_output=True, text=True, timeout=60)
        return containers
    except (OSError, subprocess.SubprocessError):
        return []


# Once test results are revealed the researcher's knowledge can steer any
# further learning, so everything that starts or shapes future sessions is
# blocked. pause/stop/terminate/delete/set_mode stay available.
_SEALED_BLOCKED_ACTIONS = frozenset({
    "approve", "resume", "restart", "set_directive", "set_prompt_override",
    "set_step_gate", "approve_step", "reply_question", "set_parent_override",
    "skip_to_heldout", "cancel_skip_to_heldout", "rollback_fold", "rerun_fold",
})


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
            if state.get("state") == "launching" or (
                state.get("worker_alive") and state.get("state") in ACTIVE_STATES
            ):
                running.append(entry.name)
        return running

    # ---- creation ----------------------------------------------------------
    def create_experiment(self, params: dict[str, object]) -> dict[str, object]:
        with self._mutate:
            return self._create_experiment(params)

    def _create_experiment(self, params: dict[str, object]) -> dict[str, object]:
        # UI hiding is not a permission boundary: hidden keys grant host-side
        # capabilities (local_dev host executor, source roots, credential env
        # names, proxy binaries). All console creation goes through here, so
        # reject them outright — operators set them in a worker-side params.json.
        hidden = sorted(set(params) & set(HIDDEN_KEYS))
        if hidden:
            raise ManagerError(
                f"operator-only parameters are not accepted by the console API: {', '.join(hidden)}"
            )
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
        # Force (never setdefault): callers must not redirect where experiments
        # or sandbox work trees land — the manager owns both roots.
        merged["experiments_root"] = str(self.experiments_root)
        # Per-experiment sandbox work root so the live run dir (and its
        # agent_trace.jsonl) can be discovered without cross-experiment noise.
        merged["work_root"] = str(self.repo_root / ".runtime" / "sandboxes" / experiment_id)
        merged = {key: value for key, value in merged.items() if value is not None}
        options = resolve_options(merged, self.repo_root)  # fail-fast validation before any mkdir
        try:
            # Full-config construction: range/overlap/id invariants fail HERE as
            # HTTP 400 instead of minutes later inside the spawned worker.
            from autotrade.pipelines.interactive import build_config_from_options

            build_config_from_options(options, repo_root=self.repo_root)
        except ValueError as exc:
            raise ManagerError(str(exc)) from exc
        if not bool(options.local_dev):
            from autotrade.environment.gpu import GpuUnavailableError, select_gpus

            try:
                select_gpus(int(options.gpu_count), require_name="L20")
            except GpuUnavailableError as exc:
                raise ManagerError(f"当前 GPU 无法满足实验默认分配：{exc}") from exc
        hitl_dir = experiment_dir / HITL_DIR_NAME
        hitl_dir.mkdir(parents=True)
        inherit_from = str(merged.get("inherit_from") or "").strip()
        if inherit_from:
            try:
                merged["_inherited_artifact"] = self._import_inherited_artifact(experiment_dir, inherit_from)
            except Exception:
                shutil.rmtree(experiment_dir, ignore_errors=True)  # leave no half-created experiment
                raise
        merged["_created_at"] = utc_now_iso()
        write_json_atomic(hitl_dir / PARAMS_NAME, merged)
        write_control(hitl_dir / CONTROL_NAME, ControlState(mode=str(options.initial_control_mode)))
        spawn = self.start_worker(experiment_id)
        return {"experiment_id": experiment_id, "experiment_dir": str(experiment_dir), **spawn}

    def _import_inherited_artifact(self, experiment_dir: Path, source_id: str) -> dict[str, object]:
        """Copy the source experiment's LATEST frozen fold output (+models) into
        the new experiment and hash-verify the copy, so the new experiment is
        self-contained even if the source is later deleted."""
        from autotrade.environment.artifacts import artifact_hash, model_artifact_hash

        from .registry import read_ledger_records, latest_fold_records

        source_dir = resolve_experiment_dir(self.experiments_root, source_id)
        folds = list(latest_fold_records(read_ledger_records(source_dir)).values())
        folds.sort(key=lambda r: (str(r.get("epoch_id")), str(r.get("test_period") or r.get("fold_id"))))
        if not folds:
            raise ManagerError(f"源实验 {source_id!r} 没有已完成的 Fold，无法继承其 Agent Output")
        record = folds[-1]
        src = Path(str(record.get("frozen_strategy_artifact_path") or ""))
        if not src.is_dir():
            raise ManagerError(f"源实验 {source_id!r} 的冻结产物目录缺失：{src}")
        artifact_id = f"strategy_inherited_{source_id}"
        dest_root = experiment_dir / "strategy_artifacts" / "_inherited"
        dest = dest_root / artifact_id
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        expected = str(record.get("frozen_strategy_artifact_hash"))
        copied = artifact_hash(dest)
        if copied != expected:
            raise ManagerError(f"继承产物拷贝后哈希不一致（{copied} != 账本 {expected}）")
        model_src = record.get("frozen_model_artifact_path")
        model_dest: Path | None = None
        if model_src and Path(str(model_src)).is_dir():
            model_dest = dest_root / f"{artifact_id}.models"
            shutil.copytree(Path(str(model_src)), model_dest)
        expected_model = str(record.get("frozen_model_artifact_hash"))
        copied_model = model_artifact_hash(model_dest if model_dest else dest / ".missing_models")
        if copied_model != expected_model:
            raise ManagerError(f"继承模型产物拷贝后哈希不一致（{copied_model} != 账本 {expected_model}）")
        return {
            "artifact_id": artifact_id,
            "path": str(dest),
            "artifact_hash": expected,
            "model_path": str(model_dest) if model_dest else None,
            "model_artifact_hash": expected_model,
            "source_experiment_id": source_id,
            "source_epoch_id": record.get("epoch_id"),
            "source_fold_id": record.get("fold_id"),
            "source_artifact_id": record.get("frozen_strategy_artifact_id"),
        }

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
        if state.get("state") == "launching":
            raise ManagerError(f"experiment {experiment_id!r} 的 worker 正在启动中，请稍候")
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
        # "launching" stub: bridges spawn -> the worker's first status write
        # (seconds of interpreter/imports during which the UI would otherwise
        # show "created"), and lets the guards above refuse a double spawn.
        # Written under _mutate strictly BEFORE the process exists, so the
        # worker remains the only concurrent status writer.
        status_path = experiment_dir / HITL_DIR_NAME / STATUS_NAME
        old_status = read_status(status_path)
        progress = {key: old_status[key] for key in ("total_sessions", "completed_sessions") if key in old_status}
        write_json_atomic(status_path, {**progress, "state": "launching", "launched_at": utc_now_iso()})
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
        if control.test_revealed and action in _SEALED_BLOCKED_ACTIONS:
            raise ManagerError("测试结果已揭示，实验已封存：不能再进行影响后续学习的控制操作")
        if action == "reveal_test_results":
            # One-way: showing OOS results to the researcher makes every later
            # directive/rerun/rollback informed by them — seal the experiment.
            control.test_revealed = True
        elif action == "pause":
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
            if mode not in ("auto", "manual", "step"):
                raise ManagerError("set_mode requires mode auto|manual|step")
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
        elif action == "set_gpu_count":
            if not session_key:
                raise ManagerError("set_gpu_count requires session_key")
            if directive and str(directive).strip():
                try:
                    count = int(str(directive).strip())
                except ValueError as exc:
                    raise ManagerError("GPU 数量必须是正整数") from exc
                if not 1 <= count <= 4:
                    raise ManagerError("GPU 数量须在 1..4 之间")
                control.gpu_counts[session_key] = count
            else:
                control.gpu_counts.pop(session_key, None)
        elif action == "set_step_gate":
            # "1" -> on, "0" -> explicitly off (overrides mode="step"), "" -> clear
            # back to the mode default.
            if not session_key:
                raise ManagerError("set_step_gate requires session_key")
            value = str(directive or "").strip()
            if not value:
                control.step_gate.pop(session_key, None)
            else:
                control.step_gate[session_key] = value not in ("0", "false", "off")
        elif action == "approve_step":
            # Release the session held at its current step gate; the optional
            # directive is delivered inside that step's tool observation.
            if not session_key:
                raise ManagerError("approve_step requires session_key")
            status = read_status(hitl_dir / STATUS_NAME)
            if str(status.get("state")) != "waiting_step_user" or str(status.get("session_key")) != session_key:
                raise ManagerError("该会话当前没有等待批准的 Step")
            step_index = int(status.get("awaiting_step") or 0)
            if step_index <= 0:
                raise ManagerError("status.json 缺少 awaiting_step")
            if directive and str(directive).strip():
                control.step_directives[f"{session_key}#{step_index}"] = str(directive).strip()
            control.step_go[session_key] = max(int(control.step_go.get(session_key, 0)), step_index)
        elif action == "reply_question":
            # Answer the ask_user question the worker is holding on. An empty
            # directive releases without guidance (the Agent decides).
            if not session_key:
                raise ManagerError("reply_question requires session_key")
            status = read_status(hitl_dir / STATUS_NAME)
            question = status.get("awaiting_question") if isinstance(status.get("awaiting_question"), dict) else None
            if (
                str(status.get("state")) != "waiting_user_reply"
                or str(status.get("session_key")) != session_key
                or not question
            ):
                raise ManagerError("该会话当前没有等待答复的提问")
            index = int(question.get("index") or 0)
            if index <= 0:
                raise ManagerError("status.json 缺少提问序号")
            reply_key = str(question.get("reply_key") or f"{session_key}#q{index}")
            if reply_key.split("#", 1)[0] != session_key:
                raise ManagerError("status.json 提问 reply_key 与 session_key 不一致")
            control.user_replies[reply_key] = str(directive or "").strip()
        elif action == "set_parent_override":
            if not session_key:
                raise ManagerError("set_parent_override requires session_key")
            node_id = str(directive or "").strip()
            if node_id:
                self._validate_parent_override(experiment_dir, session_key, node_id)
                control.parent_overrides[session_key] = node_id
            else:
                control.parent_overrides.pop(session_key, None)
        elif action == "skip_to_heldout":
            from .registry import read_ledger_records, latest_fold_records

            if not latest_fold_records(read_ledger_records(experiment_dir)):
                raise ManagerError("尚无已完成的 Fold，无法提前进入 Held-out")
            control.skip_to_heldout = True
            control.request = None
            write_control(control_path, control)
            state = experiment_state(experiment_dir)
            if not state.get("worker_alive") and state.get("state") in _TERMINAL_RESUMABLE_STATES:
                return {"control": control.to_record(), **self.start_worker(experiment_id)}
            return {"control": control.to_record()}
        elif action == "cancel_skip_to_heldout":
            control.skip_to_heldout = False
        elif action == "rollback_fold":
            if not session_key:
                raise ManagerError("rollback_fold requires session_key")
            state = experiment_state(experiment_dir)
            if state.get("worker_alive"):
                raise ManagerError("先停止运行中的 worker（停止/强制终止）再回滚")
            summary = self._rollback_to_fold(experiment_dir, session_key, control)
            control.request = None
            control.skip_to_heldout = False
            write_control(control_path, control)
            return {"control": control.to_record(), **summary, **self.start_worker(experiment_id)}
        elif action == "rerun_fold":
            if not session_key:
                raise ManagerError("rerun_fold requires session_key")
            self._validate_rerun_target(experiment_dir, session_key)
            state = experiment_state(experiment_dir)
            if state.get("worker_alive"):
                raise ManagerError("先停止运行中的 worker（停止/强制终止）再重跑该 Fold")
            control.rerun_sessions[session_key] = uuid.uuid4().hex[:12]
            # The re-run must be re-approved (prompt edits land first) and its
            # step gating starts afresh: stale step_go would auto-release the
            # first N step holds, stale per-step directives would replay.
            control.approved_sessions = tuple(k for k in control.approved_sessions if k != session_key)
            control.step_go.pop(session_key, None)
            for mapping in (control.step_directives, control.user_replies):
                for key in list(mapping):
                    if key.split("#", 1)[0] == session_key:
                        mapping.pop(key, None)
            control.request = None
            write_control(control_path, control)
            return {"control": control.to_record(), **self.start_worker(experiment_id)}
        elif action == "restart":
            # Terminate-and-restart in one step: SIGTERM the live worker, wait
            # for the pid to die (bounded), then resume via the ledger.
            status = read_status(hitl_dir / STATUS_NAME)
            if status_pid_alive(status):
                os.kill(int(status["pid"]), signal.SIGTERM)
                import time as _time

                deadline = _time.monotonic() + 30.0
                while _time.monotonic() < deadline and status_pid_alive(read_status(hitl_dir / STATUS_NAME)):
                    _time.sleep(0.5)
                if status_pid_alive(read_status(hitl_dir / STATUS_NAME)):
                    raise ManagerError("worker 未在 30s 内退出；请稍后重试或强制终止后手动恢复")
            return {"restarted": True, **self.start_worker(experiment_id)}
        elif action == "terminate":
            # Graceful first, then guaranteed: the worker's SIGTERM handler
            # unwinds through finally blocks, but blocking work (LLM retries,
            # derived-image docker build) can ignore it for a long time — test6
            # kept heartbeating for an hour. After a short grace, SIGKILL the
            # whole process group (worker runs with start_new_session=True).
            status = read_status(hitl_dir / STATUS_NAME)
            if not status_pid_alive(status):
                raise ManagerError("no live worker to terminate")
            pid = int(status["pid"])
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError as exc:  # exited between check and signal
                raise ManagerError("worker 已退出") from exc
            import time as _time

            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                if not status_pid_alive(read_status(hitl_dir / STATUS_NAME)):
                    return {"terminated_pid": pid, "escalated": False}
                _time.sleep(0.5)
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            reclaimed = _reclaim_sandbox_containers(experiment_id)
            # SIGKILL leaves no worker to stamp a terminal state; without this
            # the page shows a stale running state until pid-liveness kicks in
            # and the user cannot tell whether termination worked.
            status = read_status(hitl_dir / STATUS_NAME)
            status.update({"state": "terminated", "error": None, "terminated_at": utc_now_iso()})
            write_json_atomic(hitl_dir / STATUS_NAME, status)
            return {"terminated_pid": pid, "escalated": True, "reclaimed_containers": reclaimed}
        else:
            raise ManagerError(f"unknown control action: {action!r}")
        write_control(control_path, control)
        return {"control": control.to_record()}

    def _rollback_to_fold(self, experiment_dir: Path, session_key: str, control: ControlState) -> dict[str, object]:
        """Make ``session_key`` the experiment's frontier again.

        Drops every ledger record AFTER the target fold (later folds, later
        epochs' meta_learning, and ALL held-out records — they reflect the
        discarded frontier), archives the dropped records' frozen artifact
        dirs (so resume neither trips the orphan check nor collides in
        _freeze), and backs up the original ledger next to it. The target
        fold's own records — including earlier re-runs — are kept verbatim.
        """
        from .registry import read_ledger_records, latest_fold_records

        schedule = read_json(experiment_dir / HITL_DIR_NAME / "schedule.json")
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
        fold_keys = [str(s.get("key")) for s in sessions if s.get("kind") == "fold"]
        if session_key not in fold_keys:
            raise ManagerError(f"{session_key!r} is not a fold session")
        target_epoch, target_fold = session_key.split("/", 1)
        if (target_epoch, target_fold) not in latest_fold_records(read_ledger_records(experiment_dir)):
            raise ManagerError("目标 Fold 还没有账本记录，无法回滚到它")
        dropped_fold_keys = set(fold_keys[fold_keys.index(session_key) + 1 :])

        def _dropped(record: dict[str, object]) -> bool:
            kind = record.get("record_type")
            if kind == "fold":
                return f"{record.get('epoch_id')}/{record.get('fold_id')}" in dropped_fold_keys
            if kind == "meta_learning":
                return str(record.get("epoch_id")) > target_epoch
            return kind == "heldout"

        ledger_path = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
        raw_lines = ledger_path.read_text(encoding="utf-8").splitlines() if ledger_path.exists() else []
        kept_lines: list[str] = []
        dropped_records: list[dict[str, object]] = []
        for line in raw_lines:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                kept_lines.append(line)  # never silently discard unparseable audit lines
                continue
            if isinstance(record, dict) and _dropped(record):
                dropped_records.append(record)
            else:
                kept_lines.append(line)
        if not dropped_records:
            raise ManagerError("该 Fold 之后没有任何账本记录（Fold/元学习/Held-out），无需回滚")

        stamp = utc_now_iso().replace("-", "").replace(":", "")[:13]
        archive_root = experiment_dir / "strategy_artifacts" / "_archive" / f"rollback_{stamp}"
        archived: list[str] = []
        for record in dropped_records:
            candidates: list[Path] = []
            for field_name in ("frozen_strategy_artifact_path", "frozen_model_artifact_path"):
                raw = record.get(field_name)
                if raw:
                    candidates.append(Path(str(raw)))
            if record.get("record_type") == "meta_learning" and record.get("frozen_strategy_artifact_id"):
                base = experiment_dir / "strategy_artifacts" / str(record.get("epoch_id"))
                artifact_id = str(record.get("frozen_strategy_artifact_id"))
                candidates.extend([base / artifact_id, base / f"{artifact_id}.models"])
            for path in candidates:
                if not path.is_dir():
                    continue
                archive_root.mkdir(parents=True, exist_ok=True)
                dest = archive_root / path.name
                suffix = 1
                while dest.exists():
                    dest = archive_root / f"{path.name}.{suffix}"
                    suffix += 1
                shutil.move(str(path), str(dest))
                archived.append(str(path))

        pruned_nodes = self._prune_step_tree(experiment_dir, dropped_fold_keys, archive_root)

        backup = ledger_path.with_name(f"experiment_ledger.rollback_{stamp}.jsonl")
        shutil.copy2(ledger_path, backup)
        tmp = ledger_path.with_name(f".{ledger_path.name}.rollback.tmp")
        tmp.write_text("".join(f"{line}\n" for line in kept_lines), encoding="utf-8")
        os.replace(tmp, ledger_path)

        dropped_session_keys = dropped_fold_keys | {"heldout"} | {
            f"{record.get('epoch_id')}/meta_learning"
            for record in dropped_records
            if record.get("record_type") == "meta_learning"
        }
        control.approved_sessions = tuple(k for k in control.approved_sessions if k not in dropped_session_keys)
        for key in list(control.rerun_sessions):
            if key in dropped_session_keys:
                control.rerun_sessions.pop(key, None)
        for key in list(control.gpu_counts):
            if key in dropped_session_keys:
                control.gpu_counts.pop(key, None)
        for key in list(control.parent_overrides):
            if key in dropped_session_keys:
                control.parent_overrides.pop(key, None)
        # Session-scoped inputs of dropped sessions are stale by definition:
        # directives/prompt overrides describe runs that no longer exist, and
        # leftover step_go would auto-release the re-run's early step gates.
        for mapping in (control.directives, control.prompt_overrides, control.step_gate, control.step_go):
            for key in list(mapping):
                if key in dropped_session_keys:
                    mapping.pop(key, None)
        for mapping in (control.step_directives, control.user_replies):
            for key in list(mapping):
                if key.split("#", 1)[0] in dropped_session_keys:
                    mapping.pop(key, None)
        return {
            "rolled_back_to": session_key,
            "dropped_records": len(dropped_records),
            "archived_dirs": archived,
            "ledger_backup": str(backup),
            "pruned_step_nodes": pruned_nodes,
        }

    def _prune_step_tree(self, experiment_dir: Path, dropped_fold_keys: set[str], archive_root: Path) -> int:
        """Step-tree symmetry for fold rollback.

        Nodes recorded by the dropped fold sessions carry validation metrics and
        full strategy snapshots from periods that are FUTURE relative to the new
        frontier; the next fold's sandbox receives the experiment tree verbatim,
        so leaving them in place would hand the re-run Agent future-validated
        strategies. Dropped nodes (plus descendants) move into the rollback
        archive next to the frozen artifacts; tree.json is backed up there too."""
        from autotrade.environment.identity import agent_visible_ref
        from autotrade.environment.step_tree import StepTree, TREE_FILE

        steps_root = experiment_dir / "steps"
        if not (steps_root / TREE_FILE).exists():
            return 0
        dropped_pairs = set()
        for key in dropped_fold_keys:
            epoch_id, _, fold_id = key.partition("/")
            dropped_pairs.add((epoch_id, agent_visible_ref(fold_id, prefix="fold_ref")))
        tree = StepTree(steps_root)
        dropped_ids = {
            str(node["node_id"])
            for node in tree.nodes()
            if (str(node.get("epoch_id")), str(node.get("fold_id"))) in dropped_pairs
        }
        if not dropped_ids:
            return 0
        changed = True
        while changed:  # descendants of a dropped node are dropped too
            changed = False
            for node in tree.nodes():
                if node["node_id"] not in dropped_ids and node.get("parent_node_id") in dropped_ids:
                    dropped_ids.add(str(node["node_id"]))
                    changed = True
        archive_steps = archive_root / "steps"
        archive_steps.mkdir(parents=True, exist_ok=True)
        shutil.copy2(steps_root / TREE_FILE, archive_steps / TREE_FILE)
        for node_id in sorted(dropped_ids):
            node_dir = steps_root / node_id
            if node_dir.is_dir():
                shutil.move(str(node_dir), str(archive_steps / node_id))
        tree.data["nodes"] = [node for node in tree.nodes() if str(node["node_id"]) not in dropped_ids]
        if tree.data.get("current_node_id") in dropped_ids:
            # The next fold start repositions by parent hash (_install_step_tree).
            tree.data["current_node_id"] = None
        tree.save()
        return len(dropped_ids)

    def _validate_rerun_target(self, experiment_dir: Path, session_key: str) -> None:
        """Only the LATEST recorded fold may be re-run: earlier folds already
        fed their frozen artifacts into successors, so re-running them would
        break the parent chain the later records were built on."""
        from .registry import latest_fold_records, read_ledger_records

        schedule = read_json(experiment_dir / HITL_DIR_NAME / "schedule.json")
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
        fold_keys = [str(s.get("key")) for s in sessions if s.get("kind") == "fold"]
        if session_key not in fold_keys:
            raise ManagerError(f"{session_key!r} is not a fold session")
        recorded = latest_fold_records(read_ledger_records(experiment_dir))
        recorded_keys = [key for key in fold_keys if tuple(key.split("/", 1)) in recorded]
        if not recorded_keys:
            raise ManagerError("该实验还没有已完成的 Fold 可重跑")
        if session_key != recorded_keys[-1]:
            raise ManagerError(f"只能重跑最新完成的 Fold（{recorded_keys[-1]}）——更早的 Fold 已被后续继承")

    def _validate_parent_override(self, experiment_dir: Path, session_key: str, node_id: str) -> None:
        """The override target must be a fold session and the node a restorable snapshot.

        Past-only: a node recorded by a LATER fold session embodies strategies
        validated on periods after the target session's window; allowing it as
        the parent would leak future-fitted strategies backwards. The node's own
        session (rerun-from-node) and earlier sessions are allowed. Which fold
        may consume it is enforced where it matters: an already-run fold only
        picks the override up through rerun_fold (itself restricted to the
        latest fold), an unrun fold at its next start."""
        from autotrade.environment.step_tree import StepTree
        from autotrade.pipelines.hitl_state import assert_node_not_from_later_fold

        from .steps import node_export_dir, node_layout

        schedule = read_json(experiment_dir / HITL_DIR_NAME / "schedule.json")
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
        fold_keys = [str(s.get("key")) for s in sessions if s.get("kind") == "fold"]
        if session_key not in fold_keys:
            raise ManagerError(f"{session_key!r} is not a fold session")
        try:
            node_export_dir(experiment_dir, node_id)
        except ValueError as exc:
            raise ManagerError(str(exc)) from exc
        if node_layout(experiment_dir / "steps", node_id) != "split":
            raise ManagerError("旧格式节点仅支持查看与下载，无法设为父产物起点")
        node = StepTree(experiment_dir / "steps").get_node(node_id)
        try:
            assert_node_not_from_later_fold(node, session_key, fold_keys)
        except ValueError as exc:
            raise ManagerError(str(exc)) from exc

    # ---- deletion ------------------------------------------------------------
    def delete_experiment(self, experiment_id: str) -> dict[str, object]:
        with self._mutate:
            return self._delete_experiment(experiment_id)

    def _delete_experiment(self, experiment_id: str) -> dict[str, object]:
        experiment_dir = resolve_experiment_dir(self.experiments_root, experiment_id)
        state = experiment_state(experiment_dir)
        if state.get("worker_alive") or state.get("state") == "launching":
            raise ManagerError(
                f"experiment {experiment_id!r} has a live worker; stop or terminate it before deleting"
            )
        removed_work_root: str | None = None
        params = read_json(experiment_dir / HITL_DIR_NAME / PARAMS_NAME)
        work_root = params.get("work_root")
        # The per-experiment sandbox dir is derived from the experiment id, so
        # it is removed even when params.json is unreadable/legacy; an explicit
        # work_root is honored only when it IS that dir (never a shared root).
        expected = (self.repo_root / ".runtime" / "sandboxes" / experiment_id).resolve()
        work_path = Path(str(work_root)).resolve() if work_root else expected
        if work_path == expected and work_path.is_dir():
            if not _remove_sandbox_tree(work_path):
                raise ManagerError(f"sandbox 目录未能完全删除：{work_path}")
            removed_work_root = str(work_path)
        shutil.rmtree(experiment_dir)
        _reclaim_sandbox_containers(experiment_id)
        return {"deleted": experiment_id, "removed_work_root": removed_work_root}
