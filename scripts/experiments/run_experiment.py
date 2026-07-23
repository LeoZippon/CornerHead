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
    add_fold_exploration_directive_arguments,
    add_meta_directive_arguments,
    add_meta_sandbox_arguments,
    add_model_arguments,
    add_path_arguments,
    add_snapshot_window_arguments,
    add_web_search_arguments,
    build_experiment_config,
    build_pipeline,
    build_proxies,
    build_session_builders,
    build_web_search_providers,
    require_generic_period_args,
    resolve_fold_exploration_directive,
    resolve_meta_learning_directive,
)

from autotrade.environment.sandbox import SandboxSpec
from autotrade.pipelines import load_sse_trading_days


def _resolve_period_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Quarter runs keep the standing development defaults (held-out stays
    explicit); every other cadence demands all four labels."""
    require_generic_period_args(parser, args)
    if args.fold_period == "quarter":
        args.first_test_period = args.first_test_period or "2022Q1"
        args.last_test_period = args.last_test_period or "2025Q4"
        if not args.heldout_first_period or not args.heldout_last_period:
            parser.error("held-out period is required: pass --heldout-first-period/--heldout-last-period")


def build_parser(repo_root: Path) -> argparse.ArgumentParser:
    """The full experiment CLI parser. A function so the defaults-drift test can
    compare argparse defaults against the HITL ``PARAM_DEFAULTS`` (the two
    surfaces mirror each other dest-for-dest)."""
    parser = argparse.ArgumentParser(description="Run the rolling single-agent experiment pipeline.")
    parser.add_argument("--experiment-id", required=True)
    add_path_arguments(parser, repo_root)
    parser.add_argument(
        "--fold-period",
        choices=("week", "month", "quarter", "year"),
        default="quarter",
        help="Decision/replay period cadence for each Fold (day folds cannot satisfy the 2-trade-date replay minimum).",
    )
    parser.add_argument(
        "--first-test-period",
        help="Generic first test period label for the selected fold period; quarter runs default to 2022Q1.",
    )
    parser.add_argument(
        "--last-test-period",
        help="Generic last test period label for the selected fold period; quarter runs default to 2025Q4.",
    )
    parser.add_argument("--heldout-first-period", help="Generic first held-out period label for the selected fold period.")
    parser.add_argument("--heldout-last-period", help="Generic last held-out period label for the selected fold period.")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument(
        "--meta-learning-fold-interval",
        type=int,
        default=0,
        help=(
            "Additional within-Epoch Meta cadence in completed Folds; 0 keeps only the "
            "mandatory Epoch-start session, N>0 runs Meta after every N Folds before the next Fold."
        ),
    )
    add_snapshot_window_arguments(parser, verbose_help=True)
    parser.add_argument(
        "--max-fold-minutes",
        type=int,
        default=20,
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
    add_fold_exploration_directive_arguments(parser, verbose_help=True)
    add_web_search_arguments(parser, verbose_help=True)
    add_meta_sandbox_arguments(parser, verbose_help=True, disable_rebuild_help=EXPERIMENT_META_REBUILD_HELP)
    return parser


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = build_parser(repo_root)
    args = parser.parse_args()
    meta_learning_directive = resolve_meta_learning_directive(parser, args)
    fold_exploration_directive = resolve_fold_exploration_directive(parser, args)
    _resolve_period_args(args, parser)

    config = build_experiment_config(
        args,
        repo_root=repo_root,
        sandbox_spec=SandboxSpec.from_host_fraction(),
        meta_learning_directive=meta_learning_directive,
        fold_exploration_directive=fold_exploration_directive,
    )
    proxies = build_proxies(args)
    web_search_providers = build_web_search_providers(args)
    agent_factory, meta_learner = build_session_builders(
        config=config,
        proxies=proxies,
        web_search_providers=web_search_providers,
    )

    pipeline = build_pipeline(config, args, agent_factory, meta_learner, proxies)
    result = pipeline.run(load_sse_trading_days(pipeline.raw_dir))
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
