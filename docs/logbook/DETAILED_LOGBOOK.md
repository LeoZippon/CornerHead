## 2026-05-19 TuShare data requirement and permission probe

Task: identify required TuShare datasets/interfaces before bulk download for MacroQuant.

Repository/path checks:
- Logical cwd: `/home/coder/projects/adm-cube-l20-8884/macroquant-1741651ef8a3`
- Physical cwd confirmed with `pwd -P`: `/Data/lzp/MacroQuant`
- Git before probe: `## main...origin/main`, with previously added untracked `.gitignore`.

Credential handling:
- TuShare token was supplied by the user in chat.
- Token was only passed as a transient environment variable for HTTP API probes.
- Token was not written to tracked repo files, logs, docs, or command output during probes.
- After the user explicitly approved local environment storage, `TUSHARE_TOKEN` was written to ignored local `.env`; `.gitignore` was verified to ignore `.env`.

Resource checks:
- Before first probe: `nvidia-smi` showed GPUs 2-7 heavily occupied, but probes were CPU/network only; system memory `503Gi total`, about `422Gi available`.
- After probes: memory remained about `422Gi available`; no new GPU workload was created.

Environment:
- `~/miniconda3/bin/python` exists.
- Base Python has `pandas` and `requests`; `tushare` package is not installed.
- Permission probes used TuShare HTTP API directly and did not save returned data.

Probe result:
- Accessible: `stock_basic`, `stock_company`, `bak_basic`, `trade_cal`, `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `stk_mins`, `stk_auction`, `stk_auction_c`, `income_vip`, `balancesheet_vip`, `cashflow_vip`, `fina_indicator_vip`, `forecast_vip`, `express_vip`, `dividend`, `fina_mainbz_vip`, `disclosure_date`, `index_classify`, `index_member_all`, `margin`, `margin_detail`, `moneyflow`, `stk_holdernumber`, `stk_holdertrade`, `repurchase`, `share_float`, `block_trade`, `report_rc`, `major_news`, `cctv_news`, `research_report`.
- Not accessible: `anns_d` returned no-interface-permission error.
- Parameter issue: `fina_audit` requires `ts_code`; it should be downloaded per stock rather than by period-only probe.

Artifact:
- Added `docs/tushare_data_download_plan.md`, containing required datasets, API names, pull strategies, PIT constraints, probe summary, and recommended download order.

Conclusion:
- The current TuShare account is sufficient for first-stage formulaic daily WFO, financial PIT features, historical minute/auction workflow, and several optional event/flow datasets.
- Full natural-language evidence workflow still needs `anns_d` permission or an alternate official announcement source.

## 2026-05-19 P0 TuShare base table download

Task: download P0 TuShare base dimension tables first. User clarified that news/research interfaces were not purchased and should not be treated as default usable data.

Repository/path checks:
- Physical cwd confirmed earlier in the turn with `pwd -P` and `realpath .`: `/Data/lzp/MacroQuant`.
- Git before download: `## main...origin/main`, with tracked changes pending for `.gitignore`, docs, summaries, and local ignored `.env`.

Implementation:
- Fixed `.gitignore` root anchoring from `data/`/`logs/` to `/data/`/`/logs/`, because the broad `data/` pattern incorrectly ignored `scripts/data/`.
- Added `scripts/data/download_tushare_p0.py`.
- The script reads `TUSHARE_TOKEN` from environment or ignored local `.env`, writes Parquet partitions and sidecar metadata JSON, logs resource snapshots, supports skip-existing reruns, and warns on possible row-limit hits.
- `py_compile` passed for `scripts/data/download_tushare_p0.py`.

Command:

```bash
~/miniconda3/bin/python scripts/data/download_tushare_p0.py --start-date 20100101 --bak-start-date 20160101 --namechange-start-date 19900101 --end-date 20260519
```

Resource checks:
- Before run: `nvidia-smi` showed existing GPU workloads; the download did not create GPU workload. `free -h` showed about `416Gi` available.
- Script logged resource snapshots at run start, after the first `bak_basic` partition, and run finish.
- After run: `free -h` showed about `406Gi` available. GPU memory usage remained dominated by pre-existing workloads.

Artifacts:
- Data root: `data/raw/tushare/p0/`
- Log: `logs/tushare_p0_20260519_151803.log`
- Manifest: `data/raw/tushare/p0/manifest/p0_summary_20260519_152721.json`
- Disk usage: about `1022M`
- Parquet partitions: `3043`

Downloaded row counts:
- `stock_basic`: 5844 rows across 3 partitions. L=5519, D=325, P=0.
- `stock_company`: 6271 rows across 3 exchange partitions. SSE=2453, SZSE=3077, BSE=741.
- `trade_cal`: 11966 rows across 51 yearly/exchange partitions. SSE=5983, SZSE=5983, BSE=0.
- `bak_basic`: 10269338 rows across 2517 trade-date partitions. First nonempty date 20160809; last nonempty date 20260518; 173 early partitions are empty.
- `namechange`: 8244 rows across 437 monthly partitions; 245 monthly partitions are empty.
- `index_classify`: 511 rows, SW2021.
- `index_member_all`: 5847 rows across 31 SW2021 level-1 industry partitions.

Validation:
- `rg` over the run log found no `WARNING`, `ERROR`, `possible limit`, or `returned code` entries.
- `data/`, `logs/`, and `.env` are ignored by Git; `scripts/data/` is no longer ignored.

Conclusion:
- P0 base tables were downloaded successfully.
- `bak_basic` appears unavailable before 20160809 via this API and is empty for early 2016 dates.
- TuShare returned no BSE `trade_cal` rows; use SSE/SZSE calendars for A-share trading-day logic unless later validation finds a separate BSE calendar source.
- News/research interfaces are not part of the default download plan despite earlier probe rows.

## 2026-05-19 P0 data quality audit

Task: check whether downloaded P0 TuShare data is complete and whether there are missing-data problems.

Resource checks:
- Before audit: `pwd -P` confirmed `/Data/lzp/MacroQuant`; `free -h` showed about `405Gi` available; GPUs were occupied by pre-existing workloads but the audit was CPU/disk only.
- After audit: `free -h` showed about `404Gi` available; no new GPU workload was created.

Audit artifact:
- Ignored report written to `results/data_quality/p0_audit_20260519_153316.json`.

Checks performed:
- Parquet file count vs sidecar metadata count.
- Required partition presence for P0 interfaces.
- Key-field blanks and duplicate keys for `stock_basic`, `stock_company`, `trade_cal`, `bak_basic`, `namechange`, `index_classify`, and `index_member_all`.
- SSE/SZSE calendar date/open-day alignment.
- `bak_basic` expected open-date partition coverage, zero partitions, duplicate `(trade_date, ts_code)`, and partition filename/date consistency.
- Cross-table code coverage between `stock_basic` and `stock_company`.
- `namechange` local monthly result compared with a no-parameter TuShare probe.

Results:
- Filesystem: 3043 Parquet files and 3043 sidecar metadata JSON files; no temp files.
- `stock_basic`: 5844 rows, 5844 unique `ts_code`; L=5519, D=325; no duplicate `ts_code`; no blank required keys.
- `stock_company`: 6271 rows, 6271 unique `ts_code`; no duplicate `ts_code`; 12 rows have blank `com_name`. `stock_basic` has 16 codes absent from `stock_company`; `stock_company` has 443 codes absent from `stock_basic`.
- `trade_cal`: SSE/SZSE each have 5983 calendar rows and 3973 open days; SSE/SZSE date/open sets match. BSE returned 0 rows.
- `bak_basic`: all 2517 expected SSE open-date partitions exist from 20160101 to 20260519. Total rows 10269338. No blank keys, no duplicate `(trade_date, ts_code)`, no filename/date mismatches. There are 173 zero-row partitions; first nonempty date is 20160809; last nonempty date is 20260518. There are 26 zero-row partitions after the first nonempty date. Small reprobes showed sampled zero dates still return 0 from `bak_basic` while `daily` has rows, so these are source/interface holes rather than local write failures.
- `namechange`: all 437 monthly partitions exist, with 8244 rows and 4981 unique full-row keys; however, a no-parameter TuShare probe returned 10000 rows and 7018 unique full-row keys. Therefore the current monthly split is incomplete. The correct recovery path is per-`ts_code` download and full-row deduplication.
- `index_classify`: 511 rows, levels L1=31, L2=134, L3=346; no duplicate `index_code`.
- `index_member_all`: 31 L1 files, 5847 rows; all L1 industries covered; no blank `ts_code`; no full-row duplicates.

Conclusion:
- P0 is structurally present and mostly usable for initial research, but it is not fully complete.
- Do not use current `namechange` as complete ST/name-change history until it is redownloaded by `ts_code`.
- Do not treat `bak_basic` as a complete daily universe source; use it as a supplemental snapshot and fill/validate with `stock_basic`, `daily`, `daily_basic`, and industry tables.
- Do not rely on TuShare BSE `trade_cal` from this pull.

## 2026-05-19 Namechange per-code supplement

Task: supplement the incomplete monthly `namechange` pull identified by the P0 audit.

Implementation:
- Added `scripts/data/supplement_tushare_namechange.py`.
- Script reads `TUSHARE_TOKEN` from environment or ignored `.env`.
- It loads all unique `ts_code` values from `data/raw/tushare/p0/stock_basic/`, queries `namechange(ts_code=...)` once per code, writes raw per-code Parquet partitions with sidecar metadata, and builds a full-row-deduplicated combined table.
- Because the legacy monthly pull contained 39 records not returned by the per-code query, a best-current union table was also produced from `namechange_by_ts_code_dedup ∪ legacy monthly namechange`.

Command:

```bash
~/miniconda3/bin/python scripts/data/supplement_tushare_namechange.py
```

Resource checks:
- Before run: `free -h` showed about `429Gi` available. Existing GPU workloads were present; supplement was CPU/network only.
- Script logged `free -h` and `nvidia-smi` at run start and finish.
- After run: `free -h` again showed about `429Gi` available; no new GPU workload was created.

Artifacts:
- Raw per-code partitions: `data/raw/tushare/p0/namechange_by_ts_code/`
- Per-code dedup table: `data/raw/tushare/p0/namechange_combined/namechange_by_ts_code_dedup.parquet`
- Best-current union table: `data/raw/tushare/p0/namechange_combined/namechange_union_dedup.parquet`
- Log: `logs/tushare_namechange_by_ts_code_20260519_174730.log`
- Manifest: `data/raw/tushare/p0/manifest/namechange_by_ts_code_summary_20260519_175926.json`
- Union manifest: `data/raw/tushare/p0/manifest/namechange_union_summary_20260519_180049.json`
- Supplement audit: `results/data_quality/namechange_supplement_audit_20260519_180004.json`

Results:
- Stock codes requested: 5844.
- Raw per-code partitions: 5844.
- Codes with rows: 5843.
- Zero-row code: `TS0018.SH`.
- Raw per-code rows: 33740.
- Per-code full-row dedup rows: 19866.
- Legacy monthly unique rows: 4981.
- Legacy-only rows added to union: 39.
- Final union rows: 19905.
- Final union duplicate full rows: 0.
- Final union blank `ts_code` rows: 0.
- Final union unique `ts_code`: 5874.
- `start_date` range: 19901201 to 20260520.

Conclusion:
- The `namechange` gap from the monthly pull is materially improved.
- Use `data/raw/tushare/p0/namechange_combined/namechange_union_dedup.parquet` as the current best raw name-change/ST source.
- PIT logic must use `ann_date` or a derived `available_at`; `start_date` can be a future effective date relative to fetch/decision time.

## 2026-05-19 P0 storage and script cleanup

Task: simplify P0 storage, audit output, and data scripts after only P0 had been downloaded.

Repository/path checks:
- Physical cwd confirmed with `pwd -P` and `realpath .`: `/Data/lzp/MacroQuant`.
- `git status --short --ignored --branch` showed existing tracked edits plus ignored `.env`, `data/`, `logs/`, and `results/`.
- `AGENTS.md` already had a modification not made in this cleanup pass; it was left untouched.

Changes:
- Flattened local raw data from `data/raw/tushare/p0/*` to `data/raw/*`.
- Removed the duplicate `namechange` raw variants:
  - removed legacy monthly `data/raw/tushare/p0/namechange/`
  - removed per-code raw `data/raw/tushare/p0/namechange_by_ts_code/`
  - removed combined intermediate `data/raw/tushare/p0/namechange_combined/`
- Kept only the final union-dedup namechange table at `data/raw/namechange/namechange.parquet` with sidecar `namechange.parquet.meta.json`.
- Removed the old nested `data/raw/tushare/` hierarchy.
- Cleaned `results/data_quality/` down to a single current status file.
- Replaced three scripts with one:
  - removed `scripts/data/download_tushare_p0.py`
  - removed `scripts/data/supplement_tushare_namechange.py`
  - removed `scripts/data/audit_tushare_p0.py`
  - added `scripts/data/tushare_data.py`

New script interface:

```bash
~/miniconda3/bin/python scripts/data/tushare_data.py download-p0
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p0 --raw-dir data/raw --output results/data_quality/p0_status.json
```

Validation:
- `~/miniconda3/bin/python -m py_compile scripts/data/tushare_data.py` passed.
- Resource checks before audit: `nvidia-smi`; `free -h` showed about `428Gi` available.
- Ran `~/miniconda3/bin/python scripts/data/tushare_data.py audit-p0 --raw-dir data/raw --output results/data_quality/p0_status.json`.
- Resource checks after audit: `nvidia-smi`; `free -h` showed about `428Gi` available.
- No token was found in tracked files.

Final local layout:
- `data/raw/stock_basic/`
- `data/raw/stock_company/`
- `data/raw/trade_cal/`
- `data/raw/bak_basic/`
- `data/raw/namechange/namechange.parquet`
- `data/raw/index_classify/`
- `data/raw/index_member_all/`
- `results/data_quality/p0_status.json`
- `scripts/data/tushare_data.py`

Final counts:
- `data/raw`: about `1018M`.
- Parquet files: 2607.
- sidecar `.meta.json` files: 2607.
- `results/data_quality`: one file, `p0_status.json`.
- `scripts/data`: one script, `tushare_data.py`.

Audit result:
- Status: warning.
- Exit code: 0.
- Errors: 0.
- Warnings: 5.
- Infos: 8.

Remaining warnings:
- `stock_company`: 12 blank `com_name` rows.
- `stock_company` vs `stock_basic`: 16 `stock_basic` codes absent from `stock_company`; 443 `stock_company` codes absent from `stock_basic`.
- `trade_cal_BSE`: BSE calendar rows remain 0.
- `bak_basic`: 173 zero-row trade-date partitions, including 26 after first nonempty date.
- `index_member_all`: 9 member codes absent from `stock_basic`.

Conclusion:
- P0 storage and script surface are now simplified.
- Use `data/raw/namechange/namechange.parquet` as the sole namechange/ST raw source.
- Continue to treat `bak_basic` and BSE `trade_cal` warnings as source/interface limitations rather than local duplication problems.

## 2026-05-19 Repeatable P0 TuShare Audit Script

Task: implement and run a repeatable P0 TuShare data completeness audit that reads local `data/raw/tushare/p0/` only and writes reports under ignored `results/data_quality/`.

Implementation:
- Added `scripts/data/audit_tushare_p0.py`.
- The script supports `--p0-root` and `--output-dir`, does not download data, does not read or log `.env`, and emits machine-readable JSON plus a short Markdown report.
- Exit behavior: returns nonzero only when `error` findings exist; warnings alone return 0.
- Checks include Parquet/sidecar parity, tmp-file detection, partition presence, row counts, key blanks, duplicate keys, date coverage, and cross-table coverage for `stock_basic`, `stock_company`, `trade_cal`, `bak_basic`, legacy monthly `namechange`, `namechange_by_ts_code`, `namechange_combined/namechange_union_dedup.parquet`, `index_classify`, and `index_member_all`.

Command:

```bash
~/miniconda3/bin/python scripts/data/audit_tushare_p0.py --p0-root data/raw/tushare/p0 --output-dir results/data_quality
```

Resource checks:
- Before implementation: `pwd -P` confirmed `/Data/lzp/MacroQuant`; `git status --short --ignored --branch` showed pre-existing modifications in `SUMMARY.md`, `docs/summaries/SUMMARY.original.md`, untracked `.gitignore`, docs/scripts, and ignored `.env`, `data/`, `logs/`, `results/`.
- Before final audit run: `nvidia-smi` showed existing workloads on GPUs 0, 2, and 3; the audit was CPU/disk only. `free -h` showed about `428Gi` available memory.
- After final audit run: `nvidia-smi` still showed only the pre-existing GPU workloads; `free -h` again showed about `428Gi` available memory.

Artifacts:
- JSON report: `results/data_quality/p0_audit_20260519_181546.json`
- Markdown report: `results/data_quality/p0_audit_20260519_181546.md`

Results:
- Final exit code: 0.
- Finding counts: 0 errors, 5 warnings, 13 infos.
- Filesystem inventory: 8889 Parquet files and 8889 `.meta.json` sidecars; no temp files; no missing or orphan sidecars.
- `stock_basic`: 5844 rows and 5844 unique `ts_code`; no blank or duplicate primary keys; list statuses L=5519 and D=325.
- `stock_company`: 6271 rows and 6271 unique `ts_code`; 12 blank `com_name` rows. Cross-table coverage warning: `stock_basic` has 16 codes absent from `stock_company`; `stock_company` has 443 codes absent from `stock_basic`.
- `trade_cal`: SSE and SZSE each have 5983 rows and 3973 open days; calendars align. BSE `trade_cal` is empty in this local pull and is documented as a known source/interface limitation.
- `bak_basic`: 2517 local trade-date partitions from 20160104 to 20260519; no missing SSE open-day partitions within that local date range; 10269338 rows; no blank keys, duplicate `(trade_date, ts_code)`, or filename/date mismatches. Known source/interface warning: 173 zero-row partitions total, including 26 after first nonempty date; first nonempty date is 20160809 and last nonempty date is 20260518.
- Legacy monthly `namechange`: 437 partitions from 199001 to 202605, 8244 rows and 4981 unique full rows; still marked warning because the monthly split is known incomplete.
- `namechange_by_ts_code`: 5844 partitions matching all `stock_basic` codes; 33740 raw rows, 19866 unique full rows, one zero-row code `TS0018.SH`.
- `namechange_combined/namechange_union_dedup.parquet`: 19905 rows, 5874 unique `ts_code`, no blank `ts_code`, no duplicate full-key rows; `start_date` range 19901201 to 20260520.
- `index_classify`: 511 rows, levels L1=31, L2=134, L3=346; no blank or duplicate `index_code`.
- `index_member_all`: 31 L1 partitions and 5847 rows; no missing L1 partitions, no blank `ts_code`, no duplicate key rows. Warning: 9 member codes are absent from `stock_basic`.

Conclusion:
- The repeatable P0 audit is now available and the final run has no errors.
- Remaining findings are warnings to be handled by downstream policy: known `bak_basic` source holes, empty BSE calendar, legacy monthly `namechange` incompleteness mitigated by per-code/union tables, and cross-table code coverage differences.

## 2026-05-19 TuShare `bak_daily` vs `bak_basic` Interface Check

Task: check whether `bak_daily` is more complete than `bak_basic` before deciding whether to add it to the raw download set.

Checks:
- Used the TuShare MCP tools for `bak_daily`, `bak_basic`, and `daily` with minimal fields on selected dates.
- Cross-checked with local token probes that counted rows without persisting new raw data.
- Reviewed current TuShare interface definitions: `bak_basic` is a 2016-start backup basic/history-list table; `bak_daily` is a backup行情 table from around mid-2017 with richer price/market fields.

Resource checks:
- Before MCP/API checks: `pwd -P` confirmed `/Data/lzp/MacroQuant`; `nvidia-smi` showed pre-existing workloads on GPUs 0, 2, and 3; `free -h` showed about `428Gi` available memory.
- After MCP/API checks: GPU and memory state was materially unchanged; `free -h` still showed about `428Gi` available memory.

Selected findings:
- `20160809`: `bak_basic` had rows, `bak_daily` was empty, and `daily` had rows. This confirms `bak_daily` does not cover early `bak_basic` history.
- `20170703`: both `bak_basic` and `bak_daily` had rows.
- `20200102`: `bak_basic` was empty while `bak_daily` had rows, so `bak_daily` can fill at least some later `bak_basic` holes.
- `20260519`: both `bak_basic` and `bak_daily` had rows.

Conclusion:
- `bak_daily` is not globally more complete than `bak_basic`.
- It should be treated as a supplemental backup daily snapshot table, useful for some `bak_basic` gaps and for richer price/market fields, not as a replacement for `bak_basic`.
- If added to the pipeline, store it separately as `data/raw/bak_daily/` and audit it against `bak_basic`, `daily`, and `daily_basic`.

## 2026-05-19 P1 TuShare Daily Data Download

Task: supplement P1 daily market and trading-constraint data after P0 cleanup.

Implementation:
- Kept the single script surface and extended `scripts/data/tushare_data.py` with `download-p1` and `audit-p1`.
- Added generic trade-date partition download logic shared by `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, and optional `limit_list_d`.
- Added P1 audit checks for partition coverage, sidecar parity, tmp files, blank keys, duplicate keys, and filename/trade_date mismatches.
- Improved TuShare retry handling for Chinese rate-limit messages and changed the P1 default interval to `0.18` seconds after `stk_limit` hit the 400 calls/minute limit.

Key commands:

```bash
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --start-date 20260519 --end-date 20260519 --include-limit-list
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets daily
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets adj_factor
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets daily_basic
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets stk_limit
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets suspend_d
~/miniconda3/bin/python scripts/data/tushare_data.py download-p1 --datasets limit_list_d
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p1 --include-limit-list --output results/data_quality/p1_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p0 --raw-dir data/raw --output results/data_quality/p0_status.json
```

Resource checks:
- Before P1 work: `pwd -P` confirmed `/Data/lzp/MacroQuant`; `git status --short --branch` showed pre-existing modified `AGENTS.md`, modified summaries, untracked `.gitignore`, `check.ipynb`, docs, and scripts.
- Before and after each major download/audit step, `nvidia-smi` and `free -h` were checked. The downloads were CPU/network/disk only. Existing GPU processes remained on GPUs 0, 2, and 3; available system memory stayed around `427-428Gi`.
- Final disk snapshot: `data/raw` is about `3.9G`; `results/data_quality` about `20K`; local logs about `448K`.

Artifacts:
- Raw data: `data/raw/daily/`, `data/raw/adj_factor/`, `data/raw/daily_basic/`, `data/raw/stk_limit/`, `data/raw/suspend_d/`, `data/raw/limit_list_d/`.
- Audit status: `results/data_quality/p1_status.json`.
- Refreshed P0 status: `results/data_quality/p0_status.json`.
- Local logs: `logs/tushare_p1_*_20260519.log` and `logs/tushare_p0_audit_after_p1_20260519.log`.

Results:
- Final P1 audit status: `ok` with 0 errors, 0 warnings, 13 info findings.
- P1 sidecar inventory: 21407 Parquet files and 21407 `.meta.json` sidecars; no missing sidecars, orphan sidecars, or tmp files.
- Full raw inventory after P1: 24014 Parquet files and 24014 `.meta.json` sidecars; no tmp files.
- `daily`: 3973 partitions, 13919434 rows, 20100104-20260519, no missing or zero-row partitions.
- `adj_factor`: 3973 partitions, 14575372 rows, 20100104-20260519, no missing or zero-row partitions.
- `daily_basic`: 3973 partitions, 13828328 rows, 20100104-20260519, no missing or zero-row partitions.
- `stk_limit`: 3973 partitions, 16357937 rows, 20100104-20260519, no missing or zero-row partitions. Initial run hit TuShare 400 calls/minute at 20130424; rerun skipped existing partitions and completed with the safer interval.
- `suspend_d`: 3973 partitions, 467341 rows, 20100104-20260519; zero-row days are valid and are preserved as empty partitions when returned.
- `limit_list_d`: 1542 partitions, 153459 rows, 20200102-20260519; optional P1 table from 2020 onward.
- Row-limit sanity probes on `daily` 20221118, `adj_factor` 20220808, and `stk_limit` 20201027/20220705 with `limit=10000` and boundary `offset` found no truncation.

Conclusion:
- P1 raw data is downloaded and audited complete for the current plan.
- Current remaining P0 warnings are unchanged source/interface limitations: `stock_company` blanks/coverage mismatch, BSE `trade_cal` empty, known `bak_basic` zero-row dates, and 9 SW member codes absent from `stock_basic`.
- PIT feature construction must still enforce availability rules; do not use same-day `daily` or `daily_basic` for 09:25 decisions.

## 2026-05-19 P0/P1 TuShare Semantic Data Audit

Task: run a modifiable/repeatable data audit focused on completeness, unit consistency, `bak_*` semantics, cross-table coverage, and PIT risk.

Repository/path checks:
- `pwd -P` confirmed the working tree physical path as `/Data/lzp/MacroQuant` before edits and writes.
- Git working tree was already dirty with pre-existing `AGENTS.md`, `SUMMARY.md`, docs, `.gitignore`, `check.ipynb`, and `scripts/` changes; `AGENTS.md` and `check.ipynb` were not modified.

Implementation:
- Extended the single utility `scripts/data/tushare_data.py` with `audit-semantics`.
- The new command reads local P0/P1 Parquet and sidecar files, reuses the existing TuShare HTTP client for optional `--probe-api` checks, and writes `results/data_quality/data_semantics_status.json`.
- The command checks P0/P1 sidecar parity, temp files, trade-date partition completeness, key duplicates, filename/trade_date consistency, daily-vs-daily_basic coverage, adj_factor-vs-daily coverage, stk_limit-vs-daily coverage, local schemas, `bak_basic` volume/amount absence, small `bak_daily` unit probes, stock universe coverage, and PIT/available_at risk.
- `audit-semantics` now defaults its end date from the local SSE trade calendar max date when `--end-date` is omitted, avoiding server timezone drift beyond local data coverage.

Key commands:

```bash
~/miniconda3/bin/python -m py_compile scripts/data/tushare_data.py
~/miniconda3/bin/python scripts/data/tushare_data.py audit-semantics --include-limit-list --probe-api --end-date 20260519 --output results/data_quality/data_semantics_status.json
```

MCP/API checks:
- Used TuShare MCP `bak_daily`, `bak_basic`, and `daily` tools on selected dates, and local HTTP probes using the ignored `.env` token without printing or recording the token.
- Selected row probes: `20160809` API rows `bak_basic=2905`, `bak_daily=0`, `daily=2712`; `20170703` all `bak_basic/bak_daily` present with 3298 rows and `daily=3069`; `20200102` `bak_basic=0`, `bak_daily=3770`, `daily=3797`; `20260519` API rows `bak_basic=5523`, `bak_daily=5523`, `daily=5494`.
- Local/API mismatch: local `data/raw/bak_basic/trade_date=20260519.parquet` is still zero rows while live API now returns 5523 rows. This is a same-day fetch timing issue; no raw data was rewritten because current write scope did not include `data/raw`.

Resource checks:
- Before/after py_compile and before/after `audit-semantics`, `nvidia-smi` and `free -h` were checked.
- The audit was CPU/disk/network only and created no GPU workload. Existing GPU processes remained on GPUs 0, 2, and 3. Available system memory stayed about `427Gi`.
- Logs: `logs/tushare_data_semantics_20260520_0012.log` captured the first date-boundary run; `logs/tushare_data_semantics_20260520_0012_rerun.log` captured the successful explicit-window run.

Artifacts:
- Script: `scripts/data/tushare_data.py`
- Report: `results/data_quality/data_semantics_status.json`
- Documentation: `docs/tushare_data_download_plan.md`

Final result:
- `data_semantics_status.json`: status `warning`, 0 errors, 6 warnings, 20 info findings.
- P0/P1 sidecar inventory: 24014 Parquet files and 24014 `.meta.json` files; no missing sidecars, orphan sidecars, or temp files.
- P1 partition checks: `daily`, `adj_factor`, `daily_basic`, `stk_limit`, and `suspend_d` each have 3973 partitions from 20100104 to 20260519; `limit_list_d` has 1542 partitions from 20200102 to 20260519. No missing expected files, zero-row errors, duplicate keys, or filename/date mismatches.
- `bak_basic`: 2517 partitions, first nonempty 20160809, last local nonempty 20260518, 173 zero-row partitions total, 26 after the first nonempty date. It has no `vol` or `amount` fields and must not be used for turnover-unit alignment.
- `daily` vs `daily_basic`: `daily`-only keys total 91107 across 2148 dates, mostly `.BJ`; one `daily_basic`-only key (`000022.SZ` on 20131114). Downstream joins need explicit missing policy.
- `adj_factor` vs `daily`: `adj_factor` has 655938 extra same-day code keys and no missing daily keys. Sample extras overlap heavily with `suspend_d`, so row count greater than `daily` is reasonable.
- `stk_limit` vs `daily`: `stk_limit` has 2532258 extra keys because it includes A/B shares, funds/ETF-like codes, and non-trading/suspended names. It also has 93755 daily-only keys, mostly historical BJ codes, so limit-price joins must allow missing values and board-specific rules.
- Unit conclusions: `daily.vol` is hands and `daily.amount` is thousand CNY; `daily_basic` shares are 10k shares and market value is 10k CNY; `bak_basic` shares/assets use 100m units and has no turnover fields; `bak_daily.vol` matches `daily.vol`, `bak_daily.amount` is inferred as 10k CNY because `daily.amount / bak_daily.amount` is approximately 10, and `bak_daily` share/market-value fields convert to `daily_basic` by about 10000.
- Stock universe: `stock_basic` has 318 BSE/BJ codes and `daily` has 318 BJ codes; `daily` has 3 codes absent from `stock_basic`; `stock_basic` has 68 codes absent from local `daily`; `stock_company` still differs from `stock_basic` by 16 missing and 443 extra codes; SW member table still has 9 codes absent from `stock_basic`.
- PIT: raw rows do not contain `available_at`; most sidecars have `fetched_at`, but this is not enough for row-level PIT joins. Same-day `daily`/`daily_basic` must not be used for 09:25 decisions.

Conclusion:
- P0/P1 local structure is usable, but the dataset is not semantically risk-free.
- No broad re-download is required for P1.
- Optional recommended supplement: download `bak_daily` separately as `data/raw/bak_daily/` if a second行情口径审计源 is desired.
- Targeted refresh needed only if current-day `bak_basic` 20260519 is required; otherwise treat current-day `bak_basic` as unavailable/stale and rely on `daily`/`daily_basic` with PIT-safe timing rules.

## 2026-05-20 P2 TuShare Financial Data Download

Task: continue from P0/P1 and download P2 financial and fundamental data.

Implementation:
- Extended the existing single utility `scripts/data/tushare_data.py` with `download-p2` and `audit-p2`.
- Added three P2 partition strategies:
  - `period=YYYYMMDD` for `income_vip`, `balancesheet_vip`, `cashflow_vip`, `fina_indicator_vip`, and `disclosure_date`.
  - `ann_month=YYYYMM` for `forecast_vip` and `express_vip`.
  - `ts_code=<code>` for `dividend`, `fina_audit`, and `fina_mainbz_vip`.
- Added paged TuShare querying with `limit`/`offset` and sidecar metadata preserving page count and fetch params.
- Kept raw financial records un-deduplicated because PIT feature construction must select the version visible at decision time.

Key commands:

```bash
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --start-date 20260401 --end-date 20260520 --max-codes 5
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets income_vip balancesheet_vip cashflow_vip fina_indicator_vip disclosure_date --start-date 20250101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets income_vip balancesheet_vip cashflow_vip fina_indicator_vip disclosure_date --start-date 20100101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets forecast_vip express_vip --start-date 20100101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets dividend --start-date 20100101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets fina_audit --start-date 20100101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py download-p2 --datasets fina_mainbz_vip --start-date 20100101 --end-date 20260520
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p2 --start-date 20100101 --end-date 20260520 --output results/data_quality/p2_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p0 --raw-dir data/raw --output results/data_quality/p0_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p1 --include-limit-list --end-date 20260519 --output results/data_quality/p1_status.json
```

Resource checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant` before writes.
- `nvidia-smi` and `free -h` were checked before and after each major P2 run. The downloads/audits were CPU/network/disk only and did not create GPU workloads; existing GPU jobs changed independently during the run.
- Available system memory stayed about `400-411Gi`.
- Final raw size: about `4.7G`.

