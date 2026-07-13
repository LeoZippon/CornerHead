#!/usr/bin/env python3
"""Standalone full-window replay benchmark (engine-level, no LLM agent).

Reuses an existing run's prepared snapshot views and a frozen strategy artifact
to drive ``run_main_ctx_replay`` inside a real Docker sandbox, so engine
optimizations can be measured and parity-checked (identical return stats and
order stream) without spending tokens. NL is not served: only use strategies
whose recorded backtests show ``nl_calls == 0``.

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

from autotrade.environment.replay_stats import compute_return_stats
from autotrade.environment.broker import (
    BrokerProfile,
    load_corporate_actions_by_date,
    load_shortable_by_date,
    load_shortable_codes,
)
from autotrade.environment.executor import DockerExecutor
from autotrade.environment.main_ctx_engine import MainPolicyRunner, run_main_ctx_replay
from autotrade.environment.replay_market import ParquetMinuteReplaySource
from autotrade.environment.sandbox import DockerSandbox, LocalSandbox, SandboxSpec, link_copytree
from autotrade.environment.runtime import write_json_atomic


def _profile_kwargs(record: dict[str, object]) -> dict[str, object]:
    import dataclasses

    names = {field.name for field in dataclasses.fields(BrokerProfile)}
    return {key: value for key, value in record.items() if key in names}


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
    parser.add_argument("--offsession-tick-minutes", type=int, default=None, help="override the manifest value")
    parser.add_argument("--intraday-decision-minutes", type=int, default=None, help="override (engine support required)")
    parser.add_argument(
        "--eager-minutes",
        action="store_true",
        help="load the whole minute file before replay (legacy A/B baseline)",
    )
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    models_dir = args.models or Path(f"{args.strategy}.models")
    work_dir = repo_root / ".runtime" / "bench" / f"{args.label}_{int(time.time())}"
    out_path = args.out or (repo_root / ".runtime" / "bench" / f"{args.label}.json")

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
    spec = SandboxSpec.from_host_fraction()
    if args.image:
        spec = __import__("dataclasses").replace(spec, image=args.image)
    sandbox.write_runtime_env(mode="docker", sandbox_spec=spec)
    docker = DockerSandbox(sandbox, spec)
    docker.start()
    result_payload: dict[str, object] = {"label": args.label, "source_run": str(args.source_run)}
    try:
        docker.bind_snapshot_view("valid_decision_input")
        executor = DockerExecutor(docker.container, paths)
        replay_daily = pd.read_parquet(paths.valid / "daily.parquet")
        minute_file = paths.valid / "intraday_1min.parquet"
        replay_minutes = (
            pd.read_parquet(minute_file)
            if args.eager_minutes and minute_file.exists()
            else None
        )
        minute_source = (
            ParquetMinuteReplaySource(minute_file, include_timeview_rows=True)
            if not args.eager_minutes and minute_file.exists()
            else None
        )
        decision_time = str(manifest["valid_decision_time"])
        offsession = (
            int(args.offsession_tick_minutes)
            if args.offsession_tick_minutes is not None
            else int(manifest.get("offsession_tick_minutes", 15))
        )
        engine_kwargs: dict[str, object] = {}
        if args.intraday_decision_minutes is not None:
            engine_kwargs["intraday_decision_minutes"] = int(args.intraday_decision_minutes)
        with MainPolicyRunner(
            executor,
            paths,
            timeout_seconds=900.0,
            decision_time=decision_time,
            replay_granularity="minute" if replay_minutes is not None or minute_source is not None else "daily",
            nl_service=None,
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
                main_policy=policy,
                replay_intraday_1min=replay_minutes,
                replay_minute_source=minute_source,
                auction_enabled=bool(manifest.get("auction_enabled", True)),
                auction_preopen_time=manifest.get("auction_preopen_time", "09:15"),
                auction_decision_time=str(manifest.get("auction_decision_time", "09:25")),
                auction_close_time=(manifest.get("auction_close_time", "14:57") or None),
                afterhours_decision_time=(manifest.get("afterhours_decision_time") or None),
                execution_lag_bars=int(manifest.get("execution_lag_bars", 2)),
                offsession_tick_minutes=offsession,
                max_seconds_per_trading_day=None,  # benchmark: no load-dependent aborts
                enforce_substep_timeout=False,
                enforce_substep_coverage=False,
                timeview_enabled=bool(manifest.get("timeview_enabled", True)),
                snapshot_dir=paths.current_snapshot,
                replay_dir=paths.valid,
                **engine_kwargs,
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
    finally:
        if "minute_source" in locals() and minute_source is not None:
            minute_source.close()
        docker.stop()
        if not args.keep_workdir:
            shutil.rmtree(work_dir, ignore_errors=True)
    write_json_atomic(out_path, result_payload)
    print(json.dumps({k: result_payload.get(k) for k in ("label", "wall_seconds", "phase_seconds", "order_count", "orders_sha256")}, ensure_ascii=False, default=str))
    print(f"full result: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
