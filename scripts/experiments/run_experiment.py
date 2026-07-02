#!/usr/bin/env python3
"""Experiment pipeline entrypoint (docs/pipeline_design.md).

Runs the development Fold/Epoch loop and the frozen held-out evaluation with
the real raw-data snapshot provider and the DeepSeek LLM proxy. The docs do
not prescribe a CLI; this thin wrapper only wires documented components. Shared
argparse groups and builder closures live in ``_cli``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent
for _path in (_SCRIPTS, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from _cli import (
    EXPERIMENT_META_REBUILD_HELP,
    add_acceptance_arguments,
    add_meta_directive_arguments,
    add_meta_sandbox_arguments,
    add_model_arguments,
    add_path_arguments,
    add_snapshot_window_arguments,
    add_web_search_arguments,
    build_meta_learning_sandbox_spec,
    build_proxies,
    build_session_builders,
    build_snapshot_config,
    build_web_search_providers,
    require_generic_period_args,
)
# Re-exported for the pipeline e2e test, which imports it from this module.
from _cli import _session_config_summary  # noqa: F401

from autotrade.environment.sandbox import SandboxSpec
from autotrade.pipelines import (
    AcceptanceRules,
    ExperimentConfig,
    ExperimentPipeline,
    RawSnapshotProvider,
    load_sse_trading_days,
)


def _resolve_period_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str, str, str]:
    if args.fold_period != "quarter":
        require_generic_period_args(parser, args)
        return args.first_test_period, args.last_test_period, args.heldout_first_period, args.heldout_last_period
    first_test_period = args.first_test_period or args.first_test_quarter
    last_test_period = args.last_test_period or args.last_test_quarter
    heldout_first_period = args.heldout_first_period or args.heldout_first_quarter
    heldout_last_period = args.heldout_last_period or args.heldout_last_quarter
    if not heldout_first_period or not heldout_last_period:
        parser.error("held-out period is required: pass --heldout-first-period/--heldout-last-period or legacy quarter args")
    return first_test_period, last_test_period, heldout_first_period, heldout_last_period


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run the rolling single-agent experiment pipeline.")
    parser.add_argument("--experiment-id", required=True)
    add_path_arguments(parser, repo_root)
    parser.add_argument(
        "--fold-period",
        choices=("week", "month", "quarter", "year"),
        default="quarter",
        help="Decision/replay period cadence for each Fold (day folds cannot satisfy the 2-trade-date replay minimum).",
    )
    parser.add_argument("--first-test-period", help="Generic first test period label for the selected fold period.")
    parser.add_argument("--last-test-period", help="Generic last test period label for the selected fold period.")
    parser.add_argument("--heldout-first-period", help="Generic first held-out period label for the selected fold period.")
    parser.add_argument("--heldout-last-period", help="Generic last held-out period label for the selected fold period.")
    parser.add_argument("--first-test-quarter", default="2022Q1")
    parser.add_argument("--last-test-quarter", default="2025Q4")
    parser.add_argument("--heldout-first-quarter")
    parser.add_argument("--heldout-last-quarter")
    parser.add_argument("--epochs", type=int, default=1)
    add_snapshot_window_arguments(parser, verbose_help=True)
    parser.add_argument(
        "--max-fold-minutes",
        type=int,
        default=60,
        help="Wall-clock deadline per Fold and meta-learning run.",
    )
    parser.add_argument(
        "--convergence-start-epoch",
        type=int,
        default=3,
        help="1-based Epoch index from which the Agent prompt enters convergence mode.",
    )
    parser.add_argument(
        "--disable-step-tree",
        action="store_true",
        help="Disable the cross-Fold Step artifact tree for ablation runs.",
    )
    parser.add_argument(
        "--nl-failure-policy",
        choices=("fail", "return_error_with_audit"),
        default="return_error_with_audit",
        help="How formal NL Sub Agent calls handle individual task failures.",
    )
    add_acceptance_arguments(parser, verbose_help=True)
    add_model_arguments(parser, verbose_help=True)
    parser.add_argument("--local-dev", action="store_true", help="Use the local executor for development/tests only.")
    parser.add_argument("--no-thinking", action="store_true", help="Disable provider reasoning mode for Agent and NL calls.")
    add_meta_directive_arguments(parser, verbose_help=True)
    add_web_search_arguments(parser, verbose_help=True)
    add_meta_sandbox_arguments(parser, verbose_help=True, disable_rebuild_help=EXPERIMENT_META_REBUILD_HELP)
    args = parser.parse_args()
    if args.meta_learning_directive and args.meta_learning_directive_file:
        parser.error("pass only one of --meta-learning-directive or --meta-learning-directive-file")
    meta_learning_directive = args.meta_learning_directive
    if args.meta_learning_directive_file:
        meta_learning_directive = args.meta_learning_directive_file.read_text(encoding="utf-8")
    first_test_period, last_test_period, heldout_first_period, heldout_last_period = _resolve_period_args(args, parser)

    snapshot_config = build_snapshot_config(args)
    sandbox_spec = SandboxSpec.from_host_fraction()
    meta_learning_sandbox_spec = build_meta_learning_sandbox_spec(args, sandbox_spec, repo_root=repo_root)
    config = ExperimentConfig(
        experiment_id=args.experiment_id,
        experiments_root=args.experiments_root.resolve(),
        work_root=args.work_root.resolve(),
        template_dir=args.template_dir.resolve(),
        first_test_period=first_test_period,
        last_test_period=last_test_period,
        heldout_first_period=heldout_first_period,
        heldout_last_period=heldout_last_period,
        fold_period=args.fold_period,
        epochs=args.epochs,
        window_months=args.window_months,
        max_fold_minutes=args.max_fold_minutes,
        snapshot_config=snapshot_config,
        nl_failure_policy=args.nl_failure_policy,
        convergence_start_epoch=args.convergence_start_epoch,
        meta_learning_directive=meta_learning_directive,
        step_tree_enabled=not args.disable_step_tree,
        acceptance=AcceptanceRules(
            min_return=args.min_return,
            min_sharpe=args.min_sharpe,
            max_drawdown=args.max_drawdown,
            # A fold only freezes a fully-completed validation (the freeze candidate
            # pool hard-filters to complete_validation runs), so this stays strict.
            require_complete_validation=True,
        ),
        sandbox_spec=sandbox_spec,
        meta_learning_sandbox_spec=meta_learning_sandbox_spec,
        meta_sandbox_rebuild_enabled=not args.disable_meta_sandbox_rebuild,
        use_docker=not args.local_dev,
    )
    proxies = build_proxies(args)
    web_search_providers = build_web_search_providers(args)
    agent_factory, meta_learner = build_session_builders(
        config=config,
        proxies=proxies,
        web_search_providers=web_search_providers,
    )

    pipeline = ExperimentPipeline(
        config,
        RawSnapshotProvider(
            args.raw_dir.resolve(),
            args.fundamental_events_root.resolve(),
            config=config.snapshot_config,
            fundamental_events_status=args.fundamental_events_status.resolve(),
        ),
        agent_factory,
        proxy=proxies.proxy,
        nl_proxy=proxies.nl_proxy,
        meta_learner=meta_learner,
    )
    result = pipeline.run(load_sse_trading_days(args.raw_dir))
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
