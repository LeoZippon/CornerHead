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
flight, then blocks at the next gate. ``mode="manual"`` additionally requires an
explicit per-session approval before each session starts.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.runtime import utc_now_iso, write_json_atomic
from autotrade.environment.step_tree import StepTree
from autotrade.environment.tools.base import SessionInterrupt

from .config import ExperimentConfig, FrozenArtifact
from .folds import build_fold_schedule, heldout_periods
from .hitl_state import (
    ANALYSIS_DIR_NAME,
    CONTROL_NAME,
    ControlState,
    HELDOUT_SESSION_KEY,
    HITL_DIR_NAME,
    PARAMS_NAME,
    SCHEDULE_NAME,
    STATUS_NAME,
    StatusReporter,
    _epoch_ids,
    build_session_plan,
    fold_session_key,
    meta_session_key,
    read_control,
    read_json,
    read_status,
    repo_code_version,
    resolve_options,
    status_pid_alive,
    write_control,
)

class ExperimentStopped(SessionInterrupt):
    """Raised at a gate when the controller requested a durable stop.

    Subclasses SessionInterrupt so a stop issued while a fold is held at a
    step gate re-raises through the Agent runner's tool dispatch instead of
    being swallowed into an error observation."""


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

    sandbox_spec = SandboxSpec.from_host_fraction(gpu_count=int(options.gpu_count))
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
        intraday_decision_minutes=int(options.intraday_decision_minutes),
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

        # Inherited seed (from another experiment's frozen output) replaces the
        # blank template as the first fold's parent; hash-verified every start.
        parent: FrozenArtifact | None = self._load_inherited_parent()
        taste_prompt = ""
        epoch_id = ""
        # Early finish: with at least one frozen fold, a skip_to_heldout request
        # stops before the next session that would actually RUN (already-recorded
        # sessions still restore to keep the parent chain intact) and falls
        # through to held-out with the latest frozen artifact.
        skip_now = lambda: parent is not None and read_control(self.control_path).skip_to_heldout  # noqa: E731
        skipping = False
        for epoch_id in _epoch_ids(self.config.epochs):
            if skipping:
                break
            if meta_enabled:
                key = meta_session_key(epoch_id)
                restored = meta_records.get(epoch_id)
                if restored is not None:
                    parent, taste_prompt = self._restore_meta(restored, parent)
                elif skip_now():
                    skipping = True
                    break
                else:
                    directive = self._gate(key, phase="meta_learning", epoch_id=epoch_id)
                    control = read_control(self.control_path)
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
                        system_prompt_override=control.prompt_overrides.get(key, ""),
                        user_question_hook=self._user_question_hook(key),
                    )
                completed += 1
                self.status.set(completed_sessions=completed)
            for fold in folds:
                key = fold_session_key(epoch_id, fold.fold_id)
                restored = fold_records.get((epoch_id, fold.fold_id))
                rerun_id = read_control(self.control_path).rerun_sessions.get(key)
                needs_rerun = (
                    restored is not None
                    and rerun_id is not None
                    and str(restored.get("rerun_id") or "") != rerun_id
                )
                if restored is not None and not needs_rerun:
                    parent = self._artifact_from_fold_record(restored)
                else:
                    if skip_now():
                        skipping = True
                        break
                    if restored is None:
                        self._require_no_orphan_artifact(epoch_id, fold.fold_id)
                    prefetch_method = getattr(self.pipeline, "prefetch_fold_data", None)
                    prefetch_pool = (
                        ThreadPoolExecutor(max_workers=1, thread_name_prefix="fold-data-prefetch")
                        if callable(prefetch_method)
                        else None
                    )
                    prefetch = prefetch_pool.submit(prefetch_method, fold) if prefetch_pool is not None else None
                    try:
                        directive = self._gate(key, phase="fold", epoch_id=epoch_id, fold_id=fold.fold_id)
                        control = read_control(self.control_path)
                        # User-side step rollback: a control-plane override replaces the
                        # inherited frozen chain with a validated step-tree node snapshot.
                        override_node = control.parent_overrides.get(key)
                        session_parent = self._parent_from_step_node(override_node) if override_node else parent
                        self.status.set(
                            state="running_session", phase="fold", session_key=key,
                            epoch_id=epoch_id, fold_id=fold.fold_id, run_id=None, trace_path=None,
                            session_started_at=utc_now_iso(), fold_deadline_at=None,
                            parent_override=override_node,
                        )
                        if prefetch is not None:
                            # Join before run_fold: no prefetch can overlap an
                            # Agent-triggered formal backtest.
                            prefetch.result()
                    finally:
                        if prefetch_pool is not None:
                            prefetch_pool.shutdown(wait=True, cancel_futures=True)
                    outcome = self.pipeline.run_fold(
                        fold,
                        epoch_id=epoch_id,
                        parent=session_parent,
                        taste_prompt=taste_prompt,
                        fold_directive=directive,
                        system_prompt_override=control.prompt_overrides.get(key, ""),
                        rerun_id=rerun_id if needs_rerun else None,
                        sandbox_gpu_count=control.gpu_counts.get(key),
                        step_gate_hook=self._step_gate_hook(key),
                        user_question_hook=self._user_question_hook(key),
                    )
                    parent = outcome.frozen
                    if needs_rerun:
                        # The re-run invalidates any earlier held-out results;
                        # they are replayed below (registry shows latest-per-label).
                        heldout_done = set()
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
            if control.mode in ("manual", "step") and key not in control.approved_sessions:
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

    def _load_inherited_parent(self) -> FrozenArtifact | None:
        payload = read_json(self.hitl_dir / PARAMS_NAME).get("_inherited_artifact")
        if not isinstance(payload, dict):
            return None
        model_path = payload.get("model_path")
        return self._verified_artifact(
            artifact_id=str(payload.get("artifact_id")),
            path=Path(str(payload.get("path"))),
            expected_hash=str(payload.get("artifact_hash")),
            model_path=Path(str(model_path)) if model_path else None,
            expected_model_hash=str(payload.get("model_artifact_hash")),
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

    def _step_gate_hook(self, key: str):
        """Per-step HITL gate, evaluated live from control.json.

        Called by the Agent runner after every formal validation backtest.
        No-op while step gating is off for the session (it can be toggled
        mid-fold); when on, holds the session (state=waiting_step_user) until
        the researcher releases this step, then returns their directive (if
        any) for injection into the tool observation. Wait time is credited
        back to the fold deadline by the runner."""

        def hook(step_index: int, summary: dict[str, object]) -> str:
            control = read_control(self.control_path)
            if not control.step_gate.get(key, control.mode == "step"):
                return ""
            self.status.set(
                state="waiting_step_user", session_key=key, awaiting_step=int(step_index),
                step_summary={
                    name: summary.get(name)
                    for name in ("result_name", "total_return", "sharpe", "max_drawdown",
                                 "complete_validation", "probe_note", "diagnostic_warnings")
                },
            )
            try:
                while True:
                    control = read_control(self.control_path)
                    if control.request == "stop":
                        raise ExperimentStopped(f"stop requested at step gate {key}#{step_index}")
                    if not control.step_gate.get(key, control.mode == "step") or control.step_go.get(key, 0) >= int(step_index):
                        break
                    time.sleep(self.poll_seconds)
            finally:
                self.status.set(state="running_session", awaiting_step=None, step_summary=None)
            return control.step_directives.get(f"{key}#{int(step_index)}", "")

        return hook

    def _user_question_hook(self, key: str):
        """ask_user tool bridge, evaluated live from control.json.

        Holds the session (state=waiting_user_reply) until the researcher
        answers via the console (a per-attempt nonce key published in status),
        then returns the reply text ("" = proceed without guidance). Returns
        None immediately when nobody is attending (mode=auto), so the Agent
        decides autonomously. Wait time is credited back to the fold deadline
        by the runner."""

        # A worker/session retry restarts the Agent's question counter at q1.
        # Include a per-hook attempt nonce so an old durable q1 reply cannot
        # silently answer a different question after a crash.
        attempt = uuid.uuid4().hex[:12]

        def hook(question_index: int, question: str) -> str | None:
            control = read_control(self.control_path)
            if control.mode == "auto":
                return None
            reply_key = f"{key}#ask{attempt}#q{int(question_index)}"
            self.status.set(
                state="waiting_user_reply", session_key=key,
                awaiting_question={
                    "index": int(question_index),
                    "question": str(question),
                    "reply_key": reply_key,
                },
            )
            try:
                while True:
                    control = read_control(self.control_path)
                    if control.request == "stop":
                        raise ExperimentStopped(f"stop requested at user question {reply_key}")
                    if control.mode == "auto":
                        return None
                    if reply_key in control.user_replies:
                        return str(control.user_replies.get(reply_key) or "")
                    time.sleep(self.poll_seconds)
            finally:
                self.status.set(state="running_session", awaiting_question=None)

        return hook

    def _parent_from_step_node(self, node_id: str) -> FrozenArtifact:
        """Build the session parent from a validated step-tree node snapshot."""
        tree = StepTree(Path(self.config.experiment_dir) / "steps")
        node = tree.get_node(node_id)  # ValueError on unknown ids — fail fast
        if node.get("status") == "failed" or not node.get("complete_validation"):
            raise RuntimeError(f"parent override {node_id} is not a validated node with a snapshot")
        model_hash = node.get("model_artifact_hash")
        if not model_hash:
            raise RuntimeError(f"parent override {node_id} carries no model artifact hash")
        models_dir = tree.node_models_dir(node_id)
        return self._verified_artifact(
            artifact_id=f"stepnode_{node_id}",
            path=tree.node_output_dir(node_id),
            expected_hash=str(node.get("artifact_hash")),
            model_path=models_dir if models_dir.is_dir() else None,
            expected_model_hash=str(model_hash),
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
def _decision_alert_hook(experiment_id: str):
    """Feishu group alerts for states that need the researcher (docs §5.3 HITL).

    Opt-in via FEISHU_APP_ID/APP_SECRET/CHAT_ID in the gitignored .env; absent
    credentials disable notifications entirely. Fired from the status
    reporter's transition thread — best-effort by construction."""
    from autotrade.notify import FeishuBot, load_dotenv_values

    env = load_dotenv_values()
    bot = FeishuBot.from_env(env)
    if bot is None:
        return None
    console_url = str(env.get("CONSOLE_BASE_URL", "")).rstrip("/")

    def hook(state: str, snapshot: dict[str, object]) -> None:
        card = _decision_alert_card(experiment_id, state, snapshot)
        if card is None:
            return
        button = {}
        if console_url:
            button = {"button_text": "打开控制台", "button_url": f"{console_url}/#/exp/{experiment_id}"}
        bot.send_card(card["title"], card["body"], color=card["color"], **button)

    return hook


def _decision_alert_card(experiment_id: str, state: str, snapshot: dict[str, object]) -> dict[str, str] | None:
    """Group-alert card: colored headline + experiment/session/progress context
    so a message is actionable without opening the console first."""
    session = str(snapshot.get("session_key") or "")
    completed = snapshot.get("completed_sessions")
    total = snapshot.get("total_sessions")
    context = f"**实验** {experiment_id}"
    if session:
        context += f"\n**会话** {session}"
    if completed is not None and total:
        context += f"\n**进度** {completed}/{total}"
    if state == "waiting_user":
        return {"title": "⏸ 会话等待批准", "color": "orange",
                "body": f"{context}\n请在控制台放行（可附研究指令）。"}
    if state == "waiting_step_user":
        summary = snapshot.get("step_summary") if isinstance(snapshot.get("step_summary"), dict) else {}
        ret = summary.get("total_return")
        metric = f"\n**验证收益** {float(ret) * 100:.2f}%" if isinstance(ret, (int, float)) else ""
        return {"title": f"🛑 Step {snapshot.get('awaiting_step')} 待批准", "color": "orange",
                "body": f"{context}{metric}\n请在控制台批准，可注入 Step 指令。"}
    if state == "waiting_user_reply":
        question = snapshot.get("awaiting_question") if isinstance(snapshot.get("awaiting_question"), dict) else {}
        body = str(question.get("question") or "")
        if len(body) > 300:
            body = body[:300] + "……"
        return {"title": f"❓ Agent 提问 #{question.get('index')}", "color": "blue",
                "body": f"{context}\n{body}\n请在控制台答复（留空=由 Agent 自行决策）。"}
    if state == "failed":
        return {"title": "❌ 实验失败", "color": "red",
                "body": f"{context}\n{snapshot.get('error')}"}
    return None


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
                max_tokens=int(options.analysis_max_tokens),
            )

    status = StatusReporter(
        hitl_dir / STATUS_NAME,
        work_root=Path(config.work_root),
        on_state_change=_decision_alert_hook(config.experiment_id),
    )
    # Long-lived worker: code is imported NOW. The console flags this stamp
    # against the repo's current HEAD so stale workers are visible.
    status.set(code_version=repo_code_version())
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
