"""Shared CLI plumbing for the experiment entrypoints (docs/pipeline_design.md).

Single-sources the argparse argument groups, ``DEFAULT_*`` constants, small
manifest-summary helpers, and the Agent/meta-learning builder closures shared by
``run_experiment.py`` and ``run_audit_session.py`` so the two thin wrappers
cannot drift apart. Each script keeps only its own intentional differences
(period defaults, ``--mode`` single-session entry, Docker image preflight, etc.)
and calls into this module for everything they have in common.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

from autotrade.agent import AgentSessionConfig, AgentSessionRunner, ContextCompactionConfig
from autotrade.environment.llm import DeepSeekProxy
from autotrade.environment.sandbox import SandboxSpec
from autotrade.environment.snapshot import SnapshotConfig
from autotrade.environment.tools import ToolContext
from autotrade.environment.web_search import SemanticScholarSearchProvider, TavilySearchProvider
from autotrade.pipelines import ExperimentConfig


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

# --disable-meta-sandbox-rebuild help differs by entrypoint; keep both texts here
# so the wording stays single-sourced even though the two scripts pick different
# strings.
EXPERIMENT_META_REBUILD_HELP = (
    "Ignore meta-learning workspace/sandbox_environment.json. By default, if that file exists, "
    "Pipeline builds a derived Sandbox image and uses it for later ordinary Folds."
)
AUDIT_META_REBUILD_HELP = (
    "In --mode meta-learning, do NOT build a derived Docker image even if the "
    "session writes workspace/sandbox_environment.json (enabled by default)."
)


def _opt_help(text: str, verbose_help: bool) -> str | None:
    """Return ``text`` when the caller renders full help, else ``None``.

    ``run_experiment`` documents every flag; ``run_audit_session`` is terse and
    leaves most shared flags help-less. Gating keeps each ``--help`` identical.
    """
    return text if verbose_help else None


def require_generic_period_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Non-quarter fold periods have no safe defaults: demand explicit labels.

    Quarter labels like 2022Q1 silently mis-parse under other cadences, so both
    entrypoints must fail fast here instead of deep inside schedule building.
    """
    if args.fold_period == "quarter":
        return
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


# ---------------------------------------------------------------------------
# argparse argument groups shared by both entrypoints
# ---------------------------------------------------------------------------
def add_path_arguments(parser: argparse.ArgumentParser, repo_root: Path) -> None:
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


def add_snapshot_window_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument(
        "--window-months",
        type=int,
        default=21,
        help=_opt_help(
            "Default PIT history window in months for decision-input snapshots and Fold input windows.",
            verbose_help,
        ),
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
        help=_opt_help(
            "Number of recent visible trading days included in intraday_1min decision snapshots.",
            verbose_help,
        ),
    )


def add_model_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL, help=_opt_help("Agent main-conversation model.", verbose_help))
    parser.add_argument(
        "--nl-model",
        default=DEFAULT_NL_MODEL,
        help=_opt_help("NL Sub Agent model; defaults to deepseek-v4-flash (independent interface).", verbose_help),
    )
    parser.add_argument(
        "--compact-model",
        default=DEFAULT_COMPACT_MODEL,
        help=_opt_help("Context compaction model; defaults to deepseek-v4-flash with thinking disabled.", verbose_help),
    )
    parser.add_argument(
        "--disable-context-compact",
        action="store_true",
        help=_opt_help("Disable semantic context compaction.", verbose_help),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "max", "xhigh"),
        default="max",
        help=_opt_help(
            "DeepSeek reasoning effort for Agent and NL calls when thinking is enabled; default max.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--compact-token-threshold",
        type=int,
        default=200_000,
        help=_opt_help("Estimated context tokens that trigger semantic compaction; default 200000.", verbose_help),
    )
    parser.add_argument(
        "--compact-keep-recent-messages",
        type=int,
        default=12,
        help=_opt_help("Raw non-summary messages preserved after semantic compaction.", verbose_help),
    )
    parser.add_argument(
        "--compact-max-tokens",
        type=int,
        default=1600,
        help=_opt_help("Maximum output tokens for one compaction summary.", verbose_help),
    )
    parser.add_argument(
        "--compact-max-calls",
        type=int,
        default=8,
        help=_opt_help("Maximum semantic compaction provider calls per Agent session.", verbose_help),
    )


def add_web_search_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument(
        "--web-search-engines",
        nargs="+",
        choices=("tavily", "semantic_scholar"),
        default=("tavily", "semantic_scholar"),
        help=_opt_help(
            "Search engines exposed to Epoch-start meta-learning; the Agent chooses an engine per query.",
            verbose_help,
        ),
    )
    parser.add_argument("--tavily-api-key-env", default="TAVILY_API_KEY")
    parser.add_argument("--semantic-scholar-api-key-env", default="SEMANTIC_SCHOLAR_API_KEY")


