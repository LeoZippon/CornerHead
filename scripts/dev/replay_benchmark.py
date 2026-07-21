#!/usr/bin/env python3
"""Standalone full-window replay benchmark (engine-level, no LLM agent).

Reuses an existing run's prepared snapshot views and a frozen strategy artifact
to drive ``run_main_ctx_replay`` inside a real Docker sandbox, so engine
optimizations can be measured and parity-checked without spending Agent tokens.
By default NL is not served. Passing ``--nl-model`` wires the production NL
service to that provider model for a representative end-to-end replay.

Example (test2's frozen month fold):
  ~/miniconda3/envs/quant/bin/python scripts/dev/replay_benchmark.py \
    --source-run .runtime/sandboxes/test2/run_ac0514ed9b35 \
    --manifest experiments/test2/artifacts/run_ac0514ed9b35/host_run_manifest.json \
    --strategy experiments/test2/strategy_artifacts/epoch_001/strategy_epoch_001_fold_202512 \
    --label baseline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

import pandas as pd

from autotrade.environment.artifacts import load_strategy_artifact
from autotrade.environment.replay.stats import compute_return_stats
from autotrade.environment.broker import (
    BrokerProfile,
    auction_prints_by_date,
    load_corporate_actions_by_date,
    load_shortable_by_date,
    load_shortable_codes,
)
from autotrade.environment.executor import DockerExecutor
from autotrade.environment.llm import DeepSeekProxy
from autotrade.environment.replay.engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.nl.service import (
    StrategyNLService,
    cleanup_nl_rpc_files,
    prepare_nl_rpc_files,
)
from autotrade.environment.replay.market import ParquetMinuteReplaySource
from autotrade.environment.sandbox import DockerSandbox, LocalSandbox, SandboxSpec, link_copytree, probe_image_runtime
from autotrade.environment.runtime import write_json_atomic
from autotrade.environment.tools.backtest import nl_call_budget, read_replay_auction


def _optional_float(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _profile_kwargs(record: dict[str, object]) -> dict[str, object]:
    import dataclasses

    names = {field.name for field in dataclasses.fields(BrokerProfile)}
    return {key: value for key, value in record.items() if key in names}


def _snapshot_identity(path: Path) -> dict[str, object]:
    record = json.loads(path.read_text(encoding="utf-8"))
    return {key: record.get(key) for key in ("kind", "snapshot_id", "snapshot_hash", "period_start", "period_end")}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True, help="prepared run dir with snapshots/ + snapshot_views/")
    parser.add_argument("--manifest", type=Path, required=True, help="host_run_manifest.json carrying the replay knobs")
    parser.add_argument("--strategy", type=Path, required=True, help="frozen strategy artifact dir (output files)")
    parser.add_argument("--models", type=Path, default=None, help="frozen models dir (defaults to <strategy>.models if present)")
    parser.add_argument("--label", default="bench")
    parser.add_argument("--image", default=None, help="sandbox image override")
    parser.add_argument("--out", type=Path, default=None, help="result JSON path (default .runtime/bench/<label>.json)")
    parser.add_argument("--nl-model", default=None, help="enable live production NL using this provider model")
    parser.add_argument(
        "--nl-log-dir",
        type=Path,
        default=None,
        help="NL audit directory (default: timestamped sibling of the result JSON)",
    )
    parser.add_argument("--offsession-tick-minutes", type=int, default=None, help="override the manifest value")
    parser.add_argument("--intraday-decision-minutes", type=int, default=None, help="override (engine support required)")
    parser.add_argument(
        "--eager-minutes",
        action="store_true",
        help="load the whole minute file before replay (legacy A/B baseline)",
    )
    parser.add_argument("--cpu-only", action="store_true", help="do not reserve a GPU for the strategy container")
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    strategy_hash = load_strategy_artifact(args.strategy).artifact_hash
    decision_identity = _snapshot_identity(
        args.source_run / "runtime" / "snapshot_views" / "valid_decision_input" / "manifest.json"
    )
    replay_identity = _snapshot_identity(args.source_run / "snapshots" / "valid" / "manifest.json")
    models_dir = args.models or Path(f"{args.strategy}.models")
    run_stamp = time.time_ns()
    work_dir = repo_root / ".runtime" / "bench" / f"{args.label}_{run_stamp}"
    out_path = args.out or (repo_root / ".runtime" / "bench" / f"{args.label}.json")
    nl_log_dir = args.nl_log_dir or out_path.parent / f"{out_path.stem}-nl_{run_stamp}"
    timeview_enabled = bool(manifest.get("timeview_enabled", manifest.get("rolling_asof_enabled", True)))

    sandbox = LocalSandbox(work_dir)
    sandbox.prepare_layout()
    paths = sandbox.paths
    # Prepared inputs: hardlink the replay slot and the decision-input view.
    link_copytree(args.source_run / "snapshots" / "valid", paths.valid)
    view = paths.snapshot_views / "valid_decision_input"
    link_copytree(args.source_run / "runtime" / "snapshot_views" / "valid_decision_input", view)
    sandbox.install_replay_slot("train", view)
    sandbox.install_strategy_artifact(
        args.strategy, repo_root / "configs" / "agent_output_template",
        source_model_root=models_dir if models_dir.is_dir() else None,
    )
    spec = SandboxSpec.from_host_fraction(gpu=None) if args.cpu_only else SandboxSpec.from_host_fraction()
    if args.image:
        spec = __import__("dataclasses").replace(spec, image=args.image)
    sandbox.write_runtime_env(mode="docker", sandbox_spec=spec, image_probe=probe_image_runtime(spec.image))
    docker = DockerSandbox(sandbox, spec)
    result_payload: dict[str, object] = {
        "label": args.label,
        "source_run": str(args.source_run),
        "strategy": str(args.strategy),
        "strategy_artifact_hash": strategy_hash,
        "decision_snapshot": decision_identity,
        "replay_snapshot": replay_identity,
        "sandbox_spec": spec.to_record(),
    }
    if args.nl_model:
        result_payload["nl_log_dir"] = str(nl_log_dir)
    nl_service = None
    requests_host = responses_host = None
    try:
        docker.start()
        result_payload["image_id"] = docker.image_id
        docker.bind_snapshot_view("valid_decision_input")
        executor = DockerExecutor(docker.container, paths)
        replay_daily = pd.read_parquet(paths.valid / "daily.parquet")
        replay_auction = read_replay_auction(paths.valid)
        minute_file = paths.valid / "intraday_1min.parquet"
        replay_minutes = (
            pd.read_parquet(minute_file)
            if args.eager_minutes and minute_file.exists()
            else None
        )
        minute_source = (
            ParquetMinuteReplaySource(minute_file, include_timeview_rows=timeview_enabled)
            if not args.eager_minutes and minute_file.exists()
            else None
        )
        decision_time = str(manifest["valid_decision_time"])
        offsession = (
            int(args.offsession_tick_minutes)
            if args.offsession_tick_minutes is not None
            else int(manifest.get("offsession_tick_minutes", 30))
        )
        if args.nl_model:
            requests_host, responses_host = prepare_nl_rpc_files(paths.agent)
            nl_service = StrategyNLService(
                proxy=DeepSeekProxy.from_env(model=args.nl_model),
                snapshot_dir=paths.current_snapshot,
                replay_dir=paths.valid if timeview_enabled else None,
                log_dir=nl_log_dir,
                failure_policy=str(manifest.get("nl_failure_policy", "return_error_with_audit")),
                per_call_timeout_seconds=float(manifest.get("backtest_max_seconds_per_decision", 1800.0)) * 0.8,
                max_calls=nl_call_budget(manifest, replay_daily),
            )
        with MainPolicyRunner(
            executor,
            paths,
            timeout_seconds=float(manifest.get("backtest_max_seconds_per_decision", 1800.0)),
            decision_time=decision_time,
            replay_granularity="minute",
            nl_service=nl_service,
            requests_path=requests_host,
            responses_path=responses_host,
            decision_max_sim_minutes=manifest.get("decision_max_sim_minutes"),
        ) as policy:
            policy.validate_main()
            started = time.monotonic()
            replay = run_main_ctx_replay(
                replay_daily,
                BrokerProfile(**_profile_kwargs(dict(manifest["broker_profile"]))),
                shortable_codes=load_shortable_codes(view, decision_time[:10].replace("-", "")),
                shortable_by_date=load_shortable_by_date(paths.valid),
                corporate_actions_by_date=load_corporate_actions_by_date(paths.valid),
                auction_prints_by_date=auction_prints_by_date(
                    replay_auction if replay_auction is not None else pd.DataFrame()
                ),
                main_policy=policy,
                replay_intraday_1min=replay_minutes,
                replay_minute_source=minute_source,
                replay_auction_results=replay_auction,
                afterhours_decision_time=(manifest.get("afterhours_decision_time") or None),
                execution_lag_bars=int(manifest.get("execution_lag_bars", 2)),
                offsession_tick_minutes=offsession,
                intraday_decision_minutes=int(
                    args.intraday_decision_minutes
                    if args.intraday_decision_minutes is not None
                    else manifest.get("intraday_decision_minutes", 1)
                ),
                max_seconds_per_trading_day=_optional_float(
                    manifest.get("backtest_max_seconds_per_trading_day", 3600)
                ),
                timeview_enabled=timeview_enabled,
                snapshot_dir=paths.current_snapshot,
                replay_dir=paths.valid,
            )
            wall = time.monotonic() - started
        if minute_source is not None:
            minute_source.close()
            result_payload["minute_source"] = minute_source.stats()
        stats = compute_return_stats(replay)
        orders = replay.broker.get_trade_detail_data(account_type="STOCK", data_type="ORDER") + \
            replay.broker.get_trade_detail_data(account_type="CREDIT", data_type="ORDER")
        orders_digest = hashlib.sha256(
            json.dumps(orders, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        result_payload.update(
            {
                "wall_seconds": round(wall, 2),
                "replay_wall_seconds": replay.replay_wall_seconds,
                "replayed_trade_days": replay.replayed_trade_days,
                "total_ticks": replay.total_ticks,
                "intraday_ticks": replay.intraday_ticks,
                "offsession_ticks": replay.offsession_ticks,
                "phase_seconds": getattr(replay, "phase_seconds", None),
                "host_peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
                "order_count": len(orders),
                "orders_sha256": orders_digest,
                "stats": stats,
            }
        )
        if nl_service is not None:
            result_payload.update(
                {
                    "nl_calls": nl_service.calls,
                    "nl_executed_calls": nl_service.executed_calls,
                    "nl_cache_hits": nl_service.cache_hits,
                    "nl_cache_misses": nl_service.cache_misses,
                    "nl_outcome_counts": dict(sorted(nl_service.outcome_counts.items())),
                    "nl_cost": nl_service.cost_summary(),
                }
            )
    finally:
        try:
            try:
                if "minute_source" in locals() and minute_source is not None:
                    minute_source.close()
            finally:
                try:
                    if requests_host is not None and responses_host is not None:
                        cleanup_nl_rpc_files(requests_host, responses_host)
                finally:
                    if nl_service is not None:
                        nl_service.close()
        finally:
            try:
                docker.stop()
            finally:
                if not args.keep_workdir:
                    shutil.rmtree(work_dir, ignore_errors=True)
    write_json_atomic(out_path, result_payload)
    print(json.dumps({k: result_payload.get(k) for k in ("label", "wall_seconds", "phase_seconds", "order_count", "orders_sha256")}, ensure_ascii=False, default=str))
    print(f"full result: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
