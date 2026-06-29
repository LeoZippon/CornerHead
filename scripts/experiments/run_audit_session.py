#!/usr/bin/env python3
"""Run one audit session: either meta-learning or one ordinary Fold.

This is intentionally narrower than ``run_experiment.py``. It is for manual
process audits where running the full Epoch/Fold/Held-out pipeline would hide
the single session being inspected.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.agent import AgentSessionConfig, AgentSessionRunner, ContextCompactionConfig
from autotrade.environment.artifacts import artifact_hash, model_artifact_hash
from autotrade.environment.llm import DeepSeekProxy
from autotrade.environment.sandbox import DEFAULT_IMAGE, SandboxSpec
from autotrade.environment.snapshot import SnapshotConfig
from autotrade.environment.tools import ToolContext
from autotrade.environment.web_search import SemanticScholarSearchProvider, TavilySearchProvider
from autotrade.pipelines import (
    AcceptanceRules,
    ExperimentConfig,
    ExperimentPipeline,
    FrozenArtifact,
    RawSnapshotProvider,
    load_sse_trading_days,
)
from autotrade.pipelines.folds import build_fold_schedule

import run_experiment as rex


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("meta-learning", "fold"), required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--epoch-id", default="epoch_001")
    parser.add_argument("--fold-index", type=int, default=0, help="0-based Fold index to run; default first Fold.")
    parser.add_argument("--raw-dir", type=Path, default=repo_root / "data/raw")
    parser.add_argument("--fundamental-events-root", type=Path, default=repo_root / "data/pit/fundamental_events")
    parser.add_argument(
        "--fundamental-events-status",
        type=Path,
        default=repo_root / "results/data_quality/fundamental_events_status.json",
    )
    parser.add_argument("--experiments-root", type=Path, default=repo_root / "experiments")
    parser.add_argument("--work-root", type=Path, default=repo_root / ".runtime/sandboxes")
    parser.add_argument("--template-dir", type=Path, default=repo_root / "configs/agent_output_template")
    parser.add_argument("--fold-period", choices=("day", "week", "month", "quarter", "year"), default="quarter")
    parser.add_argument("--first-test-period", default="2022Q1")
    parser.add_argument("--last-test-period", default="2025Q4")
    parser.add_argument("--heldout-first-period", default="2026Q1")
    parser.add_argument("--heldout-last-period", default="2026Q1")
    parser.add_argument("--window-months", type=int, default=21)
    parser.add_argument("--intraday-trade-days", type=int, default=SnapshotConfig().intraday_trade_days)
    parser.add_argument("--max-fold-minutes", type=int, default=60)
    parser.add_argument("--model", default=rex.DEFAULT_AGENT_MODEL)
    parser.add_argument("--nl-model", default=rex.DEFAULT_NL_MODEL)
    parser.add_argument("--compact-model", default=rex.DEFAULT_COMPACT_MODEL)
    parser.add_argument("--disable-context-compact", action="store_true")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max", "xhigh"), default="max")
    parser.add_argument("--compact-token-threshold", type=int, default=200_000)
    parser.add_argument("--compact-keep-recent-messages", type=int, default=12)
    parser.add_argument("--compact-max-tokens", type=int, default=1600)
    parser.add_argument("--compact-max-calls", type=int, default=8)
    parser.add_argument("--local-dev", action="store_true", help="Use local executor; audit default is real Docker.")
    parser.add_argument("--sandbox-image", help="Optional Docker image override for this audit session.")
    parser.add_argument(
        "--gpu-devices",
        help="Optional comma-separated GPU device ids for this audit session, e.g. 5,6,7.",
    )
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument(
        "--web-search-engines",
        nargs="+",
        choices=("tavily", "semantic_scholar"),
        default=("tavily", "semantic_scholar"),
    )
    parser.add_argument("--tavily-api-key-env", default="TAVILY_API_KEY")
    parser.add_argument("--semantic-scholar-api-key-env", default="SEMANTIC_SCHOLAR_API_KEY")
    parser.add_argument("--meta-learning-network", choices=("none", "bridge", "host"), default="bridge")
    parser.add_argument("--meta-learning-env", action="append", default=[], metavar="NAME")
    parser.add_argument("--meta-learning-add-host-gateway", action="store_true")
    parser.add_argument("--meta-learning-host-proxy", action="store_true")
    parser.add_argument("--disable-meta-sandbox-rebuild", action="store_true")
    parser.add_argument("--meta-learning-directive", default="")
    parser.add_argument("--meta-learning-directive-file", type=Path)
    parser.add_argument(
        "--taste-file",
        type=Path,
        help="Optional Taste text injected into an ordinary Fold; ignored in meta-learning mode.",
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
    parser.add_argument("--min-return", type=float, default=0.0)
    parser.add_argument("--min-sharpe", type=float, default=0.0)
    parser.add_argument("--max-drawdown", type=float, default=0.25)
    parser.add_argument("--allow-incomplete-validation", action="store_true")
    args = parser.parse_args()
    if args.meta_learning_directive and args.meta_learning_directive_file:
        parser.error("pass only one of --meta-learning-directive or --meta-learning-directive-file")
    if args.parent_models and not args.parent_output:
        parser.error("--parent-models requires --parent-output")
    if args.mode == "meta-learning" and args.taste_file:
        parser.error("--taste-file is only meaningful with --mode fold")
    image = args.sandbox_image or DEFAULT_IMAGE
    if not args.local_dev and not args.skip_image_check:
        _require_docker_image(image)

    meta_learning_directive = args.meta_learning_directive
    if args.meta_learning_directive_file:
        meta_learning_directive = args.meta_learning_directive_file.read_text(encoding="utf-8")
    taste_prompt = args.taste_file.read_text(encoding="utf-8") if args.taste_file else ""

    config = _build_config(repo_root, args, meta_learning_directive)
    proxy = DeepSeekProxy.from_env(
        model=args.model,
        thinking_enabled=not args.no_thinking,
        reasoning_effort=args.reasoning_effort,
    )
    nl_proxy = proxy if args.nl_model == args.model else DeepSeekProxy.from_env(
        model=args.nl_model,
        thinking_enabled=not args.no_thinking,
        reasoning_effort=args.reasoning_effort,
    )
    compact_proxy = None
    if not args.disable_context_compact:
        compact_proxy = DeepSeekProxy.from_env(model=args.compact_model, thinking_enabled=False)
    explore_proxy = nl_proxy
    compact_config = ContextCompactionConfig(
        token_threshold=args.compact_token_threshold,
        keep_recent_messages=args.compact_keep_recent_messages,
        max_response_tokens=args.compact_max_tokens,
        max_calls=args.compact_max_calls,
    )
    web_search_providers = _web_search_providers(args) if args.mode == "meta-learning" else {}

    def session_config(manifest_data: dict[str, object]) -> AgentSessionConfig:
        return AgentSessionConfig(
            fold_deadline_at=datetime.fromisoformat(str(manifest_data["fold_deadline_at"])),
            finalize_before_deadline_seconds=config.finalize_before_deadline_seconds,
            per_call_timeout_seconds=config.per_call_timeout_seconds,
            max_steps=config.max_steps_per_fold,
            max_backtests_per_fold=config.max_backtests_per_fold,
            context_compaction=compact_config,
        )

    def llm_config_summary() -> dict[str, object]:
        return {
            "main": rex._proxy_summary(proxy),
            "nl": rex._proxy_summary(nl_proxy),
            "compact": rex._proxy_summary(compact_proxy),
            "explore": rex._proxy_summary(explore_proxy),
        }

    def agent_factory(ctx: ToolContext, fold, manifest_data: dict[str, object]) -> AgentSessionRunner:
        agent_session_config = session_config(manifest_data)
        ctx.manifest.update(
            agent_session_config=rex._session_config_summary(agent_session_config, compact_enabled=compact_proxy is not None),
            llm_config_summary=llm_config_summary(),
        )
        return AgentSessionRunner(
            ctx,
            proxy,
            agent_session_config,
            fold_info=fold.to_record(),
            acceptance_rules=config.acceptance.to_record(),
            phase=str(manifest_data.get("phase", "exploration")),
            step_tree_enabled=bool(manifest_data.get("step_tree_enabled", False)),
            taste_prompt=str(manifest_data.get("taste_prompt", "")),
            compact_proxy=compact_proxy,
            explore_proxy=explore_proxy,
        )

    def meta_learner(ctx: ToolContext) -> dict[str, object]:
        agent_session_config = session_config(ctx.manifest.data)
        ctx.manifest.update(
            web_search_engines=list(web_search_providers),
            agent_session_config=rex._session_config_summary(agent_session_config, compact_enabled=compact_proxy is not None),
            llm_config_summary=llm_config_summary(),
        )
        return AgentSessionRunner(
            ctx,
            proxy,
            agent_session_config,
            fold_info=dict(ctx.manifest.get("development_inputs", {})),
            acceptance_rules={},
            mode="meta_learning",
            meta_learning_directive=str(ctx.manifest.get("meta_learning_directive", "")),
            web_search_providers=web_search_providers,
            compact_proxy=compact_proxy,
            explore_proxy=explore_proxy,
        ).run()

    pipeline = ExperimentPipeline(
        config,
        RawSnapshotProvider(
            args.raw_dir.resolve(),
            args.fundamental_events_root.resolve(),
            config=config.snapshot_config,
            fundamental_events_status=args.fundamental_events_status.resolve(),
        ),
        agent_factory,
        proxy=proxy,
        nl_proxy=nl_proxy,
        meta_learner=meta_learner,
    )
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
        outcome = pipeline.run_fold(fold, epoch_id=args.epoch_id, parent=parent, taste_prompt=taste_prompt)
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
    snapshot_config = SnapshotConfig(
        window_months=args.window_months,
        intraday_trade_days=args.intraday_trade_days,
    )
    sandbox_overrides: dict[str, object] = {}
    if args.sandbox_image:
        sandbox_overrides["image"] = args.sandbox_image
    if args.gpu_devices:
        sandbox_overrides["gpu"] = _parse_gpu_devices(args.gpu_devices)
        sandbox_overrides["gpu_count"] = len(sandbox_overrides["gpu"])
    sandbox_spec = SandboxSpec.from_host_fraction(**sandbox_overrides)
    requested_envs = [name.strip() for name in args.meta_learning_env if name.strip()]
    dotenv_keys = tuple(
        dict.fromkeys(
            [
                *rex.DEFAULT_META_CREDENTIAL_ENVS,
                *(host_env for _container_env, host_env in rex.DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy),
                *requested_envs,
                args.tavily_api_key_env,
                args.semantic_scholar_api_key_env,
            ]
        )
    )
    rex._load_dotenv_into_environ(repo_root / ".env", keys=dotenv_keys)
    meta_learning_env = tuple(dict.fromkeys([*rex.DEFAULT_META_CREDENTIAL_ENVS, *requested_envs]))
    meta_learning_env_aliases = rex.DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy else ()
    meta_learning_sandbox_spec = replace(
        sandbox_spec,
        network=args.meta_learning_network,
        env_passthrough=meta_learning_env,
        env_aliases=meta_learning_env_aliases,
        add_host_gateway=args.meta_learning_add_host_gateway or args.meta_learning_host_proxy,
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
            require_complete_validation=not args.allow_incomplete_validation,
        ),
        sandbox_spec=sandbox_spec,
        meta_learning_sandbox_spec=meta_learning_sandbox_spec,
        meta_sandbox_rebuild_enabled=not args.disable_meta_sandbox_rebuild,
        use_docker=not args.local_dev,
    )


def _web_search_providers(args: argparse.Namespace) -> dict[str, object]:
    providers: dict[str, object] = {}
    for engine in args.web_search_engines:
        if engine == "tavily":
            providers[engine] = TavilySearchProvider.from_env(env_var=args.tavily_api_key_env)
        elif engine == "semantic_scholar":
            providers[engine] = SemanticScholarSearchProvider.from_env(env_var=args.semantic_scholar_api_key_env)
    return providers


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
            "docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile ops/docker"
        )


if __name__ == "__main__":
    raise SystemExit(main())