Artifacts:
- Raw P2 directories: `data/raw/income_vip/`, `data/raw/balancesheet_vip/`, `data/raw/cashflow_vip/`, `data/raw/fina_indicator_vip/`, `data/raw/forecast_vip/`, `data/raw/express_vip/`, `data/raw/dividend/`, `data/raw/fina_audit/`, `data/raw/fina_mainbz_vip/`, `data/raw/disclosure_date/`.
- Audit status: `results/data_quality/p2_status.json`.
- Refreshed status: `results/data_quality/p0_status.json`, `results/data_quality/p1_status.json`.
- Local logs: `logs/tushare_p2_*_20260520.log`, `logs/tushare_p0_audit_after_p2_20260520.log`, `logs/tushare_p1_audit_after_p2_20260520.log`.

Results:
- Final P2 audit status: `warning`, with 0 errors, 5 warnings, and 16 info findings.
- P2 sidecar inventory: 18251 Parquet files and 18251 `.meta.json` sidecars; no missing sidecars, orphan sidecars, or tmp files.
- Full raw inventory after P2: 42265 Parquet files and 42265 `.meta.json` sidecars.
- `income_vip`: 65 period partitions, 342098 rows, 20100331-20260331.
- `balancesheet_vip`: 65 period partitions, 346082 rows, 20100331-20260331.
- `cashflow_vip`: 65 period partitions, 301154 rows, 20100331-20260331.
- `fina_indicator_vip`: 65 period partitions, 523405 rows, 20100331-20260331.
- `forecast_vip`: 197 ann-month partitions, 131542 rows, 201001-202605.
- `express_vip`: 197 ann-month partitions, 27912 rows, 201001-202605; 68 zero-row months are expected sparse-event months.
- `dividend`: 5844 ts_code partitions, 167859 rows; 25 zero-row codes.
- `fina_audit`: 5844 ts_code partitions, 95973 rows; 2 zero-row codes.
- `fina_mainbz_vip`: 5844 ts_code partitions, 2090826 rows; 3 zero-row codes.
- `disclosure_date`: 65 period partitions, 253063 rows, 20100331-20260331.

Warnings and interpretation:
- `income_vip`, `balancesheet_vip`, and `cashflow_vip` have duplicate audit keys but no full-row duplicates. These are retained as raw multi-version/source rows; downstream PIT construction must choose records by `f_ann_date` and decision time.
- `fina_indicator_vip` has duplicate `(ts_code, ann_date, end_date)` rows and 8 blank `ann_date` rows. This interface lacks `f_ann_date`; use it conservatively.
- `dividend` has 653 blank `ann_date` rows, 386 full-row duplicates, and 750 duplicate business-key rows. Raw is preserved; feature construction should deduplicate by business key and derive availability from `imp_ann_date`, `ex_date`, or `record_date` where appropriate.

Conclusion:
- P2 is downloaded and structurally complete; no broad redownload is required.
- P2 is revision-sensitive and should not be joined directly without PIT version selection and business-key deduplication.
- P1 remains `ok` when audited to `20260519`; a default `20260520` audit errors because the local trading calendar currently ends at `20260519`.

## 2026-05-24 P0/P1/P2 Integrated Data Quality and Semantic Audit

Task: run a modifiable, repeatable audit of current P0/P1/P2 TuShare data with emphasis on completeness, unit consistency, cross-table semantics, and local case studies.

Repository/path checks:
- `pwd -P` confirmed the physical repository path as `/Data/lzp/MacroQuant` before writes.
- `git status --short --branch` showed pre-existing modified `AGENTS.md`, `CLAUDE.md`, `SUMMARY.md`, `docs/heuristic_learning_trading_system.md`, `docs/summaries/SUMMARY.original.md`, and untracked `.gitignore`, `check.ipynb`, docs, and scripts.
- Restricted files `AGENTS.md`, `CLAUDE.md`, `check.ipynb`, and `docs/heuristic_learning_trading_system.md` were not modified.

Implementation:
- Extended the existing single utility `scripts/data/tushare_data.py` with `audit-integrated`.
- The new command reuses the existing P0/P1/P2 audit helpers, includes P0/P1 cross-table semantic checks, adds P2 integrated completeness and PIT/unit checks, and writes case studies into `results/data_quality/p0_p1_p2_integrated_status.json`.
- `~/miniconda3/bin/python -m py_compile scripts/data/tushare_data.py` passed.

Key commands:

```bash
~/miniconda3/bin/python -m py_compile scripts/data/tushare_data.py
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p0 --raw-dir data/raw --output results/data_quality/p0_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p1 --include-limit-list --end-date 20260519 --output results/data_quality/p1_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-p2 --start-date 20100101 --end-date 20260520 --output results/data_quality/p2_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-semantics --include-limit-list --probe-api --end-date 20260519 --output results/data_quality/data_semantics_status.json
~/miniconda3/bin/python scripts/data/tushare_data.py audit-integrated --include-limit-list --probe-api --end-date 20260519 --p2-end-date 20260520 --output results/data_quality/p0_p1_p2_integrated_status.json
```

Resource checks:
- Before audit batch: `nvidia-smi` showed existing GPU jobs on GPUs 0 and 1; the audits were CPU/disk/network only. `free -h` showed about `441Gi` available.
- After audit batch: `nvidia-smi` still showed only the pre-existing GPU jobs; `free -h` showed about `440Gi` available.

Artifacts:
- Script: `scripts/data/tushare_data.py`
- Status files: `results/data_quality/p0_status.json`, `p1_status.json`, `p2_status.json`, `data_semantics_status.json`, `p0_p1_p2_integrated_status.json`
- Logs: `logs/tushare_p0_audit_integrated_20260524.log`, `logs/tushare_p1_audit_integrated_20260524.log`, `logs/tushare_p2_audit_integrated_20260524.log`, `logs/tushare_semantics_audit_integrated_20260524.log`, `logs/tushare_integrated_audit_20260524.log`
- Documentation: `docs/tushare_data_download_plan.md`

Results:
- `p0_status.json`: warning, 0 errors, 5 warnings, 8 infos.
- `p1_status.json`: ok, 0 errors, 0 warnings, 13 infos.
- `p2_status.json`: warning, 0 errors, 5 warnings, 16 infos.
- `data_semantics_status.json`: warning, 0 errors, 6 warnings, 20 infos.
- `p0_p1_p2_integrated_status.json`: warning, 0 errors, 15 warnings, 36 infos.
- P0/P1/P2 inventory: 42265 Parquet files and 42265 `.meta.json` sidecars; 0 missing sidecars, 0 orphan sidecars, 0 tmp files, and no missing dataset directories.

Core findings:
- P0 source warnings are unchanged: `stock_company` coverage/name blanks, BSE `trade_cal` empty, `bak_basic` zero-row dates, and 9 SW member codes absent from `stock_basic`.
- P1 remains structurally complete through `20260519` including optional `limit_list_d`.
- `bak_basic` has 173 zero-row trade-date partitions and 26 after the first nonempty date. Local `20260519` remains 0 rows, while the live API probe returned 5523 rows.
- `daily` vs `daily_basic`: 91107 cumulative `daily`-only keys, mostly BJ history; one `daily_basic`-only key.
- `adj_factor` has 655938 extra keys versus `daily`, and `daily` has no missing `adj_factor`; this is reasonable for suspended/non-trading names.
- `stk_limit` covers A/B shares and funds, so it has 2532258 cumulative `stk_limit`-only keys; `daily` also has 93755 `stk_limit`-missing keys, mainly historical BJ names.
- Unit cases confirmed `bak_daily.vol` is directly comparable to `daily.vol`; `bak_daily.amount` is 10k CNY and needs x10 to compare with `daily.amount` in thousand CNY; `bak_daily.total_share/total_mv` require about x10000 to compare with `daily_basic` share and market-value fields.
- P2 statement tables are structurally complete for the downloaded range but revision-sensitive. `income_vip`, `balancesheet_vip`, and `cashflow_vip` have duplicate business keys without full-row duplication; `fina_indicator_vip` lacks `f_ann_date`; `dividend` has blank `ann_date` and duplicate business keys.

Case studies written to integrated status:
- `bak_daily_unit_conversion_api_probe`: examples include `000001.SZ` on `20200102`, where `daily.amount / bak_daily.amount` is about 10 and `daily_basic.total_mv / bak_daily.total_mv` is about 10000.
- `bak_basic_bak_daily_coverage_api_probe`: `20160809` has `bak_daily=0` while `bak_basic/daily` have rows; `20200102` has `bak_basic=0` while `bak_daily/daily` have rows.
- `daily_vs_daily_basic_coverage_case`: `20210906` has 145 more `daily` codes than `daily_basic`.
- `p2_financial_pit_case`: `874142.BJ` / `20251231` has multiple `income_vip` rows across announcement/report versions.
- `p2_duplicate_business_key_case`: `601696.SH` in `income_vip period=20100630` has two rows for the same business key but not full-row duplicates.
- `dividend_blank_ann_date_case`: `000001.SZ` historical dividend records have blank `ann_date` but populated `imp_ann_date`, `record_date`, `ex_date`, and `pay_date`.

Conclusion:
- No broad P1/P2 redownload is required.
- Targeted refresh is only needed if the local `bak_basic` `20260519` snapshot will be used.
- Optional next data supplement: download `bak_daily` into `data/raw/bak_daily/` as an independent backup行情口径审计源.
- The next engineering step should be a PIT feature layer that normalizes units and applies explicit availability times before modeling or 09:25 decision logic.

## 2026-05-24 - TuShare CLI and status consolidation

Task:
- Answer whether the many audit commands/status files were necessary, then consolidate while keeping a single editable script file.

Changes:
- Kept `scripts/data/tushare_data.py` as the only TuShare data utility.
- Replaced the public CLI surface with two commands: `download --tier {p0,p1,p2}` and `audit`.
- Removed obsolete public wrapper commands for `download-p0`, `audit-p0`, `download-p1`, `audit-p1`, `download-p2`, `audit-p2`, `audit-semantics`, and `audit-integrated`.
- Removed unused old audit wrapper/helper functions after the unified audit path was in place; script length reduced from 2087 to 1748 lines.
- Updated `docs/tushare_data_download_plan.md` to document `results/data_quality/status.json` as the primary status file.

Key commands:
```bash
pwd -P
git status --short --branch
~/miniconda3/bin/python -m py_compile scripts/data/tushare_data.py
~/miniconda3/bin/python scripts/data/tushare_data.py --help
~/miniconda3/bin/python scripts/data/tushare_data.py audit --include-limit-list --probe-api --end-date 20260519 --p2-end-date 20260520 --output results/data_quality/status.json
```

Resource checks:
- Before/after compile/help/audit, `nvidia-smi` and `free -h` were checked.
- GPU usage stayed unchanged from pre-existing jobs: GPU 0 had an existing Python process using about `1792MiB`; the data audit did not start GPU work.
- System memory stayed safe: about `445Gi` available before and after the run.

Artifacts:
- Script: `scripts/data/tushare_data.py`
- Primary status: `results/data_quality/status.json`
- Documentation: `docs/tushare_data_download_plan.md`

Results:
- `py_compile` passed.
- `scripts/data/tushare_data.py --help` now shows only `{download,audit}`.
- Unified audit output: status `warning`, 0 errors, 16 warnings, 40 info findings.
- Main warnings remain known source/semantic issues: `stock_company` coverage/name blanks, BSE `trade_cal` empty, stale local `bak_basic` current-day snapshot, `daily`/`daily_basic`/`stk_limit` coverage semantics, `bak_daily` unit/coverage probe mismatch, missing row-level PIT `available_at`, and P2 multi-version/dividend key semantics.

Conclusion:
- The status file should not be edited manually. Regenerate it with the unified `audit` command.
- Old per-tier status files can be left as historical local artifacts, but they are no longer the authoritative current state.

## 2026-05-24 - Heuristic Learning code framework scaffold

Task:
- Review the updated quant blueprint and start a code framework without blindly following every design detail.

Blueprint interpretation:
- The updated `docs/heuristic_learning_trading_system.md` now treats formulaic rules, natural-language logic, and execution policy as separate Heuristic objects.
- The largest engineering change is that `TradeStrategyPolicy`, daily replay, broker constraints, event checkpoints, and trial ledger are now first-class concerns.
- Given current data availability, the first practical target remains a 2020+ daily formulaic/PIT experiment. LLM Agent evolution, natural-language final decisions, intraday execution, and true inventory trading should wait until the PIT/replay layer is reliable.

Changes:
- Added `pyproject.toml` for a src-layout Python package.
- Added `src/hl_trader/schemas/` with `HorizonTrack`, `Protocol`, `TradeStrategyPolicy`, `HeuristicTemplate`, `ExperimentConfig`, and config loading.
- Added `src/hl_trader/data/` with TuShare data contracts and PIT partition readers.
- Added `src/hl_trader/wfo/` with rolling fold generation.
- Added `src/hl_trader/execution/` with a daily `BrokerSimulator` covering A-share lot sizing, T+1 settlement, basic costs, suspend, and limit-price blocking.
- Added `src/hl_trader/backtest/` with a minimal `DailyReplayEngine`.
- Added `src/hl_trader/heuristics/`, `portfolio/`, `evaluation/`, and `storage/` for formulaic scoring, target weights, metrics, and JSONL trial ledger.
- Added `configs/experiments/pilot_2020_daily.yaml` as the first 2020+ daily value/quality pilot config.
- Added `docs/quant_framework_notes.md` to record the framework decisions and deferrals.
- Added unit tests under `tests/unit/`.

Key commands:
```bash
pwd -P
git status --short --branch
nvidia-smi
free -h
PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests
PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Resource checks:
- Before and after test runs, `nvidia-smi` showed only the pre-existing GPU 0 Python process using about `1792MiB`; this scaffold/test work did not use GPU.
- `free -h` stayed safe, around `446Gi` available.

Results:
- `compileall` passed.
- Unit tests: 9 tests passed.
- The framework is intentionally not a full strategy yet; it is a stable spine for PIT feature construction, WFO, daily replay, broker constraints, and trial ledger before adding Agent evolution.

## 2026-05-24 - HL pre-LLM API hardening

Task:
- Complete the Heuristic Learning steps that should exist before connecting a large-model API, with editable SubAgent audits after major steps.

Changes:
- Added PIT daily feature construction in `src/hl_trader/features/daily_pit.py` and leakage checks in `src/hl_trader/leakage/checks.py`.
- Added a formulaic WFO runner in `src/hl_trader/wfo/formulaic_runner.py` and hardened daily broker/replay behavior in `src/hl_trader/execution/broker.py`, `src/hl_trader/backtest/daily_replay.py`, and `src/hl_trader/portfolio/weights.py`.
- Added offline evidence/event/NL shadow modules in `src/hl_trader/evidence/`, `src/hl_trader/events/`, and `src/hl_trader/agents/nl_shadow.py`.
- Hardened `TrialLedger` hashing/timestamps in `src/hl_trader/storage/ledger.py`.
- Expanded unit tests for PIT feature leakage, WFO/replay, execution constraints, evidence packs, event checkpoints, stable hashes, and NL shadow isolation.
- Updated `docs/quant_framework_notes.md` with the pre-LLM boundaries.

SubAgent audits:
- PIT/data feature audit was run and closed. Main fixes: per-symbol rolling calculations, no current adjusted-price alpha features, duplicate-key fail-fast, PIT date handling, and stricter leakage checks.
- WFO/replay audit was run and closed. Main fixes: train/test tradable-date boundaries, duplicate price/constraint fail-fast, turnover caps, T+1/limit-price checks, chronological replay, and richer daily-close ledger state.
- Evidence/event/NL shadow audit was run and closed. Main fixes: evidence PIT metadata and unit records, pack/ledger hash verification, no-future cross-section checks, explicit TuShare `pct_chg` percent semantics, and `can_affect_trading=False` NL shadow records.

Key commands:
```bash
pwd -P
git status --short --branch
nvidia-smi
free -h
PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests
PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Resource checks:
- Before and after compile/test runs, `nvidia-smi` and `free -h` were checked.
- GPU use stayed unchanged from the pre-existing GPU 0 Python process using about `1792MiB`; these framework tests did not use GPU.
- System memory stayed safe, about `445Gi` available at the final check.

Results:
- `compileall` passed.
- Unit tests passed: 36 tests OK.
- The repo now has a pre-LLM daily research spine: PIT features, leakage checks, formulaic WFO, daily replay/execution simulation, evidence packs, event checkpoints, NL shadow logging, and hash-verifiable ledgers.

Conclusion:
- This is ready for initial 2020+ offline formulaic HL experiments and shadow natural-language logging.
- Actual LLM API integration should remain isolated behind the shadow/evidence layer until held-out policy, prompt/version freeze, and API logging rules are explicit.

## 2026-05-25 - LLM shadow API integration with DeepSeek provider

Task:
- Add the first complete LLM API integration code path for the HL system with DeepSeek as the initial provider, while preserving the shadow-only boundary and running SubAgent audits after each major step.

Reference check:
- Consulted official DeepSeek API docs. Key implementation details used: OpenAI-compatible base URL `https://api.deepseek.com`, chat endpoint `/chat/completions`, current models such as `deepseek-v4-flash` / `deepseek-v4-pro`, JSON mode via `response_format={"type":"json_object"}`, prompt must mention JSON, retryable errors include 429/500/503, and `user_id` is supported for isolation.

Changes:
- Added `src/hl_trader/llm/`:
  - `DeepSeekConfig`, `DeepSeekClient`, `ChatMessage`, `DeepSeekResponse`, and `DeepSeekAPIError`.
  - No OpenAI SDK dependency; uses Python stdlib HTTP.
  - Supports JSON mode, model validation, `thinking`, `reasoning_effort`, `user_id`, retry handling, and secret redaction.
  - Reads key from `DEEPSEEK_API_KEY` in environment or ignored `.env`; does not print or store the key in tracked files.
- Added provider-agnostic `src/hl_trader/agents/llm_shadow.py` and `prompts.py`:
  - Builds JSON-only, shadow-only prompts from verified evidence packs and event checkpoints.
  - Requires exactly one model decision per input `ts_code`.
  - Unknown, duplicate, or missing codes fail-fast.
  - Illegal actions are downgraded to `human_review`; NL shadow objects still force `nl_weight=0.0` and `can_affect_trading=False`.
  - Provider metadata is sanitized before writing to ledger.
- Extended `src/hl_trader/agents/nl_shadow.py`:
  - Centralized action whitelist.
  - Rejected invalid direct NL shadow actions.
  - Added provider metadata sanitization for API traces.
- Added provider-agnostic `src/hl_trader/pipelines/llm_shadow.py` and `scripts/hl/llm_shadow.py`:
  - Supports existing evidence JSONL input or feature-file input.
  - Feature-file path validates PIT metadata, builds evidence pack, detects price/amount/limit checkpoints, then runs shadow advisor.
  - `--dry-run` is pure local validation and ledger recording; it does not construct a DeepSeek client and does not require an API key.
  - Default outputs are under ignored `data/evidence_packs/` and `experiments/trial_ledger/`.
- Updated `docs/quant_framework_notes.md` with the LLM shadow path and dry-run boundary.
- Added unit coverage for client, advisor, and pipeline/CLI.

SubAgent audits:
- API client audit was run and closed. Fixes included hiding `api_key` from dataclass repr, validating `reasoning_effort` and `user_id`, enforcing JSON-object responses, redacting error body secrets, and retrying 429/500/503.
- Shadow advisor audit was run and closed. Fixes included pre-call evidence hash verification, exact ts_code coverage, duplicate/unknown/missing code fail-fast, action whitelist centralization, stronger prompt constraints, and provider metadata redaction.
- Pipeline/CLI audit was run and closed. Fixes included pure dry-run construction without API key, required PIT columns for feature-file input, existing evidence hash validation, default ignored output paths, and CLI dry-run subprocess coverage.

Key commands:
```bash
pwd -P
git status --short --branch
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/llm_shadow.py --help
```

Resource checks:
- Before and after test runs, `nvidia-smi` and `free -h` were checked.
- Tests did not start GPU work. Existing external GPU jobs changed independently during the session.
- Final system memory was safe, about `441Gi` available.

Results:
- `compileall` passed.
- Full unit discovery passed: 65 tests OK.
- CLI help passed.
- CLI dry-run is covered by subprocess tests for both feature-file and existing evidence JSONL paths.
- Secret pattern scan outside ignored runtime directories found 0 file matches.
- No real DeepSeek API call was made; no API balance was consumed.

Conclusion:
- The first LLM integration is complete as a shadow-only path with DeepSeek as the initial provider: PIT evidence -> optional checkpoint context -> JSON-mode prompt -> NL shadow ledger.
- The model output still cannot affect orders, weights, broker execution, or PnL.
- A live API smoke test can be run later once `DEEPSEEK_API_KEY` is placed in the local environment or ignored `.env`.

## 2026-05-25 - Provider-agnostic LLM shadow rename audit

Task:
- Audit the provider-agnostic rename for the shadow decision entrypoint, keeping public API/file/class/CLI/event/output naming on `llm_shadow` while allowing DeepSeek names only in provider-specific adapter paths and explicit provider options.

Repository/path checks:
- `pwd -P` and `realpath .` confirmed the physical repository path as `/Data/lzp/MacroQuant` before edits.
- Initial `git status --short --branch` showed pre-existing modified/untracked files; this audit only changed files in the requested scope and did not revert unrelated work.

Findings:
- No legacy DeepSeek-specific shadow-entrypoint names were found in the requested files or non-runtime repo paths.
- Public event/default output paths use `llm_shadow`: `llm_shadow_pack`, `llm_shadow_dry_run`, `data/evidence_packs/llm_shadow.jsonl`, and `experiments/trial_ledger/llm_shadow.jsonl`.
- Shadow-only boundary remains intact: LLM decisions become `NLShadowDecision` records with `nl_weight=0.0`, `action_impact=shadow_only`, and `can_affect_trading=False`; pipeline pack/dry-run records also carry `can_affect_trading=False`.
- Tests use fake clients, mocks, and dry-run subprocess coverage; no real API call is made and API-key-like values are redacted in metadata tests.

Changes:
- Updated `src/hl_trader/pipelines/llm_shadow.py` so generic `LLMShadowRunConfig.model` defaults to `None`, and `from_deepseek_env()` only passes a model override when explicitly supplied. The DeepSeek adapter now owns the default model.
- Updated `scripts/hl/llm_shadow.py` so `--model` is documented as a provider model override that defaults to the provider adapter default.
- Updated `tests/unit/test_llm_shadow_advisor.py` and `tests/unit/test_llm_shadow_pipeline.py` so generic advisor/pipeline tests use `provider_name="test-provider"` instead of binding generic tests to DeepSeek.
- Updated `docs/quant_framework_notes.md` to describe the system as LLM shadow plus a DeepSeek provider adapter, not a DeepSeek-bound shadow system.

Key commands:
- Legacy DeepSeek-specific shadow-entrypoint `rg` scan returned no matches; the exact alternation is intentionally not written into tracked docs to avoid creating stale-name matches in future audits.
```bash
pwd -P
realpath .
git status --short --branch
rg -n "DeepSeek|deepseek|deepseek-v4|DEEPSEEK" src/hl_trader/agents/llm_shadow.py src/hl_trader/agents/prompts.py src/hl_trader/agents/__init__.py src/hl_trader/pipelines/llm_shadow.py src/hl_trader/pipelines/__init__.py scripts/hl/llm_shadow.py tests/unit/test_llm_shadow_advisor.py tests/unit/test_llm_shadow_pipeline.py docs/quant_framework_notes.md SUMMARY.md docs/summaries/SUMMARY.original.md
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/llm_shadow.py --help
```

Resource checks:
- `nvidia-smi` and `free -h` were checked before and after `compileall`, before and after unit discovery, and before and after CLI help.
- These verification commands did not start GPU work. Existing external GPU jobs remained present; final system memory was safe, about `432Gi` available.

Results:
- `compileall` passed.
- Full unit discovery passed: 65 tests OK.
- CLI help passed and now shows `--model MODEL` as a provider model override without a DeepSeek model default in the generic CLI surface.
- No real DeepSeek or other LLM API call was made; no API balance was consumed.

Conclusion:
- The rename audit is closed: the public shadow entrypoint is provider-agnostic `llm_shadow`, DeepSeek naming is confined to provider-specific integration points and documentation of the current provider, and the path remains shadow-only.

## 2026-05-25 - Trusted experiment loop audit

Task:
- Audit the newly added trusted experiment loop for development WFO, freeze/held-out guards, and unified experiment ledger schema, without adding complex Agent evolution and without making real API calls.

Repository/path checks:
- `pwd -P` and `realpath .` confirmed the physical repository path as `/Data/lzp/MacroQuant` before edits.
- Initial `git status --short --branch` showed pre-existing modified/untracked files; this audit only changed files in the requested trusted-experiment scope and did not revert unrelated work.

Findings:
- Development folds already stopped before `heldout_start`, and `FormulaicWfoRunner` skipped rebalances whose `tradable_date` would fall outside the test window.
- `freeze_hash` only covered component IDs, so changing protocol windows, template parameter space, track lengths, or policy contents under the same IDs would not be detected.
- ExperimentLedger uniformly wrapped events through TrialLedger, so `record_hash` tamper detection remained intact, but the repeated ledger context did not include model/prompt/data-contract or component hashes on every event.
- `result_available_time` correctly avoided rejecting old feature frames with no such column, but non-null values needed stricter parsing so YYYYMMDD values are interpreted as dates and invalid values cannot be silently coerced away.
- CLI and docs described the runner as development WFO; the CLI help now states held-out/control-treatment runners are intentionally not implemented here.

Changes:
- Updated `src/hl_trader/protocols/guards.py` so `FreezeSpec` stores stable hashes for track/template/protocol/policy contents and includes those hashes in `freeze_hash` along with experiment, horizon, model, prompt, and data-contract identifiers.
- Hardened `assert_result_available()` to accept absent/empty availability columns for old features, parse YYYYMMDD values as local dates, and reject future or unparseable non-null result availability values.
- Updated `src/hl_trader/storage/experiment_ledger.py` so every ExperimentLedger event receives component hashes, model_id, prompt_id, and data_contract_id while preserving TrialLedger `record_hash` verification.
- Updated `scripts/hl/run_experiment.py` and `docs/quant_framework_notes.md` to make the development-only boundary and missing held-out runner explicit.
- Added unit coverage for freeze component drift, ledger context injection, TrialLedger tamper detection, result availability parsing, and side-effect-free CLI import.

Key commands:
```bash
pwd -P
realpath .
git status --short --branch
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_protocol_guards tests.unit.test_experiment_runner tests.unit.test_formulaic_wfo_runner -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/run_experiment.py --help
```

Resource checks:
- `nvidia-smi` and `free -h` were checked before and after validation runs.
- These commands did not start GPU work. Existing external GPU jobs remained present; final system memory was safe, about `432Gi` available.

Results:
- `compileall` passed.
- Targeted guard/runner/WFO tests passed: 14 tests OK.
- Full unit discovery passed: 74 tests OK.
- `run_experiment.py --help` passed and states this is a development WFO entrypoint, not a held-out/control-treatment runner.
- No real DeepSeek or other external API call was made; no API balance was consumed.

Conclusion:
- The trusted experiment loop audit is closed for the current scope: development WFO stays before held-out, freeze hashes now cover the requested experiment components by content hash/identifier, ledger context is consistently injected while retaining tamper detection, and held-out evaluation remains explicitly unimplemented.

## 2026-05-25 - Trusted experiment real-data smoke

Task:
- Continue filling the design toward a fuller system by making the trusted development WFO loop runnable on local real TuShare data, then attempt LLM/API verification where possible.

Repository/path checks:
- Physical repository path remained `/Data/lzp/MacroQuant`.
- Existing unrelated modified/untracked files were left untouched.
- New runtime artifacts were written only under ignored local paths: `data/features/`, `experiments/trial_ledger/`, and `data/evidence_packs/`.

Changes:
- Added `scripts/hl/build_daily_features.py`, a small CLI around `DailyPITFeatureBuilder`.
- The CLI builds next-day tradable daily PIT features from local TuShare raw data and writes partitioned Parquet under `data/features/<dataset>/`.
- Updated `docs/quant_framework_notes.md` with the feature-build command before the development WFO runner command.

Key commands:
```bash
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/build_daily_features.py --help
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/build_daily_features.py --raw-dir data/raw --output-root data/features --start-date 20200102 --end-date 20230703
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/run_experiment.py --config configs/experiments/pilot_2020_daily.yaml --features data/features/daily_alpha --max-folds 1
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/llm_shadow.py --feature-file data/features/daily_alpha/feature_date=20230630.parquet --decision-date 20230630 --tradable-date 20230703 --ts-code 000001.SZ --evidence-out data/evidence_packs/llm_shadow.jsonl --shadow-ledger experiments/trial_ledger/llm_shadow.jsonl --dry-run
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/llm_shadow.py --feature-file data/features/daily_alpha/feature_date=20230630.parquet --decision-date 20230630 --tradable-date 20230703 --ts-code 000001.SZ --evidence-out data/evidence_packs/llm_shadow_live_probe.jsonl --shadow-ledger experiments/trial_ledger/llm_shadow_live_probe.jsonl --max-tokens 300
```

Resource checks:
- Before feature build: GPUs were occupied by pre-existing jobs; no new GPU work was started by the feature or experiment scripts. System memory showed about `432Gi` available.
- After feature build and experiment smoke: memory remained safe, about `431Gi` available.

Results:
- Feature build succeeded:
  - Output directory: `data/features/daily_alpha`
  - Date range: `20200102` to `20230703`
  - Partitions: `847`
  - Rows: `3,838,379`
- Development WFO smoke succeeded on `configs/experiments/pilot_2020_daily.yaml` with `--max-folds 1`:
  - Ledger: `experiments/trial_ledger/pilot_2020_daily.jsonl`
  - Fold count: `1`
  - Held-out boundary: `2025-01-01`; the completed fold stayed in development.
  - Fills: `143`
  - Train score: about `0.006554`
  - Test return: about `-0.026246`
  - This is only a pipeline smoke result, not a strategy conclusion.
- Real-feature LLM shadow dry-run succeeded:
  - Input: `data/features/daily_alpha/feature_date=20230630.parquet`
  - Evidence packs: `1`
  - Checkpoints: `0`
  - Decisions: `0` because dry-run does not call the API.
- DeepSeek live smoke was attempted and failed fast with `DeepSeek api_key cannot be empty`.
  - Local checks showed `DEEPSEEK_API_KEY` is missing from both shell environment and local `.env`.
  - No DeepSeek API request was sent successfully and no API balance was consumed.

