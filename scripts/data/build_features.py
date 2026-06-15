#!/usr/bin/env python3
"""PIT feature-layer entrypoint (docs/environment_design.md 2.5).

Subcommands are the three used by the nightly cn_nightly_feature_build job:
build-fundamental-events, audit-fundamental-events, and build-features.
Experiment orchestration lives in scripts/experiments/run_experiment.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from hl_trader.environment.features import (
    FUNDAMENTAL_EVENT_DATASETS,
    DailyPITFeatureBuilder,
    FeatureBuildConfig,
    FundamentalEventsBuilder,
    FundamentalEventsConfig,
    audit_fundamental_events,
    complete_months_for_date_window,
)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PIT feature-layer commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-features", help="build next-day tradable daily PIT features")
    build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    build.add_argument("--output-root", type=Path, default=Path("data/features"))
    build.add_argument("--dataset", default="daily_alpha")
    build.add_argument("--start-date", required=True, help="YYYYMMDD or ISO date.")
    build.add_argument("--end-date", required=True, help="YYYYMMDD or ISO date.")
    build.add_argument("--lookback-days", type=int, default=80)
    build.add_argument("--no-limit-list", action="store_true", help="Do not join optional limit_list_d events.")
    build.add_argument(
        "--fundamental-events-dir", type=Path, help="Optional PIT fundamental event directory to join into daily_alpha."
    )
    build.set_defaults(handler=run_build_features)

    fundamental = sub.add_parser("build-fundamental-events", help="build PIT-ready fundamental event partitions")
    fundamental.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    fundamental.add_argument("--output-root", type=Path, default=Path("data/features/fundamental_events"))
    fundamental.add_argument("--start-date", required=True, help="YYYYMMDD or ISO date.")
    fundamental.add_argument("--end-date", required=True, help="YYYYMMDD or ISO date.")
    fundamental.add_argument(
        "--dataset", action="append", choices=FUNDAMENTAL_EVENT_DATASETS, help="Dataset to include; repeatable."
    )
    fundamental.set_defaults(handler=run_build_fundamental_events)

    fundamental_audit = sub.add_parser("audit-fundamental-events", help="audit PIT-ready fundamental event partitions")
    fundamental_audit.add_argument("--events-root", type=Path, default=Path("data/features/fundamental_events"))
    fundamental_audit.add_argument("--start-date", required=True, help="YYYYMMDD or ISO date.")
    fundamental_audit.add_argument("--end-date", required=True, help="YYYYMMDD or ISO date.")
    fundamental_audit.add_argument(
        "--dataset", action="append", choices=FUNDAMENTAL_EVENT_DATASETS, help="Dataset to include; repeatable."
    )
    fundamental_audit.add_argument("--output", type=Path, default=Path("results/data_quality/fundamental_events_status.json"))
    fundamental_audit.add_argument(
        "--require-partitions", action="store_true", help="Fail the audit when no PIT event rows exist in the window."
    )
    fundamental_audit.set_defaults(handler=run_audit_fundamental_events)
    return parser


def run_build_features(args: argparse.Namespace) -> dict[str, object]:
    builder = DailyPITFeatureBuilder(args.raw_dir)
    features = builder.build(
        FeatureBuildConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            lookback_days=args.lookback_days,
            output_dataset=args.dataset,
            include_limit_list=not args.no_limit_list,
            fundamental_events_dir=args.fundamental_events_dir,
        )
    )
    written = builder.write_partitioned(features, args.output_root, dataset=args.dataset)
    return {
        "rows": int(len(features)),
        "partitions": len(written),
        "output_dir": str(args.output_root / args.dataset),
        "first_partition": str(written[0]) if written else None,
        "last_partition": str(written[-1]) if written else None,
    }


def run_build_fundamental_events(args: argparse.Namespace) -> dict[str, object]:
    builder = FundamentalEventsBuilder(args.raw_dir)
    events = builder.build(
        FundamentalEventsConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            datasets=tuple(args.dataset or FUNDAMENTAL_EVENT_DATASETS),
        )
    )
    written = builder.write_partitioned(
        events,
        args.output_root,
        replace_months=complete_months_for_date_window(args.start_date, args.end_date),
        replace_datasets=tuple(args.dataset or FUNDAMENTAL_EVENT_DATASETS),
    )
    return {
        "rows": int(len(events)),
        "partitions": len(written),
        "output_dir": str(args.output_root),
        "first_partition": str(written[0]) if written else None,
        "last_partition": str(written[-1]) if written else None,
    }


def run_audit_fundamental_events(args: argparse.Namespace) -> dict[str, object]:
    report = audit_fundamental_events(
        args.events_root,
        FundamentalEventsConfig(
            start_date=args.start_date,
            end_date=args.end_date,
            datasets=tuple(args.dataset or FUNDAMENTAL_EVENT_DATASETS),
        ),
        output=args.output,
        require_partitions=getattr(args, "require_partitions", False),
    )
    if report["status"] == "error":
        raise ValueError(f"fundamental event audit failed: errors={report['errors']} output={args.output}")
    return {
        "audit_status": report["status"],
        "errors": report["errors"],
        "warnings": report["warnings"],
        "rows": report["rows"],
        "output": str(args.output),
    }


if __name__ == "__main__":
    raise SystemExit(main())
