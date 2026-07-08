"""Experiment pipeline: Step/Fold/Epoch/Held-out orchestration.

docs/pipeline_design.md. The Pipeline schedules Data, Environment, and Agent
in time order, freezes inputs/outputs at each boundary, and writes the single
experiment ledger. It implements no investment logic and never rewrites
strategy content; it only accepts, freezes, falls back, and records.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
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
from autotrade.environment.data_summary import write_agent_data_summary
from autotrade.environment.executor import DockerExecutor
from autotrade.environment.identity import agent_visible_ref as _agent_visible_ref
from autotrade.environment.llm.proxy import LLMProxy
from autotrade.environment.managed_proxy import ManagedProxySession
from autotrade.environment.runtime import AgentTraceWriter, RunManifest, new_id, utc_now_iso
from autotrade.environment.sandbox import DockerSandbox, LocalSandbox, link_copytree
from autotrade.environment.step_tree import StepTree
from autotrade.environment.tools import PHASE_FROZEN, BacktestTool, ModificationCheckTool, ToolContext

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
from .ledger import ExperimentLedger


SANDBOX_ENVIRONMENT_REQUEST_NAME = "sandbox_environment.json"
SANDBOX_ENVIRONMENT_EXAMPLE_NAME = "sandbox_environment.example.json"
_SANDBOX_ENVIRONMENT_EXAMPLE = {
    "python_packages": [],
    "apt_packages": [],
    "npm_packages": [],
    "reason": (
        "Copy this example to sandbox_environment.json only when later ordinary Folds "
        "need stable new dependencies."
    ),
    "notes": (
        "Do not include shell commands, URLs, tokens, cache paths, local files, "
        "or temporary exploration artifacts."
    ),
}


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
        # Identical builds recur constantly (adjacent folds share the decision
        # snapshot anchor; multi-epoch reruns are snapshot-invariant) — cache
        # them per experiment and hardlink into each run's sandbox.
        self.snapshots = CachingSnapshotProvider(snapshots, config.experiment_dir / "snapshot_cache")
        self.agent_factory = agent_factory
        self.proxy = proxy
        self.nl_proxy = nl_proxy
        self.meta_learner = meta_learner
        self.ledger = ExperimentLedger(config.ledger_path)
        self._active_sandbox_spec = self._restore_active_sandbox_image(config.sandbox_spec)

    def _restore_active_sandbox_image(self, base_spec):
        """Resume durability: a successful meta-learning sandbox rebuild updates the
        active image only in-memory. On a fresh process (a fold-only or resumed run)
        reload the most recent good derived image tag from the ledger so later folds
        inherit the extended sandbox instead of silently falling back to the base."""
        if base_spec is None:
            return base_spec
        latest_image: str | None = None
        for record in self.ledger.read("meta_learning"):
            update = record.get("sandbox_image_update")
            if isinstance(update, dict) and update.get("status") == "ok" and update.get("image"):
                latest_image = str(update["image"])
        if latest_image and latest_image != base_spec.image:
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
        first_visible_fold = folds[0] if folds else None
        for epoch_index in range(1, self.config.epochs + 1):
            epoch_id = f"epoch_{epoch_index:03d}"
            if self.meta_learner is not None:
                parent, taste_prompt = self.run_meta_learning(
                    epoch_id=epoch_id,
                    parent=parent,
                    previous_taste=taste_prompt,
                    visible_fold=first_visible_fold,
                )
            for fold in folds:
                outcome = self.run_fold(fold, epoch_id=epoch_id, parent=parent, taste_prompt=taste_prompt)
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

    def _start_sandbox(self, run_id: str, *, kind: str = "fold") -> tuple[LocalSandbox, DockerSandbox | None]:
        sandbox = LocalSandbox(Path(self.config.work_root) / run_id)
        sandbox.prepare_layout()
        if kind == "meta_learning":
            _write_sandbox_environment_example(sandbox.paths.workspace)
        spec = (
            self.config.meta_learning_sandbox_spec
            if kind == "meta_learning" and self.config.meta_learning_sandbox_spec is not None
            else self._active_sandbox_spec
        )
        sandbox.write_runtime_env(
            mode="docker" if self.config.use_docker else "local",
            sandbox_spec=spec if self.config.use_docker else None,
        )
        docker = None
        if self.config.use_docker:
            docker = DockerSandbox(sandbox, spec)
        return sandbox, docker

    @staticmethod
    def _executor_for(docker: DockerSandbox | None, sandbox: LocalSandbox):
        if docker is None:
            return None  # ToolContext defaults to LocalExecutor
        return DockerExecutor(docker.container, sandbox.paths)

    @staticmethod
    def _bind_view(sandbox: LocalSandbox, docker: DockerSandbox | None, view_name: str) -> None:
        if docker is None:
            sandbox.bind_snapshot_view(sandbox.paths.snapshot_views / view_name)
        else:
            docker.bind_snapshot_view(view_name)

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
            "auction_enabled": self.config.auction_enabled,
            "auction_preopen_time": self.config.auction_preopen_time,
            "auction_decision_time": self.config.auction_decision_time,
            "auction_close_time": self.config.auction_close_time,
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
            "rolling_asof_enabled": self.config.timeview_enabled,
            "nl_max_calls_per_decision_day": self.config.nl_max_calls_per_decision_day,
            "nl_max_calls_per_backtest": self.config.nl_max_calls_per_backtest,
        }

    # ---- fold ----

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
    ) -> FoldOutcome:
        run_id = new_id("run")
        sandbox, docker = self._start_sandbox(run_id)
        paths = sandbox.paths

        valid_view = paths.snapshot_views / "valid_decision_input"
        test_view = paths.snapshot_views / "test_decision_input"
        valid_snapshot = self.snapshots.decision_snapshot(fold.valid_decision_time, valid_view)
        test_snapshot = self.snapshots.decision_snapshot(fold.test_decision_time, test_view)
        sandbox.install_replay_slot("train", valid_view)
        valid_replay = self.snapshots.replay_slot(
            fold.validation_start, fold.validation_end, paths.valid, label="valid"
        )
        test_replay = self.snapshots.replay_slot(fold.test_start, fold.test_end, paths.test, label="test")
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
        self._start_container(docker, manifest)
        test_summary: dict[str, object] | None = None
        test_eval_error: Exception | None = None
        try:
            ctx = ToolContext(
                paths=paths,
                manifest=manifest,
                trace=trace,
                proxy=self.proxy,
                nl_proxy=self.nl_proxy,
                executor=self._executor_for(docker, sandbox),
            )
            self._bind_view(sandbox, docker, "valid_decision_input")

            agent = self.agent_factory(ctx, fold, dict(manifest.data))
            session_summary = agent.run()

            frozen, fold_status, accept_reasons, selected = self._accept_or_fallback(
                ctx, fold, epoch_id=epoch_id, run_id=run_id, parent=parent, is_initial=is_initial
            )
            sandbox.lock_agent_output()
            # The strategy is now frozen on disk. The out-of-sample test_000 eval is
            # diagnostic only — acceptance is decided on validation (H2: finals are
            # not an acceptance gate) — so a failure here must NOT discard the
            # accepted strategy or abort the experiment. Record it and fall through
            # to a durable ledger entry (C1).
            try:
                test_summary = self._frozen_test_eval(ctx, sandbox, docker, frozen, result_name="test_000")
            except Exception as exc:  # noqa: BLE001 - recorded in the ledger below
                test_eval_error = exc
        finally:
            if docker is not None:
                docker.stop()
        if self.config.step_tree_enabled and paths.steps.exists():
            link_copytree(paths.steps, self.config.experiment_dir / "steps")
        # Collect artifacts, then ALWAYS append the fold ledger entry — even if
        # collection fails — so a frozen strategy is never left without a ledger
        # record (which previously left the experiment unresumable). With the
        # order-safe collector, a raised collect error means the frozen
        # output/models could not be saved, so re-raise after the record is durable.
        collect_error: Exception | None = None
        collected: Path | None = None
        try:
            collected = sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
        except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
            collect_error = exc
        finalize_error = collect_error or test_eval_error
        run_manifest_ref = (
            str(collected / "run_manifest.json") if collected is not None else str(paths.run_manifest)
        )
        steps = self._step_summaries(manifest, selected)
        self.ledger.append(
            {
                "record_type": "fold",
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold.fold_id,
                "run_id": run_id,
                **fold.to_record(),
                "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                "fold_directive": fold_directive.strip() or None,
                "system_prompt_overridden": bool(system_prompt_override.strip()),
                "rerun_id": rerun_id,
                "finish_reason": session_summary.get("finish_status"),
                "fold_status": fold_status,
                "accept_reasons": accept_reasons,
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
                "state_changed_during_test": False,
                "run_manifest_ref": run_manifest_ref,
                "snapshot_ids": {key: ref["snapshot_id"] for key, ref in manifest.data["snapshots"].items()},
                "finalize_error": (
                    f"{type(finalize_error).__name__}: {finalize_error}" if finalize_error is not None else None
                ),
            }
        )
        if collect_error is not None:
            raise collect_error
        return FoldOutcome(
            fold_id=fold.fold_id,
            run_id=run_id,
            fold_status=fold_status,
            frozen=frozen,
            validation_summary=selected,
            test_summary=test_summary,
        )

    # ---- epoch-start meta learning + regularization ----

    def run_meta_learning(
        self,
        *,
        epoch_id: str,
        parent: FrozenArtifact | None,
        previous_taste: str = "",
        visible_fold: FoldSpec | None = None,
        directive_override: str | None = None,
    ) -> tuple[FrozenArtifact | None, str]:
        if self.meta_learner is None:
            raise RuntimeError("no meta learner configured")
        run_id = new_id("run")
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
            )
            sandbox.bind_snapshot_view(visible_view)
        write_agent_data_summary(
            paths.data_summary,
            kind="meta_learning",
            fold_id=_agent_visible_ref(f"{epoch_id}_meta_learning", prefix="fold_ref"),
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
        # Full raw development records (every fold/meta record). Held-out is
        # excluded and, in any case, is never appended before development ends.
        ledger_full_path = paths.workspace / "experiment_ledger_full.jsonl"
        ledger_records = [
            _agent_visible_ledger_record(record)
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
        # Concatenate the full dialogue/tool logs of every prior epoch's
        # meta-learning session. (The previous wiring read this epoch's own
        # not-yet-written trace, so the memory was always empty.)
        memory_path = paths.workspace / "meta_learning_memory.jsonl"
        memory_path.write_text(self._prior_meta_learning_logs(epoch_id), encoding="utf-8")
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.config.max_fold_minutes)
        fold_id = f"{epoch_id}_meta_learning"
        manifest = RunManifest.create(
            paths.run_manifest,
            {
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
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
            if managed_proxy.env:
                ctx.extra["web_fetch_proxy_env"] = dict(managed_proxy.env)

            session_summary = self.meta_learner(ctx)
            if session_summary is None:
                session_summary = ctx.extra.get("agent_session_summary")
            self._validate_meta_learning_session(session_summary)
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
                artifact_id=f"strategy_{epoch_id}_meta_learning",
                parent=parent,
                fold_id=fold_id,
                run_id=run_id,
                step="meta_learning",
            )
            status = "meta_regularized"
        elif has_parent and check.get("allowed_to_backtest"):
            status = "taste_only_kept_parent"
        elif has_parent:
            status = "rejected_kept_parent"
        sandbox_image_error: RuntimeError | None = None
        try:
            sandbox_image_update = self._maybe_rebuild_sandbox_image(
                paths.workspace / SANDBOX_ENVIRONMENT_REQUEST_NAME,
                epoch_id=epoch_id,
                run_id=run_id,
                manifest=manifest,
            )
        except RuntimeError as exc:
            sandbox_image_update = manifest.get("sandbox_image_update")
            sandbox_image_error = exc
        # Fail-fast, but with a durable audit record: collect artifacts, then ALWAYS
        # append the ledger entry (even if the build or collection failed), then
        # re-raise the hard failure. This keeps a complete record of every
        # meta-learning outcome instead of losing it when finalization throws.
        collect_error: Exception | None = None
        collected: Path | None = None
        try:
            collected = sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
        except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
            collect_error = exc
        meta_dir = self.config.experiment_dir / "meta_learning" / epoch_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        if taste:
            (meta_dir / "taste.md").write_text(taste + "\n", encoding="utf-8")
        agent_trace_ref = (collected / "agent_trace.jsonl") if collected is not None else paths.agent_trace
        finalize_error = collect_error or sandbox_image_error
        self.ledger.append(
            {
                "record_type": "meta_learning",
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold_id,
                "run_id": run_id,
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
                "web_search_engines": manifest.get("web_search_engines"),
                "sandbox_image_update": sandbox_image_update,
                "finalize_error": (
                    f"{type(finalize_error).__name__}: {finalize_error}" if finalize_error is not None else None
                ),
            }
        )
        # Record is durable; now fail-fast on the real error (collection first).
        if collect_error is not None:
            raise collect_error
        if sandbox_image_error is not None:
            raise sandbox_image_error
        return frozen, taste

    def _maybe_rebuild_sandbox_image(
        self,
        request_path: Path,
        *,
        epoch_id: str,
        run_id: str,
        manifest: RunManifest,
    ) -> dict[str, object] | None:
        if not request_path.exists():
            return None
        try:
            request = _load_sandbox_environment_request(request_path)
        except ValueError as exc:
            result = {"status": "rejected", "reason": str(exc), "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
            manifest.update(sandbox_image_update=result)
            raise RuntimeError(f"meta-learning sandbox environment request rejected: {exc}") from exc
        if not _environment_request_has_packages(request):
            result = {"status": "skipped_empty", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
            manifest.update(sandbox_image_update=result)
            return result
        if not self.config.use_docker:
            result = {"status": "skipped_local_dev", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
            manifest.update(sandbox_image_update=result)
            return result
        if not self.config.meta_sandbox_rebuild_enabled:
            result = {"status": "disabled", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
            manifest.update(sandbox_image_update=result)
            return result

        build_dir = self.config.experiment_dir / "sandbox_images" / epoch_id
        build_dir.mkdir(parents=True, exist_ok=True)
        request_hash = _sandbox_environment_hash(request)
        image = f"{_docker_tag_component(self.config.experiment_id)}-{epoch_id}-{request_hash[:12]}"
        image_tag = f"autotrade-sandbox:{image}"
        dockerfile = build_dir / "Dockerfile"
        try:
            dockerfile_text = _render_sandbox_extension_dockerfile(self._active_sandbox_spec.image, request)
        except ValueError as exc:
            result = {
                "status": "rejected",
                "reason": str(exc),
                "request_ref": f"/mnt/agent/workspace/{request_path.name}",
                "base_image": self._active_sandbox_spec.image,
                "request_hash": request_hash,
            }
            manifest.update(sandbox_image_update=result)
            raise RuntimeError(f"meta-learning sandbox image rebuild rejected: {exc}") from exc
        dockerfile.write_text(dockerfile_text, encoding="utf-8")
        request_copy = build_dir / "sandbox_environment.json"
        request_copy.write_text(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        command = ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(build_dir)]
        started_at = utc_now_iso()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.meta_sandbox_rebuild_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            result = {
                "status": "timeout",
                "request_ref": f"/mnt/agent/workspace/{request_path.name}",
                "host_request_ref": str(request_copy),
                "dockerfile_ref": str(dockerfile),
                "base_image": self._active_sandbox_spec.image,
                "image": image_tag,
                "request_hash": request_hash,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "timeout_seconds": self.config.meta_sandbox_rebuild_timeout_seconds,
                "stdout_tail": str(exc.stdout or "")[-4000:],
                "stderr_tail": str(exc.stderr or "")[-4000:],
            }
            manifest.update(sandbox_image_update=result)
            raise RuntimeError(f"meta-learning sandbox image rebuild timed out: {image_tag}") from exc
        result: dict[str, object] = {
            "status": "ok" if completed.returncode == 0 else "failed",
            "request_ref": f"/mnt/agent/workspace/{request_path.name}",
            "host_request_ref": str(request_copy),
            "dockerfile_ref": str(dockerfile),
            "base_image": self._active_sandbox_spec.image,
            "image": image_tag,
            "request_hash": request_hash,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "returncode": completed.returncode,
            "stdout_tail": str(completed.stdout)[-4000:],
            "stderr_tail": str(completed.stderr)[-4000:],
        }
        if completed.returncode == 0:
            self._active_sandbox_spec = replace(self._active_sandbox_spec, image=image_tag)
            result["pruned_images"] = self._gc_derived_sandbox_images(keep_image=image_tag)
        manifest.update(sandbox_image_update=result)
        if completed.returncode != 0:
            raise RuntimeError(f"meta-learning sandbox image rebuild failed: {image_tag}")
        return result

    def _gc_derived_sandbox_images(self, *, keep_image: str) -> list[str]:
        """Best-effort prune of stale derived images for this experiment, keeping the
        most recent ``meta_sandbox_image_keep`` (and always the active one). Docker
        image GC must never fail a build, so all errors are swallowed."""
        keep = self.config.meta_sandbox_image_keep
        if keep <= 0:
            return []
        prefix = f"autotrade-sandbox:{_docker_tag_component(self.config.experiment_id)}-"
        try:
            listed = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}",
                 "autotrade-sandbox"],
                capture_output=True, text=True, timeout=30,
            )
            if listed.returncode != 0:
                return []
            rows: list[tuple[str, str]] = []
            for line in listed.stdout.splitlines():
                if not line.startswith(prefix):
                    continue
                tag, _, created = line.partition("\t")
                rows.append((tag, created))
            # Sort newest first by Docker's CreatedAt (lexicographic on the
            # "YYYY-MM-DD HH:MM:SS …" prefix is chronological) rather than trusting
            # `docker images` default order; keep the newest, drop the older tail,
            # never removing the just-built active image.
            rows.sort(key=lambda row: row[1], reverse=True)
            stale = [tag for tag, _ in rows[keep:] if tag != keep_image]
            pruned: list[str] = []
            for tag in stale:
                removed = subprocess.run(
                    ["docker", "image", "rm", tag], capture_output=True, text=True, timeout=30
                )
                if removed.returncode == 0:
                    pruned.append(tag)
            return pruned
        except (OSError, subprocess.SubprocessError):
            return []

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
        folds = self.ledger.read("fold")
        return {
            "fold_backtest_summaries": [_compact_fold_history(record) for record in folds],
            "meta_learning": [_agent_visible_ledger_record(record) for record in self.ledger.read("meta_learning")],
            "previous_taste": previous_taste,
        }

    def _prior_meta_learning_logs(self, current_epoch_id: str) -> str:
        """Concatenated agent_trace logs of the most recent meta-learning sessions
        before ``current_epoch_id`` (ordered by epoch, bounded by
        ``meta_memory_max_epochs``; older epochs persist only through the Taste
        chain and the compact fold history)."""
        current_index = _epoch_index(current_epoch_id)
        chunks: list[str] = []
        records = sorted(
            self.ledger.read("meta_learning"),
            key=lambda item: _epoch_index(str(item.get("epoch_id", ""))),
        )
        records = [r for r in records if _epoch_index(str(r.get("epoch_id", ""))) < current_index]
        keep = max(0, self.config.meta_memory_max_epochs)
        records = records[-keep:] if keep else []
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
                str(period["start"]), str(period["end"]), paths.test, label="heldout"
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
            self._start_container(docker, manifest)
            try:
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
                self._bind_view(sandbox, docker, "test_decision_input")
                summary = BacktestTool(ctx).run(mode="frozen_eval", result_name=f"heldout_{index:03d}")
            finally:
                if docker is not None:
                    docker.stop()
            if artifact_hash(paths.agent_output) != final.artifact_hash:
                raise RuntimeError("held-out run modified the frozen strategy artifact")
            if model_artifact_hash(paths.model_artifacts) != final.model_artifact_hash:
                raise RuntimeError("held-out run modified the frozen model artifacts")
            # Collect artifacts, then ALWAYS append the held-out ledger entry — even
            # if collection fails — then re-raise the collection failure (C1).
            collect_error: Exception | None = None
            try:
                sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
            except Exception as exc:  # noqa: BLE001 - recorded below, then re-raised
                collect_error = exc
            self.ledger.append(
                {
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
                    "finalize_error": (
                        f"{type(collect_error).__name__}: {collect_error}" if collect_error is not None else None
                    ),
                }
            )
            if collect_error is not None:
                raise collect_error
            summaries.append(summary)
        return summaries

    # ---- internals ----

    def _accept_or_fallback(
        self,
        ctx: ToolContext,
        fold: FoldSpec,
        *,
        epoch_id: str,
        run_id: str,
        parent: FrozenArtifact | None,
        is_initial: bool,
    ) -> tuple[FrozenArtifact, str, list[str], dict[str, object] | None]:
        manifest = ctx.manifest
        reasons: list[str] = []
        current_hash = artifact_hash(ctx.paths.agent_output)
        current_model_hash = model_artifact_hash(ctx.paths.model_artifacts)
        check = manifest.get("last_modification_check") or {}
        valid_runs = [
            s for s in manifest.get("backtest_summaries", []) if s.get("mode") == "valid" and s.get("status") == "ok"
        ]
        # Only successful complete validations can freeze a strategy artifact.
        complete_runs = [s for s in valid_runs if s.get("complete_validation")]
        selected = complete_runs[-1] if complete_runs else None

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
            _accepted, hard_reasons = self.config.acceptance.evaluate(selected)
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
                step=self._step_id_for(manifest, selected),
            )
            return frozen, "frozen", [], selected

        if parent is not None:
            # No accepted update: reuse the parent artifact; the fold counts as not improved.
            # Distinguish "never validated anything" from "validated but not accepted" so
            # audits do not need to reverse-engineer the reason list.
            copy_artifact(parent.path, ctx.paths.agent_output)
            copy_model_artifacts(parent.model_path, ctx.paths.model_artifacts)
            status = "no_valid_backtest" if selected is None else "no_update"
            return parent, status, reasons, selected
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
        self._bind_view(sandbox, docker, "test_decision_input")
        summary = BacktestTool(ctx).run(mode="frozen_eval", result_name=result_name)
        if artifact_hash(ctx.paths.agent_output) != frozen.artifact_hash:
            raise RuntimeError("frozen test run modified the strategy artifact")
        if model_artifact_hash(ctx.paths.model_artifacts) != frozen.model_artifact_hash:
            raise RuntimeError("frozen test run modified the model artifacts")
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
                    "modification_check_ref": "embedded:modification_delta_summary",
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


def _metrics(summary: dict[str, object] | None) -> dict[str, object] | None:
    if not summary:
        return None
    keys = (
        "total_return",
        "long_return",
        "short_return",
        "sharpe",
        "max_drawdown",
        "margin_secs_reject_count",
        "order_count",
    )
    return {key: summary.get(key) for key in keys if key in summary}


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


def _write_sandbox_environment_example(workspace: Path) -> Path:
    path = workspace / SANDBOX_ENVIRONMENT_EXAMPLE_NAME
    path.write_text(
        json.dumps(_SANDBOX_ENVIRONMENT_EXAMPLE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


_PYTHON_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-\[\],<>=!~:+]*$")
_SYSTEM_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*(?:@[A-Za-z0-9][A-Za-z0-9_.+~^-]*)?$"
)
_DOCKER_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,200}$")


def _load_sandbox_environment_request(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"sandbox_environment.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("sandbox_environment.json must be a JSON object")
    allowed = {"python_packages", "apt_packages", "npm_packages", "reason", "notes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"sandbox_environment.json contains unsupported fields: {unknown}")
    request = {
        "python_packages": _validated_package_list(
            raw.get("python_packages"), field="python_packages", pattern=_PYTHON_PACKAGE_RE, max_items=40
        ),
        "apt_packages": _validated_package_list(
            raw.get("apt_packages"), field="apt_packages", pattern=_SYSTEM_PACKAGE_RE, max_items=30
        ),
        "npm_packages": _validated_package_list(
            raw.get("npm_packages"), field="npm_packages", pattern=_NPM_PACKAGE_RE, max_items=30
        ),
    }
    for key in ("reason", "notes"):
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            request[key] = value[:2000]
    return request


def _validated_package_list(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str],
    max_items: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    packages: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        package = item.strip()
        if package.startswith("-") or not pattern.match(package):
            raise ValueError(f"unsupported {field} entry: {package!r}")
        if package not in packages:
            packages.append(package)
    if len(packages) > max_items:
        raise ValueError(f"{field} has {len(packages)} entries > {max_items}")
    return packages


def _environment_request_has_packages(request: dict[str, object]) -> bool:
    return any(request.get(key) for key in ("python_packages", "apt_packages", "npm_packages"))


def _sandbox_environment_hash(request: dict[str, object]) -> str:
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _docker_tag_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return (cleaned or "experiment")[:48].lower()


def _render_sandbox_extension_dockerfile(base_image: str, request: dict[str, object]) -> str:
    if not _DOCKER_IMAGE_RE.match(base_image):
        raise ValueError(f"unsupported base sandbox image: {base_image!r}")
    lines = [
        "# Generated by AutoTrade Pipeline from meta-learning sandbox_environment.json.",
        f"FROM {base_image}",
        "ARG PIP_INDEX_URL=https://pypi.org/simple",
        "USER root",
    ]
    apt_packages = [shlex.quote(item) for item in request.get("apt_packages", [])]
    if apt_packages:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(apt_packages)
            + " && rm -rf /var/lib/apt/lists/*"
        )
    python_specs = list(request.get("python_packages", []))
    python_packages = [shlex.quote(item) for item in python_specs]
    if python_packages:
        lines.append(
            'RUN python -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" '
            + " ".join(python_packages)
        )
        # Verification layer: a build that installs a package but cannot import it
        # is a silent transfer failure for later Folds. Fail the build here so
        # "image built" implies "importable", not just "installable".
        imports = _python_import_names(python_specs)
        if imports:
            stmt = "; ".join(f"import {name}" for name in imports)
            lines.append(f'RUN python -c {shlex.quote(stmt)}')
    npm_packages = [shlex.quote(item) for item in request.get("npm_packages", [])]
    if npm_packages:
        lines.append("RUN npm install -g --no-fund --no-audit " + " ".join(npm_packages))
    lines.extend(["WORKDIR /mnt/agent", ""])
    return "\n".join(lines)


# PyPI distribution name -> import module name for the cases where they diverge.
_IMPORT_NAME_ALIASES = {
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
    "opencv-contrib-python": "cv2",
    "umap-learn": "umap",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "msgpack-python": "msgpack",
    "faiss-cpu": "faiss",
    "faiss-gpu": "faiss",
}


def _python_import_names(specs: list[str]) -> list[str]:
    """Top-level import names for declared python_packages, for a build-time smoke
    test. Only emit a name we are confident about: a known alias, or a simple
    distribution name with no '-'/'.' (where dist == import). For a hyphenated/dotted
    name that is not aliased the import module is unguessable (e.g. umap-learn->umap,
    opencv-contrib-python->cv2), so we SKIP its smoke import rather than reject a
    validly-installed package; the build still verifies pip install succeeded."""
    names: list[str] = []
    for spec in specs:
        dist = re.split(r"[<>=!~;\[\s]", str(spec).strip(), maxsplit=1)[0].strip()
        if not dist:
            continue
        lower = dist.lower()
        if lower in _IMPORT_NAME_ALIASES:
            module = _IMPORT_NAME_ALIASES[lower]
        elif "-" in lower or "." in lower:
            continue  # ambiguous import name — rely on pip install success
        else:
            module = lower
        if module and module.isidentifier() and module not in names:
            names.append(module)
    return names


def _compact_fold_history(record: dict[str, object]) -> dict[str, object]:
    manifest = _read_json(Path(str(record.get("run_manifest_ref", ""))))
    backtests = []
    if isinstance(manifest.get("backtest_summaries"), list):
        for summary in manifest["backtest_summaries"]:
            if not isinstance(summary, dict):
                continue
            backtests.append(
                {
                    key: summary.get(key)
                    for key in (
                        "result_name",
                        "mode",
                        "status",
                        "complete_validation",
                        "total_return",
                        "long_return",
                        "short_return",
                        "sharpe",
                        "max_drawdown",
                        "order_count",
                        "error",
                    )
                    if key in summary
                }
            )
    return {
        "epoch_id": record.get("epoch_id"),
        "fold_id": _agent_visible_ref(record.get("fold_id"), prefix="fold_ref"),
        "fold_status": record.get("fold_status"),
        "finish_reason": record.get("finish_reason"),
        "validation_result": record.get("validation_result"),
        "accept_reasons": record.get("accept_reasons"),
        "backtest_summaries": backtests,
    }


def _agent_visible_ledger_record(record: dict[str, object]) -> dict[str, object]:
    public = json.loads(json.dumps(record, ensure_ascii=False, default=str))
    if not isinstance(public, dict):
        return {}
    allowed = {
        "record_type",
        "experiment_id",
        "epoch_id",
        "run_id",
        "parent_strategy_artifact_id",
        "finish_reason",
        "fold_status",
        "accept_reasons",
        "selected_step_id",
        "steps",
        "frozen_strategy_artifact_id",
        "frozen_strategy_artifact_hash",
        "frozen_model_artifact_hash",
        "frozen_combined_artifact_hash",
        "validation_result",
        "state_changed_during_test",
        "snapshot_ids",
        "status",
        "modification_check",
        "taste_chars",
        "agent_session_summary",
        "agent_trace_ref",
        "meta_learning_directive",
        "web_search_engines",
        "input_window",
        "validation_period",
        "valid_decision_time",
    }
    public = {key: value for key, value in public.items() if key in allowed}
    if "fold_id" in record:
        public["fold_id"] = _agent_visible_ref(record.get("fold_id"), prefix="fold_ref")
    for key in ("parent_strategy_artifact_id", "frozen_strategy_artifact_id"):
        if public.get(key):
            public[key] = _agent_visible_ref(public[key], prefix="strategy_ref")
    steps = public.get("steps")
    if isinstance(steps, list):
        public["steps"] = [_agent_visible_step_record(step) for step in steps if isinstance(step, dict)]
    snapshot_ids = public.get("snapshot_ids")
    if isinstance(snapshot_ids, dict):
        public["snapshot_ids"] = {
            key: value
            for key, value in snapshot_ids.items()
            if not str(key).startswith("test_") and not str(key).startswith("heldout_")
        }
    return public


def _agent_visible_step_record(record: dict[str, object]) -> dict[str, object]:
    allowed = {
        "step_id",
        "status",
        "strategy_artifact_ref",
        "model_artifact_ref",
        "combined_artifact_ref",
        "modification_delta_summary",
        "timing",
        "decision_reason",
        "summary",
    }
    return {key: value for key, value in record.items() if key in allowed}


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _epoch_index(epoch_id: str) -> int:
    _, _, number = epoch_id.rpartition("_")
    try:
        return int(number)
    except ValueError:
        return 1
