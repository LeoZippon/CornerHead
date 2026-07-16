# Consolidated unit tests: test_data_sources_tushare.py


# Source: test_tushare_download_update_guards.py
import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from autotrade.data_sources.tushare import audit, common, cron_update, download


class EmptyMinuteClient:
    def query(self, api_name, params=None, fields="", retries=5):
        return common.ApiResult(fields.split(",") if fields else [], [], common.stable_hash({"api_name": api_name, "params": params or {}}))


class CountingMacroClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        result_fields = fields.split(",") if fields else []
        row = []
        for field in result_fields:
            if field == "month":
                row.append(params.get("m", "202605"))
            elif field in {"date", "trade_date"}:
                row.append(params.get("end_date", "20260529"))
            elif field == "publish_date":
                row.append("20260529")
            elif field == "title":
                row.append("sample")
            elif field == "issuing_org":
                row.append("sample_org")
            elif field == "data_api":
                row.append(api_name)
            else:
                row.append("")
        return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))


class BoardClient:
    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        result_fields = fields.split(",") if fields else []
        row = []
        for field in result_fields:
            if field == "trade_date":
                row.append(params.get("trade_date", "20200102"))
            elif field == "ts_code":
                row.append("000001.SZ")
            elif field in {"name", "ts_name"}:
                row.append("sample")
            elif field == "tag":
                row.append(params.get("tag", "涨停"))
            elif field == "limit_type":
                row.append(params.get("limit_type", "涨停池"))
            elif field == "data_type":
                row.append(params.get("market", "热股"))
            elif field == "rank_time":
                row.append("2020-01-02 10:00:00")
            elif field == "hm_name":
                row.append("sample_hot_money")
            elif field == "hm_orgs":
                row.append("sample_org")
            elif field == "exalter":
                row.append("sample_broker")
            elif field == "side":
                row.append("0")
            elif field == "reason":
                row.append("sample_reason")
            elif field == "nums":
                row.append("2")
            elif field == "rank":
                row.append(1)
            elif field == "desc":
                row.append("sample_desc")
            elif field == "orgs":
                row.append("[]")
            else:
                row.append(1.0)
        return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))


class NoQueryClient:
    def query(self, api_name, params=None, fields="", retries=5):
        raise AssertionError(f"unexpected TuShare query: {api_name}")


class ReferenceClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        result_fields = fields.split(",") if fields else []
        rows = []
        if api_name == "stock_basic" and params.get("list_status") == "L":
            rows = [["000001.SZ" if field == "ts_code" else params.get("list_status", "") if field == "list_status" else "" for field in result_fields]]
        elif api_name == "stock_company":
            rows = [["000001.SZ" if field == "ts_code" else params.get("exchange", "") if field == "exchange" else "" for field in result_fields]]
        elif api_name == "namechange":
            rows = [[params.get("ts_code", ""), "sample", "20200101", "", "20200101", "name"]]
        elif api_name == "index_classify":
            rows = [["801010.SI" if field == "index_code" else "L1" if field == "level" else "sample" for field in result_fields]]
        elif api_name == "index_member_all":
            rows = [["801010.SI" if field == "l1_code" else "000001.SZ" if field == "ts_code" else "" for field in result_fields]]
        elif api_name == "ths_index":
            rows = [["885001.TI" if field == "ts_code" else "N" if field == "type" else "sample" for field in result_fields]]
        elif api_name == "ths_member":
            rows = [[params.get("ts_code", "") if field == "ts_code" else "000001.SZ" if field == "con_code" else "" for field in result_fields]]
        elif api_name == "index_basic":
            rows = [["000300.SH" if field == "ts_code" else "sample" for field in result_fields]]
        elif api_name == "hs_const":
            rows = [["000001.SZ" if field == "ts_code" else params.get("hs_type", "") if field == "hs_type" else "" for field in result_fields]]
        elif api_name == "index_weight":
            rows = [[params.get("index_code", "") if field == "index_code" else "000001.SZ" if field == "con_code" else "20260630" if field == "trade_date" else "1.0" for field in result_fields]]
        return common.ApiResult(result_fields, rows, common.stable_hash({"api_name": api_name, "params": params}))


class TradeCalClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        result_fields = fields.split(",") if fields else []
        rows = []
        if api_name == "trade_cal":
            cal_date = params.get("end_date", "20260604")
            rows = [[
                params.get("exchange", "") if field == "exchange" else
                cal_date if field == "cal_date" else
                "1" if field == "is_open" else
                "20260603" if field == "pretrade_date" else
                ""
                for field in result_fields
            ]]
        return common.ApiResult(result_fields, rows, common.stable_hash({"api_name": api_name, "params": params}))


class EmptyReferenceClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        return common.ApiResult(fields.split(",") if fields else [], [], common.stable_hash({"api_name": api_name, "params": params}))


class DailyMarketClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        result_fields = fields.split(",") if fields else []
        row = []
        for field in result_fields:
            if field == "trade_date":
                row.append(params.get("trade_date", "20200102"))
            elif field == "ts_code":
                row.append("000001.SZ")
            elif field == "adj_factor":
                row.append(1.0)
            else:
                row.append(0)
        return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))


class FundamentalClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        if api_name == "income_vip":
            result_fields = ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"]
            row = ["000001.SZ", "20200430", "20200430", params.get("period", "20200331"), "1", "1", "1"]
        elif api_name in {"dividend", "fina_audit", "fina_mainbz_vip"}:
            result_fields = ["ts_code", "ann_date", "end_date"]
            row = [params.get("ts_code", "000001.SZ"), "20200430", "20200331"]
        else:
            result_fields = ["ts_code"]
            row = ["000001.SZ"]
        return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))


class CalendarEventClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        result_fields = fields.split(",") if fields else []
        if api_name == "trade_cal":
            row = []
            for field in result_fields:
                if field == "exchange":
                    row.append(params.get("exchange", "SSE"))
                elif field == "cal_date":
                    row.append(params.get("end_date", "20260604"))
                elif field == "is_open":
                    row.append("1")
                elif field == "pretrade_date":
                    row.append("20260603")
                else:
                    row.append("")
            return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))
        if api_name == "margin_secs":
            row = []
            for field in result_fields:
                if field == "trade_date":
                    row.append(params.get("trade_date", "20260604"))
                elif field == "ts_code":
                    row.append("000001.SZ")
                elif field in {"name", "exchange"}:
                    row.append("sample")
                else:
                    row.append("")
            return common.ApiResult(result_fields, [row], common.stable_hash({"api_name": api_name, "params": params}))
        return common.ApiResult(result_fields, [], common.stable_hash({"api_name": api_name, "params": params}))


class EmptyTradeDateClient:
    def __init__(self):
        self.calls = []

    def query(self, api_name, params=None, fields="", retries=5):
        params = params or {}
        self.calls.append((api_name, dict(params)))
        return common.ApiResult(fields.split(",") if fields else [], [], common.stable_hash({"api_name": api_name, "params": params}))


class ErrorTradeDateClient:
    def query(self, api_name, params=None, fields="", retries=5):
        raise RuntimeError("mock source failure")


class RepeatingPagedClient:
    def query(self, api_name, params=None, fields="", retries=5):
        result_fields = fields.split(",") if fields else ["trade_date", "ts_code"]
        row = ["20200102" if field == "trade_date" else "000001.SZ" for field in result_fields]
        return common.ApiResult(result_fields, [row], common.stable_hash({"fields": result_fields, "items": [row]}))


class TuShareDownloadUpdateGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw_dir = self.root / "raw"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_trade_cal(self, trade_date="20200102", is_open="1"):
        path = self.raw_dir / "trade_cal" / "exchange=SSE" / f"year={trade_date[:4]}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"cal_date": trade_date, "is_open": is_open}]).to_parquet(path, index=False)

    def _write_daily_universe(self, trade_date="20200102"):
        path = self.raw_dir / "daily" / f"trade_date={trade_date}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {"trade_date": trade_date, "ts_code": "000001.SZ"},
            {"trade_date": trade_date, "ts_code": "000002.SZ"},
        ]).to_parquet(path, index=False)

    def test_default_revision_ledger_for_temp_raw_stays_local(self):
        ledger = common.resolve_revision_ledger(self.raw_dir, common.REVISION_EVENTS_PATH, repo_root=Path.cwd())

        self.assertEqual(ledger, self.root / "revision_events.jsonl")

    def test_load_stock_codes_keeps_only_valid_a_share_codes(self):
        path = self.raw_dir / "stock_basic" / "list_status=L.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {"ts_code": "000001.SZ"},
                {"ts_code": "T00018.SH"},
                {"ts_code": "TS0018.SH"},
                {"ts_code": "920126.BJ"},
                {"ts_code": "bad"},
            ]
        ).to_parquet(path, index=False)

        self.assertEqual(common.load_stock_codes(self.raw_dir), ["000001.SZ", "920126.BJ"])

    def test_query_paged_rejects_repeated_full_pages(self):
        with self.assertRaisesRegex(RuntimeError, "returned a repeated page"):
            common.query_paged(RepeatingPagedClient(), "daily", {"trade_date": "20200102"}, "trade_date,ts_code", page_limit=1)

    def test_trade_cal_helpers_normalize_date_strings(self):
        path = self.raw_dir / "trade_cal" / "exchange=SSE" / "year=2026.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {"cal_date": "2026-06-03", "is_open": "1"},
                {"cal_date": "20260604", "is_open": "0"},
                {"cal_date": "2026/06/05", "is_open": "1"},
            ]
        ).to_parquet(path, index=False)

        self.assertEqual(common.load_sse_open_dates(self.raw_dir, "20260603", "20260605"), ["20260603", "20260605"])
        self.assertEqual(common.latest_sse_calendar_date(self.raw_dir), "20260605")
        self.assertTrue(download.sse_trade_cal_covers(self.raw_dir, "20260603", "20260605"))

    def test_bak_basic_audit_ignores_trade_cal_lookahead_after_end_date(self):
        path = self.raw_dir / "bak_basic" / "trade_date=20260618.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"trade_date": "20260618", "ts_code": "000001.SZ"}]).to_parquet(path, index=False)
        findings = []

        audit.audit_bak_basic(self.raw_dir, {"20260618", "20260622"}, "20260618", lambda *item: findings.append(item))

        partition_finding = next(item for item in findings if item[1] == "bak_basic_partitions")
        self.assertEqual(partition_finding[0], "info")
        self.assertEqual(partition_finding[3]["missing_expected_files"], 0)
        self.assertEqual(partition_finding[3]["missing_sample"], [])

    def test_update_intraday_by_date_refuses_zero_row_write_for_nonempty_universe(self):
        self._write_trade_cal()
        self._write_daily_universe()
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            output_dataset=common.STK_MINS_BY_DATE_DATASET,
            expected_codes_source="daily",
            codes=None,
            max_codes=None,
            min_rows_per_day=0,
            allow_missing_codes=2,
            allow_validation_warnings=True,
            max_retries=1,
            retry_delay_seconds=0,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            force=False,
        )

        output = self.raw_dir / common.STK_MINS_BY_DATE_DATASET / "trade_date=20200102.parquet"
        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=EmptyMinuteClient()):
            with self.assertRaisesRegex(RuntimeError, "refusing to write zero-row intraday"):
                download.update_intraday_by_date(args)
        self.assertFalse(output.exists())

    def test_minute_expected_universe_uses_existing_minute_store_when_present(self):
        self._write_daily_universe()
        minute_path = self.raw_dir / common.STK_MINS_BY_DATE_DATASET / "trade_date=20200102.parquet"
        minute_rows = pd.DataFrame([
            {
                "ts_code": "000001.SZ",
                "trade_time": "2020-01-02 09:30:00",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "vol": 100,
                "amount": 100.0,
                "trade_date": "20200102",
                "available_at": "2020-01-02 09:30:00+08:00",
                "available_at_rule": "source:trade_time_bar_close",
            },
            {
                "ts_code": "000001.SZ",
                "trade_time": "2020-01-02 15:00:00",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "vol": 100,
                "amount": 100.0,
                "trade_date": "20200102",
                "available_at": "2020-01-02 15:00:00+08:00",
                "available_at_rule": "source:trade_time_bar_close",
            },
        ])
        common.write_parquet(
            minute_path,
            minute_rows,
            api_name=common.STK_MINS_API_NAME,
            params={},
            fields=list(minute_rows.columns),
            source_hash="minute",
        )

        codes = common.intraday_expected_codes_for_day(
            self.raw_dir,
            argparse.Namespace(expected_codes_source="minute", output_dataset=common.STK_MINS_BY_DATE_DATASET, codes=None, max_codes=None),
            "20200102",
        )

        self.assertEqual(codes, {"000001.SZ"})

    def test_event_flow_trade_date_download_skips_non_trading_day(self):
        self._write_trade_cal("20260530", is_open="0")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20260530",
            end_date="20260530",
            datasets=["margin", "margin_detail", "margin_secs"],
            force=False,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=NoQueryClient()):
            self.assertEqual(download.download_event_flow(args), 0)

    def test_share_float_union_rebuild_refuses_accidental_shrink(self):
        output = self.raw_dir / "share_float_complete" / "share_float_complete.parquet"
        existing = pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20200101", "float_date": "20200102"},
            {"ts_code": "000002.SZ", "ann_date": "20200101", "float_date": "20200102"},
        ])
        common.write_parquet(output, existing, api_name="share_float", params={}, fields=list(existing.columns), source_hash="existing")
        args = argparse.Namespace(
            union_output=str(output),
            ann_start_date="20200101",
            ann_end_date="20200102",
            float_start_date="20200101",
            float_end_date="20200102",
            union_ann_start_date=None,
            union_ann_end_date=None,
            union_float_start_date=None,
            union_float_end_date=None,
            skip_float_date_union=False,
            allow_union_shrink=False,
        )

        with patch.object(download, "share_float_union_files", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "would shrink"):
                download.write_share_float_union(self.raw_dir, args, {})
        self.assertEqual(common.parquet_rows(output), 2)

        # The non-shrinking success path must complete and report (a stale
        # previous-rows reference once raised NameError right after the write).
        source = self.raw_dir / "share_float_ann_date" / "ann_date=20200101.parquet"
        rows = pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20200101", "float_date": "20200102",
             "holder_name": "h1", "share_type": "t"},
            {"ts_code": "000002.SZ", "ann_date": "20200101", "float_date": "20200102",
             "holder_name": "h2", "share_type": "t"},
        ])
        common.write_parquet(source, rows, api_name="share_float", params={}, fields=list(rows.columns), source_hash="src")
        report = {}
        with patch.object(download, "share_float_union_files", return_value=[(source, "ann_date")]):
            download.write_share_float_union(self.raw_dir, args, report)
        self.assertEqual(report["union"]["previous_rows"], 2)
        self.assertEqual(report["union"]["rows_after_dedup"], 2)

    def test_share_float_empty_refresh_keeps_existing_cap_risk_signal(self):
        path = self.raw_dir / "share_float_ann_date" / "ann_date=20200101.parquet"
        fields = common.SHARE_FLOAT_FIELDS.split(",")
        existing = pd.DataFrame([
            {
                field: (
                    f"{index:06d}.SZ" if field == "ts_code"
                    else "20200101" if field == "ann_date"
                    else "20200102" if field == "float_date"
                    else str(index) if field == "holder_name"
                    else "type" if field == "share_type"
                    else 1
                )
                for field in fields
            }
            for index in range(common.SHARE_FLOAT_ROW_LIMIT)
        ])
        common.write_parquet(path, existing, api_name="share_float", params={}, fields=fields, source_hash="old")

        result = download.query_share_float_to_path(
            EmptyTradeDateClient(),
            self.raw_dir,
            path,
            {"ann_date": "20200101"},
            "ann_date",
            True,
            revision_ledger=None,
            allow_empty_revision_overwrite=False,
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["rows"], common.SHARE_FLOAT_ROW_LIMIT)
        self.assertTrue(result["source_cap_risk"])
        self.assertEqual(common.parquet_rows(path), common.SHARE_FLOAT_ROW_LIMIT)

    def test_generic_event_flow_download_excludes_dedicated_share_float_path(self):
        selected = download.selected_event_flow_download_datasets(argparse.Namespace(datasets=None))
        self.assertNotIn("share_float", selected)
        self.assertIn("margin_secs", selected)
        with self.assertRaisesRegex(RuntimeError, "download-share-float-complete"):
            download.selected_event_flow_download_datasets(argparse.Namespace(datasets=["share_float"]))

    def test_range_partition_skip_requires_sidecar_coverage(self):
        path = self.raw_dir / "anns_d" / "month=202605.parquet"
        existing = pd.DataFrame([{"ann_date": "20260528", "title": "old"}])
        common.write_parquet(
            path,
            existing,
            api_name="anns_d",
            params={"start_date": "20260501", "end_date": "20260528"},
            fields=list(existing.columns),
            source_hash="old",
        )

        self.assertTrue(download.should_skip_existing_partition(
            path,
            force=False,
            requested_params={"start_date": "20260501", "end_date": "20260528"},
        ))
        self.assertFalse(download.should_skip_existing_partition(
            path,
            force=False,
            requested_params={"start_date": "20260501", "end_date": "20260529"},
        ))

    def test_sidecar_coverage_normalizes_date_and_datetime_bounds(self):
        path = self.raw_dir / "major_news" / "src=all" / "month=202605.parquet"
        existing = pd.DataFrame([{"pub_time": "2026-05-29 12:00:00", "title": "old"}])
        common.write_parquet(
            path,
            existing,
            api_name="major_news",
            params={"start_date": "2026-05-29 00:00:00", "end_date": "2026-05-29 23:59:59"},
            fields=list(existing.columns),
            source_hash="old",
        )
        self.assertTrue(download.should_skip_existing_partition(
            path,
            force=False,
            requested_params={"start_date": "20260529000000", "end_date": "20260529235959"},
        ))

        common.write_parquet(
            path,
            existing,
            api_name="major_news",
            params={"start_date": "20260529000000", "end_date": "20260529000000"},
            fields=list(existing.columns),
            source_hash="midnight_only",
        )
        self.assertFalse(download.should_skip_existing_partition(
            path,
            force=False,
            requested_params={"start_date": "20260529", "end_date": "20260529"},
        ))

        common.write_parquet(
            path,
            existing,
            api_name="major_news",
            params={"start_date": "20260529", "end_date": "20260529"},
            fields=list(existing.columns),
            source_hash="whole_day",
        )
        self.assertTrue(download.should_skip_existing_partition(
            path,
            force=False,
            requested_params={"start_date": "2026-05-29 00:00:00", "end_date": "2026-05-29 23:59:59"},
        ))

    def test_macro_month_loop_refreshes_only_current_month_without_coverage_sidecar(self):
        existing = pd.DataFrame([{
            "month": "202604",
            "publish_date": "20260401",
            "title": "old",
            "issuing_org": "old",
            "data_api": "cn_schedule",
        }])
        for month in ("202604", "202605"):
            path = self.raw_dir / "cn_schedule" / f"month={month}.parquet"
            frame = existing.assign(month=month)
            common.write_parquet(
                path,
                frame,
                api_name="cn_schedule",
                params={"m": month},
                fields=list(frame.columns),
                source_hash=month,
            )

        client = CountingMacroClient()
        download.download_macro_month_loop(
            client,
            self.raw_dir,
            common.MACRO_SPECS["cn_schedule"],
            "20260401",
            "20260529",
            False,
        )

        self.assertEqual([params["m"] for _, params in client.calls], ["202605"])
        refreshed_meta = json.loads((self.raw_dir / "cn_schedule" / "month=202605.parquet.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(refreshed_meta["params"]["start_date"], "20260501")
        self.assertEqual(refreshed_meta["params"]["end_date"], "20260529")
        closed_meta = json.loads((self.raw_dir / "cn_schedule" / "month=202604.parquet.meta.json").read_text(encoding="utf-8"))
        self.assertNotIn("end_date", closed_meta["params"])

    def test_window_merged_partition_preserves_rows_outside_refresh_window(self):
        path = self.raw_dir / "repurchase" / "month=202605.parquet"
        existing = pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20260501", "amount": 1},
            {"ts_code": "000002.SZ", "ann_date": "20260515", "amount": 2},
        ])
        common.write_parquet(
            path,
            existing,
            api_name="repurchase",
            params={"start_date": "20260501", "end_date": "20260531"},
            fields=list(existing.columns),
            source_hash="old",
        )
        refreshed = pd.DataFrame([
            {"ts_code": "000002.SZ", "ann_date": "20260515", "amount": 20},
            {"ts_code": "000003.SZ", "ann_date": "20260516", "amount": 30},
        ])

        rows = download.write_window_merged_partition(
            path,
            refreshed,
            api_name="repurchase",
            params={"start_date": "20260510", "end_date": "20260520"},
            fields=list(refreshed.columns),
            source_hash="fresh",
            key_columns=["ts_code", "ann_date"],
            date_columns=["ann_date"],
            start_date="20260510",
            end_date="20260520",
            revision_ledger=str(self.root / "revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
        )

        merged = pd.read_parquet(path).sort_values("ts_code").reset_index(drop=True)
        self.assertEqual(rows, 3)
        self.assertEqual(merged["ts_code"].tolist(), ["000001.SZ", "000002.SZ", "000003.SZ"])
        self.assertEqual(merged["amount"].tolist(), [1, 20, 30])

    def test_macro_range_once_uses_retained_start_during_rolling_update(self):
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            tier="macro",
            start_date="20260504",
            macro_start_date="20200101",
            end_date="20260603",
            datasets=["cn_cpi"],
            force=True,
            page_limit=None,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )
        client = CountingMacroClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_macro(args), 0)

        cpi_calls = [params for api_name, params in client.calls if api_name == "cn_cpi"]
        self.assertEqual(cpi_calls[0]["start_m"], "202001")
        self.assertTrue((self.raw_dir / "cn_cpi" / "range=202001_latest.parquet").exists())

    def test_macro_range_once_prunes_stale_end_suffixed_files(self):
        stale = self.raw_dir / "cn_cpi" / "range=202001_202605.parquet"
        pd.DataFrame([{"month": "202001"}]).pipe(
            lambda df: common.write_parquet(stale, df, api_name="cn_cpi", params={}, fields=list(df.columns), source_hash="old")
        )
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            tier="macro",
            start_date="20260504",
            macro_start_date="20200101",
            end_date="20260603",
            datasets=["cn_cpi"],
            force=True,
            page_limit=None,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )
        client = CountingMacroClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_macro(args), 0)

        remaining = sorted(path.name for path in (self.raw_dir / "cn_cpi").glob("range=*.parquet"))
        self.assertEqual(remaining, ["range=202001_latest.parquet"])
        self.assertFalse(stale.with_suffix(stale.suffix + ".meta.json").exists())

    def test_fundamental_ann_month_windows_pull_full_natural_months(self):
        windows = download.fundamental_ann_month_windows("20200615", "20200705", {"202004"})
        self.assertEqual(windows, [
            ("20200401", "20200430", "202004"),
            ("20200601", "20200630", "202006"),
            ("20200701", "20200705", "202007"),
        ])

    def test_revision_aware_writer_blocks_key_removal_overwrite(self):
        path = self.raw_dir / "forecast_vip" / "ann_month=202001.parquet"
        original = pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20200105", "type": "预增"},
            {"ts_code": "000002.SZ", "ann_date": "20200110", "type": "预减"},
        ])
        common.write_parquet(path, original, api_name="forecast_vip", params={}, fields=list(original.columns), source_hash="old")
        truncated = pd.DataFrame([
            {"ts_code": "000002.SZ", "ann_date": "20200110", "type": "预减"},
            {"ts_code": "000003.SZ", "ann_date": "20200120", "type": "预增"},
        ])
        ledger = self.root / "removal_revision_events.jsonl"

        output = io.StringIO()
        with redirect_stdout(output):
            did_write = common.write_parquet_revision_aware(
                path,
                truncated,
                api_name="forecast_vip",
                params={"start_date": "20200110", "end_date": "20200131"},
                fields=list(truncated.columns),
                source_hash="new",
                key_columns=["ts_code", "ann_date", "type"],
                revision_ledger=ledger,
            )

        self.assertFalse(did_write)
        self.assertIn("skipped_key_removal_overwrite", output.getvalue())
        self.assertTrue(pd.read_parquet(path).equals(original))
        event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["write_action"], "skipped_key_removal_overwrite")
        self.assertEqual(event["removed_keys"], 1)

        with redirect_stdout(io.StringIO()):
            did_write = common.write_parquet_revision_aware(
                path,
                truncated,
                api_name="forecast_vip",
                params={"start_date": "20200110", "end_date": "20200131"},
                fields=list(truncated.columns),
                source_hash="new",
                key_columns=["ts_code", "ann_date", "type"],
                revision_ledger=ledger,
                allow_key_removal_overwrite=True,
            )
        self.assertTrue(did_write)
        self.assertEqual(set(pd.read_parquet(path)["ts_code"]), {"000002.SZ", "000003.SZ"})

    def test_revision_aware_writer_blocks_disproportionate_shrink(self):
        path = self.raw_dir / "repurchase" / "month=202001.parquet"
        original = pd.DataFrame([
            {"ts_code": f"{index:06d}.SZ", "ann_date": "20200105", "amount": 1.0}
            for index in range(120)
        ])
        common.write_parquet(path, original, api_name="repurchase", params={}, fields=list(original.columns), source_hash="old")
        truncated = original.head(10)  # 110 keys removed: >20 keys and >20%
        ledger = self.root / "shrink_revision_events.jsonl"

        output = io.StringIO()
        with redirect_stdout(output):
            did_write = common.write_parquet_revision_aware(
                path,
                truncated,
                api_name="repurchase",
                params={},
                fields=list(truncated.columns),
                source_hash="new",
                key_columns=["ts_code", "ann_date"],
                revision_ledger=ledger,
                allow_key_removal_overwrite=True,
            )
        self.assertFalse(did_write)
        self.assertIn("blocked_shrink_overwrite", output.getvalue())
        self.assertTrue(pd.read_parquet(path).equals(original))
        event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["write_action"], "blocked_shrink_overwrite")

        # A proportionate correction (a few keys) still overwrites.
        small = original.head(115)
        with redirect_stdout(io.StringIO()):
            did_write = common.write_parquet_revision_aware(
                path,
                small,
                api_name="repurchase",
                params={},
                fields=list(small.columns),
                source_hash="new2",
                key_columns=["ts_code", "ann_date"],
                revision_ledger=ledger,
                allow_key_removal_overwrite=True,
            )
        self.assertTrue(did_write)
        self.assertEqual(len(pd.read_parquet(path)), 115)

    def test_repair_text_available_at_refreshes_sidecar_hash(self):
        from autotrade.data_sources.tushare.io import file_sha256

        spec = common.TEXT_SPECS["anns_d"]
        path = self.raw_dir / "anns_d" / "month=202001.parquet"
        frame = pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20200105", "name": "平安银行",
             "title": "t", "url": "u", "rec_time": "2025-06-01 10:00:00"},
        ])
        stamped = common.augment_text_frame(frame.copy(), spec)
        stamped["available_at"] = "2031-01-01T00:00:00+08:00"  # wrong on purpose
        common.write_parquet(path, stamped, api_name="anns_d", params={}, fields=list(stamped.columns), source_hash="old")
        stats = common.repair_text_available_at(str(self.raw_dir), ["anns_d"])
        self.assertEqual(stats["files_rewritten"], 1)
        sidecar = json.loads(path.with_suffix(path.suffix + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["parquet_sha256"], file_sha256(path))

    def test_daily_audit_warns_on_exact_limit_without_pagination_probe(self):
        path = self.raw_dir / "daily" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({
            "trade_date": ["20200102"] * 5000,
            "ts_code": [f"{index:06d}.SZ" for index in range(5000)],
        }).to_parquet(path, index=False)
        findings = []
        audit.audit_trade_date_dataset(self.raw_dir, common.DAILY_SPECS["daily"], {"20200102"}, lambda *item: findings.append(item))
        self.assertEqual(findings[0][0], "warning")
        self.assertEqual(findings[0][3]["exact_common_limit_row_count_dates"], ["20200102"])

    def test_write_raw_generation_publishes_atomic_stamp(self):
        raw = self.root / "genraw"
        cron_update.write_raw_generation(raw)
        first = json.loads((raw / ".raw_generation.json").read_text(encoding="utf-8"))
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["state"], "committed")
        self.assertTrue(first["generation_id"])
        self.assertTrue(first["completed_at"])
        cron_update.write_raw_generation(raw)
        second = json.loads((raw / ".raw_generation.json").read_text(encoding="utf-8"))
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(list((raw).glob(".raw_generation.json.tmp*")), [])

    def test_raw_generation_failed_mutation_is_dirty_and_only_same_job_can_recover(self):
        raw = self.root / "genraw"
        cron_update.write_raw_generation(raw)
        transaction = {
            "job": "cn_evening_full",
            "start_date": "20260601",
            "end_date": "20260630",
            "command_hash": "command-a",
            "config_hash": "config-a",
        }
        active = cron_update.begin_raw_generation_update(raw, transaction)
        cron_update.mark_raw_generation_dirty(raw, active, error="step 2 failed")
        dirty = json.loads((raw / ".raw_generation.json").read_text(encoding="utf-8"))
        self.assertEqual(dirty["state"], "dirty")
        self.assertEqual(dirty["transaction"]["job"], "cn_evening_full")

        with self.assertRaisesRegex(RuntimeError, "rerun the original job"):
            cron_update.begin_raw_generation_update(raw, {**transaction, "job": "another_job"})

        recovered = cron_update.begin_raw_generation_update(raw, transaction)
        committed = cron_update.write_raw_generation(raw, transaction=recovered)
        self.assertEqual(committed["state"], "committed")
        self.assertNotEqual(committed["generation_id"], dirty["generation_id"])

    def test_revision_sentinel_is_not_a_mutating_operation(self):
        self.assertNotIn("revision_sentinel", cron_update.MUTATING_OPERATIONS)

    def test_parquet_availability_survives_identical_refresh_but_moves_on_revision(self):
        path = self.raw_dir / "stk_auction" / "trade_date=20260713.parquet"
        first = pd.DataFrame(
            [{"trade_date": "20260713", "ts_code": "000001.SZ", "price": 10.0}]
        )
        availability = {
            "available_at": "2026-07-13T09:28:36+08:00",
            "rule": "observed:cn_open_auction_capture",
        }
        common.write_parquet(
            path,
            first,
            api_name="stk_auction",
            params={},
            fields=list(first.columns),
            source_hash="first",
            extra_metadata={"availability": availability},
        )
        common.write_parquet(
            path,
            first.copy(),
            api_name="stk_auction",
            params={},
            fields=list(first.columns),
            source_hash="same-payload",
        )
        self.assertEqual(common.parquet_meta(path)["availability"], availability)

        revised = first.assign(price=10.1)
        common.write_parquet(
            path,
            revised,
            api_name="stk_auction",
            params={},
            fields=list(revised.columns),
            source_hash="revision",
        )
        revised_availability = common.parquet_meta(path)["availability"]
        self.assertEqual(revised_availability["rule"], "observed:content_revision_fetch")
        self.assertNotEqual(revised_availability["available_at"], availability["available_at"])

    def test_capture_open_auction_waits_for_stable_complete_frame(self):
        self._write_trade_cal("20260713")
        previous = self.raw_dir / "stk_auction" / "trade_date=20260710.parquet"
        previous.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"x": 1}, {"x": 2}]).to_parquet(previous, index=False)
        fields = common.DAILY_SPECS["stk_auction"].fields.split(",")

        def result(items):
            return common.ApiResult(fields, items, common.stable_hash(items))

        full = [
            ["000001.SZ", "20260713", 1000.0, 10.0, 10000.0, 9.9, 0.1, 1.0, 100000.0],
            ["600000.SH", "20260713", 2000.0, 8.0, 16000.0, 7.9, 0.2, 1.1, 200000.0],
        ]
        responses = [
            (result([]), 1),
            (result(full), 1),
            RuntimeError("transient source error"),
            (result(full), 1),
            (result(full), 1),
        ]
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            trade_date="20260713",
            page_limit=10000,
            max_wait_seconds=10.0,
            retry_delay_seconds=0.0,
            stable_reads=2,
            min_rows=2,
            min_previous_day_ratio=0.98,
            max_previous_day_drop=10,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            min_interval_seconds=0.0,
            timeout_seconds=1.0,
        )
        with (
            patch.object(download, "load_token", return_value="token"),
            patch.object(download, "TuShareClient"),
            patch.object(download, "query_paged", side_effect=responses) as query,
            patch.object(download.time, "sleep", return_value=None),
        ):
            self.assertEqual(download.capture_open_auction(args), 0)

        self.assertEqual(query.call_count, 5)
        target = self.raw_dir / "stk_auction" / "trade_date=20260713.parquet"
        self.assertEqual(len(pd.read_parquet(target)), 2)
        availability = common.parquet_meta(target)["availability"]
        self.assertEqual(availability["rule"], "observed:cn_open_auction_capture")
        self.assertEqual(availability["row_count"], 2)

        # A later strict reconciliation may return the same keyed rows in a
        # different API order. Canonical persistence keeps the first landing.
        reversed_result = result(list(reversed(full)))
        with (
            patch.object(download, "load_token", return_value="token"),
            patch.object(download, "TuShareClient"),
            patch.object(download, "query_paged", side_effect=[(reversed_result, 1), (reversed_result, 1)]),
            patch.object(download.time, "sleep", return_value=None),
        ):
            self.assertEqual(download.capture_open_auction(args), 0)
        self.assertEqual(common.parquet_meta(target)["availability"], availability)
        self.assertEqual(pd.read_parquet(target)["ts_code"].tolist(), ["000001.SZ", "600000.SH"])

    def test_capture_open_auction_timeout_does_not_replace_partition(self):
        self._write_trade_cal("20260713")
        target = self.raw_dir / "stk_auction" / "trade_date=20260713.parquet"
        original = pd.DataFrame(
            [{"trade_date": "20260713", "ts_code": "000001.SZ", "price": 9.9}]
        )
        common.write_parquet(
            target,
            original,
            api_name="stk_auction",
            params={},
            fields=list(original.columns),
            source_hash="old",
        )
        original_hash = common.file_sha256(target)
        fields = common.DAILY_SPECS["stk_auction"].fields.split(",")
        partial = common.ApiResult(
            fields,
            [["000001.SZ", "20260713", 1000.0, 10.0, 10000.0, 9.9, 0.1, 1.0, 100000.0]],
            "partial",
        )
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            trade_date="20260713",
            page_limit=10000,
            max_wait_seconds=0.0,
            retry_delay_seconds=0.0,
            stable_reads=1,
            min_rows=2,
            min_previous_day_ratio=0.98,
            max_previous_day_drop=10,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            min_interval_seconds=0.0,
            timeout_seconds=1.0,
        )
        with (
            patch.object(download, "load_token", return_value="token"),
            patch.object(download, "TuShareClient"),
            patch.object(download, "query_paged", return_value=(partial, 1)),
        ):
            self.assertEqual(
                download.capture_open_auction(args),
                common.NO_MUTATION_RETRY_EXIT_CODE,
            )

        self.assertEqual(common.file_sha256(target), original_hash)

    def test_capture_open_auction_polls_on_fixed_start_times(self):
        self._write_trade_cal("20260713")
        fields = common.DAILY_SPECS["stk_auction"].fields.split(",")
        items = [
            ["000001.SZ", "20260713", 1000.0, 10.0, 10000.0, 9.9, 0.1, 1.0, 100000.0],
        ]
        result = common.ApiResult(fields, items, common.stable_hash(items))
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            trade_date="20260713",
            page_limit=10000,
            max_wait_seconds=30.0,
            retry_delay_seconds=10.0,
            stable_reads=3,
            min_rows=1,
            min_previous_day_ratio=0.98,
            max_previous_day_drop=10,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            min_interval_seconds=0.0,
            timeout_seconds=1.0,
        )

        class Clock:
            def __init__(self):
                self.now = 0.0
                self.query_starts = []
                self.sleeps = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

            def query(self, *_args, **_kwargs):
                self.query_starts.append(self.now)
                self.now += 3.0
                return result, 1

        clock = Clock()
        with (
            patch.object(download, "load_token", return_value="token"),
            patch.object(download, "TuShareClient"),
            patch.object(download, "query_paged", side_effect=clock.query),
            patch.object(download.time, "monotonic", side_effect=clock.monotonic),
            patch.object(download.time, "sleep", side_effect=clock.sleep),
        ):
            self.assertEqual(download.capture_open_auction(args), 0)

        self.assertEqual(clock.query_starts, [0.0, 10.0, 20.0])
        self.assertEqual(clock.sleeps, [7.0, 7.0])

    def test_auction_capture_rejects_duplicate_business_keys(self):
        row = {
            "ts_code": "000001.SZ",
            "trade_date": "20260713",
            "vol": 1000.0,
            "price": 10.0,
            "amount": 10000.0,
            "pre_close": 9.9,
            "turnover_rate": 0.1,
            "volume_ratio": 1.0,
            "float_share": 100000.0,
        }
        errors = download._validate_auction_capture(
            pd.DataFrame([row, row]), "20260713", min_rows=1
        )
        self.assertIn("duplicate_keys=1", errors)

    def test_auction_capture_validates_trade_and_no_trade_quantities(self):
        base = {
            "trade_date": "20260713",
            "pre_close": 9.9,
            "turnover_rate": 0.1,
            "volume_ratio": 1.0,
            "float_share": 100000.0,
        }
        valid = pd.DataFrame([
            {**base, "ts_code": "000001.SZ", "price": 10.0, "vol": 1000.0, "amount": 10000.0},
            # A missing source price is safe when the clearing price can be
            # reconstructed exactly from two positive finite quantities.
            {**base, "ts_code": "000002.SZ", "price": None, "vol": 2000.0, "amount": 16000.0},
            {**base, "ts_code": "000003.SZ", "price": None, "vol": 0.0, "amount": 0.0},
        ])
        self.assertEqual(download._validate_auction_capture(valid, "20260713", min_rows=3), [])

        invalid = pd.DataFrame([
            {**base, "ts_code": "000004.SZ", "price": 10.0, "vol": float("nan"), "amount": 10.0},
            {**base, "ts_code": "000005.SZ", "price": 10.0, "vol": -1.0, "amount": 0.0},
            {**base, "ts_code": "000006.SZ", "price": 10.0, "vol": 1.0, "amount": 0.0},
            {**base, "ts_code": "000007.SZ", "price": 10.0, "vol": 1.0, "amount": float("nan")},
            {**base, "ts_code": "000008.SZ", "price": 10.0, "vol": 1.0, "amount": -1.0},
            {**base, "ts_code": "000009.SZ", "price": None, "vol": 5e-324, "amount": 1e308},
            {**base, "ts_code": "000010.SZ", "price": 10.0, "vol": 0.0, "amount": 0.0},
            {**base, "ts_code": "000011.SZ", "price": 100.0, "vol": 1000.0, "amount": 10000.0},
        ])
        errors = download._validate_auction_capture(invalid, "20260713", min_rows=8)
        self.assertIn("invalid_vol_rows=2", errors)
        self.assertIn("invalid_amount_rows=2", errors)
        self.assertIn("inconsistent_trade_rows=1", errors)
        self.assertIn("unrecoverable_trade_price_rows=1", errors)
        self.assertIn("hidden_no_trade_price_rows=1", errors)
        self.assertIn("inconsistent_trade_price_rows=1", errors)

    def test_auction_capture_row_floor_allows_only_small_day_to_day_drop(self):
        previous = self.raw_dir / "stk_auction" / "trade_date=20260710.parquet"
        previous.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"row": range(5519)}).to_parquet(previous, index=False)

        minimum = download._auction_capture_min_rows(
            self.raw_dir,
            "20260713",
            floor=1000,
            ratio=0.995,
            max_previous_day_drop=10,
        )

        self.assertEqual(minimum, 5509)

    def test_non_trading_auction_job_skips_before_generation_fence(self):
        self._write_trade_cal("20260712", is_open="0")
        config_path = self.root / "auction_schedule.json"
        config_path.write_text(
            json.dumps(
                {
                    "timezone": "Asia/Shanghai",
                    "repo_root": str(self.root),
                    "python": "/env/python",
                    "default_raw_dir": "raw",
                    "default_start_date": "20200101",
                    "jobs": {
                        "auction": {
                            "operation": "auction_capture",
                            "only_if_sse_open_date": True,
                            "start_date_lookback_days": 0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        generation = self.raw_dir / ".raw_generation.json"
        cron_update.write_raw_generation(self.raw_dir)
        before = generation.read_bytes()
        args = argparse.Namespace(
            config=str(config_path),
            job="auction",
            start_date=None,
            end_date="20260712",
            dry_run=False,
            force_run=False,
        )
        written_state = []
        with (
            patch.object(cron_update, "parse_args", return_value=args),
            patch.object(cron_update.os, "chdir"),
            patch.object(cron_update, "read_state", return_value={}),
            patch.object(cron_update, "write_state", side_effect=written_state.append),
            patch.object(cron_update, "append_dispatch"),
            patch.object(cron_update, "acquire_lock", side_effect=AssertionError("must not lock")),
        ):
            self.assertEqual(cron_update.main(), 0)

        self.assertEqual(generation.read_bytes(), before)
        self.assertTrue(written_state[0]["auction"]["skipped_non_trading_day"])

    def test_same_day_open_check_fails_when_calendar_does_not_cover_target(self):
        self._write_trade_cal("20260712", is_open="0")

        with self.assertRaisesRegex(RuntimeError, "does not cover target date 20260713"):
            cron_update.is_sse_open_date(self.root, "raw", "20260713")

    def test_not_ready_auction_job_restores_committed_generation(self):
        self._write_trade_cal("20260713", is_open="1")
        config_path = self.root / "auction_schedule.json"
        config_path.write_text(
            json.dumps(
                {
                    "timezone": "Asia/Shanghai",
                    "repo_root": str(self.root),
                    "python": "/env/python",
                    "default_raw_dir": "raw",
                    "default_start_date": "20200101",
                    "jobs": {
                        "auction": {
                            "operation": "auction_capture",
                            "only_if_sse_open_date": True,
                            "start_date_lookback_days": 0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        generation = self.raw_dir / ".raw_generation.json"
        cron_update.write_raw_generation(self.raw_dir)
        before = json.loads(generation.read_text(encoding="utf-8"))
        args = argparse.Namespace(
            config=str(config_path),
            job="auction",
            start_date=None,
            end_date="20260713",
            dry_run=False,
            force_run=False,
        )

        class FakeLock:
            fd = 7

            def release(self):
                return None

        written_state = []
        with (
            patch.object(cron_update, "parse_args", return_value=args),
            patch.object(cron_update.os, "chdir"),
            patch.object(cron_update, "read_state", return_value={}),
            patch.object(cron_update, "write_state", side_effect=written_state.append),
            patch.object(cron_update, "append_dispatch"),
            patch.object(cron_update, "acquire_lock", return_value=FakeLock()),
            patch.object(
                cron_update,
                "run_update",
                return_value=common.NO_MUTATION_RETRY_EXIT_CODE,
            ),
        ):
            self.assertEqual(cron_update.main(), common.NO_MUTATION_RETRY_EXIT_CODE)

        self.assertEqual(json.loads(generation.read_text(encoding="utf-8")), before)
        self.assertEqual(written_state[-1]["auction"]["status"], "not_ready")

    def test_auction_cron_command_overrides_global_request_timeout(self):
        ctx = cron_update.RunContext(
            config={
                "default_raw_dir": "raw",
                "default_update_args": ["--timeout-seconds", "120"],
            },
            repo_root=self.root,
            python="/env/python",
            job_name="auction",
            job={
                "operation": "auction_capture",
                "extra_args": ["--timeout-seconds", "15"],
            },
            start_date="20260713",
            end_date="20260713",
            timezone_name="Asia/Shanghai",
        )

        command = cron_update.build_job_commands(ctx)[0]
        timeout_positions = [i for i, value in enumerate(command) if value == "--timeout-seconds"]
        self.assertEqual(command[timeout_positions[-1] + 1], "15")

    def test_cron_job_hash_ignores_unrelated_job_edits(self):
        selected_job = {"operation": "auction_capture", "skip_if_already_ok": True}
        base_config = {
            "schema_version": 1,
            "timezone": "Asia/Shanghai",
            "repo_root": str(self.root),
            "python": "/env/python",
            "default_start_date": "20200101",
            "default_raw_dir": "raw",
            "default_update_args": ["--timeout-seconds", "15"],
            "jobs": {"auction": selected_job, "unrelated": {"extra_args": ["old"]}},
        }
        edited_config = {
            **base_config,
            "jobs": {"auction": selected_job, "unrelated": {"extra_args": ["new"]}},
        }
        contexts = [
            cron_update.RunContext(
                config=config,
                repo_root=self.root,
                python="/env/python",
                job_name="auction",
                job=selected_job,
                start_date="20260713",
                end_date="20260713",
                timezone_name="Asia/Shanghai",
            )
            for config in (base_config, edited_config)
        ]
        first_hash, edited_hash = map(cron_update.job_config_hash, contexts)
        self.assertEqual(first_hash, edited_hash)
        payload = {"command_hash": "same-command", "config_hash": edited_hash}
        state = {
            "start_date": "20260713",
            "end_date": "20260713",
            "status": "ok",
            "command_hash": "same-command",
            "config_hash": first_hash,
        }
        args = argparse.Namespace(force_run=False)
        self.assertTrue(cron_update.should_skip_completed(contexts[1], args, state, payload))

    def test_cron_full_audit_builds_all_formal_status_commands(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_full_audit",
            job={"operation": "audit_full", "event_flow_end_extra_offset_days": 1, "text_end_extra_offset_days": 1},
            start_date="20200101",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 6)
        command_text = [" ".join(command) for command in commands]
        self.assertIn("scripts/data/tushare_audit.py base", command_text[0])
        self.assertIn("--include-limit-list", command_text[0])
        self.assertIn("scripts/data/tushare_audit.py macro", command_text[1])
        self.assertIn("scripts/data/tushare_audit.py intraday-by-date", command_text[2])
        self.assertIn("--expected-codes-source minute", command_text[2])
        self.assertIn("scripts/data/tushare_audit.py event-flow", command_text[3])
        self.assertIn("--end-date 20260531", command_text[3])
        self.assertIn("scripts/data/tushare_audit.py board-trading", command_text[4])
        self.assertIn("--include-text", command_text[5])
        self.assertIn("--text-end-date 20260531", command_text[5])
        self.assertTrue(all("--start-date 20200101" in text for text in command_text))
        self.assertTrue(all("--raw-dir raw" in text for text in command_text))

    def test_cron_full_audit_can_use_open_date_for_event_flow(self):
        trade_cal = self.raw_dir / "trade_cal" / "exchange=SSE" / "year=2026.parquet"
        trade_cal.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {"cal_date": "20260618", "is_open": "1"},
                {"cal_date": "20260619", "is_open": "0"},
                {"cal_date": "20260620", "is_open": "0"},
                {"cal_date": "20260621", "is_open": "0"},
            ]
        ).to_parquet(trade_cal, index=False)
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_full_audit",
            job={"operation": "audit_full", "event_flow_end_date_mode": "sse_open_on_or_before"},
            start_date="20200101",
            end_date="20260621",
            timezone_name="Asia/Shanghai",
        )

        command_text = [" ".join(command) for command in cron_update.build_job_commands(ctx)]

        self.assertIn("scripts/data/tushare_audit.py event-flow", command_text[3])
        self.assertIn("--end-date 20260618", command_text[3])
        self.assertIn("--text-end-date 20260621", command_text[5])

    def test_cron_update_job_can_use_rolling_start_lookback(self):
        config_path = self.root / "schedule.json"
        trade_cal = self.raw_dir / "trade_cal" / "exchange=SSE" / "year=2026.parquet"
        trade_cal.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {"cal_date": "20260618", "is_open": "1"},
                {"cal_date": "20260619", "is_open": "0"},
                {"cal_date": "20260620", "is_open": "0"},
                {"cal_date": "20260621", "is_open": "0"},
            ]
        ).to_parquet(trade_cal, index=False)
        config_path.write_text(json.dumps({
            "timezone": "Asia/Shanghai",
            "repo_root": str(self.root),
            "python": "/env/python",
            "default_raw_dir": "raw",
            "default_start_date": "20200101",
            "jobs": {
                "cn_evening_full": {
                    "start_date_lookback_days": 30,
                    "extra_args": ["--refresh-daily-datasets", "daily", "adj_factor"],
                },
                "cn_preopen_margin_backfill_0905": {
                    "operation": "download_event_flow",
                    "end_date_offset_days": 1,
                    "end_date_mode": "sse_open_on_or_before",
                },
            },
        }), encoding="utf-8")

        args = argparse.Namespace(config=str(config_path), job="cn_evening_full", start_date=None, end_date="20260601", dry_run=False, force_run=False)
        ctx = cron_update.build_context(args)
        self.assertEqual(ctx.start_date, "20260502")
        self.assertEqual(ctx.end_date, "20260601")
        command = " ".join(cron_update.build_job_commands(ctx)[0])
        self.assertIn("--refresh-daily-datasets daily adj_factor", command)

        margin_args = argparse.Namespace(config=str(config_path), job="cn_preopen_margin_backfill_0905", start_date=None, end_date="20260621", dry_run=False, force_run=False)
        margin_ctx = cron_update.build_context(margin_args)
        self.assertEqual(margin_ctx.start_date, "20260618")
        self.assertEqual(margin_ctx.end_date, "20260618")

    def test_cron_pit_event_job_aligns_rolling_start_to_month(self):
        config_path = self.root / "schedule.json"
        existing = self.root / "pit" / "fundamental_events" / "income_vip"
        existing.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"dataset": "income_vip", "available_at": "2026-01-01T00:00:00+08:00"}]).to_parquet(
            existing / "available_month=202601.parquet",
            index=False,
        )
        config_path.write_text(json.dumps({
            "timezone": "Asia/Shanghai",
            "repo_root": str(self.root),
            "python": "/env/python",
            "default_start_date": "20200101",
            "default_raw_dir": "raw",
            "default_pit_root": "pit",
            "jobs": {
                "cn_nightly_pit_event_build": {
                    "operation": "pit_event_pipeline",
                    "start_date_lookback_days": 120,
                    "fundamental_events_root": "pit/fundamental_events",
                },
            },
        }), encoding="utf-8")

        args = argparse.Namespace(
            config=str(config_path),
            job="cn_nightly_pit_event_build",
            start_date=None,
            end_date="20260621",
            dry_run=False,
            force_run=False,
        )
        ctx = cron_update.build_context(args)
        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(ctx.start_date, "20260221")
        self.assertIn("--start-date 20260201", " ".join(commands[0]))
        self.assertIn("--start-date 20260201", " ".join(commands[1]))

    def test_cron_download_tier_job_builds_targeted_command(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw", "default_update_args": ["--min-interval-seconds", "0.22"]},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_preopen_board_backfill_0850",
            job={"operation": "download_tier", "tier": "board_trading", "extra_args": ["--datasets", "kpl_list", "--force"]},
            start_date="20260601",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 1)
        text = " ".join(commands[0])
        self.assertIn("scripts/data/tushare_download.py download --tier board_trading", text)
        self.assertIn("--start-date 20260601 --end-date 20260601", text)
        self.assertIn("--raw-dir raw", text)
        self.assertIn("--datasets kpl_list --force", text)

    def test_cron_revision_sentinel_job_builds_audit_command(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw", "default_update_args": ["--min-interval-seconds", "0.22"]},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_daily_revision_sentinel",
            job={"operation": "revision_sentinel", "extra_args": ["--sample-size", "12", "--datasets", "adj_factor"]},
            start_date="20200101",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 1)
        text = " ".join(commands[0])
        self.assertIn("scripts/data/tushare_audit.py revision-sentinel", text)
        self.assertIn("--start-date 20200101 --end-date 20260601", text)
        self.assertIn("--raw-dir raw", text)
        self.assertIn("--sample-size 12 --datasets adj_factor", text)

    def test_cron_event_flow_audit_job_builds_targeted_status_refresh(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_preopen_event_flow_audit_0920",
            job={"operation": "audit_event_flow"},
            start_date="20200101",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 1)
        text = " ".join(commands[0])
        self.assertIn("scripts/data/tushare_audit.py event-flow", text)
        self.assertIn("--start-date 20200101 --end-date 20260601", text)
        self.assertIn("--raw-dir raw", text)

    def test_cron_pit_event_pipeline_builds_and_audits_fundamental_events(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw", "default_pit_root": "pit"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_pit_event_build",
            job={
                "operation": "pit_event_pipeline",
                "fundamental_events_root": "pit/fundamental_events",
                "fundamental_events_status": "results/data_quality/fundamental_events_status.json",
            },
            start_date="20260201",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 2)
        self.assertIn("scripts/data/build_pit_events.py build-fundamental-events", " ".join(commands[0]))
        self.assertIn("--raw-dir raw --output-root pit/fundamental_events", " ".join(commands[0]))
        self.assertIn("scripts/data/build_pit_events.py audit-fundamental-events", " ".join(commands[1]))
        self.assertIn("--events-root pit/fundamental_events", " ".join(commands[1]))
        self.assertIn("--require-partitions", " ".join(commands[1]))

    def test_cron_pit_event_pipeline_initializes_missing_event_layer_from_default_start(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw", "default_pit_root": "pit", "default_start_date": "20200101"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_pit_event_build",
            job={"operation": "pit_event_pipeline", "start_date_lookback_days": 120},
            start_date="20260201",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertIn("--start-date 20200101 --end-date 20260601", " ".join(commands[0]))
        self.assertIn("--start-date 20200101 --end-date 20260601", " ".join(commands[1]))

    def test_cron_pit_event_pipeline_uses_rolling_window_after_event_layer_exists(self):
        partition = self.root / "pit" / "fundamental_events" / "dividend" / "available_month=202605.parquet"
        partition.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"ts_code": "000001.SZ"}]).to_parquet(partition, index=False)
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw", "default_pit_root": "pit", "default_start_date": "20200101"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_pit_event_build",
            job={"operation": "pit_event_pipeline", "start_date_lookback_days": 120},
            start_date="20260201",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertIn("--start-date 20260201 --end-date 20260601", " ".join(commands[0]))

    def test_cron_lock_blocks_while_held_and_releases_on_exit(self):
        # flock is per open-file-description: a second acquire in the same
        # process must block exactly like a second process would.
        runtime = self.root / ".runtime" / "tushare"
        with patch.object(cron_update, "RUNTIME_ROOT", runtime):
            held = cron_update.acquire_lock("tushare_update", wait_seconds=0)
            try:
                with self.assertRaisesRegex(RuntimeError, "lock is held"):
                    cron_update.acquire_lock("tushare_update", wait_seconds=0)
            finally:
                held.release()
            reacquired = cron_update.acquire_lock("tushare_update", wait_seconds=0)
            reacquired.release()

    def test_cron_lock_file_from_dead_process_never_blocks(self):
        # A leftover lock FILE (crash, kill -9, PID reuse) carries no kernel
        # flock, so the next run acquires immediately - no stale-lock heuristics.
        runtime = self.root / ".runtime" / "tushare"
        lock_file = runtime / "locks" / "tushare_update.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("pid=999999999\nstarted_at=2020-01-01T00:00:00+00:00\n", encoding="utf-8")

        with patch.object(cron_update, "RUNTIME_ROOT", runtime):
            acquired = cron_update.acquire_lock("tushare_update", wait_seconds=0)
            self.assertTrue(acquired.path.exists())
            self.assertIn(f"pid={cron_update.os.getpid()}", acquired.path.read_text(encoding="utf-8"))
            acquired.release()

    def test_cron_multi_command_jobs_fail_fast(self):
        ctx = cron_update.RunContext(
            config={},
            repo_root=self.root,
            python="/env/python",
            job_name="unit_fail_fast",
            job={"fail_fast": True},
            start_date="20200101",
            end_date="20200102",
            timezone_name="Asia/Shanghai",
        )
        commands = [["cmd1"], ["cmd2"], ["cmd3"]]
        calls = []

        class Result:
            def __init__(self, returncode):
                self.returncode = returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            return Result(1 if command == ["cmd2"] else 0)

        with patch.object(cron_update, "run_probe"), patch.object(cron_update.subprocess, "run", side_effect=fake_run):
            code = cron_update.run_update(ctx, commands, self.root / "cron.log")

        self.assertEqual(code, 1)
        self.assertEqual(calls, [["cmd1"], ["cmd2"]])

    def test_cron_multi_command_jobs_can_continue_after_error(self):
        ctx = cron_update.RunContext(
            config={},
            repo_root=self.root,
            python="/env/python",
            job_name="unit_continue",
            job={"fail_fast": False},
            start_date="20200101",
            end_date="20200102",
            timezone_name="Asia/Shanghai",
        )
        commands = [["cmd1"], ["cmd2"], ["cmd3"]]
        calls = []

        class Result:
            def __init__(self, returncode):
                self.returncode = returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            return Result(1 if command == ["cmd2"] else 0)

        with patch.object(cron_update, "run_probe"), patch.object(cron_update.subprocess, "run", side_effect=fake_run):
            code = cron_update.run_update(ctx, commands, self.root / "cron_continue.log")

        self.assertEqual(code, 1)
        self.assertEqual(calls, commands)

    def test_reference_refresh_datasets_force_selected_tables(self):
        self._write_trade_cal("20200102")
        for status in ("L", "D", "P"):
            path = self.raw_dir / "stock_basic" / f"list_status={status}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{"ts_code": "999999.SZ", "list_status": status}]).to_parquet(path, index=False)
        for exchange in ("SSE", "SZSE", "BSE"):
            path = self.raw_dir / "stock_company" / f"exchange={exchange}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{"ts_code": "999999.SZ", "exchange": exchange}]).to_parquet(path, index=False)
        namechange = self.raw_dir / "namechange" / "namechange.parquet"
        namechange.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"ts_code": "999999.SZ", "name": "old"}]).to_parquet(namechange, index=False)
        classify = self.raw_dir / "index_classify" / "src=SW2021.parquet"
        classify.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"index_code": "801999.SI", "level": "L1"}]).to_parquet(classify, index=False)
        member = self.raw_dir / "index_member_all" / "l1_code=801999.SI.parquet"
        member.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"l1_code": "801999.SI", "ts_code": "999999.SZ"}]).to_parquet(member, index=False)

        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            bak_start_date="20200102",
            skip_bak_basic=True,
            force=False,
            refresh_reference_datasets=["stock_basic", "stock_company", "namechange", "index_classify", "index_member_all"],
            min_interval_seconds=0,
            timeout_seconds=1,
        )
        client = ReferenceClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_reference(args), 0)

        called_apis = [api_name for api_name, _ in client.calls]
        self.assertEqual(called_apis.count("stock_basic"), 3)
        self.assertEqual(called_apis.count("stock_company"), 3)
        self.assertIn("namechange", called_apis)
        self.assertIn("index_classify", called_apis)
        self.assertIn("index_member_all", called_apis)
        # New reference statics download on first run (files absent).
        for api in ("ths_index", "ths_member", "index_basic", "hs_const", "index_weight"):
            self.assertIn(api, called_apis)

    def test_reference_refresh_does_not_overwrite_existing_stock_company_on_empty_response(self):
        self._write_trade_cal("20200102")
        for status in ("L", "D", "P"):
            stock_path = self.raw_dir / "stock_basic" / f"list_status={status}.parquet"
            stock_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{"ts_code": "000001.SZ", "list_status": status}]).to_parquet(stock_path, index=False)
        path = self.raw_dir / "stock_company" / "exchange=SSE.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = pd.DataFrame([{"ts_code": "000001.SH", "exchange": "SSE", "com_name": "old"}])
        original.to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            bak_start_date="20200102",
            skip_bak_basic=True,
            force=False,
            refresh_reference_datasets=["stock_company"],
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=EmptyReferenceClient()):
            with self.assertRaisesRegex(RuntimeError, "required reference partition"):
                download.download_reference(args)
        self.assertTrue(pd.read_parquet(path).equals(original))

    def test_trade_cal_force_refresh_merges_without_shrinking_year_partition(self):
        path = self.raw_dir / "trade_cal" / "exchange=SSE" / "year=2026.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {"exchange": "SSE", "cal_date": "20260603", "is_open": "1", "pretrade_date": "20260602"},
        ]).to_parquet(path, index=False)
        client = TradeCalClient()

        open_dates = download.download_trade_cal(client, self.raw_dir, "20260604", "20260604", force=True)

        refreshed = pd.read_parquet(path)
        self.assertEqual(sorted(refreshed["cal_date"].astype(str).tolist()), ["20260603", "20260604"])
        self.assertIn("20260604", open_dates)

    def test_update_parser_force_refreshes_stock_company_by_default(self):
        argv = [
            "download.py",
            "update",
            "--start-date",
            "20260601",
            "--end-date",
            "20260601",
        ]
        with patch.object(sys, "argv", argv):
            args = download.parse_args()

        self.assertIn("stock_basic", args.refresh_reference_datasets)
        self.assertIn("stock_company", args.refresh_reference_datasets)
        self.assertIn("namechange", args.refresh_reference_datasets)
        self.assertIn("index_classify", args.refresh_reference_datasets)
        self.assertIn("index_member_all", args.refresh_reference_datasets)
        self.assertEqual(args.refresh_daily_datasets, [])
        self.assertEqual(args.macro_start_date, "20200101")

    def test_update_all_dimensions_uses_retained_start_for_macro_context(self):
        args = argparse.Namespace(
            start_date="20260504",
            end_date="20260603",
            macro_start_date="20200101",
            bak_start_date=None,
            force=False,
            refresh_open_window=True,
            trade_cal_lookahead_days=7,
            raw_dir=str(self.raw_dir),
            page_limit=None,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
            reference_min_interval_seconds=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            refresh_reference_datasets=[],
            daily_datasets=None,
            include_limit_list=True,
            refresh_daily_datasets=[],
            macro_datasets=["cn_gdp"],
            global_datasets=["index_global"],
            event_datasets=[],
            include_board_trading=False,
            include_intraday=False,
            include_share_float_complete=False,
            text_datasets=[],
            fundamental_datasets=[],
            fundamental_refresh_period_count=0,
            fundamental_refresh_ann_month_count=0,
            fundamental_refresh_ts_code_datasets=[],
            fundamental_refresh_event_days=0,
            fundamental_dividend_probe_days=0,
        )
        seen = {}

        def capture(label, _fn, step_args, summary):
            seen[label] = step_args
            summary.append({"step": label, "exit_code": 0})

        with patch.object(download, "run_update_step", side_effect=capture):
            summary = []
            download.update_all_dimensions(args, summary)

        self.assertEqual(seen["daily"].start_date, "20260504")
        self.assertEqual(seen["event_flow"].start_date, "20260504")
        self.assertEqual(seen["macro"].start_date, "20260504")
        self.assertEqual(seen["macro"].macro_start_date, "20200101")
        self.assertEqual(seen["global"].start_date, "20260504")
        self.assertEqual(seen["global"].macro_start_date, "20200101")

    def test_daily_refresh_datasets_force_only_selected_trade_date_dataset(self):
        self._write_trade_cal("20200102")
        for dataset in ("daily", "adj_factor"):
            path = self.raw_dir / dataset / "trade_date=20200102.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{"trade_date": "20200102", "ts_code": "999999.SZ"}]).to_parquet(path, index=False)

        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["daily", "adj_factor"],
            include_limit_list=False,
            refresh_daily_datasets=["adj_factor"],
            revision_ledger=str(self.root / "revision_events.jsonl"),
            force=False,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
        )
        client = DailyMarketClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(download.download_daily(args), 0)

        self.assertEqual([api_name for api_name, _ in client.calls], ["adj_factor"])
        self.assertIn("REVISION_ALERT", output.getvalue())
        self.assertIn('"api_name": "adj_factor"', output.getvalue())
        self.assertIn('"removed_keys": 1', output.getvalue())
        ledger_lines = (self.root / "revision_events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger_lines), 1)
        ledger_event = json.loads(ledger_lines[0])
        self.assertEqual(ledger_event["downstream_status"], "pending_review")
        self.assertEqual(ledger_event["write_action"], "overwrite")
        self.assertIn("new_source_hash", ledger_event)

    def test_fundamental_update_refreshes_recent_periods_and_affected_ts_code_snapshots(self):
        stock_basic = self.raw_dir / "stock_basic" / "list_status=L.parquet"
        stock_basic.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"ts_code": "000001.SZ"}]).to_parquet(stock_basic, index=False)
        for dataset in ("dividend", "fina_audit", "fina_mainbz_vip"):
            path = self.raw_dir / dataset / "ts_code=000001.SZ.parquet"
            common.write_parquet(
                path,
                pd.DataFrame([{"ts_code": "000001.SZ", "ann_date": "20190101", "end_date": "20181231"}]),
                api_name=dataset,
                params={"ts_code": "000001.SZ"},
                fields=["ts_code", "ann_date", "end_date"],
                source_hash="old",
            )
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200601",
            end_date="20200603",
            datasets=["dividend", "fina_audit", "fina_mainbz_vip", "income_vip"],
            force=False,
            page_limit=None,
            max_codes=None,
            fundamental_refresh_period_count=2,
            fundamental_refresh_ann_month_count=0,
            fundamental_refresh_ts_code_datasets=["dividend", "fina_audit", "fina_mainbz_vip"],
            fundamental_dividend_probe_days=0,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        client = FundamentalClient()
        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_fundamental(args), 0)

        calls = [(api, params) for api, params in client.calls if params.get("offset") == 0]
        income_periods = [params["period"] for api, params in calls if api == "income_vip"]
        self.assertEqual(income_periods, ["20191231", "20200331"])
        refreshed_ts_code_calls = [(api, params.get("ts_code")) for api, params in calls if api in {"dividend", "fina_audit", "fina_mainbz_vip"}]
        self.assertEqual(refreshed_ts_code_calls, [("dividend", "000001.SZ"), ("fina_audit", "000001.SZ"), ("fina_mainbz_vip", "000001.SZ")])

    def test_fundamental_download_uses_explicit_codes_for_ts_code_datasets(self):
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200601",
            end_date="20200603",
            datasets=["dividend", "fina_audit", "fina_mainbz_vip"],
            force=True,
            page_limit=None,
            max_codes=None,
            codes=["920126.BJ"],
            fundamental_refresh_period_count=0,
            fundamental_refresh_ann_month_count=0,
            fundamental_refresh_ts_code_datasets=[],
            fundamental_refresh_event_days=0,
            fundamental_dividend_probe_days=0,
            min_interval_seconds=0,
            timeout_seconds=1,
            revision_ledger=None,
            allow_empty_revision_overwrite=False,
        )
        client = FundamentalClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_fundamental(args), 0)

        calls = [(api, params.get("ts_code")) for api, params in client.calls if params.get("offset") == 0]
        self.assertEqual(calls, [("dividend", "920126.BJ"), ("fina_audit", "920126.BJ"), ("fina_mainbz_vip", "920126.BJ")])

    def test_dividend_probe_uses_only_supported_date_params(self):
        client = FundamentalClient()

        codes = download.probe_recent_dividend_codes(client, "20200603", 1, page_limit=1000)

        self.assertEqual(codes, {"000001.SZ"})
        probe_params = [
            set(params) - {"limit", "offset"}
            for api_name, params in client.calls
            if api_name == "dividend"
        ]
        self.assertEqual(probe_params, [{"ann_date"}, {"imp_ann_date"}, {"ex_date"}, {"record_date"}])
        self.assertNotIn("pay_date", {key for params in probe_params for key in params})

    def test_event_flow_refreshes_trade_cal_before_same_day_margin_secs(self):
        self._write_trade_cal("20260603")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20260604",
            end_date="20260604",
            datasets=["margin_secs"],
            force=True,
            page_limit=None,
            revision_ledger=str(self.root / "revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )
        client = CalendarEventClient()

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=client):
            self.assertEqual(download.download_event_flow(args), 0)

        self.assertTrue((self.raw_dir / "margin_secs" / "trade_date=20260604.parquet").exists())
        self.assertIn(("trade_cal", {"exchange": "SSE", "start_date": "20260604", "end_date": "20260604"}), client.calls)
        margin_calls = [params for api_name, params in client.calls if api_name == "margin_secs"]
        self.assertEqual(margin_calls[0]["trade_date"], "20260604")

    def test_recent_fundamental_event_codes_filters_period_rows_by_visible_date(self):
        period_path = self.raw_dir / "income_vip" / "period=20200331.parquet"
        period_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {"ts_code": "000001.SZ", "ann_date": "20200430", "end_date": "20200331"},
            {"ts_code": "000002.SZ", "ann_date": "20190430", "end_date": "20190331"},
        ]).to_parquet(period_path, index=False)

        codes = download.recent_fundamental_event_codes(
            self.raw_dir,
            {"20200331"},
            set(),
            ["income_vip"],
            [],
            "20200401",
            "20200603",
        )

        self.assertEqual(codes, {"000001.SZ"})

    def test_revision_sentinel_compares_without_overwriting_raw(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "adj_factor" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = pd.DataFrame([{"trade_date": "20200102", "ts_code": "999999.SZ", "adj_factor": 9.9}])
        original.to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["adj_factor"],
            sample_size=0,
            seed=None,
            page_limit=10000,
            revision_ledger=str(self.root / "sentinel_events.jsonl"),
            output=str(self.root / "sentinel_summary.json"),
            fail_on_revision=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(audit, "load_token", return_value="token"), patch.object(audit, "TuShareClient", return_value=DailyMarketClient()):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(audit.audit_revision_sentinel(args), 0)

        self.assertIn("REVISION_ALERT", output.getvalue())
        self.assertTrue((self.root / "sentinel_events.jsonl").exists())
        summary = json.loads((self.root / "sentinel_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "warning")
        self.assertEqual(summary["revision_events"], 1)
        self.assertTrue(pd.read_parquet(path).equals(original))

    def test_revision_history_sample_reports_numeric_deltas_without_overwriting_raw(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "adj_factor" / "trade_date=20200102.parquet"
        original = pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "adj_factor": 9.9}])
        common.write_parquet(path, original, api_name="adj_factor", params={}, fields=list(original.columns), source_hash="old")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            sample_per_year=0,
            seed=None,
            groups=["daily"],
            daily_datasets=["adj_factor"],
            event_datasets=None,
            board_datasets=None,
            include_bak_basic=False,
            page_limit=10000,
            events_output=str(self.root / "history_events.jsonl"),
            output=str(self.root / "history_summary.json"),
            fail_on_error=False,
            kpl_tag=[],
            ths_limit_type=[],
            ths_hot_market=[],
            dc_hot_market=[],
            dc_hot_type=[],
            hot_is_new=[],
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(audit, "load_token", return_value="token"), patch.object(audit, "TuShareClient", return_value=DailyMarketClient()):
            self.assertEqual(audit.audit_revision_history_sample(args), 0)

        summary = json.loads((self.root / "history_summary.json").read_text(encoding="utf-8"))
        adj = next(item for item in summary["datasets"] if item["dataset"] == "adj_factor")
        self.assertEqual(summary["status"], "warning")
        self.assertEqual(adj["revision_partitions"], 1)
        self.assertEqual(adj["changed_columns"], {"adj_factor": 1})
        self.assertAlmostEqual(adj["numeric_deltas"]["adj_factor"]["max_abs_delta"], 8.9)
        self.assertTrue(pd.read_parquet(path).equals(original))

    def test_revision_comparison_flags_missing_and_duplicate_keys(self):
        missing = common.build_revision_event(
            dataset="daily",
            partition="trade_date=20200102",
            path=self.raw_dir / "daily" / "trade_date=20200102.parquet",
            old_df=pd.DataFrame([{"trade_date": "20200102", "open": 1.0}]),
            new_df=pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "open": 1.0}]),
            key_columns=["trade_date", "ts_code"],
            source="unit",
        )
        self.assertIsNotNone(missing)
        self.assertEqual(missing["comparison_issue"], "missing_key_columns")
        self.assertEqual(missing["missing_key_columns_old"], ["ts_code"])

        duplicate = common.build_revision_event(
            dataset="daily",
            partition="trade_date=20200102",
            path=self.raw_dir / "daily" / "trade_date=20200102.parquet",
            old_df=pd.DataFrame([
                {"trade_date": "20200102", "ts_code": "000001.SZ", "open": 1.0},
                {"trade_date": "20200102", "ts_code": "000001.SZ", "open": 1.0},
            ]),
            new_df=pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "open": 1.0}]),
            key_columns=["trade_date", "ts_code"],
            source="unit",
        )
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate["comparison_issue"], "duplicate_key_rows")
        self.assertEqual(duplicate["duplicate_key_rows_old"], 1)

    def test_revision_comparison_canonicalizes_numeric_values(self):
        old_df = pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "adj_factor": 1}])
        new_df = pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "adj_factor": 1.0}])
        event = common.build_revision_event(
            dataset="adj_factor",
            partition="trade_date=20200102",
            path=self.raw_dir / "adj_factor" / "trade_date=20200102.parquet",
            old_df=old_df,
            new_df=new_df,
            key_columns=["trade_date", "ts_code"],
            source="unit",
        )
        self.assertIsNone(event)

    def test_revision_event_records_changed_columns_and_row_samples(self):
        old_df = pd.DataFrame([
            {"trade_date": "20200102", "ts_code": "000001.SZ", "close": 10.0, "amount": 100.0},
            {"trade_date": "20200102", "ts_code": "000002.SZ", "close": 20.0, "amount": 200.0},
            {"trade_date": "20200102", "ts_code": "000003.SZ", "close": 30.0, "amount": 300.0},
        ])
        new_df = pd.DataFrame([
            {"trade_date": "20200102", "ts_code": "000001.SZ", "close": 10.5, "amount": 100.0},
            {"trade_date": "20200102", "ts_code": "000002.SZ", "close": 20.0, "amount": 201.5},
            {"trade_date": "20200102", "ts_code": "000004.SZ", "close": 40.0, "amount": 400.0},
        ])

        event = common.build_revision_event(
            dataset="daily",
            partition="trade_date=20200102",
            path=self.raw_dir / "daily" / "trade_date=20200102.parquet",
            old_df=old_df,
            new_df=new_df,
            key_columns=["trade_date", "ts_code"],
            source="unit",
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["changed_keys"], 2)
        self.assertEqual(event["added_keys"], 1)
        self.assertEqual(event["removed_keys"], 1)
        self.assertEqual(event["changed_columns"], {"amount": 1, "close": 1})
        self.assertEqual(event["changed_columns_sample"][0]["key"], ["20200102", "000001.SZ"])
        self.assertEqual(event["changed_columns_sample"][0]["changes"], [{"column": "close", "old": "10", "new": "10.5"}])
        self.assertEqual(event["added_rows_sample"][0]["key"], ["20200102", "000004.SZ"])
        self.assertEqual(event["removed_rows_sample"][0]["key"], ["20200102", "000003.SZ"])

    def test_zero_ok_force_refresh_does_not_overwrite_existing_nonempty_partition(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "limit_list_d" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "limit": "U"}])
        original.to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["limit_list_d"],
            include_limit_list=True,
            refresh_daily_datasets=["limit_list_d"],
            revision_ledger=str(self.root / "zero_ok_revision_events.jsonl"),
            allow_empty_revision_overwrite=False,
            force=False,
            page_limit=10000,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=EmptyTradeDateClient()):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(download.download_daily(args), 0)

        self.assertIn("skipped_empty_revision_overwrite", output.getvalue())
        self.assertTrue(pd.read_parquet(path).equals(original))
        ledger = (self.root / "zero_ok_revision_events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(json.loads(ledger[0])["removed_keys"], 1)

    def test_revision_aware_writer_empty_guard_does_not_depend_on_ledger(self):
        path = self.raw_dir / "limit_list_d" / "trade_date=20200102.parquet"
        original = pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "limit": "U"}])
        common.write_parquet(path, original, api_name="limit_list_d", params={}, fields=list(original.columns), source_hash="old")

        did_write = common.write_parquet_revision_aware(
            path,
            pd.DataFrame(columns=list(original.columns)),
            api_name="limit_list_d",
            params={"trade_date": "20200102"},
            fields=list(original.columns),
            source_hash="empty",
            key_columns=["trade_date", "ts_code", "limit"],
            revision_ledger=None,
            allow_empty_revision_overwrite=False,
        )

        self.assertFalse(did_write)
        self.assertTrue(pd.read_parquet(path).equals(original))

    def test_required_event_flow_zero_rows_raise_instead_of_cron_ok(self):
        self._write_trade_cal("20200102")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["margin"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=EmptyTradeDateClient()):
            with self.assertRaisesRegex(RuntimeError, "required event/flow partitions returned zero rows"):
                download.download_event_flow(args)

    def test_event_flow_zero_rows_not_ready_exits_75_without_mutation(self):
        self._write_trade_cal("20200102")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["margin"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            zero_rows_not_ready=True,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=EmptyTradeDateClient()):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(download.download_event_flow(args), common.NO_MUTATION_RETRY_EXIT_CODE)

        self.assertIn("not_ready_no_mutation", output.getvalue())
        self.assertFalse((self.raw_dir / "margin" / "trade_date=20200102.parquet").exists())

    def test_event_flow_not_ready_vetoed_by_trade_cal_refresh(self):
        # A trade_cal coverage refresh IS a lake write: exit 75 asserts "no
        # mutation" and must not fire even when every dataset was empty.
        self._write_trade_cal("20200102")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["margin"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            zero_rows_not_ready=True,
        )

        with patch.object(download, "load_token", return_value="token"), \
                patch.object(download, "TuShareClient", return_value=EmptyTradeDateClient()), \
                patch.object(download, "ensure_trade_cal_coverage", return_value=True):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(download.download_event_flow(args), 0)

        self.assertIn("deferred to the retry job", output.getvalue())
        self.assertNotIn("not_ready_no_mutation", output.getvalue())

    def test_event_flow_blocked_shrink_raises_even_when_not_ready_enabled(self):
        # A non-empty response refused by the destructive-shrink guard is a
        # data-integrity alarm, never a "source not published yet" condition.
        self._write_trade_cal("20200102")
        path = self.raw_dir / "margin" / "trade_date=20200102.parquet"
        original = pd.DataFrame(
            [{"trade_date": "20200102", "exchange_id": f"EX{i:02d}", "rzye": 1.0} for i in range(30)]
        )
        common.write_parquet(path, original, api_name="margin", params={}, fields=list(original.columns), source_hash="old")

        class ShrunkMarginClient(EmptyTradeDateClient):
            def query(self, api_name, params=None, fields="", retries=5):
                if api_name == "margin":
                    columns = fields.split(",")
                    row = ["20200102" if col == "trade_date" else "EX00" if col == "exchange_id" else 1.0 for col in columns]
                    return common.ApiResult(columns, [row], common.stable_hash({"api_name": api_name}))
                return super().query(api_name, params, fields, retries)

        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["margin"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            zero_rows_not_ready=True,
            revision_ledger=str(self.root / "shrink_revision_events.jsonl"),
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=ShrunkMarginClient()):
            output = io.StringIO()
            with redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "overwrite was blocked"):
                    download.download_event_flow(args)

        self.assertTrue(pd.read_parquet(path).equals(original))

    def test_event_flow_zero_rows_not_ready_partial_write_commits(self):
        self._write_trade_cal("20200102")

        class MarginOnlyClient(EmptyTradeDateClient):
            def query(self, api_name, params=None, fields="", retries=5):
                if api_name == "margin":
                    columns = fields.split(",")
                    row = ["20200102" if col == "trade_date" else "SSE" if col == "exchange_id" else 1.0 for col in columns]
                    return common.ApiResult(columns, [row], common.stable_hash({"api_name": api_name}))
                return super().query(api_name, params, fields, retries)

        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["margin", "margin_detail"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            zero_rows_not_ready=True,
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=MarginOnlyClient()):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(download.download_event_flow(args), 0)

        self.assertIn("deferred to the retry job", output.getvalue())
        self.assertTrue((self.raw_dir / "margin" / "trade_date=20200102.parquet").exists())
        self.assertFalse((self.raw_dir / "margin_detail" / "trade_date=20200102.parquet").exists())

    def test_revision_sentinel_marks_source_failures_as_error(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "daily" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ"}]).to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["daily"],
            sample_size=0,
            seed=None,
            page_limit=10000,
            revision_ledger=str(self.root / "sentinel_error_events.jsonl"),
            output=str(self.root / "sentinel_error_summary.json"),
            fail_on_revision=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(audit, "load_token", return_value="token"), patch.object(audit, "TuShareClient", return_value=ErrorTradeDateClient()):
            self.assertEqual(audit.audit_revision_sentinel(args), 1)

        summary = json.loads((self.root / "sentinel_error_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["errors"], 1)

    def test_revision_sentinel_warns_on_missing_local_partition(self):
        self._write_trade_cal("20200102")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["adj_factor"],
            sample_size=0,
            seed=None,
            page_limit=10000,
            revision_ledger=str(self.root / "sentinel_missing_events.jsonl"),
            output=str(self.root / "sentinel_missing_summary.json"),
            fail_on_revision=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(audit, "load_token", return_value="token"), patch.object(audit, "TuShareClient", return_value=DailyMarketClient()):
            self.assertEqual(audit.audit_revision_sentinel(args), 0)

        summary = json.loads((self.root / "sentinel_missing_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "warning")
        self.assertEqual(summary["missing_local_dates"], 1)

    def test_revision_sentinel_marks_required_remote_zero_as_error(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "daily" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ"}]).to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            datasets=["daily"],
            sample_size=0,
            seed=None,
            page_limit=10000,
            revision_ledger=str(self.root / "sentinel_zero_events.jsonl"),
            output=str(self.root / "sentinel_zero_summary.json"),
            fail_on_revision=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with patch.object(audit, "load_token", return_value="token"), patch.object(audit, "TuShareClient", return_value=EmptyTradeDateClient()):
            self.assertEqual(audit.audit_revision_sentinel(args), 1)

        summary = json.loads((self.root / "sentinel_zero_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["remote_zero_dates"], 1)

    def test_revision_sentinel_default_ledger_for_temp_raw_stays_local(self):
        self._write_trade_cal("20200102")
        path = self.raw_dir / "daily" / "trade_date=20200102.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"trade_date": "20200102", "ts_code": "000001.SZ", "open": 99.0}]).to_parquet(path, index=False)
        args = argparse.Namespace(
            raw_dir="raw",
            start_date="20200102",
            end_date="20200102",
            datasets=["daily"],
            sample_size=0,
            seed=None,
            page_limit=10000,
            revision_ledger=common.REVISION_EVENTS_PATH,
            output=str(self.root / "sentinel_default_ledger_summary.json"),
            fail_on_revision=False,
            min_interval_seconds=0,
            timeout_seconds=1,
        )

        with (
            patch.object(audit.Path, "cwd", return_value=self.root),
            patch.object(audit, "load_token", return_value="token"),
            patch.object(audit, "TuShareClient", return_value=DailyMarketClient()),
        ):
            self.assertEqual(audit.audit_revision_sentinel(args), 0)

        local_ledger = self.root / "revision_events.jsonl"
        formal_ledger = self.root / common.REVISION_EVENTS_PATH
        self.assertTrue(local_ledger.exists())
        self.assertFalse(formal_ledger.exists())
        event = json.loads(local_ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["dataset"], "daily")
        self.assertEqual(event["changed_columns"]["open"], 1)

    def test_intraday_by_date_audit_errors_on_zero_row_partition(self):
        self._write_trade_cal()
        path = self.raw_dir / common.STK_MINS_BY_DATE_DATASET / "trade_date=20200102.parquet"
        empty = pd.DataFrame(columns=common.STK_MINS_REQUIRED_COLUMNS)
        common.write_parquet(path, empty, api_name=common.STK_MINS_API_NAME, params={}, fields=list(empty.columns), source_hash="empty")
        status_path = self.root / "status.json"
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            output_dataset=common.STK_MINS_BY_DATE_DATASET,
            codes=None,
            max_codes=None,
            expected_codes_source="none",
            min_rows_per_day=0,
            allow_missing_codes=0,
            full_scan=False,
            sample_limit=0,
            output=str(status_path),
        )

        self.assertEqual(audit.audit_intraday_by_date(args), 1)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        inventory = next(item for item in status["findings"] if item["check"] == f"{common.STK_MINS_BY_DATE_DATASET}_inventory")
        self.assertEqual(inventory["severity"], "error")
        self.assertEqual(inventory["details"]["zero_row_files"], 1)

    def test_board_trading_download_and_audit_use_dedicated_dimension(self):
        self._write_trade_cal("20231101")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20231101",
            end_date="20231101",
            datasets=["kpl_list", "limit_step", "limit_list_ths", "top_list", "hm_list", "ths_hot", "dc_hot"],
            force=False,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            kpl_tag=["涨停"],
            ths_limit_type=["涨停池"],
            ths_hot_market=["热股"],
            dc_hot_market=["A股市场"],
            dc_hot_type=["人气榜"],
            hot_is_new=["N"],
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=BoardClient()):
            self.assertEqual(download.download_board_trading(args), 0)

        self.assertTrue((self.raw_dir / "kpl_list" / f"tag={common.safe_partition_value('涨停')}" / "trade_date=20231101.parquet").exists())
        self.assertTrue((self.raw_dir / "limit_list_ths" / f"limit_type={common.safe_partition_value('涨停池')}" / "trade_date=20231101.parquet").exists())
        hot = pd.read_parquet(self.raw_dir / "ths_hot" / f"market={common.safe_partition_value('热股')}" / "is_new=N" / "trade_date=20231101.parquet")
        self.assertEqual(hot.loc[0, "available_at"], "2020-01-02 10:00:00+08:00")

        status_path = self.root / "board_status.json"
        audit_args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20231101",
            end_date="20231101",
            datasets=["kpl_list", "limit_step", "limit_list_ths", "top_list", "hm_list", "ths_hot", "dc_hot"],
            kpl_tag=["涨停"],
            ths_limit_type=["涨停池"],
            ths_hot_market=["热股"],
            dc_hot_market=["A股市场"],
            dc_hot_type=["人气榜"],
            hot_is_new=["N"],
            output=str(status_path),
        )
        self.assertEqual(audit.audit_board_trading_only(audit_args), 0)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "ok")
        self.assertIn("kpl_list", status["datasets"])

    def test_board_trading_skips_non_trading_window(self):
        self._write_trade_cal("20260530", is_open="0")
        args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20260530",
            end_date="20260530",
            datasets=["kpl_list", "limit_step", "limit_cpt_list"],
            force=True,
            page_limit=None,
            min_interval_seconds=0,
            timeout_seconds=1,
            kpl_tag=["涨停"],
            ths_limit_type=["涨停池"],
            ths_hot_market=["热股"],
            dc_hot_market=["A股市场"],
            dc_hot_type=["人气榜"],
            hot_is_new=["N"],
        )

        with patch.object(download, "load_token", return_value="token"), patch.object(download, "TuShareClient", return_value=NoQueryClient()):
            self.assertEqual(download.download_board_trading(args), 0)

    def test_text_source_time_is_normalized_to_china_timezone(self):
        frame = pd.DataFrame([{"title": "sample", "pub_time": "2020-01-02 10:00:00", "src": "x"}])
        out = common.augment_text_frame(frame, common.TEXT_SPECS["major_news"])
        self.assertEqual(out.loc[0, "available_at"], "2020-01-02 10:00:00+08:00")
        self.assertEqual(out.loc[0, "available_at_rule"], "source:pub_time")


# Source: test_tushare_intraday_by_date.py
import argparse
import importlib.util
import json
import types
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def load_tushare_data_module():
    script_root = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(script_root))

    def load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    download = load("autotrade_tushare_download", script_root / "data" / "tushare_download.py")
    audit = load("autotrade_tushare_audit", script_root / "data" / "tushare_audit.py")
    return types.SimpleNamespace(
        compact_intraday_by_date=download.compact_intraday_by_date,
        audit_intraday_by_date=audit.audit_intraday_by_date,
    )


class IntradayByDateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self.tmp.name) / "raw"
        self.module = load_tushare_data_module()
        self._write_reference_inputs()
        self._write_stock_year_inputs()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_reference_inputs(self):
        trade_cal = self.raw_dir / "trade_cal" / "exchange=SSE" / "year=2020.parquet"
        trade_cal.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {"cal_date": "20200102", "is_open": "1"},
        ]).to_parquet(trade_cal, index=False)

        stock_basic = self.raw_dir / "stock_basic" / "list_status=L.parquet"
        stock_basic.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {
                "ts_code": "000001.SZ",
                "name": "A",
                "market": "主板",
                "exchange": "SZSE",
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": "",
            },
            {
                "ts_code": "000002.SZ",
                "name": "B",
                "market": "主板",
                "exchange": "SZSE",
                "list_status": "L",
                "list_date": "19910129",
                "delist_date": "",
            },
        ]).to_parquet(stock_basic, index=False)

    def _write_stock_year_inputs(self):
        rows_by_code = {
            "000001.SZ": [
                ("2020-01-02 09:30:00", 10.0),
                ("2020-01-02 15:00:00", 10.5),
            ],
            "000002.SZ": [
                ("2020-01-02 09:30:00", 20.0),
                ("2020-01-02 15:00:00", 20.5),
            ],
        }
        for ts_code, bars in rows_by_code.items():
            path = self.raw_dir / "stk_mins_1min" / f"ts_code={ts_code}" / "year=2020.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {
                    "ts_code": ts_code,
                    "trade_time": trade_time,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "vol": 100,
                    "amount": price * 100,
                    "trade_date": "20200102",
                    "available_at": f"{trade_time}+08:00",
                    "available_at_rule": "source:trade_time_bar_close",
                }
                for trade_time, price in bars
            ]).to_parquet(path, index=False)

    def test_compact_and_audit_intraday_by_date(self):
        compact_args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            output_dataset="stk_mins_1min_by_date",
            codes=None,
            max_codes=None,
            expected_codes_source="active",
            min_rows_per_day=4,
            allow_missing_codes=0,
            force=False,
            allow_empty=False,
            allow_validation_warnings=False,
        )
        self.assertEqual(self.module.compact_intraday_by_date(compact_args), 0)

        output = self.raw_dir / "stk_mins_1min_by_date" / "trade_date=20200102.parquet"
        self.assertTrue(output.exists())
        self.assertTrue(output.with_suffix(output.suffix + ".meta.json").exists())
        df = pd.read_parquet(output)
        self.assertEqual(len(df), 4)
        self.assertEqual(sorted(df["ts_code"].unique().tolist()), ["000001.SZ", "000002.SZ"])
        self.assertEqual(set(df["trade_date"].astype(str)), {"20200102"})

        status_path = Path(self.tmp.name) / "intraday_by_date_status.json"
        audit_args = argparse.Namespace(
            raw_dir=str(self.raw_dir),
            start_date="20200102",
            end_date="20200102",
            output_dataset="stk_mins_1min_by_date",
            codes=None,
            max_codes=None,
            expected_codes_source="active",
            min_rows_per_day=4,
            allow_missing_codes=0,
            full_scan=True,
            sample_limit=20,
            output=str(status_path),
        )
        self.assertEqual(self.module.audit_intraday_by_date(audit_args), 0)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "ok")


