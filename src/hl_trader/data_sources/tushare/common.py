#!/usr/bin/env python3
"""Shared TuShare constants, schemas, client, and utility helpers."""

from __future__ import annotations
import argparse
import calendar
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pandas as pd
import pyarrow.parquet as pq
import requests


API_URL = "https://api.tushare.pro"

BASE_RESEARCH_STATUS_PATH = "results/data_quality/base_research_status.json"

TEXT_EVIDENCE_STATUS_PATH = "results/data_quality/text_evidence_status.json"

INTRADAY_MINUTES_STATUS_PATH = "results/data_quality/intraday_minutes_status.json"

MACRO_CONTEXT_STATUS_PATH = "results/data_quality/macro_context_status.json"

EVENT_FLOW_STATUS_PATH = "results/data_quality/event_flow_status.json"

BOARD_TRADING_STATUS_PATH = "results/data_quality/board_trading_status.json"

DOWNLOAD_TIER_CHOICES = ("reference", "daily", "fundamental", "intraday", "event_flow", "board_trading", "text_evidence", "macro", "global")

REFERENCE_DATASETS = [
    "stock_basic",
    "stock_company",
    "trade_cal",
    "bak_basic",
    "namechange",
    "index_classify",
    "index_member_all",
]

DAILY_REQUIRED_DATASETS = ["daily", "adj_factor", "daily_basic", "stk_limit", "suspend_d"]

DAILY_OPTIONAL_DATASETS = ["limit_list_d"]

FUNDAMENTAL_DATASETS = [
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
]

TEXT_DATASETS = [
    "anns_d",
    "major_news",
    "cctv_news",
    "npr",
    "research_report",
    "report_rc",
    "news",
]

INTRADAY_DATASETS = ["stk_mins_1min"]

EVENT_FLOW_DATASETS = [
    "margin",
    "margin_detail",
    "moneyflow",
    "stk_holdernumber",
    "stk_holdertrade",
    "repurchase",
    "share_float",
    "block_trade",
]

BOARD_TRADING_DATASETS = [
    "kpl_list",
    "limit_step",
    "limit_cpt_list",
    "limit_list_ths",
    "top_list",
    "top_inst",
    "hm_list",
    "hm_detail",
    "ths_hot",
    "dc_hot",
]

BOARD_TRADING_DEFAULT_DATASETS = list(BOARD_TRADING_DATASETS)

BOARD_KPL_TAGS = ["涨停", "炸板", "跌停", "自然涨停", "竞价"]

BOARD_THS_LIMIT_TYPES = ["涨停池", "连扳池", "冲刺涨停", "炸板池", "跌停池"]

BOARD_THS_HOT_MARKETS = ["热股", "行业板块", "概念板块"]

BOARD_DC_HOT_MARKETS = ["A股市场"]

BOARD_DC_HOT_TYPES = ["人气榜", "飙升榜"]

BOARD_HOT_IS_NEW = ["N"]

SHARE_FLOAT_FIELDS = "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"

SHARE_FLOAT_ROW_LIMIT = 6000

SHARE_FLOAT_UNLOCK_TITLE_PATTERN = re.compile(
    r"限售|解禁|上市流通|解除限售|限售股份上市|限售股上市|首次公开发行限售|非公开发行限售|定向增发限售"
)

MACRO_DATASETS = [
    "cn_schedule",
    "cn_gdp",
    "cn_cpi",
    "cn_ppi",
    "cn_pmi",
    "cn_m",
    "sf_month",
    "shibor",
    "shibor_quote",
    "shibor_lpr",
    "hibor",
    "libor",
    "repo_daily",
    "us_tycr",
    "us_trycr",
    "us_tbr",
    "us_tltr",
    "index_global",
    "fx_daily",
    "eco_cal",
    "monetary_policy",
]

MACRO_REGIME_DEFAULT_DATASETS = [
    "cn_schedule",
    "cn_gdp",
    "cn_cpi",
    "cn_ppi",
    "cn_pmi",
    "cn_m",
    "sf_month",
    "shibor",
    "shibor_lpr",
    "repo_daily",
    "monetary_policy",
]

GLOBAL_CONTEXT_DEFAULT_DATASETS = [
    "eco_cal",
    "index_global",
    "fx_daily",
    "us_tycr",
    "us_trycr",
    "us_tbr",
    "us_tltr",
    "libor",
    "hibor",
]

DEFAULT_GLOBAL_INDEX_CODES = ["XIN9", "HSI", "HKTECH", "DJI", "SPX", "IXIC", "FTSE", "GDAXI", "N225", "RUT"]

DEFAULT_FX_CODES = ["USDCNH.FXCM"]

DEFAULT_LIBOR_CURRENCIES = ["USD", "EUR", "JPY", "GBP", "CHF"]

TRADE_DATE_PAGE_LIMIT = 5000

STK_MINS_API_NAME = "stk_mins"

STK_MINS_DATASET = "stk_mins_1min"

STK_MINS_BY_DATE_DATASET = "stk_mins_1min_by_date"

STK_MINS_FREQ = "1min"

STK_MINS_FIELDS = "ts_code,trade_time,open,high,low,close,vol,amount"

STK_MINS_PAGE_LIMIT = 8000

STK_MINS_REQUIRED_COLUMNS = ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount", "trade_date", "available_at", "available_at_rule"]

TEXT_DEFAULT_DATASETS = [
    "anns_d",
    "major_news",
    "cctv_news",
    "npr",
    "research_report",
    "report_rc",
    "news",
]

NEWS_SOURCES = [
    "sina",
    "wallstreetcn",
    "10jqka",
    "eastmoney",
    "yuncaijing",
    "fenghuang",
    "jinrongjie",
    "cls",
    "yicai",
]

MAJOR_NEWS_SOURCES = [
    "新华网",
    "凤凰财经",
    "同花顺",
    "新浪财经",
    "华尔街见闻",
    "中证网",
    "财新网",
    "第一财经",
    "财联社",
]

BAK_BASIC_FIELDS = "trade_date,ts_code,name,industry,area,pe,float_share,total_share,total_assets,liquid_assets,fixed_assets,reserved,reserved_pershare,eps,bvps,pb,list_date,undp,per_undp,rev_yoy,profit_yoy,gpr,npr,holder_num"

SEMANTIC_DOC_REFS = {
    "stock_basic": "https://tushare.pro/document/2?doc_id=25",
    "trade_cal": "https://tushare.pro/document/2?doc_id=26",
    "daily": "https://tushare.pro/document/2?doc_id=27",
    "daily_basic": "https://tushare.pro/document/2?doc_id=32",
    "bak_daily": "https://tushare.pro/document/2?doc_id=255",
    "bak_basic": "https://tushare.pro/document/2?doc_id=262",
    "stk_limit": "https://tushare.pro/document/2?doc_id=183",
    "adj_factor": "https://tushare.pro/document/2?doc_id=28",
    "suspend_d": "https://tushare.pro/document/2?doc_id=214",
    "limit_list_d": "https://tushare.pro/document/2?doc_id=298",
}

