"""Agent-visible experiment facts: the manifest/runtime_env/data_summary projection.

``build_experiment_facts`` is the visibility contract for what a Fold or
meta-learning session may know about its own run (budgets, snapshot windows,
broker replay policy, artifact contract, runtime tools). Pure data shaping —
the prompt text that wraps it lives in ``prompts.py``.
"""

from __future__ import annotations

from collections.abc import Mapping

from autotrade.environment.identity import agent_visible_ref
from autotrade.environment.tools.web_search import META_SEARCH_PERSPECTIVES

EXPERIMENT_FACTS_SCHEMA_VERSION = 1


def build_experiment_facts(
    *,
    manifest: Mapping[str, object],
    runtime_env: Mapping[str, object] | None = None,
    data_summary: Mapping[str, object] | None = None,
    max_llm_calls: int | None = None,
    context_compaction: Mapping[str, object] | None = None,
    model_artifacts_empty: bool | None = None,
) -> dict[str, object]:
    """Build the short Agent-visible operational-facts projection.

    This is a convenience index, not a security boundary. It intentionally
    omits test/held-out schedule fields; exact trusted details remain in the
    referenced JSON files.
    """

    runtime_env = runtime_env or {}
    data_summary = data_summary or {}
    kind = str(manifest.get("kind") or "fold")
    is_meta = kind == "meta_learning"
    snapshot_config = _as_mapping(manifest.get("snapshot_config"))
    if is_meta:
        experiment_parameters = _as_mapping(manifest.get("experiment_parameters"))
        snapshot_config = _as_mapping(experiment_parameters.get("snapshot_config")) or snapshot_config
        fold_period = experiment_parameters.get("fold_period")
    else:
        fold_period = manifest.get("fold_period")

    facts: dict[str, object] = {
        "identity": _compact_mapping(
            {
                "facts_schema_version": EXPERIMENT_FACTS_SCHEMA_VERSION,
                "experiment_id": manifest.get("experiment_id"),
                "run_id": manifest.get("run_id"),
                "epoch_id": manifest.get("epoch_id"),
                "session_kind": kind,
                "fold_sequence_or_opaque_id": _opaque_fold_ref(manifest.get("fold_id")),
                "phase": None if is_meta else manifest.get("phase"),
            }
        ),
        "source_refs": {
            "run_manifest_ref": "/mnt/artifacts/run_manifest.json",
            "runtime_env_ref": str(manifest.get("runtime_env_ref") or "/mnt/artifacts/runtime_env.json"),
            "data_summary_ref": str(manifest.get("data_summary_ref") or "/mnt/artifacts/data_summary.json"),
        },
        "visibility_policy": {
            "train_visible": True,
            "valid_visible": True,
            "test_visible": False,
            "heldout_visible": False,
            "hidden_schedule_redacted": True,
            "formal_strategy_read_roots": ["/mnt/snapshot", "/mnt/agent/output", "/mnt/agent/models"],
        },
        "visible_timeline": _visible_timeline(
            manifest=manifest,
            data_summary=data_summary,
            snapshot_config=snapshot_config,
            fold_period=fold_period,
            is_meta=is_meta,
        ),
        "budgets": _budget_facts(manifest, max_llm_calls=max_llm_calls, context_compaction=context_compaction),
        "paths": _path_facts(),
        "artifact_contract": _artifact_contract_facts(
            manifest, model_artifacts_empty=model_artifacts_empty, is_meta=is_meta
        ),
        "data_profile": _data_profile_facts(data_summary, include_dates=not is_meta),
        "broker_replay": _broker_replay_facts(manifest),
        "runtime_tools": _runtime_tool_facts(runtime_env, manifest=manifest, is_meta=is_meta),
    }
    if is_meta:
        facts["meta_learning"] = _meta_learning_facts(manifest)
    return _compact_mapping(facts)



def _visible_timeline(
    *,
    manifest: Mapping[str, object],
    data_summary: Mapping[str, object],
    snapshot_config: Mapping[str, object],
    fold_period: object,
    is_meta: bool,
) -> dict[str, object]:
    replay_policy = _replay_policy(data_summary)
    timeline = {
        "fold_period": fold_period,
        "snapshot_windows": _snapshot_windows(snapshot_config),
        "replay_policy": replay_policy,
    }
    if is_meta:
        timeline["sample_window_only"] = True
        timeline["exact_sample_coverage_ref"] = "/mnt/artifacts/data_summary.json"
    else:
        fold = _as_mapping(manifest.get("fold"))
        timeline.update(
            {
                "current_decision_time": manifest.get("valid_decision_time"),
                "visible_input_window": fold.get("input_window"),
                "visible_validation_replay_period": fold.get("validation_period"),
            }
        )
    return _compact_mapping(timeline)