Conclusion:
- The local real-data path now runs through raw TuShare data -> PIT daily features -> frozen development WFO -> broker fills -> contextual TrialLedger records.
- The LLM evidence/hash/ledger path also runs on real feature data in dry-run mode.
- A real DeepSeek API smoke still requires placing `DEEPSEEK_API_KEY` in the local environment or ignored `.env`; the code path is ready but could not authenticate in this environment.

## 2026-05-25 - Trusted experiment hardening follow-up and DeepSeek smoke

Task:
- Continue optimizing the trusted experiment loop toward the design document where reasonable, and use the supplied DeepSeek API key for a minimal live API test.

Credential handling:
- `DEEPSEEK_API_KEY` was written to the ignored local `.env`.
- `.env` remained ignored by Git.
- The key was not printed in command output, tracked files, or ledger artifacts.

Changes:
- Tightened `src/hl_trader/wfo/formulaic_runner.py`:
  - `FormulaicWfoRunner` now fails fast unless the frozen `TradeStrategyPolicy.allowed_actions` contains `rebalance`, because this runner is explicitly a rebalance strategy.
  - Training slices now require non-null `result_available_time`, preventing accidental bypass of the `result_available_time <= train_end` rule.
  - Test-window `event_checkpoint` records are written to the ledger as `action=log_only`, `action_impact=shadow_only`, and `can_affect_trading=false`.
- Tightened `src/hl_trader/protocols/guards.py`:
  - `assert_result_available(..., require_column=True)` now fails on missing column or null values.
- Updated `src/hl_trader/features/daily_pit.py`:
  - Daily PIT features now emit `result_available_time`, equal to `available_at` for the current daily feature contract.
- Expanded tests:
  - Policy must allow `rebalance` for the formulaic rebalance runner.
  - Training feature frames must provide `result_available_time`.
  - Event checkpoints are logged without trading impact.
  - `require_column=True` catches missing/null result-availability fields.

Key commands:
```bash
pwd -P
git status --short --branch
git status --ignored --short .env
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python -c '<minimal DeepSeek JSON-mode smoke using load_deepseek_api_key()>'
git diff --check -- src tests scripts configs docs/quant_framework_notes.md SUMMARY.md docs/summaries/SUMMARY.original.md
```

Resource checks:
- `nvidia-smi` and `free -h` were checked before and after validation and before and after the DeepSeek smoke.
- No new GPU workload was started. Existing external GPU jobs remained present.
- System memory stayed safe, about `431Gi` available at finish.

Verification results:
- `compileall` passed.
- Full unit discovery passed: 77 tests OK.
- `git diff --check` passed.
- Secret-pattern scan outside ignored runtime directories and `.env` returned 0 file matches.
- Test-generated `__pycache__` directories were removed after validation.

DeepSeek live smoke:
- Model: `deepseek-v4-flash`
- Response JSON: `{"status":"ok","check":"deepseek_smoke"}`
- Usage: 62 total tokens.
- This was only a small JSON-mode connectivity and parser smoke; it did not run trading decisions and did not write API output to a ledger.

Conclusion:
- The previously identified high-priority gap around frozen policy enforcement is closed for the current formulaic rebalance runner.
- Result-availability enforcement is now fail-fast for training in this runner and supported by the daily PIT feature builder.
- Event checkpoints are now part of the experiment ledger, but still intentionally log-only; event-driven trading actions remain future work.

## 2026-05-25 - Held-out control runner and DeepSeek v4-pro smoke

Task:
- Continue moving the implementation toward the design document, allowing future real experiments to use `deepseek-v4-pro`, and identify remaining blockers.

Changes:
- Added `DailyFormulaicHeldoutRunner` in `src/hl_trader/pipelines/experiment.py`.
  - It requires `protocol.heldout_start`.
  - It accepts explicit frozen `FormulaicParameters`.
  - It evaluates only the held-out window and does not fit parameters.
  - It records `heldout_start` and `heldout_result` events with phase `heldout`.
- Added `HeldoutRunResult`.
- Extended `ExperimentLedger` with `default_phase`, restricted to `development` or `heldout`.
- Exported held-out runner/result from `src/hl_trader/pipelines/__init__.py`.
- Added `scripts/hl/run_heldout.py`, a CLI for frozen formulaic held-out Control evaluation.
- Added tests:
  - held-out runner uses frozen parameters,
  - all held-out ledger records stay in phase `heldout`,
  - no development `experiment_start` event is emitted by the held-out runner,
  - `run_heldout.py` imports without side effects.
- Updated `docs/quant_framework_notes.md` with the held-out command and the `deepseek-v4-pro` convention.

Key commands:
```bash
pwd -P
git status --short --branch
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_experiment_runner -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/run_heldout.py --help
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python -c '<minimal DeepSeek JSON-mode smoke using model=deepseek-v4-pro>'
```

Resource checks:
- `nvidia-smi` and `free -h` were checked before validation and before the DeepSeek v4-pro smoke.
- These commands did not start GPU work; existing external GPU jobs remained present.
- System memory stayed safe, about `431Gi` available.

Verification results:
- `compileall` passed.
- Targeted experiment-runner tests passed: 6 tests OK.
- `run_heldout.py --help` passed.
- Full unit discovery passed: 79 tests OK.

DeepSeek v4-pro live smoke:
- Model: `deepseek-v4-pro`
- Response JSON: `{"status":"ok","check":"deepseek_v4_pro_smoke","model":"v4-pro"}`
- Usage: 81 total tokens.
- This was a connectivity and JSON-mode parser smoke only; it did not run trading decisions and did not write API output to a ledger.

Current blockers:
- P2 financial PIT feature construction is still not implemented. Raw P2 is available, but statement version selection by `f_ann_date`/`ann_date`, event-date handling, deduplication, and `result_available_time` derivation must be added before financial features can be trusted in WFO.
- Held-out Control now exists for frozen formulaic parameters, but Treatment A/B are still not implemented. Learned execution policy and natural-language final review are not yet allowed to affect trading.
- Event checkpoints are recorded and auditable, but event-driven actions remain log-only.
- The initial experiment config still starts at 2020 for the current pilot. A full 2010-2024 development WFO will need broader feature-building coverage and likely a relaxed config guard once earlier data contracts are validated.
- Natural-language/news/announcement evidence and minute-line execution remain outside the current default workflow.

Conclusion:
- The repo now has a runnable path for frozen-parameter held-out Control evaluation.
- `deepseek-v4-pro` is usable with the current key for later LLM shadow or frozen-context experiments.
- The next highest-impact implementation is P2 financial PIT features, followed by held-out Treatment A/B scaffolding.
## 2026-05-25 - HL cleanup, event execution, and optional TuShare text tier

Task:
- Reduce redundant HL scripts, expand formulaic execution beyond pure rebalance, decide whether events should affect trading, and implement missing support for newly available TuShare text/NL data sources without launching an uncontrolled full historical download.

Changes:
- Replaced four thin HL CLI wrappers with one entrypoint: `scripts/hl/hl.py`.
  - Subcommands: `build-features`, `run-development`, `run-heldout`, `llm-shadow`.
  - Deleted the old wrapper files under `scripts/hl/`.
  - Updated tests and `docs/quant_framework_notes.md` to use the new commands.
- Extended `TradeStrategyPolicy` with event execution parameters: `event_de_risk_pct` and `event_exit_loss_pct`.
- Updated `FormulaicWfoRunner` so actual fills are no longer labeled only as `rebalance`.
  - Routine target changes now emit `enter`, `add`, `trim`, or `exit` reasons.
  - Those sub-actions are enforced as real policy permissions; a disabled action is skipped rather than disguised as `rebalance`.
  - Execution constraint columns `up_limit`, `down_limit`, and `is_suspended` are required fail-fast before trading.
  - Event checkpoints remain deterministic/frozen policy logic, not LLM output, but negative price moves or down-limit events can now trigger `event_de_risk` or `exit` orders for existing holdings.
  - Event checkpoints record whether the specific checkpoint can affect trading.
- Added optional TuShare text/P5 support to `scripts/data/tushare_data.py`.
  - New download tier: `download --tier text` or `download --tier p5`.
  - Default text datasets: `anns_d`, `major_news`, `cctv_news`, `npr`, `research_report`, `report_rc`.
  - Optional short news dataset: `news`, requiring explicit `--news-src`.
  - Added source-specific partition support for `major_news` and `news`.
  - Added row-level `available_at` derivation with source-time priority and conservative date fallback.
  - Added `audit --include-text` with expected partition checks, sidecar checks, key duplication checks, blank/unparseable available-time checks, and PIT notes.
- Updated `docs/tushare_data_download_plan.md` to reflect the new optional P5/text tier and PIT cautions.

SubAgent audits:
- First GPT-5.5 xhigh SubAgent performed a read-only TuShare text/NL data audit and confirmed existing scripts had no text download/audit support.
- Second GPT-5.5 xhigh SubAgent audited the implemented script consolidation, event execution, and text-tier changes. It found four issues; all were addressed:
  - missing execution constraint columns now fail fast,
  - text audit now checks expected partitions from `--text-start-date/--text-end-date`,
  - `available_at` fallback is row-level and audit catches unparseable times,
  - action permissions now block disabled `enter/add/trim/exit` orders.

Key commands:
```bash
pwd -P
git status --short --branch
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_formulaic_wfo_runner tests.unit.test_experiment_runner tests.unit.test_llm_shadow_pipeline -v
PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_formulaic_wfo_runner -v
PYTHONPATH=src ~/miniconda3/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/hl.py --help
PYTHONPATH=src ~/miniconda3/bin/python scripts/hl/hl.py run-development --help
~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --help
~/miniconda3/bin/python scripts/data/tushare_data.py audit --help
git diff --check
```

Resource checks:
- GPU/RAM were checked before and after validation runs.
- Existing external GPU jobs remained present; these tests did not start GPU workloads.
- Available system memory stayed safe, about `422-424Gi` available.

Verification results:
- `compileall` passed.
- Targeted WFO tests passed: 11 tests OK.
- Full unit discovery passed: 81 tests OK.
- HL and TuShare CLI help checks passed.
- `git diff --check` passed.
- Secret-pattern scan outside ignored runtime directories and `.env` did not expose real local API keys; expected test fixture strings remain in unit tests.

Not run:
- Full historical text/NL data download was not started. These interfaces can be large and should first be smoke-tested over a short date window after choosing sources for `news` and any optional `major_news` source filters.

Conclusion:
- The codebase is less redundant at the script-entrypoint layer.
- Events can now change trading, but only through deterministic frozen execution-policy rules; LLM/NL shadow still cannot trade.
- Optional TuShare text/NL ingestion and audit scaffolding is implemented, with PIT-aware availability metadata and expected-partition checks.
## 2026-05-26 - TuShare text download from 2020 and script/rate follow-up

Task:
- Further simplify scripts where possible, check implementation progress against the design document, run a text-data window test, then download TuShare text/NL history starting from 2020 while respecting API rate limits.

Script cleanup/status:
- Confirmed the repository now has only two real script entrypoints under `scripts/`:
  - `scripts/data/tushare_data.py` for TuShare download/audit,
  - `scripts/hl/hl.py` for HL feature/experiment/held-out/LLM-shadow commands.
- Removed generated `__pycache__` directories from `scripts/`, `src/`, and `tests/` after verification.
- Adjusted text-tier download defaults in `scripts/data/tushare_data.py`:
  - default min interval for `text/p5` is now `0.65` seconds per call,
  - default text page limit is now `800`, while non-text defaults remain `10000`.
- Fixed text audit datetime parsing to use mixed-format parsing so source-time and conservative fallback `available_at` values are both recognized.

Short-window test:
```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --raw-dir data/raw --start-date 20260520 --end-date 20260525 --min-interval-seconds 0.35 --timeout-seconds 120 --page-limit 10000 | tee logs/tushare_text_window_20260520_20260525.log
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py audit --raw-dir data/raw --include-limit-list --include-text --text-start-date 20260520 --text-end-date 20260525 --end-date 20260519 --p2-end-date 20260520 --output results/data_quality/text_window_status_20260520_20260525.json | tee logs/tushare_text_window_audit_20260520_20260525.log
```

Short-window result:
- Download succeeded for default text datasets.
- Rows: `anns_d` 6000, `major_news` 800, `cctv_news` 79, `npr` 4, `research_report` 291, `report_rc` 1398.
- The row counts revealed endpoint caps, so the full run used smaller page limits to force proper pagination.

Full 2020+ text download commands:
```bash
# Initial all-in-one run was stopped after verifying pagination worked but `anns_d` was the bottleneck at 800 rows/page.
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --raw-dir data/raw --datasets anns_d --start-date 20200101 --end-date 20260525 --force --page-limit 6000 --min-interval-seconds 0.65 --timeout-seconds 180 > logs/tushare_text_anns_d_20200101_20260525.log 2>&1
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --raw-dir data/raw --datasets major_news cctv_news npr research_report report_rc --start-date 20200101 --end-date 20260525 --force --page-limit 800 --min-interval-seconds 0.65 --timeout-seconds 180 > logs/tushare_text_rest_20200101_20260525.log 2>&1
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py audit --raw-dir data/raw --include-limit-list --include-text --text-start-date 20200101 --text-end-date 20260525 --end-date 20260519 --p2-end-date 20260520 --output results/data_quality/status_text_20200101_20260525.json | tee logs/tushare_text_audit_20200101_20260525_rerun.log
```

Downloaded text data:
- `anns_d`: 77 parquet/meta files, 9,339,855 rows.
- `major_news`: 77 parquet/meta files, 2,726,571 rows.
- `cctv_news`: 2,337 parquet/meta files, 35,142 rows.
- `npr`: 77 parquet/meta files, 8,552 rows.
- `research_report`: 77 parquet/meta files, 234,836 rows.
- `report_rc`: 77 parquet/meta files, 1,477,359 rows.
- `news` short-message data was not downloaded because TuShare requires explicit `src`; choose sources before enabling it.
- `data/raw` is now about 12G.

Audit result:
- Report: `results/data_quality/status_text_20200101_20260525.json`.
- Status: warning.
- Counts: 0 errors, 20 warnings, 49 info.
- Text partition completeness passed for the default downloaded text datasets.
- Text `available_at` parse checks passed after mixed-format parser fix.
- Text warnings are duplicate business-key rows in `anns_d`, `major_news`, `research_report`, and `report_rc`; these should be handled by downstream evidence/document deduplication, not by deleting raw rows.

Implementation progress vs design document:
- Implemented: core schemas, experiment config, PIT daily features, leakage checks, formulaic WFO, daily execution simulator, frozen policy context, held-out Control runner, deterministic event execution overlay, LLM shadow integration, and now local TuShare text raw ingestion/audit.
- Partially implemented: execution strategy learning is still deterministic policy parameters rather than a learned execution-policy search family; natural-language logic is API-backed shadow only, not trading; text data is raw/audited but not yet integrated into evidence packs or entity/event mapping.
- Not yet implemented: P2 financial PIT feature derivation and version selection, Treatment A/B held-out comparisons, natural-language final review with frozen action space, minute/intraday execution, multi-horizon track orchestration, and full template-learning agents.

Resource checks:
- GPU/RAM checked before/after downloads and audits.
- No new GPU workload was launched; existing external GPU jobs remained visible.
- System memory stayed safe, roughly 424-453Gi available during/after the run.

Verification:
```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q src tests scripts
PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_formulaic_wfo_runner tests.unit.test_experiment_runner tests.unit.test_llm_shadow_pipeline -v
~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --help
~/miniconda3/bin/python scripts/data/tushare_data.py audit --help
git diff --check
```
- Compile passed.
- Targeted 26 tests passed.
- CLI help checks passed.
- `git diff --check` passed.

Conclusion:
- The 2020+ default TuShare text history is now locally downloaded and structurally auditable with no missing expected partitions.
- Remaining warnings are semantic/raw duplication warnings that should be resolved in a later evidence normalization layer.

## 2026-05-26 - TuShare official-rate news source download

Task:
- Re-check TuShare official documentation for frequency limits and source lists, then download all documented `news` sources from 2020 while keeping rate and per-call row limits compliant.

Official-doc constraints applied:
- 10000-point account: regular data 500 calls/minute; special data 300 calls/minute.
- Independent text permissions: news information 400 calls/minute; announcements and policy datasets 500 calls/minute.
- Text per-call page clamps in `scripts/data/tushare_data.py`: `anns_d=2000`, `major_news=400`, `npr=500`, `research_report=1000`, `report_rc=3000`, `news=1500`.
- `news` default sources now expand to all official identifiers: `sina`, `wallstreetcn`, `10jqka`, `eastmoney`, `yuncaijing`, `fenghuang`, `jinrongjie`, `cls`, `yicai`.

Script changes:
- Included `news` in `TEXT_DEFAULT_DATASETS`.
- Added official source constants and source validation/default expansion.
- Changed mixed `text/p5` default interval from `0.65s` to `0.22s`; this remains within the more restrictive 300 calls/minute special-data ceiling.
- Kept `news` download command at `--min-interval-seconds 0.16`, within the official 400 calls/minute news tier.
- Changed `news` partitioning from source+month to source+day because high-volume monthly windows hit TuShare `50101` at large offsets even with official `limit=1500`. The failed partial monthly `data/raw/news` output was deleted before the daily rerun.
- Updated `docs/tushare_data_download_plan.md` and `docs/quant_framework_notes.md`.

Commands:
```bash
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --raw-dir data/raw --datasets news --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.16 2>&1 | tee logs/tushare_news_20200101_20260525_daily.log
PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py audit --raw-dir data/raw --include-limit-list --include-text --text-start-date 20200101 --text-end-date 20260525 --end-date 20260519 --p2-end-date 20260520 --output results/data_quality/status_text_20200101_20260525_all_sources.json 2>&1 | tee logs/tushare_text_audit_20200101_20260525_all_sources.log
```

Download result:
- `news` all-source total: 21,033 parquet files and 21,033 sidecars, 10,258,167 rows.
- Source rows: `sina` 3,335,991; `eastmoney` 2,344,537; `jinrongjie` 1,341,727; `wallstreetcn` 995,344; `10jqka` 995,082; `yuncaijing` 673,696; `fenghuang` 358,763; `yicai` 110,533; `cls` 102,494.
- Some sources have many zero-row early days because TuShare returns no data for those source/date combinations; the files are retained as explicit completeness markers.
- `data/raw` is about 14G after the full text/news download.

Audit result:
- Report: `results/data_quality/status_text_20200101_20260525_all_sources.json`.
- Status: warning; finding counts: 0 errors, 21 warnings, 50 info.
- Text partition checks have 0 missing expected files and 0 extra files.
- Text `available_at` checks have 0 unparseable rows.
- `news_text_partitions`: 21,033/21,033 expected files, 10,258,167 rows, 8,821 zero-row day/source partitions.
- `news_text_keys`: 3,586,012 duplicate business-key rows; raw layer keeps these for later evidence/document deduplication.

Verification/resource checks:
- `compileall` passed after script changes.
- Download/audit CLI help checks passed.
- GPU/RAM were checked before and after script runs; memory remained safe, with roughly 423-425Gi available around the news download/audit.

Conclusion:
- P5 text raw coverage from 2020 now includes all currently scripted text interfaces plus all official `news` sources.
- Remaining warnings are raw duplication and known source/semantic warnings, not structural download failures.

Follow-up official-limit redownload:
- Re-ran `anns_d`, `major_news`, and `npr` with `--force`, `--min-interval-seconds 0.22`, and script-level official page clamps (`2000`, `400`, `500`).
- Command: `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier text --raw-dir data/raw --datasets anns_d major_news npr --start-date 20200101 --end-date 20260525 --force --min-interval-seconds 0.22 2>&1 | tee logs/tushare_text_official_limits_redownload_20200101_20260525.log`.
- Final counts after redownload: `anns_d` 77 files / 9,340,185 rows; `major_news` 77 / 2,726,603; `npr` 77 / 8,552.
- Reran all-text audit to `results/data_quality/status_text_20200101_20260525_all_sources.json`; final status remains warning with 0 errors, 21 warnings, and 50 infos. No expected text partitions are missing, no extra text partitions are present, and `available_at` parsing has 0 unparseable rows.

## 2026-05-26 - TuShare required-interface permission probe before P3/P4 downloads

Task:
- User decided not to copy ChouQuant minute data and asked whether the interfaces required by the data document are now obtainable directly from TuShare.

Commands/logs:
- Resource checks: `nvidia-smi`, `free -h` before and after probes.
- Probe log: `logs/tushare_required_interfaces_probe_20260526.log`.
- The probe script loaded `TUSHARE_TOKEN` from ignored local environment/.env and did not print the token.

Probe result:
- P3: `stk_mins` returned 6 rows for `000001.SZ` 20260525 09:30-09:35 with expected OHLCV/amount fields; `stk_auction` returned code 0 and fields including `price`, `vol`, `amount`, `turnover_rate`, `volume_ratio`, `float_share`; `stk_auction_c` returned code 0 and fields including OHLCV, amount, vwap.
- P4: `margin`, `margin_detail`, `moneyflow`, `stk_holdernumber`, `stk_holdertrade`, `repurchase`, `share_float`, `block_trade` all returned code 0 with expected schemas. `margin` and `margin_detail` returned rows when probed on 20260525/20260522.
- `report_rc` remains accessible and is already included in the downloaded text/P5 raw set.

Conclusion:
- The currently documented historical/research interfaces needed for MacroQuant P0-P5 appear obtainable with the current TuShare permission set.
- Not yet proven by full download: P3 historical minute/auction full-market completeness and P4 full-history completeness; these still need window tests, rate limits, pagination checks, unit contracts, and unified audit before being treated as production-ready local raw data.
- Realtime-only interfaces such as `rt_min`/`rt_min_daily` were not tested in this historical probe and should be validated separately during live-market workflow work.

## 2026-05-26 - P3 TuShare historical minute window test

Task:
- User clarified that historical opening and closing auction do not need separate downloads because minute data carries those bars, and approved downloading full-A data.

Implementation:
- Added P3 support to `scripts/data/tushare_data.py` through `download --tier p3` for TuShare `stk_mins` 1-minute data.
- Full-A universe is derived from local `data/raw/stock_basic/list_status=*.parquet`; partitions are written as `data/raw/stk_mins_1min/ts_code=<TS_CODE>/year=<YYYY>.parquet` with sidecar metadata.
- `stk_mins` paging uses the official 8000-row cap; each stock-year query is resumable and skipped when the parquet already exists unless `--force` is used.
- Added `audit-p3` to avoid running the expensive full P0/P1/P2 unified audit for small minute-window checks.
- P3 unit contract recorded in metadata and status: `vol` is shares, `amount` is CNY, and `available_at` is the source minute `trade_time` treated as bar-close visibility.
- Documentation updated to make `stk_auction`/`stk_auction_c` validation-only for historical work; no historical full auction download is planned.

Commands and artifacts:
- Resource checks before/after download and audit used `nvidia-smi` and `free -h`; memory remained safe at about 411-415Gi available.
- Download command: `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier p3 --raw-dir data/raw --datasets stk_mins --codes 000001.SZ 300750.SZ --start-date 20200101 --end-date 20200131 --min-interval-seconds 0.22 --timeout-seconds 120`.
- Download log: `logs/tushare_p3_stk_mins_window_20200101_20200131.log`.
- Audit command: `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py audit-p3 --raw-dir data/raw --p3-codes 000001.SZ 300750.SZ --p3-start-date 20200101 --p3-end-date 20200131 --output results/data_quality/status_p3_window_20200101_20200131.json`.
- Audit log: `logs/tushare_p3_window_audit_20200101_20200131.log`.
- Window data: `data/raw/stk_mins_1min/ts_code=000001.SZ/year=2020.parquet` and `data/raw/stk_mins_1min/ts_code=300750.SZ/year=2020.parquet`.

Result:
- Window download wrote 2 stock-year partitions, 7712 rows, and 2 API pages.
- `audit-p3` status is `ok` with 0 errors and 0 warnings.
- Checks passed: expected files 2/2, sidecars 2/2, no missing required columns, no duplicate `(ts_code, trade_time)`, no partition mismatches, and sampled files contain both 09:30 and 15:00 bars.
- Full-A 20200101-20260525 estimate from local `stock_basic` and SSE calendar: 5734 active codes, 35855 stock-year partitions, about 18.22B minute rows and 250544 API calls. At 0.22s between calls the rate-limit lower bound is about 15.3 hours before network, retries, and disk IO.
- Full-A download was not started in the foreground; the script is ready for a deliberate long resumable run.

## 2026-05-26 - Data-download document audit and P3 full-A minute download start

Task:
- User requested a SubAgent audit of the data-download documentation to remove redundant function/structure while preserving completeness, then requested full-A data download startup.

SubAgent audit:
- Spawned high-capability SubAgent `Copernicus` for editable documentation audit.
- Scope was limited to `docs/tushare_data_download_plan.md` and directly related data-boundary wording in `docs/quant_framework_notes.md`; it did not touch scripts, data, results, logs, or secrets.
- The SubAgent reported that it reduced `docs/tushare_data_download_plan.md` to 185 lines, preserved P0-P5 scope, unified entrypoints, TuShare rate/page limits, PIT/unit rules, audit entrypoints, P3 full-A scale, and the decision that historical P3 uses `stk_mins` 09:30/15:00 bars instead of separate historical `stk_auction` / `stk_auction_c` downloads.
- Closed SubAgent `019e637a-7e5b-7c63-be81-4f118931cdce` after completion.

Pre-run checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant`.
- `df -h /Data/lzp/MacroQuant` showed about 1.4T available on `/Data`.
- `pgrep -af tushare_data.py` found no existing TuShare data job before launch.
- `nvidia-smi` and `free -h` were checked before launch; about 413Gi memory was available and the P3 job uses CPU/network/disk, not GPU.

Run command:
- `nohup bash -lc 'PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py download --tier p3 --raw-dir data/raw --datasets stk_mins --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 120 --force' > logs/tushare_p3_stk_mins_fullA_20200101_20260525_20260526.log 2>&1 &`
- PID file: `logs/tushare_p3_stk_mins_fullA_20200101_20260525_20260526.pid`.
- PID: `2896717`.
- `--force` was used for this first full-A launch because the previous window test had written two partial `year=2020` partitions; the force run overwrites those into complete stock-year partitions. If this long job stops later, resume without `--force` to preserve completed stock-year partitions.

Startup result:
- After about 3 minutes, process `2896717` was still running.
- Log progress: `stk_mins_1min 50/35855 skipped=0 written=50 rows_written=2662809 pages=365`.
- Local inventory shortly after launch: 65 parquet files, 65 sidecar metadata files, 0 tmp files under `data/raw/stk_mins_1min`.
- Post-start resource checks remained safe with about 412Gi available memory.

## 2026-05-26 - Data quality status semantic naming cleanup

Task:
- User asked whether `results/data_quality` should be split by data scope without date suffixes, and whether P1-P5 naming should be replaced because all required data is now accessible and the old priority labels are inconvenient.

Changes:
- Updated `scripts/data/tushare_data.py` status defaults:
  - Base research audit defaults to `results/data_quality/base_research_status.json`.
  - Text evidence audit defaults to `results/data_quality/text_evidence_status.json` when `--include-text` is used.
  - Intraday minute audit defaults to `results/data_quality/intraday_minutes_status.json`.
  - Combined mixed-scope audit defaults to `results/data_quality/combined_status.json` if both text and intraday are explicitly included in the unified audit.
- Added semantic download tier aliases while preserving old compatibility aliases:
  - `reference` -> old `p0`
  - `daily` / `market_daily` -> old `p1`
  - `fundamental` / `fundamentals` -> old `p2`
  - `intraday` / `minute` / `minutes` -> old `p3`
  - `text_evidence` / `evidence_text` -> old `text`
- Updated data download documentation and framework notes to use semantic data-domain names instead of P1-P5 for user-facing guidance.
- Moved obsolete top-level status files into `results/data_quality/archive/20260526_status_cleanup/`:
  - `status_text_20200101_20260525.json`
  - `text_window_status_20260520_20260525.json`
  - `status_p3_window_20200101_20200131.json`
- Renamed current top-level statuses:
  - `status.json` -> `base_research_status.json`
  - `status_text_20200101_20260525_all_sources.json` -> `text_evidence_status.json`

Current top-level status files:
- `results/data_quality/base_research_status.json`
- `results/data_quality/text_evidence_status.json`
- `results/data_quality/intraday_minutes_status.json` will be generated after the full intraday minute download completes.
- `results/data_quality/event_flow_status.json` is reserved for future event/flow data once implemented/downloaded.

Verification:
- Resource checks before/after lightweight script checks stayed safe, with about 437Gi available memory after verification.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q scripts src tests` passed.
- CLI help checks passed for `download`, `audit`, and `audit-p3`.
- `git diff --check` passed.
- The running full-A intraday minute download was not stopped; at the follow-up check it was still running under PID `2896717` and had reached `2550/35855` tasks, `135173044` rows, and `18508` API pages.

## 2026-05-26 - Root archive migration and base audit rerun

Task:
- User requested that `archive` live at the repository root and hold project historical files, then asked to rename or rerun the current audit outputs.

Changes:
- Added `/archive/` to `.gitignore` so historical runtime artifacts do not appear as commit candidates.
- Moved historical data-quality status files from `results/data_quality/archive/20260526_status_cleanup/` to `archive/data_quality/20260526_status_cleanup/`.
- Kept `results/data_quality/` for current status files only.
- Updated `docs/tushare_data_download_plan.md` to document root `archive/data_quality/` as the historical status location.

Current data-quality layout:
- Current files:
  - `results/data_quality/base_research_status.json`
  - `results/data_quality/text_evidence_status.json`
- Historical files:
  - `archive/data_quality/20260526_status_cleanup/status_p3_window_20200101_20200131.json`
  - `archive/data_quality/20260526_status_cleanup/status_text_20200101_20260525.json`
  - `archive/data_quality/20260526_status_cleanup/text_window_status_20260520_20260525.json`

Audit rerun:
- Pre-run checks used `nvidia-smi` and `free -h`; about 438Gi memory was available.
- Command: `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/data/tushare_data.py audit --raw-dir data/raw --include-limit-list --end-date 20260519 --p2-end-date 20260520 2>&1 | tee logs/data_quality_base_research_audit_20260526.log`.
- Result: `results/data_quality/base_research_status.json` was regenerated with status `warning`, 0 errors, 16 warnings, and 40 info findings.
- `results/data_quality/text_evidence_status.json` remains the current renamed all-sources text audit with status `warning`, 0 errors, 21 warnings, and 50 info findings. It was not rerun because the existing file already represents the all-sources text audit and a full text scan would add unnecessary IO while the full intraday minute download is active.

Concurrent P3 job:
- Full-A intraday minute download PID `2896717` was still running after the audit.
- Latest observed progress: `2800/35855` tasks, `148429249` rows, and `20323` API pages.
- Post-run resource checks remained safe, with about 438Gi available memory.
- `git diff --check` passed.

## 2026-05-26 - Project logbook file rename

Task:
- User approved renaming the project logging files because `SUMMARY.md` and `docs/summaries/SUMMARY.original.md` were acting as durable logs rather than generic summaries.

