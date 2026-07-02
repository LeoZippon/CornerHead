#!/usr/bin/env python3
"""Experiment pipeline entrypoint (docs/pipeline_design.md).

Runs the development Fold/Epoch loop and the frozen held-out evaluation with
the real raw-data snapshot provider and the DeepSeek LLM proxy. The docs do
not prescribe a CLI; this thin wrapper only wires documented components.
"""
from __future__ import annotations

import argparse
import json
import os
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
from autotrade.environment.llm import DeepSeekProxy
from autotrade.environment.sandbox import SandboxSpec
from autotrade.environment.snapshot import SnapshotConfig
from autotrade.environment.tools import ToolContext
from autotrade.environment.web_search import SemanticScholarSearchProvider, TavilySearchProvider
from autotrade.pipelines import (
    AcceptanceRules,
    ExperimentConfig,
    ExperimentPipeline,
    RawSnapshotProvider,
    load_sse_trading_days,
)


DEFAULT_AGENT_MODEL = "deepseek-v4-pro"
DEFAULT_NL_MODEL = "deepseek-v4-flash"
DEFAULT_COMPACT_MODEL = "deepseek-v4-flash"
DEFAULT_META_CREDENTIAL_ENVS = ("GITHUB_TOKEN", "HF_TOKEN")
DEFAULT_META_PROXY_ALIASES = (
    ("AT_PROXY_HTTP", "HTTP_PROXY"),
    ("AT_PROXY_HTTPS", "HTTPS_PROXY"),
    ("AT_PROXY_ALL", "ALL_PROXY"),
    ("AT_PROXY_NO_PROXY", "NO_PROXY"),
)


def _resolve_period_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str, str, str]:
    if args.fold_period != "quarter":
        missing = [
            flag
            for flag, value in (
                ("--first-test-period", args.first_test_period),
                ("--last-test-period", args.last_test_period),
                ("--heldout-first-period", args.heldout_first_period),
                ("--heldout-last-period", args.heldout_last_period),
            )
            if not value
        ]
        if missing:
            parser.error(f"--fold-period {args.fold_period} requires explicit generic period args: {', '.join(missing)}")
        return args.first_test_period, args.last_test_period, args.heldout_first_period, args.heldout_last_period
    first_test_period = args.first_test_period or args.first_test_quarter
    last_test_period = args.last_test_period or args.last_test_quarter
    heldout_first_period = args.heldout_first_period or args.heldout_first_quarter
    heldout_last_period = args.heldout_last_period or args.heldout_last_quarter
    if not heldout_first_period or not heldout_last_period:
        parser.error("held-out period is required: pass --heldout-first-period/--heldout-last-period or legacy quarter args")
    return first_test_period, last_test_period, heldout_first_period, heldout_last_period


def _proxy_summary(proxy: object | None) -> dict[str, object] | None:
    if proxy is None:
        return None
    record: dict[str, object] = {
        "provider": getattr(proxy, "provider", "unknown"),
        "model": getattr(proxy, "model", "unknown"),
    }
    config = getattr(getattr(proxy, "client", None), "config", None)
    if config is not None:
        for name in (
            "base_url",
            "thinking_enabled",
            "reasoning_effort",
            "max_tokens",
            "temperature",
            "timeout_seconds",
            "max_retries",
        ):
            record[name] = getattr(config, name, None)
    return record


def _session_config_summary(config: AgentSessionConfig, *, compact_enabled: bool) -> dict[str, object]:
    compact = config.context_compaction
    return {
        "finalize_before_deadline_seconds": config.finalize_before_deadline_seconds,
        "per_call_timeout_seconds": config.per_call_timeout_seconds,
        "max_llm_calls": config.max_llm_calls,
        "max_steps": config.max_steps,
        "max_history_messages": config.max_history_messages,
        "trim_token_threshold": config.trim_token_threshold,
        "max_response_tokens": config.max_response_tokens,
        "context_summary_max_items": config.context_summary_max_items,
        "context_summary_max_chars": config.context_summary_max_chars,
        "clear_tool_results": config.clear_tool_results,
        "tool_result_keep_recent": config.tool_result_keep_recent,
        "tool_result_clear_min_chars": config.tool_result_clear_min_chars,
        "tool_result_clear_token_threshold": config.tool_result_clear_token_threshold,
        "context_compaction": {
            "enabled": compact_enabled,
            "token_threshold": compact.token_threshold,
            "min_messages": compact.min_messages,
            "keep_recent_messages": compact.keep_recent_messages,
            "max_response_tokens": compact.max_response_tokens,
            "max_failures": compact.max_failures,
            "max_calls": compact.max_calls,
            "timeout_seconds": compact.timeout_seconds,
            "min_remaining_seconds": compact.min_remaining_seconds,
        },
    }


