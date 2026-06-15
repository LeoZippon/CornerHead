"""Experiment pipeline: Step/Fold/Epoch/Held-out orchestration.

docs/pipeline_design.md. The Pipeline schedules Data, Environment, and Agent
in time order, freezes inputs/outputs at each boundary, and writes the single
experiment ledger. It implements no investment logic and never rewrites
factor/ or nl_prior/ content; it only accepts, freezes, falls back, and
records.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hl_trader.environment.artifacts import artifact_hash, copy_artifact, load_strategy_artifact
from hl_trader.environment.executor import DockerExecutor
from hl_trader.environment.llm.proxy import LLMProxy
from hl_trader.environment.runtime import AgentTraceWriter, RunManifest, new_id, utc_now_iso
from hl_trader.environment.sandbox import DockerSandbox, LocalSandbox, SandboxSpec, link_copytree
from hl_trader.environment.step_tree import StepTree
from hl_trader.environment.tools import PHASE_FROZEN, BacktestTool, ModificationCheckTool, ToolContext

from .config import AgentFactory, ExperimentConfig, FoldOutcome, FrozenArtifact, MetaLearner, SnapshotProvider
from .folds import FoldSpec, build_fold_schedule, heldout_periods
from .ledger import ExperimentLedger


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
        self.snapshots = snapshots
        self.agent_factory = agent_factory
        self.proxy = proxy
        self.nl_proxy = nl_proxy
        self.meta_learner = meta_learner
        self.ledger = ExperimentLedger(config.ledger_path)

    # ---- top-level run ----

    def run(self, trading_days: list[str]) -> dict[str, object]:
        folds = build_fold_schedule(
            self.config.first_test_quarter,
            self.config.last_test_quarter,
            trading_days,
            window_months=self.config.window_months,
        )
        parent: FrozenArtifact | None = None
        taste_prompt = ""
        epoch_id = ""
        for epoch_index in range(1, self.config.epochs + 1):
            epoch_id = f"epoch_{epoch_index:03d}"
            if self.meta_learner is not None:
                parent, taste_prompt = self.run_meta_learning(epoch_id=epoch_id, parent=parent, previous_taste=taste_prompt)
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
        tree.set_position(tree.position_for_hash(parent.artifact_hash) if parent else None)

    def _start_sandbox(self, run_id: str) -> tuple[LocalSandbox, DockerSandbox | None]:
        sandbox = LocalSandbox(Path(self.config.work_root) / run_id)
        sandbox.prepare_layout()
        docker = None
        if self.config.use_docker:
            docker = DockerSandbox(sandbox, self.config.sandbox_spec)
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

    # ---- fold ----

    def run_fold(
        self,
        fold: FoldSpec,
        *,
        epoch_id: str,
        parent: FrozenArtifact | None,
        taste_prompt: str = "",
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

        is_initial = sandbox.install_strategy_artifact(
            parent.path if parent else None, Path(self.config.template_dir)
        )
        if self.config.step_tree_enabled:
            self._install_step_tree(paths, parent)
        constraints = replace(self.config.step_constraints, is_initial_artifact=is_initial)
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
                "fold": fold.to_record(),
                "valid_decision_time": fold.valid_decision_time.isoformat(),
                "test_decision_time": fold.test_decision_time.isoformat(),
                "snapshots": {
                    "valid_decision_input": _snapshot_ref(valid_snapshot),
                    "test_decision_input": _snapshot_ref(test_snapshot),
                    "valid_replay": _snapshot_ref(valid_replay),
                    "test_replay": _snapshot_ref(test_replay),
                },
                "is_initial_artifact": is_initial,
                "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                "parent_strategy_artifact_hash": parent.artifact_hash if parent else None,
                "template_dir": str(self.config.template_dir),
                "modification_constraints": constraints.to_record(),
                "acceptance_rules": self.config.acceptance.to_record(),
                "broker_profile": self.config.broker_profile.to_record(),
                "long_score_threshold": self.config.broker_profile.long_score_threshold,
                "short_score_threshold": self.config.broker_profile.short_score_threshold,
                "max_total_holdings": self.config.broker_profile.max_total_holdings,
                "short_inventory_mode": self.config.broker_profile.short_inventory_mode,
                "max_candidates": self.config.max_candidates,
                "nl_failure_policy": self.config.nl_failure_policy,
                "step_tree_enabled": self.config.step_tree_enabled,
                "factor_attribution_enabled": self.config.factor_attribution_enabled,
                "epoch_index": _epoch_index(epoch_id),
                "phase": "convergence" if _epoch_index(epoch_id) >= self.config.convergence_start_epoch else "exploration",
                "max_steps": self.config.max_steps_per_fold,
                "fold_deadline_at": deadline.isoformat(),
                "finalize_before_deadline_seconds": self.config.finalize_before_deadline_seconds,
                "per_call_timeout_seconds": self.config.per_call_timeout_seconds,
                "sandbox_spec": self.config.sandbox_spec.to_record(),
                "taste_prompt": taste_prompt,
            },
        )
        trace = AgentTraceWriter(
            paths.agent_trace,
            ids={
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold.fold_id,
                "run_id": run_id,
                "conversation_id": conversation_id,
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
            )
            self._bind_view(sandbox, docker, "valid_decision_input")

            agent = self.agent_factory(ctx, fold, dict(manifest.data))
            session_summary = agent.run()

            frozen, fold_status, accept_reasons, selected = self._accept_or_fallback(
                ctx, fold, epoch_id=epoch_id, run_id=run_id, parent=parent, is_initial=is_initial
            )
            sandbox.lock_agent_output()
            test_summary = self._frozen_test_eval(ctx, sandbox, docker, frozen, result_name="test_000")
        finally:
            if docker is not None:
                docker.stop()
        if self.config.step_tree_enabled and paths.steps.exists():
            link_copytree(paths.steps, self.config.experiment_dir / "steps")
        collected = sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)

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
                "finish_reason": session_summary.get("finish_status"),
                "fold_status": fold_status,
                "accept_reasons": accept_reasons,
                "selected_step_id": self._step_id_for(manifest, selected) if selected else None,
                "steps": steps,
                "frozen_strategy_artifact_id": frozen.artifact_id,
                "frozen_strategy_artifact_hash": frozen.artifact_hash,
                "frozen_strategy_artifact_path": str(frozen.path),
                "validation_result": _metrics(selected),
                "test_result": _metrics(test_summary),
                "state_changed_during_test": False,
                "run_manifest_ref": str(collected / "run_manifest.json"),
                "snapshot_ids": {key: ref["snapshot_id"] for key, ref in manifest.data["snapshots"].items()},
            }
        )
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
    ) -> tuple[FrozenArtifact | None, str]:
        if self.meta_learner is None:
            raise RuntimeError("no meta learner configured")
        run_id = new_id("run")
        sandbox, docker = self._start_sandbox(run_id)
        paths = sandbox.paths
        has_parent = parent is not None
        if has_parent:
            sandbox.install_strategy_artifact(parent.path, Path(self.config.template_dir))
        else:
            sandbox.install_strategy_artifact(None, Path(self.config.template_dir))
        # Development history and prior meta-learning memory are explicit inputs.
        history_path = paths.workspace / "development_history.json"
        history_path.write_text(
            json.dumps(self._development_history(previous_taste), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        memory_path = paths.workspace / "meta_learning_memory.jsonl"
        previous_memory = self.config.experiment_dir / "meta_learning" / epoch_id / "agent_trace.jsonl"
        if previous_memory.exists():
            memory_path.write_text(previous_memory.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            memory_path.write_text("", encoding="utf-8")
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
                "parent_strategy_artifact_id": parent.artifact_id if parent else None,
                "parent_strategy_artifact_hash": parent.artifact_hash if parent else None,
                "template_dir": str(self.config.template_dir),
                "modification_constraints": replace(
                    self.config.regularization_constraints, is_initial_artifact=not has_parent
                ).to_record(),
                "fold_deadline_at": deadline.isoformat(),
                "development_inputs": {
                    "experiment_ledger": str(self.config.ledger_path),
                    "development_history": str(history_path),
                    "meta_learning_memory": str(memory_path),
                    "previous_taste": bool(previous_taste.strip()),
                },
                "taste_output": str(paths.workspace / "taste.md"),
                "web_search_provider": "host_configured",
            },
        )
        trace = AgentTraceWriter(
            paths.agent_trace,
            ids={
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold_id,
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
            )
            ctx.extra["allow_backtest"] = False

            self.meta_learner(ctx)
            check = ModificationCheckTool(ctx).run()
        finally:
            if docker is not None:
                docker.stop()
        taste = _read_text(paths.workspace / "taste.md").strip() or previous_taste
        status = "taste_only"
        frozen = parent
        changed = _check_has_changes(check)
        if has_parent and check.get("allowed_to_backtest") and changed:
            load_strategy_artifact(paths.agent_output)  # read-only contract check; no return backtest
            frozen = self._freeze(
                paths.agent_output,
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
        collected = sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
        meta_dir = self.config.experiment_dir / "meta_learning" / epoch_id
        meta_dir.mkdir(parents=True, exist_ok=True)
        if taste:
            (meta_dir / "taste.md").write_text(taste + "\n", encoding="utf-8")
        if (collected / "agent_trace.jsonl").exists():
            (meta_dir / "agent_trace.jsonl").write_text(
                (collected / "agent_trace.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
            )
        self.ledger.append(
            {
                "record_type": "meta_learning",
                "experiment_id": self.config.experiment_id,
                "epoch_id": epoch_id,
                "fold_id": fold_id,
                "run_id": run_id,
                "status": status,
                "modification_check": {k: check.get(k) for k in ("allowed_to_backtest", "reasons", "artifact_hash")},
                "frozen_strategy_artifact_id": frozen.artifact_id if frozen else None,
                "frozen_strategy_artifact_hash": frozen.artifact_hash if frozen else None,
                "taste_path": str(meta_dir / "taste.md") if taste else None,
                "taste_chars": len(taste),
                "web_search_provider": manifest.get("web_search_provider"),
            }
        )
        return frozen, taste

    def _development_history(self, previous_taste: str) -> dict[str, object]:
        folds = self.ledger.read("fold")
        return {
            "folds": folds,
            "fold_backtest_summaries": [_compact_fold_history(record) for record in folds],
            "meta_learning": self.ledger.read("meta_learning"),
            "previous_taste": previous_taste,
        }

    # ---- held-out ----

    def run_heldout(self, final: FrozenArtifact, trading_days: list[str], *, epoch_id: str) -> list[dict[str, object]]:
        periods = heldout_periods(self.config.heldout_first_quarter, self.config.heldout_last_quarter, trading_days)
        summaries: list[dict[str, object]] = []
        for index, period in enumerate(periods):
            run_id = new_id("run")
            sandbox, docker = self._start_sandbox(run_id)
            paths = sandbox.paths
            copy_artifact(final.path, paths.parent_output)
            copy_artifact(final.path, paths.agent_output)
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
                    "test_decision_time": decision_time.isoformat(),
                    "snapshots": {
                        "test_decision_input": _snapshot_ref(snapshot),
                        "test_replay": _snapshot_ref(replay),
                    },
                    "frozen_strategy_artifact_hash": final.artifact_hash,
                    "broker_profile": self.config.broker_profile.to_record(),
                    "long_score_threshold": self.config.broker_profile.long_score_threshold,
                    "short_score_threshold": self.config.broker_profile.short_score_threshold,
                    "max_total_holdings": self.config.broker_profile.max_total_holdings,
                    "short_inventory_mode": self.config.broker_profile.short_inventory_mode,
                    "max_candidates": self.config.max_candidates,
                    "nl_failure_policy": self.config.nl_failure_policy,
                    "factor_attribution_enabled": self.config.factor_attribution_enabled,
                    "per_call_timeout_seconds": self.config.per_call_timeout_seconds,
                },
            )
            trace = AgentTraceWriter(
                paths.agent_trace,
                ids={
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": fold_id,
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
                summary = BacktestTool(ctx).run(mode="frozen_eval", nl_mode="on", result_name=f"heldout_{index:03d}")
            finally:
                if docker is not None:
                    docker.stop()
            if artifact_hash(paths.agent_output) != final.artifact_hash:
                raise RuntimeError("held-out run modified the frozen strategy artifact")
            sandbox.collect_artifacts(self.config.experiment_dir / "artifacts" / run_id)
            self.ledger.append(
                {
                    "record_type": "heldout",
                    "experiment_id": self.config.experiment_id,
                    "epoch_id": epoch_id,
                    "fold_id": fold_id,
                    "run_id": run_id,
                    "period": {"start": period["start"], "end": period["end"]},
                    "strategy_artifact_id": final.artifact_id,
                    "test_result": _metrics(summary),
                }
            )
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
        check = manifest.get("last_modification_check") or {}
        valid_runs = [
            s for s in manifest.get("backtest_summaries", []) if s.get("mode") == "valid" and s.get("status") == "ok"
        ]
        # Only complete (nl=on) validations can freeze a strategy artifact;
        # off/sample runs are debugging and never become Fold Steps.
        complete_runs = [s for s in valid_runs if s.get("complete_validation")]
        selected = complete_runs[-1] if complete_runs else None

        if not selected:
            reasons.append("no successful complete validation backtest in this fold")
        elif str(selected.get("artifact_hash")) != current_hash:
            reasons.append("artifact changed after the last successful validation backtest")
        elif not check.get("allowed_to_backtest") or str(check.get("artifact_hash")) != current_hash:
            reasons.append("current artifact lacks a passing modification check")
        else:
            _accepted, hard_reasons = self.config.acceptance.evaluate(selected)
            reasons.extend(hard_reasons)
        if not reasons:
            frozen = self._freeze(
                ctx.paths.agent_output,
                epoch_id=epoch_id,
                artifact_id=f"strategy_{epoch_id}_{fold.fold_id}",
                parent=parent,
                fold_id=fold.fold_id,
                run_id=run_id,
                step=self._step_id_for(manifest, selected),
            )
            return frozen, "frozen", [], selected

        if parent is not None:
            # No accepted update: reuse the parent artifact; the fold counts as not improved.
            copy_artifact(parent.path, ctx.paths.agent_output)
            return parent, "no_update_timeout", reasons, selected
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
        ctx.manifest.update(frozen_strategy_artifact_hash=frozen.artifact_hash)
        self._bind_view(sandbox, docker, "test_decision_input")
        summary = BacktestTool(ctx).run(mode="frozen_eval", nl_mode="on", result_name=result_name)
        if artifact_hash(ctx.paths.agent_output) != frozen.artifact_hash:
            raise RuntimeError("frozen test run modified the strategy artifact")
        return summary

    def _freeze(
        self,
        source_root: Path,
        *,
        epoch_id: str,
        artifact_id: str,
        parent: FrozenArtifact | None,
        fold_id: str,
        run_id: str,
        step: object,
    ) -> FrozenArtifact:
        dest = self.config.experiment_dir / "strategy_artifacts" / epoch_id / artifact_id
        if dest.exists():
            raise FileExistsError(f"strategy artifact already frozen: {dest}")
        dest.mkdir(parents=True)
        copy_artifact(source_root, dest)
        digest = artifact_hash(dest)
        manifest = {
            "experiment_id": self.config.experiment_id,
            "epoch_id": epoch_id,
            "strategy_artifact_id": artifact_id,
            "parent_strategy_artifact_id": parent.artifact_id if parent else None,
            "strategy_artifact_hash": digest,
            "created_at_fold": fold_id,
            "created_at_step": step,
            "frozen": True,
            "source_run_id": run_id,
            "created_at": utc_now_iso(),
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return FrozenArtifact(artifact_id=artifact_id, path=dest, artifact_hash=digest)

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
    if not isinstance(delta, dict):
        return False
    return any(
        [
            int(delta.get("changed_file_count") or 0) > 0,
            int(delta.get("diff_lines") or 0) > 0,
            bool(delta.get("factors_added") or delta.get("factors_removed") or delta.get("factors_modified")),
            bool(delta.get("rules_added") or delta.get("rules_removed") or delta.get("rules_rewritten")),
        ]
    )


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
                        "nl_mode",
                        "status",
                        "complete_validation",
                        "total_return",
                        "long_return",
                        "short_return",
                        "sharpe",
                        "max_drawdown",
                        "order_count",
                        "candidates_truncated",
                        "error",
                    )
                    if key in summary
                }
            )
    return {
        "epoch_id": record.get("epoch_id"),
        "fold_id": record.get("fold_id"),
        "fold_status": record.get("fold_status"),
        "finish_reason": record.get("finish_reason"),
        "validation_result": record.get("validation_result"),
        "test_result": record.get("test_result"),
        "accept_reasons": record.get("accept_reasons"),
        "backtest_summaries": backtests,
    }


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
