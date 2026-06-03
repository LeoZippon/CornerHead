from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
import hashlib
import json

import pandas as pd

from hl_trader.environment.data.contracts import CN_TZ
from hl_trader.environment.data.pit import yyyymmdd


FUNDAMENTAL_EVENT_DATASETS = (
    "income_vip",
    "balancesheet_vip",
    "cashflow_vip",
    "fina_indicator_vip",
    "forecast_vip",
    "express_vip",
    "dividend",
    "fina_audit",
    "fina_mainbz_vip",
    "disclosure_date",
)

BUSINESS_KEYS = {
    "income_vip": ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    "balancesheet_vip": ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    "cashflow_vip": ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    "fina_indicator_vip": ("ts_code", "ann_date", "end_date"),
    "forecast_vip": ("ts_code", "ann_date", "end_date", "type", "first_ann_date", "update_flag"),
    "express_vip": ("ts_code", "ann_date", "end_date"),
    "dividend": ("ts_code", "end_date", "ann_date", "div_proc", "record_date", "ex_date", "pay_date"),
    "fina_audit": ("ts_code", "ann_date", "end_date"),
    "fina_mainbz_vip": ("ts_code", "end_date", "bz_item", "bz_code", "curr_type"),
    "disclosure_date": ("ts_code", "end_date", "ann_date", "pre_date", "actual_date"),
}

RAW_PATTERNS = {
    "income_vip": "period=*.parquet",
    "balancesheet_vip": "period=*.parquet",
    "cashflow_vip": "period=*.parquet",
    "fina_indicator_vip": "period=*.parquet",
    "forecast_vip": "ann_month=*.parquet",
    "express_vip": "ann_month=*.parquet",
    "dividend": "ts_code=*.parquet",
    "fina_audit": "ts_code=*.parquet",
    "fina_mainbz_vip": "ts_code=*.parquet",
    "disclosure_date": "period=*.parquet",
}


@dataclass(frozen=True)
class FundamentalEventsConfig:
    start_date: str
    end_date: str
    datasets: tuple[str, ...] = field(default_factory=lambda: FUNDAMENTAL_EVENT_DATASETS)


class FundamentalEventsBuilder:
    """Build PIT-ready financial/event records from raw TuShare fundamental files."""

    def __init__(self, raw_dir: str | Path) -> None:
        self.raw_dir = Path(raw_dir)

    def build(self, config: FundamentalEventsConfig) -> pd.DataFrame:
        datasets = tuple(config.datasets or FUNDAMENTAL_EVENT_DATASETS)
        statement_availability = self._statement_availability()
        frames = [self._read_dataset(dataset, statement_availability) for dataset in datasets]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame(columns=self._event_columns())
        events = pd.concat(frames, ignore_index=True)
        events = events[events["available_at"].astype(str).str.strip().ne("")].copy()
        if events.empty:
            return pd.DataFrame(columns=self._event_columns())
        parsed = pd.to_datetime(events["available_at"], errors="coerce")
        start = pd.Timestamp(yyyymmdd(config.start_date)).tz_localize(CN_TZ)
        end = pd.Timestamp(yyyymmdd(config.end_date)).tz_localize(CN_TZ) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        events = events[(parsed >= start) & (parsed <= end)].copy()
        if events.empty:
            return pd.DataFrame(columns=self._event_columns())
        events["available_month"] = pd.to_datetime(events["available_at"], errors="coerce").dt.strftime("%Y%m")
        return events.sort_values(["available_at", "dataset", "ts_code", "business_key"]).reset_index(drop=True)

    def write_partitioned(
        self,
        events: pd.DataFrame,
        output_root: str | Path,
        replace_months: set[str] | None = None,
        replace_datasets: tuple[str, ...] | None = None,
    ) -> list[Path]:
        output_root = Path(output_root)
        written: list[Path] = []
        replace_months = replace_months or set()
        if replace_datasets is None:
            replace_datasets = tuple(events["dataset"].dropna().astype(str).unique()) if not events.empty else ()
        for dataset in replace_datasets:
            for month in replace_months:
                stale_path = output_root / dataset / f"available_month={month}.parquet"
                if stale_path.exists():
                    stale_path.unlink()
        if events.empty:
            return written
        for (dataset, month), group in events.groupby(["dataset", "available_month"], sort=True):
            path = output_root / dataset / f"available_month={month}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and month not in replace_months:
                existing = pd.read_parquet(path)
                group = pd.concat([existing, group], ignore_index=True)
            dedupe_cols = [col for col in ["dataset", "business_key", "available_at"] if col in group.columns]
            if dedupe_cols:
                group = group.drop_duplicates(dedupe_cols, keep="last")
            tmp = path.with_suffix(path.suffix + ".tmp")
            group.to_parquet(tmp, index=False)
            tmp.replace(path)
            written.append(path)
        return written

    def _read_dataset(self, dataset: str, statement_availability: dict[tuple[str, str], str]) -> pd.DataFrame:
        dataset_dir = self.raw_dir / dataset
        if not dataset_dir.exists():
            return pd.DataFrame(columns=self._event_columns())
        frames: list[pd.DataFrame] = []
        for path in sorted(dataset_dir.glob(RAW_PATTERNS[dataset])):
            df = pd.read_parquet(path)
            if df.empty:
                continue
            df = df.copy()
            df["dataset"] = dataset
            df["source_path"] = str(path)
            df["source_hash"] = _source_hash(path)
            df["source_row_id"] = range(len(df))
            df["available_at"], df["available_at_rule"] = zip(*[
                _available_at_for_row(dataset, row, statement_availability) for row in df.to_dict("records")
            ])
            df["business_key"] = [_business_key(dataset, row) for row in df.to_dict("records")]
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=self._event_columns())
        return pd.concat(frames, ignore_index=True)

    def _statement_availability(self) -> dict[tuple[str, str], str]:
        availability: dict[tuple[str, str], str] = {}
        for dataset in ("income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator_vip"):
            dataset_dir = self.raw_dir / dataset
            if not dataset_dir.exists():
                continue
            for path in sorted(dataset_dir.glob("period=*.parquet")):
                df = pd.read_parquet(path)
                for row in df.to_dict("records"):
                    key = (str(row.get("ts_code", "")), _clean_date(row.get("end_date", "")))
                    if not key[0] or not key[1]:
                        continue
                    value, _rule = _available_at_for_row(dataset, row, {})
                    if value and (key not in availability or value > availability[key]):
                        availability[key] = value
        return availability

    @staticmethod
    def _event_columns() -> list[str]:
        return ["dataset", "ts_code", "available_at", "available_at_rule", "available_month", "business_key", "source_path", "source_hash", "source_row_id"]


