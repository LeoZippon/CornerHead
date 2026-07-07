"""Interactive (human-in-the-loop) experiment orchestration (docs/pipeline_design.md).

Drives the same ``run_meta_learning`` / ``run_fold`` / ``run_heldout`` primitives
as ``ExperimentPipeline.run()``, but with a researcher gate between sessions,
per-session directives, durable pause/stop, and ledger-based resume. Unlike
``run()`` (which fail-fasts on a populated experiment), the interactive runner
treats the append-only ledger as the source of truth: completed sessions are
skipped and the parent artifact chain is reconstructed and hash-verified.

All control state lives under ``experiments/<id>/hitl/`` as single-writer JSON
files (atomic replace, no locking needed):

  params.json    creation parameters (written once by the creator; rebuilt into
                 ExperimentConfig + providers deterministically on every start)
  control.json   written by the controller (web backend / researcher)
  status.json    written only by the worker (heartbeat, position, live trace)
  schedule.json  written by the worker at startup (planned sessions)
  analysis/      post-fold LLM analysis (validation-only evidence)

Pausing always lands at a session boundary: the worker finishes the session in
flight, then blocks at the next gate. ``mode="step"`` additionally requires an
explicit per-session approval before each session starts.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping

from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.runtime import utc_now_iso
from autotrade.environment.snapshot import SnapshotConfig

from .config import ExperimentConfig, FrozenArtifact
from .folds import build_fold_schedule, heldout_periods

HITL_DIR_NAME = "hitl"
PARAMS_NAME = "params.json"
CONTROL_NAME = "control.json"
STATUS_NAME = "status.json"
SCHEDULE_NAME = "schedule.json"
ANALYSIS_DIR_NAME = "analysis"
HELDOUT_SESSION_KEY = "heldout"

CONTROL_MODES = ("auto", "step")
CONTROL_REQUESTS = (None, "pause", "stop")

# Creation parameters mirror the run_experiment.py CLI dests one-to-one so the
# same assembly builders can be reused; HITL-only knobs are appended at the end.
# None means "no default: required" for the four period labels + experiment_id.
PARAM_DEFAULTS: dict[str, object] = {
    "experiment_id": None,
    "raw_dir": "data/raw",
    "fundamental_events_root": "data/pit/fundamental_events",
    "fundamental_events_status": "results/data_quality/fundamental_events_status.json",
    "experiments_root": "experiments",
    "work_root": ".runtime/sandboxes",
    "template_dir": "configs/agent_output_template",
    "fold_period": "quarter",
    "first_test_period": None,
    "last_test_period": None,
    "heldout_first_period": None,
    "heldout_last_period": None,
    "epochs": 1,
    "window_months": 21,
    "daily_window_months": None,
    "fundamentals_window_months": None,
    "events_window_months": None,
    "macro_window_months": None,
    "text_window_months": None,
    "intraday_trade_days": SnapshotConfig().intraday_trade_days,
    "max_fold_minutes": 60,
    "convergence_start_epoch": 3,
    "disable_step_tree": False,
    "nl_failure_policy": "return_error_with_audit",
    # Session / replay budgets (ExperimentConfig fields, no CLI dests).
    "max_steps_per_fold": 10,
    "max_backtests_per_fold": 30,
    "finalize_before_deadline_seconds": 300,
    "per_call_timeout_seconds": 300,
    "meta_memory_max_epochs": 3,
    "record_failed_attempts": True,
    "meta_sandbox_rebuild_timeout_seconds": 1800,
    "meta_sandbox_image_keep": 3,
    "offsession_tick_minutes": 15,
    "execution_lag_bars": 2,
    "decision_max_sim_minutes": 60.0,
    "backtest_max_seconds_per_decision": 300.0,
    "backtest_max_seconds_per_trading_day": 900.0,
    "nl_max_calls_per_decision_day": 10,
    "nl_max_calls_per_backtest": None,
    # Broker profile overrides (dataclasses.replace over the default profile).
    "stock_initial_cash": 500_000.0,
    "credit_initial_cash": 500_000.0,
    "commission_bps": 1.0,
    "slippage_bps": 5.0,
    "max_total_holdings": None,
    "max_single_name_weight": None,
    "fin_rate_annual": 0.0835,
    "slo_rate_annual": 0.085,
    "min_return": 0.0,
    "min_sharpe": 0.0,
    "max_drawdown": 0.25,
    "model": "deepseek-v4-pro",
    "nl_model": "deepseek-v4-flash",
    "compact_model": "deepseek-v4-flash",
    "disable_context_compact": False,
    "reasoning_effort": "max",
    "compact_token_threshold": 200_000,
    "compact_keep_recent_messages": 12,
    "compact_max_tokens": 1600,
    "compact_max_calls": 8,
    "local_dev": False,
    "no_thinking": False,
    "meta_learning_directive": "",
    "web_search_engines": ("tavily", "semantic_scholar"),
    "tavily_api_key_env": "TAVILY_API_KEY",
    "semantic_scholar_api_key_env": "SEMANTIC_SCHOLAR_API_KEY",
    "meta_learning_network": "bridge",
    "meta_learning_env": (),
    "meta_learning_add_host_gateway": False,
    "meta_learning_host_proxy": False,
    "disable_meta_learning_host_proxy": False,
    "disable_meta_learning_managed_proxy": False,
    "meta_learning_xray_bin": None,
    "meta_learning_xray_startup_timeout": 15.0,
    "disable_meta_sandbox_rebuild": False,
    # HITL-only knobs (not run_experiment CLI dests).
    "initial_control_mode": "step",
    "analysis_enabled": True,
    "analysis_model": "deepseek-v4-pro",
}
_REQUIRED_PARAMS = (
    "experiment_id",
    "first_test_period",
    "last_test_period",
    "heldout_first_period",
    "heldout_last_period",
)
_PATH_PARAMS = (
    "raw_dir",
    "fundamental_events_root",
    "fundamental_events_status",
    "experiments_root",
    "work_root",
    "template_dir",
)


class ExperimentStopped(Exception):
    """Raised at a gate when the controller requested a durable stop."""


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


# ---------------------------------------------------------------------------
# creation parameters
# ---------------------------------------------------------------------------
def resolve_options(params: Mapping[str, object], repo_root: Path) -> SimpleNamespace:
    """Merge creation params over PARAM_DEFAULTS into an assembly-compatible namespace.

    Unknown keys fail fast (a typo must not silently fall back to a default);
    underscore-prefixed keys are creator metadata (e.g. ``_created_at``) and are
    ignored; relative paths resolve against the repo root.
    """
    params = {key: value for key, value in params.items() if not str(key).startswith("_")}
    unknown = sorted(set(params) - set(PARAM_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown experiment parameters: {unknown}")
    merged: dict[str, object] = {**PARAM_DEFAULTS, **dict(params)}
    missing = [key for key in _REQUIRED_PARAMS if not merged.get(key)]
    if missing:
        raise ValueError(f"missing required experiment parameters: {missing}")
    for key in _PATH_PARAMS:
        value = Path(str(merged[key]))
        merged[key] = value if value.is_absolute() else (repo_root / value)
    merged["meta_learning_env"] = [str(name) for name in (merged.get("meta_learning_env") or ())]
    merged["web_search_engines"] = tuple(str(engine) for engine in (merged.get("web_search_engines") or ()))
    mode = str(merged["initial_control_mode"])
    if mode not in CONTROL_MODES:
        raise ValueError(f"initial_control_mode must be one of {CONTROL_MODES}, got {mode!r}")
    return SimpleNamespace(**merged)


def build_config_from_options(options: SimpleNamespace, *, repo_root: Path) -> ExperimentConfig:
    """Mirror run_experiment.py's config construction from resolved options."""
    from dataclasses import replace as dc_replace

    from .assembly import (
        build_meta_learning_managed_proxy_spec,
        build_meta_learning_sandbox_spec,
        build_snapshot_config,
    )
    from autotrade.environment.broker import BrokerProfile
    from autotrade.environment.sandbox import SandboxSpec

    from .config import AcceptanceRules

    sandbox_spec = SandboxSpec.from_host_fraction()
    meta_learning_sandbox_spec = build_meta_learning_sandbox_spec(options, sandbox_spec, repo_root=repo_root)
    meta_learning_managed_proxy = build_meta_learning_managed_proxy_spec(
        options,
        repo_root=repo_root,
        sandbox_spec=meta_learning_sandbox_spec,
    )
    broker_profile = dc_replace(
        BrokerProfile(),
        stock_initial_cash=float(options.stock_initial_cash),
        credit_initial_cash=float(options.credit_initial_cash),
        commission_bps=float(options.commission_bps),
        slippage_bps=float(options.slippage_bps),
        max_total_holdings=int(options.max_total_holdings) if options.max_total_holdings is not None else None,
        max_single_name_weight=(
            float(options.max_single_name_weight) if options.max_single_name_weight is not None else None
        ),
        fin_rate_annual=float(options.fin_rate_annual),
        slo_rate_annual=float(options.slo_rate_annual),
    )
    return ExperimentConfig(
        experiment_id=str(options.experiment_id),
        experiments_root=options.experiments_root.resolve(),
        work_root=options.work_root.resolve(),
        template_dir=options.template_dir.resolve(),
        first_test_period=str(options.first_test_period),
        last_test_period=str(options.last_test_period),
        heldout_first_period=str(options.heldout_first_period),
        heldout_last_period=str(options.heldout_last_period),
        fold_period=str(options.fold_period),
        epochs=int(options.epochs),
        window_months=int(options.window_months),
        max_fold_minutes=int(options.max_fold_minutes),
        finalize_before_deadline_seconds=int(options.finalize_before_deadline_seconds),
        per_call_timeout_seconds=int(options.per_call_timeout_seconds),
        max_steps_per_fold=int(options.max_steps_per_fold),
        max_backtests_per_fold=int(options.max_backtests_per_fold),
        offsession_tick_minutes=int(options.offsession_tick_minutes),
        execution_lag_bars=int(options.execution_lag_bars),
        decision_max_sim_minutes=(
            float(options.decision_max_sim_minutes) if options.decision_max_sim_minutes is not None else None
        ),
        backtest_max_seconds_per_decision=float(options.backtest_max_seconds_per_decision),
        backtest_max_seconds_per_trading_day=float(options.backtest_max_seconds_per_trading_day),
        nl_max_calls_per_decision_day=int(options.nl_max_calls_per_decision_day),
        nl_max_calls_per_backtest=(
            int(options.nl_max_calls_per_backtest) if options.nl_max_calls_per_backtest is not None else None
        ),
        snapshot_config=build_snapshot_config(options),
        nl_failure_policy=str(options.nl_failure_policy),
        convergence_start_epoch=int(options.convergence_start_epoch),
        meta_learning_directive=str(options.meta_learning_directive),
        meta_memory_max_epochs=int(options.meta_memory_max_epochs),
        step_tree_enabled=not bool(options.disable_step_tree),
        record_failed_attempts=bool(options.record_failed_attempts),
        acceptance=AcceptanceRules(
            min_return=float(options.min_return),
            min_sharpe=float(options.min_sharpe),
            max_drawdown=float(options.max_drawdown),
            require_complete_validation=True,
        ),
        broker_profile=broker_profile,
        sandbox_spec=sandbox_spec,
        meta_learning_sandbox_spec=meta_learning_sandbox_spec,
        meta_learning_managed_proxy=meta_learning_managed_proxy,
        meta_sandbox_rebuild_enabled=not bool(options.disable_meta_sandbox_rebuild),
        meta_sandbox_rebuild_timeout_seconds=int(options.meta_sandbox_rebuild_timeout_seconds),
        meta_sandbox_image_keep=int(options.meta_sandbox_image_keep),
        use_docker=not bool(options.local_dev),
    )


