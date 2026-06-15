"""PIT snapshot construction (docs/environment_design.md chapter 2).

Builds the six domain files plus universe and manifest for one decision time:

    manifest.json, daily.parquet, intraday_1min.parquet, fundamentals.parquet,
    events.parquet, macro.parquet, text_index.parquet, text_library/, universe.parquet

Every row satisfies ``available_at <= decision_time``. Datasets whose raw rows
carry an ``available_at`` column (events/macro/text/minute) are filtered on it;
the daily core uses the dataset contracts. Numeric fields are normalized to the
unit contract (CNY, shares, decimals) and every conversion is recorded in the
manifest. Replay slots (valid/test) are built separately and are NOT
PIT-filtered: they are the replay regions read only by backtest_tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from hl_trader.environment.data import PITDataStore, default_tushare_contracts
from hl_trader.environment.data.contracts import CN_TZ
from hl_trader.environment.data.pit import yyyymmdd
from hl_trader.environment.features.auction import apply_open_auction_correction
from hl_trader.environment.features.fundamental_events import FUNDAMENTAL_EVENT_DATASETS, read_fundamental_events
from hl_trader.environment.runtime import new_id, utc_now_iso

SNAPSHOT_FILES = (
    "daily.parquet",
    "intraday_1min.parquet",
    "fundamentals.parquet",
    "events.parquet",
    "macro.parquet",
    "text_index.parquet",
    "universe.parquet",
)

# Percent -> decimal, 手 -> shares, 千元/万元 -> CNY (docs/environment_design.md 2.3).
DAILY_UNIT_CONVERSIONS: tuple[tuple[str, float, str], ...] = (
    ("vol", 100.0, "hands->shares"),
    ("amount", 1000.0, "thousand_cny->cny"),
    ("pct_chg", 0.01, "percent->decimal"),
    ("turnover_rate", 0.01, "percent->decimal"),
    ("turnover_rate_f", 0.01, "percent->decimal"),
    ("dv_ratio", 0.01, "percent->decimal"),
    ("total_share", 10_000.0, "ten_thousand_shares->shares"),
    ("float_share", 10_000.0, "ten_thousand_shares->shares"),
    ("free_share", 10_000.0, "ten_thousand_shares->shares"),
    ("total_mv", 10_000.0, "ten_thousand_cny->cny"),
    ("circ_mv", 10_000.0, "ten_thousand_cny->cny"),
)


@dataclass(frozen=True)
class SnapshotConfig:
    window_months: int = 21
    intraday_trade_days: int = 5
    events_datasets: tuple[str, ...] = (
        "margin",
        "margin_detail",
        "margin_secs",
        "moneyflow",
        "block_trade",
        "stk_holdernumber",
        "stk_holdertrade",
        "repurchase",
        "share_float_complete",
        "top_list",
        "top_inst",
    )
    macro_datasets: tuple[str, ...] = (
        "cn_gdp",
        "cn_cpi",
        "cn_ppi",
        "cn_pmi",
        "cn_m",
        "sf_month",
        "shibor",
        "shibor_lpr",
        "monetary_policy",
        "eco_cal",
        "index_global",
        "fx_daily",
    )
    text_datasets: tuple[str, ...] = ("anns_d", "major_news", "cctv_news", "npr", "research_report", "report_rc")
    fundamental_datasets: tuple[str, ...] = FUNDAMENTAL_EVENT_DATASETS
    include_intraday: bool = True
    include_industry: bool = True
    text_body_chars: int = 4000
    replay_include_events: bool = True
    replay_include_text: bool = True
    replay_include_minutes: bool = True


@dataclass
class SnapshotBuilder:
    raw_dir: Path
    fundamental_events_root: Path

    def __init__(self, raw_dir: str | Path, fundamental_events_root: str | Path) -> None:
        self.raw_dir = Path(raw_dir)
        self.fundamental_events_root = Path(fundamental_events_root)
        self.contracts = default_tushare_contracts()
        self.store = PITDataStore(self.raw_dir, self.contracts)

    # ---- decision-input snapshot ----

    def build_decision_snapshot(
        self, decision_time: datetime, output_dir: str | Path, config: SnapshotConfig | None = None
    ) -> dict[str, object]:
        config = config or SnapshotConfig()
        decision_time = decision_time if decision_time.tzinfo else decision_time.replace(tzinfo=CN_TZ)
        decision_time = decision_time.astimezone(CN_TZ)
        window_start = (pd.Timestamp(decision_time) - pd.DateOffset(months=config.window_months)).tz_localize(None)
        window_start = window_start.tz_localize(CN_TZ)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        domains: dict[str, dict[str, object]] = {}

        daily, daily_meta = self._build_daily(decision_time, window_start)
        _write(output_dir / "daily.parquet", daily)
        domains["daily"] = daily_meta

        if config.include_intraday:
            intraday, intraday_meta = self._build_intraday(decision_time, daily_meta["trade_dates"], config)
        else:
            intraday, intraday_meta = pd.DataFrame(), {"rows": 0, "datasets": [], "skipped": True}
        _write(output_dir / "intraday_1min.parquet", intraday)
        domains["intraday_1min"] = intraday_meta

        fundamentals = read_fundamental_events(
            self.fundamental_events_root, decision_time.isoformat(), datasets=config.fundamental_datasets
        )
        if not fundamentals.empty:
            parsed = to_cn_timestamps(fundamentals["available_at"])
            fundamentals = fundamentals[parsed >= window_start].reset_index(drop=True)
        _write(output_dir / "fundamentals.parquet", fundamentals)
        domains["fundamentals"] = {"rows": int(len(fundamentals)), "datasets": list(config.fundamental_datasets)}

        events, events_meta = self._build_available_at_domain(config.events_datasets, decision_time, window_start)
        _write(output_dir / "events.parquet", events)
        domains["events"] = events_meta

        macro, macro_meta = self._build_available_at_domain(config.macro_datasets, decision_time, window_start)
        _write(output_dir / "macro.parquet", macro)
        domains["macro"] = macro_meta

        text_index, text_meta = self._build_text(config, decision_time, window_start, output_dir)
        _write(output_dir / "text_index.parquet", text_index)
        domains["text"] = text_meta

        universe = self._build_universe(decision_time, config)
        _write(output_dir / "universe.parquet", universe)
        domains["universe"] = {"rows": int(len(universe))}

        manifest = {
            "snapshot_id": new_id("snap"),
            "kind": "decision_input",
            "created_at": utc_now_iso(),
            "decision_time": decision_time.isoformat(),
            "window_start": window_start.isoformat(),
            "window_months": config.window_months,
            "domains": domains,
            "snapshot_hash": "",
        }
        manifest["snapshot_hash"] = _snapshot_hash(output_dir)
        _write_manifest(output_dir, manifest)
        return manifest

    # ---- replay slot (valid/test region; not PIT-filtered) ----

    def build_replay_slot(
        self,
        start_date: str,
        end_date: str,
        output_dir: str | Path,
        *,
        label: str,
        config: SnapshotConfig | None = None,
    ) -> dict[str, object]:
        """Replay region data: daily bars plus the events/text/minutes published
        inside the period, for backtest replay and Agent validation review."""
        config = config or SnapshotConfig()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        start_key, end_key = yyyymmdd(start_date), yyyymmdd(end_date)
        period_start = pd.Timestamp(start_key, tz=CN_TZ)
        period_end = pd.Timestamp(end_key, tz=CN_TZ) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        domains: dict[str, dict[str, object]] = {}

        daily = self._daily_join(start_key, end_key)
        daily, conversions = normalize_daily_units(daily)
        _write(output_dir / "daily.parquet", daily)
        domains["daily"] = {"rows": int(len(daily)), "unit_conversions": conversions}

        if config.replay_include_events:
            events, events_meta = self._build_available_at_domain(
                config.events_datasets, period_end, period_start
            )
            _write(output_dir / "events.parquet", events)
            domains["events"] = events_meta
        if config.replay_include_text:
            text_index, text_meta = self._build_text(config, period_end, period_start, output_dir)
            _write(output_dir / "text_index.parquet", text_index)
            domains["text"] = text_meta
        if config.replay_include_minutes:
            minutes, minutes_meta = self._read_minutes_range(start_key, end_key)
            _write(output_dir / "intraday_1min.parquet", minutes)
            domains["intraday_1min"] = minutes_meta

        manifest = {
            "snapshot_id": new_id("replay"),
            "kind": "replay_slot",
            "label": label,
            "created_at": utc_now_iso(),
            "period_start": start_key,
            "period_end": end_key,
            "domains": domains,
            "snapshot_hash": "",
        }
        manifest["snapshot_hash"] = _snapshot_hash(output_dir)
        _write_manifest(output_dir, manifest)
        return manifest

    def _read_minutes_range(self, start_key: str, end_key: str) -> tuple[pd.DataFrame, dict[str, object]]:
        dataset_dir = self.raw_dir / "stk_mins_1min_by_date"
        if not dataset_dir.exists():
            raise FileNotFoundError(f"missing intraday by-date dataset: {dataset_dir}")
        frames = [
            pd.read_parquet(path)
            for path in sorted(dataset_dir.glob("trade_date=*.parquet"))
            if start_key <= path.stem.split("=", 1)[1] <= end_key
        ]
        minutes = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return minutes, {"rows": int(len(minutes)), "datasets": ["stk_mins_1min_by_date"], "files": len(frames)}

    # ---- domain builders ----

    def _build_daily(self, decision_time: datetime, window_start: pd.Timestamp) -> tuple[pd.DataFrame, dict[str, object]]:
        contract = self.contracts["daily"]
        visible_dates = [
            key
            for key in self.store.trade_dates("daily")
            if contract.available_at(datetime.strptime(key, "%Y%m%d").date()) <= decision_time
            and pd.Timestamp(key, tz=CN_TZ) >= window_start
        ]
        if not visible_dates:
            raise ValueError(f"no visible daily trade dates before {decision_time.isoformat()}")
        frame = self._daily_join(visible_dates[0], visible_dates[-1])
        frame, conversions = normalize_daily_units(frame)
        meta = {
            "rows": int(len(frame)),
            "datasets": ["daily", "daily_basic", "adj_factor", "stk_limit", "suspend_d"],
            "coverage_start": visible_dates[0],
            "coverage_end": visible_dates[-1],
            "trade_dates": visible_dates,
            "unit_conversions": conversions,
            "availability_rule": "daily close-time contracts; same-day data is not visible at pre-open decisions",
        }
        return frame, meta

    def _daily_join(self, start: str, end: str) -> pd.DataFrame:
        daily = self.store.read_trade_range("daily", start, end)
        if daily.empty:
            raise ValueError(f"daily raw data empty for {start}..{end}")
        basic = self.store.read_trade_range("daily_basic", start, end)
        limits = self.store.read_trade_range("stk_limit", start, end)
        adj = self.store.read_trade_range("adj_factor", start, end)
        suspend = self.store.read_trade_range("suspend_d", start, end, columns=["trade_date", "ts_code"])
        for name, frame in (("daily", daily), ("daily_basic", basic), ("stk_limit", limits)):
            if frame.duplicated(["trade_date", "ts_code"]).any():
                raise ValueError(f"{name} has duplicate (trade_date, ts_code) keys in {start}..{end}")
        out = daily.merge(basic, on=["trade_date", "ts_code"], how="left", suffixes=("", "_basic"))
        out = out.merge(limits, on=["trade_date", "ts_code"], how="left", suffixes=("", "_limit"))
        if not adj.empty:
            out = out.merge(adj[["trade_date", "ts_code", "adj_factor"]], on=["trade_date", "ts_code"], how="left")
        suspended = set(zip(suspend.get("trade_date", []), suspend.get("ts_code", [])))
        out["is_suspended"] = [(d, c) in suspended for d, c in zip(out["trade_date"], out["ts_code"])]
        out["trade_date"] = out["trade_date"].astype(str)
        out["ts_code"] = out["ts_code"].astype(str)
        return out

    def _build_intraday(
        self, decision_time: datetime, visible_daily_dates: list[str], config: SnapshotConfig
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        dataset_dir = self.raw_dir / "stk_mins_1min_by_date"
        if not dataset_dir.exists():
            raise FileNotFoundError(f"missing intraday by-date dataset: {dataset_dir}")
        recent = visible_daily_dates[-config.intraday_trade_days :]
        frames = []
        for key in recent:
            path = dataset_dir / f"trade_date={key}.parquet"
            if not path.exists():
                raise FileNotFoundError(f"missing intraday partition: {path}")
            frames.append(pd.read_parquet(path))
        minute = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not minute.empty:
            available = to_cn_timestamps(minute["available_at"])
            minute = minute[available <= decision_time].reset_index(drop=True)
            minute = apply_open_auction_correction(minute)
        meta = {
            "rows": int(len(minute)),
            "datasets": ["stk_mins_1min_by_date"],
            "trade_dates": recent,
            "availability_rule": "available_at=bar close time (trade_time)",
            "auction_correction": {
                "rule_id": "minute_0930_to_live_stk_auction_by_market_bucket",
                "factors": {"00*.SZ": 0.76, "30*.SZ": 0.58, "other": 1.0},
                "applies_to": "09:30 SZ bars as live stk_auction proxy features only",
            },
        }
        return minute, meta

    def _build_available_at_domain(
        self, datasets: tuple[str, ...], decision_time: datetime, window_start: pd.Timestamp
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        frames: list[pd.DataFrame] = []
        rules: dict[str, str] = {}
        for dataset in datasets:
            dataset_dir = self.raw_dir / dataset
            if not dataset_dir.exists():
                raise FileNotFoundError(f"missing configured dataset directory: {dataset_dir}")
            rows = self._read_dataset_window(dataset_dir, decision_time, window_start)
            rules[dataset] = "raw available_at column"
            if rows.empty:
                continue
            rows.insert(0, "dataset", dataset)
            frames.append(rows)
        merged = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        meta = {"rows": int(len(merged)), "datasets": list(datasets), "availability_rules": rules}
        return merged, meta

    def _read_dataset_window(
        self, dataset_dir: Path, decision_time: datetime, window_start: pd.Timestamp
    ) -> pd.DataFrame:
        start_day = window_start.strftime("%Y%m%d")
        end_day = decision_time.strftime("%Y%m%d")
        frames = []
        for path in sorted(dataset_dir.rglob("*.parquet")):
            if not _partition_overlaps(path.stem, start_day, end_day):
                continue
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            if "available_at" not in frame.columns:
                raise ValueError(f"{path} has no available_at column; cannot enforce the PIT wall")
            available = to_cn_timestamps(frame["available_at"])
            keep = frame[(available <= decision_time) & (available >= window_start)]
            if not keep.empty:
                frames.append(keep)
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def _build_text(
        self, config: SnapshotConfig, decision_time: datetime, window_start: pd.Timestamp, output_dir: Path
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        """Text index plus per-dataset body shards under text_library/.

        Bodies are stored as one parquet per dataset keyed by text_id (not one
        file per document) so multi-million-row text windows stay tractable.
        """
        library_dir = output_dir / "text_library"
        library_dir.mkdir(parents=True, exist_ok=True)
        index_frames: list[pd.DataFrame] = []
        for dataset in config.text_datasets:
            dataset_dir = self.raw_dir / dataset
            if not dataset_dir.exists():
                raise FileNotFoundError(f"missing configured text dataset: {dataset_dir}")
            rows = self._read_dataset_window(dataset_dir, decision_time, window_start)
            if rows.empty:
                continue
            title_column = next((c for c in ("title", "report_title", "name") if c in rows.columns), None)
            body_columns = [c for c in ("title", "report_title", "abstr", "content", "content_html", "url") if c in rows.columns]
            if title_column is None or not body_columns:
                raise ValueError(f"text dataset {dataset} has no usable title/body columns: {list(rows.columns)}")
            titles = rows[title_column].fillna("").astype(str)
            if "content" in rows.columns:
                titles = titles.where(titles.str.len() > 0, rows["content"].fillna("").astype(str))
            bodies = rows[body_columns[0]].fillna("").astype(str)
            for key in body_columns[1:]:
                bodies = bodies + "\n" + rows[key].fillna("").astype(str)
            bodies = bodies.str.slice(0, config.text_body_chars)
            available = rows["available_at"].astype(str)
            text_ids = [
                hashlib.sha1(f"{dataset}|{avail}|{title[:200]}|{position}".encode("utf-8")).hexdigest()
                for position, (avail, title) in enumerate(zip(available, titles))
            ]
            library_file = f"{dataset}.parquet"
            _write(library_dir / library_file, pd.DataFrame({"text_id": text_ids, "body": bodies.values}))
            index_frames.append(
                pd.DataFrame(
                    {
                        "text_id": text_ids,
                        "dataset": dataset,
                        "ts_codes": rows.get("ts_code", pd.Series("", index=rows.index)).fillna("").astype(str).values,
                        "title": titles.str.slice(0, 200).values,
                        "available_at": available.values,
                        "source_hash": [hashlib.sha1(body.encode("utf-8")).hexdigest() for body in bodies],
                        "library_file": library_file,
                    }
                )
            )
        index = pd.concat(index_frames, ignore_index=True) if index_frames else pd.DataFrame(
            columns=["text_id", "dataset", "ts_codes", "title", "available_at", "source_hash", "library_file"]
        )
        meta = {"rows": int(len(index)), "datasets": list(config.text_datasets), "library_dir": "text_library"}
        return index, meta

    def _build_universe(self, decision_time: datetime, config: SnapshotConfig) -> pd.DataFrame:
        """Stocks listed as of the decision day (delistings after it included).

        Building from the current L partition alone would drop names delisted
        later than the decision day and inject survivorship bias.
        """
        day = decision_time.strftime("%Y%m%d")
        frames = []
        for status in ("L", "D", "P"):
            path = self.raw_dir / "stock_basic" / f"list_status={status}.parquet"
            if path.exists():
                frames.append(pd.read_parquet(path))
        if not frames:
            raise FileNotFoundError(f"missing stock_basic partitions under {self.raw_dir / 'stock_basic'}")
        basic = pd.concat(frames, ignore_index=True)
        keep = [col for col in ("ts_code", "name", "exchange", "list_date", "delist_date", "market") if col in basic.columns]
        universe = basic[keep].copy()
        universe["ts_code"] = universe["ts_code"].astype(str)
        universe = universe.drop_duplicates("ts_code", keep="first")
        if "list_date" in universe.columns:
            universe = universe[universe["list_date"].fillna("").astype(str) <= day]
        if "delist_date" in universe.columns:
            delist = universe["delist_date"].fillna("").astype(str)
            universe = universe[(delist == "") | (delist == "None") | (delist > day)]
        name_asof = self._names_as_of(decision_time)
        if not name_asof.empty:
            universe = universe.merge(name_asof, on="ts_code", how="left")
            universe["name_asof"] = universe["name_asof"].fillna(universe.get("name"))
        if config.include_industry:
            industry = self._industry_membership(decision_time.strftime("%Y%m%d"))
            if not industry.empty:
                universe = universe.merge(industry, on="ts_code", how="left")
        return universe.reset_index(drop=True)

    def _names_as_of(self, decision_time: datetime) -> pd.DataFrame:
        path = self.raw_dir / "namechange" / "namechange.parquet"
        if not path.exists():
            return pd.DataFrame()
        names = pd.read_parquet(path)
        day = decision_time.strftime("%Y%m%d")
        if "ann_date" in names.columns:
            names = names[names["ann_date"].astype(str).str.strip().le(day) | names["ann_date"].isna()]
        names = names[names["start_date"].astype(str) <= day]
        names = names.sort_values("start_date").drop_duplicates("ts_code", keep="last")
        return names[["ts_code", "name"]].rename(columns={"name": "name_asof"})

    def _industry_membership(self, decision_day: str) -> pd.DataFrame:
        """As-of SW level-1 membership: in_date <= decision day < out_date."""
        dataset_dir = self.raw_dir / "index_member_all"
        if not dataset_dir.exists():
            return pd.DataFrame()
        frames = []
        for path in sorted(dataset_dir.glob("l1_code=*.parquet")):
            frame = pd.read_parquet(path)
            cols = [col for col in ("ts_code", "l1_code", "l1_name", "in_date", "out_date") if col in frame.columns]
            if "ts_code" in cols:
                frames.append(frame[cols])
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True)
        if "in_date" in merged.columns:
            merged = merged[merged["in_date"].fillna("").astype(str) <= decision_day]
        if "out_date" in merged.columns:
            out_date = merged["out_date"].fillna("").astype(str)
            merged = merged[(out_date == "") | (out_date == "None") | (out_date > decision_day)]
        merged = merged.sort_values("in_date" if "in_date" in merged.columns else "ts_code")
        return merged.drop_duplicates("ts_code", keep="last")[
            [col for col in ("ts_code", "l1_code", "l1_name") if col in merged.columns]
        ]


def to_cn_timestamps(series: pd.Series) -> pd.Series:
    """Parse available_at values to Asia/Shanghai timestamps.

    Raw datasets mix tz-aware ISO strings (e.g. margin) and tz-naive Beijing
    wall-clock strings (e.g. anns_d rec_time); naive values must be localized
    to CN, never treated as UTC.
    """
    try:
        parsed = pd.to_datetime(series, errors="coerce")
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None and getattr(parsed.dtype, "tz", None) is not None:
        return parsed.dt.tz_convert(CN_TZ)
    if parsed is not None and parsed.dtype != object:
        return parsed.dt.tz_localize(CN_TZ)
    # Mixed aware/naive values: normalize element-wise.
    fallback = pd.to_datetime(series, errors="coerce", utc=False)
    if fallback.dtype != object:
        if getattr(fallback.dtype, "tz", None) is not None:
            return fallback.dt.tz_convert(CN_TZ)
        return fallback.dt.tz_localize(CN_TZ)
    return fallback.map(
        lambda value: (value.tz_localize(CN_TZ) if value.tzinfo is None else value.tz_convert(CN_TZ))
        if pd.notna(value)
        else pd.NaT
    )


def normalize_daily_units(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Apply the unit contract to a joined daily frame and record conversions."""
    frame = frame.copy()
    conversions: list[dict[str, object]] = []
    for column, factor, rule in DAILY_UNIT_CONVERSIONS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce") * factor
            conversions.append({"column": column, "factor": factor, "rule": rule})
    return frame, conversions