Changes:
- Renamed `SUMMARY.md` to `LOGBOOK.md`.
- Renamed `docs/summaries/SUMMARY.original.md` to `docs/logbook/DETAILED_LOGBOOK.md`.
- Removed the now-empty `docs/summaries/` directory.
- Updated current logging instructions in `AGENTS.md` and `CLAUDE.md`:
  - Routine context gathering should read `LOGBOOK.md` first.
  - Detailed traceability should go to `docs/logbook/DETAILED_LOGBOOK.md`.
- Updated `docs/tushare_data_download_plan.md` to point historical audit readers at the new logbook paths.

Notes:
- Historical references inside older detailed log entries were intentionally left intact when they described commands or working-tree state at the time. Those references are factual history, not current operating instructions.
- The running full-A intraday minute download was not stopped; PID `2896717` was still active before this rename task.

## 2026-05-26 - Scripts directory flattening

Task:
- User requested flattening `scripts/` because `scripts/data/` and `scripts/hl/` each contained only one active entrypoint.

Changes:
- Moved `scripts/data/tushare_data.py` to `scripts/tushare_data.py`.
- Moved `scripts/hl/hl.py` to `scripts/hl.py`.
- Removed the now-empty `scripts/data/` and `scripts/hl/` directories plus their local `__pycache__` directories.
- Updated current command references in:
  - `docs/tushare_data_download_plan.md`
  - `docs/quant_framework_notes.md`
  - `tests/unit/test_experiment_runner.py`
  - `tests/unit/test_llm_shadow_pipeline.py`
  - `scripts/tushare_data.py` help docstring

Current script surface:
- `scripts/tushare_data.py` for TuShare download/audit.
- `scripts/hl.py` for feature build, development WFO, held-out evaluation, and LLM shadow commands.

Verification:
- Confirmed physical repo path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Pre-check resources: GPU memory usage was stable and system memory had about 435Gi available.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q scripts src tests` passed.
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/python scripts/tushare_data.py download --help` passed.
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/python scripts/tushare_data.py audit --help` passed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python scripts/hl.py --help` passed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m unittest tests.unit.test_experiment_runner tests.unit.test_llm_shadow_pipeline -v` passed with 15 tests OK.
- `git diff --check` passed after the logbook update.

Concurrent P3 job:
- The full-A intraday minute download was not stopped.
- PID `2896717` was still running after the flattening; its process command still shows the old path because it was launched before the file move.
- Latest observed progress: `4050/35855` tasks, `209909795` rows, and `28764` API pages.
- Post-check resources remained safe, with about 436Gi available memory.

## 2026-05-26 - Raw ingestion reliability audit

Task:
- User asked to critically reference `/Data/lzp/ChouQuant` minute-line retry logic and first check whether current MacroQuant downloads could silently discard failed requests or leave missing data.

Reference implementation reviewed:
- `/Data/lzp/ChouQuant/data/update_data.py` keeps a daily `pending_codes` set, retries failed minute-code requests up to `MAX_RETRIES`, and only atomically writes the formal daily minute file if all pending codes succeed.
- It also validates existing minute files by required columns and minimum row count before skipping.

MacroQuant current behavior:
- `scripts/tushare_data.py` uses `TuShareClient.query(..., retries=5)` for HTTP/JSON failures and retryable TuShare rate/timeout messages.
- `query_paged` raises on inconsistent fields or pagination safety overflow.
- P3 `download_p3` writes a stock-year partition only after the full paged query returns, and an exception aborts the job instead of silently skipping the partition.
- The subtle remaining risk is a logical empty response: if TuShare returns `code=0` with `items=[]` for a code-year that should have trading data, current code writes a zero-row stock-year file. That should be hardened by checking same-year `daily` rows before accepting an empty `stk_mins` partition.

Audit run:
- Output: `results/data_quality/ingestion_reliability_status.json`.
- Scope:
  - TuShare runtime log scan for explicit failures.
  - Full `data/raw` parquet/footer/meta inventory.
  - `.tmp`, missing sidecar, orphan sidecar, row-count mismatch, and pagination consistency checks.
  - Running P3 expected stock-year partition check for `20200101-20260525`.
  - Existing semantic status files error-count check.

Result:
- Final status: `warning`.
- `data/raw` scan at the audit snapshot:
  - 78,154 parquet files.
  - 78,154 meta files.
  - 0 tmp files.
  - 0 missing meta files.
  - 0 orphan meta files after correcting for concurrent P3 writer race.
  - 0 row-count mismatches.
  - 0 pagination inconsistencies.
- Existing semantic status files:
  - `results/data_quality/base_research_status.json`: warning, 0 error findings.
  - `results/data_quality/text_evidence_status.json`: warning, 0 error findings.

P3-specific findings:
- Expected full-A stock-year tasks: 35,855.
- At audit time, 12,149 expected files existed and 12,148 matched the full target window.
- Latest P3 log progress during the scan: around `12100/35855`, `635295521` rows, `87017` API pages; the background job remained running under PID `2896717`.
- Three zero-row P3 stock-year partitions were found:
  - `000670.SZ` year 2021.
  - `002260.SZ` year 2020.
  - `002260.SZ` year 2021.
- Cross-check against local `daily` found 0 same-year daily rows for all three zero-row minute partitions, so these are not evidence of dropped valid trading data.
- One stale partial window-test partition remains:
  - `data/raw/stk_mins_1min/ts_code=300750.SZ/year=2020.parquet`
  - Actual window: `2020-01-01 09:00:00` to `2020-01-31 15:00:00`.
  - Expected full-run window: `2020-01-01 09:00:00` to `2020-12-31 15:00:00`.
  - This is expected while the sequential full-A job has not reached task index `15527`; it should be overwritten by the current `--force` run when reached.

Log scan:
- Current P3 full-A log had no failure/error matches.
- Historical TuShare logs contain older expected failures and warnings, including the earlier failed `news` monthly-source attempt and old audit warning/error summaries. These are preserved as warning context in the reliability status, not treated as current data loss.

Resource and verification:
- Resource checks before/after stayed safe, with about 418Gi available memory after the scan.
- `git diff --check` passed.

Follow-up hardening target:
- Add P3 logical-empty validation: if `stk_mins` returns zero rows for a code-year where local `daily` has rows, retry and then fail the partition instead of writing a zero-row file.
- Optionally record attempt counts and empty-response validation details in sidecar metadata for future reproducibility.

## 2026-05-26 Macro/Global TuShare Scaffolding

Task:
- Add code and docs for the now-unlocked macro/global/policy context datasets, but do not start new downloads yet.

Code changes:
- Extended `scripts/tushare_data.py` with `download --tier macro`, `download --tier global`, and `audit-macro`.
- Added dataset specs for `cn_schedule`, `cn_gdp`, `cn_cpi`, `cn_ppi`, `cn_pmi`, `cn_m`, `sf_month`, `shibor`, `shibor_quote`, `shibor_lpr`, `hibor`, `libor`, `repo_daily`, `us_tycr`, `us_trycr`, `us_tbr`, `us_tltr`, `index_global`, `fx_daily`, `eco_cal`, and `monetary_policy`.
- Added conservative raw-layer `available_at` rules: date-only rows use local end-of-day, month-only macro rows use month-end plus 31 days, quarter-only rows use quarter-end plus 45 days, and `eco_cal` uses `date+time` when parseable.
- Added macro/global audit inventory, sidecar, key, duplicate, and `available_at` parse checks with default output `results/data_quality/macro_context_status.json`.

Documentation:
- Updated `docs/tushare_data_download_plan.md` with macro/global commands, status naming, download order, PIT/unit notes, official docs, and a dedicated macro/global context table.
- Updated `docs/quant_framework_notes.md` to record that macro/global context is raw scaffolding for regime/evidence and is not yet part of default formulaic daily features.

Verification commands:
- `nvidia-smi`
- `free -h`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q scripts src tests`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/python scripts/tushare_data.py download --help`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/python scripts/tushare_data.py audit-macro --help`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/python -c "... macro helper smoke ..."`
- `git diff --check`

Result:
- All verification commands passed.
- No new TuShare download was started.
- P3 full-A minute background job remained running under PID `2896717`; latest checked progress was about `13050/35855` tasks and `685,692,477` rows.
- Resource checks stayed safe with about 418Gi available system memory after verification.

## 2026-05-27 P3 Full-A Minute Download Completion Check

Task:
- Check the current progress of the long-running TuShare `stk_mins_1min` full-A download for `20200101-20260525`.

Commands:
- `ps -p 2896717 -o pid,etime,stat,pcpu,pmem,rss,cmd`
- `tail -n 25 logs/tushare_p3_stk_mins_fullA_20200101_20260525_20260526.log`
- `find data/raw/stk_mins_1min -name '*.parquet' | wc -l`
- `find data/raw/stk_mins_1min -name '*.meta.json' | wc -l`
- `find data/raw/stk_mins_1min -name '*tmp*' | wc -l`
- `rg -n "Traceback|ERROR|Error|error|failed|Failed|Exception|returned code" logs/tushare_p3_stk_mins_fullA_20200101_20260525_20260526.log`
- `du -sh data/raw/stk_mins_1min`
- `free -h`

Result:
- PID `2896717` was no longer running.
- Runtime log ended with `P3 download finished under /Data/lzp/MacroQuant/data/raw`.
- Final progress line: `stk_mins_1min done tasks=35855 skipped=0 written=35855 rows_written=1820916656 pages=249867`.
- File inventory: 35,855 parquet files and 35,855 sidecar `.meta.json` files.
- Temporary files: 0.
- Directory size: about 44G.
- Error keyword scan returned no matches.
- System memory check stayed safe with about 418Gi available memory.

Conclusion:
- The full-A historical 1-minute raw download finished cleanly at the log/inventory level.
- Next required validation step is a full `audit-p3 --p3-start-date 20200101 --p3-end-date 20260525` before declaring the minute layer research-ready.

## 2026-05-27 P3 Audit And Macro/Global Context Download

Task:
- Run the full P3 minute audit.
- Continue the next data-download step without violating TuShare rate limits.

Commands:
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py audit-p3 --raw-dir data/raw --p3-start-date 20200101 --p3-end-date 20260525 --output results/data_quality/intraday_minutes_status.json`
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py download --tier macro --raw-dir data/raw --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 90`
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py download --tier global --raw-dir data/raw --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 90`
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py download --tier macro --raw-dir data/raw --datasets shibor_quote --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 90`
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py audit-macro --raw-dir data/raw --start-date 20200101 --end-date 20260525 --output results/data_quality/macro_context_status.json`

P3 audit result:
- Output: `results/data_quality/intraday_minutes_status.json`.
- Status: warning.
- Counts: 0 errors, 1 warning, 2 infos.
- Inventory:
  - Expected files: 35,855.
  - Parquet files: 35,855.
  - Meta files: 35,855.
  - Missing expected files: 0.
  - Extra files: 0.
  - Missing sidecars: 0.
  - Orphan sidecars: 0.
  - Schema missing required columns: 0.
  - Rows: 1,820,916,656.
- Warning:
  - 135 zero-row stock-year partitions.
  - Local same-year `daily` cross-check found 128 of those zero-row partitions have same-year daily rows.
  - The affected set is mostly BJ codes; non-BJ same-year daily rows were `302132.SZ` for 2020-2024.
  - This is not a filesystem/download-completion error, but it is a source-coverage/data-availability issue that must be handled before using these names in intraday research.

Macro/global download result:
- Macro default:
  - `cn_schedule`: 77 month partitions, 71 rows.
  - `cn_gdp`: 24 rows.
  - `cn_cpi`: 76 rows.
  - `cn_ppi`: 76 rows.
  - `cn_pmi`: 76 rows.
  - `cn_m`: 76 rows.
  - `sf_month`: 76 rows.
  - `shibor`: 1,577 rows.
  - `shibor_lpr`: 74 rows.
  - `repo_daily`: 14,000 rows.
  - `monetary_policy`: 25 rows.
- Global default:
  - `eco_cal`: 77 month partitions, 6,667 rows.
  - `index_global`: 70 code-year tasks, 15,782 rows.
  - `fx_daily`: 7 year tasks, 1,994 rows.
  - `us_tycr`: 1,597 rows.
  - `us_trycr`: 1,596 rows.
  - `us_tbr`: 1,597 rows.
  - `us_tltr`: 1,597 rows.
  - `libor`: 35 currency-year tasks, 605 rows.
  - `hibor`: 119 rows.
- Supplemental macro:
  - `shibor_quote`: 7 year partitions, 17,954 rows.

Macro audit result:
- Fixed an overly broad warning condition in `scripts/tushare_data.py` where any non-empty key-count dictionary produced a warning.
- Verification: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/python -m compileall -q scripts src tests`.
- Output: `results/data_quality/macro_context_status.json`.
- Status: warning.
- Counts: 0 errors, 2 warnings, 42 infos.
- Remaining warnings:
  - `cn_schedule_macro_keys`: 9 blank `data_api` values.
  - `eco_cal_macro_keys`: 655 duplicate event business keys.

Resource and verification:
- Resource checks were run before/after script execution.
- Final memory check showed about 417Gi available system memory.
- `git diff --check` passed.

## 2026-05-27 Required Data Completeness Audit

Task:
- Audit whether the currently required data is complete after P3 minute, text, macro, and global context downloads.

Commands:
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py audit --raw-dir data/raw --include-limit-list --include-text --include-p3 --start-date 20200101 --end-date 20260519 --p2-start-date 20100101 --p2-end-date 20260520 --text-start-date 20200101 --text-end-date 20260525 --p3-start-date 20200101 --p3-end-date 20260525 --output results/data_quality/combined_status.json`
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/python scripts/tushare_data.py audit-macro --raw-dir data/raw --start-date 20200101 --end-date 20260525 --output results/data_quality/macro_context_status.json`
- Event/flow directory check for `margin`, `margin_detail`, `moneyflow`, `stk_holdernumber`, `stk_holdertrade`, `repurchase`, `share_float`, and `block_trade`.

Combined audit result:
- Output: `results/data_quality/combined_status.json`.
- Created at: `2026-05-27T09:34:17.227495+00:00`.
- Status: warning.
- Counts: 0 errors, 22 warnings, 51 infos.
- Integrated filesystem inventory:
  - 101,875 parquet files.
  - 101,875 sidecar `.meta.json` files.
  - Missing dataset directories: 0 for the audited combined scope.
  - Missing sidecars: 0.
  - Orphan sidecars: 0.
  - Temp files: 0.

Macro audit result:
- Output: `results/data_quality/macro_context_status.json`.
- Created at: `2026-05-27T09:25:15.963995+00:00`.
- Status: warning.
- Counts: 0 errors, 2 warnings, 42 infos.
- Macro/global filesystem inventory: 342 parquet files in the audited macro/global scope.
- Remaining macro warnings:
  - `cn_schedule_macro_keys`: 9 blank `data_api` values.
  - `eco_cal_macro_keys`: 655 duplicate event business keys.

Completeness verdict:
- Structurally complete for the current first research loop:
  - P0 reference/raw dimensions.
  - P1 daily market and trading constraints.
  - P2 financial/fundamental tables.
  - P3 full-A 1-minute raw layer.
  - Text evidence raw layer.
  - Macro/global/policy context raw layer.
- Not warning-free:
  - PIT/unit semantics still must be enforced in feature construction.
  - Raw financial and text tables contain expected duplicate business keys and multi-version records.
  - P3 has 135 zero-row stock-year partitions; earlier cross-check found 128 of them have same-year daily rows, mostly BJ names plus `302132.SZ`, requiring source-coverage handling before intraday research uses those names.
  - `bak_basic` has known source-empty partitions and starts non-empty at 20160809.
  - `daily`/`daily_basic`/`stk_limit` coverage differences are board/fund/BJ semantics, not missing files.
- Not complete if the broader event/flow layer is treated as required:
  - Missing local raw directories: `margin`, `margin_detail`, `moneyflow`, `stk_holdernumber`, `stk_holdertrade`, `repurchase`, `share_float`, and `block_trade`.

Resource:
- Pre-run memory check showed about 416Gi available memory.
- Post-run memory check showed about 424Gi available memory.

## 2026-05-27 P4 Event/Flow Download and Audit

Task:
- Continue the previously missing P4 event/flow layer if required.

Code/docs changes:
- Added P4/event-flow support to `scripts/tushare_data.py` for `margin`, `margin_detail`, `moneyflow`, `stk_holdernumber`, `stk_holdertrade`, `repurchase`, `share_float`, and `block_trade`.
- Added `audit-p4` output to `results/data_quality/event_flow_status.json`.
- P4 trade-date datasets now cap the requested end date to the local SSE `trade_cal` coverage; on this run local `trade_cal` covered through `20260519`.
- P4 audit now only counts files matching the active partition strategy, while reporting ignored legacy non-strategy partitions separately.
- Updated `docs/tushare_data_download_plan.md` with current P4 status and the `share_float` source-cap warning.

Commands:
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download --tier event_flow --raw-dir data/raw --start-date 20200101 --end-date 20260525`
  - Initial one-shot failed before downloading because local SSE `trade_cal` covered only `20100101-20260519`.
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download --tier event_flow --raw-dir data/raw --datasets margin margin_detail moneyflow block_trade --start-date 20200101 --end-date 20260519 --min-interval-seconds 0.22 --timeout-seconds 90 > logs/tushare_p4_event_flow_trade_date_20200101_20260519_20260527.log 2>&1`
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download --tier event_flow --raw-dir data/raw --datasets stk_holdernumber stk_holdertrade repurchase share_float --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 90 > logs/tushare_p4_event_flow_monthly_20200101_20260525_20260527.log 2>&1`
  - `stk_holdernumber`, `stk_holdertrade`, and `repurchase` completed; first `share_float` monthly attempt failed with TuShare 50101 due unsupported pagination.
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download --tier event_flow --raw-dir data/raw --datasets share_float --start-date 20200101 --end-date 20260525 --min-interval-seconds 0.22 --timeout-seconds 90 > logs/tushare_p4_share_float_20200101_20260525_20260527.log 2>&1`
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py audit-p4 --raw-dir data/raw --start-date 20200101 --end-date 20260525 --output results/data_quality/event_flow_status.json > logs/audit_p4_event_flow_20200101_20260525_20260527.log 2>&1`

Download result:
- `margin`: 1,542 trade-date partitions, 3,874 rows, 1,542 pages.
- `margin_detail`: 1,542 partitions, 4,817,891 rows, 1,542 pages.
- `moneyflow`: 1,542 partitions, 7,291,707 rows, 1,542 pages.
- `block_trade`: 1,542 partitions, 207,426 rows, 1,543 pages.
- `stk_holdernumber`: 77 month partitions, 304,607 rows, 134 pages.
- `stk_holdertrade`: 77 month partitions, 100,443 rows, 77 pages.
- `repurchase`: 77 month partitions, 73,427 rows, 78 pages.
- `share_float`: 2,337 natural-day partitions, 6,432,361 rows.

Audit result:
- Output: `results/data_quality/event_flow_status.json`.
- Status: warning.
- Counts: 0 errors, 8 warnings, 10 infos.
- All expected P4 partitions are present; no missing expected files or sidecars.
- Cleanup after user approval:
  - Deleted legacy failed-attempt files `data/raw/share_float/month=202001.parquet`, `data/raw/share_float/month=202001.parquet.meta.json`, `data/raw/share_float/month=202002.parquet`, and `data/raw/share_float/month=202002.parquet.meta.json`.
  - Reran `audit-p4`; `ignored_non_strategy_parquet_files` is now 0.
- Remaining warnings:
  - `moneyflow`: 1 partition exactly at a common limit row count.
  - `stk_holdernumber`: 1 partition exactly at a common limit row count; duplicate event keys.
  - `stk_holdertrade`: duplicate event keys and 4,156 blank `begin_date` values.
  - `repurchase`: duplicate event keys; blank `end_date` and `exp_date` values.
  - `share_float`: 966 natural-day partitions exactly at 6,000 rows, 717 zero-row natural days, 308,660 blank `ann_date` values, and 94,467 duplicate raw keys.
  - `block_trade`: duplicate event keys and 9 blank buyer/seller fields.
- TuShare/MCP probe showed `share_float` returns exactly 6,000 rows even for `float_date` or `ts_code + date` probes, and the MCP interface exposes no `limit/offset` parameters. Treat exact-6,000 `share_float` partitions as possible source-capped data rather than proven complete rows.

Verification and resources:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/bin/python -m compileall -q scripts src tests` passed.
- `git diff --check` passed after code changes.
- Resource checks were run before/after script execution; final memory check showed about 419Gi available system memory.

## 2026-05-27 Share Float Completion Strategy Trial

Task:
- Test whether `share_float` truncation can be mitigated by the proposed `ann_date`, `ann_date+ts_code`, and `float_date+ts_code` strategy.

Code change:
- Added `download-share-float-complete` to `scripts/tushare_data.py`.
- New raw directories:
  - `data/raw/share_float_ann_date/ann_date=YYYYMMDD.parquet`
  - `data/raw/share_float_ann_date_ts_code/ann_date=YYYYMMDD/ts_code=XXXX.parquet`
  - `data/raw/share_float_float_date_ts_code/float_date=YYYYMMDD/ts_code=XXXX.parquet`
- New status file:
  - `results/data_quality/share_float_completion_status.json`

Pilot command:
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download-share-float-complete --raw-dir data/raw --ann-start-date 20190820 --ann-end-date 20190825 --float-start-date 20200106 --float-end-date 20200106 --float-rescue-date 20200106 --rescue-ann-limit-hits --write-union --union-output data/raw/share_float_complete_pilot/share_float_complete.parquet --output results/data_quality/share_float_completion_status.json --min-interval-seconds 0.22 --timeout-seconds 90 > logs/share_float_complete_pilot_20260527.log 2>&1`

Pilot result:
- `ann_date=20190820-20190825`: 6 day partitions, 6,060 rows, 0 limit-hit announcement days.
- `float_date=20200106 + ts_code`: 5,844 code queries, 13,628 rows seen, 1 limit-hit stock:
  - `002973.SZ`, 6,000 rows.
- Pilot union:
  - Output: `data/raw/share_float_complete_pilot/share_float_complete.parquet`.
  - Input files: 5,851.
  - Rows before dedup: 25,688.
  - Rows after dedup: 19,630.
- Interpretation:
  - The existing all-market `float_date=20200106` file with 6,000 rows is materially capped.
  - `float_date+ts_code` can recover many rows, but single-stock single-day caps can still remain.

Full ann_date first-stage command:
- `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/bin/python scripts/tushare_data.py download-share-float-complete --raw-dir data/raw --ann-start-date 20100101 --ann-end-date 20260525 --float-start-date 20200101 --float-end-date 20260525 --output results/data_quality/share_float_completion_status.json --min-interval-seconds 0.22 --timeout-seconds 90 > logs/share_float_ann_date_full_20100101_20260525_20260527.log 2>&1`

Full ann_date result:
- 5,989 expected announcement-day partitions.
- 5,983 newly written, 6 skipped from the pilot window.
- 6,999,549 rows seen.
- 976 announcement days hit the 6,000-row source cap.
- Limit-hit count by year:
  - 2016: 12
  - 2017: 72
  - 2018: 44
  - 2019: 79
  - 2020: 129
  - 2021: 205
  - 2022: 156
  - 2023: 132
  - 2024: 56
  - 2025: 66
  - 2026: 25

Conclusion:
- `ann_date` is still the correct PIT primary path, but it is not complete by itself because many announcement days also hit the 6,000-row cap.
- Full rescue of all 976 limit-hit announcement days by all 5,844 stock codes would be about 5.7M API calls, which is too large for a default download.
- Practical next step is targeted rescue: only run `ann_date+ts_code` or `float_date+ts_code` for key research windows, high-impact dates, or candidate universe stocks. Rows from still-capped finest partitions must carry `source_cap_risk=true`.

Verification and resources:
- Resource checks were run before/after both data jobs; memory remained safe with about 415Gi available at finish.

Follow-up hardening:
- User clarified Python execution should use the `stock` conda environment.
- Verified environment:
  - `~/miniconda3/bin/conda run -n stock python -c "import sys, pandas, pyarrow; ..."`
  - Python: 3.10.16.
  - pandas: 2.0.3.
  - pyarrow: 19.0.1.
- Added targeted rescue controls to `download-share-float-complete`:
  - `--rescue-ann-date`
  - `--rescue-universe {candidate,explicit,all_a}`
  - `--rescue-code`
  - `--rescue-codes-file`
  - `--no-anns-candidates`
  - `--no-cross-path-candidates`
  - `--max-rescue-calls`
- Guard probe:
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --raw-dir data/raw --skip-ann-date --float-rescue-date 20200106 --max-rescue-calls 1 --output results/data_quality/share_float_budget_guard_probe.json`
  - Expected result: failed fast before download with `share_float rescue would make 5844 calls ... exceeding --max-rescue-calls=1`.
  - No probe status file was written.
- Multi-code batching probe:
  - `share_float` with comma-separated `ts_code` values returned 0 rows, so this interface should be treated as one `ts_code` per rescue call.
- Candidate rescue update:
  - Default rescue mode is now `--rescue-universe candidate`, not all-A.
  - Candidate sources are capped partition self-codes, cross-path `share_float` evidence, unlock-related `anns_d` title matches, and explicit user codes/files.
  - `--rescue-universe all_a` is required to scan all `stock_basic` codes.
  - Rechecked `float_date=20200106`: self-candidates from `share_float/date=20200106` were 18 codes, cross-path candidates from the `ann_date` path raised final candidates to 24 codes, matching the nonzero stocks from the earlier all-A rescue. The probe skipped existing files and reported 24 tasks, 13,628 rows seen, and one still-capped stock (`002973.SZ`).
- Verification under `stock`:
  - `~/miniconda3/bin/conda run -n stock python -m compileall -q scripts src tests`
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --help`
  - `git diff --check`

## 2026-05-27 - Full share_float Candidate Supplementation

Task:
- Follow the candidate-only rescue decision and supplement all capped `share_float` announcement-date partitions.
- Keep one complete merged file as a backup artifact.

Pre-run state:
- `results/data_quality/share_float_completion_status.json` from the full `ann_date` scan had 5,989 announcement-day partitions, 6,999,549 rows seen, and 976 exact-6,000 `ann_date` partitions.
- No `data/raw/share_float_ann_date_ts_code/` rescue files existed before this run.
- Resource checks before the probe/download showed safe RAM and no new GPU pressure for this CPU/network-bound job.

Budget probe:
- Command:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --raw-dir data/raw --ann-start-date 20100101 --ann-end-date 20260525 --float-start-date 20200101 --float-end-date 20260525 --rescue-ann-limit-hits --max-ann-rescue-days 2000 --rescue-universe candidate --max-rescue-calls 1 --output results/data_quality/share_float_candidate_budget_probe.json > logs/share_float_candidate_budget_probe_20260527.log 2>&1`
- Result:
  - Failed fast before API rescue calls, as intended.
  - Estimated 16,505 candidate rescue calls across 976 capped `ann_date` dates.

Full candidate rescue command:
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --raw-dir data/raw --ann-start-date 20100101 --ann-end-date 20260525 --float-start-date 20200101 --float-end-date 20260525 --rescue-ann-limit-hits --max-ann-rescue-days 2000 --rescue-universe candidate --max-rescue-calls 50000 --write-union --union-output data/raw/share_float_complete/share_float_complete.parquet --output results/data_quality/share_float_completion_status.json --min-interval-seconds 0.22 --timeout-seconds 90 > logs/share_float_candidate_rescue_full_20260527.log 2>&1`

Full candidate rescue result:
- Runtime: about 67 minutes.
- `ann_date` first-stage files were all skipped/reused: 5,989 skipped, 0 written, 6,999,549 rows seen.
- Candidate rescue:
  - Dates: 976.
  - Tasks: 16,505.
  - Written files: 16,505.
  - Skipped files: 0.
  - Rows seen: 9,487,781.
  - No-candidate dates: 0.
  - Zero-row candidate files: 8,784.
  - Finest partitions still at or above 6,000 rows: 1,369.
- Backup union:
  - Output: `data/raw/share_float_complete/share_float_complete.parquet`.
  - Input files: 30,675.
  - Rows before dedup: 22,933,319.
  - Rows after dedup: 12,735,947.
  - File size: about 136.87 MiB.

Post-run audit:
- Command:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit-p4 --raw-dir data/raw --start-date 20200101 --end-date 20260525 --output results/data_quality/event_flow_status.json > logs/tushare_audit_p4_after_share_float_candidate_20260527.log 2>&1`
- Result:
  - `event_flow_status.json` remains warning with 0 errors, 8 warnings, and 10 infos.
  - This audit is still scoped to the raw P4 source directories, so the original `share_float/date=` path continues to show 966 exact-6,000 partitions, 717 zero-row natural days, and duplicate/blank raw business-key warnings.

Conclusion:
- Candidate supplementation is complete for all known capped `ann_date` partitions in the 20100101-20260525 scope.
- It is not mathematically complete against TuShare source truncation because 1,369 single-stock single-announcement-date files still hit the source cap. These rows must carry `source_cap_risk=true` and downstream features should prefer PIT-safe aggregation rather than relying on exact event counts on those dates.
- `--rescue-universe all_a` remains available as a full-scan backup mode, but the project default remains candidate rescue.

Resource notes:
- Resource checks were run before and after the budget probe, full rescue, separate completeness check, and P4 audit.
- Memory remained safe, with roughly 392-409 GiB available during/after these jobs; GPU usage was from unrelated existing processes and this job did not add GPU load.

## 2026-05-27 - Documentation Naming and Maintenance Policy

Task:
- Rename the active project documents to clearer English names.
- Merge QMT live workflow and Aliyun deployment notes into one QMT document.
- Add durable documentation-maintenance rules to the collaboration instructions.

Changes:
- Renamed `docs/heuristic_learning_trading_system.md` to `docs/project_design_draft.md`.
- Renamed `docs/quant_framework_notes.md` to `docs/code_framework_design.md`.
- Renamed `docs/tushare_data_download_plan.md` to `docs/data_documentation.md`.
- Merged `docs/live_qmt_workflow.md` and `docs/aliyun_qmt_deployment.md` into `docs/qmt_deployment_documentation.md`.
- Removed the old standalone `docs/live_qmt_workflow.md` after its daily workflow, startup, and risk-boundary content was merged.
- Updated document titles to match the new roles.
- Added a `Living Documentation` section to both `AGENTS.md` and `CLAUDE.md`.

Documentation policy now in force:
- `docs/project_design_draft.md`: strategy/system design.
- `docs/code_framework_design.md`: code architecture and implemented boundaries.
- `docs/data_documentation.md`: data sources, download/audit entrypoints, PIT rules, and unit semantics.
- `docs/qmt_deployment_documentation.md`: QMT deployment and live-operation workflow.
- Material changes in any of these areas should update the corresponding document in the same work item.

Verification:
- Checked active references for the old document names across `AGENTS.md`, `CLAUDE.md`, `docs`, `scripts`, `src`, `tests`, `configs`, and top-level metadata.
- Old path references remain only in historical logbook entries, where they describe actions taken before this rename.

## 2026-05-27 - QMT Documentation Adaptation

Task:
- Convert `docs/qmt_deployment_documentation.md` from a historical trading workflow into a MacroQuant-specific live-readiness document.

Changes:
- Removed historical strategy-specific references such as `wfB` order names and old live scheduler commands.
- Documented current state:
  - Remote Aliyun Windows + MiniQMT deployment exists.
  - The MacroQuant repo does not yet have a frozen model, active `scripts/live/` order generator, or approved live trading workflow.
  - Current QMT use is limited to standby, read-only checks, reconcile, and optional dry-run.
- Added current daily standby workflow:
  - Remote QMT health checks.
  - Local research data/feature/evidence maintenance.
  - No live payload generation unless explicitly testing dry-run.
- Added future live workflow and上线门槛:
  - Frozen model/config/data-contract/ledger requirements.
  - Explicit dry-run before live execution.
  - LLM shadow remains non-trading unless separately audited and enabled.
- Added a MacroQuant payload draft with model/config/data-contract/ledger hashes and BUY/SELL order details.

Verification:
- Reviewed the rewritten QMT document for old strategy references.
- Existing `qmt_executor.py` remote commands are preserved as remote deployment assumptions; no local live script was added or run.

## 2026-05-28 - Data Script and Documentation Cleanup Audit Follow-up

Task:
- Respond to the request to remove old P0/P1/P2/P3/P5-facing compatibility language, keep living docs current-only, and check for other source-cap truncation risks.

SubAgent:
- Spawned GPT-5.5 xhigh SubAgent `Galileo` for a read-only editable audit of `scripts/tushare_data.py`, `docs/data_documentation.md`, `data/raw`, and `results/data_quality`.
- Closed the SubAgent after completion.

Audit findings used:
- `data/raw` sidecars and parquet files are structurally paired: 139,293 parquet files and 139,293 `.meta.json` files.
- Besides `share_float`, suspicious cap-risk partitions are:
  - `daily/trade_date=20221118`
  - `adj_factor/trade_date=20220808`
  - `stk_limit/trade_date=20201027`
  - `stk_limit/trade_date=20220705`
  - 21 `balancesheet_vip` period partitions at 7,000 rows with `page_limit=10000/pages=1`
  - `moneyflow/trade_date=20230704` at 5,000 rows with `page_limit=6000/pages=1`
- Text and macro exact-limit samples with an extra empty page are not current truncation evidence.
- `share_float` remains source-cap-risk after candidate rescue; it is not mathematically complete.

Code/doc changes made:
- Public `download --tier` choices now use semantic tier names only: `reference`, `daily`, `fundamental`, `intraday`, `event_flow`, `text_evidence`, `macro`, `global`.
- Removed the old tier compatibility alias table from the public CLI path.
- Replaced public audit subcommands `audit-p3` and `audit-p4` with `audit-intraday` and `audit-event-flow`.
- Started internal semantic renaming for core constants/functions from P0-P5 labels toward reference/daily/fundamental/intraday/event_flow.
- Set the default fundamental page limit to 7,000 so future fundamental downloads page at the observed source cap.
- Set `moneyflow` event-flow page limit to 5,000 so future moneyflow downloads page at the observed source cap.
- Updated `docs/data_documentation.md` to remove historical narrative, fix the “面向人的命名” wording, use `conda run -n stock`, and keep only current data contracts/status constraints.
- Updated `AGENTS.md` and `CLAUDE.md` to say living docs should describe the latest accepted state, while chronology and superseded details belong in logbooks.

Verification status:
- Resource checks were run before the SubAgent, before read-only local scanning, and again after shell access recovered.
- Confirmed `pwd -P` resolves the wrapper path to `/Data/lzp/MacroQuant`.
- `git diff --check` passed.
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare_data.py` passed.
- CLI help checks passed for:
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download --help`
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit --help`
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit-event-flow --help`
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit-intraday --help`
  - `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --help`
