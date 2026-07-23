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
    args = parser.parse_args()
    experiment_dir = args.experiments_root / args.experiment_id
    ledger = experiment_dir / "ledgers" / "experiment_ledger.jsonl"
    output_dir = args.output_dir or experiment_dir / "reports"
    # Benchmark returns come from each ledger record's frozen benchmark block
    # (computed at replay time); the report never reads the raw lake.
    summary = build_experiment_report(ledger, output_dir)
    # build_experiment_report always sets summary["status"] (ok|warning).
    result = {"output_dir": str(output_dir), **summary}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
