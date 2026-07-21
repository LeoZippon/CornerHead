"""Shared CLI plumbing for the experiment entrypoints (docs/pipeline_design.md).

Single-sources the argparse argument groups and ``--help`` wording shared by
``run_experiment.py`` and ``run_audit_session.py`` so the two thin wrappers
cannot drift apart. The provider/session wiring itself lives in
``autotrade.pipelines.assembly`` (also used by the interactive HITL worker)
and is re-exported here so the entrypoints keep a single import surface.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from autotrade.environment.managed_proxy import META_XRAY_BIN_ENV
from autotrade.environment.snapshot import SnapshotConfig

# Re-exported wiring shared with the interactive worker (autotrade.pipelines.assembly).
from autotrade.pipelines.assembly import (  # noqa: F401
    DEFAULT_AGENT_MODEL,
    DEFAULT_COMPACT_MODEL,
    DEFAULT_META_CREDENTIAL_ENVS,
    DEFAULT_META_PROXY_ALIASES,
    DEFAULT_NL_MODEL,
    ProxyBundle,
    build_meta_learning_managed_proxy_spec,
    build_meta_learning_sandbox_spec,
    build_pipeline,
    build_proxies,
    build_session_builders,
    build_snapshot_config,
    build_web_search_providers,
    load_dotenv_into_environ,
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


def resolve_meta_learning_directive(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if args.meta_learning_directive and args.meta_learning_directive_file:
        parser.error("pass only one of --meta-learning-directive or --meta-learning-directive-file")
    if args.meta_learning_directive_file:
        return args.meta_learning_directive_file.read_text(encoding="utf-8")
    return args.meta_learning_directive


def resolve_fold_exploration_directive(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if args.fold_exploration_directive and args.fold_exploration_directive_file:
        parser.error(
            "pass only one of --fold-exploration-directive or --fold-exploration-directive-file"
        )
    if args.fold_exploration_directive_file:
        return args.fold_exploration_directive_file.read_text(encoding="utf-8")
    return args.fold_exploration_directive


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
    parser.add_argument("--screen-exclude-st", action="store_true",
                        help="Universe screen: exclude names carrying ST at the decision anchor.")
    parser.add_argument("--screen-exclude-new-listed-days", type=int, default=0,
                        help="Universe screen: exclude stocks listed within N days of the anchor (0=off).")
    parser.add_argument("--screen-min-circ-mv-yi", type=float, default=None,
                        help="Universe screen: minimum circulating market cap (亿元).")
    parser.add_argument("--screen-max-circ-mv-yi", type=float, default=None,
                        help="Universe screen: maximum circulating market cap (亿元).")
    parser.add_argument("--screen-min-price", type=float, default=None,
                        help="Universe screen: minimum close price at the anchor.")
    parser.add_argument("--screen-max-price", type=float, default=None,
                        help="Universe screen: maximum close price at the anchor.")
    parser.add_argument("--screen-boards", nargs="+", choices=["main", "gem", "star", "bj"], default=[],
                        help="Universe screen: restrict to these boards (empty = all).")


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
            "Search engines exposed to every meta-learning session; the Agent chooses an engine per query.",
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
            "Docker network mode for meta-learning sessions only; default bridge gives direct internet. "
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
            "Only names are recorded; values are never written to manifests.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-add-host-gateway",
        action="store_true",
        help=_opt_help(
            "Add host.docker.internal -> host-gateway for bridge-mode access to managed XRay ports.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--disable-meta-learning-host-proxy",
        action="store_true",
        help=_opt_help(
            "Do not expose managed proxy values as AT_PROXY_* aliases in the meta-learning Docker container.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--disable-meta-learning-managed-proxy",
        action="store_true",
        help=_opt_help(
            "Do not start a host-managed XRay process for meta-learning, even if META_LEARNING_XRAY_* config is present.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-xray-bin",
        help=_opt_help(
            f"XRay binary for managed meta-learning proxy startup; defaults to ${META_XRAY_BIN_ENV} or xray.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--meta-learning-xray-startup-timeout",
        type=float,
        default=15.0,
        help=_opt_help(
            "Seconds to wait for the managed XRay HTTP/SOCKS ports before failing the meta-learning run.",
            verbose_help,
        ),
    )
    parser.add_argument("--disable-meta-sandbox-rebuild", action="store_true", help=disable_rebuild_help)


def add_meta_directive_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument(
        "--meta-learning-directive",
        default="",
        help=_opt_help(
            "Optional experiment-level research direction injected into each meta-learning prompt.",
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


def add_fold_exploration_directive_arguments(
    parser: argparse.ArgumentParser, *, verbose_help: bool
) -> None:
    parser.add_argument(
        "--fold-exploration-directive",
        default="",
        help=_opt_help(
            "Optional experiment-level exploration direction injected into every ordinary Fold prompt.",
            verbose_help,
        ),
    )
    parser.add_argument(
        "--fold-exploration-directive-file",
        type=Path,
        help=_opt_help(
            "Optional UTF-8 text file whose content is injected into every ordinary Fold prompt.",
            verbose_help,
        ),
    )


def add_acceptance_arguments(parser: argparse.ArgumentParser, *, verbose_help: bool) -> None:
    parser.add_argument("--min-return", type=float, default=0.0, help=_opt_help("Minimum validation total return.", verbose_help))
    parser.add_argument("--min-sharpe", type=float, default=0.0, help=_opt_help("Minimum validation Sharpe.", verbose_help))
    parser.add_argument("--max-drawdown", type=float, default=0.25, help=_opt_help("Maximum validation drawdown.", verbose_help))
