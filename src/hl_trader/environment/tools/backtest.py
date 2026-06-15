"""backtest_tool: the only formal backtest entrypoint.

docs/environment_design.md 4.4. Execution order:

1. enforce (or hash-reuse) the modification check;
2. load factor/ and nl_prior/;
3. call generate_candidates() against the bound /mnt/snapshot view;
4. validate the candidate pool;
5. run / sample / skip NL scoring per the nl switch;
6. normalize factor scores and compose final_score;
7. build the order plan from thresholds, holdings cap, and risk tags;
8. validate the order plan;
9. replay fills/rejects/costs through the simulated Broker.

``valid`` and ``frozen_eval`` are the only modes; test and held-out share
``frozen_eval`` and differ only in replay region, result directory, and ledger
labels chosen by the Pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from hl_trader.environment.artifacts import artifact_hash, load_strategy_artifact
from hl_trader.environment.attribution import build_attribution_report, factor_column
from hl_trader.environment.backtest_engine import (
    BacktestError,
    build_order_plan,
    compose_final_scores,
    compute_return_stats,
    run_fixed_holding_replay,
    run_generate_candidates,
    truncate_candidates,
    validate_candidates,
    validate_order_plan,
)
from hl_trader.environment.broker import BrokerProfile, load_shortable_codes
from hl_trader.environment.nl.context import build_company_contexts
from hl_trader.environment.nl.engine import NLBatchResult, NLScoringConfig, NLScoringEngine, NLTaskResult, TextRetriever
from hl_trader.environment.runtime import sanitize_for_log, utc_now_iso
from hl_trader.environment.snapshot import load_snapshot_manifest
from hl_trader.environment.step_tree import StepTree

from .base import PHASE_FROZEN, PHASE_TRAIN_VALID, ToolContext, ToolError
from .modification_check import ModificationCheckTool

MODES = ("valid", "frozen_eval")
NL_MODES = ("off", "sample", "on")


class BacktestTool:
    name = "backtest_tool"

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    # ---- public entrypoints ----

    def run(self, *, mode: str, nl_mode: str, result_name: str | None = None) -> dict[str, object]:
        if mode not in MODES:
            raise ToolError(f"unsupported backtest mode: {mode}")
        if nl_mode not in NL_MODES:
            raise ToolError(f"unsupported nl mode: {nl_mode}")
        if not self.ctx.extra.get("allow_backtest", True):
            raise ToolError("formal backtests are not allowed in this meta-learning run")
        if mode == "valid":
            self.ctx.require_phase(PHASE_TRAIN_VALID, tool=self.name)
            self.ctx.require_writable(tool=self.name)
        else:
            self.ctx.require_phase(PHASE_FROZEN, tool=self.name)
            if nl_mode != "on":
                raise ToolError("test/held-out replays require full natural-language scoring (nl=on)")
        try:
            return self._execute(mode=mode, nl_mode=nl_mode, result_name=result_name)
        except BacktestError as exc:
            self._record_failure(mode, nl_mode, str(exc))
            raise ToolError(str(exc)) from exc

    def contract_check(self) -> dict[str, object]:
        """finish_fold's light check: loadable entrypoint, valid schema, intact
        order-plan preconditions; no NL scoring, no results, no fills."""
        artifact = load_strategy_artifact(self.ctx.paths.agent_output)
        snapshot_dir = self._resolved_snapshot()
        candidates = run_generate_candidates(
            self.ctx.executor,
            self.ctx.paths,
            timeout_seconds=float(self.ctx.manifest.get("per_call_timeout_seconds", 300)),
        )
        universe = self._universe(snapshot_dir)
        validate_candidates(candidates, universe=universe)
        for key in ("long_score_threshold", "short_score_threshold", "max_total_holdings", "short_inventory_mode"):
            self.ctx.manifest.require(key)
        summary = {
            "tool": self.name,
            "kind": "contract_check",
            "checked_at": utc_now_iso(),
            "status": "ok",
            "candidate_rows": int(len(candidates)),
            "factors": len(artifact.factors),
            "rules": len(artifact.rules),
        }
        self.ctx.trace.emit("tool", summary, step_id=self.ctx.current_step_id)
        return summary

    # ---- internal orchestration ----

    def _execute(self, *, mode: str, nl_mode: str, result_name: str | None) -> dict[str, object]:
        manifest = self.ctx.manifest
        self._enforce_modification_check(mode)
        modification_check = manifest.get("last_modification_check") if mode == "valid" else None
        artifact = load_strategy_artifact(self.ctx.paths.agent_output)

        snapshot_dir = self._resolved_snapshot()
        self._verify_snapshot_binding(mode, snapshot_dir)
        decision_time = str(manifest.require("valid_decision_time" if mode == "valid" else "test_decision_time"))

        candidates = run_generate_candidates(
            self.ctx.executor,
            self.ctx.paths,
            timeout_seconds=float(manifest.get("per_call_timeout_seconds", 300)),
        )
        universe = self._universe(snapshot_dir)
        candidates = validate_candidates(candidates, universe=universe)
        candidates, truncated_count = truncate_candidates(candidates, max_candidates=self._max_candidates())
        self._enforce_attribution_contract(candidates, artifact, nl_mode=nl_mode)

        result_dir = self._new_result_dir(mode, result_name)
        nl_dir = result_dir / "nl_output"
        nl_dir.mkdir(parents=True, exist_ok=True)

        batch = self._run_nl_scoring(candidates, nl_mode, snapshot_dir, artifact)
        self._write_nl_outputs(nl_dir, batch)
        self._emit_nl_batch_summary(batch, nl_dir)
        if batch.has_blocking_failure():
            raise BacktestError(f"natural-language scoring failed for {batch.summary['failed']} candidate task(s)")

        scored = compose_final_scores(candidates, batch.scores_by_code(), nl_mode=nl_mode)
        profile = BrokerProfile(**_profile_kwargs(dict(manifest.require("broker_profile"))))
        shortable = load_shortable_codes(snapshot_dir, _decision_date(decision_time))
        order_shortable = _order_plan_shortable_codes(profile, shortable)
        short_unavailable_skipped = _short_unavailable_skipped_count(
            scored,
            short_threshold=float(manifest.require("short_score_threshold")),
            shortable_codes=order_shortable,
        )
        plan = build_order_plan(
            scored,
            long_threshold=float(manifest.require("long_score_threshold")),
            short_threshold=float(manifest.require("short_score_threshold")),
            max_total_holdings=int(manifest.require("max_total_holdings")),
            max_single_name_weight=profile.max_single_name_weight,
            shortable_codes=order_shortable,
        )
        validate_order_plan(
            plan,
            universe=universe,
            max_total_holdings=int(manifest.require("max_total_holdings")),
            max_single_name_weight=profile.max_single_name_weight,
        )

        replay_dir = self.ctx.paths.valid if mode == "valid" else self.ctx.paths.test
        replay_daily = pd.read_parquet(replay_dir / "daily.parquet")
        replay = run_fixed_holding_replay(
            plan, replay_daily, profile, decision_time_iso=decision_time, shortable_codes=shortable
        )
        stats = compute_return_stats(replay)

        plan_path = result_dir / "order_plan.parquet"
        plan.to_parquet(plan_path, index=False)
        (result_dir / "detailed_return.json").write_text(
            json.dumps(sanitize_for_log(stats), ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

        attribution_path = None
        if manifest.get("factor_attribution_enabled"):
            attribution_path = self._write_factor_attribution(
                result_dir,
                candidates=candidates,
                factors=[dict(f) for f in artifact.factors],
                nl_scores=batch.scores_by_code(),
                nl_mode=nl_mode,
                profile=profile,
                replay_daily=replay_daily,
                decision_time=decision_time,
                shortable=shortable,
                full_return=float(stats["total_return"]),
            )

        summary = {
            "tool": self.name,
            "mode": mode,
            "nl_mode": nl_mode,
            "status": "ok",
            "artifact_hash": artifact.artifact_hash,
            "complete_validation": nl_mode == "on",
            "result_name": result_dir.name,
            "result_path": self.ctx.executor.map_path(result_dir),
            "host_result_path": str(result_dir),
            "decision_time": decision_time,
            "candidate_rows": int(len(candidates)),
            "candidates_truncated": truncated_count,
            "order_count": int(len(plan)),
            "total_return": stats["total_return"],
            "long_return": stats["long_return"],
            "short_return": stats["short_return"],
            "sharpe": stats["sharpe"],
            "max_drawdown": stats["max_drawdown"],
            "margin_secs_reject_count": stats["margin_secs_reject_count"],
            "short_unavailable_skipped_count": short_unavailable_skipped,
            "factor_attribution_path": self.ctx.executor.map_path(attribution_path) if attribution_path else None,
            "host_factor_attribution_path": str(attribution_path) if attribution_path else None,
            "modification_delta_summary": _modification_delta_summary(modification_check),
            "finished_at": utc_now_iso(),
        }
        self.ctx.manifest.append_backtest_summary(summary)
        self.ctx.trace.emit("backtest", summary, step_id=self.ctx.current_step_id)
        if mode == "valid" and nl_mode == "on" and self.ctx.manifest.get("step_tree_enabled"):
            StepTree(self.ctx.paths.steps).record_step(
                self.ctx.paths.agent_output,
                epoch_id=str(manifest.get("epoch_id", "")) or None,
                fold_id=str(manifest.require("fold_id")),
                result_name=result_dir.name,
                artifact_hash=artifact.artifact_hash,
                metrics={k: stats[k] for k in ("total_return", "long_return", "short_return", "sharpe", "max_drawdown")},
                complete_validation=nl_mode == "on",
                attachments={"factor_attribution.json": attribution_path} if attribution_path else None,
            )
        return summary

    def _enforce_attribution_contract(self, candidates: pd.DataFrame, artifact, *, nl_mode: str) -> None:
        if not self.ctx.manifest.get("factor_attribution_enabled") or nl_mode != "on" or candidates.empty:
            return
        if not artifact.factors:
            raise BacktestError(
                "factor attribution is enabled but factor/factors.json has no registered factors"
            )
        available = [
            str(factor.get("id"))
            for factor in artifact.factors
            if factor_column(str(factor.get("id"))) in candidates.columns
        ]
        if not available:
            raise BacktestError(
                "factor attribution is enabled but generate_candidates() returned no registered factor_<id> columns"
            )

    def _write_factor_attribution(
        self,
        result_dir: Path,
        *,
        candidates,
        factors: list[dict[str, object]],
        nl_scores: dict[str, dict[str, object]],
        nl_mode: str,
        profile: BrokerProfile,
        replay_daily,
        decision_time: str,
        shortable: frozenset[str],
        full_return: float,
    ) -> Path:
        """Shapley factor contributions over the same composition/replay pipeline."""
        manifest = self.ctx.manifest

        def evaluate(scores) -> float:
            trial = candidates.copy()
            trial["factor_score"] = scores.values
            trial_scored = compose_final_scores(trial, nl_scores, nl_mode=nl_mode)
            trial_plan = build_order_plan(
                trial_scored,
                long_threshold=float(manifest.require("long_score_threshold")),
                short_threshold=float(manifest.require("short_score_threshold")),
                max_total_holdings=int(manifest.require("max_total_holdings")),
                max_single_name_weight=profile.max_single_name_weight,
                shortable_codes=_order_plan_shortable_codes(profile, shortable),
            )
            if trial_plan.empty:
                return 0.0
            trial_replay = run_fixed_holding_replay(
                trial_plan, replay_daily, profile, decision_time_iso=decision_time, shortable_codes=shortable
            )
            return float(compute_return_stats(trial_replay)["total_return"])

        report = build_attribution_report(candidates, factors, evaluate, full_return=full_return)
        path = result_dir / "factor_attribution.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _enforce_modification_check(self, mode: str) -> None:
        manifest = self.ctx.manifest
        current_hash = artifact_hash(self.ctx.paths.agent_output)
        if mode == "frozen_eval":
            frozen = str(manifest.require("frozen_strategy_artifact_hash"))
            if current_hash != frozen:
                raise ToolError(f"frozen artifact changed before frozen_eval: {current_hash} != {frozen}")
            return
        last = manifest.get("last_modification_check")
        if not last or str(last.get("artifact_hash")) != current_hash:
            last = ModificationCheckTool(self.ctx).run()
        if not last.get("allowed_to_backtest"):
            raise ToolError(f"modification check rejected the backtest: {last.get('reasons')}")

    def _run_nl_scoring(self, candidates: pd.DataFrame, nl_mode: str, snapshot_dir: Path, artifact) -> NLBatchResult:
        codes = candidates["ts_code"].astype(str).tolist()
        if nl_mode == "off" or not codes:
            return NLBatchResult(
                results=[
                    NLTaskResult(ts_code=code, task_id=f"off_{i}", state="skipped_by_config", error="nl_mode=off")
                    for i, code in enumerate(codes)
                ],
                mode="off",
            )
        nl_proxy = self.ctx.effective_nl_proxy
        if nl_proxy is None:
            raise BacktestError("nl scoring requested but no LLM proxy is configured")
        retriever = TextRetriever(snapshot_dir / "text_index.parquet", snapshot_dir / "text_library")
        readme = (self.ctx.paths.agent_output / "nl_prior" / "README.md").read_text(encoding="utf-8")
        engine = NLScoringEngine(
            nl_proxy,
            retriever,
            prior_rules=[dict(rule) for rule in artifact.rules],
            scoring_readme=readme,
            company_contexts=build_company_contexts(snapshot_dir, codes),
        )
        config = NLScoringConfig(
            mode=nl_mode,
            sample_size=int(self.ctx.manifest.get("nl_sample_size", 3)),
            per_call_timeout_seconds=float(self.ctx.manifest.get("per_call_timeout_seconds", 300)),
            max_workers=int(self.ctx.manifest.get("nl_max_workers", 4)),
            failure_policy=str(self.ctx.manifest.get("nl_failure_policy", "neutral_with_audit")),
        )
        return engine.score_candidates(codes, config)

    # ---- helpers ----

    def _resolved_snapshot(self) -> Path:
        link = self.ctx.paths.snapshot
        if not link.exists():
            raise ToolError("/mnt/snapshot is not bound to a decision-input view")
        return link.resolve()

    def _verify_snapshot_binding(self, mode: str, snapshot_dir: Path) -> None:
        expected_key = "valid_decision_input" if mode == "valid" else "test_decision_input"
        expected = dict(self.ctx.manifest.require("snapshots")).get(expected_key)
        if not expected:
            raise ToolError(f"run manifest has no snapshot record for {expected_key}")
        actual = load_snapshot_manifest(snapshot_dir)
        if actual.get("snapshot_id") != expected.get("snapshot_id") or actual.get("snapshot_hash") != expected.get(
            "snapshot_hash"
        ):
            raise ToolError(
                f"bound snapshot does not match the pipeline record for {expected_key}: "
                f"{actual.get('snapshot_id')} != {expected.get('snapshot_id')}"
            )

    def _universe(self, snapshot_dir: Path) -> set[str]:
        universe = pd.read_parquet(snapshot_dir / "universe.parquet")
        return set(universe["ts_code"].astype(str))

    def _max_candidates(self) -> int:
        return int(self.ctx.manifest.get("max_candidates", 10))

    def _new_result_dir(self, mode: str, result_name: str | None) -> Path:
        results_root = self.ctx.paths.results
        if result_name is None:
            prefix = "valid" if mode == "valid" else "test"
            existing = sorted(p.name for p in results_root.glob(f"{prefix}_*"))
            result_name = f"{prefix}_{len(existing):03d}"
        result_dir = results_root / result_name
        if result_dir.exists():
            raise ToolError(f"result directory already exists: {result_dir}")
        result_dir.mkdir(parents=True)
        return result_dir

    def _write_nl_outputs(self, nl_dir: Path, batch: NLBatchResult) -> None:
        def dump(name: str, records: list[dict[str, object]]) -> None:
            with (nl_dir / name).open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(sanitize_for_log(record), ensure_ascii=False, default=str) + "\n")

        dump("company_context.jsonl", [{"ts_code": r.ts_code, **r.company_context} for r in batch.results])
        dump(
            "search_requests.jsonl",
            [
                {"ts_code": r.ts_code, "early_stop_reason": r.early_stop_reason, "state": r.state, **request}
                for r in batch.results
                for request in r.search_requests
            ],
        )
        dump("evidence.jsonl", [{"ts_code": r.ts_code, **item} for r in batch.results for item in r.evidence])
        dump(
            "scores.jsonl",
            [
                {"state": r.state, "error": r.error, **(r.score or {"ts_code": r.ts_code})}
                for r in batch.results
            ],
        )
        dump("nl_llm_calls.jsonl", [call for r in batch.results for call in r.llm_calls])

    def _emit_nl_batch_summary(self, batch: NLBatchResult, nl_dir: Path) -> None:
        self.ctx.trace.emit(
            "nl_batch_summary",
            {**batch.summary, "nl_output_dir": str(nl_dir)},
            step_id=self.ctx.current_step_id,
        )

    def _record_failure(self, mode: str, nl_mode: str, error: str) -> None:
        summary = {
            "tool": self.name,
            "mode": mode,
            "nl_mode": nl_mode,
            "status": "error",
            "error": error,
            "finished_at": utc_now_iso(),
        }
        self.ctx.manifest.append_backtest_summary(summary)
        self.ctx.trace.emit("backtest", summary, step_id=self.ctx.current_step_id)


def _profile_kwargs(record: dict[str, object]) -> dict[str, object]:
    """Accept a profile record produced by BrokerProfile.to_record()."""
    fields = {
        "initial_cash",
        "commission_bps",
        "long_score_threshold",
        "short_score_threshold",
        "max_total_holdings",
        "short_inventory_mode",
        "short_margin_ratio",
        "short_borrow_fee_annual",
        "maintenance_closeout_ratio",
        "maintenance_warning_ratio",
        "maintenance_withdraw_ratio",
        "max_single_name_weight",
        "profile_id",
        "source",
    }
    return {key: value for key, value in record.items() if key in fields}


def _order_plan_shortable_codes(profile: BrokerProfile, shortable: frozenset[str]) -> frozenset[str] | None:
    if profile.short_inventory_mode == "theoretical_short":
        return None
    if profile.short_inventory_mode == "proxy_margin_secs":
        return shortable
    return frozenset()


def _short_unavailable_skipped_count(
    scored: pd.DataFrame,
    *,
    short_threshold: float,
    shortable_codes: frozenset[str] | None,
) -> int:
    if shortable_codes is None:
        return 0
    eligible_short = scored[
        (~scored["hard_excluded"])
        & (scored["final_score"] <= short_threshold)
        & (~scored["ts_code"].astype(str).isin(shortable_codes))
    ]
    return int(len(eligible_short))


def _decision_date(decision_time: str) -> str:
    return decision_time[:10].replace("-", "")


def _modification_delta_summary(check: object) -> dict[str, object] | None:
    if not isinstance(check, dict):
        return None
    delta = check.get("delta")
    if not isinstance(delta, dict):
        return None
    return {
        "changed_file_count": delta.get("changed_file_count"),
        "diff_lines": delta.get("diff_lines"),
        "factor_changes": {
            "added": len(delta.get("factors_added") or []),
            "removed": len(delta.get("factors_removed") or []),
            "modified": len(delta.get("factors_modified") or []),
        },
        "nl_prior_changes": {
            "added": len(delta.get("rules_added") or []),
            "removed": len(delta.get("rules_removed") or []),
            "rewritten": len(delta.get("rules_rewritten") or []),
        },
        "total_factors": delta.get("total_factors"),
        "total_rules": delta.get("total_rules"),
        "max_rule_text_chars": delta.get("max_rule_text_chars"),
    }