def audit_fundamental_events(events_root: str | Path, config: FundamentalEventsConfig, output: str | Path | None = None, require_partitions: bool = False) -> dict[str, object]:
    root = Path(events_root)
    datasets = tuple(config.datasets or FUNDAMENTAL_EVENT_DATASETS)
    start_ts = pd.Timestamp(yyyymmdd(config.start_date)).tz_localize(CN_TZ)
    end_ts = pd.Timestamp(yyyymmdd(config.end_date)).tz_localize(CN_TZ) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    expected_months = set(_month_keys_between(config.start_date, config.end_date))
    checks: list[dict[str, object]] = []
    total_rows = 0
    errors = 0
    warnings = 0
    for dataset in datasets:
        files = sorted(
            path for path in (root / dataset).glob("available_month=*.parquet")
            if path.stem.split("=", 1)[1] in expected_months
        )
        file_months = {path.stem.split("=", 1)[1] for path in files}
        missing_months = sorted(expected_months - file_months)
        if not files:
            checks.append({"severity": "warning", "check": f"{dataset}_partitions", "message": "no PIT event partitions", "details": {"missing_months": missing_months}})
            warnings += 1
            continue
        if missing_months:
            checks.append({"severity": "warning", "check": f"{dataset}_missing_months", "message": "missing PIT event months in requested audit window", "details": {"missing_months": missing_months[:24], "missing_month_count": len(missing_months)}})
            warnings += 1
        rows = 0
        unparseable = 0
        wrong_partition = 0
        wrong_dataset = 0
        disallowed_rules = 0
        outside_window = 0
        blank_source_hash = 0
        sidecar_hash_mismatch = 0
        blank_source_path = 0
        wrong_source_path = 0
        missing_source_path = 0
        bad_source_row_id = 0
        duplicate_keys = 0
        for path in files:
            expected_dataset = path.parent.name
            df = pd.read_parquet(path)
            rows += len(df)
            missing = {"dataset", "ts_code", "available_at", "available_at_rule", "available_month", "business_key", "source_path", "source_hash", "source_row_id"} - set(df.columns)
            if missing:
                checks.append({"severity": "error", "check": f"{dataset}_schema", "message": f"missing columns in {path}", "details": {"missing": sorted(missing)}})
                errors += 1
                continue
            parsed = pd.to_datetime(df["available_at"], errors="coerce", utc=True).dt.tz_convert(CN_TZ)
            unparseable += int(parsed.isna().sum())
            outside_window += int(((parsed < start_ts) | (parsed > end_ts)).sum())
            expected_month = path.stem.split("=", 1)[1]
            wrong_partition += int((df["available_month"].astype(str) != expected_month).sum())
            wrong_dataset += int((df["dataset"].astype(str) != expected_dataset).sum())
            disallowed_rules += int((~df["available_at_rule"].astype(str).map(_is_allowed_available_at_rule)).sum())
            blank_source_hash += int(df["source_hash"].astype(str).str.strip().eq("").sum())
            source_paths = df["source_path"].astype(str)
            blank_source_path += int(source_paths.str.strip().eq("").sum())
            wrong_source_path += int((~source_paths.map(lambda value: _source_path_matches_dataset(value, expected_dataset))).sum())
            missing_source_path += int((~source_paths.map(lambda value: Path(value).exists())).sum())
            bad_source_row_id += int(pd.to_numeric(df["source_row_id"], errors="coerce").isna().sum())
            sidecar_hash_mismatch += int(sum(_source_hash_mismatch(row["source_path"], row["source_hash"]) for row in df.to_dict("records")))
            duplicate_keys += int(df.duplicated(["dataset", "business_key", "available_at"], keep=False).sum())
        total_rows += rows
        severity = "error" if unparseable or wrong_partition or wrong_dataset or disallowed_rules or outside_window or blank_source_path or wrong_source_path or missing_source_path or bad_source_row_id else "warning" if duplicate_keys or blank_source_hash or sidecar_hash_mismatch else "info"
        if severity == "error":
            errors += 1
        elif severity == "warning":
            warnings += 1
        checks.append({
            "severity": severity,
            "check": f"{dataset}_pit_events",
            "message": f"{dataset} PIT event partition checks",
            "details": {
                "files": len(files),
                "rows": rows,
                "unparseable_available_at_rows": unparseable,
                "outside_audit_window_rows": outside_window,
                "wrong_available_month_rows": wrong_partition,
                "wrong_dataset_rows": wrong_dataset,
                "disallowed_available_at_rule_rows": disallowed_rules,
                "blank_source_hash_rows": blank_source_hash,
                "sidecar_hash_mismatch_rows": sidecar_hash_mismatch,
                "blank_source_path_rows": blank_source_path,
                "wrong_source_path_rows": wrong_source_path,
                "missing_source_path_rows": missing_source_path,
                "bad_source_row_id_rows": bad_source_row_id,
                "duplicate_dataset_business_key_available_at_rows": duplicate_keys,
            },
        })
    if require_partitions and total_rows == 0:
        checks.append({"severity": "error", "check": "fundamental_events_partitions", "message": "no PIT event rows found for required audit window", "details": {"start_date": config.start_date, "end_date": config.end_date}})
        errors += 1
    status = "error" if errors else "warning" if warnings else "ok"
    report = {"status": status, "errors": errors, "warnings": warnings, "rows": total_rows, "checks": checks}
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report