- Removed one unused local constant found during the post-SSH复核 (`TRADE_DATE_PAGE_LIMIT`).

## 2026-05-28 - Data Quality Top-level Cleanup

Task:
- Address review comments on `limit_list_d` wording, event/flow priority labels, and redundant `results/data_quality` status files.

Changes:
- Kept human-facing data names in Chinese in `docs/data_documentation.md` and `docs/code_framework_design.md`; TuShare interface ids such as `limit_list_d` remain in interface columns or CLI examples.
- Removed the `优先级` column from the event/flow table in `docs/data_documentation.md`.
- Moved ad hoc or duplicate status files out of `results/data_quality/`:
  - `combined_status.json`
  - `ingestion_reliability_status.json`
  - `share_float_candidate_probe.json`
  - `share_float_completion_status.json`
- Archive destination: `archive/data_quality/legacy/20260528_cleanup/`.
- Changed future default output paths for combined audit and share-float completion process statuses to `results/data_quality/process/`; historical process files should move to root `archive/` when superseded.

Current top-level `results/data_quality/` files:
- `base_research_status.json`
- `macro_context_status.json`
- `intraday_minutes_status.json`
- `event_flow_status.json`
- `text_evidence_status.json`

Verification:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare_data.py` passed.
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit --help` passed.
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --help` passed.
- Current docs/scripts wording scan found no hits for the removed event/flow priority table, the old `limit_list_d` prose phrasing, or old top-level combined/share-float status paths. Historical logbook entries still retain old paths as factual command history.
- `git diff --check` passed.
- Resource checks stayed safe.

## 2026-05-28 - Combined and Share Float Status Merge

Task:
- Merge the two process-style status outputs into the current audit model instead of maintaining them as separate data-quality files.

Changes:
- Removed default persistent constants for combined audit and share-float completion process status files.
- Unified audit with both `--include-text` and `--include-intraday` now requires an explicit `--output`; this keeps combined audits as ad hoc diagnostics.
- `download-share-float-complete` now writes no status file by default. Passing `--output` still writes a process report when a run needs traceability beyond runtime logs and the durable logbook.
- Added `share_float_complete_union` to `audit-event-flow`; it checks:
  - `data/raw/share_float_complete/share_float_complete.parquet`
  - sidecar presence and row count
  - source download-path counts
  - remaining `source_cap_risk` rows
  - finest rescue files still at or above the 6,000-row cap
- Updated `docs/data_documentation.md` to state that `event_flow_status.json` is the single current status file for the event/flow layer.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare_data.py` passed.
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit --help` passed.
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py download-share-float-complete --help` passed.
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit --include-text --include-intraday --end-date 20200102 --fundamental-end-date 20200102` failed fast as intended, requiring explicit `--output`.
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py audit-event-flow --raw-dir data/raw --start-date 20200101 --end-date 20260525` passed with status warning, 0 errors, 9 warnings, and 10 infos.
- Merged `share_float_complete_union` details: 12,735,947 union rows, sidecar row count 12,735,947, 10,697,263 source-cap-risk rows, 1,370 rescue files still at or above the 6,000-row cap.

## 2026-05-28 - Full Data Coverage and Unit Audit

Task:
- Open a SubAgent to audit all current data for units, completeness, PIT semantics, and whether the current data documentation's 2020-202605 scope has been downloaded.

SubAgent:
- Spawned GPT-5.5 xhigh SubAgent `Bernoulli`.
- Scope was read-only: `AGENTS.md`, `docs/data_documentation.md`, `scripts/tushare_data.py`, `results/data_quality/*.json`, and `data/raw`.
- Closed the SubAgent after completion.

Main-thread checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant`.
- Resource checks before and after read-only scans stayed safe; memory available was about 390-392Gi and no GPU job was started by this audit.
- Status summary: `base_research_status.json`, `event_flow_status.json`, `intraday_minutes_status.json`, `macro_context_status.json`, and `text_evidence_status.json` are all warning with 0 errors.
- Raw metadata scan over the documented data directories found no bad Parquet files in the scanned set and matching sidecars for every scanned dataset.

Coverage findings:
- Current retained data items in `docs/data_documentation.md` are downloaded.
- Trading-day datasets cover through local SSE calendar `20260519`.
- Natural-day/monthly/text/share-float datasets cover through `20260525` or `202605` where appropriate.
- Financial period datasets cover through `20260331`; forecast/express cover through `202605`.
- `stk_mins_1min` has 35,855 stock-year files and 1,820,916,656 rows for 2020-2026.
- Text evidence row counts: `anns_d` 9,340,185; `major_news` 2,726,603; `cctv_news` 35,142; `npr` 8,552; `research_report` 234,836; `report_rc` 1,477,359; `news` 10,258,167.
- `share_float_complete` union exists with 12,735,947 rows.

Unit/PIT findings:
- No contradiction with `docs/data_documentation.md` was found.
- SubAgent sample check for `000001.SZ` on `20240102` found minute `vol` to daily `vol` ratio of 100 and minute `amount` to daily `amount` ratio of 1000, matching `stk_mins` 股/元 vs `daily` 手/千元.
- `daily_basic.total_share` vs `bak_basic.total_share` ratio is about 10000, matching 万股 vs 亿股.
- `bak_basic` has no `vol/amount`; local `data/raw/bak_daily` does not exist and is not part of the current retained raw boundary.
- Row-level `available_at` exists for intraday, event/flow, text, and macro/global raw layers. Daily/reference/fundamental still require feature-layer PIT rules.

Residual risks:
- `share_float` remains source-cap-risk: 10,697,263 union rows marked source-cap-risk and 1,370 finest rescue files still at or above the 6,000-row cap.
- Minute data has 135 zero-row stock-year partitions; use effective stock universe/listing filters before minute research.
- Financial, event, and text raw tables intentionally retain duplicate business keys; feature/evidence layers must select records by PIT and business key.
- `cn_schedule` is sparse and has blank `data_api` rows; `eco_cal` has duplicate event keys and heterogeneous event values.
- Exact-limit candidates still need targeted review: `daily/trade_date=20221118`, `adj_factor/trade_date=20220808`, `stk_limit/trade_date=20201027`, `stk_limit/trade_date=20220705`, selected `balancesheet_vip` periods, `moneyflow/trade_date=20230704`, and `stk_holdernumber` 202511.

Conclusion:
- The current raw layer is structurally complete enough for 2020+ research.
- The remaining issues are source/semantic/PIT risks rather than missing broad downloads.

## 2026-05-28 - Targeted Risk Redownload and Audit Refresh

Task:
- Recheck remaining risk items and redownload sparse or missing-looking data where a retry could reasonably improve completeness.

Code change:
- Added paged download support for daily trade-date datasets in `scripts/tushare_data.py`.
- Daily tier default `page_limit` is now 5,000, and sidecar metadata records pagination pages for refreshed daily partitions.
- Updated exact-limit audit logic so files with a successful extra-page probe are not counted as unverified exact-limit partitions.

Targeted redownloads:
- Daily exact-limit candidates:
  - `daily/trade_date=20221118`
  - `adj_factor/trade_date=20220808`
  - `stk_limit/trade_date=20201027`
  - `stk_limit/trade_date=20220705`
- Fundamental exact-7000 candidates:
  - `balancesheet_vip` periods from `20121231` through `20231231`.
  - Former exact-7000 periods expanded to 7,005-11,771 rows where the source had second-page records.
- Event/flow exact candidates:
  - `moneyflow/trade_date=20230704`
  - `stk_holdernumber/month=202511`
  - Both now have extra-page probe sidecars and no longer trigger exact-limit partition warnings.
- Macro sparse candidates:
  - `cn_schedule`, `eco_cal`, `shibor_quote`, `hibor`, `libor`.
  - Force refresh did not change row counts; remaining zero partitions are treated as source sparsity.
- Intraday zero-row candidates:
  - Re-ran all 135 zero-row `stk_mins_1min` stock-year partitions by year.
  - All still returned 0 rows, so these are source/effective-universe issues rather than transient request failures.

Audit refresh:
- `results/data_quality/base_research_status.json`: warning, 0 errors, 16 warnings.
- `results/data_quality/event_flow_status.json`: warning, 0 errors, 7 warnings.
- `results/data_quality/macro_context_status.json`: warning, 0 errors, 2 warnings.
- `results/data_quality/intraday_minutes_status.json`: warning, 0 errors, 1 warning.

Remaining risks:
- `share_float` source cap remains unresolved: original float-date path has 966 exact-6000 partitions and the complete union still has source-cap-risk rows plus 1,370 finest rescue files at or above the 6,000-row cap.
- Minute data still has 135 zero-row stock-year partitions after retry.
- Macro calendar sparsity and `eco_cal` duplicate/heterogeneous event keys remain source/semantic issues.
- Financial, event, and text duplicate business keys remain raw-layer PIT/version-selection issues.

## 2026-05-28 - Full Daily Feature and Experiment Pass

Task:
- Move from raw-data readiness into the first reproducible daily experiment loop.
- Rebuild 2020-2025 PIT daily features, run full development WFO, and run one held-out control.

Environment and resource checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant`.
- Used `~/miniconda3/bin/conda run -n stock python` for all Python commands.
- Memory stayed safe across runs, with roughly 409-422Gi available after major steps.
- No new GPU workload was started; existing unrelated GPU processes remained outside this task.

Code changes:
- Replaced Python 3.11-only `datetime.UTC` usage in `src/hl_trader/storage/ledger.py` with `timezone.utc` for the project `stock` Python 3.10 environment.
- Replaced pandas test fixture `freq="ME"` with `freq="M"` in `tests/unit/test_formulaic_wfo_runner.py` for the installed pandas version.
- Added `--ledger-path` override support to `scripts/hl.py` for `run-development` and `run-heldout`, so smoke/full/held-out runs can write separate JSONL ledgers.
- Updated `docs/code_framework_design.md` to use `conda run -n stock` commands and document separate ledger paths.

Commands and artifacts:
- Feature build:
  - `PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/hl.py build-features --raw-dir data/raw --output-root data/features --dataset daily_alpha --start-date 20200102 --end-date 20251231`
  - Log: `logs/hl_build_features_daily_alpha_20200102_20251231_20260528.log`
  - Output: `data/features/daily_alpha`, 1,455 partitions, 7,095,173 rows, about 1.4G.
- Development smoke:
  - `run-development --max-folds 1`
  - Log: `logs/hl_run_development_pilot_2020_daily_maxfold1_20260528.log`
  - Result: 1 fold, test_return -2.4575%, 145 fills.
- Full development WFO:
  - `run-development --ledger-path experiments/trial_ledger/pilot_2020_daily_full_20260528.jsonl`
  - Log: `logs/hl_run_development_pilot_2020_daily_full_20260528.log`
  - Result: 7 folds, median test_return -0.7563%, positive fold rate 28.57%, worst fold -5.3703%, total fills 910.
  - Modal development parameters: `top_n=80`, `max_pe_ttm_quantile=0.2`, `max_pb_quantile=0.2`, `min_amount_quantile=0.2`.
- Held-out control:
  - `run-heldout --ledger-path experiments/trial_ledger/pilot_2020_daily_heldout_mode_20260528.jsonl --top-n 80 --max-pe-ttm-quantile 0.2 --max-pb-quantile 0.2 --min-amount-quantile 0.2 --model-id formulaic_mode_control --treatment control_formulaic_mode`
  - Log: `logs/hl_run_heldout_pilot_2020_daily_mode_20260528.log`
  - Result: 2025 held-out return +4.5687%, ending equity 1,045,686.65, 363 fills.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m compileall -q src tests scripts` passed.
- Targeted experiment and WFO tests passed after compatibility fixes.

Conclusion:
- The daily PIT feature builder, development WFO, execution ledger, event action logging, and held-out runner are now proven on current local 2020-2025 data.
- The first formulaic value/quality control is weak in development despite positive 2025 held-out, so it should be treated as a pipeline/control result rather than a tradable strategy.
- Next engineering priorities are benchmark/excess-return reporting, WFO runtime optimization, and adding event/macro/text PIT feature layers before spending more API budget on LLM decision experiments.

## 2026-05-28 - Intraday Minute By-Date Storage Implementation

Task:
- Implement the minute storage logic discussed after comparing the existing `ts_code/year` TuShare bulk layout with the ChouQuant-style daily full-market layout.
- Keep the script structure simple and avoid persistent staging outputs.

Environment and resource checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant`.
- Used `~/miniconda3/bin/conda run -n stock python` for verification.
- Memory was safe before validation, with about 410Gi available. No new GPU workload was started.

Code changes:
- Added `compact-intraday-by-date` to `scripts/tushare_data.py`.
  - Reads existing `data/raw/stk_mins_1min/ts_code=<TS_CODE>/year=<YYYY>.parquet` source partitions.
  - Writes final files to `data/raw/stk_mins_1min_by_date/trade_date=<YYYYMMDD>.parquet`.
  - Normalizes required fields, drops duplicate `(ts_code, trade_time)` rows, validates date/time/PIT fields, and records validation details in sidecar metadata.
- Added `update-intraday-by-date`.
  - Downloads one or more trade dates directly into the final date file.
  - Keeps per-code retry data in memory only; if missing codes exceed the configured tolerance, the date file is not written.
- Added `audit-intraday-by-date`.
  - Checks final date-organized files for inventory, sidecars, required schema, duplicate keys, wrong dates, invalid timestamps, auction bars, and optional expected-code coverage.
- Added `tests/unit/test_tushare_intraday_by_date.py` covering compact + audit on a temporary minute fixture.
- Updated `docs/data_documentation.md` and `docs/code_framework_design.md` to document the two minute layouts and the preferred research/update path.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts tests src` passed.
- CLI help checks passed:
  - `scripts/tushare_data.py compact-intraday-by-date --help`
  - `scripts/tushare_data.py update-intraday-by-date --help`
  - `scripts/tushare_data.py audit-intraday-by-date --help`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest tests.unit.test_tushare_intraday_by_date -v` passed.
- Related existing tests passed for contracts/config, daily PIT features, and formulaic WFO.
- Full unit discovery passed with 82 tests OK.

Result:
- The code now supports source `code+year` bulk history and final `trade_date` grouped minute files without keeping extra intermediate parquet files.
- No real TuShare download or full historical by-date compaction was run in this step.

## 2026-05-28 - Final Intraday and Share-Float Storage Cleanup

Task:
- Fully compact historical 1-minute data into daily full-market files.
- Audit the final by-date minute layer.
- Move old minute source and `share_float` process folders out of active `data/raw`, keeping only final storage boundaries.

Environment and resource checks:
- `pwd -P` confirmed `/Data/lzp/MacroQuant`.
- Used `~/miniconda3/bin/conda run -n stock python` for all Python commands.
- Memory remained safe during the long compaction and audits; available memory stayed roughly 385-404Gi after major checks.
- No new GPU workload was started; visible GPU processes were unrelated to this data-storage task.

Code changes:
- `download_trade_cal` now refreshes and merges an existing year partition if the local file does not cover the requested date range.
- `audit-intraday-by-date` now defaults to `results/data_quality/intraday_minutes_status.json`, making the by-date minute layer the current retained intraday status.
- `audit-event-flow` treats `share_float_complete/share_float_complete.parquet` as the retained `share_float` boundary when the union exists, so archived intermediate `share_float` paths do not create false missing-partition errors.
- Updated `docs/data_documentation.md` to describe active by-date minute storage and active `share_float_complete` union storage.

Data refresh:
- Refreshed 2026 SSE/SZSE/BSE `trade_cal` to cover `20260525`.
- After the calendar refresh, downloaded the newly expected event trade-date partitions for `20260520`, `20260521`, `20260522`, and `20260525`:
  - `margin`: 12 rows.
  - `margin_detail`: 17,446 rows.
  - `moneyflow`: 20,757 rows.
  - `block_trade`: 607 rows.

Compaction:
- Command:
  - `PYTHONUNBUFFERED=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py compact-intraday-by-date --raw-dir data/raw --start-date 20200101 --end-date 20260525 --expected-codes-source none --min-rows-per-day 1`
- Log:
  - `logs/compact_intraday_by_date_20200101_20260525_20260528.log`
- Result:
  - Wrote `data/raw/stk_mins_1min_by_date`.
  - 1546 trade-date Parquet files.
  - 1,820,916,656 rows.
  - About 25G.

Audits:
- Structural by-date audit:
  - `results/data_quality/intraday_minutes_status.json`
  - Status ok, 0 errors, 0 warnings.
  - 1546/1546 expected files, 1546 sidecars, 0 missing files, 0 missing sidecars, 0 schema misses, 0 zero-row files, full-scan row/key/time/PIT checks passed.
- Daily-universe coverage audit:
  - `results/data_quality/process/intraday_minutes_by_date_daily_coverage_status.json`
  - Status warning, 0 errors, 1 warning.
  - 1542 expected trading days through `20260519`; inventory complete, but 1228 days have `daily` universe mismatches. Samples are dominated by BJ/effective-universe differences and some abnormal/legacy codes, so feature construction still needs an explicit minute-eligible universe.
- Event-flow audit after archiving `share_float` intermediates:
  - `results/data_quality/event_flow_status.json`
  - Status warning, 0 errors, 5 warnings.
  - Remaining warnings are event business-key/blank-date semantics and `share_float_complete` source-cap risk, not missing files.

Archive moves:
- Moved to `archive/data_raw/20260528_final_storage_cleanup/`:
  - `stk_mins_1min_source` from `data/raw/stk_mins_1min`.
  - `share_float_float_date` from `data/raw/share_float`.
  - `share_float_ann_date`.
  - `share_float_ann_date_ts_code`.
  - `share_float_float_date_ts_code`.
  - `share_float_complete_pilot`.
- Active retained directories now include:
  - `data/raw/stk_mins_1min_by_date` for historical minute research/update.
  - `data/raw/share_float_complete` for unlock-share evidence.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts tests src` passed.
- CLI help checks passed for `audit-intraday-by-date` and `audit-event-flow`.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest tests.unit.test_tushare_intraday_by_date -v` passed.
- Full unit discovery passed with 82 tests OK.

Conclusion:
- Minute data is now available in the PIT-friendlier daily full-market layout.
- Old minute source and `share_float` process paths are preserved under root `archive/` rather than deleted.
- The remaining risks are semantic universe selection and `share_float` source caps, not failed compaction or missing retained files.

## 2026-05-28 - Documentation Split for Data Audits, PIT, WFO, and LLM Agent

Task:
- Document the five current data-domain audit code paths and PIT construction rules.
- Split the single code-framework document into clearer WFO/environment and LLM Agent design documents.

Context:
- `pwd -P` confirmed the real repository path is `/Data/lzp/MacroQuant`.
- This was a documentation-only change; no data download, training, inference, evaluation, or feature build was run.

Documentation changes:
- Expanded `docs/data_documentation.md` with:
  - Status-file schema shared by the five current top-level data quality files.
  - Detailed logic for base research, macro/global context, intraday by-date, event/flow, and text evidence audits.
  - Current PIT construction path for `daily_alpha`, leakage checks, raw/evidence availability rules, and evidence-pack PIT boundaries.
  - Converted the older raw PIT section into a short visibility quick reference to avoid duplicated rules.
- Replaced `docs/code_framework_design.md` with a concise framework index.
- Added `docs/walk_forward_environment_design.md` covering:
  - Experiment config contracts.
  - PIT feature environment.
  - Rolling WFO folds.
  - Development WFO and held-out control.
  - Broker execution, event actions, freeze specs, and ledgers.
- Added `docs/llm_agent_design.md` covering:
  - Shadow-only safety boundary.
  - Evidence pack schema and hash rules.
  - Feature-file to pack construction.
  - Prompt/response validation.
  - DeepSeek adapter and future provider-agnostic extension rules.
- Updated `AGENTS.md` and `CLAUDE.md` so future living-doc maintenance includes the new WFO/environment and LLM Agent documents.

Verification:
- Checked for trailing whitespace in the edited docs and repository instruction files.
- Ran `git diff --check -- AGENTS.md CLAUDE.md`.

Conclusion:
- The data audit and PIT logic are now documented in the data contract document.
- Code framework documentation is split by responsibility while keeping `docs/code_framework_design.md` as the stable index.

## 2026-05-28 - Four-Document Maintenance Cleanup

Task:
- Remove the code-framework index document and keep only four maintained living documents.

Context:
- `pwd -P` confirmed the real repository path is `/Data/lzp/MacroQuant`.
- This was a documentation-only change; no data download, training, inference, evaluation, or feature build was run.

Documentation changes:
- Removed `docs/code_framework_design.md`.
- Renamed:
  - `docs/llm_agent_design.md` to `docs/agent_design.md`.
  - `docs/walk_forward_environment_design.md` to `docs/environment_design.md`.
  - `docs/qmt_deployment_documentation.md` to `docs/QMT_documentation.md`.
- Kept `docs/data_documentation.md` unchanged because it already matches the requested name.
- Updated internal links between the Agent and environment documents.
- Updated `AGENTS.md` and `CLAUDE.md` so the maintained living-document set is exactly:
  - `docs/data_documentation.md`
  - `docs/agent_design.md`
  - `docs/environment_design.md`
  - `docs/QMT_documentation.md`

Conclusion:
- Current living-document maintenance is now limited to the requested four files.

## 2026-05-28 - Agent and Environment Code Refactor

Task:
- Reorganize the HL codebase into explicit Agent and Environment layers before real LLM trading integration.

Context:
- `pwd -P` confirmed the real repository path is `/Data/lzp/MacroQuant`.
- Resource checks were run before and after compile/tests.
- No TuShare download, feature build, training run, evaluation run, or real LLM API call was made.

Code changes:
- Created `src/hl_trader/environment/` for market-environment code:
  - `data`, `features`, `leakage`, `wfo`, `backtest`, `execution`, `events`, `portfolio`, `evaluation`, `protocols`, `schemas`, and `storage`.
- Created `src/hl_trader/agent/` for decision/evidence/provider code:
  - `formulaic.py`
  - `evidence/`
  - `llm/`
  - `shadow/`
- Moved cross-layer orchestration to `src/hl_trader/pipelines/`:
  - `formulaic_wfo.py` now holds the formulaic WFO runner because it combines Agent candidate selection with Environment replay/execution.
  - `experiment.py` and `llm_shadow.py` remain pipeline entrypoints.
- Moved formulaic strategy primitives out of the WFO runner:
  - `FormulaicParameters`
  - `parameter_grid`
  - `select_formulaic_candidates`
  - `FormulaicScoreRule`
  - `score_cross_section`
- Updated `scripts/hl.py` and unit tests to use the new imports.
- Removed old source-level package entry files for `heuristics` and `tracks`; old non-source directories may still contain ignored `__pycache__` artifacts.
- Added architecture-boundary tests that fail if `environment` imports `agent` or if old top-level source packages regain Python modules.

Architecture rule:
- `environment` must not import `agent`.
- `agent` may consume Environment-produced PIT/evidence/ledger primitives.
- `pipelines` may combine both layers.
- `scripts` should only parse CLI arguments and call pipelines.

Documentation:
- Updated `docs/environment_design.md` with the new module layout and import-direction rule.
- Updated `docs/agent_design.md` with the new Agent module layout.
- Updated `docs/data_documentation.md` to point PIT feature construction at `src/hl_trader/environment/features/daily_pit.py`.

Verification:
- Resource checks:
  - Final verification run started with about 374Gi available system memory; GPUs were already busy with unrelated processes.
  - After tests, system memory remained about 374Gi available; no new GPU workload was started.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts tests src` passed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest discover -s tests/unit -p 'test_*.py' -v` passed with 84 tests OK.
- Import scan found no `hl_trader.agent` import inside `src/hl_trader/environment`.
- Current docs/source scan found no references to the removed top-level source package paths in the maintained docs.
- `git diff --check` passed for the edited docs, scripts, source, and tests.

Conclusion:
- The codebase now has a clear Agent/Environment split suitable for later LLM integration without letting Agent code own PIT visibility, execution state, or market replay.

## 2026-05-28 - Pipeline Living Document Split

Task:
- Add a standalone Pipeline design document and redistribute existing Agent/Environment documentation by responsibility.

Context:
- `pwd -P` had confirmed the real repository path as `/Data/lzp/MacroQuant` earlier in the work item.
- This was a documentation-only change; no data download, training, inference, evaluation, feature build, or real LLM call was run.

Documentation changes:
- Added `docs/pipeline_design.md` as the fifth living document.
- Moved orchestration detail out of `docs/environment_design.md`:
  - development WFO run flow
  - held-out control run flow
  - formulaic WFO runner behavior
  - LLM shadow CLI flow
- Rewrote `docs/environment_design.md` around Environment primitives:
  - PIT data reader
  - PIT feature builder
  - leakage checks
  - WFO fold generation
  - broker/execution/replay/events
  - protocol guards and ledgers
- Rewrote `docs/agent_design.md` around Agent contracts:
  - formulaic parameters/scoring/candidate selection
  - evidence pack
  - LLM shadow advisor
  - NL shadow recorder
  - DeepSeek provider adapter
  - shadow-only upgrade boundary
- Updated `docs/QMT_documentation.md` to point research-side execution flow to `docs/pipeline_design.md`.
- Updated `AGENTS.md` and `CLAUDE.md` so the current living document set is:
  - `docs/data_documentation.md`
  - `docs/agent_design.md`
  - `docs/environment_design.md`
  - `docs/pipeline_design.md`
  - `docs/QMT_documentation.md`

Conclusion:
- Pipeline is now documented as its own orchestration layer rather than being mixed into Agent or Environment.

Follow-up cleanup:
- Moved the superseded `docs/project_design_draft.md` to `archive/project_design_draft.md`.
- `docs/` now contains only the five maintained living documents plus `docs/logbook/`.

Additional data-quality cleanup:
- Clarified `docs/data_documentation.md` so `results/data_quality/process/` is a temporary processing area only.
- Moved completed process outputs to `archive/data_quality/20260528_process_cleanup/`:
  - `intraday_minutes_by_date_status.json`
  - `intraday_minutes_by_date_daily_coverage_status.json`
- Current `results/data_quality/` is expected to keep only the five domain status files.
- Reorganized `docs/data_documentation.md` order so data download boundaries and commands come first, audit/status rules come second, and PIT construction rules come last.
- Removed redundant cross-domain audit wording; the retained rule is that any temporary report combining multiple top-level data domains must use an explicit `--output` and must not overwrite one of the five current status files.
- Refined the PIT documentation boundary:
  - `docs/data_documentation.md` now keeps raw PIT data contracts only: sidecar, raw availability candidates, business keys, unit rules, source-cap and sparse-data risks.
  - `docs/environment_design.md` now owns PIT feature/observation construction, selector rules, leakage checks, and `decision_time` visibility.

## 2026-05-28 - TuShare Update CLI Split

Task:
- Add repeatable daily and periodic data-update commands.
- Split TuShare download/update commands from audit commands while keeping the implementation simple and correct.

Context:
- `pwd -P` confirmed the real repository path is `/Data/lzp/MacroQuant`.
- Resource checks were run before and after Python verification.
- No real TuShare data download or raw data audit run was started in this work item.

Code changes:
- Moved the shared TuShare implementation to `scripts/tushare_core.py`.
- Added `scripts/tushare_download.py` as the formal download/update CLI:
  - `download`
  - `update`
  - `compact-intraday-by-date`
  - `update-intraday-by-date`
  - `download-share-float-complete`
- Added `scripts/tushare_audit.py` as the formal audit CLI:
  - `base`
  - `intraday`
  - `intraday-by-date`
  - `event-flow`
  - `macro`
- Kept `scripts/tushare_data.py` as a compatibility wrapper for old imports and old combined commands.
- Added update orchestration:
  - `update --mode daily` refreshes recent daily market data, trading constraints, lightweight calendar/reference data, and trade-date event/flow datasets.
  - `update --mode periodic` refreshes recent reference/fundamental/macro/global/text/sparse-event windows.
  - `update --mode all` runs both paths.
  - Full-market minute updates are opt-in with `--include-intraday`.
  - `share_float_complete` refresh is opt-in with `--include-share-float-complete`.
- Fixed a split-entrypoint bug before completion: periodic text updates now accept `--text-start-date`, and the core function uses a guarded `getattr` for `text_start_date`.