def finalize_snapshot_dir(snapshot_dir: str | Path, **fields: object) -> dict[str, object]:
    """Stamp an externally assembled snapshot directory with id/hash/manifest."""
    snapshot_dir = Path(snapshot_dir)
    manifest: dict[str, object] = {"snapshot_id": new_id("snap"), "created_at": utc_now_iso(), **fields}
    manifest["snapshot_hash"] = _snapshot_hash(snapshot_dir)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return manifest


def load_snapshot_manifest(snapshot_dir: str | Path) -> dict[str, object]:
    path = Path(snapshot_dir) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"snapshot manifest missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_snapshot_hash(snapshot_dir: str | Path) -> None:
    manifest = load_snapshot_manifest(snapshot_dir)
    actual = _snapshot_hash(Path(snapshot_dir))
    if manifest.get("snapshot_hash") != actual:
        raise ValueError(f"snapshot hash mismatch in {snapshot_dir}: manifest={manifest.get('snapshot_hash')} actual={actual}")


def _partition_overlaps(stem: str, start_day: str, end_day: str) -> bool:
    """Cheap pre-filter on partition file names; unknown layouts are read fully."""
    if "=" not in stem:
        return True
    key, value = stem.split("=", 1)
    if key in {"trade_date", "date", "ann_date"} and len(value) == 8 and value.isdigit():
        return start_day <= value <= end_day
    if key in {"month", "ann_month"} and len(value) == 6 and value.isdigit():
        return start_day[:6] <= value <= end_day[:6]
    if key == "year" and len(value) == 4 and value.isdigit():
        return start_day[:4] <= value <= end_day[:4]
    return True


def _snapshot_hash(snapshot_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            digest.update(str(path.relative_to(snapshot_dir)).encode("utf-8"))
            digest.update(b"\x00")
            digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _write(path: Path, frame: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


def _write_manifest(output_dir: Path, manifest: dict[str, object]) -> None:
    trimmed = json.loads(json.dumps(manifest, ensure_ascii=False, default=str))
    for domain in trimmed.get("domains", {}).values():
        domain.pop("trade_dates", None)  # keep the manifest small; coverage fields remain
    (output_dir / "manifest.json").write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