def read_fundamental_events(events_root: str | Path, max_available_at: str, datasets: tuple[str, ...] = FUNDAMENTAL_EVENT_DATASETS) -> pd.DataFrame:
    root = Path(events_root)
    max_ts = pd.Timestamp(max_available_at)
    frames: list[pd.DataFrame] = []
    for dataset in datasets:
        dataset_dir = root / dataset
        if not dataset_dir.exists():
            continue
        for path in sorted(dataset_dir.glob("available_month=*.parquet")):
            month = path.stem.split("=", 1)[1]
            if month > max_ts.strftime("%Y%m"):
                continue
            df = pd.read_parquet(path)
            if not df.empty:
                frames.append(df)
    if not frames:
        return pd.DataFrame()
    events = pd.concat(frames, ignore_index=True)
    parsed = pd.to_datetime(events["available_at"], errors="coerce")
    return events[parsed <= max_ts].copy()


def _available_at_for_row(dataset: str, row: dict[str, object], statement_availability: dict[tuple[str, str], str]) -> tuple[str, str]:
    if dataset in {"income_vip", "balancesheet_vip", "cashflow_vip"}:
        return _first_available(row, ("f_ann_date", "ann_date"), "source:f_ann_date_or_ann_date")
    if dataset == "fina_indicator_vip":
        return _first_available(row, ("ann_date",), "source:ann_date")
    if dataset in {"forecast_vip", "express_vip"}:
        return _first_available(row, ("first_ann_date", "ann_date"), "source:first_ann_date_or_ann_date")
    if dataset == "dividend":
        value = _first_available(row, ("imp_ann_date", "ann_date"), "source:imp_ann_date_or_ann_date")
        if value[0]:
            return value
        return "", "missing_announcement_date_not_pit_visible"
    if dataset in {"fina_audit", "fina_mainbz_vip"}:
        value = _first_available(row, ("ann_date",), "source:ann_date")
        if value[0]:
            return value
        key = (str(row.get("ts_code", "")), _clean_date(row.get("end_date", "")))
        if key in statement_availability:
            return statement_availability[key], "fallback_joined_statement_available_at"
        return "", "missing_source_date"
    if dataset == "disclosure_date":
        return _first_available(row, ("ann_date", "actual_date", "pre_date"), "source:ann_or_conservative_disclosure_date")
    return "", "unsupported_dataset"