class RealtimeMinuteTest(unittest.TestCase):
    def test_normalize_poll_dedup_and_store_roundtrip(self):
        import pandas as pd

        from autotrade.data_sources.tushare.common import ApiResult, STK_MINS_REQUIRED_COLUMNS
        from autotrade.data_sources.tushare.realtime import RealtimeMinuteFeed, RealtimeMinuteStore

        fields = ["ts_code", "freq", "time", "open", "close", "high", "low", "vol", "amount"]

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def query(self, api_name, params=None, fields_arg="", retries=5):
                assert api_name == "rt_min" and params["freq"] == "1MIN"
                self.calls += 1
                return ApiResult(fields=fields, items=[
                    [params["ts_code"], "1MIN", "2026-07-13 09:31:00", 10.0, 10.1, 10.2, 9.9, 1000, 10050.0],
                ], source_hash="x")

        client = FakeClient()
        feed = RealtimeMinuteFeed(client, ["000001.SZ", "600000.SH", "000001.SZ"])
        first = feed.poll()
        self.assertEqual(list(first.columns), STK_MINS_REQUIRED_COLUMNS)
        self.assertEqual(len(first), 2)  # watchlist deduped
        self.assertEqual(first.loc[0, "trade_date"], "20260713")
        self.assertEqual(first.loc[0, "available_at"], "2026-07-13T09:31:00+08:00")
        # Second poll returns the same bars -> all filtered as already seen.
        self.assertTrue(feed.poll().empty)

        with tempfile.TemporaryDirectory() as tmp:
            store = RealtimeMinuteStore(Path(tmp) / "rt_min_live")
            self.assertEqual(store.append(first), {"20260713": 2})
            # Re-append overlaps: dedup by (ts_code, trade_time), atomic replace.
            store.append(first)
            bars = store.bars("20260713")
            self.assertEqual(len(bars), 2)
            self.assertEqual(list(bars.columns), STK_MINS_REQUIRED_COLUMNS)
            self.assertTrue(store.bars("20990101").empty)