# ---------------------------------------------------------------------------
# control / status files
# ---------------------------------------------------------------------------
@dataclass
class ControlState:
    mode: str = "step"
    request: str | None = None
    approved_sessions: tuple[str, ...] = ()
    directives: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mode": self.mode,
            "request": self.request,
            "approved_sessions": sorted(self.approved_sessions),
            "directives": dict(self.directives),
            "updated_at": utc_now_iso(),
        }


def read_control(path: Path) -> ControlState:
    payload = read_json(path)
    mode = str(payload.get("mode") or "step")
    if mode not in CONTROL_MODES:
        mode = "step"
    request = payload.get("request")
    request = str(request) if request in ("pause", "stop") else None
    approved = payload.get("approved_sessions")
    directives = payload.get("directives")
    return ControlState(
        mode=mode,
        request=request,
        approved_sessions=tuple(str(key) for key in approved) if isinstance(approved, list) else (),
        directives={str(k): str(v) for k, v in directives.items()} if isinstance(directives, dict) else {},
    )


def write_control(path: Path, state: ControlState) -> None:
    write_json_atomic(path, state.to_record())


class StatusReporter:
    """Single-writer status.json with a heartbeat thread.

    The main thread owns state transitions; the daemon thread refreshes
    ``heartbeat_at`` and, while a session runs, discovers the newest run
    directory under the experiment work root so the web backend can tail the
    live agent_trace.jsonl without hooking into run_fold internals.
    """

    def __init__(self, path: Path, *, work_root: Path, interval_seconds: float = 3.0) -> None:
        self.path = path
        self.work_root = Path(work_root)
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._data: dict[str, object] = {
            "schema_version": 1,
            "pid": os.getpid(),
            "state": "starting",
            "phase": None,
            "session_key": None,
            "epoch_id": None,
            "fold_id": None,
            "run_id": None,
            "trace_path": None,
            "session_started_at": None,
            "fold_deadline_at": None,
            "completed_sessions": 0,
            "total_sessions": None,
            "error": None,
            "analysis_error": None,
            "started_at": utc_now_iso(),
        }

    def start(self) -> None:
        self._write()
        self._thread = threading.Thread(target=self._heartbeat_loop, name="hitl-status-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds * 2)

    def set(self, **fields: object) -> None:
        with self._lock:
            self._data.update(fields)
            self._write_locked()

    def _write(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        payload = dict(self._data)
        payload["heartbeat_at"] = utc_now_iso()
        payload["updated_at"] = utc_now_iso()
        write_json_atomic(self.path, payload)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with self._lock:
                if self._data.get("state") == "running_session":
                    self._refresh_live_run_locked()
                self._write_locked()

    def _refresh_live_run_locked(self) -> None:
        """Surface the live run dir, its trace path, and the session deadline
        so the console can show a preparation indicator and a countdown."""
        live = self._latest_run_dir()
        if live is None:
            return
        self._data["run_id"] = live.name
        self._data["trace_path"] = str(live / "artifacts" / "agent_trace.jsonl")
        if not self._data.get("fold_deadline_at"):
            manifest_path = live / "artifacts" / "run_manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                deadline = manifest.get("fold_deadline_at")
                if deadline:
                    self._data["fold_deadline_at"] = str(deadline)
            except (OSError, json.JSONDecodeError, ValueError):
                pass  # manifest not written yet or mid-write; retry next beat

    def _latest_run_dir(self) -> Path | None:
        try:
            candidates = [entry for entry in self.work_root.glob("run_*") if entry.is_dir()]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.stat().st_mtime)


def read_status(path: Path) -> dict[str, object]:
    return read_json(path)


def status_pid_alive(status: Mapping[str, object]) -> bool:
    pid = status.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # os.kill(pid, 0) succeeds on zombies (an exited worker whose spawning
    # server has not reaped it yet); treat state Z as dead.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        if stat.rpartition(")")[2].split()[:1] == ["Z"]:
            return False
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# session keys and schedule projection
# ---------------------------------------------------------------------------
def meta_session_key(epoch_id: str) -> str:
    return f"{epoch_id}/meta_learning"


def fold_session_key(epoch_id: str, fold_id: str) -> str:
    return f"{epoch_id}/{fold_id}"


def _epoch_ids(epochs: int) -> list[str]:
    return [f"epoch_{index:03d}" for index in range(1, epochs + 1)]


def build_session_plan(config: ExperimentConfig, folds, heldout, *, meta_enabled: bool) -> list[dict[str, object]]:
    sessions: list[dict[str, object]] = []
    for epoch_id in _epoch_ids(config.epochs):
        if meta_enabled:
            sessions.append({"key": meta_session_key(epoch_id), "kind": "meta_learning", "epoch_id": epoch_id})
        for fold in folds:
            sessions.append(
                {
                    "key": fold_session_key(epoch_id, fold.fold_id),
                    "kind": "fold",
                    "epoch_id": epoch_id,
                    **fold.to_record(),
                }
            )
    sessions.append(
        {
            "key": HELDOUT_SESSION_KEY,
            "kind": "heldout",
            "epoch_id": _epoch_ids(config.epochs)[-1],
            "periods": [
                {"label": period["label"], "start": period["start"], "end": period["end"]} for period in heldout
            ],
        }
    )
    return sessions


# ---------------------------------------------------------------------------
# interactive runner
# ---------------------------------------------------------------------------
class InteractiveExperimentRunner:
    """Gated epoch -> meta-learning -> folds -> held-out loop with resume.

    ``pipeline`` needs the ExperimentPipeline surface: ``config``, ``ledger``,
    ``meta_learner``, ``run_meta_learning``, ``run_fold``, ``run_heldout``.
    ``post_fold_hook`` receives ``(fold_record, outcome)`` after each newly run
    fold; hook failures are recorded in status.json but never abort the run.
    """

    def __init__(
        self,
        pipeline,
        *,
        hitl_dir: Path,
        poll_seconds: float = 2.0,
        post_fold_hook: Callable[[dict[str, object], object], None] | None = None,
        status: StatusReporter | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.config: ExperimentConfig = pipeline.config
        self.hitl_dir = Path(hitl_dir)
        self.poll_seconds = poll_seconds
        self.post_fold_hook = post_fold_hook
        self.status = status or StatusReporter(
            self.hitl_dir / STATUS_NAME, work_root=Path(self.config.work_root)
        )
        self.control_path = self.hitl_dir / CONTROL_NAME

    # ---- main loop ----

    def run(self, trading_days: list[str]) -> dict[str, object]:
        folds = build_fold_schedule(
            self.config.first_test_period,
            self.config.last_test_period,
            trading_days,
            window_months=self.config.window_months,
            period=self.config.fold_period,
        )
        heldout = heldout_periods(
            self.config.heldout_first_period,
            self.config.heldout_last_period,
            trading_days,
            period=self.config.fold_period,
        )
        meta_enabled = self.pipeline.meta_learner is not None
        sessions = build_session_plan(self.config, folds, heldout, meta_enabled=meta_enabled)
        write_json_atomic(
            self.hitl_dir / SCHEDULE_NAME,
            {"schema_version": 1, "epochs": self.config.epochs, "sessions": sessions},
        )
        fold_records = self._latest_records("fold")
        meta_records = self._latest_records("meta_learning")
        heldout_done = {
            str(record.get("fold_id", "")).removeprefix("heldout_")
            for record in self.pipeline.ledger.read("heldout")
        }
        total = len(sessions)
        completed = 0
        self.status.set(total_sessions=total, completed_sessions=0, state="starting")

        parent: FrozenArtifact | None = None
        taste_prompt = ""
        epoch_id = ""
        for epoch_id in _epoch_ids(self.config.epochs):
            if meta_enabled:
                key = meta_session_key(epoch_id)
                restored = meta_records.get(epoch_id)
                if restored is not None:
                    parent, taste_prompt = self._restore_meta(restored, parent)
                else:
                    directive = self._gate(key, phase="meta_learning", epoch_id=epoch_id)
                    self.status.set(
                        state="running_session", phase="meta_learning", session_key=key,
                        epoch_id=epoch_id, fold_id=None, run_id=None, trace_path=None,
                        session_started_at=utc_now_iso(), fold_deadline_at=None,
                    )
                    parent, taste_prompt = self.pipeline.run_meta_learning(
                        epoch_id=epoch_id,
                        parent=parent,
                        previous_taste=taste_prompt,
                        visible_fold=folds[0] if folds else None,
                        directive_override=directive if directive.strip() else None,
                    )
                completed += 1
                self.status.set(completed_sessions=completed)
            for fold in folds:
                key = fold_session_key(epoch_id, fold.fold_id)
                restored = fold_records.get((epoch_id, fold.fold_id))
                if restored is not None:
                    parent = self._artifact_from_fold_record(restored)
                else:
                    self._require_no_orphan_artifact(epoch_id, fold.fold_id)
                    directive = self._gate(key, phase="fold", epoch_id=epoch_id, fold_id=fold.fold_id)
                    self.status.set(
                        state="running_session", phase="fold", session_key=key,
                        epoch_id=epoch_id, fold_id=fold.fold_id, run_id=None, trace_path=None,
                        session_started_at=utc_now_iso(), fold_deadline_at=None,
                    )
                    outcome = self.pipeline.run_fold(
                        fold,
                        epoch_id=epoch_id,
                        parent=parent,
                        taste_prompt=taste_prompt,
                        fold_directive=directive,
                    )
                    parent = outcome.frozen
                    self._run_post_fold_hook(epoch_id, fold.fold_id, outcome)
                completed += 1
                self.status.set(completed_sessions=completed)
        if parent is None:
            raise RuntimeError("experiment produced no frozen strategy artifact")
        heldout_runs = 0
        pending_heldout = [period for period in heldout if str(period["label"]) not in heldout_done]
        if pending_heldout:
            self._gate(HELDOUT_SESSION_KEY, phase="heldout", epoch_id=epoch_id)
            self.status.set(
                state="running_session", phase="heldout", session_key=HELDOUT_SESSION_KEY,
                epoch_id=epoch_id, fold_id=None, run_id=None, trace_path=None,
                session_started_at=utc_now_iso(), fold_deadline_at=None,
            )
            summaries = self.pipeline.run_heldout(
                parent, trading_days, epoch_id=epoch_id, skip_labels=frozenset(heldout_done)
            )
            heldout_runs = len(summaries)
        completed += 1
        self.status.set(completed_sessions=completed, state="completed", phase=None, session_key=None)
        return {"final_strategy_artifact": parent.artifact_id, "heldout_runs": heldout_runs}

    # ---- gating ----

    def _gate(self, key: str, *, phase: str, epoch_id: str, fold_id: str | None = None) -> str:
        announced_state: str | None = None
        while True:
            control = read_control(self.control_path)
            if control.request == "stop":
                raise ExperimentStopped(f"stop requested before session {key}")
            if control.request == "pause":
                if announced_state != "paused":
                    announced_state = "paused"
                    self.status.set(state="paused", phase=phase, session_key=key, epoch_id=epoch_id, fold_id=fold_id)
                time.sleep(self.poll_seconds)
                continue
            if control.mode == "step" and key not in control.approved_sessions:
                if announced_state != "waiting_user":
                    announced_state = "waiting_user"
                    self.status.set(
                        state="waiting_user", phase=phase, session_key=key, epoch_id=epoch_id, fold_id=fold_id
                    )
                time.sleep(self.poll_seconds)
                continue
            return control.directives.get(key, "")

    # ---- resume helpers ----

    def _latest_records(self, record_type: str) -> dict:
        latest: dict = {}
        for record in self.pipeline.ledger.read(record_type):
            if record_type == "fold":
                latest[(str(record.get("epoch_id")), str(record.get("fold_id")))] = record
            else:
                latest[str(record.get("epoch_id"))] = record
        return latest

    def _restore_meta(self, record: dict[str, object], parent: FrozenArtifact | None) -> tuple[FrozenArtifact | None, str]:
        taste = ""
        taste_path = record.get("taste_path")
        if taste_path:
            path = Path(str(taste_path))
            if not path.exists():
                raise RuntimeError(f"resume failed: recorded taste file missing: {path}")
            taste = path.read_text(encoding="utf-8").strip()
        if not taste:
            raise RuntimeError(
                f"resume failed: meta-learning record for {record.get('epoch_id')} has no non-empty taste"
            )
        if str(record.get("status")) == "meta_regularized":
            artifact_id = str(record.get("frozen_strategy_artifact_id"))
            path = Path(self.config.experiment_dir) / "strategy_artifacts" / str(record.get("epoch_id")) / artifact_id
            parent = self._verified_artifact(
                artifact_id=artifact_id,
                path=path,
                expected_hash=str(record.get("frozen_strategy_artifact_hash")),
                model_path=path.with_name(f"{artifact_id}.models"),
                expected_model_hash=str(record.get("frozen_model_artifact_hash")),
            )
        return parent, taste

    def _artifact_from_fold_record(self, record: dict[str, object]) -> FrozenArtifact:
        path = record.get("frozen_strategy_artifact_path")
        if not path:
            raise RuntimeError(f"resume failed: fold record {record.get('fold_id')} has no frozen artifact path")
        model_path = record.get("frozen_model_artifact_path")
        return self._verified_artifact(
            artifact_id=str(record.get("frozen_strategy_artifact_id")),
            path=Path(str(path)),
            expected_hash=str(record.get("frozen_strategy_artifact_hash")),
            model_path=Path(str(model_path)) if model_path else None,
            expected_model_hash=str(record.get("frozen_model_artifact_hash")),
        )

    def _verified_artifact(
        self,
        *,
        artifact_id: str,
        path: Path,
        expected_hash: str,
        model_path: Path | None,
        expected_model_hash: str,
    ) -> FrozenArtifact:
        if not path.is_dir():
            raise RuntimeError(f"resume failed: frozen artifact directory missing: {path}")
        current_hash = artifact_hash(path)
        if current_hash != expected_hash:
            raise RuntimeError(
                f"resume failed: frozen artifact {artifact_id} hash changed on disk "
                f"({current_hash} != ledger {expected_hash})"
            )
        # model_artifact_hash tolerates a missing dir (stable empty-models hash).
        current_model_hash = model_artifact_hash(model_path if model_path is not None else path / ".missing_models")
        if current_model_hash != expected_model_hash:
            raise RuntimeError(
                f"resume failed: frozen model artifact for {artifact_id} hash changed on disk "
                f"({current_model_hash} != ledger {expected_model_hash})"
            )
        return FrozenArtifact(
            artifact_id=artifact_id,
            path=path,
            artifact_hash=expected_hash,
            model_path=model_path if model_path and model_path.is_dir() else None,
            model_artifact_hash=expected_model_hash,
        )

    def _require_no_orphan_artifact(self, epoch_id: str, fold_id: str) -> None:
        # A hard kill between _freeze and the ledger append leaves a frozen dir
        # without a record; rerunning the fold would FileExistsError deep inside
        # _freeze. Surface it clearly instead of half-running a session.
        candidate = (
            Path(self.config.experiment_dir) / "strategy_artifacts" / epoch_id / f"strategy_{epoch_id}_{fold_id}"
        )
        if candidate.exists():
            raise RuntimeError(
                f"orphan frozen artifact without a ledger record: {candidate}; "
                "inspect and remove it manually before resuming this fold"
            )

    def _run_post_fold_hook(self, epoch_id: str, fold_id: str, outcome) -> None:
        if self.post_fold_hook is None:
            return
        record = self._latest_records("fold").get((epoch_id, fold_id))
        try:
            self.post_fold_hook(record or {}, outcome)
            self.status.set(analysis_error=None)
        except Exception as exc:  # noqa: BLE001 - analysis is advisory, never fatal
            self.status.set(analysis_error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# worker entrypoint
# ---------------------------------------------------------------------------
def run_interactive_worker(experiment_dir: Path, *, repo_root: Path, poll_seconds: float = 2.0) -> dict[str, object]:
    """Load hitl/params.json, rebuild the pipeline, and run the gated loop.

    Returns a summary dict; raises on hard failures after recording them in
    status.json. Designed to run as a detached process spawned by the web
    backend (or manually for a headless HITL run).
    """
    from .assembly import (
        build_pipeline,
        build_proxies,
        build_session_builders,
        build_web_search_providers,
    )
    from .folds import load_sse_trading_days

    experiment_dir = Path(experiment_dir)
    hitl_dir = experiment_dir / HITL_DIR_NAME
    params = read_json(hitl_dir / PARAMS_NAME)
    if not params:
        raise FileNotFoundError(f"missing or empty {hitl_dir / PARAMS_NAME}")
    options = resolve_options(params, repo_root)
    if experiment_dir.resolve() != (Path(options.experiments_root) / str(options.experiment_id)).resolve():
        raise ValueError(
            f"params experiment location mismatch: {experiment_dir} vs "
            f"{Path(options.experiments_root) / str(options.experiment_id)}"
        )

    existing = read_status(hitl_dir / STATUS_NAME)
    if existing and existing.get("state") in ("starting", "running_session", "waiting_user", "paused") and status_pid_alive(existing):
        raise RuntimeError(
            f"experiment {options.experiment_id} already has a live worker (pid {existing.get('pid')})"
        )

    # Provider key loading matches the CLI entrypoints: .env is read relative to
    # the repo root (DeepSeekProxy.from_env and web-search providers).
    os.chdir(repo_root)

    config = build_config_from_options(options, repo_root=repo_root)
    proxies = build_proxies(options)
    web_search_providers = build_web_search_providers(options)
    agent_factory, meta_learner = build_session_builders(
        config=config,
        proxies=proxies,
        web_search_providers=web_search_providers,
    )
    pipeline = build_pipeline(config, options, agent_factory, meta_learner, proxies)

    control_path = hitl_dir / CONTROL_NAME
    if not control_path.exists():
        write_control(control_path, ControlState(mode=str(options.initial_control_mode)))

    post_fold_hook = None
    if bool(options.analysis_enabled):
        from autotrade.environment.llm import DeepSeekProxy

        from .fold_analysis import analyze_fold

        analysis_proxy = DeepSeekProxy.from_env(
            model=str(options.analysis_model),
            thinking_enabled=not bool(options.no_thinking),
            reasoning_effort="high",
        )

        def post_fold_hook(record: dict[str, object], outcome) -> None:  # noqa: F811 - deliberate rebind
            analyze_fold(
                analysis_proxy,
                ledger_record=record,
                strategy_dir=Path(outcome.frozen.path),
                model_dir=Path(outcome.frozen.model_path) if outcome.frozen.model_path else None,
                out_dir=hitl_dir / ANALYSIS_DIR_NAME,
            )

    status = StatusReporter(hitl_dir / STATUS_NAME, work_root=Path(config.work_root))
    status.start()
    runner = InteractiveExperimentRunner(
        pipeline,
        hitl_dir=hitl_dir,
        poll_seconds=poll_seconds,
        post_fold_hook=post_fold_hook,
        status=status,
    )
    try:
        result = runner.run(load_sse_trading_days(options.raw_dir))
        return {"status": "completed", **result}
    except ExperimentStopped as exc:
        status.set(state="stopped", error=None, phase=None)
        return {"status": "stopped", "reason": str(exc)}
    except SystemExit:
        status.set(state="stopped", error="terminated_by_signal", phase=None)
        raise
    except BaseException as exc:
        status.set(state="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        status.stop()