Documentation:
- Updated `docs/data_documentation.md` to document the split entrypoints, daily update commands, periodic update commands, optional intraday/share-float updates, and the audit command names.
- Updated `docs/pipeline_design.md` so raw data download/audit responsibility points to `scripts/tushare_download.py`, `scripts/tushare_audit.py`, and `docs/data_documentation.md`.

Verification commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare_core.py scripts/tushare_download.py scripts/tushare_audit.py scripts/tushare_data.py`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_download.py update --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_download.py download --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_audit.py base --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_audit.py macro --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_audit.py event-flow --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_download.py download-share-float-complete --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py --help`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m unittest tests.unit.test_tushare_intraday_by_date -v`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest discover -s tests/unit -p 'test_*.py' -v`
- `git diff --check -- AGENTS.md CLAUDE.md docs scripts src tests LOGBOOK.md`

Verification result:
- Compile and all CLI help checks passed.
- Compatibility wrapper help passed.
- Targeted intraday-by-date test passed.
- Full unit discovery passed with 84 tests OK.
- `git diff --check` passed.

Conclusion:
- The data tooling now has separate download/update and audit entrypoints while preserving old command compatibility.
- Daily and periodic refreshes are available but remain explicit commands; no unattended scheduler was installed.

## 2026-05-28 - TuShare Script Package Cleanup

Task:
- Reorganize the TuShare scripts under a dedicated folder and reduce the previous single large implementation file.
- Clarify daily update behavior for both daily and lower-frequency data.

Context:
- `pwd -P` confirmed the real repository path is `/Data/lzp/MacroQuant`.
- Resource checks were run before and after verification.
- No real TuShare data download or raw audit was started.

Code changes:
- Created `scripts/tushare/` as the formal TuShare script package.
- Moved formal entrypoints:
  - `scripts/tushare/download.py`
  - `scripts/tushare/audit.py`
- Split the former 5271-line `scripts/tushare_core.py` implementation into:
  - `scripts/tushare/common.py`: constants, dataset specs, TuShare client, shared date/path/PIT/unit helpers, minute validation helpers.
  - `scripts/tushare/download_ops.py`: reference/daily/fundamental/macro/global/intraday/event/text/share_float download and update operations.
  - `scripts/tushare/audit_ops.py`: base/macro/intraday/event/text audit operations and status report construction.
  - `scripts/tushare/legacy_cli.py`: former combined-command compatibility CLI.
- Replaced root TuShare files with compatibility wrappers:
  - `scripts/tushare_download.py`
  - `scripts/tushare_audit.py`
  - `scripts/tushare_core.py`
  - `scripts/tushare_data.py`
- Fixed a split dependency issue by placing `load_minute_universe` in `common.py`, because both intraday download/update and intraday by-date audit need it.

Update behavior:
- `scripts/tushare/download.py update --mode all` is the recommended daily research/live-data refresh command.
- `update --mode periodic/all` now refreshes recent active month/year/period partitions by default through `--refresh-existing-periodic`.
- `--no-refresh-existing-periodic` is available for skip-only checks.
- Default periodic fundamental updates include report-period and announcement-month datasets:
  - `income_vip`
  - `balancesheet_vip`
  - `cashflow_vip`
  - `fina_indicator_vip`
  - `forecast_vip`
  - `express_vip`
  - `disclosure_date`
- Per-ts-code long fundamental tables such as `dividend`, `fina_audit`, and `fina_mainbz_vip` are not refreshed by default in daily periodic mode; they can be requested explicitly with `--fundamental-datasets`.
- Full-market intraday minute refresh and `share_float_complete` remain explicit opt-ins with `--include-intraday` and `--include-share-float-complete`.

Documentation:
- Updated `docs/data_documentation.md` with the new `scripts/tushare/` structure, formal command paths, compatibility wrapper policy, and daily update semantics.
- Updated `docs/pipeline_design.md` so raw data responsibilities point to the new formal entrypoints.

Verification commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare scripts/tushare_core.py scripts/tushare_data.py scripts/tushare_download.py scripts/tushare_audit.py`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py event-flow --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_download.py update --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_audit.py macro --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare_data.py compact-intraday-by-date --help`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m unittest tests.unit.test_tushare_intraday_by_date -v`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest discover -s tests/unit -p 'test_*.py' -v`
- `git diff --check -- AGENTS.md CLAUDE.md docs scripts src tests LOGBOOK.md`

Verification result:
- Compile passed.
- New formal CLI paths and compatibility wrapper help checks passed.
- Targeted intraday by-date unit test passed.
- Full unit discovery passed with 84 tests OK.
- `git diff --check` passed.

Conclusion:
- TuShare tooling now has two formal user-facing scripts under `scripts/tushare/`, several small compatibility wrappers at root, and implementation split into maintainable modules.
- Daily updates can use one command for both daily and lower-frequency sources while keeping heavy intraday and share-float completion explicit.

## 2026-05-28 - TuShare Final Script Simplification

Task:
- Remove the outer TuShare compatibility files.
- Remove the redundant inner `download.py`/`download_ops.py` and `audit.py`/`audit_ops.py` split.

Code changes:
- Deleted root compatibility wrappers:
  - `scripts/tushare_data.py`
  - `scripts/tushare_download.py`
  - `scripts/tushare_audit.py`
  - `scripts/tushare_core.py`
- Deleted inner compatibility/ops split:
  - `scripts/tushare/download_ops.py`
  - `scripts/tushare/audit_ops.py`
  - `scripts/tushare/legacy_cli.py`
- Merged download/update implementation directly into `scripts/tushare/download.py`.
- Merged audit implementation directly into `scripts/tushare/audit.py`.
- Kept shared definitions in `scripts/tushare/common.py`.
- Updated `tests/unit/test_tushare_intraday_by_date.py` to load the formal `download.py` and `audit.py` modules directly.

Current TuShare script surface:
- `scripts/tushare/download.py`: formal download, update, compaction, intraday refresh, and share-float completion CLI plus implementation.
- `scripts/tushare/audit.py`: formal audit CLI plus implementation.
- `scripts/tushare/common.py`: shared constants, dataset specs, client, and helpers.

Documentation:
- Updated `docs/data_documentation.md` to remove compatibility-wrapper and `*_ops.py` references.
- `docs/pipeline_design.md` already pointed to the formal entrypoints and did not need further structural change.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m compileall -q scripts/tushare`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py download-share-float-complete --help`
- `~/miniconda3/bin/conda run -n stock python scripts/tushare/audit.py event-flow --help`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/bin/conda run -n stock python -m unittest tests.unit.test_tushare_intraday_by_date -v`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/bin/conda run -n stock python -m unittest discover -s tests/unit -p 'test_*.py' -v`
- `git diff --check -- AGENTS.md CLAUDE.md docs scripts src tests LOGBOOK.md`

Conclusion:
- The TuShare tooling no longer has outer compatibility files or inner `*_ops.py` indirection.
- The remaining file count is minimal while keeping download, audit, and shared contracts separated.

## 2026-05-28 - TuShare Update To 20260528

Task:
- Trial the current daily/periodic TuShare update path through `20260528`.
- Keep the simplified `scripts/tushare/` structure and fix only issues exposed by the run.

Resource checks:
- Confirmed real repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Checked system RAM and GPU state before and after data/audit runs.
- RAM stayed safe with roughly 395-397Gi available; no GPU workload was launched by these commands.

Initial update command:
- `PYTHONUNBUFFERED=1 ~/miniconda3/bin/conda run -n stock python scripts/tushare/download.py update --mode all --end-date 20260528 --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_update_all_20260528.log 2>&1`

Issues found and fixed:
- `daily_event_flow` failed because `latest_sse_calendar_date` still lived only in `audit.py` after the script merge.
  - Moved `latest_sse_calendar_date` into `scripts/tushare/common.py`.
  - Removed the duplicate implementation from `scripts/tushare/audit.py`.
- `update --mode all` then spent time in periodic `reference` because default periodic refresh forced static tables such as `namechange`.
  - Stopped the run started by this session.
  - Changed periodic `reference` so static reference refresh only happens with explicit `--force`.
- Macro update failed on `month_end_from_yyyymm`.
  - Moved `month_end_from_yyyymm` into `scripts/tushare/common.py`.
  - Removed the duplicate implementation from `scripts/tushare/audit.py`.
- Base audit then found five newly listed stock codes missing from per-`ts_code` long fundamental tables.
  - Ran skip-existing backfill for `dividend`, `fina_audit`, and `fina_mainbz_vip`.
  - Added default `fundamental_code_backfill` in periodic updates for those three datasets; it fills new stock-code partitions without force-refreshing all existing per-code files.

Successful update commands:
- Daily/base/event/fundamental partial run log:
  - `logs/tushare_update_all_20260528_retry2.log`
  - Daily EOD, daily event/flow, periodic reference, and periodic fundamental completed.
  - The run stopped at macro before the helper fix.
- Context/text continuation:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --mode periodic --periodic-tiers macro global text_evidence --end-date 20260528 --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_update_context_text_20260528.log 2>&1`
  - Result: status ok for macro, global, and text_evidence continuation.
- Fundamental per-code backfill:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier fundamental --raw-dir data/raw --start-date 20200101 --end-date 20260528 --datasets dividend fina_audit fina_mainbz_vip --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_download_fundamental_code_backfill_20260528.log 2>&1`
  - Result: each of the three datasets skipped 5844 existing code partitions and wrote 5 new code partitions.

Important update outputs:
- Daily EOD for the recent window is present through `20260528`; the retry skipped the already written 9 recent daily partitions.
- `margin`, `margin_detail`, `moneyflow`, and `block_trade` recent trade-date partitions are present through `20260528`.
- Recent periodic fundamental partitions were refreshed for `income_vip`, `balancesheet_vip`, `cashflow_vip`, `fina_indicator_vip`, `forecast_vip`, `express_vip`, and `disclosure_date`.
- Macro/global context was refreshed through `20260528`; sparse zero-row returns for current `cn_gdp`, `libor`, and `hibor` are source/current-period sparsity rather than script failure.
- Text evidence was refreshed through `20260528` for the current recent window.
- Full-market intraday by-date data and `share_float_complete` were not refreshed; they remain explicit opt-ins because of API volume and cap-risk semantics.

Audit commands:
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py base --raw-dir data/raw --start-date 20200101 --bak-start-date 20200101 --end-date 20260528 --fundamental-start-date 20200101 --fundamental-end-date 20260528 --include-limit-list > logs/tushare_audit_base_20260528_rerun.log 2>&1`
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --raw-dir data/raw --start-date 20200101 --end-date 20260528 > logs/tushare_audit_event_flow_20260528.log 2>&1`
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py macro --raw-dir data/raw --start-date 20200101 --end-date 20260528 > logs/tushare_audit_macro_20260528.log 2>&1`
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py base --raw-dir data/raw --start-date 20200101 --bak-start-date 20200101 --end-date 20260528 --fundamental-start-date 20200101 --fundamental-end-date 20260528 --include-limit-list --include-text --text-start-date 20260428 --text-end-date 20260528 > logs/tushare_audit_text_20260528.log 2>&1`

Audit results:
- `results/data_quality/base_research_status.json`: warning, 0 errors, 16 warnings.
- `results/data_quality/event_flow_status.json`: warning, 0 errors, 7 warnings.
- `results/data_quality/macro_context_status.json`: warning, 0 errors, 2 warnings.
- `results/data_quality/text_evidence_status.json`: warning, 0 errors, 21 warnings.
- `results/data_quality/intraday_minutes_status.json`: unchanged current by-date status, ok, 0 errors, 0 warnings.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare scripts/hl.py`
- `env PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
- `git diff --check`

Verification result:
- Compile passed.
- Full unit discovery passed with 84 tests OK when `PYTHONPATH=src` was set.
- A first unit-test attempt without `PYTHONPATH=src` failed to import `hl_trader`; that was an invocation issue, not a code failure.
- `git diff --check` passed.

Conclusion:
- The current TuShare update path can update daily/periodic research data to `20260528` without raw-data errors.
- Remaining status warnings are known semantic/source risks, not missing required files from this run.
- Future daily `update --mode all` avoids repeated static reference pulls and backfills new per-code fundamental partitions by default.

## 2026-05-29 - TuShare Daily Update Default Expansion

Task:
- Make daily TuShare update include full-market by-date minute refresh and `share_float_complete` by default.
- Preserve an explicit lightweight mode for cases where the operator intentionally skips heavyweight daily updates.

Context:
- User clarified that full-market minute data and `share_float_complete` also need daily refresh.
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Resource checks were run before and after verification; no GPU workload was launched.

Code changes:
- Changed `scripts/tushare/download.py update` defaults:
  - `--include-intraday` now defaults to true.
  - `--include-share-float-complete` now defaults to true.
  - `--rescue-ann-limit-hits` now defaults to true in the update path.
  - Added BooleanOptionalAction reverse flags:
    - `--no-include-intraday`
    - `--no-include-share-float-complete`
    - `--no-rescue-ann-limit-hits`
- Added `update_share_float_complete_data` so both `update --mode periodic/all` and `update --mode daily` can refresh `share_float_complete`.
- Split the `share_float_complete` refresh window from the union rebuild window:
  - Recent raw download window uses the update lookback window.
  - Union rebuild scans the retained full historical range by default: `ann_date=20100101-<end_date>` and `float_date=20200101-<end_date>`.
  - This avoids overwriting the historical union with only recent-window rows.
- Added optional direct CLI args for manual union rebuild bounds:
  - `--union-ann-start-date`
  - `--union-ann-end-date`
  - `--union-float-start-date`
  - `--union-float-end-date`

Documentation:
- Updated `docs/data_documentation.md` so the daily update section states that `update --mode all` covers daily data, full-market by-date minutes, periodic context, text, event/flow, and `share_float_complete`.
- Documented the lightweight skip flags and the full-history union rebuild rule.

Verification commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare scripts/hl.py`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download-share-float-complete --help`
- `env PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
- `git diff --check`

Verification result:
- Compile passed.
- CLI help shows the new default-on BooleanOptionalAction flags.
- Full unit discovery passed with 84 tests OK.
- `git diff --check` passed.

Conclusion:
- The default daily update contract now includes full-market by-date minute refresh and `share_float_complete`.
- A real full-market minute/share-float update was not launched in this change; the implementation and docs were updated and verified.

## 2026-05-29 - TuShare Default Update Launch And Bug Fixes

Task:
- Start the real default TuShare update and check whether the new default path has runtime bugs.
- The requested default path includes daily/periodic refresh, full-market by-date minute refresh, and `share_float_complete`.

Environment and resource checks:
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Used the `stock` Python environment at `~/miniconda3/envs/stock`.
- Checked RAM/GPU before and after the data/audit work.
- Final observed RAM stayed safe at about 395Gi available; no GPU workload was launched by these commands.

Initial command:
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --mode all --end-date 20260529 --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_update_all_20260529_default.log 2>&1`
- The run was stopped after it exposed update-path bugs.

Issues found:
- Current-day source readiness:
  - `daily`, `daily_basic`, `margin`, `margin_detail`, and `moneyflow` returned 0 rows for `20260529`.
  - The original implementation wrote those zero-row required trade-date files into active raw data.
- Existing intraday by-date validation was too strict:
  - Existing `trade_date=20260522` had 1,330,079 rows and 5,519 codes, but missed 3 daily-universe codes.
  - The updater treated that as stale and began a full-market re-download even though the file was structurally usable.
- `share_float_complete` union rebuild was not archive-aware:
  - Historical `share_float` process dirs had been moved to `archive/data_raw/...`.
  - The update rebuilt the union from active recent-window files only, temporarily reducing the union from about 12.736M rows to 148,670 rows.

Code fixes:
- `scripts/tushare/download.py`
  - `download_trade_date_dataset` now skips active writes when a required trade-date query returns 0 rows, printing `returned zero rows; skipped_write`.
  - Existing nonzero required trade-date partitions are skipped; existing zero-row required partitions are treated as stale.
  - `download_event_trade_date_dataset` now applies the same required zero-row skip/write behavior for event-flow trade-date datasets.
  - `update_intraday_by_date` now supports `--existing-allow-missing-codes`, default 50, so small already-retained historical universe gaps do not trigger unnecessary full re-downloads.
  - If the expected intraday code universe is empty, the date is skipped instead of raising.
  - `share_float_complete` union rebuild now scans both `data/raw` and retained `archive/data_raw/*` process roots.
- `scripts/tushare/common.py`
  - `intraday_expected_codes_for_day` returns an empty set when the daily universe partition is missing or zero-row.
  - Shared intraday parser args now expose `--existing-allow-missing-codes`.
- `docs/data_documentation.md`
  - Documented required zero-row skipped writes, existing-minute tolerance, and archive-aware `share_float_complete` union rebuild.

Validation after fixes:
- Compile:
  - `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare scripts/hl.py`
- CLI/help checks:
  - `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help`
  - `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update-intraday-by-date --help`
- Targeted intraday skip probe:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update-intraday-by-date --raw-dir data/raw --start-date 20260522 --end-date 20260522 --expected-codes-source daily --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_intraday_skip_probe_20260522.log 2>&1`
  - Result: `written=0 skipped=1`.
- Full unit tests:
  - `env PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
  - Result: 84 tests OK.
- Whitespace check:
  - `git diff --check`
  - Result: passed.

Successful retry command:
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --mode all --end-date 20260529 --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_update_all_20260529_default_retry.log 2>&1`

Successful retry result:
- `daily` and `daily_basic` for `20260529` returned zero rows and were skipped without active writes.
- `margin`, `margin_detail`, and `moneyflow` for `20260529` returned zero rows and were skipped without active writes.
- Prior stale event-flow partitions were refreshed where data existed:
  - `margin` wrote 3 rows.
  - `margin_detail` wrote 4,365 rows.
- Full-market by-date minute update:
  - `20260526`: wrote 1,326,464 rows, missing_codes=0.
  - `20260527`: wrote 1,326,946 rows, missing_codes=0.
  - `20260528`: wrote 1,326,946 rows, missing_codes=0.
  - `20260529`: skipped because the expected daily universe was empty.
  - Summary: 9 dates checked, 3 written, 6 skipped, 3,980,356 minute rows written.
- Periodic/fundamental/macro/global/text refresh completed.

Share-float union repair:
- Rebuilt union without API downloads:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download-share-float-complete --raw-dir data/raw --ann-start-date 20260129 --ann-end-date 20260529 --float-start-date 20260129 --float-end-date 20260529 --skip-ann-date --write-union --union-ann-start-date 20100101 --union-ann-end-date 20260529 --union-float-start-date 20200101 --union-float-end-date 20260529 --union-output data/raw/share_float_complete/share_float_complete.parquet --min-interval-seconds 0.22 --timeout-seconds 120 > logs/share_float_union_rebuild_20260529.log 2>&1`
  - Result: `union_rows=12736101`.
- The repaired `data/raw/share_float_complete/share_float_complete.parquet` has 12,736,101 rows.
- This restored the retained historical union after the temporary recent-window truncation.

Cleanup:
- Moved stale zero-row probe files from the first stopped run to `archive/data_raw/incomplete_20260529_update_probe/`.
- Active raw no longer contains zero-row required trade-date files for:
  - `daily/trade_date=20260529`
  - `daily_basic/trade_date=20260529`
  - `margin/trade_date=20260529`
  - `margin_detail/trade_date=20260529`
  - `moneyflow/trade_date=20260529`

Audit commands:
- Intraday:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py intraday-by-date --raw-dir data/raw --start-date 20200101 --end-date 20260528 --min-rows-per-day 1 > logs/tushare_audit_intraday_by_date_20260528_after_update.log 2>&1`
- Event/flow:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --raw-dir data/raw --start-date 20200101 --end-date 20260528 > logs/tushare_audit_event_flow_20260528_after_update.log 2>&1`

Audit results:
- `results/data_quality/intraday_minutes_status.json`
  - Status: ok.
  - Errors: 0.
  - Warnings: 0.
  - Audited through `20260528`.
- `results/data_quality/event_flow_status.json`
  - Status: warning.
  - Errors: 0.
  - Warnings: 5.
  - `share_float_complete_union` has 12,736,101 rows and matching sidecar row count.
  - Remaining warnings are source-cap/semantic risks, not broad missing active files.

Final conclusion:
- The default update path now runs end-to-end.
- `20260529` was correctly treated as a not-yet-ready source date for required daily trade-date datasets.
- Full-market by-date minute data is current through `20260528`.
- `share_float_complete` union is restored and archive-aware.

## 2026-05-29 - TuShare Editable Audit Hardening

Task:
- Perform an independent editable audit of the current TuShare download/update/audit scripts.
- Confirm the default download/update paths avoid truncation, zero-row pollution, and invisible gaps without launching large downloads.

Environment and resource checks:
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Used the `stock` Python environment at `~/miniconda3/envs/stock`.
- Checked `nvidia-smi` and `free -h` before and after validation commands.
- RAM stayed safe at about 396Gi available. Existing GPUs were already heavily occupied by unrelated processes; no GPU workload was launched.

Code changes:
- `scripts/tushare/download.py`
  - Added `selected_event_flow_download_datasets`: generic `download --tier event_flow` now defaults to non-`share_float` datasets and rejects explicit `share_float`, requiring `download-share-float-complete` for the ann_date rescue/union path.
  - Added `share_float_complete` union shrink protection: if a rebuild would produce fewer rows than the existing union, it raises unless `--allow-union-shrink` is explicit.
  - Added `--allow-union-shrink` to both `update` and `download-share-float-complete`.
  - Added a hard guard in `update-intraday-by-date`: if the expected code universe is nonempty, a zero-row by-date file is not written even when `--allow-missing-codes` is large.
- `scripts/tushare/audit.py`
  - Daily trade-date and fundamental exact-limit partitions now become warnings when they lack a pagination probe.
  - `audit intraday-by-date` now errors on zero-row final by-date files and reports orphan sidecars.
  - Stock-year minute audit now reports exact common-limit partitions without pagination probes.
- `tests/unit/test_tushare_download_update_guards.py`
  - Added focused tests for zero-row intraday write refusal, share_float union shrink refusal, generic event_flow/share_float rejection, exact-limit audit warning, and zero-row by-date audit error.
- `docs/data_documentation.md`
  - Documented the current `share_float` dedicated path, union shrink guard, and final by-date zero-row error behavior.

Validation commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare`
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards tests.unit.test_tushare_intraday_by_date`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download-share-float-complete --help`
- `git diff --check`

Validation result:
- Compile passed.
- Targeted TuShare tests passed: 6 tests OK.
- Full unit discovery passed: 89 tests OK.
- CLI help shows `--allow-union-shrink` on update and share-float complete commands.
- `git diff --check` passed.

Conclusion:
- No large download or live TuShare API call was started.
- The remaining high-severity update risks found in this pass were addressed with small guards and tests.
- Residual risks remain source-semantic rather than broad script gaps: `share_float` exact-6000 source caps still cannot be mathematically proven complete, and current-day data may still be source-not-ready but is now visible as skipped/missing rather than silently written as active zero-row data.

## 2026-05-29 - TuShare Update Entrypoint Simplification

Task:
- Simplify the daily TuShare update surface.
- Replace the split `update --mode daily|periodic|all` workflow with one daily command that fills all retained data domains from a chosen start date to the current/end date.

Environment and resource checks:
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Used the `stock` Python environment at `~/miniconda3/envs/stock`.
- Checked `nvidia-smi` and `free -h` before and after validation.
- RAM stayed safe at about 401-405Gi available. No GPU workload, live TuShare API call, or large download was launched.

Design decision:
- The new daily entrypoint is:
  - `scripts/tushare/download.py update --start-date <YYYYMMDD> --end-date <YYYYMMDD>`
- `--end-date` defaults to the current date.
- `--start-date` is required so the operator explicitly chooses the current research/live-data lower bound.
- The command runs retained domains in one sequence:
  - reference
  - daily
  - fundamental
  - macro
  - global
  - event_flow
  - intraday_by_date
  - share_float_complete
  - text_evidence
- Default behavior is skip-existing:
  - Existing complete partitions are skipped.
  - Missing partitions are downloaded.
  - `--force` is the only way to intentionally rewrite existing complete partitions.
- Range partitions such as current month text/event files or current year macro files are only considered complete if their sidecar `params.start_date/end_date` covers the requested range. This avoids skipping a stale current-month file just because `month=YYYYMM.parquet` already exists.

Code changes:
- `scripts/tushare/common.py`
  - Removed old update-mode and periodic-update constants that are no longer part of the public update contract.
- `scripts/tushare/download.py`
  - Removed `update_daily_data`, `update_periodic_data`, and `date_minus_days`.
  - Added `update_all_dimensions`, called by `update_data`.
  - Simplified `add_update_parser`: removed `--mode`, lookback windows, periodic tiers, and refresh-existing-periodic switches.
  - Added `--start-date` as a required update argument and `--bak-start-date` as an optional `bak_basic` lower bound.
  - Renamed event filter to `--event-datasets`; generic event update excludes `share_float`, which remains dedicated to `download-share-float-complete`.
  - Added `sidecar_params`, `existing_partition_covers_request`, and `should_skip_existing_partition`.
  - Applied sidecar coverage checks to range/year/month functions where a path can exist while the covered end date is stale.
- `tests/unit/test_tushare_download_update_guards.py`
  - Added a guard test proving a stale `month=YYYYMM` sidecar is not treated as complete for a later requested end date.
- `docs/data_documentation.md`
  - Updated the daily update section to document the single `update --start-date` workflow and current skip/force semantics.

Validation commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards tests.unit.test_tushare_intraday_by_date`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
- `~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download-share-float-complete --help`
- `git diff --check`

Validation result:
- Compile passed.
- `update --help` now shows a single update command with required `--start-date` and no `--mode`.
- Targeted TuShare tests passed: 7 tests OK.
- Full unit discovery passed: 90 tests OK.
- `download-share-float-complete --help` remained valid.
- `git diff --check` passed.

Conclusion:
- The daily update surface is now one command instead of three modes.
- Closed existing partitions skip by default, missing partitions are filled, and stale range sidecars are refreshed to avoid date gaps in current month/year aggregate partitions.

## 2026-05-29 - TuShare Update Editable Audit Follow-Up

Task:
- Audit the new single TuShare update entrypoint before a 20260529 window retry.
- Check for old `--mode`/lookback/periodic residues, domain coverage, skip-existing behavior, sidecar coverage across date formats, and risks of broad re-pulls or hidden gaps.

Environment and resource checks:
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Used `~/miniconda3/envs/stock/bin/python`.
- Checked `nvidia-smi` and `free -h` around validation. RAM stayed safe at about 402-403Gi available. Existing GPU processes were present, but this audit launched no GPU workload, live TuShare API call, or large download.

Findings and changes:
- Current code and `docs/data_documentation.md` no longer contain the old update `--mode`, lookback, periodic-tier, or refresh-existing-periodic path.
- `update_all_dimensions` still covers the retained domains in order: reference, daily, fundamental, macro, global, event_flow, intraday_by_date, share_float_complete, and text_evidence.
- Fixed sidecar coverage comparison in `scripts/tushare/download.py` by normalizing `YYYYMMDD`, `YYYYMMDDHHMMSS`, and `YYYY-MM-DD HH:MM:SS` to comparable timestamp bounds. Date-only `end_date` is treated as end-of-day, so a sidecar ending at `20260529000000` no longer covers a full `20260529` request.
- Added sidecar coverage metadata to macro quarter/month range files.
- Changed `cn_schedule` month-loop skip logic so only the current end month requires coverage metadata; closed historical month files with old `m`-only sidecars still skip, avoiding a broad historical re-pull.
- Updated `docs/data_documentation.md` with the current sidecar date-normalization rule.
- Added unit coverage for datetime sidecar bounds and current-month-only macro refresh.

Validation commands:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q /Data/lzp/MacroQuant/scripts/tushare`
- `~/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/scripts/tushare/download.py update --help`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant ~/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src:/Data/lzp/MacroQuant ~/miniconda3/envs/stock/bin/python -m unittest discover -s /Data/lzp/MacroQuant/tests/unit`
- `git -C /Data/lzp/MacroQuant diff --check`
- Residue scan: `rg -n -e "--mode|lookback|periodic|refresh-existing-periodic|periodic-tiers|update_daily_data|update_periodic_data|date_minus_days" scripts/tushare/download.py scripts/tushare/common.py scripts/tushare/audit.py tests/unit/test_tushare_download_update_guards.py docs/data_documentation.md`

Validation result:
- Compile passed.
- `update --help` shows required `--start-date` and no `--mode`.
- Targeted TuShare update guard tests passed: 8 tests OK.
- Full unit discovery passed after using the correct `src` path: 92 tests OK. An earlier discovery attempt with only the repository root on `PYTHONPATH` failed to import `hl_trader`; this was an invocation issue, not a code failure.
- `git diff --check` passed.
- Residue scan returned no matches in current code or `docs/data_documentation.md`.

Conclusion:
- The 20260529 update path is safer to retry: stale datetime sidecars should not hide new text/event windows, current `cn_schedule` month files refresh when needed, and closed month files should not cause a broad historical re-pull.
- Recommend the parent agent continue with a controlled 20260529 update test, using normal resource checks and log capture, rather than `--force`.

## 2026-05-29 - Single-Entrypoint 20260529 Update Test

Task:
- Run the new single TuShare update entrypoint on the `20260529` window.
- Check whether the previously missing current-day data is filled and whether the new skip-existing/sidecar behavior works in practice.

Environment and resource checks:
- Real repository path was confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Used `~/miniconda3/envs/stock/bin/python`.
- Checked `nvidia-smi` and `free -h` before, during, and after the run.
- RAM stayed safe. During share-float union rebuild the lowest observed available RAM was about 391Gi; final available RAM was about 405Gi. No new GPU workload was launched.

SubAgent audit:
- Started and closed GPT-5.5 xhigh SubAgent `Franklin` before the real update test.
- The SubAgent found and fixed two pre-test issues:
  - Sidecar coverage comparison now normalizes date and datetime strings and treats date-only end bounds as end-of-day.
  - `cn_schedule` month-loop refreshes only the current end month by coverage, while closed historical months still skip.
- SubAgent verification passed: compileall, help check, targeted tests, full unit discovery with 92 tests OK, and `git diff --check`.

Update command:
- `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --start-date 20260529 --end-date 20260529 --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_update_20260529_single_entry.log 2>&1`

Update result:
- Process completed with JSON status `ok`.
- Step elapsed times:
  - reference: 0.146s.
  - daily: 0.820s.
  - fundamental: 0.209s.
  - macro: 1.345s.
  - global: 0.005s.
  - event_flow: 0.874s.
  - intraday_by_date: 1602.695s.
  - share_float_complete: 124.402s.
  - text_evidence: 0.008s.

Rows and fill status:
- `data/raw/daily/trade_date=20260529.parquet`: 5,505 rows, newly filled.
- `data/raw/daily_basic/trade_date=20260529.parquet`: 5,506 rows, newly filled.
- `data/raw/adj_factor/trade_date=20260529.parquet`: 5,525 rows, already present and skipped.
- `data/raw/stk_limit/trade_date=20260529.parquet`: 7,628 rows, already present and skipped.
- `data/raw/suspend_d/trade_date=20260529.parquet`: 23 rows, already present and skipped.
- `data/raw/limit_list_d/trade_date=20260529.parquet`: 0 rows, optional zero-row partition already present and skipped.
- `data/raw/moneyflow/trade_date=20260529.parquet`: 5,190 rows, newly filled.
- `data/raw/block_trade/trade_date=20260529.parquet`: 0 rows, sparse zero-row partition already present and skipped.
- `data/raw/stk_mins_1min_by_date/trade_date=20260529.parquet`: 1,326,705 rows, newly filled.
  - Expected codes from daily: 5,505.
  - Missing codes after retry: 0.
  - `09:30` and `15:00` bars present.
  - Duplicate key rows: 0.
