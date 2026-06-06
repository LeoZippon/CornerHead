from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hl_trader.environment.data import PITDataStore, default_tushare_contracts
from hl_trader.environment.data.pit import yyyymmdd
from .fundamental_events import read_fundamental_events


@dataclass(frozen=True)
class FeatureBuildConfig:
    start_date: str
    end_date: str
    lookback_days: int = 80
    output_dataset: str = "daily_alpha"
    include_limit_list: bool = True
    fundamental_events_dir: Path | None = None


class DailyPITFeatureBuilder:
    """Build next-day tradable daily features from raw TuShare P1 data.

    Rows are available after the source trade date close and are intended for
    orders on the next local trading day. Return and volatility features use
    the daily table's published ``pct_chg`` instead of current-snapshot
    ``adj_factor`` values, because historical adjustment factors can be
    rewritten by later corporate actions and are not PIT-safe as alpha inputs.
    """

    KEY_COLUMNS = ["trade_date", "ts_code"]
    LIMIT_LIST_D_FEATURE_COLUMNS = ["trade_date", "ts_code", "limit"]
    LIMIT_LIST_D_RAW_ONLY_COLUMNS = frozenset({
        "limit_amount",
        "fd_amount",
        "first_time",
        "last_time",
        "open_times",
        "strth",
        "limit_order",
    })

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
            # Only the stable daily limit status is admitted; seal amount/timing details stay raw/audit-only.
            limit_list = self.store.read_trade_range(
                "limit_list_d",
                limit_start,
                load_end,
                columns=self.LIMIT_LIST_D_FEATURE_COLUMNS,
            )
            limit_list = self._normalize_keys(limit_list, "limit_list_d", allow_empty=True)
            raw_only = self.LIMIT_LIST_D_RAW_ONLY_COLUMNS.intersection(limit_list.columns)
            if raw_only:
                limit_list = limit_list.drop(columns=sorted(raw_only))
            if not limit_list.empty:
                limit_list = limit_list.drop_duplicates(self.KEY_COLUMNS, keep="last")
                frame = frame.merge(limit_list, on=self.KEY_COLUMNS, how="left")
            else:
                frame["limit"] = pd.NA
        else:
            frame["limit"] = pd.NA

        calendar_trade_dates = self._calendar_trade_dates() or trade_dates
        next_trade = {calendar_trade_dates[i]: calendar_trade_dates[i + 1] for i in range(len(calendar_trade_dates) - 1)}
        frame = frame[frame["trade_date"].isin(selected)].copy()
        frame["feature_date"] = frame["trade_date"]
        frame["source_trade_date"] = frame["trade_date"]
        frame["tradable_date"] = frame["feature_date"].map(next_trade)
        frame = frame[frame["tradable_date"].notna()].copy()
        frame["available_at"] = frame["feature_date"].map(self._available_at_for_feature_date)
        frame["result_available_time"] = frame["available_at"]
        if config.fundamental_events_dir is not None:
            frame = self._join_fundamental_events(frame, Path(config.fundamental_events_dir))

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
        derived_keep = [col for col in frame.columns if col.startswith(("fund_", "dividend_"))]
        return frame[[col for col in keep + derived_keep if col in frame.columns]].reset_index(drop=True)

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

    def _calendar_trade_dates(self) -> list[str]:
        calendar_dir = self.raw_dir / "trade_cal" / "exchange=SSE"
        if not calendar_dir.exists():
            return []
        frames = []
        for path in sorted(calendar_dir.glob("year=*.parquet")):
            try:
                frames.append(pd.read_parquet(path, columns=["cal_date", "is_open"]))
            except Exception:
                continue
        if not frames:
            return []
        calendar = pd.concat(frames, ignore_index=True)
        if calendar.empty or not {"cal_date", "is_open"}.issubset(calendar.columns):
            return []
        calendar = calendar[calendar["is_open"].astype(str) == "1"].copy()
        if calendar.empty:
            return []
        dates = [yyyymmdd(value) for value in calendar["cal_date"].dropna()]
        return sorted(set(dates))

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

    def _join_fundamental_events(self, frame: pd.DataFrame, events_dir: Path) -> pd.DataFrame:
        if frame.empty or not events_dir.exists():
            return frame
        max_available_at = str(frame["available_at"].max())
        events = read_fundamental_events(events_dir, max_available_at, datasets=("fina_indicator_vip", "dividend"))
        if events.empty:
            return frame
        out = frame.copy()
        joins: list[pd.DataFrame] = []
        for feature_date, cross_section in out.groupby("feature_date", sort=True):
            decision_time = pd.Timestamp(cross_section["available_at"].iloc[0])
            visible = events[pd.to_datetime(events["available_at"], errors="coerce") <= decision_time]
            if visible.empty:
                continue
            joined = pd.DataFrame({"_row_id": cross_section.index, "ts_code": cross_section["ts_code"].values})
            indicator = self._latest_by_symbol(visible[visible["dataset"] == "fina_indicator_vip"], prefer_end_date=True)
            if not indicator.empty:
                indicator = self._fundamental_indicator_columns(indicator)
                joined = joined.merge(indicator, on="ts_code", how="left")
            dividend = self._latest_by_symbol(visible[visible["dataset"] == "dividend"], prefer_end_date=False)
            if not dividend.empty:
                dividend = self._dividend_columns(dividend, str(feature_date))
                joined = joined.merge(dividend, on="ts_code", how="left")
            joins.append(joined.set_index("_row_id"))
        if not joins:
            return out
        features = pd.concat(joins, axis=0).sort_index()
        for column in features.columns:
            if column != "ts_code":
                out.loc[features.index, column] = features[column]
        return out

    @staticmethod
    def _latest_by_symbol(events: pd.DataFrame, *, prefer_end_date: bool) -> pd.DataFrame:
        if events.empty or "ts_code" not in events.columns:
            return pd.DataFrame()
        preferred = ["end_date", "available_at", "ann_date"] if prefer_end_date else ["available_at", "ex_date", "record_date", "pay_date"]
        sort_cols = [col for col in preferred if col in events.columns]
        return events.sort_values(sort_cols).drop_duplicates("ts_code", keep="last")

    @staticmethod
    def _fundamental_indicator_columns(events: pd.DataFrame) -> pd.DataFrame:
        columns = ["ts_code"]
        rename = {
            "end_date": "fund_latest_end_date",
            "available_at": "fund_latest_available_at",
            "roe": "fund_roe",
            "roe_dt": "fund_roe_dt",
            "roa": "fund_roa",
            "grossprofit_margin": "fund_grossprofit_margin",
            "netprofit_margin": "fund_netprofit_margin",
            "debt_to_assets": "fund_debt_to_assets",
            "assets_turn": "fund_assets_turn",
            "or_yoy": "fund_or_yoy",
            "netprofit_yoy": "fund_netprofit_yoy",
        }
        columns.extend(col for col in rename if col in events.columns)
        out = events[columns].copy()
        out = out.rename(columns={col: rename[col] for col in columns if col in rename})
        for column in out.columns:
            if column.startswith("fund_") and column not in {"fund_latest_end_date", "fund_latest_available_at"}:
                out[column] = pd.to_numeric(out[column], errors="coerce")
        return out

    @staticmethod
    def _dividend_columns(events: pd.DataFrame, feature_date: str) -> pd.DataFrame:
        columns = ["ts_code"]
        rename = {
            "available_at": "dividend_latest_available_at",
            "ex_date": "dividend_latest_ex_date",
            "record_date": "dividend_latest_record_date",
            "pay_date": "dividend_latest_pay_date",
            "cash_div": "dividend_cash_div",
            "cash_div_tax": "dividend_cash_div_tax",
            "base_share": "dividend_base_share",
        }
        columns.extend(col for col in rename if col in events.columns)
        out = events[columns].copy().rename(columns={col: rename[col] for col in columns if col in rename})
        for column in ("dividend_cash_div", "dividend_cash_div_tax", "dividend_base_share"):
            if column in out.columns:
                out[column] = pd.to_numeric(out[column], errors="coerce")
        if "dividend_latest_ex_date" in out.columns:
            ex_date = pd.to_datetime(out["dividend_latest_ex_date"], errors="coerce")
            feature_dt = pd.Timestamp(feature_date)
            out["dividend_days_to_ex"] = (ex_date - feature_dt).dt.days
        return out
