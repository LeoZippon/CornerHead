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

from hl_trader.data_sources.tushare import audit, common, cron_update, download


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
            datasets=["margin", "margin_detail"],
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

    def test_generic_event_flow_download_excludes_dedicated_share_float_path(self):
        selected = download.selected_event_flow_download_datasets(argparse.Namespace(datasets=None))
        self.assertNotIn("share_float", selected)
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

    def test_cron_full_audit_builds_all_formal_status_commands(self):
        ctx = cron_update.RunContext(
            config={"default_raw_dir": "raw"},
            repo_root=self.root,
            python="/env/python",
            job_name="cn_nightly_full_audit",
            job={"operation": "audit_full", "event_flow_end_extra_offset_days": 1},
            start_date="20200101",
            end_date="20260601",
            timezone_name="Asia/Shanghai",
        )

        commands = cron_update.build_job_commands(ctx)

        self.assertEqual(len(commands), 6)
        command_text = [" ".join(command) for command in commands]
        self.assertIn("scripts/tushare/audit.py base", command_text[0])
        self.assertIn("--include-limit-list", command_text[0])
        self.assertIn("scripts/tushare/audit.py macro", command_text[1])
        self.assertIn("scripts/tushare/audit.py intraday-by-date", command_text[2])
        self.assertIn("--expected-codes-source minute", command_text[2])
        self.assertIn("scripts/tushare/audit.py event-flow", command_text[3])
        self.assertIn("--end-date 20260531", command_text[3])
        self.assertIn("scripts/tushare/audit.py board-trading", command_text[4])
        self.assertIn("--include-text", command_text[5])
        self.assertTrue(all("--start-date 20200101" in text for text in command_text))
        self.assertTrue(all("--raw-dir raw" in text for text in command_text))

    def test_cron_update_job_can_use_rolling_start_lookback(self):
        config_path = self.root / "schedule.json"
        config_path.write_text(json.dumps({
            "timezone": "Asia/Shanghai",
            "repo_root": str(self.root),
            "python": "/env/python",
            "default_start_date": "20200101",
            "jobs": {
                "cn_evening_full": {
                    "start_date_lookback_days": 30,
                    "extra_args": ["--refresh-daily-datasets", "daily", "adj_factor"],
                },
                "cn_preopen_margin_backfill_0905": {"operation": "download_event_flow", "end_date_offset_days": 1},
            },
        }), encoding="utf-8")

        args = argparse.Namespace(config=str(config_path), job="cn_evening_full", start_date=None, end_date="20260601", dry_run=False, force_run=False)
        ctx = cron_update.build_context(args)
        self.assertEqual(ctx.start_date, "20260502")
        self.assertEqual(ctx.end_date, "20260601")
        command = " ".join(cron_update.build_job_commands(ctx)[0])
        self.assertIn("--refresh-daily-datasets daily adj_factor", command)

        margin_args = argparse.Namespace(config=str(config_path), job="cn_preopen_margin_backfill_0905", start_date=None, end_date="20260601", dry_run=False, force_run=False)
        margin_ctx = cron_update.build_context(margin_args)
        self.assertEqual(margin_ctx.start_date, "20260601")

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
        self.assertIn("scripts/tushare/download.py download --tier board_trading", text)
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
        self.assertIn("scripts/tushare/audit.py revision-sentinel", text)
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
        self.assertIn("scripts/tushare/audit.py event-flow", text)
        self.assertIn("--start-date 20200101 --end-date 20260601", text)
        self.assertIn("--raw-dir raw", text)

    def test_cron_lock_waits_and_reports_live_locks(self):
        runtime = self.root / ".runtime" / "tushare"
        lock = runtime / "locks" / "tushare_update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("pid=1\nstarted_at=2099-01-01T00:00:00+00:00\n", encoding="utf-8")

        with patch.object(cron_update, "RUNTIME_ROOT", runtime):
            with self.assertRaisesRegex(RuntimeError, "lock exists"):
                cron_update.acquire_lock("tushare_update", wait_seconds=0, stale_seconds=21600)

    def test_cron_lock_removes_stale_dead_pid_lock(self):
        runtime = self.root / ".runtime" / "tushare"
        lock = runtime / "locks" / "tushare_update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("pid=999999999\nstarted_at=2020-01-01T00:00:00+00:00\n", encoding="utf-8")

        with patch.object(cron_update, "RUNTIME_ROOT", runtime), patch.object(cron_update, "DISPATCH_LOG_PATH", self.root / "dispatch.log"):
            acquired = cron_update.acquire_lock("tushare_update", wait_seconds=0, stale_seconds=21600)
        self.assertTrue(acquired.exists())
        acquired.unlink()

    def test_cron_lock_does_not_remove_live_pid_lock_by_age(self):
        runtime = self.root / ".runtime" / "tushare"
        lock = runtime / "locks" / "tushare_update.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("pid=1\nstarted_at=2020-01-01T00:00:00+00:00\n", encoding="utf-8")

        with patch.object(cron_update, "RUNTIME_ROOT", runtime), patch.object(cron_update, "pid_is_alive", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "lock exists"):
                cron_update.acquire_lock("tushare_update", wait_seconds=0, stale_seconds=1)
        self.assertTrue(lock.exists())

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
        self.assertEqual(json.loads(ledger_lines[0])["downstream_status"], "pending_review")

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

    download = load("macroquant_tushare_download", script_root / "tushare" / "download.py")
    audit = load("macroquant_tushare_audit", script_root / "tushare" / "audit.py")
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
