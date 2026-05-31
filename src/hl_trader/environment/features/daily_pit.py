from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hl_trader.environment.data import PITDataStore, default_tushare_contracts
from hl_trader.environment.data.pit import yyyymmdd


@dataclass(frozen=True)
class FeatureBuildConfig:
    start_date: str
    end_date: str
    lookback_days: int = 80
    output_dataset: str = "daily_alpha"
    include_limit_list: bool = True


class DailyPITFeatureBuilder:
    """Build next-day tradable daily features from raw TuShare P1 data.

    Rows are available after the source trade date close and are intended for
    orders on the next local trading day. Return and volatility features use
    the daily table's published ``pct_chg`` instead of current-snapshot
    ``adj_factor`` values, because historical adjustment factors can be
    rewritten by later corporate actions and are not PIT-safe as alpha inputs.
    """

    KEY_COLUMNS = ["trade_date", "ts_code"]

    def __init__(self, raw_dir: str | Path) -> None:
        self.raw_dir = Path(raw_dir)
        self.contracts = default_tushare_contracts()
        self.store = PITDataStore(self.raw_dir, self.contracts)

    def build(self, config: FeatureBuildConfig) -> pd.DataFrame:
        start_key = yyyymmdd(config.start_date)
        end_key = yyyymmdd(config.end_date)
        if start_key > end_key:
            return pd.DataFrame()

        trade_dates = self.store.trade_dates("daily")
        selected = [d for d in trade_dates if start_key <= d <= end_key]
        if not selected:
            return pd.DataFrame()
        first_index = max(0, trade_dates.index(selected[0]) - config.lookback_days)
        load_start = trade_dates[first_index]
        load_end = selected[-1]

        daily = self._normalize_keys(self.store.read_trade_range("daily", load_start, load_end), "daily")
        daily_basic = self._normalize_keys(self.store.read_trade_range("daily_basic", load_start, load_end), "daily_basic")
        stk_limit = self._normalize_keys(self.store.read_trade_range("stk_limit", load_start, load_end), "stk_limit")
        suspend_d = self._normalize_keys(
            self.store.read_trade_range("suspend_d", load_start, load_end, columns=["trade_date", "ts_code"]),
            "suspend_d",
            allow_empty=True,
        )

        self._assert_unique_keys(daily, "daily")
        self._assert_unique_keys(daily_basic, "daily_basic")
        self._assert_unique_keys(stk_limit, "stk_limit")

        frame = daily.merge(daily_basic, on=self.KEY_COLUMNS, how="left", suffixes=("", "_basic"))
        frame = frame.merge(stk_limit, on=self.KEY_COLUMNS, how="left", suffixes=("", "_limit"))
        frame = frame.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

        for col in [
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "pct_chg",
            "vol",
            "amount",
            "turnover_rate",
            "turnover_rate_f",
            "pe",
            "pe_ttm",
            "pb",
            "ps_ttm",
            "dv_ratio",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
            "up_limit",
            "down_limit",
        ]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")

        grouped = frame.groupby("ts_code", sort=False)
        if "pct_chg" in frame.columns:
            frame["ret_1d"] = frame["pct_chg"] / 100.0
        else:
            frame["ret_1d"] = grouped["close"].pct_change()

        frame["ret_5d"] = self._compound_trailing_return(frame, 5)
        frame["ret_20d"] = self._compound_trailing_return(frame, 20)
        frame["ret_60d"] = self._compound_trailing_return(frame, 60)
        frame["amount_ma20"] = grouped["amount"].transform(lambda s: s.rolling(20, min_periods=5).mean())
        frame["volatility_20d"] = frame.groupby("ts_code", sort=False)["ret_1d"].transform(
            lambda s: s.rolling(20, min_periods=5).std()
        )

        suspended_keys = set()
        if not suspend_d.empty:
            suspended_keys = set(zip(suspend_d["trade_date"], suspend_d["ts_code"]))
        frame["is_suspended"] = [(d, c) in suspended_keys for d, c in zip(frame["trade_date"], frame["ts_code"])]

        if config.include_limit_list and (self.raw_dir / "limit_list_d").exists():
            limit_start = max("20200102", load_start)
            limit_list = self.store.read_trade_range(
                "limit_list_d",
                limit_start,
                load_end,
                columns=["trade_date", "ts_code", "limit"],
            )
            limit_list = self._normalize_keys(limit_list, "limit_list_d", allow_empty=True)
            if not limit_list.empty:
                limit_list = limit_list.drop_duplicates(self.KEY_COLUMNS, keep="last")
                frame = frame.merge(limit_list, on=self.KEY_COLUMNS, how="left")
            else:
                frame["limit"] = pd.NA
        else:
            frame["limit"] = pd.NA

        next_trade = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
        frame = frame[frame["trade_date"].isin(selected)].copy()
        frame["feature_date"] = frame["trade_date"]
        frame["source_trade_date"] = frame["trade_date"]
        frame["tradable_date"] = frame["feature_date"].map(next_trade)
        frame = frame[frame["tradable_date"].notna()].copy()
        frame["available_at"] = frame["feature_date"].map(self._available_at_for_feature_date)
        frame["result_available_time"] = frame["available_at"]

        keep = [
            "feature_date",
            "source_trade_date",
            "tradable_date",
            "available_at",
            "result_available_time",
            "ts_code",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "pct_chg",
            "vol",
            "amount",
            "ret_1d",
            "ret_5d",
            "ret_20d",
            "ret_60d",
            "amount_ma20",
            "volatility_20d",
            "turnover_rate",
            "turnover_rate_f",
            "pe",
            "pe_ttm",
            "pb",
            "ps_ttm",
            "dv_ratio",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
            "up_limit",
            "down_limit",
            "is_suspended",
            "limit",
        ]
        return frame[[col for col in keep if col in frame.columns]].reset_index(drop=True)

    def write_partitioned(self, features: pd.DataFrame, output_root: str | Path, dataset: str = "daily_alpha") -> list[Path]:
        output_root = Path(output_root) / dataset
        output_root.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        if features.empty:
            return written
        for feature_date, group in features.groupby("feature_date", sort=True):
            path = output_root / f"feature_date={feature_date}.parquet"
            tmp = path.with_suffix(path.suffix + ".tmp")
            group.to_parquet(tmp, index=False)
            tmp.replace(path)
            written.append(path)
        return written

    def _available_at_for_feature_date(self, feature_date: str) -> str:
        dt = datetime.strptime(feature_date, "%Y%m%d").date()
        # The feature row includes daily_basic, so use the later daily_basic close-time contract.
        return self.contracts["daily_basic"].available_at(dt).isoformat()

    @classmethod
    def _normalize_keys(cls, frame: pd.DataFrame, dataset: str, allow_empty: bool = False) -> pd.DataFrame:
        missing = [col for col in cls.KEY_COLUMNS if col not in frame.columns]
        if missing and (not allow_empty or not frame.empty):
            raise ValueError(f"{dataset} missing required columns: {missing}")
        if missing:
            return pd.DataFrame(columns=cls.KEY_COLUMNS)
        frame = frame.copy()
        frame["trade_date"] = frame["trade_date"].map(yyyymmdd)
        frame["ts_code"] = frame["ts_code"].astype(str)
        return frame

    @classmethod
    def _assert_unique_keys(cls, frame: pd.DataFrame, dataset: str) -> None:
        duplicated = frame.duplicated(cls.KEY_COLUMNS, keep=False)
        if duplicated.any():
            sample = frame.loc[duplicated, cls.KEY_COLUMNS].head(3).to_dict("records")
            raise ValueError(f"{dataset} duplicate trade_date/ts_code keys would expand feature rows: {sample}")

    @staticmethod
    def _compound_trailing_return(frame: pd.DataFrame, window: int) -> pd.Series:
        def compound(values: np.ndarray) -> float:
            return float(np.prod(1.0 + values) - 1.0)

        return frame.groupby("ts_code", sort=False)["ret_1d"].transform(
            lambda s: s.rolling(window, min_periods=window).apply(compound, raw=True)
        )
