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

## 2026-06-03 Revision Ledger Field-Diff Samples

Task:
- Make future revision events more actionable by recording which fields changed, not only how many business keys changed.

Changes:
- `src/hl_trader/data_sources/tushare/common.py`
  - `compare_keyed_frames` now returns:
    - `changed_columns`: per-column changed-key counts;
    - `changed_columns_sample`: up to 5 changed business keys with up to 12 normalized old/new field values each;
    - `added_rows_sample` and `removed_rows_sample`: up to 5 normalized row-value samples for added/removed business keys.
  - Existing numeric canonicalization remains in place, so `1` and `1.0` do not create false revisions.
- `docs/data_documentation.md`
  - Documented the new revision event fields and the boundary that old JSONL rows are not backfilled.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_revision_event_records_changed_columns_and_row_samples tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_revision_comparison_canonicalizes_numeric_values tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_daily_refresh_datasets_force_only_selected_trade_date_dataset -v
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
```

Results:
- Targeted revision tests passed: 3 OK.
- Full unit discovery passed: 136 OK.
- Compile passed.
- `git diff --check` passed.
- A readonly TuShare probe for `limit_list_d` on `20250428` compared the current raw partition with a fresh API response without writing data. The comparator reported 101 old rows, 101 fresh rows, 25 changed keys, 0 added keys, 0 removed keys, and `changed_columns={"limit_amount": 25}`.
- No data download or ledger rewrite was run; the new fields will appear on future revision events.

## 2026-06-03 Agent Margin Short-Sell Shadow Action

Task:
- Let the Agent record a 融券卖出 style action while keeping the current LLM path shadow-only.

Changes:
- `src/hl_trader/agent/shadow/nl_shadow.py`
  - Added `MARGIN_SHORT_SELL_ACTION = "margin_short_sell"`.
  - Added `margin_short_sell` to `DEFAULT_NL_SHADOW_ACTIONS`.
- `src/hl_trader/agent/shadow/prompts.py`
  - Updated the JSON schema prompt action list to include `margin_short_sell`.
- `docs/agent_design.md`
  - Documented that `margin_short_sell` is a shadow research label, not a broker order.
  - Recorded that real execution needs Environment/Pipeline constraints for borrow availability, collateral, borrow costs, liquidation risk, whitelist, and review.
- `tests/unit/test_agent.py`
  - Added coverage that `NLShadowDecision(action="margin_short_sell")` is valid and remains `can_affect_trading=False`.
  - Added LLM advisor coverage that the default prompt/action set accepts `margin_short_sell`.

Verification:

```bash
free -h
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_agent -v
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
```

Results:
- Resource checks were safe.
- Agent unit tests passed: 34 OK.
- Full unit discovery passed: 136 OK.
- Compile passed.
- `git diff --check` passed.
- No broker execution or live trading behavior was changed.

## 2026-06-03 Margin Short Data And Return Split

Task:
- Add the short-side data needed for future `margin_short_sell` research and split return reporting into long and theoretical short sleeves.

Changes:
- `src/hl_trader/data_sources/tushare/common.py`
  - Added event-flow specs for `margin_secs`, `slb_sec`, and `slb_sec_detail`.
  - Added TuShare doc references and conservative `available_at` rules.
  - Added `EventDataset.end_date` so stopped `slb_*` interfaces do not create expected partitions after their valid history windows.
- `src/hl_trader/data_sources/tushare/download.py`
  - Event-flow download now respects per-dataset effective end dates.
- `src/hl_trader/data_sources/tushare/audit.py`
  - Added unit/PIT rules for the new datasets.
  - Expected event paths now respect per-dataset effective end dates.
- `configs/tushare_update_schedule.json` and `ops/cron/tushare_update.cron`
  - Added same-day `margin_secs` pre-open refresh jobs at 09:03 and 09:13.
  - Kept stopped `slb_*` interfaces out of daily rolling cron; they are initial/manual history backfill only.
- `src/hl_trader/environment/evaluation/metrics.py`
  - Added `ShortSaleAssumptions`, `theoretical_short_return`, and `long_short_return_breakdown`.
  - Default theoretical short-side assumptions are 100% cash collateral and 18% annual borrow fee.
- `src/hl_trader/pipelines/formulaic_wfo.py` and `src/hl_trader/pipelines/experiment.py`
  - Added `test_long_return` and `test_short_return` reporting fields while keeping current execution long-only.
- Living docs
  - Updated data, agent, environment, and pipeline docs for short-side data, PIT/unit rules, stopped-source boundaries, pre-open cron, and theoretical short-return scope.

Key commands:

```bash
free -h
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits
PYTHONUNBUFFERED=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier event_flow --start-date 20200601 --end-date 20200601 --datasets margin_secs slb_sec slb_sec_detail --force --min-interval-seconds 0.22 --timeout-seconds 120
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --start-date 20200601 --end-date 20200601 --datasets margin_secs slb_sec slb_sec_detail --raw-dir data/raw --output /tmp/macroquant_event_flow_short_sources_smoke.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py download --tier event_flow --start-date 20200101 --end-date 20260603 --datasets margin_secs slb_sec slb_sec_detail --min-interval-seconds 0.22 --timeout-seconds 120
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --start-date 20200101 --end-date 20260602 --raw-dir data/raw
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
/home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
```

Results:
- Resource checks were safe.
- `margin_secs` full retained history: 1552 files, 5,510,538 rows, first `20200102`, last local trade_cal date `20260602`.
- `slb_sec` stopped-source history: 1151 files, 2,137,157 rows, first `20200102`, valid through `20240930`.
- `slb_sec_detail` stopped-source history: 1095 files, 1,030,564 rows, first `20200102`, valid through `20240710`.
- Smoke audit for the three new datasets passed: status `ok`, 0 errors, 0 warnings.
- Formal `event_flow_status.json` refresh passed with 0 errors and 5 known warnings from existing duplicate/sparse event keys and `share_float` source-cap risk; no warning came from the new short-side datasets.
- Installed managed cron block; current crontab includes `cn_preopen_margin_secs_backfill_0903` and `cn_preopen_margin_secs_retry_0913`.
- JSON config parse passed.
- Targeted data-source, agent, and pipeline tests passed.
- Full unit discovery passed: 138 OK.
- `git diff --check` passed.
- Generated `__pycache__` and `.pyc` files were removed after verification.

## 2026-06-03 short-side data contract cleanup

Task: remove stopped TuShare transfer-lending interfaces from the active data contract and clarify update rules in the living data documentation.

Scope:
- Removed `slb_sec` and `slb_sec_detail` from active `EVENT_FLOW_DATASETS`, integrated doc refs, event-flow specs, event availability rules, and event audit unit/PIT rules.
- Removed the stopped-interface effective-end-date helper and the corresponding download/audit branching.
- Removed `slb_sec` and `slb_sec_detail` entries from `configs/tushare_update_schedule.json`.
- Removed unit assertions that expected stopped interfaces in the default event-flow selection.
- Updated `docs/data_documentation.md`:
  - `daily` is defined in `2.2.2` and only reused by 打板专题, not redefined in `2.6`.
  - Added grouped update frequency and refresh-rule table.
  - Documented `namechange` as announcement-driven and locally force-refreshed every evening with slower `0.50s` pacing.
  - Kept short-side live data support limited to `margin_secs` exchange eligibility; broker-side borrow inventory, fees, collateral, and liquidation rules remain outside current TuShare raw data.
- Updated `docs/agent_design.md` so `margin_short_sell` no longer references stopped TuShare transfer-lending sources.
- Kept already-downloaded local `data/raw/slb_sec*` history untouched; physical deletion is reversible only from backups, so the active contract was cleaned first without destructive data removal.

Key commands:

```bash
free -h
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
/home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json >/tmp/macroquant_schedule_check.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py event-flow --start-date 20200101 --end-date 20260602
rg -n "slb_sec|slb_sec_detail|event_effective_end_date" src configs tests docs/agent_design.md docs/data_documentation.md results/data_quality/event_flow_status.json
git diff --check
```

Results:
- Resource check was safe: about 450 GiB available RAM; GPUs 4-7 were effectively idle.
- JSON config parse passed.
- Targeted TuShare data-source tests passed: 41 tests OK.
- Full unit discovery passed: 137 tests OK. The count is one lower than the previous 138 because the stopped-interface effective-end-date regression test was removed with the stopped data contract.
- Formal event-flow audit refreshed `results/data_quality/event_flow_status.json`: status `warning`, 0 errors, 5 known warnings.
- Active code/config/test/living-doc search found no `slb_sec`, `slb_sec_detail`, or `event_effective_end_date` references.
- `git diff --check` passed.

## 2026-06-04 TuShare open-window revision hardening

Task: implement open-window force refresh with broad revision-ledger coverage and empty-response overwrite protection across the active TuShare data domains, then update cron and data documentation.

Scope:
- Added a generic `write_parquet_revision_aware()` path in `src/hl_trader/data_sources/tushare/common.py` with old/new parquet comparison, `REVISION_ALERT` JSONL append, old/new source hashes, write action, and default protection against overwriting an existing nonempty partition with an empty response.
- Wired revision-aware writes into `bak_basic`, fundamental period/month/ts_code refreshes, macro/global partitions, event/flow, board-trading, text evidence, intraday by-date writes, `share_float` raw/rescue partitions, and `share_float_complete` union rebuilds.
- Preserved the `share_float_complete` union shrink guard and changed union ledger events to use `dataset=share_float_complete` with `source=share_float_union_rebuild`.
- Added `--refresh-open-window` to the daily update entrypoint. In cron it force-refreshes only the rolling open window for macro/global/event/board/text/share_float while leaving large historical fills skip-existing by default.
- Added `--intraday-refresh-lookback-days 1` so nightly open-window refreshes do not force-rewrite the full 30-day minute window.
- Added active `margin_secs` support to event/flow specs and same-day PIT availability, and scheduled 09:03/09:13 pre-open forced raw refreshes. The 09:20 event-flow status refresh remains a full-window status through the previous day for T+1 `margin/margin_detail`; same-day `margin_secs` is protected by the raw pre-open refresh and later full audits.
- Updated `configs/tushare_update_schedule.json`, `ops/cron/tushare_update.cron`, and `docs/data_documentation.md`.
- Removed generated Python caches after tests.

SubAgent audits:
- GPT-5.5 xhigh SubAgent `Bernoulli` performed editable audit and fixed three small issues: empty overwrite protection no longer depends on a ledger path, `share_float_complete` union ledger classification is correct, and empty `share_float` refreshes preserve existing cap-risk row counts.
- GPT-5.5 xhigh SubAgent `Hubble` performed final editable audit and found no blocking issues or further required changes. Both SubAgents were closed after completion.

Key commands:

```bash
nvidia-smi
free -h
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -c "import json; json.load(open('configs/tushare_update_schedule.json', encoding='utf-8'))"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_secs_backfill_0903 --end-date 20260601 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_event_flow_audit_0920 --end-date 20260601 --dry-run
git diff --check
find . -type d -name __pycache__ -o -type d -name .pytest_cache -o -type f -name '*.pyc'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python ops/cron/install_tushare_cron.py
crontab -l | sed -n '/BEGIN MacroQuant TuShare update/,/END MacroQuant TuShare update/p'
```

Results:
- Resource checks were safe for this CPU/light-I/O validation path: about 447 GiB available RAM; GPUs had external load but this task did not launch GPU workloads.
- Schedule JSON parse passed.
- Targeted TuShare data-source tests passed: 43 tests OK.
- Full unit discovery passed: 139 tests OK.
- `cn_evening_full` dry-run includes `--refresh-open-window`, `--intraday-refresh-lookback-days 1`, selected reference refreshes including `bak_basic`, daily revision-monitored datasets, and `margin_secs` in event/flow.
- `cn_preopen_margin_secs_backfill_0903` dry-run targets same-day `margin_secs --force`.
- `cn_preopen_event_flow_audit_0920` dry-run builds the full event-flow status command through the configured previous-day target in real cron use.
- Installed managed cron block; crontab now includes 09:03/09:13 `margin_secs` jobs and the updated open-window comment.
- `git diff --check` passed.
- Cache scan is clean after cleanup.
- No live TuShare API download or raw data mutation was run in this task.

## 2026-06-04 TuShare historical revision sampling

Task: because the daily revision sentinel found historical inconsistencies, add and run a broader source-vs-local historical sample to identify which interfaces are unstable, which are stable, and what values actually changed.

Scope:
- Added `scripts/tushare/audit.py revision-history-sample` / `audit_revision_history_sample()` as a non-mutating historical probe.
- The command selects deterministic SSE trade dates by year (`--sample-per-year`) and checks active trade-date partitioned interfaces:
  - daily research: `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `suspend_d`, `limit_list_d`.
  - reference trade-date table: `bak_basic`.
  - event/flow trade-date tables: `margin`, `margin_detail`, `margin_secs`, `moneyflow`, `block_trade`.
  - board-trading trade-date and parameterized trade-date partitions: `kpl_list`, `limit_step`, `limit_cpt_list`, `limit_list_ths`, `top_list`, `top_inst`, `hm_detail`, `ths_hot`, `dc_hot`.
- The command writes process-only artifacts under `results/data_quality/process/`, does not overwrite raw, and does not append to the formal `revision_events.jsonl` ledger unless a separate workflow copies events.
- Added reporting for revision partitions, stable partitions, structural duplicate-key issues, missing local partitions, required remote-zero responses, changed columns, numeric deltas, and numeric-to-blank / blank-to-numeric value transitions.
- Updated `docs/data_documentation.md` to document `history_sample_probe` and the discovered historical `limit_list_d.limit_amount` risk.
- Added unit coverage to confirm the historical sample detects numeric deltas without overwriting raw.

Key commands:

```bash
free -h
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py revision-history-sample --start-date 20200101 --end-date 20260602 --sample-per-year 1 --seed 20260602_history_smoke --min-interval-seconds 0.25 --timeout-seconds 120 --output results/data_quality/process/revision_history_sample_smoke_status.json --events-output results/data_quality/process/revision_history_sample_smoke_events.jsonl
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py revision-history-sample --start-date 20200101 --end-date 20260602 --sample-per-year 3 --seed 20260602_history_v1 --min-interval-seconds 0.25 --timeout-seconds 120 --output results/data_quality/process/revision_history_sample_status.json --events-output results/data_quality/process/revision_history_sample_events.jsonl
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py revision-history-sample --start-date 20200101 --end-date 20260602 --sample-per-year 20 --seed 20260602_history_focus_v1 --groups daily --daily-datasets limit_list_d suspend_d --min-interval-seconds 0.25 --timeout-seconds 120 --output results/data_quality/process/revision_history_focus_limit_suspend_status.json --events-output results/data_quality/process/revision_history_focus_limit_suspend_events.jsonl
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/audit.py revision-history-sample --help
git diff --check
find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
```

Results:
- Main sample window: `20200101-20260602`; 3 sampled SSE trade dates/year, 21 trade dates total.
- Main sample checked 21 active trade-date interfaces. No API errors, no required remote-zero responses, and no local gaps with non-empty remote responses.
- Stable in the main sample: `daily`, `adj_factor`, `daily_basic`, `stk_limit`, `bak_basic`, `margin`, `margin_detail`, `margin_secs`, `moneyflow`, `top_inst`, `kpl_list`, `limit_step`, `limit_cpt_list`, `limit_list_ths`, `hm_detail`, `ths_hot`, `dc_hot`.
- Main substantive revisions:
  - `limit_list_d`: 13/21 partitions revised, 157 changed business keys, all changes in `limit_amount`.
  - `suspend_d`: 1/21 partitions revised; 20251127 gained `688766.SH` with `suspend_type=S`.
- Main structural issues:
  - `block_trade`: 6/21 partitions had duplicate business keys in both old and new data.
  - `top_list`: 3/21 partitions had duplicate business keys in both old and new data.
  - These were separated from source-value revisions.
- Focus sample:
  - `limit_list_d`: 86/140 partitions revised, 481 changed keys, all `limit_amount` numeric-to-blank. Mean old numeric absolute value 33,315,310,089.74; median 1,918,907,038; p95 146,601,240,000; max 1,332,256,113,900.
  - `suspend_d`: 1/140 partitions revised; 20260116 removed `688005.SH` with `suspend_type=R`.
- Generated human-readable analysis: `results/data_quality/process/revision_history_sample_analysis.md`.
- Targeted TuShare tests passed: 44 tests OK.
- Full unit discovery passed: 140 tests OK.
- `git diff --check` passed.
- Cache scan is clean after cleanup.

## 2026-06-04 daily_alpha limit_amount quarantine

Task: after historical revision sampling showed `limit_list_d.limit_amount` repeatedly changing from numeric local values to blank current TuShare values, exclude that field from the feature layer and record the risk contract.

Scope:
- Raw `limit_list_d` schema and TuShare download/audit behavior were not changed.
- `DailyPITFeatureBuilder` now uses an explicit `limit_list_d` feature whitelist: `trade_date`, `ts_code`, and `limit`.
- `limit_amount` is declared as a quarantined `limit_list_d` column for Environment feature construction.
- Added a unit test proving that raw partitions containing `limit_amount` still do not emit `limit_amount` or `limit_list_d_limit_amount` in `daily_alpha`.
- Updated `docs/data_documentation.md`, `docs/environment_design.md`, and `docs/pipeline_design.md` to state that `limit_amount` is retained for raw/audit only and excluded from `daily_alpha`.

Key commands:

```bash
free -h
nvidia-smi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_environment -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit
git diff --check
find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
```

Results:
- Environment unit tests passed: 26 tests OK.
- Full unit discovery passed: 141 tests OK.
- `git diff --check` passed.
- Cache cleanup completed.
- Other historical revision findings remain unchanged: sampled value-level revisions are concentrated in `limit_list_d.limit_amount`; `suspend_d` showed sparse added/removed keys; `block_trade` and `top_list` showed structural duplicate-key issues rather than source-value rewrites; the other sampled active trade-date interfaces were stable in the sample.

## 2026-06-04 structural duplicate-key risk documentation

Task: record the `block_trade`/`top_list` duplicate business-key finding in the durable data risk section.

Scope:
- Added a risk row to `docs/data_documentation.md` chapter 7 for structural duplicate business keys.
- The documented contract is:
  - raw keeps original duplicate rows;
  - audit reports duplicate-key warnings;
  - PIT feature/evidence layers must use a fuller event key, exact duplicate removal, or `trade_date+ts_code` aggregation before joining to daily samples.
- No data or code path was changed.

Validation:

```bash
git diff --check
```

Result:
- `git diff --check` passed.

## 2026-06-04 HL orchestration and sandbox design documentation

Task: update the living design documents with the agreed outer/inner Agent HL flow and Sandbox-internal API-driven LLM Agent model, without adding a new design document and without introducing version-numbered implementation names.

Scope:
- `docs/agent_design.md`
- `docs/environment_design.md`
- `docs/pipeline_design.md`
- `LOGBOOK.md`

Design recorded:
- Restored the archive-level two-layer HL architecture:
  - outer Agent learns and mutates abstract Heuristic Templates across folds/trials;
  - inner Agent runs only inside train sandbox and instantiates candidate Heuristic Instances from a frozen Template;
  - test sandbox executes frozen Instances and cannot change template, prompt, parameters, protocol, or trade policy.
- Defined four template categories:
  - Factor Heuristic Template;
  - Natural Language Heuristic Template;
  - Trade Decision Template;
  - Trade Strategy Template.
- Recorded Sandbox-internal API-driven LLM Agent boundary:
  - sandbox can instantiate an LLM Agent;
  - sandbox cannot use internet search or arbitrary HTTP;
  - provider calls go through a controlled local LLM API Proxy;
  - API keys stay outside sandbox;
  - all prompts/responses are conversation-logged and hashable.
- Added Environment-level contracts for:
  - Data Gateway as the phase/fold/time permission layer;
  - as-of snapshot physical data boundary;
  - Sandbox Runner resource and filesystem boundary;
  - train/test/held-out sandbox permission matrix;
  - LLM API Proxy allowlist, logging, budget, cache and redaction rules.
- Added Pipeline-level orchestration:
  - docs/ledger/case context -> outer Agent -> templates -> folds -> train sandbox -> frozen Instance -> test sandbox -> metrics/cases -> Trial Ledger -> outer mutation.
- Recorded initial implementation scope:
  - first pass can omit short selling, T+0/inventory trading, event-driven re-decision, natural-language scoring in PnL, and dynamic inner-Agent parameter tuning;
  - retain interfaces for those capabilities without naming them as versioned features.

Validation:

```bash
pwd -P
rg -n "^## |^### " docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
git diff --check
```

Result:
- Documentation structure was inspected for duplicate top-level headings after edits.
- No code, raw data, cron, or experiment artifact was changed.

## 2026-06-04 data documentation vs TuShare code audit

Task: act as an editable audit SubAgent for the current TuShare data documentation and the data download/update/audit implementation.

Scope reviewed:
- `docs/data_documentation.md`
- `configs/tushare_update_schedule.json`
- `ops/cron/tushare_update.cron`
- `src/hl_trader/data_sources/tushare/{common,download,audit,cron_update}.py`
- `scripts/tushare/*.py`
- `tests/unit/test_data_sources_tushare.py`

Key findings:
- The documented six semantic data domains match the current code and schedule. A set comparison between schedule interfaces and code dataset constants had no unexplained differences after accepted aliases: `share_float_complete`, `stk_mins_1min`, and the final `stk_mins_1min_by_date` layer.
- `scripts/tushare/*.py` are thin wrappers and do not contain duplicate business logic; stable implementation is under `src/hl_trader/data_sources/tushare/`.
- `cn_evening_full` dry-run matches the documented rolling 30-day update, selected reference/daily force refresh, open-window refresh, 1-day intraday force refresh, `share_float_complete`, and fundamental-at-end ordering.
- `cn_nightly_full_audit` dry-run builds the six formal status commands, uses `--expected-codes-source minute` for the minute status, and offsets event/flow by one additional day for T+1 margin timing.
- The previous code changes for `dividend` probe, trade-calendar lookahead, non-fail-fast full audit, same-day `margin_secs`, and daily-alpha next-trade-date mapping are reflected in code/tests.
- Two documentation boundaries were too implicit:
  - `cn_nightly_feature_build` is scheduled after raw audit but does not read the six raw status files as a gate.
  - `cn_preopen_event_flow_audit_0920` refreshes previous-day `margin/margin_detail` status but does not include same-day `margin_secs`; same-day margin eligibility is currently guarded by the raw refresh job state and file existence.

Edits made:
- `docs/data_documentation.md`
  - Updated整理日期.
  - Clarified that `dividend/fina_audit/fina_mainbz_vip` are `ts_code` historical snapshots and that daily refresh targets recently affected symbols plus dividend date-probe candidates rather than full-market date slices.
  - Clarified feature-build gating: only `audit-fundamental-events` gates `daily_alpha`; strict raw-status gating must be implemented by Pipeline/QMT if required.
  - Clarified pre-open `margin_secs` boundary and added a timing/gating summary table.
- Main-thread follow-up:
  - Collapsed the long `3.5` cron bullet list and the separate timing/gating table into one task table.
  - Corrected refresh flag wording: daily trade-date tables use `--refresh-daily-datasets`; macro/global, event/flow, board-trading, text evidence, and share-float process windows use `--refresh-open-window`.
  - Re-ran schedule/code dataset set comparison, cron dry-runs, schedule JSON parse, `git diff --check`, and TuShare data-source unit tests after the table rewrite.

Validation and commands:

```bash
pwd -P
free -h
nvidia-smi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -c 'import json; from pathlib import Path; from hl_trader.data_sources.tushare import common as c; cfg=json.loads(Path("configs/tushare_update_schedule.json").read_text()); schedule={i["dataset"] for i in cfg["interfaces"]}; code=set(c.REFERENCE_DATASETS+c.DAILY_REQUIRED_DATASETS+c.DAILY_OPTIONAL_DATASETS+c.FUNDAMENTAL_DATASETS+[c.STK_MINS_DATASET,c.STK_MINS_BY_DATE_DATASET]+c.EVENT_FLOW_DATASETS+c.BOARD_TRADING_DATASETS+c.TEXT_DATASETS+c.MACRO_DATASETS); alias_ok={"share_float_complete","stk_mins_1min"}; print(json.dumps({"schedule_minus_code":sorted(schedule-code-alias_ok),"code_minus_schedule":sorted(code-schedule-{"share_float","stk_mins","stk_mins_1min_by_date"})},ensure_ascii=False,indent=2))'
/home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260603 --dry-run
/home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --end-date 20260604 --dry-run
git diff --check
PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
find src scripts tests -type d -name __pycache__ -prune -exec rm -rf {} +
find src scripts tests -type d -name __pycache__ -o -type f \( -name '*.pyc' -o -name '*.pyo' \)
```

