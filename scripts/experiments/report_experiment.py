#!/usr/bin/env python3
"""Render experiment result charts and summary from the experiment ledger."""
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

from autotrade.pipelines.reporting import build_experiment_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--experiments-root", type=Path, default=Path("experiments"))
    parser.add_argument("--output-dir", type=Path, help="Defaults to <experiment>/reports/.")
    parser.add_argument("--benchmark-code", default="000300.SH", help="Benchmark index code; default is CSI 300 000300.SH.")
    parser.add_argument("--benchmark-raw-dir", type=Path, help="Raw data root containing index_daily/; defaults to auto-detected data/raw.")
    parser.add_argument("--no-benchmark", action="store_true", help="Disable benchmark/active-return overlays.")
    args = parser.parse_args()
    experiment_dir = args.experiments_root / args.experiment_id
    ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    output_dir = args.output_dir or experiment_dir / "reports"
    summary = build_experiment_report(
        ledger,
        output_dir,
        benchmark_code=None if args.no_benchmark else args.benchmark_code,
        benchmark_raw_dir=args.benchmark_raw_dir,
    )
    # build_experiment_report sets summary["status"] (ok|warning); default to ok.
    result = {"output_dir": str(output_dir), **summary}
    result.setdefault("status", "ok")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