def _snapshot_windows(snapshot_config: Mapping[str, object]) -> dict[str, object]:
    windows = _as_mapping(snapshot_config.get("decision_windows"))
    return _compact_mapping(
        {
            "daily_months": windows.get("daily_months"),
            "fundamentals_months": windows.get("fundamentals_months"),
            "events_months": windows.get("events_months"),
            "macro_months": windows.get("macro_months"),
            "text_months": windows.get("text_months"),
            "intraday_trade_days": windows.get("intraday_trade_days"),
        }
    )


def _replay_policy(data_summary: Mapping[str, object]) -> dict[str, object]:
    visible_files = _visible_file_names(data_summary)
    return {
        "include_minutes": "intraday_1min.parquet" in visible_files,
        "include_events": "events.parquet" in visible_files,
        "include_text": "text_index.parquet" in visible_files,
        "minute_when_available_else_daily_fallback": True,
        "forced_liquidation_last_day": True,
    }


def _budget_facts(
    manifest: Mapping[str, object],
    *,
    max_llm_calls: int | None,
    context_compaction: Mapping[str, object] | None,
) -> dict[str, object]:
    return _compact_mapping(
        {
            "fold_deadline_at": manifest.get("fold_deadline_at"),
            "finalize_before_deadline_seconds": manifest.get("finalize_before_deadline_seconds"),
            "max_steps": manifest.get("max_steps"),
            "max_llm_calls": max_llm_calls,
            "per_call_timeout_seconds": manifest.get("per_call_timeout_seconds"),
            "max_backtests_per_fold": manifest.get("max_backtests_per_fold"),
            "backtest_wall_excluded_from_deadline": True,
            "context_compaction": context_compaction,
        }
    )


def _path_facts() -> dict[str, object]:
    return {
        "snapshot_dir": "/mnt/snapshot",
        "train_dir": "/mnt/snapshots/train",
        "valid_dir": "/mnt/snapshots/valid",
        "workspace_dir": "/mnt/agent/workspace",
        "output_dir": "/mnt/agent/output",
        "models_dir": "/mnt/agent/models",
        "parent_output_dir": "/mnt/artifacts/parent_output",
        "parent_models_dir": "/mnt/artifacts/parent_models",
        "results_dir": "/mnt/artifacts/results",
        "steps_dir": "/mnt/artifacts/steps",
        "logs_dir": "/mnt/artifacts/logs",
    }


def _artifact_contract_facts(
    manifest: Mapping[str, object],
    *,
    model_artifacts_empty: bool | None,
    is_meta: bool,
) -> dict[str, object]:
    is_initial = bool(manifest.get("is_initial_artifact", manifest.get("initial_template_hash") is not None))
    parent_id = manifest.get("parent_strategy_artifact_id")
    parent = {
        "kind": "initial_template" if is_initial else "frozen_artifact",
        # Artifact ids embed the raw fold label (strategy_<epoch>_fold_<period>);
        # project them like every other agent-visible surface.
        "id": agent_visible_ref(parent_id, prefix="strategy_ref") if parent_id else None,
        "strategy_hash": manifest.get("parent_strategy_artifact_hash") or manifest.get("initial_template_hash"),
        "model_hash": manifest.get("parent_model_artifact_hash"),
        "model_artifacts_empty": model_artifacts_empty,
    }
    return _compact_mapping(
        {
            "required_entry": "output/main.py",
            "strategy_entry_function": "main",
            "model_artifacts_allowed": True,
            "workspace_frozen": False,
            "parent": _compact_mapping(parent),
            "modification_constraints": manifest.get("modification_constraints"),
            "acceptance_rules": None if is_meta else manifest.get("acceptance_rules"),
            # Semantics: max_drawdown + complete validation are HARD gates;
            # min_return / min_sharpe are targets — shortfalls freeze WITH a
            # recorded warning instead of resetting the fold.
            "acceptance_semantics": None if is_meta else "drawdown+complete=hard; return/sharpe=warn-only targets",
            "step_tree_enabled": manifest.get("step_tree_enabled"),
            "record_failed_attempts": manifest.get("record_failed_attempts"),
            "nl_failure_policy": manifest.get("nl_failure_policy"),
        }
    )


