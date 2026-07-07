"""Provider/session wiring shared by every experiment entrypoint.

Single-sources the DeepSeek proxy bundle, web-search providers, snapshot
config, meta-learning sandbox/managed-proxy specs, and the Agent/meta-learning
session builder closures used by ``scripts/experiments/*`` and the interactive
(HITL) worker. The CLI scripts keep only argparse plumbing and re-export these
names via ``scripts/experiments/_cli.py``.

Functions take an ``args`` namespace for compatibility with argparse; any
attribute-bearing object (e.g. ``types.SimpleNamespace``) works, which is how
the interactive worker rebuilds identical wiring from persisted parameters.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

from autotrade.agent import AgentSessionConfig, AgentSessionRunner, ContextCompactionConfig
from autotrade.environment.llm import DeepSeekProxy
from autotrade.environment.managed_proxy import (
    DEFAULT_XRAY_CONFIG_ENVS,
    META_XRAY_BIN_ENV,
    META_XRAY_CONFIG_B64_ENV,
    META_XRAY_CONFIG_JSON_ENV,
    META_XRAY_CONFIG_PATH_ENV,
    ManagedProxySpec,
)
from autotrade.environment.sandbox import SandboxSpec
from autotrade.environment.snapshot import SnapshotConfig
from autotrade.environment.tools import ToolContext
from autotrade.environment.web_search import SemanticScholarSearchProvider, TavilySearchProvider

from .config import ExperimentConfig, RawSnapshotProvider
from .experiment import ExperimentPipeline


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


def build_pipeline(
    config: ExperimentConfig,
    args,
    agent_factory,
    meta_learner,
    proxies: "ProxyBundle",
) -> ExperimentPipeline:
    """Provider + pipeline wiring shared verbatim by every entrypoint."""
    return ExperimentPipeline(
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


def load_dotenv_into_environ(path: Path, *, keys: tuple[str, ...]) -> tuple[str, ...]:
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
def build_snapshot_config(args) -> SnapshotConfig:
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
    args,
    base_sandbox_spec: SandboxSpec,
    *,
    repo_root: Path,
    extra_dotenv_keys: tuple[str, ...] = (),
) -> SandboxSpec:
    """Load the required .env keys and derive the meta-learning sandbox spec.

    ``run_audit_session`` additionally loads the web-search key envs via
    ``extra_dotenv_keys``; ``run_experiment`` passes none.
    """
    meta_network = str(args.meta_learning_network)
    proxy_allowed = (
        not bool(getattr(args, "disable_meta_learning_host_proxy", False))
        and meta_network != "none"
    )
    managed_proxy_allowed = proxy_allowed and not bool(getattr(args, "disable_meta_learning_managed_proxy", False))
    requested_envs = [name.strip() for name in args.meta_learning_env if name.strip()]
    dotenv_keys = tuple(
        dict.fromkeys(
            [
                *DEFAULT_META_CREDENTIAL_ENVS,
                *(DEFAULT_XRAY_CONFIG_ENVS if managed_proxy_allowed else ()),
                *requested_envs,
                *extra_dotenv_keys,
            ]
        )
    )
    load_dotenv_into_environ(repo_root / ".env", keys=dotenv_keys)
    managed_proxy_configured = managed_proxy_allowed and _managed_xray_config_present(repo_root)
    meta_learning_env = tuple(dict.fromkeys([*DEFAULT_META_CREDENTIAL_ENVS, *requested_envs]))
    meta_learning_env_aliases = DEFAULT_META_PROXY_ALIASES if managed_proxy_configured else ()
    add_host_gateway = bool(args.meta_learning_add_host_gateway) or (
        managed_proxy_configured and meta_network == "bridge"
    )
    host_gateway_ip = _detect_docker0_ipv4() if add_host_gateway and meta_network == "bridge" else None
    return replace(
        base_sandbox_spec,
        network=meta_network,
        env_passthrough=meta_learning_env,
        env_aliases=meta_learning_env_aliases,
        add_host_gateway=add_host_gateway,
        host_gateway_ip=host_gateway_ip,
    )


def build_meta_learning_managed_proxy_spec(
    args,
    *,
    repo_root: Path,
    sandbox_spec: SandboxSpec | None = None,
) -> ManagedProxySpec:
    """Return the redacted managed-proxy policy after loading .env config names."""
    meta_network = str(sandbox_spec.network if sandbox_spec is not None else getattr(args, "meta_learning_network", "bridge"))
    proxy_allowed = (
        not bool(getattr(args, "disable_meta_learning_host_proxy", False))
        and meta_network != "none"
    )
    if meta_network == "none":
        return ManagedProxySpec(enabled=False, disabled_status="disabled_by_network_none")
    managed_proxy_allowed = proxy_allowed and not bool(getattr(args, "disable_meta_learning_managed_proxy", False))
    if managed_proxy_allowed:
        load_dotenv_into_environ(repo_root / ".env", keys=DEFAULT_XRAY_CONFIG_ENVS)
    configured = managed_proxy_allowed and _managed_xray_config_present(repo_root)
    enabled = configured
    listen_host = "127.0.0.1"
    container_host: str | None = None
    if enabled and meta_network == "bridge":
        bridge_host = sandbox_spec.host_gateway_ip if sandbox_spec is not None else _detect_docker0_ipv4()
        if bridge_host:
            listen_host = bridge_host
            container_host = bridge_host
        elif _managed_xray_config_present(repo_root):
            raise RuntimeError(
                "managed XRay config is present, but Docker bridge host IP could not be detected; "
                "use --meta-learning-network host, fix docker0 visibility, or disable managed proxy"
            )
    disabled_status = "not_configured"
    if not proxy_allowed:
        disabled_status = "disabled"
    elif not managed_proxy_allowed:
        disabled_status = "disabled"
    xray_bin = (
        str(args.meta_learning_xray_bin).strip()
        if getattr(args, "meta_learning_xray_bin", None)
        else os.environ.get(META_XRAY_BIN_ENV, "xray")
    )
    return ManagedProxySpec(
        enabled=enabled,
        xray_bin=xray_bin,
        default_config_path=str(repo_root / ".env.xray.json"),
        startup_timeout_seconds=float(getattr(args, "meta_learning_xray_startup_timeout", 15.0)),
        listen_host=listen_host,
        container_host=container_host,
        disabled_status=disabled_status,
    )


def _managed_xray_config_present(repo_root: Path) -> bool:
    if os.environ.get(META_XRAY_CONFIG_PATH_ENV, "").strip():
        return True
    if os.environ.get(META_XRAY_CONFIG_JSON_ENV, "").strip():
        return True
    if os.environ.get(META_XRAY_CONFIG_B64_ENV, "").strip():
        return True
    return (repo_root / ".env.xray.json").exists()


def _detect_docker0_ipv4() -> str | None:
    """Return the host docker0 IPv4 address for bridge containers, if visible.

    Docker's ``host-gateway`` can resolve to the daemon bridge address rather
    than the network namespace running the experiment process. The local
    docker0 address is the host endpoint that bridge containers can actually
    use to reach a host-managed proxy on this workstation class.
    """
    try:
        completed = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "docker0"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for token in completed.stdout.split():
        if "/" not in token or token.count(".") != 3:
            continue
        address = token.split("/", 1)[0].strip()
        if address:
            return address
    return None


class ProxyBundle(NamedTuple):
    proxy: DeepSeekProxy
    nl_proxy: DeepSeekProxy
    compact_proxy: DeepSeekProxy | None
    explore_proxy: DeepSeekProxy
    compact_config: ContextCompactionConfig


def build_proxies(args) -> ProxyBundle:
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


def build_web_search_providers(args) -> dict[str, object]:
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

    Every entrypoint wires identical Agent/meta-learning sessions; only the
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
            fold_directive=str(manifest_data.get("fold_directive", "")),
            system_prompt_override=str(manifest_data.get("system_prompt_override", "")),
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
