"""Shared synthetic sandbox/snapshot fixtures for tool and pipeline tests."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from hl_trader.environment.snapshot import finalize_snapshot_dir

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "configs" / "agent_output_template"
TS_CODE = "000001.SZ"

STRATEGY_MAIN = '''
import os
from pathlib import Path
import pandas as pd

SNAPSHOT_DIR = Path(os.environ.get("MQ_SNAPSHOT_DIR", "/mnt/snapshot"))


def factor_fixture_top():
    return None


def generate_candidates() -> pd.DataFrame:
    daily = pd.read_parquet(SNAPSHOT_DIR / "daily.parquet")
    code = sorted(daily["ts_code"].astype(str).unique())[0]
    return pd.DataFrame(
        [{"ts_code": code, "factor_score": 1.0, "factor_fixture_top": 1.0,
          "reason": "fixture_top", "source_artifacts": ["daily_window"]}]
    )
'''

STRATEGY_FACTORS = {
    "factors": [
        {"id": "fixture_top", "function": "factor_fixture_top", "description": "fixture factor", "lookback_days": 1, "direction": "positive", "rationale": "fixture rationale"}
    ]
}
STRATEGY_PRIOR = {
    "rules": [{"id": "r1", "text": "negative regulatory news lowers the score", "evidence": "announcements", "effect": "lower_score"}]
}

# (trade_date, open, close) for the single fixture stock across all periods.
PRICE_ROWS = [
    ("20211008", 10.0, 10.5),
    ("20211011", 10.6, 11.0),
    ("20211230", 11.1, 12.0),
    ("20220104", 12.0, 12.4),
    ("20220105", 12.5, 13.0),
    ("20220331", 13.1, 14.0),
    ("20220406", 13.2, 13.4),
    ("20220630", 13.5, 13.8),
    ("20260105", 14.0, 14.5),
    ("20260106", 14.6, 15.0),
    ("20260331", 15.1, 16.0),
]
TRADING_DAYS = [row[0] for row in PRICE_ROWS]


def nl_score_response(ts_code: str = TS_CODE, nl_score: float = 0.8) -> str:
    return json.dumps(
        {
            "ts_code": ts_code,
            "nl_score": nl_score,
            "confidence": 0.9,
            "risk_tags": [],
            "applied_prior_ids": ["r1"],
            "evidence_ids": [],
        }
    )


def write_strategy(agent_output: Path) -> None:
    (agent_output / "factor" / "main.py").write_text(STRATEGY_MAIN, encoding="utf-8")
    (agent_output / "factor" / "factors.json").write_text(json.dumps(STRATEGY_FACTORS), encoding="utf-8")
    (agent_output / "nl_prior" / "prior.json").write_text(json.dumps(STRATEGY_PRIOR), encoding="utf-8")


def make_snapshot_dir(out_dir: Path, *, decision_date: str, kind: str) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{"trade_date": decision_date, "ts_code": TS_CODE, "open": 10.0, "close": 10.5, "vol": 100000.0, "amount": 1050000.0}]
    ).to_parquet(out_dir / "daily.parquet", index=False)
    pd.DataFrame([{"ts_code": TS_CODE, "name": "平安银行", "name_asof": "平安银行", "exchange": "SZSE"}]).to_parquet(
        out_dir / "universe.parquet", index=False
    )
    pd.DataFrame([{"dataset": "margin_secs", "trade_date": decision_date, "ts_code": TS_CODE}]).to_parquet(
        out_dir / "events.parquet", index=False
    )
    pd.DataFrame(columns=["dataset", "ts_code", "available_at", "end_date", "bz_item"]).to_parquet(
        out_dir / "fundamentals.parquet", index=False
    )
    pd.DataFrame(
        columns=["text_id", "dataset", "ts_codes", "title", "available_at", "source_hash", "library_file"]
    ).to_parquet(out_dir / "text_index.parquet", index=False)
    (out_dir / "text_library").mkdir(exist_ok=True)
    return finalize_snapshot_dir(out_dir, kind=kind, decision_date=decision_date)


def make_replay_dir(out_dir: Path, *, start: str, end: str, label: str) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"trade_date": d, "ts_code": TS_CODE, "open": o, "close": c, "up_limit": o * 1.2, "down_limit": o * 0.8, "is_suspended": False}
        for d, o, c in PRICE_ROWS
        if start <= d <= end
    ]
    pd.DataFrame(rows).to_parquet(out_dir / "daily.parquet", index=False)
    return finalize_snapshot_dir(out_dir, kind="replay_slot", label=label, period_start=start, period_end=end)


class FakeSnapshotProvider:
    """Synthetic SnapshotProvider for pipeline tests."""

    def decision_snapshot(self, decision_time: datetime, out_dir: Path) -> dict[str, object]:
        return make_snapshot_dir(Path(out_dir), decision_date=decision_time.strftime("%Y%m%d"), kind="decision_input")

    def replay_slot(self, start: str, end: str, out_dir: Path, *, label: str) -> dict[str, object]:
        return make_replay_dir(Path(out_dir), start=start, end=end, label=label)