def _data_profile_facts(data_summary: Mapping[str, object], *, include_dates: bool) -> dict[str, object]:
    views = _as_mapping(data_summary.get("views"))
    compact_views: dict[str, object] = {}
    for name in ("snapshot", "train", "valid"):
        view = _as_mapping(views.get(name))
        if not view:
            continue
        detailed = name == "snapshot"
        compact_views[name] = _compact_mapping(
            {
                "mount_path": view.get("mount_path"),
                "decision_time": view.get("decision_time") if include_dates else None,
                "period_start": view.get("period_start") if include_dates else None,
                "period_end": view.get("period_end") if include_dates else None,
                "domain_windows": view.get("domain_windows") if include_dates else None,
                "large_tables": view.get("large_tables"),
                "files": [
                    _compact_file_facts(item, detailed=detailed, include_dates=include_dates)
                    for item in _as_list(view.get("files"))
                ],
            }
        )
    return _compact_mapping(
        {
            "views": compact_views,
            "large_table_guidance": data_summary.get("large_table_guidance"),
        }
    )


def _compact_file_facts(item: object, *, detailed: bool, include_dates: bool) -> dict[str, object]:
    record = _as_mapping(item)
    base = {
        "path": record.get("path"),
        "mount_path": record.get("mount_path"),
        "rows": record.get("rows"),
        "size_bytes": record.get("size_bytes"),
        "date_ranges": record.get("date_ranges") if include_dates else None,
        "large_table": record.get("large_table"),
    }
    if detailed:
        base.update(
            {
                "column_count": record.get("column_count"),
                "key_columns": _limit_list(record.get("key_columns"), 60),
                "metadata_null_counts": record.get("metadata_null_counts"),
            }
        )
    return _compact_mapping(base)


def _broker_replay_facts(manifest: Mapping[str, object]) -> dict[str, object]:
    profile = _as_mapping(manifest.get("broker_profile"))
    if not profile:
        experiment_parameters = _as_mapping(manifest.get("experiment_parameters"))
        profile = _as_mapping(experiment_parameters.get("broker_profile"))
    concentration = _compact_mapping(
        {
            "max_total_holdings": profile.get("max_total_holdings"),
            "max_single_name_weight": profile.get("max_single_name_weight"),
        }
    )
    return _compact_mapping(
        {
            "profile_id": profile.get("profile_id"),
            "stock_initial_cash": profile.get("stock_initial_cash"),
            "credit_initial_cash": profile.get("credit_initial_cash"),
            "commission_bps": profile.get("commission_bps"),
            "min_commission_cny": profile.get("min_commission_cny"),
            "stamp_duty_policy": _compact_mapping(
                {
                    "sell_bps_before_cutover": profile.get("stamp_duty_sell_bps_before_cutover"),
                    "sell_bps_from_cutover": profile.get("stamp_duty_sell_bps_from_cutover"),
                    "cutover_date": profile.get("stamp_duty_cutover_date"),
                }
            ),
            "slippage_bps": profile.get("slippage_bps"),
            "t_plus_one": True,
            "order_lot_size": 100,
            "price_limit_enforced": True,
            "suspension_enforced": True,
            "corporate_actions": profile.get("corporate_actions"),
            "dividend_tax_rate": profile.get("dividend_tax_rate"),
            "execution_lag_bars": manifest.get("execution_lag_bars"),
            "auction_close_time": manifest.get("auction_close_time"),
            "afterhours_decision_time": manifest.get("afterhours_decision_time"),
            "offsession_tick_minutes": manifest.get("offsession_tick_minutes"),
            "decision_max_sim_minutes": manifest.get("decision_max_sim_minutes"),
            "backtest_max_seconds_per_decision": manifest.get("backtest_max_seconds_per_decision"),
            "backtest_max_seconds_per_trading_day": manifest.get("backtest_max_seconds_per_trading_day"),
            "nl_max_calls_per_decision_day": manifest.get("nl_max_calls_per_decision_day"),
            "nl_max_calls_per_backtest": manifest.get("nl_max_calls_per_backtest"),
            "short_inventory_mode": profile.get("short_inventory_mode") or manifest.get("short_inventory_mode"),
            "credit_target_source": "events.parquet dataset=margin_secs (temporary shared gate for 担保品买入, 融资 and 融券)",
            "fin_margin_ratio": profile.get("fin_margin_ratio"),
            "slo_margin_ratio": profile.get("slo_margin_ratio"),
            "fin_rate_annual": profile.get("fin_rate_annual"),
            "slo_rate_annual": profile.get("slo_rate_annual"),
            "credit_rates_are_assumed": profile.get("credit_rates_are_assumed"),
            "assure_ratio": profile.get("assure_ratio"),
            "fin_max_quota": profile.get("fin_max_quota"),
            "slo_max_quota": profile.get("slo_max_quota"),
            "maintenance_closeout_ratio": profile.get("maintenance_closeout_ratio"),
            "maintenance_withdraw_ratio": profile.get("maintenance_withdraw_ratio"),
            "concentration_limits": concentration or None,
        }
    )


