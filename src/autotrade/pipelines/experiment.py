"""Experiment pipeline: Step/Fold/Epoch/Held-out orchestration.

docs/pipeline_design.md. The Pipeline schedules Data, Environment, and Agent
in time order, freezes inputs/outputs at each boundary, and writes the single
experiment ledger. It implements no investment logic and never rewrites
strategy content; it only accepts, freezes, falls back, and records.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrade.environment.artifacts import (
    artifact_hash,
    combined_artifact_hash,
    copy_artifact,
    copy_model_artifacts,
    load_strategy_artifact,
    model_artifact_hash,
)
from autotrade.environment.data.summary import write_agent_data_summary
from autotrade.environment.executor import DockerExecutor
from autotrade.environment.identity import agent_visible_ref as _agent_visible_ref
from autotrade.environment.llm.proxy import LLMProxy
from autotrade.environment.managed_proxy import ManagedProxySession
from autotrade.environment.runtime import chmod_tree, AgentTraceWriter, RunManifest, new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.sandbox import DockerSandbox, LocalSandbox, link_copytree, probe_image_runtime, resolve_image_identity
from autotrade.environment.sandbox_images import (
    SANDBOX_ENVIRONMENT_REQUEST_NAME,
    maybe_rebuild_sandbox_image,
    write_sandbox_environment_example,
)
from autotrade.environment.replay.style import write_style_rollup
from autotrade.environment.step_tree import StepTree
from autotrade.environment.tools import PHASE_FROZEN, BacktestTool, ModificationCheckTool, ToolContext
from autotrade.environment.tools.finish_fold import cleanup_agent_processes

from .agent_views import (
    agent_visible_ledger_record as _agent_visible_ledger_record,
    compact_fold_history as _compact_fold_history,
    metrics as _metrics,
)
from .config import (
    AgentFactory,
    CachingSnapshotProvider,
    ExperimentConfig,
    FoldOutcome,
    FrozenArtifact,
    MetaLearner,
    SnapshotProvider,
)
from .folds import FoldSpec, build_fold_schedule, heldout_periods
from .hitl_state import iter_development_sessions
from .ledger import ExperimentLedger, latest_fold_records
from .meta_schedule import meta_learning_id, meta_record_id


class FrozenArtifactMutatedError(RuntimeError):
    """A frozen artifact's bytes changed where the contract requires identity.

    Distinguished from ordinary diagnostic-eval failures so callers record the
    truth (state_changed_during_test) and terminate after the ledger persists,
    instead of swallowing an integrity breach as a finalize_error."""


class ExperimentPipeline:
    def __init__(
        self,
        config: ExperimentConfig,
        snapshots: SnapshotProvider,
        agent_factory: AgentFactory,
        *,
        proxy: LLMProxy | None = None,
        nl_proxy: LLMProxy | None = None,
        meta_learner: MetaLearner | None = None,
    ) -> None:
        self.config = config
        provider_raw_dir = getattr(snapshots, "raw_dir", None)
        self._raw_dir = Path(provider_raw_dir) if provider_raw_dir is not None else None
        # Identical builds recur constantly (adjacent folds share the decision
        # snapshot anchor; multi-epoch reruns are snapshot-invariant) — cache
        # them and hardlink into each run's sandbox. The key is fully
        # content-derived (parts + provider config + raw-lake generation +
        # format version) and builds are cross-process single-flighted, so the
        # cache is SHARED across experiments under one root: parallel
        # experiments over the same windows reuse each other's builds
        # (measured: 8.1GB and ~6.5min/fold duplicated between two live
        # experiments). The dot-dir stays invisible to the console listing;
        # clear it manually when no experiments run if disk ever demands.
        self.snapshots = CachingSnapshotProvider(snapshots, config.experiment_dir.parent / ".snapshot_cache")
        self.agent_factory = agent_factory
        self.proxy = proxy
        self.nl_proxy = nl_proxy
        self.meta_learner = meta_learner
        self.ledger = ExperimentLedger(config.ledger_path)
        self._active_sandbox_spec = self._restore_active_sandbox_image(config.sandbox_spec)

    @property
    def raw_dir(self) -> Path:
        if self._raw_dir is None:
            raise RuntimeError("snapshot provider does not expose its pinned raw_dir")
        return self._raw_dir

    def _restore_active_sandbox_image(self, base_spec):
        """Resume durability: a successful meta-learning sandbox rebuild updates the
        active image only in-memory. On a fresh process (a fold-only or resumed run)
        reload the most recent good derived image tag from the ledger so later folds
        inherit the extended sandbox instead of silently falling back to the base."""
        if base_spec is None:
            return base_spec
        latest_update: dict[str, object] | None = None
        for record in self.ledger.read("meta_learning"):
            update = record.get("sandbox_image_update")
            if isinstance(update, dict) and update.get("status"):
                latest_update = update
        if latest_update is None:
            return base_spec
        if latest_update.get("status") != "ok" or not latest_update.get("image"):
            # The docs promise "构建失败显式终止，不回退旧环境": resuming on the
            # base image would silently drop meta-declared dependencies.
            raise RuntimeError(
                "the latest meta-learning sandbox image build did not succeed "
                f"(status={latest_update.get('status')!r}); rebuild the derived image "
                "or clear the declaration before resuming"
            )
        latest_image = str(latest_update["image"])
        recorded_id = str(latest_update.get("image_id") or "")
        if recorded_id:
            from autotrade.environment.sandbox import resolve_image_identity

            current_id, _ = resolve_image_identity(latest_image)
            if current_id != recorded_id:
                raise RuntimeError(
                    f"derived sandbox tag {latest_image!r} now resolves to {current_id}, but the "
                    f"ledger recorded {recorded_id}; the mutable tag drifted — rebuild or retag "
                    "before resuming so later folds run the recorded environment"
                )
        if latest_image != base_spec.image:
            return replace(base_spec, image=latest_image)
        return base_spec

    # ---- top-level run ----

    def run(self, trading_days: list[str]) -> dict[str, object]:
        # Full runs are not resumable: freezing into an already-populated experiment
        # raises FileExistsError deep inside _freeze, past the durable-ledger guard.
        # Fail fast up front instead.
        artifacts_root = self.config.experiment_dir / "strategy_artifacts"
        if any(artifacts_root.glob("epoch_*/*")) or self.ledger.read("fold"):
            raise RuntimeError(
                f"experiment {self.config.experiment_id!r} already holds frozen artifacts or fold records; "
                "re-running a full experiment in place is not supported — use a fresh experiment id"
            )
        folds = build_fold_schedule(
            self.config.first_test_period,
            self.config.last_test_period,
            trading_days,
            window_months=self.config.window_months,
            period=self.config.fold_period,
        )
        parent: FrozenArtifact | None = None
        taste_prompt = ""
        epoch_id = ""
        for session in iter_development_sessions(
            self.config.epochs,
            folds,
            meta_enabled=self.meta_learner is not None,
            meta_learning_fold_interval=self.config.meta_learning_fold_interval,
        ):
            epoch_id = session.epoch_id
            if session.kind == "meta_learning":
                parent, taste_prompt = self.run_meta_learning(
                    epoch_id=epoch_id,
                    meta_learning_id=meta_learning_id(epoch_id, session.fold_index),
                    trigger_after_folds=session.fold_index,
                    parent=parent,
                    previous_taste=taste_prompt,
                    visible_fold=session.fold,
                )
            else:
                outcome = self.run_fold(session.fold, epoch_id=epoch_id, parent=parent, taste_prompt=taste_prompt)
                parent = outcome.frozen
        if parent is None:
            raise RuntimeError("experiment produced no frozen strategy artifact")
        heldout = self.run_heldout(parent, trading_days, epoch_id=epoch_id)
        return {"final_strategy_artifact": parent.artifact_id, "heldout_runs": len(heldout)}

    # ---- sandbox helpers ----

    def _install_step_tree(self, paths, parent: FrozenArtifact | None) -> None:
        """Hand the experiment-level step tree to the fold and mark the start node."""
        experiment_tree = self.config.experiment_dir / "steps"
        if experiment_tree.exists():
            link_copytree(experiment_tree, paths.steps)
        tree = StepTree(paths.steps)
        tree.set_position(
            tree.position_for_hash(parent.artifact_hash, parent.model_artifact_hash) if parent else None
        )

    @staticmethod
    def _write_style_rollups(
        results_root: Path, *prefixes: str, only: dict[str, str] | None = None
    ) -> str | None:
        """Aggregate per-window attribution sidecars into ``style_<prefix>.json``.

        Advisory analytics: a rollup failure must not discard a finished
        fold/held-out run, so each prefix is attempted independently and any
        errors are returned for the ledger record instead of raising. ``only``
        maps a prefix to a single window name: valid attempt windows repeat
        the same dates with different strategy versions, so the valid rollup
        must describe the selected window, not a stitch of every attempt."""
        errors: list[str] = []
        for prefix in prefixes:
            try:
                window = (only or {}).get(prefix)
                write_style_rollup(results_root, prefix, windows=(window,) if window else None)
            except Exception as exc:  # noqa: BLE001 - recorded in the ledger by the caller
                errors.append(f"{prefix}: {type(exc).__name__}: {exc}")
        return "; ".join(errors) or None

    def _start_sandbox(
        self, run_id: str, *, kind: str = "fold", gpu_count: int | None = None
    ) -> tuple[LocalSandbox, DockerSandbox | None]:
        sandbox = LocalSandbox(Path(self.config.work_root) / run_id)
        sandbox.prepare_layout()
        if kind == "meta_learning":
            write_sandbox_environment_example(sandbox.paths.workspace)
        spec = (
            self.config.meta_learning_sandbox_spec
            if kind == "meta_learning" and self.config.meta_learning_sandbox_spec is not None
            else self._active_sandbox_spec
        )
        if gpu_count is not None:
            # Per-session HITL override; the "auto" selector still picks the
            # requested number of GPUs by free memory at container start.
            spec = replace(spec, gpu_count=int(gpu_count))
        if self.config.use_docker:
            # Pin the session to the immutable image id before anything reads
            # the tag: a tag reassigned between the probe and container start
            # would otherwise publish a runtime_env for a different image.
            image_id, _ = resolve_image_identity(spec.image)
            spec = replace(spec, image=image_id)
        sandbox.write_runtime_env(
            mode="docker" if self.config.use_docker else "local",
            sandbox_spec=spec if self.config.use_docker else None,
            image_probe=probe_image_runtime(spec.image) if self.config.use_docker else None,
        )
        docker = None
        if self.config.use_docker:
            docker = DockerSandbox(sandbox, spec, labels={"mq.experiment": self.config.experiment_id})
        return sandbox, docker

    @staticmethod
    def _executor_for(docker: DockerSandbox | None, sandbox: LocalSandbox):
        if docker is None:
            return None  # ToolContext defaults to LocalExecutor
        return DockerExecutor(
            docker.container,
            sandbox.paths,
            formal_factory=docker.formal_executor,
            formal_guard_factory=docker.formal_guard,
            formal_seal_factory=docker.retain_pause_until_stop,
        )

    @staticmethod
    def _bind_view(sandbox: LocalSandbox, docker: DockerSandbox | None, view_name: str) -> None:
        if docker is None:
            sandbox.bind_snapshot_view(sandbox.paths.snapshot_views / view_name)
        else:
            docker.bind_snapshot_view(view_name)

    @staticmethod
    def _bind_formal_view(sandbox: LocalSandbox, docker: DockerSandbox | None, view_name: str) -> None:
        view = sandbox.paths.snapshot_views / view_name
        if docker is None:
            sandbox.bind_snapshot_view(view)
        else:
            # Do not copy hidden Test/Held-out input into the directory mounted
            # by the development Agent container.
            sandbox.bind_formal_snapshot_view(view)

    @staticmethod
    def _start_container(docker: DockerSandbox | None, manifest: RunManifest) -> None:
        if docker is None:
            return
        docker.start()
        manifest.update(sandbox_runtime=docker.allocation_record())

    def _start_meta_learning_proxy(self, sandbox: LocalSandbox, manifest: RunManifest) -> ManagedProxySession:
        if not self.config.use_docker:
            session = ManagedProxySession(record={"enabled": False, "status": "local_mode"})
            manifest.update(meta_learning_proxy_runtime=session.record)
            return session
        session = self.config.meta_learning_managed_proxy.start(
            sandbox.paths.root / "runtime" / "managed_proxy"
        )
        manifest.update(meta_learning_proxy_runtime=session.record)
        return session

    def _experiment_parameter_summary(self) -> dict[str, object]:
        return {
            "fold_period": self.config.fold_period,
            "periods": {
                "first_test_period": self.config.first_test_period,
                "last_test_period": self.config.last_test_period,
                "heldout_first_period": self.config.heldout_first_period,
                "heldout_last_period": self.config.heldout_last_period,
            },
            "epochs": self.config.epochs,
            "window_months": self.config.window_months,
            "snapshot_config": self.config.snapshot_config.to_record(),
            "acceptance_rules": self.config.acceptance.to_record(),
            "broker_profile": self.config.broker_profile.to_record(),
            "nl_failure_policy": self.config.nl_failure_policy,
            "step_tree_enabled": self.config.step_tree_enabled,
            "record_failed_attempts": self.config.record_failed_attempts,
            "convergence_start_epoch": self.config.convergence_start_epoch,
            "meta_learning_fold_interval": self.config.meta_learning_fold_interval,
            "max_fold_minutes": self.config.max_fold_minutes,
            "max_steps_per_fold": self.config.max_steps_per_fold,
            "finalize_before_deadline_seconds": self.config.finalize_before_deadline_seconds,
            "per_call_timeout_seconds": self.config.per_call_timeout_seconds,
            "sandbox_spec": self._active_sandbox_spec.to_record(),
            "meta_learning_sandbox_spec": (
                self.config.meta_learning_sandbox_spec.to_record()
                if self.config.meta_learning_sandbox_spec is not None
                else None
            ),
            "meta_learning_managed_proxy": self.config.meta_learning_managed_proxy.to_record(),
        }

    def _replay_config_fields(self) -> dict[str, object]:
        """The replay/execution knobs the Fold and held-out run manifests share, so a
        new knob is added once (these manifests are the Agent-visible PIT contract)."""
        return {
            "per_call_timeout_seconds": self.config.per_call_timeout_seconds,
            "afterhours_decision_time": self.config.afterhours_decision_time,
            "offsession_tick_minutes": self.config.offsession_tick_minutes,
            "intraday_decision_minutes": self.config.intraday_decision_minutes,
            "execution_lag_bars": self.config.execution_lag_bars,
            "decision_max_sim_minutes": self.config.decision_max_sim_minutes,
            "backtest_max_seconds_per_decision": self.config.backtest_max_seconds_per_decision,
            "backtest_max_seconds_per_trading_day": self.config.backtest_max_seconds_per_trading_day,
            "backtest_final_eval_max_seconds_per_decision": self.config.final_eval_max_seconds_per_decision(),
            "backtest_final_eval_max_seconds_per_trading_day": self.config.final_eval_max_seconds_per_trading_day(),
            "timeview_enabled": self.config.timeview_enabled,
            "nl_max_calls_per_decision_day": self.config.nl_max_calls_per_decision_day,
            "nl_max_calls_per_backtest": self.config.nl_max_calls_per_backtest,
        }

    # ---- fold ----

    def prefetch_fold_data(self, fold: FoldSpec) -> dict[str, dict[str, object]]:
        """Warm only immutable data-cache entries; no sandbox or container."""
        return self.snapshots.prefetch_fold(fold)

    def run_fold(
        self,
        fold: FoldSpec,
        *,
        epoch_id: str,
        parent: FrozenArtifact | None,
        taste_prompt: str = "",
        fold_directive: str = "",
        system_prompt_override: str = "",
        rerun_id: str | None = None,
        sandbox_gpu_count: int | None = None,
        step_gate_hook=None,
        user_question_hook=None,
        environment_progress_hook=None,
    ) -> FoldOutcome:
        run_id = new_id("run")
        try:
            return self._run_fold_impl(
                fold,
                run_id=run_id,
                epoch_id=epoch_id,
                parent=parent,
                taste_prompt=taste_prompt,
                fold_directive=fold_directive,
                system_prompt_override=system_prompt_override,
                rerun_id=rerun_id,
                sandbox_gpu_count=sandbox_gpu_count,
                step_gate_hook=step_gate_hook,
                user_question_hook=user_question_hook,
                environment_progress_hook=environment_progress_hook,
            )
        except Exception as exc:
            self._record_attempt_failure(epoch_id=epoch_id, fold_id=fold.fold_id, run_id=run_id, exc=exc)
            raise

    def _run_fold_impl(
        self,
        fold: FoldSpec,
        *,
        run_id: str,
        epoch_id: str,
        parent: FrozenArtifact | None,
        taste_prompt: str = "",
        fold_directive: str = "",
        system_prompt_override: str = "",
        rerun_id: str | None = None,
        sandbox_gpu_count: int | None = None,
        step_gate_hook=None,
        user_question_hook=None,
        environment_progress_hook=None,
    ) -> FoldOutcome:
        run_started = time.monotonic()
        sandbox, docker = self._start_sandbox(run_id, gpu_count=sandbox_gpu_count)
        paths = sandbox.paths

        valid_view = paths.snapshot_views / "valid_decision_input"
        test_view = paths.snapshot_views / "test_decision_input"
        valid_snapshot = self.snapshots.decision_snapshot(fold.valid_decision_time, valid_view)
        test_snapshot = self.snapshots.decision_snapshot(fold.test_decision_time, test_view)
        sandbox.install_replay_slot("train", valid_view)
        valid_replay = self.snapshots.replay_slot(
            fold.validation_start, fold.validation_end, paths.valid, label="valid",
            available_from=fold.valid_decision_time,
        )
        test_replay = self.snapshots.replay_slot(
            fold.test_start, fold.test_end, paths.test, label="test",
            available_from=fold.test_decision_time,
        )
        _assert_single_raw_generation(
            valid_decision_input=valid_snapshot,
            test_decision_input=test_snapshot,
            valid_replay=valid_replay,
            test_replay=test_replay,
        )
        write_agent_data_summary(
            paths.data_summary,
            kind="fold",
            # Agent-visible: opaque the fold id so the calendar period (e.g.
            # 2022Q1) cannot leak through data_summary.json. Host correlation
            # uses run_id + host_run_manifest.json. Same projection as the
            # run_manifest and ledger views.
            fold_id=_agent_visible_ref(fold.fold_id, prefix="fold_ref"),
            views={
                "snapshot": (valid_view, "/mnt/snapshot"),
                "train": (paths.train, "/mnt/snapshots/train"),
                "valid": (paths.valid, "/mnt/snapshots/valid"),
            },
        )

        is_initial = sandbox.install_strategy_artifact(
            parent.path if parent else None,
            Path(self.config.template_dir),
            source_model_root=parent.model_path if parent else None,
        )
        if self.config.step_tree_enabled:
            self._install_step_tree(paths, parent)
        constraints = replace(self.config.step_constraints, is_initial_artifact=is_initial).for_epoch(
            _epoch_index(epoch_id)
        )
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.config.max_fold_minutes)
        conversation_id = new_id("conv")
        manifest = RunManifest.create(
            paths.run_manifest,
            {
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold.fold_id,
                "run_id": run_id,
                "conversation_id": conversation_id,
                "kind": "fold",
                "fold": fold.to_record(),
                "runtime_env_ref": "/mnt/artifacts/runtime_env.json",
                "data_summary_ref": "/mnt/artifacts/data_summary.json",
                "fold_period": self.config.fold_period,
                "snapshot_config": self.config.snapshot_config.to_record(),
                "valid_decision_time": fold.valid_decision_time.isoformat(),
                "test_decision_time": fold.test_decision_time.isoformat(),
                "snapshots": {
                    "train_snapshot": {
                        **_snapshot_ref(valid_snapshot),
                        "alias_of": "valid_decision_input",
                    },
                    "valid_decision_input": _snapshot_ref(valid_snapshot),
                    "test_decision_input": _snapshot_ref(test_snapshot),
                    "valid_replay": _snapshot_ref(valid_replay),
                    "test_replay": _snapshot_ref(test_replay),
                },
                "is_initial_artifact": is_initial,
                "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                "parent_strategy_artifact_hash": parent.artifact_hash if parent else None,
                "parent_model_artifact_hash": parent.model_artifact_hash if parent else None,
                "template_ref": "agent_output_template",
                "initial_template_hash": artifact_hash(paths.parent_output) if is_initial else None,
                "modification_constraints": constraints.to_record(),
                "acceptance_rules": self.config.acceptance.to_record(),
                "broker_profile": self.config.broker_profile.to_record(),
                "short_inventory_mode": self.config.broker_profile.short_inventory_mode,
                "nl_failure_policy": self.config.nl_failure_policy,
                "step_tree_enabled": self.config.step_tree_enabled,
                "record_failed_attempts": self.config.record_failed_attempts,
                "epoch_index": _epoch_index(epoch_id),
                "phase": "convergence" if _epoch_index(epoch_id) >= self.config.convergence_start_epoch else "exploration",
                "max_steps": self.config.max_steps_per_fold,
                "max_backtests_per_fold": self.config.max_backtests_per_fold,
                "fold_deadline_at": deadline.isoformat(),
                "finalize_before_deadline_seconds": self.config.finalize_before_deadline_seconds,
                **self._replay_config_fields(),
                "sandbox_spec": self._active_sandbox_spec.to_record(),
                "taste_prompt": taste_prompt,
                # Experiment-level direction from creation; every ordinary Fold
                # receives it independently of optional per-session guidance.
                "fold_exploration_directive": self.config.fold_exploration_directive.strip(),
                # Researcher-injected per-fold direction (HITL). Prompt-level input
                # like taste_prompt: recorded for audit, never hashed into artifacts.
                "fold_directive": fold_directive.strip(),
                # HITL: verbatim researcher-edited system prompt (replaces the
                # assembled one, incl. the runtime facts block) and the re-run tag.
                "system_prompt_override": system_prompt_override.strip(),
                "rerun_id": rerun_id,
            },
        )
        trace = AgentTraceWriter(
            paths.agent_trace,
            ids={
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                # Stamped on every agent_trace event (agent-readable); opaque it.
                "fold_id": _agent_visible_ref(fold.fold_id, prefix="fold_ref"),
                "run_id": run_id,
                "conversation_id": conversation_id,
            },
        )
        test_summary: dict[str, object] | None = None
        test_eval_error: Exception | None = None
        try:
            # Inside the try: a partially-started container (or a failure right
            # after start) is still torn down by the finally's docker.stop().
            self._start_container(docker, manifest)
            ctx = ToolContext(
                paths=paths,
                manifest=manifest,
                trace=trace,
                proxy=self.proxy,
                nl_proxy=self.nl_proxy,
                executor=self._executor_for(docker, sandbox),
            )
            self._bind_view(sandbox, docker, "valid_decision_input")

            if step_gate_hook is not None:
                # Step-level HITL: the runner calls this after every formal
                # validation backtest (see AgentSessionRunner._do_backtest).
                ctx.extra["step_gate_hook"] = step_gate_hook
            if user_question_hook is not None:
                # ask_user tool bridge (see AgentSessionRunner._do_ask_user).
                ctx.extra["user_question_hook"] = user_question_hook
            if environment_progress_hook is not None:
                # Host-only observability. Frozen test progress must never enter
                # the Agent-readable trace or strategy surface.
                ctx.extra["environment_progress_hook"] = environment_progress_hook
                ctx.extra["environment_replay_stage"] = "frozen_test"
            agent = self.agent_factory(ctx, fold, dict(manifest.data))
            session_summary = agent.run()
            researcher_wait_seconds = _researcher_wait_seconds(session_summary)

            if environment_progress_hook is not None:
                environment_progress_hook("acceptance", None)

            if not ctx.write_locked:
                # Deadline / call-limit exits skip finish_fold's quiesce: kill any
                # agent background process and read-lock the artifacts BEFORE
                # acceptance hashes them, or a surviving writer could mutate the
                # working copy between verification and the freeze copy.
                cleanup_agent_processes(ctx)
                sandbox.lock_agent_output()
                ctx.write_locked = True
            frozen, fold_status, accept_reasons, accept_warnings, selected = self._accept_or_fallback(
                ctx, fold, epoch_id=epoch_id, run_id=run_id, parent=parent, is_initial=is_initial
            )
            sandbox.lock_agent_output()
            # The strategy is now frozen on disk. The out-of-sample test_000 eval is
            # diagnostic only — acceptance is decided on validation (H2: finals are
            # not an acceptance gate) — so a failure here must NOT discard the
            # accepted strategy or abort the experiment. Record it and fall through
            # to a durable ledger entry (C1).
            try:
                if environment_progress_hook is not None:
                    environment_progress_hook("frozen_test", None)
                test_summary = self._frozen_test_eval(ctx, sandbox, docker, frozen, result_name="test_000")
            except Exception as exc:  # noqa: BLE001 - recorded in the ledger below
                test_eval_error = exc
        finally:
            if docker is not None:
                docker.stop()
        if environment_progress_hook is not None:
            environment_progress_hook("persistence", None)
        selected_window = str((selected or {}).get("result_name") or "") or None
        style_rollup_error = self._write_style_rollups(
            paths.results, "valid", "test", only={"valid": selected_window} if selected_window else None
        )
        if self.config.step_tree_enabled and paths.steps.exists():
            link_copytree(paths.steps, self.config.experiment_dir / "steps")
        steps = self._step_summaries(manifest, selected)

        def fold_record(collected: Path | None, collect_error: Exception | None) -> dict[str, object]:
            # A frozen strategy must never be left without a durable record;
            # a recorded (not raised) test_eval_error keeps the fold usable.
            finalize_error = collect_error or test_eval_error
            run_manifest_ref = (
                str(collected / "run_manifest.json") if collected is not None else str(paths.run_manifest)
            )
            return {
                    "record_type": "fold",
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": fold.fold_id,
                    "run_id": run_id,
                    # Sandbox start -> record time excluding external researcher
                    # holds; backtests and all Environment work remain included.
                    "run_wall_seconds": round(
                        max(0.0, time.monotonic() - run_started - researcher_wait_seconds), 1
                    ),
                    "researcher_wait_seconds": round(researcher_wait_seconds, 1),
                    **fold.to_record(),
                    "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                    "fold_exploration_directive": self.config.fold_exploration_directive.strip() or None,
                    "fold_directive": fold_directive.strip() or None,
                    "system_prompt_overridden": bool(system_prompt_override.strip()),
                    "rerun_id": rerun_id,
                    "finish_reason": session_summary.get("finish_status"),
                    "fold_status": fold_status,
                    "accept_reasons": accept_reasons,
                    "accept_warnings": accept_warnings,
                    "selected_step_id": self._step_id_for(manifest, selected) if selected else None,
                    "steps": steps,
                    "frozen_strategy_artifact_id": frozen.artifact_id,
                    "frozen_strategy_artifact_hash": frozen.artifact_hash,
                    "frozen_model_artifact_hash": frozen.model_artifact_hash,
                    "frozen_combined_artifact_hash": combined_artifact_hash(
                        frozen.artifact_hash, frozen.model_artifact_hash
                    ),
                    "frozen_strategy_artifact_path": str(frozen.path),
                    "frozen_model_artifact_path": str(frozen.model_path) if frozen.model_path else None,
                    "validation_result": _metrics(selected),
                    "test_result": _metrics(test_summary),
                    "state_changed_during_test": isinstance(test_eval_error, FrozenArtifactMutatedError),
                    "run_manifest_ref": run_manifest_ref,
                    "snapshot_ids": {key: ref["snapshot_id"] for key, ref in manifest.data["snapshots"].items()},
                    "style_rollup_error": style_rollup_error,
                    "finalize_error": (
                        f"{type(finalize_error).__name__}: {finalize_error}" if finalize_error is not None else None
                    ),
            }

        self._finalize_run(sandbox, run_id, record_builder=fold_record)
        if isinstance(test_eval_error, FrozenArtifactMutatedError):
            # Integrity breach (docs §4.3 terminate class): the truth is on the
            # ledger; do not continue the experiment on a mutated frozen artifact.
            raise test_eval_error
        return FoldOutcome(
            fold_id=fold.fold_id,
            run_id=run_id,
            fold_status=fold_status,
            frozen=frozen,
            validation_summary=selected,
            test_summary=test_summary,
        )

    # ---- scheduled meta learning + regularization ----

    def run_meta_learning(
        self,
        *,
        epoch_id: str,
        meta_learning_id: str | None = None,
        trigger_after_folds: int = 0,
        parent: FrozenArtifact | None,
        previous_taste: str = "",
        visible_fold: FoldSpec | None = None,
        directive_override: str | None = None,
        system_prompt_override: str = "",
        user_question_hook=None,
        agent_ready_hook=None,
        environment_progress_hook=None,
    ) -> tuple[FrozenArtifact | None, str]:
        if self.meta_learner is None:
            raise RuntimeError("no meta learner configured")
        meta_id = str(meta_learning_id or epoch_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", meta_id):
            raise ValueError(f"invalid meta_learning_id: {meta_id!r}")
        if trigger_after_folds < 0:
            raise ValueError("trigger_after_folds must be non-negative")
        run_id = new_id("run")
        try:
            return self._run_meta_learning_impl(
                run_id=run_id,
                epoch_id=epoch_id,
                meta_learning_id=meta_id,
                trigger_after_folds=trigger_after_folds,
                parent=parent,
                previous_taste=previous_taste,
                visible_fold=visible_fold,
                directive_override=directive_override,
                system_prompt_override=system_prompt_override,
                user_question_hook=user_question_hook,
                agent_ready_hook=agent_ready_hook,
                environment_progress_hook=environment_progress_hook,
            )
        except Exception as exc:
            self._record_attempt_failure(
                epoch_id=epoch_id, fold_id=f"{meta_id}_meta_learning", run_id=run_id, exc=exc
            )
            raise

    def _run_meta_learning_impl(
        self,
        *,
        run_id: str,
        epoch_id: str,
        meta_learning_id: str,
        trigger_after_folds: int,
        parent: FrozenArtifact | None,
        previous_taste: str = "",
        visible_fold: FoldSpec | None = None,
        directive_override: str | None = None,
        system_prompt_override: str = "",
        user_question_hook=None,
        agent_ready_hook=None,
        environment_progress_hook=None,
    ) -> tuple[FrozenArtifact | None, str]:
        run_started = time.monotonic()
        meta_fold_id = f"{meta_learning_id}_meta_learning"
        sandbox, docker = self._start_sandbox(run_id, kind="meta_learning")
        paths = sandbox.paths
        has_parent = parent is not None
        if has_parent:
            sandbox.install_strategy_artifact(
                parent.path, Path(self.config.template_dir), source_model_root=parent.model_path
            )
        else:
            sandbox.install_strategy_artifact(None, Path(self.config.template_dir))
        if self.config.step_tree_enabled:
            self._install_step_tree(paths, parent)
        visible_snapshot = None
        visible_replay = None
        if visible_fold is not None:
            visible_view = paths.snapshot_views / "valid_decision_input"
            visible_snapshot = self.snapshots.decision_snapshot(visible_fold.valid_decision_time, visible_view)
            sandbox.install_replay_slot("train", visible_view)
            visible_replay = self.snapshots.replay_slot(
                visible_fold.validation_start,
                visible_fold.validation_end,
                paths.valid,
                label="valid",
                available_from=visible_fold.valid_decision_time,
            )
            _assert_single_raw_generation(
                valid_decision_input=visible_snapshot,
                valid_replay=visible_replay,
            )
            sandbox.bind_snapshot_view(visible_view)
        write_agent_data_summary(
            paths.data_summary,
            kind="meta_learning",
            fold_id=_agent_visible_ref(meta_fold_id, prefix="fold_ref"),
            views=(
                {
                    "snapshot": (paths.current_snapshot, "/mnt/snapshot"),
                    "train": (paths.train, "/mnt/snapshots/train"),
                    "valid": (paths.valid, "/mnt/snapshots/valid"),
                }
                if visible_snapshot is not None
                else {}
            ),
        )
        # Development history and prior meta-learning memory are explicit inputs.
        history_path = paths.workspace / "development_history.json"
        history_path.write_text(
            json.dumps(self._development_history(previous_taste), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        # Compact Agent-visible development records. Held-out is excluded; Fold
        # Test results pass only through the explicit metric whitelist below.
        ledger_full_path = paths.workspace / "experiment_ledger_full.jsonl"
        ledger_records = [
            _agent_visible_ledger_record(record, include_frozen_test_metrics=True)
            for record in self.ledger.read()
            if record.get("record_type") != "heldout"
        ]
        ledger_full_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                for record in ledger_records
            ),
            encoding="utf-8",
        )
        # Keep the latest prior Meta dialogue/tool trace per recent Epoch. This
        # includes the immediate same-Epoch periodic predecessor without making
        # raw memory grow with every interval trigger.
        memory_path = paths.workspace / "meta_learning_memory.jsonl"
        memory_path.write_text(self._prior_meta_learning_logs(meta_learning_id), encoding="utf-8")
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.config.max_fold_minutes)
        fold_id = meta_fold_id
        manifest = RunManifest.create(
            paths.run_manifest,
            {
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "meta_learning_id": meta_learning_id,
                "trigger_after_folds": trigger_after_folds,
                "fold_id": fold_id,
                "run_id": run_id,
                "conversation_id": new_id("conv"),
                "kind": "meta_learning",
                "runtime_env_ref": "/mnt/artifacts/runtime_env.json",
                "data_summary_ref": "/mnt/artifacts/data_summary.json",
                "experiment_parameters": self._experiment_parameter_summary(),
                "meta_learning_visible_fold": visible_fold.to_record() if visible_fold is not None else None,
                "valid_decision_time": visible_fold.valid_decision_time.isoformat() if visible_fold is not None else None,
                "snapshots": (
                    {
                        "train_snapshot": {
                            **_snapshot_ref(visible_snapshot),
                            "alias_of": "valid_decision_input",
                        },
                        "valid_decision_input": _snapshot_ref(visible_snapshot),
                        "valid_replay": _snapshot_ref(visible_replay),
                    }
                    if visible_snapshot is not None
                    else {}
                ),
                "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                "parent_strategy_artifact_hash": parent.artifact_hash if parent else None,
                "parent_model_artifact_hash": parent.model_artifact_hash if parent else None,
                "template_ref": "agent_output_template",
                "initial_template_hash": artifact_hash(paths.parent_output) if not has_parent else None,
                "modification_constraints": replace(
                    self.config.regularization_constraints, is_initial_artifact=not has_parent
                ).to_record(),
                **self._replay_config_fields(),
                "fold_deadline_at": deadline.isoformat(),
                # Agent-facing manifest: expose sandbox mount paths, not host paths.
                # The raw experiment ledger dir is not mounted; the agent reads the
                # mounted experiment_ledger_full instead.
                "development_inputs": {
                    "experiment_ledger_full": f"/mnt/agent/workspace/{ledger_full_path.name}",
                    "development_history": f"/mnt/agent/workspace/{history_path.name}",
                    "meta_learning_memory": f"/mnt/agent/workspace/{memory_path.name}",
                    "previous_taste": bool(previous_taste.strip()),
                },
                "taste_output": "/mnt/agent/workspace/taste.md",
                # HITL runs may supply a per-epoch directive; the config default
                # stays the experiment-level directive from the CLI.
                "meta_learning_directive": (
                    self.config.meta_learning_directive if directive_override is None else directive_override
                ).strip(),
                "fold_exploration_directive": self.config.fold_exploration_directive.strip(),
                "system_prompt_override": system_prompt_override.strip(),
                "web_search_engines": [],
            },
        )
        trace = AgentTraceWriter(
            paths.agent_trace,
            ids={
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": _agent_visible_ref(fold_id, prefix="fold_ref"),
                "run_id": run_id,
                "conversation_id": str(manifest.require("conversation_id")),
            },
        )
        managed_proxy = self._start_meta_learning_proxy(sandbox, manifest)
        try:
            with managed_proxy.applied_to_environ():
                self._start_container(docker, manifest)
            ctx = ToolContext(
                paths=paths,
                manifest=manifest,
                trace=trace,
                proxy=self.proxy,
                nl_proxy=self.nl_proxy,
                executor=self._executor_for(docker, sandbox),
            )
            ctx.extra["allow_backtest"] = False
            if user_question_hook is not None:
                ctx.extra["user_question_hook"] = user_question_hook
            if managed_proxy.env:
                ctx.extra["web_fetch_proxy_env"] = dict(managed_proxy.env)
            if agent_ready_hook is not None:
                agent_ready_hook()

            session_summary = self.meta_learner(ctx)
            if session_summary is None:
                session_summary = ctx.extra.get("agent_session_summary")
            self._validate_meta_learning_session(session_summary)
            researcher_wait_seconds = _researcher_wait_seconds(session_summary)
            if environment_progress_hook is not None:
                environment_progress_hook("meta_finalize", None)
            taste_current = _read_text(paths.workspace / "taste.md").strip()
            if not taste_current:
                raise RuntimeError("meta-learning completed without writing non-empty taste.md")
            # Runs after the agent session returns (post-session_end), so tag it as
            # pipeline finalization rather than an agent action in the trace.
            check = ModificationCheckTool(ctx).run(phase="pipeline_finalize")
        finally:
            if docker is not None:
                docker.stop()
            managed_proxy.stop()
        taste = _read_text(paths.workspace / "taste.md").strip()
        status = "taste_only"
        frozen = parent
        changed = _check_has_changes(check)
        if has_parent and check.get("allowed_to_backtest") and changed:
            load_strategy_artifact(paths.agent_output)  # read-only contract check; no return backtest
            frozen = self._freeze(
                paths.agent_output,
                paths.model_artifacts,
                epoch_id=epoch_id,
                artifact_id=f"strategy_{meta_learning_id}_meta_learning",
                parent=parent,
                fold_id=fold_id,
                run_id=run_id,
                step="meta_learning",
                requires_validation=True,
            )
            status = "meta_regularized"
        elif has_parent and check.get("allowed_to_backtest"):
            status = "taste_only_kept_parent"
        elif has_parent:
            status = "rejected_kept_parent"
        sandbox_image_error: RuntimeError | None = None
        if environment_progress_hook is not None:
            environment_progress_hook("environment_update", None)
        try:
            sandbox_image_update, self._active_sandbox_spec = maybe_rebuild_sandbox_image(
                paths.workspace / SANDBOX_ENVIRONMENT_REQUEST_NAME,
                base_spec=self._active_sandbox_spec,
                experiment_id=self.config.experiment_id,
                epoch_id=meta_learning_id,
                experiment_dir=self.config.experiment_dir,
                manifest=manifest,
                use_docker=self.config.use_docker,
                rebuild_enabled=self.config.meta_sandbox_rebuild_enabled,
                timeout_seconds=self.config.meta_sandbox_rebuild_timeout_seconds,
                image_keep=self.config.meta_sandbox_image_keep,
            )
        except RuntimeError as exc:
            sandbox_image_update = manifest.get("sandbox_image_update")
            sandbox_image_error = exc
        meta_dir = self.config.experiment_dir / "meta_learning" / meta_learning_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        if taste:
            (meta_dir / "taste.md").write_text(taste + "\n", encoding="utf-8")

        if environment_progress_hook is not None:
            environment_progress_hook("persistence", None)

        def meta_record(collected: Path | None, collect_error: Exception | None) -> dict[str, object]:
            agent_trace_ref = (collected / "agent_trace.jsonl") if collected is not None else paths.agent_trace
            finalize_error = collect_error or sandbox_image_error
            return {
                    "record_type": "meta_learning",
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "meta_learning_id": meta_learning_id,
                    "trigger_after_folds": trigger_after_folds,
                    "fold_id": fold_id,
                    "run_id": run_id,
                    # Same boundary as a regular Fold, excluding external
                    # researcher holds but retaining all Environment work.
                    "run_wall_seconds": round(
                        max(0.0, time.monotonic() - run_started - researcher_wait_seconds), 1
                    ),
                    "researcher_wait_seconds": round(researcher_wait_seconds, 1),
                    "status": status,
                    "modification_check": {
                        k: check.get(k)
                        for k in (
                            "allowed_to_backtest",
                            "reasons",
                            "artifact_hash",
                            "model_artifact_hash",
                            "combined_artifact_hash",
                        )
                    },
                    "frozen_strategy_artifact_id": frozen.artifact_id if frozen else None,
                    "frozen_strategy_artifact_hash": frozen.artifact_hash if frozen else None,
                    "frozen_model_artifact_hash": frozen.model_artifact_hash if frozen else None,
                    "taste_path": str(meta_dir / "taste.md") if taste else None,
                    "taste_chars": len(taste),
                    "agent_session_summary": session_summary if isinstance(session_summary, dict) else None,
                    "agent_trace_ref": str(agent_trace_ref) if agent_trace_ref.exists() else None,
                    "meta_learning_directive": manifest.get("meta_learning_directive"),
                    "fold_exploration_directive": manifest.get("fold_exploration_directive"),
                    "system_prompt_overridden": bool(manifest.get("system_prompt_override")),
                    "web_search_engines": manifest.get("web_search_engines"),
                    "sandbox_image_update": sandbox_image_update,
                    "finalize_error": (
                        f"{type(finalize_error).__name__}: {finalize_error}" if finalize_error is not None else None
                    ),
            }

        # Collection failure outranks the image-rebuild failure; both re-raise
        # only after the meta record is durable.
        self._finalize_run(sandbox, run_id, record_builder=meta_record, raise_after=sandbox_image_error)
        return frozen, taste

    @staticmethod
    def _validate_meta_learning_session(summary: object) -> None:
        """Enforce the Runner contract when a real session summary is available.

        Hand-written test or research hooks may return ``None``; the production
        CLI meta learner returns the AgentSessionRunner summary and must have
        reached the explicit ``done`` state before Pipeline accepts Taste or
        regularization changes.
        """
        if summary is None:
            return
        if not isinstance(summary, dict):
            raise RuntimeError(f"meta-learning returned invalid session summary type: {type(summary).__name__}")
        if summary.get("finish_status") != "meta_learning_done":
            raise RuntimeError(f"meta-learning did not finish with done: {summary.get('finish_status')}")

    def _development_history(self, previous_taste: str) -> dict[str, object]:
        folds = list(latest_fold_records(self.ledger.read("fold")).values())
        # Doctrine-collapse tell: lzp-test21 epoch_003 froze 13 folds onto 2
        # distinct strategy hashes under a "convergence" taste. A later meta
        # session must see the prior epochs' hash diversity to notice it.
        by_epoch: dict[str, list[object]] = {}
        for record in folds:
            by_epoch.setdefault(str(record.get("epoch_id")), []).append(
                record.get("frozen_combined_artifact_hash")
            )
        fold_hash_diversity = {
            epoch: {
                "folds": len(hashes),
                "distinct_strategy_hashes": len({h for h in hashes if h}),
            }
            for epoch, hashes in sorted(by_epoch.items())
        }
        return {
            "evaluation_contract": {
                "validation": "Fold selection and iteration evidence",
                "frozen_test": "compact completed-Fold metrics are adaptive meta-development feedback",
                "heldout": "never visible; sole final untouched evaluation",
            },
            "fold_backtest_summaries": [
                _compact_fold_history(record, include_frozen_test_metrics=True) for record in folds
            ],
            "fold_hash_diversity": fold_hash_diversity,
            "meta_learning": [
                _agent_visible_ledger_record(record, include_frozen_test_metrics=True)
                for record in self.ledger.read("meta_learning")
            ],
            "previous_taste": previous_taste,
        }

    def _prior_meta_learning_logs(self, current_meta_learning_id: str) -> str:
        """Latest prior Meta trace from each of the most recent N Epochs.

        Periodic sessions in the current Epoch are eligible, so the immediate
        predecessor remains visible without allowing raw-memory growth to
        multiply by the number of interval triggers.
        """
        chunks: list[str] = []
        keep = max(0, self.config.meta_memory_max_epochs)
        if not keep:
            return ""
        latest_by_epoch: dict[str, dict[str, object]] = {}
        epoch_order: list[str] = []
        for record in self.ledger.read("meta_learning"):
            if meta_record_id(record) == current_meta_learning_id:
                continue
            epoch = str(record.get("epoch_id") or "")
            if epoch not in latest_by_epoch:
                epoch_order.append(epoch)
            latest_by_epoch[epoch] = record
        records = [latest_by_epoch[epoch] for epoch in epoch_order[-keep:]]
        for record in records:
            trace = self._meta_learning_trace_ref(record)
            if not trace.exists():
                continue
            text = trace.read_text(encoding="utf-8")
            if text.strip():
                chunks.append(text if text.endswith("\n") else text + "\n")
        return "".join(chunks)

    def _meta_learning_trace_ref(self, record: dict[str, object]) -> Path:
        ref = record.get("agent_trace_ref")
        if ref:
            return Path(str(ref))
        run_id = record.get("run_id")
        if run_id:
            return self.config.experiment_dir / "artifacts" / str(run_id) / "agent_trace.jsonl"
        return Path("__missing_meta_learning_agent_trace__")

    # ---- held-out ----

    def run_heldout(
        self,
        final: FrozenArtifact,
        trading_days: list[str],
        *,
        epoch_id: str,
        skip_labels: frozenset[str] | set[str] | None = None,
        environment_progress_hook=None,
    ) -> list[dict[str, object]]:
        attempt: dict[str, str] = {}
        try:
            return self._run_heldout_impl(
                final,
                trading_days,
                epoch_id=epoch_id,
                skip_labels=skip_labels,
                attempt=attempt,
                environment_progress_hook=environment_progress_hook,
            )
        except Exception as exc:
            if attempt:
                self._record_attempt_failure(
                    epoch_id=epoch_id, fold_id=attempt["fold_id"], run_id=attempt["run_id"], exc=exc
                )
            raise

    def _run_heldout_impl(
        self,
        final: FrozenArtifact,
        trading_days: list[str],
        *,
        epoch_id: str,
        skip_labels: frozenset[str] | set[str] | None,
        attempt: dict[str, str],
        environment_progress_hook=None,
    ) -> list[dict[str, object]]:
        periods = heldout_periods(
            self.config.heldout_first_period,
            self.config.heldout_last_period,
            trading_days,
            period=self.config.fold_period,
        )
        summaries: list[dict[str, object]] = []
        for index, period in enumerate(periods):
            # Interactive resume: periods that already hold a heldout ledger record
            # are skipped so a re-run cannot append duplicate records. The enumerate
            # index stays label-stable for result_name.
            if skip_labels and str(period["label"]) in skip_labels:
                continue
            run_id = new_id("run")
            attempt.update(run_id=run_id, fold_id=f"heldout_{period['label']}")
            sandbox, docker = self._start_sandbox(run_id)
            paths = sandbox.paths
            copy_artifact(final.path, paths.parent_output)
            copy_artifact(final.path, paths.agent_output)
            copy_model_artifacts(final.model_path, paths.parent_model_artifacts)
            copy_model_artifacts(final.model_path, paths.model_artifacts)
            sandbox.lock_agent_output()
            test_view = paths.snapshot_views / "test_decision_input"
            decision_time = period["decision_time"]
            snapshot = self.snapshots.decision_snapshot(decision_time, test_view)
            replay = self.snapshots.replay_slot(
                str(period["start"]), str(period["end"]), paths.test, label="heldout",
                available_from=decision_time,
            )
            _assert_single_raw_generation(
                test_decision_input=snapshot,
                test_replay=replay,
            )
            fold_id = f"heldout_{period['label']}"
            manifest = RunManifest.create(
                paths.run_manifest,
                {
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": fold_id,
                    "run_id": run_id,
                    "conversation_id": new_id("conv"),
                    "kind": "heldout",
                    "runtime_env_ref": "/mnt/artifacts/runtime_env.json",
                    "fold_period": self.config.fold_period,
                    "snapshot_config": self.config.snapshot_config.to_record(),
                    "test_decision_time": decision_time.isoformat(),
                    "snapshots": {
                        "test_decision_input": _snapshot_ref(snapshot),
                        "test_replay": _snapshot_ref(replay),
                    },
                    "frozen_strategy_artifact_hash": final.artifact_hash,
                    "frozen_model_artifact_hash": final.model_artifact_hash,
                    "broker_profile": self.config.broker_profile.to_record(),
                    "short_inventory_mode": self.config.broker_profile.short_inventory_mode,
                    "nl_failure_policy": self.config.nl_failure_policy,
                    **self._replay_config_fields(),
                    "sandbox_spec": self._active_sandbox_spec.to_record(),
                },
            )
            trace = AgentTraceWriter(
                paths.agent_trace,
                ids={
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": _agent_visible_ref(fold_id, prefix="fold_ref"),
                    "run_id": run_id,
                    "conversation_id": str(manifest.require("conversation_id")),
                },
            )
            try:
                self._start_container(docker, manifest)
                ctx = ToolContext(
                    paths=paths,
                    manifest=manifest,
                    trace=trace,
                    proxy=self.proxy,
                    nl_proxy=self.nl_proxy,
                    executor=self._executor_for(docker, sandbox),
                    phase=PHASE_FROZEN,
                    write_locked=True,
                )
                if environment_progress_hook is not None:
                    ctx.extra["environment_progress_hook"] = environment_progress_hook
                    ctx.extra["environment_replay_stage"] = "heldout"
                self._bind_formal_view(sandbox, docker, "test_decision_input")
                summary = BacktestTool(ctx).run(mode="frozen_eval", result_name=f"heldout_{index:03d}")
            finally:
                if docker is not None:
                    docker.stop()
            if artifact_hash(paths.agent_output) != final.artifact_hash:
                raise RuntimeError("held-out run modified the frozen strategy artifact")
            if model_artifact_hash(paths.model_artifacts) != final.model_artifact_hash:
                raise RuntimeError("held-out run modified the frozen model artifacts")
            style_rollup_error = self._write_style_rollups(paths.results, "heldout")
            def heldout_record(collected: Path | None, collect_error: Exception | None) -> dict[str, object]:
                return {
                        "record_type": "heldout",
                        "experiment_id": self.config.experiment_id,
                        "epoch_id": epoch_id,
                        "fold_id": fold_id,
                        "run_id": run_id,
                        "period": {"start": period["start"], "end": period["end"]},
                        "strategy_artifact_id": final.artifact_id,
                        "strategy_artifact_hash": final.artifact_hash,
                        "model_artifact_hash": final.model_artifact_hash,
                        "test_result": _metrics(summary),
                        "style_rollup_error": style_rollup_error,
                        "finalize_error": (
                            f"{type(collect_error).__name__}: {collect_error}" if collect_error is not None else None
                        ),
                }

            self._finalize_run(sandbox, run_id, record_builder=heldout_record)
            summaries.append(summary)
        return summaries

    # ---- internals ----

    def _finalize_run(
        self,
        sandbox,
        run_id: str,
        *,
        record_builder,
        raise_after: Exception | None = None,
    ):
        """Collect artifacts, ALWAYS append the ledger record, then fail-fast.

        The ordering is the durability invariant shared by fold / meta-learning /
        held-out finalization: a finished run's record must survive even when
        artifact collection (or an earlier finalize step, passed as
        ``raise_after``) failed. ``record_builder(collected, collect_error)``
        returns the kind-specific ledger dict. A collection error outranks
        ``raise_after``; both re-raise only after the record is durable."""
        collect_error: Exception | None = None
        collected: Path | None = None
        try:
            collected = sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
        except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
            collect_error = exc
        self.ledger.append(record_builder(collected, collect_error))
        if collect_error is not None:
            raise collect_error
        if raise_after is not None:
            raise raise_after
        return collected

    def _record_attempt_failure(self, *, epoch_id: str, fold_id: str, run_id: str, exc: Exception) -> None:
        """Append a permanent ``attempt_failed`` ledger record when a run throws
        before its success record, so recovery can distinguish a failed attempt
        from a never-started one and the error evidence survives. Best-effort:
        a failing append must never mask the original exception."""
        try:
            self.ledger.append(
                {
                    "record_type": "attempt_failed",
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": fold_id,
                    "run_id": run_id,
                    "error_type": type(exc).__name__,
                    "error": sanitize_for_log(str(exc))[:2000],
                    "trace": sanitize_for_log("".join(traceback.format_exception(exc)))[-4000:],
                }
            )
        except Exception:  # noqa: BLE001 - audit write only; the run error propagates
            pass

    def _accept_or_fallback(
        self,
        ctx: ToolContext,
        fold: FoldSpec,
        *,
        epoch_id: str,
        run_id: str,
        parent: FrozenArtifact | None,
        is_initial: bool,
    ) -> tuple[FrozenArtifact, str, list[str], list[str], dict[str, object] | None]:
        manifest = ctx.manifest
        accept_warnings: list[str] = []
        reasons: list[str] = []
        current_hash = artifact_hash(ctx.paths.agent_output)
        current_model_hash = model_artifact_hash(ctx.paths.model_artifacts)
        check = manifest.get("last_modification_check") or {}
        valid_runs = [
            s for s in manifest.get("backtest_summaries", []) if s.get("mode") == "valid" and s.get("status") == "ok"
        ]
        # Only successful complete validations can freeze a strategy artifact.
        complete_runs = [s for s in valid_runs if s.get("complete_validation")]
        # The LATEST complete validation of the CURRENT hash: a revert (or
        # step_rollback) back to an already-validated artifact freezes without
        # forcing a redundant full re-validation of identical content.
        selected = next(
            (
                s for s in reversed(complete_runs)
                if str(s.get("artifact_hash")) == current_hash
                and str(s.get("model_artifact_hash")) == current_model_hash
            ),
            complete_runs[-1] if complete_runs else None,
        )

        if not selected:
            reasons.append("no successful complete validation backtest in this fold")
        elif str(selected.get("artifact_hash")) != current_hash:
            reasons.append("artifact changed after the last successful validation backtest")
        elif str(selected.get("model_artifact_hash")) != current_model_hash:
            reasons.append("model artifacts changed after the last successful validation backtest")
        elif (
            not check.get("allowed_to_backtest")
            or str(check.get("artifact_hash")) != current_hash
            or str(check.get("model_artifact_hash")) != current_model_hash
        ):
            reasons.append("current artifact lacks a passing modification check")
        else:
            hard_reasons, accept_warnings = self.config.acceptance.evaluate(selected)
            reasons.extend(hard_reasons)
        if not reasons:
            # A HITL re-run freezes under a tagged id so it cannot collide with
            # the original attempt's frozen directory (append-only artifacts).
            rerun_tag = str(manifest.get("rerun_id") or "")
            artifact_id = f"strategy_{epoch_id}_{fold.fold_id}" + (f"__r{rerun_tag[:8]}" if rerun_tag else "")
            frozen = self._freeze(
                ctx.paths.agent_output,
                ctx.paths.model_artifacts,
                epoch_id=epoch_id,
                artifact_id=artifact_id,
                parent=parent,
                fold_id=fold.fold_id,
                run_id=run_id,
                expected_hash=current_hash,
                expected_model_hash=current_model_hash,
                step=self._step_id_for(manifest, selected),
            )
            return frozen, "frozen", [], accept_warnings, selected

        if parent is not None:
            if parent.requires_validation:
                parent_validation = next(
                    (
                        summary
                        for summary in reversed(complete_runs)
                        if str(summary.get("artifact_hash")) == parent.artifact_hash
                        and str(summary.get("model_artifact_hash")) == parent.model_artifact_hash
                    ),
                    None,
                )
                parent_hard_reasons = (
                    self.config.acceptance.evaluate(parent_validation)[0]
                    if parent_validation is not None
                    else ["no matching complete Validation"]
                )
            else:
                parent_hard_reasons = []
            if parent_hard_reasons:
                raise RuntimeError(
                    "Meta-regularized parent has no acceptable complete Validation in this Fold; "
                    f"refusing unvalidated fallback: {parent_hard_reasons}"
                )
            if parent.requires_validation:
                parent = replace(parent, requires_validation=False)
            # No accepted update: reuse the parent artifact; the fold counts as not improved.
            # finish_fold read-locks output/ and models/ (and the Docker agent's
            # subuid may own files inside) — make them deletable again or the
            # restore below dies with PermissionError on unlink.
            chmod_tree(ctx.paths.agent_output, file_mode=0o666, dir_mode=0o777)
            chmod_tree(ctx.paths.model_artifacts, file_mode=0o666, dir_mode=0o777)
            # Distinguish "never validated anything" from "validated but not accepted" so
            # audits do not need to reverse-engineer the reason list.
            copy_artifact(parent.path, ctx.paths.agent_output)
            copy_model_artifacts(parent.model_path, ctx.paths.model_artifacts)
            status = "no_valid_backtest" if selected is None else "no_update"
            return parent, status, reasons, accept_warnings, selected
        if is_initial:
            raise RuntimeError(f"initial fold produced no acceptable baseline artifact: {reasons}")
        raise RuntimeError(f"fold has neither an accepted artifact nor a parent fallback: {reasons}")

    def _frozen_test_eval(
        self,
        ctx: ToolContext,
        sandbox: LocalSandbox,
        docker: DockerSandbox | None,
        frozen: FrozenArtifact,
        *,
        result_name: str,
    ) -> dict[str, object]:
        ctx.phase = PHASE_FROZEN
        ctx.write_locked = True
        ctx.manifest.update(
            frozen_strategy_artifact_hash=frozen.artifact_hash,
            frozen_model_artifact_hash=frozen.model_artifact_hash,
        )
        self._bind_formal_view(sandbox, docker, "test_decision_input")
        summary = BacktestTool(ctx).run(mode="frozen_eval", result_name=result_name)
        if artifact_hash(ctx.paths.agent_output) != frozen.artifact_hash:
            raise FrozenArtifactMutatedError("frozen test run modified the strategy artifact")
        if model_artifact_hash(ctx.paths.model_artifacts) != frozen.model_artifact_hash:
            raise FrozenArtifactMutatedError("frozen test run modified the model artifacts")
        return summary

    def _freeze(
        self,
        source_root: Path,
        source_model_root: Path,
        *,
        epoch_id: str,
        artifact_id: str,
        parent: FrozenArtifact | None,
        fold_id: str,
        run_id: str,
        step: object,
        expected_hash: str | None = None,
        expected_model_hash: str | None = None,
        requires_validation: bool = False,
    ) -> FrozenArtifact:
        dest = self.config.experiment_dir / "strategy_artifacts" / epoch_id / artifact_id
        model_dest = self.config.experiment_dir / "strategy_artifacts" / epoch_id / f"{artifact_id}.models"
        if dest.exists():
            raise FileExistsError(f"strategy artifact already frozen: {dest}")
        if model_dest.exists():
            raise FileExistsError(f"model artifact already frozen: {model_dest}")
        dest.mkdir(parents=True)
        copy_artifact(source_root, dest)
        copy_model_artifacts(source_model_root, model_dest)
        digest = artifact_hash(dest)
        model_digest = model_artifact_hash(model_dest)
        if expected_hash is not None and digest != expected_hash:
            raise FrozenArtifactMutatedError(
                f"frozen copy hash {digest} != validated hash {expected_hash} for {artifact_id}; "
                "the working copy changed between acceptance and freeze"
            )
        if expected_model_hash is not None and model_digest != expected_model_hash:
            raise FrozenArtifactMutatedError(
                f"frozen model copy hash {model_digest} != validated hash {expected_model_hash} for {artifact_id}"
            )
        manifest = {
            "experiment_id": self.config.experiment_id,
            "epoch_id": epoch_id,
            "strategy_artifact_id": artifact_id,
            "parent_strategy_artifact_id": parent.artifact_id if parent else None,
            "strategy_artifact_hash": digest,
            "model_artifact_hash": model_digest,
            "combined_artifact_hash": combined_artifact_hash(digest, model_digest),
            "source_run_id": run_id,
            "source_fold_id": fold_id,
            "source_step_id": step,
            "requires_validation": requires_validation,
            "created_at": utc_now_iso(),
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return FrozenArtifact(
            artifact_id=artifact_id,
            path=dest,
            artifact_hash=digest,
            model_path=model_dest,
            model_artifact_hash=model_digest,
            requires_validation=requires_validation,
        )

    def _step_summaries(self, manifest: RunManifest, selected: dict[str, object] | None) -> list[dict[str, object]]:
        steps = []
        valid_runs = [
            s
            for s in manifest.get("backtest_summaries", [])
            if s.get("mode") == "valid" and s.get("complete_validation")
        ]
        for index, summary in enumerate(valid_runs):
            if summary is selected:
                status = "accepted"
            elif summary.get("status") == "ok":
                status = "completed"
            else:
                status = "rejected"
            steps.append(
                {
                    "step_id": f"step_{index + 1:03d}",
                    "status": status,
                    "strategy_artifact_ref": summary.get("artifact_hash"),
                    "model_artifact_ref": summary.get("model_artifact_hash"),
                    "combined_artifact_ref": summary.get("combined_artifact_hash"),
                    "validation_result_ref": summary.get("host_result_path") or summary.get("result_path"),
                    "modification_delta_summary": summary.get("modification_delta_summary"),
                    "run_manifest_ref": str(manifest.path),
                    "timing": {"finished_at": summary.get("finished_at")},
                    "decision_reason": summary.get("error") or "validation_backtest",
                    "summary": _metrics(summary),
                }
            )
        return steps

    def _step_id_for(self, manifest: RunManifest, selected: dict[str, object]) -> str:
        valid_runs = [
            s
            for s in manifest.get("backtest_summaries", [])
            if s.get("mode") == "valid" and s.get("complete_validation")
        ]
        for index, summary in enumerate(valid_runs):
            if summary is selected:
                return f"step_{index + 1:03d}"
        return "step_unknown"


def _snapshot_ref(manifest: dict[str, object]) -> dict[str, object]:
    return {"snapshot_id": manifest.get("snapshot_id"), "snapshot_hash": manifest.get("snapshot_hash")}


def _assert_single_raw_generation(**inputs: dict[str, object]) -> None:
    """Fail before Agent/container start when one run mixes raw-lake generations.

    Legacy and synthetic views may omit a generation stamp, so only non-empty
    generation IDs participate in the comparison.
    """
    generations: dict[str, str] = {}
    unstamped: list[str] = []
    for name, manifest in inputs.items():
        raw_generation = manifest.get("raw_generation")
        if not isinstance(raw_generation, dict):
            unstamped.append(name)
            continue
        generation_id = str(raw_generation.get("generation_id") or "").strip()
        if generation_id:
            generations[name] = generation_id
        else:
            unstamped.append(name)
    if generations and unstamped:
        raise RuntimeError(
            "run inputs mix generation-stamped and unstamped data; refusing to start "
            f"Agent/container: stamped={sorted(generations)}, unstamped={sorted(unstamped)}"
        )
    if len(set(generations.values())) > 1:
        details = ", ".join(f"{name}={generation_id}" for name, generation_id in generations.items())
        raise RuntimeError(
            "raw lake generation changed while building run inputs; "
            f"refusing to start Agent/container: {details}"
        )


def _check_has_changes(check: dict[str, object]) -> bool:
    delta = check.get("delta")
    model_delta = check.get("model_delta")
    if not isinstance(delta, dict):
        delta = {}
    if not isinstance(model_delta, dict):
        model_delta = {}
    return any(
        [
            int(delta.get("changed_file_count") or 0) > 0,
            int(delta.get("diff_lines") or 0) > 0,
            int(delta.get("code_diff_lines") or 0) > 0,
            int(model_delta.get("changed_file_count") or 0) > 0,
        ]
    )


def _researcher_wait_seconds(summary: object) -> float:
    if not isinstance(summary, dict):
        return 0.0
    try:
        return max(0.0, float(summary.get("researcher_wait_seconds") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _epoch_index(epoch_id: str) -> int:
    _, _, number = epoch_id.rpartition("_")
    try:
        return int(number)
    except ValueError:
        return 1
