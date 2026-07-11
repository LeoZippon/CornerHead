"""PIT snapshot construction (docs/environment_design.md §1).

Builds the six domain files plus universe and manifest for one decision time:

    manifest.json, daily.parquet, intraday_1min.parquet, fundamentals.parquet,
    events.parquet, macro.parquet, text_index.parquet, text_library/, universe.parquet

Every row satisfies ``available_at <= decision_time``. Datasets whose raw rows
carry an ``available_at`` column (events/macro/text/minute) are filtered on it;
the daily core uses the dataset contracts. The unit contract (CNY, shares,
decimals) covers the DAILY domain only — every conversion is recorded in the
manifest; events/macro/fundamentals/text keep TuShare per-source units and
their domain meta carries ``units="source"`` (env docs §1.4). Replay slots
(valid/test) are built separately and are NOT PIT-filtered: they are the
replay regions read only by backtest_tool.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from autotrade.environment.data import PITDataStore, default_tushare_contracts
from autotrade.environment.data.contracts import CN_TZ
from autotrade.environment.data.pit import yyyymmdd
from autotrade.environment.features.auction import apply_open_auction_correction
from autotrade.environment.features.fundamental_events import FUNDAMENTAL_EVENT_DATASETS, read_fundamental_events
from autotrade.environment.features.units import normalize_daily_units
from autotrade.environment.runtime import new_id, utc_now_iso

@dataclass(frozen=True)
class SnapshotConfig:
    window_months: int = 21
    daily_window_months: int | None = None
    fundamentals_window_months: int | None = None
    events_window_months: int | None = None
    macro_window_months: int | None = None
    text_window_months: int | None = None
    # One trading month of decision-input minute bars; valid/test replay minute
    # windows are sized by the fold periods, not this field.
    intraday_trade_days: int = 21
    events_datasets: tuple[str, ...] = (
        "margin",
        "margin_detail",
        "margin_secs",
        "moneyflow",
        "moneyflow_dc",
        "moneyflow_ths",
        "moneyflow_ind_dc",
        "moneyflow_ind_ths",
        "moneyflow_cnt_ths",
        "cyq_perf",
        "bak_daily",
        "stk_premarket",
        "slb_len",
        "slb_len_mm",
        "block_trade",
        "stk_holdernumber",
        "top10_holders",
        "top10_floatholders",
        "pledge_detail",
        "stk_surv",
        "new_share",
        "stk_holdertrade",
        "repurchase",
        "share_float_complete",
        "top_list",
        "top_inst",
        # Board-trading / sentiment cluster (row-level available_at; day-end or
        # next-morning labels — descriptive sentiment signals, never a truth
        # source for fills/tradability/risk):
        "kpl_list",
        "kpl_concept_cons",
        "dc_index",
        "dc_member",
        "limit_step",
        "limit_cpt_list",
        "limit_list_ths",
        "ths_hot",
        "dc_hot",
        "hm_detail",
        "hm_list",
    )
    macro_datasets: tuple[str, ...] = (
        "cn_gdp",
        "cn_cpi",
        "cn_ppi",
        "cn_pmi",
        "cn_m",
        "sf_month",
        "shibor",
        "shibor_quote",
        "shibor_lpr",
        "monetary_policy",
        "eco_cal",
        "index_global",
        # Core A-share benchmark indexes (000001/000016/000300/000905/000852/
        # 399006/000688): market timing, beta management, relative strength.
        "index_daily",
        "index_dailybasic",
        "sw_daily",
        "ci_daily",
        "daily_info",
        "sz_daily_info",
        "moneyflow_mkt_dc",
        "broker_recommend",
        "ths_daily",
        "fx_daily",
        # Regime/background additions: repo liquidity, US nominal + real yield
        # curves (risk appetite). cn_schedule deliberately NOT exposed: the
        # source keeps no history (only current months), so it contributes
        # nothing to historical replay.
        "repo_daily",
        "us_tycr",
        "us_trycr",
    )
    text_datasets: tuple[str, ...] = (
        "anns_d", "major_news", "cctv_news", "npr", "research_report", "report_rc",
        "irm_qa_sh", "irm_qa_sz", "news",
    )
    # Newswire knobs. Defaults are deliberately generous (testing phase,
    # maximize Agent-visible data): every src= partition on disk, the full
    # text window. Cross-source content dedup always applies — measured 43%
    # of full-window rows are duplicates (4.56M -> 2.60M, ~0.4GB library).
    # Tighten via an explicit source tuple and/or a months clamp if needed.
    news_sources: tuple[str, ...] = ()  # empty = all sources present on disk
    news_window_months: int | None = None  # None = follow the text window
    fundamental_datasets: tuple[str, ...] = FUNDAMENTAL_EVENT_DATASETS
    include_intraday: bool = True
    include_industry: bool = True
    text_body_chars: int = 4000
    replay_include_events: bool = True
    replay_include_text: bool = True
    replay_include_minutes: bool = True
    replay_include_macro: bool = True
    replay_include_fundamentals: bool = True
    # ---- universe screening (experiment-level research universe) ----
    # Applied to every per-stock domain (universe/daily/minutes/auction/events/
    # fundamentals) at snapshot AND replay-slot build, using only decision-time
    # knowledge (as-of names, list_date, latest daily_basic <= anchor). The set
    # is frozen at the decision anchor: codes turning ST / delisting inside the
    # replay period keep their data (they were eligible when chosen). Empty /
    # default values disable screening entirely (zero overhead).
    screen_exclude_st: bool = False
    screen_exclude_new_listed_days: int = 0
    screen_min_circ_mv_yi: float | None = None  # 流通市值下限（亿元）
    screen_max_circ_mv_yi: float | None = None  # 流通市值上限（亿元）
    screen_min_price: float | None = None
    screen_max_price: float | None = None
    screen_boards: tuple[str, ...] = ()  # subset of main/gem/star/bj; empty = all

    def screening_active(self) -> bool:
        return bool(
            self.screen_exclude_st
            or self.screen_exclude_new_listed_days > 0
            or self.screen_min_circ_mv_yi is not None
            or self.screen_max_circ_mv_yi is not None
            or self.screen_min_price is not None
            or self.screen_max_price is not None
            or self.screen_boards
        )

    def __post_init__(self) -> None:
        for name, value in self.to_record()["decision_windows"].items():
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.intraday_trade_days <= 0:
            raise ValueError("intraday_trade_days must be positive")
        if self.screen_exclude_new_listed_days < 0:
            raise ValueError("screen_exclude_new_listed_days must be >= 0")
        unknown_boards = set(self.screen_boards) - {"main", "gem", "star", "bj"}
        if unknown_boards:
            raise ValueError(f"unknown screen_boards: {sorted(unknown_boards)}")
        for low, high, label in (
            (self.screen_min_circ_mv_yi, self.screen_max_circ_mv_yi, "screen_circ_mv_yi"),
            (self.screen_min_price, self.screen_max_price, "screen_price"),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError(f"{label}: min must be <= max")

    def months_for(self, domain: str) -> int:
        overrides = {
            "daily": self.daily_window_months,
            "fundamentals": self.fundamentals_window_months,
            "events": self.events_window_months,
            "macro": self.macro_window_months,
            "text": self.text_window_months,
        }
        if domain not in overrides:
            raise ValueError(f"unknown snapshot window domain: {domain}")
        return int(overrides[domain] if overrides[domain] is not None else self.window_months)

    def window_start_for(self, decision_time: datetime, domain: str) -> pd.Timestamp:
        return _window_start(decision_time, self.months_for(domain))

    def to_record(self) -> dict[str, object]:
        return {
            "decision_windows": {
                "daily_months": self.months_for("daily"),
                "fundamentals_months": self.months_for("fundamentals"),
                "events_months": self.months_for("events"),
                "macro_months": self.months_for("macro"),
                "text_months": self.months_for("text"),
                "intraday_trade_days": self.intraday_trade_days,
            },
            "datasets": {
                "events": list(self.events_datasets),
                "macro": list(self.macro_datasets),
                "text": list(self.text_datasets),
                "fundamentals": list(self.fundamental_datasets),
            },
            "include_intraday": self.include_intraday,
            "include_industry": self.include_industry,
            "text_body_chars": self.text_body_chars,
            "universe_screen": {
                "exclude_st": self.screen_exclude_st,
                "exclude_new_listed_days": self.screen_exclude_new_listed_days,
                "min_circ_mv_yi": self.screen_min_circ_mv_yi,
                "max_circ_mv_yi": self.screen_max_circ_mv_yi,
                "min_price": self.screen_min_price,
                "max_price": self.screen_max_price,
                "boards": list(self.screen_boards),
            },
            "replay": {
                "include_events": self.replay_include_events,
                "include_text": self.replay_include_text,
                "include_minutes": self.replay_include_minutes,
                "include_macro": self.replay_include_macro,
                "include_fundamentals": self.replay_include_fundamentals,
            },
        }


@dataclass
class SnapshotBuilder:
    raw_dir: Path
    fundamental_events_root: Path
    fundamental_events_status: Path | None

    def __init__(
        self,
        raw_dir: str | Path,
        fundamental_events_root: str | Path,
        fundamental_events_status: str | Path | None = None,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.fundamental_events_root = Path(fundamental_events_root)
        self.fundamental_events_status = Path(fundamental_events_status) if fundamental_events_status is not None else None
        # The per-domain audit status files live next to the fundamental one
        # (results/data_quality/); no status path disables the domain gates too.
        self.data_quality_dir = self.fundamental_events_status.parent if self.fundamental_events_status is not None else None
        self.contracts = default_tushare_contracts()
        self.store = PITDataStore(self.raw_dir, self.contracts)

    @contextmanager
    def _raw_lake_guard(self):
        """Shared flock over the cron updater's exclusive lock plus a
        generation double-read: a snapshot build never overlaps a raw-lake
        mutation run, and fails fast if the lake changed under it anyway
        (a writer bypassing the lock). Lakes without the cron lock file
        (manual/test raw dirs) have no updater to exclude."""
        lock_path = self.raw_dir.parent.parent / ".runtime" / "tushare" / "locks" / "tushare_update.lock"
        fd = None
        if lock_path.exists():
            fd = os.open(lock_path, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            generation = read_raw_generation(self.raw_dir)
            yield generation
            if read_raw_generation(self.raw_dir) != generation:
                raise RuntimeError(f"raw lake generation changed during snapshot build under {self.raw_dir}")
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    # ---- decision-input snapshot ----

    def build_decision_snapshot(
        self, decision_time: datetime, output_dir: str | Path, config: SnapshotConfig | None = None
    ) -> dict[str, object]:
        with self._raw_lake_guard() as raw_generation:
            manifest = self._build_decision_snapshot_impl(decision_time, output_dir, config, raw_generation)
        return manifest

    def _build_decision_snapshot_impl(
        self,
        decision_time: datetime,
        output_dir: str | Path,
        config: SnapshotConfig | None,
        raw_generation: dict[str, object] | None,
    ) -> dict[str, object]:
        config = config or SnapshotConfig()
        decision_time = decision_time if decision_time.tzinfo else decision_time.replace(tzinfo=CN_TZ)
        decision_time = decision_time.astimezone(CN_TZ)
        daily_window_start = config.window_start_for(decision_time, "daily")
        fundamentals_window_start = config.window_start_for(decision_time, "fundamentals")
        events_window_start = config.window_start_for(decision_time, "events")
        macro_window_start = config.window_start_for(decision_time, "macro")
        text_window_start = config.window_start_for(decision_time, "text")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        data_quality_warnings = self._domain_status_gates(config)
        domains: dict[str, dict[str, object]] = {}
        profiles: dict[str, dict[str, object]] = {}
        total_started = time.perf_counter()

        # Research-universe screen: one decision-time set restricts every
        # per-stock domain below (None = screening off, zero overhead).
        screened = self._screened_codes(decision_time, config)

        started = time.perf_counter()
        daily, daily_meta = self._build_daily(decision_time, daily_window_start)
        daily = self._apply_screen(daily, screened)
        daily_meta = {**daily_meta, "rows": int(len(daily))}
        profiles["daily.parquet"] = _write_with_profile(
            output_dir / "daily.parquet", daily, build_seconds=time.perf_counter() - started
        )
        domains["daily"] = daily_meta

        started = time.perf_counter()
        auction, auction_meta = self._build_auction(
            daily_window_start.strftime("%Y%m%d"), decision_time.strftime("%Y%m%d")
        )
        auction = self._apply_screen(auction, screened)
        if not auction.empty:
            auction = auction[auction["available_at"] <= decision_time.isoformat()].reset_index(drop=True)
            auction_meta = {**auction_meta, "rows": int(len(auction))}
        profiles["auction.parquet"] = _write_with_profile(
            output_dir / "auction.parquet", auction, build_seconds=time.perf_counter() - started
        )
        domains["auction"] = auction_meta

        started = time.perf_counter()
        if config.include_intraday:
            intraday, intraday_meta = self._build_intraday(decision_time, daily_meta["trade_dates"], config)
            intraday = self._apply_screen(intraday, screened)
            intraday_meta = {**intraday_meta, "rows": int(len(intraday))}
        else:
            intraday, intraday_meta = pd.DataFrame(), {"rows": 0, "datasets": [], "skipped": True}
        profiles["intraday_1min.parquet"] = _write_with_profile(
            output_dir / "intraday_1min.parquet", intraday, build_seconds=time.perf_counter() - started
        )
        domains["intraday_1min"] = intraday_meta

        started = time.perf_counter()
        if config.fundamental_datasets:
            self._assert_fundamental_event_status_ok()
        fundamentals = read_fundamental_events(
            self.fundamental_events_root,
            decision_time.isoformat(),
            datasets=config.fundamental_datasets,
            min_available_at=fundamentals_window_start.isoformat(),
            require_partitions=bool(config.fundamental_datasets),
        )
        fundamentals = self._apply_screen(fundamentals, screened)
        profiles["fundamentals.parquet"] = _write_with_profile(
            output_dir / "fundamentals.parquet", fundamentals, build_seconds=time.perf_counter() - started
        )
        domains["fundamentals"] = {
            "rows": int(len(fundamentals)),
            "datasets": list(config.fundamental_datasets),
            "units": "source",
        }

        started = time.perf_counter()
        events, events_meta = self._build_available_at_domain(config.events_datasets, decision_time, events_window_start)
        events = self._apply_screen(events, screened)
        events_meta = {**events_meta, "rows": int(len(events))}
        profiles["events.parquet"] = _write_with_profile(
            output_dir / "events.parquet", events, build_seconds=time.perf_counter() - started
        )
        domains["events"] = events_meta

        started = time.perf_counter()
        macro, macro_meta = self._build_available_at_domain(config.macro_datasets, decision_time, macro_window_start)
        profiles["macro.parquet"] = _write_with_profile(
            output_dir / "macro.parquet", macro, build_seconds=time.perf_counter() - started
        )
        domains["macro"] = macro_meta

        started = time.perf_counter()
        text_index, text_meta = self._build_text(config, decision_time, text_window_start, output_dir)
        profiles["text_index.parquet"] = _write_with_profile(
            output_dir / "text_index.parquet", text_index, build_seconds=time.perf_counter() - started
        )
        domains["text"] = text_meta

        started = time.perf_counter()
        universe = self._build_universe(decision_time, config)
        universe = self._apply_screen(universe, screened)
        profiles["universe.parquet"] = _write_with_profile(
            output_dir / "universe.parquet", universe, build_seconds=time.perf_counter() - started
        )
        domains["universe"] = {"rows": int(len(universe))}
        domains["universe_screen"] = {
            "active": screened is not None,
            "codes": len(screened) if screened is not None else None,
            "config": config.to_record()["universe_screen"],
        }

        manifest = {
            "snapshot_id": new_id("snap"),
            "kind": "decision_input",
            "created_at": utc_now_iso(),
            "decision_time": decision_time.isoformat(),
            "window_start": daily_window_start.isoformat(),
            "window_months": config.months_for("daily"),
            "window_config": config.to_record()["decision_windows"],
            "domain_windows": {
                "daily": {"window_start": daily_window_start.isoformat(), "window_months": config.months_for("daily")},
                "fundamentals": {"window_start": fundamentals_window_start.isoformat(), "window_months": config.months_for("fundamentals")},
                "events": {"window_start": events_window_start.isoformat(), "window_months": config.months_for("events")},
                "macro": {"window_start": macro_window_start.isoformat(), "window_months": config.months_for("macro")},
                "text": {"window_start": text_window_start.isoformat(), "window_months": config.months_for("text")},
                "intraday_1min": {"trade_days": config.intraday_trade_days},
            },
            "domains": domains,
            "data_quality_warnings": data_quality_warnings,
            "raw_generation": raw_generation,
            "build_profile": {
                "total_seconds": round(time.perf_counter() - total_started, 3),
                "domains": _profile_timings(profiles),
            },
            "data_profile": {"files": profiles},
            "snapshot_hash": "",
        }
        manifest["snapshot_hash"] = _snapshot_hash(output_dir)
        _write_manifest(output_dir, manifest)
        return manifest

    # Enabled-domain data-quality gates over the audit status files. Execution-
    # critical domains (daily bars, intraday minutes; fundamentals has its own
    # stricter gate) hard-fail on a missing/unreadable/error status — bad
    # execution data invalidates every fill. Research domains (events/macro/
    # text) degrade to a manifest warning: their audits flag source-level
    # sparsity and calibration artifacts that should not block an experiment.
    _DOMAIN_STATUS_FILES: tuple[tuple[str, str, bool], ...] = (
        ("daily", "base_research_status.json", True),
        ("intraday_1min", "intraday_minutes_status.json", True),
        ("events", "event_flow_status.json", False),
        ("macro", "macro_context_status.json", False),
        ("text", "text_evidence_status.json", False),
    )

    def _domain_status_gates(self, config: SnapshotConfig) -> dict[str, str]:
        """Check each enabled domain's audit status; return research-domain warnings."""
        if self.data_quality_dir is None:
            return {}
        enabled = {
            "daily": True,
            "intraday_1min": bool(config.include_intraday),
            "events": bool(config.events_datasets),
            "macro": bool(config.macro_datasets),
            "text": bool(config.text_datasets),
        }
        warnings: dict[str, str] = {}
        for domain, filename, critical in self._DOMAIN_STATUS_FILES:
            if not enabled[domain]:
                continue
            path = self.data_quality_dir / filename
            problem = ""
            if not path.exists():
                problem = "status file missing"
            else:
                try:
                    status = str(json.loads(path.read_text(encoding="utf-8")).get("status", "")).lower()
                except json.JSONDecodeError:
                    problem = "status file unreadable"
                else:
                    if status == "error":
                        problem = "audit status is error"
            if not problem:
                continue
            if critical:
                raise ValueError(
                    f"data-quality gate failed for execution-critical domain {domain!r}: {problem} ({path})"
                )
            warnings[domain] = f"{problem} ({filename})"
        return warnings

    def _assert_fundamental_event_status_ok(self) -> None:
        if self.fundamental_events_status is None:
            raise ValueError("PIT fundamental events status is required when fundamental datasets are enabled")
        if not self.fundamental_events_status.exists():
            raise FileNotFoundError(f"missing PIT fundamental events status: {self.fundamental_events_status}")
        try:
            report = json.loads(self.fundamental_events_status.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid PIT fundamental events status JSON: {self.fundamental_events_status}") from exc
        errors = int(report.get("errors", 0) or 0)
        status = str(report.get("status", "")).lower()
        if status == "error" or errors > 0:
            raise ValueError(
                f"PIT fundamental events audit is not usable: "
                f"status={report.get('status')!r} errors={errors} path={self.fundamental_events_status}"
            )

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
        """Replay region data: daily bars plus the events/text/minutes/macro/
        fundamentals published inside the period, every domain carrying row-level
        ``available_at`` so the per-tick Timeview can roll each dataset in on its
        refresh node. Read only by backtest_tool; never PIT-filtered up front."""
        with self._raw_lake_guard() as raw_generation:
            manifest = self._build_replay_slot_impl(start_date, end_date, output_dir, label, config, raw_generation)
        return manifest

    def _build_replay_slot_impl(
        self,
        start_date: str,
        end_date: str,
        output_dir: str | Path,
        label: str,
        config: SnapshotConfig | None,
        raw_generation: dict[str, object] | None,
    ) -> dict[str, object]:
        config = config or SnapshotConfig()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        start_key, end_key = yyyymmdd(start_date), yyyymmdd(end_date)
        period_start = pd.Timestamp(start_key, tz=CN_TZ)
        period_end = pd.Timestamp(end_key, tz=CN_TZ) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        domains: dict[str, dict[str, object]] = {}
        profiles: dict[str, dict[str, object]] = {}
        total_started = time.perf_counter()

        # Same screened set the agent's decision snapshot used: anchored strictly
        # BEFORE the period, frozen across it (no intra-period re-screening).
        screened = self._screened_codes(
            period_start.to_pydatetime() - timedelta(seconds=1), config
        ) if config.screening_active() else None

        started = time.perf_counter()
        daily = self._daily_join(start_key, end_key)
        daily = self._apply_screen(daily, screened)
        daily, conversions = normalize_daily_units(daily)
        daily = _stamp_daily_available_at(daily, self.contracts["daily"])
        profiles["daily.parquet"] = _write_with_profile(
            output_dir / "daily.parquet", daily, build_seconds=time.perf_counter() - started
        )
        domains["daily"] = {"rows": int(len(daily)), "unit_conversions": conversions}

        if config.replay_include_macro:
            started = time.perf_counter()
            macro, macro_meta = self._build_available_at_domain(
                config.macro_datasets, period_end, period_start
            )
            profiles["macro.parquet"] = _write_with_profile(
                output_dir / "macro.parquet", macro, build_seconds=time.perf_counter() - started
            )
            domains["macro"] = macro_meta
        if config.replay_include_fundamentals and config.fundamental_datasets:
            started = time.perf_counter()
            # Not the formal PIT decision boundary: take fundamentals published
            # inside the period without requiring partitions or the audit status,
            # so a slot still builds where a fundamental window happens to be empty.
            fundamentals = read_fundamental_events(
                self.fundamental_events_root,
                period_end.isoformat(),
                datasets=config.fundamental_datasets,
                min_available_at=period_start.isoformat(),
                require_partitions=False,
            )
            fundamentals = self._apply_screen(fundamentals, screened)
            profiles["fundamentals.parquet"] = _write_with_profile(
                output_dir / "fundamentals.parquet", fundamentals, build_seconds=time.perf_counter() - started
            )
            domains["fundamentals"] = {"rows": int(len(fundamentals)), "datasets": list(config.fundamental_datasets)}

        if config.replay_include_events:
            started = time.perf_counter()
            events, events_meta = self._build_available_at_domain(
                config.events_datasets, period_end, period_start
            )
            events = self._apply_screen(events, screened)
            events_meta = {**events_meta, "rows": int(len(events))}
            profiles["events.parquet"] = _write_with_profile(
                output_dir / "events.parquet", events, build_seconds=time.perf_counter() - started
            )
            domains["events"] = events_meta
        if config.replay_include_text:
            started = time.perf_counter()
            text_index, text_meta = self._build_text(config, period_end, period_start, output_dir)
            profiles["text_index.parquet"] = _write_with_profile(
                output_dir / "text_index.parquet", text_index, build_seconds=time.perf_counter() - started
            )
            domains["text"] = text_meta
        if config.replay_include_minutes:
            started = time.perf_counter()
            minutes, minutes_meta = self._read_minutes_range(start_key, end_key)
            minutes = self._apply_screen(minutes, screened)
            minutes_meta = {**minutes_meta, "rows": int(len(minutes))}
            profiles["intraday_1min.parquet"] = _write_with_profile(
                output_dir / "intraday_1min.parquet", minutes, build_seconds=time.perf_counter() - started
            )
            domains["intraday_1min"] = minutes_meta

        started = time.perf_counter()
        actions, actions_meta = self._build_corporate_actions(start_key, end_key, period_end)
        profiles["corporate_actions.parquet"] = _write_with_profile(
            output_dir / "corporate_actions.parquet", actions, build_seconds=time.perf_counter() - started
        )
        domains["corporate_actions"] = actions_meta

        started = time.perf_counter()
        auction, auction_meta = self._build_auction(start_key, end_key)
        auction = self._apply_screen(auction, screened)
        auction_meta = {**auction_meta, "rows": int(len(auction))}
        profiles["auction.parquet"] = _write_with_profile(
            output_dir / "auction.parquet", auction, build_seconds=time.perf_counter() - started
        )
        domains["auction"] = auction_meta
        domains["universe_screen"] = {
            "active": screened is not None,
            "codes": len(screened) if screened is not None else None,
            "config": config.to_record()["universe_screen"],
        }

        manifest = {
            "snapshot_id": new_id("replay"),
            "kind": "replay_slot",
            "label": label,
            "created_at": utc_now_iso(),
            "period_start": start_key,
            "period_end": end_key,
            "domains": domains,
            "raw_generation": raw_generation,
            "build_profile": {
                "total_seconds": round(time.perf_counter() - total_started, 3),
                "domains": _profile_timings(profiles),
            },
            "data_profile": {"files": profiles},
            "snapshot_hash": "",
        }
        manifest["snapshot_hash"] = _snapshot_hash(output_dir)
        _write_manifest(output_dir, manifest)
        return manifest

    _AUCTION_COLUMNS = (
        "ts_code", "trade_date", "session", "price", "vol", "amount", "pre_close",
        "turnover_rate", "volume_ratio", "float_share", "available_at", "available_at_rule",
    )

    def _build_auction(self, start_key: str, end_key: str) -> tuple[pd.DataFrame, dict[str, object]]:
        """Exact opening call-auction results available from 2025-01-16."""
        frame = self.store.read_trade_range("stk_auction", start_key, end_key)
        price_quality = {"source_price_rows": 0, "derived_price_rows": 0, "no_trade_rows": 0}
        if frame.empty:
            auction = pd.DataFrame(columns=list(self._AUCTION_COLUMNS))
        else:
            auction = frame.copy()
            price = pd.to_numeric(auction["price"], errors="coerce")
            volume = pd.to_numeric(auction["vol"], errors="coerce")
            amount = pd.to_numeric(auction["amount"], errors="coerce")
            valid_price = price.notna() & price.gt(0)
            derived_price = ~valid_price & volume.gt(0) & amount.gt(0)
            no_trade = ~valid_price & volume.fillna(0).eq(0) & amount.fillna(0).eq(0)
            price_quality = {
                "source_price_rows": int(valid_price.sum()),
                "derived_price_rows": int(derived_price.sum()),
                "no_trade_rows": int(no_trade.sum()),
            }
            auction["session"] = "open"
            auction["available_at"] = [
                f"{d[:4]}-{d[4:6]}-{d[6:]}T09:25:00+08:00"
                for d in auction["trade_date"].astype(str)
            ]
            auction["available_at_rule"] = "rule:opening_auction_match_time"
            auction = auction[list(self._AUCTION_COLUMNS)]
            auction = auction.sort_values(["trade_date", "session", "ts_code"]).reset_index(drop=True)
        return auction, {
            "rows": int(len(auction)),
            "datasets": ["stk_auction"],
            "units": "vol=股, amount=元",
            "coverage_start": "20250116",
            "clearing_price_fields": {"open": "price", "close": "15:00 bar close"},
            "precoverage_fallback": "labelled 09:30 minute proxy; Shenzhen vol/amount use configured correction",
            "price_quality": price_quality,
        }

    _CORPORATE_ACTION_COLUMNS = (
        "ts_code", "ex_date", "record_date", "pay_date", "div_listdate",
        "cash_per_share", "stock_per_share",
    )

    def _build_corporate_actions(
        self, start_key: str, end_key: str, period_end: pd.Timestamp
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        """Implemented dividend events with an ex-date inside the replay window,
        one row per (ts_code, ex_date): SimBroker's ex-date corporate-action truth
        (docs/environment_design.md §3.2). Not an agent input — agent visibility of
        dividends stays announcement-gated via the PIT fundamental events.

        ``cash_per_share`` is the gross (税前) per-share cash amount and
        ``stock_per_share`` the combined 送转 ratio. Announcements are read without
        a lower available_at bound (an ex-date can trail its 实施公告 by weeks), a
        row announced only after its own ex-date is dropped as a revision artifact,
        and same-day events for one code are summed (they share the record-date
        share base)."""
        raw = read_fundamental_events(
            self.fundamental_events_root,
            period_end.isoformat(),
            datasets=("dividend",),
            require_partitions=False,
        )
        empty = pd.DataFrame(columns=list(self._CORPORATE_ACTION_COLUMNS))
        dropped = {"missing_ex_date": 0, "announced_after_ex_date": 0}
        meta: dict[str, object] = {"rows": 0, "datasets": ["dividend"], "dropped": dropped}
        if raw.empty:
            return empty, meta
        required = {
            "ts_code", "end_date", "div_proc", "ex_date", "available_at",
            "cash_div", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate",
            "record_date", "pay_date", "div_listdate",
        }
        missing = sorted(required - set(raw.columns))
        if missing:
            raise ValueError(f"dividend events missing columns: {missing}")
        frame = raw[raw["div_proc"].astype(str) == "实施"].copy()
        for column in ("ex_date", "record_date", "pay_date", "div_listdate"):
            frame[column] = frame[column].astype("string").str.strip().fillna("")
        dropped["missing_ex_date"] = int((frame["ex_date"] == "").sum())
        frame = frame[(frame["ex_date"] >= start_key) & (frame["ex_date"] <= end_key)]
        if frame.empty:
            return empty, meta
        announced = frame["available_at"].astype(str).str[:10].str.replace("-", "", regex=False)
        late = announced > frame["ex_date"]
        dropped["announced_after_ex_date"] = int(late.sum())
        frame = frame[~late]
        # Latest announced version per dividend event, then per-share amounts with
        # the documented fallbacks (cash_div_tax is gross; stk_div is the combined
        # 送股+转增 ratio; audit.py records the unit semantics).
        frame = frame.sort_values("available_at").drop_duplicates(["ts_code", "end_date", "ex_date"], keep="last")
        cash = pd.to_numeric(frame["cash_div_tax"], errors="coerce")
        cash = cash.fillna(pd.to_numeric(frame["cash_div"], errors="coerce")).fillna(0.0)
        bo = pd.to_numeric(frame["stk_bo_rate"], errors="coerce").fillna(0.0)
        co = pd.to_numeric(frame["stk_co_rate"], errors="coerce").fillna(0.0)
        stock = pd.to_numeric(frame["stk_div"], errors="coerce").fillna(bo + co)
        frame = frame.assign(cash_per_share=cash.clip(lower=0.0), stock_per_share=stock.clip(lower=0.0))
        frame = frame[(frame["cash_per_share"] > 0.0) | (frame["stock_per_share"] > 0.0)]
        if frame.empty:
            return empty, meta
        out = (
            frame.groupby(["ts_code", "ex_date"], as_index=False)
            .agg(
                cash_per_share=("cash_per_share", "sum"),
                stock_per_share=("stock_per_share", "sum"),
                record_date=("record_date", "first"),
                pay_date=("pay_date", "first"),
                div_listdate=("div_listdate", "max"),
            )
            .sort_values(["ex_date", "ts_code"], ignore_index=True)
        )
        meta["rows"] = int(len(out))
        return out[list(self._CORPORATE_ACTION_COLUMNS)], meta

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
        if not minutes.empty:
            # Match the frozen intraday schema (_build_intraday) so the Timeview rolls
            # replay rows into the same columns and never NaN-backfills the auction
            # correction; available_at is kept here as the row-level Timeview gate.
            minutes = apply_open_auction_correction(minutes)
        return minutes, {"rows": int(len(minutes)), "datasets": ["stk_mins_1min_by_date"], "files": len(frames)}

    # ---- domain builders ----

    def _build_daily(self, decision_time: datetime, window_start: pd.Timestamp) -> tuple[pd.DataFrame, dict[str, object]]:
        daily_datasets = ("daily", "daily_basic", "adj_factor", "stk_limit", "suspend_d")
        visible_by_dataset = {
            dataset: self._visible_trade_dates(dataset, decision_time, window_start) for dataset in daily_datasets
        }
        visible_dates = visible_by_dataset["daily"]
        if not visible_dates:
            raise ValueError(f"no visible daily trade dates before {decision_time.isoformat()}")
        frame = self._daily_join(visible_dates[0], visible_dates[-1], visible_dates_by_dataset=visible_by_dataset)
        frame, conversions = normalize_daily_units(frame)
        meta = {
            "rows": int(len(frame)),
            "datasets": list(daily_datasets),
            "coverage_start": visible_dates[0],
            "coverage_end": visible_dates[-1],
            "trade_dates": visible_dates,
            "visible_trade_dates_by_dataset": visible_by_dataset,
            "units": "unit_contract",  # the only domain the unit contract covers
            "unit_conversions": conversions,
            "availability_rule": "per-dataset daily contracts; joins include only partitions visible at the decision time",
        }
        return frame, meta

    def _visible_trade_dates(self, dataset: str, decision_time: datetime, window_start: pd.Timestamp) -> list[str]:
        contract = self.contracts[dataset]
        return [
            key
            for key in self.store.trade_dates(dataset)
            if contract.available_at(datetime.strptime(key, "%Y%m%d").date()) <= decision_time
            and pd.Timestamp(key, tz=CN_TZ) >= window_start
        ]

    def _daily_join(
        self,
        start: str,
        end: str,
        *,
        visible_dates_by_dataset: dict[str, list[str]] | None = None,
    ) -> pd.DataFrame:
        daily = self.store.read_trade_range("daily", start, end)
        if daily.empty:
            raise ValueError(f"daily raw data empty for {start}..{end}")
        basic = self.store.read_trade_range("daily_basic", start, end)
        limits = self.store.read_trade_range("stk_limit", start, end)
        adj = self.store.read_trade_range("adj_factor", start, end)
        suspend = self.store.read_trade_range("suspend_d", start, end, columns=["trade_date", "ts_code"])
        if visible_dates_by_dataset is not None:
            daily = _filter_trade_dates(daily, visible_dates_by_dataset.get("daily", []))
            basic = _filter_trade_dates(basic, visible_dates_by_dataset.get("daily_basic", []))
            limits = _filter_trade_dates(limits, visible_dates_by_dataset.get("stk_limit", []))
            adj = _filter_trade_dates(adj, visible_dates_by_dataset.get("adj_factor", []))
            suspend = _filter_trade_dates(suspend, visible_dates_by_dataset.get("suspend_d", []))
            if daily.empty:
                raise ValueError(f"daily raw data empty after PIT filter for {start}..{end}")
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
            # For minute bars available_at == the bar close (trade_time), so it is an
            # internal gating column, not agent information. Drop it (as daily does) to
            # keep the agent-facing intraday schema clean; the replay slot keeps its own
            # available_at as the Timeview gate.
            minute = minute.drop(columns=["available_at", "available_at_rule"], errors="ignore")
        meta = {
            "rows": int(len(minute)),
            "datasets": ["stk_mins_1min_by_date"],
            "trade_dates": recent,
            "availability_rule": "available_at=bar close time (trade_time)",
            "auction_correction": {
                "rule_id": "minute_0930_to_live_stk_auction_by_market_bucket",
                "factors": {"00*.SZ": 0.76, "30*.SZ": 0.58, "other": 1.0},
                "applies_to": "09:30 SZ bars as live stk_auction proxy columns only",
            },
        }
        return minute, meta

    def _build_available_at_domain(
        self, datasets: tuple[str, ...], decision_time: datetime, window_start: pd.Timestamp
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        frames: list[pd.DataFrame] = []
        rules: dict[str, str] = {}
        duplicate_rows_dropped: dict[str, int] = {}
        for dataset in datasets:
            dataset_dir = self.raw_dir / dataset
            if not dataset_dir.exists():
                raise FileNotFoundError(f"missing configured dataset directory: {dataset_dir}")
            rows = self._read_dataset_window(dataset_dir, decision_time, window_start)
            rules[dataset] = "raw available_at column"
            if rows.empty:
                continue
            # Overlapping partition files (the pre-canonical macro range
            # family) repeat identical rows; a duplicated series distorts
            # every frequency/aggregate a strategy computes on it.
            deduped = rows.drop_duplicates(ignore_index=True)
            if len(deduped) < len(rows):
                duplicate_rows_dropped[dataset] = int(len(rows) - len(deduped))
                rows = deduped
            rows.insert(0, "dataset", dataset)
            frames.append(rows)
        merged = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        # units="source": heterogeneous unions keep TuShare per-source units —
        # the daily-domain unit contract does NOT extend to same-named fields
        # here (env docs §1.4; raw unit table in data docs §1.2).
        meta = {"rows": int(len(merged)), "datasets": list(datasets), "units": "source", "availability_rules": rules}
        if duplicate_rows_dropped:
            meta["duplicate_rows_dropped"] = duplicate_rows_dropped
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
            if dataset == "news":
                news_start = window_start
                if config.news_window_months is not None:
                    news_start = max(
                        window_start, pd.Timestamp(decision_time) - pd.DateOffset(months=config.news_window_months)
                    )
                if config.news_sources:
                    source_dirs = [dataset_dir / f"src={source}" for source in config.news_sources]
                    for source_dir in source_dirs:
                        if not source_dir.exists():
                            raise FileNotFoundError(f"missing configured news source: {source_dir}")
                else:
                    source_dirs = sorted(p for p in dataset_dir.glob("src=*") if p.is_dir())
                source_frames = []
                for source_dir in source_dirs:
                    source_rows = self._read_dataset_window(source_dir, decision_time, news_start)
                    if not source_rows.empty:
                        source_frames.append(source_rows.assign(src=source_dir.name.split("=", 1)[1]))
                rows = pd.concat(source_frames, ignore_index=True) if source_frames else pd.DataFrame()
            else:
                rows = self._read_dataset_window(dataset_dir, decision_time, window_start)
            if rows.empty:
                continue
            if dataset in {"irm_qa_sh", "irm_qa_sz"}:
                title_column = "q" if "q" in rows.columns else None
                body_columns = [c for c in ("q", "a") if c in rows.columns]
            else:
                title_column = next((c for c in ("title", "report_title", "name") if c in rows.columns), None)
                body_columns = [
                    c for c in ("title", "report_title", "abstr", "content", "content_html", "url")
                    if c in rows.columns
                ]
            if title_column is None or not body_columns:
                raise ValueError(f"text dataset {dataset} has no usable title/body columns: {list(rows.columns)}")
            titles = rows[title_column].fillna("").astype(str)
            if "content" in rows.columns:
                titles = titles.where(titles.str.len() > 0, rows["content"].fillna("").astype(str))
            bodies = rows[body_columns[0]].fillna("").astype(str)
            for key in body_columns[1:]:
                bodies = bodies + "\n" + rows[key].fillna("").astype(str)
            bodies = bodies.str.slice(0, config.text_body_chars)
            if dataset == "news":
                # Cross-source duplicate flashes collapse to the earliest copy;
                # identity is the truncated body content.
                order = rows["available_at"].astype(str).sort_values(kind="stable").index
                hashes = pd.Series([hashlib.sha1(body.encode("utf-8")).hexdigest() for body in bodies], index=rows.index)
                keep = order[~hashes.loc[order].duplicated().values]
                rows, titles, bodies = rows.loc[keep], titles.loc[keep], bodies.loc[keep]
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

    _BOARD_PREFIXES = {
        "main": ("600", "601", "603", "605", "000", "001", "002", "003"),
        "gem": ("300", "301", "302"),
        "star": ("688", "689"),
        "bj": (),  # matched by the .BJ suffix instead
    }

    def _screened_codes(self, decision_time: datetime, config: SnapshotConfig) -> frozenset[str] | None:
        """Research-universe screen, evaluated with decision-time knowledge only.

        Returns None when screening is off. ST status comes from the as-of name
        (namechange), listing age from stock_basic list_date, cap/price bands
        from the latest daily_basic row at or before the anchor day. Codes with
        a missing attribute fail closed for that filter (an unnamed or unpriced
        code cannot prove eligibility)."""
        if not config.screening_active():
            return None
        universe = self._build_universe(decision_time, replace(config, include_industry=False))
        day = decision_time.strftime("%Y%m%d")
        keep = universe[["ts_code"]].copy()
        keep["name"] = universe.get("name")
        keep["list_date"] = universe.get("list_date")
        if config.screen_exclude_st:
            names = keep["name"].fillna("").astype(str).str.upper()
            keep = keep[(names != "") & ~names.str.contains("ST")]
        if config.screen_exclude_new_listed_days > 0:
            cutoff = (decision_time - timedelta(days=config.screen_exclude_new_listed_days)).strftime("%Y%m%d")
            listed = keep["list_date"].fillna("").astype(str)
            keep = keep[(listed != "") & (listed <= cutoff)]
        if config.screen_boards:
            codes = keep["ts_code"].astype(str)
            allowed_boards = set(config.screen_boards)
            prefixes = tuple(p for board in allowed_boards for p in self._BOARD_PREFIXES[board])
            mask = codes.str.startswith(prefixes) if prefixes else pd.Series(False, index=codes.index)
            if "bj" in allowed_boards:
                mask = mask | codes.str.endswith(".BJ")
            keep = keep[mask]
        needs_basic = any(
            value is not None
            for value in (config.screen_min_circ_mv_yi, config.screen_max_circ_mv_yi,
                          config.screen_min_price, config.screen_max_price)
        )
        if needs_basic:
            basic_dates = [d for d in self.store.trade_dates("daily_basic") if d <= day]
            if not basic_dates:
                raise FileNotFoundError(f"universe screening needs a daily_basic partition at or before {day}")
            basic = self.store.read_trade_date("daily_basic", basic_dates[-1], columns=["ts_code", "close", "circ_mv"])
            keep = keep.merge(basic, on="ts_code", how="left")
            circ_mv_yi = pd.to_numeric(keep["circ_mv"], errors="coerce") / 1e4  # 万元 -> 亿元
            close = pd.to_numeric(keep["close"], errors="coerce")
            if config.screen_min_circ_mv_yi is not None:
                keep = keep[circ_mv_yi.reindex(keep.index) >= config.screen_min_circ_mv_yi]
            if config.screen_max_circ_mv_yi is not None:
                keep = keep[circ_mv_yi.reindex(keep.index) <= config.screen_max_circ_mv_yi]
            if config.screen_min_price is not None:
                keep = keep[close.reindex(keep.index) >= config.screen_min_price]
            if config.screen_max_price is not None:
                keep = keep[close.reindex(keep.index) <= config.screen_max_price]
        screened = frozenset(keep["ts_code"].astype(str))
        if not screened:
            raise ValueError(
                "universe screening left ZERO eligible codes at the decision anchor - "
                "loosen the screen_* configuration (this would otherwise surface later "
                "as an empty replay region)"
            )
        return screened

    @staticmethod
    def _apply_screen(frame: pd.DataFrame, allowed: frozenset[str] | None) -> pd.DataFrame:
        """Restrict per-stock rows to the screened set; market-level rows
        (no ts_code column or null ts_code) always pass."""
        if allowed is None or frame.empty or "ts_code" not in frame.columns:
            return frame
        codes = frame["ts_code"].astype(str)
        return frame[frame["ts_code"].isna() | codes.isin(allowed)].reset_index(drop=True)

    def _build_universe(self, decision_time: datetime, config: SnapshotConfig) -> pd.DataFrame:
        """Stocks listed as of the decision day (delistings after it included).

        Building from the current L partition alone would drop names delisted
        later than the decision day and inject survivorship bias. Point-in-time
        columns: ``name`` is the name in force at the decision day (from
        namechange — the current stock_basic name may be a future rename), and
        ``delist_date`` is dropped after filtering — every survivor's delisting
        is after the decision day, i.e. future information.
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
        keep = [col for col in ("ts_code", "exchange", "list_date", "delist_date", "market") if col in basic.columns]
        universe = basic[keep].copy()
        universe["ts_code"] = universe["ts_code"].astype(str)
        universe = universe.drop_duplicates("ts_code", keep="first")
        if "list_date" in universe.columns:
            universe = universe[universe["list_date"].fillna("").astype(str) <= day]
        if "delist_date" in universe.columns:
            delist = universe["delist_date"].fillna("").astype(str)
            universe = universe[(delist == "") | (delist == "None") | (delist > day)]
            universe = universe.drop(columns=["delist_date"])
        universe = universe.merge(self._names_as_of(decision_time), on="ts_code", how="left")
        if config.include_industry:
            industry = self._industry_membership(decision_time.strftime("%Y%m%d"))
            if not industry.empty:
                universe = universe.merge(industry, on="ts_code", how="left")
        return universe.reset_index(drop=True)

    def _names_as_of(self, decision_time: datetime) -> pd.DataFrame:
        """``ts_code -> name`` in force at the decision day (announced by then).

        The namechange dataset carries every code's listing name, so a null
        merge result is a genuine data gap, not a normal case; the current
        stock_basic name is never used as a fallback — it may be a rename the
        market had not seen at the decision day."""
        path = self.raw_dir / "namechange" / "namechange.parquet"
        if not path.exists():
            raise FileNotFoundError(f"namechange dataset required for as-of universe names: {path}")
        names = pd.read_parquet(path)
        day = decision_time.strftime("%Y%m%d")
        if "ann_date" in names.columns:
            names = names[names["ann_date"].astype(str).str.strip().le(day) | names["ann_date"].isna()]
        names = names[names["start_date"].astype(str) <= day]
        names = names.sort_values("start_date").drop_duplicates("ts_code", keep="last")
        return names[["ts_code", "name"]]

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


def _stamp_daily_available_at(daily: pd.DataFrame, contract) -> pd.DataFrame:
    """Add a row-level ``available_at`` to replay daily bars (the daily core's
    publish time, ``trade_date`` close). The Timeview gates the whole daily domain
    on the evening refresh node, so any time before that night's 23:35 makes the
    row roll in from the next day; this column carries that timestamp explicitly."""
    if daily.empty or "trade_date" not in daily.columns:
        return daily
    out = daily.copy()
    out["available_at"] = [
        contract.available_at(datetime.strptime(str(date), "%Y%m%d").date()).isoformat()
        for date in out["trade_date"].astype(str)
    ]
    return out


def _filter_trade_dates(frame: pd.DataFrame, visible_dates: list[str]) -> pd.DataFrame:
    if frame.empty or "trade_date" not in frame.columns:
        return frame.copy()
    visible = set(visible_dates)
    out = frame[frame["trade_date"].astype(str).isin(visible)].copy()
    return out


def _window_start(decision_time: datetime, months: int) -> pd.Timestamp:
    window_start = (pd.Timestamp(decision_time) - pd.DateOffset(months=months)).tz_localize(None)
    return window_start.tz_localize(CN_TZ)


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


def read_raw_generation(raw_dir: str | Path | None) -> dict[str, object] | None:
    """Raw-lake generation stamp published by the cron updater after each
    fully-successful mutation run; None for lakes without one (manual/test)."""
    if raw_dir is None:
        return None
    path = Path(raw_dir) / ".raw_generation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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


PROFILE_DATE_COLUMNS = ("trade_date", "date", "available_at", "trade_time", "ann_date", "end_date")
PROFILE_NULL_COLUMNS = (
    "ts_code",
    "trade_date",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "amount",
    "vol",
    "dataset",
    "text_id",
)


def _write_with_profile(path: Path, frame: pd.DataFrame, *, build_seconds: float) -> dict[str, object]:
    started = time.perf_counter()
    _write(path, frame)
    return _frame_profile(path, frame, build_seconds=build_seconds, write_seconds=time.perf_counter() - started)


def _profile_timings(profiles: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        name: {"build_seconds": item["build_seconds"], "write_seconds": item["write_seconds"]}
        for name, item in profiles.items()
    }


def _frame_profile(
    path: Path,
    frame: pd.DataFrame,
    *,
    build_seconds: float,
    write_seconds: float,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "file": path.name,
        "rows": int(len(frame)),
        "size_bytes": int(path.stat().st_size) if path.exists() else 0,
        "column_count": int(len(frame.columns)),
        "columns": [str(col) for col in frame.columns],
        "build_seconds": round(float(build_seconds), 3),
        "write_seconds": round(float(write_seconds), 3),
    }
    if not frame.empty:
        date_ranges = _profile_date_ranges(frame)
        if date_ranges:
            profile["date_ranges"] = date_ranges
        key_nulls = _profile_key_nulls(frame)
        if key_nulls:
            profile["key_nulls"] = key_nulls
        if "dataset" in frame.columns and len(frame) <= 1_000_000:
            counts = frame["dataset"].fillna("").astype(str).value_counts().head(50)
            profile["dataset_counts"] = {str(key): int(value) for key, value in counts.items()}
        elif "dataset" in frame.columns:
            profile["dataset_counts"] = "skipped_large_frame"
    return profile


def _profile_date_ranges(frame: pd.DataFrame) -> dict[str, dict[str, str]]:
    ranges: dict[str, dict[str, str]] = {}
    for column in PROFILE_DATE_COLUMNS:
        if column not in frame.columns:
            continue
        values = frame[column].dropna()
        if values.empty:
            continue
        text = values.astype(str)
        ranges[column] = {"min": str(text.min()), "max": str(text.max())}
    return ranges


def _profile_key_nulls(frame: pd.DataFrame) -> dict[str, int]:
    nulls: dict[str, int] = {}
    for column in PROFILE_NULL_COLUMNS:
        if column in frame.columns:
            nulls[column] = int(frame[column].isna().sum())
    return nulls


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