def _runtime_tool_facts(
    runtime_env: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    is_meta: bool,
) -> dict[str, object]:
    tools = _as_mapping(runtime_env.get("tools"))
    available = sorted(name for name, record in tools.items() if _as_mapping(record).get("available") is True)
    missing = sorted(name for name, record in tools.items() if _as_mapping(record).get("available") is False)
    sandbox_spec = _as_mapping(runtime_env.get("sandbox_spec")) or _as_mapping(manifest.get("sandbox_spec"))
    sandbox_runtime = _as_mapping(manifest.get("sandbox_runtime"))
    proxy_aliases = [
        str(item.get("container_env"))
        for item in _as_list(sandbox_runtime.get("active_env_aliases"))
        if isinstance(item, Mapping) and str(item.get("container_env", "")).startswith("AT_PROXY_")
    ]
    active_env_passthrough = [
        str(name)
        for name in _as_list(sandbox_runtime.get("active_env_passthrough"))
        if str(name).strip()
    ]
    network = runtime_env.get("network") or sandbox_spec.get("network")
    web_search_engines = manifest.get("web_search_engines") if is_meta else None
    return _compact_mapping(
        {
            "python": runtime_env.get("python"),
            "python_packages": _compact_python_packages(runtime_env.get("python_packages")),
            "cli_tools_available": available,
            "cli_tools_missing": missing,
            "network_mode": network,
            "web_search_engines": web_search_engines,
            "credential_env_names_active": active_env_passthrough,
            "proxy_alias_names_active": proxy_aliases,
            "network_install_policy": {
                "ordinary_fold": "no_network_prebuilt_dependencies_only",
                "meta_learning": (
                    "workspace_only_if_network_enabled"
                    if is_meta and str(network or "none") != "none"
                    else "blocked_unless_runtime_env_enables_network"
                ),
            },
        }
    )


def _compact_python_packages(value: object) -> dict[str, object]:
    packages = _as_mapping(value)
    return {
        str(name): _compact_mapping(
            {
                "version": _as_mapping(record).get("version"),
                "available": _as_mapping(record).get("available"),
            }
        )
        for name, record in packages.items()
    }


def _meta_learning_facts(manifest: Mapping[str, object]) -> dict[str, object]:
    development_inputs = _as_mapping(manifest.get("development_inputs"))
    return _compact_mapping(
        {
            "taste_output_path": manifest.get("taste_output") or "/mnt/agent/workspace/taste.md",
            "taste_injected_scope": "current_epoch_fold_prompts",
            "development_inputs": {
                key: value
                for key, value in development_inputs.items()
                if key in {"development_history", "experiment_ledger_full", "meta_learning_memory"}
            },
            "previous_taste_available": bool(development_inputs.get("previous_taste")),
            "history_available": bool(development_inputs),
            "required_web_search_perspectives": list(META_SEARCH_PERSPECTIVES),
            "sample_window_only": True,
            "backtest_allowed": False,
            "meta_learning_directive_present": bool(str(manifest.get("meta_learning_directive") or "").strip()),
        }
    )


def _visible_file_names(data_summary: Mapping[str, object]) -> set[str]:
    names: set[str] = set()
    for view in _as_mapping(data_summary.get("views")).values():
        for item in _as_list(_as_mapping(view).get("files")):
            path = str(_as_mapping(item).get("path") or "")
            if path:
                names.add(path.rsplit("/", 1)[-1])
    return names


def _opaque_fold_ref(value: object) -> str | None:
    if value is None or str(value) == "":
        return None
    return agent_visible_ref(value, prefix="fold_ref")


def _as_mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _limit_list(value: object, limit: int) -> list[object]:
    seq = _as_list(value)
    return seq[:limit]


def _compact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            item = _compact_mapping(item)
        elif isinstance(item, list):
            item = [_compact_mapping(x) if isinstance(x, Mapping) else x for x in item]
        if item is None or item == "" or item == {} or item == []:
            continue
        compact[str(key)] = item
    return compact