INTEGRATED_DOC_REFS = {
    **SEMANTIC_DOC_REFS,
    "stock_company": "https://tushare.pro/document/2?doc_id=112",
    "namechange": "https://tushare.pro/document/2?doc_id=100",
    "index_classify": "https://tushare.pro/document/2?doc_id=181",
    "index_member_all": "https://tushare.pro/document/2?doc_id=335",
    "income_vip": "https://tushare.pro/document/2?doc_id=33",
    "balancesheet_vip": "https://tushare.pro/document/2?doc_id=36",
    "cashflow_vip": "https://tushare.pro/document/2?doc_id=44",
    "forecast_vip": "https://tushare.pro/document/2?doc_id=45",
    "express_vip": "https://tushare.pro/document/2?doc_id=46",
    "fina_indicator_vip": "https://tushare.pro/document/2?doc_id=79",
    "dividend": "https://tushare.pro/document/2?doc_id=103",
    "fina_audit": "https://tushare.pro/document/2?doc_id=80",
    "fina_mainbz_vip": "https://tushare.pro/document/2?doc_id=81",
    "disclosure_date": "https://tushare.pro/document/2?doc_id=162",
    "anns_d": "https://www.tushare.pro/document/2?doc_id=176",
    "major_news": "https://tushare.pro/document/2?doc_id=195",
    "cctv_news": "https://tushare.pro/document/2?doc_id=154",
    "npr": "https://tushare.pro/document/2?doc_id=406",
    "research_report": "https://tushare.pro/document/2?doc_id=415",
    "report_rc": "https://tushare.pro/document/2?doc_id=292",
    "news": "https://www.tushare.pro/document/41?doc_id=143",
    "stk_mins_1min": "https://tushare.pro/document/2?doc_id=370",
    "cn_schedule": "https://tushare.pro/document/2?doc_id=461",
    "cn_gdp": "https://tushare.pro/document/2?doc_id=227",
    "cn_cpi": "https://tushare.pro/document/2?doc_id=228",
    "cn_ppi": "https://tushare.pro/document/2?doc_id=229",
    "cn_pmi": "https://tushare.pro/document/2?doc_id=325",
    "cn_m": "https://tushare.pro/document/2?doc_id=242",
    "sf_month": "https://tushare.pro/document/2?doc_id=310",
    "shibor": "https://tushare.pro/document/2?doc_id=202",
    "shibor_quote": "https://tushare.pro/document/2?doc_id=203",
    "shibor_lpr": "https://tushare.pro/document/2?doc_id=204",
    "libor": "https://tushare.pro/document/2?doc_id=205",
    "hibor": "https://tushare.pro/document/2?doc_id=206",
    "repo_daily": "https://tushare.pro/document/2?doc_id=256",
    "us_tycr": "https://tushare.pro/document/2?doc_id=218",
    "us_trycr": "https://tushare.pro/document/2?doc_id=219",
    "us_tbr": "https://tushare.pro/document/2?doc_id=220",
    "us_tltr": "https://tushare.pro/document/2?doc_id=222",
    "index_global": "https://tushare.pro/document/2?doc_id=211",
    "fx_daily": "https://tushare.pro/document/2?doc_id=179",
    "eco_cal": "https://tushare.pro/document/2?doc_id=233",
    "monetary_policy": "https://tushare.pro/document/2?doc_id=465",
    "margin": "https://tushare.pro/document/2?doc_id=58",
    "margin_detail": "https://tushare.pro/document/2?doc_id=59",
    "moneyflow": "https://tushare.pro/document/2?doc_id=170",
    "stk_holdernumber": "https://tushare.pro/document/2?doc_id=166",
    "stk_holdertrade": "https://tushare.pro/document/2?doc_id=175",
    "repurchase": "https://tushare.pro/document/2?doc_id=124",
    "share_float": "https://tushare.pro/document/2?doc_id=160",
    "block_trade": "https://tushare.pro/document/2?doc_id=161",
    "kpl_list": "https://tushare.pro/document/2?doc_id=347",
    "limit_step": "https://tushare.pro/document/2?doc_id=356",
    "limit_cpt_list": "https://tushare.pro/document/2?doc_id=357",
    "limit_list_ths": "https://tushare.pro/document/2?doc_id=355",
    "top_list": "https://tushare.pro/document/2?doc_id=106",
    "top_inst": "https://tushare.pro/document/2?doc_id=107",
    "hm_list": "https://tushare.pro/document/2?doc_id=311",
    "hm_detail": "https://tushare.pro/document/2?doc_id=312",
    "ths_hot": "https://tushare.pro/document/2?doc_id=320",
    "dc_hot": "https://tushare.pro/document/2?doc_id=321",
}

@dataclass
class ApiResult:
    fields: list[str]
    items: list[list[Any]]
    source_hash: str

@dataclass
class TradeDateDataset:
    api_name: str
    fields: str
    start_date: str = "20100101"
    zero_rows_ok: bool = False
    key_columns: tuple[str, ...] = ("trade_date", "ts_code")

@dataclass
class FundamentalDataset:
    api_name: str
    strategy: str
    key_columns: tuple[str, ...]
    period_param: str = "period"
    fields: str = ""

@dataclass
class TextDataset:
    api_name: str
    strategy: str
    key_columns: tuple[str, ...]
    fields: str
    page_limit: int | None = None
    start_date: str = "20100101"
    zero_rows_ok: bool = True
    time_column: str = ""
    date_column: str = ""

@dataclass
class MacroDataset:
    api_name: str
    strategy: str
    key_columns: tuple[str, ...]
    fields: str = ""
    page_limit: int | None = None
    date_column: str = ""
    time_column: str = ""
    start_date: str = "20100101"
    start_month: str = "201001"
    start_quarter: str = "2010Q1"

@dataclass
class EventDataset:
    api_name: str
    strategy: str
    key_columns: tuple[str, ...]
    fields: str
    page_limit: int
    date_column: str
    start_date: str = "20100101"
    fallback_date_column: str = ""
    zero_rows_ok: bool = True

@dataclass
class BoardTradingDataset:
    api_name: str
    strategy: str
    key_columns: tuple[str, ...]
    fields: str
    page_limit: int
    date_column: str = "trade_date"
    time_column: str = ""
    start_date: str = "20200101"
    zero_rows_ok: bool = True

FUNDAMENTAL_SPECS = {
    "income_vip": FundamentalDataset(
        api_name="income_vip",
        strategy="period",
        key_columns=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    ),
    "balancesheet_vip": FundamentalDataset(
        api_name="balancesheet_vip",
        strategy="period",
        key_columns=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    ),
    "cashflow_vip": FundamentalDataset(
        api_name="cashflow_vip",
        strategy="period",
        key_columns=("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type"),
    ),
    "fina_indicator_vip": FundamentalDataset(
        api_name="fina_indicator_vip",
        strategy="period",
        key_columns=("ts_code", "ann_date", "end_date"),
    ),
    "forecast_vip": FundamentalDataset(
        api_name="forecast_vip",
        strategy="ann_month",
        key_columns=("ts_code", "ann_date", "end_date", "type", "first_ann_date", "update_flag"),
    ),
    "express_vip": FundamentalDataset(
        api_name="express_vip",
        strategy="ann_month",
        key_columns=("ts_code", "ann_date", "end_date"),
    ),
    "dividend": FundamentalDataset(
        api_name="dividend",
        strategy="ts_code",
        key_columns=("ts_code", "end_date", "ann_date", "div_proc", "record_date", "ex_date", "pay_date"),
    ),
    "fina_audit": FundamentalDataset(
        api_name="fina_audit",
        strategy="ts_code",
        key_columns=("ts_code", "ann_date", "end_date"),
    ),
    "fina_mainbz_vip": FundamentalDataset(
        api_name="fina_mainbz_vip",
        strategy="ts_code",
        key_columns=("ts_code", "end_date", "bz_item", "bz_code", "curr_type"),
    ),
    "disclosure_date": FundamentalDataset(
        api_name="disclosure_date",
        strategy="period",
        key_columns=("ts_code", "end_date", "ann_date", "pre_date", "actual_date"),
        period_param="end_date",
    ),
}

TEXT_SPECS = {
    "anns_d": TextDataset(
        api_name="anns_d",
        strategy="range_month",
        fields="ann_date,ts_code,name,title,url,rec_time",
        page_limit=2000,
        key_columns=("ann_date", "ts_code", "title", "url"),
        start_date="20100101",
        time_column="rec_time",
        date_column="ann_date",
    ),
    "major_news": TextDataset(
        api_name="major_news",
        strategy="time_range_month",
        fields="title,pub_time,src,content",
        page_limit=400,
        key_columns=("pub_time", "src", "title"),
        start_date="20170101",
        time_column="pub_time",
    ),
    "cctv_news": TextDataset(
        api_name="cctv_news",
        strategy="day",
        fields="date,title,content",
        key_columns=("date", "title"),
        start_date="20170101",
        date_column="date",
    ),
    "npr": TextDataset(
        api_name="npr",
        strategy="range_month",
        fields="pubtime,title,pcode,puborg,ptype,url,content_html",
        page_limit=500,
        key_columns=("pubtime", "pcode", "title"),
        start_date="20100101",
        time_column="pubtime",
    ),
    "research_report": TextDataset(
        api_name="research_report",
        strategy="range_month",
        fields="trade_date,abstr,title,report_type,author,name,ts_code,inst_csname,ind_name,url",
        page_limit=1000,
        key_columns=("trade_date", "report_type", "title", "inst_csname", "ts_code"),
        start_date="20170101",
        date_column="trade_date",
    ),
    "report_rc": TextDataset(
        api_name="report_rc",
        strategy="range_month",
        fields="ts_code,name,report_date,report_title,report_type,classify,org_name,author_name,quarter,op_rt,op_pr,tp,np,eps,pe,rd,roe,ev_ebitda,rating,max_price,min_price,imp_dg,create_time",
        page_limit=3000,
        key_columns=("ts_code", "report_date", "report_title", "org_name", "author_name", "quarter"),
        start_date="20100101",
        time_column="create_time",
        date_column="report_date",
    ),
    "news": TextDataset(
        api_name="news",
        strategy="news_src_day",
        fields="datetime,content,title,channels",
        page_limit=1500,
        key_columns=("datetime", "title"),
        start_date="20180101",
        time_column="datetime",
    ),
}