- `data/raw/share_float_complete/share_float_complete.parquet`: 12,736,101 rows.
  - Union did not shrink.
  - Sidecar `meta.row_count` matches 12,736,101.
- Text evidence for 20260529 was already present and skipped by sidecar coverage.

Not filled:
- `margin` and `margin_detail` for `20260529` were not filled.
- During the update, both interfaces returned 0 rows and were handled as `skipped_write`, so no active zero-row files were written.
- A targeted retry was run:
  - `PYTHONUNBUFFERED=1 ~/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier event_flow --start-date 20260529 --end-date 20260529 --datasets margin margin_detail --raw-dir data/raw --min-interval-seconds 0.22 --timeout-seconds 120 > logs/tushare_event_margin_retry_20260529.log 2>&1`
  - Result: both interfaces again returned 0 rows and were skipped without active writes.

Audit probes:
- Intraday by-date probe:
  - `~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py intraday-by-date --raw-dir data/raw --start-date 20260529 --end-date 20260529 --expected-codes-source daily --min-rows-per-day 1 --output results/data_quality/process/intraday_20260529_update_probe.json`
  - Result: status ok, 0 errors, 0 warnings.
- Event-flow probe:
  - `~/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --raw-dir data/raw --start-date 20260529 --end-date 20260529 --output results/data_quality/process/event_flow_20260529_update_probe.json`
  - Result: status error, 2 errors, 5 warnings.
  - The 2 errors are exactly the missing `margin/trade_date=20260529` and `margin_detail/trade_date=20260529` partitions.
  - Warnings are existing semantic/source warnings for duplicate event keys and share-float source-cap risk.
- Temporary probe JSON files were archived after review:
  - `archive/data_quality/20260529_single_update_probe/intraday_20260529_update_probe.json`
  - `archive/data_quality/20260529_single_update_probe/event_flow_20260529_update_probe.json`

Post-run verification:
- `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/stock/bin/python -m compileall -q scripts/tushare`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards tests.unit.test_tushare_intraday_by_date`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
- `git diff --check`

Post-run verification result:
- Compile passed.
- Targeted TuShare tests passed: 9 tests OK.
- Full unit discovery passed: 92 tests OK.
- `git diff --check` passed.
- No `scripts/tushare/download.py update` process remained running.

Conclusion:
- The simplified single `update --start-date/--end-date` entrypoint works on the 20260529 test window.
- It filled the previously missing daily, daily_basic, moneyflow, and full-market minute data without force-overwriting existing complete partitions.
- The only known 20260529 gap is `margin` and `margin_detail`, where TuShare still returns 0 rows; the script now makes that gap visible by not writing active zero-row files.

## 2026-05-29 TuShare cron maintenance

Task:
- Search official TuShare documentation for update-time semantics across the retained MacroQuant interfaces.
- Maintain a cron schedule that updates all retained data domains without overwriting existing non-MacroQuant cron jobs.

Resource checks:
- Pre-run `nvidia-smi` showed existing GPU jobs only; no GPU workload was started.
- Pre-run `free -h` showed about 402Gi available RAM.

Official documentation checked:
- Used official TuShare pages and permission/update tables for daily, adj_factor, daily_basic, stk_limit, historical minutes, margin/margin_detail, moneyflow, block_trade, financial statements, text evidence, macro rates, and monetary policy.
- Not every official page provides an exact time. The retained catalog records exact times where published and marks the rest as event-driven, real-time, regular, monthly, quarterly, or official time unspecified.

Files changed:
- `configs/tushare_update_schedule.json`
  - New per-interface catalog with `dataset`, `api`, data domain, update frequency, official update-time text, cron policy, and official doc URL.
  - Schedule jobs:
    - `cn_evening_full`: same-day full update, Beijing time evening.
    - `cn_next_morning_backfill`: previous-day backfill, Beijing time next morning.
- `scripts/tushare/cron_update.py`
  - New cron-safe runner.
  - Computes target date from job offset in `Asia/Shanghai`.
  - Calls `scripts/tushare/download.py update --start-date <configured> --end-date <computed>`.
  - Uses per-job lock files under `logs/`.
  - Skips duplicate successful job/date runs via `logs/tushare_cron_state.json`.
  - Writes `nvidia-smi`, `free -h`, command output, return code, and finish time to per-run logs.
- `ops/cron/tushare_update.cron`
  - New cron template for the managed MacroQuant block.
- `ops/cron/install_tushare_cron.py`
  - New installer that preserves non-MacroQuant crontab entries and only replaces the managed BEGIN/END block.
- `scripts/tushare/common.py`
  - Filled missing official doc refs for reference/daily/fundamental interfaces.
  - Corrected `repo_daily` doc ref to `doc_id=256`.
- `docs/data_documentation.md`
  - Added the scheduled-update section and operational log/state paths.
- `LOGBOOK.md`
  - Added concise result.

Commands:
- `crontab -l`
- `PYTHONPATH=/Data/lzp/MacroQuant /home/lzp/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/scripts/tushare/cron_update.py --job cn_evening_full --dry-run`
- `PYTHONPATH=/Data/lzp/MacroQuant /home/lzp/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/scripts/tushare/cron_update.py --job cn_next_morning_backfill --dry-run`
- Managed crontab refresh preserving the existing ChouQuant block.
- `/home/lzp/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/ops/cron/install_tushare_cron.py`
- `/home/lzp/miniconda3/envs/stock/bin/python -m json.tool /Data/lzp/MacroQuant/configs/tushare_update_schedule.json`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q /Data/lzp/MacroQuant/scripts/tushare`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s /Data/lzp/MacroQuant/tests/unit`
- `git -C /Data/lzp/MacroQuant diff --check`

Cron installed:
- Existing ChouQuant cron was preserved.
- Added managed block:
  - `35 23 * * * ... cron_update.py --job cn_evening_full`
  - `45 9 * * * ... cron_update.py --job cn_next_morning_backfill`
- Both jobs use `CRON_TZ=Asia/Shanghai`.

Verification result:
- JSON validation passed.
- Cron dry-runs computed:
  - evening job command with `--end-date 20260529`;
  - next-morning job command with `--end-date 20260528`.
- Compileall passed.
- Full unit discovery passed: 92 tests OK.
- `git diff --check` passed.
- `crontab -l` shows the managed MacroQuant block and the existing ChouQuant block.
- The living data document now instructs using the installer rather than `crontab ops/cron/tushare_update.cron`, because the latter would replace the whole crontab.

Conclusion:
- Scheduled all retained TuShare domains through the single `update --start-date/--end-date` entrypoint.
- The schedule intentionally runs same-day late evening and previous-day next morning because official update times span pre-open, post-close, evening, next-morning, and event-driven windows.
- No live update was run by the new cron runner during this task; only dry-runs and crontab installation were performed.

## 2026-05-29 TuShare cron SubAgent audit follow-up

Task:
- Open a high-capability SubAgent to independently audit the TuShare cron/update-time changes, apply necessary fixes in the main thread, and close all SubAgents.

SubAgent:
- Opened GPT-5.5 xhigh SubAgent `Banach`.
- Closed `Banach` after receiving the completed audit result.

SubAgent findings:
- Medium: `scripts/tushare/cron_update.py` used per-job locks, so `cn_evening_full` and `cn_next_morning_backfill` could overlap and write the same raw data/state file concurrently.
- Low: `ops/cron/tushare_update.cron` redirected to `logs/tushare_cron_dispatch.log` before ensuring ignored `logs/` exists.
- Fixed by SubAgent: `configs/tushare_update_schedule.json` `news` doc URL should use `https://www.tushare.pro/document/41?doc_id=143`.

Main-thread fixes:
- `scripts/tushare/cron_update.py`
  - `acquire_lock()` now accepts a generic lock name.
  - Scheduled runs use one global `.runtime/tushare/locks/tushare_update.lock`, preventing evening and backfill jobs from running concurrently against the same `data/raw` and state file.
- `ops/cron/tushare_update.cron`
  - Both cron rows now run `mkdir -p logs` before redirecting output.
- `docs/data_documentation.md`
  - Cron examples now include `mkdir -p logs`.
  - Documented the global lock behavior.
- `LOGBOOK.md`
  - Added concise audit follow-up entry.

Commands:
- `rg` and `sed` inspection of cron/config/runner files.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q /Data/lzp/MacroQuant/scripts/tushare /Data/lzp/MacroQuant/ops/cron`
- `/home/lzp/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/ops/cron/install_tushare_cron.py --dry-run`
- `/home/lzp/miniconda3/envs/stock/bin/python /Data/lzp/MacroQuant/ops/cron/install_tushare_cron.py`
- `crontab -l`

Conclusion:
- The SubAgent audit found two valid operational reliability issues.
- Both were fixed and the installed crontab was refreshed with the safer lines.

## 2026-05-29 TuShare cron runtime path cleanup

Task:
- Keep `ops/` as the tracked operations-asset directory, but move runtime lock/state files out of `logs/`.

Files changed:
- `.gitignore`
  - Added `/.runtime/`.
- `scripts/tushare/cron_update.py`
  - Added `RUNTIME_ROOT = Path(".runtime/tushare")`.
  - Moved cron state from `logs/tushare_cron_state.json` to `.runtime/tushare/cron_state.json`.
  - Moved global lock from `logs/tushare_cron_global_update.lock` to `.runtime/tushare/locks/tushare_update.lock`.
  - Kept dispatch and per-run logs under `logs/`.
- `docs/data_documentation.md`
  - Updated the scheduled-update section to describe `.runtime/tushare/` lock/state paths.
- `LOGBOOK.md`
  - Added concise result.

Conclusion:
- `ops/` now contains only tracked cron operations tooling and templates.
- `.runtime/` is the ignored machine-local state directory.
- `logs/` is reserved for logs.

## 2026-05-29 TuShare pre-open backfill adjustment

Task:
- Move the morning backfill early enough for downstream Agent decisions before 09:25, without running heavyweight full-domain updates during the pre-open window.

Files changed:
- `configs/tushare_update_schedule.json`
  - `cn_next_morning_backfill` now has `operation=download_event_flow`.
  - It targets the previous calendar day and passes `--datasets margin margin_detail`.
- `scripts/tushare/cron_update.py`
  - Added job operation dispatch.
  - Default `update` jobs still call `scripts/tushare/download.py update`.
  - `download_event_flow` jobs call `scripts/tushare/download.py download --tier event_flow`.
  - If no explicit start date is provided for an event-flow download job, the start date is set to the computed target end date.
- `ops/cron/tushare_update.cron`
  - Morning job moved from 09:45 to 09:10 Beijing time.
  - Comment now states it only refreshes `margin` and `margin_detail`.
- `docs/data_documentation.md`
  - Cron schedule and explanation updated.
- `LOGBOOK.md`
  - Added concise result.

Rationale:
- TuShare permissions table states both `margin` and `margin_detail` update daily at 09:00.
- Running full update at 09:10 could be too heavy because it may touch minute files, share-float union, macro/text, and filesystem scans.
- A two-interface event-flow backfill should leave time for quick audit, feature freeze, and Agent decision generation before 09:25.

Note:
- Shell verification was blocked in this turn by a local zsh/GLIBC startup error from the command wrapper, so the intended next verification is compileall, JSON validation, two cron dry-runs, crontab refresh via `ops/cron/install_tushare_cron.py`, `crontab -l`, unit tests, and `git diff --check` once shell execution is available again.

## 2026-05-29 TuShare pre-open margin retry split

Task:
- Make the pre-open margin backfill more robust before the 09:25 decision freeze.

Files changed:
- `configs/tushare_update_schedule.json`
  - Replaced `cn_next_morning_backfill` with two light event-flow jobs:
    - `cn_preopen_margin_backfill_0905`
    - `cn_preopen_margin_retry_0915`
  - Both target the previous calendar day and pass `--datasets margin margin_detail`.
- `ops/cron/tushare_update.cron`
  - Replaced the single 09:10 row with 09:05 and 09:15 rows.
- `docs/data_documentation.md`
  - Updated cron examples and explained why separate job names are used.
- `LOGBOOK.md`
  - Added concise result.

Rationale:
- TuShare documents `margin` and `margin_detail` as daily 09:00 updates, but practical source lag can still produce empty responses shortly after 09:00.
- Separate job names avoid the runtime state from making 09:15 skip merely because 09:05 returned process status ok.
- If 09:05 successfully writes the two partitions, 09:15 still starts but should skip quickly through the downloader's existing partition/sidecar checks.

## 2026-05-30 Data documentation order and board-trading scope

Task:
- Clarify whether initial data download should be documented before data update.
- Clarify whether extra 打板 data needs to be downloaded now.

Files changed:
- `docs/data_documentation.md`
  - Reordered the top data section to put initial download and compaction before daily update and cron operations.
  - Added a 打板策略数据准备 section.
  - Updated `limit_list_d` from optional wording to default retained board-label/event data.
- `LOGBOOK.md`
  - Added concise result.

Data checks:
- `limit_list_d` has active daily partitions from `20200102` through `20260529`.
- `stk_limit` has active daily partitions from `20100104` through `20260529`.
- `stk_mins_1min_by_date` has active by-date minute partitions for the 2020+ research window.

Conclusion:
- The living data document should present download/bootstrap first, then incremental update/cron. That order is now reflected.
- No broad new TuShare historical download is needed solely for future 打板 strategy work. The current retained raw set supports labels, rough minute replay, and daily/next-day features.
- True intraday 打板 execution must be implemented in the Environment layer using PIT minute data and limit prices; daily `limit_list_d` fields such as first/last limit time, open times, and seal amount are post-event summaries and must not be used for decisions before they are observable.

## 2026-05-30 TuShare board-trading source scan

Task:
- Scan TuShare's current documented data source surface for datasets that are more directly useful for 打板 strategies.

Sources checked:
- TuShare official documentation search and MCP metadata for 打板专题数据, including 开盘啦、同花顺、东方财富、龙虎榜、游资、集合竞价、板块和热榜 interfaces.

Files changed:
- `docs/data_documentation.md`
  - Updated the document date.
  - Expanded `打板策略数据准备` from current retained raw coverage to a candidate supplement list.
  - Recorded PIT boundaries for post-close board lists, next-morning 开盘啦 data, intraday hot lists, and auction data.
- `LOGBOOK.md`
  - Added concise result.

Conclusion:
- The current retained raw data remains sufficient for first-stage 日终标签 and minute replay.
- For a stronger 打板 stack, the first implementation batch should be small-window validation and later downloader support for `kpl_list`, `limit_step`, `limit_cpt_list`, `limit_list_ths`, `top_list/top_inst`, `hm_list/hm_detail`, `ths_hot/dc_hot`, and optional `stk_auction`.
- Topic/sector taxonomies should be added selectively. Choose one primary taxonomy first, then use other sources for cross-source validation to avoid feature drift and redundant maintenance.

## 2026-05-30 Board-trading data domain implementation

Task:
- Supplement the selected TuShare 打板专题 datasets and decide whether they should be a separate data dimension.

Decision:
- Added a dedicated `board_trading` data domain. These datasets are structurally closer to event/evidence than to daily market data, but their PIT timestamps, business keys, and update timing are specific to limit-up/Dragon-Tiger/hot-list workflows, so keeping them separate avoids overloading `event_flow` or `text_evidence`.

Files changed:
- `scripts/tushare/common.py`
  - Added `BOARD_TRADING_STATUS_PATH`, `BOARD_TRADING_DATASETS`, specs, default hot-list/tag selectors, doc refs, PIT augmentation, and unit/PIT helper behavior.
- `scripts/tushare/download.py`
  - Added `download --tier board_trading`.
  - Added trade-date, tag-partition, market-partition, market/hot-type partition, and static reference download strategies.
  - Wired board-trading into daily `update` by default, with `--no-include-board-trading` for lightweight runs.
- `scripts/tushare/audit.py`
  - Added `audit.py board-trading`.
  - Added expected path generation, partition/sidecar checks, business-key checks, `available_at` parsing, and board unit/PIT rules.
- `configs/tushare_update_schedule.json`
  - Added update metadata for `kpl_list`, `limit_step`, `limit_cpt_list`, `top_list`, `top_inst`, `hm_list`, `hm_detail`, `ths_hot`, and `dc_hot`.
- `docs/data_documentation.md`
  - Promoted 打板专题数据 from candidate note to an active sixth data domain.
  - Added download/update/audit commands, status file, PIT rules, and audit logic.
- `tests/unit/test_tushare_download_update_guards.py`
  - Added a board-trading download/audit guard test.
- `LOGBOOK.md`
  - Added concise result.

Real data window tests:
- Downloaded all default `board_trading` datasets for `20260529`.
  - `kpl_list`: 5 tag partitions, 488 rows.
  - `limit_step`: 16 rows.
  - `limit_cpt_list`: 20 rows.
  - `top_list`: 90 rows.
  - `top_inst`: 940 rows.
  - `hm_list`: 110 rows.
  - `hm_detail`: 301 rows.
  - `ths_hot`: 3 market partitions, 2,380 rows.
  - `dc_hot`: 2 partitions, 3,398 rows.
- Downloaded an early-window probe for `20200102`.
  - `kpl_list`, `top_list`, and `top_inst` returned rows.
  - `limit_step`, `limit_cpt_list`, `ths_hot`, and `dc_hot` returned zero rows for that old date.
  - `hm_detail` starts from the configured `20220801` boundary and had no expected 20200102 partition.
- `results/data_quality/board_trading_status.json` was written for `20260529` and is `ok` with 0 errors and 0 warnings.
- The temporary 20200102 process audit was moved to `archive/data_quality/20260530_board_trading_window/`.

Verification:
- Compileall for `scripts/` and `tests/` passed.
- Targeted TuShare unit test module passed with 9 tests.
- Real window audit passed after refining `top_list`/`top_inst` business keys to avoid false duplicate warnings.
- JSON schedule validation passed.
- GPU/RAM checks were recorded before and after live TuShare data runs; resource usage stayed safe.

Backfill note:
- Full `20200101-20260529` board-trading backfill is estimated at 22,627 API tasks with current defaults. It was not launched in this turn because the implementation and two-window validation were the main change; the new tier can be backfilled with:

```bash
PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier board_trading --start-date 20200101 --end-date 20260529 --min-interval-seconds 0.22 --timeout-seconds 90
```

## 2026-05-30 THS limit-list and auction alignment follow-up

Task:
- Add the 2023-start 同花顺打板数据 to the retained board-trading domain.
- Resolve the known Shenzhen 09:30 minute auction mismatch against TuShare `stk_auction`.
- Check whether nearby raw data has similar unit or source-alignment issues.

Files changed:
- `scripts/tushare/common.py`
  - Added `limit_list_ths` to `BOARD_TRADING_DATASETS`, official doc ref, fields, start date `20231101`, `trade_date_by_limit_type` strategy, default pools, and 16:00 PIT availability.
- `scripts/tushare/download.py`
  - Added `download_board_limit_list_ths` and wired the strategy into `download --tier board_trading`.
- `scripts/tushare/audit.py`
  - Added expected paths, PIT/unit rules, scope reporting for `limit_list_ths`.
  - Added `auction-alignment`, a process-only audit comparing local 09:30 minute bars with TuShare `stk_auction` and full-day minute sums with local `daily`.
- `src/hl_trader/environment/features/auction.py`
  - Added PIT-layer correction utility that keeps raw `vol/amount` unchanged and emits `vol_pit/amount_pit`.
  - Current factors: `00*.SZ = 0.76`, `30*.SZ = 0.58`, other buckets `1.0`.
- `src/hl_trader/environment/features/__init__.py`
  - Exported the auction correction helper.
- `configs/tushare_update_schedule.json`
  - Added `limit_list_ths` update metadata.
- `docs/data_documentation.md`
  - Documented `limit_list_ths`, its 20231101 boundary, audit paths, and historical auction correction policy.
- `docs/environment_design.md`
  - Documented Environment ownership of historical auction correction.
- `tests/unit/test_auction_correction.py`
  - Added bucket and correction tests.
- `tests/unit/test_tushare_download_update_guards.py`
  - Extended board-trading download/audit guard coverage to `limit_list_ths`.

Key commands and results:
- MCP checked current TuShare metadata:
  - `limit_list_ths`: history from `20231101`, increment around 16:00, pools `涨停池/连扳池/冲刺涨停/炸板池/跌停池`.
  - `stk_auction`: current-day opening auction data, available around 09:25-09:29.
- Smoke download:
  - `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier board_trading --datasets limit_list_ths --start-date 20260529 --end-date 20260529 --min-interval-seconds 0.25 --timeout-seconds 120`
  - Result: 5 partitions, 224 rows.
- Current board-trading status:
  - `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py board-trading --start-date 20260529 --end-date 20260529`
  - Result: `results/data_quality/board_trading_status.json` ok, 0 errors, 0 warnings.
- Full `limit_list_ths` backfill:
  - `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier board_trading --datasets limit_list_ths --start-date 20231101 --end-date 20260529 --min-interval-seconds 0.25 --timeout-seconds 120`
  - Result: 3,115 tasks, 5 skipped existing partitions, 3,110 written partitions, 114,428 rows.
- Full `limit_list_ths`专项 audit:
  - `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py board-trading --start-date 20231101 --end-date 20260529 --datasets limit_list_ths --output results/data_quality/process/limit_list_ths_20231101_20260529_status.json`
  - Result: ok, 0 errors, 0 warnings.
  - Archived to `archive/data_quality/20260530_limit_list_ths/limit_list_ths_20231101_20260529_status.json`.
- Auction alignment audit:
  - `PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py auction-alignment --raw-dir data/raw --start-date 20260201 --end-date 20260529 --max-trade-dates 8 --min-interval-seconds 0.25 --timeout-seconds 120`
  - Result: ok, 0 errors, 0 warnings.
  - Archived to `archive/data_quality/20260530_auction_alignment/auction_alignment_status.json`.

Auction alignment conclusions:
- The issue is specific to historical 09:30 auction proxy bars from minute data when compared with live-style `stk_auction`.
- On sampled recent dates, `20260529` bucket medians were representative:
  - `sz_main_00`: minute/stk_auction vol median about `1.325`; after factor `0.76`, about `1.007`.
  - `sz_gem_30`: minute/stk_auction vol median about `1.723`; after factor `0.58`, about `0.999`.
  - `sh_main_60` and `sh_star_68`: around `1.0`; no correction.
- Full-day minute sums against local `daily` remain aligned:
  - `sum(stk_mins.vol) / daily.vol ~= 100`, matching shares vs hands.
  - `sum(stk_mins.amount) / daily.amount ~= 1000`, matching CNY vs thousand CNY.
- No broad full-day daily/minute unit mismatch was found in the sampled cross-check.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_auction_correction tests.unit.test_tushare_download_update_guards`
  - Passed: 11 tests.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
  - Passed: 95 tests.
- `/home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json`
  - Passed.
- `git diff --check`
  - Passed.

## 2026-05-31 Unit-test consolidation

Task:
- Reduce the number of unit test files under `tests/unit` without dropping test coverage.

Files changed:
- Removed the 17 fine-grained `tests/unit/test_*.py` files.
- Added 6 domain-level test files:
  - `tests/unit/test_agent.py`
  - `tests/unit/test_agent_shadow_pipeline.py`
  - `tests/unit/test_environment.py`
  - `tests/unit/test_pipeline.py`
  - `tests/unit/test_protocol_architecture.py`
  - `tests/unit/test_data_sources_tushare.py`

Grouping:
- `test_agent.py`: formulaic scoring/weights/metrics, DeepSeek adapter, evidence pack, event checkpoint, NL shadow, LLM shadow advisor.
- `test_agent_shadow_pipeline.py`: LLM shadow pipeline CLI and ledger flow.
- `test_environment.py`: broker/replay, PIT feature build, leakage checks, auction correction, contracts/config.
- `test_pipeline.py`: experiment runner, formulaic WFO runner, WFO splitter.
- `test_protocol_architecture.py`: protocol freeze/held-out guards and architecture import boundaries.
- `test_data_sources_tushare.py`: TuShare download/update/audit guards plus intraday-by-date compaction/audit.

Verification:
- File count:

```bash
find tests/unit -maxdepth 1 -type f -name 'test_*.py' -print | sort
```

  - Returned 6 files.
- Compile and tests:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q tests/unit src scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
```

  - Passed: 97 tests.
- `git diff --check`
  - Passed.
- Cleanup:
  - Removed all `__pycache__` directories after verification.
- Resource checks with `nvidia-smi` and `free -h` were recorded before and after data-processing/API runs; system memory remained safe.

## 2026-05-31 Board-trading 2020+ completion follow-up

Task:
- Record current data findings in living documentation.
- Confirm whether the retained `board_trading` domain is complete from the 2020 research boundary.

Files changed:
- `docs/data_documentation.md`
  - Added current board-trading findings and retained boundaries: domain lower bound `20200101`, `limit_list_ths` source start `20231101`, `hm_detail` source start `20220801`, and source-specific treatment for `limit_list_d` vs `limit_list_ths`.
  - Added confirmed `top_list` source behavior: historical rows can contain ST/name aliases and small exact duplicates, so downstream PIT/evidence layers must deterministic-deduplicate.
- `docs/environment_design.md`
  - Added current auction-alignment findings: Shenzhen `00*.SZ` 09:30 minute/stk_auction ratio about `1.32`, Shenzhen `30*.SZ` about `1.72`, SH/BJ about `1.0`, and full-day minute-vs-daily unit ratios align at `100x` volume and `1000x` amount.
- `scripts/tushare/common.py`
  - Added `name` to the `top_list` audit key to avoid treating ST/name aliases as duplicate raw business rows.

Data command:

```bash
PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier board_trading --start-date 20200101 --end-date 20260529 --min-interval-seconds 0.25 --timeout-seconds 120
```

Run result:
- The command completed under PID `3314669`; log path:
  - `logs/tushare_board_trading_20200101_20260529_20260531.log`
- All expected partitions already existed and were skipped:
  - `kpl_list`: 7,750 / 7,750 skipped.
  - `limit_step`: 1,550 / 1,550 skipped.
  - `limit_cpt_list`: 1,550 / 1,550 skipped.
  - `limit_list_ths`: 3,115 / 3,115 skipped.
  - `top_list`: 1,550 / 1,550 skipped.
  - `top_inst`: 1,550 / 1,550 skipped.
  - `hm_list`: static skipped.
  - `hm_detail`: 926 / 926 skipped.
  - `ths_hot`: 4,650 / 4,650 skipped.
  - `dc_hot`: 3,100 / 3,100 skipped.

Final audit:
- Command:

```bash
PYTHONUNBUFFERED=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py board-trading --start-date 20200101 --end-date 20260529
```

- Output: `results/data_quality/board_trading_status.json`
- Status: warning, 0 errors, 1 warning.
- Completeness result: all dataset partition counts matched expected counts; missing files, missing sidecars, orphan sidecars, and pagination cap warnings were all 0.
- Remaining warning: `top_list_board_keys` has 342 duplicate key rows after including `name`; inspection showed these are exact duplicate raw rows. They are retained in raw and must be deterministic-deduplicated in PIT/evidence layers.

Partition counts from final audit:
- `kpl_list`: 7,750 files / 7,750 expected, 467,104 rows.
- `limit_step`: 1,550 / 1,550, 12,599 rows.
- `limit_cpt_list`: 1,550 / 1,550, 12,333 rows.
- `limit_list_ths`: 3,115 / 3,115, 114,652 rows.
- `top_list`: 1,550 / 1,550, 117,945 rows.
- `top_inst`: 1,550 / 1,550, 1,417,193 rows.
- `hm_list`: 1 / 1, 110 rows.
- `hm_detail`: 926 / 926, 209,752 rows.
- `ths_hot`: 4,650 / 4,650, 1,387,782 rows.
- `dc_hot`: 3,100 / 3,100, 1,833,150 rows.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards tests.unit.test_auction_correction`
  - Passed: 11 tests.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Data/lzp/MacroQuant/src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit`
  - Passed: 95 tests.
- `git diff --check`
  - Passed.
- Resource checks with `nvidia-smi` and `free -h` were recorded before and after data/audit/test runs.

## 2026-05-31 LLM conversation logging and repo cleanup

Task:
- Decide whether the heavy TuShare downloader should remain in `scripts/`.
- Clean empty folders whose only content was Python cache output.
- Ensure real provider API calls record complete conversation data for future audit/distillation.

Files changed:
- `src/hl_trader/agent/llm/deepseek.py`
  - Added default local JSONL conversation logging at `data/llm_conversations/deepseek/<model>/<YYYYMMDD>.jsonl`.
  - Each HTTP attempt now records request payload/messages, raw provider response, usage, request/response hashes, timing, attempt count, HTTP status, and error metadata.
  - Logging excludes Authorization/API key data and recursively redacts `sk-...` strings.
  - The adapter prepares the log directory before the API call and fails fast if logging cannot be prepared or written.
- `tests/unit/test_deepseek_client.py`
  - Added success and HTTP-error conversation-log tests.
  - Existing tests disable default logging with `conversation_log_dir=None` to avoid persistent artifacts.
- `docs/agent_design.md`
  - Documented the provider conversation-log contract and sensitive-local-artifact boundary.
- `docs/data_documentation.md`
  - Documented the current TuShare placement decision: `scripts/tushare/` can remain the runnable CLI/current implementation boundary, but further growth should migrate stable implementation to `src/hl_trader/data_sources/tushare/` with thin scripts.
- `AGENTS.md` and `CLAUDE.md`
  - Added the persistent rule that every real LLM provider call must be logged locally with prompts/messages and raw responses, without API keys or Authorization headers.

Cleanup:
- Removed all `__pycache__` directories.
- Removed stale empty directories left under `src/hl_trader/` from the earlier Agent/Environment refactor.
- Rechecked `src`, `scripts`, `tests`, and `ops`: no empty directories remain.

Verification:
- Targeted provider test:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_deepseek_client
```

  - Passed: 12 tests.
- Compile check:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
```

  - Passed.
- Full unit discovery:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
```

  - Passed: 97 tests.
- `git diff --check`
  - Passed.
- `find . -type d -name __pycache__ -prune -print`
  - Returned no paths after cleanup.
- `find src scripts tests ops -type d -empty -print`
  - Returned no paths.
- Resource checks with `nvidia-smi` and `free -h` were recorded before and after tests; system memory remained safe.

Notes:
- No live DeepSeek API call was made in this change; logging behavior was verified with mocked provider responses.
- Conversation logs are under ignored `data/`, so they are retained locally for audit/distillation but not committed.

## 2026-05-31 TuShare data_sources package refactor

Task:
- Clarify why `src/hl_trader` exists and place `data_sources` under that package.
- Move the heavy TuShare implementation out of `scripts/` while keeping current command paths stable.

Files changed:
- `src/hl_trader/data_sources/`
  - Added the data-source integration package.
- `src/hl_trader/data_sources/tushare/`
  - Moved `common.py`, `download.py`, `audit.py`, and `cron_update.py` here.
  - Fixed direct-module import guards in `download.py` and `audit.py` so they import through `hl_trader.data_sources.tushare`.
- `scripts/tushare/common.py`, `download.py`, `audit.py`, `cron_update.py`
  - Replaced heavy implementations with thin wrappers that add `src/` to `sys.path`, import the package implementation, and call `main()` for CLI scripts.
