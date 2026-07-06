#!/usr/bin/env python3
"""Run one audit session: either meta-learning or one ordinary Fold.

This is intentionally narrower than ``run_experiment.py``. It is for manual
process audits where running the full Epoch/Fold/Held-out pipeline would hide
the single session being inspected.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
    AUDIT_META_REBUILD_HELP,
    add_acceptance_arguments,
    add_meta_directive_arguments,
    add_meta_sandbox_arguments,
    add_model_arguments,
    add_path_arguments,
    add_snapshot_window_arguments,
    add_web_search_arguments,
    build_meta_learning_sandbox_spec,
    build_meta_learning_managed_proxy_spec,
    build_pipeline,
    build_proxies,
    build_session_builders,
    build_snapshot_config,
    build_web_search_providers,
    require_generic_period_args,
    resolve_meta_learning_directive,
)

from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.sandbox import DEFAULT_IMAGE, SandboxSpec
from autotrade.pipelines import (
    AcceptanceRules,
    ExperimentConfig,
    FrozenArtifact,
    load_sse_trading_days,
)
from autotrade.pipelines.folds import build_fold_schedule


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("meta-learning", "fold"), required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--epoch-id", default="epoch_001")
    parser.add_argument("--fold-index", type=int, default=0, help="0-based Fold index to run; default first Fold.")
    add_path_arguments(parser, repo_root)
    parser.add_argument("--fold-period", choices=("week", "month", "quarter", "year"), default="quarter")
    parser.add_argument("--first-test-period", help="default 2022Q1 for quarter folds; required otherwise")
    parser.add_argument("--last-test-period", help="default 2025Q4 for quarter folds; required otherwise")
    parser.add_argument("--heldout-first-period", help="default 2026Q1 for quarter folds; required otherwise")
    parser.add_argument("--heldout-last-period", help="default 2026Q1 for quarter folds; required otherwise")
    add_snapshot_window_arguments(parser, verbose_help=False)
    parser.add_argument("--max-fold-minutes", type=int, default=60)
    add_model_arguments(parser, verbose_help=False)
    parser.add_argument("--local-dev", action="store_true", help="Use local executor; audit default is real Docker.")
    parser.add_argument("--sandbox-image", help="Optional Docker image override for this audit session.")
    parser.add_argument(
        "--gpu-devices",
        help="Optional comma-separated GPU device ids for this audit session, e.g. 5,6,7.",
    )
    parser.add_argument("--no-thinking", action="store_true")
    add_web_search_arguments(parser, verbose_help=False)
    add_meta_sandbox_arguments(parser, verbose_help=False, disable_rebuild_help=AUDIT_META_REBUILD_HELP)
    add_meta_directive_arguments(parser, verbose_help=False)
    parser.add_argument(
        "--taste-file",
        type=Path,
        help="Optional Taste text injected into an ordinary Fold; ignored in meta-learning mode.",
    )
    parser.add_argument(
        "--fold-directive-file",
        type=Path,
        help="Optional UTF-8 researcher directive injected into this ordinary Fold's system prompt.",
    )
    parser.add_argument(
        "--parent-output",
        type=Path,
        help="Optional frozen parent output/ artifact directory for this single session.",
    )
    parser.add_argument(
        "--parent-models",
        type=Path,
        help="Optional frozen parent models/ artifact directory. Requires --parent-output if provided.",
    )
    parser.add_argument("--parent-artifact-id", default="manual_parent")
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="Skip Docker image existence preflight. Useful only with --local-dev or custom Docker handling.",
    )
    add_acceptance_arguments(parser, verbose_help=False)
    args = parser.parse_args()
    require_generic_period_args(parser, args)
    if args.fold_period == "quarter":
        args.first_test_period = args.first_test_period or "2022Q1"
        args.last_test_period = args.last_test_period or "2025Q4"
        args.heldout_first_period = args.heldout_first_period or "2026Q1"
        args.heldout_last_period = args.heldout_last_period or "2026Q1"
    if args.parent_models and not args.parent_output:
        parser.error("--parent-models requires --parent-output")
    if args.mode == "meta-learning" and args.taste_file:
        parser.error("--taste-file is only meaningful with --mode fold")
    if args.mode == "meta-learning" and args.fold_directive_file:
        parser.error("--fold-directive-file is only meaningful with --mode fold")
    image = args.sandbox_image or DEFAULT_IMAGE
    if not args.local_dev and not args.skip_image_check:
        _require_docker_image(image)

    meta_learning_directive = resolve_meta_learning_directive(parser, args)
    taste_prompt = args.taste_file.read_text(encoding="utf-8") if args.taste_file else ""
    fold_directive = args.fold_directive_file.read_text(encoding="utf-8") if args.fold_directive_file else ""

    config = _build_config(repo_root, args, meta_learning_directive)
    proxies = build_proxies(args)
    web_search_providers = build_web_search_providers(args) if args.mode == "meta-learning" else {}
    agent_factory, meta_learner = build_session_builders(
        config=config,
        proxies=proxies,
        web_search_providers=web_search_providers,
    )

    pipeline = build_pipeline(config, args, agent_factory, meta_learner, proxies)
    trading_days = load_sse_trading_days(args.raw_dir)
    folds = build_fold_schedule(
        config.first_test_period,
        config.last_test_period,
        trading_days,
        window_months=config.window_months,
        period=config.fold_period,
    )
    if not 0 <= args.fold_index < len(folds):
        raise SystemExit(f"--fold-index {args.fold_index} out of range for {len(folds)} folds")
    fold = folds[args.fold_index]
    parent = _parent_artifact(args)

    if args.mode == "meta-learning":
        frozen, taste = pipeline.run_meta_learning(
            epoch_id=args.epoch_id,
            parent=parent,
            previous_taste="",
            visible_fold=fold,
        )
        result: dict[str, object] = {
            "status": "ok",
            "mode": args.mode,
            "experiment_id": args.experiment_id,
            "epoch_id": args.epoch_id,
            "visible_fold": fold.to_record(),
            "frozen_strategy_artifact_id": frozen.artifact_id if frozen else None,
            "taste_chars": len(taste),
            "experiment_dir": str(config.experiment_dir),
        }
    else:
        outcome = pipeline.run_fold(
            fold,
            epoch_id=args.epoch_id,
            parent=parent,
            taste_prompt=taste_prompt,
            fold_directive=fold_directive,
        )
        result = {
            "status": "ok",
            "mode": args.mode,
            "experiment_id": args.experiment_id,
            "epoch_id": args.epoch_id,
            "fold": fold.to_record(),
            "run_id": outcome.run_id,
            "fold_status": outcome.fold_status,
            "frozen_strategy_artifact_id": outcome.frozen.artifact_id,
            "validation_total_return": _metric(outcome.validation_summary, "total_return"),
            "test_total_return": _metric(outcome.test_summary, "total_return"),
            "experiment_dir": str(config.experiment_dir),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _build_config(repo_root: Path, args: argparse.Namespace, meta_learning_directive: str) -> ExperimentConfig:
    snapshot_config = build_snapshot_config(args)
    sandbox_overrides: dict[str, object] = {}
    if args.sandbox_image:
        sandbox_overrides["image"] = args.sandbox_image
    if args.gpu_devices:
        sandbox_overrides["gpu"] = _parse_gpu_devices(args.gpu_devices)
        sandbox_overrides["gpu_count"] = len(sandbox_overrides["gpu"])
    sandbox_spec = SandboxSpec.from_host_fraction(**sandbox_overrides)
    meta_learning_sandbox_spec = build_meta_learning_sandbox_spec(
        args,
        sandbox_spec,
        repo_root=repo_root,
        extra_dotenv_keys=(args.tavily_api_key_env, args.semantic_scholar_api_key_env),
    )
    meta_learning_managed_proxy = build_meta_learning_managed_proxy_spec(
        args,
        repo_root=repo_root,
        sandbox_spec=meta_learning_sandbox_spec,
    )
    return ExperimentConfig(
        experiment_id=args.experiment_id,
        experiments_root=args.experiments_root.resolve(),
        work_root=args.work_root.resolve(),
        template_dir=args.template_dir.resolve(),
        first_test_period=args.first_test_period,
        last_test_period=args.last_test_period,
        heldout_first_period=args.heldout_first_period,
        heldout_last_period=args.heldout_last_period,
        fold_period=args.fold_period,
        epochs=1,
        window_months=args.window_months,
        max_fold_minutes=args.max_fold_minutes,
        snapshot_config=snapshot_config,
        nl_failure_policy="return_error_with_audit",
        convergence_start_epoch=3,
        meta_learning_directive=meta_learning_directive,
        step_tree_enabled=True,
        acceptance=AcceptanceRules(
            min_return=args.min_return,
            min_sharpe=args.min_sharpe,
            max_drawdown=args.max_drawdown,
            require_complete_validation=True,
        ),
        sandbox_spec=sandbox_spec,
        meta_learning_sandbox_spec=meta_learning_sandbox_spec,
        meta_learning_managed_proxy=meta_learning_managed_proxy,
        meta_sandbox_rebuild_enabled=not args.disable_meta_sandbox_rebuild,
        use_docker=not args.local_dev,
    )


def _parent_artifact(args: argparse.Namespace) -> FrozenArtifact | None:
    if not args.parent_output:
        return None
    output = args.parent_output.resolve()
    models = args.parent_models.resolve() if args.parent_models else None
    model_hash = model_artifact_hash(models) if models is not None else model_artifact_hash(Path("__empty_models__"))
    return FrozenArtifact(
        artifact_id=args.parent_artifact_id,
        path=output,
        artifact_hash=artifact_hash(output),
        model_path=models,
        model_artifact_hash=model_hash,
    )


def _metric(summary: dict[str, object] | None, key: str) -> object:
    if not isinstance(summary, dict):
        return None
    return summary.get(key)


def _parse_gpu_devices(value: str) -> list[int]:
    devices: list[int] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            devices.append(int(item))
        except ValueError as exc:
            raise SystemExit(f"invalid --gpu-devices item: {item!r}") from exc
    if not devices:
        raise SystemExit("--gpu-devices must contain at least one integer id")
    return devices


def _require_docker_image(image: str) -> None:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"missing Docker image {image!r}. Build it first, for example: "
            "docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile ."
        )


if __name__ == "__main__":
    raise SystemExit(main())