def add_meta_sandbox_arguments(
    parser: argparse.ArgumentParser,
    *,
    verbose_help: bool,
    disable_rebuild_help: str,
) -> None:
    parser.add_argument(
        "--meta-learning-network",
        choices=("none", "bridge", "host"),
        default="bridge",
        help=_opt_help(
            "Docker network mode for Epoch-start meta-learning only; default bridge gives direct internet. "
            "Ordinary folds stay on the base sandbox spec.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-env",
        action="append",
        default=[],
        metavar="NAME",
        help=_opt_help(
            "Host environment variable name to pass into the meta-learning Docker container. "
            "Repeat for custom non-proxy variables; GITHUB_TOKEN and HF_TOKEN are included by default. "
            "Use --meta-learning-host-proxy for optional proxy aliases. Only names are recorded; values are never written to manifests.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-add-host-gateway",
        action="store_true",
        help=_opt_help(
            "Add host.docker.internal -> host-gateway for bridge-mode access to host proxy ports.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-host-proxy",
        action="store_true",
        help=_opt_help(
            "Expose host proxy values as non-standard AT_PROXY_* aliases and record the alias names "
            "in runtime_env.json. Direct internet remains the default behavior.",
            verbose_help,
        ),
    )
    parser.add_argument("--disable-meta-sandbox-rebuild", action="store_true", help=disable_rebuild_help)


def add_meta_directive_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument(
        "--meta-learning-directive",
        default="",
        help=_opt_help(
            "Optional experiment-level research direction injected into the Epoch-start meta-learning prompt.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-directive-file",
        type=Path,
        help=_opt_help(
            "Optional UTF-8 text file whose content is injected as the meta-learning research direction.",
            verbose_help,
        ),
    )


def add_acceptance_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument("--min-return", type=float, default=0.0, help=_opt_help("Minimum validation total return.", verbose_help))
    parser.add_argument("--min-sharpe", type=float, default=0.0, help=_opt_help("Minimum validation Sharpe.", verbose_help))
    parser.add_argument("--max-drawdown", type=float, default=0.25, help=_opt_help("Maximum validation drawdown.", verbose_help))


# ---------------------------------------------------------------------------
# manifest-summary helpers
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# shared config / proxy / provider builders
# ---------------------------------------------------------------------------
def build_snapshot_config(args: argparse.Namespace) -> SnapshotConfig:
    return SnapshotConfig(
        window_months=args.window_months,
        daily_window_months=args.daily_window_months,
        fundamentals_window_months=args.fundamentals_window_months,
        events_window_months=args.events_window_months,
        macro_window_months=args.macro_window_months,
        text_window_months=args.text_window_months,
        intraday_trade_days=args.intraday_trade_days,
    )


def build_meta_learning_sandbox_spec(
    args: argparse.Namespace,
    base_sandbox_spec: SandboxSpec,
    *,
    repo_root: Path,
    extra_dotenv_keys: tuple[str, ...] = (),
) -> SandboxSpec:
    """Load the required .env keys and derive the meta-learning sandbox spec.

    ``run_audit_session`` additionally loads the web-search key envs via
    ``extra_dotenv_keys``; ``run_experiment`` passes none.
    """
    requested_envs = [name.strip() for name in args.meta_learning_env if name.strip()]
    dotenv_keys = tuple(
        dict.fromkeys(
            [
                *DEFAULT_META_CREDENTIAL_ENVS,
                *(host_env for _container_env, host_env in DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy),
                *requested_envs,
                *extra_dotenv_keys,
            ]
        )
    )
    _load_dotenv_into_environ(repo_root / ".env", keys=dotenv_keys)
    meta_learning_env = tuple(dict.fromkeys([*DEFAULT_META_CREDENTIAL_ENVS, *requested_envs]))
    meta_learning_env_aliases = DEFAULT_META_PROXY_ALIASES if args.meta_learning_host_proxy else ()
    return replace(
        base_sandbox_spec,
        network=args.meta_learning_network,
        env_passthrough=meta_learning_env,
        env_aliases=meta_learning_env_aliases,
        add_host_gateway=args.meta_learning_add_host_gateway or args.meta_learning_host_proxy,
    )


class ProxyBundle(NamedTuple):
    proxy: DeepSeekProxy
    nl_proxy: DeepSeekProxy
    compact_proxy: DeepSeekProxy | None
    explore_proxy: DeepSeekProxy
    compact_config: ContextCompactionConfig


def build_proxies(args: argparse.Namespace) -> ProxyBundle:
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
    return ProxyBundle(proxy, nl_proxy, compact_proxy, explore_proxy, compact_config)


def build_web_search_providers(args: argparse.Namespace) -> dict[str, object]:
    providers: dict[str, object] = {}
    for engine in args.web_search_engines:
        if engine == "tavily":
            providers[engine] = TavilySearchProvider.from_env(env_var=args.tavily_api_key_env)
        elif engine == "semantic_scholar":
            providers[engine] = SemanticScholarSearchProvider.from_env(env_var=args.semantic_scholar_api_key_env)
    return providers


AgentFactory = Callable[[ToolContext, object, dict[str, object]], AgentSessionRunner]
MetaLearner = Callable[[ToolContext], dict[str, object]]


def build_session_builders(
    *,
    config: ExperimentConfig,
    proxies: ProxyBundle,
    web_search_providers: dict[str, object],
) -> tuple[AgentFactory, MetaLearner]:
    """Build the ordinary-Fold ``agent_factory`` and the ``meta_learner`` closure.

    Both entrypoints wire identical Agent/meta-learning sessions; only the
    captured ``config``, ``proxies`` and ``web_search_providers`` differ.
    """
    proxy = proxies.proxy
    nl_proxy = proxies.nl_proxy
    compact_proxy = proxies.compact_proxy
    explore_proxy = proxies.explore_proxy
    compact_config = proxies.compact_config

    def _llm_config_summary() -> dict[str, object]:
        return {
            "main": _proxy_summary(proxy),
            "nl": _proxy_summary(nl_proxy),
            "compact": _proxy_summary(compact_proxy),
            "explore": _proxy_summary(explore_proxy),
        }

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
            llm_config_summary=_llm_config_summary(),
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
            llm_config_summary=_llm_config_summary(),
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

    return agent_factory, meta_learner