def _first_available(row: dict[str, object], columns: tuple[str, ...], rule: str) -> tuple[str, str]:
    for column in columns:
        value = _clean_date(row.get(column, ""))
        if value:
            return _date_at(value, time(18, 0)), rule if column == columns[0] else f"{rule}:{column}"
    return "", "missing_source_date"


def _is_allowed_available_at_rule(rule: str) -> bool:
    text = str(rule)
    allowed_rules = {
        "source:f_ann_date_or_ann_date",
        "source:f_ann_date_or_ann_date:ann_date",
        "source:ann_date",
        "source:first_ann_date_or_ann_date",
        "source:first_ann_date_or_ann_date:ann_date",
        "source:imp_ann_date_or_ann_date",
        "source:imp_ann_date_or_ann_date:ann_date",
        "fallback_joined_statement_available_at",
        "source:ann_or_conservative_disclosure_date",
        "source:ann_or_conservative_disclosure_date:actual_date",
        "source:ann_or_conservative_disclosure_date:pre_date",
    }
    return text in allowed_rules


def _month_keys_between(start_date: str, end_date: str) -> list[str]:
    start = pd.Timestamp(yyyymmdd(start_date))
    end = pd.Timestamp(yyyymmdd(end_date))
    current = pd.Timestamp(year=start.year, month=start.month, day=1)
    months: list[str] = []
    while current <= end:
        months.append(current.strftime("%Y%m"))
        current = current + pd.DateOffset(months=1)
    return months


def complete_months_for_date_window(start_date: str, end_date: str) -> set[str]:
    start = pd.Timestamp(yyyymmdd(start_date))
    end = pd.Timestamp(yyyymmdd(end_date))
    months: set[str] = set()
    current = pd.Timestamp(year=start.year, month=start.month, day=1)
    while current <= end:
        last = current + pd.offsets.MonthEnd(0)
        if start <= current and end >= last:
            months.add(current.strftime("%Y%m"))
        current = current + pd.DateOffset(months=1)
    return months


def _date_at(value: str, when: time) -> str:
    dt = datetime.strptime(value, "%Y%m%d").replace(hour=when.hour, minute=when.minute, second=when.second, tzinfo=CN_TZ)
    return dt.isoformat()


def _clean_date(value: object) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    try:
        return yyyymmdd(text)
    except ValueError:
        return ""


def _business_key(dataset: str, row: dict[str, object]) -> str:
    keys = BUSINESS_KEYS.get(dataset, ("ts_code",))
    payload = {"dataset": dataset, "values": {key: str(row.get(key, "")) for key in keys}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _source_hash(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    if not sidecar.exists():
        return ""
    try:
        return str(json.loads(sidecar.read_text(encoding="utf-8")).get("source_hash", ""))
    except json.JSONDecodeError:
        return ""


def _source_path_matches_dataset(source_path: str, dataset: str) -> bool:
    parts = Path(str(source_path)).parts
    return dataset in parts


def _source_hash_mismatch(source_path: object, source_hash: object) -> bool:
    path = Path(str(source_path))
    if not path.exists():
        return False
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    if not sidecar.exists():
        return False
    try:
        expected = str(json.loads(sidecar.read_text(encoding="utf-8")).get("source_hash", ""))
    except json.JSONDecodeError:
        return False
    actual = str(source_hash).strip()
    return bool(expected and actual and expected != actual)