MACRO_SPECS = {
    "cn_schedule": MacroDataset(
        api_name="cn_schedule",
        strategy="month_loop",
        fields="month,publish_date,title,issuing_org,data_api",
        key_columns=("month", "publish_date", "title", "issuing_org", "data_api"),
        date_column="publish_date",
    ),
    "cn_gdp": MacroDataset(
        api_name="cn_gdp",
        strategy="quarter_once",
        fields="quarter,gdp,gdp_yoy,pi,pi_yoy,si,si_yoy,ti,ti_yoy",
        key_columns=("quarter",),
        date_column="quarter",
        start_quarter="2010Q1",
    ),
    "cn_cpi": MacroDataset(
        api_name="cn_cpi",
        strategy="month_once",
        fields="month,nt_val,nt_yoy,nt_mom,nt_accu,town_val,town_yoy,town_mom,town_accu,cnt_val,cnt_yoy,cnt_mom,cnt_accu",
        key_columns=("month",),
        date_column="month",
    ),
    "cn_ppi": MacroDataset(
        api_name="cn_ppi",
        strategy="month_once",
        fields="month,ppi_yoy,ppi_mp_yoy,ppi_mp_qm_yoy,ppi_mp_rm_yoy,ppi_mp_p_yoy,ppi_cg_yoy,ppi_cg_f_yoy,ppi_cg_c_yoy,ppi_cg_adu_yoy,ppi_cg_dcg_yoy,ppi_mom,ppi_mp_mom,ppi_mp_qm_mom,ppi_mp_rm_mom,ppi_mp_p_mom,ppi_cg_mom,ppi_cg_f_mom,ppi_cg_c_mom,ppi_cg_adu_mom,ppi_cg_dcg_mom,ppi_accu,ppi_mp_accu,ppi_mp_qm_accu,ppi_mp_rm_accu,ppi_mp_p_accu,ppi_cg_accu,ppi_cg_f_accu,ppi_cg_c_accu,ppi_cg_adu_accu,ppi_cg_dcg_accu",
        key_columns=("month",),
        date_column="month",
    ),
    "cn_pmi": MacroDataset(
        api_name="cn_pmi",
        strategy="month_once",
        fields="month,pmi010000,pmi010400,pmi010500,pmi010900,pmi011000,pmi011200,pmi011300,pmi011600,pmi020100,pmi020200,pmi020300,pmi020400,pmi020600,pmi030000",
        key_columns=("month",),
        date_column="month",
    ),
    "cn_m": MacroDataset(
        api_name="cn_m",
        strategy="month_once",
        fields="month,m0,m0_yoy,m0_mom,m1,m1_yoy,m1_mom,m2,m2_yoy,m2_mom",
        key_columns=("month",),
        date_column="month",
    ),
    "sf_month": MacroDataset(
        api_name="sf_month",
        strategy="month_once",
        fields="month,inc_month,inc_cumval,stk_endval",
        key_columns=("month",),
        date_column="month",
    ),
    "shibor": MacroDataset(
        api_name="shibor",
        strategy="date_year",
        fields="date,on,1w,2w,1m,3m,6m,9m,1y",
        key_columns=("date",),
        date_column="date",
    ),
    "shibor_quote": MacroDataset(
        api_name="shibor_quote",
        strategy="date_year",
        fields="date,bank,on_b,on_a,1w_b,1w_a,2w_b,2w_a,1m_b,1m_a,3m_b,3m_a,6m_b,6m_a,9m_b,9m_a,1y_b,1y_a",
        key_columns=("date", "bank"),
        date_column="date",
    ),
    "shibor_lpr": MacroDataset(
        api_name="shibor_lpr",
        strategy="date_year",
        fields="date,1y,5y",
        key_columns=("date",),
        date_column="date",
    ),
    "hibor": MacroDataset(
        api_name="hibor",
        strategy="date_year",
        fields="date,on,1w,2w,1m,2m,3m,6m,12m",
        key_columns=("date",),
        date_column="date",
    ),
    "libor": MacroDataset(
        api_name="libor",
        strategy="date_year_by_curr_type",
        fields="date,curr_type,on,1w,1m,2m,3m,6m,12m",
        key_columns=("date", "curr_type"),
        date_column="date",
    ),
    "repo_daily": MacroDataset(
        api_name="repo_daily",
        strategy="date_year",
        fields="ts_code,trade_date,repo_maturity,pre_close,open,high,low,close,weight,weight_r,amount,num",
        key_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    "us_tycr": MacroDataset(
        api_name="us_tycr",
        strategy="date_year",
        fields="date,m1,m2,m3,m4,m6,y1,y2,y3,y5,y7,y10,y20,y30",
        key_columns=("date",),
        date_column="date",
    ),
    "us_trycr": MacroDataset(
        api_name="us_trycr",
        strategy="date_year",
        fields="date,y5,y7,y10,y20,y30",
        key_columns=("date",),
        date_column="date",
    ),
    "us_tbr": MacroDataset(
        api_name="us_tbr",
        strategy="date_year",
        fields="date,w4_bd,w4_ce,w8_bd,w8_ce,w13_bd,w13_ce,w17_bd,w17_ce,w26_bd,w26_ce,w52_bd,w52_ce",
        key_columns=("date",),
        date_column="date",
    ),
    "us_tltr": MacroDataset(
        api_name="us_tltr",
        strategy="date_year",
        fields="date,ltc,cmt,e_factor",
        key_columns=("date",),
        date_column="date",
    ),
    "index_global": MacroDataset(
        api_name="index_global",
        strategy="date_year_by_ts_code",
        fields="ts_code,trade_date,open,close,high,low,pre_close,change,pct_chg,swing,vol,amount",
        key_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    "fx_daily": MacroDataset(
        api_name="fx_daily",
        strategy="date_year_by_ts_code",
        fields="ts_code,trade_date,bid_open,bid_close,bid_high,bid_low,ask_open,ask_close,ask_high,ask_low,tick_qty",
        key_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    "eco_cal": MacroDataset(
        api_name="eco_cal",
        strategy="eco_cal_month",
        fields="date,time,currency,country,event,value,pre_value,fore_value",
        page_limit=3000,
        key_columns=("date", "time", "currency", "country", "event"),
        date_column="date",
        time_column="time",
    ),
    "monetary_policy": MacroDataset(
        api_name="monetary_policy",
        strategy="date_year",
        fields="pub_date,title,url,pdf_url,content_html",
        page_limit=1000,
        key_columns=("pub_date", "title", "url"),
        date_column="pub_date",
        start_date="20010101",
    ),
}

EVENT_FLOW_SPECS = {
    "margin": EventDataset(
        api_name="margin",
        strategy="trade_date",
        fields="trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl",
        page_limit=4000,
        key_columns=("trade_date", "exchange_id"),
        date_column="trade_date",
        zero_rows_ok=False,
    ),
    "margin_detail": EventDataset(
        api_name="margin_detail",
        strategy="trade_date",
        fields="trade_date,ts_code,name,rzye,rqye,rzmre,rqyl,rzche,rqchl,rqmcl,rzrqye",
        page_limit=6000,
        key_columns=("trade_date", "ts_code"),
        date_column="trade_date",
        zero_rows_ok=False,
    ),
    "moneyflow": EventDataset(
        api_name="moneyflow",
        strategy="trade_date",
        fields="ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,net_mf_vol,net_mf_amount",
        page_limit=5000,
        key_columns=("trade_date", "ts_code"),
        date_column="trade_date",
        zero_rows_ok=False,
    ),
    "stk_holdernumber": EventDataset(
        api_name="stk_holdernumber",
        strategy="range_month",
        fields="ts_code,ann_date,end_date,holder_num",
        page_limit=3000,
        key_columns=("ts_code", "ann_date", "end_date"),
        date_column="ann_date",
        zero_rows_ok=True,
    ),
    "stk_holdertrade": EventDataset(
        api_name="stk_holdertrade",
        strategy="range_month",
        fields="ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share,begin_date,close_date",
        page_limit=3000,
        key_columns=("ts_code", "ann_date", "holder_name", "holder_type", "in_de", "begin_date", "close_date"),
        date_column="ann_date",
        zero_rows_ok=True,
    ),
    "repurchase": EventDataset(
        api_name="repurchase",
        strategy="range_month",
        fields="ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit",
        page_limit=2000,
        key_columns=("ts_code", "ann_date", "end_date", "proc", "exp_date"),
        date_column="ann_date",
        start_date="20110101",
        zero_rows_ok=True,
    ),
    "share_float": EventDataset(
        api_name="share_float",
        strategy="day",
        fields=SHARE_FLOAT_FIELDS,
        page_limit=6000,
        key_columns=("ts_code", "ann_date", "float_date", "holder_name", "share_type"),
        date_column="ann_date",
        fallback_date_column="float_date",
        zero_rows_ok=True,
    ),
    "block_trade": EventDataset(
        api_name="block_trade",
        strategy="trade_date",
        fields="ts_code,trade_date,price,vol,amount,buyer,seller",
        page_limit=1000,
        key_columns=("trade_date", "ts_code", "price", "vol", "amount", "buyer", "seller"),
        date_column="trade_date",
        zero_rows_ok=True,
    ),
}

BOARD_TRADING_SPECS = {
    "kpl_list": BoardTradingDataset(
        api_name="kpl_list",
        strategy="trade_date_by_tag",
        fields="ts_code,name,trade_date,lu_time,ld_time,open_time,last_time,lu_desc,tag,theme,net_change,bid_amount,status,bid_change,bid_turnover,lu_bid_vol,pct_chg,bid_pct_chg,rt_pct_chg,limit_order,amount,turnover_rate,free_float,lu_limit_order",
        page_limit=8000,
        key_columns=("trade_date", "ts_code", "tag", "status", "lu_time", "open_time", "last_time"),
    ),
    "limit_step": BoardTradingDataset(
        api_name="limit_step",
        strategy="trade_date",
        fields="ts_code,name,trade_date,nums",
        page_limit=2000,
        key_columns=("trade_date", "ts_code", "nums"),
    ),
    "limit_cpt_list": BoardTradingDataset(
        api_name="limit_cpt_list",
        strategy="trade_date",
        fields="ts_code,name,trade_date,days,up_stat,cons_nums,up_nums,pct_chg,rank",
        page_limit=2000,
        key_columns=("trade_date", "ts_code", "rank"),
    ),
    "limit_list_ths": BoardTradingDataset(
        api_name="limit_list_ths",
        strategy="trade_date_by_limit_type",
        fields="trade_date,ts_code,name,price,pct_chg,open_num,lu_desc,limit_type,tag,status,limit_order,limit_amount,turnover_rate,free_float,lu_limit_order,limit_up_suc_rate,turnover,market_type,rise_rate,sum_float,first_lu_time,last_lu_time,first_ld_time,last_ld_time",
        page_limit=8000,
        key_columns=("trade_date", "ts_code", "limit_type", "tag", "status", "first_lu_time", "last_lu_time", "first_ld_time", "last_ld_time"),
        start_date="20231101",
    ),
    "top_list": BoardTradingDataset(
        api_name="top_list",
        strategy="trade_date",
        fields="trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate,amount_rate,float_values,reason",
        page_limit=10000,
        key_columns=(
            "trade_date",
            "ts_code",
            "name",
            "close",
            "pct_change",
            "turnover_rate",
            "amount",
            "l_sell",
            "l_buy",
            "l_amount",
            "net_amount",
            "net_rate",
            "amount_rate",
            "float_values",
            "reason",
        ),
        zero_rows_ok=True,
    ),
    "top_inst": BoardTradingDataset(
        api_name="top_inst",
        strategy="trade_date",
        fields="trade_date,ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy,side,reason",
        page_limit=10000,
        key_columns=("trade_date", "ts_code", "exalter", "buy", "sell", "net_buy", "side", "reason"),
        zero_rows_ok=True,
    ),
    "hm_list": BoardTradingDataset(
        api_name="hm_list",
        strategy="static_once",
        fields="name,desc,orgs",
        page_limit=1000,
        key_columns=("name",),
        date_column="",
        start_date="20220801",
    ),
    "hm_detail": BoardTradingDataset(
        api_name="hm_detail",
        strategy="trade_date",
        fields="trade_date,ts_code,ts_name,buy_amount,sell_amount,net_amount,hm_name,hm_orgs",
        page_limit=10000,
        key_columns=("trade_date", "ts_code", "hm_name", "hm_orgs"),
        start_date="20220801",
    ),
    "ths_hot": BoardTradingDataset(
        api_name="ths_hot",
        strategy="trade_date_by_market",
        fields="trade_date,data_type,ts_code,ts_name,rank,pct_change,current_price,hot,concept,rank_time,rank_reason",
        page_limit=2000,
        key_columns=("trade_date", "data_type", "ts_code", "rank_time", "rank"),
        time_column="rank_time",
    ),
    "dc_hot": BoardTradingDataset(
        api_name="dc_hot",
        strategy="trade_date_by_market_hot_type",
        fields="trade_date,data_type,ts_code,ts_name,rank,pct_change,current_price,hot,concept,rank_time",
        page_limit=2000,
        key_columns=("trade_date", "data_type", "ts_code", "rank_time", "rank"),
        time_column="rank_time",
    ),
}

BAK_BASIC_SPEC = TradeDateDataset(
    api_name="bak_basic",
    fields=BAK_BASIC_FIELDS,
    start_date="20160101",
    zero_rows_ok=True,
    key_columns=("trade_date", "ts_code"),
)

DAILY_SPECS = {
    "daily": TradeDateDataset(
        api_name="daily",
        fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
    ),
    "adj_factor": TradeDateDataset(
        api_name="adj_factor",
        fields="ts_code,trade_date,adj_factor",
    ),
    "daily_basic": TradeDateDataset(
        api_name="daily_basic",
        fields="ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv",
    ),
    "stk_limit": TradeDateDataset(
        api_name="stk_limit",
        fields="trade_date,ts_code,pre_close,up_limit,down_limit",
    ),
    "suspend_d": TradeDateDataset(
        api_name="suspend_d",
        fields="ts_code,trade_date,suspend_timing,suspend_type",
        zero_rows_ok=True,
        key_columns=("trade_date", "ts_code", "suspend_type", "suspend_timing"),
    ),
    "limit_list_d": TradeDateDataset(
        api_name="limit_list_d",
        fields="trade_date,ts_code,industry,name,close,pct_chg,amount,limit_amount,float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,open_times,up_stat,limit_times,limit",
        start_date="20200101",
        zero_rows_ok=True,
        key_columns=("trade_date", "ts_code", "limit"),
    ),
}

class TuShareClient:
    def __init__(self, token: str, min_interval: float, timeout: int) -> None:
        self.token = token
        self.min_interval = min_interval
        self.timeout = timeout
        self.last_call = 0.0
        self.session = requests.Session()

    def query(self, api_name: str, params: dict[str, Any] | None = None, fields: str = "", retries: int = 5) -> ApiResult:
        payload = {"api_name": api_name, "token": self.token, "params": params or {}, "fields": fields}
        for attempt in range(1, retries + 1):
            self._throttle()
            try:
                response = self.session.post(API_URL, json=payload, timeout=self.timeout)
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                if attempt == retries:
                    raise RuntimeError(f"{api_name} failed after {retries} attempts: {exc}") from exc
                time.sleep(2 * attempt)
                continue

            if body.get("code") == 0:
                data = body.get("data") or {}
                result_fields = list(data.get("fields") or [])
                items = list(data.get("items") or [])
                return ApiResult(result_fields, items, stable_hash({"fields": result_fields, "items": items}))

            message = str(body.get("msg") or "").strip()
            lowered = message.lower()
            rate_limited = any(key in lowered for key in ("rate", "limit", "频率", "频繁", "超限"))
            retryable = rate_limited or any(key in lowered for key in ("timeout", "超时"))
            if retryable and attempt < retries:
                time.sleep(30 if rate_limited else 3 * attempt)
                continue
            raise RuntimeError(f"{api_name} returned code={body.get('code')}: {message}")
        raise RuntimeError(f"{api_name} failed unexpectedly")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call = time.monotonic()

def stable_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def load_token(repo_root: Path) -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env_file = repo_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TUSHARE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    return token
    raise RuntimeError("TUSHARE_TOKEN is not set in the environment or ignored .env")

def frame(result: ApiResult) -> pd.DataFrame:
    return pd.DataFrame(result.items, columns=result.fields)

def query_paged(client: TuShareClient, api_name: str, params: dict[str, Any], fields: str = "", page_limit: int = 10000) -> tuple[ApiResult, int]:
    all_items: list[list[Any]] = []
    result_fields: list[str] = []
    pages = 0
    offset = 0
    while True:
        page_params = dict(params)
        page_params.update({"limit": page_limit, "offset": offset})
        result = client.query(api_name, page_params, fields)
        if not result_fields:
            result_fields = result.fields
        elif result.fields != result_fields:
            raise RuntimeError(f"{api_name} returned inconsistent fields while paging")
        all_items.extend(result.items)
        pages += 1
        if len(result.items) < page_limit:
            break
        offset += page_limit
        if offset > 500000:
            raise RuntimeError(f"{api_name} pagination exceeded safety limit for params={params}")
    return ApiResult(result_fields, all_items, stable_hash({"fields": result_fields, "items": all_items})), pages

def write_parquet(path: Path, df: pd.DataFrame, *, api_name: str, params: dict[str, Any], fields: list[str], source_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    meta = {
        "api_name": api_name,
        "params": params,
        "fields": fields,
        "row_count": int(len(df)),
        "source_hash": source_hash,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "format": "parquet",
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

def read_many(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=columns) for path in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def parquet_rows(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows

def has_pagination_probe(path: Path) -> bool:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    pagination = (meta.get("params") or {}).get("pagination") or {}
    return int(pagination.get("pages") or 0) > 1

def load_stock_codes(raw_dir: Path) -> list[str]:
    files = sorted((raw_dir / "stock_basic").glob("list_status=*.parquet"))
    if not files:
        raise RuntimeError("stock_basic partitions are missing")
    data = read_many(files, columns=["ts_code"])
    return sorted(data["ts_code"].dropna().astype(str).str.strip().unique().tolist())

def load_sse_open_dates(raw_dir: Path, start_date: str, end_date: str) -> list[str]:
    files = sorted((raw_dir / "trade_cal" / "exchange=SSE").glob("year=*.parquet"))
    if not files:
        raise RuntimeError("SSE trade_cal partitions are missing; run download --tier reference first")
    calendar = read_many(files, columns=["cal_date", "is_open"])
    if calendar.empty:
        raise RuntimeError("SSE trade_cal is empty; run download --tier reference first")
    calendar["cal_date"] = calendar["cal_date"].astype(str)
    available_min = str(calendar["cal_date"].min())
    available_max = str(calendar["cal_date"].max())
    if start_date < available_min or end_date > available_max:
        raise RuntimeError(
            f"SSE trade_cal covers {available_min}-{available_max}, not requested {start_date}-{end_date}; refresh reference trade_cal first"
        )
    mask = (calendar["is_open"].astype(str) == "1") & (calendar["cal_date"] >= start_date) & (calendar["cal_date"] <= end_date)
    dates = sorted(calendar.loc[mask, "cal_date"].tolist())
    if not dates:
        raise RuntimeError(f"no SSE open dates found for {start_date}-{end_date}")
    return dates

def latest_sse_calendar_date(raw_dir: Path) -> str:
    files = sorted((raw_dir / "trade_cal" / "exchange=SSE").glob("year=*.parquet"))
    if not files:
        raise RuntimeError("SSE trade_cal partitions are missing; run download --tier reference first")
    calendar = read_many(files, columns=["cal_date"])
    if calendar.empty:
        raise RuntimeError("SSE trade_cal is empty; run download --tier reference first")
    return str(calendar["cal_date"].dropna().astype(str).max())

def selected_daily_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(args.datasets or DAILY_REQUIRED_DATASETS)
    if getattr(args, "include_limit_list", False) and "limit_list_d" not in datasets:
        datasets.append("limit_list_d")
    invalid = sorted(set(datasets) - set(DAILY_SPECS))
    if invalid:
        raise RuntimeError(f"unknown daily market datasets: {invalid}")
    return datasets

def partition_date(path: Path) -> str:
    return path.stem.split("=", 1)[1] if "=" in path.stem else ""

def selected_fundamental_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(args.datasets or FUNDAMENTAL_DATASETS)
    invalid = sorted(set(datasets) - set(FUNDAMENTAL_SPECS))
    if invalid:
        raise RuntimeError(f"unknown fundamental datasets: {invalid}")
    return datasets

def selected_integrated_fundamental_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(getattr(args, "fundamental_datasets", None) or FUNDAMENTAL_DATASETS)
    invalid = sorted(set(datasets) - set(FUNDAMENTAL_SPECS))
    if invalid:
        raise RuntimeError(f"unknown fundamental datasets: {invalid}")
    return datasets

def parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()

def format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")

def month_windows(start_date: str, end_date: str) -> list[tuple[str, str, str]]:
    start = parse_yyyymmdd(start_date)
    end = parse_yyyymmdd(end_date)
    current = date(start.year, start.month, 1)
    windows: list[tuple[str, str, str]] = []
    while current <= end:
        last = date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
        window_start = max(current, start)
        window_end = min(last, end)
        windows.append((format_yyyymmdd(window_start), format_yyyymmdd(window_end), f"{current.year}{current.month:02d}"))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return windows

def quarter_periods(start_date: str, end_date: str) -> list[str]:
    start = parse_yyyymmdd(start_date)
    end = parse_yyyymmdd(end_date)
    periods: list[str] = []
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            period = date(year, month, day)
            if start <= period <= end:
                periods.append(format_yyyymmdd(period))
    return periods

def yyyymmdd_to_month(value: str) -> str:
    return value[:6]

def month_end_from_yyyymm(value: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError(f"invalid yyyymm value: {value}")
    year = int(text[:4])
    month = int(text[4:6])
    day = calendar.monthrange(year, month)[1]
    return f"{year}{month:02d}{day:02d}"

def yyyymmdd_to_quarter(value: str) -> str:
    parsed = parse_yyyymmdd(value)
    quarter = (parsed.month - 1) // 3 + 1
    return f"{parsed.year}Q{quarter}"

def quarter_end_date(value: str) -> str:
    match = re.fullmatch(r"(\d{4})Q([1-4])", str(value).strip())
    if not match:
        return ""
    year = int(match.group(1))
    quarter = int(match.group(2))
    month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[quarter - 1]
    return f"{year}{month:02d}{day:02d}"

def local_eod(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]} 23:59:59+08:00"

def period_available_at(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return local_eod(text), "conservative_date_eod"
    if re.fullmatch(r"\d{6}", text):
        month_end = parse_yyyymmdd(month_end_from_yyyymm(text))
        available = month_end + timedelta(days=31)
        return local_eod(format_yyyymmdd(available)), "conservative_month_end_plus_31d"
    if re.fullmatch(r"\d{4}Q[1-4]", text):
        quarter_end = quarter_end_date(text)
        available = parse_yyyymmdd(quarter_end) + timedelta(days=45)
        return local_eod(format_yyyymmdd(available)), "conservative_quarter_end_plus_45d"
    return "", "missing_source_date"

def combine_date_time_available(date_value: str, time_value: str) -> str:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if not re.fullmatch(r"\d{8}", date_text):
        return ""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", time_text)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return ""
    return f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]} {hour:02d}:{minute:02d}:{second:02d}+08:00"

def augment_macro_frame(df: pd.DataFrame, spec: MacroDataset) -> pd.DataFrame:
    out = df.copy()
    if "available_at" not in out.columns:
        out["available_at"] = ""
    if "available_at_rule" not in out.columns:
        out["available_at_rule"] = "missing_source_date"
    if spec.date_column and spec.date_column in out.columns:
        date_values = out[spec.date_column].astype(str).str.strip()
        if spec.time_column and spec.time_column in out.columns:
            time_values = out[spec.time_column].astype(str).str.strip()
            combined = [combine_date_time_available(day, clock) for day, clock in zip(date_values, time_values, strict=False)]
            combined_series = pd.Series(combined, index=out.index)
            mask = combined_series.astype(str).str.strip().ne("")
            out.loc[mask, "available_at"] = combined_series[mask]
            out.loc[mask, "available_at_rule"] = f"source:{spec.date_column}+{spec.time_column}"
        missing = out["available_at"].astype(str).str.strip().eq("")
        derived = date_values.map(period_available_at)
        available = derived.map(lambda item: item[0])
        rules = derived.map(lambda item: item[1])
        mask = missing & available.astype(str).str.strip().ne("")
        out.loc[mask, "available_at"] = available[mask]
        out.loc[mask, "available_at_rule"] = rules[mask]
    return out

def selected_macro_datasets(args: argparse.Namespace) -> list[str]:
    default = GLOBAL_CONTEXT_DEFAULT_DATASETS if args.tier == "global" else MACRO_REGIME_DEFAULT_DATASETS
    datasets = list(args.datasets or default)
    invalid = sorted(set(datasets) - set(MACRO_SPECS))
    if invalid:
        raise RuntimeError(f"unknown macro/global datasets: {invalid}; supported={sorted(MACRO_SPECS)}")
    return datasets

def macro_page_limit(spec: MacroDataset, requested: int | None) -> int:
    if spec.page_limit is not None:
        return min(requested or spec.page_limit, spec.page_limit)
    return requested or 10000

def selected_index_codes(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "index_code", None) or [] if str(value).strip()]
    return values or list(DEFAULT_GLOBAL_INDEX_CODES)

def selected_fx_codes(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "fx_code", None) or [] if str(value).strip()]
    return values or list(DEFAULT_FX_CODES)

def selected_libor_currencies(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip().upper() for value in getattr(args, "libor_currency", None) or [] if str(value).strip()]
    return values or list(DEFAULT_LIBOR_CURRENCIES)

def selected_eco_filter_values(args: argparse.Namespace, attr: str) -> list[str | None]:
    values = [str(value).strip() for value in getattr(args, attr, None) or [] if str(value).strip()]
    return values or [None]

def write_macro_result(path: Path, result: ApiResult, spec: MacroDataset, params: dict[str, Any]) -> int:
    df = augment_macro_frame(frame(result), spec)
    write_parquet(path, df, api_name=spec.api_name, params=params, fields=list(df.columns), source_hash=result.source_hash)
    return len(df)

def selected_event_flow_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(args.datasets or EVENT_FLOW_DATASETS)
    invalid = sorted(set(datasets) - set(EVENT_FLOW_SPECS))
    if invalid:
        raise RuntimeError(f"unknown event/flow datasets: {invalid}; supported={sorted(EVENT_FLOW_SPECS)}")
    return datasets

def event_page_limit(spec: EventDataset, requested: int | None) -> int:
    return min(requested or spec.page_limit, spec.page_limit)

def selected_board_trading_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(getattr(args, "datasets", None) or BOARD_TRADING_DEFAULT_DATASETS)
    invalid = sorted(set(datasets) - set(BOARD_TRADING_SPECS))
    if invalid:
        raise RuntimeError(f"unknown board-trading datasets: {invalid}; supported={sorted(BOARD_TRADING_SPECS)}")
    return datasets

def board_page_limit(spec: BoardTradingDataset, requested: int | None) -> int:
    return min(requested or spec.page_limit, spec.page_limit)

def selected_board_kpl_tags(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "kpl_tag", None) or [] if str(value).strip()]
    return values or list(BOARD_KPL_TAGS)

def selected_board_ths_limit_types(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "ths_limit_type", None) or [] if str(value).strip()]
    return values or list(BOARD_THS_LIMIT_TYPES)

def selected_board_ths_hot_markets(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "ths_hot_market", None) or [] if str(value).strip()]
    return values or list(BOARD_THS_HOT_MARKETS)