- `tests/unit/test_tushare_download_update_guards.py`
  - Updated tests to import/patch `hl_trader.data_sources.tushare` implementation modules directly.
- `docs/data_documentation.md` and `docs/pipeline_design.md`
  - Documented the new implementation boundary and preserved script command boundary.

Rationale:
- `src/hl_trader` is the importable project package under the standard `src` layout. Data-source implementations belong inside it so they can be imported, tested, packaged, and kept separate from runnable script entrypoints.
- `scripts/` now remains an operations surface only: stable shell/cron command paths are preserved, but new TuShare business logic should be added in `src/hl_trader/data_sources/tushare/`.

Verification:
- Direct wrapper entrypoints:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py --help
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py --help
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py board-trading --help
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --dry-run
```

  - Passed.
- Package import:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -c "from hl_trader.data_sources.tushare import download, audit, common, cron_update; print(download.__name__, audit.__name__, common.__name__, cron_update.__name__)"
```

  - Passed.
- Targeted TuShare tests:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tushare_download_update_guards
```

  - Passed: 9 tests.
- Compile and full unit tests:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
```

  - Passed: 97 unit tests.
- `git diff --check`
  - Passed.
- Cleanup:
  - Removed all `__pycache__` directories after compile/test runs.

## 2026-05-31 Living-doc wording cleanup

Task:
- Remove migration-style or old-version wording from current living documentation.

Files changed:
- `docs/data_documentation.md`
  - Rewrote the TuShare implementation section to state the current implementation path and command-entry path directly.
  - Rephrased daily update, event/flow, `share_float` rescue, and intraday source audit wording to avoid old-version or migration language.
- `docs/pipeline_design.md`
  - Rephrased raw-data boundary and feature-build wording to describe the current accepted design.

Verification:
- Keyword scan:

```bash
rg -n "后续不要|旧的|仍保留|兼容手工|迁移|不再用|wrapper|薄命令|薄 CLI|旧版本|历史迁移|superseded|obsolete|legacy" docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md
```

  - Returned no matches.
- `git diff --check`
  - Passed.

## 2026-05-31 TuShare Nightly Full-Window Audit Cron

Task:
- Add a nightly full-window raw-data audit after the regular TuShare update.

Resource checks:
- Before work:
  - `pwd -P` confirmed `/Data/lzp/MacroQuant`.
  - `free -h` showed about `417Gi` available RAM.
  - `nvidia-smi` showed existing GPU workloads; this change used CPU-only compile/tests and cron dry-runs.
- After install:
  - `free -h` showed about `415Gi` available RAM.
  - `nvidia-smi` showed no new large GPU workload from this task.

Files changed:
- `src/hl_trader/data_sources/tushare/cron_update.py`
  - Added `audit_full` cron operation.
  - `build_job_commands` now supports one or more commands per cron job.
  - Nightly audit runs the six formal status refresh commands: base, macro, intraday-by-date, event-flow, board-trading, and text evidence.
  - The runner logs each command index and return code, then marks the job error if any command exits nonzero.
- `configs/tushare_update_schedule.json`
  - Added `cn_nightly_full_audit` with `operation=audit_full`.
- `ops/cron/tushare_update.cron`
  - Added the 02:30 Beijing-time `cn_nightly_full_audit` entry.
- `docs/data_documentation.md`
  - Documented the new nightly audit job and its boundary.
  - Clarified that nightly minute audit is full-window inventory plus sampled deep checks by default; full historical row-level minute scan remains manual via `intraday-by-date --full-scan`.
- `tests/unit/test_data_sources_tushare.py`
  - Added coverage for the new cron audit command construction.

Commands run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_cron_full_audit_builds_all_formal_status_commands
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --dry-run
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare
git diff --check
/home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l
```

Results:
- Compile passed.
- New cron command-construction test passed.
- TuShare unit file passed: 11 tests.
- `cron_update.py --job cn_nightly_full_audit --dry-run` produced six audit commands over `20200101-20260531`.
- `install_tushare_cron.py --dry-run` preserved the existing ChouQuant crontab entry and refreshed only the MacroQuant managed block.
- Installed the refreshed managed crontab; `crontab -l` shows:
  - 23:35 `cn_evening_full`
  - 02:30 `cn_nightly_full_audit`
  - 09:05 `cn_preopen_margin_backfill_0905`
  - 09:15 `cn_preopen_margin_retry_0915`
- `git diff --check` passed.

Conclusion:
- Nightly full-window status refresh is now scheduled.
- The six formal `results/data_quality/*_status.json` files will be refreshed by the 02:30 job if the previous update has finished and the global TuShare lock is free.

## 2026-05-31 Data Documentation Restructure

Task:
- Reorganize `docs/data_documentation.md` so the document reads as a stable data contract rather than an accumulated download plan.

Files changed:
- `docs/data_documentation.md`
  - Reordered the document into numbered sections:
    - `1. 文档边界与数据域`
    - `2. 数据域与数据表`
    - `3. 下载与更新`
    - `4. 审计与 Status`
    - `5. Raw PIT 数据合同`
    - `6. 官方文档索引`
  - Moved data table/domain descriptions before download/update operations.
  - Rewrote audit documentation with one shared audit layer plus concrete per-status logic for base research, macro context, intraday minutes, event/flow, board-trading, and text evidence.
  - Kept the current `board_trading` boundary without candidate/priority table wording.

Verification:

```bash
rg -n "^(#|##|###|####) " docs/data_documentation.md
rg -n "优先级|条件补充|主要价值|下载与 PIT 边界|P0|P1|P2|旧|迁移|wrapper|兼容" docs/data_documentation.md
wc -l docs/data_documentation.md
git diff --check
```

Results:
- Heading structure is numbered and ordered by contract flow.
- Candidate-table wording is gone; only a normal use of `优先级` remains in the fundamental audit explanation.
- Document length is 616 lines.
- `git diff --check` passed.

## 2026-05-31 Living Docs Navigation And Numbering

Task:
- Add a navigation block to the beginning of each current living document.
- Number headings consistently.
- Audit the high-level document flow for the five maintained docs.

Files changed:
- `docs/data_documentation.md`
  - Added a top navigation block.
  - Kept the new contract order: document boundary, data domains/tables, downloads/updates, audit/status logic, Raw PIT contract, official document links.
- `docs/agent_design.md`
  - Added navigation.
  - Numbered the main sections from boundary principles through provider adapter and trading isolation.
- `docs/environment_design.md`
  - Added navigation.
  - Numbered the main sections and the auction correction subsection.
- `docs/pipeline_design.md`
  - Added navigation.
  - Numbered the pipeline sections from boundary principles through future extensions.
- `docs/QMT_documentation.md`
  - Added navigation.
  - Numbered the QMT operational sections from current state through failure handling.

Logic audit:
- Data documentation now presents data definitions before download/update operations.
- Agent documentation already followed a sensible boundary -> code -> agent/evidence/LLM -> logging/provider -> trading isolation flow; numbering and navigation made that explicit.
- Environment documentation already followed boundary -> contracts -> PIT/features -> execution/replay -> ledger/future boundary; numbering and navigation made that explicit.
- Pipeline documentation already followed boundary -> CLI -> build/development/held-out/shadow -> outputs/reproducibility/fail-fast; numbering and navigation made that explicit.
- QMT documentation already followed current state -> target architecture -> current flow -> future live flow -> deployment/payload/execution/failure handling; numbering and navigation made that explicit.

Verification:

```bash
rg -n "^(#|##|###|####) " docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md
rg -n "优先级\s*\|\s*接口|优先级.*接口.*主要价值|下载与 PIT 边界|主要价值|条件补充|当前接入|P0|P1|P2|旧版本|历史迁移|wrapper|兼容" docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md
git diff --check
```

Results:
- All five living docs have navigation and numbered business headings.
- The residual keyword scan only matched `llm/deepseek.py` documentation text saying the adapter is compatible with OpenAI JSON mode, which is an active provider contract rather than a migration note.
- `git diff --check` passed.

## 2026-05-31 Living Docs Detailed Navigation

Task:
- Make the navigation blocks more detailed and useful for direct jumping inside each living document.

Files changed:
- `docs/data_documentation.md`
  - Expanded navigation to include all current numbered subsections and the nested `2.2.x` data-table sections.
- `docs/agent_design.md`
  - Expanded navigation to include second-level topics such as responsibilities, safety boundary, pack structure, response validation, conversation logging, adapter config, and trading isolation.
  - Added corresponding `###` headings where the content already had natural prose blocks.
- `docs/environment_design.md`
  - Expanded navigation to include PIT, selector, WFO, execution, replay, checkpoint, portfolio, freeze, and ledger subtopics.
  - Added corresponding `###` headings for existing logical blocks.
- `docs/pipeline_design.md`
  - Expanded navigation to command, flow, output, training, testing, held-out, shadow, ledger, freeze, fail-fast, and extension subtopics.
  - Added corresponding `###` headings.
- `docs/QMT_documentation.md`
  - Expanded navigation to health checks, read-only commands, future live order, remote deployment, payload schema, execution semantics, dry-run/live commands, reconcile, and failure handling.
  - Added corresponding `###` headings.

Verification:

```bash
rg -n "^(#|##|###|####) " docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md
for f in docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md; do grep -c '^```' "$f"; done
rg -n "优先级\s*\|\s*接口|优先级.*接口.*主要价值|下载与 PIT 边界|主要价值|条件补充|当前接入|P0|P1|P2|旧版本|历史迁移|wrapper" docs/data_documentation.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/QMT_documentation.md
git diff --check
```

Results:
- Heading structure now includes the detailed anchors used in each navigation block.
- Code-fence counts are even in all five docs.
- Stale candidate-table wording scan returned no matches.
- `git diff --check` passed.

## 2026-05-31 Full Code and Documentation Audit Follow-up

Task:
- Open a best-performing SubAgent for a full code/document audit, close it after completion, and address the actionable findings.

SubAgent:
- Spawned GPT-5.5 xhigh SubAgent `McClintock`.
- Audit scope covered source code, tests, configs, operational scripts, and living documents.
- Result: no blocking finding. Actionable findings were one high-risk logging contract issue, two medium-risk data/security consistency issues, and two low-risk stale references.

Fixes:
- `src/hl_trader/agent/llm/deepseek.py`
  - Conversation logging now writes a `status=started` JSONL record before each provider HTTP attempt, then writes the terminal `status=ok/error` record after completion.
  - Final response logging still includes raw provider response, usage, hashes, and error metadata.
  - Recursive log sanitization now redacts values under sensitive dict keys such as `api_key`, `authorization`, `token`, `secret`, and `password`, while preserving normal usage counters like `total_tokens`.
  - Derived `response_id` and standalone `usage` fields are sanitized consistently with the raw provider response.
- `src/hl_trader/data_sources/tushare/common.py`
  - Text evidence `available_at` now uses the same source-time normalization path as board-trading data, adding explicit Asia/Shanghai `+08:00` offsets for standard TuShare timestamp strings.
- `docs/agent_design.md`
  - Documented the `started` plus terminal conversation-log records and sensitive-key redaction.
- `docs/environment_design.md`
  - Updated the architecture boundary test reference to `tests/unit/test_protocol_architecture.py`.
- `configs/experiments/pilot_2020_daily.yaml`
  - Removed the stale P1/P2 comment and described the current semantic data source boundary.
- `tests/unit/test_agent.py`, `tests/unit/test_data_sources_tushare.py`
  - Added regression coverage for pre-call logging, sensitive-key redaction, logging fail-fast behavior, and text timestamp normalization.

Resource checks:

```bash
free -h
nvidia-smi
```

Result:
- System memory remained about 417 GiB available.
- No new GPU workload was launched; existing GPU allocations were unchanged.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_agent tests.unit.test_data_sources_tushare
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
find . -type d -name __pycache__ -prune -exec rm -rf {} +
```

Results:
- Compile passed.
- Targeted agent/TuShare test run passed: 46 tests OK.
- Full unit discovery passed: 101 tests OK.
- `git diff --check` passed.
- Post-test `__pycache__` directories were removed.

## 2026-06-01 Claude TuShare Cron Audit Validation

Task:
- Validate whether Claude's external audit summary about TuShare scheduled ingestion failures is reasonable.

Scope:
- Read-only inspection of `.runtime/tushare/cron_state.json`, `logs/tushare_cron_dispatch.log`, per-job cron logs, `configs/tushare_update_schedule.json`, installed crontab, TuShare update/audit code, top-level data-quality status files, and sampled raw partitions under `data/raw`.

Key checks:

```bash
cat .runtime/tushare/cron_state.json
tail -n 120 logs/tushare_cron_dispatch.log
sed -n '220,270p' logs/tushare_cron_cn_evening_full_20260531_20260531_233501.log
sed -n '1370,1510p' src/hl_trader/data_sources/tushare/download.py
sed -n '60,130p' src/hl_trader/data_sources/tushare/cron_update.py
crontab -l
```

Raw data samples:

```text
20200102 daily=3797 minute=3750 gap=58  bj_gap=56  extra=11
20210104 daily=4208 minute=4126 gap=89  bj_gap=87  extra=7
20220104 daily=4737 minute=4600 gap=146 bj_gap=144 extra=9
20230103 daily=5062 minute=5066 gap=2   bj_gap=0   extra=6
20250102 daily=5369 minute=5383 gap=2   bj_gap=0   extra=16
20260529 daily=5505 minute=5505 gap=0   bj_gap=0   extra=0
```

Findings:
- Claude's main operational finding is correct: `cn_evening_full` failed on 20260529, 20260530, and 20260531 with `RuntimeError: 20200102: 57 minute codes still missing after retries`.
- The failure path is exactly `update -> intraday_by_date`; `update` then stops before `share_float_complete` and `text_evidence`.
- The root coverage mismatch is real: `expected_codes_source=daily` uses every `daily/trade_date=YYYYMMDD` code, while early historical `stk_mins_1min_by_date` files do not contain many NEEQ/BSE-renamed `.BJ` codes and the persistent `300114.SZ`/`302132.SZ` gap.
- The local explanation for why this appeared on 20260529 is weaker than Claude stated. `data/raw/daily/trade_date=20200102.parquet` has filesystem mtime `2026-05-19`, so the local corpus already had the >50 early-date gap before the cron was installed and before the 20260529 update path was enabled.
- Blindly excluding all `.BJ` codes is not correct: sampled 2023/2025/20260529 minute files include complete `.BJ` coverage. The fix should be a minute-coverage expected-universe rule that excludes historical no-minute source rows only for dates where the source does not provide them, plus known persistent no-minute exceptions or a documented tolerance for existing historical files.
- Claude's non-trading-day pre-open margin finding is correct: the 20260531 09:05 and 09:15 jobs targeted 20260530 and failed with `no SSE open dates found for 20260530-20260530`.
- Status staleness is correct: most top-level status files were last generated for 20260528/20260529, while only `board_trading_status.json` was refreshed on 20260531. The nightly audit job had been installed but had not yet reached its first 02:30 Beijing run at inspection time.
- The audit warning forecast is directionally correct, but not exactly as phrased: `cn_nightly_full_audit` passes `--expected-codes-source daily`, but the default intraday audit checks only the first 20 files unless `--full-scan` is set; those first 20 sampled early-2020 files all fail coverage under the daily universe.

Conclusion:
- The audit is mostly reasonable and caught a real critical automation failure.
- Recommended fixes: make pre-open event-flow jobs skip non-trading target dates; change minute expected coverage to a source-aware minute universe instead of full daily universe; avoid re-downloading an entire existing day when only stable source-unavailable codes are missing; and rerun the cron dry-run/update-window tests plus intraday audit after patching.

## 2026-06-01 TuShare Cron Ingestion Fix

Task:
- Fix the confirmed cron ingestion failures with minimal code churn.

Changes:
- `src/hl_trader/data_sources/tushare/common.py`
  - Added optional `allow_empty` to `load_sse_open_dates`; default remains strict.
  - Added `expected_codes_source=minute` for intraday by-date validation. If the final by-date minute file already exists, this uses the file's own `ts_code` coverage as the source-aware expected universe. If the file does not exist, it falls back to the `daily` universe for new-day ingestion.
- `src/hl_trader/data_sources/tushare/download.py`
  - `download_event_flow` now treats empty SSE trading windows as a successful skip. This fixes weekend/holiday `margin` and `margin_detail` pre-open jobs.
  - Daily `update` and manual `update-intraday-by-date` now default to `expected_codes_source=minute`.
- `src/hl_trader/data_sources/tushare/cron_update.py`
  - Nightly full audit now calls intraday-by-date audit with `--expected-codes-source minute`.
- `configs/tushare_update_schedule.json`
  - `cn_nightly_full_audit` now uses `end_date_offset_days=1`, because the 02:30 job runs after the prior day's 23:35 update and should not audit a date that has not yet had an evening update.
- `docs/data_documentation.md`
  - Documented the source-aware minute universe, non-trading-day margin skip, and previous-natural-day nightly audit window.
- `tests/unit/test_data_sources_tushare.py`
  - Added regression coverage for source-aware minute expected codes and non-trading event-flow skip.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py intraday-by-date --start-date 20200101 --end-date 20200131 --expected-codes-source minute --min-rows-per-day 1 --output /tmp/macroquant_intraday_minute_audit_fix.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py update-intraday-by-date --start-date 20200102 --end-date 20200102 --expected-codes-source minute --min-interval-seconds 0.22 --timeout-seconds 120
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier event_flow --start-date 20260530 --end-date 20260530 --datasets margin margin_detail --min-interval-seconds 0.22 --timeout-seconds 120
```

Results:
- TuShare unit tests passed: 14 OK.
- Full unit discovery passed: 103 OK.
- Cron audit dry-run uses `--expected-codes-source minute` and targets the previous natural day.
- January 2020 intraday-by-date audit with `minute` coverage was ok.
- `update-intraday-by-date 20200102` wrote nothing and skipped the existing file.
- Non-trading `20260530` margin/margin_detail backfill skipped cleanly with return code 0.

## 2026-06-01 GitHub Collaboration Commit Prep

Task:
- Clean generated files, document GitHub collaboration standards, and prepare the current work for reviewable commits.

Changes:
- Removed generated Python cache directories and files:
  - `__pycache__`
  - `.pytest_cache`
  - `.mypy_cache`
  - `.ruff_cache`
  - `*.pyc`
  - `*.pyo`
- Updated `AGENTS.md` and `CLAUDE.md` with collaboration rules:
  - Prefer reviewable branches and pull requests for non-trivial work.
  - Split commits by independently reviewable concern.
  - Keep code, tests, and living docs together where practical.
  - Use concise imperative commit subjects.
  - Never commit runtime logs, local state, data dumps, API keys, scratch notebooks, or ignored artifacts.
  - Run meaningful verification plus `git diff --check` before commits or PRs.
  - Review `git diff --cached` before every commit.

Verification:

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -print
```

Results:
- `git diff --check` passed.



## 2026-06-01 GitHub Branch Naming Cleanup

Task:
- Keep the agent collaboration instructions concise while adding standard branch naming rules before pushing the review branch.

Changes:
- Consolidated the separate `Git` and `GitHub Collaboration` sections in `AGENTS.md` and `CLAUDE.md` into one `Git and GitHub` section.
- Added branch prefix conventions:
  - `fix/` for bug or data-integrity fixes.
  - `feat/` for new capabilities.
  - `docs/` for documentation-only updates.
  - `refactor/` for internal restructuring.
  - `test/` for tests.
  - `ops/` for deployment or scheduling changes.
  - `chore/` for maintenance.
- Corrected the repository-cleanliness spelling issue from `orgnized` to `organized`.

Verification:

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -print
```

Results:
- Resource checks stayed safe: about 417-418 GiB available system memory; GPU usage unchanged from existing processes.
- `git diff --check` passed.
- Compile passed.
- Full unit discovery passed: 103 OK.
- Post-test cache scans found no `__pycache__`, pytest/mypy/ruff cache directories, `*.pyc`, or `*.pyo` files.

## 2026-06-01 GitHub Language Convention

Task:
- Record the preferred language policy for PR comments and commit messages.

Changes:
- Updated `AGENTS.md` and `CLAUDE.md` so PR titles, descriptions, review comments, and discussion comments may be written in Chinese when that is clearer for project collaboration.
- Kept the default recommendation that commit subjects use concise English imperative wording for tooling/search consistency.
- Documented that Chinese commit subjects remain acceptable for human-facing milestones or domain-specific wording, and that commit bodies may use Chinese for context and validation details.

Verification:

```bash
git diff --check
```

Results:
- `git diff --check` passed.

## 2026-06-02 GitHub PR Splitting Guidance

Task:
- Clarify whether large changes should be split into multiple commits and pull requests.

Changes:
- Updated `AGENTS.md` and `CLAUDE.md` to state that broad work should be split by the smallest coherent review and revert unit.
- Documented that multiple PRs are preferred when changes can be reviewed, tested, deployed, or reverted independently.
- Documented the matching exception: tightly coupled changes should stay in one PR, and small follow-up docs/log updates may stay in the current PR when they do not distract from review.

Verification:

```bash
git diff --check
```

Results:
- `git diff --check` passed.

## 2026-06-02 TuShare Daily Update Policy Hardening

Task:
- Make daily TuShare cron updates fit the overnight window without re-scanning all historical minute data every night.
- Refresh important low-frequency reference data daily now that the overnight window is available.
- Recheck update cycles for delayed sources and install the revised cron after independent audit.

Changes:
- `configs/tushare_update_schedule.json`
  - Changed `cn_evening_full` from full-window default to `start_date_lookback_days=30`.
  - Added `--reference-min-interval-seconds 0.50` only to `cn_evening_full`.
  - Added `cn_preopen_board_backfill_0850` for previous-day `kpl_list/limit_step/limit_cpt_list`.
  - Added `cn_preopen_text_backfill_0855` for recent `cctv_news/news` refresh.
  - Updated cron policies for `stock_basic`, `namechange`, `index_classify`, `index_member_all`, `kpl_list`, `cctv_news`, and `news`.
- `src/hl_trader/data_sources/tushare/download.py`
  - Added selective reference refresh: daily update force-refreshes only configured reference datasets instead of forcing the whole reference tier.
  - Defaults daily update reference refresh to `stock_basic/namechange/index_classify/index_member_all`.
  - Added `--reference-min-interval-seconds`.
  - Made board-trading trade-date downloads skip successfully when the target SSE window has no open dates.
- `src/hl_trader/data_sources/tushare/cron_update.py`
  - Added `start_date_lookback_days`.
  - Added generic `download_tier` cron operation for targeted pre-open downloads.
- `ops/cron/tushare_update.cron`
  - Added 08:50 board and 08:55 text refresh jobs.
- `docs/data_documentation.md`
  - Documented the rolling update window, daily reference refreshes, delayed source backfills, and reference pacing.
- `tests/unit/test_data_sources_tushare.py`
  - Added tests for rolling cron start dates, targeted download-tier jobs, selective reference refresh, and board non-trading-day skip.

SubAgent audit:
- Spawned GPT-5.5 xhigh explorer `Aquinas`.
- Blocking finding: 08:50 board backfill would fail on non-trading target dates because board-trading used strict SSE calendar loading.
- Fix applied: `download_board_trading` now uses `allow_empty=True` and succeeds with zero tasks when no SSE open date exists.
- Medium/low findings were reviewed: installed cron was still old before this change, explicit `TUSHARE_UPDATE_START_DATE` remains an intentional override, and reference refresh selection was confirmed correct.

Verification:

```bash
/home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json >/tmp/mq_tushare_schedule.json
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts ops
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_board_backfill_0850 --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_text_backfill_0855 --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_backfill_0905 --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
/home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l | sed -n '/BEGIN MacroQuant TuShare update/,/END MacroQuant TuShare update/p'
```

Results:
- JSON config parse passed.
- Compile passed.
- Cron dry-runs showed:
  - evening job uses a rolling `end_date-30` to `end_date` window and `--reference-min-interval-seconds 0.50`;
  - board pre-open job targets `20260601` and forces `kpl_list/limit_step/limit_cpt_list`;
  - text pre-open job targets `20260530-20260601` and forces `cctv_news/news`;
  - margin and full audit commands remain scoped as intended.
- TuShare unit file passed: 18 OK.
- Full unit discovery passed: 107 OK.
- `git diff --check` passed.
- Post-test cache cleanup removed generated Python caches.
- Cron managed block was installed and inspected successfully.
- The old 2026-06-01 23:35 cron process was still running during the change; it was not stopped and will not use the new rolling-window config until the next scheduled run.
- Compile passed.
- Full unit discovery passed: 103 OK.
- Cache directory scan returned no remaining cache directories.
- `check.ipynb` remains untracked and intentionally unstaged.

## 2026-06-02 TuShare Revision Supervision

Task:
- Add daily recent-window force refresh and historical sentinel checks for source-side data corrections.
- Make source-correction monitoring visible without silently overwriting zero-ok partitions or hiding failed probes.

Changes:
- Added shared revision comparison helpers and a JSONL revision ledger contract at `results/data_quality/revision_events.jsonl`.
- Changed cron-driven daily `update` so retained daily trade-date datasets are force-refreshed inside the rolling update window while revision differences append `REVISION_ALERT` events.
- Added `stock_company` to the daily forced reference refresh set.
- Added `audit.py revision-sentinel` to sample historical daily trade-date partitions, compare TuShare source responses with local raw files, and write `results/data_quality/revision_summary.json` without overwriting raw data.
- Added cron job `cn_daily_revision_sentinel` at 04:00 Beijing time for daily sentinel sampling of `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, and `limit_list_d`.
- Expanded the evening rolling window from 14 to 30 natural days to cover longer holiday/late-correction windows.
- Hardened cron locking so jobs wait for the global lock, clear stale dead-PID locks, return nonzero on lock timeout, and compare command/config hashes before skip-existing.
- Protected `suspend_d` and `limit_list_d` nonempty raw partitions from empty overwrite unless `--allow-empty-revision-overwrite` is explicit.
- Kept T+1 `margin` and `margin_detail` out of the 23:35 full update, forced the 09:05/09:15 margin backfills, and added a 09:20 event-flow status refresh for pre-open gates.
- Updated data documentation to explain the revision ledger schema, pending-review workflow, cron timing, lock semantics, and date-partition refresh boundary.

Verification:

```bash
/home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_update_parser_force_refreshes_stock_company_by_default tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_daily_refresh_datasets_force_only_selected_trade_date_dataset tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_revision_sentinel_compares_without_overwriting_raw tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_cron_revision_sentinel_job_builds_audit_command
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts ops
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260602 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_daily_revision_sentinel --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py revision-sentinel --help
git diff --check
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l
```

Results:
- First-pass revision verification passed before the second SubAgent audit.
- Second-pass audit findings were incorporated: `page_limit=None` is normalized, reference forced refreshes skip empty overwrites, required zero-row daily/event-flow partitions raise, and pre-open event-flow status is refreshed after margin retry.
- Current verification passed: JSON config parse, compileall, TuShare unit tests 34 OK, full unit discovery 123 OK, cron dry-runs for evening/audit/revision/pre-open jobs, `git diff --check`, cache cleanup, final three-way SubAgent review with no blockers, and local cron reinstall/inspection.

## 2026-06-03 Fundamental PIT Refresh Groundwork

Task:
- Update financial/fundamental raw refresh strategy before deploying it.
- Add a PIT-ready `fundamental_events` layer.
- Connect the new PIT layer to `daily_alpha` without changing raw storage semantics.

Changes:
- `src/hl_trader/data_sources/tushare/download.py`
  - Added daily refresh controls for 2.2.3 financial data:
    - latest 6 report periods;
    - latest 3 announcement months;
    - targeted affected-code refresh for `dividend`, `fina_audit`, and `fina_mainbz_vip`;
    - 90-day dividend date-field probes across `ann_date`, `imp_ann_date`, `ex_date`, `record_date`, and `pay_date` to discover affected stocks.
  - Kept raw storage aligned with stable TuShare query patterns: report-period files, announcement-month files, and `ts_code` snapshots.
- `configs/tushare_update_schedule.json`
  - Added the new fundamental refresh arguments to `cn_evening_full`.
  - Updated financial interface cron policies to reflect latest-period/latest-month/affected-code refresh behavior.
- `src/hl_trader/environment/features/fundamental_events.py`
  - Added `FundamentalEventsBuilder`, `FundamentalEventsConfig`, `audit_fundamental_events`, and event readers.
  - Writes PIT-ready partitions under `data/features/fundamental_events/<dataset>/available_month=<YYYYMM>.parquet`.
  - Derives conservative `available_at` using `f_ann_date`, `ann_date`, `first_ann_date`, or `imp_ann_date`; dividend rows without `imp_ann_date/ann_date` are excluded from PIT events instead of using future event dates.
  - Uses statement availability as fallback for `fina_audit` and `fina_mainbz_vip` when their raw rows lack announcement dates.
- `src/hl_trader/environment/features/daily_pit.py`
  - Added optional `fundamental_events_dir` support.
  - Joins latest visible `fina_indicator_vip` and `dividend` PIT event features into `daily_alpha` only when explicitly provided.
- `scripts/hl.py`
  - Added `build-fundamental-events` and `audit-fundamental-events`.
  - Added optional `--fundamental-events-dir` to `build-features`.
- Updated `docs/data_documentation.md`, `docs/environment_design.md`, and `docs/pipeline_design.md`.
- SubAgent pre-deploy audit fixes:
  - `audit-fundamental-events` returns nonzero through CLI error handling when structural errors exist.
  - Dividend rows without `imp_ann_date/ann_date` are excluded from PIT events.
  - `available_month` writes use full-month replace and partial-month merge semantics.
  - Fundamental raw refresh now runs period/announcement-month datasets before affected-code `ts_code` snapshots.
  - PIT event audit checks window bounds, dataset/path consistency, `available_at_rule` allowlist, `source_path`, `source_hash`, and `source_row_id`.
  - Complete-month replace also deletes stale PIT partitions when a rebuilt month has no rows for a dataset.
  - `cn_nightly_feature_build` was added to the managed cron template after raw audit to build/audit `fundamental_events` and refresh recent `daily_alpha` with `--fundamental-events-dir`.

Verification:

```bash
free -h
nvidia-smi
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts ops
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_environment -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
```

Results:
- Resource checks were safe: about 423 GiB available system memory; GPU usage was from existing processes.
- Compile passed.
- Environment unit tests passed: 22 OK.
- TuShare unit tests passed: 35 OK.
- Follow-up SubAgent audit initially found two deployment risks:
  - affected `ts_code` selection could become near-full-market because it read whole recent period partitions;
  - the feature cron was rolling-only when `fundamental_events` had not been initialized.
- Fixes added after that audit:
  - affected-code selection now filters refreshed financial rows by a 90-day visible-date window using `f_ann_date/ann_date/first_ann_date/imp_ann_date/actual_date/pre_date`, while dividend also uses 90-day date probes;
  - `cn_nightly_feature_build` initializes from `default_start_date` when `data/features/fundamental_events` has no partitions;
  - `audit-fundamental-events --require-partitions` is passed only by cron before `daily_alpha` construction;
  - `run_update` fail-fast prevents `daily_alpha` build after a failed event-layer audit.
- The managed cron block was installed and inspected; it includes `cn_nightly_feature_build` at 03:35 Asia/Shanghai.
- Final verification passed:
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests scripts ops`
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit` -> 135 tests OK
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_feature_build --end-date 20260603 --dry-run`
  - `git diff --check`
- Final SubAgent audit found no blocker/high and explicitly cleared the changes for commit/PR.
- No real TuShare download or feature build was run during this deployment-prep pass.