def _load_dotenv_into_environ(path: Path, *, keys: tuple[str, ...]) -> tuple[str, ...]:
    """Load selected .env keys into process env without logging values."""
    wanted = {key for key in keys if key}
    if not wanted or not path.exists():
        return ()
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")
        loaded.append(key)
    return tuple(loaded)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run the rolling single-agent experiment pipeline.")
    parser.add_argument("--experiment-id", required=True)
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
    parser.add_argument(
        "--fold-period",
        choices=("day", "week", "month", "quarter", "year"),
        default="quarter",
        help="Decision/replay period cadence for each Fold.",
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
    parser.add_argument(
        "--window-months",
        type=int,
        default=21,
        help="Default PIT history window in months for decision-input snapshots and Fold input windows.",
    )
    parser.add_argument("--daily-window-months", type=int, help="Override daily decision-input window in months.")
    parser.add_argument(
        "--fundamentals-window-months",
        type=int,
        help="Override fundamentals decision-input window in months.",
    )
    parser.add_argument("--events-window-months", type=int, help="Override events decision-input window in months.")
    parser.add_argument("--macro-window-months", type=int, help="Override macro decision-input window in months.")
    parser.add_argument("--text-window-months", type=int, help="Override text decision-input window in months.")
    parser.add_argument(
        "--intraday-trade-days",
        type=int,
        default=SnapshotConfig().intraday_trade_days,
        help="Number of recent visible trading days included in intraday_1min decision snapshots.",
    )
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
    parser.add_argument("--min-return", type=float, default=0.0, help="Minimum validation total return.")
    parser.add_argument("--min-sharpe", type=float, default=0.0, help="Minimum validation Sharpe.")
    parser.add_argument("--max-drawdown", type=float, default=0.25, help="Maximum validation drawdown.")
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL, help="Agent main-conversation model.")
    parser.add_argument(
        "--nl-model",
        default=DEFAULT_NL_MODEL,
        help="NL Sub Agent model; defaults to deepseek-v4-flash (independent interface).",
    )
    parser.add_argument(
        "--compact-model",
        default=DEFAULT_COMPACT_MODEL,
        help="Context compaction model; defaults to deepseek-v4-flash with thinking disabled.",
    )
    parser.add_argument("--disable-context-compact", action="store_true", help="Disable semantic context compaction.")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "max", "xhigh"),
        default="max",
        help="DeepSeek reasoning effort for Agent and NL calls when thinking is enabled; default max.",
    )
    parser.add_argument(
        "--compact-token-threshold",
        type=int,
        default=200_000,
        help="Estimated context tokens that trigger semantic compaction; default 200000.",
    )
    parser.add_argument(
        "--compact-keep-recent-messages",
        type=int,
        default=12,
        help="Raw non-summary messages preserved after semantic compaction.",
    )
    parser.add_argument(
        "--compact-max-tokens",
        type=int,
        default=1600,
        help="Maximum output tokens for one compaction summary.",
    )
    parser.add_argument(
        "--compact-max-calls",
        type=int,
        default=8,
        help="Maximum semantic compaction provider calls per Agent session.",
    )
    parser.add_argument("--local-dev", action="store_true", help="Use the local executor for development/tests only.")
    parser.add_argument("--no-thinking", action="store_true", help="Disable provider reasoning mode for Agent and NL calls.")
    parser.add_argument(
        "--meta-learning-directive",
        default="",
        help="Optional experiment-level research direction injected into the Epoch-start meta-learning prompt.",
    )
    parser.add_argument(
        "--meta-learning-directive-file",
        type=Path,
        help="Optional UTF-8 text file whose content is injected as the meta-learning research direction.",
    )
    parser.add_argument(
        "--web-search-engines",
        nargs="+",
        choices=("tavily", "semantic_scholar"),
        default=("tavily", "semantic_scholar"),
        help="Search engines exposed to Epoch-start meta-learning; the Agent chooses an engine per query.",
    )
    parser.add_argument("--tavily-api-key-env", default="TAVILY_API_KEY")
    parser.add_argument("--semantic-scholar-api-key-env", default="SEMANTIC_SCHOLAR_API_KEY")
    parser.add_argument(
        "--meta-learning-network",
        choices=("none", "bridge", "host"),
        default="bridge",
        help=(
            "Docker network mode for Epoch-start meta-learning only; default bridge gives direct internet. "
            "Ordinary folds stay on the base sandbox spec."
        ),
    )
    parser.add_argument(
        "--meta-learning-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Host environment variable name to pass into the meta-learning Docker container. "
            "Repeat for custom non-proxy variables; GITHUB_TOKEN and HF_TOKEN are included by default. "
            "Use --meta-learning-host-proxy for optional proxy aliases. Only names are recorded; values are never written to manifests."
        ),
    )
    parser.add_argument(
        "--meta-learning-add-host-gateway",
        action="store_true",
        help="Add host.docker.internal -> host-gateway for bridge-mode access to host proxy ports.",
    )
    parser.add_argument(
        "--meta-learning-host-proxy",
        action="store_true",
        help=(
            "Expose host proxy values as non-standard AT_PROXY_* aliases and record the alias names "
            "in runtime_env.json. Direct internet remains the default behavior."
        ),
    )
    parser.add_argument(
        "--disable-meta-sandbox-rebuild",
        action="store_true",
        help=(
            "Ignore meta-learning workspace/sandbox_environment.json. By default, if that file exists, "
            "Pipeline builds a derived Sandbox image and uses it for later ordinary Folds."
        ),
    )
    args = parser.parse_args()
    if args.meta_learning_directive and args.meta_learning_directive_file:
        parser.error("pass only one of --meta-learning-directive or --meta-learning-directive-file")
    meta_learning_directive = args.meta_learning_directive
    if args.meta_learning_directive_file:
        meta_learning_directive = args.meta_learning_directive_file.read_text(encoding="utf-8")
    first_test_period, last_test_period, heldout_first_period, heldout_last_period = _resolve_period_args(args, parser)

    snapshot_config = SnapshotConfig(
        window_months=args.window_months,
        daily_window_months=args.daily_window_months,
        fundamentals_window_months=args.fundamentals_window_months,
        events_window_months=args.events_window_months,
        macro_window_months=args.macro_window_months,
        text_window_months=args.text_window_months,
        intraday_trade_days=args.intraday_trade_days,
    )
    sandbox_spec = SandboxSpec.from_host_fraction()
    requested_envs = [name.strip() for name in args.meta_learning_env if name.strip()]
    dotenv_keys = tuple(
        dict.fromkeys(
            [
                *DEFAULT_META_CREDENTIAL_ENVS,
                *(host_env for _container_env, host_env in DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy),
                *requested_envs,
            ]
        )
    )
    _load_dotenv_into_environ(repo_root / ".env", keys=dotenv_keys)
    meta_learning_env = tuple(
        dict.fromkeys(
            [
                *DEFAULT_META_CREDENTIAL_ENVS,
                *requested_envs,
            ]
        )
    )
    meta_learning_env_aliases = DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy else ()
    meta_learning_add_host_gateway = args.meta_learning_add_host_gateway or args.meta_learning_host_proxy
    meta_learning_sandbox_spec = replace(
        sandbox_spec,
        network=args.meta_learning_network,
        env_passthrough=meta_learning_env,
        env_aliases=meta_learning_env_aliases,
        add_host_gateway=meta_learning_add_host_gateway,
    )
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
    # Read-only Explore sub-agent runs on the cheaper flash interface (reuses nl_proxy).
    explore_proxy = nl_proxy
    compact_config = ContextCompactionConfig(
        token_threshold=args.compact_token_threshold,
        keep_recent_messages=args.compact_keep_recent_messages,
        max_response_tokens=args.compact_max_tokens,
        max_calls=args.compact_max_calls,
    )
    web_search_providers = {}
    for engine in args.web_search_engines:
        if engine == "tavily":
            web_search_providers[engine] = TavilySearchProvider.from_env(env_var=args.tavily_api_key_env)
        elif engine == "semantic_scholar":
            web_search_providers[engine] = SemanticScholarSearchProvider.from_env(
                env_var=args.semantic_scholar_api_key_env
            )

    def session_config(manifest_data: dict[str, object]) -> AgentSessionConfig:
        return AgentSessionConfig(
            fold_deadline_at=datetime.fromisoformat(str(manifest_data["fold_deadline_at"])),
            finalize_before_deadline_seconds=config.finalize_before_deadline_seconds,
            per_call_timeout_seconds=config.per_call_timeout_seconds,
            max_steps=config.max_steps_per_fold,
            max_backtests_per_fold=config.max_backtests_per_fold,
            context_compaction=compact_config,
        )

    def agent_factory(ctx: ToolContext, fold, manifest_data: dict[str, object]) -> AgentSessionRunner:
        agent_session_config = session_config(manifest_data)
        ctx.manifest.update(
            agent_session_config=_session_config_summary(agent_session_config, compact_enabled=compact_proxy is not None),
            llm_config_summary={
                "main": _proxy_summary(proxy),
                "nl": _proxy_summary(nl_proxy),
                "compact": _proxy_summary(compact_proxy),
                "explore": _proxy_summary(explore_proxy),
            },
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
            agent_session_config=_session_config_summary(agent_session_config, compact_enabled=compact_proxy is not None),
            llm_config_summary={
                "main": _proxy_summary(proxy),
                "nl": _proxy_summary(nl_proxy),
                "compact": _proxy_summary(compact_proxy),
                "explore": _proxy_summary(explore_proxy),
            },
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
    result = pipeline.run(load_sse_trading_days(args.raw_dir))
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
