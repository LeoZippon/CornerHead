#!/usr/bin/env python3
"""TuShare download, update, and raw-data maintenance CLI for MacroQuant."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from hl_trader.data_sources.tushare import common as core
    from hl_trader.data_sources.tushare.common import *  # noqa: F401,F403
else:
    from . import common as core
    from .common import *  # noqa: F401,F403

def download_reference(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    refresh_datasets = set(getattr(args, "refresh_reference_datasets", None) or [])

    def should_force(dataset: str) -> bool:
        return bool(args.force or dataset in refresh_datasets)

    stock_basic_fields = "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type"
    for status in ("L", "D", "P"):
        path = raw_dir / "stock_basic" / f"list_status={status}.parquet"
        if path.exists() and not should_force("stock_basic"):
            continue
        params = {"exchange": "", "list_status": status}
        result = client.query("stock_basic", params, stock_basic_fields)
        write_parquet(path, frame(result), api_name="stock_basic", params=params, fields=result.fields, source_hash=result.source_hash)

    company_fields = "ts_code,com_name,com_id,exchange,chairman,manager,secretary,reg_capital,setup_date,province,city,introduction"
    for exchange in ("SSE", "SZSE", "BSE"):
        path = raw_dir / "stock_company" / f"exchange={exchange}.parquet"
        if path.exists() and not should_force("stock_company"):
            continue
        params = {"exchange": exchange}
        result = client.query("stock_company", params, company_fields)
        write_parquet(path, frame(result), api_name="stock_company", params=params, fields=result.fields, source_hash=result.source_hash)

    open_dates = download_trade_cal(client, raw_dir, args.start_date, args.end_date, should_force("trade_cal"))
    if not args.skip_bak_basic:
        download_bak_basic(client, raw_dir, open_dates, args.bak_start_date, should_force("bak_basic"))
    download_namechange(client, raw_dir, should_force("namechange"))
    classify = download_index_classify(client, raw_dir, should_force("index_classify"))
    download_index_member_all(client, raw_dir, classify, should_force("index_member_all"))
    print(f"reference download finished under {raw_dir}")
    return 0

def download_trade_cal(client: TuShareClient, raw_dir: Path, start_date: str, end_date: str, force: bool) -> list[str]:
    fields = "exchange,cal_date,is_open,pretrade_date"
    sse_open: set[str] = set()
    for exchange in ("SSE", "SZSE", "BSE"):
        for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
            path = raw_dir / "trade_cal" / f"exchange={exchange}" / f"year={year}.parquet"
            params = {"exchange": exchange, "start_date": max(start_date, f"{year}0101"), "end_date": min(end_date, f"{year}1231")}
            if path.exists() and not force:
                df = pd.read_parquet(path)
                dates = df["cal_date"].astype(str) if "cal_date" in df.columns else pd.Series(dtype=str)
                if not dates.empty and dates.min() <= params["start_date"] and dates.max() >= params["end_date"]:
                    if exchange == "SSE" and not df.empty:
                        sse_open.update(df.loc[df["is_open"].astype(str) == "1", "cal_date"].astype(str).tolist())
                    continue
                result = client.query("trade_cal", params, fields)
                refreshed = frame(result)
                df = pd.concat([df, refreshed], ignore_index=True) if not df.empty else refreshed
                if not df.empty:
                    df = df.drop_duplicates(["exchange", "cal_date"], keep="last").sort_values(["exchange", "cal_date"], ascending=[True, False]).reset_index(drop=True)
                meta_params = dict(params)
                meta_params["merge_existing"] = True
                write_parquet(path, df, api_name="trade_cal", params=meta_params, fields=fields.split(","), source_hash=stable_hash(df.fillna("").astype(str).to_dict("records")))
            else:
                result = client.query("trade_cal", params, fields)
                df = frame(result)
                write_parquet(path, df, api_name="trade_cal", params=params, fields=result.fields, source_hash=result.source_hash)
            if exchange == "SSE" and not df.empty:
                sse_open.update(df.loc[df["is_open"].astype(str) == "1", "cal_date"].astype(str).tolist())
    return sorted(sse_open)

def download_bak_basic(client: TuShareClient, raw_dir: Path, trade_dates: list[str], start_date: str, force: bool) -> None:
    fields = "trade_date,ts_code,name,industry,area,pe,float_share,total_share,total_assets,liquid_assets,fixed_assets,reserved,reserved_pershare,eps,bvps,pb,list_date,undp,per_undp,rev_yoy,profit_yoy,gpr,npr,holder_num"
    dates = [d for d in trade_dates if d >= start_date]
    for index, trade_date in enumerate(dates, start=1):
        path = raw_dir / "bak_basic" / f"trade_date={trade_date}.parquet"
        if path.exists() and not force:
            continue
        params = {"trade_date": trade_date}
        result = client.query("bak_basic", params, fields)
        write_parquet(path, frame(result), api_name="bak_basic", params=params, fields=result.fields, source_hash=result.source_hash)
        if index % 250 == 0:
            print(f"bak_basic {index}/{len(dates)}")

def download_namechange(client: TuShareClient, raw_dir: Path, force: bool) -> None:
    path = raw_dir / "namechange" / "namechange.parquet"
    if path.exists() and not force:
        return
    fields = "ts_code,name,start_date,end_date,ann_date,change_reason"
    frames: list[pd.DataFrame] = []
    for index, code in enumerate(load_stock_codes(raw_dir), start=1):
        result = client.query("namechange", {"ts_code": code}, fields)
        df = frame(result)
        if not df.empty:
            frames.append(df)
        if index % 500 == 0:
            print(f"namechange {index}")
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=fields.split(","))
    deduped = combined.drop_duplicates().sort_values(["ts_code", "start_date", "name"], na_position="last").reset_index(drop=True)
    write_parquet(path, deduped, api_name="namechange", params={"strategy": "per_ts_code_all_stock_basic"}, fields=fields.split(","), source_hash=stable_hash(deduped.fillna("").astype(str).to_dict("records")))

def download_index_classify(client: TuShareClient, raw_dir: Path, force: bool) -> pd.DataFrame:
    path = raw_dir / "index_classify" / "src=SW2021.parquet"
    if path.exists() and not force:
        return pd.read_parquet(path)
    fields = "index_code,industry_name,level,industry_code,is_pub,parent_code,src"
    params = {"src": "SW2021"}
    result = client.query("index_classify", params, fields)
    df = frame(result)
    write_parquet(path, df, api_name="index_classify", params=params, fields=result.fields, source_hash=result.source_hash)
    return df

def download_index_member_all(client: TuShareClient, raw_dir: Path, classify: pd.DataFrame, force: bool) -> None:
    fields = "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new"
    l1_codes = classify.loc[classify["level"].astype(str) == "L1", "index_code"].dropna().astype(str).tolist()
    for code in sorted(l1_codes):
        path = raw_dir / "index_member_all" / f"l1_code={code}.parquet"
        if path.exists() and not force:
            continue
        params = {"l1_code": code}
        result = client.query("index_member_all", params, fields)
        write_parquet(path, frame(result), api_name="index_member_all", params=params, fields=result.fields, source_hash=result.source_hash)

def download_daily(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    for dataset in selected_daily_datasets(args):
        spec = DAILY_SPECS[dataset]
        start_date = max(args.start_date, spec.start_date)
        dates = [d for d in trade_dates if start_date <= d <= args.end_date]
        download_trade_date_dataset(client, raw_dir, spec, dates, args.force, args.page_limit)
    print(f"daily market download finished under {raw_dir}")
    return 0

def download_trade_date_dataset(client: TuShareClient, raw_dir: Path, spec: TradeDateDataset, trade_dates: list[str], force: bool, page_limit: int) -> None:
    dataset_dir = raw_dir / spec.api_name
    written = 0
    skipped = 0
    zero_skipped = 0
    total_rows = 0
    for index, trade_date in enumerate(trade_dates, start=1):
        path = dataset_dir / f"trade_date={trade_date}.parquet"
        if path.exists() and not force:
            if spec.zero_rows_ok or parquet_rows(path) > 0:
                skipped += 1
                if index % 250 == 0:
                    print(f"{spec.api_name} {index}/{len(trade_dates)} skipped={skipped} written={written}")
                continue
        params = {"trade_date": trade_date}
        try:
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        except Exception as exc:
            raise RuntimeError(f"{spec.api_name} trade_date={trade_date} failed: {exc}") from exc
        df = frame(result)
        if df.empty and not spec.zero_rows_ok:
            zero_skipped += 1
            print(f"{spec.api_name} trade_date={trade_date} returned zero rows; skipped_write")
            continue
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=result.fields, source_hash=result.source_hash)
        total_rows += len(df)
        written += 1
        if index % 250 == 0:
            print(f"{spec.api_name} {index}/{len(trade_dates)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done dates={len(trade_dates)} skipped={skipped} written={written} zero_skipped={zero_skipped} rows_written={total_rows}")

def download_macro(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    for dataset in selected_macro_datasets(args):
        spec = MACRO_SPECS[dataset]
        start_date = max(args.start_date, spec.start_date)
        if spec.strategy == "quarter_once":
            download_macro_quarter_once(client, raw_dir, spec, start_date, args.end_date, args.force)
        elif spec.strategy == "month_once":
            download_macro_month_once(client, raw_dir, spec, start_date, args.end_date, args.force)
        elif spec.strategy == "month_loop":
            download_macro_month_loop(client, raw_dir, spec, start_date, args.end_date, args.force)
        elif spec.strategy == "date_year":
            download_macro_date_year(client, raw_dir, spec, start_date, args.end_date, args.force, macro_page_limit(spec, args.page_limit))
        elif spec.strategy == "date_year_by_curr_type":
            download_macro_date_year_by_curr_type(client, raw_dir, spec, start_date, args.end_date, args.force, macro_page_limit(spec, args.page_limit), selected_libor_currencies(args))
        elif spec.strategy == "date_year_by_ts_code":
            codes = selected_index_codes(args) if dataset == "index_global" else selected_fx_codes(args)
            download_macro_date_year_by_ts_code(client, raw_dir, spec, start_date, args.end_date, args.force, macro_page_limit(spec, args.page_limit), codes)
        elif spec.strategy == "eco_cal_month":
            download_macro_eco_cal_month(client, raw_dir, spec, start_date, args.end_date, args.force, macro_page_limit(spec, args.page_limit), args)
        else:
            raise RuntimeError(f"unsupported macro strategy {spec.strategy} for {dataset}")
    print(f"{args.tier} download finished under {raw_dir}")
    return 0

def download_macro_quarter_once(client: TuShareClient, raw_dir: Path, spec: MacroDataset, start_date: str, end_date: str, force: bool) -> None:
    start_q = max(yyyymmdd_to_quarter(start_date), spec.start_quarter)
    end_q = yyyymmdd_to_quarter(end_date)
    path = raw_dir / spec.api_name / f"range={start_q}_{end_q}.parquet"
    coverage_params = {"start_date": start_date, "end_date": end_date}
    if should_skip_existing_partition(path, force=force, requested_params=coverage_params):
        return
    params = {"start_q": start_q, "end_q": end_q}
    result = client.query(spec.api_name, params, spec.fields)
    rows = write_macro_result(path, result, spec, {**params, **coverage_params})
    print(f"{spec.api_name} quarters {start_q}-{end_q} rows={rows}")

def download_macro_month_once(client: TuShareClient, raw_dir: Path, spec: MacroDataset, start_date: str, end_date: str, force: bool) -> None:
    start_m = max(yyyymmdd_to_month(start_date), spec.start_month)
    end_m = yyyymmdd_to_month(end_date)
    path = raw_dir / spec.api_name / f"range={start_m}_{end_m}.parquet"
    coverage_params = {"start_date": start_date, "end_date": end_date}
    if should_skip_existing_partition(path, force=force, requested_params=coverage_params):
        return
    params = {"start_m": start_m, "end_m": end_m}
    result = client.query(spec.api_name, params, spec.fields)
    rows = write_macro_result(path, result, spec, {**params, **coverage_params})
    print(f"{spec.api_name} months {start_m}-{end_m} rows={rows}")

def download_macro_month_loop(client: TuShareClient, raw_dir: Path, spec: MacroDataset, start_date: str, end_date: str, force: bool) -> None:
    windows = month_windows(start_date, end_date)
    end_month = end_date[:6]
    written = 0
    skipped = 0
    total_rows = 0
    for index, (window_start, window_end, month) in enumerate(windows, start=1):
        path = raw_dir / spec.api_name / f"month={month}.parquet"
        coverage_params = {"start_date": window_start, "end_date": window_end} if month == end_month else None
        if should_skip_existing_partition(path, force=force, requested_params=coverage_params):
            skipped += 1
            continue
        params = {"m": month}
        meta_params = {**params, **coverage_params} if coverage_params else params
        result = client.query(spec.api_name, params, spec.fields)
        total_rows += write_macro_result(path, result, spec, meta_params)
        written += 1
        if index % 24 == 0:
            print(f"{spec.api_name} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows}")

def download_macro_date_year(client: TuShareClient, raw_dir: Path, spec: MacroDataset, start_date: str, end_date: str, force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    years = range(int(start_date[:4]), int(end_date[:4]) + 1)
    for year in years:
        year_start = max(start_date, f"{year}0101")
        year_end = min(end_date, f"{year}1231")
        path = raw_dir / spec.api_name / f"year={year}.parquet"
        params = {"start_date": year_start, "end_date": year_end}
        if should_skip_existing_partition(path, force=force, requested_params=params):
            skipped += 1
            continue
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        total_rows += write_macro_result(path, result, spec, meta_params)
        written += 1
    print(f"{spec.api_name} done years={int(end_date[:4]) - int(start_date[:4]) + 1} skipped={skipped} written={written} rows_written={total_rows}")

def download_macro_date_year_by_curr_type(
    client: TuShareClient,
    raw_dir: Path,
    spec: MacroDataset,
    start_date: str,
    end_date: str,
    force: bool,
    page_limit: int,
    currencies: list[str],
) -> None:
    total_tasks = len(currencies) * (int(end_date[:4]) - int(start_date[:4]) + 1)
    written = 0
    skipped = 0
    total_rows = 0
    task_index = 0
    for curr_type in currencies:
        for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
            task_index += 1
            year_start = max(start_date, f"{year}0101")
            year_end = min(end_date, f"{year}1231")
            path = raw_dir / spec.api_name / f"curr_type={safe_partition_value(curr_type)}" / f"year={year}.parquet"
            params = {"curr_type": curr_type, "start_date": year_start, "end_date": year_end}
            if should_skip_existing_partition(path, force=force, requested_params=params):
                skipped += 1
                continue
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            meta_params = dict(params)
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            total_rows += write_macro_result(path, result, spec, meta_params)
            written += 1
            if task_index % 25 == 0:
                print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows}")

def download_macro_date_year_by_ts_code(
    client: TuShareClient,
    raw_dir: Path,
    spec: MacroDataset,
    start_date: str,
    end_date: str,
    force: bool,
    page_limit: int,
    codes: list[str],
) -> None:
    total_tasks = len(codes) * (int(end_date[:4]) - int(start_date[:4]) + 1)
    written = 0
    skipped = 0
    total_rows = 0
    task_index = 0
    for ts_code in codes:
        for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
            task_index += 1
            year_start = max(start_date, f"{year}0101")
            year_end = min(end_date, f"{year}1231")
            path = raw_dir / spec.api_name / f"ts_code={safe_partition_value(ts_code)}" / f"year={year}.parquet"
            params = {"ts_code": ts_code, "start_date": year_start, "end_date": year_end}
            if should_skip_existing_partition(path, force=force, requested_params=params):
                skipped += 1
                continue
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            meta_params = dict(params)
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            total_rows += write_macro_result(path, result, spec, meta_params)
            written += 1
            if task_index % 25 == 0:
                print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows}")

def download_macro_eco_cal_month(
    client: TuShareClient,
    raw_dir: Path,
    spec: MacroDataset,
    start_date: str,
    end_date: str,
    force: bool,
    page_limit: int,
    args: argparse.Namespace,
) -> None:
    countries = selected_eco_filter_values(args, "eco_country")
    currencies = selected_eco_filter_values(args, "eco_currency")
    events = selected_eco_filter_values(args, "eco_event")
    windows = month_windows(start_date, end_date)
    total_tasks = len(countries) * len(currencies) * len(events) * len(windows)
    written = 0
    skipped = 0
    total_rows = 0
    task_index = 0
    for country in countries:
        for currency in currencies:
            for event in events:
                country_part = safe_partition_value(country) if country else "all"
                currency_part = safe_partition_value(currency) if currency else "all"
                event_part = safe_partition_value(event) if event else "all"
                for start, end, month in windows:
                    task_index += 1
                    path = raw_dir / spec.api_name / f"country={country_part}" / f"currency={currency_part}" / f"event={event_part}" / f"month={month}.parquet"
                    params: dict[str, Any] = {"start_date": start, "end_date": end}
                    if country:
                        params["country"] = country
                    if currency:
                        params["currency"] = currency
                    if event:
                        params["event"] = event
                    if should_skip_existing_partition(path, force=force, requested_params=params):
                        skipped += 1
                        continue
                    result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
                    meta_params = dict(params)
                    meta_params.update({
                        "country_partition": country_part,
                        "currency_partition": currency_part,
                        "event_partition": event_part,
                        "month": month,
                        "pagination": {"page_limit": page_limit, "pages": pages},
                    })
                    total_rows += write_macro_result(path, result, spec, meta_params)
                    written += 1
                    if task_index % 24 == 0:
                        print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows}")

def download_event_flow(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    datasets = selected_event_flow_download_datasets(args)
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    trade_dates: list[str] = []
    trade_end_date = args.end_date
    if any(EVENT_FLOW_SPECS[name].strategy == "trade_date" for name in datasets):
        latest_trade_calendar_date = latest_sse_calendar_date(raw_dir)
        trade_end_date = min(args.end_date, latest_trade_calendar_date)
        if trade_end_date < args.end_date:
            print(
                f"event/flow trade-date datasets capped at local SSE trade_cal end {trade_end_date}; "
                f"requested end_date={args.end_date}"
            )
        trade_dates = load_sse_open_dates(raw_dir, args.start_date, trade_end_date, allow_empty=True)
        if not trade_dates:
            print(f"event/flow trade-date datasets skipped: no SSE open dates for {args.start_date}-{trade_end_date}")
    windows = month_windows(args.start_date, args.end_date)
    days = date_range_days(args.start_date, args.end_date)
    for dataset in datasets:
        spec = EVENT_FLOW_SPECS[dataset]
        start_date = max(args.start_date, spec.start_date)
        if spec.strategy == "trade_date":
            dates = [d for d in trade_dates if start_date <= d <= trade_end_date]
            download_event_trade_date_dataset(client, raw_dir, spec, dates, args.force, event_page_limit(spec, args.page_limit))
        elif spec.strategy == "range_month":
            dataset_windows = [(s, e, m) for s, e, m in windows if e >= start_date]
            download_event_range_month(client, raw_dir, spec, dataset_windows, args.force, event_page_limit(spec, args.page_limit))
        elif spec.strategy == "day":
            dataset_days = [day for day in days if day >= start_date]
            download_event_day_dataset(client, raw_dir, spec, dataset_days, args.force)
        else:
            raise RuntimeError(f"unsupported event/flow strategy {spec.strategy} for {dataset}")
    print(f"event/flow download finished under {raw_dir}")
    return 0

def selected_event_flow_download_datasets(args: argparse.Namespace) -> list[str]:
    default = [dataset for dataset in EVENT_FLOW_DATASETS if dataset != "share_float"]
    datasets = list(args.datasets or default)
    invalid = sorted(set(datasets) - set(EVENT_FLOW_SPECS))
    if invalid:
        raise RuntimeError(f"unknown event/flow datasets: {invalid}; supported={sorted(EVENT_FLOW_SPECS)}")
    if "share_float" in datasets:
        raise RuntimeError(
            "share_float must be downloaded with download-share-float-complete; "
            "the generic event_flow path cannot provide the ann_date rescue and union guard."
        )
    return datasets

def download_event_trade_date_dataset(client: TuShareClient, raw_dir: Path, spec: EventDataset, trade_dates: list[str], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    zero_skipped = 0
    total_rows = 0
    total_pages = 0
    for index, trade_date in enumerate(trade_dates, start=1):
        path = raw_dir / spec.api_name / f"trade_date={trade_date}.parquet"
        if path.exists() and not force:
            if spec.zero_rows_ok or parquet_rows(path) > 0:
                skipped += 1
                if index % 250 == 0:
                    print(f"{spec.api_name} {index}/{len(trade_dates)} skipped={skipped} written={written} rows_written={total_rows}")
                continue
        params = {"trade_date": trade_date}
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        df = augment_event_frame(frame(result), spec)
        if df.empty and not spec.zero_rows_ok:
            zero_skipped += 1
            total_pages += pages
            print(f"{spec.api_name} trade_date={trade_date} returned zero rows; skipped_write")
            continue
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
        total_rows += len(df)
        written += 1
        total_pages += pages
        if index % 250 == 0:
            print(f"{spec.api_name} {index}/{len(trade_dates)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done dates={len(trade_dates)} skipped={skipped} written={written} zero_skipped={zero_skipped} rows_written={total_rows} pages={total_pages}")

def download_event_range_month(client: TuShareClient, raw_dir: Path, spec: EventDataset, windows: list[tuple[str, str, str]], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for index, (start_date, end_date, month) in enumerate(windows, start=1):
        path = raw_dir / spec.api_name / f"month={month}.parquet"
        params = {"start_date": start_date, "end_date": end_date}
        if should_skip_existing_partition(path, force=force, requested_params=params):
            skipped += 1
            continue
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        meta_params = dict(params)
        meta_params["month"] = month
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        total_rows += write_event_result(path, result, spec, meta_params)
        written += 1
        total_pages += pages
        if index % 24 == 0:
            print(f"{spec.api_name} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def download_event_day_dataset(client: TuShareClient, raw_dir: Path, spec: EventDataset, days: list[str], force: bool) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, day in enumerate(days, start=1):
        path = raw_dir / spec.api_name / f"date={day}.parquet"
        if path.exists() and not force:
            skipped += 1
            if index % 250 == 0:
                print(f"{spec.api_name} days {index}/{len(days)} skipped={skipped} written={written} rows_written={total_rows}")
            continue
        params = {"start_date": day, "end_date": day}
        result = client.query(spec.api_name, params, spec.fields)
        total_rows += write_event_result(path, result, spec, params)
        written += 1
        if index % 250 == 0:
            print(f"{spec.api_name} days {index}/{len(days)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done days={len(days)} skipped={skipped} written={written} rows_written={total_rows}")

def download_board_trading(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    datasets = selected_board_trading_datasets(args)
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    trade_dates: list[str] = []
    if any(BOARD_TRADING_SPECS[name].strategy != "static_once" for name in datasets):
        latest_trade_calendar_date = latest_sse_calendar_date(raw_dir)
        trade_end_date = min(args.end_date, latest_trade_calendar_date)
        if trade_end_date < args.end_date:
            print(
                f"board-trading trade-date datasets capped at local SSE trade_cal end {trade_end_date}; "
                f"requested end_date={args.end_date}"
            )
        trade_dates = load_sse_open_dates(raw_dir, args.start_date, trade_end_date, allow_empty=True)
        if not trade_dates:
            print(f"board-trading trade-date datasets skipped: no SSE open dates for {args.start_date}-{trade_end_date}")
    for dataset in datasets:
        spec = BOARD_TRADING_SPECS[dataset]
        start_date = max(args.start_date, spec.start_date)
        dates = [trade_date for trade_date in trade_dates if start_date <= trade_date <= args.end_date]
        page_limit = board_page_limit(spec, args.page_limit)
        if spec.strategy == "static_once":
            download_board_static_dataset(client, raw_dir, spec, args.force)
        elif spec.strategy == "trade_date":
            download_board_trade_date_dataset(client, raw_dir, spec, dates, args.force, page_limit)
        elif spec.strategy == "trade_date_by_tag":
            download_board_kpl_list(client, raw_dir, spec, dates, args.force, page_limit, selected_board_kpl_tags(args))
        elif spec.strategy == "trade_date_by_limit_type":
            download_board_limit_list_ths(client, raw_dir, spec, dates, args.force, page_limit, selected_board_ths_limit_types(args))
        elif spec.strategy == "trade_date_by_market":
            download_board_hot_by_market(client, raw_dir, spec, dates, args.force, page_limit, selected_board_ths_hot_markets(args), selected_board_hot_is_new(args))
        elif spec.strategy == "trade_date_by_market_hot_type":
            download_board_dc_hot(client, raw_dir, spec, dates, args.force, page_limit, selected_board_dc_hot_markets(args), selected_board_dc_hot_types(args), selected_board_hot_is_new(args))
        else:
            raise RuntimeError(f"unsupported board-trading strategy {spec.strategy} for {dataset}")
    print(f"board-trading download finished under {raw_dir}")
    return 0

def download_board_static_dataset(client: TuShareClient, raw_dir: Path, spec: BoardTradingDataset, force: bool) -> None:
    path = raw_dir / spec.api_name / f"{spec.api_name}.parquet"
    if path.exists() and not force:
        print(f"{spec.api_name} static skipped")
        return
    result = client.query(spec.api_name, {}, spec.fields)
    rows = write_board_result(path, result, spec, {})
    print(f"{spec.api_name} static rows={rows}")

def download_board_trade_date_dataset(client: TuShareClient, raw_dir: Path, spec: BoardTradingDataset, trade_dates: list[str], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for index, trade_date in enumerate(trade_dates, start=1):
        path = raw_dir / spec.api_name / f"trade_date={trade_date}.parquet"
        if should_skip_existing_partition(path, force=force):
            skipped += 1
            continue
        params = {"trade_date": trade_date}
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        total_rows += write_board_result(path, result, spec, meta_params)
        total_pages += pages
        written += 1
        if index % 250 == 0:
            print(f"{spec.api_name} {index}/{len(trade_dates)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done dates={len(trade_dates)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def download_board_kpl_list(client: TuShareClient, raw_dir: Path, spec: BoardTradingDataset, trade_dates: list[str], force: bool, page_limit: int, tags: list[str]) -> None:
    total_tasks = len(trade_dates) * len(tags)
    task_index = 0
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for tag in tags:
        tag_part = safe_partition_value(tag)
        for trade_date in trade_dates:
            task_index += 1
            path = raw_dir / spec.api_name / f"tag={tag_part}" / f"trade_date={trade_date}.parquet"
            if should_skip_existing_partition(path, force=force):
                skipped += 1
                continue
            params = {"trade_date": trade_date, "tag": tag}
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            meta_params = dict(params)
            meta_params["tag_partition"] = tag_part
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            total_rows += write_board_result(path, result, spec, meta_params)
            total_pages += pages
            written += 1
            if task_index % 250 == 0:
                print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def download_board_limit_list_ths(client: TuShareClient, raw_dir: Path, spec: BoardTradingDataset, trade_dates: list[str], force: bool, page_limit: int, limit_types: list[str]) -> None:
    total_tasks = len(trade_dates) * len(limit_types)
    task_index = 0
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for limit_type in limit_types:
        limit_type_part = safe_partition_value(limit_type)
        for trade_date in trade_dates:
            task_index += 1
            path = raw_dir / spec.api_name / f"limit_type={limit_type_part}" / f"trade_date={trade_date}.parquet"
            if should_skip_existing_partition(path, force=force):
                skipped += 1
                continue
            params = {"trade_date": trade_date, "limit_type": limit_type}
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            meta_params = dict(params)
            meta_params["limit_type_partition"] = limit_type_part
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            total_rows += write_board_result(path, result, spec, meta_params)
            total_pages += pages
            written += 1
            if task_index % 250 == 0:
                print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def download_board_hot_by_market(
    client: TuShareClient,
    raw_dir: Path,
    spec: BoardTradingDataset,
    trade_dates: list[str],
    force: bool,
    page_limit: int,
    markets: list[str],
    is_new_values: list[str],
) -> None:
    total_tasks = len(trade_dates) * len(markets) * len(is_new_values)
    task_index = 0
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for market in markets:
        market_part = safe_partition_value(market)
        for is_new in is_new_values:
            for trade_date in trade_dates:
                task_index += 1
                path = raw_dir / spec.api_name / f"market={market_part}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet"
                if should_skip_existing_partition(path, force=force):
                    skipped += 1
                    continue
                params = {"trade_date": trade_date, "market": market, "is_new": is_new}
                result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
                meta_params = dict(params)
                meta_params["market_partition"] = market_part
                meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
                total_rows += write_board_result(path, result, spec, meta_params)
                total_pages += pages
                written += 1
                if task_index % 250 == 0:
                    print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def download_board_dc_hot(
    client: TuShareClient,
    raw_dir: Path,
    spec: BoardTradingDataset,
    trade_dates: list[str],
    force: bool,
    page_limit: int,
    markets: list[str],
    hot_types: list[str],
    is_new_values: list[str],
) -> None:
    total_tasks = len(trade_dates) * len(markets) * len(hot_types) * len(is_new_values)
    task_index = 0
    written = 0
    skipped = 0
    total_rows = 0
    total_pages = 0
    for market in markets:
        market_part = safe_partition_value(market)
        for hot_type in hot_types:
            hot_type_part = safe_partition_value(hot_type)
            for is_new in is_new_values:
                for trade_date in trade_dates:
                    task_index += 1
                    path = raw_dir / spec.api_name / f"market={market_part}" / f"hot_type={hot_type_part}" / f"is_new={is_new}" / f"trade_date={trade_date}.parquet"
                    if should_skip_existing_partition(path, force=force):
                        skipped += 1
                        continue
                    params = {"trade_date": trade_date, "market": market, "hot_type": hot_type, "is_new": is_new}
                    result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
                    meta_params = dict(params)
                    meta_params["market_partition"] = market_part
                    meta_params["hot_type_partition"] = hot_type_part
                    meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
                    total_rows += write_board_result(path, result, spec, meta_params)
                    total_pages += pages
                    written += 1
                    if task_index % 250 == 0:
                        print(f"{spec.api_name} {task_index}/{total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{spec.api_name} done tasks={total_tasks} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")

def query_share_float_to_path(client: TuShareClient, raw_dir: Path, path: Path, params: dict[str, Any], source: str, force: bool) -> dict[str, Any]:
    if path.exists() and not force:
        rows = parquet_rows(path)
        return {"path": str(path), "rows": rows, "skipped": True, "source_cap_risk": rows >= SHARE_FLOAT_ROW_LIMIT}
    result = client.query("share_float", params, SHARE_FLOAT_FIELDS)
    meta_params = dict(params)
    meta_params["download_path"] = source
    meta_params["row_limit"] = SHARE_FLOAT_ROW_LIMIT
    rows = write_event_result(path, result, EVENT_FLOW_SPECS["share_float"], meta_params)
    return {"path": str(path), "rows": rows, "skipped": False, "source_cap_risk": rows >= SHARE_FLOAT_ROW_LIMIT}

def download_share_float_ann_dates(client: TuShareClient, raw_dir: Path, args: argparse.Namespace, report: dict[str, Any]) -> list[str]:
    days = date_range_days(args.ann_start_date, args.ann_end_date)
    limit_hits: list[str] = []
    written = 0
    skipped = 0
    total_rows = 0
    for index, day in enumerate(days, start=1):
        path = raw_dir / "share_float_ann_date" / f"ann_date={day}.parquet"
        result = query_share_float_to_path(client, raw_dir, path, {"ann_date": day}, "ann_date", args.force)
        written += 0 if result["skipped"] else 1
        skipped += 1 if result["skipped"] else 0
        total_rows += int(result["rows"])
        if result["source_cap_risk"]:
            limit_hits.append(day)
        if index % 250 == 0:
            print(f"share_float ann_date {index}/{len(days)} skipped={skipped} written={written} rows_seen={total_rows} limit_hits={len(limit_hits)}")
    report["ann_date"] = {
        "start_date": args.ann_start_date,
        "end_date": args.ann_end_date,
        "days": len(days),
        "written": written,
        "skipped": skipped,
        "rows_seen": total_rows,
        "limit_hit_days": limit_hits,
    }
    print(f"share_float ann_date done days={len(days)} skipped={skipped} written={written} rows_seen={total_rows} limit_hits={len(limit_hits)}")
    return limit_hits

def download_share_float_ts_code_rescue_by_date(
    client: TuShareClient,
    raw_dir: Path,
    date_codes: dict[str, list[str]],
    *,
    date_param: str,
    dataset_dir: str,
    source: str,
    force: bool,
) -> dict[str, Any]:
    written = 0
    skipped = 0
    total_rows = 0
    limit_hits: list[dict[str, Any]] = []
    no_candidate_dates = sorted(date for date, codes in date_codes.items() if not codes)
    total_tasks = sum(len(codes) for codes in date_codes.values())
    task_index = 0
    for day in sorted(date_codes):
        for code in date_codes[day]:
            task_index += 1
            path = raw_dir / dataset_dir / f"{date_param}={day}" / f"ts_code={code}.parquet"
            result = query_share_float_to_path(client, raw_dir, path, {date_param: day, "ts_code": code}, source, force)
            written += 0 if result["skipped"] else 1
            skipped += 1 if result["skipped"] else 0
            total_rows += int(result["rows"])
            if result["source_cap_risk"]:
                limit_hits.append({"date": day, "ts_code": code, "rows": int(result["rows"])})
            if task_index % 500 == 0:
                print(
                    f"share_float {source} {task_index}/{total_tasks} skipped={skipped} "
                    f"written={written} rows_seen={total_rows} limit_hits={len(limit_hits)}"
                )
    return {
        "date_param": date_param,
        "dates": sorted(date_codes),
        "date_candidate_counts": {date: len(codes) for date, codes in sorted(date_codes.items())},
        "no_candidate_dates": no_candidate_dates,
        "tasks": total_tasks,
        "written": written,
        "skipped": skipped,
        "rows_seen": total_rows,
        "limit_hits": limit_hits,
    }

def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result

def explicit_rescue_stock_codes(args: argparse.Namespace) -> list[str]:
    requested: list[str] = []
    requested.extend(getattr(args, "rescue_code", []) or [])
    codes_file = getattr(args, "rescue_codes_file", None)
    if codes_file:
        text = Path(codes_file).read_text(encoding="utf-8")
        requested.extend(re.split(r"[\s,]+", text))
    return unique_ordered(requested)

def selected_share_float_rescue_dates(args: argparse.Namespace, ann_limit_hits: list[str]) -> tuple[list[str], list[str]]:
    explicit = unique_ordered(getattr(args, "rescue_ann_date", []) or [])
    auto: list[str] = []
    skipped_auto: list[str] = []
    if args.rescue_ann_limit_hits:
        auto = ann_limit_hits[: args.max_ann_rescue_days]
        skipped_auto = ann_limit_hits[args.max_ann_rescue_days :]
    elif ann_limit_hits:
        skipped_auto = ann_limit_hits
    return unique_ordered(explicit + auto), skipped_auto

def enforce_share_float_rescue_date_code_budget(args: argparse.Namespace, ann_date_codes: dict[str, list[str]], float_date_codes: dict[str, list[str]]) -> int:
    estimated_calls = sum(len(codes) for codes in ann_date_codes.values()) + sum(len(codes) for codes in float_date_codes.values())
    if args.max_rescue_calls is not None and estimated_calls > args.max_rescue_calls:
        raise RuntimeError(
            f"share_float rescue would make {estimated_calls} calls "
            f"({len(ann_date_codes)} ann_date dates and {len(float_date_codes)} float_date dates), "
            f"exceeding --max-rescue-calls={args.max_rescue_calls}"
        )
    return estimated_calls

def read_ts_codes_from_parquet(path: Path, *, extra_filter: tuple[str, str] | None = None) -> list[str]:
    if not path.exists() or parquet_rows(path) == 0:
        return []
    columns = ["ts_code"]
    if extra_filter:
        columns.append(extra_filter[0])
    try:
        df = pd.read_parquet(path, columns=list(dict.fromkeys(columns)))
    except Exception:
        return []
    if extra_filter and extra_filter[0] in df.columns:
        df = df[df[extra_filter[0]].astype(str).str.strip() == extra_filter[1]]
    if "ts_code" not in df.columns:
        return []
    return unique_ordered(df["ts_code"].dropna().astype(str).str.strip().tolist())

def share_float_self_candidate_codes(raw_dir: Path, date_param: str, dates: list[str]) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {date: [] for date in dates}
    for day in dates:
        if date_param == "ann_date":
            path = raw_dir / "share_float_ann_date" / f"ann_date={day}.parquet"
            candidates[day].extend(read_ts_codes_from_parquet(path))
        elif date_param == "float_date":
            path = raw_dir / "share_float" / f"date={day}.parquet"
            candidates[day].extend(read_ts_codes_from_parquet(path))
        else:
            raise RuntimeError(f"unsupported share_float candidate date_param {date_param}")
        candidates[day] = unique_ordered(candidates[day])
    return candidates

def anns_unlock_candidate_codes(raw_dir: Path, ann_dates: list[str]) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {date: [] for date in ann_dates}
    dates_by_month: dict[str, set[str]] = {}
    for day in ann_dates:
        dates_by_month.setdefault(day[:6], set()).add(day)
    for month, days in sorted(dates_by_month.items()):
        path = raw_dir / "anns_d" / f"month={month}.parquet"
        if not path.exists() or parquet_rows(path) == 0:
            continue
        columns = ["ann_date", "ts_code", "title"]
        try:
            df = pd.read_parquet(path, columns=columns)
        except Exception:
            continue
        df["ann_date"] = df["ann_date"].astype(str).str.strip()
        mask = df["ann_date"].isin(days) & df["title"].fillna("").astype(str).str.contains(SHARE_FLOAT_UNLOCK_TITLE_PATTERN)
        for day, group in df.loc[mask].groupby("ann_date"):
            candidates.setdefault(str(day), []).extend(group["ts_code"].dropna().astype(str).str.strip().tolist())
    return {date: unique_ordered(codes) for date, codes in candidates.items()}

def share_float_float_path_ann_candidate_codes(raw_dir: Path, ann_dates: list[str], float_start_date: str, float_end_date: str) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {date: [] for date in ann_dates}
    target_dates = set(ann_dates)
    for day in date_range_days(float_start_date, float_end_date):
        path = raw_dir / "share_float" / f"date={day}.parquet"
        if not path.exists() or parquet_rows(path) == 0:
            continue
        try:
            df = pd.read_parquet(path, columns=["ann_date", "ts_code"])
        except Exception:
            continue
        df["ann_date"] = df["ann_date"].astype(str).str.strip()
        mask = df["ann_date"].isin(target_dates)
        for ann_date, group in df.loc[mask].groupby("ann_date"):
            candidates.setdefault(str(ann_date), []).extend(group["ts_code"].dropna().astype(str).str.strip().tolist())
    return {date: unique_ordered(codes) for date, codes in candidates.items()}

def share_float_ann_path_float_candidate_codes(raw_dir: Path, float_dates: list[str], ann_start_date: str, ann_end_date: str) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {date: [] for date in float_dates}
    target_dates = set(float_dates)
    for day in date_range_days(ann_start_date, ann_end_date):
        path = raw_dir / "share_float_ann_date" / f"ann_date={day}.parquet"
        if not path.exists() or parquet_rows(path) == 0:
            continue
        try:
            df = pd.read_parquet(path, columns=["float_date", "ts_code"])
        except Exception:
            continue
        df["float_date"] = df["float_date"].astype(str).str.strip()
        mask = df["float_date"].isin(target_dates)
        for float_date, group in df.loc[mask].groupby("float_date"):
            candidates.setdefault(str(float_date), []).extend(group["ts_code"].dropna().astype(str).str.strip().tolist())
    return {date: unique_ordered(codes) for date, codes in candidates.items()}

def merge_candidate_maps(*maps: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for mapping in maps:
        for date_key, codes in mapping.items():
            merged.setdefault(date_key, []).extend(codes)
    return {date_key: unique_ordered(codes) for date_key, codes in merged.items()}

def select_share_float_rescue_date_codes(raw_dir: Path, args: argparse.Namespace, *, date_param: str, dates: list[str]) -> tuple[dict[str, list[str]], dict[str, Any]]:
    explicit = explicit_rescue_stock_codes(args)
    all_a_codes: list[str] = []
    detail: dict[str, Any] = {"date_param": date_param, "mode": args.rescue_universe, "dates": dates}
    if args.rescue_universe == "all_a":
        all_a_codes = load_stock_codes(raw_dir)
        if args.max_codes:
            all_a_codes = all_a_codes[: args.max_codes]
        return {date: all_a_codes for date in dates}, {**detail, "all_a_codes": len(all_a_codes)}
    if args.rescue_universe == "explicit":
        if not explicit:
            raise RuntimeError("--rescue-universe explicit requires --rescue-code or --rescue-codes-file")
        codes = explicit[: args.max_codes] if args.max_codes else explicit
        return {date: codes for date in dates}, {**detail, "explicit_codes": len(codes)}

    self_candidates = share_float_self_candidate_codes(raw_dir, date_param, dates)
    explicit_map = {date: explicit for date in dates}
    if date_param == "ann_date":
        anns_candidates = anns_unlock_candidate_codes(raw_dir, dates) if not args.no_anns_candidates else {date: [] for date in dates}
        cross_candidates = (
            share_float_float_path_ann_candidate_codes(raw_dir, dates, args.float_start_date, args.float_end_date)
            if not args.no_cross_path_candidates
            else {date: [] for date in dates}
        )
    else:
        anns_candidates = {date: [] for date in dates}
        cross_candidates = (
            share_float_ann_path_float_candidate_codes(raw_dir, dates, args.ann_start_date, args.ann_end_date)
            if not args.no_cross_path_candidates
            else {date: [] for date in dates}
        )
    candidates = merge_candidate_maps(self_candidates, anns_candidates, cross_candidates, explicit_map)
    if args.max_codes:
        candidates = {date: codes[: args.max_codes] for date, codes in candidates.items()}
    detail.update({
        "self_candidate_counts": {date: len(self_candidates.get(date, [])) for date in dates},
        "anns_candidate_counts": {date: len(anns_candidates.get(date, [])) for date in dates},
        "cross_path_candidate_counts": {date: len(cross_candidates.get(date, [])) for date in dates},
        "explicit_codes": len(explicit),
        "final_candidate_counts": {date: len(candidates.get(date, [])) for date in dates},
        "max_codes": args.max_codes,
    })
    return candidates, detail

def share_float_union_roots(raw_dir: Path) -> list[Path]:
    roots = [raw_dir]
    archive_root = Path.cwd().resolve() / "archive" / "data_raw"
    if archive_root.exists():
        for root in sorted(archive_root.iterdir()):
            if not root.is_dir():
                continue
            if any((root / name).exists() for name in (
                "share_float_ann_date",
                "share_float_ann_date_ts_code",
                "share_float",
                "share_float_float_date",
                "share_float_float_date_ts_code",
            )):
                roots.append(root)
    return roots

def share_float_union_files(raw_dir: Path, args: argparse.Namespace) -> list[tuple[Path, str]]:
    ann_start_date = getattr(args, "union_ann_start_date", None) or args.ann_start_date
    ann_end_date = getattr(args, "union_ann_end_date", None) or args.ann_end_date
    float_start_date = getattr(args, "union_float_start_date", None) or args.float_start_date
    float_end_date = getattr(args, "union_float_end_date", None) or args.float_end_date
    files: list[tuple[Path, str]] = []
    for root in share_float_union_roots(raw_dir):
        for day in date_range_days(ann_start_date, ann_end_date):
            path = root / "share_float_ann_date" / f"ann_date={day}.parquet"
            if path.exists():
                files.append((path, "ann_date"))
            rescue_dir = root / "share_float_ann_date_ts_code" / f"ann_date={day}"
            files.extend((path, "ann_date_ts_code") for path in sorted(rescue_dir.glob("ts_code=*.parquet")))
        if not args.skip_float_date_union:
            for day in date_range_days(float_start_date, float_end_date):
                for dirname in ("share_float", "share_float_float_date"):
                    path = root / dirname / f"date={day}.parquet"
                    if path.exists():
                        files.append((path, "float_date_existing"))
                rescue_dir = root / "share_float_float_date_ts_code" / f"float_date={day}"
                files.extend((path, "float_date_ts_code") for path in sorted(rescue_dir.glob("ts_code=*.parquet")))
    return files

def write_share_float_union(raw_dir: Path, args: argparse.Namespace, report: dict[str, Any]) -> None:
    output = (Path.cwd().resolve() / args.union_output).resolve()
    files = share_float_union_files(raw_dir, args)
    frames: list[pd.DataFrame] = []
    for path, source in files:
        if parquet_rows(path) == 0:
            continue
        df = pd.read_parquet(path)
        df["download_path"] = source
        df["source_file"] = str(path)
        df["source_cap_risk"] = parquet_rows(path) >= SHARE_FLOAT_ROW_LIMIT
        frames.append(df)
    union = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SHARE_FLOAT_FIELDS.split(","))
    before = len(union)
    key_columns = ["ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"]
    existing_key_columns = [col for col in key_columns if col in union.columns]
    if existing_key_columns and not union.empty:
        union = union.drop_duplicates(existing_key_columns).reset_index(drop=True)
    existing_rows = parquet_rows(output) if output.exists() else None
    allow_shrink = bool(getattr(args, "allow_union_shrink", False))
    if existing_rows is not None and len(union) < existing_rows and not allow_shrink:
        raise RuntimeError(
            "share_float_complete union rebuild would shrink "
            f"{output} from {existing_rows} to {len(union)} rows using {len(files)} input files; "
            "check active/archive process roots or pass --allow-union-shrink for an intentional rebuild."
        )
    write_parquet(
        output,
        union,
        api_name="share_float",
        params={
            "strategy": "ann_date_float_date_union",
            "ann_start_date": args.ann_start_date,
            "ann_end_date": args.ann_end_date,
            "float_start_date": args.float_start_date,
            "float_end_date": args.float_end_date,
            "union_ann_start_date": getattr(args, "union_ann_start_date", None) or args.ann_start_date,
            "union_ann_end_date": getattr(args, "union_ann_end_date", None) or args.ann_end_date,
            "union_float_start_date": getattr(args, "union_float_start_date", None) or args.float_start_date,
            "union_float_end_date": getattr(args, "union_float_end_date", None) or args.float_end_date,
            "input_files": len(files),
        },
        fields=list(union.columns),
        source_hash=stable_hash({"input_files": [str(path) for path, _ in files], "rows": len(union), "columns": list(union.columns)}),
    )
    report["union"] = {
        "output": str(output),
        "input_files": len(files),
        "rows_before_dedup": before,
        "rows_after_dedup": len(union),
        "previous_rows": existing_rows,
        "allow_union_shrink": allow_shrink,
    }

def download_share_float_complete(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    report: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_dir": str(raw_dir),
        "row_limit": SHARE_FLOAT_ROW_LIMIT,
        "scope": {
            "ann_start_date": args.ann_start_date,
            "ann_end_date": args.ann_end_date,
            "float_start_date": args.float_start_date,
            "float_end_date": args.float_end_date,
            "union_ann_start_date": getattr(args, "union_ann_start_date", None) or args.ann_start_date,
            "union_ann_end_date": getattr(args, "union_ann_end_date", None) or args.ann_end_date,
            "union_float_start_date": getattr(args, "union_float_start_date", None) or args.float_start_date,
            "union_float_end_date": getattr(args, "union_float_end_date", None) or args.float_end_date,
            "float_rescue_dates": args.float_rescue_date,
            "max_codes": args.max_codes,
        },
    }
    ann_limit_hits: list[str] = []
    if not args.skip_ann_date:
        ann_limit_hits = download_share_float_ann_dates(client, raw_dir, args, report)

    rescue_ann_dates, skipped_ann_rescue = selected_share_float_rescue_dates(args, ann_limit_hits)
    ann_date_codes: dict[str, list[str]] = {}
    float_date_codes: dict[str, list[str]] = {}
    if rescue_ann_dates or args.float_rescue_date:
        ann_detail: dict[str, Any] = {}
        float_detail: dict[str, Any] = {}
        if rescue_ann_dates:
            ann_date_codes, ann_detail = select_share_float_rescue_date_codes(raw_dir, args, date_param="ann_date", dates=rescue_ann_dates)
        if args.float_rescue_date:
            float_date_codes, float_detail = select_share_float_rescue_date_codes(raw_dir, args, date_param="float_date", dates=args.float_rescue_date)
        report["rescue_candidates"] = {"ann_date": ann_detail, "float_date": float_detail}
        report["rescue_estimated_calls"] = enforce_share_float_rescue_date_code_budget(args, ann_date_codes, float_date_codes)

    if rescue_ann_dates:
        report["ann_date_ts_code"] = download_share_float_ts_code_rescue_by_date(
            client,
            raw_dir,
            ann_date_codes,
            date_param="ann_date",
            dataset_dir="share_float_ann_date_ts_code",
            source="ann_date_ts_code",
            force=args.force,
        )
    report["ann_date_ts_code_skipped_limit_days"] = skipped_ann_rescue

    if args.float_rescue_date:
        report["float_date_ts_code"] = download_share_float_ts_code_rescue_by_date(
            client,
            raw_dir,
            float_date_codes,
            date_param="float_date",
            dataset_dir="share_float_float_date_ts_code",
            source="float_date_ts_code",
            force=args.force,
        )

    if args.write_union:
        write_share_float_union(raw_dir, args, report)

    if args.output:
        output = (repo_root / args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(f"share_float complete download process_report={output}")
    else:
        union = report.get("union") or {}
        print(
            "share_float complete download finished; "
            f"ann_limit_hits={len(report.get('ann_date', {}).get('limit_hit_days', []))} "
            f"ann_rescue_tasks={report.get('ann_date_ts_code', {}).get('tasks', 0)} "
            f"union_rows={union.get('rows_after_dedup', 'not_written')}; "
            "pass --output to write a process report"
        )
    return 0

def download_fundamental(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    datasets = selected_fundamental_datasets(args)
    stock_codes: list[str] = []
    if any(FUNDAMENTAL_SPECS[name].strategy == "ts_code" for name in datasets):
        stock_codes = load_stock_codes(raw_dir)
        if args.max_codes:
            stock_codes = stock_codes[: args.max_codes]
    periods = quarter_periods(args.start_date, args.end_date)
    windows = month_windows(args.start_date, args.end_date)
    for dataset in datasets:
        spec = FUNDAMENTAL_SPECS[dataset]
        if spec.strategy == "period":
            download_fundamental_period_dataset(client, raw_dir, spec, periods, args.force, args.page_limit)
        elif spec.strategy == "ann_month":
            download_fundamental_ann_month_dataset(client, raw_dir, spec, windows, args.force, args.page_limit)
        elif spec.strategy == "ts_code":
            download_fundamental_ts_code_dataset(client, raw_dir, spec, stock_codes, args.force, args.page_limit)
        else:
            raise RuntimeError(f"unsupported fundamental strategy {spec.strategy} for {dataset}")
    print(f"fundamental download finished under {raw_dir}")
    return 0

def download_fundamental_period_dataset(client: TuShareClient, raw_dir: Path, spec: FundamentalDataset, periods: list[str], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, period in enumerate(periods, start=1):
        path = raw_dir / spec.api_name / f"period={period}.parquet"
        if path.exists() and not force:
            skipped += 1
            continue
        params = {spec.period_param: period}
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        df = frame(result)
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=result.fields, source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        if index % 16 == 0:
            print(f"{spec.api_name} periods {index}/{len(periods)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done periods={len(periods)} skipped={skipped} written={written} rows_written={total_rows}")

def download_fundamental_ann_month_dataset(client: TuShareClient, raw_dir: Path, spec: FundamentalDataset, windows: list[tuple[str, str, str]], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, (start_date, end_date, ann_month) in enumerate(windows, start=1):
        path = raw_dir / spec.api_name / f"ann_month={ann_month}.parquet"
        params = {"start_date": start_date, "end_date": end_date}
        if should_skip_existing_partition(path, force=force, requested_params=params):
            skipped += 1
            continue
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        df = frame(result)
        meta_params = dict(params)
        meta_params["ann_month"] = ann_month
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=result.fields, source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        if index % 24 == 0:
            print(f"{spec.api_name} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows}")

def download_fundamental_ts_code_dataset(client: TuShareClient, raw_dir: Path, spec: FundamentalDataset, stock_codes: list[str], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, ts_code in enumerate(stock_codes, start=1):
        path = raw_dir / spec.api_name / f"ts_code={ts_code}.parquet"
        if path.exists() and not force:
            skipped += 1
            if index % 500 == 0:
                print(f"{spec.api_name} codes {index}/{len(stock_codes)} skipped={skipped} written={written}")
            continue
        params = {"ts_code": ts_code}
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        df = frame(result)
        meta_params = dict(params)
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=result.fields, source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        if index % 500 == 0:
            print(f"{spec.api_name} codes {index}/{len(stock_codes)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done codes={len(stock_codes)} skipped={skipped} written={written} rows_written={total_rows}")

def download_intraday(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    datasets = selected_intraday_datasets(args)
    if STK_MINS_DATASET not in datasets:
        print("intraday minute download finished: no supported dataset selected")
        return 0
    universe = load_minute_universe(raw_dir, args)
    tasks: list[tuple[str, int, str, str]] = []
    for _, row in universe.iterrows():
        ts_code = str(row["ts_code"])
        for year, year_start, year_end in active_year_windows(row, args.start_date, args.end_date):
            tasks.append((ts_code, year, year_start, year_end))
    if not tasks:
        raise RuntimeError(f"no active stock-year windows for {args.start_date}-{args.end_date}")
    page_limit = min(args.page_limit or STK_MINS_PAGE_LIMIT, STK_MINS_PAGE_LIMIT)
    dataset_dir = raw_dir / STK_MINS_DATASET
    skipped = 0
    written = 0
    total_rows = 0
    total_pages = 0
    for index, (ts_code, year, year_start, year_end) in enumerate(tasks, start=1):
        path = dataset_dir / f"ts_code={safe_partition_value(ts_code)}" / f"year={year}.parquet"
        if path.exists() and not args.force:
            skipped += 1
            if index % 250 == 0:
                print(f"{STK_MINS_DATASET} {index}/{len(tasks)} skipped={skipped} written={written} rows_written={total_rows}")
            continue
        params = {
            "ts_code": ts_code,
            "freq": STK_MINS_FREQ,
            "start_date": minute_datetime(year_start),
            "end_date": minute_datetime(year_end, end=True),
        }
        try:
            result, pages = query_paged(client, STK_MINS_API_NAME, params, STK_MINS_FIELDS, page_limit)
        except Exception as exc:
            raise RuntimeError(f"{STK_MINS_API_NAME} ts_code={ts_code} year={year} failed: {exc}") from exc
        df = augment_stk_mins_frame(frame(result))
        meta_params = dict(params)
        meta_params.update({
            "dataset": STK_MINS_DATASET,
            "official_page_limit": STK_MINS_PAGE_LIMIT,
            "pagination": {"page_limit": page_limit, "pages": pages},
            "unit_rules": {"vol": "shares", "amount": "CNY", "trade_time": "Asia/Shanghai minute/bar timestamp; includes 09:30 and 15:00 auction bars"},
        })
        write_parquet(path, df, api_name=STK_MINS_API_NAME, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        total_pages += pages
        if index % 50 == 0:
            print(f"{STK_MINS_DATASET} {index}/{len(tasks)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"{STK_MINS_DATASET} done tasks={len(tasks)} skipped={skipped} written={written} rows_written={total_rows} pages={total_pages}")
    print(f"intraday minute download finished under {raw_dir}")
    return 0

def read_stk_mins_source_subset(path: Path, trade_dates: set[str]) -> pd.DataFrame:
    if not trade_dates:
        return pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
    columns = [col for col in STK_MINS_REQUIRED_COLUMNS if col in pq.ParquetFile(path).schema_arrow.names]
    try:
        df = pd.read_parquet(path, columns=columns, filters=[("trade_date", "in", sorted(trade_dates))])
    except Exception:
        df = pd.read_parquet(path, columns=columns)
    if df.empty or "trade_date" not in df.columns:
        return pd.DataFrame(columns=columns)
    return df[df["trade_date"].astype(str).isin(trade_dates)].copy()

def source_stk_mins_files(raw_dir: Path, args: argparse.Namespace, years: set[str]) -> list[Path]:
    files = sorted((raw_dir / STK_MINS_DATASET).glob("ts_code=*/year=*.parquet"))
    if years:
        files = [path for path in files if path.stem.split("=", 1)[-1] in years]
    if getattr(args, "codes", None):
        wanted = {safe_partition_value(str(code).strip()) for code in args.codes if str(code).strip()}
        files = [path for path in files if path.parent.name.split("=", 1)[-1] in wanted]
    if getattr(args, "max_codes", None):
        selected_codes = sorted({path.parent.name for path in files})[: int(args.max_codes)]
        files = [path for path in files if path.parent.name in set(selected_codes)]
    return files

def write_stk_mins_by_date(
    path: Path,
    df: pd.DataFrame,
    *,
    trade_date: str,
    source: str,
    params: dict[str, Any],
) -> None:
    meta_params = dict(params)
    meta_params.update({
        "dataset": STK_MINS_BY_DATE_DATASET,
        "trade_date": trade_date,
        "source": source,
        "unit_rules": {"vol": "shares", "amount": "CNY", "available_at": "source trade_time bar close"},
    })
    source_hash = stable_hash({
        "dataset": STK_MINS_BY_DATE_DATASET,
        "trade_date": trade_date,
        "source": source,
        "rows": int(len(df)),
        "unique_codes": int(df["ts_code"].nunique()) if "ts_code" in df.columns else 0,
        "params": meta_params,
    })
    write_parquet(path, df, api_name=STK_MINS_API_NAME, params=meta_params, fields=list(df.columns), source_hash=source_hash)

def compact_intraday_by_date(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    output_dataset = args.output_dataset
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    month_to_dates: dict[str, list[str]] = {}
    for trade_date in trade_dates:
        month_to_dates.setdefault(trade_date[:6], []).append(trade_date)
    years = {trade_date[:4] for trade_date in trade_dates}
    source_files = source_stk_mins_files(raw_dir, args, years)
    if not source_files:
        raise RuntimeError(f"no source {STK_MINS_DATASET} files found for years={sorted(years)}")
    written = 0
    skipped = 0
    total_rows = 0
    for month, month_dates in sorted(month_to_dates.items()):
        needed_dates = []
        for trade_date in month_dates:
            path = stk_mins_by_date_path(raw_dir, output_dataset, trade_date)
            if path.exists() and not args.force:
                skipped += 1
            else:
                needed_dates.append(trade_date)
        if not needed_dates:
            continue
        date_set = set(needed_dates)
        buffers: dict[str, list[pd.DataFrame]] = {trade_date: [] for trade_date in needed_dates}
        scanned = 0
        for source_path in source_files:
            if source_path.stem.split("=", 1)[-1] not in {date[:4] for date in date_set}:
                continue
            subset = read_stk_mins_source_subset(source_path, date_set)
            scanned += 1
            if subset.empty:
                continue
            for trade_date, group in subset.groupby("trade_date", sort=False):
                key = str(trade_date)
                if key in buffers:
                    buffers[key].append(group)
        for trade_date in needed_dates:
            if not buffers[trade_date]:
                if args.allow_empty:
                    combined = pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
                else:
                    raise RuntimeError(f"no minute rows found while compacting trade_date={trade_date}")
            else:
                combined = pd.concat(buffers[trade_date], ignore_index=True)
            normalized, normalize_details = normalize_stk_mins_by_date_frame(combined, trade_date)
            expected_codes = intraday_expected_codes_for_day(raw_dir, args, trade_date)
            ok, details = validate_stk_mins_by_date_frame(
                normalized,
                trade_date,
                expected_codes=expected_codes,
                min_rows=args.min_rows_per_day,
                allow_missing_codes=args.allow_missing_codes,
            )
            if not ok and not args.allow_validation_warnings:
                raise RuntimeError(f"compacted intraday by-date validation failed for {trade_date}: {details}")
            params = {
                "source_dataset": STK_MINS_DATASET,
                "source_layout": "ts_code/year",
                "output_layout": "trade_date",
                "month": month,
                "source_files_scanned": scanned,
                "normalize": normalize_details,
                "validation": details,
            }
            write_stk_mins_by_date(
                stk_mins_by_date_path(raw_dir, output_dataset, trade_date),
                normalized,
                trade_date=trade_date,
                source="compact_from_stock_year",
                params=params,
            )
            written += 1
            total_rows += len(normalized)
        print(f"{output_dataset} month={month} written={written} skipped={skipped} rows_written={total_rows}")
    print(f"{output_dataset} compact finished dates={len(trade_dates)} written={written} skipped={skipped} rows_written={total_rows}")
    return 0

def update_intraday_by_date(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = (repo_root / args.raw_dir).resolve()
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    trade_dates = load_sse_open_dates(raw_dir, args.start_date, args.end_date)
    page_limit = min(args.page_limit or STK_MINS_PAGE_LIMIT, STK_MINS_PAGE_LIMIT)
    written = 0
    skipped = 0
    total_rows = 0
    for trade_date in trade_dates:
        path = stk_mins_by_date_path(raw_dir, args.output_dataset, trade_date)
        if path.exists() and not args.force:
            existing = pd.read_parquet(path)
            if args.expected_codes_source == "minute" and not existing.empty:
                expected_codes = set(existing["ts_code"].dropna().astype(str))
                if getattr(args, "max_codes", None):
                    expected_codes = set(sorted(expected_codes)[: int(args.max_codes)])
            else:
                expected_codes = intraday_expected_codes_for_day(raw_dir, args, trade_date) or set()
            existing_allow_missing = max(int(args.allow_missing_codes), int(getattr(args, "existing_allow_missing_codes", 0)))
            ok, _ = validate_stk_mins_by_date_frame(
                existing,
                trade_date,
                expected_codes=expected_codes if expected_codes else None,
                min_rows=args.min_rows_per_day,
                allow_missing_codes=existing_allow_missing,
            )
            if ok:
                skipped += 1
                continue
        else:
            expected_codes = intraday_expected_codes_for_day(raw_dir, args, trade_date) or set()
        if not expected_codes:
            skipped += 1
            print(f"{args.output_dataset} trade_date={trade_date} skipped_empty_expected_codes")
            continue
        collected: dict[str, pd.DataFrame] = {}
        pending = sorted(expected_codes)
        pages_by_code: dict[str, int] = {}
        for attempt in range(1, args.max_retries + 1):
            if not pending:
                break
            failed: list[str] = []
            for index, ts_code in enumerate(pending, start=1):
                params = {
                    "ts_code": ts_code,
                    "freq": STK_MINS_FREQ,
                    "start_date": minute_datetime(trade_date),
                    "end_date": minute_datetime(trade_date, end=True),
                }
                try:
                    result, pages = query_paged(client, STK_MINS_API_NAME, params, STK_MINS_FIELDS, page_limit)
                    df = augment_stk_mins_frame(frame(result))
                    df = df[df["trade_date"].astype(str) == trade_date].copy()
                    if df.empty:
                        failed.append(ts_code)
                    else:
                        collected[ts_code] = df
                        pages_by_code[ts_code] = pages
                except Exception:
                    failed.append(ts_code)
                if index % 500 == 0:
                    print(f"{trade_date} attempt={attempt}/{args.max_retries} codes={index}/{len(pending)} collected={len(collected)} failed_current={len(failed)}")
            pending = failed
            if pending and attempt < args.max_retries:
                time.sleep(args.retry_delay_seconds)
        if len(pending) > args.allow_missing_codes:
            raise RuntimeError(f"{trade_date}: {len(pending)} minute codes still missing after retries; sample={pending[:20]}")
        combined = pd.concat(collected.values(), ignore_index=True) if collected else pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
        normalized, normalize_details = normalize_stk_mins_by_date_frame(combined, trade_date)
        if expected_codes and normalized.empty:
            raise RuntimeError(
                f"{trade_date}: refusing to write zero-row intraday by-date file "
                f"for nonempty expected universe ({len(expected_codes)} codes)"
            )
        ok, details = validate_stk_mins_by_date_frame(
            normalized,
            trade_date,
            expected_codes=expected_codes,
            min_rows=args.min_rows_per_day,
            allow_missing_codes=args.allow_missing_codes,
        )
        if not ok and not args.allow_validation_warnings:
            raise RuntimeError(f"{trade_date}: intraday by-date update validation failed: {details}")
        params = {
            "output_layout": "trade_date",
            "expected_codes_source": args.expected_codes_source,
            "expected_codes": len(expected_codes),
            "missing_codes_after_retry": len(pending),
            "missing_code_sample": pending[:20],
            "normalize": normalize_details,
            "validation": details,
            "pagination": {"page_limit": page_limit, "pages_total": int(sum(pages_by_code.values()))},
            "max_retries": args.max_retries,
        }
        write_stk_mins_by_date(path, normalized, trade_date=trade_date, source="daily_incremental_update", params=params)
        written += 1
        total_rows += len(normalized)
        print(f"{args.output_dataset} trade_date={trade_date} written rows={len(normalized)} missing_codes={len(pending)}")
    print(f"{args.output_dataset} update finished dates={len(trade_dates)} written={written} skipped={skipped} rows_written={total_rows}")
    return 0

def download_text(args: argparse.Namespace) -> int:
    repo_root = Path.cwd().resolve()
    raw_dir = repo_root / args.raw_dir
    client = TuShareClient(load_token(repo_root), args.min_interval_seconds, args.timeout_seconds)
    windows = month_windows(args.start_date, args.end_date)
    days = date_range_days(args.start_date, args.end_date)
    for dataset in selected_text_datasets(args):
        spec = TEXT_SPECS[dataset]
        start_date = max(args.start_date, spec.start_date)
        dataset_windows = [(s, e, m) for s, e, m in windows if e >= start_date]
        dataset_days = [d for d in days if d >= start_date]
        page_limit = text_page_limit(spec, args.page_limit)
        if spec.strategy == "range_month":
            download_text_range_month(client, raw_dir, spec, dataset_windows, args.force, page_limit)
        elif spec.strategy == "time_range_month":
            download_text_time_range_month(client, raw_dir, spec, dataset_windows, args.force, page_limit, args.major_news_src)
        elif spec.strategy == "news_src_month":
            download_text_news_src_month(client, raw_dir, spec, dataset_windows, args.force, page_limit, args.news_src)
        elif spec.strategy == "news_src_day":
            download_text_news_src_day(client, raw_dir, spec, dataset_days, args.force, page_limit, args.news_src)
        elif spec.strategy == "day":
            download_text_day(client, raw_dir, spec, dataset_days, args.force)
        else:
            raise RuntimeError(f"unsupported text strategy {spec.strategy} for {dataset}")
    print(f"Text download finished under {raw_dir}")
    return 0

def download_text_range_month(client: TuShareClient, raw_dir: Path, spec: TextDataset, windows: list[tuple[str, str, str]], force: bool, page_limit: int) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, (start_date, end_date, month) in enumerate(windows, start=1):
        path = raw_dir / spec.api_name / f"month={month}.parquet"
        params = {"start_date": start_date, "end_date": end_date}
        if should_skip_existing_partition(path, force=force, requested_params=params):
            skipped += 1
            continue
        result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
        df = augment_text_frame(frame(result), spec)
        meta_params = dict(params)
        meta_params["month"] = month
        meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
        write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        if index % 24 == 0:
            print(f"{spec.api_name} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows}")

def download_text_time_range_month(
    client: TuShareClient,
    raw_dir: Path,
    spec: TextDataset,
    windows: list[tuple[str, str, str]],
    force: bool,
    page_limit: int,
    sources: list[str],
) -> None:
    source_values = sources or [""]
    for source in source_values:
        source_suffix = f"src={safe_partition_value(source)}" if source else "src=all"
        written = 0
        skipped = 0
        total_rows = 0
        for index, (start_date, end_date, month) in enumerate(windows, start=1):
            path = raw_dir / spec.api_name / source_suffix / f"month={month}.parquet"
            params = {"start_date": as_datetime_window(start_date), "end_date": as_datetime_window(end_date, end=True)}
            if source:
                params["src"] = source
            if should_skip_existing_partition(path, force=force, requested_params=params):
                skipped += 1
                continue
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            df = augment_text_frame(frame(result), spec)
            meta_params = dict(params)
            meta_params["month"] = month
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
            written += 1
            total_rows += len(df)
            if index % 24 == 0:
                print(f"{spec.api_name}/{source_suffix} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows}")
        print(f"{spec.api_name}/{source_suffix} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows}")

def download_text_news_src_month(
    client: TuShareClient,
    raw_dir: Path,
    spec: TextDataset,
    windows: list[tuple[str, str, str]],
    force: bool,
    page_limit: int,
    sources: list[str],
) -> None:
    for source in selected_news_sources(sources):
        source_suffix = f"src={safe_partition_value(source)}"
        written = 0
        skipped = 0
        total_rows = 0
        for index, (start_date, end_date, month) in enumerate(windows, start=1):
            path = raw_dir / spec.api_name / source_suffix / f"month={month}.parquet"
            params = {"src": source, "start_date": as_datetime_window(start_date), "end_date": as_datetime_window(end_date, end=True)}
            if should_skip_existing_partition(path, force=force, requested_params=params):
                skipped += 1
                continue
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            df = augment_text_frame(frame(result), spec)
            meta_params = dict(params)
            meta_params["month"] = month
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
            written += 1
            total_rows += len(df)
            if index % 24 == 0:
                print(f"{spec.api_name}/{source_suffix} months {index}/{len(windows)} skipped={skipped} written={written} rows_written={total_rows}")
        print(f"{spec.api_name}/{source_suffix} done months={len(windows)} skipped={skipped} written={written} rows_written={total_rows}")

def download_text_news_src_day(
    client: TuShareClient,
    raw_dir: Path,
    spec: TextDataset,
    days: list[str],
    force: bool,
    page_limit: int,
    sources: list[str],
) -> None:
    for source in selected_news_sources(sources):
        source_suffix = f"src={safe_partition_value(source)}"
        written = 0
        skipped = 0
        total_rows = 0
        for index, day in enumerate(days, start=1):
            path = raw_dir / spec.api_name / source_suffix / f"date={day}.parquet"
            params = {"src": source, "start_date": as_datetime_window(day), "end_date": as_datetime_window(day, end=True)}
            if should_skip_existing_partition(path, force=force, requested_params=params):
                skipped += 1
                continue
            result, pages = query_paged(client, spec.api_name, params, spec.fields, page_limit)
            df = augment_text_frame(frame(result), spec)
            meta_params = dict(params)
            meta_params["date"] = day
            meta_params["pagination"] = {"page_limit": page_limit, "pages": pages}
            write_parquet(path, df, api_name=spec.api_name, params=meta_params, fields=list(df.columns), source_hash=result.source_hash)
            written += 1
            total_rows += len(df)
            if index % 250 == 0:
                print(f"{spec.api_name}/{source_suffix} days {index}/{len(days)} skipped={skipped} written={written} rows_written={total_rows}")
        print(f"{spec.api_name}/{source_suffix} done days={len(days)} skipped={skipped} written={written} rows_written={total_rows}")

def download_text_day(client: TuShareClient, raw_dir: Path, spec: TextDataset, days: list[str], force: bool) -> None:
    written = 0
    skipped = 0
    total_rows = 0
    for index, day in enumerate(days, start=1):
        path = raw_dir / spec.api_name / f"date={day}.parquet"
        if path.exists() and not force:
            skipped += 1
            continue
        params = {"date": day}
        result = client.query(spec.api_name, params, spec.fields)
        df = augment_text_frame(frame(result), spec)
        write_parquet(path, df, api_name=spec.api_name, params=params, fields=list(df.columns), source_hash=result.source_hash)
        written += 1
        total_rows += len(df)
        if index % 250 == 0:
            print(f"{spec.api_name} days {index}/{len(days)} skipped={skipped} written={written} rows_written={total_rows}")
    print(f"{spec.api_name} done days={len(days)} skipped={skipped} written={written} rows_written={total_rows}")

def set_download_defaults(args: argparse.Namespace) -> None:
    if args.min_interval_seconds is None:
        args.min_interval_seconds = {
            "reference": 0.12,
            "daily": 0.18,
            "fundamental": 0.22,
            "intraday": 0.22,
            "event_flow": 0.22,
            "board_trading": 0.22,
            "text_evidence": 0.22,
            "macro": 0.22,
            "global": 0.22,
        }[args.tier]
    if args.timeout_seconds is None:
        args.timeout_seconds = 120 if args.tier == "intraday" else 90 if args.tier in {"fundamental", "event_flow", "board_trading", "text_evidence", "macro", "global"} else 60
    if args.page_limit is None:
        if args.tier == "intraday":
            args.page_limit = STK_MINS_PAGE_LIMIT
        elif args.tier == "text_evidence":
            args.page_limit = None
        elif args.tier == "fundamental":
            args.page_limit = 7000
        elif args.tier == "daily":
            args.page_limit = TRADE_DATE_PAGE_LIMIT
        elif args.tier == "board_trading":
            args.page_limit = None
        else:
            args.page_limit = 10000

def download_selected_tier(args: argparse.Namespace) -> int:
    set_download_defaults(args)
    if args.tier == "reference":
        return download_reference(args)
    if args.tier == "daily":
        return download_daily(args)
    if args.tier == "fundamental":
        return download_fundamental(args)
    if args.tier == "intraday":
        return download_intraday(args)
    if args.tier == "event_flow":
        return download_event_flow(args)
    if args.tier == "board_trading":
        return download_board_trading(args)
    if args.tier == "text_evidence":
        return download_text(args)
    if args.tier in {"macro", "global"}:
        return download_macro(args)
    raise RuntimeError(f"unknown download tier {args.tier}")

def ns_from(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(overrides)
    return argparse.Namespace(**values)

def sidecar_params(path: Path) -> dict[str, Any]:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("params") or {}
    except Exception:
        return {}

def normalized_coverage_bound(value: Any, *, end: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if re.fullmatch(r"\d{8}", digits):
        return f"{digits}{'235959' if end else '000000'}"
    if re.fullmatch(r"\d{14}", digits):
        return digits
    if len(digits) > 14:
        return digits[:14]
    return ""

def existing_partition_covers_request(path: Path, requested_params: dict[str, Any] | None = None) -> bool:
    if not path.exists():
        return False
    requested_params = requested_params or {}
    if "start_date" in requested_params and "end_date" in requested_params:
        existing = sidecar_params(path)
        existing_start = normalized_coverage_bound(existing.get("start_date"), end=False)
        existing_end = normalized_coverage_bound(existing.get("end_date"), end=True)
        requested_start = normalized_coverage_bound(requested_params.get("start_date"), end=False)
        requested_end = normalized_coverage_bound(requested_params.get("end_date"), end=True)
        return bool(existing_start and existing_end and existing_start <= requested_start and existing_end >= requested_end)
    return True

def should_skip_existing_partition(path: Path, *, force: bool, requested_params: dict[str, Any] | None = None) -> bool:
    return not force and existing_partition_covers_request(path, requested_params)

def run_update_step(label: str, fn, args: argparse.Namespace, summary: list[dict[str, Any]]) -> None:
    print(f"update step start: {label}")
    started = time.monotonic()
    code = fn(args)
    elapsed = round(time.monotonic() - started, 3)
    summary.append({"step": label, "exit_code": int(code), "elapsed_seconds": elapsed})
    if code:
        raise RuntimeError(f"update step failed: {label} exit_code={code}")
    print(f"update step done: {label} elapsed_seconds={elapsed}")

def update_share_float_complete_data(
    args: argparse.Namespace,
    summary: list[dict[str, Any]],
    *,
    start_date: str,
    force: bool,
) -> None:
    run_update_step(
        "share_float_complete",
        download_share_float_complete,
        ns_from(
            args,
            ann_start_date=start_date,
            ann_end_date=args.end_date,
            float_start_date=start_date,
            float_end_date=args.end_date,
            union_ann_start_date="20100101",
            union_ann_end_date=args.end_date,
            union_float_start_date="20200101",
            union_float_end_date=args.end_date,
            skip_ann_date=False,
            rescue_ann_limit_hits=args.rescue_ann_limit_hits,
            rescue_ann_date=[],
            max_ann_rescue_days=args.max_ann_rescue_days,
            float_rescue_date=[],
            rescue_universe=args.rescue_universe,
            rescue_code=[],
            rescue_codes_file=None,
            no_anns_candidates=False,
            no_cross_path_candidates=False,
            max_rescue_calls=args.max_rescue_calls,
            skip_float_date_union=False,
            force=force,
            write_union=True,
            union_output=args.union_output,
            output=args.share_float_process_output,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )

def update_all_dimensions(args: argparse.Namespace, summary: list[dict[str, Any]]) -> None:
    start_date = args.start_date
    if parse_yyyymmdd(start_date) > parse_yyyymmdd(args.end_date):
        raise RuntimeError(f"start_date {start_date} is after end_date {args.end_date}")
    run_update_step(
        "reference",
        download_selected_tier,
        ns_from(
            args,
            tier="reference",
            start_date=start_date,
            bak_start_date=args.bak_start_date or start_date,
            end_date=args.end_date,
            datasets=None,
            force=args.force,
            page_limit=args.page_limit,
            refresh_reference_datasets=getattr(args, "refresh_reference_datasets", []),
            min_interval_seconds=getattr(args, "reference_min_interval_seconds", None) or args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    run_update_step(
        "daily",
        download_selected_tier,
        ns_from(
            args,
            tier="daily",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.daily_datasets,
            include_limit_list=args.include_limit_list,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    run_update_step(
        "fundamental",
        download_selected_tier,
        ns_from(
            args,
            tier="fundamental",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.fundamental_datasets,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    run_update_step(
        "macro",
        download_selected_tier,
        ns_from(
            args,
            tier="macro",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.macro_datasets,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    run_update_step(
        "global",
        download_selected_tier,
        ns_from(
            args,
            tier="global",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.global_datasets,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    run_update_step(
        "event_flow",
        download_selected_tier,
        ns_from(
            args,
            tier="event_flow",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.event_datasets,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )
    if args.include_board_trading:
        run_update_step(
            "board_trading",
            download_selected_tier,
            ns_from(
                args,
                tier="board_trading",
                start_date=start_date,
                end_date=args.end_date,
                datasets=args.board_datasets,
                force=args.force,
                page_limit=args.page_limit,
                min_interval_seconds=args.min_interval_seconds,
                timeout_seconds=args.timeout_seconds,
            ),
            summary,
        )
    if args.include_intraday:
        run_update_step(
            "intraday_by_date",
            update_intraday_by_date,
            ns_from(
                args,
                start_date=start_date,
                end_date=args.end_date,
                output_dataset=args.output_dataset,
                expected_codes_source=args.expected_codes_source,
                codes=args.codes,
                max_codes=args.max_codes,
                min_rows_per_day=args.min_rows_per_day,
                allow_missing_codes=args.allow_missing_codes,
                allow_validation_warnings=args.allow_validation_warnings,
                max_retries=args.max_retries,
                retry_delay_seconds=args.retry_delay_seconds,
                existing_allow_missing_codes=args.existing_allow_missing_codes,
                page_limit=args.page_limit,
                min_interval_seconds=args.min_interval_seconds,
                timeout_seconds=args.timeout_seconds,
            ),
            summary,
        )
    if args.include_share_float_complete:
        update_share_float_complete_data(args, summary, start_date=start_date, force=args.force)
    run_update_step(
        "text_evidence",
        download_selected_tier,
        ns_from(
            args,
            tier="text_evidence",
            start_date=start_date,
            end_date=args.end_date,
            datasets=args.text_datasets,
            force=args.force,
            page_limit=args.page_limit,
            min_interval_seconds=args.min_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        ),
        summary,
    )

def update_data(args: argparse.Namespace) -> int:
    if args.min_interval_seconds is None:
        args.min_interval_seconds = 0.22
    if args.timeout_seconds is None:
        args.timeout_seconds = 120
    summary: list[dict[str, Any]] = []
    update_all_dimensions(args, summary)
    print(json.dumps({"status": "ok", "start_date": args.start_date, "end_date": args.end_date, "steps": summary}, ensure_ascii=False, indent=2))
    return 0


def add_download_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("download", help="download TuShare raw data by semantic tier")
    parser.add_argument("--tier", required=True, choices=core.DOWNLOAD_TIER_CHOICES)
    core.add_raw_arg(parser)
    parser.add_argument("--start-date", default="20100101")
    parser.add_argument("--bak-start-date", default="20160101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--datasets", nargs="+")
    parser.add_argument("--include-limit-list", action="store_true")
    parser.add_argument("--skip-bak-basic", action="store_true")
    parser.add_argument("--refresh-reference-datasets", nargs="+", choices=core.REFERENCE_DATASETS, default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-codes", type=int)
    parser.add_argument("--codes", nargs="+", help="Optional explicit ts_code list for intraday minute window tests or targeted refreshes.")
    parser.add_argument("--page-limit", type=int, help="Optional requested page size; text evidence datasets are clamped to official per-call limits.")
    parser.add_argument("--news-src", action="append", default=[], help="News short-message source; repeatable. Defaults to all official TuShare sources; use all to expand explicitly.")
    parser.add_argument("--major-news-src", action="append", default=[], help="Optional major_news source filter; repeatable.")
    core.add_board_filter_args(parser)
    core.add_macro_filter_args(parser)
    core.add_runtime_args(parser, min_interval=None, timeout=None)

def add_update_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("update", help="fill missing TuShare data across all retained domains")
    core.add_raw_arg(parser)
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--start-date", required=True, help="Fill missing data from this date through --end-date across all retained data domains.")
    parser.add_argument("--bak-start-date", help="Optional bak_basic lower bound. Defaults to --start-date.")
    parser.add_argument("--daily-datasets", nargs="+", choices=core.DAILY_REQUIRED_DATASETS + core.DAILY_OPTIONAL_DATASETS)
    parser.add_argument("--include-limit-list", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--refresh-reference-datasets",
        nargs="+",
        choices=core.REFERENCE_DATASETS,
        default=["stock_basic", "namechange", "index_classify", "index_member_all"],
        help="Reference datasets to force-refresh during daily update; heavier datasets should be explicit.",
    )
    parser.add_argument("--reference-min-interval-seconds", type=float, help="Optional lower call frequency for the reference refresh step.")
    parser.add_argument("--fundamental-datasets", nargs="+", choices=core.FUNDAMENTAL_DATASETS)
    parser.add_argument("--macro-datasets", nargs="+", choices=core.MACRO_DATASETS)
    parser.add_argument("--global-datasets", nargs="+", choices=core.MACRO_DATASETS)
    parser.add_argument("--event-datasets", nargs="+", choices=[dataset for dataset in core.EVENT_FLOW_DATASETS if dataset != "share_float"])
    parser.add_argument("--board-datasets", nargs="+", choices=core.BOARD_TRADING_DATASETS)
    parser.add_argument(
        "--include-board-trading",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Update 打板专题数据 by default; use --no-include-board-trading for lightweight refreshes.",
    )
    parser.add_argument("--text-datasets", nargs="+", choices=core.TEXT_DATASETS)
    parser.add_argument(
        "--include-intraday",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Update final by-date 1-minute files by default; use --no-include-intraday for lightweight metadata-only refreshes.",
    )
    parser.add_argument("--output-dataset", default=core.STK_MINS_BY_DATE_DATASET)
    parser.add_argument("--expected-codes-source", choices=["daily", "active", "minute"], default="minute")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--max-codes", type=int)
    parser.add_argument("--min-rows-per-day", type=int, default=0)
    parser.add_argument("--allow-missing-codes", type=int, default=0)
    parser.add_argument("--existing-allow-missing-codes", type=int, default=50)
    parser.add_argument("--allow-validation-warnings", action="store_true")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0)
    parser.add_argument(
        "--include-share-float-complete",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Refresh recent share_float raw partitions and rebuild the full share_float_complete union by default.",
    )
    parser.add_argument("--rescue-ann-limit-hits", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-ann-rescue-days", type=int, default=5)
    parser.add_argument("--rescue-universe", choices=["candidate", "explicit", "all_a"], default="candidate")
    parser.add_argument("--max-rescue-calls", type=int, default=50000)
    parser.add_argument("--union-output", default="data/raw/share_float_complete/share_float_complete.parquet")
    parser.add_argument(
        "--allow-union-shrink",
        action="store_true",
        help="Allow share_float_complete union rebuilds that produce fewer rows than the existing union.",
    )
    parser.add_argument("--share-float-process-output", help="Optional temporary process report path for share_float_complete.")
    parser.add_argument("--skip-bak-basic", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--page-limit", type=int)
    parser.add_argument("--news-src", action="append", default=[])
    parser.add_argument("--major-news-src", action="append", default=[])
    core.add_board_filter_args(parser)
    core.add_macro_filter_args(parser)
    core.add_runtime_args(parser, min_interval=None, timeout=None)

def add_intraday_parsers(sub: argparse._SubParsersAction) -> None:
    compact = sub.add_parser("compact-intraday-by-date", help="build final full-market daily minute files from stock-year source partitions")
    core.add_intraday_by_date_common_args(compact)
    compact.add_argument("--force", action="store_true")
    compact.add_argument("--allow-empty", action="store_true")
    compact.add_argument("--allow-validation-warnings", action="store_true")

    update = sub.add_parser("update-intraday-by-date", help="download/retry trade dates directly into final daily minute files")
    core.add_intraday_by_date_common_args(update, expected_codes_choices=["daily", "active", "minute"], expected_codes_default="minute")
    update.add_argument("--force", action="store_true")
    update.add_argument("--allow-validation-warnings", action="store_true")
    update.add_argument("--max-retries", type=int, default=3)
    update.add_argument("--retry-delay-seconds", type=float, default=5.0)
    update.add_argument("--page-limit", type=int)
    core.add_runtime_args(update, min_interval=0.22, timeout=120)

def add_share_float_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("download-share-float-complete", help="download share_float through ann_date and targeted ts_code rescue paths")
    core.add_raw_arg(parser)
    parser.add_argument("--ann-start-date", default="20100101")
    parser.add_argument("--ann-end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--float-start-date", default="20200101")
    parser.add_argument("--float-end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--skip-ann-date", action="store_true")
    parser.add_argument("--rescue-ann-limit-hits", action="store_true", help="Retry ann_date partitions that hit 6000 rows by ann_date + ts_code.")
    parser.add_argument("--rescue-ann-date", action="append", default=[], help="Specific ann_date to retry by ts_code; repeatable.")
    parser.add_argument("--max-ann-rescue-days", type=int, default=5, help="Safety cap for automatic ann_date + ts_code rescue days.")
    parser.add_argument("--float-rescue-date", action="append", default=[], help="Specific float_date to retry by ts_code; repeatable.")
    parser.add_argument("--rescue-universe", choices=["candidate", "explicit", "all_a"], default="candidate")
    parser.add_argument("--rescue-code", action="append", default=[])
    parser.add_argument("--rescue-codes-file")
    parser.add_argument("--no-anns-candidates", action="store_true")
    parser.add_argument("--no-cross-path-candidates", action="store_true")
    parser.add_argument("--max-rescue-calls", type=int, default=50000)
    parser.add_argument("--skip-float-date-union", action="store_true")
    parser.add_argument("--max-codes", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--write-union", action="store_true")
    parser.add_argument(
        "--allow-union-shrink",
        action="store_true",
        help="Allow share_float_complete union rebuilds that produce fewer rows than the existing union.",
    )
    parser.add_argument("--union-output", default="data/raw/share_float_complete/share_float_complete.parquet")
    parser.add_argument("--union-ann-start-date", help="Optional ann_date lower bound used only when rebuilding the union.")
    parser.add_argument("--union-ann-end-date", help="Optional ann_date upper bound used only when rebuilding the union.")
    parser.add_argument("--union-float-start-date", help="Optional float_date lower bound used only when rebuilding the union.")
    parser.add_argument("--union-float-end-date", help="Optional float_date upper bound used only when rebuilding the union.")
    core.add_runtime_args(parser, min_interval=0.22, timeout=90)
    parser.add_argument("--output", help="Optional process report path. No status file is written by default; event-flow audit checks the union artifact.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add_download_parser(sub)
    add_update_parser(sub)
    add_intraday_parsers(sub)
    add_share_float_parser(sub)
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    if args.command == "download":
        return download_selected_tier(args)
    if args.command == "update":
        return update_data(args)
    if args.command == "compact-intraday-by-date":
        return compact_intraday_by_date(args)
    if args.command == "update-intraday-by-date":
        return update_intraday_by_date(args)
    if args.command == "download-share-float-complete":
        return download_share_float_complete(args)
    raise RuntimeError(f"unknown command {args.command}")

if __name__ == "__main__":
    raise SystemExit(main())