Results:
- Interface set comparison: no unexplained schedule/code mismatch.
- Cron dry-runs produced the expected command shapes.
- `git diff --check` passed.
- Schedule JSON parsed successfully.
- TuShare data-source unit tests passed: 52 tests OK.
- Generated Python caches were removed.
- No live TuShare API download or raw-data mutation was run.
- Main-thread follow-up validation also passed: no unexplained schedule/code dataset mismatch, cron dry-runs for `cn_evening_full` / `cn_nightly_full_audit` / same-day `margin_secs` matched the documented command shapes, `git diff --check` stayed clean, and the cache scan was empty.

Remaining main-thread consideration:
- If the production pipeline should fail closed, add an explicit pre-feature/Agent gate that reads the six raw status files and same-day `margin_secs` raw job state/file freshness before feature freeze or order decisions. The current cron schedule orders jobs sensibly, but feature build is not automatically blocked by raw status warnings/errors.

## 2026-06-04 TuShare data-code editable audit

Task: perform an editable audit of the TuShare data-related code for redundant/garbage logic, stale branches, and obvious data-update/audit errors.

Scope:
- Reviewed `src/hl_trader/data_sources/tushare/{common,download,audit,cron_update}.py`, `scripts/tushare/*.py`, `configs/tushare_update_schedule.json`, `ops/cron/*`, `tests/unit/test_data_sources_tushare.py`, and `docs/data_documentation.md`.
- Focused on `trade_cal` lookahead, `dividend` probe, update order, audit `fail_fast=false`, revision ledger, empty-response protection, and cron command construction.

Findings and changes:
- `trade_cal` force refresh could shrink an existing yearly calendar partition if called with a narrow window. Added `merge_trade_cal_partition()` and made both normal coverage refresh and force refresh merge the refreshed rows into existing year partitions.
- Daily trade-date datasets had a separate revision-alert path that lacked the full shared revision event fields. Replaced it with `write_parquet_revision_aware()` so daily rows share the same ledger contract as `bak_basic`, macro/global, event/flow, board, text, intraday, and share-float writes.
- Rolling/open-window refreshes could shrink month/year aggregate partitions if a forced 30-day response overwrote a larger existing month/year file. Added `write_window_merged_partition()` so aggregate partitions replace rows inside the refreshed window while preserving same-partition rows outside the window; applied it to macro/global month/year partitions, event month partitions, and text month partitions.
- Rolling `update` could pass the 30-day cron start date into macro/global range-style datasets, creating short `range=YYYYMM_YYYYMM.parquet` files and risking future full-window audit misses. Added `--macro-start-date` with default `20200101`; range-style macro/global datasets use that retained lower bound, while ordinary month/year/code partitions still use the rolling window plus safe window merge.
- `scripts/tushare/*.py` are thin CLI wrappers and were not expanded or refactored. No large restructuring was done.
- Updated `docs/data_documentation.md` to record merged `trade_cal` writes and retained macro/global range-window semantics.

Validation commands:

```bash
pwd -P
free -h
nvidia-smi
PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest -v
PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260603 --dry-run
PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --end-date 20260603 --dry-run
PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit -v
git diff --check
find src scripts tests ops -type d -name '__pycache__' -prune -exec rm -rf {} +
find src scripts tests ops -type d -name '__pycache__' -o -type f \( -name '*.pyc' -o -name '*.pyo' \)
```

Results:
- Real path confirmed as `/Data/lzp/MacroQuant`.
- Resource checks were safe for CPU tests; GPUs remained heavily occupied by unrelated jobs but no GPU workload was started.
- Targeted TuShare guard tests passed: 49 OK.
- Cron dry-runs for `cn_evening_full` and `cn_nightly_full_audit` built successfully.
- TuShare data-source unit file passed: 52 OK.
- Schedule JSON parse passed.
- `compileall` passed.
- Full unit discovery passed: 150 OK.
- `git diff --check` passed.
- Generated Python caches were removed and the final cache scan was empty.
- No live TuShare download or raw-data mutation was run.

## 2026-06-04 TuShare cron recovery hardening

Task: implement the five follow-up fixes from the 20260603 update/audit review: repair the `dividend` crash, prevent fundamental refresh from blocking later daily domains, keep trade calendars current for pre-open and feature mapping, let full audit refresh every status domain even when one fails, and make `daily_alpha` use the official trading calendar for `tradable_date`.

Scope:
- Changed `probe_recent_dividend_codes()` to query only TuShare-supported dividend date params: `ann_date`, `imp_ann_date`, `ex_date`, and `record_date`. `pay_date` remains an event attribute, not a query param.
- Added trade-calendar coverage helpers in `download.py`.
  - `update` now refreshes `trade_cal` through `end_date + 7` by default.
  - Direct date-driven paths for daily, event/flow, board-trading, and by-date minutes refresh missing local `trade_cal` coverage before loading SSE open dates.
  - This specifically prevents same-day `margin_secs` pre-open refresh from skipping when local `trade_cal` only covers the previous trading day.
- Reordered `update_all_dimensions()` so fundamental data runs after macro/global, event/flow, board-trading, intraday, `share_float_complete`, and text evidence.
  - A future fundamental error still marks the job failed, but no longer prevents the operational daily domains from updating first.
- Set `configs/tushare_update_schedule.json` `cn_nightly_full_audit.fail_fast=false`.
  - The audit runner still returns non-zero if any audit command fails, but executes all six formal status commands before reporting the aggregate failure.
- Changed `DailyPITFeatureBuilder` so `tradable_date` maps from SSE `trade_cal` when available, falling back to the `daily` partition sequence only if no calendar exists.
  - This allows the latest completed daily partition to produce features for the next trading session even before that next session has a `daily` partition.
- Updated `docs/data_documentation.md`, `docs/environment_design.md`, and `docs/pipeline_design.md` for the current contracts.
- Added focused unit tests for:
  - dividend probe params;
  - same-day `margin_secs` trade-calendar refresh;
  - non-fail-fast cron multi-command behavior;
  - latest daily partition mapping to next `trade_cal` session.

Validation and resource checks:

```bash
pwd -P
free -h
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m json.tool configs/tushare_update_schedule.json >/dev/null
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_dividend_probe_uses_only_supported_date_params tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_event_flow_refreshes_trade_cal_before_same_day_margin_secs tests.unit.test_environment.DailyPITFeatureBuilderTest.test_last_daily_feature_uses_trade_cal_for_next_tradable_date -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare tests.unit.test_environment -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_evening_full --end-date 20260603 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_nightly_full_audit --end-date 20260603 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/cron_update.py --job cn_preopen_margin_secs_backfill_0903 --end-date 20260604 --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/tushare/download.py update --help
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src scripts tests
git diff --check
find src scripts tests -type d -name __pycache__ -print
find src scripts tests -type f \( -name '*.pyc' -o -name '*.pyo' \) -print
```

Results:
- Targeted new tests passed.
- TuShare + Environment test subset passed: 73 tests OK.
- Full unit discovery passed: 145 tests OK.
- `compileall` passed.
- `git diff --check` passed.
- Cron dry-runs produced the expected evening update, full audit, and same-day `margin_secs` commands.
- Generated Python caches were removed from `src/`, `scripts/`, and `tests/`.
- No live TuShare download or raw-data mutation was run in this task.

## 2026-06-04 data-code audit follow-up

Task: open a SubAgent to audit data-related code for garbage, redundancy, logic errors, and possible small refactors; perform necessary main-thread fixes after review.

SubAgent:
- Spawned `Huygens` with editable audit scope covering `src/hl_trader/data_sources/tushare/`, `scripts/tushare/`, schedule config, cron ops, data-source tests, and `docs/data_documentation.md`.
- The agent did not return a final report within the review window after two waits and was closed while still running.
- No SubAgent edits were integrated.

Main-thread finding:
- `trade_cal` date handling used multiple local string-normalization patterns:
  - `download.sse_trade_cal_covers()` stripped non-digits;
  - `common.load_sse_open_dates()` and `common.latest_sse_calendar_date()` compared raw strings.
- TuShare normally returns `YYYYMMDD`, so this was not an immediate production blocker, but it was a real maintainability and edge-format risk.

Changes:
- Added `normalize_date_key()` in `src/hl_trader/data_sources/tushare/common.py`.
- Reused it in:
  - `load_sse_open_dates()`;
  - `latest_sse_calendar_date()`;
  - `download_trade_cal()` existing-year coverage checks and SSE open-date collection;
  - `sse_trade_cal_covers()`.
- Added `test_trade_cal_helpers_normalize_date_strings()` to cover `YYYYMMDD`, `YYYY-MM-DD`, and `YYYY/MM/DD` calendar values.

Validation and resource checks:

```bash
pwd -P
free -h
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_trade_cal_helpers_normalize_date_strings tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest.test_event_flow_refreshes_trade_cal_before_same_day_margin_secs -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_data_sources_tushare -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests/unit -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m compileall -q src scripts tests
git diff --check
find src scripts tests -type d -name __pycache__ -print
find src scripts tests -type f \( -name '*.pyc' -o -name '*.pyo' \) -print
```

Results:
- Targeted tests passed.
- TuShare data-source test file passed: 48 tests OK.
- Full unit discovery passed: 150 tests OK.
- `compileall` passed.
- `git diff --check` passed.
- Generated Python caches were removed from `src/`, `scripts/`, and `tests/`.
- No live TuShare download or raw-data mutation was run.

## 2026-06-04 data update section simplification

Task: remove duplicated 3.2.1/3.2.2 daily-update explanations from `docs/data_documentation.md`.

Scope:
- Removed the separate `3.2.1 更新频率与刷新规则速查` and `3.2.2 分层更新语义` subsections from the table of contents.
- Replaced the long prose section with two tables under `3.2 日常增量更新`:
  - global update semantics;
  - per-domain refresh rules.
- Kept the operational details for skip-existing, sidecar coverage, force refresh, empty-response protection, minute universe, cron windows, and per-domain refresh cadence.

Validation:

```bash
git diff --check
```

Result:
- `git diff --check` passed.

## 2026-06-04 data documentation risk-section order

Task: move the global data-risk summary before the official TuShare document index and confirm whether cross-interface stock-pool coverage differences are treated as audit errors.

Scope:
- Moved `全文数据风险与口径修正总结` to chapter 6.
- Moved `官方文档索引` to chapter 7.
- Updated the table of contents and the historical auction-risk internal link.
- Reviewed `audit_daily_cross_coverage()` and `audit_stock_universe_semantics()`:
  - `daily` vs `daily_basic` coverage differences are warning findings when either side has extra codes.
  - `adj_factor` and `stk_limit` are warning only when `daily` has codes missing from those tables; extra `adj_factor`/`stk_limit` rows are documented as valid source-scope differences.
  - `stock_company` vs `stock_basic` coverage is a semantic warning, and the living data doc states `stock_company` is not required to equal the full stock pool.
  - These checks are designed to expose missing-data risk and source-scope differences, not to fail the audit as hard errors.

Validation:

```bash
git diff --check
```

Result:
- `git diff --check` passed.

## 2026-06-04 Agent/Environment/Pipeline document architecture audit

Task: audit the three HL orchestration living docs for consistency, implementation-readiness, organization, and readability.

Scope:
- Reviewed `docs/agent_design.md`, `docs/environment_design.md`, and `docs/pipeline_design.md`.
- Focused on outer/inner Agent roles, Sandbox-internal API-driven LLM Agent, Data Gateway, as-of snapshot, Sandbox Runner, LLM API Proxy, freeze points, and Trial Ledger boundaries.
- No code, raw data, TuShare download, or live LLM API call was run.

Findings:
- The overall design is coherent: Agent owns Template/Instance semantics and LLM behavior, Environment owns data visibility and execution isolation, Pipeline owns fold orchestration, freeze, artifact verification, and ledger merge.
- One wording risk could lead to a wrong implementation: Environment's permission table previously described train sandbox ability as "LLM changes template/parameters." This now says inner Agent generates candidate Instance/parameters/search plans; outer Template mutation remains outside test and happens through the outer Agent loop.
- Another boundary needed clarification: sandbox writes only ledger fragments/artifacts, while Pipeline writes the authoritative Trial Ledger after artifact/manifest/exit-code checks.
- Pipeline's Template section repeated Agent semantic details. It now focuses on schema, complexity, data boundary, action boundary, search boundary, and NL boundary checks.

Documentation changes:
- `docs/agent_design.md`: added a scope sentence for the HL Agent chapter and a Template/Instance boundary table; clarified that the inner Agent runs against a frozen Template and cannot mutate the outer Template.
- `docs/environment_design.md`: added a scope sentence for Data Gateway/Sandbox, replaced ambiguous template-mutation wording in the sandbox permission matrix, and clarified ledger fragment versus authoritative ledger ownership.
- `docs/pipeline_design.md`: added a scope sentence for HL orchestration and replaced the repeated template semantics table with Pipeline acceptance checks.
- `LOGBOOK.md`: recorded the concise audit result.

Validation:

```bash
nvidia-smi
free -h
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|') and set(lines[i + 1].replace('|','').replace('-','').replace(':','').strip()) <= set():
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                if lines[j].count('|') != expected:
                    problems.append((path.as_posix(), j + 1, expected, lines[j].count('|'), lines[j]))
                j += 1
            i = j
        else:
            i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
```

Results:
- GPU memory was already heavily used by unrelated processes; this was a docs-only audit and did not start GPU work.
- System RAM was safe for a docs-only task.
- Markdown table column check passed.
- `git diff --check` passed for the three docs.

## 2026-06-05 Environment documentation consolidation

Task: merge overly granular `docs/environment_design.md` top-level chapters while keeping the same design content.

Scope:
- Consolidated the Environment living doc into six top-level chapters:
  1. Boundary principles and code organization.
  2. Configuration contract.
  3. PIT data, features, and leakage.
  4. WFO, execution, replay, and evaluation.
  5. Data Gateway and Sandbox.
  6. Pending environment boundaries.
- Updated `docs/agent_design.md` and `docs/pipeline_design.md` references from the old Environment sandbox chapter to the new chapter 5.
- No code, raw data, TuShare download, live LLM API call, or cron change was run.

Validation:

```bash
nvidia-smi
free -h
rg -n '^(##|###) ' docs/environment_design.md
rg -n '第 14 章|第 15 章|#14-|#15-' docs/agent_design.md docs/pipeline_design.md docs/environment_design.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Resource checks were safe for a docs-only validation; no GPU workload was started.
- Environment heading scan shows the six intended top-level chapters.
- Old Environment chapter 14/15 references are absent.
- Markdown table column check passed.
- `git diff --check` passed for the affected documentation and log files.

## 2026-06-05 Agent/Pipeline documentation consolidation

Task: apply the same chapter-consolidation pattern to `docs/agent_design.md` and `docs/pipeline_design.md`.

Scope:
- Consolidated `docs/agent_design.md` into five top-level sections:
  1. Boundary principles and code organization.
  2. HL Agent architecture and formulaic baseline.
  3. Evidence, prompt, and response contract.
  4. Shadow recorder, provider, and logs.
  5. Trading-system isolation.
- Consolidated `docs/pipeline_design.md` into six top-level sections:
  1. Boundary principles, code organization, and CLI.
  2. Feature Build and PIT entrypoints.
  3. WFO, held-out, and replay execution.
  4. LLM shadow, evidence, and provider calls.
  5. Ledger, Freeze, and Fail-Fast.
  6. HL two-layer Agent orchestration and extensions.
- Updated cross-document references in Agent, Environment, and Pipeline docs to use the new chapter numbers.
- No code, raw data, TuShare download, live LLM API call, or cron change was run.

Validation:

```bash
nvidia-smi
free -h
rg -n '^(##|###) ' docs/agent_design.md docs/pipeline_design.md docs/environment_design.md
rg -n '第 3 章|第 11 章|第 12 章|第 14 章|第 15 章' docs/agent_design.md docs/pipeline_design.md docs/environment_design.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Resource checks were safe for a docs-only validation; no GPU workload was started.
- Heading scan confirmed Agent has 5 top-level chapters and Pipeline has 6 top-level chapters.
- Stale cross-references to old Agent/Pipeline/Environment chapter numbers were absent after updates.
- Markdown table column check passed.
- `git diff --check` passed for the affected documentation and log files.

## 2026-06-05 limit_list_d feature quarantine

Task: explicitly isolate unstable or日终明细 fields from `limit_list_d` while keeping the stable daily limit-status label.

Changes:
- `src/hl_trader/environment/features/daily_pit.py` now names `LIMIT_LIST_D_RAW_ONLY_COLUMNS` for seal amount, seal timing, reopen-count, strength, and order fields.
- `DailyPITFeatureBuilder` still reads only `trade_date/ts_code/limit` from `limit_list_d`; a defensive drop keeps any raw-only columns out of the merge if they appear.
- `tests/unit/test_environment.py` now verifies `limit_amount/fd_amount/first_time/last_time/open_times/strth/limit_order` do not enter `daily_alpha`.
- Updated Data, Environment, and Pipeline docs to state that current daily features admit only `limit_list_d.limit`; other `limit_list_d` fields remain raw/audit-only.

Validation:

```bash
nvidia-smi
free -h
PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_environment.DailyPITFeatureBuilderTest
git diff --check -- src/hl_trader/environment/features/daily_pit.py tests/unit/test_environment.py docs/environment_design.md docs/data_documentation.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/environment_design.md'), Path('docs/data_documentation.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
nvidia-smi
free -h
```

Results:
- Resource checks were safe; no GPU workload was started.
- `DailyPITFeatureBuilderTest` passed: 8 tests OK.
- `git diff --check` passed for the changed code/docs/log files.
- Markdown table column check passed for Data, Environment, and Pipeline docs.

## 2026-06-05 dynamic feature-window design note

Task: record the intended Agent/Pipeline/Environment boundary for historical rolling-window features before implementation.

Decision:
- Agent may propose historical windows as part of a Factor Template, such as 20/60/120-day momentum, liquidity, or volatility windows.
- Pipeline must validate the proposed windows against an allowed set, maximum lookback, data availability, and freeze/hash rules.
- Environment must be the only layer that reads raw data and constructs PIT-safe rolling features; Agent must not directly read raw data or change windows inside test/held-out.
- Current implementation is not dynamic: `daily_alpha` still builds fixed `ret_5d/ret_20d/ret_60d/amount_ma20/volatility_20d`, and `lookback_days` is still a manual/cron CLI parameter with default 80.

Documentation changes:
- `docs/agent_design.md`: added the Agent-side proposal boundary.
- `docs/environment_design.md`: recorded current fixed-window status and target dynamic-window design.
- `docs/pipeline_design.md`: recorded future validation/freeze responsibility and current lack of automatic lookback inference.

Validation:

```bash
nvidia-smi
free -h
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Resource checks were safe for documentation-only work; no GPU workload was started.
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the affected docs/logbooks.

## 2026-06-05 universe selector design note

Task: record the intended Agent/Pipeline/Environment boundary for universe selection before implementation.

Decision:
- Agent may propose universe preferences, such as exchange scope, ST exclusion, minimum listing days, and liquidity thresholds.
- Pipeline must validate those rules, ensure they are PIT-computable, and include them in freeze/hash records.
- Environment must be the only layer that turns the rules into a daily tradable universe using PIT data.
- Agent must not bypass the universe selector by scanning all raw/full-market data directly.
- Current implementation is not active: `ExperimentConfig.universe` is loaded as a configuration record but does not yet filter `daily_alpha` or backtest candidates.

Documentation changes:
- `docs/agent_design.md`: added the Agent-side universe proposal boundary.
- `docs/environment_design.md`: recorded target universe selector responsibilities and current non-enforcement.
- `docs/pipeline_design.md`: added the Pipeline validation/freeze boundary for universe rules.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the affected docs/logbooks.

## 2026-06-05 Environment feature table documentation

Task: make the `daily_alpha` feature-construction section easier to audit by listing current feature fields as a table.

Changes:
- `docs/environment_design.md` now separates feature construction into process rules and a `daily_alpha` field table.
- The table records each feature group's source, calculation or meaning, unit/value convention, and PIT boundary.
- The table explicitly documents `ret_1d`, trailing compound returns, rolling liquidity/volatility features, valuation/share fields, trading constraints, `limit_list_d.limit`, and optional `fund_*`/`dividend_*` fields.
- No code, raw data, tests, live API calls, or cron jobs were changed.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/environment_design.md')
lines = path.read_text(encoding='utf-8').splitlines()
problems = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
        marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
        if marker == '':
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                actual = lines[j].count('|')
                if actual != expected:
                    problems.append((j + 1, expected, actual, lines[j]))
                j += 1
            i = j
            continue
    i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('environment table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Environment Markdown table column check passed.
- `git diff --check` passed for Environment documentation and logbooks.

## 2026-06-05 history-window snapshot design note

Task: update Agent, Environment, and Pipeline docs so the HL design is not limited to pre-computed single-date `daily_alpha` features.

Decision:
- Keep `daily_alpha/feature_date=<YYYYMMDD>.parquet` as a single-date cross-sectional feature layer for baseline, deterministic replay, quick evidence pack, and frozen execution.
- Add a target `history_window` snapshot concept under Data Gateway/as-of snapshot for train sandbox research.
- `history_window` should contain only data visible before `decision_time`, potentially including daily, minute, fundamental, event, macro, and text sequences.
- Inner Agent may use Python tools in train sandbox to discover candidate windows, factors, and NL rules from this snapshot.
- Agent must not read full `data/raw`; Pipeline freezes accepted definitions; Environment rebuilds PIT features for test/held-out execution.

Documentation changes:
- `docs/environment_design.md`: added single-date feature vs historical sequence boundary, `history_window` Data Gateway output, and snapshot directory/rule contract.
- `docs/agent_design.md`: added train-sandbox historical sequence input boundary.
- `docs/pipeline_design.md`: added Pipeline responsibility for building `history_window` snapshots and freezing resulting rules.
- No code, tests, data, live API calls, or cron jobs were changed.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for affected docs/logbooks.

## 2026-06-05 history-window-only design correction

Task: remove the single-date cross-sectional feature layer from the target HL architecture after design review.

Decision:
- Target design no longer keeps a pre-compressed single-date feature layer as a separate main data layer.
- Data Gateway provides as-of `history_window`; train sandbox uses it for research and candidate discovery.
- Pipeline freezes accepted feature specs, universe rules, action policy, and LLM settings.
- Environment recomputes decision observation and order constraints from `history_window` for train/test/held-out/replay.
- Existing fixed daily feature builder remains a transitional baseline in code, not the target design path.

Documentation changes:
- `docs/environment_design.md`: replaced the daily feature table with `history_window` input and decision observation rules; removed `features.parquet` from snapshot input layout.
- `docs/agent_design.md`: changed the Agent boundary to use `history_window` and transitionary formulaic features instead of fixed daily features.
- `docs/pipeline_design.md`: changed Section 2 to `History Window 与 PIT 入口`, removed `daily_alpha` command examples from target flow, and rewrote evidence flow as snapshot-based.
- `docs/data_documentation.md`: changed remaining `daily_alpha` references to observation or transitional feature-build language.
- No code, tests, data, live API calls, or cron jobs were changed.

Validation:

```bash
rg -n "daily_alpha|feature_date=<|data/features/daily|features.parquet|factor_frame|Feature 到 Evidence|feature file|--features" docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/data_documentation.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md'), Path('docs/data_documentation.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/data_documentation.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Stale target-layer keyword scan returned no matches.
- Markdown table column check passed for Agent, Environment, Pipeline, and Data docs.
- `git diff --check` passed for affected docs/logbooks.

## 2026-06-05 Environment universe selector doc placement

Task: move universe selector details out of the static `ExperimentConfig` object table.

