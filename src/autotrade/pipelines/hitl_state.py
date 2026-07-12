"""HITL control-plane state: creation params, control/status file protocol.

The shared vocabulary between the interactive worker (``interactive.py``), the
web console (``autotrade.webui``), and the CLI: parameter defaults + option
resolution, and the single-writer JSON files under ``experiments/<id>/hitl/``
(``params.json`` / ``control.json`` / ``status.json`` / ``schedule.json``) with
their readers, writers, and worker-liveness checks. No orchestration logic —
importing this module must not drag in the worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from autotrade.agent.compact import ContextCompactionConfig
from autotrade.environment.broker import BrokerProfile
from autotrade.environment.runtime import utc_now_iso, write_json_atomic
from autotrade.environment.sandbox import SandboxSpec
from autotrade.environment.snapshot import SnapshotConfig

from .config import AcceptanceRules, ExperimentConfig

HITL_DIR_NAME = "hitl"
PARAMS_NAME = "params.json"
CONTROL_NAME = "control.json"
STATUS_NAME = "status.json"
SCHEDULE_NAME = "schedule.json"
ANALYSIS_DIR_NAME = "analysis"
HELDOUT_SESSION_KEY = "heldout"

# auto: run continuously; manual: approve each SESSION before it starts;
# step: manual PLUS every fold session holds at each validated step
# (per-session step_gate entries override in both directions).
CONTROL_MODES = ("auto", "manual", "step")
CONTROL_REQUESTS = (None, "pause", "stop")

# Creation parameters mirror the run_experiment.py CLI dests one-to-one so the
# same assembly builders can be reused; HITL-only knobs are appended at the end.
# None means "no default: required" for the four period labels + experiment_id.
#
# Keys that are 1:1 domain-dataclass fields take their defaults FROM the
# dataclass (single source; see the overlay below the literal): only keys with
# no domain owner (paths, model names, HITL knobs) stay literal here. A drift
# test pins both this dict and the CLI argparse defaults to the dataclasses.
PARAM_DEFAULTS: dict[str, object] = {
    "experiment_id": None,
    "raw_dir": "data/raw",
    "fundamental_events_root": "data/pit/fundamental_events",
    "fundamental_events_status": "results/data_quality/fundamental_events_status.json",
    "experiments_root": "experiments",
    "work_root": ".runtime/sandboxes",
    "template_dir": "configs/agent_output_template",
    # Seed the first fold from another experiment's latest frozen fold output
    # instead of the blank template (manager copies + hash-verifies at create).
    "inherit_from": "",
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
    # Data-domain filtering: domain off = excluded from decision snapshots AND
    # replay slots; empty dataset tuples = the domain's full default set.
    "include_events": True,
    "include_macro": True,
    "include_text": True,
    "include_fundamentals": True,
    "include_intraday": True,
    "events_datasets": (),
    "macro_datasets": (),
    "text_datasets": (),
    "fundamental_datasets": (),
    "screen_exclude_st": False,
    "screen_exclude_new_listed_days": 0,
    "screen_min_circ_mv_yi": None,
    "screen_max_circ_mv_yi": None,
    "screen_min_price": None,
    "screen_max_price": None,
    "screen_boards": (),
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
    "offsession_tick_minutes": 30,
    "intraday_decision_minutes": 1,
    "execution_lag_bars": 2,
    "decision_max_sim_minutes": 30.0,
    "backtest_max_seconds_per_decision": 1800.0,
    "backtest_max_seconds_per_trading_day": 3600.0,
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
    "initial_control_mode": "manual",
    "gpu_count": SandboxSpec().gpu_count,
    "analysis_enabled": True,
    "analysis_model": "deepseek-v4-pro",
    "analysis_max_tokens": 6000,
}
# Single-source overlay: every PARAM_DEFAULTS key that names an ExperimentConfig
# field takes the dataclass default; broker/acceptance/compaction keys map to
# their own dataclasses. The literals above stay readable, this keeps them honest.
PARAM_DEFAULTS.update(
    {
        f.name: f.default
        for f in fields(ExperimentConfig)
        if f.name in PARAM_DEFAULTS and f.default is not MISSING
    }
)
_BROKER_DEFAULTS = BrokerProfile()
_ACCEPTANCE_DEFAULTS = AcceptanceRules()
_COMPACT_DEFAULTS = ContextCompactionConfig()
PARAM_DEFAULTS.update(
    {
        **{
            key: getattr(_BROKER_DEFAULTS, key)
            for key in (
                "stock_initial_cash", "credit_initial_cash", "commission_bps", "slippage_bps",
                "max_total_holdings", "max_single_name_weight", "fin_rate_annual", "slo_rate_annual",
            )
        },
        "min_return": _ACCEPTANCE_DEFAULTS.min_return,
        "min_sharpe": _ACCEPTANCE_DEFAULTS.min_sharpe,
        "max_drawdown": _ACCEPTANCE_DEFAULTS.max_drawdown,
        "compact_token_threshold": _COMPACT_DEFAULTS.token_threshold,
        "compact_keep_recent_messages": _COMPACT_DEFAULTS.keep_recent_messages,
        "compact_max_tokens": _COMPACT_DEFAULTS.max_response_tokens,
        "compact_max_calls": _COMPACT_DEFAULTS.max_calls,
    }
)
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
    try:
        merged["gpu_count"] = int(merged["gpu_count"])
    except (TypeError, ValueError) as exc:
        raise ValueError("gpu_count must be an integer") from exc
    if not 1 <= merged["gpu_count"] <= 4:
        raise ValueError("gpu_count must be between 1 and 4")
    return SimpleNamespace(**merged)



@dataclass
class ControlState:
    mode: str = "manual"
    request: str | None = None
    approved_sessions: tuple[str, ...] = ()
    directives: dict[str, str] = field(default_factory=dict)
    # Verbatim system-prompt overrides per session (fold sessions): when set,
    # the assembled prompt is replaced wholesale (recorded in the run manifest).
    prompt_overrides: dict[str, str] = field(default_factory=dict)
    # session_key -> rerun_id: re-run an already-recorded fold. Idempotent: a
    # fold whose latest ledger record carries the same rerun_id is NOT re-run.
    rerun_sessions: dict[str, str] = field(default_factory=dict)
    # Early finish: stop before the next unrun fold/meta session and jump to
    # held-out with the latest frozen artifact. Ignored until one fold froze.
    skip_to_heldout: bool = False
    # session_key -> GPU count for that fold's sandbox (set at the approval
    # gate; absent = the experiment's SandboxSpec default).
    gpu_counts: dict[str, int] = field(default_factory=dict)
    # session_key -> step-tree node_id: that fold session starts from the node
    # snapshot as its parent artifact instead of the inherited frozen chain
    # (user-side step rollback; consumed at session start, persists until cleared).
    parent_overrides: dict[str, str] = field(default_factory=dict)
    # Step-level gating: session_key -> enabled. When on, the fold session
    # holds after EVERY formal validation backtest until step_go[session_key]
    # reaches that step index; step_directives["<session>#<n>"] carries the
    # researcher's per-step guidance (injected into the tool observation).
    step_gate: dict[str, bool] = field(default_factory=dict)
    step_go: dict[str, int] = field(default_factory=dict)
    step_directives: dict[str, str] = field(default_factory=dict)
    # ask_user tool: "<session>#q<n>" -> researcher reply. Presence releases the
    # waiting question (empty string = proceed without guidance).
    user_replies: dict[str, str] = field(default_factory=dict)
    # Human-in-the-loop OOS discipline: test/held-out results stay hidden in the
    # console until the researcher explicitly reveals them; revealing SEALS the
    # experiment (no further approvals/directives/reruns/rollbacks).
    test_revealed: bool = False

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mode": self.mode,
            "request": self.request,
            "approved_sessions": sorted(self.approved_sessions),
            "directives": dict(self.directives),
            "prompt_overrides": dict(self.prompt_overrides),
            "rerun_sessions": dict(self.rerun_sessions),
            "skip_to_heldout": self.skip_to_heldout,
            "gpu_counts": dict(self.gpu_counts),
            "parent_overrides": dict(self.parent_overrides),
            "step_gate": dict(self.step_gate),
            "step_go": dict(self.step_go),
            "step_directives": dict(self.step_directives),
            "user_replies": dict(self.user_replies),
            "test_revealed": self.test_revealed,
            "updated_at": utc_now_iso(),
        }


def read_control(path: Path) -> ControlState:
    payload = read_json(path)
    mode = str(payload.get("mode") or "manual")
    if mode not in CONTROL_MODES:
        mode = "manual"
    request = payload.get("request")
    request = str(request) if request in ("pause", "stop") else None
    approved = payload.get("approved_sessions")
    directives = payload.get("directives")
    overrides = payload.get("prompt_overrides")
    reruns = payload.get("rerun_sessions")
    parents = payload.get("parent_overrides")
    return ControlState(
        mode=mode,
        request=request,
        approved_sessions=tuple(str(key) for key in approved) if isinstance(approved, list) else (),
        directives={str(k): str(v) for k, v in directives.items()} if isinstance(directives, dict) else {},
        prompt_overrides={str(k): str(v) for k, v in overrides.items()} if isinstance(overrides, dict) else {},
        rerun_sessions={str(k): str(v) for k, v in reruns.items()} if isinstance(reruns, dict) else {},
        skip_to_heldout=bool(payload.get("skip_to_heldout")),
        gpu_counts=_int_map(payload.get("gpu_counts")),
        parent_overrides={str(k): str(v) for k, v in parents.items()} if isinstance(parents, dict) else {},
        step_gate={str(k): bool(v) for k, v in payload.get("step_gate", {}).items()} if isinstance(payload.get("step_gate"), dict) else {},
        step_go=_int_map(payload.get("step_go")),
        step_directives={str(k): str(v) for k, v in payload.get("step_directives", {}).items()} if isinstance(payload.get("step_directives"), dict) else {},
        user_replies={str(k): str(v) for k, v in payload.get("user_replies", {}).items()} if isinstance(payload.get("user_replies"), dict) else {},
        test_revealed=bool(payload.get("test_revealed")),
    )


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        try:
            count = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[str(key)] = count
    return out


def repo_code_version(repo_root: Path | None = None) -> str:
    """Short git HEAD of the running code. Long-lived workers import code at
    spawn: the console compares this stamp against the repo's current HEAD to
    flag workers running stale code (restart to pick up fixes). Uncommitted
    edits do not change the stamp — commit-level granularity only."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root or Path.cwd(), capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


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
            "pid_start_ticks": proc_start_ticks(os.getpid()),
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


def proc_start_ticks(pid: int) -> int | None:
    """Kernel start time (clock ticks since boot) of ``pid``; None if unreadable.

    ``(pid, start_ticks)`` uniquely identifies a process incarnation, so a pid
    number recycled after a crash/reboot never impersonates a dead worker.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return int(stat.rpartition(")")[2].split()[19])
    except (OSError, IndexError, ValueError):
        return None


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
    # (pid, kernel start ticks) identifies a process incarnation; a status
    # without a matching recorded start time is dead (or a recycled pid).
    recorded_ticks = status.get("pid_start_ticks")
    if not isinstance(recorded_ticks, int) or proc_start_ticks(pid) != recorded_ticks:
        return False
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