def selected_board_dc_hot_markets(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "dc_hot_market", None) or [] if str(value).strip()]
    return values or list(BOARD_DC_HOT_MARKETS)

def selected_board_dc_hot_types(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip() for value in getattr(args, "dc_hot_type", None) or [] if str(value).strip()]
    return values or list(BOARD_DC_HOT_TYPES)

def selected_board_hot_is_new(args: argparse.Namespace) -> list[str]:
    values = [str(value).strip().upper() for value in getattr(args, "hot_is_new", None) or [] if str(value).strip()]
    values = values or list(BOARD_HOT_IS_NEW)
    invalid = sorted(set(values) - {"Y", "N"})
    if invalid:
        raise RuntimeError(f"invalid hot is_new values: {invalid}; supported=['Y', 'N']")
    return values

def local_time(value: str, clock: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]} {clock}+08:00"

def event_available_at(value: str, api_name: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return "", "missing_source_date"
    if api_name in {"margin", "margin_detail"}:
        next_day = parse_yyyymmdd(text) + timedelta(days=1)
        return local_time(format_yyyymmdd(next_day), "09:00:00"), "official_next_day_09_from:trade_date"
    if api_name in {"moneyflow", "stk_holdertrade"}:
        return local_time(text, "19:00:00"), f"official_19_from:{'trade_date' if api_name == 'moneyflow' else 'ann_date'}"
    if api_name == "block_trade":
        return local_time(text, "21:00:00"), "official_21_from:trade_date"
    return local_eod(text), "conservative_date_eod"

def augment_event_frame(df: pd.DataFrame, spec: EventDataset) -> pd.DataFrame:
    out = df.copy()
    if "available_at" not in out.columns:
        out["available_at"] = ""
    if "available_at_rule" not in out.columns:
        out["available_at_rule"] = "missing_source_date"
    if spec.date_column and spec.date_column in out.columns:
        date_values = out[spec.date_column].astype(str).str.strip()
        derived = date_values.map(lambda item: event_available_at(item, spec.api_name))
        available = derived.map(lambda item: item[0])
        rules = derived.map(lambda item: item[1])
        mask = available.astype(str).str.strip().ne("")
        out.loc[mask, "available_at"] = available[mask]
        out.loc[mask, "available_at_rule"] = rules[mask]
    if spec.fallback_date_column and spec.fallback_date_column in out.columns:
        missing = out["available_at"].astype(str).str.strip().eq("")
        fallback_values = out[spec.fallback_date_column].astype(str).str.strip()
        derived = fallback_values.map(lambda item: event_available_at(item, spec.api_name))
        available = derived.map(lambda item: item[0])
        mask = missing & available.astype(str).str.strip().ne("")
        out.loc[mask, "available_at"] = available[mask]
        out.loc[mask, "available_at_rule"] = f"fallback_conservative_from:{spec.fallback_date_column}"
    return out

def write_event_result(path: Path, result: ApiResult, spec: EventDataset, params: dict[str, Any]) -> int:
    df = augment_event_frame(frame(result), spec)
    write_parquet(path, df, api_name=spec.api_name, params=params, fields=list(df.columns), source_hash=result.source_hash)
    return len(df)

def board_available_at(value: str, api_name: str, params: dict[str, Any] | None = None) -> tuple[str, str]:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return "", "missing_source_date"
    if api_name == "kpl_list":
        next_day = parse_yyyymmdd(text) + timedelta(days=1)
        return local_time(format_yyyymmdd(next_day), "08:30:00"), "official_next_day_0830_from:trade_date"
    if api_name == "limit_list_ths":
        return local_time(text, "16:00:00"), "official_16_from:trade_date"
    if api_name in {"top_list", "top_inst"}:
        return local_time(text, "20:00:00"), "official_20_from:trade_date"
    if api_name in {"ths_hot", "dc_hot"} and (params or {}).get("is_new") == "Y":
        return local_time(text, "22:30:00"), "official_latest_2230_from:trade_date"
    return local_eod(text), "conservative_date_eod"

def normalize_source_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {"nan", "None"}:
        return ""
    if re.fullmatch(r"\d{8} \d{2}:\d{2}:\d{2}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[9:]}+08:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return f"{text}+08:00"
    return text

def augment_board_frame(df: pd.DataFrame, spec: BoardTradingDataset, params: dict[str, Any] | None = None) -> pd.DataFrame:
    out = df.copy()
    if "available_at" not in out.columns:
        out["available_at"] = ""
    if "available_at_rule" not in out.columns:
        out["available_at_rule"] = "missing_source_time"
    if spec.time_column and spec.time_column in out.columns:
        source_time = out[spec.time_column].map(normalize_source_datetime)
        mask = source_time.astype(str).str.strip().ne("")
        out.loc[mask, "available_at"] = source_time[mask]
        out.loc[mask, "available_at_rule"] = f"source:{spec.time_column}"
    if spec.date_column and spec.date_column in out.columns:
        missing = out["available_at"].astype(str).str.strip().eq("")
        date_values = out[spec.date_column].astype(str).str.strip()
        derived = date_values.map(lambda item: board_available_at(item, spec.api_name, params))
        available = derived.map(lambda item: item[0])
        rules = derived.map(lambda item: item[1])
        mask = missing & available.astype(str).str.strip().ne("")
        out.loc[mask, "available_at"] = available[mask]
        out.loc[mask, "available_at_rule"] = rules[mask]
    if spec.strategy == "static_once" and out["available_at"].astype(str).str.strip().eq("").all():
        out["available_at_rule"] = "static_reference_no_pit_time"
    return out

def write_board_result(path: Path, result: ApiResult, spec: BoardTradingDataset, params: dict[str, Any]) -> int:
    df = augment_board_frame(frame(result), spec, params)
    write_parquet(path, df, api_name=spec.api_name, params=params, fields=list(df.columns), source_hash=result.source_hash)
    return len(df)

def selected_intraday_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(args.datasets or INTRADAY_DATASETS)
    aliases = {"stk_mins": STK_MINS_DATASET, "stk_mins_1min": STK_MINS_DATASET}
    normalized = [aliases.get(str(name), str(name)) for name in datasets]
    invalid = sorted(set(normalized) - set(INTRADAY_DATASETS))
    if invalid:
        raise RuntimeError(f"unknown intraday minute datasets: {invalid}; supported={INTRADAY_DATASETS}")
    return normalized

def clean_yyyymmdd(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{8}", text) else ""

def date_overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return start_a <= end_b and start_b <= end_a


def load_minute_universe(raw_dir: Path, args: argparse.Namespace) -> pd.DataFrame:
    files = sorted((raw_dir / "stock_basic").glob("list_status=*.parquet"))
    if not files:
        raise RuntimeError("stock_basic partitions are missing; run download --tier reference first")
    cols = ["ts_code", "name", "market", "exchange", "list_status", "list_date", "delist_date"]
    df = read_many(files, columns=cols)
    if df.empty:
        raise RuntimeError("stock_basic is empty; cannot build full-A minute universe")
    df = df.dropna(subset=["ts_code"]).copy()
    df["ts_code"] = df["ts_code"].astype(str).str.strip()
    df = df[df["ts_code"].ne("")].drop_duplicates("ts_code", keep="first")
    if getattr(args, "codes", None):
        wanted = {str(code).strip() for code in args.codes if str(code).strip()}
        df = df[df["ts_code"].isin(wanted)]
    df = df.sort_values("ts_code").reset_index(drop=True)
    if getattr(args, "max_codes", None):
        df = df.head(int(args.max_codes))
    if df.empty:
        raise RuntimeError("minute universe is empty after code/max-code filtering")
    return df


def active_year_windows(row: pd.Series, start_date: str, end_date: str) -> list[tuple[int, str, str]]:
    list_date = clean_yyyymmdd(row.get("list_date")) or start_date
    delist_date = clean_yyyymmdd(row.get("delist_date")) or end_date
    active_start = max(start_date, list_date)
    active_end = min(end_date, delist_date)
    if active_start > active_end:
        return []
    windows: list[tuple[int, str, str]] = []
    for year in range(int(active_start[:4]), int(active_end[:4]) + 1):
        year_start = max(active_start, f"{year}0101")
        year_end = min(active_end, f"{year}1231")
        if date_overlaps(year_start, year_end, start_date, end_date):
            windows.append((year, year_start, year_end))
    return windows


def minute_datetime(value: str, *, end: bool = False) -> str:
    suffix = "15:00:00" if end else "09:00:00"
    return f"{value[:4]}-{value[4:6]}-{value[6:8]} {suffix}"

def augment_stk_mins_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
    out = df.copy()
    out["trade_time"] = out["trade_time"].astype(str).str.strip()
    parsed = pd.to_datetime(out["trade_time"], errors="coerce")
    out["trade_date"] = parsed.dt.strftime("%Y%m%d").fillna("")
    out["available_at"] = parsed.dt.strftime("%Y-%m-%d %H:%M:%S+08:00").fillna("")
    out["available_at_rule"] = "source:trade_time_bar_close"
    sort_cols = [col for col in ["ts_code", "trade_time"] if col in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols).reset_index(drop=True)
    for col in STK_MINS_REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = "" if col in {"ts_code", "trade_time", "trade_date", "available_at", "available_at_rule"} else pd.NA
    return out[STK_MINS_REQUIRED_COLUMNS]

def stk_mins_by_date_path(raw_dir: Path, dataset: str, trade_date: str) -> Path:
    return raw_dir / dataset / f"trade_date={trade_date}.parquet"

def normalize_stk_mins_by_date_frame(df: pd.DataFrame, trade_date: str) -> tuple[pd.DataFrame, dict[str, int]]:
    if df.empty:
        out = pd.DataFrame(columns=STK_MINS_REQUIRED_COLUMNS)
        return out, {"input_rows": 0, "filtered_rows": 0, "duplicate_rows_dropped": 0}
    out = df.copy()
    for col in STK_MINS_REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = "" if col in {"ts_code", "trade_time", "trade_date", "available_at", "available_at_rule"} else pd.NA
    out = out[STK_MINS_REQUIRED_COLUMNS]
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out["trade_time"] = out["trade_time"].astype(str).str.strip()
    if "trade_date" not in out or out["trade_date"].astype(str).str.strip().eq("").all():
        parsed = pd.to_datetime(out["trade_time"], errors="coerce")
        out["trade_date"] = parsed.dt.strftime("%Y%m%d").fillna("")
    else:
        out["trade_date"] = out["trade_date"].astype(str).str.strip()
    before_filter = len(out)
    out = out[out["trade_date"] == trade_date].copy()
    if out["available_at"].astype(str).str.strip().eq("").any():
        parsed = pd.to_datetime(out["trade_time"], errors="coerce")
        mask = out["available_at"].astype(str).str.strip().eq("") & parsed.notna()
        out.loc[mask, "available_at"] = parsed[mask].dt.strftime("%Y-%m-%d %H:%M:%S+08:00")
    missing_rule = out["available_at_rule"].astype(str).str.strip().eq("")
    out.loc[missing_rule, "available_at_rule"] = "source:trade_time_bar_close"
    before_dedup = len(out)
    out = out.drop_duplicates(["ts_code", "trade_time"], keep="last")
    out = out.sort_values(["ts_code", "trade_time"]).reset_index(drop=True)
    return out, {
        "input_rows": int(before_filter),
        "filtered_rows": int(before_filter - len(out)),
        "duplicate_rows_dropped": int(before_dedup - len(out)),
    }

def intraday_day_time_details(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or "trade_time" not in df.columns:
        return {"unique_times": 0, "has_0930": False, "has_1500": False, "invalid_time_rows": int(len(df))}
    time_hhmm = df["trade_time"].astype(str).str.extract(r"(\d{2}:\d{2})", expand=False)
    valid = (
        (time_hhmm == "09:30")
        | ((time_hhmm >= "09:31") & (time_hhmm <= "11:30"))
        | ((time_hhmm >= "13:00") & (time_hhmm <= "15:00"))
    )
    times = set(time_hhmm.dropna().tolist())
    return {
        "unique_times": int(time_hhmm.nunique(dropna=True)),
        "has_0930": "09:30" in times,
        "has_1500": "15:00" in times,
        "invalid_time_rows": int((~valid.fillna(False)).sum()),
    }

def validate_stk_mins_by_date_frame(
    df: pd.DataFrame,
    trade_date: str,
    *,
    expected_codes: set[str] | None = None,
    min_rows: int = 0,
    allow_missing_codes: int = 0,
) -> tuple[bool, dict[str, Any]]:
    missing_columns = sorted(set(STK_MINS_REQUIRED_COLUMNS) - set(df.columns))
    details: dict[str, Any] = {
        "trade_date": trade_date,
        "rows": int(len(df)),
        "unique_codes": int(df["ts_code"].nunique()) if "ts_code" in df.columns else 0,
        "missing_columns": missing_columns,
        "duplicate_key_rows": 0,
        "wrong_trade_date_rows": 0,
        "unparseable_trade_time_rows": 0,
        "unparseable_available_at_rows": 0,
        "min_rows": int(min_rows),
        "expected_codes": len(expected_codes or set()),
        "missing_expected_codes": 0,
        "extra_codes": 0,
        "missing_expected_sample": [],
        "extra_code_sample": [],
    }
    if missing_columns:
        return False, details
    details["duplicate_key_rows"] = int(df.duplicated(["ts_code", "trade_time"]).sum())
    details["wrong_trade_date_rows"] = int((df["trade_date"].astype(str) != trade_date).sum())
    parsed_trade = pd.to_datetime(df["trade_time"].astype(str), errors="coerce")
    details["unparseable_trade_time_rows"] = int(parsed_trade.isna().sum())
    available = df["available_at"].astype(str).str.strip()
    parsed_available = pd.to_datetime(available[available.ne("")], errors="coerce", utc=True)
    details["unparseable_available_at_rows"] = int(parsed_available.isna().sum())
    details.update(intraday_day_time_details(df))
    if expected_codes is not None:
        actual_codes = set(df["ts_code"].dropna().astype(str))
        missing = sorted(expected_codes - actual_codes)
        extra = sorted(actual_codes - expected_codes)
        details["missing_expected_codes"] = len(missing)
        details["extra_codes"] = len(extra)
        details["missing_expected_sample"] = missing[:20]
        details["extra_code_sample"] = extra[:20]
    ok = not any([
        missing_columns,
        details["duplicate_key_rows"],
        details["wrong_trade_date_rows"],
        details["unparseable_trade_time_rows"],
        details["unparseable_available_at_rows"],
        not details["has_0930"] if len(df) else False,
        not details["has_1500"] if len(df) else False,
        len(df) < min_rows,
        details["missing_expected_codes"] > allow_missing_codes,
    ])
    return bool(ok), details

def intraday_expected_codes_for_day(raw_dir: Path, args: argparse.Namespace, trade_date: str) -> set[str] | None:
    source = getattr(args, "expected_codes_source", "none")
    if source == "none":
        return None
    if getattr(args, "codes", None):
        codes = {str(code).strip() for code in args.codes if str(code).strip()}
    elif source == "daily":
        daily_path = raw_dir / "daily" / f"trade_date={trade_date}.parquet"
        if not daily_path.exists():
            return set()
        if parquet_rows(daily_path) == 0:
            return set()
        daily = pd.read_parquet(daily_path, columns=["ts_code"])
        codes = set(daily["ts_code"].dropna().astype(str))
    else:
        universe = load_minute_universe(raw_dir, argparse.Namespace(codes=None, max_codes=None))
        active = []
        for _, row in universe.iterrows():
            list_date = clean_yyyymmdd(row.get("list_date")) or "00000000"
            delist_date = clean_yyyymmdd(row.get("delist_date")) or "99999999"
            if list_date <= trade_date <= delist_date:
                active.append(str(row["ts_code"]))
        codes = set(active)
    if getattr(args, "max_codes", None):
        codes = set(sorted(codes)[: int(args.max_codes)])
    return codes

def selected_integrated_intraday_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(getattr(args, "intraday_datasets", None) or INTRADAY_DATASETS)
    aliases = {"stk_mins": STK_MINS_DATASET, "stk_mins_1min": STK_MINS_DATASET}
    normalized = [aliases.get(str(name), str(name)) for name in datasets]
    invalid = sorted(set(normalized) - set(INTRADAY_DATASETS))
    if invalid:
        raise RuntimeError(f"unknown intraday minute datasets: {invalid}; supported={INTRADAY_DATASETS}")
    return normalized

def selected_text_datasets(args: argparse.Namespace) -> list[str]:
    datasets = list(args.datasets or TEXT_DEFAULT_DATASETS)
    invalid = sorted(set(datasets) - set(TEXT_SPECS))
    if invalid:
        raise RuntimeError(f"unknown text datasets: {invalid}")
    if "news" in datasets:
        selected_news_sources(args.news_src)
    return datasets

def selected_news_sources(values: list[str] | None) -> list[str]:
    requested = [str(value).strip() for value in values or [] if str(value).strip()]
    if not requested or any(value.lower() == "all" for value in requested):
        return list(NEWS_SOURCES)
    invalid = sorted(set(requested) - set(NEWS_SOURCES))
    if invalid:
        raise RuntimeError(f"unknown news source(s): {invalid}; official sources are {NEWS_SOURCES}")
    return requested

def text_page_limit(spec: TextDataset, requested: int | None) -> int:
    documented_limit = spec.page_limit
    if documented_limit is None:
        return requested or 10000
    if requested is None:
        return documented_limit
    return min(requested, documented_limit)

def date_range_days(start_date: str, end_date: str) -> list[str]:
    start = parse_yyyymmdd(start_date)
    end = parse_yyyymmdd(end_date)
    days = pd.date_range(start, end, freq="D")
    return days.strftime("%Y%m%d").tolist()

def as_datetime_window(value: str, *, end: bool = False) -> str:
    suffix = "23:59:59" if end else "00:00:00"
    return f"{value[:4]}-{value[4:6]}-{value[6:8]} {suffix}"

def safe_partition_value(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value).strip())
    return cleaned.strip("_") or stable_hash(value)[:12]

def augment_text_frame(df: pd.DataFrame, spec: TextDataset) -> pd.DataFrame:
    df = df.copy()
    if "available_at" not in df.columns:
        df["available_at"] = ""
        df["available_at_rule"] = "missing_source_time"
    if spec.time_column and spec.time_column in df.columns:
        source_time = df[spec.time_column].astype(str).str.strip()
        mask = source_time.ne("") & source_time.ne("nan") & source_time.ne("None")
        df.loc[mask, "available_at"] = source_time[mask]
        df.loc[mask, "available_at_rule"] = f"source:{spec.time_column}"
    if spec.date_column and spec.date_column in df.columns:
        date_value = df[spec.date_column].astype(str).str.strip()
        mask = (df["available_at"].astype(str).str.strip() == "") & date_value.str.fullmatch(r"\d{8}", na=False)
        suffix = " 22:00:00+08:00" if spec.api_name == "report_rc" else " 23:59:59+08:00"
        fallback = date_value.str.slice(0, 4) + "-" + date_value.str.slice(4, 6) + "-" + date_value.str.slice(6, 8) + suffix
        df.loc[mask, "available_at"] = fallback[mask]
        df.loc[mask, "available_at_rule"] = f"conservative_from:{spec.date_column}"
    return df

def add_raw_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw-dir", default="data/raw")

def add_daily_selection_args(parser: argparse.ArgumentParser, choices: list[str]) -> None:
    parser.add_argument("--datasets", nargs="+", choices=choices)
    parser.add_argument("--include-limit-list", action="store_true")

def add_macro_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index-code", action="append", default=[], help="Global index ts_code for index_global; repeatable. Defaults to a curated major-index list.")
    parser.add_argument("--fx-code", action="append", default=[], help="FX ts_code for fx_daily; repeatable. Defaults to USDCNH.FXCM.")
    parser.add_argument("--libor-currency", action="append", default=[], help="LIBOR currency code; repeatable. Defaults to USD/EUR/JPY/GBP/CHF.")
    parser.add_argument("--eco-country", action="append", default=[], help="Optional eco_cal country filter; repeatable. Omit for all countries.")
    parser.add_argument("--eco-currency", action="append", default=[], help="Optional eco_cal currency filter; repeatable. Omit for all currencies.")
    parser.add_argument("--eco-event", action="append", default=[], help="Optional eco_cal event fuzzy filter; repeatable. Omit for all events.")

def add_board_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kpl-tag", action="append", default=[], help="开盘啦榜单 tag; repeatable. Defaults to 涨停/炸板/跌停/自然涨停/竞价.")
    parser.add_argument("--ths-limit-type", action="append", default=[], help="同花顺涨跌停榜单 limit_type; repeatable. Defaults to all official THS pools.")
    parser.add_argument("--ths-hot-market", action="append", default=[], help="同花顺热榜 market; repeatable. Defaults to 热股/行业板块/概念板块.")
    parser.add_argument("--dc-hot-market", action="append", default=[], help="东方财富热榜 market; repeatable. Defaults to A股市场.")
    parser.add_argument("--dc-hot-type", action="append", default=[], help="东方财富热榜 hot_type; repeatable. Defaults to 人气榜/飙升榜.")
    parser.add_argument("--hot-is-new", action="append", default=[], help="Hot-list is_new value Y or N; repeatable. Defaults to N for PIT rank_time snapshots.")

def add_runtime_args(parser: argparse.ArgumentParser, *, min_interval: float | None, timeout: int | None) -> None:
    parser.add_argument("--min-interval-seconds", type=float, default=min_interval)
    parser.add_argument("--timeout-seconds", type=int, default=timeout)

def add_intraday_by_date_common_args(
    parser: argparse.ArgumentParser,
    *,
    expected_codes_choices: list[str] | None = None,
    expected_codes_default: str = "none",
) -> None:
    expected_choices = expected_codes_choices or ["none", "daily", "active"]
    add_raw_arg(parser)
    parser.add_argument("--start-date", default="20200101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--output-dataset", default=STK_MINS_BY_DATE_DATASET)
    parser.add_argument("--codes", nargs="+", help="Optional explicit ts_code list for window tests or targeted refreshes.")
    parser.add_argument("--max-codes", type=int, help="Optional first-N stock_basic/daily codes for window tests.")
    parser.add_argument(
        "--expected-codes-source",
        choices=expected_choices,
        default=expected_codes_default,
        help=f"Coverage universe for validation: {', '.join(expected_choices)}.",
    )
    parser.add_argument("--min-rows-per-day", type=int, default=0)
    parser.add_argument("--allow-missing-codes", type=int, default=0)
    parser.add_argument("--existing-allow-missing-codes", type=int, default=50)

if __name__ == "__main__":
    raise SystemExit(main())