Changes:
- `docs/environment_design.md` Section 2.1 now lists only the four core static config objects.
- `universe` loaded-config wording was removed from the static object table.
- Universe selector execution rules live outside the static config table with other pending environment boundaries.
- No code, tests, data, live API calls, or cron jobs were changed.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/environment_design.md')
lines = path.read_text(encoding='utf-8').splitlines()
problems = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
        marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
        if marker == '':
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                actual = lines[j].count('|')
                if actual != expected:
                    problems.append((j + 1, expected, actual, lines[j]))
                j += 1
            i = j
            continue
    i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('environment table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Environment table column check passed.
- `git diff --check` passed for Environment doc and logbooks.

## 2026-06-05 Environment implementation boundary spec

Task: expand the pending Environment boundary list into implementation details for review before coding.

Changes:
- Replaced `docs/environment_design.md` Section 6 with a concrete implementation/audit contract.
- Added detailed subsections for universe selector, history-window observation, cross-domain selectors, intraday track PIT rules, benchmark/risk attribution, Data Gateway/as-of snapshot/Sandbox/LLM Proxy, and acceptance checks.
- Clarified that Agent proposes rules and candidates, Pipeline validates/freezes, and Environment owns PIT visibility, observation construction, execution constraints, replay, evaluation, and ledger primitives.
- Kept the target design history-window based; no single-date cross-sectional feature layer was reintroduced.
- No code, data, live API calls, cron jobs, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/environment_design.md')
lines = path.read_text(encoding='utf-8').splitlines()
problems = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
        marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
        if marker == '':
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                actual = lines[j].count('|')
                if actual != expected:
                    problems.append((j + 1, expected, actual, lines[j]))
                j += 1
            i = j
            continue
    i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('environment table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Environment Markdown table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.
- Keyword scan confirmed the target wording did not reintroduce `daily_alpha`.

## 2026-06-05 Theory-complete HL design document pass

Task: rewrite Agent, Environment, and Pipeline docs as theory-complete target design documents rather than current-code status documents.

Changes:
- `docs/environment_design.md`: removed the standalone pending-environment chapter and moved its content into the relevant body sections.
- Environment PIT section now embeds data visibility, history-window observation, selector contracts, cross-domain selector families, universe selector, and intraday PIT track.
- Environment execution/evaluation section now embeds long/short constraints, inventory-trade requirements, benchmark return, excess return, risk exposure, and attribution primitives.
- Environment Data Gateway/Sandbox section now embeds component order, Tool Gateway, LLM API Proxy, and acceptance checks.
- `docs/agent_design.md`: rewrote headings and wording around safety, double-layer Agent, Template/Instance, sandbox LLM, action proposals, provider logging, and trade-impact conditions as target architecture.
- `docs/pipeline_design.md`: rewrote history-window construction, WFO, evidence, freeze/fail-fast, and double-layer Agent orchestration as the target flow; complex features are now represented as policy-gated capabilities rather than pending work.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the three design docs and logbooks.
- Keyword scans found no remaining standalone pending/current-code status headings such as `待实现`, `尚未实现`, `当前代码`, `过渡 baseline`, `当前流程`, `初始落地`, or `后续扩展`.

## 2026-06-05 Template config boundary cleanup

Task: correct the design boundary between Environment configuration and Agent-generated Templates.

Changes:
- `docs/environment_design.md` Section 2.1 now treats `ExperimentConfig` as predefined experiment/permission constraints.
- Replaced the singular `HeuristicTemplate` row with `TemplateSearchPolicy`, which constrains allowed template types, variable families, data domains, maximum lookback, complexity, and mutation limits.
- Added text that concrete Templates are Agent outputs, not Environment config objects.
- `docs/agent_design.md` Section 2.2 now states that four Template types are generated by the outer Agent under `TemplateSearchPolicy`.
- `docs/pipeline_design.md` Section 6.2 now checks generated Templates against `TemplateSearchPolicy` before freeze.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the three design docs and logbooks.

## 2026-06-05 Template governance boundary cleanup

Task: move complexity and mutation limits out of the Environment contract.

Changes:
- `docs/environment_design.md` now uses `DataAccessPolicy` instead of `TemplateSearchPolicy` in `ExperimentConfig`.
- `DataAccessPolicy` covers allowed data domains, maximum lookback, phase permissions, snapshot scope, and available-at policy.
- `docs/agent_design.md` now defines `TemplateGovernancePolicy` for allowed template types, variable families, complexity limits, parameter/search budget, and mutation limits.
- `docs/pipeline_design.md` Section 6.2 now checks generated Templates against both Agent `TemplateGovernancePolicy` and Environment `DataAccessPolicy`.
- Confirmed no `TemplateSearchPolicy`, `complexity_limits`, `mutation_limits`, `allowed_template_types`, or `allowed_variable_families` references remain in the three living design docs.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the three design docs and logbooks.
- Residual keyword scan found no `TemplateSearchPolicy`, `complexity_limits`, `mutation_limits`, `allowed_template_types`, or `allowed_variable_families` in the three living design docs.

## 2026-06-05 Design-doc historical wording cleanup

Task: remove historical or transition-oriented wording from living design docs.

Changes:
- `docs/environment_design.md` Section 3.3 now directly states the `history_window -> decision_observation` contract without contrasting it against a pre-compressed single-day feature layer.
- `docs/pipeline_design.md` Section 2.2 now uses `构造流程` and removes the phrase that contrasted test/held-out replay with a pre-compressed daily cross-section layer.
- `docs/data_documentation.md` cron table now describes the 03:35 task as constructing/auditing `fundamental_events` for the `history_window -> observation` contract, without mentioning transition code paths.
- Removed a macro PIT sentence that framed precise publish timestamps as a later replacement; it now states the priority order directly.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
rg -n "目标主路径|预压缩|历史版本|当前 cron 仍|过渡|待替换|不作为目标|目标路径应|尚未实现|待实现|当前代码|baseline" docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/data_documentation.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md'), Path('docs/data_documentation.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md docs/data_documentation.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Keyword scan found no `目标主路径`, `预压缩`, `历史版本`, `当前 cron 仍`, `过渡`, `待替换`, `不作为目标`, `目标路径应`, `尚未实现`, `待实现`, `当前代码`, or `baseline` in the four living docs.
- Markdown table column check passed for Agent, Environment, Pipeline, and Data docs.
- `git diff --check` passed for the four docs and logbooks.

## 2026-06-05 Inner-vs-outer Agent boundary cleanup

Task: correct wording that implied the inner Agent discovers windows, factors, natural-language rules, or strategy Templates.

Changes:
- `docs/agent_design.md`: inner Agent now instantiates candidate Instances, parameter values, feature specs, NL rubric, action policy, and train scores from frozen Template/search space; outer Agent owns candidate windows, variable families, natural-language rules, and strategy Templates.
- `docs/environment_design.md`: train sandbox wording now says it executes outer Template-defined windows/specs/rubrics/policies and scores candidate Instances; Data Gateway `history_window` row now says train sandbox uses it for Template-bounded instantiation and scoring.
- `docs/pipeline_design.md`: train sandbox flow now says inner Agent/Python tools instantiate and score candidate Instances inside the frozen Template search space; forbidden actions include adding windows, factor families, NL rules, or strategies outside the outer Template.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
rg -n "内层 Agent.*发现|内层 Agent.*挖掘|Train sandbox.*发现|train sandbox.*发现|挖掘候选窗口|发现候选窗口|自然语言规则候选|交易策略候选|特征探索|history-window analysis|feature analysis" docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Residual keyword scan found no inner-Agent discovery wording such as `内层 Agent.*发现`, `内层 Agent.*挖掘`, `Train sandbox.*发现`, `挖掘候选窗口`, `自然语言规则候选`, `交易策略候选`, `特征探索`, `history-window analysis`, or `feature analysis`.
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the three design docs and logbooks.

## 2026-06-06 Selector wording cleanup

Task: clarify that selector/PIT-reader gating applies to all data domains, not only financial, event, macro, and text data.

Changes:
- `docs/environment_design.md` Section 3.2 now states that all raw or PIT-ready data entering `history_window`, `decision_observation`, or evidence must pass through Environment PIT reader/selector.
- Kept specific bullets for daily market data, minute data, and financial/event/macro/text data so each domain's visibility rule is explicit.
- `docs/environment_design.md` Section 3.7 now says daily market state, minute, financial, event, macro, text, and universe data all enter observation through Environment PIT reader/selector.
- Added a daily market selector row to Section 3.8 covering daily bars, adjustment factors, daily indicators, limits, suspensions, and whitelisted daily board-trading fields.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/environment_design.md')
lines = path.read_text(encoding='utf-8').splitlines()
problems = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
        marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
        if marker == '':
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                actual = lines[j].count('|')
                if actual != expected:
                    problems.append((j + 1, expected, actual, lines[j]))
                j += 1
            i = j
            continue
    i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('environment table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Environment table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-06 Data-visibility wording cleanup

Task: refine Environment Section 3.2 wording so selector is not repeated inside the financial/event/macro/text bullet.

Changes:
- `docs/environment_design.md` keeps the global rule that all raw or PIT-ready data entering `history_window`, `decision_observation`, or evidence must pass through Environment PIT reader/selector.
- The daily, minute, financial/event/macro/text bullets now focus on available-at timing and retained source/unit metadata.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-06 Agent output wording cleanup

Task: clarify whether the Environment decision-observation rule refers to the outer Agent or inner Agent.

Changes:
- `docs/environment_design.md` Section 3.3 now states that the outer Agent outputs structured Template candidates, mutations, and experiment queues.
- The same section states that the inner Agent outputs structured Instance candidates, parameter values, feature spec instances, NL rubric instances, action policy instances, and train scores.
- The no-direct-write/no-direct-raw rule now explicitly applies to both outer and inner Agents.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
rg -n 'Agent 输出的是结构化候选定义' docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
rg -n '外层 Agent 输出结构化 Template|内层 Agent 输出结构化 Instance|外层和内层 Agent 都不能' docs/environment_design.md
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Exact old generic sentence scan returned no matches.
- New explicit outer/inner Agent wording is present in `docs/environment_design.md`.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-06 History-window request boundary cleanup

Task: split the ambiguous `history_window_request` generator from `Agent 或 Pipeline` into separate intent and executable request objects.

Changes:
- `docs/environment_design.md` now defines `history_window_intent` as an outer-Agent object containing desired domains, candidate windows, purpose, and Template linkage.
- `history_window_request` is now a Pipeline object containing `decision_time`, `tradable_date`, fold, phase, universe, validated domains, max lookback, permission policy, and source-status requirements.
- Added the rule that Data Gateway only accepts Pipeline-generated `history_window_request`, not raw Agent intent.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/environment_design.md')
lines = path.read_text(encoding='utf-8').splitlines()
problems = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
        marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
        if marker == '':
            expected = line.count('|')
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                actual = lines[j].count('|')
                if actual != expected:
                    problems.append((j + 1, expected, actual, lines[j]))
                j += 1
            i = j
            continue
    i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('environment table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Environment table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-06 Feature-spec wording cleanup

Task: clarify `feature_spec` ownership and the meaning of calculation operators.

Changes:
- `docs/environment_design.md` now says `feature_spec` is proposed by the outer Agent and frozen by Pipeline before train/test execution, not generated by an ambiguous `Train Pipeline freeze`.
- The `feature_spec` row now uses `确定性计算算子`.
- Added explanatory text that calculation operators are reproducible feature operations such as returns, means, volatility, quantiles/ranks, truncation, and normalization, not Agent-written Environment code.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-06 Feature-spec ownership cleanup

Task: align feature ownership with the HL design: outer Agent discovers/sets factors; inner Agent only tunes inside the frozen spec.

Changes:
- `docs/environment_design.md` now defines `feature_spec` as an outer-Agent Factor Template proposal that Pipeline validates and freezes before train/test execution.
- Environment text now says the inner Agent can only tune parameters, factor weights/thresholds, and train scores under the frozen `feature_spec`; it cannot add factor definitions.
- `docs/agent_design.md` now describes Factor Heuristic Template as carrying factor definitions, input domains/columns, windows, deterministic operators, direction, filters, parameter space, and objective.
- `docs/agent_design.md` now describes Heuristic Instance as concrete parameters, factor weights/thresholds, NL rubric parameters, action policy parameters, and train scores.
- `docs/pipeline_design.md` train-sandbox flow now says the inner Agent instantiates candidate Instances and tunes parameters/weights under frozen Template and `feature_spec`.
- `docs/pipeline_design.md` freeze ordering now says `feature_spec` is already frozen before train, while the post-train freeze records the selected Instance, parameters, weights/thresholds, universe rule, action policy, and prompt/model/settings.
- Removed residual wording that could imply inner Agent generates `feature_spec` or that Pipeline waits until after train to freeze factor definitions.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
rg -n '生成候选 Instance、参数、feature spec|Pipeline 校验并 freeze 通过后的 feature spec|Train Pipeline freeze|Pipeline 在 train 结束|特征选择' docs/environment_design.md docs/agent_design.md docs/pipeline_design.md
rg -n 'feature_spec 是外层 Agent|Pipeline 在 train 前校验并冻结外层 Agent|基于冻结 Template 和 `feature_spec`|外层 Agent 负责提出窗口需求和因子定义' docs/environment_design.md docs/agent_design.md docs/pipeline_design.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Stale ownership scan returned no residual wording that assigns `feature_spec` generation to train Pipeline or inner-Agent feature selection.
- Expected ownership wording is present: outer Agent proposes `feature_spec` / factor definitions, Pipeline validates and freezes them, and inner Agent cannot add new factor definitions.
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the three design docs and logbooks.

## 2026-06-06 Multi-domain history-window clarification

Task: clarify whether financial data, macro events, and other non-price data are also organized as time-window inputs.

Changes:
- `docs/environment_design.md` now explicitly states that `history_window` is not only a market-price sequence; it contains dense market series, stock-level sparse event streams, market-level macro/global context, and text evidence indexes.
- `docs/pipeline_design.md` now says observation construction from `history_window` covers price/volume sequences, stock-level events, macro/global context, and text evidence.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Environment and Pipeline docs.
- `git diff --check` passed for the changed docs and logbooks.
- Target wording scan found the new dense-series, sparse-event, macro/global, and text-evidence window definitions.

## 2026-06-06 Text evidence and case-library boundary

Task: separate the as-of text evidence library from the post-trial Case Library.

Changes:
- `docs/environment_design.md` now defines snapshot `text_evidence` as a local as-of text library containing only visible texts within the requested history window.
- `docs/agent_design.md` now says LLM Agent can query that local library through whitelist keyword/BM25 tools, and every retrieved item must carry evidence/source hashes.
- `docs/pipeline_design.md` now inserts the text retrieval step before EvidencePackBuilder and adds a Case Library schema for post-trial lessons.
- Clarified that Case Library is for outer-Agent Template learning and is gated by `case_available_at <= outer_agent_decision_time`; it is not the raw text/evidence database for a decision date.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the changed docs and logbooks.
- Target wording scan found the as-of text library, whitelist keyword/BM25 retrieval, evidence ids, and Case Library time-boundary definitions.

## 2026-06-07 Template handoff contract and case

Task: define what Template objects move between outer Agent, Pipeline, Environment, inner Agent, and test sandbox, and provide a concrete example.

Changes:
- `docs/agent_design.md` now defines the handoff objects: `TemplateCandidateBundle`, `FrozenTemplateBundle`, `TemplateExecutionSpec`, `CandidateInstance`, and `FrozenInstance`.
- `docs/environment_design.md` now states that Environment only executes Pipeline-frozen `template_execution_spec`, not raw Agent free text or unreviewed template candidates.
- `docs/environment_design.md` and `docs/pipeline_design.md` now include `template_execution_spec_hash` in the freeze/audit chain.
- `docs/pipeline_design.md` now documents the handoff flow and adds concrete example `T_MOM_EARN_NEG_001`, covering a momentum/liquidity/profitability/text-risk template, Pipeline selector conversion, inner-Agent parameter selection, and frozen test execution.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
rg -n 'TemplateCandidateBundle|TemplateExecutionSpec|FrozenTemplateBundle|CandidateInstance|FrozenInstance|T_MOM_EARN_NEG_001|template_execution_spec_hash' docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
```

Results:
- Markdown table column check passed for Agent, Environment, and Pipeline docs.
- `git diff --check` passed for the changed docs and logbooks.
- Target wording scan found all handoff objects, the concrete `T_MOM_EARN_NEG_001` case, and `template_execution_spec_hash`.

## 2026-06-07 Environment documentation readability rewrite

Task: reduce redundancy, repeated boundaries, and English-heavy terminology in `docs/environment_design.md`.

SubAgent audit:
- Started SubAgent `Newton` for read-only audit.
- Main findings: Section 3.3 mixed history window, text evidence, Case Library, Agent permissions, and execution contracts; selector/universe sections repeated the same `available_at` rules; Data Gateway/Sandbox repeated logical visibility rules already stated earlier; execution/replay/evaluation sections were split into too many small sections; terminology density was too high.
- SubAgent was closed after returning the audit.

Changes:
- Rewrote `docs/environment_design.md` into 6 top-level chapters: environment responsibilities, time wall and history window, selectors and universe, replay/execution/evaluation, Data Gateway/snapshot/sandbox, and acceptance checklist.
- Reduced the document from 738 lines to 458 lines.
- Kept code variable names such as `history_window`, `decision_observation`, `available_at`, `selector`, `FreezeSpec`, and `template_execution_spec_hash`, but made the surrounding prose mostly plain Chinese.
- Removed repeated Agent/Pipeline handoff explanation from Environment and left only the execution boundary: Environment executes Pipeline-frozen contracts and fails fast on missing/unauthorized/unverifiable inputs.
- Kept Data Gateway and Sandbox as chapter 5 so existing Agent/Pipeline cross-references remain valid.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
wc -l docs/environment_design.md
rg -n '^#{1,4} ' docs/environment_design.md
rg -n 'environment_design\\.md.*第|docs/environment_design.md` 第' docs/agent_design.md docs/pipeline_design.md docs/data_documentation.md docs/QMT_documentation.md AGENTS.md CLAUDE.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/environment_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
```

Results:
- `docs/environment_design.md` is now 458 lines.
- Heading scan shows 6 top-level chapters with compact subsection structure.
- Agent/Pipeline references to `docs/environment_design.md` chapter 5 remain valid because chapter 5 is still Data Gateway/snapshot/sandbox.
- Markdown table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and `docs/logbook/DETAILED_LOGBOOK.md`.

## 2026-06-07 Data/Agent/Pipeline documentation readability rewrite

Task: audit and reduce redundancy, repeated cross-layer explanations, and unexplained terminology in Data, Agent, and Pipeline living docs.

SubAgent audits:
- `Harvey` audited `docs/data_documentation.md`; finding: content complete but mixed data dictionary, operation manual, audit implementation, and risk memo. Recommended terminology glossary, merge raw/PIT contracts, compress audit details into tables, and keep strict data contracts.
- `Curie` audited `docs/agent_design.md`; finding: boundaries correct but repeated Agent role and Template/Instance constraints. Recommended moving handoff mechanics to Pipeline and keeping Agent semantics, Evidence/Case, LLM logging, and trading-impact boundaries.
- `Beauvoir` audited `docs/pipeline_design.md`; finding: core boundaries correct but it read like a combined Agent/Environment/Pipeline document. Recommended preserving orchestration/freeze/ledger and shortening history window, evidence, event-action, and Template handoff details.
- All three SubAgents were closed after returning read-only audits.

Changes:
- Rewrote `docs/data_documentation.md` from 762 to 484 lines.
- Rewrote `docs/agent_design.md` from 502 to 313 lines.
- Rewrote `docs/pipeline_design.md` from 752 to 346 lines.
- Added short terminology sections where helpful, while keeping code variable names.
- Preserved strict boundaries:
  - Data owns raw downloads, units, updates, status, revision ledger, and raw PIT support.
  - Agent owns Template/Instance semantics, Evidence/Case boundaries, LLM calls/logs, and trading proposal boundaries.
  - Pipeline owns orchestration, history-window entrypoints, WFO/held-out, freeze, ledger, and Agent handoff flow.
  - Environment owns time visibility, selectors, execution, replay, snapshots, and sandbox permissions.
- No code, data, cron jobs, live API calls, commits, or PR operations were run.

Validation:

```bash
wc -l docs/data_documentation.md docs/agent_design.md docs/pipeline_design.md docs/environment_design.md docs/QMT_documentation.md
rg -n '^#{1,4} ' docs/data_documentation.md docs/agent_design.md docs/pipeline_design.md
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [
    Path('docs/data_documentation.md'),
    Path('docs/agent_design.md'),
    Path('docs/pipeline_design.md'),
    Path('docs/environment_design.md'),
    Path('docs/QMT_documentation.md'),
]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for item in problems:
        print(item)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/data_documentation.md docs/agent_design.md docs/pipeline_design.md docs/environment_design.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md
rg -n 'docs/(data_documentation|agent_design|environment_design|pipeline_design)\\.md` 第|docs/(data_documentation|agent_design|environment_design|pipeline_design)\\.md.*第' docs/*.md AGENTS.md CLAUDE.md
```

Results:
- Final global documentation audit SubAgent `Euclid` found no blocking issue and was closed.
- Applied its QMT follow-up: refreshed整理日期, added a compact glossary for PIT/WFO/LLM shadow/ledger/payload/dry-run, and replaced the versioned sample strategy id with a semantic id.
- Final validation passed: five-doc table column check, `git diff --check`, stale QMT keyword scan, and resource checks.

## 2026-06-07 Tool Gateway, sandbox, and inner-Agent handoff documentation

Task: supplement the Agent/Environment/Pipeline design docs with concrete tool, sandbox, Python runtime, LLM proxy, and inner-Agent handoff contracts without moving tool details into the Data documentation.

Changes:
- `docs/environment_design.md`
  - Added Environment-owned Tool Gateway.
  - Added the default local Docker sandbox boundary.
  - Required gVisor/runsc for Agent/LLM generated or otherwise unreviewed Python code.
  - Defined the frozen Python image contract, read-only snapshot mount, write-only artifact mount, resource limits, and `sandbox_manifest.json`.
  - Defined the unique LLM call chain: `Agent or sandbox -> llm_proxy_tool -> host-side LLM proxy -> provider`.
  - Clarified that API keys are read only by the host-side proxy and never enter Agent, Pipeline, sandbox artifact, or conversation log.
- `docs/agent_design.md`
  - Expanded the inner-Agent `Candidate Instance` handoff with `train_snapshot_id`, `snapshot_manifest_hash`, `template_hash`, `template_execution_spec_hash`, `seed`, `search_budget`, `tool_call_manifest`, and `failure_notes`.
  - Split action semantics into `TradeAction`, `ActionProposal`, and `ResearchOnlyAction`.
  - Clarified that inner Agent can only read visible training case metadata, not Case Library items as historical evidence.
- `docs/pipeline_design.md`
  - Added `SandboxRunSpec` and phase-level `tool_policy_id` dispatch.
  - Removed duplicated concrete tool lists from Pipeline; Environment remains the single tool directory.
  - Clarified Pipeline only issues LLM proxy policy/budget and frozen prompt/model/settings, not API keys.

SubAgent audits:
- `Turing` found no Blocking issues but raised High findings on LLM key/proxy ownership and action taxonomy, plus Medium findings on tool-policy duplication, snapshot/artifact ambiguity, Candidate Instance provenance, and gVisor wording. All were fixed.
- `Nash` performed a final read-only audit after fixes. It found no Blocking or High issues, then raised Medium cleanup items on `SandboxRunSpec` wording, action proposal terminology in examples, and case metadata visibility. All were fixed, and `Nash` was closed.

Validation:

```bash
/home/lzp/miniconda3/envs/stock/bin/python - <<'PY'
from pathlib import Path
paths = [Path('docs/agent_design.md'), Path('docs/environment_design.md'), Path('docs/pipeline_design.md')]
problems = []
for path in paths:
    lines = path.read_text(encoding='utf-8').splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('|') and i + 1 < len(lines) and lines[i + 1].startswith('|'):
            marker = lines[i + 1].replace('|', '').replace('-', '').replace(':', '').strip()
            if marker == '':
                expected = line.count('|')
                j = i
                while j < len(lines) and lines[j].startswith('|'):
                    actual = lines[j].count('|')
                    if actual != expected:
                        problems.append((path.as_posix(), j + 1, expected, actual, lines[j]))
                    j += 1
                i = j
                continue
        i += 1
if problems:
    for p in problems:
        print(p)
    raise SystemExit(1)
print('markdown table column check ok')
PY
git diff --check -- docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
rg -n '允许工具列表|artifacts/|API key 只从环境变量|调 provider API|event_de_risk/inventory_trade|允许 `.*event_de_risk|历史 case 子集' docs/agent_design.md docs/environment_design.md docs/pipeline_design.md
```

Result: table check and `git diff --check` passed. Stale-conflict keyword scan had no remaining match after replacing the direct provider-call wording.

## 2026-06-07 Environment model-visible data case

Task: add a concrete Environment case showing which data a model can see inside a sandbox at a decision time, without duplicating Data documentation or Agent/Pipeline tool contracts.

Change:
- Added `docs/environment_design.md` section `2.4 模型可见数据 Case`.
- The case uses a daily test decision at `2024-06-28 20:30:00+08:00` for `2024-07-01`.
- It lists the visible boundaries for daily market data, financial/dividend data, event/flow data, macro/global context, text evidence, positions, and trading constraints.
- It now also includes a concrete row-level sample table for `000001.SZ`, covering example daily market rows, trade constraints, financial records, moneyflow, margin detail, macro context, text evidence, and position/constraint state. The sample values are explicitly documentation-only and should be replaced by real snapshot output after implementation.
- It explicitly blocks future data, held-out/test results leakage, full `data/raw` paths, unfiltered text/events, API keys, host shell, internet search, and unauthorized Python/SQL.
- It restates train vs test/held-out behavior: train can tune inside train snapshot; test/held-out can only execute frozen Instance.

Validation:
- SubAgent `Ptolemy` performed a read-only audit after the edit and found no Blocking or High issues.
- Follow-up fixes added `instance_hash`, `snapshot_manifest_hash`, `tool_policy_hash`, and `template_execution_spec_hash` to the case, clarified that the model means sandbox inner/test/frozen strategy code rather than host Environment/Pipeline, added a source-object column to visible data, normalized event/macro/text visibility to `available_at <= decision_time`, and added a train/test/held-out comparison table.
- Final validation passed: Environment table check, `git diff --check`, heading scan, and resource checks.
- SubAgent `Anscombe` later audited the concrete row-level table and found one High ambiguity: a涨跌停/停牌 sample under `history_window.daily` could be read as future `2024-07-01` `stk_limit` visibility. Fixed it by moving the example to `constraints`, adding `source_feature_date=2024-06-28`, and stating that future `stk_limit` is not read.
- Also fixed the row-level table to include explicit `available_at` and raw-unit fields, and changed the text sample from Agent-level `evidence_id` to snapshot-level `text_id/source_doc_id`.
- Follow-up cleanup changed the sample from single-row examples to window/state snippets: `history_window.daily`, `history_window.events`, `history_window.macro`, `history_window.text_evidence`, and `history_window.fundamentals` now show explicit `lookback` lengths or counts; raw-unit prose and extra explanatory phrases were removed from the visible table, and `constraints` is separated from `position_state`.

## 2026-06-07 Feature unit normalization documentation

Task: document that feature construction must normalize units before data reaches models, replay, or `decision_observation`, while raw TuShare data remains unchanged.

Changes:
- Added `docs/environment_design.md` section `2.5 特征单位统一`.
- Documented canonical feature units:
  - money -> yuan with `_yuan` suffix.
  - volume -> shares with `_shares` suffix.
  - percentage points -> decimal with explicit return/rate names.
  - bps fields remain bps with `_bps` suffix.
- Added a raw-to-feature conversion table for `daily.amount`, `daily.vol`, `stk_mins.amount`, `stk_mins.vol`, `moneyflow.net_mf_amount`, `margin_detail.rzye`, `daily_basic.total_mv`, `daily_basic.total_share`, `pct_chg`, and `turnover_rate`.
- Added required `feature_manifest` / `observation_manifest` fields: feature field, source field, source unit, transform, feature unit, available-at rule, and source hash.
- Added a cross-reference in `docs/data_documentation.md` clarifying that Data records and audits raw units, while Environment performs feature-unit normalization.

Validation:
- Environment/Data table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `docs/data_documentation.md`, `LOGBOOK.md`, and this detailed logbook.
- Heading scan confirmed the Environment navigation includes `2.5 特征单位统一`.

## 2026-06-07 Tool Gateway input/output examples

Task: add a concrete example of callable tools with standard input and output shape.

Changes:
- Added examples to `docs/environment_design.md` under `5.3 工具网关`.
- Defined a common tool-call input envelope containing `tool_call_id`, `tool`, `phase`, `snapshot_id`, `decision_time`, `tradable_date`, `tool_policy_id`, `tool_policy_hash`, budget, and tool-specific `input`.
- Defined a common output envelope containing status, `tool_call_hash`, `output_schema_hash`, artifact path, and warnings.
- Added a `data_query_tool` example for `history_window.daily` returning normalized units such as `amount_yuan` and `ret_1d`.
- Added a `keyword_search_tool` example returning snapshot-level `text_id` / `source_doc_id`, `available_at`, snippet, hash, and artifact path.

SubAgent audit:
- `Poincare` performed a read-only audit and found no Blocking issue, but raised High findings on incomplete envelope/hash fields, partial tool examples that could look like bypass calls, and missing `available_at` filtering language for text search.
- Fixed by making both tool examples full request/response envelopes and adding `snapshot_manifest_hash`, `input_schema_hash`, `output_schema_hash`, `artifact_hash`, lineage metadata, `available_at_lte`, `available_at_max`, and explicit `error` fields.
- Clarified that Tool Gateway first filters text by `available_at <= decision_time`, then applies publish-time query filters.
- Updated model-visible sample rows to use normalized feature fields such as `amount_yuan`, `net_mf_amount_yuan`, and `margin_balance_yuan`; raw units now appear only in lineage.

Validation:
- Environment table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `LOGBOOK.md`, and this detailed logbook.
- Keyword scan confirmed the tool examples now include `snapshot_manifest_hash`, `input_schema_hash`, `output_schema_hash`, `artifact_hash`, `available_at_lte`, and explicit Tool Gateway `available_at` filtering language.

## 2026-06-07 Tool Gateway simplification

Task: simplify Tool Gateway documentation after the user noted that too many tools and full JSON examples made the document hard to read.

SubAgent audit:
- `Halley` performed a read-only audit and found no Blocking issue.
- Main finding: adding full JSON examples for every missing tool would make the living doc a schema manual.
- Recommendation: keep one compact request/result envelope, reduce top-level tools, and describe per-tool differences in a table.

Changes:
- Replaced the 9 granular top-level tools with 4 stable tools:
  - `data_access_tool` with `query` and `search_text` modes.
  - `compute_tool` with `python`, `factor`, and `optimize` modes.
  - `replay_tool` with `backtest`, `event_check`, and `order_sim` modes.
  - `llm_proxy_tool` with `complete_json` mode.
- Removed long inline JSON request/response examples from `docs/environment_design.md`.
- Added compact request and result envelope field tables.
- Added a tool-specific I/O delta table that covers data access, text search, Python analysis, factor compute, optimizer, backtest, event check, order simulation, and LLM proxy calls.
- Clarified that `outer_review/post_review` use `context_manifest_id/hash`, while train/test/held-out require `snapshot_id/hash`.
- Updated Agent example tool call from `backtest_tool` to `replay_tool` with `mode=backtest`.

Validation:
- Agent/Environment table column check passed.
- `git diff --check` passed for `docs/environment_design.md`, `docs/agent_design.md`, `LOGBOOK.md`, and this detailed logbook.
- Legacy granular tool-name scan found no remaining `data_query_tool`, `keyword_search_tool`, `python_analysis_tool`, `factor_compute_tool`, `optimizer_tool`, `backtest_tool`, `event_check_tool`, or `order_sim_tool` references in the living docs.

## 2026-06-07 Dynamic window and factor-code execution documentation

Task: update Agent/Environment/Pipeline design docs so historical window length is not a single preconfigured value, and so outer-Agent factor Python code can be run by sandbox inner Agents through controlled tools.

Changes:
- `docs/environment_design.md`
  - Replaced the ambiguous `requested_lookback` wording with `max_lookback`, `lookback_space`, `selected_lookback`, and `effective_lookback`.
  - Documented that `effective_lookback` is a Tool/Data Gateway manifest result, not a strategy decision field.
  - Updated the model-visible data example to show `selected_lookback` windows.
  - Added the factor-code execution path: Pipeline saves `factor_code_artifact`; Data Gateway builds `history_window_artifact`; `compute_tool.python/factor` runs registered code only on that artifact.
- `docs/agent_design.md`
  - Clarified that the outer Agent proposes `lookback_space`, input columns, and factor Python code.
  - Clarified that the inner Agent may select `selected_lookback` only from the outer Template's `lookback_space`, and cannot generate or replace factor code.
  - Updated Candidate Instance fields and example from `selected_windows` to `selected_lookback`.
- `docs/pipeline_design.md`
  - Added the freeze flow for `factor_code_artifact`, `factor_code_hashes`, `lookback_space`, and `selected_lookback`.
  - Clarified that train selects `selected_lookback`, while test/held-out execute only frozen code and frozen windows.

SubAgent audits:
- `Ramanujan` found no Blocking issue, but raised High concerns that `requested_lookback` was overloaded and `compute_tool` data inputs were under-specified. Both were fixed.
- `Archimedes` performed the final read-only readability review after the fix and found no Blocking or High findings. Its Low suggestions were folded in: `selected_lookback` is now described as the Pipeline-frozen final execution window, and the primary compute path is `Data Gateway -> history_window_artifact -> compute_tool`.

Validation:
- Agent/Environment/Pipeline table column check passed.
- `git diff --check` passed for the three docs.
- Keyword scan confirmed `requested_lookback`, `requested_window`, and `selected_windows` no longer remain in the three design docs.

## 2026-06-07 Single-Agent Step/Fold/Epoch redesign

Task: replace the double-layer Agent design with a simpler per-Fold Agent session design.

Design changes:
- Rewrote `docs/agent_design.md`.
  - Each Fold starts a new Agent conversation.
  - Fold-to-Fold sharing is limited to strategy artifacts: factor code and global experience.
  - Previous Fold messages, tool logs, text subtask logs and `results/test_*` outputs cannot enter the next Fold prompt or strategy artifact.
  - Agent can write Python factor code inside Sandbox and call controlled tools.
  - Modification budgets are machine-auditable with fields such as `max_modified_functions_per_fold`, `max_diff_lines_per_fold`, and `max_experience_changes_per_fold`.
  - Epoch regularization can only delete, merge, and abstract rules; it cannot read Fold test results or held-out.
- Rewrote `docs/environment_design.md`.
  - Environment prepares PIT windows under `/mnt/snapshot` and run artifacts under `/mnt/artifacts`.
  - Ordinary `python_tool` has no network and cannot access LLM proxy.
  - Only registered LLM tool calls can access host-side LLM proxy.
  - Paths and `decision_time` come from run manifest, not Agent-provided absolute paths.
  - `nl_analysis_tool` examples use artifact IDs rather than absolute candidate paths.
  - Freeze manifest includes provider/model/settings/token budget and text retrieval config when LLM is used.
- Rewrote `docs/pipeline_design.md`.
  - Main loop is Step -> Fold -> Epoch.
  - `fold_202101` example trains on 2020-12 with only data visible before the first December trading day, then tests on 2021-01 with frozen strategy artifacts.
  - Each Fold creates a new `conversation_id` and Agent session.
  - Next Fold inherits only the strategy artifact frozen before the previous Fold test.
  - Fold test results are written to ledgers only; they cannot enter later prompts, strategy artifacts, or Epoch regularization.
  - Held-out range must be frozen before the experiment and must not overlap 2021-01 to 2025-12 development.

SubAgent audits:
- `Avicenna` audited Agent docs and initially found one High risk around Epoch overfitting plus Medium issues in modification budget and LLM logs. Fixed by forbidding test-result regularization, making regularization delete/merge/abstract only, and adding machine-auditable modification/log fields.
- `Boyle` audited Environment docs and found High issues around LLM proxy access and path/time trust. Fixed by making `python_tool` networkless, limiting proxy access to registered LLM tool calls, and moving paths/time to run manifest.
- `Herschel` audited Pipeline docs and found High leakage risks from inheriting previous Fold test summaries and letting Epoch regularization read Fold test results. Fixed by inheriting only pre-test frozen strategy artifacts and forbidding Fold test results in regularization.
- Final复审 by `Mendel`, `Averroes`, and `Galileo` found no Blocking/High issues. Their Medium suggestions were folded in: Step outputs now show before/after hashes and diff metadata; `nl_analysis_tool` uses artifact IDs; each Fold gets a new `conversation_id`; regularization uses a whitelist manifest.

Validation:
- Agent/Environment/Pipeline table column check passed.
- `git diff --check` passed for the three rewritten docs and logbooks.
- Keyword scan found no `外层`, `内层`, `双层`, `Template`, `Instance`, `agent_state`, `state_tool`, `requested_lookback`, `lookback_space`, or `selected_lookback` residues in the three redesigned docs.

## 2026-06-07 Trade-list boundary cleanup

Task: simplify the single-Agent tool boundary after deciding that a separate factor-computation tool is unnecessary when Agent can write and run Python inside Sandbox.

Changes:
- `docs/environment_design.md`
  - Removed the standalone `factor_tool` from the tool list.
  - Kept `python_tool` as the only code-execution path for Agent-written strategy code.
  - Added `trade_list_tool`, whose only job is to validate Agent-produced candidate/trade-list artifacts before replay.
  - Changed replay input to consume a `validated_trade_list_artifact_id`.
- `docs/agent_design.md`
  - Reworded Agent responsibilities so Agent writes strategy code and outputs candidate/trade lists directly.
  - Changed the example entrypoint from `compute_factors()` to `build_trade_list()`.
  - Added `trade_list_hash` to Step output.
- `docs/pipeline_design.md`
  - Changed Step/test flow so Pipeline validates the final trade list before backtest.

Boundary:
- Agent code may compute factors, rank stocks, apply text scores, and create target weights.
- Environment does not compute or choose factors.
- Environment validates schema, tradability, weights, evidence references, PIT boundaries, and then runs replay/backtest.

Validation:
- Follow-up checks were run after the edit; see the final assistant response for exact commands and results.

## 2026-06-07 Simulated Broker boundary

Task: clarify that Environment can mimic the QMT execution environment while keeping research Sandboxes isolated from real trading.

Changes:
- `docs/environment_design.md`
  - Reframed `backtest_tool` as a QMT-like simulated Broker/replay tool.
  - Added simulated Broker interfaces: account query, position query, submit order, cancel order, and order query.
  - Added structured order fields and explicit accepted/rejected order logging.
  - Clarified that validated trade lists are the preferred audited input, while frozen test/held-out strategies may submit structured `orders_artifact_id` directly under the same constraints.
  - Added cash, position, tradability, limit, and A-share T+1 checks to the execution constraints.
- `docs/agent_design.md`
  - Clarified that Agent may output simulated orders, but cannot connect to real QMT or generate real orders.
- `docs/pipeline_design.md`
  - Updated train/test flow so validated lists or orders pass through simulated Broker before replay/backtest metrics are written.

Boundary:
- Agent owns strategy logic and proposed orders.
- Environment owns order validation, acceptance/rejection, fill simulation, positions, costs, and PnL.
- Test and held-out execute frozen code/prompt only; order submission is allowed only inside simulated replay.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Quarterly Fold and 9-month window policy

Task: update the design after deciding that the main data domains should use a unified visible window and that Fold cadence should move from monthly to quarterly.

Decision:
- Main PIT domains use a default 9-month visible window:
  - `daily`
  - `fundamentals`
  - `events`
  - `macro`
  - `text_index`
- `intraday_1min` uses the latest 5 trading days because minute data is heavy and mainly serves intraday, auction, and board-trading studies.
- Pipeline rolls by quarter:
  - 9-month input window.
  - Next quarter validation, where Agent may iterate within the modification budget.
  - Following quarter frozen test, where code and experience cannot change.

Example:
- `fold_2021Q1`
  - Input: 2020-01 to 2020-09.
  - Validation: 2020Q4.
  - Test: 2021Q1.
- `fold_2021Q2`
  - Input: 2020-04 to 2020-12.
  - Validation: 2021Q1.
  - Test: 2021Q2.

Boundary:
- The same natural quarter may later become a validation replay window in a future Fold, but prior `results/test_*` directories, logs and Agent messages from that quarter must not be passed into Agent prompts or strategy artifacts.
- The 9-month rule is a maximum visible window. Agent code may use shorter slices inside the prepared data.

Changes:
- `docs/environment_design.md`: changed the window table and visible-data example.
- `docs/agent_design.md`: changed the model-visible window table.
- `docs/pipeline_design.md`: changed Fold timing, rolling examples, Step terminology, held-out cadence, and ledger wording from monthly training/test to quarterly validation/test.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 21-month visible window and 2022Q1 first test

Task: revise the just-added quarterly Fold policy so the main visible window is 21 months and the first validation/test schedule starts later.

Decision:
- Main PIT domains use a default 21-month visible window:
  - `daily`
  - `fundamentals`
  - `events`
  - `macro`
  - `text_index`
- `intraday_1min` remains latest 5 trading days.
- First Fold:
  - Visible input window: 2020-01 to 2021-09.
  - Validation interval: 2021-10 to 2021-12.
  - Frozen test quarter: 2022Q1.
- Subsequent Folds roll by natural quarter; the prior test quarter becomes the next validation interval.

Changes:
- `docs/environment_design.md`: changed main-domain window table and visible-data example to 21 months and the 2021-10 validation start.
- `docs/agent_design.md`: changed model-visible window table and output examples to `fold_2022Q1`.
- `docs/pipeline_design.md`: changed first Fold, rolling table, output example, Epoch start, and held-out overlap boundary to start at `fold_2022Q1`.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Visible-data intraday example

Task: fix `docs/environment_design.md` Section 2.4 after the visible-data example omitted minute data.

Change:
- Added `intraday_1min.parquet` to the model-visible data table.
- Documented that a pre-open decision sees only the latest 5 prior trading days of 1-minute bars.
- Documented that an intraday decision must truncate minute data by bar close time up to `decision_time`.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Environment-owned logging boundary

Task: clarify that trusted runtime logs should be written by Environment, not by Agent.

Decision:
- Agent submits structured outputs, explanations, candidate/trade lists, and simulated orders.
- Trusted logs are generated automatically by Environment components:
  - Runner records execution inputs, outputs, exit code, stdout/stderr hash, and artifact hashes.
  - Tool Gateway records tool request/response envelopes and errors.
  - LLM Proxy records all provider messages, raw responses, parsed responses, usage, and errors.
  - Simulated Broker records accepted/rejected orders, fills, cancellations, positions, costs, and PnL.
- Agent-generated text can be stored as an artifact but cannot replace Environment logs.

Changes:
- `docs/agent_design.md`: removed the implication that Agent writes trusted Step logs; it now submits Step outputs while Environment records hashes and LLM calls.
- `docs/environment_design.md`: added the authoritative logging boundary and explicit Runner logging requirement.
- `docs/pipeline_design.md`: clarified that Pipeline validates Environment-generated logs and writes ledgers from them.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Artifact directory ownership

Task: clarify what each `/mnt/artifacts` directory is for and who writes it.

Changes:
- Added an artifacts ownership table to `docs/environment_design.md`.
- Clarified:
  - `factor_code/` is Agent-written strategy/因子代码.
  - `trade_list/` is Agent/tool-written candidate, trade-list, order, and validated-list output.
  - `nl_output/` is written by `nl_analysis_tool` and can be read by Agent during the current training Step.
  - `backtest/` is written by simulated Broker / `backtest_tool`; training Agent may read it, while test/held-out results are not fed back to Agent.
  - `logs/` is Environment-owned trusted audit output, not Agent-maintained strategy input.
- Clarified that `/mnt/artifacts` is a runtime mount and Environment/Pipeline collects it into a host experiment directory such as `experiments/artifacts/<run_id>/`.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Controlled debug shell boundary

Task: revise the Sandbox design after deciding Agent needs shell-like debugging ability.

Decision:
- Allow a controlled `debug_shell_tool` for training/validation debugging.
- The shell is not a host shell and not an unrestricted shell.
- It runs only inside the Sandbox container as a non-root user, with no sudo and no network.
- It can read `/mnt/snapshot` and read/write `/mnt/artifacts`; it must not access host paths.
- It has CPU, memory, process, output-size, and timeout limits.
- It records command, exit code, stdout/stderr hash, and transcript or transcript hash through Environment logs.
- Test and held-out keep debug shell disabled by default; failure复核 can run it read-only under explicit tool policy.

Rationale:
- A Linux user alone is not enough to define the safety boundary. User permissions only cover part of file access and do not fully constrain network, syscalls, process resources, mount behavior, `/proc` exposure, package installation, or container escape risk.
- The safe boundary is the combination of Sandbox runtime, mount policy, resource limits, no-network policy, non-root user, and Tool Gateway logging.

Changes:
- `docs/environment_design.md`: replaced the absolute "no shell" wording with a controlled debug shell contract and added `debug_shell_tool` to the tool table.
- `docs/agent_design.md`: documented that Agent may use `debug_shell_tool` in training but may not start shells outside the tool or access host paths.
- `docs/pipeline_design.md`: documented tool-policy gating and ledger/hash requirements for debug shell transcripts.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Sandbox runtime default and shell comparison

Task: revise the Sandbox runtime wording after deciding gVisor/runsc should not be required for v1, and clarify how `debug_shell_tool` differs from a regular non-root shell.

Changes:
- `docs/environment_design.md`
  - Set Docker as the default v1 runtime.
  - Reframed Docker + gVisor/runsc as an optional enhanced isolation runtime.
  - Added usage guidance: enable gVisor/runsc when code becomes freer, data sensitivity increases, experiments are broader, machines are shared, or security audit requirements increase.
  - Added a comparison table between ordinary non-root shell and `debug_shell_tool`.

Decision:
- A normal non-root user shell is a user identity plus OS file permissions.
- `debug_shell_tool` is a Tool Gateway mediated action with fixed Sandbox location, mount restrictions, no-network policy, resource limits, command policy, and automatic logging.
- v1 should keep runtime switchability but not make gVisor/runsc the default dependency.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Environment documentation readability audit

Task: run a SubAgent audit on `docs/environment_design.md` after the sandbox runtime/debug-shell changes, then optimize logic, readability, and terminology density.

SubAgent:
- Opened `Dirac` for read-only review.
- Closed it after completion.
- Main findings:
  - `debug_shell_tool` test/held-out read-only boundary conflicted with generic `/mnt/artifacts` writable wording.
  - `nl_analysis_tool` returned a log path even though `logs/` should not be Agent input.
  - Network/proxy mechanism needed a clearer boundary.
  - Artifacts were mixed into the PIT input chapter.
  - Text body storage conflicted with path restrictions.
  - Snapshot unit rules were underspecified.
  - Environment text-boundary wording conflicted with `nl_analysis_tool`.
  - gVisor wording still sounded like a versioned requirement.
  - Terms such as PIT, Sandbox, Runner, Tool Gateway, LLM Proxy, artifact, manifest, and held-out needed short explanations.
  - `python_tool` needed an explicit code artifact selector.

Changes:
- Added a terminology quick-reference table near the top of the Environment doc.
- Kept chapter 2 focused on PIT input/snapshot data.
- Moved `/mnt/artifacts` ownership into the Sandbox chapter.
- Added snapshot standard-unit rules and manifest expectations.
- Put optional text body files under `/mnt/snapshot/text_body/`; host-side text search returns only text ids, hashes, snippets, and metadata.
- Changed runtime wording: Docker is default; gVisor/runsc is optional enhanced isolation.
- Split `debug_shell_tool` permissions by phase and added `debug_review/` for read-only failure review outputs.
- Changed `nl_analysis_tool` output from a readable conversation log path to `conversation_log_id/hash`.
- Clarified that only Tool Gateway registered LLM tools can trigger host-side LLM Proxy calls.
- Added `code_artifact_id` to `python_tool` input and stated Runner only executes registered code artifacts.

Validation:
- Environment table column check passed.
- `git diff --check` passed for the touched docs/logbooks.
- Keyword scan found no stale `v1`, `conversation_log_path`, `logs/llm_conversations` path return, or old text-boundary phrasing in the Environment doc.

## 2026-06-07 Debug shell wording cleanup

Task: remove repeated debug shell details and clarify whether shell debugging changes model instructions.

Changes:
- `docs/agent_design.md`: replaced the repeated path/network/sudo details with a short reference to Environment as the authoritative `debug_shell_tool` contract.
- `docs/environment_design.md`: tightened the wording so `debug_shell_tool` is defined as a Tool Gateway mediated observation source, not a normal login shell.
- Clarified that shell output cannot override system prompts, tool policy, PIT time walls, or frozen execution rules.

Conclusion:
- `debug_shell_tool` only changes how Agent can inspect and debug files/code inside Sandbox.
- It does not change the model instruction hierarchy or grant additional strategy permissions.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Debug shell simplification

Task: simplify the Sandbox debug-shell design around a non-root Docker user instead of a complex permission model.

Changes:
- `docs/environment_design.md`: simplified the Sandbox runtime to local Docker with a non-root container user, no network, fixed mounts, resource limits, and automatic logs.
- Redefined `debug_shell_tool` as a logged/time-limited Sandbox shell running inside that container.
- Fixed the boundary to `/mnt/snapshot:ro` and `/mnt/artifacts:rw`; explicitly excluded host repo, host home, `data/raw`, API key files, and Docker socket mounts.
- Kept Sandbox networking disabled; ordinary Python and debug shell cannot directly call LLM providers.
- Condensed the comparison with ordinary non-root shell to four operational differences: read/write boundary, network/resources, audit, and model-instruction effect.
- Clarified that shell output is only a tool observation and cannot override system prompts, tool permissions, PIT time walls, or frozen strategy rules.

Conclusion:
- The design now matches the intended simple implementation: assign an Agent container user, disable network, expose only snapshot/artifacts mounts, and let Environment handle logs and resource limits.
- The current living design does not require an extra container runtime.

Validation:
- Follow-up documentation checks were run after the edit; see final assistant response for exact commands and results.

## 2026-06-07 Living-doc terminology pass

Task: add terminology explanations to every living doc and reduce unnecessary English jargon without translating core project terms.

Changes:
- `docs/data_documentation.md`: renamed the existing common-terms section to `术语说明`, kept raw/sidecar/status/revision-ledger/source-cap-risk as explained implementation terms, and changed some explanatory prose from English-heavy wording to Chinese.
- `docs/agent_design.md`: added `术语说明` and kept Agent, Sandbox, PIT, Step, Fold, Epoch, Held-out, and LLM analysis as primary terms.
- `docs/environment_design.md`: normalized the existing terminology table and kept Environment, Sandbox, Runner, Tool Gateway, LLM Proxy, artifact, manifest, and Held-out as primary terms.
- `docs/pipeline_design.md`: added `术语说明` and kept Pipeline, Step, Fold, Epoch, Held-out, Development, and ledger as primary terms.
- `docs/QMT_documentation.md`: added `术语说明` and kept PIT, WFO, LLM shadow, payload, dry-run, ledger, and state as primary terms.

Decision:
- Basic system terms remain in English/code form because they are used in code, logs, manifests, and cross-document references.
- The glossary explains meaning;正文只减少不必要的英文堆叠，不强行翻译基础术语.

Validation:
- Five-doc glossary/table check passed.
- `git diff --check` passed for the touched docs and logbooks.
- Keyword scan confirmed there are no remaining forced translations of Agent/Sandbox/Step/Fold/Epoch/Pipeline terms in Agent/Environment/Pipeline/QMT docs, aside from ordinary Chinese words used in explanations.

## 2026-06-07 Fold strategy-artifact handoff clarification

Task: clarify where previous-Fold factor logic and investment priors are passed into the next Fold, and simplify the naming.

Decision:
- Do not use a separate `/mnt/strategy_artifact` mount.
- Pipeline persists accepted/frozen strategy artifacts under `experiments/strategy_artifacts/<strategy_artifact_id>/`.
- Each artifact contains:
  - `manifest.json`
  - `factor/`
  - `nl_prior/`
- At the next Fold start, Pipeline validates hashes and initializes the Sandbox working copy directly under:
  - `/mnt/artifacts/factor/`
  - `/mnt/artifacts/nl_prior/`
- `factor/` contains factor logic, entrypoints, configs, and related code artifacts.
- `nl_prior/` contains transferable natural-language investment logic and risk-selection priors.

Changes:
- `docs/agent_design.md`: documented the persistent strategy artifact directory and renamed `factor_code` / `global_experience` concepts to `factor/` / `nl_prior/`.
- `docs/pipeline_design.md`: documented the exact previous-Fold handoff chain from `fold_ledger.frozen_strategy_artifact_id` to `experiments/strategy_artifacts/...` to `/mnt/artifacts/factor/` and `/mnt/artifacts/nl_prior/`.
- `docs/environment_design.md`: made `/mnt/artifacts/factor/` and `/mnt/artifacts/nl_prior/` the Sandbox working locations and removed the separate `/mnt/strategy_artifact` path.

Validation:
- `rg` found no remaining `factor_code`, `global_experience`, `/mnt/strategy_artifact`, `因子代码`, `全局经验`, or `投资经验` wording in Agent/Pipeline/Environment docs.
- Table column checks passed for Agent/Pipeline/Environment docs.
- `git diff --check` passed for the touched docs.

## 2026-06-07 Strategy modification constraints

Task: define how to prevent Agent from changing `factor/` and `nl_prior/` too much after the initial strategy artifact.

Design:
- Initial creation uses `is_initial_artifact=true` and separate initialization constraints.
- Every later Fold must reference `parent_strategy_artifact_id`.
- Agent may edit `/mnt/artifacts/factor/` and `/mnt/artifacts/nl_prior/` in the training Sandbox, but Pipeline only freezes the result if it passes strategy modification constraints.
- `factor/` constraints check changed files, changed registered functions, diff lines, new factor IDs, and deleted factor IDs.
- `nl_prior/` constraints check added/deleted/rewritten rules, total rule count, and maximum length per rule.
- `nl_prior/` should have a structured JSON authority with stable `prior_id`; Markdown can remain a human-readable view.
- Pipeline writes `strategy_artifact_diff.json` with parent/current hashes, constraints, actual modification usage, and pass/reject status.

Changes:
- `docs/agent_design.md`: added the modification-constraints contract and an example JSON constraint policy.
- `docs/pipeline_design.md`: added the acceptance gate before freezing a new `strategy_artifact`.
- `docs/environment_design.md`: documented that Environment must expose enough hashes, AST/function metadata, and structured `nl_prior` content for Pipeline to enforce modification constraints.

Validation:
- `rg` found the new constraint fields and no stale `max_modified*`, `max_experience*`, `global_experience`, or `factor_code` terms in Agent/Pipeline/Environment docs.
- Table column checks passed for Agent/Pipeline/Environment docs.
- `git diff --check` passed for touched docs.

## 2026-06-07 Pre-backtest modification-constraints gate

Task: clarify whether Environment can provide an interface that counts strategy changes and returns true/false before backtest.

Decision:
- Yes. Environment provides `strategy_artifact_tool.check_modification_constraints`.
- The tool compares the current `/mnt/artifacts/factor/` and `/mnt/artifacts/nl_prior/` against the parent strategy artifact and computes modification metrics.
- The constraints come from Pipeline/run manifest, not from Agent.
- The tool returns `allowed_to_backtest`.
- Pipeline must call it before `backtest_tool`.
- If `allowed_to_backtest=false`, the Step receives no backtest result; Agent must reduce the change and re-check.
- Pipeline remains the final gatekeeper. Environment computes facts and writes `strategy_artifact_diff.json`.

Changes:
- `docs/environment_design.md`: added `strategy_artifact_tool.check_modification_constraints` input/output contract and `allowed_to_backtest` behavior.
- `docs/pipeline_design.md`: moved the modification-constraints gate before backtest and added the false-return retry behavior.
- `docs/agent_design.md`: documented that Agent may use the check as preflight, but cannot self-attest budget compliance.

Validation:
- `rg` confirmed `strategy_artifact_tool.check_modification_constraints`, `allowed_to_backtest`, and `strategy_artifact_diff.json` are aligned across Agent/Pipeline/Environment docs.
- Table column checks passed for the three docs.
- `git diff --check` passed for touched docs.

## 2026-06-07 Environment responsibility wording

Task: adjust the Environment responsibility list after adding strategy modification constraints.

Change:
- `docs/environment_design.md`: changed the tool responsibility sentence from generic "natural-language analysis" to "controlled LLM text analysis" and added "strategy modification constraints".
- Clarified the strategy-artifact responsibility as controlled read/write, modification metrics, and hash audit.

Rationale:
- Environment should provide controlled tools and measurable checks.
- Environment should not be described as making free-form natural-language or investment judgments.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-08 Environment visible-window table cleanup

Task: reduce duplicated explanation between Environment section 2.2 window configuration and section 2.5 visible-data example.

Decision:
- Keep one integrated table in section 2.2.
- The table now covers data domain, snapshot file, default prepared window, the `2021-10-08 09:20:00+08:00` example content, and the PIT visibility boundary.
- Remove the separate section 2.5 visible-data example.
- Keep section 2.3 unit contract and section 2.4 snapshot path unchanged.

Changes:
- `docs/environment_design.md`: renamed section 2.2 to "可见数据窗口", merged default windows and the 2021Q4 validation example into one table, and removed the redundant section 2.5 table.

Validation:
- Follow-up searches and `git diff --check` were run after the edit; see final assistant response.

## 2026-06-08 Agent-facing Tool surface cleanup

Task: simplify Environment Tool exposure and remove Agent-controlled inputs from modification checks.

Decision:
- `nl_analysis_tool` is no longer documented as an Agent-facing Tool.
- Natural-language analysis remains a `backtest_tool` internal step that performs text retrieval, LLM Proxy calls, evidence binding and `nl_output/` writing.
- `modification_check_tool` is a no-business-argument trigger from the Agent perspective. Parent strategy artifact, `/mnt/artifacts`, initial-artifact flag, constraints, Fold ID and decision context are injected from run manifest.

Changes:
- `docs/environment_design.md`: removed `nl_analysis_tool` from the 4.1 Tool table, rewrote section 4.2 input semantics, and renamed the natural-language section to an internal `backtest_tool` step.
- `docs/agent_design.md`: replaced direct `nl_analysis_tool` wording with `backtest_tool` internal natural-language analysis and documented zero-business-argument modification checks.
- `docs/pipeline_design.md`: documented that Pipeline/Environment supply modification-check context from run manifest and that `backtest_tool` runs natural-language analysis internally.

Validation:
- Documentation consistency checks were run after the edit; see final assistant response.

## 2026-06-08 Sandbox artifact workspace boundary

Task: clarify that Agent cannot freely restructure `/mnt/artifacts`, while still allowing temporary code exploration.

Decision:
- `/mnt/artifacts` top-level directories are created and controlled by Environment.
- Agent freely writes temporary scripts, exploratory notebooks/code and scratch outputs under `/mnt/artifacts/workspace/`.
- Agent promotes only final strategy files into `/mnt/artifacts/factor/` and `/mnt/artifacts/nl_prior/` using their fixed file contracts.
- `workspace/` is not frozen, not replayed, and not included in `strategy_artifact_diff.json`.
- `modification_check_tool`, freezing and formal replay only inspect `factor/` and `nl_prior/`.

Changes:
- `docs/environment_design.md`: added `workspace/`, fixed directory ownership, Shell/apply_patch boundary, Python writing rule, and modification-check scope.
- `docs/agent_design.md`: changed Step flow so Agent explores in `workspace/` before writing final `factor/` / `nl_prior` outputs.
- `docs/pipeline_design.md`: documented that Pipeline freezes only `factor/` and `nl_prior/`, while `workspace/` remains temporary run output.

Validation:
- Follow-up doc searches and `git diff --check` were run after the edit; see final assistant response.

## 2026-06-08 Factor main entry contract

Task: remove the separate formal trade-list directory and define the factor strategy main-function interface.

Decision:
- Formal strategy code lives under `/mnt/artifacts/factor/`.
- The required entrypoint is `/mnt/artifacts/factor/main.py::generate_orders(context)`.
- `backtest_tool` constructs `context`; Agent cannot pass paths, dates or constraints into the formal call.
- `generate_orders(context)` returns a structured `pandas.DataFrame` with at least `ts_code`, `action`, `target_weight`, `score`, `reason` and `source_artifacts`.
- `backtest_tool` receives that return value in memory, runs internal natural-language analysis, normalizes weights, builds the order plan and writes replay artifacts under `/mnt/artifacts/backtest/`.
- No separate formal intermediate trade-list directory is needed.

Changes:
- `docs/environment_design.md`: added the strategy main-function contract, removed the separate intermediate directory, and changed backtest/Broker wording to order-plan artifacts.
- `docs/agent_design.md`: changed Agent output from maintaining a list file to writing `factor/main.py::generate_orders(context)`.
- `docs/pipeline_design.md`: changed Step, validation and test wording to main-function return values and order-plan validation.

Validation:
- Documentation searches and `git diff --check` were run after the edit; see final assistant response.

## 2026-06-08 Artifact layer split

Task: add one layer above Agent formal outputs and one layer above backtest results, with clear write permissions.

Decision:
- `/mnt/artifacts/workspace/` remains the Agent scratch area.
- `/mnt/artifacts/agent_output/` is the formal Agent output root.
- `/mnt/artifacts/agent_output/factor/` contains `main.py::generate_orders(context)` and related strategy code.
- `/mnt/artifacts/agent_output/nl_prior/` contains the formal natural-language investment prior.
- `/mnt/artifacts/results/` is the `backtest_tool` result root.
- Every `backtest_tool` call writes one new `results/<phase>_<idx>/` directory, such as `valid_000`, `valid_001`, `test_000`, or `heldout_000`.
- A result directory contains `summary.json`, `detailed_return.json`, `order_plan.parquet`, `nl_output/` and any replay details.
- Agent can write `workspace/` and `agent_output/`. Agent cannot write `results/`; it can only read training/validation results. Test and held-out results are not returned to Agent.

Changes:
- `docs/environment_design.md`: updated artifact tree, directory ownership, Shell/apply_patch permissions, modification-check scope, `backtest_tool` input/output examples and runtime file table.
- `docs/agent_design.md`: updated Sandbox example, Step flow and tool boundaries to use `agent_output/` and read-only `results/`.
- `docs/pipeline_design.md`: updated strategy-artifact handoff, modification diff scope, Step execution, test execution and regularization inputs.

Validation:
- Follow-up path searches and `git diff --check` were run after the edit; see final assistant response.

## 2026-06-08 Hash granularity simplification

Task: reduce logging/hash complexity after deciding that per Shell/Python input-output hashes are too heavy.

Decision:
- Experiment ID is an index and grouping key, not an integrity proof.
- Keep aggregate hashes or versions only at important boundaries: frozen strategy artifact, snapshot manifest, and backtest result.
- Ordinary Shell/Python calls do not need input/output/code hashes.
- Shell/Python calls should record command, exit code, stdout/stderr, transcript path, script path and artifact paths.
- If future reproducibility audits show this is insufficient, add finer-grained hashes later.

Changes:
- `docs/environment_design.md`: replaced per-call hash requirements with run/Fold manifest and key artifact version wording.
- `docs/agent_design.md`: simplified Step output and LLM logging examples away from input/output hash fields.
- `docs/pipeline_design.md`: replaced the “must record hashes” list with a “version and integrity record” section.

Validation:
- Searched living docs for remaining per Shell/Python input-output hash requirements; none remained.

## 2026-06-08 Logging-doc responsibility split

Task: simplify repeated logging/audit descriptions across Agent, Environment and Pipeline docs.

Decision:
- `docs/pipeline_design.md` is the single experiment-level ledger contract.
- `docs/environment_design.md` only lists the runtime files a Sandbox run writes.
- `docs/agent_design.md` only states the Agent boundary: Agent does not write trusted logs and cannot bypass Environment/LLM Proxy logging.
- Data and QMT logging remain scoped to data operations and live trading, not Agent research experiments.

Changes:
- Removed the detailed LLM log-field table from `docs/agent_design.md`.
- Compressed `docs/environment_design.md` section 7 into a short runtime-file table.
- Added an explicit authority sentence to `docs/pipeline_design.md` section 7.

Validation:
- Follow-up scans confirmed the old Agent-side LLM log schema table was removed and Pipeline now owns the experiment-level logging contract.

## 2026-06-08 Backtest-owned NL and hidden test snapshot

Task: update the design so natural-language analysis is part of formal backtest execution, and test snapshots can live in the Sandbox without being readable by the Agent user.

Decision:
- Formal validation and test results only come from `backtest_tool`.
- `backtest_tool` automatically loads `factor/` and `nl_prior/`, runs factor code, invokes internal `nl_analysis_tool`, validates trade lists/orders, and executes simulated Broker replay.
- Agent may use Shell/Python for exploration and debugging, but those temporary results are not official backtest results.
- Test snapshots may be mounted in the same Sandbox as a root-only path such as `/mnt/test_snapshot`.
- Agent user and `sandbox_shell_tool` cannot read or list the test snapshot path.
- After exploration ends, Runner/root freezes the strategy artifact, executes test replay through `backtest_tool`, writes test results, and ends the Fold.

Changes:
- `docs/environment_design.md`: documented root-only test snapshot mounting and rewrote `backtest_tool` as the formal replay executor with internal NL analysis.
- `docs/agent_design.md`: changed Step flow so Agent calls `backtest_tool` for formal validation and cannot read root-only test snapshots.
- `docs/pipeline_design.md`: changed validation/test flow so Runner/root executes frozen test replay through `backtest_tool`.

Validation:
- Follow-up scans checked the new `backtest_tool`/`nl_analysis_tool` and `/mnt/test_snapshot` wording across Agent, Environment and Pipeline docs.

## 2026-06-07 Sandbox shell consolidation

Task: simplify the Agent-visible execution interface after deciding that a Shell-capable Sandbox can read files, write Python, run code and debug without separate Python or strategy-artifact tools.

Decision:
- Agent-visible general execution uses `sandbox_shell_tool`.
- Agent can create files, inspect `/mnt/snapshot`, write `/mnt/artifacts`, run Python, inspect stderr and search the local text library through Shell/Python.
- There is no separate Agent-facing Python execution tool or strategy-artifact read/write tool.
- Strategy modification checks, trade-list validation and simulated Broker/replay are Environment services, not Agent tools.
- LLM provider calls remain behind `llm_proxy_call` so the host side can hide API keys and record full conversation logs.

Changes:
- `docs/environment_design.md`: renamed the tool chapter to execution entries and internal checks; kept `sandbox_shell_tool` plus `llm_proxy_call` as Agent-visible entries; moved strategy diff, trade-list validation and replay under Environment internal services.
- `docs/agent_design.md`: updated Agent responsibilities and Step flow to use Sandbox Shell for Python execution and LLM Proxy for natural-language analysis.
- `docs/pipeline_design.md`: replaced the old strategy-tool wording with Pipeline-scheduled Environment checks and renamed `tool_policy` to `execution_policy`.
- Updated log terminology from generic tool calls to Shell/LLM/service calls in the living docs.

Validation:
- Searched `docs/environment_design.md`, `docs/agent_design.md` and `docs/pipeline_design.md` for obsolete tool names; no matches remained for the removed Agent-facing tools.

## 2026-06-07 Agent Tool boundary correction

Task: correct the previous over-simplification of the Agent execution surface.

Decision:
- Keep `sandbox_shell_tool` as the local execution path for file inspection, code editing, Python execution and debugging.
- Allow common local commands inside the Sandbox, including `rg`, `sed`, Python and a restricted `apply_patch`.
- Keep three Agent-facing trusted Tools because they require Environment ownership, permission checks and durable logs:
  - `modification_check_tool` for strategy modification limits and `strategy_artifact_diff.json`.
  - `nl_analysis_tool` for text retrieval, LLM Proxy calls, evidence validation and conversation logs.
  - `backtest_tool` for trade-list validation, simulated Broker replay and validation/test metrics.
- Natural-language scoring must go through `nl_analysis_tool`; Agent cannot call provider APIs directly.

Changes:
- `docs/environment_design.md`: added `rg`/`sed`/restricted `apply_patch` to the Sandbox Shell boundary; restored `modification_check_tool`, `nl_analysis_tool` and `backtest_tool` as explicit trusted Tools; documented that `nl_analysis_tool` contains internal text retrieval.
- `docs/agent_design.md`: updated Step order so modification checking happens before natural-language analysis and backtest; documented local Shell commands and Tool boundaries.
- `docs/pipeline_design.md`: updated the gate so Pipeline reruns `modification_check_tool` before natural-language analysis and backtest.

Validation:
- Follow-up scans and `git diff --check` were run after the edit; see final assistant response.

## 2026-06-08 NL Prior iteration boundary

Task: clarify whether `nl_analysis_tool` can be called in every Step and whether it can be used to iterate `nl_prior`.

Decision:
- `nl_analysis_tool` can run in every training Step.
- The Tool only writes `nl_output/` and conversation logs; it cannot directly modify `nl_prior/`.
- Agent may use `nl_output/`, validation results and failure reasons to edit `nl_prior/` through the Sandbox Shell.
- The normal path is to let those edits become the next Step's starting prior.
- If Agent wants a post-analysis `nl_prior` change to affect the current Step, Pipeline must rerun `modification_check_tool` and ensure `nl_prior`, `nl_output`, trade list and backtest manifest are consistent.

Changes:
- `docs/agent_design.md`: added a section on using natural-language analysis to iterate `nl_prior`.
- `docs/environment_design.md`: stated that `nl_analysis_tool` can run every training Step but cannot write `nl_prior/`.
- `docs/pipeline_design.md`: documented the manifest and rerun-check requirement when `nl_prior` changes after natural-language analysis.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Text library wording cleanup

Task: remove explicit negative raw-text mount wording from the current Environment design.

Changes:
- `docs/environment_design.md`: removed the sentence about not mounting a full raw text directory.
- `docs/environment_design.md`: changed the debug shell mount row to a positive boundary: Sandbox uses `/mnt/snapshot` and `/mnt/artifacts`, without listing raw paths.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Debug review directory cleanup

Task: remove the separate `debug_review/` artifact directory because `logs/` already covers trusted runtime and review output.

Changes:
- `docs/environment_design.md`: removed `/mnt/artifacts/debug_review/` from the artifact tree.
- `docs/environment_design.md`: removed the dedicated `debug_review/` ownership row.
- `docs/environment_design.md`: changed故障复核 output to write under `/mnt/artifacts/logs/`.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Experiment output path layering

Task: decide whether to add an intermediate directory under `experiments/strategy_artifacts/` for parallel experiments and per-Epoch history.

Decision:
- Use `experiments/<experiment_id>/` as the top-level isolation boundary for each experiment.
- Store strategy artifacts under `experiments/<experiment_id>/strategy_artifacts/<epoch_id>/<strategy_artifact_id>/`.
- Keep `epoch_id` as a full ID such as `epoch_001`; do not construct `epoch_<epoch_id>` from an already-prefixed value.
- Store ledgers, runtime artifacts, and reports under the same `experiments/<experiment_id>/` root.

Changes:
- `docs/agent_design.md`: updated the strategy artifact directory structure and manifest fields.
- `docs/environment_design.md`: updated the source strategy artifact path and collected runtime artifact path.
- `docs/pipeline_design.md`: updated Fold handoff, Fold output example, and experiment output tree.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Modification-constraint ownership wording

Task: clarify whether Pipeline or Environment owns strategy modification constraint checks, and remove the unclear `AST/函数信息` wording.

Decision:
- Environment computes facts: file hashes, code-structure summary, changed functions/registered factors, `nl_prior` structured diff, and `allowed_to_backtest` under Pipeline-provided constraints.
- Environment directly gates `backtest_tool`: `allowed_to_backtest=false` means the backtest tool refuses to run.
- Pipeline owns orchestration: it supplies constraints, records the Environment result, and decides whether to freeze the strategy artifact after validation.
- `AST/函数信息` is too implementation-heavy for living docs; use "代码结构摘要、函数/登记因子变更统计" instead.

Changes:
- `docs/environment_design.md`: rewrote the strategy artifact working-copy paragraph and tool description around diff-report generation and Environment-side backtest gating.
- `docs/pipeline_design.md`: clarified that Pipeline calls the Environment check, but Environment gates the backtest tool.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Modification-constraint simplicity

Task: avoid adding LLM judgment to modification constraints.

Decision:
- Keep `modification_constraints` as deterministic count checks.
- Count changed files, diff lines, changed functions/registered factors, `nl_prior` rule changes, total rules, and per-rule character length.
- Do not add an LLM judge for this gate.

Changes:
- `docs/agent_design.md`: kept the count-based constraint table and removed extra wording about LLM suggestions for this gate.
- `docs/environment_design.md`: described the check as using reproducible counts.
- `docs/pipeline_design.md`: described `allowed_to_backtest` as a deterministic count-check result.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Runtime deadline policy

Task: clarify whether runtime control should primarily be Fold wall-clock time, and define the validation early-stop target.

Decision:
- Fold wall-clock deadline is the primary runtime control.
- Step has no separate deadline; all Step attempts share the same Fold time window.
- Each Fold defaults to 20 minutes.
- Runner/Proxy should not remind Agent about remaining time while more than 5 minutes remain.
- When 5 minutes remain, Runner/Proxy issues one fixed finalization prompt asking for the current best `factor/` and `nl_prior`.
- At `fold_deadline_at`, Pipeline truncates the Fold and records timeout state.
- CPU, memory, disk, process count, and output size remain basic Docker guardrails to prevent one run from exhausting the machine before the deadline.
- Pipeline provides `fold_deadline_at`, `max_fold_minutes`, `per_tool_timeout_seconds`, and `finalize_before_deadline_seconds`.
- LLM Proxy must not start provider calls that cannot finish within the remaining deadline.
- If close to deadline, LLM Proxy may make one fixed best-effort finalization call.
- If a provider request is already in flight and stuck, the system can only timeout/cancel/drop it; it cannot inject a new prompt into that same request.
- Pipeline early stop uses validation results only. The first Epoch requires positive, valid Fold results; later Epochs require each Fold's validation score to beat the same Fold in the previous Epoch by `min_delta`, subject to risk and trade-list constraints.

Changes:
- `docs/environment_design.md`: changed runtime wording to deadline-first and documented LLM Proxy timeout/finalization behavior.
- `docs/pipeline_design.md`: added `fold_time_limit`, a 20-minute Fold example, 5-minute finalization behavior, and validation-only early-stop rules.
- Replaced living-doc "resource budget" wording with deadline/resource-guardrail wording.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 Environment readability audit cleanup

Task: audit `docs/environment_design.md` for redundancy, repeated expressions, and hard-to-read sections.

SubAgent:
- Opened read-only SubAgent `Plato` for `docs/environment_design.md`.
- `Plato` found no contradiction with the single-Agent, Fold deadline, and Sandbox design.
- Main findings were repeated logging rules, duplicated LLM Proxy/API text, verbose `debug_shell_tool` explanation, repeated PIT time-wall wording, and several unexplained technical terms.
- Closed `Plato` after completion.

Changes:
- Added short glossary entries for `hash`, `Broker`, `provider`, and `schema`.
- Pointed general log wording to the authoritative log contract in section 7.
- Compressed `debug_shell_tool` into one boundary table and kept the single sentence that it is a restricted Sandbox shell, not host shell.
- Changed the Runner deadline row to explicitly state Fold-only deadline, no Step timer, T-5 finalization prompt, and hard Fold cutoff.
- Replaced repeated PIT wording in tool sections with references to the section 2.1 time wall.
- Replaced `rubric` with "评分规则".
- Shortened Python, text search, and LLM analysis tool examples into compact requirement tables.
- Simplified section 6 into the LLM API security contract and conversation-log boundary; detailed file requirements remain in section 7.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-07 As-of text library Sandbox path

Task: decide the Sandbox path/name for visible text正文 and remove the host-side retrieval branch from current design.

Decision:
- Use `/mnt/snapshot/text_library/`.
- `text_library/` is the English directory name for "文本库".
- The directory is an as-of, read-only Sandbox mount, not a raw-data mount.
- It may contain正文 or正文片段, but only for texts visible at the current Fold and `decision_time`.
- `text_index.parquet` is the index and authority; `text_library/` contents must be referenced by the index.
- Do not use a host-side read-only text retrieval service in the current design.

Changes:
- `docs/environment_design.md`: replaced `text_body/` with `text_library/`, removed host-side retrieval wording, and made text search read from the mounted as-of library.
- `docs/agent_design.md`: changed natural-language analysis inputs to `text_index` / `text_library` and pointed retrieval to `/mnt/snapshot/text_library/`.

Validation:
- Follow-up doc checks were run after the edit; see final assistant response.

## 2026-06-08 Agent output seed templates

Task: provide basic files under the runtime `agent_output/factor/` and `agent_output/nl_prior/` contract so the Sandbox Agent sees the required input/output format in-place.

Decision:
- Keep the committed source templates under `configs/agent_output_template/`.
- Environment copies these files into `/mnt/artifacts/agent_output/` when creating the first strategy artifact.
- Later Folds inherit the frozen strategy artifact instead of reinitializing templates.
- Do not place mutable runtime artifact directories at the repository root.

Changes:
- Current seed set is `factor/README.md`, `factor/main.py`, `factor/factors.json`, `nl_prior/README.md`, and `nl_prior/prior.json`.
- `factor/main.py` provides a schema-valid `generate_orders(context)` entrypoint and output validation helper.
- `factor/factors.json` is the empty initial factor registry.
- `nl_prior/prior.json` is the only formal natural-language investment prior state.
- Agent, Environment, and Pipeline docs name `main.py`, `factors.json`, and `prior.json` as formal mutable files initialized in `agent_output/`; `README.md` files are read-only instructions.

Validation:
- Python compile, JSON parsing, documentation reference scan, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Factor registry template

Task: add a machine-readable factor registry so `modification_check_tool` can validate factor metadata and deterministically count new, deleted, and modified factor IDs.

Decision:
- Store the registry at `agent_output/factor/factors.json`.
- `new_factor_ids`, `deleted_factor_ids`, and `modified_factor_ids` are only derived from this registry.
- Format errors, duplicate IDs, missing parent/current registry, or unsynchronized code/registry changes must reject formal backtest.

Changes:
- Added `configs/agent_output_template/factor/factors.json` with empty `factors` and required-field metadata.
- Updated `configs/agent_output_template/factor/main.py` to point Agent to the registry.
- Updated Agent, Environment, and Pipeline docs to initialize, validate, compare, and freeze `factors.json` alongside `main.py`.

Validation:
- JSON parsing, Python syntax compile, reference scans, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Environment table readability cleanup

Task: make `docs/environment_design.md` section 4.2 easier to audit with tables, while keeping section 3.2 readable as prose.

Changes:
- Restored section 3.2 runtime artifact rules to concise prose after review.
- Kept section 4.2 modification-check rules as three tables: Tool boundary, `factors.json` format validation, and factor ID diff statistics.
- Kept the design semantics unchanged.

Validation:
- `git diff --check`, section rendering inspection, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 LLM proxy deadline wording cleanup

Task: remove Pipeline/Runner finalization behavior from the Environment LLM Proxy section.

Changes:
- Replaced the repeated `finalize_before_deadline_seconds` and fixed finalization prompt bullets in `docs/environment_design.md`.
- Kept only the provider-request timeout boundary in the LLM Proxy paragraph.
- Left Fold deadline and finalization control under Pipeline/Runner.

Validation:
- `git diff --check`, deadline wording search, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 LLM API path clarification

Task: clarify that Agent Runner can also call the local LLM Proxy, not only `backtest_tool`.

Changes:
- Updated `docs/environment_design.md` section 6.1 to list two allowed LLM API paths: Agent Runner main conversation and `backtest_tool` natural-language analysis.
- Updated `docs/agent_design.md` section 6 to state that Runner calls the host-side LLM Proxy for Agent main dialogue, while Sandbox Shell/Python cannot call providers directly.
- Preserved the API-key boundary: keys stay host-side and never enter Sandbox, prompt, artifact, or logs.

Validation:
- LLM path reference search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Conversation trace logging clarification

Task: clarify whether `execution_calls.jsonl` and `llm_conversations.jsonl` belong to the same conversation.

Decision:
- Treat them as two event streams under the same Agent session / conversation trace.
- Link both files with `experiment_id`, `epoch_id`, `fold_id`, `step_id`, `run_id`, `conversation_id`, `call_id`, and `parent_call_id`.
- Keep the files separate because execution events and full provider conversation records have different schemas and privacy requirements.

Changes:
- Updated `docs/environment_design.md` section 7 to define shared conversation trace IDs and the cross-reference from execution summary events to full LLM conversation records.
- Updated `docs/pipeline_design.md` ledger wording from `conversation_log` to `conversation_trace`.
- Updated `docs/agent_design.md` so Agent references conversation trace ID rather than managing separate log files.

Validation:
- Log reference search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Rolling validation/test boundary clarification

Task: clarify that the previous Fold's test calendar quarter can become the next Fold's validation interval.

Decision:
- The same calendar period may be re-used as validation in a later Fold.
- The later Fold must re-run `backtest_tool` and generate current validation results.
- Previous Fold `results/test_*` directories, `logs/` records and messages remain saved experiment records, but are not copied into the next Sandbox, prompt or strategy artifact.

Changes:
- Updated `docs/agent_design.md` time-wall, strategy-artifact handoff and forbidden-behavior sections with direct file-level wording.
- Updated `docs/pipeline_design.md` rolling handoff wording to use `results/test_*`, `logs/`, `results/valid_*` and Agent-message boundaries.
- Clarified that a fresh replay may produce the same numbers when strategy, data, config and seed are identical; the rule is about data flow isolation, not changing the result.

Validation:
- Boundary wording search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Strategy artifact ownership cleanup

Task: keep Agent docs focused on Agent-visible behavior and move host persistence details to Pipeline docs.

Decision:
- Agent docs should describe only the Sandbox-visible `agent_output/factor/` and `agent_output/nl_prior/` contract.
- Host paths, `strategy_artifact_id`, manifest fields, frozen state and cross-Fold copy rules are Pipeline responsibilities.

Changes:
- Removed the `experiments/<experiment_id>/strategy_artifacts/...` tree and manifest JSON example from `docs/agent_design.md`.
- Added `docs/pipeline_design.md` section 7.3 as the strategy-artifact manifest contract.
- Kept the data-flow rule that only frozen `factor/` and `nl_prior/` copy into the next Sandbox; prior `results/test_*`, `logs/` and messages do not.

Validation:
- Ownership wording search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Agent output README split

Task: separate read-only instructions from Agent-editable strategy outputs.

Decision:
- `prior.json` is the only formal natural-language investment logic artifact.
- Human-readable explanations should be generated from `prior.json` when needed, not maintained as a second strategy artifact.
- `factor/README.md` and `nl_prior/README.md` are read-only instruction files in the Sandbox.

Changes:
- Added `configs/agent_output_template/factor/README.md`.
- Added `configs/agent_output_template/nl_prior/README.md`.
- Removed `configs/agent_output_template/nl_prior/prior.md` from the current strategy-artifact contract.
- Updated Agent, Environment and Pipeline docs so the Step order is: explore/debug in `workspace/`, prepare final draft, write `factor/` and `nl_prior/`, run modification check, then run `backtest_tool`.

Validation:
- Template JSON parsing, README/prior wording search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Agent output JSON simplification

Task: make Agent-editable JSON files easier to modify and avoid mixing schema descriptions into formal strategy artifacts.

Decision:
- `factors.json` and `prior.json` should contain formal artifact data only.
- Field descriptions, allowed values and filled examples belong in read-only README files.
- Each JSON template keeps one blank row with all required keys so Agent can fill or copy it.

Changes:
- Removed embedded `factor_schema` from `configs/agent_output_template/factor/factors.json`.
- Removed embedded `rule_schema` from `configs/agent_output_template/nl_prior/prior.json`.
- Added field tables and filled examples to `factor/README.md` and `nl_prior/README.md`.
- Updated Agent and Environment docs to explain blank template rows: fully blank rows are treated as empty; partially filled incomplete rows must fail formal backtest.

Validation:
- JSON parsing, template reference search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 Baseline NL scoring prompt

Task: provide a runnable baseline for `backtest_tool` natural-language scoring.

Decision:
- Keep the default natural-language analysis prompt and scoring table in read-only `nl_prior/README.md`.
- `backtest_tool` should concatenate the fixed prompt contract, active `prior.json` rules, candidate data and as-of evidence.
- LLM output must be strict JSON and parsed as JSON; string search is not acceptable for score extraction.
- The baseline final score is `0.7 * factor_score_norm + 0.3 * nl_score`.

Changes:
- Added prompt templates, keyword-search workflow, local retrieval input/output examples, scoring table, strict JSON output schema and baseline score fusion rule to `configs/agent_output_template/nl_prior/README.md`.
- Updated Agent docs so natural-language analysis runs inside `backtest_tool`: generate `search_requests`, run local as-of text retrieval, optionally run one supplement retrieval round, then emit `nl_score`, `confidence`, `risk_tags`, `applied_prior_ids` and `evidence_ids` through JSON parsing.
- Updated Environment docs so `backtest_tool` performs keyword JSON parsing, local retrieval, final score JSON parsing, score-component recording and malformed-output failure handling unless run config explicitly defines another audited handling rule.
- Updated Pipeline docs so `nl_output/search_requests.jsonl`, `nl_output/evidence.jsonl` and `nl_output/scores.jsonl` are the structured natural-language output sources.

Validation:
- JSON parsing, prompt/scoring reference search, `git diff --check`, and resource checks were run after the edit; see final assistant response.

## 2026-06-08 PIT company context for natural-language scoring

Task: ensure LLM natural-language analysis knows what each candidate company does without leaking current or future company descriptions.

Decision:
- `backtest_tool` should build a PIT-safe `company_context` for each candidate before keyword generation.
- Historical WFO must not directly inject `stock_company.introduction` because it is a current company-introduction field without a reliable historical visible timestamp.
- `company_context` should prefer historical names, stock basics, industry membership, `fina_mainbz_vip` business segments and as-of text evidence. Missing context should lower confidence and broaden retrieval rather than letting the LLM guess.

Changes:
- Added `company_context` construction and prompt injection to `configs/agent_output_template/nl_prior/README.md`.
- Updated Agent docs so formal natural-language scoring includes company identity and business context before search.
- Updated Environment docs so `backtest_tool` writes `nl_output/company_context.jsonl` and uses it in keyword generation and final scoring.
- Updated Pipeline docs so `nl_output/company_context.jsonl` is part of each formal `backtest_tool` result.
- Updated Data docs to mark `stock_company.introduction` as a historical Prompt leakage risk unless an explicit visible time is assigned.

Validation:
- Ran JSON parsing for the editable templates, `company_context` reference search, `git diff --check`, and resource checks after the edit.

## 2026-06-08 Agent Step/test boundary

Task: remove ambiguity in Agent section 3.2 about whether Step output means running test mode.

Decision:
- A Step only runs `backtest_tool` validation mode and reads `results/valid_<idx>/`.
- Agent can summarize the Step and recommend acceptance, but cannot submit or run Fold test results.
- If Pipeline accepts a Step as the Fold strategy artifact, Pipeline freezes `agent_output/factor/` and `agent_output/nl_prior/`, then Runner/root calls `backtest_tool` test mode against the root-only test snapshot and ends the Fold.

Changes:
- Updated `docs/agent_design.md` section 3.2 to replace “提交 Step 输出” with a Step summary and explicit no-test-mode boundary.
- Updated `docs/pipeline_design.md` Step execution wording to say Agent calls validation mode.

Validation:
- Ran wording search for validation/test-mode boundaries, `git diff --check`, and resource checks after the edit.

## 2026-06-08 Step finish tool

Task: clarify how Agent actively ends a Step.

Decision:
- Agent ends a Step by calling no-argument `finish_step_tool`.
- The Tool writes `results/step_finish.json`, stops the current Step and locks writes.
- Pipeline then checks modification constraints, validation result consistency and whether to accept, reject, continue, or freeze.
- `backtest_tool` test mode remains Runner/root-only after Fold strategy freeze.

Changes:
- Updated `docs/agent_design.md` section 3.2 and 7.1 with no-argument `finish_step_tool` behavior and output fields.
- Updated `docs/pipeline_design.md` section 3.2 with Runner/Pipeline handling after `finish_step_tool`.
- Updated `docs/environment_design.md` Tool table to add `finish_step_tool`, state Agent can request only validation-mode backtests, and require test-mode requests to be rejected.

Validation:
- Ran wording search for `finish_step_tool` and test-mode boundaries, `git diff --check`, and resource checks after the edit.

## 2026-06-08 Strategy artifact and Environment readability simplification

Task: reduce schema clutter and make Environment/Agent artifact contracts easier to read.

Decision:
- `finish_step_tool` takes no Agent input; it is a direct Step-ending control interface.
- `factors.json` should only register active strategy factors, not workflow metadata.
- `prior.json` should only register reusable natural-language rules, not Fold history or reports.
- Environment docs should describe Tool purpose and outputs once, then delegate examples to the README templates.

Changes:
- Changed `configs/agent_output_template/factor/factors.json` to `{"factors": []}`.
- Changed `configs/agent_output_template/nl_prior/prior.json` to `{"rules": []}`.
- Rewrote `factor/README.md` with minimal factor fields: `id`, `enabled`, `function`, `description`, `lookback_days`, `direction`.
- Rewrote `nl_prior/README.md` into five readable parts: Agent writes rules, `backtest_tool` uses rules, company context, Prompt template, default score fusion.
- Updated Agent, Environment and Pipeline docs for the simplified JSON fields and no-argument `finish_step_tool`.
- Compressed Environment `modification_check_tool` output from a long JSON example into a short result table.

Validation:
- Ran JSON parsing, old-field wording search, `git diff --check`, and resource checks after the edit.

## 2026-06-08 Factor/NL score boundary

Task: clarify whether `factor/main.py` returns final orders or just candidate stocks and factor scores.

Decision:
- `agent_output/factor/main.py::generate_orders(context)` should return the candidate pool and factor-only score.
- `backtest_tool` owns natural-language scoring, score fusion, target-weight generation, order-plan validation and replay.
- `target_weight` and `action` may appear as optional hints, but they are not the formal final order plan.

Changes:
- Updated `configs/agent_output_template/factor/main.py` required columns to `ts_code`, `factor_score`, `reason`, and `source_artifacts`.
- Updated `configs/agent_output_template/factor/README.md` with the candidate-pool output contract.
- Updated `configs/agent_output_template/nl_prior/README.md`, Agent docs and Environment docs so the default fusion uses the factor-output score.
- Updated Environment docs so final `target_weight` and `final_score` are generated by `backtest_tool`, not by Agent factor code.

Validation:
- Ran Python compile, JSON parsing, wording search, `git diff --check`, resource checks, and removed the generated `__pycache__` after the edit.

## 2026-06-08 Environment/Agent/Pipeline boundary cleanup

Task: reduce duplicated and misplaced logic across Environment, Agent and Pipeline docs after the backtest-owned natural-language scoring redesign.

SubAgent audit:
- Opened read-only SubAgent `Mencius`.
- It found that `docs/agent_design.md` described too much `backtest_tool` internals and Pipeline gating, while `docs/environment_design.md` chapter 4 mixed Tool summary, modification checks, Python execution, natural-language scoring and order validation.
- It recommended keeping Agent focused on visible inputs, writable outputs and Step behavior; Environment focused on Sandbox, Tool contracts, backtest/Broker/LLM internals; Pipeline focused on Step/Fold/Epoch orchestration, freezing and ledger records.

Changes:
- Rewrote `docs/environment_design.md` chapter 4 into four contracts: Agent-visible Tool list, `modification_check_tool`, `generate_orders(context)` and `backtest_tool`.
- Moved natural-language scoring internals into `docs/environment_design.md` chapter 6, including `company_context`, search request flow, JSON score parsing, LLM Proxy and conversation log boundaries.
- Compressed `docs/agent_design.md` chapters 4-5 so Agent only maintains `prior.json`, reads validation `nl_output/`, calls `modification_check_tool`, and keeps formal strategy changes within the allowed boundary.
- Shortened `docs/pipeline_design.md` Step text so Pipeline schedules Environment checks and backtests instead of re-explaining diff and natural-language parsing implementation.
- Synchronized template wording in `configs/agent_output_template/factor/README.md` from natural-language analysis to natural-language scoring.

Validation:
- Ran wording searches for removed Agent internals and stale Tool names.
- Ran Python compile for `configs/agent_output_template/factor/main.py`.
- Parsed `configs/agent_output_template/factor/factors.json` and `configs/agent_output_template/nl_prior/prior.json`.
- Ran `git diff --check`.
- Removed generated `configs/agent_output_template/factor/__pycache__`.

## 2026-06-08 Environment Tool chapter layout correction

Task: correct the Environment chapter layout so chapter 4 remains a Tool chapter rather than a mix of Tool and standalone strategy-contract sections.

Decision:
- `generate_orders(context)` is not a separate top-level Environment chapter concept; it is the input sub-contract used by `backtest_tool`.
- Natural-language scoring is also a `backtest_tool` internal flow, so its detailed steps belong in chapter 4 under `backtest_tool`.
- Chapter 6 should only describe LLM API access and conversation-log boundaries.

Changes:
- Changed `docs/environment_design.md` chapter 4 to list Tool contracts and put strategy main-function details under `backtest_tool`.
- Moved company context, search request generation, evidence retrieval, JSON score parsing and `nl_output/` files into the `backtest_tool` section.
- Renamed chapter 6 from text/LLM coverage to LLM API and log boundaries, leaving provider access and conversation-log rules there.

Validation:
- Checked Environment headings and stale-section searches after the edit.

## 2026-06-08 Backtest Tool preflight simplification

Task: simplify the `backtest_tool` and strategy-function contract for the current fixed-horizon validation flow.

Decision:
- `backtest_tool` consumes an already prepared PIT snapshot and does not construct PIT data or perform raw data filtering.
- Validation dates, buy date, sell date, costs and sizing belong to Pipeline/run manifest, not to Agent and not to `generate_orders(context)`.
- `generate_orders()` takes no arguments; it reads fixed Sandbox paths such as `/mnt/snapshot/` and `/mnt/artifacts/agent_output/nl_prior/`.
- Snapshot metadata remains in `/mnt/snapshot/manifest.json` and is checked by `backtest_tool`.
- Strategy output only contains the candidate pool and factor score; optional order hint columns are removed from the current contract.
- Formal `backtest_tool` must be preceded by `modification_check_tool`.
- `finish_step_tool` must run a lightweight `backtest_tool` contract check without LLM scoring, replay or simulated fills before ending the Step.

Changes:
- Updated `docs/environment_design.md`, `docs/agent_design.md`, `docs/pipeline_design.md`, and `configs/agent_output_template/factor/README.md`.
- Updated `configs/agent_output_template/factor/main.py` to a no-argument entrypoint and removed optional order-output columns.

Validation:
- Ran Python compile, JSON parsing, wording search, `git diff --check`, and removed the generated `__pycache__`.

## 2026-06-08 Valid/test snapshot binding clarification

Task: clarify how `backtest_tool` distinguishes valid and test after `generate_orders()` became a no-argument function.

Decision:
- `generate_orders()` is phase-agnostic and always reads `/mnt/snapshot`.
- `backtest_tool` mode is selected by run manifest and Runner execution context, not by strategy function arguments.
- In validation, `/mnt/snapshot` is the validation snapshot and results under `results/valid_<idx>/` are readable to Agent.
- In test and held-out, Agent is stopped; Runner/root binds the frozen test or held-out snapshot as the replay process `/mnt/snapshot`, writes `results/test_<idx>/` or `results/heldout_<idx>/`, and does not feed results back to Agent.

Changes:
- Updated `docs/environment_design.md` section 2.4, 3.1 and 4.3.
- Updated `docs/pipeline_design.md` section 4.3.

Validation:
- Follow-up validation should check stale `/mnt/test_snapshot` wording, `generate_orders(context)` wording, Python compile and `git diff --check`.

## 2026-06-08 Backtest mode switching simplification

Task: make the actual valid/test mode switch explicit without introducing a separate named spec object.

Decision:
- Pipeline gives Runner simple call parameters before each formal `backtest_tool` call: `mode`, `snapshot_path`, and `result_name`.
- `mode` has only two values: `valid` and `frozen_eval`.
- `test` and `heldout` share `frozen_eval`; they differ only by snapshot, output directory name and ledger label.
- Runner binds the chosen snapshot as `/mnt/snapshot` for that run, and `backtest_tool` verifies `/mnt/snapshot/manifest.json` against the Pipeline-recorded snapshot ID/hash.
- `valid` may run in the Agent-active validation Sandbox; `frozen_eval` runs after Agent stops, usually in a new short-lived replay container with the same path layout.

Changes:
- Updated `docs/environment_design.md` section 4.3 with the two-mode Runner binding flow.
- Updated `docs/pipeline_design.md` section 3.2 and 4.3 with simple Runner call parameters and the shared `frozen_eval` path for test/heldout.

Validation:
- Follow-up validation should run stale wording searches and `git diff --check`.

## 2026-06-08 Single-container snapshot slots and NL toggle

Task: align the design with a single Docker Sandbox that contains train, valid and test snapshot slots, while keeping test data unreadable to the Agent user.

Decision:
- A Fold Sandbox may mount three read-only snapshot slots: `/mnt/snapshots/train`, `/mnt/snapshots/valid` and `/mnt/snapshots/test`.
- `/mnt/snapshot` is a Runner-managed current-view alias used by `generate_orders()`.
- Agent can inspect train/valid data and validation results, but cannot read `/mnt/snapshots/test`.
- Held-out does not need a separate Sandbox path; Pipeline places the held-out evaluation data in the `test` slot and records a held-out ledger label.
- Validation can use `nl=off`, `nl=sample` or `nl=on` to control API cost. Test and held-out force factor and natural-language scoring on.

Changes:
- Updated `docs/environment_design.md` snapshot layout, Sandbox permissions and `backtest_tool` mode/switch wording.
- Updated `docs/agent_design.md` Sandbox example, Agent operation steps and prohibited test-data access wording.
- Updated `docs/pipeline_design.md` orchestration wording from per-call `snapshot_path` binding to `snapshot_stage` selection inside mounted snapshot slots.
- Updated `configs/agent_output_template/factor/README.md` and `main.py` wording to describe `/mnt/snapshot` as the Runner-managed current view.

Validation:
- Run stale wording search, template compile, JSON parse and `git diff --check` after this edit.

## 2026-06-08 Validation replay visibility correction

Task: decide whether `/mnt/snapshots/valid` should be readable by the Agent user.

Decision:
- `/mnt/snapshot` is the Agent-visible decision input view and contains only data available before the decision time.
- `/mnt/snapshots/valid` is validation replay data, not Agent input, so it must be unreadable to the Agent user.
- `/mnt/snapshots/test` remains unreadable to the Agent user.
- `backtest_tool` reads valid/test replay data internally after calling the strategy function on `/mnt/snapshot`.
- Agent can read `results/valid_<idx>/` after validation, but cannot browse the validation replay raw files.

Changes:
- Updated `docs/environment_design.md` to split decision input from replay data.
- Updated `docs/agent_design.md` so Agent can read current decision input and train data, but not valid/test replay data.
- Updated `docs/pipeline_design.md` to use `replay_stage` instead of `snapshot_stage`.

Validation:
- Run wording search, Python/JSON checks and `git diff --check` after this edit.

## 2026-06-08 Validation replay as readable development set

Task: revise the validation boundary so Agent can inspect validation replay data and make targeted Step changes.

Decision:
- `/mnt/snapshots/valid` is a read-only development/validation replay directory visible to the Agent user.
- Agent may inspect validation prices, returns, fills, rejected orders and failure cases to improve the next Step.
- Formal `generate_orders()` still must use `/mnt/snapshot` as its runtime input and must not read `/mnt/snapshots/valid` or `/mnt/snapshots/test`.
- `modification_check_tool` and `backtest_tool` should reject obvious direct references to replay directories in formal strategy code.
- `/mnt/snapshots/test` remains hidden from Agent and is reused for test and held-out replay under Pipeline control.

Changes:
- Updated `docs/environment_design.md`, `docs/agent_design.md` and `docs/pipeline_design.md`.
- Left the single-container train/valid/test layout intact.

Validation:
- Run stale wording search, template compile, JSON parse and `git diff --check` after this edit.

## 2026-06-08 Root-managed snapshot symlink

Task: record how Runner switches the current `/mnt/snapshot` view inside the Sandbox.

Decision:
- `/mnt/snapshot` may be implemented as a root-owned symlink.
- Runner/root may switch it with `ln -sfn <decision_input_view> /mnt/snapshot` before formal `backtest_tool` execution.
- Agent user must not own, delete or overwrite `/mnt/snapshot`.
- If `valid` or `test` directories contain full replay data, Runner must not point formal `generate_orders()` directly at those replay directories; it should point to the prepared decision-time visible input view.

Changes:
- Updated `docs/environment_design.md` section 2.4 and Runner execution steps.
- Updated `docs/pipeline_design.md` Runner call-parameter explanation.

Validation:
- Run wording search and `git diff --check` after this edit.

## 2026-06-08 Fold data mount example

Task: record how a single Sandbox can mount a full Fold's train/valid/test data while keeping the formal strategy input PIT-safe.

Decision:
- A Sandbox can mount all data needed for the Fold if the data is split by use: `train`, `valid`, `test`, and separate decision input views.
- Example split:
  - `train`: 2020-01 to 2021-09.
  - `valid`: 2021-10 to 2021-12.
  - `test`: 2022-01 to 2022-03.
  - `valid_decision_input`: 2020-01 to 2021-09.
  - `test_decision_input`: 2020-04 to 2021-12.
- Runner/root switches `/mnt/snapshot` to the decision input view before validation or test replay.
- `backtest_tool` reads the corresponding replay directory separately.

Changes:
- Added the concrete mount/view example to `docs/environment_design.md`.
- Added the shorter orchestration version to `docs/pipeline_design.md`.

Validation:
- Run stale wording search and `git diff --check` after this edit.

## 2026-06-08 Constraints generation contract

Task: answer how the Environment 2.2 `constraints.parquet` window is generated.

Decision:
- `constraints.parquet` is synthesized by Environment and is not an independent downloaded table.
- Agent-visible `/mnt/snapshot/constraints.parquet` contains only decision-time-visible next-trade constraints.
- Replay execution constraints under `/mnt/snapshots/valid` and `/mnt/snapshots/test` contain buy/sell/holding-day execution truth and are used by `backtest_tool` and the simulated Broker.
- Main inputs are `trade_cal`, stock/universe metadata, `suspend_d`, `stk_limit` or previous-close limit-price derivation, daily/minute liquidity data, and simulated Broker account/position state.

Changes:
- Updated `docs/environment_design.md` section 2.2 with source tables and generation rules.
- Updated `docs/environment_design.md` section 5.4 to state that Broker checks use replay execution constraints.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 Constraints documentation trim

Task: reduce detail in Environment 2.2 while keeping the generation idea visible.

Decision:
- Removed the detailed constraints generation subsection from the living Environment doc.
- Added a `生成方式` column to the main 2.2 visible-window table.
- `constraints` is now described there as an Environment-synthesized domain from calendar, universe, suspension, limit-price, liquidity and Broker state.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 Environment domain assembly table

Task: keep the visible-window table compact and move data-domain generation notes into a separate table.

Decision:
- Removed the `生成方式` column from the main Environment 2.2 visible-window table.
- Added a separate `数据域拼接方式` table with sources, join/filter rules and output boundaries.
- Kept constraints at the same abstraction level as other domains, without a detailed rule subsection.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 Fold deadline 30 minutes

Task: change the default per-Fold runtime limit from 20 minutes to 30 minutes.

Decision:
- Each Fold defaults to 30 minutes.
- Step still has no separate timer and shares the Fold deadline.
- The finalization prompt threshold remains T-5 minutes.

Changes:
- Updated `docs/agent_design.md`.
- Updated `docs/environment_design.md`.
- Updated `docs/pipeline_design.md`, including the example `max_fold_minutes` and `fold_deadline_at`.

Validation:
- Run deadline wording search and `git diff --check` after this edit.

## 2026-06-08 NL scoring parallel task boundary

Task: clarify how `backtest_tool` runs and detects completion of LLM natural-language scoring.

Decision:
- `backtest_tool` starts independent per-stock scoring tasks, which may run in a bounded thread pool.
- Each task owns one stock's candidate row, company context, enabled rules, evidence and conversation trace.
- Each stock can run up to three retrieval rounds and may stop early once evidence is sufficient.
- A task is complete only after validated JSON, configured skip/failure handling, timeout or hard failure.
- `backtest_tool` waits for all candidate-stock tasks to reach a terminal state before score fusion; hard failures without explicit policy fail the formal backtest.

Changes:
- Updated `docs/environment_design.md` natural-language scoring flow and task terminal-state rules.
- Updated `configs/agent_output_template/nl_prior/README.md` baseline prompt workflow and retrieval-round wording.

Validation:
- Run wording search and `git diff --check` after this edit.

## 2026-06-08 LLM JSON extraction contract

Task: record how `backtest_tool` extracts JSON from LLM API responses.

Decision:
- LLM never writes formal result files directly.
- `backtest_tool` receives provider responses through LLM Proxy and extracts JSON in this order: tool/function call arguments, JSON mode or structured response content, then a single complete JSON object from plain text.
- Plain text extraction may remove one json code fence, but must not search long explanations for score fields.
- Extracted content must pass `json.loads` and schema checks before `backtest_tool` writes `nl_output/scores.jsonl`.

Changes:
- Updated `docs/environment_design.md`.
- Updated `configs/agent_output_template/nl_prior/README.md`.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 NL prompt final-output wording

Task: clarify whether natural-language scoring prompts can allow model reasoning.

Decision:
- The LLM may internally analyze `company_context`, evidence and enabled `prior.json` rules.
- The final provider response consumed by `backtest_tool` must still be exactly one JSON object or structured JSON payload.
- The prompt should not ask for a full reasoning trace. The formal `reason` field should be a short auditable basis tied to evidence.
- `backtest_tool` JSON extraction and schema validation rules are unchanged.

Changes:
- Updated `configs/agent_output_template/nl_prior/README.md` fixed system constraints.
- Updated the search-request prompt wording to say internal analysis is allowed, but final output must be JSON.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 Think-tag compatibility

Task: document how the Environment handles provider responses with explicit reasoning text such as `<think>...</think>`.

Decision:
- Reasoning extraction belongs to LLM Proxy or the provider adapter, not to strategy code.
- If the provider separates `reasoning_content` and final `content`, only final `content` is passed to JSON extraction; reasoning is kept in conversation logs.
- If plain text contains a closed `<think>...</think>` block, the adapter may strip the closed block before JSON extraction and log the raw response.
- Unclosed think blocks, remaining non-JSON explanation, multiple JSON objects or fields found only inside reasoning text are failures unless run config permits one fixed JSON repair call.
- Formal scores, risk labels and evidence references must come from final JSON only.

Changes:
- Updated `docs/environment_design.md` near the `backtest_tool` natural-language JSON extraction contract.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-08 NL input de-anchoring

Task: decide whether natural-language scoring prompts should receive JSON inputs and factor scores.

Decision:
- Structured JSON objects remain the preferred prompt input representation because they are easier to log, replay, validate and adapt across providers.
- The LLM-facing candidate object is renamed to `candidate_identity` and must contain only `ts_code`.
- `factor_score`, factor rank, factor reason, target weight, validation return, replay result and other stock conclusions must not be passed into natural-language scoring prompts.
- `backtest_tool` combines `factor_outputs.factor_score` and `nl_score` only after the LLM score has been parsed and validated.

Changes:
- Updated `docs/environment_design.md` natural-language scoring contract and default score-fusion variable name.
- Updated `configs/agent_output_template/nl_prior/README.md` prompt variables, example identity JSON, and score-fusion wording.

Validation:
- Run stale-name search and `git diff --check` after this edit.

## 2026-06-08 NL identity minimization

Task: decide whether `task_id` should be passed into natural-language scoring prompts.

Decision:
- LLM-visible `candidate_identity` should contain only `ts_code`.
- `task_id`, call ID and thread ID remain internal `backtest_tool` logging and task-management fields.
- Keeping task/call identifiers out of the prompt reduces irrelevant tokens and avoids leaking validation/result naming conventions into natural-language scoring.

Changes:
- Updated `docs/environment_design.md`.
- Updated `configs/agent_output_template/nl_prior/README.md` example and forbidden-field wording.

Validation:
- Run `task_id` search and `git diff --check` after this edit.

## 2026-06-08 Agent-readable output templates

Task: make `factor/README.md` and `nl_prior/README.md` better suited for the Sandbox Agent.

Decision:
- Template READMEs should be Agent work instructions, not Environment implementation manuals.
- `factor/README.md` should focus on `main.py`, `factors.json`, PIT-safe factor logic and candidate-pool output.
- `nl_prior/README.md` should focus on writing reusable `prior.json` rules, rule quality, scoring meaning and the boundary that Agent does not write `nl_score`.
- Detailed `backtest_tool`, LLM Proxy, provider adapter, JSON extraction, think-tag handling and parallel scoring internals stay in `docs/environment_design.md`.

Changes:
- Rewrote `configs/agent_output_template/factor/README.md`.
- Rewrote `configs/agent_output_template/nl_prior/README.md`.
- Updated `docs/environment_design.md` wording from `nl_prior/README.md` Prompt template to scoring instructions.

Validation:
- Run stale implementation-detail search, JSON/Python template checks and `git diff --check` after this edit.

## 2026-06-09 Initial NL prior workflow

Task: fix the `nl_prior/README.md` workflow because the first strategy artifact has no historical `nl_output/`.

Decision:
- Initial `prior.json` creation should use visible snapshot data: company context, announcements, news, research, policy text samples and general investment reasoning.
- Validation `nl_output/` is available only after the first backtest run, so it should be used for later Step refinement, not required upfront.
- The README should explicitly distinguish initial rule creation from later rule updates.

Changes:
- Updated `configs/agent_output_template/nl_prior/README.md` working-flow section.

Validation:
- Run `git diff --check` after this edit.

## 2026-06-09 NL prior rule simplification

Task: simplify `prior.json` rules and restore an Agent-readable NL analysis prompt outline.

Decision:
- Natural-language rules no longer need `enabled`; unused rules should be deleted.
- `prior.json` rule schema is now `id`, `text`, `evidence`, and `effect`.
- `nl_prior/README.md` should include a concise NL analysis flow and baseline Prompt outline so the Agent understands how rules are consumed.
- Provider adapter details, JSON extraction internals and parallel task mechanics remain in `docs/environment_design.md`.

Changes:
- Updated `configs/agent_output_template/nl_prior/README.md`.
- Updated `docs/environment_design.md` modification-check schema and natural-language scoring flow wording.

Validation:
- Run natural-language rule-schema search, JSON checks and `git diff --check` after this edit.

## 2026-06-09 Factor registry simplification

Task: remove `enabled` from the factor registry schema.

Decision:
- `factors.json` entries are active by definition.
- Unused factors should be deleted from the registry instead of kept with an enabled/disabled flag.
- `modification_check_tool` should validate registered factor entries with `id`, `function`, `description`, `lookback_days`, and `direction`.

Changes:
- Updated `configs/agent_output_template/factor/README.md`.
- Updated `docs/environment_design.md` modification-check schema table.

Validation:
- Run current-doc factor-enabled search, JSON checks and `git diff --check` after this edit.

## 2026-06-09 Living-doc navigation depth

Task: unify navigation depth across Data, Agent, Environment and Pipeline design docs.

Decision:
- For these long design documents, navigation to numbered main chapters plus `###` second-level sections is more useful than chapter-only navigation.
- This keeps audit and implementation review efficient without expanding to deeper headings.
- Use the heading name `导航` consistently.

Changes:
- Updated `docs/agent_design.md` navigation.
- Updated `docs/environment_design.md` navigation.
- Updated `docs/pipeline_design.md` navigation.

Validation:
- Run heading/navigation search and `git diff --check` after this edit.

## 2026-06-09 Snapshot input boundary

Task: clarify whether Agent should see both `/mnt/snapshot` and `/mnt/snapshots/train`.

Decision:
- Agent should not have two equivalent current-input paths.
- `/mnt/snapshot` is the single Agent-facing PIT input view and the only formal `generate_orders()` data entry.
- `/mnt/snapshots/valid` remains Agent-readable for validation review.
- `/mnt/snapshots/test` remains hidden from Agent.
- `/mnt/snapshots/train` is removed from the Agent-facing design to avoid duplicated input semantics and hard-coded strategy paths.

Changes:
- Updated `docs/environment_design.md` snapshot layout, Sandbox permissions and Runner actions.
- Updated `docs/agent_design.md` Sandbox example and strategy-code input rules.
- Updated `docs/pipeline_design.md` Step orchestration and Fold example.

Validation:
- Run `/mnt/snapshots/train` residual search and `git diff --check` after this edit.

## 2026-06-09 Sandbox snapshot role split

Task: reconsider the snapshot boundary after deciding whether `/mnt/snapshot` should be Agent-facing.

Decision:
- Agent should read explicit stage slots: `/mnt/snapshots/train` for training/exploration and `/mnt/snapshots/valid` for validation review.
- `/mnt/snapshot` should be reserved for `backtest_tool` formal execution and contract checks; it is the current decision input bound by Runner/root before calling `generate_orders()`.
- Formal strategy code should not hard-code `/mnt/snapshots/train`, `/mnt/snapshots/valid` or `/mnt/snapshots/test`.
- The template can support `MQ_SNAPSHOT_DIR=/mnt/snapshots/train` for Agent debugging, but formal `backtest_tool` must set or clear it so `/mnt/snapshot` is used.
- This replaces the immediately previous Agent-facing `/mnt/snapshot`-only boundary because explicit train/valid slots are easier for Agent exploration and reduce ambiguity about formal execution.

Changes:
- Updated `docs/environment_design.md` snapshot layout, Sandbox permissions and Runner steps.
- Updated `docs/agent_design.md` Sandbox example, code rules and forbidden behavior.
- Updated `docs/pipeline_design.md` Step orchestration and Fold example.
- Updated `configs/agent_output_template/factor/README.md`.
- Updated `configs/agent_output_template/factor/main.py` to support `MQ_SNAPSHOT_DIR` for debugging.

Validation:
- Run path-boundary searches, template compile and `git diff --check` after this edit.

## 2026-06-09 Agent document flow cleanup

Task: improve `docs/agent_design.md` organization after review that the chapter order did not read naturally.

Decision:
- Organize the Agent document by lifecycle rather than by accumulated boundary rules.
- Lead with three-layer responsibility and Epoch/Fold/Step flow.
- Put Fold-to-Fold inheritance near the top because it defines what Agent state actually persists.
- Keep Sandbox path roles in the working-area chapter and state `snapshots` versus `snapshot` usage explicitly.
- Move Tool details after Step workflow and formal artifact contracts, because Tool semantics are implementation details supporting the flow.

Changes:
- Rewrote `docs/agent_design.md` section order to:
  1. Agent in the system.
  2. Agent workspace.
  3. Step execution flow.
  4. Formal strategy artifacts.
  5. Tool semantics.
  6. Modification constraints and regularization.
  7. LLM calls and logging.
  8. Prohibited behavior and acceptance checklist.
- Added a compact path-role table in Agent docs for `snapshots` versus `snapshot`; detailed runtime switching remains documented in Environment docs.

Validation:
- Run heading search, snapshot keyword search and `git diff --check` after this edit.

## 2026-06-09 Agent/Pipeline document boundary cleanup

Task: correct Agent docs after review that chapter 1 still described mostly Pipeline responsibilities.

Decision:
- Agent docs should define the Agent work contract after Pipeline has already prepared a Sandbox.
- Pipeline docs should be the authority for Step/Fold/Epoch scheduling, strategy artifact freezing, testing, held-out and ledgers.
- Agent docs can refer to Step/Fold terms, but should not duplicate the orchestration flow as if Agent controls it.

Changes:
- Rewrote `docs/agent_design.md` chapter 1 as:
  - Agent responsibilities.
  - Non-Agent responsibilities and owning documents.
  - Agent session/memory boundary.
- Kept `docs/agent_design.md` focused on visible data, writable outputs, Step-internal work, formal artifact schemas, Tool use and Agent prohibitions.
- Updated `docs/pipeline_design.md` introduction to state Pipeline ownership of orchestration, freezing, tests and ledgers.
- Fixed the Pipeline Step input example from `max_fold_minutes=20` to `max_fold_minutes=30`.

Validation:
- Run Agent/Pipeline heading search, stale orchestration wording search and `git diff --check` after this edit.

## 2026-06-09 Agent chapter style alignment

Task: align Agent chapter 1 with the clearer Environment chapter 1 style.

Decision:
- Use one concise responsibilities chapter rather than splitting chapter 1 into multiple small subsections.
- Keep Agent chapter 1 focused on what Agent owns, what it does not own, the session/memory boundary and the trustworthy-log boundary.
- Leave orchestration and artifact-freezing details in Pipeline docs.

Changes:
- Renamed Agent chapter 1 from `Agent 合同` to `Agent 职责`.
- Removed `1.1/1.2/1.3` subheadings from Agent chapter 1.
- Added an Agent trustworthy-log boundary sentence mirroring Environment's log-boundary wording.

Validation:
- Run Agent heading search and `git diff --check` after this edit.

## 2026-06-09 Environment non-responsibility table

Task: align Environment chapter 1 `Environment 不负责` with Agent's responsibility-boundary table style.

Decision:
- Keep Environment's responsibilities as bullets.
- Convert non-responsibilities into a table with owning document or hard boundary.
- Make clear that Environment executes and records, while Agent owns strategy logic and Pipeline owns held-out boundaries.

Changes:
- Updated `docs/environment_design.md` chapter 1 table for non-responsibilities.

Validation:
- Run Environment section check and `git diff --check` after this edit.

## 2026-06-09 Agent visible-domain alignment

Task: fix mismatch where Environment documented `constraints` as a visible data domain but Agent docs did not mention it, and align visible-domain ordering.

Decision:
- Agent docs should list the same visible data domains as Environment docs in the same order.
- `constraints` is Agent-visible context and a trusted execution input, but Agent does not synthesize it.
- `backtest_tool` remains responsible for formal pre-check, fill/reject and replay enforcement.

Changes:
- Updated `docs/agent_design.md` section 2.1 default window table to order domains as `daily`, `intraday_1min`, `fundamentals`, `events`, `macro`, `text`, `constraints`.
- Added a `constraints` row explaining tradability, suspension/limit, liquidity and Broker-state use.

Validation:
- Run visible-domain search and `git diff --check` after this edit.

## 2026-06-09 Constraints visible-domain removal

Task: remove `constraints` as a separate visible data domain after deciding trade executability should be handled by Broker/backtest execution rather than exposed as a dedicated Agent data domain.

Decision:
- Do not list `constraints` in Agent or Environment visible-data domain tables.
- Do not include `constraints.parquet` in the snapshot example.
- Keep Broker and `backtest_tool` responsible for final executability, fills and rejects.
- Agent may infer suspension or trading risk from visible market data, but formal成交判断 remains in Environment execution.

Changes:
- Removed the `constraints` row from `docs/agent_design.md` visible data table.
- Removed the `constraints` row from `docs/environment_design.md` visible window and assembly tables.
- Removed `constraints.parquet` from the Environment snapshot example.

Validation:
- Run visible-domain search and `git diff --check` after this edit.

## 2026-06-09 Pipeline Step input snapshot naming

Task: clarify the Pipeline Step input table after review that `validation_snapshot` mixed together the validation replay slot and the formal decision-time PIT input view.

Decision:
- Split the Step input wording into `train_snapshot`, `validation_replay_snapshot`, and `decision_input_view`.
- `train_snapshot` maps to `/mnt/snapshots/train` and is Agent-readable training/exploration data.
- `validation_replay_snapshot` maps to `/mnt/snapshots/valid` and is Agent-readable validation replay/review data.
- `decision_input_view` is the Runner/root-created PIT view bound to `/mnt/snapshot` before `backtest_tool` calls the formal strategy entry.

Changes:
- Updated `docs/pipeline_design.md` section 3.1.

Validation:
- Run `validation_snapshot` search and `git diff --check` after this edit.

## 2026-06-09 Modification-check parent artifact

Task: make the modification-check diff baseline robust so Agent cannot accidentally or intentionally lose the original parent strategy artifact.

Decision:
- Non-initial Steps must keep an Agent-readable but read-only parent artifact copy in the Sandbox, separate from Agent-writable `agent_output/`.
- The parent copy path is documented as `/mnt/artifacts/parent_output/`, under the existing artifacts root.
- `modification_check_tool` must validate the parent copy hash against run manifest before diffing.
- The Tool compares that immutable parent copy with current `agent_output/factor/` and `agent_output/nl_prior/`; it must not infer the parent from Agent-controlled files.

Changes:
- Updated `docs/environment_design.md`.
- Updated `docs/pipeline_design.md`.
- Updated `docs/agent_design.md`.

Validation:
- Run parent-artifact wording search and `git diff --check` after this edit.

## 2026-06-09 Fold deadline fallback

Task: clarify what happens when a Fold times out without a valid strategy output.

Decision:
- Runner/Proxy may trigger one fixed finalization prompt before deadline.
- After `fold_deadline_at`, Pipeline must stop new Shell, service and LLM calls; it must not keep appending prompts until a strategy passes.
- If a valid Step already exists in the Fold, Pipeline uses the latest accepted Step artifact.
- If no Step was accepted, Pipeline carries forward the parent strategy artifact unchanged and records `no_update_timeout`.
- If this is the first initialization and no valid configured baseline artifact passes contract checks, the Fold / Epoch fails.

Changes:
- Updated `docs/pipeline_design.md`.
- Updated `docs/environment_design.md`.
- Updated `docs/agent_design.md`.

Validation:
- Run deadline wording search and `git diff --check` after this edit.

## 2026-06-09 Pipeline modification-check step

Task: add the newly defined `parent_output` baseline into the Pipeline Step execution flow.

Decision:
- Agent self-check and Pipeline pre-backtest check both call the same `modification_check_tool`.
- The Tool has no business parameters and always compares `/mnt/artifacts/parent_output/` with `/mnt/artifacts/agent_output/`.
- The Tool must validate parent hash against run manifest before computing the diff.
- Pipeline reruns the Tool before formal `backtest_tool` to catch any changes after Agent self-check.

Changes:
- Updated `docs/pipeline_design.md` section 3.2 Step execution item 7.

Validation:
- Run targeted wording search and `git diff --check` after this edit.

## 2026-06-09 Shell transcript wording

Task: clarify what `sandbox_shell_tool transcript` means in the Step output section.

Decision:
- Replace the English `transcript` wording with `Shell 调用记录`.
- The record path points to Environment-generated logs for Sandbox Shell/Python calls, including command, stdout/stderr, exit code, timestamps and related artifact paths.

Changes:
- Updated `docs/pipeline_design.md`.
- Updated `docs/environment_design.md`.

Validation:
- Run transcript wording search and `git diff --check` after this edit.

## 2026-06-09 Pipeline Step output simplification

Task: reduce `docs/pipeline_design.md` section 3.3 because it mixed Pipeline ledger fields with Environment runtime output files.

Decision:
- Pipeline should not enumerate every Shell/LLM/backtest/natural-language output file in Step output.
- Step output at Pipeline level is a `step_ledger` record with compact references and decision status.
- Environment remains responsible for runtime files: `execution_calls.jsonl`, `llm_conversations.jsonl`, `strategy_artifact_diff.json`, `results/<phase>_<idx>/`, `nl_output/`, and manifests.

Changes:
- Rewrote `docs/pipeline_design.md` section 3.3 as a small table.
- Tightened the `step_ledger` row in Pipeline section 7.1.

Validation:
- Run Step-output wording search and `git diff --check` after this edit.

## 2026-06-09 Fold finish tool

Task: correct the tool boundary after deciding that one Fold should use one Agent session/conversation, while Step is only an in-Fold validation iteration.

Decision:
- Replace `finish_step_tool` with `finish_fold_tool` in the living design docs.
- `finish_fold_tool` is the no-argument Agent-facing signal that the current Fold should stop modifying.
- Step does not end the Agent conversation; each validation run writes a Step ledger, and the same Agent can continue to the next Step.
- Pipeline now describes Fold startup once, repeated Step iterations inside the same Agent session, and Fold ending through `finish_fold_tool`, Step limit, early stop, deadline, or timeout fallback.

Changes:
- Updated `docs/agent_design.md`.
- Updated `docs/environment_design.md`.
- Updated `docs/pipeline_design.md`.

Validation:
- Run `finish_step_tool` residual search in living docs and `git diff --check` after this edit.

## 2026-06-09 SubAgent living-doc residual audit

Task: run a read-only SubAgent audit of `docs/agent_design.md`, `docs/environment_design.md`, and `docs/pipeline_design.md` for old-design residue.

SubAgent:
- `Lovelace`

Result:
- No Blocking findings.
- No High findings.
- Medium: Pipeline 7.3 still said the next Fold copies frozen output only into `/mnt/artifacts/agent_output/`.
- Low: Agent PIT wording was too broad for `/mnt/snapshots/valid`.
- Low: Pipeline used `best-effort Step 输出` while `step_ledger.status` did not include `best_effort`.

Changes:
- Updated Pipeline 7.3 to copy frozen strategy into both `/mnt/artifacts/parent_output/` and `/mnt/artifacts/agent_output/`.
- Clarified Agent visible-data wording: train and `/mnt/snapshot` are PIT decision inputs; `/mnt/snapshots/valid` is validation replay/review and cannot be read by formal `generate_orders()`.
- Removed `best-effort` from Pipeline Step status wording and mapped timeout finalization to existing `rejected` / `timeout` / accepted Step semantics.

Validation:
- Run living-doc old-residue search and `git diff --check` after this edit.

## 2026-06-09 Step/Fold ledger boundary cleanup

Task: simplify Pipeline ledger fields and keep `finish_fold_tool` at Fold scope.

Decision:
- `step_ledger` should not include `finish_fold_tool`; Fold finishing is not a Step gate.
- `step_ledger` should not enumerate `execution_calls` and `llm_conversations`; it records one `run_ref` pointing to Environment's run manifest.
- `finish_fold_tool` belongs in Fold output / `fold_ledger`.

Changes:
- Replaced `gate_refs` with `modification_check_ref` in `docs/pipeline_design.md`.
- Replaced `run_trace_refs` with `run_ref`.
- Added `finish_fold_ref` and `fold_status` to Fold output example.
- Updated Pipeline section 7.1 ledger descriptions.

Validation:
- Run ledger-field residual search and `git diff --check` after this edit.

## 2026-06-09 Environment runtime artifact cleanup

Task: simplify the Environment runtime output contract without updating Pipeline docs in this pass.

Decision:
- Agent itself writes only the controlled workspace and formal strategy output directories.
- Environment runtime state should be centered on `run_manifest.json`.
- Shell, Tool, backtest, Broker, Fold finish and real LLM provider calls should share one `agent_trace.jsonl` event stream.
- Backtest result directories should keep only large result artifacts such as return details, order plan and natural-language scoring output.

Changes:
- Updated `docs/environment_design.md` artifact tree and subdirectory ownership table.
- Replaced the standalone diff-file contract with `modification_check_tool` returning results to Agent, appending a trace event, and updating the latest-check summary in `run_manifest.json`.
- Replaced standalone Fold-finish output with `finish_fold_tool` updating `run_manifest.json` and appending a trace event.
- Removed the separate `summary.json`, `execution_calls.jsonl`, `llm_conversations.jsonl` and `strategy_artifact_diff.json` contracts from Environment docs.

Validation:
- Old-file-name residual search in `docs/environment_design.md` returned no matches.
- `git diff --check` passed.
- Resource checks after edit: system memory about 405 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Agent trace readability

Task: clarify whether Agent can read `agent_trace.jsonl`.

Decision:
- Training/validation `agent_trace.jsonl` should be Agent-readable and read-only.
- This lets Agent review its own Shell, Tool, validation backtest and natural-language scoring calls during the current Fold.
- Test and held-out traces remain hidden from Agent.

Changes:
- Updated `docs/environment_design.md` artifact ownership table.
- Added the training/validation read-only rule to the logging section.

Validation:
- Targeted search confirmed the training/validation read-only rule is present.
- `git diff --check` passed.

## 2026-06-09 NL trace/output boundary

Task: clarify whether natural-language scoring belongs in `agent_trace.jsonl` or `nl_output/`.

Decision:
- Both are needed, but they record different things.
- `agent_trace.jsonl` records the LLM/API call process for audit and future distillation: prompts/messages, raw provider response, parsing result, usage and errors.
- `results/<phase>_<idx>/nl_output/` records the formal backtest product: per-stock score, risk tags, retrieval requests and evidence references.
- Backtest and score fusion should consume `nl_output/`, not parse scores from `agent_trace.jsonl`.

Changes:
- Updated `docs/environment_design.md` natural-language scoring section.
- Updated the LLM API logging boundary and runtime artifact table.

Validation:
- Targeted search confirmed the `agent_trace.jsonl` / `nl_output/` boundary is present.
- `git diff --check` passed.

## 2026-06-09 NL LLM log split

Task: reduce `agent_trace.jsonl` size by moving batch natural-language scoring call details into the backtest result directory.

Decision:
- `agent_trace.jsonl` should stay a lightweight process index for Shell, Tool, Broker, backtest, Fold finish, Agent main LLM calls and natural-language scoring batch summaries.
- Per-stock, multi-round natural-language scoring calls can be large and should live beside the formal scoring outputs.
- Store those detailed calls in `results/<phase>_<idx>/nl_output/nl_llm_calls.jsonl`.
- Agent can read training/validation `nl_output/` for review, while test/held-out `nl_output/` stays hidden.

Changes:
- Updated `docs/environment_design.md` natural-language scoring output table.
- Updated LLM API logging boundary.
- Updated runtime artifact and audit wording.

Validation:
- Targeted search confirmed `nl_llm_calls.jsonl` is documented and `agent_trace.jsonl` is described as a lightweight index.
- `git diff --check` passed.
- Resource checks after edit: system memory about 402 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Environment LLM/log chapter cleanup

Task: make `docs/environment_design.md` chapter 6 and chapter 7 less repetitive and easier to read.

Decision:
- Chapter 6 should be only the LLM API boundary: call entry points, key/network/timeout safety, and where different LLM call details are written.
- Chapter 7 should be only runtime logs and audit: runtime files, read permissions, and artifact checks.
- Keep natural-language scoring LLM details under `nl_output/nl_llm_calls.jsonl`, with only batch summaries in `agent_trace.jsonl`.

Changes:
- Renamed chapter 6 to `LLM API 边界`.
- Added chapter 6 subsections for call entry, safety/timeout, and call-detail destinations.
- Renamed chapter 7 to `运行日志和审计`.
- Added chapter 7 subsections for runtime files, read permissions, and audit checks.
- Updated the navigation and Runner LLM logging row.

Validation:
- Heading/residual search confirmed the new chapter titles and `nl_llm_calls.jsonl` / `agent_trace.jsonl` boundary are present, with no `LLM API 和日志边界` or `Conversation Log` residual.
- `git diff --check` passed.
- Resource checks after edit: system memory about 404 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Factor entrypoint candidate contract

Task: clarify whether the Agent returns full-market factor scores, pre-screened candidates, or final orders.

Decision:
- The formal factor entrypoint should be named `generate_candidates()`, not `generate_orders()`.
- Agent owns factor calculation, ranking and pre-screening.
- Agent returns a bounded candidate pool with `ts_code`, `factor_score`, `reason` and `source_artifacts`.
- Environment validates schema, candidate count, duplicate/illegal symbols and path misuse, but does not truncate full-market output or substitute its own strategy screening.
- `backtest_tool` runs natural-language scoring only on the candidate pool, then builds the final order plan.

Changes:
- Updated `docs/agent_design.md`.
- Updated `docs/environment_design.md`.
- Updated `docs/pipeline_design.md`.
- Updated `configs/agent_output_template/factor/main.py`.
- Updated `configs/agent_output_template/factor/README.md`.

Validation:
- Entrypoint residual search over Agent/Environment/Pipeline docs and factor template found no `generate_orders` residual.
- Factor template source compiled with the stock Python environment without writing bytecode.
- Generated `__pycache__` from the earlier compile check was removed.
- `git diff --check` passed.
- Resource checks after edit: system memory about 425 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Agent backtest NL modes

Task: make `docs/agent_design.md` section 5.3 explicit about validation natural-language scoring modes.

Decision:
- Agent should know validation `backtest_tool` can be run with `nl_mode=off`, `sample`, or `on`.
- `off` is for fast factor/link sanity checks.
- `sample` is for cost-controlled natural-language spot checks.
- `on` is the default formal validation state before ending a Fold.
- Test and held-out keep natural-language scoring fixed on and are not Agent-selectable.

Changes:
- Added an `nl_mode` table to `docs/agent_design.md` section 5.3.

Validation:
- Targeted search confirmed `nl_mode=off|sample|on` is documented in Agent section 5.3.
- `git diff --check` passed.
- Resource checks after edit: system memory about 427 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Fold Step cap

Task: update the default maximum Step count per Fold.

Decision:
- Each Fold should allow up to 10 Step iterations by default.
- The Step cap is still subordinate to the Fold deadline, early-stop rules and `finish_fold_tool`.

Changes:
- Updated `docs/pipeline_design.md` Step definition.

Validation:
- Targeted Step-cap search confirmed the old `3-5` wording is gone and the default 10-Step cap is documented.
- `git diff --check` passed.
- Resource checks after edit: system memory about 427 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Step ledger simplification

Task: remove the separate Step ledger file from the Pipeline design.

Decision:
- Environment's `run_manifest.json`, `agent_trace.jsonl` and `results/<phase>_<idx>/` already carry the runtime details.
- Pipeline should not duplicate those details in a Step-level log file.
- Step state should remain queryable as lightweight summaries embedded in `fold_ledger.steps[]`.
- `fold_ledger` remains the Fold-level experiment index that points to Environment run artifacts and selected strategy artifacts.

Changes:
- Updated `docs/pipeline_design.md` Step summary section.
- Removed `step_ledger` from the ledger-type table.
- Removed `step_ledger.jsonl` from the suggested experiment path layout.
- Updated Fold output example to include a `steps` array and selected Step.
- Removed old references to `summary.json`, `strategy_artifact_diff.json`, `results/fold_finish.json`, and old execution/LLM conversation logs from the touched Pipeline sections.

Validation:
- Targeted ledger-residual search found no `step_ledger`, old summary/diff/fold-finish files, or old execution/LLM conversation log names in `docs/pipeline_design.md`.
- `git diff --check` passed.
- Resource checks after edit: system memory about 427 GiB available; GPU state unchanged from pre-check and no new workload was started.

## 2026-06-09 Pipeline strategy handoff boundary

Task: clarify whether Pipeline chooses final factors/prior or only accepts the Agent's final submission.

Decision:
- Agent owns the Fold's submitted `factor/` and `nl_prior`.
- Pipeline may inject submission criteria into the Agent prompt and hard-validate the submitted artifact with validation results, risk constraints and modification checks.
- Pipeline must not independently pick, merge or rewrite factors and natural-language prior rules.
- If no valid Agent submission exists, Pipeline uses the documented fallback path: last accepted Step, parent artifact carry-forward, or initialization failure when no valid baseline exists.

Changes:
- Updated `docs/pipeline_design.md` Fold timeout, Fold finish and validation sections.
- Updated `docs/agent_design.md` responsibilities and Step flow so Agent uses prompt-provided submission criteria before calling `finish_fold_tool`.
- Removed the stale `results/fold_finish.json` path from the Agent `finish_fold_tool` example.

Validation:
- Targeted search confirmed the old wording around Pipeline choosing the final strategy artifact was removed from the active docs.
- `git diff --check` passed.
- Resource checks before edit: system memory about 427 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Agent-owned Fold early stop

Task: remove Pipeline-side complex early-stop strategy selection.

Decision:
- Early stop is an Agent action: the Agent reads Prompt criteria and validation results, then calls `finish_fold_tool` when it thinks the current artifact is good enough.
- Pipeline should not compute a complex validation score, compare same-Fold results across Epochs, or choose the best historical Step.
- Pipeline may inject early-stop guidance into the Prompt and then perform only hard checks: modification constraints, formal artifact contract, order validity, validation result/risk constraints and fallback handling.

Changes:
- Rewrote `docs/pipeline_design.md` section 4.2 from a score-formula early-stop target into an Agent-owned early-stop contract.
- Removed Pipeline wording around `validation_score`, `previous_epoch_same_fold`, `target_score` and automatic freezing of the current best strategy.
- Updated `docs/agent_design.md` Step flow to say Agent may call `finish_fold_tool` when continued search is no longer worth the remaining Fold time.

Validation:
- Targeted search over Agent/Pipeline docs confirmed the old score-formula terms were removed.
- `git diff --check` passed.
- Resource checks before edit: system memory about 427 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Broker config ownership boundary

Task: clarify that Pipeline does not own simulated Broker configuration.

Decision:
- Pipeline owns orchestration, artifact freezing and ledger references.
- Environment owns replay/Broker profiles, including costs, fill rules, position limits and reject logic.
- Pipeline should record Environment `run_manifest.json` and snapshot manifest references, rather than listing Broker configuration as a Pipeline-frozen object.
- The separate freeze checklist in Pipeline section 4.3 was redundant with the numbered test flow, so the flow is now the single source for that section.

Changes:
- Updated `docs/pipeline_design.md` section 4.3 to remove the redundant freeze checklist and keep the numbered test flow.
- Updated the test flow so Pipeline records strategy artifact IDs/hashes, validation/test result refs, Environment run manifest refs and snapshot manifest refs in the Fold ledger.
- Updated `docs/environment_design.md` section 5.3 so Broker costs/fills/limits/reject rules are resolved by Environment replay/Broker profiles and written to `run_manifest.json`.

Validation:
- Targeted residual search found no active wording that Pipeline freezes Broker configuration.
- `git diff --check` passed.
- Resource checks before edit: system memory about 427 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Regularization Docker boundary

Task: update Epoch post-regularization design to use a separate Docker with full non-held-out development history and mandatory modification checks.

Decision:
- Epoch regularization runs in a separate Docker, not inside any Fold Agent container.
- It can read full development history, including Fold ledgers, run manifests, agent traces, validation/test summaries and non-held-out snapshots.
- It cannot read held-out and cannot use formal `backtest_tool` loops to continue tuning on development history.
- Its purpose is to delete, merge, shorten and abstract `factor/` and `nl_prior/`, not to discover a new high-return strategy from full history.
- Regularized `factor/` and `nl_prior/` must pass the same deterministic `modification_check_tool` style gate before Pipeline freezes them as the next Epoch starting artifact.

Changes:
- Rewrote `docs/pipeline_design.md` section 5.2 around a regularization Docker and development-history boundary.
- Updated Pipeline risk/checklist wording to forbid held-out access and development-history backtest tuning, rather than forbidding all Fold test summaries from regularization.
- Updated `docs/environment_design.md` Tool table so Shell and modification check explicitly support regularization, while `backtest_tool` is not available as a regularization search loop.
- Clarified that `modification_check_tool` in regularization decides whether the regularized artifact may freeze, not whether to enter another backtest search.

Validation:
- Targeted searches checked for old "regularization cannot read Fold test" wording and for the new regularization/modification-check boundary.
- `git diff --check` passed.
- Resource checks before edit: system memory about 404 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Regularization section cleanup

Task: make Pipeline section 5.2 clearer and include every current-Epoch Fold `results/` as regularization input.

Decision:
- Regularization keeps the same 30-minute default time budget as a Fold.
- It receives every current-Epoch Fold `results/` directory as development material, including validation/test replay outputs, order plans, rejects, return/drawdown details, `nl_output` and error cases.
- Those results can support anti-overfitting review, but cannot become a new formal backtest tuning loop.
- The section should read as purpose, inputs, allowed edits, forbidden actions, modification check and final contract check.

Changes:
- Rewrote `docs/pipeline_design.md` section 5.2 into shorter paragraphs and a table.
- Added explicit current-Epoch `results/` input.
- Added the 30-minute regularization deadline.
- Kept mandatory `modification_check_tool` before freezing.

Validation:
- Targeted section read confirmed 5.2 now has a single input table and no repeated development/held-out wording.
- `git diff --check` passed.
- Resource checks before edit: system memory about 426 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Pipeline output path details

Task: clarify whether Fold/Epoch ledgers should be separate and document what each Docker run writes locally.

Decision:
- Keep `fold_ledger.jsonl`, `epoch_ledger.jsonl` and `heldout_ledger.jsonl` as separate files because their append cadence and semantic granularity differ.
- Connect them by IDs rather than merging all events into one large ledger.
- Treat `strategy_artifacts/` as the only reusable strategy handoff store.
- Treat `artifacts/<run_id>/` as the full runtime evidence store collected from Sandbox `/mnt/artifacts`.

Changes:
- Expanded `docs/pipeline_design.md` section 7.4.
- Added a path-role table for `ledgers/`, `strategy_artifacts/`, `artifacts/` and `reports/`.
- Added a Docker-run output table for Fold training/validation, frozen test replay, Epoch regularization and Held-out frozen evaluation.
- Clarified that `workspace/`, historical `results/` and Agent conversations are audit evidence only and are not copied into the next Fold as strategy input.

Validation:
- Targeted read of Pipeline 7.4 confirmed the output paths and Docker-run products are documented.
- `git diff --check` passed.
- Resource checks before edit: system memory about 426 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Pipeline ledger simplification

Task: simplify Pipeline chapter 7 and decide whether held-out needs its own ledger.

Decision:
- Held-out is a frozen Fold-style evaluation, so it should be recorded in `fold_ledger.jsonl` with `phase=heldout`.
- Keep only `fold_ledger.jsonl` and `epoch_ledger.jsonl` as formal ledger files.
- Reorder chapter 7 by how a reader looks for artifacts: host path, ledger files, Docker outputs, then strategy artifact/version records.
- Keep Environment responsible for runtime file contents; Pipeline records paths, summaries and aggregate hashes.

Changes:
- Updated Pipeline TOC for chapter 7.
- Rewrote chapter 7 into `7.1 宿主机路径`, `7.2 账本文件`, `7.3 Docker 结束产物`, and `7.4 策略产物和版本记录`.
- Removed `heldout_ledger.jsonl` from active Pipeline design.
- Preserved the per-Docker output table and made held-out write to `fold_ledger.jsonl` with `phase=heldout`.

Validation:
- Targeted residual search confirmed active Pipeline docs no longer reference `heldout_ledger`.
- `git diff --check` passed.
- Resource checks before edit: system memory about 377 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Per-run artifact collection wording

Task: clarify when Sandbox `/mnt/artifacts` is collected to the host experiment directory.

Decision:
- Artifact collection happens after every Docker or frozen replay run.
- Each run gets its own `experiments/<experiment_id>/artifacts/<run_id>/` directory.
- A Fold can therefore have multiple artifact directories, such as train/valid and frozen-test runs.

Changes:
- Updated `docs/pipeline_design.md` section 7.1 to say each Docker or frozen replay run is collected immediately under a distinct `artifacts/<run_id>/`.
- Updated `docs/environment_design.md` section 3.2 wording to match the per-run collection boundary.

Validation:
- Targeted search confirmed the misleading "all Docker runs finish" wording was removed.
- `git diff --check` passed.
- Resource checks before edit: system memory about 377 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Single experiment ledger

Task: decide whether `epoch_ledger.jsonl` is necessary when Fold records already index the experiment.

Decision:
- `epoch_ledger.jsonl` is not necessary for the current design.
- Use one formal ledger file: `ledgers/experiment_ledger.jsonl`.
- Distinguish events with `record_type`, including `fold`, `fold_test`, `epoch_regularization`, and `heldout`.
- Step summaries remain embedded in the `record_type=fold` record's `steps[]`.

Changes:
- Updated `docs/pipeline_design.md` handoff wording from `fold_ledger` to `experiment_ledger.jsonl`.
- Removed `fold_ledger.jsonl` and `epoch_ledger.jsonl` from the active output path.
- Updated Docker-output table so all run types append to `experiment_ledger.jsonl`.
- Kept `strategy_artifacts/` and `artifacts/<run_id>/` unchanged.

Validation:
- Targeted residual search confirmed active Pipeline docs no longer define separate `fold_ledger.jsonl`, `epoch_ledger.jsonl`, or `heldout_ledger.jsonl` files.
- `git diff --check` passed.
- Resource checks before edit: system memory about 377 GiB available; GPU state was unchanged from earlier checks and no heavy workload was started.

## 2026-06-09 Pipeline reports path removal

Task: remove optional report output path from active Pipeline experiment layout.

Decision:
- Active experiment layout should only define required durable paths.
- `reports/` is not needed in the current design; summaries can be generated later from `experiment_ledger.jsonl` and `artifacts/<run_id>/` if needed.

Changes:
- Removed `reports/` from `docs/pipeline_design.md` section 7.1 path tree.
- Removed the `reports/` row from the path table.

Validation:
- Targeted residual search confirmed active Pipeline docs no longer mention `reports/`.
- `git diff --check` passed.
- Resource checks were not needed for this documentation-only edit beyond the current session checks; no workload was started.

## 2026-06-09 Fold single-Docker output boundary

Task: align Fold output accounting with the design that training/validation and frozen test run in the same Fold Docker.

Decision:
- A Fold uses one Docker run by default.
- The same Fold run contains Agent training/validation, then Agent shutdown/write lock, then Runner/root frozen test.
- The Fold record in `experiment_ledger.jsonl` should include both `results/valid_*` and `results/test_*`.
- `record_type=fold_test` is unnecessary and was removed from the current design.

Changes:
- Updated `docs/pipeline_design.md` section 4.3 and chapter 7.
- Merged the previous Fold training/validation and frozen-test rows into one `Fold Docker` row.
- Updated `docs/environment_design.md` so `frozen_eval` runs in the same Fold Docker after Agent stop and write lock.

Validation:
- Targeted residual search confirmed active docs no longer mention `record_type=fold_test` or separate Fold frozen-test ledger output.
- `git diff --check` passed.
- No workload was started.

## 2026-06-09 Strategy artifact vs run artifacts wording

Task: clarify that historical results and Agent conversations are retained, but not stored in the strategy handoff package.

Decision:
- `strategy_artifacts/` should be the minimal reusable package passed between Folds/Epochs: `factor/`, `nl_prior/`, and `manifest.json`.
- Historical `results/`, Agent conversations, Shell/Tool traces and debug materials remain useful and should be preserved under `artifacts/<run_id>/`.
- Chapter 7 should not repeat the next-Fold copy procedure already described earlier in Pipeline docs.

Changes:
- Updated `docs/pipeline_design.md` section 7.4 wording.
- Removed the repeated next-Fold copy sentence from that section.

Validation:
- Targeted search confirmed the old "do not save historical results or Agent conversations" wording was removed from active docs.
- `git diff --check` passed.
- No workload was started.

## 2026-06-09 Run artifact wording precision

Task: replace vague retained-artifact wording with concrete file names.

Decision:
- Use concrete retained artifact names in Pipeline chapter 7.
- The retained run evidence is `results/`, `agent_trace.jsonl`, `run_manifest.json`, and optional `workspace/` debug materials.

Changes:
- Updated `docs/pipeline_design.md` section 7.4.

Validation:
- Targeted section read confirmed the wording now uses concrete file names.
- `git diff --check` passed.
- No workload was started.
