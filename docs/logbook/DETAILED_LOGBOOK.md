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

## 2026-06-09 Design document audit fixes

Task: address concrete issues from a read-only audit of Data, Agent, Environment and Pipeline design documents.

Changes:
- Fixed Pipeline strategy artifact examples to use the quarterly Fold convention (`fold_2022Q1`, `strategy_epoch001_fold2022Q1`) and the initial template parent for the startup Fold.
- Defined the natural-language score contract as `nl_score ∈ [-1, 1]` and `confidence ∈ [0, 1]`, with the default factor/NL blend using the same score scale.
- Replaced a stale Data document reference to `decision_observation` and “特征单位统一” with the current Environment “单位合同”.
- Clarified that `/mnt/snapshots/valid` stores validation replay data, while validation results live under `/mnt/artifacts/results/valid_<idx>/`.
- Removed the duplicated numeric snapshot-switching example from Pipeline and kept Environment as the owner of `/mnt/snapshot` binding mechanics.
- Clarified that Epoch regularization cannot call formal `backtest_tool` to create return backtests; it may only pass modification checks and a read-only contract check.
- Made Environment the canonical source for default visible-window lengths and detailed natural-language scoring modes; Agent and Pipeline now reference that contract instead of copying the full values.
- Documented the minute update/audit distinction: new trading days attempt the `daily` universe, while existing by-date minute files use the local minute coverage口径; strict daily coverage remains a专项排查.

Validation:
- Targeted grep checked removal of stale `decision_observation`, month-style strategy artifact examples, and duplicated Pipeline snapshot example.
- Source grep confirmed TuShare intraday update/audit defaults already use `expected_codes_source=minute` for existing by-date validation paths.
- `git diff --check` passed.
- Documentation-only change; no data download, audit, training or inference workload was started.

## 2026-06-10 Tool and auction boundary cleanup

Task: finish the remaining documentation issues from the same design-doc audit: duplicated `backtest_tool` semantics and owner-less auction correction constants.

Changes:
- Updated `docs/agent_design.md` so Agent docs no longer list the authoritative `backtest_tool` internal execution order. Agent docs now state only what the Agent prepares, requests, and reads; Environment remains the owner of candidate validation, natural-language scoring, score synthesis, order generation, trading constraints, and Broker replay order.
- Updated `docs/data_documentation.md` so the深圳 09:30 minute-vs-auction issue is recorded as a raw-data risk without hard-coding Environment-layer constants.
- Updated `docs/environment_design.md` unit/口径 contract with the actual derived-field rule: when historical 09:30 minute bars are used as live `stk_auction` proxy features, Shenzhen `00*.SZ` uses multiplier `0.76`, Shenzhen `30*.SZ` uses `0.58`, and SH/BJ/other times keep `1.0`; raw data and 15:00 close auction are not rewritten.

Validation:
- Targeted section reads confirmed Agent docs point to Environment for the hard `backtest_tool` contract.
- Targeted grep confirmed the auction multiplier constants only remain in Environment docs and code/audit references, not in Data's risk table.
- `git diff --check` passed.
- Documentation-only change; no workload was started.

## 2026-06-10 Backtest defaults, Sharpe and convergence docs

Task: add the requested default capital/cost assumptions, Sharpe evaluation metric and Pipeline convergence criterion at the design-document level only.

Changes:
- Documented the target default Broker profile in `docs/environment_design.md`: initial capital `1_000_000` CNY and commission `1.0 bps`.
- Added Sharpe to Environment result statistics, Pipeline validation/early-stop language and Fold output examples, and Agent early-stop criteria.
- Added a Pipeline convergence criterion: if recent Step modification deltas for `factor/` and `nl_prior/` shrink while validation return, Sharpe and risk constraints do not deteriorate materially, the Fold search can be considered converging.
- Added Prompt priority guidance: first protect validation return, Sharpe and risk constraints; second, when performance is close or marginal gains are small, prefer the smaller and simpler factor/prior modification.
- Added `modification_delta_summary` to Pipeline Step summaries so convergence can be derived from `modification_check_tool` diff outputs.
- Reverted the initial code/test attempt after the user clarified this should be documentation-only.

Validation:
- Confirmed no code/test diff remains after reverting the initial implementation attempt.
- `git diff --check` passed.
- Targeted grep confirmed the documented default capital/commission, Sharpe references and Pipeline convergence guidance are present in the active docs.

## 2026-06-10 Concentrated holding target

Task: keep the final tradable portfolio concentrated without constraining Agent output count directly.

Changes:
- Updated `docs/agent_design.md` so `generate_candidates()` returns factor-scored candidates, not final orders; Agent output count is not the concentration control.
- Updated `docs/environment_design.md` so `backtest_tool` applies `factor_score_threshold` from run manifest, then keeps the top `max_holdings` names by `factor_score`; default `max_holdings=10`.
- Updated `docs/pipeline_design.md` so Pipeline writes `factor_score_threshold` and `max_holdings=10` into run manifest and validates that final holdings follow this rule.
- Updated the factor template README and starter comment so future Agents understand that threshold filtering and top-10 selection happen inside `backtest_tool`.

Validation:
- Targeted grep confirmed active docs and factor template no longer contain the old `30-100` wording or Agent-output-count limit wording.
- `git diff --check` passed.

## 2026-06-10 Short-side mechanism design

Task: add the requested securities-lending short mechanism at the design-document level while keeping the Agent free to learn bull/bear regime behavior through directional scores.

Changes:
- Updated `docs/environment_design.md` so `backtest_tool` combines factor and natural-language scores into `final_score`, then uses `final_score >= +0.7` for long orders and `final_score <= -0.7` for broker-constrained short orders. The default portfolio cap is now 10 total long+short names, ranked by absolute final score.
- Added the 中信 short-side Broker profile boundary: 100% securities-lending margin ratio for ordinary investors, 120% for private securities funds, broker-provided per-security/per-contract borrow fee, PIT券源 quantity, maintenance collateral ratio risk lines, borrowing fee accrual and forced-cover accounting.
- Updated Data docs to distinguish TuShare `margin_secs` exchange eligibility from actual 中信 account borrow inventory, fees and credit-risk parameters, which must come from local broker files with `available_at`.
- Updated Agent docs and the factor/NL templates so `factor_score` is explicitly directional: positive is bullish/long preference, negative is bearish/avoid/short preference. The Agent does not output orders or check broker inventory.
- Updated Pipeline docs so run manifests carry `long_score_threshold`, `short_score_threshold`, `max_total_holdings` and `short_mode`, while Environment resolves the default 中信 Broker profile and writes actual costs,券源、费率 and 风控 sources into `run_manifest.json`. Validation summaries record long/short return splits and short reject counts.

Validation:
- Targeted grep checked removal of the active `factor_score_threshold`/`max_holdings` wording from current docs/templates and confirmed the new long/short threshold language is present.
- `git diff --check` passed.
- `git diff -- src tests` was empty.
- Documentation/template-only change; no runtime code, tests, data download, audit, training or inference workload was started.

## 2026-06-10 Environment shell tool contract

Task: add the missing detailed `sandbox_shell_tool` contract to Environment chapter 4.

Changes:
- Moved the detailed shell boundary from the Sandbox/Runner discussion into `docs/environment_design.md` chapter 4 as `4.2 sandbox_shell_tool`.
- Kept chapter 3 as a short Runner/Sandbox summary and linked it to the chapter 4 contract.
- Documented allowed shell uses, readable/writable paths, read-only files, invisible paths, no-network/non-root constraints, resource limits, command boundaries for `rg`/`sed`/`python`/`apply_patch`, and automatic `agent_trace.jsonl` logging.
- Clarified that Shell outputs are observations only and cannot replace `modification_check_tool` or `backtest_tool`.

Validation:
- Checked Environment table of contents and section numbering.
- `git diff --check` passed.
- Documentation-only change; no runtime code, tests, data download, audit, training or inference workload was started.

## 2026-06-10 Short inventory proxy mode

Task: simplify the current short-side borrow-availability assumption so research can proceed before real CITIC inventory files are available.

Changes:
- Updated `docs/pipeline_design.md` so the current run manifest parameter is `short_inventory_mode=proxy_margin_secs`, not a real broker-inventory mode.
- Updated `docs/environment_design.md` so default short availability uses decision-date `margin_secs` membership: in-list means borrowable for research, out-of-list means reject. Individual borrow quantity is not constrained yet; position sizing and holding caps constrain exposure.
- Kept `broker_inventory` as the later live-approximation mode requiring real CITIC inventory, borrow quantity and per-security/per-contract fee files.
- Updated `docs/data_documentation.md` to record that `margin_secs` is currently a research proxy, not a true CITIC account inventory.
- Updated Agent/Pipeline wording so Agent does not reason about broker inventory and validation tracks `margin_secs` proxy rejections separately from true broker-inventory rejections.

Validation:
- Targeted grep checked current docs for `proxy_margin_secs`, `broker_inventory`, and old short-mode wording.
- Documentation-only change; no runtime code, tests, data download, audit, training or inference workload was started.

## 2026-06-10 Four living docs audit and presentation cleanup

Task: audit `docs/data_documentation.md`, `docs/environment_design.md`, `docs/agent_design.md`, `docs/pipeline_design.md` for logical flaws, incorrect/ambiguous descriptions, redundancy and blurred boundaries; optimize presentation without changing meaning.

Verification against implementation (all matched unless noted):
- Cron job names/times vs `configs/tushare_update_schedule.json` and `ops/cron/install_tushare_cron.py` managed block (23:35 / 02:30 / 03:35 / 04:00 / 08:50 / 08:55 / 09:03 / 09:05 / 09:13 / 09:15 / 09:20).
- Text page clamps (`anns_d=2000`, `major_news=400`, `npr=500`, `research_report=1000`, `report_rc=3000`, `news=1500`) and `STK_MINS_PAGE_LIMIT=8000` in `src/hl_trader/data_sources/tushare/common.py`.
- Audit CLI subcommands (`base [--include-text]`, `macro`, `intraday-by-date`, `event-flow`, `board-trading`, `auction-alignment`, `revision-sentinel`).
- Macro dataset list (`cn_m`, `sf_month`, `shibor_lpr`, `us_tycr/us_trycr/us_tbr/us_tltr`, etc.), `trade_cal_lookahead_days=7`, evening lookback 30 days, intraday refresh lookback 1 day, fundamental refresh 6 periods / 3 ann-months.
- Auction correction factors 0.76/0.58 in `src/hl_trader/environment/features/auction.py`; revision ledger paths and `downstream_status=pending_review`; `MQ_SNAPSHOT_DIR` in `configs/agent_output_template/`.
- moneyflow 19:00 / block_trade 21:00 / margin 09:00 / margin_secs pre-open visibility vs config interface metadata.

Fixed inconsistencies:
- data doc 3.3: pre-open retry jobs are `cn_preopen_margin_secs_retry_0913` and `cn_preopen_margin_retry_0915`, not `*_backfill_*`.
- data doc 3.3/4.1: `results/data_quality/` also holds feature-layer `fundamental_events_status.json` (from `cn_nightly_feature_build`); documented it as outside the 6 raw-data status files, alongside the revision ledger.
- pipeline 3.1: `/mnt/artifacts` tree showed `strategy_artifact_manifest.json`, absent from the authoritative Environment 3.2 layout; replaced with `run_manifest.json` + `agent_trace.jsonl`.
- agent 2.1: formal `generate_candidates()` restriction now names all `/mnt/snapshots/` stage dirs, not only `valid`/`results`.

Presentation-only edits (meaning preserved):
- environment 4.4: removed in-place duplication of candidate-return/threshold statements already covered by the adjacent column table and the "默认交易阈值" table.
- environment 2.2/2.4: removed repeated "Agent may use a shorter window" sentence and the fully redundant closing recap of the snapshot-slot walkthrough.
- environment 4.1: phase column renamed to 训练/验证/正则化; environment 6.3: LLM call-detail table reworded to separate full-detail location vs `agent_trace.jsonl` content.
- pipeline 3.2: dense pre-call preparation paragraph converted to a bullet list.

Report-only findings (no edit):
- Trade thresholds (+0.7/-0.7/max 10) restated in env 4.4/5.3/5.4, agent 4.1, pipeline 3.2/4.1; values are consistent today but drift-prone — Environment 5.3 profile should remain the single source when next touched.
- Short-inventory mode semantics duplicated between data doc 2.5/6 and environment 5.3 (boundary blur; data doc should keep only the data-contract part).
- `fundamental_events`/`daily_alpha` feature layer is built nightly but its contract appears in no living doc; environment doc only references `fundamental_events` indirectly.
- Decision-time examples use 09:20 (environment 2.2) while data doc 5.2 and cron comments gate at 09:25; harmless for historical research but worth unifying later.
- env 4.4 "复用当前产物对应的 modification_check_tool 结果" vs pipeline 3.2 "必须再次调度复查": consistent only if reuse is keyed to the current artifact hash; wording could be tightened later.

Validation:
- `git diff --check` passed; grep confirmed no `strategy_artifact_manifest` references remain in docs and threshold tables are intact.
- Documentation-only change; no runtime code, tests, data download, audit, training or inference workload was started.

## 2026-06-10 Docs-driven rebuild: Agent / Environment / Pipeline implementation

Task: fix the five report-only documentation issues from the same-day audit, then re-implement the system strictly per docs/{data_documentation,environment_design,agent_design,pipeline_design}.md, removing code inconsistent with the documented architecture. Docs were frozen after the five fixes; no doc was modified during implementation.

Doc fixes (before freeze):
- Threshold/holding-cap numbers now live only in Environment 5.3 (agent 4.1, pipeline 3.2/4.1, env 4.4/5.2/5.4 reference it).
- data doc 2.5 keeps only the margin_secs/broker-file data contract; short-mode execution semantics point to Environment 5.3.
- New Environment 2.5 "PIT 特征层产物" documents fundamental_events + daily_alpha contracts (paths, available_at rules, pct_chg-not-adj_factor rule, limit_list_d quarantine, build/audit entrypoints).
- Environment 2.2 example decision time unified to 09:25.
- Environment 4.4 step 1 reuse of modification checks is now explicitly artifact-hash-keyed.

Removed (superseded by current docs): src/hl_trader/agent/{formulaic.py,evidence/,shadow/,llm/}, environment/{backtest,evaluation,events,execution,leakage,portfolio,protocols,schemas,storage,wfo}/, pipelines/{experiment,formulaic_wfo,llm_shadow}.py (old), configs/experiments/pilot_2020_daily.yaml, legacy tests (test_agent, test_agent_shadow_pipeline, test_environment, test_pipeline, test_protocol_architecture). DeepSeek client tests and feature-layer tests were salvaged into tests/unit/test_llm_deepseek.py and test_features.py.

Kept: data_sources/tushare (data layer), environment/data (PIT raw store), environment/features (cron-live feature layer), configs/agent_output_template, scripts/tushare/*; scripts/hl.py trimmed to build-features / build-fundamental-events / audit-fundamental-events.

New implementation (src/hl_trader):
- environment/artifacts.py: factor//nl_prior schemas, AST checks (generate_candidates presence, registered functions, stage-dir string scan excluding docstrings/comments), sha256 artifact hash (caches excluded), deterministic ModificationDelta + ModificationConstraints.
- environment/runtime.py: SandboxPaths, RunManifest (atomic, latest-check summary, backtest summaries), AgentTraceWriter (shared ids, call_id/parent_call_id, secret redaction).
- environment/snapshot.py: decision snapshots (daily/intraday_1min/fundamentals/events/macro/text_index+text_library/universe + manifest + aggregate hash; unit contract conversions recorded; SZ 09:30 auction correction; events/macro/text filtered on raw available_at, fail-fast when absent) and replay slots (normalized daily bars, not PIT-filtered).
- environment/sandbox.py: SandboxSpec + docker run args rendering; LocalSandbox (layout, artifact install, ln -sfn-style snapshot binding, artifact collection).
- environment/broker.py: CITIC default profile (1bp, 1e6, ±0.7, cap 10, proxy_margin_secs, 100%/120% margin, maintenance lines, assumed borrow fee flagged), SimBroker (get_account/get_positions/submit_order/cancel_order/query_orders, lot rounding, suspension/limit/T+1, margin occupancy, forced close, reject counters incl. margin_secs_not_shortable).
- environment/backtest_engine.py: generate_candidates subprocess driver (MQ_SNAPSHOT_DIR pinned, PYTHONDONTWRITEBYTECODE), candidate validation, max-abs cross-section normalize, 0.7/0.3 composition, threshold/cap/hard_exclude order plan, plan validation, fixed-holding replay, 5.5 return statistics.
- environment/nl/: strict extraction (tool-call args > JSON mode > single JSON object; one fence; closed-think stripped, unclosed-think fails), score schema validation (ranges, ts_code, evidence ids), engine with ≤3 retrieval rounds, early-final, one repair call, terminal states completed/skipped_by_config/failed_with_policy/timeout/failed, thread pool, prompts containing only ts_code+context+rules; context builder from universe + visible fina_mainbz_vip.
- environment/llm/: DeepSeek client relocated (conversation JSONL logging intact), LLMProxy/DeepSeekProxy/ScriptedLLM.
- environment/tools/: sandbox_shell (logged, phase-gated, test-dir guard), modification_check (parent-hash verification before diff), backtest (mode valid/frozen_eval; nl off/sample/on; snapshot-binding verification vs run manifest; nl_output files; manifest summary + trace events; frozen nl=on enforced; regularization runs blocked), finish_fold (check + light contract check, write lock).
- agent/: prompts (protocol, wrap-up, anti-overfit, convergence) and AgentSessionRunner (one conversation per fold, JSON action protocol, llm_call full-detail trace events, deadline + finalize window, step counting, step cap).
- pipelines/: folds (quarter math, 21-month windows, 09:25 decision times, heldout periods + overlap assertion), ledger (single experiment_ledger.jsonl, record_type fold/epoch_regularization/heldout, link-key validation), experiment (fold run with snapshot provisioning, manifest fields, accept/freeze/fallback incl. no_update_timeout and initial-failure, frozen test eval with hash invariance, artifact collection to experiments/<id>/artifacts/<run_id>/, strategy_artifacts manifests per 7.4, regularization gating, held-out frozen runs).
- scripts/experiment.py: thin wiring entrypoint (docs define no CLI).

Validation:
- PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src compileall src scripts tests passed.
- Full unit discovery: 134 tests OK (new: artifacts, broker/engine, NL scoring, snapshot builder on synthetic raw, tool flow incl. frozen-eval guards and NL-failure audit trail, scripted agent session, pipeline E2E incl. regularization accept/reject, fallback-to-parent, held-out, fold schedule windows, ledger validation, environment-not-importing-agent boundary).
- CLI help checks for scripts/hl.py, scripts/experiment.py, scripts/tushare/download.py; cron dry-run cn_nightly_feature_build resolves the trimmed scripts/hl.py commands.
- git diff --check passed; RAM ~438Gi available; no GPU workload.

Not exercised in this session (reported as discrepancies for review): real Docker container runs and OS-level isolation, live-LLM agent sessions, full-scale 21-month snapshot builds on real raw data, real regularization LLM, corporate actions/stamp duty in short replay, per-step artifact snapshots for mid-fold rollback.

## 2026-06-10 Implementation hardening, Docker driver, and on-machine evaluation

Task: close the 26 review directives on the docs-driven rebuild, then update the living docs.

Host facts verified first:
- Docker 29.5.2 installed but `lzp` is not in the `docker` group and has no sudo: containers cannot run in this session. One-time admin fix: `sudo usermod -aG docker lzp`, re-login, then `docker build -t macroquant-sandbox:latest -f ops/docker/sandbox.Dockerfile ops/docker`; the gated `DockerSandboxE2ETest` then runs automatically.
- CITIC maintenance lines fetched from https://pb.citics.com/trading/xxgs/wcdbbl/: 平仓 130% / 安全 140% / 提取 300% for the >200% base case; concentration-dependent variants below 200% exist and are not modeled.
- Raw available_at conventions match data doc 5.2 exactly (margin next-day 09:00, margin_secs same-day 09:00, moneyflow 19:00, top_list 20:00, block_trade 21:00). `anns_d.rec_time` / `report_rc.create_time` are TuShare ingestion times for backfilled history (e.g. 2025), so those documents are invisible in historical windows under the strict wall — conservative, recorded as a data risk with a candidate ingestion fix.
- `index_member_all` has `in_date`/`out_date` → as-of industry implemented.

Code changes:
- broker.py: profile v2 (min commission ¥5, sell-side stamp duty 10bps→5bps at 20230828 incl. short opening sales, slippage 5bps adverse on fills/closes, short_corporate_actions="disabled" flag, maintenance 1.30/1.40/3.00 with source URL); stamp duty tracked separately in stats.
- backtest_engine.py: candidates truncated to Top-100 by abs(factor_score) before NL (recorded in summary); score-proportional weights (gross = n/max_total_holdings capped at 1.0, per-name cap 20%, no redistribution); nl=sample composes unsampled names with the mean sampled nl_score; generate_candidates now runs through an executor abstraction.
- executor.py (new): LocalExecutor and DockerExecutor (docker exec --user agent, /mnt path mapping, /mnt/snapshot special case); sandbox.py: DockerSandbox lifecycle (detached container, root-bound container symlink, stop), SandboxSpec.from_host_fraction(0.10) reading nproc/MemTotal, filesystem permission enforcement (READMEs/parent_output 0444/0555, test slot 0700, lock/unlock agent_output), hardlinked replay-slot installs, resolved-absolute sandbox roots (fixes a relative-symlink bug found during the real-data run).
- snapshot.py: per-dataset parquet text library shards (avoids millions of files; 21-month real text window = 721k docs), to_cn_timestamps() for naive/aware available_at mixes, as-of industry membership, replay slots now include events/text/minutes (configurable).
- nl/engine.py: retrieval ranked by keyword match count then recency, 2000-char snippets from lazy per-dataset body shards.
- llm: DeepSeek reasoning_content captured in _parse_response; proxy from_env(thinking_enabled=True) default; verified live that thinking+JSON mode coexist on deepseek-v4-flash (149-token smoke).
- agent: protocol prompt now states Top-100/Top-10 rules and presents the workflow as a recommendation with Environment-enforced hard rules; deadline dispatch guard refuses new tool calls past the deadline (analysis: no preemptive kill needed — per-call timeouts bound the overrun to one call); regularization session mode (backtest/finish_fold rejected, done action) with REGULARIZATION_INSTRUCTION prompt.
- pipelines: docker wiring in fold/regularization/heldout with try/finally container stop; development_history.json staged into the regularization workspace and listed in the manifest; agent_output locked before frozen eval; /experiments/ now gitignored; scripts/experiment.py gains --use-docker/--no-thinking and a real LLM regularizer.

On-machine evaluation (real data):
- Decision snapshot at 2021-10-08 09:25, full default config: 2.1G; daily 1,745,235 rows (max trade_date 20210930 — same day correctly invisible), events 8,688,422 (latest = margin_secs same-day 09:00), minutes 5,406,353 (5 days, corrected columns present), fundamentals 492,416, macro 7,535, text index 720,871, universe 4,380; hash verified.
- Full fold_2022Q1 run (snapshots + replay slots incl. quarter minutes + live NL on valid and frozen test): 680 s end-to-end. Validation +5.68% (sharpe 2.23, maxDD 2.6%, 1 order of 4 candidates passed the ±0.7 gate; fees ¥25.62 incl. min-commission floor, stamp duty ¥156.57, turnover 0.2). Frozen test +5.47% (sharpe 1.49). fold_status=frozen, selected step_001, state_changed_during_test=false; artifacts collected (584K) under experiments/exp_eval_realdata_001.
- Live LLM: 15 calls, 0 errors, all 8 NL tasks completed; 57,699 prompt + 5,782 completion tokens (4,811 reasoning) — negligible cost at v4-flash rates. Conversation JSONL audit logging active.
- RAM stayed ≥391Gi available throughout; sandbox workdirs cleaned after collection (the agent_output write-lock blocking rm confirmed the fs enforcement).

Living docs updated (env 3.1 Docker/permissions/resources, 4.4 truncation/weights/sample-average/retrieval, 5.3 cost rows + verified CITIC lines, 5.5/6.2 additions, 2.4 replay-slot and text-library formats; agent 3.2/4.1; pipeline 3.1 final-state-only retention and 5.2 regularization protocol; data doc rec_time risk row). Validation: 146 unit tests OK (1 docker-gated skip), compileall, CLI helps, git diff --check.

## 2026-06-10 Rootless Docker enablement, text available_at repair, v4-pro default

Task: enable rootless Docker and run the dockerized E2Es; fix anns_d/report_rc PIT visibility per official field semantics; full-body retrieval snippets; switch the default provider model to deepseek-v4-pro; final doc alignment.

Rootless Docker:
- `dockerd-rootless-setuptool.sh install --force` (rootful socket exists but is inaccessible to lzp); user systemd service started; CLI context "rootless"; cgroup v2/systemd driver.
- Docker Hub unreachable from this host: pulled `python:3.10-slim` via `docker.m.daocloud.io`, retagged, and built `macroquant-sandbox:latest` with `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` (Dockerfile now takes the index as an ARG).
- Rootless subuid mapping fix: container `agent` (uid 61000) maps to a subuid, so the agent-writable surface is world-writable on the host (`workspace/` 0777; `agent_output/` 0666/0777 on unlock) while READMEs stay 0444, `parent_output/` 0444/0555, and the test slot is 0700 from `prepare_layout` (re-applied on install). Read-only/invisibility guarantees are mount- and mode-based, unchanged.
- Live verification: `DockerSandboxE2ETest` (agent-user exec, workspace write through the mount, `ls /mnt/snapshots/test` denied) and a new gated `DockerizedFoldE2ETest` (full pipeline fold with containerized `generate_candidates` via `docker exec --user agent`, `MQ_SNAPSHOT_DIR=/mnt/snapshot`, frozen fold, positive replays) both pass for real. Full suite: 153 tests OK, no skips.

Text available_at fix (anns_d / report_rc):
- Official semantics (interface metadata captured from the TuShare docs; the site is now a JS SPA): anns_d `rec_time` is the publication-time field; report_rc updates daily 19:00-22:00. Empirically, backfilled history carries collection timestamps: anns_d 2020-01 lags ann_date by ~2000 days for 100% of rows; even 2026-01 is only 50% within ±3 days; report_rc 2020-01 lags ~840 days.
- Implemented in `augment_text_frame`: a date-anchored source timestamp is trusted only when `lag ∈ [-1, +3]` days; otherwise available_at falls back to `ann_date 23:59:59+08:00` (anns_d) / `report_date 22:00:00+08:00` (report_rc), rule `conservative_from:<date>:implausible_<time>`. Constants TEXT_TIME_PLAUSIBLE_BEFORE/AFTER_DAYS.
- New local maintenance command `scripts/tushare/download.py repair-text-available-at` re-derives the two columns on existing partitions without API calls; run on anns_d+report_rc: 152 files rewritten, 6,534,489 rows changed.
- Effect verified: the 2021-10-08 09:25 21-month window now sees anns_d 2,458,277 rows (max 2021-10-07 23:59:59) and report_rc 370,820 rows (max 2021-10-07 22:00) — previously 0; conservative, no future leakage.
- Unit coverage in tests/unit/test_text_available_at.py (plausible kept, backfill fallback, evening-before allowed, missing-time fallback, report_rc 22:00 rule, idempotent repair).
- Formal `text_evidence_status.json` full-scope refresh launched after the rewrite (a first targeted run had narrowed the scope; relaunched over all text datasets).

Retrieval and model:
- Snippets now return the full stored body by default (bodies capped at 4000 chars at snapshot build) — supersedes the 2000-char setting.
- Default model: `deepseek-v4-pro` in DeepSeekProxy.from_env and scripts/experiment.py; live thinking+JSON smoke OK (126 tokens, reasoning separated).

Docs: data doc 2.7 visibility table + plausibility paragraph + repair command, 5.2 text row, risk row updated to "fixed"; env doc 3.1 rootless note and 4.4 full-body snippet wording. Historical logbook references left as history.

Validation: compileall, 153 unit tests OK (docker E2Es live), `git diff --check`, repair/audit CLI help checks; RAM stayed safe throughout.

## 2026-06-10 Data-doc trims, prompt overhaul, visualization, full-epoch launch

Items: remove rt_min and broker-lending content from the data doc; optimize all prompts and export for audit; run one full epoch per docs; add result visualization; standardize doc structure.

- Data doc: dropped the 实盘/实时分钟 rt_min/rt_min_daily row (not in current scope) and all broker 券源/费率/信用风控 rows/paragraphs; `proxy_margin_secs` semantics restated as "decision-date margin_secs members are all treated as borrowable"; risk table condensed to one proxy-approximation row pointing at the future broker_inventory switch.
- Prompts: full rewrite in structured Chinese with English JSON keys. Fold Agent protocol now separates 角色/动作协议/环境硬约束/候选池与下单规则/推荐工作流/风格要求 and states the Top-100 truncation + Top-10 selection and the 0.7/0.3 composition explicitly. NL scoring round prompt defines per-field semantics (nl_score sign/magnitude, confidence behavior under missing context, risk_tags incl. hard_exclude, evidence_ids anti-fabrication); final/repair prompts are strict-JSON collapses. Regularization prompt lists allowed/forbidden moves and the done-action contract. `scripts/export_prompts.py` renders everything (incl. fully built system prompts with sample fold context) to `configs/prompts/PROMPTS.md` for review; code remains the source of truth.
- Visualization: `pipelines/reporting.py` reads experiment_ledger.jsonl and renders (1) per-fold dual-axis chart — validation return bars (left axis) vs frozen-test return line (right axis), held-out diamonds + shaded band, metrics table (valid/test return, Sharpe, maxDD, orders, short rejects) under the chart; (2) cumulative frozen-test equity with drawdown panel; (3) summary.json aggregates (mean/median/positive-rate/worst test return, fold status counts, held-out returns). CLI `scripts/report_experiment.py --experiment-id <id>`; unit-tested on a synthetic ledger.
- Doc structure: data doc chapter 1 aligned with the other docs (intro + 相关边界 + 术语说明 + 导航 + "1. 数据层职责与数据域"), added a mermaid raw→audit/features→snapshot→agent data-flow diagram; pipeline main path converted to a mermaid Fold/Epoch/Held-out lifecycle diagram; nav anchors of all four docs validated programmatically.
- Bug fixed en route: `_build_text` crashed on datasets without title/content/url columns (report_rc uses report_title/abstr) — only reachable after the rec_time repair made report_rc visible; per-dataset title/body column candidates with fail-fast when none exist; suite green.
- Full-epoch evaluation launched: `exp_epoch_eval_001`, folds 2022Q1..2025Q4 + regularization + held-out 2026Q1, real AgentSessionRunner on deepseek-v4-pro (thinking on), dockerized sandbox (rootless), budgets max_fold_minutes=10/max_steps=3/max_llm_calls=30/max_candidates=20/per_call_timeout=240, lenient acceptance (min_return=-1, maxDD=1) so losing quarters fall back instead of aborting; all budgets recorded in run manifests. Long background run: log .runtime/eval/epoch_eval.log; report via scripts/report_experiment.py when finished.
- Audit note: text_evidence_status refreshed at nightly-exact parameters after the text repair — text datasets show no errors; the remaining error is a pre-existing bak_basic "5 missing expected files" also present in last night's formal statuses, while a direct SSE-calendar recount finds 0 missing — audit expectation logic needs separate investigation.
- Verification: compileall; 155 unit tests OK (docker E2Es live); git diff --check; prompts export + affected suites rerun.

## 2026-06-11 Grep retrieval, Step artifact tree, exploration/convergence phases

Items: grep tool integration for Agent and NL analysis; per-Step artifact snapshots organized as a cross-Fold tree; free-exploration prompt; removal of prev-epoch outperformance constraint; n-th-epoch convergence prompts; doc updates.

- NL retrieval (src/hl_trader/environment/nl/engine.py): `TextRetriever.search(pattern, ...)` now uses grep semantics — case-insensitive regex over title+codes first, then full bodies via lazily loaded, lock-protected per-dataset shards when more hits are needed; ranking title-hit > body-only, recency second; `_safe_regex` falls back to literal on invalid patterns; `search_requests` accepts `{"pattern": ...}` with legacy `{"keywords": [...]}` mapped to an escaped alternation. Round prompt rewritten for grep usage. Tests: alternation, body-only grep, invalid-regex fallback, legacy mapping.
- Step tree (src/hl_trader/environment/step_tree.py): nodes `{node_id, parent_node_id, fold_id, result_name, artifact_hash, metrics, complete_validation, created_at}` in `steps/tree.json` plus full `factor/`+`nl_prior/` snapshots per node; `record_step` appends with parent=current and moves the position; `position_for_hash` locates the parent-artifact node; `render_ascii` marks the current position. BacktestTool records on every successful valid backtest when `step_tree_enabled`; ExperimentPipeline copies the experiment-level tree into each fold sandbox (hardlinks), positions it at the parent node, and syncs back to `experiments/<id>/steps/` after the fold. Toggle: `ExperimentConfig.step_tree_enabled` (default True) -> run manifest. Agent visibility via a prompt section; the tree is read-only for the agent (host-owned files under rootless Docker). Tests: lineage/position/render/duplicate guard, tool-level recording on/off, cross-fold handoff in the pipeline E2E.
- Phase prompts (src/hl_trader/agent/prompts.py): EXPLORATION_PHASE_PROMPT (hypothesis-driven exploration allowed even at lower returns; random no-hypothesis edits discouraged) and CONVERGENCE_PHASE_PROMPT (minimize modifications while holding returns; prefer validating the unchanged parent; no new factors/experience), selected by `build_system_prompt(phase=...)`; `ExperimentConfig.convergence_start_epoch` (default 3) drives `phase` in run manifests; AgentSessionRunner and scripts/experiment.py pass-through. Prompt also documents the steps tree when enabled.
- Constraint check: grep confirmed no code compares a fold against its previous-epoch counterpart; pipeline 4.1 now states acceptance uses only current-fold hard rules and cross-epoch convergence is prompt-guided.
- Docs: environment 3.2 (artifacts tree + ownership row + recording note), 4.4 (grep retrieval contract); agent 2.2 (steps tree visibility), chapter 3 (phase guidance); pipeline 3.1 (step_tree/phase inputs), 4.1 (no prev-epoch comparison), 5.3 (exploration->convergence schedule). configs/prompts/PROMPTS.md re-exported.
- Validation: 164 unit tests OK (docker E2Es live), git diff --check. The running evaluation epoch (attempt 5, exp_epoch_eval_001) uses pre-feature code; the new features apply from the next launch.

## 2026-06-12 Claude implementation audit follow-up: attribution, GPU allocation, and consistency

Task: audit Claude's newly implemented Agent/Environment/Pipeline code against the latest docs, then finish the remaining requested items: optional Shapley factor contribution analysis, GPU allocation for ML/NN experiments, separate Agent/NL model routing, code simplicity, and doc/code consistency.

Resource checks:
- Before tests: `free -h` reported 503Gi RAM total and 269Gi available; `nvidia-smi` showed 8 NVIDIA L20 GPUs, with GPU 4 essentially idle (4MiB used, 45457MiB free).
- After tests: RAM available was 279Gi; GPU 4 remained the freest L20.

Audit findings acted on:
- Step-tree node IDs were only `fold_id__result_name`, so repeated Epochs could collide on the same Fold/result name.
- Docker GPU support only allocated one device with `gpu="auto"` and did not record the resolved GPU allocation in `run_manifest.json`.
- Debug validations (`nl=off/sample`) could still pollute the Step tree and Step ledger even though only complete `nl=on` validations should count as formal Steps.
- Factor attribution could be enabled while the strategy supplied no registered factor columns, producing an unhelpful attribution report.
- Docker image used Python 3.10 while the project requires Python >=3.11, and dependency versions were unpinned.
- The Agent-facing `factors.json` example in docs missed the required `rationale` field.
- `scripts/export_prompts.py` required an external `PYTHONPATH`; it now prepends repo `src/` itself.

Implementation details:
- `src/hl_trader/environment/gpu.py`: added `select_gpus(count, require_name="L20")`, selecting matching GPUs by descending free memory. `select_gpu()` now delegates to it.
- `src/hl_trader/environment/sandbox.py`: `SandboxSpec` now has `gpu_count` and `gpu_name_filter`; DockerSandbox accepts auto, fixed integer, or fixed list GPU requests; multi-GPU Docker args are rendered as `--gpus=device=<ids>`. Auto CPU fallback is only allowed when `nvidia-smi` is unavailable/no GPUs; insufficient requested L20 GPUs fails explicitly. `allocation_record()` records container, image, requested GPU mode, count, filter, and actual GPU indices.
- `src/hl_trader/pipelines/experiment.py`: after Docker start, Pipeline writes `sandbox_runtime` to the run manifest. Fold acceptance, Step summaries and Step IDs now consider only complete validations. This keeps off/sample results in `results/` for debugging but prevents them from freezing artifacts or consuming formal Step lineage.
- `src/hl_trader/environment/step_tree.py`: `record_step()` accepts `epoch_id` and optional file attachments. Node IDs include epoch when provided, and the tree can copy small per-Step attachments such as `factor_attribution.json`.
- `src/hl_trader/environment/tools/backtest.py`: complete Shapley-enabled validation now requires at least one registered factor and at least one corresponding `factor_<id>` candidate column. Backtest still writes `results/<phase>_<idx>/factor_attribution.json`; complete validation Step nodes also keep a copy as an attachment. Shortable-code loading now uses the decision date instead of the first replay date.
- `ops/docker/sandbox.Dockerfile`: moved to `python:3.11-slim`; pinned pandas/numpy/pyarrow/duckdb/scikit-learn/statsmodels/torch versions.
- `docs/agent_design.md`, `docs/environment_design.md`, `docs/pipeline_design.md`, `configs/prompts/PROMPTS.md`: updated the factor rationale, attribution, multi-GPU and complete-Step contracts. Prompt snapshots were regenerated from code.
- Tests updated for multi-Epoch Step-tree IDs, debug-vs-complete Step semantics, factor-attribution attachments and failure behavior, GPU selection, fixed GPU lists, and two-Epoch pipeline E2E collision protection.

Validation commands:
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python scripts/export_prompts.py` initially failed because `hl_trader` was not importable without `PYTHONPATH`; after fixing the script, `/home/lzp/miniconda3/envs/stock/bin/python scripts/export_prompts.py` succeeded.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m pytest ...` failed because the stock env has no pytest installed. No package installation was performed.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_step_tree tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_attribution` ran 41 tests OK.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests -t . -p 'test_*.py'` ran 175 tests OK. A first discover attempt from `tests/unit` failed only because unittest loaded modules without package context and relative imports broke; rerun with `tests` as start directory and repo root as top-level succeeded.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_sandbox_isolation` reran 12 tests OK after the GPU fallback tightening.
- `git diff --check` completed with no whitespace errors.

Notes:
- Full Docker image rebuild and real container GPU runtime execution were not performed in this follow-up; tests cover argument generation/selection logic and existing Docker-gated tests remain available when the image is present.
- The working tree still contains the larger Claude refactor and untracked `check.ipynb`; this session did not revert or stage unrelated changes.

## 2026-06-12 Sandbox tooling and Step-tree hardening

Task: supplement the remaining review fixes after the grep/Step-tree/convergence implementation, with `ripgrep` available to the Agent and an independent SubAgent audit after completion.

Resource checks:
- Before targeted tests: `free -h` reported 503Gi RAM total and 334Gi available; `nvidia-smi` showed GPU 3 as the freest L20 with 45457MiB free.
- Before full tests: RAM available was 325Gi; GPU 3 remained the freest L20 with 45166MiB free.
- After full tests and `git diff --check`: RAM available was 314Gi; GPU 3 had 45457MiB free.

Implementation details:
- `ops/docker/sandbox.Dockerfile`: installed `ripgrep` with `apt-get` before Python dependencies, so `rg` is available inside the Agent sandbox image.
- `src/hl_trader/environment/tools/shell.py`: added a Step-tree write guard. Commands may read `/mnt/artifacts/steps` or the local mapped `steps/` path with tools such as `rg`/`cat`/`ls`, but write-like commands (`touch`, `rm`, `mv`, `cp`, `tee`, `dd`, redirection, Python, etc.) are rejected.
- `src/hl_trader/environment/runtime.py` and `src/hl_trader/environment/sandbox.py`: added `steps` to the artifact top-level contract and create the directory during sandbox layout preparation.
- `scripts/experiment.py`: exposed `--convergence-start-epoch` and `--disable-step-tree`, wiring them to `ExperimentConfig.convergence_start_epoch` and `ExperimentConfig.step_tree_enabled`.
- `src/hl_trader/environment/tools/backtest.py`: each validation backtest summary now carries `modification_delta_summary`, derived from the latest matching modification check.
- `src/hl_trader/pipelines/experiment.py`: Fold Step ledger entries now include the `modification_delta_summary` from the corresponding backtest summary.
- `docs/environment_design.md`, `docs/agent_design.md`, and `docs/pipeline_design.md`: documented the sandbox `rg` dependency, Step-tree read-only boundary, Step-tree run artifact collection, and experiment CLI toggles.
- Tests: added coverage for reading but not writing the Step tree through `sandbox_shell_tool`, and for `modification_delta_summary` appearing in the pipeline Step record.

Validation commands:
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_step_tree tests.unit.test_nl_scoring` ran 45 tests OK.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests -t . -p 'test_*.py'` ran 176 tests OK.
- `git diff --check` completed with no whitespace errors.
- Generated Python cache directories were removed after tests.

Notes:
- `rg` was already available on the host at `/usr/bin/rg`; only the Docker image needed explicit installation.
- No Docker image rebuild or real container run was performed in this step.
- The working tree still contains the broader Claude refactor and untracked `check.ipynb`; this step did not revert or stage unrelated changes.

SubAgent audit follow-up:
- A GPT-5.5 xhigh read-only audit found one blocking bug and two cleanup issues. The blocking bug was that `sandbox_shell_tool` only matched absolute Step-tree paths; because the shell cwd is `/mnt/artifacts`, commands such as `touch steps/agent_write` could write the Step tree in local mode.
- Fix: `sandbox_shell_tool` now recognizes absolute and relative Step-tree references (`steps/...`, `./steps/...`, `cd steps`, `pushd steps`) and rejects write-like commands against them while still allowing read-only commands such as `rg`, `cat`, `ls` and `sed`.
- Fix: Pipeline Step records no longer point `modification_check_ref` at `run_manifest.last_modification_check`, which only stores the latest check and can mislead multi-Step audits. The record now points to the embedded `modification_delta_summary` captured at the backtest time.
- Fix: `docs/environment_design.md` now says Docker is recommended for formal experiments and enabled with `--use-docker`; this matches `ExperimentConfig.use_docker=False` and the CLI default.
- Additional tests: relative Step-tree write attempts (`touch steps/...`, `touch ./steps/...`, redirection, `cd steps && touch ...`) are now covered; Pipeline E2E checks the embedded modification-check reference.
- Re-validation after fixes: `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_pipeline_e2e` ran 23 tests OK; full unittest discovery ran 176 tests OK; `git diff --check` passed; caches were cleaned again.

## 2026-06-12 Sandbox writable-surface simplification and Docker-default alignment

Task: simplify the Agent sandbox file layout after the Step-tree write-guard audit. The goal was to reduce redundant string-based protection, make formal Docker execution the default path, and make the file ownership boundary easier to understand and maintain.

Resource checks:
- Before final validation: `free -h` reported 503Gi RAM total and 407Gi available.
- `nvidia-smi --query-gpu=index,name,memory.used,memory.free --format=csv,noheader,nounits` showed 8 NVIDIA L20 GPUs; GPU 3 was the freest with 43158MiB free.

Implementation details:
- `src/hl_trader/environment/runtime.py`: added a separate `SandboxPaths.agent` root. Agent-writable paths now live under `/mnt/agent`: `workspace/` for scratch work and `agent_output/` for formal strategy output. Trusted artifacts remain under `/mnt/artifacts`: `run_manifest.json`, `agent_trace.jsonl`, `parent_output/`, `results/`, `steps/`, and `logs/`.
- `src/hl_trader/environment/sandbox.py`: Docker now mounts `/mnt/artifacts` read-only and `/mnt/agent` read-write. Local layout preparation mirrors that split, and artifact collection gathers both runtime roots back into the host run-artifact directory. This keeps the existing host artifact layout stable while separating write authority inside the sandbox.
- `src/hl_trader/environment/executor.py`, `src/hl_trader/environment/tools/shell.py`, and `src/hl_trader/environment/backtest_engine.py`: default execution cwd moved to `/mnt/agent`; formal candidate execution runs from the Agent root.
- `src/hl_trader/environment/tools/shell.py`: removed the redundant Step-tree-specific command parser. With `/mnt/artifacts` mounted read-only in formal Docker runs, Shell only keeps simple hard denials for the test snapshot and Docker socket. Local executor remains a development/test convenience, not the security boundary.
- `ops/docker/sandbox.Dockerfile`: workdir changed to `/mnt/agent` and `/mnt/agent` is created in the image.
- `src/hl_trader/pipelines/experiment.py` and `scripts/experiment.py`: formal experiments now default to Docker. The old positive `--use-docker` flag was replaced with `--local-dev`, which is explicitly for development/tests. `scripts/experiment.py` now prepends repo `src/` to `sys.path`, so direct stock-Python invocation works without an external `PYTHONPATH`.
- Agent templates, prompts and living docs were updated to use `/mnt/agent/workspace` and `/mnt/agent/agent_output`; `/mnt/artifacts` is now described as trusted/read-only to Agent.

Validation commands:
- `/home/lzp/miniconda3/envs/stock/bin/python scripts/export_prompts.py` succeeded and regenerated `configs/prompts/PROMPTS.md`.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_step_tree` ran 40 tests OK.
- `PYTHONPATH=src /home/lzp/miniconda3/envs/stock/bin/python -m unittest discover -s tests -t . -p 'test_*.py'` ran 177 tests OK.
- `/home/lzp/miniconda3/envs/stock/bin/python scripts/experiment.py --help` succeeded and showed `--local-dev`.
- `git diff --check` completed with no whitespace errors.
- Generated cache directories (`__pycache__`, pytest/mypy/ruff caches if present) were removed after validation.

Notes:
- Historical logbook entries still mention older `/mnt/artifacts/workspace` or explicit `--use-docker` behavior; those are preserved as history. Current living docs, templates, prompts and code use the new `/mnt/agent` split.
- The sandbox Docker image was not rebuilt in this step. Rebuild `macroquant-sandbox:latest` before the next real Dockerized experiment so the image workdir and installed tools match the updated Dockerfile.
- The broader Claude refactor and untracked `check.ipynb` remain unstaged/uncommitted; this task did not revert unrelated work.

## 2026-06-12 quant environment, script bootstrap, artifact hardening, and docs cleanup

Task: follow up on the broader repo-structure audit. Create the new local Python environment, make the current docs/config point to it, simplify script import bootstrapping, split small common modules out of TuShare/Pipeline, harden formal strategy artifacts, and keep the living docs aligned.

Resource checks:
- `nvidia-smi` before validation showed all 8 NVIDIA L20 GPUs already in use; no real Fold/API run was launched in this step.
- `free -h` before/after validation showed about 398-399Gi available RAM.

Implementation details:
- Created `~/miniconda3/envs/quant` with Python 3.11.15 and installed the project plus required scientific/ML packages.
- Updated `AGENTS.md`, `CLAUDE.md`, `docs/data_documentation.md`, `ops/cron/tushare_update.cron`, and `configs/tushare_update_schedule.json` so new local scripts, cron jobs, tests and non-Docker tools use `/home/lzp/miniconda3/envs/quant/bin/python`.
- Documented that Docker Sandbox Python is independent from the outer conda environment and is controlled by `ops/docker/sandbox.Dockerfile`.
- Added `scripts/_bootstrap.py` and wired direct script entrypoints through it. The helper walks upward from the script path to find the real repo root, so both top-level scripts and nested `scripts/tushare/*` entrypoints work without external `PYTHONPATH`.
- Split TuShare low-level IO helpers into `src/hl_trader/data_sources/tushare/io.py`; `download.py` and `audit.py` now explicitly import `pyarrow.parquet as pq` where direct schema inspection is used.
- Split Pipeline config/record types and the default raw snapshot provider into `src/hl_trader/pipelines/config.py`, while preserving public imports from `hl_trader.pipelines`.
- Added formal candidate-generation isolation in `src/hl_trader/environment/backtest_engine.py`: during `generate_candidates()`, existing `/mnt/snapshots/train|valid|test` stage directories are temporarily hidden from the non-root Agent user, leaving `/mnt/snapshot` as the formal decision-input view.
- Hardened `src/hl_trader/environment/artifacts.py` so strategy artifacts reject symlinks and non-regular special files during hash/diff/load/copy checks.
- Updated `docs/environment_design.md`, `docs/agent_design.md`, `docs/pipeline_design.md`, and `docs/data_documentation.md` for the current script structure, candidate isolation, artifact rules, and reduced repeated path descriptions.
- Copied the long root logbook to `/Data/lzp/MacroQuant_archive/logbook/LOGBOOK_before_20260612_simplification.md` and replaced `LOGBOOK.md` with a concise current-state summary.

Validation commands:
- `/home/lzp/miniconda3/envs/quant/bin/python --version` -> Python 3.11.15.
- `/home/lzp/miniconda3/envs/quant/bin/python -c "import hl_trader, pandas, pyarrow, torch, duckdb"` succeeded; torch version was `2.5.1+cu124`.
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/hl.py --help` succeeded.
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiment.py --help` succeeded and showed `--local-dev`.
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/report_experiment.py --help` succeeded.
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/tushare/download.py --help` succeeded.
- `/home/lzp/miniconda3/envs/quant/bin/python ops/cron/install_tushare_cron.py --dry-run` succeeded and rendered the managed block with `QUANT_PYTHON`.
- First full unittest run exposed missing explicit `pq` imports in the TuShare split; after fixing, targeted TuShare regression tests passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v` ran 179 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m compileall -q src scripts tests` passed.
- `git diff --check` passed.
- Generated `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.pyc`, and `*.pyo` under `src/`, `scripts/`, and `tests/` were removed after validation.

Notes:
- The cron template/config now point at `quant`, but the live crontab was not refreshed in this step pending the final audit.
- No real LLM API or Fold smoke was launched because this step changed structure, environment, docs, and local isolation; the existing unit + Docker E2E coverage exercised the touched paths directly.
- A GPT-5.5 xhigh SubAgent read-only audit was started after local validation to check docs/code consistency and remaining simplification opportunities.

SubAgent audit follow-up:
- The GPT-5.5 xhigh audit found a real high-severity isolation issue: Docker mounted the whole `snapshot_views` directory under `/mnt/runtime/snapshot_views`, so an Agent could inspect `test_decision_input` even though `/mnt/snapshots/test` was hidden.
- Fix: Docker no longer mounts `snapshot_views`. `LocalSandbox.bind_snapshot_view()` now refreshes a host-side `runtime/current_snapshot/` mirror with hardlinks/copies from the selected decision view; Docker mounts only that mirror read-only as `/mnt/snapshot`. `DockerSandbox.bind_snapshot_view()` refreshes the mirror instead of creating an in-container symlink to `/mnt/runtime/snapshot_views`.
- Added tests proving the mounted Docker command excludes `/mnt/runtime/snapshot_views`, the Agent can read `/mnt/snapshot/manifest.json`, and `ls /mnt/runtime/snapshot_views` fails inside the container.
- The same audit found symlink validation ordering and structured-return issues in `modification_check_tool`. Fix: `load_strategy_artifact()` validates the whole artifact tree before reading required files; `ModificationCheckTool` returns `allowed_to_backtest=false` with `artifact_hash=null` for invalid artifacts instead of re-raising during hash calculation.
- Low-risk cleanup: added explicit `pandas as pd` and `typing.Any` imports in TuShare `download.py`/`audit.py`; `.gitignore` now excludes `*.egg-info/` and local `check.ipynb`.
- Docs were updated to remove the old `ln -sfn /mnt/runtime/snapshot_views/...` container model and describe the current snapshot mirror boundary.

Re-validation after audit fixes:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_tools_flow tests.unit.test_artifacts -v` ran 37 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v` ran 181 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m compileall -q src scripts tests` passed.
- `git diff --check` passed.
- Cache directories and bytecode were removed after validation.

## 2026-06-12 exp_epoch_eval_001 report regeneration and result review

Task: inspect the previous experiment process/results and add one return-line chart per Epoch, with Held-Out appended to the final Epoch chart.

Resource checks:
- Before report generation: `free -h` showed 503Gi RAM total and 374Gi available; `nvidia-smi` showed all 8 L20 GPUs already occupied by unrelated processes. No GPU workload was launched.
- After report generation/tests: `free -h` still showed 374Gi available; GPU usage remained from unrelated processes.

Implementation details:
- Updated `src/hl_trader/pipelines/reporting.py` so `build_experiment_report()` writes `epoch_returns/<epoch_id>_returns.png` under the experiment report directory.
- The final Epoch chart appends Held-Out point(s); non-final Epoch charts contain only their development folds.
- Added `epoch_comparison_returns.png`, a professional Fold-aligned overview chart with one frozen-test return curve per Epoch, Held-Out appended to the final Epoch, and a metrics table containing Fold count, mean/median/cumulative return, positive rate, mean Sharpe, worst max drawdown, worst Fold, and Held-Out return.
- Added a missing-value plotting guard so absent validation/test values render as NaN rather than being passed through as `None`.
- Updated `tests/unit/test_reporting.py` to assert that per-Epoch charts and the cross-Epoch comparison chart are created and listed in the report summary.

Commands:
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/report_experiment.py --experiment-id exp_epoch_eval_001`
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_reporting -v`
- `/home/lzp/miniconda3/envs/quant/bin/python -m compileall scripts/report_experiment.py src/hl_trader/pipelines/reporting.py tests/unit/test_reporting.py`
- `git diff --check`

Generated report artifacts:
- `experiments/exp_epoch_eval_001/reports/fold_returns.png`
- `experiments/exp_epoch_eval_001/reports/cumulative_test_return.png`
- `experiments/exp_epoch_eval_001/reports/epoch_comparison_returns.png`
- `experiments/exp_epoch_eval_001/reports/epoch_returns/epoch_001_returns.png`
- `experiments/exp_epoch_eval_001/reports/summary.json`

Experiment summary:
- Ledger: 16 development Fold records, 1 Epoch regularization record, and 1 Held-Out record.
- Recorded process window: 2026-06-11T11:15:07Z to 2026-06-12T00:10:36Z, about 12.9 hours by ledger timestamps. Runtime log ended with `EPOCH_RESULT {"final_strategy_artifact": "strategy_epoch_002_regularized", "heldout_runs": 1}` and `EPOCH_SECONDS 49296`.
- Development frozen-test mean return: +5.37%; median: +3.12%; positive Fold rate: 81.25%; compounded development frozen-test return: +107.73%.
- Development mean test Sharpe: 1.10; mean max drawdown: 7.43%.
- Held-Out 2026Q1 return: -1.65%, Sharpe -0.96, max drawdown 3.61%.
- Best development Fold: 2023Q4, +51.73% test return. Worst development Fold: 2025Q3, -14.38% test return, mainly from short-side loss.
- Validation/test return correlation was about 0.20, so the one-Epoch validation signal is weak and should not yet be treated as stable generalization evidence.
- Step process: 27 validation backtests; selected steps were `step_001` 6 times, `step_002` 8 times, `step_003` 2 times. One rejected Step was caused by a candidate outside the visible universe.

Validation results:
- Reporting unit tests ran 2 tests OK.
- Targeted `compileall` passed.
- `git diff --check` passed.
- PNG pixel checks confirmed `epoch_comparison_returns.png` and `epoch_001_returns.png` are non-empty images.

## 2026-06-12 script organization and final report-output cleanup

Task: clean the script layout using a standard GitHub-style organization and keep only the requested experiment report images.

External organization review:
- `pandas-dev/pandas` keeps a top-level `scripts/` directory for thin maintenance checks and developer utilities.
- `pytorch/pytorch` uses a larger `tools/` tree with subdirectories by responsibility.
- `mlflow/mlflow` uses `dev/` for developer/build automation and groups related automation there.
- `ray-project/ray` keeps small runtime script entrypoints close to the relevant package.
- Conclusion for this repository: keep implementation in `src/hl_trader/`, keep `scripts/` as thin CLI wrappers, and group wrappers by responsibility rather than fully flattening them.

Implementation details:
- Reorganized script entrypoints:
  - `scripts/data/build_features.py`
  - `scripts/data/tushare_download.py`
  - `scripts/data/tushare_audit.py`
  - `scripts/data/tushare_cron_update.py`
  - `scripts/experiments/run_experiment.py`
  - `scripts/experiments/report_experiment.py`
  - `scripts/dev/export_prompts.py`
- Removed the old empty `scripts/tushare/` path and deleted the unused `scripts/data/tushare_common.py` wrapper.
- Updated docs, cron template/config, tests, and dynamic TuShare script loader references to the new script paths.
- Added repository-organization guidance to `AGENTS.md` and `CLAUDE.md`: scripts are grouped thin entrypoints; business logic belongs under `src/hl_trader/`.
- Updated `src/hl_trader/pipelines/reporting.py` so report generation deletes legacy `fold_returns.png`, `cumulative_test_return.png`, and `summary.json`, then writes only:
  - `experiments/exp_epoch_eval_001/reports/epoch_comparison_returns.png`
  - `experiments/exp_epoch_eval_001/reports/epoch_returns/epoch_001_returns.png`

Commands:
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_download.py --help`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_audit.py --help`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_feature_build --end-date 20260603 --dry-run`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/data/build_features.py --help`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/report_experiment.py --help`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/report_experiment.py --experiment-id exp_epoch_eval_001`
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_reporting tests.unit.test_data_sources_tushare -v` ran 54 tests OK.
- `/home/lzp/miniconda3/envs/quant/bin/python ops/cron/install_tushare_cron.py --dry-run` succeeded and rendered the new `scripts/data/tushare_cron_update.py` path.
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v` ran 181 tests OK.
- `/home/lzp/miniconda3/envs/quant/bin/python -m compileall -q scripts src tests` passed.
- `git diff --check` passed.
- PNG pixel checks confirmed the two remaining report images are non-empty: `epoch_comparison_returns.png` at 2448x1504 and `epoch_001_returns.png` at 2448x1632.
- Generated `__pycache__` and bytecode under `scripts/`, `src/`, and `tests/` were removed after validation.

Current conclusion:
- `scripts/` is no longer flat or TuShare-specific at the top level; it is grouped by data, experiment, and developer responsibilities.
- `results/data_quality/` and `experiments/<experiment_id>/` remain separate by design: the former is current data-quality state, the latter is run-specific experiment output.

## 2026-06-12 report layout follow-up

Task: fix label/title overlap in the per-Epoch report chart and add compounded equity curves to the cross-Epoch comparison chart.

Implementation details:
- Updated `epoch_returns/<epoch_id>_returns.png` layout:
  - increased figure height;
  - replaced arrow annotations for best/worst Fold with a compact top-right note box;
  - renamed the drawdown overlay to `Peak-to-current loss` to clarify that it means the current compounded equity is below its previous peak.
- Updated `epoch_comparison_returns.png` layout:
  - top panel now shows Fold return by Epoch;
  - middle panel now shows each Epoch's compounded equity curve, with Held-Out appended to the final Epoch;
  - bottom panel keeps the summary metrics table.
- Follow-up fix: the summary table axis was accidentally attached to the middle grid slot, overlapping the compounded-equity panel. It now uses the bottom grid slot and a bounded table box.

Commands:
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/report_experiment.py --experiment-id exp_epoch_eval_001`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_reporting -v` ran 2 tests OK.
- `/home/lzp/miniconda3/envs/quant/bin/python -m compileall -q src/hl_trader/pipelines/reporting.py tests/unit/test_reporting.py`
- `git diff --check` passed.
- PNG pixel checks confirmed non-empty images: `epoch_comparison_returns.png` at 2448x1920 and `epoch_001_returns.png` at 2448x1728.

## 2026-06-12 held-out range and NL model default check

Task: answer whether held-out can cover two quarters and set the default natural-language scoring model to DeepSeek V4 Flash.

Findings:
- `heldout_periods(first_quarter, last_quarter, trading_days)` already expands every quarter in the inclusive range, and `ExperimentPipeline.run_heldout()` runs one frozen evaluation per generated period.
- Therefore a configuration such as `--heldout-first-quarter 2026Q1 --heldout-last-quarter 2026Q2` creates two held-out runs, as long as local `trade_cal` and snapshot source data cover both quarters.
- DeepSeek official API docs list `deepseek-v4-flash` and `deepseek-v4-pro` as current API model names. The NL scoring default should use `deepseek-v4-flash`, while the main Agent default remains `deepseek-v4-pro`.

Implementation:
- Updated `scripts/experiments/run_experiment.py` with explicit `DEFAULT_AGENT_MODEL = "deepseek-v4-pro"` and `DEFAULT_NL_MODEL = "deepseek-v4-flash"`.
- `--nl-model` now defaults to `deepseek-v4-flash`; if the user sets it equal to `--model`, the proxy is reused, otherwise the CLI creates an independent NL proxy.
- Updated `docs/environment_design.md` and `docs/pipeline_design.md` to record the default model split.

Validation:
- `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help` shows `--nl-model` defaults to `deepseek-v4-flash`.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e tests.unit.test_tools_flow tests.unit.test_llm_deepseek -v` ran 40 tests OK.
- `/home/lzp/miniconda3/envs/quant/bin/python -m compileall -q scripts/experiments/run_experiment.py src/hl_trader/environment/llm src/hl_trader/environment/nl src/hl_trader/pipelines tests/unit/test_pipeline_e2e.py tests/unit/test_tools_flow.py tests/unit/test_llm_deepseek.py` passed.

## 2026-06-12 real smoke experiment with Fold, regularization, and Held-Out

Task: run a real experiment instead of relying only on unit tests: one regular Fold, one post-Epoch regularization Fold, and one Held-Out period.

Resource checks:
- Before launch: system memory about 396 GiB available; no MacroQuant Docker containers were running.
- During snapshot/Agent runs: memory remained safe, with more than 370 GiB available after the heavy snapshot stage.
- After completion: system memory about 408 GiB available. Local GPUs were mostly occupied by unrelated processes; this experiment did not require local GPU compute because LLM calls used the provider API.

First attempted real run:
- Experiment ID: `exp_real_smoke_20260612_180600`.
- Command used the same 2022Q1 development Fold and 2022Q2 Held-Out shape with `--max-fold-minutes 60`.
- Outcome: stopped manually after more than 60 minutes because `nl_mode=on` with the previous default 100 candidates kept running too long.
- Useful artifacts:
  - `.runtime/sandboxes/run_d1bab1ebd555/artifacts/agent_trace.jsonl`
  - `.runtime/sandboxes/run_d1bab1ebd555/artifacts/results/valid_000/`
  - `.runtime/sandboxes/run_d1bab1ebd555/artifacts/results/valid_001/`
  - `.runtime/sandboxes/run_d1bab1ebd555/artifacts/results/valid_002/`
  - `.runtime/sandboxes/run_d1bab1ebd555/artifacts/results/valid_003/`
- Findings:
  - The first Agent-generated factor emitted codes outside the visible universe; `backtest_tool` correctly rejected them.
  - `backtest_tool` returned host paths that were awkward for the container Agent to read.
  - `valid_001` with full NL scoring had 99 completed candidate tasks and 1 failure; the old strict failure policy blocked the formal backtest after substantial API work.
  - A 100-stock NL candidate set is too large for a one-hour smoke Fold.

Pre-rerun fixes made in the same work item:
- Default `max_candidates` changed to 10 in experiment config and CLI.
- CLI gained `--max-candidates` and `--nl-failure-policy`.
- Default NL failure policy changed to `neutral_with_audit`.
- `backtest_tool` now returns container-readable paths such as `/mnt/artifacts/results/...`, while retaining host paths for the outer pipeline.
- Prompt/docs were updated to describe the top-10 candidate cap.
- The strict NL-failure unit test now explicitly sets `nl_failure_policy="fail"` so strict mode remains covered.

Successful real run:
- Experiment ID: `exp_real_smoke_20260612_191433`.
- Command:
  `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --experiment-id exp_real_smoke_20260612_191433 --first-test-quarter 2022Q1 --last-test-quarter 2022Q1 --heldout-first-quarter 2022Q2 --heldout-last-quarter 2022Q2 --epochs 1 --max-fold-minutes 60 --max-candidates 10 --nl-failure-policy neutral_with_audit`
- Exit status: 0.
- Runtime log: `logs/experiments/exp_real_smoke_20260612_191433.log`.
- Ledger: `experiments/exp_real_smoke_20260612_191433/ledgers/experiment_ledger.jsonl`.
- Regular Fold runtime artifact: `experiments/exp_real_smoke_20260612_191433/artifacts/run_2e64f9b835fc/`.
- Regularization runtime artifact: `experiments/exp_real_smoke_20260612_191433/artifacts/run_fcba06ac9cd1/`.
- Held-Out runtime artifact: `experiments/exp_real_smoke_20260612_191433/artifacts/run_3dee7c6e695c/`.
- Final strategy artifact: `strategy_epoch_002_regularized`.

Key metrics:
- `valid_000`, NL off: total return -6.18%, short-side return -5.49%, Sharpe -1.11, max drawdown 16.72%, 10 orders.
- `valid_001`, NL off after Agent simplification to 20-day momentum: total return +9.25%, long-side return +9.25%, Sharpe 1.94, max drawdown 12.66%, 10 orders.
- `valid_002`, NL on and used as final validation Step: total return +5.63%, long-side return +5.63%, Sharpe 2.16, max drawdown 7.44%, 3 orders. NL batch had 9 completed tasks and 1 neutral-with-audit failure.
- Frozen Fold test `test_000`: total return -0.03%, long-side return -0.03%, Sharpe -0.13, max drawdown 7.97%, 1 order.
- Held-Out `heldout_000` for 2022Q2: total return -0.81%, long-side return -0.81%, Sharpe -0.25, max drawdown 3.89%, 1 order.

Call and tool traces:
- Regular Fold trace `run_2e64f9b835fc`: 57 Agent LLM calls, 44 Shell calls, 7 Tool calls, 6 backtest events, 4 NL batch summaries.
- Regularization trace `run_fcba06ac9cd1`: 15 LLM calls, 13 Shell calls, 2 Tool calls.
- Frozen test NL output recorded 27 provider calls; final validation NL output recorded 27 provider calls; Held-Out NL output recorded 28 provider calls.

Report generation:
- Command: `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/report_experiment.py --experiment-id exp_real_smoke_20260612_191433`
- Output:
  - `experiments/exp_real_smoke_20260612_191433/reports/epoch_comparison_returns.png`
  - `experiments/exp_real_smoke_20260612_191433/reports/epoch_returns/epoch_001_returns.png`
- PNG checks confirmed valid non-empty images: 2080x1920 and 2080x1728.

Validation:
- `git diff --check` passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_pipeline_e2e tests.unit.test_reporting -v` ran 44 tests OK.

Current conclusion:
- The real end-to-end path now runs through a regular Agent Fold, a regularization Fold, and Held-Out evaluation with real PIT snapshots, Docker sandbox execution, provider-backed Agent calls, provider-backed NL scoring, step-tree artifacts, factor attribution, and reports.
- The smoke experiment is not evidence of strategy quality: validation improved after Agent iteration, but frozen test and Held-Out were slightly negative. Its value is mainly operational: it verified the pipeline path and exposed realistic performance/UX constraints.
- Follow-up candidates: reduce snapshot copy cost, make Fold deadline cancel or reject long-running tool calls more forcefully, and keep the 10-candidate cap for real smoke tests unless the Fold deadline is increased substantially.

## 2026-06-12 final-Epoch regularization fix and real-trace audit

Task: ensure the last Epoch does not run regularization, then audit the real experiment trace, Agent shutdown, and NL grep retrieval behavior.

Implementation:
- Updated `ExperimentPipeline.run()` so `run_regularization()` is called only when `epoch_index < config.epochs`.
- Updated `docs/pipeline_design.md` to state that regularization is an inter-Epoch step; the final Epoch proceeds directly to Held-Out.
- Updated `tests/unit/test_pipeline_e2e.py`:
  - `test_single_epoch_skips_final_regularization_and_runs_heldout` proves a one-Epoch run does not call the regularizer and Held-Out uses the Fold artifact directly.
  - `test_multi_epoch_regularizes_only_between_epochs` proves a two-Epoch run regularizes only after `epoch_001`, then Held-Out uses the final ordinary Fold artifact from `epoch_002`.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e -v` ran 11 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_pipeline_e2e tests.unit.test_reporting -v` ran 45 tests OK.
- `git diff --check` passed.
- Resource checks after validation remained safe: about 408 GiB RAM available; no MacroQuant Docker containers left running.

Trace audit for `exp_real_smoke_20260612_191433`:
- Regular Fold `run_2e64f9b835fc` trace counts: 57 Agent LLM calls, 44 Shell calls, 7 Tool calls, 6 backtest events, 4 NL batch summaries, 1 `finish_fold`, 1 `session_end`.
- The Agent workflow was coherent:
  - inspected `agent_output`, `steps/tree.json`, `train` snapshot, daily/fundamental/event/macro schemas, and factor templates;
  - drafted a multi-factor strategy;
  - hit a universe boundary rejection (`300114.SZ`) and fixed it by filtering against `universe.parquet`;
  - ran a first valid backtest that lost money on short-side exposure;
  - explored IC and margin/shortability data;
  - simplified to a 20-day momentum factor after the earlier composite underperformed;
  - hit a finite-score validation error, dropped NaN scores, and reran;
  - achieved positive validation in `valid_001`;
  - added a simple `nl_prior`, ran `nl_mode=on` validation in `valid_002`, inspected attribution/orders, then called `finish_fold`.
- Pipeline behavior was normal:
  - `finish_fold_tool` returned successfully;
  - session ended;
  - Pipeline froze the accepted `valid_002` artifact and ran frozen test `test_000`.
- Regularization run `run_fcba06ac9cd1` did not run backtests, edited `prior.json`, passed `modification_check_tool`, and ended. Attempts to read host `/Data/...` paths returned `not found`, so no host data escape was observed.
- Held-Out run `run_3dee7c6e695c` ran only a frozen evaluation and did not modify the strategy artifact.

NL grep audit:
- `valid_002`: 27 NL provider calls, 22 grep/regex search requests, 93 evidence rows, 10 scores; 9 completed, 1 `failed_with_policy` neutral score. Scores ranged from -0.30 to +0.35.
- `test_000`: 27 NL provider calls, 22 search requests, 100 evidence rows, 10 completed scores. Scores ranged from -0.70 to +0.20.
- `heldout_000`: 28 NL provider calls, 27 search requests, 121 evidence rows, 10 completed scores. Scores ranged from -0.45 to +0.35.
- Requests used `pattern` regex form such as `公司名|证券代码` and `问询函|处罚|立案`, not only legacy keyword lists. Code path is `TextRetriever.search()`, which matches titles/codes first and then body text.
- Environment return values were structurally reasonable: `search_requests.jsonl`, `evidence.jsonl`, `scores.jsonl`, `company_context.jsonl`, and `nl_llm_calls.jsonl` were all present; scores cited valid evidence IDs except one `valid_002` task, which was converted to neutral by `neutral_with_audit`.
- Two quality issues remain:
  - Generic risk patterns often retrieve non-candidate-company news; approximate generic-risk title rows were 40/93 in `valid_002`, 41/100 in `test_000`, and 51/121 in Held-Out.
  - `applied_prior_ids` was empty in all audited NL scores, meaning the model used evidence but did not explicitly map conclusions back to `prior.json` rules. This is auditable but weakens prior-rule attribution.

## 2026-06-12 meta-learning Fold and NL relevance hardening

Task: replace the final-Epoch regularization skip with an Epoch-start meta-learning + optional regularization Fold, improve candidate-company relevance in NL retrieval, strengthen NL prompt/schema validation, and add a web-search tool for meta-learning.

Resource checks:
- Before implementation validation: about 403 GiB system RAM available; GPUs 4 and 6 were nearly free, other L20 GPUs were mostly occupied by unrelated jobs.
- After validation: resource usage remained safe; no long-running MacroQuant job was left active.

Implementation:
- Added `src/hl_trader/environment/web_search.py` with a host-side Tavily provider and traceable `WebSearchTool`.
- Stored `TAVILY_API_KEY` in local ignored `.env`; no key was written to Git-tracked files.
- Added `web_search` action to `AgentSessionRunner`, available only in `mode="meta_learning"`.
- Replaced the main Pipeline regularization schedule with Epoch-start meta-learning:
  - each Epoch calls `run_meta_learning()` before ordinary Folds;
  - meta-learning writes `workspace/taste.md`;
  - Pipeline copies Taste to `experiments/<experiment_id>/meta_learning/<epoch_id>/taste.md`;
  - Taste is injected as `taste_prompt` into all Fold Agent prompts for that Epoch;
  - if a parent artifact exists and the meta-learning edits pass `modification_check_tool`, Pipeline freezes `strategy_<epoch_id>_meta_learning` as the Epoch starting artifact;
  - otherwise Pipeline keeps the parent and still uses the generated Taste.
- Updated ledger record type from the old regularization event to `meta_learning`; removed the old direct regularization session path so the current entry is `run_meta_learning()`.
- Added a compact development-history package for meta-learning. It combines fold ledger records with selected `run_manifest.json` backtest summaries, so Taste generation can see validation/test return, Sharpe, drawdown, order count, candidate truncation, failure reasons, and complete-validation status without mounting full historical artifacts.
- Strengthened NL scoring:
  - `TextRetriever.search()` now ranks candidate-code/name hits above generic title/body hits and marks evidence as `candidate` or `background`.
  - Only `candidate` evidence IDs are allowed in final score citations; background evidence can inform context but cannot become formal per-stock evidence.
  - NL prompt now asks for company/code/business-context grep patterns first and treats generic searches as background.
  - `validate_score_payload()` checks `applied_prior_ids` against visible `prior.json` rule IDs and requires a prior ID for non-neutral or evidence-backed scores.
- Hardened Tavily error handling so HTTP and transport errors redact the configured key before returning an agent-visible observation.
- Updated `scripts/experiments/run_experiment.py` with `--web-search-provider` and `--tavily-api-key-env`.
- Updated living docs (`agent_design`, `environment_design`, `pipeline_design`), `configs/prompts/PROMPTS.md`, and `configs/agent_output_template/nl_prior/README.md`.

Provider check:
- Command: small Tavily query through `TavilySearchProvider.from_env()`.
- Result: provider returned 2 results for a WFO/overfitting query, confirming the configured key and HTTP path work.

SubAgent audit:
- SubAgent found no Critical issues.
- High issues fixed in this pass: other-company evidence can no longer be cited as candidate evidence; zero-diff meta-learning no longer freezes a duplicate artifact.
- Medium issues fixed or scoped: meta-learning docs now describe compact history summaries instead of full artifact access; old regularization session mode was removed; Tavily error text redacts the configured key; `applied_prior_ids` is now required only for non-neutral/evidence-backed conclusions.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_nl_scoring tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e tests.unit.test_tools_flow tests.unit.test_reporting -v`
- Result: 64 tests OK.
- `git diff --check` passed.
- `py_compile` passed for changed core modules and CLI.
- Final resource check: about 404 GiB RAM available; local GPUs remained safe, with GPUs 4 and 6 nearly free and other GPUs occupied by unrelated processes.

Current conclusion:
- The previous final-Epoch skip logic is no longer the accepted design. The current design runs a meta-learning + optional regularization Fold before every Epoch.
- Ordinary Fold Agents remain offline except for the main LLM provider; only meta-learning can use Tavily search.
- The two real-trace NL audit issues have targeted fixes: candidate-related evidence is prioritized, and NL scores must map back to prior rules.

## 2026-06-13 short candidate rollover in order planning

Task: change the backtest order-plan logic so short candidates that are not currently shortable do not consume a holding slot. Instead, the Environment should skip them and roll down to the next shortable candidate.

Implementation:
- Updated `build_order_plan()` in `src/hl_trader/environment/backtest_engine.py`:
  - long candidates are ranked by descending `final_score`;
  - short candidates are ranked by negative-score strength;
  - when a shortable universe is supplied, non-shortable short candidates are filtered before sizing;
  - the final Top N is selected from executable candidates and sized by `abs(final_score)`.
- Updated `BacktestTool` so default `proxy_margin_secs` loads decision-date `margin_secs` before order-plan construction and passes that set into `build_order_plan()`.
- Kept `theoretical_short` unfiltered, and treated currently unsupported `broker_inventory` as no available short inventory until real inventory files are wired.
- Added `short_unavailable_skipped_count` to backtest summaries for audit.
- Updated Shapley factor-attribution replay to use the same order-plan shortability rule as the formal backtest.
- Updated Agent prompt exports, factor template README/main.py comments, and living docs to describe the new side-aware selection rule.
- Added unit coverage: `test_short_order_plan_rolls_down_to_next_shortable_candidate`.

Validation:
- Resource checks before/around validation: about 404 GiB RAM available; GPUs 4 and 6 nearly free, other GPUs occupied by unrelated processes.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow tests.unit.test_pipeline_e2e -v` ran 42 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_nl_scoring tests.unit.test_reporting -v` ran 64 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/tools/backtest.py src/hl_trader/agent/prompts.py configs/agent_output_template/factor/main.py` passed.
- `git diff --check` passed.

Current conclusion:
- Formal backtest order planning now rolls unavailable short candidates down to the next shortable candidate within the scored candidate pool.
- Simulated Broker still keeps reject/fill accounting as a final safeguard for cash, suspension, limit, margin, fee, and future broker-inventory constraints.

## 2026-06-13 CSI 300 benchmark in experiment reports

Task: add the CSI 300 return curve to experiment report charts and expose active return against the benchmark.

Resource checks:
- Before validation: about 397 GiB system RAM available. GPUs were safe for this CPU-only reporting task; GPU 6 was nearly idle, several other L20 cards were occupied by unrelated processes.
- After validation: resource usage remained safe; no long-running report job was left active.

Implementation:
- Updated `src/hl_trader/pipelines/reporting.py`:
  - default benchmark is `000300.SH` labelled `CSI 300`;
  - benchmark data is auto-loaded from `data/raw/index_daily/ts_code=000300.SH/`;
  - each Fold/Held-Out period gets `benchmark_return` and `active_return`;
  - benchmark period return uses the first replay trade day's open and the last replay trade day's close, matching the fixed holding-period replay convention;
  - cross-Epoch and single-Epoch charts now include benchmark return/equity lines, active-return metrics, and relative equity versus benchmark.
- Updated `scripts/experiments/report_experiment.py` with `--benchmark-code`, `--benchmark-raw-dir`, and `--no-benchmark`.
- Updated `tests/unit/test_reporting.py` to cover benchmark loading and summary fields.
- Updated `docs/pipeline_design.md` with the reporting contract and active-return definition.
- Local ignored data was supplemented with TuShare `index_daily` for `000300.SH`: 1,560 rows covering `20200102` through `20260612`, written as yearly Parquet partitions under `data/raw/index_daily/ts_code=000300.SH/`.

Report regeneration:
- Command: `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/report_experiment.py --experiment-id exp_epoch_eval_001`
- Output charts:
  - `experiments/exp_epoch_eval_001/reports/epoch_comparison_returns.png`
  - `experiments/exp_epoch_eval_001/reports/epoch_returns/epoch_001_returns.png`
- Benchmark coverage: 17/17 report periods.
- Development summary: mean strategy return +5.37%, mean CSI 300 return -0.66%, mean active return +6.03%, compound active return +103.82%.
- Held-Out 2026Q1: strategy return -1.65%, CSI 300 return -4.54%, active return +2.89%.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_reporting -v` passed, 2 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/pipelines/reporting.py scripts/experiments/report_experiment.py tests/unit/test_reporting.py` passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_multi_epoch_runs_meta_learning_before_each_epoch -v` passed, 1 test OK.
- `git diff --check` passed.
- PNG integrity check with PIL confirmed both regenerated charts are readable and non-empty.

Current conclusion:
- Experiment visualizations now compare strategy returns to CSI 300 and explicitly report active return.
- The benchmark layer is optional; reports still render with benchmark disabled or missing, but the summary records benchmark status for audit.

## 2026-06-14 Semantic Scholar provider for meta-learning search

Task: add Semantic Scholar Academic Graph API as an optional provider for the Epoch-start meta-learning `web_search` tool.

Resource checks:
- Before implementation: about 375 GiB system RAM available. GPUs were heavily occupied by unrelated Python jobs; this task used CPU/network only.
- After validation: resources remained safe; no long-running MacroQuant job was left active.

Implementation:
- Added `SemanticScholarSearchProvider` to `src/hl_trader/environment/web_search.py`.
  - Reads `SEMANTIC_SCHOLAR_API_KEY` from the host environment or ignored local `.env`.
  - Uses Semantic Scholar Graph API paper search with `x-api-key` header.
  - Returns unified `WebSearchResult` records containing title, URL, abstract snippet, year/date, venue, authors, citation count and influential citation count when available.
  - Applies a small minimum interval between provider calls and redacts the configured key from provider error messages.
- Updated `scripts/experiments/run_experiment.py`:
  - `--web-search-provider` now accepts `tavily`, `semantic_scholar`, or `disabled`.
  - Added `--semantic-scholar-api-key-env`, defaulting to `SEMANTIC_SCHOLAR_API_KEY`.
- Updated meta-learning prompt text to clarify that `web_search` may be backed by general web search or Semantic Scholar academic paper search.
- Updated `docs/environment_design.md`, `docs/pipeline_design.md`, and regenerated `configs/prompts/PROMPTS.md`.
- Stored the provided Semantic Scholar key in the local ignored `.env`; no tracked file contains the key.

Provider references:
- Semantic Scholar official tutorial and API docs state that the Academic Graph API base URL is `https://api.semanticscholar.org/graph/v1`, paper search is under `/paper/search`, and API keys are sent in the case-sensitive `x-api-key` header. The tutorial also recommends key-backed users keep calls around 1 request per second.

Live smoke:
- Command: instantiate `SemanticScholarSearchProvider.from_env()` and query `walk forward optimization finance` with `max_results=2`.
- Result: provider returned 2 paper records with Semantic Scholar URLs; no key was printed.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v` passed, 30 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/web_search.py scripts/experiments/run_experiment.py src/hl_trader/agent/prompts.py tests/unit/test_sandbox_isolation.py` passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help` confirmed the new `semantic_scholar` provider option.
- `git diff --check` passed.
- Tracked diff scan for the Semantic Scholar key pattern returned no matches.

Current conclusion:
- Meta-learning can now use Semantic Scholar for academic/theory search without exposing the key to Sandbox Agents.
- Ordinary Fold Agents and NL scoring remain offline except for their existing LLM provider calls.

## 2026-06-15 PR validation for agent experiment branch

Task: prepare the current Agent/Environment/Pipeline/Data update branch for GitHub PR submission.

Resource checks:
- Before validation: about 375 GiB RAM available. GPUs were heavily occupied by unrelated Python jobs; validation was CPU-focused.
- After validation: about 373 GiB RAM available. No long-running MacroQuant validation job was left active.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v`
  - Result: 190 tests OK.
  - Note: the direct command `python -m unittest discover tests/unit -v` is not package-aware for the current relative-import test layout and reports import errors for tests that use `from .fixtures_sandbox`; the correct full-suite entrypoint is `-s tests -t .`.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m compileall -q src scripts configs`
  - Result: passed.
- `git diff --check`
  - Result: passed.
- Removed ignored local Python caches after validation.

Current conclusion:
- The branch is ready for PR from a verification standpoint. The PR includes the broader single-Agent experiment runtime, Docker/Sandbox contracts, TuShare script organization, reporting visualization, short-side order rollover, Semantic Scholar meta-learning search provider, and synchronized living documentation/logbook updates.

## 2026-06-18 audit fixes for Environment/Pipeline contracts

Task: fix the code/documentation mismatches found in the project audit and keep the living docs concise.

Resource checks:
- Confirmed the real repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Before validation: system RAM stayed above 424 GiB available; GPUs were occupied by unrelated jobs, but this work used CPU-only tests.
- After validation: system RAM stayed above 424 GiB available; no MacroQuant long-running job was left active.

Implementation:
- Broker/profile:
  - `BrokerProfile` records are restored from manifest using the dataclass field list, so cost/slippage/stamp-duty/maintenance fields no longer get silently dropped.
  - Exit-side constraints now keep positions open and record broker events when long exits are suspended/limit-down blocked or short covers are suspended/limit-up blocked.
  - Short realized PnL now includes the opening short sale fee and stamp duty; replay avoids charging borrow fees twice on an exit day when exits are blocked.
- NL scoring:
  - `neutral_with_audit` now gives timeout and provider failures an auditable neutral score.
  - `nl_mode=on` now fails if any candidate lacks an NL score, preventing silent fallback to factor-only scoring.
  - Unexpected per-task exceptions are normalized into terminal task states.
- PIT/features:
  - Decision snapshot daily joins now filter `daily`, `daily_basic`, `adj_factor`, `stk_limit`, and `suspend_d` by their own dataset contracts before joining.
  - Added shared daily unit normalization for snapshots and `daily_alpha`; `daily_alpha` now emits decimal percentages, shares, and CNY values.
- Pipeline/agent:
  - Meta-learning `development_history.json` no longer includes full fold records, only compact fold summaries plus meta-learning memory.
  - When a web-search provider is configured, meta-learning `done` requires all three categories: `finance`, `cross_domain`, and `philosophy`; a rejected `done` no longer terminates the session.
  - `pyproject.toml` now lists direct runtime imports: `numpy`, `matplotlib`, and `requests`.
- Documentation:
  - Updated `docs/environment_design.md` for per-dataset PIT daily joins, standard units, NL timeout policy, and exit-side liquidity constraints.
  - Updated `docs/pipeline_design.md` for compact meta-learning history and enforced search categories.
  - Updated meta-learning prompt text to match the provider-gated search requirement.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_nl_scoring tests.unit.test_snapshot_builder tests.unit.test_features tests.unit.test_pipeline_e2e -v`
  - Result: 70 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  - Result: 32 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v`
  - Result: 196 tests OK.
- `git diff --check`
  - Result: passed.

Current conclusion:
- The audited high-risk contract mismatches are fixed and covered by focused regression tests.
- An unrelated untracked `claude-code-main.zip` is present in the working tree and was left untouched.

## 2026-06-18 Agent tool protocol and structured search

Task: implement the high- and medium-priority Agent improvements identified by the Claude Code comparison: typed action metadata, structured grep/glob, stronger shell guard, result budgeting, deterministic context summary, and auditable cancellation.

Resource checks:
- Confirmed the real repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Before validation: `free -h` reported 503 GiB total RAM and about 425 GiB available; `nvidia-smi` showed unrelated Python jobs on GPUs 0 and 7, while this work used CPU-only tests.
- After validation: `free -h` reported about 423 GiB available; the same unrelated GPU jobs remained, and no MacroQuant long-running job was left active.

Implementation:
- Added lightweight Runner-side action schema metadata in `src/hl_trader/environment/tools/base.py`: each action can declare fields, allowed modes, read-only/destructive/concurrency-safe flags, and result budget.
- Added `src/hl_trader/environment/tools/search.py` with structured read-only `grep` and `glob` over allowlisted sandbox roots. `grep` supports `content`, `files`, and `count` modes plus `glob`, pagination, context lines, case-insensitive search, multiline search, VCS-dir exclusion, and result-budget storage.
- Updated `AgentSessionRunner` to validate action payloads before dispatch, expose `grep`/`glob`, record tool schema in observations/traces, return `cancelled` observations after the Fold deadline, and inject deterministic `context_summary` observations when message history is trimmed.
- Hardened `SandboxShellTool` path checks for explicit test/runtime/Docker-socket/host-outside-sandbox references and write-like commands against read-only roots. Shell stdout/stderr previews remain capped at 20k chars; oversized bounded capture is stored under `logs/tool_results/`.
- Added tool metadata to modification check, backtest, finish-fold, and web-search trace payloads.
- Updated `src/hl_trader/agent/prompts.py`, `docs/agent_design.md`, `docs/environment_design.md`, and `docs/pipeline_design.md` to document the current tool contract without adding historical migration notes.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow -v`
  - Result: 18 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation -v`
  - Result: 19 tests OK, including Docker E2E on this machine.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_tools_flow.py`
  - Result: passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v`
  - Result: 200 tests OK.
- `git diff --check`
  - Result: passed.

SubAgent audit follow-up:
- Raman (GPT-5.5 xhigh) completed a read-only audit and found no direct test/held-out escape, but flagged real risks in the new tooling.
- Fixes after the audit:
  - `StructuredSearchTool` no longer reads full `rg` output before paging; it streams lines and terminates after `offset + head_limit + 1`, with `head_limit` capped at 1000.
  - `glob` now stops after the requested page plus one extra item instead of sorting/enumerating the whole tree.
  - `SandboxShellTool` now uses executor-side bounded capture for stdout/stderr, avoiding unlimited `subprocess.run(..., capture_output=True)` memory growth on Agent shell calls.
  - Shell path guard now recognizes `sed -i.bak`, Python write snippets against explicit read-only paths, quoted `>` patterns without treating them as redirection, and path-prefix boundaries such as `/mnt/agent/workspace_evil`.
  - `grep` content filenames now split only on the `path:line:content` colon separator, so paths containing hyphens stay intact.
  - `WebSearchTool` returns `result_count`; CLI meta-learning records the actual provider name or `disabled` in the manifest.
  - `ActionSpec.validate()` rejects unknown action fields instead of silently dropping typo fields.
  - `docs/environment_design.md` no longer claims Agent main trace records `temperature` or `seed`, and tool-result wording now says bounded captured content rather than unlimited full output.

Re-validation after SubAgent fixes:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_sandbox_isolation -v`
  - Result: 37 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/executor.py src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py src/hl_trader/environment/web_search.py src/hl_trader/agent/runner.py scripts/experiments/run_experiment.py tests/unit/test_tools_flow.py`
  - Result: passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v`
  - Result: 200 tests OK.
- `git diff --check`
  - Result: passed before the logbook update; final diff check is still pending.

Final SubAgent audit follow-up:
- Bohr (GPT-5.5 high) completed a final read-only audit after the Raman fixes. It found no direct formal Docker hard-isolation escape, but reported four remaining issues.
- Fixes:
  - Container paths in `SandboxShellTool` are now mapped back to the host sandbox path and resolved before permission checks, so `/mnt/agent/workspace/../../snapshots/test/...` is rejected by the same real-path guard.
  - `$PWD` and `${PWD}` path tokens are expanded to the Agent cwd before guard checks, closing the local-dev bypass for `$PWD/../snapshots/test/...`.
  - `grep output_mode="count"` now reports `page_matches`, `num_matches_lower_bound`, and `num_matches_known` instead of presenting a paged count as a global total.
  - `glob` preserves the requested `offset` in observation metadata.
  - Removed the unused `_text_or_empty()` helper from the structured search tool.

Final validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_sandbox_isolation -v`
  - Result: 37 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py tests/unit/test_tools_flow.py`
  - Result: passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py' -v`
  - Result: 200 tests OK.
- `git diff --check`
  - Result: passed before this final logbook update; a final diff check follows this entry.

Current conclusion:
- The Agent now has structured code/file search and auditable tool metadata while preserving the project boundary: ordinary Fold Agents remain offline, cannot read test, and cannot call the NL evidence retriever directly.
- Full-suite validation passed; Raman's audit findings were addressed. A final short SubAgent review is planned after the follow-up fixes.

## 2026-06-18 Backtest strategy-program interface

Task: extend the formal backtest path so the Agent can submit a `main.py`
strategy program that coordinates candidate selection, audited NL scores, and
trade-intent construction while the Environment still owns PIT binding, NL API
logging, cash, margin, short inventory, fills, rejects, and return statistics.

Implementation in progress:
- Added the new artifact scaffold: `factor/main.py`, `factor/candidate.py`,
  `factor/trading.py`, `factor/factors.json`, `nl_prior/prior.json`, and
  `nl_prior/prompt.md`.
- `load_strategy_artifact()` now validates the new files, permits registered
  factor functions in `factor/*.py`, rejects hard-coded stage/runtime/artifact
  paths in strategy code, and counts total strategy files/bytes in modification
  checks.
- Added `run_strategy_program()` in `backtest_engine.py`. It executes
  `run_strategy(context)` when present and normalizes legacy
  `generate_candidates()` output into the same candidate result shape.
- Added structured `trade_intents` validation, default plan-to-intent
  conversion, custom-intent order-plan merge, and daily trade-intent replay.
- `backtest_tool` now runs the strategy once for candidates, performs audited
  NL scoring, writes `nl_scores.json` and `scored_candidates.parquet`, and
  optionally reruns the strategy to collect `trade_intents`. It persists
  `trade_intents.parquet` and `strategy_metadata.json` alongside existing
  return and NL artifacts.
- Updated Agent prompt export, factor/NL templates, and the living Agent,
  Environment, and Pipeline docs.

Validation so far:
- Resource checks before tests: about 425 GiB system RAM available; unrelated
  GPU jobs on GPU 0 and 7, no new GPU workload from this task.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/artifacts.py src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/broker.py src/hl_trader/environment/tools/backtest.py src/hl_trader/agent/prompts.py configs/agent_output_template/factor/main.py configs/agent_output_template/factor/candidate.py configs/agent_output_template/factor/trading.py`
  - Result: passed.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_broker_engine tests.unit.test_tools_flow -v`
  - Result: 49 tests OK.

Current conclusion:
- The minimal code path for two-stage strategy programs and custom trade intents
  is implemented and covered by focused tests.
- Full-suite validation, API/proxy flow validation, `git diff --check`, and
  iterative SubAgent audit are still pending.

Franklin SubAgent audit follow-up:
- Franklin found four blocking issues: unforced artifact file whitelist, legacy
  compatibility conflict for new required files, `close_buy` filling at the
  open instead of the close, and active trade intents being removed even when
  exit was blocked by suspension/limits.
- Fixes:
  - `artifacts.py` now rejects any non-cache file outside the formal
    whitelist. New helper files are template-standard but not required for old
    minimal artifacts; missing `prompt.md` is treated as an empty prompt.
  - `SimBroker` now has `fill_close()`, and daily `close_buy` intents fill at
    the close.
  - Trade-intent replay activates intents only after orders actually fill and
    removes active exit intents only after `close_position()` returns True.
  - Trade-intent validation now checks strategy/side compatibility and
    `YYYYMMDD` start/end dates; unavailable shorts are filtered before holding
    count enforcement.
  - Added regression tests for extra artifact-file rejection, old minimal
    artifact loading, close-price `close_buy`, blocked-exit retry, date/side
    validation, unavailable short filtering before plan size, and custom
    trade-intent attribution skipping.
- Re-validation:
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/artifacts.py src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/broker.py src/hl_trader/environment/tools/backtest.py tests/unit/test_artifacts.py tests/unit/test_broker_engine.py tests/unit/test_tools_flow.py`
    - Result: passed.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_broker_engine tests.unit.test_tools_flow -v`
    - Result: 54 tests OK.

Laplace SubAgent audit follow-up:
- Laplace found two blocking issues after the Franklin fixes:
  - `copy_artifact()` copied whole `factor/` and `nl_prior/` directories, so
    runtime cache files could enter frozen/step artifacts while hash/diff/load
    ignored them.
  - Formal strategy execution hid replay slots but not `/mnt/artifacts`, so
    runtime path construction could read trusted result/step artifacts despite
    the static string-constant check.
- Fixes:
  - `artifacts.py` now rejects `__pycache__`, `.pyc`, and `.pyo` in formal
    artifacts instead of ignoring them.
  - `init_from_template()` and `copy_artifact()` now copy exactly the formal
    file whitelist returned by `_artifact_files()`.
  - `run_strategy_program()` passes a forbidden-path list into the strategy
    driver and temporarily hides train/valid/test/artifacts during formal
    execution.
  - `backtest_tool` now stages second-pass `nl_scores.json` and
    `scored_candidates.parquet` under `/mnt/agent/workspace/.strategy_inputs/`
    before rerunning strategy code, then removes the temporary copy. The
    durable artifacts remain in `results/<phase>_<idx>/`.
  - Trade-intent `start_date`/`end_date` now use real `YYYYMMDD` calendar-date
    parsing, not only a regex.
  - Pipeline docs now mention `factor_attribution.json` and the custom
    `trade_intents` skip reason; Agent/Environment docs and prompt export now
    state that formal artifacts reject Python caches and extra files.
- Validation:
  - Resource checks before/after tests: about 426 GiB available system RAM;
    unrelated GPU jobs on GPU 0 and GPU 7; this work did not launch a GPU job.
  - Real API smoke earlier in this task used the local DeepSeek-compatible
    proxy with keys redacted and returned
    `{"ok": true, "check": "strategy_program_smoke"}`; raw local smoke logs are
    under ignored `logs/api_smoke/`.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
    - Result: regenerated `configs/prompts/PROMPTS.md`.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/artifacts.py src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/tools/backtest.py src/hl_trader/agent/prompts.py tests/unit/test_artifacts.py tests/unit/test_broker_engine.py tests/unit/test_tools_flow.py`
    - Result: passed.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_broker_engine tests.unit.test_tools_flow -v`
    - Result: 56 tests OK.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
    - Result: 211 tests OK.
  - `git diff --check`
    - Result: passed before this logbook update.
  - Generated Python caches were removed after validation.

Current conclusion:
- The strategy-program backtest path now has the intended two-stage interface
  while preserving Environment-owned NL scoring, artifact auditability, broker
  constraints, and formal runtime isolation.
- A third SubAgent audit is required after these fixes before delivery.

Plato SubAgent audit follow-up:
- Plato found one blocking issue:
  - The default template returned `rerun_after_nl=True`, but the default
    `build_trade_intents()` returned an empty table. New strategies that only
    implement candidates would therefore fail instead of using the default
    Environment order plan.
- Fixes:
  - `configs/agent_output_template/factor/main.py` now defaults to
    `rerun_after_nl=False`; the README states Agent should enable rerun only
    after implementing non-empty custom `trade_intents`.
  - `trading.py`, Agent docs, Environment docs, and prompt export now list
    optional `start_date`/`end_date` intent fields and their `YYYYMMDD`
    contract.
  - The factor README and prompt now distinguish default-order attribution
    from the custom-intent path, where the summary records
    `factor_attribution_skipped_reason=custom_trade_intents`.
  - `init_from_template()` ignores local runtime cache files in the trusted
    template source but still copies only formal whitelist files; formal
    artifacts and copied parent artifacts still reject cache files.
  - Added tests for template-source cache ignoring and for a template-default
    strategy that only modifies `candidate.py`, runs formal backtest, and uses
    the default order plan without rerun.
- Validation:
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
    - Result: regenerated `configs/prompts/PROMPTS.md`.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/artifacts.py src/hl_trader/agent/prompts.py tests/unit/test_artifacts.py tests/unit/test_tools_flow.py`
    - Result: passed.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_broker_engine tests.unit.test_tools_flow -v`
    - Result: 58 tests OK.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
    - Result: 213 tests OK.
  - `git diff --check`
    - Result: passed before this logbook update.
  - Generated Python caches were removed after validation.

Current conclusion:
- The template-default path now works without custom trade intents, while
  two-stage custom trade-intent strategies remain supported.
- A fourth SubAgent audit is required after these fixes before delivery.

Mencius SubAgent audit follow-up:
- Mencius reported no blocking findings.
- One non-blocking documentation issue was fixed:
  - Agent prompt now states explicitly that the first `run_strategy(context)`
    call must return `rerun_after_nl=True` before Environment performs the
    second call with `scored_candidates_path` and `nl_scores_path`.
  - `configs/prompts/PROMPTS.md` was regenerated from the prompt source.
- Validation:
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
    - Result: regenerated `configs/prompts/PROMPTS.md`.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py`
    - Result: passed.
  - `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
    - Result: 213 tests OK.
  - Resource checks after the run still showed about 426 GiB available system
    RAM and no new GPU workload from this task.
  - `git diff --check`
    - Result: passed before this logbook update.
  - Generated Python caches were removed.

Current conclusion:
- All SubAgent findings so far have been resolved. A final short SubAgent
  review is required to confirm no remaining findings after the prompt sync.

Final SubAgent review:
- Chandrasekhar completed a final read-only review after the prompt sync.
- Findings:
  - No blocking findings.
  - No non-blocking findings.
- It confirmed:
  - `src/hl_trader/agent/prompts.py` and `configs/prompts/PROMPTS.md` explicitly
    state the `rerun_after_nl=True` trigger for second-pass strategy execution.
  - The factor README, Agent design doc, and Environment design doc have
    consistent two-stage semantics.
  - The prior fixes remain intact: default template does not force rerun,
    two-stage inputs are workspace-staged, formal artifacts reject runtime
    cache files while trusted template sources ignore local cache, `/mnt/artifacts`
    runtime access is guarded, date fields are documented, and custom
    `trade_intents` attribution skip semantics match the implementation.
- Final local checks after closing the SubAgent:
  - `git diff --check`
    - Result: passed.
  - Cache scan for `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
    `*.pyc`, and `*.pyo`
    - Result: empty.

Final conclusion:
- The requested backtest strategy-program interface, related templates, docs,
  validation coverage, and iterative SubAgent audit cycle are complete for this
  work item.

## 2026-06-19 - Custom trade-intent minute replay

Task:
- Add minute-line replay for custom `trade_intents` so strategies such as
  `close_buy`, `low_buy`, `high_short`, and `t` can execute at finer granularity
  when replay slots provide minute bars.

Implementation:
- `backtest_tool` now reads `intraday_1min.parquet` once per replay slot and
  passes `replay_granularity="minute"` to `run_strategy(context)` only when the
  minute file is present and non-empty. Empty or missing minute files use daily
  fallback consistently in context, summary, and `detailed_return.json`.
- `run_trade_intent_replay()` uses minute replay for custom intents when a
  non-empty minute frame is available; default order-plan replay remains daily
  fixed holding.
- Minute replay normalizes `trade_time` to `HH:MM`, uses first bar at or after
  `trigger_time`, defaults `close_buy` to `14:57`, uses minute `low/high` to
  trigger `low_buy/high_short`, fills triggered minute entries at minute
  `close`, and lets `t` exits happen on later trading days at minute `close`.
- Missing minute coverage is handled with daily synthetic bars:
  - If a whole day has no minutes, daily open/close synthetic bars are used.
  - If a code is missing from a partially covered day, daily synthetic bars are
    added for that code so Broker rejection/constraint paths still run.
  - If a code has only early minutes and no real late/close bar, a synthetic
    `15:00` close bar is added for late triggers such as `close_buy`.
- Daily fallback for `low_buy/high_short` now uses daily `low/high` when
  present, or an `open/close` range when those columns are absent. Entry fill
  price remains the existing daily entry price.
- Broker gained explicit-price fills and exits through `fill_prices()` and
  `close_position_at_price()` while preserving commission, stamp duty, cash,
  short margin, T+1, suspension, limit-price, and shortability checks.
- `validate_trade_intents()` now requires valid `trigger_price` for executable
  `low_buy/high_short/t`, rejects non-positive, non-finite, and non-numeric
  provided trigger prices, and validates non-empty `trigger_time`.
- Docs and prompt sources now state the non-empty minute-file rule, minute
  trigger/fill semantics, daily fallback semantics, T+1 limit, and summary
  `replay_granularity`.

Validation:
- Resource checks:
  - Before target tests: system RAM generally stayed above about 59 GiB
    available after unrelated cluster jobs grew; GPU jobs were unrelated and no
    GPU workload was launched by this task.
  - After full tests: system RAM about 59 GiB available; unrelated GPU jobs
    continued on GPU 0/1-7.
- Commands:
  - `/home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/broker.py src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/tools/backtest.py`
    - Result: passed.
  - `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine`
    - Result after final fixes: 31 tests OK.
  - `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow.ToolFlowTest.test_empty_minute_replay_file_reports_daily_granularity`
    - Result after doc wording fix and cache cleanup: 32 tests OK.
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
    - Result: 222 tests OK.
  - `git diff --check`
    - Result: passed.
  - Cache scan for `__pycache__`, `.pytest_cache`, `.mypy_cache`,
    `.ruff_cache`, `*.pyc`, and `*.pyo`
    - Result: empty after cleanup.

SubAgent audit loop:
- Nash found five issues:
  - Empty minute files could make context say minute while replay fell back
    daily.
  - Missing trigger prices and invalid trigger times could silently alter
    strategy semantics.
  - Partial minute coverage could skip Broker constraints for missing codes.
  - Daily fallback used only open for `low_buy/high_short`.
  - Prompt omitted `close_buy` default `14:57`.
- Fixes were implemented and Nash was closed.
- Sagan found two follow-up issues:
  - A code with only early minutes could still skip late `close_buy` triggers.
  - `trigger_price=inf` passed validation.
- Fixes were implemented and Sagan was closed.
- Parfit found one low-severity issue:
  - Explicit non-numeric `trigger_price` was coerced to missing instead of
    rejected.
- Fix was implemented and Parfit was closed.
- Dirac found no blocking code issues and one low-severity wording mismatch:
  - Some docs/prompt text said the minute file only needed to exist, while code
    requires it to be non-empty.
- Wording was updated and Dirac was closed.

Current conclusion:
- Custom trade-intent minute replay is implemented, documented, and covered by
  regression tests. The final SubAgent review found no blocking code findings.

## 2026-06-19 - Living docs format normalization

Task:
- Unify the visible structure of the five current living docs after noticing
  that only the data and QMT docs still carried `整理日期：2026-06-07`.

Changes:
- Removed stale `整理日期` lines from `docs/data_documentation.md` and
  `docs/QMT_documentation.md`.
- Moved the QMT doc top matter into the same order as the other living docs:
  introduction, `相关边界`, `## 术语说明`, then `## 导航`.
- Added QMT cross-boundary links to the data, Agent, Environment, and Pipeline
  docs.

Validation:
- Scanned the five living docs for obsolete metadata labels; no matches
  remained.
- Confirmed all five docs now expose `相关边界`, `## 术语说明`, and `## 导航`
  in the same order.
- `git diff --check`: passed.
- Cache scan for `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `*.pyc`, and `*.pyo`: empty.

Current conclusion:
- The living docs now use a consistent concise top structure, without stale
  per-file date metadata.

## 2026-06-19 - Data update, audit, and revision sentinel check

Task:
- Inspect recent TuShare download/update/audit health and check whether
  revision sampling found source data that differs from existing local data.

Context:
- Real repository path confirmed with `pwd -P`: `/Data/lzp/MacroQuant`.
- Existing `LOGBOOK.md`, `logs/tushare_cron_dispatch.log`,
  `.runtime/tushare/cron_state.json`, `results/data_quality/*_status.json`,
  and `results/data_quality/revision_events.jsonl` were inspected.

Resource checks:
- Before API checks: system memory had about 461 GiB available; GPU 0 had an
  unrelated Python process using about 10.3 GiB, and no GPU workload was
  launched.
- After API checks: system memory still had about 461 GiB available; GPU state
  was unchanged apart from unrelated load.

Findings:
- The installed crontab still points to
  `/home/lzp/miniconda3/envs/stock/bin/python` and the removed
  `scripts/tushare/cron_update.py` entrypoint. This explains why
  `logs/tushare_cron_dispatch.log` has no structured successful job after
  2026-06-12 and only repeats old-entrypoint failures.
- `.runtime/tushare/cron_state.json` last valid entries are from
  2026-06-11/2026-06-12:
  - `cn_evening_full`: OK for `20260512-20260611`.
  - `cn_daily_revision_sentinel`: OK through `20260611`.
  - `cn_preopen_*`: OK through 2026-06-12 morning for board/text/margin
    backfills.
  - `cn_nightly_full_audit`: error on 2026-06-12 because base audit and
    intraday-by-date audit returned errors.
  - `cn_nightly_feature_build`: error on 2026-06-12 because
    `audit-fundamental-events` failed and the job fail-fast skipped the
    remaining commands.
- Current formal status files are stale by design after the cron break:
  base/macro/intraday/text/board are from 2026-06-11 UTC, event-flow is from
  2026-06-12 01:21 UTC, and the formal `revision_summary.json` is from the
  last 2026-06-12 sentinel.
- The formal `revision_events.jsonl` has 7,917 events. Of these, 7,701 point to
  real `/Data/lzp/MacroQuant/data/raw/...` paths and 216 are `/tmp/...` events
  from tests or temp runs. Real events stop on 2026-06-12; 2026-06-18/19 ledger
  tail entries are test/tmp pollution, not real raw-data changes.

Commands and results:
- Current-gap sentinel:
  - Command:
    `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_audit.py revision-sentinel --start-date 20260612 --end-date 20260619 --datasets daily daily_basic adj_factor stk_limit suspend_d limit_list_d --sample-size 0 --seed 20260619 --revision-ledger results/data_quality/process/revision_sentinel_20260619_current_gap_events.jsonl --output results/data_quality/process/revision_sentinel_20260619_current_gap_status.json --min-interval-seconds 0.22 --timeout-seconds 120`
  - Result: failed before API comparison because local SSE `trade_cal` covers
    only `20100101-20260618`, not `20260619`.
- Current-gap sentinel rerun:
  - Command:
    `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_audit.py revision-sentinel --start-date 20260612 --end-date 20260618 --datasets daily daily_basic adj_factor stk_limit suspend_d limit_list_d --sample-size 0 --seed 20260619 --revision-ledger results/data_quality/process/revision_sentinel_20260619_current_gap_events.jsonl --output results/data_quality/process/revision_sentinel_20260619_current_gap_status.json --min-interval-seconds 0.22 --timeout-seconds 120`
  - Result: `status=warning`, `revision_events=0`, `missing_local_dates=30`,
    `datasets_without_effective_checks=6`.
  - Interpretation: for `20260612`, `20260615`, `20260616`, `20260617`, and
    `20260618`, all six checked daily datasets lack local partitions, so the
    new-date source-vs-local comparison cannot run. This is a freshness gap,
    not a detected content revision.
- Historical sample sentinel:
  - Command:
    `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_audit.py revision-sentinel --start-date 20200101 --end-date 20260611 --datasets daily daily_basic adj_factor stk_limit suspend_d limit_list_d --sample-size 6 --seed 20260619 --revision-ledger results/data_quality/process/revision_sentinel_20260619_historical_sample_events.jsonl --output results/data_quality/process/revision_sentinel_20260619_historical_sample_status.json --min-interval-seconds 0.22 --timeout-seconds 120`
  - Result: `status=warning`, 36 checks across six datasets, 3 revision events.
  - The three events are all `limit_list_d.limit_amount` source rewrites:
    `20240102` for `688525.SH`, `20240418` for `000628.SZ`/`600083.SH`/
    `600836.SH`, and `20250305` for `000042.SZ`/`600811.SH`.
  - In all three cases, `limit_amount` changed from an old non-empty local
    value to an empty source value; keys and row counts were otherwise stable.
- Existing formal `revision_summary.json` from 2026-06-12 also showed the same
  pattern: 10 `limit_list_d` sample events, all `limit_amount` blanking on
  historical partitions, while sampled `daily`, `adj_factor`, and
  `daily_basic` had no revision events.

Artifacts:
- `results/data_quality/process/revision_sentinel_20260619_current_gap_status.json`
- `results/data_quality/process/revision_sentinel_20260619_historical_sample_status.json`
- `results/data_quality/process/revision_sentinel_20260619_historical_sample_events.jsonl`

Current conclusion:
- Recent automated data updates are broken because the installed cron block was
  not refreshed after the script/env migration. Data is stale after the
  2026-06-12 morning jobs.
- There is no successful new-date comparison after 2026-06-12 because the
  checked local daily partitions for 2026-06-12 through 2026-06-18 are missing.
- For existing historical partitions, revision sampling continues to detect
  `limit_list_d.limit_amount` instability. Treat this field as unreliable for
  frozen trading inputs until a downstream policy either ignores it, rebuilds
  it from stable sources, or explicitly versions the source rewrites.

## 2026-06-20 - Data repair and cron/sentinel hardening

Task:
- Repair the data download/update/audit flow after the 2026-06-19 inspection,
  clean the revision ledger, and make the sentinel field policy explicit.

Resource checks:
- Before and after the repair/test commands, system memory stayed around
  457 GiB available. GPU 0 had an unrelated Python process using about
  10.3 GiB; no project GPU workload was launched.

Code and config changes:
- `src/hl_trader/data_sources/tushare/common.py`
  - Added formal-vs-temp revision ledger resolution so test and scratch raw
    roots write local `revision_events.jsonl` instead of polluting the formal
    ledger.
  - Added a repeated full-page guard in paged API reads.
  - Filtered `stock_basic` code loading to valid A-share codes only.
- `src/hl_trader/data_sources/tushare/download.py`
  - Routed revision-aware writes through the ledger resolver.
  - Made fundamental downloads honor explicit `--codes`.
- `src/hl_trader/data_sources/tushare/cron_update.py`
  - Runs child commands with unbuffered Python output for live cron logs.
- `src/hl_trader/data_sources/tushare/audit.py`
  - Capped `bak_basic` expected trade dates at the audit `end_date`.
- `configs/tushare_update_schedule.json`
  - Added `limit_list_d.unstable_fields=["limit_amount"]`.
  - Added `field_policy.limit_amount=raw_audit_only_until_field_versioned`.

Data actions:
- Removed 216 `/tmp/...` pollution rows from
  `results/data_quality/revision_events.jsonl`; formal ledger now contains only
  real raw-data paths.
- Reinstalled the crontab to use
  `/home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py`.
- Backfilled recent gaps:
  - `margin` and `margin_detail` for `20260612-20260617`.
  - `cctv_news` and `news` for `20260612-20260618`.
  - `stk_mins_1min_by_date` for `20260612`, `20260615`, `20260616`,
    `20260617`, and `20260618`.
  - Explicit `920126.BJ` fundamental zero-row valid partitions.
  - Removed invalid code partitions `T600018.SH`, `T00018.SH`, `TS0018.SH`.

Audits:
- Full audit command:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_full_audit --start-date 20200101 --end-date 20260618 --force-run`
- Result: return code 0.
- Current formal statuses:
  - `base_research_status.json`: warning, errors 0, warnings 15.
  - `macro_context_status.json`: warning, errors 0, warnings 2.
  - `intraday_minutes_status.json`: ok, errors 0, warnings 0.
  - `event_flow_status.json`: warning, errors 0, warnings 5.
  - `board_trading_status.json`: warning, errors 0, warnings 1.
  - `text_evidence_status.json`: warning, errors 0, warnings 20.
- Current-gap revision sentinel over `20260612-20260618` for
  `daily/adj_factor/daily_basic/stk_limit/suspend_d/limit_list_d` completed
  with no revision events after the backfill.
- Historical revision sentinel over `20200101-20260618`, sample size 6,
  seed `20260619-history`, found only `limit_list_d.limit_amount` source
  blanking on sampled historical partitions; no sampled key, row-count, or
  other field changes were found.

Validation:
- `tests.unit.test_data_sources_tushare` passed as part of the 107-test target
  run on 2026-06-20.
- Cache scan for `__pycache__` and `*.pyc`: empty after cleanup.

Current conclusion:
- The automated data path is repaired and documented. Sentinel policy is now
  both human-readable in `docs/data_documentation.md` and machine-readable in
  `configs/tushare_update_schedule.json`.
- `limit_list_d.limit_amount` remains raw/audit-only until a field-versioning
  policy exists.

## 2026-06-20 - Flat agent_output/main.py backtest refactor

Task:
- Refactor formal strategy execution to the user-requested model:
  `agent_output/main.py` is the only required entrypoint, Agent output stays
  flat, NL is callable from strategy code, and Broker replays daily/minute
  strategy logic while enforcing constraints.

Implementation:
- Replaced the fixed `factor/` + `nl_prior/` artifact contract with a flat
  `agent_output/` contract:
  - `main.py` required.
  - `candidate.py`, `trading.py`, `nl_prompt.md`, and other flat text/code
    helpers allowed.
  - Subdirectories, caches, hidden files, symlinks, binary/data dumps, logs and
    unsupported suffixes rejected.
- Reworked `modification_check_tool` to compare only flat `agent_output`
  files, total diff lines, Python code diff lines, file count, byte count, and
  readonly violations. Early Epoch constraints are looser; later Epochs use the
  stricter limits.
- Reworked `backtest_tool`:
  - Runs only `agent_output/main.py`.
  - Does not load `factor.json` or `prior.json`.
  - Does not force NL scoring or score fusion.
  - Serves `mq_tools.nl(ts_code, prompt=...)` over JSONL RPC only when strategy
    code calls it.
  - Writes `detailed_return.json`, `trade_intents.parquet`,
    `strategy_metadata.json`, optional `candidates.parquet`, and optional
    `nl_tool/` logs.
- Added a sandbox policy runner for custom trade strategy functions:
  - Built-ins: `target_weight`, `low_buy`, `close_buy`, `high_short`, `t`.
  - Non-built-in `trade_strategy` names must resolve to a function in
    `trading.py` or `main.py`.
  - The function runs inside Sandbox for every due daily/minute bar and receives
    `state` or keyword arguments including `bar`, `account`, `positions`,
    `price`, `cur_time`, `position`, `params`, and helpers
    `buy/sell/short/cover/close`.
  - Returned actions are executed by the host Broker; Agent code never writes
    cash, positions, fills, or returns.
- Updated prompt source, prompt export, templates, Agent/Environment/Pipeline
  docs, and root logbook.
- Removed the unused `factor_attribution_enabled` experiment config field; the
  attribution module remains marked legacy for historical tests/reports.

Validation:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
- Syntax:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/backtest_engine.py src/hl_trader/environment/tools/backtest.py src/hl_trader/environment/executor.py src/hl_trader/environment/broker.py src/hl_trader/agent/prompts.py configs/agent_output_template/main.py configs/agent_output_template/trading.py`
- Targeted custom callback test:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_custom_trading_function_runs_during_minute_replay -v`
  passed.
- Full tool-flow test:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow -v`
  passed, 24 tests.
- Core regression set:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_data_sources_tushare -v`
  passed, 107 tests.
- Cache scan for `__pycache__` and `*.pyc`: empty after cleanup.

Current conclusion:
- The formal backtest path now matches the flat `agent_output/main.py` design.
- NL and trading strategy freedom are moved into strategy code, while Broker
  and Environment keep the trusted enforcement/audit boundary.

## 2026-06-20 - SubAgent audit fixes after flat backtest refactor

Task:
- Close the issues found by the first SubAgent review and remove remaining
  obsolete score-fusion/order-plan code that no longer matches the accepted
  `agent_output/main.py` contract.

Audit findings and fixes:
- Fixed `audit_revision_sentinel()` so temporary/test raw roots use a local
  `revision_events.jsonl` via `resolve_revision_ledger()` instead of polluting
  the formal `results/data_quality/revision_events.jsonl`.
- Removed the misleading `nl_mode` field from BacktestTool-facing schemas,
  summaries, runner calls, pipeline calls and failure records. Backtest runs
  `main.py`; NL executes only when strategy code calls `mq_tools.nl(...)`.
- Set `SandboxSpec.max_fold_minutes` to 60 to match the Fold time budget.
- Removed legacy `compose_final_scores`, `build_order_plan`,
  `build_order_plan_from_trade_intents`, `validate_order_plan`,
  `build_trade_intents_from_plan`, and `run_fixed_holding_replay` from
  `backtest_engine.py`, plus their historical tests. Current tests now cover
  direct trade-intent validation and Broker replay.
- Removed the legacy factor-attribution module and tests because they only
  referenced retired `factor_score` / `factor_<id>` candidate semantics and had
  no active production caller.
- Replaced stale NL internal error strings (`nl_mode=off/sample`) with neutral
  tool-local labels, and renamed the LLM proxy purpose from `final_score` to
  `nl_score`.

Validation:
- Affected set:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow tests.unit.test_nl_scoring -v`
  passed, 67 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
  passed, 209 tests.
- `git diff --check` passed.
- Cache scans for `__pycache__` and `*.pyc` were empty after the final test
  run.

Current conclusion:
- Active code no longer contains the retired forced factor/NL score-fusion
  path. The remaining historical references to `nl_mode` are only old logbook
  records.

## 2026-06-20 - SubAgent follow-up fixes for no-op strategies and RPC cleanup

Task:
- Resolve the follow-up findings from the third SubAgent audit.

Implementation:
- Made `trade_strategy=flat` and `trade_strategy=none` explicit built-in no-op
  strategies.
- `validate_trade_intents()` now forces `side=flat` for those no-op strategies
  even when the Agent provided `side=long` or `side=short`.
- Expanded `run_strategy_program()` cleanup so `.strategy_*`,
  `.nl_requests_*`, and `.nl_responses_*` are unlinked even when executor path
  mapping, hide-context setup, or sandbox process startup raises.
- Hardened `StrategyPolicyRunner.__enter__()` so failed startup calls
  `__exit__()` and cleans `.policy_nl_*` files. Added `_hide_entered` to avoid
  exiting a snapshot-hide context that was never entered.
- Updated Agent/Environment docs, the flat template README, prompt source, and
  exported prompts to document `flat` / `none` as no-op built-ins.

Validation:
- Syntax:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/backtest_engine.py tests/unit/test_broker_engine.py`
  passed.
- Affected set:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow -v`
  passed, 49 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
  passed, 212 tests.
- `git diff --check` passed.
- Cache scans for `__pycache__` and `*.pyc` were empty after cleanup.

Current conclusion:
- No-op strategies cannot create orders, and formal strategy / custom policy
  temporary RPC files are cleaned on normal and setup-failure paths.

## 2026-06-20 - Restore living doc detail after refactor compression

Task:
- Improve readability of the Agent, Environment and Pipeline living docs after
  the flat backtest refactor, restore the previous second-level navigation
  style, and recover operational detail that was compressed even though it was
  unrelated to the refactor.

Review:
- Opened three SubAgents and closed all three after completion:
  - Agent doc comparison: restored session isolation, visible PIT data,
    tool semantics, Step workflow, workspace paths, modification checks, NL
    logs, forbidden behavior and acceptance checklist.
  - Environment doc comparison: restored PIT data windows, snapshot paths,
    feature units, Sandbox/Runner boundaries, trusted tools, Broker replay,
    shorting rules, LLM/NL boundaries, logs and audit checklist.
  - Pipeline doc comparison: restored rolling Fold windows, Step execution,
    Fold acceptance, frozen evaluation, artifact manifest, Epoch/meta-learning,
    Held-out workflow, ledger schema, reporting and failure conditions.

Implementation:
- Rewrote `docs/agent_design.md`, `docs/environment_design.md`, and
  `docs/pipeline_design.md` with explicit `## 导航` lists pointing to
  second-level numbered sections.
- Kept the accepted flat `agent_output/main.py` contract while expanding
  retained operating details for PIT windows, Sandbox, Agent tools, Broker
  replay, Fold/Epoch orchestration and reporting.
- Updated `src/hl_trader/agent/prompts.py` and regenerated
  `configs/prompts/PROMPTS.md` so prompt docs match the restored living docs.
- Updated the concise `LOGBOOK.md` current-state summary with positive current
  contracts for the Agent and backtest path.

Validation:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
- Checked restored headings:
  `rg -n "^(# |## 导航|## [0-9]|## 术语说明)" docs/agent_design.md docs/environment_design.md docs/pipeline_design.md`
- Checked retired comparison keywords in living docs and prompt exports:
  `rg -n "final_score|nl_mode|factor\\.json|prior\\.json|build_order_plan|compose_final_scores|order-plan|order_plan|nl_prior|factor/|不再强制|不再生成|旧版|旧版本|重构前|重构后" docs/agent_design.md docs/environment_design.md docs/pipeline_design.md src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md`
  returned no matches.
- `git diff --check` passed.
- Cache scans for `__pycache__` and `*.pyc` were empty after cleanup.

Current conclusion:
- The three restored living docs are still shorter than the pre-refactor
  versions because duplicated history and superseded wording were removed, but
  the non-refactor operational details identified by SubAgents are back in the
  current design contract.

## 2026-06-20 - Remove precomputed daily alpha layer

Task:
- Remove the fixed daily alpha feature path so Agent-visible inputs are only
  PIT snapshot/history windows, normalized units, and visibility constraints.

Implementation:
- Deleted `src/hl_trader/environment/features/daily_pit.py` and removed
  `DailyPITFeatureBuilder` / `FeatureBuildConfig` exports.
- Renamed the PIT event CLI from `scripts/data/build_features.py` to
  `scripts/data/build_pit_events.py`; the CLI now exposes only
  `build-fundamental-events` and `audit-fundamental-events`.
- Changed the scheduled job from `cn_nightly_feature_build` /
  `hl_feature_pipeline` to `cn_nightly_pit_event_build` /
  `pit_event_pipeline`. The job now builds and audits only
  `fundamental_events`.
- Changed the default PIT event root from `data/features/fundamental_events`
  to `data/pit/fundamental_events`.
- Updated Data, Environment and QMT living docs to state that Agent-visible
  input is the PIT snapshot/history window with standardized units and
  visibility filtering, not precomputed rolling alpha columns.
- Removed daily-alpha-specific unit tests and kept coverage for PIT raw store,
  auction correction and fundamental event visibility indexing.

Validation:
- Syntax: `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile scripts/data/build_pit_events.py src/hl_trader/data_sources/tushare/cron_update.py src/hl_trader/data_sources/tushare/audit.py src/hl_trader/environment/snapshot.py src/hl_trader/environment/features/__init__.py src/hl_trader/pipelines/config.py scripts/experiments/run_experiment.py tests/unit/test_features.py tests/unit/test_data_sources_tushare.py`
- CLI help: `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/build_pit_events.py --help`
- Cron dry-run: `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_pit_event_build --end-date 20260618 --dry-run`
- Tests: `tests.unit.test_features` passed, 7 tests.
- Tests: `tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest` passed, 57 tests.
- Tests: `tests.unit.test_snapshot_builder` passed, 4 tests.
- Tests: `tests.unit.test_pipeline_e2e` passed, 13 tests.

Current conclusion:
- The current path no longer builds or exposes `daily_alpha`, fixed rolling
  returns, moving averages, volatility, final scores, or candidate rankings as
  Environment-provided alpha inputs.

## 2026-06-20 - Sandbox null audit and PIT/NL follow-up fixes

Task:
- Inspect `.runtime/sandboxes/run_00add6d7173e/snapshots/train/macro.parquet`
  for the high null rate in `cn_gdp`, check the same sandbox for similar data
  issues, decide whether data needs reprocessing/deletion, and run iterative
  SubAgent review over the current code/docs changes.

Data inspection:
- Resource checks before and after the local scripts showed more than 400 GiB
  available RAM. GPUs were already occupied by unrelated Python jobs; this work
  used CPU/IO only.
- The train snapshot manifest is `kind=decision_input`,
  `decision_time=2022-07-01T09:25:00+08:00`, and
  `window_start=2020-10-01T09:25:00+08:00`.
- `macro.parquet` shape is `(7649, 124)`. `cn_gdp` has 7 rows and 124 columns;
  its non-null columns are `dataset`, `quarter`, `gdp`, `gdp_yoy`, `pi`,
  `pi_yoy`, `si`, `si_yoy`, `ti`, `ti_yoy`, `available_at`, and
  `available_at_rule`. The other 112 columns are structural nulls from the
  multi-dataset wide union, not missing GDP data.
- `daily.parquet`, `intraday_1min.parquet`, and `text_index.parquet` had no
  high-null data columns in the same scan. `universe.delist_date` is high-null
  because most visible names are not delisted. `events.parquet` and
  `fundamentals.parquet` are sparse by dataset for the same wide-union reason;
  observed high-null fields such as `repurchase.exp_date` and optional
  financial statement fields are source/schema sparsity rather than join
  failure.

PIT event repair:
- Built the new event layer:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/build_pit_events.py build-fundamental-events --raw-dir data/raw --output-root data/pit/fundamental_events --start-date 20200101 --end-date 20260618`
- Audited the new event layer:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/build_pit_events.py audit-fundamental-events --events-root data/pit/fundamental_events --start-date 20200101 --end-date 20260618 --output results/data_quality/fundamental_events_status.json --require-partitions`
- Audit result: `status=warning`, `errors=0`, `warnings=6`,
  `rows=1,828,774`; `data/pit/fundamental_events` contains 742 parquet
  partitions.
- A SubAgent found that snapshot construction did not enforce the PIT event
  audit. Fixed by adding `fundamental_events_status` to `SnapshotBuilder` and
  `RawSnapshotProvider`, checking the status file when `fundamental_datasets`
  is non-empty, and making `read_fundamental_events(require_partitions=True)`
  fail fast on missing root or zero usable partitions.
- Added snapshot tests for missing PIT event partitions and audit error
  blocking decision snapshot construction.
- A follow-up SubAgent noted that direct `SnapshotBuilder` construction could
  still omit the audit status path. Fixed by requiring a PIT event status file
  whenever `fundamental_datasets` is non-empty and setting the default
  `RawSnapshotProvider` status path to
  `results/data_quality/fundamental_events_status.json`.

NL schema repair:
- A follow-up SubAgent found the NL prompt/schema still referenced
  `prior_rules` and `applied_prior_ids`. Removed that contract from
  `ROUND_INSTRUCTION`, `FINAL_INSTRUCTION`, `NLScoringEngine`,
  `validate_score_payload()`, `backtest_tool` neutral scores, prompt export,
  and NL-related tests.
- Current `mq_tools.nl(ts_code, prompt=...)` score schema is
  `ts_code`, `nl_score`, `confidence`, `risk_tags`, and `evidence_ids`.

Documentation and cleanup:
- Restored `###`-level navigation in `docs/agent_design.md`,
  `docs/environment_design.md`, and `docs/pipeline_design.md`.
- Removed ignored `src/macroquant_hl_trader.egg-info` after SubAgent noted it
  still referenced a deleted source file.
- Removed the unused legacy `run_generate_candidates()` wrapper from
  `src/hl_trader/environment/backtest_engine.py`; the formal path is
  `run_strategy_program()` executing flat `agent_output/main.py`.
- Cleared generated `__pycache__` / `*.pyc` after validation.
- Old `data/features/daily_alpha` (2.1G) and
  `data/features/fundamental_events` (598M) are retired local data products.
  They are not required for current correctness and do not need reprocessing;
  they can be deleted or quarantined later as a disk cleanup step. Raw data and
  the current `data/pit/fundamental_events` layer should be retained.

Validation:
- Syntax checks passed for the affected PIT/NL modules and tests.
- `tests.unit.test_snapshot_builder` passed, 7 tests.
- `tests.unit.test_features` passed, 7 tests.
- `tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest`
  passed, 57 tests.
- `tests.unit.test_pipeline_e2e` passed, 13 tests.
- `tests.unit.test_nl_scoring` + `tests.unit.test_tools_flow` passed, 45
  tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t . -p 'test_*.py'`
  passed, 208 tests.
- `git diff --check` passed after removing a trailing blank line.
- Keyword scan for current code/docs/tests found no matches for
  `prior_rules`, `applied_prior_ids`, `nl_prior`, `factor.json`,
  `prior.json`, forced `final_score`, `daily_alpha`, `DailyPITFeatureBuilder`,
  `build_features.py`, or `data/features/fundamental_events` outside the
  historical detailed logbook and external references.
- Cache scans for `__pycache__` and `*.pyc` were empty after cleanup.

Current conclusion:
- The `cn_gdp` null pattern is reasonable under the current wide-union macro
  schema. No sandbox data reprocessing is needed for this issue.
- The current PIT event layer is usable with warnings and errors=0; snapshot
  construction now fails fast on unavailable, omitted, or failed PIT event
  status inputs.
- The current NL tool no longer depends on prior-rule artifacts.

## 2026-06-22 - Delete retired daily alpha data product

Task:
- Delete the retired `data/features/daily_alpha` data product after confirming
  it is no longer used by the current PIT snapshot/backtest pipeline.

Commands:
- Confirmed the real path with `pwd -P`:
  `/Data/lzp/MacroQuant`.
- Checked size before deletion:
  `du -sh data/features/daily_alpha` -> `2.1G`.
- Deleted:
  `rm -rf data/features/daily_alpha`.
- Verified:
  `test ! -e data/features/daily_alpha && echo deleted` -> `deleted`.

Current conclusion:
- `data/features/daily_alpha` has been removed. Current raw data and
  `data/pit/fundamental_events` were not touched. The old
  `data/features/fundamental_events` directory remains as a separate retired
  product and can be handled in a later cleanup if requested.

## 2026-06-22 - Repair TuShare trading-day cron windows and refresh data status

Task:
- Remove fixed-date operational wording from living data documentation.
- Fix scheduled TuShare jobs so trade-date windows use the latest SSE open date
  on or before the natural target date.
- Backfill the missing margin data, refresh audits, install the current crontab,
  and verify the PIT event job.

Code and documentation changes:
- Added `end_date_mode=sse_open_on_or_before` support in
  `src/hl_trader/data_sources/tushare/cron_update.py`.
- Updated `configs/tushare_update_schedule.json` so trade-date jobs use open
  trading-day semantics while text evidence keeps natural-day windows.
- Updated `docs/data_documentation.md` to describe reusable status-file
  acceptance criteria instead of a one-off dated audit result.
- Removed the top-level `更新时间` metadata from the five living design docs.
  Example dates remain only where they are part of command, window, or payload
  examples.
- Aligned `cn_nightly_pit_event_build` rolling windows to the first day of the
  month, because the PIT event layer writes monthly partitions and the audit
  checks the same partition granularity.
- Refreshed the system crontab with `ops/cron/install_tushare_cron.py`; the
  installed block now calls `cn_nightly_pit_event_build` and no longer calls
  `cn_nightly_feature_build`.

Data repair and audits:
- Resource checks before and after data operations showed more than 400 GiB
  available RAM. GPUs were occupied by unrelated jobs; these TuShare operations
  were CPU/IO only.
- Ran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_preopen_margin_backfill_0905 --end-date 20260621 --force-run`
  - Result: `status=ok`, `start_date=end_date=20260618`.
  - Log: `logs/tushare_cron_cn_preopen_margin_backfill_0905_20260618_20260622_191627.log`.
  - Wrote `margin` rows=3 and `margin_detail` rows=4370 for the repaired
    trade date.
- Ran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_preopen_event_flow_audit_0920 --end-date 20260621 --force-run`
  - Result: `status=ok`, `end_date=20260618`.
  - Log: `logs/tushare_cron_cn_preopen_event_flow_audit_0920_20260618_20260622_191644.log`.
- Ran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_full_audit --end-date 20260621 --force-run`
  - Result: `status=ok`, return codes `[0, 0, 0, 0, 0, 0]`.
  - Log: `logs/tushare_cron_cn_nightly_full_audit_20260621_20260622_191804.log`.
- Ran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_pit_event_build --end-date 20260621 --force-run`
  - First run exposed a month-window audit error from a mid-month rolling
    start. After aligning PIT starts to month start, rerun succeeded.
  - Final log: `logs/tushare_cron_cn_nightly_pit_event_build_20260621_20260622_194630.log`.
- Ran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_evening_full --end-date 20260621 --force-run`
  - Result: `status=ok`, resolved trade-date window `20260519-20260618`.
  - Log: `logs/tushare_cron_cn_evening_full_20260618_20260622_195604.log`.
  - This refreshed reference, daily, macro, global, event-flow, board-trading,
    intraday, share-float, text-evidence, and fundamental raw domains.
- Because the evening update changed raw fundamental data, reran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_pit_event_build --end-date 20260621 --force-run`
  - Result: `status=ok`.
  - Log: `logs/tushare_cron_cn_nightly_pit_event_build_20260621_20260622_223725.log`.
- Reran:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/data/tushare_cron_update.py --job cn_nightly_full_audit --end-date 20260621 --force-run`
  - Result: `status=ok`.
  - Log: `logs/tushare_cron_cn_nightly_full_audit_20260621_20260622_224404.log`.

Final status:
- `cn_evening_full`: ok, `20260519-20260618`.
- `cn_preopen_margin_backfill_0905`: ok, `20260618-20260618`.
- `cn_preopen_event_flow_audit_0920`: ok, `20200101-20260618`.
- `cn_nightly_full_audit`: ok, `20200101-20260621`.
- `cn_nightly_pit_event_build`: ok, command window `20260201-20260621`
  internally, cron state target `20260221-20260621`.
- `base_research_status.json`: warning, errors=0.
- `macro_context_status.json`: warning, errors=0.
- `intraday_minutes_status.json`: ok, errors=0.
- `event_flow_status.json`: warning, errors=0.
- `board_trading_status.json`: warning, errors=0.
- `text_evidence_status.json`: warning, errors=0.
- `fundamental_events_status.json`: warning, errors=0.
- No TuShare cron/update/audit process remained running; `.runtime/tushare/locks`
  was empty.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_data_sources_tushare.TuShareDownloadUpdateGuardsTest`
  passed, 59 tests.
- Dry-run checks confirmed `cn_preopen_margin_backfill_0905 --end-date
  20260621` resolves to `20260618`, and `cn_nightly_full_audit` keeps natural
  text windows while using `20260618` for event-flow.
- `crontab -l` contains `cn_nightly_pit_event_build` and does not contain
  `cn_nightly_feature_build`.
- Living-data-doc scan no longer finds fixed operational result wording; only
  example dates remain in living docs.
- `git diff --check` passed.

## 2026-06-22 Decouple trading strategies from the Broker into Agent ctx functions

Task: remove the Broker's built-in strategy vocabulary so trading strategies live
entirely in the Agent layer; the Broker exposes only fundamental primitives and
the Environment replays minute-by-minute, calling each mapped stock's strategy
function. Branch `refactor/decouple-broker-strategies` off `a662b77` (work kept
as uncommitted working-tree changes per the user; not committed).

Design (confirmed with user): strategy functions take a single `ctx` object
(`ctx.broker`, `ctx.stock`, `ctx.cur_price/cur_time/cur_date`, `ctx.params`,
`ctx.nl`). `main.py` returns `trade_intents` mapping each stock to one function
name, e.g. `{"code": "600000.SH", "trade_strategy": "t", "amount": 2000}`.

Key changes:
- `src/hl_trader/environment/broker.py`: `SimBroker` now exposes amount-based
  partial primitives via `execute(ts_code, action, ...)` (+`buy/sell/short/
  cover/close` wrappers) with weighted-average cost, per-position `locked_today`
  T+1 sellable tracking (`_advance_date` releases prior-day shares), runtime
  `max_total_holdings` (`max_holdings_reached`), single-name weight cap clamp,
  per-code `trade_ledger`/`trades_for()`, and `position_reduced`/`position_closed`
  PnL events. Removed the two-phase submit/fill order path.
- `src/hl_trader/environment/backtest_engine.py`: removed built-in strategy
  names and trigger helpers; unified minute/daily replay into a single
  minute-canonical loop (daily-synthesized 09:30/15:00 fallback) that calls the
  strategy function for every due intent each bar; rewrote `_STRATEGY_POLICY_
  DRIVER` to build the `ctx` object (broker/stock proxies with optimistic
  intra-bar view); simplified `validate_trade_intents` (unique codes, resolvable
  function name, params merge, date checks); renamed `custom_trade_strategy_
  names` → `strategy_function_names`; `compute_return_stats` sums partial + full
  exits and adds `max_holdings_reject_count`.
- `src/hl_trader/environment/tools/backtest.py`: always runs through the policy
  runner; dropped the static side/gross/`_short_unavailable_intent_count`
  checks (now runtime); summary uses `trade_strategies`.
- `src/hl_trader/environment/executor.py`: Docker `popen` now passes `-i` so the
  persistent stdin-based policy runner works in-container (previously masked
  because built-in strategies bypassed the runner).
- Template `configs/agent_output_template/{trading.py,main.py,README.md}`,
  `src/hl_trader/agent/prompts.py`, and `configs/prompts/PROMPTS.md` rewritten to
  the `ctx` contract. Living docs updated: `docs/environment_design.md` §6.1/§7,
  `docs/agent_design.md` §5.2/§5.3.
- Tests rewritten: `tests/unit/test_broker_engine.py` (primitive accounting,
  T+1 partial clamp, max-holdings, single-name cap, `trades_for`, ctx-replay via
  a fake policy), `tests/unit/test_tools_flow.py`, `tests/unit/fixtures_sandbox.py`.

Resource checks: system memory ~404Gi available before and after (CPU-only unit
tests; no GPU workload).

Validation (env `~/miniconda3/envs/quant`):
- `python -m unittest tests.unit.test_broker_engine tests.unit.test_artifacts
  tests.unit.test_tools_flow tests.unit.test_pipeline_e2e` → 66 tests OK
  (includes the Dockerized fold e2e, which exercises the in-container ctx policy
  runner).
- `python -m unittest discover -s tests` → 208 tests OK.
- `python scripts/dev/export_prompts.py` regenerated `configs/prompts/PROMPTS.md`.
- `git diff --check` passed; no tracked caches/`.pyc`.

## 2026-06-22 - Strategy template wording cleanup

Task: follow up on the Broker/Agent strategy decoupling review by reducing the
appearance of built-in strategy names and making the documented T-strategy
example safe when no prior trade exists.

Changes:
- Renamed optional template strategy examples in
  `configs/agent_output_template/trading.py` to `example_*` names and documented
  that they are ordinary Agent-owned code, not Broker or Environment keywords.
- Updated `configs/agent_output_template/README.md`,
  `docs/agent_design.md`, and `docs/environment_design.md` to state that
  examples are editable samples and not built-ins.
- Updated the sample T strategy to guard empty `ctx.stock.trades` by falling
  back to the current bar price.
- Adjusted affected unit-test fixtures to use `example_build_once` and
  `example_swing_t`.

Validation:
- Resource checks before tests showed about 405 GiB available RAM; GPU load was
  unrelated to these CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow tests.unit.test_artifacts`
  passed, 53 tests.

## 2026-06-22 - Remove retired feature fundamental events directory

Task: delete the retired `data/features/fundamental_events` local data product
after the current PIT event layer moved to `data/pit/fundamental_events`.

Actions:
- Confirmed real repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Confirmed `data/features/fundamental_events` was an old 598M generated data
  product and current configuration/code use `data/pit/fundamental_events`.
- Deleted `data/features/fundamental_events`.
- Removed the now-empty `data/features/` parent directory.
- Confirmed `data/pit/fundamental_events` remains present.

Validation:
- `test ! -e data/features/fundamental_events` returned `removed`.
- `data/pit/fundamental_events` remains present at about 600M.

## 2026-06-23 - Rename Agent formal output mount

Task: rename the sandbox-visible Agent formal strategy output path from
`/mnt/agent/agent_output/` to `/mnt/agent/output/` while keeping the existing
internal strategy-artifact APIs stable.

Actions:
- Confirmed real repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- Changed `SandboxPaths.agent_output` to resolve to `agent/output`, so Docker
  path mapping exposes the formal strategy artifact as `/mnt/agent/output`.
- Changed `AGENT_TOP_LEVEL` to collect `workspace/` and `output/` from
  `/mnt/agent/`.
- Updated `MQ_AGENT_OUTPUT_DIR` default to `/mnt/agent/output`.
- Updated structured search to expose root `output`; removed the old
  `agent_output` public search root.
- Kept internal Python method/property names such as `paths.agent_output` and
  `lock_agent_output()` to avoid unrelated API churn.
- Updated current living docs, generated prompt snapshot, prompt source, and
  template README to use `output/`.
- Regenerated `configs/prompts/PROMPTS.md` with
  `scripts/dev/export_prompts.py`.
- Added a unit-test assertion that structured search can read `root="output"`.

Validation:
- Resource checks before tests showed about 409 GiB available RAM; GPU was not
  used by these CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_step_tree`
  passed, 60 tests.
- `git diff --check` passed.
- Cache scan under `src`, `tests`, and `scripts` returned empty after cleanup.

## 2026-06-23 - Delegate concentration limits to Agent strategy

Task: make maximum holdings and single-name weight limits Agent strategy
decisions by default rather than mandatory Broker defaults.

Actions:
- Changed `BrokerProfile.max_total_holdings` and
  `BrokerProfile.max_single_name_weight` defaults to `None`.
- Bumped the default Broker profile id to `citic_default_v3` because the
  default concentration behavior changed.
- Broker now enforces those two concentration limits only when the profile
  explicitly sets them.
- Removed the unused `max_single_name_weight` parameter from
  `validate_trade_intents()`; trade-intent validation remains structural.
- Removed the top-level `max_total_holdings` run-manifest field from Pipeline
  records; optional concentration limits remain inside `broker_profile` when
  explicitly configured.
- Updated `docs/environment_design.md`, `docs/agent_design.md`, Agent prompt
  source, generated `configs/prompts/PROMPTS.md`, and Agent output template
  docs to state that concentration is controlled by Agent logic by default.
- Kept tests proving explicit Broker concentration limits still work when set.

Validation:
- Resource checks before tests showed about 410 GiB available RAM; GPU was not
  used by these CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_tools_flow tests.unit.test_pipeline_e2e`
  passed, 61 tests.
- `git diff --check` passed.
- Cache scan under `src`, `tests`, and `scripts` returned empty.

## 2026-06-23 - Refactor NL service into Sub Agent tool

Task: replace fixed-schema NL scoring with an Agent-callable NL Sub Agent that
can use PIT text retrieval and return free-form results for strategy code to
parse.

Actions:
- Replaced `NLScoringEngine` with `NLSubAgentEngine`. The host now starts one
  bounded NL Sub Agent task for each `mq_tools.nl()` request.
- Preserved `TextRetriever` grep/PIT retrieval semantics and wrapped it as the
  Sub Agent-only `text_retrieve` tool. Tool calls use a small JSON protocol;
  final Sub Agent answers are not schema-limited.
- Changed sandbox `mq_tools.nl()` / `ctx.nl` to return a result dict with
  `status`, `content`, `tool_calls`, `evidence`, `error`, and related metadata.
  Agent code must parse any score, label, or rule signal itself.
- Removed fixed `nl_score` validation and neutral-score failure handling.
  Default NL failure behavior is now `return_error_with_audit`.
- Updated BacktestTool logs so `nl_requests.jsonl` records Sub Agent results,
  `search_requests.jsonl` records text retrieval tool calls, `evidence.jsonl`
  records returned PIT evidence, and `nl_llm_calls.jsonl` records provider
  calls.
- Updated Agent prompts, exported `configs/prompts/PROMPTS.md`, Agent output
  template docs/code, `docs/agent_design.md`, and `docs/environment_design.md`.

Validation:
- Resource checks before validation showed about 410 GiB available RAM; GPU was
  not used by these CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_nl_scoring tests.unit.test_tools_flow tests.unit.test_pipeline_e2e`
  passed, 50 tests.
- `git diff --check` passed.
- Cache scan under `src`, `tests`, and `scripts` returned empty.
- Post-validation resource check showed about 411 GiB available RAM; GPU load
  was from unrelated existing processes.

## 2026-06-23 - Simplify frozen strategy artifact manifest

Task: reduce the strategy artifact `manifest.json` to fields that belong to
the frozen artifact itself, and keep Step/run audit references in the Fold
ledger.

Actions:
- Updated `ExperimentPipeline._freeze()` so artifact manifests now use
  `source_fold_id` and `source_step_id` instead of `created_at_fold` and
  `created_at_step`.
- Removed the redundant `frozen` field from artifact manifests. The artifact
  directory under `strategy_artifacts/` and the immutable hash check define the
  frozen boundary.
- Kept `validation_result_ref`, `run_manifest_ref`, `modification_check_ref`,
  and `modification_delta_summary` in Fold ledger Step records rather than in
  artifact manifests.
- Updated `docs/pipeline_design.md` section 5.2 to describe artifact manifest
  as identity, lineage, source run/fold/step, hash, and creation time only.

Validation:
- Resource checks before validation showed about 410 GiB available RAM; GPU was
  not used by these CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e tests.unit.test_artifacts tests.unit.test_step_tree`
  passed, 23 tests.
- `git diff --check` passed.
- Cache scan under `src`, `tests`, and `scripts` returned empty.
- Post-validation resource check showed about 410 GiB available RAM; GPU load
  was from unrelated existing processes.

## 2026-06-23 - Step tree visibility and meta-learning full records

Task: three small Step-tree visibility improvements plus two meta-learning input
fixes, scoped deliberately small (no new memory-store module). Approved scope:
(1) surface the tree rendering, optionally record failed attempts, reuse the
attachment field; (2) inject full raw development records into the meta-learning
Agent; (3) concatenate all prior-epoch meta-learning logs.

Step tree (`src/hl_trader/environment/step_tree.py`, `tools/backtest.py`,
`pipelines/config.py`, `pipelines/experiment.py`):
- `StepTree.save()` now also writes `steps/tree.txt` (the existing but unused
  `render_ascii()` rendering) on every mutation, so the Fold Agent reads a
  digest instead of parsing `tree.json`. `render_ascii()` marks dead ends with
  `[failed]`.
- Added `StepTree.record_failed_attempt(...)`: a lightweight dead-end node with
  no `output/` snapshot that intentionally does NOT move `current_node_id`.
  `position_for_hash()` now skips non-`complete_validation` nodes so a failed
  attempt can never be resolved as a parent. `BacktestTool._record_failure()`
  records one for `mode=="valid"` failures, gated by the new
  `ExperimentConfig.record_failed_attempts` flag (default True) threaded into
  the fold run manifest.
- `BacktestTool._execute()` now passes `attachments={detailed_return.json,
  strategy_metadata.json}` to `record_step`, so each validated node carries its
  own return/metadata digest.

Meta-learning inputs (`ExperimentPipeline.run_meta_learning`):
- Fixed the dead `meta_learning_memory.jsonl` wiring (it previously read this
  epoch's own not-yet-written `agent_trace.jsonl`, so it was always empty). New
  helper `_prior_meta_learning_logs(epoch_id)` concatenates the `agent_trace`
  logs of every earlier epoch's meta-learning session, ordered by epoch.
- Added `experiment_ledger_full.jsonl` to `workspace/`: every raw `fold` /
  `meta_learning` ledger record, with `heldout` excluded. Recorded the new path
  under the run manifest `development_inputs`.

Prompts/docs:
- `agent/prompts.py`: STEP_TREE_SECTION documents `tree.txt`, `[failed]` nodes
  and per-node attachments; META_LEARNING_INSTRUCTION documents the raw ledger
  and concatenated prior-epoch memory. Regenerated `configs/prompts/PROMPTS.md`
  via `scripts/dev/export_prompts.py` (the snapshot was stale relative to recent
  refactors, so the regen also caught it up; no commits exist yet, so this is
  purely working-tree alignment).
- Updated `docs/agent_design.md` (steps tree description) and
  `docs/pipeline_design.md` 6.1 (meta-learning inputs).

Tests:
- `tests/unit/test_step_tree.py`: failed-attempt dead-end semantics
  (position/parent/no-snapshot/hash-skip) and the persisted `tree.txt` rendering
  with `[failed]`; extended the phase-prompt assertion.
- `tests/unit/test_pipeline_e2e.py`: a 2-epoch run asserting epoch 1 has empty
  ledger/memory inputs, epoch 2 sees epoch 1's raw fold+meta records (no
  held-out) and its concatenated meta-learning log.

Validation:
- Pre/post resource checks showed about 420 GiB available RAM; CPU-only tests.
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 204 tests.
- `git diff --check` clean; cache/artifact leak scan empty.

## 2026-06-23 - Living docs consistency audit

Task: audit the five living docs (`data_documentation`, `agent_design`,
`environment_design`, `pipeline_design`, `QMT_documentation`) for consistency
with the current code, analyze process-logic rationality, and trim redundant or
obsolete content while keeping them concise yet comprehensive.

Consistency checks (all matched code/config):
- Tool action names and `allowed_modes` (runner + tool specs): fold tools
  shell/grep/glob/modification_check/backtest/finish_fold/note; meta-learning
  adds web_search/done, no backtest/finish_fold.
- Snapshot files (`snapshot.py` SNAPSHOT_FILES) and replay-slot subset.
- Broker primitives and profile (`broker.py`: buy/sell/short/cover/close +
  get_account/get_positions/query_orders/trades_for; default
  `proxy_margin_secs`, no forced max-holdings/single-name caps).
- 09:30 SZ auction factors 0.76 (`00*`) / 0.58 (`30*`) (`features/auction.py`).
- NL classes `TextRetriever` / `NLSubAgentEngine` / `build_company_contexts`
  and the `status/content/tool_calls/evidence/error` result dict.
- 11 cron jobs (`configs/tushare_update_schedule.json` + `ops/cron`), CLI
  subcommands (`download.py` / `audit.py`), the 6 raw status-file names
  (`common.py`) + `fundamental_events_status.json`, code-entry files, and the
  `output/` template file set.
- Stale-token scan and obsolete/migration/version-label scan over the five docs
  both came back empty — the living docs had been kept current (unlike the
  `PROMPTS.md` snapshot fixed earlier).

Two fixes applied:
- `agent_design.md` 5.3: removed the `ctx` interface bullet list that duplicated
  `environment_design.md` 7.2 almost verbatim; replaced with a one-line summary
  pointing to environment_design chapter 7 (the owner of the Broker/runtime
  contract). Removes cross-doc redundancy.
- `pipeline_design.md` 8.3: the fold-record example listed
  `parent_strategy_artifact_hash`, but `ExperimentPipeline` writes only
  `parent_strategy_artifact_id` into the fold ledger record (the parent hash
  lives in the run manifest, reachable via `run_manifest_ref`). Removed that
  field and corrected `snapshot_ids` keys to the actual
  `valid_decision_input` / `test_decision_input` / `valid_replay` /
  `test_replay`.

Process-logic review: download/update/audit gating, Fold/Epoch/Held-out
orchestration, freeze-before-test, roll-forward (test quarter -> next validation
quarter), PIT visibility, and the QMT standby workflow are all rational and
internally consistent. Deliberately retained: the data-doc 2/5.2 visibility
quick-reference and the agent/environment mount-path tables — overlapping by
design (different readers/uses), not redundant cruft.

Validation: `git diff --check` clean on `docs/`. Documentation-only pass; no
code or tests changed.

## 2026-06-23 - Full audit follow-up and configurable Fold period

Task: continue the repository-wide audit follow-up, close the SubAgent audit,
fix remaining clarity/contract issues, and ensure each Fold decision period can
be configured at day/week/month/quarter/year cadence.

Repository/path checks:
- Logical cwd: `/home/coder/projects/adm-cube-l20-8884/macroquant-1741651ef8a3`
- Physical cwd confirmed by `pwd -P` / `realpath .`: `/Data/lzp/MacroQuant`
- Worktree was already dirty from the broader backtest/data/docs refactor; only
  related changes were edited.

SubAgent result:
- Audit SubAgent completed and was closed.
- Findings: missing `train_snapshot` alias/hash in run manifest; Step summary
  docs overpromised timeout/no-update status details; legacy candidate/score
  fields remained in code paths; experiment CLI defaults were cwd-sensitive;
  `external_references/` is intentional external reference material and should
  remain outside production code paths.

Implementation:
- `folds.py`: generalized schedule generation from quarter-only helpers to
  `period_range`, `period_bounds`, `previous_period`, `heldout_periods`, and
  `build_fold_schedule(..., period=...)`. Supported units are `day`, `week`,
  `month`, `quarter`, and `year`; quarter helpers remain for compatibility.
- `ExperimentConfig`: main config fields are now `first_test_period`,
  `last_test_period`, `heldout_first_period`, and `heldout_last_period`, with
  legacy `*_quarter` InitVar aliases guarded against conflicting values.
- `ExperimentPipeline`: passes `fold_period` through development and held-out
  scheduling; run manifests record `fold_period`; `/mnt/snapshots/train` is
  recorded as `train_snapshot` with `alias_of=valid_decision_input` and the same
  snapshot hash.
- `run_experiment.py`: default paths now resolve from the repository root, not
  the caller's cwd; CLI adds generic `--fold-period`, `--first-test-period`,
  `--last-test-period`, `--heldout-first-period`, and
  `--heldout-last-period`, while legacy quarter flags remain accepted.
- Removed dead legacy configuration/reporting fields:
  `long_score_threshold`, `short_score_threshold`, `max_candidates`, and
  `candidates_truncated`. BrokerProfile now only records active replay/Broker
  constraints and costs.
- Docs updated: `agent_design.md`, `environment_design.md`, and
  `pipeline_design.md` describe the train snapshot alias, configurable Fold
  cadence, and actual Step summary fields. Old “测试季度/历史季度” wording was
  replaced with period-neutral language where relevant.

Validation:
- Resource checks before tests: GPUs already occupied by existing workloads; no
  new GPU workload was started. System memory about 421 GiB available.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e tests.unit.test_broker_engine tests.unit.test_tools_flow`
  passed, 66 tests.
- CLI/schedule smoke checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  passed; the Fold period boundary unit test passed.
- Full test suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 210 tests.
- Post-test resource check: about 422 GiB available RAM; no new GPU workload.
- `git diff --check` clean.
- Cache scan empty after removing test-created `__pycache__` directories.

## 2026-06-24 - Runner context compact layer

Task: reference `external_references/claude-code-main` and implement a compact
layer so long Agent sessions can use DeepSeek V4 Flash to compress context
before expensive main-model calls.

Repository/path checks:
- Logical cwd:
  `/home/coder/projects/adm-cube-l20-8884/macroquant-1741651ef8a3`
- Physical cwd confirmed by `pwd -P`: `/Data/lzp/MacroQuant`
- Local Python: `/home/lzp/miniconda3/envs/quant/bin/python`

Reference inspection:
- Reviewed Claude Code context/token files under
  `external_references/claude-code-main/src/`, especially
  `services/compact/autoCompact.ts`, `services/compact/compact.ts`,
  `services/compact/prompt.ts`, `services/compact/microCompact.ts`,
  `utils/tokens.ts`, and `utils/analyzeContext.ts`.
- Adopted the relevant design idea rather than the full implementation:
  measure context by token estimate, reserve output/main-call budget, keep
  recent raw messages, use failure circuit breakers, and record compact
  boundaries/events.

Implementation:
- Added `src/hl_trader/agent/compact.py`:
  - `ContextCompactionConfig` with token threshold, minimum messages,
    preserved recent messages, max summary tokens, max failures, max calls,
    timeout and reserved remaining time.
  - `ContextCompactor` that calls a dedicated `LLMProxy` in JSON mode, asks
    for a structured continuation state, replaces older messages with
    `context_compaction`, and returns a trace event payload.
  - Conservative rough token estimation and compact-message helpers.
  - Error summary redaction for bearer/authorization-style tokens before
    trace emission.
- Updated `AgentSessionRunner`:
  - Optional `compact_proxy`.
  - Runs compaction before main LLM calls when the estimated context exceeds
    threshold.
  - Recomputes remaining deadline after compaction and does not start a main
    call if the deadline has expired.
  - Keeps the latest LLM compact summary when deterministic message-count
    trimming later fires.
  - Session summary now reports `context_compactions` and
    `context_compaction_calls`.
- Updated `scripts/experiments/run_experiment.py`:
  - Defaults context compact to `deepseek-v4-flash` with thinking disabled.
  - Added CLI flags:
    `--compact-model`, `--disable-context-compact`,
    `--compact-token-threshold`, `--compact-keep-recent-messages`,
    `--compact-max-tokens`, and `--compact-max-calls`.
- Updated `ScriptedLLM` test proxy to record `max_tokens` for compact tests.

SubAgent audit:
- Spawned best-available read-only SubAgents for iterative code audit and
  closed each one after completion.
- First-round findings:
  - High: stale remaining time after compact could allow a main LLM call after
    the Fold deadline.
  - Medium: compact calls needed a separate cap and explicit accounting.
  - Medium: failure/circuit paths needed test coverage.
  - Low: raw provider exception text should be defensively redacted.
- First-round fixes:
  - Compact timeout now uses only `remaining_seconds - min_remaining_seconds`;
    Runner recomputes deadline immediately after compact.
  - `max_calls` limits compact provider calls per session; session summary
    exposes attempted compact calls separately from main `llm_calls`.
  - Added tests for deadline recomputation, time reservation, failure trace,
    failure circuit, and preserving LLM compact summaries during deterministic
    trim.
  - Added bearer/authorization redaction in compact error summaries.
- Second-round findings and fixes:
  - `max_calls=0` previously disabled the limit; it now means zero compact
    provider calls, and a direct regression test covers this.
  - Main LLM provider error trace now uses the same bearer/authorization
    redaction as compact errors, with a regression test.
- Third-round audit found no blocking or obvious remaining compact-layer
  issues.

Documentation:
- `docs/agent_design.md`: compact is an internal Runner continuation
  mechanism, not a new data permission or new conversation boundary.
- `docs/environment_design.md`: Runner responsibility, LLM boundary, trace
  fields and deadline/failure behavior now include context compact.
- `docs/pipeline_design.md`: `max_llm_calls` is the main Agent action budget;
  compact has an independent low-cost model, call cap and trace event while
  sharing the same Fold deadline.

Validation:
- Pre-test resources:
  - `free -h`: about 221 GiB available RAM.
  - `nvidia-smi`: GPUs were already occupied by external Python workloads; no
    new GPU workload was started.
- Targeted Runner tests:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest -v`
  passed, 12 tests.
- Syntax checks:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/compact.py src/hl_trader/agent/runner.py src/hl_trader/agent/__init__.py src/hl_trader/environment/llm/proxy.py scripts/experiments/run_experiment.py tests/unit/test_tools_flow.py`
  passed.
- CLI smoke:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  passed and shows compact flags.
- Pipeline regression:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e -v`
  passed, 16 tests.
- `git diff --check` passed.
- Commit-surface cache scan excluding `.runtime`, `data`, `results`, `wandb`,
  `external_references`, and `experiments` returned empty.
- Post-test resources:
  - `free -h`: about 221 GiB available RAM.
  - `nvidia-smi`: GPU load remained external to this run.

## 2026-06-23 - Configurable snapshot preparation windows and final audits

Task: make the data preparation window configurable from each Experiment Config,
then run one code SubAgent audit and one documentation SubAgent audit, fixing
the issues they found.

Repository/path checks:
- Logical cwd:
  `/home/coder/projects/adm-cube-l20-8884/macroquant-1741651ef8a3`
- Physical cwd confirmed by `pwd -P`: `/Data/lzp/MacroQuant`
- Local Python: `/home/lzp/miniconda3/envs/quant/bin/python`

Implementation:
- `SnapshotConfig` now carries the experiment-level preparation policy:
  `window_months`, optional per-domain months for daily/fundamentals/events/
  macro/text, and `intraday_trade_days`.
- `build_decision_snapshot` computes and applies per-domain PIT windows. Daily,
  fundamentals, events, macro, text, and intraday inputs no longer share a
  hard-coded default window unless the config intentionally leaves them to the
  unified fallback.
- Snapshot manifests record the effective `window_config` and `domain_windows`;
  run manifests record `snapshot_config` for both Fold and held-out runs.
- `ExperimentConfig` owns `snapshot_config`; `run_experiment.py` exposes
  `--window-months`, per-domain window flags, and `--intraday-trade-days`.

SubAgent audits:
- Code audit SubAgent completed and was closed.
- Documentation audit SubAgent completed and was closed.
- Follow-up fixes:
  - Local shell path guard blocks direct reads of host
    `runtime/snapshot_views`; Agent-visible snapshot access must go through the
    mounted `/mnt/snapshot` contract.
  - Day-level Fold schedules use SSE trading days, including previous trading
    day validation periods.
  - Non-quarter CLI runs must provide generic period arguments explicitly,
    avoiding accidental inheritance of quarter defaults.
  - Required fundamental datasets fail fast per configured dataset, not only
    when all datasets are absent.
  - The strategy runtime proxy updates its optimistic same-bar position view
    for weight-based orders as well as share-based orders.
  - Prompt/docs wording now matches code: Step tree is visible in the rendered
    prompt snapshot, `amount` is 100 shares / 1 lot, Broker API docs separate
    strategy `ctx.broker` from host-side `SimBroker`, and data documentation
    uses stable ledger rules rather than migration notes.

Validation:
- Pre-test resources:
  - `free -h`: about 377 GiB available RAM.
  - `nvidia-smi`: GPUs already occupied by existing Python workloads; no new
    GPU workload was started.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  wrote `configs/prompts/PROMPTS.md`.
- Targeted regression suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_snapshot_builder tests.unit.test_pipeline_e2e tests.unit.test_tools_flow tests.unit.test_sandbox_isolation tests.unit.test_broker_engine`
  passed, 96 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 214 tests.
- CLI smoke checks:
  `run_experiment.py --help` passed; `--fold-period month` without explicit
  generic periods exits with the expected parser error.
- Post-test resources:
  - `free -h`: about 352 GiB available RAM.
  - `nvidia-smi`: existing GPU workloads remained external to this run.
- `git diff --check` clean.
- Cache scan for `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
  `*.pyc`, and `*.pyo` returned empty.

## 2026-06-23 - One-Epoch smoke run with configurable data windows

Task: configure and run one Epoch to inspect the new configurable data-window
flow.

Experiment configuration:
- Fold cadence: `month`
- Development Fold: `2022-01`
- Held-out: `2022-02`
- Epochs: `1`
- Snapshot windows:
  - daily: 6 months
  - fundamentals: 12 months
  - events: 6 months
  - macro: 24 months
  - text: 3 months
  - intraday: 2 recent trading days
- Docker sandbox used for the successful controlled run.

Real-LLM attempts:
- `epoch_smoke_20260623_0315`: local-dev run exposed that local executor does
  not provide real `/mnt/...` mounts; this should remain a development-only
  mode.
- `epoch_smoke_20260623_0325`: Docker run reached validation backtest but the
  template produced no candidates/trades. With default `min_return=0.0`, the
  strict acceptance rule rejected total_return `0.0`.
- `epoch_smoke_20260623_0341` and `epoch_smoke_20260623_0400`: `deepseek-v4-flash`
  repeatedly emitted the same shell heredoc in the Fold session until
  `deadline_timeout`; no successful complete validation backtest was produced.

Fixes made during the run:
- `sandbox_shell_tool` no longer treats fd-duplication redirection like
  `2>&1` as a write-to-file redirect. This preserves rejection of real write
  redirections while allowing common read-only diagnostic commands such as
  `ls /mnt/snapshot 2>&1`.
- `run_experiment.py` now exposes acceptance thresholds:
  `--min-return`, `--min-sharpe`, `--max-drawdown`, and
  `--allow-incomplete-validation`. Defaults are unchanged.
- `pipeline_design.md` documents that CLI threshold overrides are for explicit
  experiments/smoke runs; production should retain strict acceptance.

Successful controlled run:
- Experiment: `epoch_smoke_scripted_20260623_0419`
- LLM: `ScriptedLLM` with actions `done`, `modification_check`, `backtest`,
  `finish_fold`
- Result: `{"status": "ok", "final_strategy_artifact":
  "strategy_epoch_001_fold_202201", "heldout_runs": 1}`
- Fold `fold_202201`:
  - `fold_status`: `frozen`
  - selected step: `step_001`
  - validation: total_return `0.0`, sharpe `0.0`, max_drawdown `0.0`,
    order_count `0`
  - frozen test: total_return `0.0`, sharpe `0.0`, max_drawdown `0.0`,
    order_count `0`
- Held-out `heldout_202202`:
  - total_return `0.0`, sharpe `0.0`, max_drawdown `0.0`, order_count `0`
- Interpretation: the configurable snapshot/Pipeline/Broker/freeze/held-out
  path runs end-to-end. The starting template intentionally produces no
  candidates or trades, so this is a process smoke result, not a profitable
  strategy result.

Validation:
- Resource checks were run before and after long runs. Available RAM stayed
  safely above 300 GiB; GPU usage shown by `nvidia-smi` came from existing
  external Python workloads, and no new GPU workload was started.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ShellToolTest tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation`
  passed, 38 tests.
- `git diff --check` clean.
- Commit-surface cache scan excluding `.runtime`, `data`, `results`, `wandb`,
  and `external_references` returned empty.

## 2026-06-24 - Model artifacts contract

Task: add a separate `model_artifacts` design so Agent code can persist model
parameters outside `output/`, while still allowing training logic to live in
`main.py` and rerun during backtests.

Implementation:
- Added `SandboxPaths.model_artifacts` at `/mnt/agent/model_artifacts` and
  `SandboxPaths.parent_model_artifacts` at
  `/mnt/artifacts/parent_model_artifacts`.
- `LocalSandbox.prepare_layout()` creates the new directories; install/fallback
  paths copy parent model artifacts into both the trusted parent baseline and
  the Agent-writable working directory; lock/unlock applies to both `output/`
  and `model_artifacts/`.
- `artifacts.py` now has separate model-artifact validation and hashing:
  flat regular files only, no hidden files, no symlinks, no runtime caches, no
  directories, no unsupported suffixes, plus independent file/byte limits.
  `output/` remains flat text/code and still rejects model weights.
- `modification_check_tool` records `artifact_hash`, `model_artifact_hash`,
  `combined_artifact_hash`, strategy delta, and model delta.
- `backtest_tool` exposes `MQ_MODEL_ARTIFACTS_DIR`,
  `context["model_artifacts_dir"]`, `MQ_WORKSPACE_DIR`, and
  `context["workspace_dir"]`; strategy-policy `ctx` exposes
  `ctx.model_artifacts_dir` and `ctx.workspace_dir`. Backtest summaries include
  model file count, model bytes, and strategy/model/combined hashes.
- `finish_fold_tool`, Pipeline acceptance, fallback, frozen eval, held-out,
  and freeze manifests now require strategy hash and model hash consistency.
- Frozen strategy code remains in
  `strategy_artifacts/<epoch>/<strategy_artifact_id>/`; frozen model parameters
  are stored separately as
  `strategy_artifacts/<epoch>/<strategy_artifact_id>.model_artifacts/`.
- StepTree stores `model_artifact_hash`, `combined_artifact_hash`, and copies
  successful Step model artifacts under each node's `model_artifacts/` subdir.
- Structured search and shell guards allow the `model_artifacts` root while
  preserving read-only/test-data restrictions.

Documentation and prompts:
- Updated `docs/agent_design.md`, `docs/environment_design.md`, and
  `docs/pipeline_design.md`.
- Updated `src/hl_trader/agent/prompts.py` and regenerated
  `configs/prompts/PROMPTS.md`.
- Updated `configs/agent_output_template/README.md` and `main.py` to describe
  stored-parameter and retrain-each-backtest modes.

Validation:
- Pre-test resources:
  - `free -h`: about 222 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Targeted regression suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_step_tree tests.unit.test_sandbox_isolation`
  passed, 87 tests.
- Finish-fold mutation guard:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow`
  passed, 37 tests after adding the contract-check artifact mutation rejection.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 226 tests.
- `git diff --check` clean.
- Cache scan for `__pycache__` under `src`, `tests`, and
  `configs/agent_output_template` returned empty.
- Post-test resources:
  - `free -h`: about 215 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Runtime env artifact and experiment parameter visibility

Task: add Prompt-visible Python/package environment information and make the
experiment parameters needed by Fold and Meta Learning Agents available through
machine-readable run artifacts.

Implementation:
- Added `/mnt/artifacts/runtime_env.json` to the Sandbox artifact contract and
  collection list. `LocalSandbox.prepare_layout()` writes a local Python probe;
  `ExperimentPipeline._start_sandbox()` rewrites it as a Dockerfile-based
  contract for real Docker runs.
- `runtime_env.json` records:
  - Python mode/version/executable source;
  - key packages (`numpy`, `pandas`, `pyarrow`, `duckdb`, `scikit-learn`,
    `statsmodels`, `torch`);
  - `rg` availability;
  - network and package-install policy;
  - Sandbox resource spec for Docker runs.
- Ordinary Fold run manifests now include
  `runtime_env_ref=/mnt/artifacts/runtime_env.json`.
- Meta-learning run manifests now include `runtime_env_ref` and
  `experiment_parameters`, covering Fold period, development/held-out periods,
  snapshot windows, acceptance rules, Broker profile, NL failure policy, Step
  tree settings, deadline, max steps, finalization window, call timeout, and
  Sandbox spec.
- The formal CLI (`scripts/experiments/run_experiment.py`) writes
  `agent_session_config` and a sanitized `llm_config_summary` into each run
  manifest so context compact thresholds, max calls, model names, reasoning
  effort, and timeouts are auditable without exposing API keys.
- Fold and Meta Learning prompts now instruct Agents to read
  `run_manifest.json` and `runtime_env.json` before coding or forming Taste.
  The prompts explicitly say not to assume unlisted packages are installed and
  not to install packages during a Fold.
- Structured search roots now include `parent_models`, matching the documented
  readable path table. Prompt exports were regenerated.
- Updated `docs/agent_design.md`, `docs/environment_design.md`, and
  `docs/pipeline_design.md` to describe the new environment and parameter
  facts sources.

Validation:
- Pre-run resource checks:
  - `free -h`: about 272 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this task
    started no GPU work.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/runtime.py src/hl_trader/environment/sandbox.py src/hl_trader/pipelines/experiment.py src/hl_trader/agent/prompts.py src/hl_trader/environment/tools/search.py scripts/experiments/run_experiment.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py tests/unit/test_tools_flow.py tests/unit/test_step_tree.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e tests.unit.test_tools_flow tests.unit.test_step_tree -v`
  passed, 87 tests.
- CLI check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  passed.
- `git diff --check` over the touched code/docs/tests/prompt files passed.
- Cache scan initially found Python cache directories generated by the test
  run; `src/`, `tests/`, and `scripts/` caches were removed, and the follow-up
  cache scan returned empty.
- Post-run resources:
  - `free -h`: about 275 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning directive interface and max effort defaults

Task: ensure future Agent/NL calls use max reasoning effort by default and add
an experiment-level interface for injecting a user-provided exploration
direction into the Epoch-start meta-learning prompt.

Implementation:
- `scripts/experiments/run_experiment.py` defaults the main Agent and NL
  `DeepSeekProxy.from_env()` calls to `thinking_enabled=True` and
  `reasoning_effort=max`. The CLI keeps `--reasoning-effort` and
  `--no-thinking` as explicit ablation/debug overrides. Compact remains
  `thinking_enabled=False`, so it does not receive reasoning effort.
- Added `ExperimentConfig.meta_learning_directive`.
- `ExperimentPipeline.run_meta_learning()` writes
  `meta_learning_directive` to the meta-learning run manifest and the
  `meta_learning` ledger record.
- `AgentSessionRunner` accepts `meta_learning_directive` and passes it to
  `build_meta_learning_prompt()`.
- `build_meta_learning_prompt(experiment_directive=...)` appends a dedicated
  `# 实验级探索方向（用户注入）` section. The prompt tells the Agent to treat the
  injected direction as a hypothesis to test or refine, not a verified result,
  while preserving PIT, search, NL-risk and anti-overfit constraints.
- CLI injection can use either `--meta-learning-directive "..."` or
  `--meta-learning-directive-file path/to/directive.txt`; passing both is an
  argument error.
- `scripts/dev/export_prompts.py` now includes an audit example of this
  injection section in `configs/prompts/PROMPTS.md`.
- Updated `docs/agent_design.md`, `docs/environment_design.md`, and
  `docs/pipeline_design.md`.

Validation:
- Real path confirmed: `/Data/lzp/MacroQuant`.
- Resource check after tests:
  - `free -h`: about 274 GiB available RAM.
  - GPUs remained occupied by external workloads; no new GPU training job was
    started.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Syntax:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py tests/unit/test_llm_deepseek.py`
  passed.
- CLI:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  passed and shows `--reasoning-effort`, `--no-thinking`,
  `--meta-learning-directive`, and `--meta-learning-directive-file`.
- Tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_llm_deepseek tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e`
  passed, 58 tests.
- Grep confirmed the injected prompt section in `PROMPTS.md`.
- `git diff --check` clean for touched code, tests, docs, and prompt export.

## 2026-06-24 - DeepSeek max reasoning and meta-learning rerun

Task: set DeepSeek `reasoning_effort` to `max`, add a general prompt
instruction encouraging deeper reasoning before decisions, then rerun one real
Docker meta-learning Fold with the same audit configuration.

Implementation:
- Updated `src/hl_trader/environment/llm/proxy.py` so
  `DeepSeekProxy.from_env()` defaults `reasoning_effort` to `max` whenever
  `thinking_enabled=True`.
- Kept compact calls unchanged: compact uses `thinking_enabled=False`, so
  `reasoning_effort` is omitted.
- Added prompt text to both Fold Agent and Meta Learning prompts requiring
  reasoning over mechanism hypotheses, visible data, execution constraints,
  falsification paths, and failure modes while keeping the final action/Taste
  concise.
- Regenerated `configs/prompts/PROMPTS.md`.
- Updated `docs/agent_design.md` and `docs/environment_design.md`.
- Added `test_proxy_defaults_thinking_reasoning_effort_to_max`.

Validation before rerun:
- Real path: `/Data/lzp/MacroQuant`.
- Resource checks:
  - `free -h`: about 274 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; no local
    model training was launched.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/llm/proxy.py src/hl_trader/agent/prompts.py tests/unit/test_llm_deepseek.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_llm_deepseek tests.unit.test_sandbox_isolation`
  passed, 38 tests.
- Prompt grep confirmed `reasoning_effort=max` and the new deep-thinking text
  in source docs and exported `PROMPTS.md`.

Meta-learning rerun:
- Experiment ID: `meta_learning_audit_20260624_171700`.
- Invocation: direct `ExperimentPipeline.run_meta_learning()` call, so this
  was meta-learning-only and did not run ordinary Fold replay or held-out.
- Sandbox: real Docker, image `macroquant-sandbox:latest`
  (`sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`).
- Configuration: `deepseek-v4-pro` main Agent, `deepseek-v4-flash` NL,
  `deepseek-v4-flash` compact, quarterly WF, `window_months=21`,
  `intraday_trade_days=5`, Fold deadline 60 minutes, compact threshold 200k.
- Run log: `logs/meta_learning_audit_20260624_171700.log`.
- Log config line confirmed:
  - main: `thinking_enabled=true`, `reasoning_effort=max`;
  - NL: `thinking_enabled=true`, `reasoning_effort=max`;
  - compact: `thinking_enabled=false`, `reasoning_effort=null`.

Key paths:
- Ledger:
  `experiments/meta_learning_audit_20260624_171700/ledgers/experiment_ledger.jsonl`.
- Taste:
  `experiments/meta_learning_audit_20260624_171700/meta_learning/epoch_001/taste.md`.
- Canonical trace:
  `experiments/meta_learning_audit_20260624_171700/artifacts/run_877f23366817/agent_trace.jsonl`.
- Run manifest:
  `experiments/meta_learning_audit_20260624_171700/artifacts/run_877f23366817/run_manifest.json`.
- Runtime sandbox:
  `.runtime/sandboxes/run_877f23366817/`.

Result:
- Run ID: `run_877f23366817`.
- Fold ID: `epoch_001_meta_learning`.
- Status: `taste_only`.
- Taste length: 2951 chars.
- Modification check: passed; formal output/model artifacts had zero changed
  files.
- Docker runtime: container `mqsbx_45cae967363f`, allocated GPU `[3]`.
- One initial shell action using `2>/dev/null` was rejected by sandbox path
  checks; the Agent recovered with structured glob/grep actions.

Trace summary:
- Event counts:
  `llm_call=18`, `glob=2`, `grep=8`, `web_search=4`, `shell=1`,
  `tool=1`, `session_end=1`.
- Web search:
  - `finance_quant_econ`: 2 successful searches.
  - `natural_science_engineering`: 1 successful search.
  - `philosophy_methodology`: 1 successful search.
  - Engine usage: `semantic_scholar=4`; no 429 or retry-visible failures.
- Provider usage from trace:
  prompt tokens 191,728; completion tokens 8,359; total tokens 200,087;
  reasoning tokens 7,047; prompt cache hit tokens 168,320; prompt cache miss
  tokens 23,408.
- Context compact did not trigger:
  `context_compactions=0`, `context_compaction_calls=0`.
- Session ended with `finish_status=meta_learning_done`.

Post-run resources:
- `free -h`: about 275 GiB available RAM.
- GPU usage returned to pre-existing external workloads after the container
  stopped.

## 2026-06-24 - Meta Learning Docker audit rerun

Task: rerun the meta-learning Fold with the same real-sandbox configuration as
the previous audit run, after the compact threshold and meta-learning prompt
updates.

Configuration:
- Experiment ID: `meta_learning_audit_20260624_160835`.
- Invocation: direct `ExperimentPipeline.run_meta_learning()` call, so this
  was a meta-learning-only run and did not continue into normal Fold replay or
  held-out evaluation.
- Sandbox: real Docker, `macroquant-sandbox:latest`.
- Agent model: `deepseek-v4-pro`.
- Compact model: `deepseek-v4-flash`, thinking disabled.
- Web Search engines exposed to the Agent: `tavily`, `semantic_scholar`.
- Fold period/default windows: quarterly WF, `window_months=21`,
  `intraday_trade_days=5`.
- Fold deadline: 60 minutes.
- Context compaction: enabled, estimated trigger threshold 200,000 tokens,
  keep recent messages 12, max compaction calls 8.

Pre-run checks:
- Real path confirmed earlier by `pwd -P`: `/Data/lzp/MacroQuant`.
- `.env` was loaded for the run; `DEEPSEEK_API_KEY`, `TAVILY_API_KEY`, and
  `SEMANTIC_SCHOLAR_API_KEY` were present. Secret values were not printed.
- Docker image check:
  `docker image inspect macroquant-sandbox:latest --format '{{.Id}}'`
  returned image id `sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`.
- `free -h` before the run: about 274 GiB available RAM.
- `nvidia-smi` before the run: all L20 GPUs had existing external workloads;
  the meta-learning run itself did not launch local training.

Run command summary:
- Used `/home/lzp/miniconda3/envs/quant/bin/python` with
  `PYTHONDONTWRITEBYTECODE=1`.
- Constructed `ExperimentConfig` with `use_docker=True`,
  `first_test_period=2022Q1`, `last_test_period=2025Q4`,
  `heldout_first_period=2026Q1`, `heldout_last_period=2026Q1`,
  `fold_period=quarter`, `window_months=21`, `max_fold_minutes=60`,
  and `SnapshotConfig(window_months=21, intraday_trade_days=5)`.
- Called `pipeline.run_meta_learning(epoch_id="epoch_001", parent=None,
  previous_taste="")`.
- Host log:
  `logs/meta_learning_audit_20260624_160835.log`.

Key paths:
- Experiment ledger:
  `experiments/meta_learning_audit_20260624_160835/ledgers/experiment_ledger.jsonl`.
- Meta output:
  `experiments/meta_learning_audit_20260624_160835/meta_learning/epoch_001/taste.md`.
- Canonical trace:
  `experiments/meta_learning_audit_20260624_160835/artifacts/run_ba28c68398b5/agent_trace.jsonl`.
- Run manifest:
  `experiments/meta_learning_audit_20260624_160835/artifacts/run_ba28c68398b5/run_manifest.json`.
- Runtime sandbox:
  `.runtime/sandboxes/run_ba28c68398b5/`.

Result:
- Run ID: `run_ba28c68398b5`.
- Fold ID: `epoch_001_meta_learning`.
- Status: `taste_only`.
- Taste length: 2514 characters.
- Modification check: passed; no formal strategy/model changes.
- Docker runtime from manifest:
  container `mqsbx_9e6110b1c00d`, image `macroquant-sandbox:latest`,
  allocated GPU indices `[1]`.

Trace summary:
- Trace lines: 33.
- Event counts:
  `llm_call=17`, `shell=11`, `web_search=2`, `tool=2`, `session_end=1`.
- LLM model: `deepseek-v4-pro` for all 17 main-conversation calls.
- Provider usage:
  prompt tokens 117,045; completion tokens 8,117; total tokens 125,162;
  prompt cache hit tokens 103,296; prompt cache miss tokens 13,749;
  reasoning tokens 6,355.
- Web search calls:
  - Tavily query `meta-learning automated exploration of trading strategies natural language processing`, 5 results.
  - Tavily query `minute level trading strategies Chinese A-shares natural language processing`, 5 results.
- Note: both Tavily and Semantic Scholar were exposed in the manifest, but this
  Agent run chose Tavily for both searches. This is useful to audit whether the
  current prompt should hard-require multi-engine or multi-perspective search
  instead of merely encouraging it.
- Context compaction did not trigger:
  `context_compactions=0`, `context_compaction_calls=0`.

Post-run checks:
- `free -h` after the run: about 275 GiB available RAM.
- `nvidia-smi` after the run: GPU usage remained dominated by existing
  external workloads.
- `find experiments/meta_learning_audit_20260624_160835/meta_learning -type f`
  shows only `epoch_001/taste.md`; the canonical trace is not duplicated under
  `meta_learning/`.
- `git diff --check -- LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md` passed.
- `docker ps -a --filter name=mqsbx_9e6110b1c00d` returned no container after
  the run, consistent with sandbox cleanup.

Follow-up trace audit artifact:
- Replaced `check.md` with a dialogue-style summary of the canonical trace
  `experiments/meta_learning_audit_20260624_160835/artifacts/run_ba28c68398b5/agent_trace.jsonl`.
- The summary covers the system/user setup, each Agent action and Environment
  observation, the first-epoch empty-history state, output/model/template reads,
  two successful Tavily searches, one Semantic Scholar HTTP 429, the written
  Taste, the modification check, session end, and audit concerns.

## 2026-06-24 - Meta Learning search hardening and NL risk prompt

Task: add query-API rate limiting and retry, require meta-learning to search
from three research perspectives, and adjust prompts so NL analysis is treated
as useful but potentially leaky/noisy.

Implementation:
- `src/hl_trader/environment/web_search.py`
  - Added retry handling for retryable HTTP statuses: 429, 500, 502, 503, 504.
  - Tavily now retries transient HTTP failures with bounded exponential backoff.
  - Semantic Scholar now uses a lighter default field set and a per-key shared
    file-lock rate limiter under `.runtime/api_rate_limits` by default.
  - Semantic Scholar default interval is 1.25 seconds, leaving margin around
    the provider's introductory 1 RPS limit.
  - Provider error messages continue to redact API keys and include the attempt
    count.
- `src/hl_trader/agent/runner.py`
  - Added required `web_search.perspective` choices:
    `finance_quant_econ`, `natural_science_engineering`,
    `philosophy_methodology`.
  - Meta-learning `done` now requires at least one successful search for every
    configured perspective when web search providers are enabled.
  - `engine` remains independently selected by the Agent; `perspective` is only
    a research-lens audit field, not a provider/category selector.
  - Added a Taste guard that rejects workspace `taste.md` if it contains
    template example-function prefixes or becomes too detailed.
- `src/hl_trader/agent/prompts.py`
  - Meta-learning prompt now states the three-perspective search requirement as
    a hard condition, tells the Agent to retry or switch engines after provider
    failures, and keeps Taste direction-level rather than implementation-level.
  - Fold prompt now warns that NL can suffer from timestamp/ingestion ambiguity,
    retrieval bias, model prior knowledge leakage, free-text parsing instability,
    and look-ahead risk.
- Updated `configs/prompts/PROMPTS.md`, `docs/agent_design.md`,
  `docs/environment_design.md`, and `docs/pipeline_design.md`.

Validation:
- Pre-run resources:
  - `free -h`: about 275 GiB available RAM.
  - `nvidia-smi`: GPU usage belonged to existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Compile check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/web_search.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e`
  passed, 39 tests.
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow`
  passed, 39 tests.
- `git diff --check` over the touched code/docs/prompt/log files passed.
- Post-run resources:
  - `free -h`: about 275 GiB available RAM.
  - GPU usage remained external.

## 2026-06-24 - Meta Learning three-perspective search prompt

Task: update the Meta Learning system prompt so the Agent is encouraged to
perform multi-round searches from finance/quant/economics, other natural
science/engineering, and philosophy/methodology perspectives, then converge on
one innovative but practical exploration direction that fits the current
experiment period and trading frequency.

Implementation:
- Extended `META_LEARNING_INSTRUCTION` under `# 联网检索`:
  - encourages multi-round searches around the same candidate exploration
    question;
  - asks for cross-checking from finance/quant/economics, natural
    science/engineering, and philosophy/methodology;
  - states that the goal is not to list sources, but to find an innovative and
    practically useful exploration direction.
- Extended Taste requirements so the Agent must explain why the direction fits
  the current run manifest settings, including Fold period, data window,
  daily/minute trading frequency, long/short ability, replay costs, and
  validation metrics.
- Updated Pipeline and Agent docs with the same concise contract.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Pre-test resources:
  - `free -h`: 503 GiB total, 277 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py scripts/dev/export_prompts.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e`
  passed, 37 tests.
- Grep verification:
  confirmed the new finance/quant/economics, natural science/engineering,
  philosophy/methodology, Fold-period, trading-frequency, and practical
  innovation wording is present in source prompt, exported prompt, and living
  docs.
- Diff check:
  `git diff --check -- src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md docs/agent_design.md docs/pipeline_design.md`
  passed.
- Post-test resources:
  - `free -h`: 503 GiB total, 277 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Context compact threshold raised to 200k

Task: raise the default Runner semantic context compact threshold from the
initial conservative 50k estimate to 200k estimated tokens.

Rationale:
- Claude Code's auto-compact logic is based on the model context window minus
  output reservation and a safety buffer, not a fixed 50k threshold.
- DeepSeek V4 Pro/Flash expose a 1M-token context window, so a 50k threshold
  is too early for normal Agent sessions and can discard useful raw context
  prematurely.
- 200k is a conservative first step: close to Claude Code's 200k-context
  effective auto-compact range while avoiding very long 1M-context latency and
  attention-risk behavior.

Implementation:
- Changed `ContextCompactionConfig.token_threshold` default from `50_000` to
  `200_000`.
- Changed `scripts/experiments/run_experiment.py --compact-token-threshold`
  default from `50_000` to `200_000`, and made the CLI help state the default.
- Updated Agent and Environment docs to state the default compact trigger is
  an estimated 200,000 tokens.

Validation:
- Pre-test resources:
  - `free -h`: 503 GiB total, 277 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/compact.py scripts/experiments/run_experiment.py`
  passed.
- CLI/default checks:
  `/home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  shows `--compact-token-threshold` with `default 200000`.
  A direct `ContextCompactionConfig()` assertion returned `200000`.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow`
  passed, 39 tests.
- Diff checks:
  `git diff --check -- src/hl_trader/agent/compact.py scripts/experiments/run_experiment.py docs/environment_design.md docs/agent_design.md`
  passed.
  Search confirmed no `50_000` / `50,000` compact-threshold residual in the
  touched code/docs.
- Post-test resources:
  - `free -h`: 503 GiB total, 275 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning agent trace deduplication

Task: simplify meta-learning trace storage so each run keeps one canonical
`agent_trace.jsonl` instead of duplicating the same file under both
`artifacts/run_<id>/` and `meta_learning/<epoch>/`.

Implementation:
- Removed the copy from collected run artifacts into
  `experiments/<id>/meta_learning/<epoch>/agent_trace.jsonl`.
- Added `agent_trace_ref` to the `meta_learning` ledger record. The ref points
  to the canonical `experiments/<id>/artifacts/<run_id>/agent_trace.jsonl`.
- Rewired `_prior_meta_learning_logs()` to concatenate prior meta-learning logs
  from ledger `agent_trace_ref`; old records without the field are resolved via
  their `run_id` and the canonical artifacts directory.
- Updated pipeline docs so `meta_learning/<epoch>/` is documented as a Taste
  directory only, with trace lookup handled by `agent_trace_ref`.
- Added unit assertions that meta-learning trace refs exist and duplicate
  `meta_learning/<epoch>/agent_trace.jsonl` files are not created.
- Removed the existing duplicate historical copy at
  `experiments/meta_learning_audit_20260624_1458/meta_learning/epoch_001/agent_trace.jsonl`;
  the canonical trace remains under
  `experiments/meta_learning_audit_20260624_1458/artifacts/run_15b5d81f61d0/agent_trace.jsonl`.

Validation:
- Pre-test resources:
  - `free -h`: 503 GiB total, 277 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e`
  passed, 18 tests.
- Diff check:
  `git diff --check -- src/hl_trader/pipelines/experiment.py tests/unit/test_pipeline_e2e.py docs/pipeline_design.md`
  passed.
- Post-test resources:
  - `free -h`: 503 GiB total, 277 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Agent formal artifact directory freedom

Task: clarify and implement the user's intent that Agent artifact-directory
freedom should be general, not a hardcoded embedding/KG-style structure. The
stable contract should keep `output/main.py` as the entrypoint while allowing
richer code organization and larger/multiple model parameters when an
experiment chooses that design.

Implementation:
- Reworked `src/hl_trader/environment/artifacts.py` so strategy and model
  artifacts are validated, hashed, diffed, and copied recursively.
- Kept `output/main.py` as the only required strategy entrypoint and kept model
  parameters separate under `/mnt/agent/models`.
- Continued to reject symlinks, hidden files/directories, runtime caches
  (`__pycache__`, `.pyc`, `.pyo`), unsupported suffixes, and formal strategy
  code references to `/mnt/snapshots/`, `/mnt/runtime/`, or `/mnt/artifacts`.
- Increased default `ModificationConstraints` to a controlled project scale:
  64 strategy files, 1 MB strategy bytes, 64 model artifact files, and 1 GiB
  model artifact bytes. These remain experiment-configurable.
- Updated artifact tests to prove legal nested helpers/model files are accepted
  while unsupported files, hidden dirs, runtime caches, symlinks, and forbidden
  stage-dir references are rejected.
- Updated `configs/agent_output_template/README.md`, Fold Agent prompt source,
  exported `configs/prompts/PROMPTS.md`, `docs/agent_design.md`,
  `docs/environment_design.md`, `docs/pipeline_design.md`, and relevant code
  docstrings.
- Follow-up naming cleanup: current prompts/docs/code comments now call
  `/mnt/agent/output` the formal strategy artifact directory and
  `/mnt/agent/models` the inheritable model artifact directory, avoiding
  confusion with the separate Step artifact tree lineage feature.

Validation:
- Pre-test resources:
  - `free -h`: about 276-278 GiB available RAM.
  - `nvidia-smi`: GPUs were heavily occupied by existing external workloads;
    this run started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_tools_flow tests.unit.test_step_tree tests.unit.test_sandbox_isolation`
  passed, 73 tests.
- Static checks:
  `rg` over docs/prompts/source/tests found no remaining old flat/single-layer
  artifact constraints.
  Follow-up `rg` found no current prompt/source/living-doc references to
  `output/models` as artifact trees; remaining tree references are Step Tree or
  historical detailed-log entries.
  `git diff --check` passed.
- Cache scan:
  source/test/config paths had no new Python cache output from the test run.
  Existing ignored experiment artifacts under `experiments/audit_cli/.../.local`
  still contain dependency caches; they are ignored by `.gitignore:/experiments/`
  and were not removed in this task.
- Post-test resources:
  - `free -h`: about 437 GiB available RAM after the final targeted test rerun.
  - GPU usage remained external to this run; several previously busy GPUs were
    freed by unrelated external jobs by the final check.

## 2026-06-24 - Strategy entrypoint narrowed to run_strategy

Task: remove the ambiguous dual entrypoint contract from `output/main.py`.

Implementation:
- `load_strategy_artifact()` now requires `run_strategy(context)` and rejects
  `main(context)`-only strategy files.
- The backtest strategy driver now calls only `module.run_strategy(context)`;
  the `main(context)` fallback and error text were removed.
- Removed the template `main(context)` forwarding wrapper from
  `configs/agent_output_template/main.py`.
- Updated Fold Agent prompt source, exported `configs/prompts/PROMPTS.md`,
  `docs/agent_design.md`, and tests.

Validation:
- Resource checks before validation:
  - `free -h`: about 332 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this run
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_tools_flow tests.unit.test_broker_engine`
  passed, 72 tests.
- Static checks:
  Entry-contract search found no current prompt/docs/source references that
  allow `main(context)` as a valid strategy entrypoint; the only remaining
  `def main(context)` match is the regression test proving it is rejected.
  `git diff --check` passed.

## 2026-06-24 - Real Docker meta-learning audit run

Task: configure a real meta-learning Fold for audit with quarterly WF defaults,
21-month history windows, 5 visible intraday trading days, host-side web search,
and Claude-Code-inspired context compaction.

Configuration:
- Experiment ID: `meta_learning_audit_20260624_1458`.
- Invocation: direct `ExperimentPipeline.run_meta_learning()` call to avoid
  running the subsequent normal Fold and held-out evaluation.
- Sandbox: real Docker, `macroquant-sandbox:latest`.
- Agent model: `deepseek-v4-pro`.
- Compact model: `deepseek-v4-flash`, thinking disabled.
- Web Search engines: `tavily`, `semantic_scholar`.
- Snapshot config used for the experiment object: `window_months=21`,
  `intraday_trade_days=5`; fold period set to `quarter`.
- Note: this was a meta-learning-only run, so no train/valid/test snapshot was
  built. The configured data-window defaults are recorded in the local run log;
  a normal Fold run will record `snapshot_config` in its fold manifest.

Key paths:
- Run log: `logs/meta_learning_audit_20260624_1458.log`.
- Experiment ledger:
  `experiments/meta_learning_audit_20260624_1458/ledgers/experiment_ledger.jsonl`.
- Meta output:
  `experiments/meta_learning_audit_20260624_1458/meta_learning/epoch_001/taste.md`.
- Meta trace:
  `experiments/meta_learning_audit_20260624_1458/meta_learning/epoch_001/agent_trace.jsonl`.
- Collected run artifact:
  `experiments/meta_learning_audit_20260624_1458/artifacts/run_15b5d81f61d0/`.
- Runtime sandbox:
  `.runtime/sandboxes/run_15b5d81f61d0/`.

Result:
- Run ID: `run_15b5d81f61d0`.
- Fold ID: `epoch_001_meta_learning`.
- Status: `taste_only`.
- Taste length: 3132 characters.
- Modification check: passed; no formal strategy/model changes.
- Docker runtime from manifest:
  container `mqsbx_b9df49936564`, image `macroquant-sandbox:latest`,
  allocated GPU indices `[1]`.

Trace summary:
- `llm_call`: 13, all main conversation calls to `deepseek-v4-pro`.
- `web_search`: 2:
  - Tavily query `A-share stock selection quantitative strategy multi-factor model 2025`, 5 results.
  - Semantic Scholar query `meta learning for financial time series alpha discovery`, 5 results.
- `shell`: 7, `glob`: 1, `modification_check_tool`: 1.
- Total provider usage in trace: 53,032 prompt tokens, 5,083 completion
  tokens, 58,115 total tokens.
- Context compaction was configured but did not trigger because the session was
  short and stayed below the compaction conditions; `session_end` reports
  `context_compactions=0` and `context_compaction_calls=0`.

Validation and resources:
- Pre-run resources:
  - `free -h`: about 277 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this run
    used Docker allocation but no local model training job.
- Key availability checks:
  `DEEPSEEK_API_KEY`, `TAVILY_API_KEY`, and `SEMANTIC_SCHOLAR_API_KEY` were set
  without printing secret values. `macroquant-sandbox:latest` existed.
- Post-run resources:
  - `free -h`: about 276 GiB available RAM.
  - GPU usage remained dominated by external workloads.

## 2026-06-24 - PROMPTS de-duplication and Meta Learning Step Tree visibility

Task: review whether the Meta Learning prompt sections in `PROMPTS.md` are
redundant, and verify whether the Meta Learning Agent can read the structured
`steps` tree.

Implementation:
- Removed the separate `元学习 Web Search Engine 可选值` section from
  `scripts/dev/export_prompts.py`. The exported `PROMPTS.md` now avoids the
  duplicate Web Search Engine example section.
- Kept `# Web Search Engines` and `# development 摘要` inside the full rendered
  Meta Learning prompt example because those are actual Runner-injected
  dynamic sections and should remain visible in an audit snapshot.
- Fixed `ExperimentPipeline.run_meta_learning()` to call `_install_step_tree()`
  when `step_tree_enabled` is true. The Meta Learning sandbox now receives the
  experiment-level `steps/tree.json`, `tree.txt`, and node artifact snapshots,
  with `current_node_id` positioned at the parent artifact node.
- Added `test_meta_learning_can_read_existing_step_tree` to confirm a
  Meta Learning callback can read `ctx.paths.steps/tree.json` and `tree.txt`.

Validation:
- Pre-run resources:
  - `free -h`: about 222 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Targeted pipeline tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_can_read_existing_step_tree tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_two_epochs_do_not_collide_in_step_tree tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_multi_epoch_runs_meta_learning_before_each_epoch`
  passed, 3 tests.
- Pipeline e2e suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e`
  passed, 18 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.

## 2026-06-24 - Fold Agent prompt file-structure table

Task: list the Fold Agent-readable/writable file structure directly in the
Fold Agent prompt.

Implementation:
- Added `# 文件结构和读写边界` to `PROTOCOL_INSTRUCTION`.
- The table now covers:
  `/mnt/agent/workspace/`, `/mnt/agent/output/`, read-only
  `/mnt/agent/output/README.md`, `/mnt/agent/models/`, `/mnt/snapshot/`,
  `/mnt/snapshots/train/`, `/mnt/snapshots/valid/`, forbidden
  `/mnt/snapshots/test/`, `/mnt/artifacts/run_manifest.json`,
  `/mnt/artifacts/parent_output/`, `/mnt/artifacts/parent_models/`,
  `/mnt/artifacts/results/`, `/mnt/artifacts/steps/`,
  `/mnt/artifacts/logs/`, and `/mnt/artifacts/agent_trace.jsonl`.
- The prompt explicitly distinguishes Agent tool visibility from formal
  strategy-code visibility: formal strategy code may only read
  `/mnt/snapshot`, `/mnt/agent/output`, and `/mnt/agent/models`.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Resource checks:
  - before prompt export/tests: about 207 GiB available RAM; GPU usage was from
    existing external workloads.
  - after checks: about 206 GiB available RAM; this work started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_tools_flow tests.unit.test_step_tree`
  passed, 64 tests.
- Prompt grep confirmed the new file-structure table, `历史 Step 记录`, and
  `/mnt/artifacts/logs/` are present in source/exported prompts.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.

## 2026-06-24 - Meta Learning first-epoch wording de-duplication

Task: remove duplicated first-Epoch empty-history wording from the Meta
Learning prompt.

Implementation:
- Removed `若为空，这是第一轮正常情况` from the role paragraph.
- Kept the dedicated `# 首轮空历史处理` section as the single source for
  first-Epoch empty `steps` / ledger / meta-memory behavior.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Prompt grep confirmed the removed sentence no longer appears while
  `# 首轮空历史处理` remains.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_step_tree`
  passed, 25 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.
- Post-run resources:
  - `free -h`: about 222 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning prompt readable-file table

Task: remove the dynamic `Web Search Engines` and `development 摘要` sections
from the Meta Learning prompt, and directly tell the Meta Learning Agent which
files and directories are readable.

Implementation:
- `build_meta_learning_prompt()` now returns only the static
  `META_LEARNING_INSTRUCTION`; it no longer appends `# Web Search Engines` or
  `# development 摘要`.
- The `web_search` action description no longer references a lower
  `Web Search Engines` section. The static protocol already names supported
  engines, while the Runner schema still enforces actually configured choices.
- The first role paragraph now explicitly instructs the Meta Learning Agent to
  read `/mnt/artifacts/steps/tree.txt` or `/mnt/artifacts/steps/tree.json`.
- Added a single table under `# 可读文档和组织结构` covering:
  `steps/tree.txt`, `steps/tree.json`, successful step node directories,
  `development_history.json`, `experiment_ledger_full.jsonl`,
  `meta_learning_memory.jsonl`, parent output/models, `run_manifest.json`,
  writable output/models, and `taste.md`.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Prompt grep confirmed no `# Web Search Engines`, `# development 摘要`, or
  stale "engine 必须从下方" wording remains in source/exported prompts.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_can_read_existing_step_tree tests.unit.test_step_tree`
  passed, 26 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.
- Resource checks:
  - before prompt/test work: about 222 GiB available RAM; no GPU job started.
  - after checks: about 211 GiB available RAM; GPU usage remained external.

## 2026-06-24 - Meta Learning first-epoch empty-history prompt branch

Task: decide whether the Meta Learning prompt should be optimized for the
first Epoch, where there are no historical Step records or development
results.

Implementation:
- Added `# 首轮空历史处理` to `META_LEARNING_INSTRUCTION`.
- The prompt now treats `(empty step tree)`, empty `tree.json.nodes`, empty
  development ledger, or empty `meta_learning_memory.jsonl` as normal first
  Epoch state.
- The first-epoch branch tells the Agent not to chase missing history, not to
  fabricate validated conclusions, and not to regularize nonexistent overfit
  experience. It should instead inspect initial `output/`, `models/`,
  `run_manifest.json`, visible data/tool contracts, and web-search results to
  write the first Taste.
- Updated the readable-file table to mark `steps/tree.txt`, `tree.json`,
  `parent_output`, and `parent_models` as possibly empty in the first Epoch.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Pre-run resources:
  - `free -h`: about 304 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Prompt grep confirmed `# 首轮空历史处理`, `(empty step tree)`, and
  first-Epoch empty-path wording in both source and exported prompt.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_can_read_existing_step_tree tests.unit.test_step_tree`
  passed, 26 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.
- Post-run resources:
  - `free -h`: about 277 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning prompt consolidation and exploration tolerance

Task: merge the duplicate Meta Learning prompt sections in `PROMPTS.md` and add
wording that allows continued exploration when the current plan looks weak but
has a plausible path to improve.

Implementation:
- Removed the separate `元学习协议模板（META_LEARNING_INSTRUCTION）` section
  from `scripts/dev/export_prompts.py`. The exported prompt audit now keeps one
  Meta Learning section: `元学习 + 正则化系统提示词（完整渲染示例）`.
- Added `# 探索容忍` to `META_LEARNING_INSTRUCTION`: a direction may continue
  even if the current or previous result is poor when it has a clear
  hypothesis, interpretable failure reason, and testable improvement path.
- The same section tells the Agent to downgrade or avoid repeated failed
  paths, stock/month memorization, or ideas without a verifiable mechanism.
- Regenerated `configs/prompts/PROMPTS.md`.

Validation:
- Resource checks:
  - before prompt/test work: about 208 GiB available RAM; no GPU job started.
  - after checks: about 208 GiB available RAM; GPU usage remained external.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Prompt grep confirmed `元学习协议模板` no longer appears in exported
  `PROMPTS.md`, while `# 探索容忍` is present in source and export.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_can_read_existing_step_tree tests.unit.test_step_tree`
  passed, 26 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty.

## 2026-06-24 - Models naming and meta-search provider prompt alignment

Task: answer two follow-up design questions from the model-parameter work:
whether `model_artifacts` conflicts visually with outer `artifacts`, and
whether Tavily is explicitly exposed in the Meta Learning System Prompt.

Conclusion:
- The visible `model_artifacts` directory name was unnecessarily repetitive
  next to `/mnt/artifacts`, especially for
  `/mnt/artifacts/parent_model_artifacts` and
  `strategy_artifacts/<epoch>/<id>.model_artifacts`.
- Tavily was implemented and selected by `run_experiment.py` by default, but
  the Meta Learning prompt only described a generic web provider or Semantic
  Scholar. The manifest recorded the provider after CLI setup, but the System
  Prompt did not render the concrete provider name.

Implementation:
- Changed Agent-visible model parameter paths to:
  - `/mnt/agent/models/`
  - `/mnt/artifacts/parent_models/`
  - `strategy_artifacts/<epoch>/<strategy_artifact_id>.models/`
  - Step tree node model snapshots under `steps/<node_id>/models/`.
- Kept internal API and hash terminology such as `load_model_artifacts()` and
  `model_artifact_hash`, because those name the artifact type rather than the
  visible directory.
- Formal strategy execution now exposes `context["model_dir"]`, `ctx.model_dir`,
  and `MQ_MODEL_DIR`; the old agent-visible `model_artifacts_dir` /
  `MQ_MODEL_ARTIFACTS_DIR` aliases were removed from the strategy runtime.
- Structured search prompt/root choices now advertise `models`, not
  `model_artifacts`.
- `build_meta_learning_prompt()` now renders `# Web Search Provider` with the
  actual provider name passed by the runner (`tavily`, `semantic_scholar`, or
  `disabled`).
- Meta Learning prompt text now distinguishes Tavily as general web search and
  Semantic Scholar as paper search.
- Updated `configs/agent_output_template/README.md`, template `main.py`,
  `configs/prompts/PROMPTS.md`, and the Agent/Environment/Pipeline living docs.

Validation:
- Pre-test resource checks:
  - `free -h`: about 196 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Targeted regression suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_artifacts tests.unit.test_tools_flow tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e`
  passed, 82 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 226 tests.
- `git diff --check` clean.
- Source/script/test cache scan returned empty after removing Python cache
  directories from `src`, `scripts`, and `tests`.
- Post-test resource checks:
  - `free -h`: about 197 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Decision-only model/NL boundary

Task: tighten the model/NL/workspace boundary so models and NL are used only
during decision generation, not during minute-by-minute trade strategy replay.

Implementation:
- Kept decision-stage access in `output/main.py` and helpers:
  `context["model_dir"]`, `context["workspace_dir"]`, `MQ_MODEL_DIR`,
  `MQ_WORKSPACE_DIR`, and `mq_tools.nl(...)`.
- Removed `model_dir`, `workspace_dir`, and `nl` from the per-bar trade-policy
  `ctx` built by `_STRATEGY_POLICY_DRIVER`.
- Removed policy-stage NL RPC files and serving from `StrategyPolicyRunner`.
  The policy process still provides an importable `mq_tools.nl` stub so
  modules with top-level `from mq_tools import nl` import cleanly, but calling
  it during replay raises an explicit "decision stage" error.
- Policy replay no longer passes `MQ_MODEL_DIR`, `MQ_WORKSPACE_DIR`, or NL
  request/response env vars. It also treats `/mnt/agent/workspace` and
  `/mnt/agent/models` as forbidden paths during strategy-function replay.
- Updated Agent prompt, exported `PROMPTS.md`, Agent template README/trading.py,
  and Agent/Environment docs to describe `ctx` as a pure trading replay context.
- Added regression coverage:
  - trade-policy `ctx` must not expose `model_dir`, `workspace_dir`, or `nl`;
  - importing `mq_tools.nl` in `trading.py` is allowed, but calling it during
    replay is rejected.

Validation:
- Pre-test resources:
  - `free -h`: about 212-214 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `/home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Targeted regression suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_broker_engine tests.unit.test_pipeline_e2e`
  passed, 80 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 228 tests.
- `git diff --check` clean.
- Source/script/test/template cache scan returned empty.
- Post-test resources:
  - `free -h`: about 212 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning web_search engine selection

Task: let the Meta Learning Agent choose the `web_search` engine per query
instead of choosing a category under one preselected provider.

Implementation:
- Reworked `WebSearchTool` into a multi-engine wrapper. It now receives a
  mapping of engine name to provider, validates the requested `engine`, and
  traces `engine`, provider, query, result count, and sanitized results.
- Changed `AgentSessionRunner` meta-learning action schema from
  `category + query` to `engine + query`. The runner no longer enforces
  `finance/cross_domain/philosophy` categories; when engines are configured,
  `done` requires at least one successful `web_search`.
- Changed `scripts/experiments/run_experiment.py` from
  `--web-search-provider` to multi-value `--web-search-engines`, defaulting to
  `tavily semantic_scholar`. The meta-learning manifest and ledger now record
  `web_search_engines`.
- Updated `build_meta_learning_prompt()` and exported `PROMPTS.md` to render
  `# Web Search Engines` and the `{"action": "web_search", "engine": ...}`
  action contract.
- Updated Agent, Environment, and Pipeline living docs to describe host-side
  engine exposure and per-action engine selection.
- Updated sandbox isolation tests for the new action schema and completion
  gate.

Validation:
- Resource checks before prompt export/tests:
  - `free -h`: about 212-218 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded and wrote `configs/prompts/PROMPTS.md`.
- Static/CLI checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/web_search.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  passed and shows `--web-search-engines {tavily,semantic_scholar} ...`.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation`
  passed, 19 tests.
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e tests.unit.test_tools_flow`
  passed, 75 tests.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests`
  passed, 228 tests.
- `git diff --check` clean.
- Source/script/test/config cache scan returned empty after removing generated
  Python cache directories.
- Post-test resources:
  - `free -h`: about 215 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning shell network and dataset probe prompt

Task: simplify the Meta Learning data-inspection requirement to prompt-level
guidance, while allowing a deliberately network-enabled Meta Learning Docker
sandbox to use shell commands such as `git clone`, `hf download`, `pip`, and
`npm`.

Implementation:
- Kept the existing `sandbox_shell_tool` as the Agent-facing interactive shell
  path. No new hard-gated Python-only tool was retained.
- Updated the Meta Learning prompt so Taste should be written after using
  shell-invoked Python to inspect visible snapshot/runtime shape, including
  parquet files, columns, rows, date coverage, key nulls, and unit constraints.
  This is guidance, not a `done` blocker.
- Split prompt export so `configs/prompts/PROMPTS.md` contains one complete
  Meta Learning system prompt plus a short experiment-directive appendix
  example, instead of two largely duplicated complete prompts.
- Added `ExperimentConfig.meta_learning_sandbox_spec` and CLI flags:
  `--meta-learning-network`, `--meta-learning-env`, and
  `--meta-learning-add-host-gateway`. The override is used only for
  Epoch-start Meta Learning runs; ordinary Fold and held-out runs continue to
  use the base sandbox spec.
- Docker sandbox startup now supports explicit environment-variable
  passthrough by name and optional `host.docker.internal` gateway mapping.
  `docker run` uses `--env NAME`, so token/proxy values are not embedded in
  the rendered command or manifests. The allocation record and runtime
  contract record names and network mode only.
- Updated `ops/docker/sandbox.Dockerfile` to install `git`, `curl`, `npm`, and
  `huggingface_hub[cli]`; it creates an `hf` compatibility symlink when the
  package only exposes `huggingface-cli`.
- Updated runtime env records to list `git`, `npm`, `pip`, `hf`, and
  `huggingface-cli` as important tools, and to document the meta-learning
  package-install/network policy.
- Relaxed the shell path scanner so URLs such as `https://github.com/...` are
  not mistaken for absolute filesystem paths; filesystem boundary checks still
  apply to `/mnt/...` and local paths.
- Updated Agent, Environment, and Pipeline living docs. The XRay/proxy flow is
  documented generically: start a local proxy on the host, set proxy
  environment variables locally, and pass variable names into the Meta Learning
  container. No proxy subscription URL or token was written.

Security notes:
- The HuggingFace token and proxy URL provided in chat were not written to the
  repository, command outputs, manifests, docs, prompts, or logbooks.
- GitHub tokens cannot be generated by this code path; use GitHub/`gh auth` to
  create a scoped token, then expose it locally as an environment variable if
  needed.
- The exposed HuggingFace token should be rotated because it appeared in chat.

Validation:
- Pre-test resources:
  - `free -h`: 272 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by existing external workloads; this work
    started no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/environment/sandbox.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/executor.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e tests.unit.test_tools_flow tests.unit.test_step_tree -v`
  passed, 89 tests.
- Secret/proxy scan: searched for the concrete token/proxy fragments provided
  in chat across the working tree, excluding historical logbooks and external
  references; no matches were found outside transient conversation context.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests.
- Post-test resources:
  - `free -h`: 273 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning direct network and optional host proxy

Task: make the Meta Learning Docker sandbox use normal direct networking by
default, keep proxy usage optional, and pass the proxy option into the Meta
Learning system prompt without recording proxy subscriptions or token values.

Implementation:
- Changed `scripts/experiments/run_experiment.py` so
  `--meta-learning-network` defaults to `bridge`. This applies only to
  Epoch-start Meta Learning runs through `meta_learning_sandbox_spec`;
  ordinary Fold and held-out runs continue to use the base sandbox policy.
- Added default meta-learning credential env passthrough names:
  `GITHUB_TOKEN` and `HF_TOKEN`. Docker still receives env values only if
  those variables are already present in the host process environment.
- Added `--meta-learning-host-proxy`. When enabled, the meta-learning sandbox
  passthrough list includes standard proxy env names, and host gateway mapping
  is enabled so Docker bridge containers can reach host proxy ports via
  `host.docker.internal`.
- Added a dynamic non-secret Meta Learning system prompt section:
  `# 本次联网与代理选项`. It records network mode, credential/proxy env variable
  names, default direct-network behavior, and secret-handling rules.
- Added `ExperimentConfig.meta_learning_network_guidance`, persisted it into
  the meta-learning run manifest and `experiment_parameters`, and passed it
  from the CLI meta learner into `AgentSessionRunner`.
- Updated prompt export to include a short network/proxy appendix example
  rather than another full Meta Learning prompt.
- Updated Agent, Environment, and Pipeline docs to state that Meta Learning
  defaults to direct Docker bridge networking; proxy is opt-in and should be
  configured on the host through Docker-reachable proxy env vars.

Security notes:
- The raw GitHub token provided in chat was not used in a command, written to
  files, or recorded in docs/logbooks. Current host environment reported
  `GITHUB_TOKEN` missing, so token usability was not checked.
- The correct operational path is to set `GITHUB_TOKEN` in the host environment
  outside the repository; the pipeline will pass the variable by name when it
  exists.
- Any token pasted into chat should be rotated before use.

Validation:
- Pre-test resources:
  - `free -h`: 272 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/__init__.py src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  passed, 45 tests.
- Generic secret/proxy scan found no token or VLESS-style strings in the
  working tree outside external references.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests.
- Post-test resources:
  - `free -h`: 272 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning first Fold visible data parity

Task: make Meta Learning see the same visible data as the first Fold Agent.
The previous implementation exposed the first Fold `valid_decision_input` as
`/mnt/snapshot` and `/mnt/snapshots/train`, but did not install the first
Fold validation replay slot under `/mnt/snapshots/valid`. That made old
runtime directories such as `.runtime/sandboxes/run_2ce27d85d933/snapshots/valid`
empty even though the ordinary Fold Agent can read a validation replay slot.

Implementation:
- Updated `ExperimentPipeline.run_meta_learning()` to create a first-Fold
  validation replay slot with `snapshot_views.replay_slot(...)` when a
  visible Fold is supplied.
- The Meta Learning sandbox now exposes:
  - `/mnt/snapshot`: first Fold decision-input PIT view.
  - `/mnt/snapshots/train`: alias/copy of the same first Fold decision-input
    PIT view.
  - `/mnt/snapshots/valid`: first Fold validation replay slot.
  - no `/mnt/snapshots/test` and no held-out data.
- The Meta Learning run manifest now records `valid_replay` beside the
  visible decision-input snapshot reference.
- Updated Agent, Environment, and Pipeline docs plus exported
  `configs/prompts/PROMPTS.md` to match the runtime contract.
- Extended the pipeline end-to-end unit test to assert that Meta Learning can
  list and read `daily.parquet` and `manifest.json` under
  `/mnt/snapshots/valid`, while `/mnt/snapshots/test` remains unavailable.

Validation:
- Pre-test resources:
  - `free -h`: 444 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/pipelines/experiment.py src/hl_trader/agent/prompts.py tests/unit/test_pipeline_e2e.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_single_epoch_runs_meta_learning_before_fold_and_heldout tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_multi_epoch_runs_meta_learning_before_each_epoch tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_injects_full_records_and_prior_epoch_logs`
  passed, 3 tests.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.

Note:
- Existing runtime sandboxes are immutable historical artifacts. The old
  `.runtime/sandboxes/run_2ce27d85d933/snapshots/valid` directory remains
  empty because it was created before this fix. A new Meta Learning run is
  required to observe the corrected mounted data layout.

## 2026-06-24 - Meta Learning visible data and Shell guard simplification

Task: give the Epoch-start Meta Learning Agent the same visible PIT data as the
first ordinary Fold, keep the Agent-facing language Chinese, and refine Shell
path guarding so read-only exploration is not rejected as a write.

Implementation:
- `ExperimentPipeline.run()` now passes the first scheduled Fold into
  `run_meta_learning()`.
- `run_meta_learning()` builds only that Fold's `valid_decision_input`, binds it
  as `/mnt/snapshot`, and copies it into `/mnt/snapshots/train` as the
  Agent-visible alias. It does not build validation replay, test replay, or
  held-out slots for Meta Learning.
- The meta-learning run manifest records the visible Fold, decision time, and
  `train_snapshot` / `valid_decision_input` snapshot id/hash. Existing
  `experiment_parameters` remains the source for fold period and window
  configuration, avoiding duplicate top-level fields.
- Runner initial user messages for both Meta Learning and ordinary Fold
  sessions are now Chinese.
- Fold and Meta Learning prompts now say not to hide errors with
  `2>/dev/null`; stderr should remain visible in trace.
- `SandboxShellTool` now separates command-level writes from redirection or
  copy targets. Read-only listing and reading are allowed, and copying a
  visible read-only file into `/mnt/agent/workspace` is allowed. Writes to
  snapshot, artifacts, results/steps, test, runtime, unmanaged sandbox paths, or
  outside the sandbox remain blocked. Common bash redirections including `>`,
  `2>`, `&>`, and `&>>` are treated as write-target syntax.
- Living docs and `configs/prompts/PROMPTS.md` were updated to match the
  current contract.

Validation:
- Pre-test resources:
  - `free -h`: 440 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/pipelines/experiment.py src/hl_trader/environment/tools/shell.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_pipeline_e2e.py tests/unit/test_tools_flow.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e tests.unit.test_tools_flow.ShellToolTest tests.unit.test_sandbox_isolation.MetaLearningSessionTest`
  passed, 38 tests.
- `git diff --check` over touched files passed.
- Post-test resources were not materially changed; no GPU job was launched.

## 2026-06-24 - Standard Meta Learning Fold rerun

Task: rerun one formal Epoch-start Meta Learning Fold using the standard
experiment environment and configuration after the visible-data and Shell guard
changes.

Configuration:
- Experiment ID: `meta_learning_formal_20260624_230548`.
- Invocation: direct `ExperimentPipeline.run_meta_learning()` call; this was
  meta-learning-only and did not run ordinary Fold replay or held-out.
- Visible Fold: first scheduled Fold, `fold_2022Q1`.
- Visible decision input: `valid_decision_input` at
  `2021-10-08T09:25:00+08:00`, bound to `/mnt/snapshot` and installed as
  `/mnt/snapshots/train`.
- Sandbox: real Docker, image `macroquant-sandbox:latest`
  (`sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`).
- Agent model: `deepseek-v4-pro`.
- NL model: `deepseek-v4-flash`.
- Compact model: `deepseek-v4-flash`, thinking disabled.
- Reasoning effort: `max` for Agent and NL calls.
- Web Search engines: `tavily`, `semantic_scholar`.
- Fold period/default windows: quarterly WF, `window_months=21`,
  `intraday_trade_days=5`.
- Fold deadline: 60 minutes.
- Context compaction: enabled, trigger threshold 200,000 tokens, keep recent
  messages 12, max compaction calls 8.
- Docker network: meta-learning sandbox `bridge`; base ordinary Fold sandbox
  remains `none`.

Pre-run checks:
- Real path: `/Data/lzp/MacroQuant`.
- Docker image check:
  `docker image inspect macroquant-sandbox:latest --format '{{.Id}}'`
  returned `sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`.
- `free -h` before the run: 439 GiB available RAM.
- `nvidia-smi` before the run: GPUs were occupied by external workloads; the
  meta-learning run itself was not local training.
- Python cache scan before the run was empty.

Run:
- Used `/home/lzp/miniconda3/envs/quant/bin/python` with
  `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=src`.
- Constructed `ExperimentConfig` with `use_docker=True`,
  `first_test_period=2022Q1`, `last_test_period=2025Q4`,
  `heldout_first_period=2026Q1`, `heldout_last_period=2026Q1`,
  `fold_period=quarter`, `window_months=21`, `max_fold_minutes=60`, and
  `SnapshotConfig(window_months=21, intraday_trade_days=5)`.
- Called `pipeline.run_meta_learning(epoch_id="epoch_001", parent=None,
  previous_taste="", visible_fold=fold_2022Q1)`.
- Host log:
  `logs/meta_learning_formal_20260624_230548.log`.

Result:
- Run ID: `run_2ce27d85d933`.
- Fold ID: `epoch_001_meta_learning`.
- Status: `taste_only`.
- Agent session finish status: `meta_learning_done`.
- Taste length: 969 characters.
- Modification check: `allowed_to_backtest=true`, no reasons, no regularized
  parent artifact because this was the first epoch with no parent.
- Context compaction: 0 calls / 0 compactions.
- LLM usage from trace integer counters:
  `prompt_tokens=204750`, `completion_tokens=13167`,
  `total_tokens=217917`, `prompt_cache_hit_tokens=186752`,
  `prompt_cache_miss_tokens=17998`.
- Trace event counts:
  `llm_call=20`, `shell=11`, `web_search=3`, `tool=1`, `session_end=1`.
- Web search calls:
  - `finance_quant_econ`: Tavily, 5 results.
  - `natural_science_engineering`: Semantic Scholar, 5 results.
  - `philosophy_methodology`: Tavily, 5 results.
- Shell trace showed multiple read-only data checks over `/mnt/snapshot`
  parquet/text files and Step tree. It did not use `2>/dev/null`; one command
  used `2>&1`, which merged stderr into stdout rather than hiding it.

Key paths:
- Experiment ledger:
  `experiments/meta_learning_formal_20260624_230548/ledgers/experiment_ledger.jsonl`.
- Taste:
  `experiments/meta_learning_formal_20260624_230548/meta_learning/epoch_001/taste.md`.
- Canonical trace:
  `experiments/meta_learning_formal_20260624_230548/artifacts/run_2ce27d85d933/agent_trace.jsonl`.
- Run manifest:
  `experiments/meta_learning_formal_20260624_230548/artifacts/run_2ce27d85d933/run_manifest.json`.
- Runtime env:
  `experiments/meta_learning_formal_20260624_230548/artifacts/run_2ce27d85d933/runtime_env.json`.
- Runtime sandbox:
  `.runtime/sandboxes/run_2ce27d85d933/` (about 2.6 GiB, retained for audit).

Post-run checks:
- Manifest snapshot check: `train_snapshot.alias_of=valid_decision_input`; both
  entries share snapshot hash
  `sha256:55996c50915eb753e3138b39e0ca4a1735bdc85e1d800c75124b20883d8d0fc2`.
- `docker ps --filter name=mqsbx` showed no running sandbox container.
- Secret/proxy scan over the run log, experiment artifacts, and runtime
  sandbox found no GitHub/HF/OpenAI-style token or VLESS-style proxy string.
- Generated Python cache scan under `src/`, `scripts`, and `tests` was empty.
- `free -h` after the run: 443 GiB available RAM.
- `nvidia-smi` after the run: GPU usage remained external to this run; Docker
  allocation record shows GPU 5 was assigned to the sandbox, but no local
  training workload was launched.

## 2026-06-24 - Formal meta-learning Fold audit run

Task: start one formal Epoch-start meta-learning Fold with the same real
Docker audit configuration used in the previous run, so the process and output
can be manually audited.

Configuration:
- Experiment ID: `meta_learning_formal_20260624_2153`.
- Invocation: direct `ExperimentPipeline.run_meta_learning()` call, so this
  was meta-learning-only and did not run ordinary Fold replay or held-out.
- Sandbox: real Docker, image `macroquant-sandbox:latest`
  (`sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`).
- Agent model: `deepseek-v4-pro`.
- NL model: `deepseek-v4-flash`.
- Compact model: `deepseek-v4-flash`, thinking disabled.
- Reasoning effort: `max` for Agent and NL calls.
- Web Search engines: `tavily`, `semantic_scholar`.
- Fold period/default windows: quarterly WF, `window_months=21`,
  `intraday_trade_days=5`.
- Fold deadline: 60 minutes.
- Context compaction: enabled, estimated trigger threshold 200,000 tokens,
  keep recent messages 12, max compaction calls 8.
- Docker network: meta-learning sandbox `bridge`; base ordinary Fold sandbox
  remains `none`.
- Environment variables: `DEEPSEEK_API_KEY`, `TAVILY_API_KEY`,
  `SEMANTIC_SCHOLAR_API_KEY`, and `GITHUB_TOKEN` were present via `.env`;
  `HF_TOKEN` and standard host proxy variables were not present. Values were
  not printed or written.

Pre-run checks:
- Real path: `/Data/lzp/MacroQuant`.
- Docker image check:
  `docker image inspect macroquant-sandbox:latest --format '{{.Id}}'`
  returned `sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`.
- `free -h` before the run: 272 GiB available RAM.
- `nvidia-smi` before the run: GPUs were occupied by external workloads; the
  meta-learning run itself was not a local training workload.

Run:
- Used `/home/lzp/miniconda3/envs/quant/bin/python` with
  `PYTHONDONTWRITEBYTECODE=1`.
- Constructed `ExperimentConfig` with `use_docker=True`,
  `first_test_period=2022Q1`, `last_test_period=2025Q4`,
  `heldout_first_period=2026Q1`, `heldout_last_period=2026Q1`,
  `fold_period=quarter`, `window_months=21`, `max_fold_minutes=60`,
  and `SnapshotConfig(window_months=21, intraday_trade_days=5)`.
- Called `pipeline.run_meta_learning(epoch_id="epoch_001", parent=None,
  previous_taste="")`.
- Host log:
  `logs/meta_learning_formal_20260624_2153.log`.

Result:
- Run ID: `run_c68b0781704c`.
- Fold ID: `epoch_001_meta_learning`.
- Status: `taste_only`.
- Agent session finish status: `meta_learning_done`.
- Taste length: 761 characters.
- Context compaction: 0 calls / 0 compactions.
- LLM usage from trace integer counters:
  `prompt_tokens=231042`, `completion_tokens=14096`,
  `total_tokens=245138`, `prompt_cache_hit_tokens=209024`,
  `prompt_cache_miss_tokens=22018`.
- Trace event counts:
  `llm_call=22`, `shell=12`, `web_search=3`, `tool=1`, `session_end=1`.
- Web search calls:
  - `finance_quant_econ`: Tavily, 5 results.
  - `natural_science_engineering`: Semantic Scholar, 3 results.
  - `philosophy_methodology`: Semantic Scholar, 5 results.

Key paths:
- Experiment ledger:
  `experiments/meta_learning_formal_20260624_2153/ledgers/experiment_ledger.jsonl`.
- Taste:
  `experiments/meta_learning_formal_20260624_2153/meta_learning/epoch_001/taste.md`.
- Canonical trace:
  `experiments/meta_learning_formal_20260624_2153/artifacts/run_c68b0781704c/agent_trace.jsonl`.
- Run manifest:
  `experiments/meta_learning_formal_20260624_2153/artifacts/run_c68b0781704c/run_manifest.json`.
- Runtime env:
  `experiments/meta_learning_formal_20260624_2153/artifacts/run_c68b0781704c/runtime_env.json`.
- Runtime sandbox:
  `.runtime/sandboxes/run_c68b0781704c/`.

Post-run checks:
- `free -h` after the run: 273 GiB available RAM.
- `nvidia-smi` after the run: GPU usage remained external to this run.
- Secret/proxy scan over the run log, experiment artifacts, and runtime
  sandbox found no GitHub/HF/OpenAI-style token or VLESS-style proxy string.
- Generated Python cache scan under `src/`, `scripts/`, and `tests/` was empty.

## 2026-06-24 - Meta Learning network prompt simplification

Task: fold the Meta Learning network/proxy rules into the static
`# 环境与配置` system-prompt section and remove the separate
`network_guidance` option, because Meta Learning network access is now a
default capability and run-specific details are already visible in
`runtime_env.json` and the run manifest.

Implementation:
- Rewrote the Meta Learning prompt subsection as
  `## 运行环境、联网与代理` under `# 环境与配置`.
- Removed `build_meta_learning_network_section(...)`,
  `build_meta_learning_prompt(network_guidance=...)`,
  `AgentSessionRunner(meta_learning_network_guidance=...)`, and the
  `ExperimentConfig.meta_learning_network_guidance` field.
- Removed the duplicated `meta_learning_network_guidance` manifest entries.
  Runtime facts now come from `/mnt/artifacts/runtime_env.json`
  (`network`, `sandbox_spec.env_passthrough`, `sandbox_spec.env_aliases`) and
  the run manifest.
- Removed the separate PROMPTS.md network/proxy example section; the base Meta
  Learning system prompt now contains the full stable policy.
- Updated `agent_design.md`, `pipeline_design.md`, CLI help text, and unit
  tests to reference runtime metadata instead of a dynamic prompt fragment.

Validation:
- Pre-test resources:
  - `free -h`: 271 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/agent/__init__.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation -q`
  passed, 29 tests.
- Pipeline tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e -q`
  passed, 21 tests.
- Tool-flow regression tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow -q`
  passed, 40 tests.
- CLI smoke:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/experiments/run_experiment.py --help`
  succeeded and now describes `--meta-learning-host-proxy` as recording alias
  names in `runtime_env.json`.
- Secret/proxy scan excluding `.env`, external references, logs,
  experiments, data, results, and wandb found no GitHub/HF token or
  VLESS-style strings in the working tree.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 272 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning network prompt injection

Task: make the Meta Learning network/proxy options section part of every
Meta Learning system prompt, and keep the injected guidance in Chinese.

Implementation:
- `build_meta_learning_prompt()` now always includes
  `build_meta_learning_network_section(...)`.
- When no experiment-specific network guidance is provided, the system prompt
  still includes `# 本次联网与代理选项` with a Chinese default section:
  follow `/mnt/artifacts/run_manifest.json` and
  `/mnt/artifacts/runtime_env.json`, use direct internet by default, do not
  enable proxy variables unless they are explicitly listed, and never print or
  persist credential/proxy values.
- `_meta_learning_network_guidance()` now renders the configured Docker
  network mode, credential env names, passthrough env names, proxy alias env
  names, and host-proxy behavior in Chinese.
- `scripts/dev/export_prompts.py` was updated so `PROMPTS.md` shows the
  always-present base section plus a Chinese configured-section example.
- Unit coverage now checks both configured guidance and the no-guidance default
  prompt path.

Validation:
- Pre-test resources:
  - `free -h`: 271 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py scripts/experiments/run_experiment.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation -q`
  passed, 29 tests.
- Secret/proxy scan excluding `.env`, external references, logs,
  experiments, data, results, and wandb found no GitHub/HF token or
  VLESS-style strings in the working tree.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 271 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning system prompt cleanup

Task: make the Meta Learning Agent system prompt cleaner, easier to audit,
and complete enough for current data, networking, tool, and Taste semantics.

Implementation:
- Reorganized `META_LEARNING_INSTRUCTION` in
  `src/hl_trader/agent/prompts.py` into stable sections:
  role and goal, work order, first-run empty-history handling, readable and
  writable files, runtime/networking, action protocol, research protocol,
  Taste output contract, exploration tolerance, optional regularization, and
  forbidden behavior.
- Kept the prompt focused on the current accepted behavior: read the Step
  tree first, inspect run/runtime manifests, sample visible PIT snapshots with
  Python through shell, use all configured research perspectives when web
  search is enabled, write `/mnt/agent/workspace/taste.md`, and call
  `modification_check` only if `output/` or `models/` were changed.
- Clarified that credentials and proxy values are environment-only and must
  not be printed, copied, written to Taste, written to artifacts, or logged.
- Renamed the exported prompt audit section from a rendered-example title to
  `元学习 Agent System Prompt（基础模板）`.
- Re-rendered `configs/prompts/PROMPTS.md` from the source templates.

Validation:
- Pre-test resources:
  - `free -h`: 271 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation -v`
  passed, 26 tests.
- Prompt snapshot scan confirmed the old `Web Search Engines`,
  `development 摘要`, and duplicate rendered-example meta prompt labels are not
  present in the exported Meta Learning prompt section.
- Secret/proxy scan excluding `.env` and `external_references/` found no
  GitHub/HF token or VLESS-style strings in the working tree.
- `git diff --check` over the touched prompt/log files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 271 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Prompt structure cleanup

Task: reorganize Fold Agent and Meta Learning Agent prompts into the requested
three top-level sections, make Meta Learning workflow steps non-sequential,
and replace sampling-oriented wording with detailed data inspection language.

Implementation:
- Split the Fold Agent prompt source into `FOLD_ROLE_SECTION`,
  `FOLD_ENV_SECTION`, and `FOLD_ACTION_SECTION`, rendered as:
  `# 角色与目标`, `# 环境与配置`, and `# 动作与流程`.
- Updated `build_system_prompt()` so dynamic Fold info, acceptance rules,
  Step tree, Taste, anti-overfit rules, convergence guidance, and phase
  guidance are inserted as second-level sections under the three top-level
  blocks instead of creating additional top-level prompt sections.
- Reorganized `META_LEARNING_INSTRUCTION` into the same three top-level
  sections. Under `# 环境与配置`, it now contains first-run empty-history
  handling, readable/writable files, and runtime/networking. Under
  `# 动作与流程`, it now contains action protocol, non-sequential work steps,
  Taste output contract, and forbidden behavior.
- Changed Meta Learning wording from fixed `工作顺序` to flexible `工作步骤`,
  explicitly allowing repeated `shell`, `grep/glob`, and `web_search` calls as
  new evidence appears.
- Replaced sampling wording with detailed inspection wording:
  visible snapshot checks are now described as read-only detailed inspection
  and analysis. The old “再形成 Taste” phrase was removed.
- Updated the Runner initial Meta Learning user message to match the flexible
  shell/web_search workflow.
- Kept ordinary Fold networking and package installation disabled by default.
  The decision is intentional: ordinary Fold validation should stay
  reproducible and not depend on transient workspace downloads. Meta Learning
  remains the place for networked research and dependency feasibility checks.
- Re-rendered `configs/prompts/PROMPTS.md`.
- Updated `docs/agent_design.md` and `docs/pipeline_design.md` to match the
  new detailed data-inspection wording.

Validation:
- Pre-test resources:
  - `free -h`: 271 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py scripts/dev/export_prompts.py tests/unit/test_sandbox_isolation.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  passed, 49 tests.
- Residual wording scan confirmed no `工作顺序`, `再形成 Taste`, or
  Meta-Learning-specific `抽样检查` remains in prompt sources or prompt
  snapshot. Data sentinel documentation still uses `抽样检查` for its separate
  historical-partition audit concept.
- Secret/proxy scan excluding `.env` and `external_references/` found no
  GitHub/HF token or VLESS-style strings in the working tree.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 272 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning audit fixes and runtime cleanup

Task: delete the current `.runtime` directory and fix the SubAgent audit
findings around Meta Learning completion, Taste output, ordinary Fold shell
network/package boundaries, secret redaction, and web_search success semantics.

Runtime cleanup:
- Confirmed the physical repository path with `pwd -P`: `/Data/lzp/MacroQuant`.
- `.runtime` was about 222 GiB before deletion.
- Initial `rm -rf .runtime` removed most contents but was blocked by read-only
  historical sandbox files.
- The remaining tree was about 2.3 MiB and owned by the current user. Applied
  `chmod -R u+w .runtime`, then removed it.
- Final check confirmed `.runtime` no longer exists.

Implementation:
- `AgentSessionRunner.run()` now stores the session summary in `ctx.extra` and
  marks `meta_learning_done` only after the explicit `done` path succeeds.
- Meta Learning `done` now requires non-empty
  `/mnt/agent/workspace/taste.md`; missing or blank Taste is rejected.
- `web_search` only counts a research perspective as complete when the result
  count is non-zero. Empty search results are traced as `empty_results` and do
  not satisfy the three-perspective requirement.
- The production CLI meta learner now returns the `AgentSessionRunner` summary.
  Pipeline validates real summaries and accepts Taste/regularization only when
  `finish_status == "meta_learning_done"`; it also fail-fasts on missing or
  empty Taste.
- Ordinary Fold `sandbox_shell_tool` now rejects common install/download/network
  entry points such as `pip install`, `python -m pip install`, `conda install`,
  `npm install`, `git clone`, `hf download`, `curl`, and `wget`. Meta Learning
  runs retain the configured open-network shell behavior.
- Runtime log redaction now covers common OpenAI/HF/GitHub token forms, VLESS
  links, and proxy URLs with embedded credentials. Oversized Shell stdout/stderr
  files are sanitized before being written under tool results.
- Meta Learning prompt wording no longer hard-codes a fixed web_search engine
  list in the action example; the tool schema remains the source of actual
  configured engines.
- Updated living docs and re-rendered `configs/prompts/PROMPTS.md`.

Validation:
- Pre-test resources:
  - `free -h`: 271 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py src/hl_trader/environment/runtime.py src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py scripts/experiments/run_experiment.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py tests/unit/test_tools_flow.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e tests.unit.test_tools_flow -v`
  passed, 89 tests.
- Secret/proxy scan excluding `.env` and `external_references/` found no
  GitHub/HF token or VLESS-style strings in the working tree.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 271 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Meta Learning proxy aliases and GitHub token check

Task: keep Meta Learning Docker proxy configuration available without making
proxy the default networking path, and verify the locally configured GitHub
token without exposing it.

Implementation:
- Added `SandboxSpec.env_aliases`, recorded as container env name to host env
  name mappings. Values are not written to manifests.
- Docker startup now supports alias env vars by placing values in the
  `subprocess.run(env=...)` environment and passing only `--env CONTAINER_NAME`
  to `docker run`. This avoids embedding proxy values in the command line.
- `--meta-learning-host-proxy` now maps host standard proxy variables into
  non-standard container aliases:
  `MQ_PROXY_HTTP`, `MQ_PROXY_HTTPS`, `MQ_PROXY_ALL`, `MQ_PROXY_NO_PROXY`.
  It does not inject standard `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or
  `NO_PROXY`, so Agent shell commands use direct internet by default.
- The experiment CLI now selectively loads the allowed passthrough variable
  names from the repository `.env` into the process environment before
  rendering the Meta Learning sandbox spec. Values are not printed or
  recorded, and existing process env values are not overwritten.
- For Docker bridge mode with host gateway enabled, localhost proxy URLs are
  rewritten to `host.docker.internal` before being placed into the container
  alias env vars.
- Meta Learning system prompt guidance now tells Agent to try direct access
  first and only map alias vars to standard proxy vars for a specific command
  if GitHub/HuggingFace/PyPI/npm is slow or blocked.
- Updated Agent/Environment/Pipeline docs and prompt snapshot to match the
  proxy-alias behavior.

GitHub token check:
- `.env` is ignored by `.gitignore`.
- Loaded `GITHUB_TOKEN` from `.env` in memory and called GitHub `/user` with an
  Authorization header. The token authenticated successfully as login
  `LeoZippon`; GitHub reported a 5000/hour rate-limit bucket. The token value
  was not printed or written.

Validation:
- Pre-test resources:
  - `free -h`: 272 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/sandbox.py scripts/experiments/run_experiment.py src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/pipelines/config.py src/hl_trader/pipelines/experiment.py tests/unit/test_sandbox_isolation.py tests/unit/test_pipeline_e2e.py`
  passed.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  passed, 46 tests.
- Secret scan excluding `.env` found no GitHub/HF token or VLESS-style strings
  in the working tree outside external references.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests.
- Post-test resources:
  - `free -h`: 272 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-24 - Shell output budget and template path cleanup

Task: review whether the Agent-facing shell schema should support
`max_output_chars`, compare with `external_references/claude-code-main`, and
explain why inner Agent sessions could see the host path
`/Data/lzp/MacroQuant/configs/agent_output_template/`.

Findings:
- Claude Code's `BashTool` does not expose a model-facing
  `max_output_chars` field. Its input schema includes command metadata such as
  command, timeout, description and background/sandbox options, while output is
  controlled by fixed tool/result budgets. Large command output is persisted to
  a tool-results file and the model receives a preview plus path.
- This repository already had the lower-level executor parameter
  `max_output_chars` and tool-result persistence, but the Agent-facing shell
  action schema only accepted `command`. The model could not ask for a smaller
  inline budget when running noisy commands.
- Inner Agent sessions could see the host template path because Pipeline wrote
  `"template_dir": str(self.config.template_dir)` into the Agent-readable
  `/mnt/artifacts/run_manifest.json`. `modification_check_tool` used that host
  path as the initial diff baseline.

Implementation:
- Added optional shell action field `max_output_chars` with bounds
  `1..20000`. It can only reduce inline stdout/stderr; executor capture remains
  bounded separately and oversized output is still written to tool-results.
- Runner now passes the validated field to `SandboxShellTool.run(...)`, and
  shell trace records the effective inline output budget.
- Initial strategy template is now copied into read-only
  `/mnt/artifacts/parent_output/`, making the diff baseline sandbox-local.
- `modification_check_tool` uses `parent_output` for initial artifacts and
  validates it against `initial_template_hash` when present.
- Fold and Meta Learning run manifests now write `template_ref` and
  `initial_template_hash`; they no longer write the host `template_dir` path.
- Updated Agent/Environment/Pipeline docs and exported prompt snapshot.

Validation:
- Pre-test resources:
  - `free -h`: 442 GiB available RAM.
  - `nvidia-smi`: GPUs were occupied by external workloads; this work started
    no GPU job.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/shell.py src/hl_trader/agent/runner.py src/hl_trader/environment/sandbox.py src/hl_trader/environment/tools/modification_check.py src/hl_trader/pipelines/experiment.py src/hl_trader/agent/prompts.py tests/unit/test_tools_flow.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Tool flow tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow -v`
  passed, 41 tests.
- Pipeline and sandbox tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation -v`
  passed, 50 tests.
- Path scan over source/docs/prompt snapshots found no new Agent-visible
  manifest `template_dir` field. Remaining `template_dir` references are
  internal CLI/config/template-copy implementation details.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.
- Post-test resources:
  - `free -h`: 441 GiB available RAM.
  - GPU usage remained external to this run.

## 2026-06-25 - Claude Code tool-pattern selective adoption

Task: selectively adopt useful Claude Code Tool patterns while keeping the
system simple and maintainable; also make `web_search` structurally consistent
with other Agent-facing tools.

Adopted:
- `ActionSpec` now records `schema_version` and `result_policy`. These fields
  are emitted through `tool_spec` in trace records and make future schema
  evolution and output-budget audits explicit.
- `sandbox_shell_tool` now records `command_kind` for audit:
  `read`, `list`, `search`, `write`, `install`, `network`, `neutral`, or
  `unknown`. This is a best-effort trace label; enforcement remains in the
  existing phase policy, Sandbox mounts, path guard, and install/network guard.
- `sandbox_shell_tool` accepts optional `timeout_seconds`, bounded by the
  existing default per-call shell timeout. Agent can shorten a noisy command
  but cannot extend execution beyond the configured boundary.
- `grep` and `glob` mark their result policy as `paginated_bounded_inline`.
- `web_search_tool` is now an Agent-facing tool under
  `src/hl_trader/environment/tools/web_search.py`. The provider aggregation and
  concrete Tavily/Semantic Scholar clients remain in
  `src/hl_trader/environment/web_search.py` as `WebSearchService`.
- Runner now gets the web-search spec and trace behavior from
  `AgentWebSearchTool` instead of constructing that tool inline.

Not adopted:
- Provider-native Claude/Anthropic tool-use protocol: useful later, but it
  would force a broader LLM Proxy and trace migration across providers.
- Arbitrary background shell tasks: high operational risk for Fold deadlines,
  resource limits, and reproducible freezing.
- Interactive permission prompts and sed preview: useful in a human CLI, but
  not aligned with automated Docker Fold execution.

Validation:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py src/hl_trader/environment/tools/web_search.py src/hl_trader/environment/tools/__init__.py src/hl_trader/environment/web_search.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_tools_flow.py tests/unit/test_sandbox_isolation.py`
  passed.
- Prompt snapshot consistency:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -c 'from pathlib import Path; from scripts.dev.export_prompts import render; assert Path("configs/prompts/PROMPTS.md").read_text(encoding="utf-8") == render()'`
  passed.
- Combined tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  passed, 93 tests.
- Resource checks after tests:
  - `free -h`: 446 GiB available RAM.
  - `nvidia-smi`: existing external GPU process remained on GPU 0; this work
    started no GPU job.
- `git diff --check` over touched files passed.
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests; follow-up cache scan was empty.

## 2026-06-25 - Tool guard audit fixes and meta-learning Fold rerun

Task: open SubAgents to audit current code structure/tool logic, close all
SubAgents after completion, fix discovered issues, then rerun one formal
meta-learning Fold in the Docker sandbox.

SubAgent audit trail:
- Initial broad tool/code audit found Shell guard weaknesses around
  install/network command segmentation and write-like commands under read-only
  paths.
- Follow-up focused audits found additional Shell boundary cases:
  naked relative write targets, `bash/sh -c` wrappers, `find -exec`, Python
  write APIs, command substitution, background execution, and no-space shell
  redirection such as `printf x>stray`.
- Final focused audit over `printf x>stray`, nested shell, and `find -exec`
  returned `Blocking: 无`, `Should Fix: 无`, `Nice To Have: 无`.
- All spawned SubAgents were closed after their results were consumed.

Implementation:
- `sandbox_shell_tool` now scans command segments including `&&`, `||`, `;`,
  `&`, `|`, and newline where safe; it also recurses through `env` and
  `bash/sh/zsh -c` wrappers.
- Ordinary Fold install/network guard now blocks wrapped and nested forms such
  as `echo ok && pip install`, `true & curl`, command substitution with
  `curl/wget`, and `find -exec curl` / `find -exec sh -c 'curl ...'`.
- Path guard now sends all write targets through `_guard_one_path()`, including
  bare relative writes such as `touch stray`, `printf x>stray`, `cp ... stray`,
  Python write APIs (`open`, `Path.open`, `write_text`, `to_csv`), nested
  `bash/sh -c`, and `find -exec sh -c` write targets.
- The no-space redirection scanner skips quoted text, preserving read-only
  commands like `rg "a>b" /mnt/snapshots/train`.
- `sandbox_shell_tool` result policy was renamed to
  `bounded_inline_with_persisted_captured_output` to match actual executor
  capture semantics.
- `web_search_tool` now emits a `web_search` trace record even on provider
  failure, with sanitized error text and the relevant tool spec/engine/query
  metadata.
- Runner now sanitizes ToolError/WebSearchError and generic Exception
  observations before returning them to the Agent context and trace.
- `StructuredSearchTool.glob()` now returns deterministically sorted matches.
- Meta/Fold prompt text was updated to reference `run_manifest.web_search_engines`
  instead of implying the complete schema is directly visible in the prompt;
  `configs/prompts/PROMPTS.md` was regenerated.

Verification before formal run:
- Resource checks:
  - `free -h`: about 441 GiB available RAM.
  - `nvidia-smi`: existing external process on GPU 0 using about 10.5 GiB; no
    local training started by this work.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static checks:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/web_search.py src/hl_trader/environment/tools/search.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_tools_flow.py tests/unit/test_sandbox_isolation.py`
  passed.
- Focused tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ShellToolTest tests.unit.test_sandbox_isolation.MetaLearningSessionTest -v`
  passed, 22 tests.
- Combined regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e -v`
  passed, 95 tests.
- `git diff --check` over touched tool/runner/test/prompt files passed.

Formal meta-learning-only Fold:
- Command log:
  `logs/meta_learning_tool_audit_20260625_013641.log`
- Experiment:
  `experiments/meta_learning_tool_audit_20260625_013641/`
- Run:
  `run_id=run_4c7511878785`
- Entry point:
  direct `ExperimentPipeline.run_meta_learning(epoch_id="epoch_001", parent=None, visible_fold=folds[0])`.
- Config:
  - `fold_period=quarter`
  - first development test period `2022Q1`, last `2025Q4`
  - held-out config `2026Q1..2026Q1` recorded but not executed in this
    meta-learning-only run
  - default decision window 21 months
  - intraday visible window 5 trading days
  - deadline 60 minutes
  - Docker sandbox network `bridge` for meta-learning, ordinary sandbox spec
    remains `network=none`
  - main Agent model `deepseek-v4-pro`, reasoning effort `max`
  - NL/compact model `deepseek-v4-flash`; compact thinking disabled
  - context compaction token threshold `200000`
  - web search engines `tavily`, `semantic_scholar`
- Visible fold:
  - `fold_id=fold_2022Q1`
  - input window `20200101..20210930`
  - validation period `20211001..20211231`
  - test period `20220101..20220331`
  - valid decision time `2021-10-08T09:25:00+08:00`
- Important artifacts:
  - Manifest:
    `experiments/meta_learning_tool_audit_20260625_013641/artifacts/run_4c7511878785/run_manifest.json`
  - Trace:
    `experiments/meta_learning_tool_audit_20260625_013641/artifacts/run_4c7511878785/agent_trace.jsonl`
  - Taste:
    `experiments/meta_learning_tool_audit_20260625_013641/meta_learning/epoch_001/taste.md`
  - Ledger:
    `experiments/meta_learning_tool_audit_20260625_013641/ledgers/experiment_ledger.jsonl`
  - Runtime sandbox:
    `.runtime/sandboxes/run_4c7511878785/`

Formal run result:
- Pipeline command returned code 0 after about 8.6 minutes.
- Ledger status: `taste_only`.
- Agent session summary:
  - `finish_status=meta_learning_done`
  - `llm_calls=22`
  - `steps_used=1`
  - `context_compactions=0`
  - `context_compaction_calls=0`
- Modification check passed:
  - `allowed_to_backtest=true`
  - no reasons
  - no frozen parent because this was first meta-learning taste-only run
- Trace counts:
  - `llm_call=22`
  - `shell=16`
  - `web_search=3`
  - `tool=1`
  - `session_end=1`
- Token summary from `agent_trace.jsonl`:
  - prompt tokens: 385,881
  - completion tokens: 13,213
  - total tokens: 399,094
  - prompt cache hit tokens: 343,680
  - prompt cache miss tokens: 42,201
- Web search:
  - Tavily `finance_quant_econ`, 5 results, status OK
  - Semantic Scholar `natural_science_engineering`, 5 results, status OK
  - Tavily `philosophy_methodology`, 5 results, status OK
- Shell:
  - 16 shell calls.
  - One `duckdb -c ...` call returned exit 127 because the CLI was unavailable
    in the sandbox; the Agent continued and completed via Python/pandas.
- Taste summary:
  event-driven candidate selection with text evidence as an auxiliary filter,
  combined with rule-based price/volume and event signals; NL should not be
  treated as a primary signal because of forward-looking and leakage risks.

Post-run resource checks:
- `free -h`: about 441 GiB available RAM.
- `nvidia-smi`: existing external GPU 0 process remained; no local training
  job started by this run.
- `docker ps` showed no running sandbox container from this run.

Cleanup:
- Generated Python cache directories under `src/`, `scripts/`, and `tests/`
  were removed after tests/runs where found.

## 2026-06-25 - Shell guard slimming and structured failure hints

Task:
- Reassess whether Shell guard had become too strict now that formal
  experiments use Docker sandboxing rather than local-dev.
- Slim the guard while preserving the experiment contract, and make failed
  tool calls more actionable for the Agent.

Resource checks:
- Before work:
  - `free -h`: about 446 GiB available RAM.
  - `nvidia-smi`: GPU 0 had an existing external Python process using about
    10.5 GiB; no GPU job was started for this change.
- After verification:
  - `free -h`: about 447 GiB available RAM.
  - `nvidia-smi`: same external GPU 0 process remained.

Implementation:
- `src/hl_trader/environment/tools/base.py`
  - Extended `ToolError` with optional structured fields:
    `error_type`, `reason`, `retry_hint`, `blocked_target`, and `details`.
  - Preserved `ToolError("message")` compatibility for existing tools.
- `src/hl_trader/agent/runner.py`
  - Schema errors now include `error_type=schema_error`, `reason`, and a
    retry hint while still returning the original `error` string and
    `tool_spec`.
  - ToolError observations now include structured fields from `ToolError`.
- `src/hl_trader/environment/tools/shell.py`
  - Reframed Shell guard as a light contract guard rather than a full Bash
    parser.
  - Kept the hard policy checks that matter for the experiment contract:
    phase/write lock, explicit forbidden paths, explicit writes to read-only
    roots, writes to unmanaged sandbox paths, ordinary Fold install/download
    commands, output budget, and timeout budget.
  - Removed Python literal write-target regex handling and nested write-target
    recursion for `bash/sh -c` and `find -exec`; those cases are now left to
    Docker read-only mounts, directory permissions, and artifact checks.
  - Fixed the previous global `-i*` write heuristic so read-only commands such
    as `rg -i` and `grep -i` are not misclassified. Only `sed`/`perl`
    in-place flags are treated as write-like.
  - Added structured path/network errors with retry hints, e.g. telling the
    Agent to write scratch files under `/mnt/agent/workspace/...`.
- `src/hl_trader/environment/sandbox.py`
  - Changed the `/mnt/agent` root directory mode to non-writable (`0555`) and
    kept only `/mnt/agent/workspace`, `/mnt/agent/output`, and
    `/mnt/agent/models` writable as documented.
- `tests/unit/test_tools_flow.py`
  - Updated Shell guard tests to match the slimmer contract.
  - Added coverage that `rg -i` is allowed and Shell guard failures returned
    through Runner include `error_type`, `retry_hint`, and `blocked_target`.
- Docs/prompt updates:
  - `src/hl_trader/agent/prompts.py`
  - `configs/prompts/PROMPTS.md`
  - `docs/agent_design.md`
  - `docs/environment_design.md`
  now describe Shell guard as a light contract layer and document structured
  failure fields.

Commands:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/sandbox.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py tests/unit/test_tools_flow.py`
  succeeded.
- Focused regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ShellToolTest tests.unit.test_sandbox_isolation.MetaLearningSessionTest tests.unit.test_pipeline_e2e -v`
  passed, 44 tests.
- Broader tool/sandbox regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_sandbox_isolation -v`
  passed, 75 tests.
- Diff whitespace check:
  `git diff --check -- src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/sandbox.py src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md docs/agent_design.md docs/environment_design.md tests/unit/test_tools_flow.py`
  passed.

Cleanup:
- Removed generated caches:
  `src/hl_trader/environment/tools/__pycache__`,
  `src/hl_trader/environment/__pycache__`,
  `src/hl_trader/agent/__pycache__`,
  `tests/unit/__pycache__`.
- Final cache scan for `src`, `tests`, and `scripts` was clean before the
  logbook edit.

Conclusion:
- Shell guard is now less dependent on fragile Bash/Python source parsing while
  still enforcing the key experiment contract.
- Agent-facing failures are more actionable and include structured retry hints.

2026-06-24 Runtime env package field clarification

Resource checks:
- Before verification:
  - `free -h`: about 446 GiB available RAM.
  - `nvidia-smi`: GPU 0 had an existing external Python process using about
    10.5 GiB; no new GPU workload was started.
- After verification:
  - `free -h`: about 446 GiB available RAM.
  - `nvidia-smi`: same external GPU 0 process remained.

Implementation:
- `src/hl_trader/environment/sandbox.py`
  - Renamed the runtime environment package field from `important_packages` to
    `python_packages`.
  - Renamed the internal package constant to `PYTHON_PACKAGES`.
  - Kept `schema_version=1` to keep this as a lightweight wording/schema-field
    clarification rather than a broader contract migration.
- `tests/unit/test_sandbox_isolation.py`
  - Updated the runtime env contract assertions to use `python_packages`.
- `src/hl_trader/agent/prompts.py`
  - Clarified runtime env wording as Python packages plus CLI tools.
- `configs/prompts/PROMPTS.md`
  - Regenerated from the prompt source.
- `docs/agent_design.md`, `docs/environment_design.md`,
  `docs/pipeline_design.md`
  - Applied matching lightweight terminology updates.

Commands:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/sandbox.py src/hl_trader/agent/prompts.py tests/unit/test_sandbox_isolation.py`
  succeeded.
- Runtime env focused test:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation.SandboxSpecTest.test_runtime_env_artifact_records_local_and_docker_contracts -v`
  passed.
- Diff whitespace check:
  `git diff --check -- src/hl_trader/environment/sandbox.py src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md docs/agent_design.md docs/environment_design.md docs/pipeline_design.md tests/unit/test_sandbox_isolation.py`
  passed.

Cleanup:
- Removed generated caches under `src/hl_trader/environment`,
  `src/hl_trader/agent`, and `tests/unit`.

Conclusion:
- New runtime env artifacts distinguish Python import packages
  (`python_packages`) from executable CLI tools (`tools`), reducing the chance
  that an Agent tries a package name such as `duckdb` as a shell command.

2026-06-24 Backtest engine lightweight slimming

Resource checks:
- Before verification:
  - `free -h`: about 446 GiB available RAM.
  - `nvidia-smi`: GPU 0 had an existing external Python process using about
    10.5 GiB; no new GPU workload was started.
- After verification:
  - `free -h`: about 445 GiB available RAM.
  - `nvidia-smi`: same external GPU 0 process remained.

Implementation:
- `src/hl_trader/environment/backtest_engine.py`
  - Extracted the duplicated sandbox path-guard/open/listdir/scandir bootstrap
    used by the decision-stage strategy driver and minute-policy RPC driver
    into `_STRATEGY_PATH_GUARD`.
  - Removed the old `target_weight` action alias in replay execution; strategy
    actions now use the current `weight` field only.

Commands:
- Static syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/backtest_engine.py`
  succeeded.
- Broker/backtest engine regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine -v`
  passed, 25 tests.
- Tool-flow regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest -v`
  passed, 23 tests.

Cleanup:
- Removed generated Python caches under `src`, `scripts`, and `tests`.

Conclusion:
- The change keeps the external backtest contract unchanged while trimming a
  stale compatibility alias and reducing duplicated sandbox bootstrap code.

2026-06-24 Meta-learning rerun audit trace

Resource checks:
- Before run:
  - `free -h`: about 447 GiB available RAM.
  - `nvidia-smi`: GPU 0 had an existing external Python process using about
    10.5 GiB; no new training workload was started.
- After run:
  - `free -h`: about 445 GiB available RAM.
  - `nvidia-smi`: same external GPU 0 process remained.

Run:
- Ran a real Docker meta-learning-only Fold by constructing the standard
  `ExperimentPipeline` components and calling `run_meta_learning()` directly,
  avoiding a full Fold/held-out experiment.
- Config:
  - experiment_id: `meta_learning_rerun_20260625_0238`
  - run_id: `run_1b509f529ccf`
  - fold_period: `quarter`
  - first/last test period: `2022Q1`
  - held-out period fields for config validity: `2022Q2`
  - visible Fold: first quarterly Fold
  - history window: 21 months
  - intraday visible window: 5 trading days
  - meta-learning Docker network: `bridge`
  - main model: `deepseek-v4-pro`
  - reasoning_effort: `max`
  - compact model: `deepseek-v4-flash`
  - compact threshold: 200000 estimated tokens
  - web_search engines: `tavily`, `semantic_scholar`

Command:
- The Python one-off runner was executed with:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python`
  and stdout/stderr tee'd to
  `logs/meta_learning_rerun_20260625_0238.log`.

Artifacts:
- Ledger:
  `experiments/meta_learning_rerun_20260625_0238/ledgers/experiment_ledger.jsonl`
- Run manifest:
  `experiments/meta_learning_rerun_20260625_0238/artifacts/run_1b509f529ccf/run_manifest.json`
- Runtime env:
  `experiments/meta_learning_rerun_20260625_0238/artifacts/run_1b509f529ccf/runtime_env.json`
- Canonical trace:
  `experiments/meta_learning_rerun_20260625_0238/artifacts/run_1b509f529ccf/agent_trace.jsonl`
- Taste:
  `experiments/meta_learning_rerun_20260625_0238/meta_learning/epoch_001/taste.md`
- Audit digest:
  `check.md`

Result:
- `finish_status=meta_learning_done`
- `status=taste_only`
- `taste_chars=1538`
- Trace events:
  - 24 `llm_call`
  - 16 `shell`
  - 3 `web_search`
  - 1 `glob`
  - 1 `tool`
  - 1 `session_end`
- Context compact did not trigger.
- Trace usage totals from recorded provider usage:
  prompt tokens 536410, completion tokens 10688, total tokens 547098.
- Notable trace issues:
  - Two main LLM calls failed with `DeepSeek response content is not valid JSON`;
    Runner continued and the session completed.
  - One DuckDB Python query referenced missing `text_index.trade_date`; the
    Agent corrected it by inspecting columns and querying `available_at`.

Report:
- Overwrote `check.md` with only this run's dialogue-style audit summary,
  including Agent actions, tool observations, final Taste, and audit notes.

Conclusion:
- The meta-learning flow completed in a real Docker sandbox with the standard
  experimental settings and produced a new Taste without modifying output or
  models.

2026-06-24 Meta-learning JSON retry hint and overfit boundary fix

Analysis:
- The two `LLMProxyError: deepseek request failed: DeepSeek response content is
  not valid JSON` events in
  `experiments/meta_learning_rerun_20260625_0238/artifacts/run_1b509f529ccf/agent_trace.jsonl`
  were not HTTP/network/provider availability failures.
- The provider conversation log showed HTTP 200 responses whose content was an
  action-like JSON object, but the `command` string embedded multiline
  `python -c` code and nested quotes without valid JSON escaping. The client
  therefore rejected the response before the Runner could parse the action.
- Runner appended `invalid_action` observations and the model recovered on the
  following call by using a heredoc.
- The final Taste also expressed an incorrect anti-overfit principle by saying
  not to inspect the validation period. In this project, validation replay and
  validation results are development feedback. They may be used for review and
  model selection, while test and held-out remain invisible.

Implementation:
- `src/hl_trader/agent/runner.py`
  - Added a concrete retry hint to `invalid_action` observations: return
    exactly one valid JSON object, and use heredoc or correct escaping for
    multiline Python/shell commands.
- `src/hl_trader/agent/prompts.py`
  - Extended the Fold anti-overfit prompt to clarify that validation results
    are development feedback, while test and held-out are invisible.
  - Extended the meta-learning Taste contract to distinguish training input,
    validation feedback, test, and held-out.
- `configs/prompts/PROMPTS.md`
  - Regenerated from the prompt source.

Commands:
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Static syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/runner.py src/hl_trader/agent/prompts.py`
  succeeded.
- Focused regression:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest.test_main_llm_error_trace_redacts_bearer_token tests.unit.test_sandbox_isolation.MetaLearningSessionTest.test_meta_learning_prompt_describes_default_network_without_secret_values -v`
  passed, 2 tests.

Conclusion:
- The JSON action failure is now more actionable for the model.
- Future Taste output should no longer describe validation feedback as
  forbidden data; the boundary is decision input vs validation feedback vs
  test/held-out.

2026-06-25 Meta Learning rerun after Claude prompt/docs update

Task:
- Re-run one formal meta-learning Fold after Claude optimized documentation and
  prompt text.

Resource checks:
- Before run:
  - `free -h`: about 368 GiB available RAM.
  - `nvidia-smi`: multiple GPUs were already occupied by external processes;
    this run did not start local training.
- After run:
  - `free -h`: about 339 GiB available RAM.
  - GPU load remained attributable to pre-existing external processes.

Run:
- Ran a real Docker meta-learning-only Fold by constructing standard pipeline
  components and calling `ExperimentPipeline.run_meta_learning()` directly.
- This intentionally avoided ordinary Fold replay and held-out evaluation; the
  goal was to audit the meta-learning flow and generated Taste only.

Config:
- experiment_id: `meta_learning_after_claude_20260625_1113`
- run_id: `run_2bdfdf1a4375`
- fold_period: `quarter`
- first/last test period: `2022Q1`
- held-out config fields: `2022Q2`
- visible Fold: first quarterly Fold
- history window: 21 months
- intraday visible window: 5 trading days
- meta-learning Docker network: `bridge`
- main model: `deepseek-v4-pro`
- reasoning_effort: `max`
- compact model: `deepseek-v4-flash`
- compact threshold: 200000 estimated tokens
- web_search engines: `tavily`, `semantic_scholar`

Command:
- The Python one-off runner used:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python`
  with stdout/stderr saved to
  `logs/meta_learning_after_claude_20260625_1113.log`.

Artifacts:
- Ledger:
  `experiments/meta_learning_after_claude_20260625_1113/ledgers/experiment_ledger.jsonl`
- Run manifest:
  `experiments/meta_learning_after_claude_20260625_1113/artifacts/run_2bdfdf1a4375/run_manifest.json`
- Runtime env:
  `experiments/meta_learning_after_claude_20260625_1113/artifacts/run_2bdfdf1a4375/runtime_env.json`
- Canonical trace:
  `experiments/meta_learning_after_claude_20260625_1113/artifacts/run_2bdfdf1a4375/agent_trace.jsonl`
- Taste:
  `experiments/meta_learning_after_claude_20260625_1113/meta_learning/epoch_001/taste.md`

Result:
- `finish_status=meta_learning_done`
- `status=taste_only`
- `taste_chars=1627`
- `modification_check.allowed_to_backtest=true`
- Strategy artifact hash stayed at
  `sha256:349964db2cc6bc9c7445bd9c4f018fe59183e669b803b6f81e14761c20add92d`.
- Model artifact hash was empty:
  `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Trace summary:
- 37 `llm_call`
- 28 `shell`
- 6 `web_search`
- 7 `context_summary`
- 1 `tool`
- 1 `session_end`
- Context compact did not trigger:
  `context_compactions=0`, `context_compaction_calls=0`.
- Recorded provider usage totals:
  prompt tokens 747450, completion tokens 22573, total tokens 770023.
- Two `llm_call` events had no usage because the provider response content was
  not valid JSON.

Notable non-fatal issues:
- Two DeepSeek calls failed with
  `LLMProxyError: deepseek request failed: DeepSeek response content is not valid JSON`;
  the Runner retried and the session completed.
- One web search returned `empty_results`.
- The Agent attempted a few overly large or incorrect exploratory data queries
  while inspecting parquet/DuckDB data, then continued with narrower checks.
- The generated Taste improved the validation/test boundary, but still used
  external world knowledge about the 2022Q1 down market and described expected
  test-period conditions. This is a residual leakage risk at the prompt level:
  calendar labels should not permit inferring or discussing realized hidden
  test/held-out market outcomes.

Verification:
- Sensitive scan across the new run log, trace, manifest, and runtime env found
  no raw GitHub/HuggingFace/proxy/API-key patterns.
- Source/test/script `__pycache__` directories created during inspection were
  removed.
- `git diff --check` is run separately after the logbook update.

Conclusion:
- The post-Claude meta-learning rerun completed successfully in a real Docker
  sandbox and produced a new Taste.
- The main remaining design issue is prompt-level: explicitly forbid use of
  model-internal historical knowledge about hidden test or held-out outcomes,
  even when the date range is visible as scheduling metadata.

2026-06-25 Meta Learning no-lookahead prompt hardening and check report

Task:
- Add an explicit no-lookahead rule to the meta-learning prompt prohibitions.
- Rewrite `check.md` as a dialogue-style audit summary for the latest
  `meta_learning_after_claude_20260625_1113` run.

Implementation:
- `src/hl_trader/agent/prompts.py`
  - Added a `## 禁止事项` bullet: meta-learning must not use model-internal
    historical knowledge, public search results, or date labels to infer hidden
    test/held-out realized market returns, sector rotation, or stock
    performance. Date ranges are experiment scheduling metadata, not available
    trading evidence.
- `configs/prompts/PROMPTS.md`
  - Regenerated from source via `scripts/dev/export_prompts.py`.
- `tests/unit/test_sandbox_isolation.py`
  - Updated one stale assertion from the older mixed-language network phrase to
    the current Chinese prompt wording.
- `check.md`
  - Rewritten with the latest run's experiment parameters, artifact paths,
    dialogue-style tool flow, non-fatal failures, token stats, Taste summary,
    and audit conclusion.

Validation commands:
- Resource checks:
  - Before script/test work: `free -h`, `nvidia-smi`.
  - After script/test work: `free -h`, `nvidia-smi`.
- Prompt export:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
  succeeded.
- Syntax check:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py tests/unit/test_sandbox_isolation.py`
  succeeded.
- Focused tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation.MetaLearningSessionTest -v`
  passed, 15 tests.
- Diff whitespace check:
  `git diff --check -- src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md tests/unit/test_sandbox_isolation.py check.md LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md`
  succeeded.

Resource notes:
- RAM stayed healthy, about 339 GiB available before the work and about 337 GiB
  after the focused tests.
- GPUs remained heavily occupied by pre-existing external processes; this task
  did not start GPU training.

Conclusion:
- The meta-learning prompt now directly blocks the exact forward-looking
  failure mode seen in the latest Taste.
- `check.md` is ready for manual audit of the run process and result.

2026-06-25 Meta Learning rerun after no-lookahead hardening

Task:
- Re-run one real Docker meta-learning Fold after adding the explicit
  no-lookahead prohibition to the meta-learning prompt.

Resource checks:
- Before run:
  - `pwd -P`: `/Data/lzp/MacroQuant`.
  - `free -h`: about 417 GiB available RAM.
  - `nvidia-smi`: several GPUs were already occupied by external processes;
    this run did not start local model training.
  - `find src tests scripts -name __pycache__`: empty.
  - Docker image:
    `macroquant-sandbox:latest`
    `sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`.
- After run:
  - `free -h`: about 339 GiB available RAM.
  - `nvidia-smi`: GPU occupancy remained attributable to external processes.
  - No running Docker container for `run_8caa7f451792`.

Run:
- Used a direct `ExperimentPipeline.run_meta_learning()` one-off, not the full
  experiment CLI, so no ordinary Fold replay or held-out evaluation was run.
- The wrapper used:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python`.
- Host log:
  `logs/meta_learning_no_lookahead_20260625_1148.log`.
- The Python wrapper printed a successful JSON result, but the outer process
  returned exit code 120 because the temporary stdout/stderr Tee object was
  still referenced at interpreter shutdown. Ledger, manifest, trace and Taste
  all show the pipeline run itself completed successfully.

Config:
- experiment_id: `meta_learning_no_lookahead_20260625_1148`
- run_id: `run_8caa7f451792`
- fold_period: `quarter`
- first/last test period: `2022Q1`
- held-out config fields: `2022Q2`
- visible Fold: `fold_2022Q1`
- visible decision input: `2020-01-01..2021-09-30`
- validation replay: `2021-10-01..2021-12-31`
- hidden test period: `2022-01-01..2022-03-31`
- history window: 21 months
- intraday visible window: 5 trading days
- meta-learning Docker network: `bridge`
- main model: `deepseek-v4-pro`
- reasoning_effort: `max`
- NL model: `deepseek-v4-flash`
- compact model: `deepseek-v4-flash`
- compact threshold: 200000 estimated tokens
- web_search engines: `tavily`, `semantic_scholar`

Artifacts:
- Ledger:
  `experiments/meta_learning_no_lookahead_20260625_1148/ledgers/experiment_ledger.jsonl`
- Run manifest:
  `experiments/meta_learning_no_lookahead_20260625_1148/artifacts/run_8caa7f451792/run_manifest.json`
- Runtime env:
  `experiments/meta_learning_no_lookahead_20260625_1148/artifacts/run_8caa7f451792/runtime_env.json`
- Canonical trace:
  `experiments/meta_learning_no_lookahead_20260625_1148/artifacts/run_8caa7f451792/agent_trace.jsonl`
- Taste:
  `experiments/meta_learning_no_lookahead_20260625_1148/meta_learning/epoch_001/taste.md`
- Runtime sandbox retained for audit:
  `.runtime/sandboxes/run_8caa7f451792/` (~3.9 GiB).

Result:
- `finish_status=meta_learning_done`
- `status=taste_only`
- `taste_chars=2429`
- `modification_check.allowed_to_backtest=true`
- Strategy artifact hash stayed at
  `sha256:349964db2cc6bc9c7445bd9c4f018fe59183e669b803b6f81e14761c20add92d`.
- Model artifact hash was empty:
  `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Trace summary:
- 40 `llm_call`
- 24 `shell`
- 5 `web_search`
- 11 `context_summary`
- 2 `tool`
- 1 `session_end`
- Context compact did not trigger:
  `context_compactions=0`, `context_compaction_calls=0`.
- Recorded provider usage totals:
  prompt tokens 990218, completion tokens 21108, total tokens 1011326.
- One LLM event had no usage because the provider response content was empty.
- Web search calls:
  - Tavily finance/quant/econ: 5 results.
  - Semantic Scholar finance/quant/econ: 5 results.
  - Semantic Scholar natural science/engineering: empty results.
  - Tavily natural science/engineering: 5 results.
  - Tavily philosophy/methodology: 5 results.

Notable non-fatal issues:
- One pyarrow schema probe failed because the code referenced a missing
  `num_columns` attribute.
- One pandas query failed due to a missing column access.
- One shell data inspection command timed out after 120 seconds.
- One DeepSeek call failed with empty content; the Runner recovered.
- One Semantic Scholar query returned empty results; Agent retried the
  perspective with Tavily and succeeded.

No-lookahead audit:
- The generated Taste no longer states hidden test-period realized market
  direction, index return, sector rotation, or stock performance.
- It still contains an imprecise period-label sentence:
  `禁止将验证期（2022Q1）或测试期（2022Q2）的具体股票、行业、月份特征等硬编码进策略`.
- Manifest shows the correct boundary for this run:
  validation replay is `20211001..20211231`, hidden test is
  `20220101..20220331`, and `2022Q2` is only the held-out config field.
  This is not the same leak as before, but it indicates the prompt should make
  the relationship between Fold label, validation replay, test period and
  held-out config even clearer.

Verification:
- Sensitive scan across log, trace, manifest and runtime env found no raw
  GitHub/HuggingFace/proxy/API key patterns.
- `find src tests scripts -name __pycache__` remained empty after the run.

Conclusion:
- The no-lookahead hardening improved the main failure mode: the new Taste did
  not discuss hidden test-period realized outcomes.
- Remaining issue is terminology/period labeling, not realized outcome leakage.

2026-06-25 Meta Learning runtime analysis report

Task:
- Analyze why `meta_learning_no_lookahead_20260625_1148` took much longer than
  expected and overwrite `check.md` with a process/result report.

Inputs:
- Trace:
  `experiments/meta_learning_no_lookahead_20260625_1148/artifacts/run_8caa7f451792/agent_trace.jsonl`
- Runtime sandbox:
  `.runtime/sandboxes/run_8caa7f451792/`
- Taste:
  `experiments/meta_learning_no_lookahead_20260625_1148/meta_learning/epoch_001/taste.md`
- Host log:
  `logs/meta_learning_no_lookahead_20260625_1148.log`

Findings:
- Outer command wall time was about 2958 seconds, about 49 minutes.
- Agent trace elapsed time was only 508.6 seconds, about 8.5 minutes.
- The dominant cost happened before manifest/trace/Agent startup:
  `run_meta_learning()` synchronously built the visible decision snapshot and
  validation replay before writing development history and starting the Agent.
- Approximate pre-Agent timeline from runtime file mtimes:
  - runtime env prepared at 11:49:53.
  - decision daily completed at 11:58:28, about +515 seconds.
  - decision intraday completed at 12:01:30, about +182 seconds.
  - decision fundamentals completed at 12:18:38, about +1028 seconds.
  - decision events completed at 12:21:59, about +201 seconds.
  - decision text_index completed at 12:23:28, about +89 seconds.
  - valid replay intraday completed at 12:30:35, about +391 seconds.
  - Agent trace then ran from 12:30:39 to 12:39:07.
- Data volume was large:
  - decision `events.parquet`: 8.69M rows, about 239 MB.
  - decision `text_index.parquet`: 3.55M rows, about 369 MB.
  - decision `text_library/major_news.parquet`: about 1.3 GB.
  - valid replay `intraday_1min.parquet`: 66.8M rows, about 970 MB.
  - runtime sandbox total: about 3.9 GB.
- Agent-stage costs were secondary:
  - LLM calls: 40, cumulative 354.8 seconds.
  - Shell calls: 24, cumulative 130.5 seconds, including one 120-second
    timeout from full-reading `events.parquet`.
  - Web search: 5 calls, cumulative 22.6 seconds.
  - Total recorded tokens: 1,011,326.

Report:
- Overwrote `check.md` with:
  - runtime split,
  - detailed timeline,
  - data volume table,
  - code-path explanation,
  - Agent-stage call analysis,
  - no-lookahead result,
  - prioritized optimization suggestions.

Recommended follow-ups:
- Add deterministic snapshot/replay caching keyed by decision/replay period,
  `SnapshotConfig`, source data manifests/hashes and fundamental event status.
- Add domain-level timing logs to `build_decision_snapshot()` and
  `build_replay_slot()`.
- Consider a lighter meta-learning validation replay view if full quarter
  minute replay is not needed for Taste generation.
- Pre-generate a data summary artifact so Agent can inspect schema/rows/nulls
  without repeatedly scanning large parquet files.
- Encourage DuckDB/Parquet metadata probes for large files instead of full
  `pd.read_parquet()` loads.

Conclusion:
- The long runtime was a normal performance bottleneck in snapshot/replay
  preparation, not an Agent hang, Web Search problem, or DeepSeek-only issue.

2026-06-25 Data build summary and dynamic data facts

Task:
- Inspect data construction bottlenecks from the latest meta-learning runtime
  and implement a first optimization plus Agent-visible data summaries.
- Clarify that future data changes should not require editing static Prompt
  data facts.

Changes:
- `src/hl_trader/environment/features/fundamental_events.py`
  - Added optional `min_available_at` to `read_fundamental_events()`.
  - Decision snapshot construction now pushes the fundamentals window down to
    `available_month` partition selection and still applies an `available_at`
    row filter afterward.
- `src/hl_trader/environment/snapshot.py`
  - Added lightweight `build_profile` and `data_profile` entries to decision
    and replay manifests.
  - Profiles record rows, columns, size, selected date ranges, key null counts,
    and build/write timing without changing parquet contents or snapshot hash
    semantics.
- `src/hl_trader/environment/data_summary.py`
  - New Agent-visible summary writer based on snapshot manifests and Parquet
    metadata, not full-table reads.
  - Summaries include only the views visible to the current Agent run and add
    large-table guidance for events, text index, and minute bars.
- `src/hl_trader/pipelines/experiment.py`
  - Fold runs now write `/mnt/artifacts/data_summary.json` for snapshot/train/
    valid only; test remains omitted.
  - Meta-learning runs write the same visible first-Fold views.
  - Run manifests include `data_summary_ref=/mnt/artifacts/data_summary.json`.
- `src/hl_trader/environment/runtime.py`
  - Added `SandboxPaths.data_summary` and included `data_summary.json` in
    collected trusted artifacts.
- Prompt/tool/docs:
  - Fold and meta-learning Prompt, Runner initial messages, Shell tool
    description, and living docs now tell Agent to read `data_summary.json`
    before expensive probes.
  - Prompts explicitly state that Prompt text is a stable protocol, not current
    data facts. Current fields, rows, coverage, and snapshot hashes are bound to
    each run's dynamically generated data summary and manifests.
  - Large tables should be inspected with DuckDB `count(*)`/`limit`, Parquet
    metadata, column-select reads, or date filters; unknown large tables should
    not be fully loaded with pandas.

Design conclusion:
- Future data changes do not require static Prompt edits. A new Fold/meta run
  rebuilds snapshots and writes a fresh `data_summary.json` from the actual
  snapshot/replay parquet metadata.
- The current run remains frozen: if raw data changes after the run starts, the
  already-generated snapshot and summary do not mutate underneath the Agent.
- If snapshot/replay caching is added later, the cache key must include
  SnapshotConfig, decision/replay period, source-data manifests/hashes, and
  `fundamental_events_status.json` so stale summaries cannot be reused.

Resource checks:
- Before tests: system memory about 338 GiB available; GPUs were heavily used
  by unrelated processes, but this work was CPU-only.
- After tests: system memory still about 338 GiB available; no GPU workload was
  started by this task.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/data_summary.py src/hl_trader/environment/snapshot.py src/hl_trader/environment/features/fundamental_events.py src/hl_trader/pipelines/experiment.py src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py src/hl_trader/environment/runtime.py src/hl_trader/environment/tools/shell.py`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_snapshot_builder -v`
  - 10 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_single_epoch_runs_meta_learning_before_fold_and_heldout -v`
  - 1 test OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation.MetaLearningSessionTest.test_meta_learning_prompt_describes_default_network_without_secret_values tests.unit.test_sandbox_isolation.MetaLearningSessionTest.test_meta_learning_network_policy_is_inside_environment_section -v`
  - 2 tests OK.
- Combined rerun of the above focused suite: 13 tests OK.
- After dynamic-data-fact Prompt clarification: prompt export OK, prompt
  `py_compile` OK, 2 meta-learning prompt tests OK.
- `git diff --check` OK.
- Generated `__pycache__` directories were removed.

2026-06-25 Meta Learning rerun with data summary
================================================

Task:
- Rerun one real meta-learning Fold so the current data-summary workflow and
  Prompt/tool behavior can be audited.

Run configuration:
- Experiment id: `meta_learning_data_summary_20260625_1343`.
- Run id: `run_89f2ee1f54e4`.
- Entry: direct `ExperimentPipeline.run_meta_learning(epoch_id="epoch_001",
  parent=None, visible_fold=fold_2022Q1)` from the `quant` Python
  environment.
- Mode: Docker sandbox, meta-learning only, taste-only output.
- Agent model: DeepSeek `deepseek-v4-pro`, `reasoning_effort=max`.
- NL/context compact model: `deepseek-v4-flash`; compact threshold 200000
  tokens.
- Web search engines: Tavily and Semantic Scholar.
- Walk-forward config: quarterly Fold, first/last test period `2022Q1`; config
  includes held-out `2022Q2` only as the held-out boundary.
- Visible Fold:
  - input/train window: `20200101..20210930`;
  - validation window: `20211001..20211231`;
  - hidden test window: `20220101..20220331`;
  - valid decision time: `2021-10-08T09:25:00+08:00`.
- Data windows: default historical window 21 months; minute replay window 5
  trading days.

Resource checks:
- Before run: system memory about 338 GiB available. GPUs were occupied by
  unrelated external processes; this run did not start local GPU training.
- After run: system memory still about 338 GiB available. Docker sandbox
  container exited; `docker ps` was empty.

Key artifacts:
- Runtime log:
  `logs/meta_learning_data_summary_20260625_1343.log`.
- Run manifest:
  `experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/run_manifest.json`.
- Agent trace:
  `experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/agent_trace.jsonl`.
- Data summary:
  `experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/data_summary.json`.
- Taste:
  `experiments/meta_learning_data_summary_20260625_1343/meta_learning/epoch_001/taste.md`.
- Ledger:
  `experiments/meta_learning_data_summary_20260625_1343/ledgers/experiment_ledger.jsonl`.
- Runtime sandbox:
  `.runtime/sandboxes/run_89f2ee1f54e4`.

Result:
- Ledger status: `taste_only`.
- Finish status: `meta_learning_done`.
- Taste length: about 2110 chars.
- No frozen strategy artifact was created because this was a parentless
  meta-learning-only run.
- Modification check passed for the meta-learning output.

Agent trace summary:
- LLM calls: 25.
- Shell calls: 14.
- Web search calls: 4.
- Context compactions: 0.
- Token usage recorded in trace:
  - prompt tokens: 646216;
  - completion tokens: 12704;
  - total tokens: 658920;
  - prompt cache hit tokens: 561792;
  - prompt cache miss tokens: 84424.
- Non-fatal LLM errors:
  - 4 DeepSeek responses were not valid JSON;
  - 1 response stopped with `finish_reason=length`.
- The runner recovered after these errors and completed the run.
- Shell failures: none.
- Web search calls:
  - Tavily finance/quant/econ search returned results;
  - Semantic Scholar natural-science/engineering search returned results;
  - Tavily philosophy/methodology search returned results;
  - one finance search was repeated by the Agent.

Data summary/profile observations:
- `data_summary.json` includes only visible views: `snapshot`, `train`, and
  `valid`; no hidden test or held-out view is exposed.
- Decision snapshot build total: about 188 seconds.
  - daily build/write: about 15.2s / 2.2s;
  - intraday build/write: about 8.4s / 4.7s;
  - fundamentals build/write: about 25.8s / 4.0s;
  - events build/write: about 28.1s / 17.7s;
  - macro build/write: about 0.3s / 0.0s;
  - text index build/write: about 68.8s / 4.5s.
- Valid replay build total: about 554 seconds.
  - valid `intraday_1min.parquet` remains the main bottleneck, with build
    about 452s and write about 43s.
- The fundamentals window pushdown optimization was effective: the previous
  slow decision fundamentals build was reduced to roughly 30 seconds including
  write.

Runtime observations:
- Agent read `/mnt/artifacts/data_summary.json` at the start and used DuckDB
  for parquet inspection.
- One non-fatal pandas `FutureWarning` appeared in
  `snapshot.py:_read_dataset_window` during concatenation of empty/all-NA
  frames. The run completed, but the warning is worth cleaning up later.
- Experiment artifact directory size was about 3.8 MiB; runtime sandbox size
  was about 3.9 GiB.

Cleanup and verification:
- Generated Python caches under `src`, `scripts`, and `tests` were checked and
  none remained after the run.
- `git diff --check` was rerun after logbook updates and passed.

2026-06-25 Meta Learning prompt and data summary slimming
=========================================================

Task:
- Remove Taste contract clauses that encouraged the meta-learning Agent to
  restate a specific Fold period/window.
- Reduce Agent-visible `data_summary.json` so it is a compact data index, not a
  large runtime/profile artifact.

Changes:
- `src/hl_trader/agent/prompts.py`
  - Removed two Taste output contract bullets:
    - why the direction fits the current Fold period/window/trading frequency;
    - a long train/valid/test/held-out boundary explanation inside the Taste
      output contract.
  - Kept the actual no-lookahead and hidden-test constraints in `禁止事项`;
    this keeps the guardrail without asking Taste to narrate specific Fold
    dates or windows.
  - Updated data summary wording: it is now a lightweight index; full schema is
    inspected on demand through snapshot manifest, Parquet metadata or DuckDB.
- `src/hl_trader/environment/data_summary.py`
  - Removed Agent-visible `build_profile`.
  - Removed full per-file `columns`; each file now exposes `column_count` and
    `key_columns`.
  - Replaced duplicated full `large_files` entries with `large_tables`, a list
    of large table paths.
  - Kept rows, row groups, file size, key metadata null counts, date ranges and
    recommended large-table access hints.
- Docs and generated prompt snapshot:
  - Updated `docs/agent_design.md`, `docs/environment_design.md`,
    `docs/pipeline_design.md`, and `configs/prompts/PROMPTS.md`.
  - Clarified that snapshot manifests may retain build/profile information for
    host-side audit, while Agent-visible `data_summary.json` does not expose
    build timing.
- Tests:
  - Added assertions that meta-learning `data_summary.json` has no
    `schema_version`, omits `build_profile`, exposes `key_columns`, and does not expose full
    `columns`.

Audit note:
- The historical artifact
  `.runtime/sandboxes/run_89f2ee1f54e4/artifacts/data_summary.json` and the
  corresponding collected experiment artifact were not rewritten. That run's
  Agent actually saw the old larger summary, so mutating it would make the
  audit trace inconsistent.
- A temporary regenerated sample from the same snapshot showed the new summary
  size would be about 37,684 bytes versus the old 130,533 bytes.

Resource checks:
- Before tests: system memory about 337 GiB available. GPUs were occupied by
  unrelated external processes; this work did not start local GPU training.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/data_summary.py src/hl_trader/agent/prompts.py`
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_single_epoch_runs_meta_learning_before_fold_and_heldout -v`
  - 1 test OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_sandbox_isolation.MetaLearningSessionTest.test_meta_learning_prompt_describes_default_network_without_secret_values tests.unit.test_sandbox_isolation.MetaLearningSessionTest.test_meta_learning_network_policy_is_inside_environment_section -v`
  - 2 tests OK.

## 2026-06-25 Migrate Agent loop to DeepSeek V4 native tool calling

Task: replace the single-JSON-action-per-turn text protocol with provider-native function calling (the "Claude Code tool invocation approach"), motivated by `.runtime/sandboxes/run_89f2ee1f54e4/artifacts/agent_trace.jsonl` (25 LLM calls / 19 tools; 4 of 5 failures = "response content is not valid JSON", 1 = finish_reason=length; context 7K->185K chars; strictly serial).

Repository/path: physical root `/Data/lzp/MacroQuant`; env `~/miniconda3/envs/quant` (py3.11).

Spike (live DeepSeek, key from ignored `.env`, never printed):
- `deepseek-v4-pro` and `deepseek-v4-flash` both return native `tool_calls` (`finish_reason=tool_calls`); flash emitted 2 parallel tool_calls in one turn; `reasoning_content` present (reasoning_tokens 141-181) alongside tool_calls under `thinking.enabled` + `reasoning_effort=max`; `content` empty when tool_calls present; `tool` role + `tool_call_id` round-trip accepted. All three gates pass.

Changes (code is source of truth; PROMPTS.md is generated):
- `src/hl_trader/environment/tools/base.py`: `ActionField.to_json_schema()`, `ActionSpec.to_tool_schema()` (function name = `action`).
- `src/hl_trader/environment/llm/deepseek.py`: `DeepSeekResponse.tool_calls`; `chat_tools()` + `_tools_payload()` + module `_tool_message_record()`; `_parse_response` keeps tool_calls and allows empty content when tool_calls present (finish_reason=length still fails).
- `src/hl_trader/environment/llm/proxy.py`: `ProviderResponse.tool_calls`; `LLMProxy.complete_tools()` (DeepSeekProxy + ScriptedLLM); test helpers `tool_call()` / `tool_call_response()`.
- `src/hl_trader/agent/runner.py`: `_tool_schemas()` (mode-filtered), `_next_turn()` (calls `complete_tools`), `_dispatch_tool_calls()` (all calls per turn; concurrency-safe-only batches via ThreadPoolExecutor, else sequential), `_parse_tool_arguments()`; `run()` appends one `assistant` tool_calls msg + one `tool` result per call; `_drop_leading_orphan_tools()` guard in `_trim`; removed JSON-action extraction.
- `src/hl_trader/agent/compact.py`: token estimate counts `tool_calls`; retained tail guarded by `_drop_leading_orphan_tools`.
- `src/hl_trader/agent/prompts.py`: Fold + meta-learning action protocol rewritten for native tool calling + parallel read-only; `configs/prompts/PROMPTS.md` regenerated (deterministic, in sync).
- Living docs: `docs/environment_design.md` 5.1 and `docs/agent_design.md` 3.2 note native function calling + parallel batching + tool-group integrity.
- Tests: `tests/unit/test_tools_flow.py` and `tests/unit/test_sandbox_isolation.py` scripted actions converted to `tool_call_response(tool_call(...))`; llm_call trace assertion `raw_content`->`content`. `test_pipeline_e2e.py` unaffected (ScriptedFoldAgent/IdleAgent callables, not the LLM loop).

Validation:
- `PYTHONPATH=src python -m unittest discover -t . -s tests -p "test_*.py"` -> Ran 251 tests, OK.
- Live integration via refactored `DeepSeekProxy.complete_tools` + `ActionSpec.to_tool_schema()` (deepseek-v4-pro): one turn returned 2 parallel tool_calls (grep + shell), reasoning present, content empty; round 2 (assistant tool_calls echo + tool results) continued -> INTEGRATION_OK.
- Temp spike/integration scripts removed; `git diff --check` clean. Memory ~ unchanged; GPU was pre-existing external load, no training launched this session.

## 2026-06-25 Six Claude-Code-style agent optimizations (Tier 1+2)

Task: implement the six Tier 1/2 items identified from the Claude Code design survey, fitted to actual project conditions (DeepSeek V4 provider, native tool loop, PIT/audit discipline).

Repository/path: physical root `/Data/lzp/MacroQuant`; env `~/miniconda3/envs/quant` (py3.11).

Changes:
- #1 Token/cache accounting (`src/hl_trader/agent/runner.py`): `_accumulate_usage()` sums prompt/completion/total/reasoning and cache hit/miss (DeepSeek `prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens`); `token_usage` (with `cache_hit_ratio`) added to the session summary. Documented that `_trim`/compaction reset the cached prefix.
- #2 Read-only Explore sub-agent (`src/hl_trader/environment/explore.py`): `ExploreSubAgentEngine` runs a bounded native-tool loop over read-only `shell`/`grep`/`glob` on a cheaper proxy, returns a compact digest, traces `explore` + `explore_llm_call`. Exposed as the `explore(task, max_rounds?)` tool (read-only, decision-phase, both modes); runner gains `explore_proxy` (falls back to main proxy). CLI `run_experiment.py` wires `explore_proxy = nl_proxy` (flash) into both runner constructions + `llm_config_summary`.
- #3 write_file/edit_file (`src/hl_trader/environment/tools/artifact_io.py`): `ArtifactIOTool` writes host-side into `workspace`/`output`/`models`; `edit_file` requires a unique `old_string` (staleness) or `replace_all`; rejects `..`/hidden/escape, `output/README.md`, and writes after `write_lock`. Wired into runner specs + dispatch.
- #4 NL Sub Agent native tools (`src/hl_trader/environment/nl/engine.py`): `complete_tools` + `TEXT_RETRIEVE_SCHEMA` replace the text-JSON extraction; final-after-budget call uses `tool_choice="none"`. Removed the embedded-JSON parsers; system prompt rewritten.
- #5 Streaming (`src/hl_trader/environment/llm/deepseek.py`): `stream_tool_calls` config (default on); `_tools_payload(stream=...)` adds `stream_options.include_usage`; `_post_json` reconstructs the completion via `_read_sse_chunks` + `_merge_stream_chunks` (reassembles split tool_call argument fragments by index). `_config_kwargs` carries the flag.
- #6 Context editing (`src/hl_trader/agent/runner.py`): `_clear_stale_tool_results()` replaces oversized old `tool` bodies with a stub (keeps `tool_call_id`), keeping the most recent N; emits `context_edit`. Config `clear_tool_results`/`tool_result_keep_recent`/`tool_result_clear_min_chars`. Runs before `_trim`.
- Prompts: Fold + meta-learning action protocols list `write_file`/`edit_file`/`explore`; `configs/prompts/PROMPTS.md` regenerated (in sync). Living docs `docs/environment_design.md` 5.1 and `docs/agent_design.md` 3.1 updated.
- Tests: `tests/unit/test_tools_flow.py` adds `ArtifactIOToolTest` (4) + explore dispatch tests (2); `tests/unit/test_nl_scoring.py` converted to native tool calls.

Validation:
- `PYTHONPATH=src python -m unittest discover -t . -s tests -p "test_*.py"` -> Ran 257 tests, OK.
- Live: SSE merge unit (split tool_call args reassembled) + a real streamed `deepseek-v4-pro` tools turn (tool_calls + reasoning + cache-token usage) -> STREAM_LIVE_OK. NL native loop covered by unit tests.
- py_compile OK; PROMPTS.md in sync; `git diff --check` clean; temp scripts removed. GPU pre-existing external load; no training launched.

## 2026-06-25 Explore Shell hardening and secret-redaction audit fixes

Task: answer whether Explore Shell should be less restrictive by comparing with `external_references/claude-code-main`, then fix the audit findings without importing Claude Code's full Bash runtime.

Repository/path: physical root `/Data/lzp/MacroQuant`; env `~/miniconda3/envs/quant` (py3.11).

Claude Code reference points used:
- `src/tools/BashTool/BashTool.tsx`: separates search/read/list command classification from permission and side-effect handling.
- `src/tools/BashTool/readOnlyValidation.ts`: read-only mode is not "unrestricted"; it uses allowlists plus command/flag validation for commands such as git, find, sed, sort and rg.

Changes:
- `src/hl_trader/environment/tools/shell.py`: Explore/read-only shell now has a narrower review allowlist plus parameter-level danger checks. It allows common read/list/search commands and safe git inspection (`status`, `log`, `diff`, `show`, `ls-files`, `rev-parse`, `blame`), and rejects write-like commands, interpreters, unknown commands, redirection, `find -exec/-delete/-fprint`, `sort -o`, and `rg --pre`.
- `src/hl_trader/environment/explore.py`: prompt now tells Explore that shell uses read-only parameter validation and should hand complex DuckDB/Python work back to the main Agent.
- `src/hl_trader/environment/llm/deepseek.py`: conversation logging redacts `sk-*`, `Bearer ...`, and `Authorization: ...` patterns inside nested strings, raw responses, error bodies and error messages.
- `src/hl_trader/environment/nl/engine.py`: NL SubAgent errors are sanitized before returning to strategy-visible records; malformed native `text_retrieve` arguments now produce an explicit tool error result instead of an empty retrieval.
- `src/hl_trader/environment/backtest_engine.py`: NL RPC and strategy-policy RPC error responses are sanitized. The policy RPC driver has its own tiny local sanitizer because it runs as generated sandbox-side Python.
- `docs/environment_design.md` and `docs/agent_design.md`: documented the Claude-Code-style Explore shell boundary and the side-effect forms that are rejected.
- Tests:
  - Added coverage for `find -delete`, `find -fprint`, `awk`/`sed` write-like forms, `sort -o`, and safe `find`/git inspection.
  - Added DeepSeek log redaction coverage for Bearer/Authorization strings.
  - Added NL malformed native tool-call argument handling and sanitized provider failure coverage.
  - Added strategy policy RPC error redaction coverage.

Resource checks:
- Before targeted tests: memory about 417 GiB available; GPUs were already occupied by unrelated external Python processes. No GPU work was launched.
- After full test run: memory about 418 GiB available; GPUs still occupied by unrelated external processes.

Verification:
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_policy_rpc_error_is_redacted tests.unit.test_tools_flow.ShellToolTest tests.unit.test_tools_flow.AgentSessionRunnerTest tests.unit.test_tools_flow.StructuredSearchToolTest tests.unit.test_llm_deepseek.DeepSeekClientTest tests.unit.test_nl_scoring.NLSubAgentEngineTest`
  - 58 tests OK.
- `/home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_llm_deepseek tests.unit.test_nl_scoring tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_artifacts tests.unit.test_step_tree`
  - 183 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  - 268 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output after cleanup.

SubAgent follow-up:
- Explorer `Laplace` reviewed the Claude-Code-style Explore/Shell/NL/redaction changes and was closed after reporting findings.
- High finding fixed: `readonly_review` now fails closed on shell expansion and environment overrides: `$(`, backticks, process substitution `<(`/`>(`, heredoc `<<`, leading `VAR=...`, and `env ...` are rejected before command allowlist evaluation. This closes examples such as `cat $(touch workspace/x)`, `cat <(python3 -c ...)`, `PATH=workspace:$PATH ls`, and `GIT_EXTERNAL_DIFF=... git diff`.
- Medium finding fixed: `text_retrieve` now returns an explicit `status=error` tool result when `pattern`/legacy `keywords` are missing or `pattern` is non-string; the SubAgent no longer sees those mistakes as empty evidence.
- Medium finding fixed: strategy program failure stderr, policy BrokenPipe stderr, and policy early-exit stderr are sanitized before being wrapped in `BacktestError`; import-time `trading.py` failures with Bearer/Authorization tokens are covered.
- Medium finding fixed: Explore grep gets a timeout capped by remaining Fold deadline; glob receives a monotonic deadline and fails if it would overrun.
- Docs and Explore prompt updated to mention command substitution/process substitution/heredoc/env overrides as rejected read-only forms.

Follow-up verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ShellToolTest tests.unit.test_tools_flow.ToolFlowTest.test_policy_rpc_error_is_redacted tests.unit.test_tools_flow.ToolFlowTest.test_policy_import_error_is_redacted tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_cancels_search_when_deadline_is_too_close tests.unit.test_nl_scoring.NLSubAgentEngineTest.test_invalid_native_tool_arguments_return_tool_error tests.unit.test_nl_scoring.NLSubAgentEngineTest.test_missing_text_retrieve_pattern_returns_tool_error tests.unit.test_nl_scoring.NLSubAgentEngineTest.test_non_string_text_retrieve_pattern_returns_tool_error`
  - 16 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_llm_deepseek tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_artifacts tests.unit.test_step_tree`
  - 187 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  - 272 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output.

Second SubAgent follow-up:
- Explorer `Leibniz` reviewed the post-fix state and was closed. It found no High issues.
- Medium fixed: `StructuredSearchTool.glob()` no longer performs `sorted(target.glob(...))` before checking the Explore deadline. It now iterates incrementally, checks `deadline_monotonic` before processing each candidate, keeps only a bounded `offset + head_limit + 1` window, then sorts that window for stable output.
- Low defense-in-depth fixed: `StepTree.save()` sanitizes the JSON payload written to `tree.json`, so failed-attempt error fields are protected even if an upstream error path missed redaction.
- Tests added/adjusted:
  - decision-stage `run_strategy()` stderr with Bearer/Authorization token is redacted;
  - StepTree failed-attempt error is redacted on disk;
  - Explore search deadline boundary is tested through `_search_timeout()` / `_search_deadline()` and direct expired `glob`, avoiding a brittle 500ms LLM-loop timing test.

Second follow-up verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_strategy_program_failure_stderr_is_redacted tests.unit.test_tools_flow.ToolFlowTest.test_policy_import_error_is_redacted tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_search_helpers_reject_expired_deadline tests.unit.test_step_tree.StepTreeTest.test_failed_attempt_error_is_redacted_on_disk tests.unit.test_tools_flow.StructuredSearchToolTest tests.unit.test_step_tree.StepTreeTest`
  - 11 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_llm_deepseek tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_artifacts tests.unit.test_step_tree`
  - 189 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  - 274 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output.

Third SubAgent follow-up:
- Explorer `Epicurus` reviewed the second follow-up and was closed. It found no High/Medium blocking issues.
- Low note addressed: `glob()` no longer relies on `Path.glob()` iteration order plus per-window sorting. It now uses a deterministic sorted directory traversal and anchored segment matcher (`**` supported), applies deadline checks before directory scans and candidate processing, and paginates the deterministic traversal order without full enumeration.
- Added regression coverage that `*.py` matches only top-level files, `**/*.py` matches recursive files, and adjacent pages do not repeat entries.

Third follow-up verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.StructuredSearchToolTest tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_search_helpers_reject_expired_deadline tests.unit.test_step_tree.StepTreeTest`
  - 8 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_llm_deepseek tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_artifacts tests.unit.test_step_tree`
  - 189 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  - 274 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output.

Final bounded follow-up:
- Explorer `Hume` was closed after finding one remaining Medium in the custom glob traversal: directory symlinks could recurse outside the intended tree. Fixed by skipping symlink candidates in `StructuredSearchTool._iter_glob_matches()` and adding a symlink-loop regression test.
- Explorer `Kuhn` was closed after finding one remaining Medium in replay policy isolation: `_STRATEGY_PATH_GUARD` compared normalized paths without resolving symlinks and did not distinguish write-mode access to `output`. Fixed without starting another review round, per the user instruction to stop further iterative复核:
  - `_STRATEGY_PATH_GUARD` now checks both normalized and real paths.
  - Replay policy sets `MQ_WRITE_FORBIDDEN_PATHS` to `output/`, while still allowing read-only import of `output/main.py` and `output/trading.py`.
  - Replay policy sets `MQ_DISABLE_LINKS=1`; generated strategy code cannot create soft or hard links during replay.
  - Common write mutators (`open` write modes, `os.open` write flags, mkdir/unlink/rmdir/rename/replace and Path equivalents) go through write guards.
  - Regression tests cover replay write to `output/`, replay read of `models/` through an external symlink, and replay link creation.
- Low fixes from the same review:
  - NL native tool loop returns an explicit tool error for unknown tool names instead of silently ignoring them.
  - `docs/environment_design.md` describes DeepSeek SSE handling as response compatibility and delta merge, not true incremental consumption.

Final verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_trade_policy_cannot_write_output_artifacts_during_replay tests.unit.test_tools_flow.ToolFlowTest.test_trade_policy_cannot_read_model_artifacts_through_output_symlink tests.unit.test_tools_flow.ToolFlowTest.test_trade_policy_cannot_create_links_during_replay tests.unit.test_nl_scoring.NLSubAgentEngineTest.test_unknown_native_tool_call_returns_tool_error`
  - 4 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_llm_deepseek tests.unit.test_broker_engine tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation tests.unit.test_artifacts tests.unit.test_step_tree`
  - 193 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  - 278 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output.
- Resource check after tests: system memory about 422 GiB available. GPUs were already occupied by unrelated external Python processes; no GPU workload was launched.

## 2026-06-25 - Recent-change redundancy audit

Task: check whether the recent Claude-Code-style Explore/Shell/Search, replay policy guard, NL native-tool and redaction changes introduced junk files or redundant logic.

Findings:
- Code directories `src/`, `tests/`, and `scripts/` had no remaining `__pycache__` after cleanup.
- Ignored historical experiment directories under `experiments/audit_cli/` contain local package caches and virtualenv files. They are runtime artifacts ignored by Git, not part of the recent code patch.
- The large generated replay policy guard in `backtest_engine.py` is intentionally embedded because it runs inside a sandbox-side strategy process and cannot rely on host-only helpers.
- `StructuredSearchTool.glob()` reimplements deterministic traversal instead of using `Path.glob()` to support deadline checks, stable pagination, and symlink skipping; this is not redundant in the current design.
- Real redundancy found: `environment/llm/deepseek.py` carried local secret-looking string regexes that overlapped with `runtime.sanitize_for_log`. Fixed by reusing `sanitize_for_log` for string redaction while keeping DeepSeek-specific sensitive-key handling for provider conversation logs.

Verification:
- `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_llm_deepseek tests.unit.test_nl_scoring tests.unit.test_tools_flow.ToolFlowTest.test_policy_rpc_error_is_redacted tests.unit.test_tools_flow.ToolFlowTest.test_policy_import_error_is_redacted tests.unit.test_tools_flow.ToolFlowTest.test_strategy_program_failure_stderr_is_redacted tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_llm_error_trace_redacts_bearer_token`
  - 40 tests OK.
- `git diff --check`
  - OK.
- `find src tests scripts -type d -name __pycache__`
  - no output.

## 2026-06-25 Revert unsound readonly_review shell guard; audit guards/fallbacks

Task: review/fix a Codex round; remove the over-engineered Explore "read-only shell" static write-detector; audit other redundant security-boundary logic; apply fail-fast; cut unnecessary fallbacks — retaining the Claude Code design philosophy.

Findings on Codex's round:
- GOOD (kept): `deepseek.py` `_redact_secrets` now delegates to `runtime.sanitize_for_log` (broader redaction; removed single-pattern `SECRET_PATTERN`); `nl/engine.py` native `_parse_native_tool_calls` surfaces invalid-JSON args and unknown tool names as `status:"error"` tool records (one `tool` result per call → message-pairing preserved) + NL errors sanitized; `explore.py` deadline budget + bounded tool timeouts + parallel `_dispatch_calls`.
- DEFECT (removed): `readonly_review` static write-detector in `shell.py` (~169 lines) used by Explore. Empirically: `sed 'w workspace/nope'` slips through as ALLOWED (failing test `test_readonly_shell_rejects_nested_writes`); `python3 -c "...duckdb..."` BLOCKED (excluded from `READONLY_REVIEW_COMMANDS`) → Explore can't inspect parquet, its core job. Unsound (write detection on arbitrary shell is undecidable) and contradicts the documented "Shell guard 轻量合同层，不是完整 Bash 解析器；Docker/权限/产物检查兜底".

Changes:
- `src/hl_trader/environment/tools/shell.py`: removed `readonly_review` param on `run()`/`_guard_paths`, the `_guard_readonly_segment`/`_readonly_forbidden_shell_construct`/`_readonly_env_prefix`/`_readonly_review_danger`/`_is_readonly_git_command` functions, and constants `READONLY_REVIEW_COMMANDS`/`READONLY_FORBIDDEN_SHELL_EXPANSION_RE`/`FIND_DANGEROUS_ACTIONS`/`SORT_DANGEROUS_FLAGS`/`RG_DANGEROUS_FLAGS`/`GIT_READ_ONLY_SUBCOMMANDS`/`GIT_DANGEROUS_READ_FLAGS`. Kept `ENV_BINARIES` (used by the shared `_strip_shell_prefix`) and the legitimate path/install-network guard. 987 → 818 lines.
- `src/hl_trader/environment/explore.py`: dropped `readonly_review=True` on the shell call (Explore is read-only by instruction; writes caught by modification_check/freeze-hash/Docker).
- `tests/unit/test_tools_flow.py`: removed `test_readonly_shell_rejects_nested_writes`, `test_readonly_shell_allows_safe_git_inspection_only`, `test_explore_shell_rejects_writes` (tested the removed feature).
- Docs: `agent_design.md` §3.2 and `environment_design.md` §5.1/§6.1 corrected from "hard read-only enforcement" to "read-only by convention, hard isolation via modification_check + freeze-hash + Docker". `PROMPTS.md` regenerated (Codex had edited prompts.py without exporting).

Audit conclusion (fail-fast / fallbacks): the remaining broad `except Exception  # noqa: BLE001` sites (runner `_dispatch`, compaction, explore, NL, proxy boundary) are the intentional, design-mandated "tool/sub-agent failure → audited observation, do not kill the Fold" boundary; the pipeline layer stays fail-fast. They are not redundant fallbacks and were kept. Did not force-abstract the three native-tool loops (main/NL/Explore have distinct concerns) — premature abstraction.

Validation:
- `PYTHONPATH=src python -m unittest discover -t . -s tests -p "test_*.py"` -> Ran 275 tests, OK.
- `grep readonly_review|readonly_shell src tests` -> none. `py_compile` OK. `PROMPTS.md` in sync. `git diff --check` clean.

## 2026-06-25 - Post-Claude readonly_review audit and meta-learning Fold

Task: audit Claude's latest code changes, accept the design decision to remove
the Explore `readonly_review` static shell checker, make any small consistency
fixes, then run one formal meta-learning Fold in the real Docker sandbox.

Audit:
- `readonly_review` and `readonly_shell` no longer appear in production source
  or tests. The remaining historical mentions are in logbook history only.
- `src/hl_trader/environment/tools/shell.py` keeps the ordinary path/network
  contract guard but no longer tries to be a read-only Bash parser.
- `src/hl_trader/environment/explore.py` no longer passes `readonly_review=True`.
- Fixed one prompt mismatch in `src/hl_trader/environment/explore.py`: the
  Explore system prompt no longer says shell performs read-only argument
  validation. It now states the intended contract: Explore is read-only by
  instruction, shell is a light contract guard, and hard isolation/validation
  are handled by Docker, modification check, and artifact hashes.

Verification before the run:
- Real path check: `pwd -P` returned `/Data/lzp/MacroQuant`.
- Resource checks:
  - Before full tests: about 425 GiB available RAM; GPUs were occupied by
    unrelated external Python workloads.
  - Before meta-learning run: about 408 GiB available RAM; no Docker sandbox
    was already running.
- Docker image:
  `docker image inspect macroquant-sandbox:latest --format '{{.Id}}'`
  returned `sha256:5f574f7d1ebb6e5d73b957bddd943a268aaf007c56f3a2c4508a4146c49fe8da`.
- Residual scan:
  `rg -n "readonly_review|readonly_shell|只读参数校验" src tests configs docs/agent_design.md docs/environment_design.md`
  returned no matches.
- Targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow`
  passed, 62 tests.
- Full tests:
  `PYTHONDONTWRITEBYTECODE=1 /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p "test_*.py"`
  passed, 275 tests.
- `git diff --check` passed.
- Python caches created by the test run were removed; final
  `find src tests scripts -type d -name __pycache__` returned no output.

Meta-learning run:
- Experiment ID: `meta_learning_after_readonly_revert_20260626_0053`.
- Run ID: `run_de253393feea`.
- Entry: direct `ExperimentPipeline.run_meta_learning(epoch_id="epoch_001",
  parent=None, previous_taste="", visible_fold=fold_2022Q1)`.
- Mode: real Docker sandbox, meta-learning only, taste-only output.
- Models: main Agent `deepseek-v4-pro` with `reasoning_effort=max`;
  NL/Explore `deepseek-v4-flash`; compact `deepseek-v4-flash` with thinking
  disabled.
- Walk-forward config: quarterly Fold; development periods `2022Q1..2025Q4`;
  held-out boundary `2026Q1..2026Q1`; visible first Fold `fold_2022Q1`.
- Data windows: default PIT history 21 months; intraday 1-minute visible window
  5 trading days.
- Web search engines: `tavily`, `semantic_scholar`.
- Meta-learning Docker network: `bridge`; ordinary Fold sandbox spec remains
  recorded separately with network disabled.
- Runtime log: `logs/meta_learning_after_readonly_revert_20260626_0053.log`.

Observed runtime:
- Host-side snapshot/data-summary preparation took about 6.5 minutes before the
  Docker container started.
- Runtime sandbox: `.runtime/sandboxes/run_de253393feea/`.
- A pandas `FutureWarning` was emitted from
  `src/hl_trader/environment/snapshot.py:503` about concat behavior with empty
  or all-NA entries. It did not fail the run.
- Docker container `mqsbx_97378baf51cc` started and was stopped by the
  pipeline after completion.

Result:
- Ledger status: `taste_only`.
- Agent finish status: `meta_learning_done`.
- Taste length: 3709 characters.
- Modification check: `allowed_to_backtest=true`, no reasons.
- No frozen strategy artifact was created because this was parentless
  meta-learning-only.
- Trace event counts:
  - `llm_call`: 22
  - `shell`: 37
  - `web_search`: 9
  - `glob`: 4
  - `context_edit`: 12
  - `context_summary`: 4
  - `session_end`: 1
  - `tool`: 1
- Trace errors: 0.
- Context compaction calls: 0.
- Token usage from ledger:
  - prompt tokens: 628009
  - completion tokens: 17425
  - total tokens: 645434
  - cache hit tokens: 324608
  - cache miss tokens: 303401
  - cache hit ratio: 0.5169

Artifacts:
- Manifest:
  `experiments/meta_learning_after_readonly_revert_20260626_0053/artifacts/run_de253393feea/run_manifest.json`.
- Canonical trace:
  `experiments/meta_learning_after_readonly_revert_20260626_0053/artifacts/run_de253393feea/agent_trace.jsonl`.
- Data summary:
  `experiments/meta_learning_after_readonly_revert_20260626_0053/artifacts/run_de253393feea/data_summary.json`.
- Taste:
  `experiments/meta_learning_after_readonly_revert_20260626_0053/meta_learning/epoch_001/taste.md`.
- Ledger:
  `experiments/meta_learning_after_readonly_revert_20260626_0053/ledgers/experiment_ledger.jsonl`.

Post-run resource check:
- System memory: about 421 GiB available.
- GPUs remained occupied by the same unrelated external Python workloads; this
  run did not launch local GPU training.
- `docker ps` returned no running containers.

## 2026-06-25 Trace/context/guard/data fixes from run_de253393feea audit

Source: deep audit of `.runtime/sandboxes/run_de253393feea` (first-epoch meta-learning, `meta_learning_done`, 22 LLM calls, ~11m). Implemented the 4 agreed tasks plus the subset of Codex's 7 findings that were real.

Changes:
- Trace delta (`agent/runner.py`): `_next_turn` logs `new_messages` (messages first seen this turn, tagged by a monotonic `_seq`) + `message_count` instead of the full `messages` each call (was 83% of a 2.6 MB/90-event trace). Assistant messages are `_seq`-tagged but excluded (their content/tool_calls are already on the producing llm_call). Reconstruct = concat each call's new_messages with its content/tool_calls. Updated `test_scripted_session_finishes_fold` (`messages`→`new_messages`).
- Token-primary context triggers (`agent/runner.py`): import `estimate_messages_tokens`; `_trim` fires on `len > max_history_messages(150) OR est_tokens > trim_token_threshold(60000)`; `_clear_stale_tool_results` skips while `est_tokens < tool_result_clear_token_threshold(24000)`. Message counts are now high safety caps; tokens are primary, so the cacheable prefix is rewritten far less often (cache reset cost). Count-cap path still exercised by existing small-`max_history_messages` tests.
- Prompts (`agent/prompts.py`, regenerated PROMPTS.md): `## 动作协议`→`## 可用工具` as a markdown tool table (FOLD + meta, incl. write_file/edit_file/explore/web_search); workflow note that sandbox data is a sample and later Folds expand the backtest range; Taste contract states template filenames (candidate.py/trading.py/nl_prompt.md) are not a fixed structure — only `output/main.py` is required (#7).
- Data sample (`environment/snapshot.py`): `SnapshotConfig.intraday_trade_days` 5→21 (one trading month of decision-input minute bars; replay windows are sized by fold periods, not this field).
- data_summary slim (`environment/data_summary.py`): full schema (key_columns + key-column null counts) only for the primary `snapshot` view; `train`/`valid` compact (path/mount_path/rows/size/large_table/date_ranges); dropped `row_groups`/`recommended_access`; write compact (un-indented) JSON. On the run's real snapshot: 37,661 → 15,058 chars (fits the 20k cat limit; lower token cost). `test_pipeline_e2e` asserts key_columns only on the snapshot view → still passes.
- Shell guard false positives (`environment/tools/shell.py`): added `_strip_heredoc_bodies` (drops heredoc bodies, keeps the opener line + any real redirect) applied in `run()` and at the top of `_guard_paths`; `_path_references` now scans `_mask_quoted(command)` so the absolute-path regex cannot reach inside quoted `-c` payloads. Verified offline: `python3 << 'EOF' ... > 150 ... [:5] ... EOF` and `python3 -c "... b > 150; '/5' ..."` no longer raise; `echo hi > /etc/passwd`, `cat <<EOF > output/x.txt`, `cp a /mnt/snapshot/x` still flagged. Added regression asserts in `test_shell_runs_and_logs_and_guards_test_dir`.
- Manifest host paths (`pipelines/experiment.py`): meta `development_inputs` (`experiment_ledger_full`/`development_history`/`meta_learning_memory`) and `taste_output` now `/mnt/agent/workspace/...` mount paths; dropped the un-mounted raw `experiment_ledger` host dir. (These fields are write-only in the manifest — not read back by the pipeline.)
- web_search (`environment/web_search.py`): cap tavily per-result `content` at 1500 chars (matches the semantic_scholar abstract cap).

Codex-finding verdicts not actioned:
- #3 snapshot build ~6.5 min: dominated by the ~1 GB valid `intraday_1min.parquet`, which is the replay window the backtest requires — not reducible without breaking validation; "not mandatory" → left as-is.
- #6 `2>/dev/null`: already prohibited by prompt; a hard guard rule would be brittle over-engineering, and the root cause (the agent probing guessed paths defensively) is removed by the #2 manifest-path fix.

Validation: `python -m unittest discover -t . -s tests` → Ran 275 tests, OK. PROMPTS.md regenerated and in sync. `py_compile` OK. `git diff --check` clean. Nothing committed (left unstaged for review).

## 2026-06-26 Trace/context/data summary audit follow-up

Task: audit Claude's trace/context/tool/data-summary modifications for logical errors, redundant code, stale docs, and junk files.

Independent review:
- Spawned one read-only Explorer SubAgent for cross-check and closed it after completion.
- SubAgent confirmed the main DeepSeek tool-call payload strips non-standard fields, but found `_seq` would still enter compact-model prompts through `conversation_messages`; it also found context editing could clear same-turn tool outputs before the LLM had seen them.

Fixes:
- `scripts/experiments/run_experiment.py`: `--intraday-trade-days` default now comes from `SnapshotConfig().intraday_trade_days`, so CLI runs no longer override the 21-trading-day default back to 5. `agent_session_config` now records `trim_token_threshold`, `clear_tool_results`, `tool_result_keep_recent`, `tool_result_clear_min_chars`, and `tool_result_clear_token_threshold`.
- `src/hl_trader/agent/compact.py`: compact request serialization strips runner-local keys such as `_seq` before sending history to the compact proxy.
- `src/hl_trader/agent/runner.py`: `_clear_stale_tool_results()` accepts `protect_from_index`; the main loop protects all tool results generated in the current assistant turn so context editing only clears older outputs already visible to the model.
- `src/hl_trader/environment/data_summary.py`: metadata-error strings are converted to sandbox-safe text and host absolute paths are redacted before writing Agent-visible `data_summary.json`.
- `src/hl_trader/agent/prompts.py` and regenerated `configs/prompts/PROMPTS.md`: replaced “later Folds expand the backtest range” with the correct contract that Folds roll forward by configured period and replay windows are sized by each Fold period.
- `docs/environment_design.md` and `docs/data_documentation.md`: synchronized intraday decision-input default to 21 trading days without retaining obsolete version wording.

Regression tests added:
- `test_context_compaction_request_strips_runner_internal_fields`
- `test_context_edit_preserves_current_turn_tool_results`
- `test_session_config_summary_records_context_token_thresholds`
- `test_data_summary_metadata_error_redacts_host_paths`

Validation:
- Pre/post resource checks: system memory stayed above ~432 GiB available; GPUs were occupied by unrelated jobs, and this test run did not use GPU.
- `PYTHONPATH=src ~/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_snapshot_builder tests.unit.test_llm_deepseek` → Ran 115 tests, OK.
- `PYTHONPATH=src ~/miniconda3/envs/quant/bin/python -m unittest discover -s tests` → Ran 279 tests, OK.
- `git diff --check` → clean.
- Removed generated `__pycache__` directories after tests; no cache files remain under `src/`, `tests/`, or `scripts/`.

## 2026-06-26 Pipeline Taste inheritance clarification

Task: clarify in the Pipeline flow that the Taste produced by the meta-learning session is directly inherited by later Folds, and that each Fold inherits the strategy/model artifacts frozen by the previous Fold.

Changes:
- `docs/pipeline_design.md`: expanded the core Mermaid flow and text around the two inheritance channels:
  - Taste is generated once per Epoch and injected into every ordinary Fold Prompt in that Epoch.
  - Strategy and model artifacts are inherited sequentially from the previous Fold's frozen output, or from the Pipeline-selected fallback parent when no acceptable update is frozen.
- `src/hl_trader/agent/prompts.py`: updated the meta-learning Pipeline flow section so the meta-learning Agent understands that its `taste.md` is a key implementation guide for all subsequent Fold Agents in the same Epoch.
- `configs/prompts/PROMPTS.md`: regenerated from `scripts/dev/export_prompts.py`.

Validation:
- `PYTHONPATH=src ~/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` → OK.
- `git diff --check` → clean.
- Removed `__pycache__` generated by prompt export.

## 2026-06-26 Meta Learning trace detail run

Task: start one formal meta-learning Fold and overwrite `check.md` with a dialogue-style reconstruction of the Agent Trace, including detailed Agent outputs.

Resource checks:
- Before report extraction, `free -h` showed about 432 GiB available system memory.
- GPU query showed GPUs 0-6 occupied by unrelated jobs and GPU 7 idle; this meta-learning run did not launch GPU training.

Run:
- Experiment: `meta_learning_trace_detail_20260626_115832`
- Run ID: `run_027521b81c60`
- Config: Docker sandbox, `deepseek-v4-pro`, `reasoning_effort=max`, quarterly fold period, 21-month history window, 21-trading-day intraday decision-input window, Web Search engines `tavily` and `semantic_scholar`.
- Primary log: `logs/meta_learning_trace_detail_20260626_115832.log`

Artifacts:
- Trace: `experiments/meta_learning_trace_detail_20260626_115832/artifacts/run_027521b81c60/agent_trace.jsonl`
- Taste: `experiments/meta_learning_trace_detail_20260626_115832/artifacts/run_027521b81c60/workspace/taste.md`
- Manifest: `experiments/meta_learning_trace_detail_20260626_115832/artifacts/run_027521b81c60/run_manifest.json`
- Data summary: `experiments/meta_learning_trace_detail_20260626_115832/artifacts/run_027521b81c60/data_summary.json`
- Audit report: `check.md`

Trace summary:
- Agent session reached `finish_status=meta_learning_done` and wrote `taste.md`.
- Event counts: 18 `llm_call`, 60 `shell`, 10 `grep`, 3 `explore`, 24 `explore_llm_call`, 6 `web_search`, 7 `context_edit`, 0 context compactions.
- DeepSeek usage from Trace: 494,721 prompt tokens, 10,697 completion tokens, 505,418 total tokens, 2,419 reasoning tokens, 353,280 prompt cache-hit tokens, 141,441 prompt cache-miss tokens.

Known issue:
- After Agent completion, `sandbox.collect_artifacts()` attempted to copy workspace `.cache/pip` files and hit permission denied. The already-collected Trace/Taste/manifest/data_summary were sufficient for this audit, but the artifact collector should ignore cache directories or prevent pip cache writes inside collectable workspace in a follow-up fix.

Report:
- `check.md` was overwritten with a dialogue-style reconstruction of the trace, including initial prompt excerpt, each top-level Agent turn, tool calls, selected tool returns, the outer collection error, and the final Taste text.
- A secret scan over `check.md` for common API token patterns returned no matches.

## 2026-06-26 Meta Taste prompt cleanup

Task: remove concrete time/Fold examples from the meta-learning Taste contract and merge the transferability rule with the continued-exploration rule.

Changes:
- `src/hl_trader/agent/prompts.py`: replaced the two separate Taste bullets with one generic rule. It now forbids quarter/year/Fold labels, Fold-specific plans, and restating valid/test/held-out ranges without listing concrete example labels; it also states when a weak or failed direction can still be worth continuing.
- `src/hl_trader/agent/runner.py`: generalized the nearby comment so future edits do not copy concrete period examples back into prompts.
- `configs/prompts/PROMPTS.md`: regenerated from `scripts/dev/export_prompts.py`.

Validation:
- Resource checks before script execution showed about 428 GiB available memory; GPUs were occupied by unrelated jobs.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` → OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py src/hl_trader/agent/runner.py` → OK.
- `git diff --check` → clean.
- Removed the `src/hl_trader/agent/__pycache__` generated by compilation.

Follow-up:
- Moved the template-file and time-window transferability rules out of the "内容应覆盖" bullet list in the meta-learning Taste contract. They are now two ordinary "注意" paragraphs after the list, so the Agent treats them as writing boundaries rather than required Taste output sections.
- Regenerated `configs/prompts/PROMPTS.md`.
- Validation: prompt export OK, `py_compile src/hl_trader/agent/prompts.py` OK, `git diff --check` clean, no `__pycache__` left under `src/hl_trader/agent`.

## 2026-06-26 Meta Learning rerun with prompt-only Taste constraints

Task: rerun one formal meta-learning Fold after prompt cleanup.

Initial run:
- Experiment: `meta_learning_rerun_20260626_151833`
- Run ID: `run_14e9415649d7`
- Config: Docker meta-learning-only run, CPU-only sandbox to avoid current GPU contention, `deepseek-v4-pro` main model, `deepseek-v4-flash` NL/explore/compact, reasoning effort max, quarterly Fold period, 21-month history window, 21-trading-day intraday decision-input window, web search engines `tavily` and `semantic_scholar`.
- Result: Agent reached `finish_status=meta_learning_done`, status `taste_only`, and artifact collection succeeded. However, Taste still contained concrete decision dates/year windows, showing that the existing policy guard only rejected quarter/Fold/held-out labels and did not catch specific dates/years.

Fix:
- `src/hl_trader/agent/runner.py`: removed the remaining content-style Taste regex guard. The runner now only enforces that `taste.md` exists and is non-empty before accepting `done`.
- `src/hl_trader/agent/prompts.py`: strengthened the meta-learning Taste note to forbid concrete dates, quarter/year/Fold labels, Fold-specific plans, and valid/test/held-out ranges; the prompt also tells the Agent to self-check and rewrite before `done` rather than relying on tool-side interception.
- `configs/prompts/PROMPTS.md`: regenerated.
- `tests/unit/test_tools_flow.py`: updated the meta-learning `done` test to cover only the actual hard condition: `taste.md` must be written and non-empty. Transferability is prompt-guided and audit-reviewed, not regex-rejected.

Validation before rerun:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest.test_meta_done_rejects_fold_specific_taste` → OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest` → 23 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` → OK.
- `git diff --check` → clean.

Final rerun:
- Experiment: `meta_learning_rerun_strict_20260626_153215`
- Run ID: `run_c1b20ae82ed1`
- Log: `logs/meta_learning_rerun_strict_20260626_153215.log`
- Trace: `experiments/meta_learning_rerun_strict_20260626_153215/artifacts/run_c1b20ae82ed1/agent_trace.jsonl`
- Taste: `experiments/meta_learning_rerun_strict_20260626_153215/meta_learning/epoch_001/taste.md`
- Result: `finish_status=meta_learning_done`, ledger status `taste_only`, `modification_check.allowed_to_backtest=true`, Taste 2232 chars.
- Trace summary: 10 `llm_call`, 25 `shell`, 1 `explore`, 6 `web_search`, 5 `context_edit`, 0 context compactions, 0 trace errors.
- Token usage: 230,766 prompt tokens, 8,700 completion tokens, 239,466 total tokens, 2,951 reasoning tokens, cache hit ratio 0.5303.
- Web search: all three required perspectives completed with successful non-empty searches.
- Taste audit: grep for concrete dates/years/Fold/held-out/test/valid labels returned no matches. The final artifact remains clean, and future prevention is handled by prompt guidance plus audit rather than content regex guards.

Resource and cleanup:
- Pre/post memory stayed above roughly 427 GiB available. GPUs were occupied by unrelated jobs; meta-learning sandbox was run CPU-only and did not launch local GPU training.
- Removed generated `__pycache__` directories under `src/`, `tests/`, and `scripts/`.

## 2026-06-25 run_027521b81c60 audit: Explore robustness, Taste period-agnostic, collect_artifacts scope

Source: audit of `.runtime/sandboxes/run_027521b81c60` (first-epoch meta-learning, `meta_learning_done`, 18 main calls, 3 explore tasks, cache_hit_ratio 0.71). Fixed the three reported issues + verified the rest.

Issue 1 — Explore failures: 1 of 3 explore tasks ended `status=error, digest=""` with `deepseek request failed: ... finish_reason=length` at `explore_round_6`. Root cause: `ExploreSubAgentConfig.max_tokens=3000` is too small for a round that emits a long DuckDB tool-call argument plus reasoning; deepseek `_parse_response` treats `finish_reason=length` as a hard error. Fix (`environment/explore.py`): `max_tokens` 3000→6000 (completed digests were only 1913/3286 chars, so no main-context bloat); restructured the rounds loop to a `while` that catches a non-timeout `LLMProxyError` per round, breaks, and forces a single concise final summary (`tool_choice="none"`) — a length/transient cut no longer fails the whole task. Contained already (a sub-agent failure never killed the Fold), now degrades gracefully.

Issue 2 — Taste fold-specific labels: `taste.md` contained `验证期 2021Q4，测试期 2022Q1`, `Held-out 仅 2022Q2`, and a `Fold_2022Q1（首个 Fold）: ...` plan. The Taste is injected into every walk-forward Fold prompt, so quarter/Fold labels are both non-transferable and a leak of the test schedule. Final fix: meta prompt Taste contract (`agent/prompts.py`) explicitly forbids quarter/year/date/Fold labels and restating valid/test/held-out windows, and tells the Agent to self-check and rewrite before `done`. Runner no longer uses content regex guards for Taste; it only requires `taste.md` to exist and be non-empty.

Issue 3 — collect_artifacts too broad: `_copy_path` did `shutil.copytree(workspace, ...)` with no filter, so `workspace/.cache/pip/...` (written by the container user, owner 357607:297607, mode 0600) was archived; the host `lzp` collector can't read 0600 files → PermissionError. Fix (`environment/sandbox.py`): added `_COLLECT_IGNORE = shutil.ignore_patterns(".cache","__pycache__","*.pyc","*.pyo",".git",".mypy_cache",".pytest_cache",".ruff_cache",".ipynb_checkpoints","node_modules",".venv",".conda",".npm",...)` and passed it to the copytree in `_copy_path`. Caches are scratch, not artifacts; genuinely-unexpected unreadable files still fail loudly (fail-fast).

Also verified (no new code):
- 47 path_guard hits (`.../agent/0`, `/CAST`) came from `duckdb -c "...CAST(count(*) AS VARCHAR)... > 0..."`; these are the OLD guard — the unstaged heredoc-strip + quote-mask fix returns clean for the exact command (checked offline). This run executed pre-fix code; the guard fix just needs to ship.
- `duckdb` CLI is absent (exit 127); the agent self-corrected to `python3 -c "import duckdb"`. Minor; no change.
- web_search covered all 3 perspectives (one transient semantic_scholar failure auto-retried).

Validation: `python -m unittest discover -t . -s tests` → Ran 282 tests, OK (3 new regressions: `test_explore_salvages_digest_after_length_cutoff`, `test_meta_done_rejects_fold_specific_taste`, `test_collect_artifacts_excludes_transient_caches`). PROMPTS.md regenerated/in sync. py_compile OK. Nothing committed (unstaged for review).

## 2026-06-26 Strategy context workspace cleanup

Task: revert the previous backtest-summary marker change and remove the workspace path from the formal strategy decision context.

Changes:
- `src/hl_trader/environment/tools/backtest.py`: reverted `modification_check_auto_run`; `_enforce_modification_check()` again returns only the check summary while retaining the existing missing/stale auto-check behavior.
- `src/hl_trader/environment/backtest_engine.py`: removed `context["workspace_dir"]` and `MQ_WORKSPACE_DIR` from the `run_strategy(context)` subprocess environment.
- `tests/unit/test_tools_flow.py`: removed assertions for the reverted summary field and added a regression test that fails if `workspace_dir` or `MQ_WORKSPACE_DIR` reaches the formal decision entrypoint.
- `src/hl_trader/agent/prompts.py`, generated prompts, template docs, and living docs: synchronized the contract so decision code uses `model_dir` for persisted parameters and does not rely on workspace being passed through context.

Validation:
- Resource checks before and after scripts/tests showed about 423 GiB available memory. GPUs were occupied by unrelated jobs; this work used CPU-only prompt export and unit tests.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_strategy_program_context_does_not_expose_workspace_dir tests.unit.test_tools_flow.ToolFlowTest.test_modification_check_backtest_and_finish_fold tests.unit.test_tools_flow.ToolFlowTest.test_backtest_runs_strategy_program_trade_intents` -> 3 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow` -> 67 tests OK. Existing ResourceWarning about unclosed file descriptors in `ast.py` appeared, but tests passed.
- `git diff --check` -> clean.
- `find src tests scripts -type d -name __pycache__ -print` -> no output.

Follow-up audit:
- Spawned GPT-5.5 High SubAgent `019f0346-52f3-7901-acff-75771d68b02d` for a read-only audit of the erroneous `modification_check_auto_run` change and the workspace context removal.
- SubAgent verdict: current runtime protocol is acceptable. It found no code/test/live Prompt residue for `modification_check_auto_run`; it confirmed `run_strategy(context)` no longer receives `workspace_dir` or `MQ_WORKSPACE_DIR`, while `model_dir`, `MQ_MODEL_DIR`, snapshot, output, decision time, replay granularity, and NL remain available.
- Low-risk cleanup from the audit: clarified `docs/agent_design.md` so workspace is not described as a formal decision context input, and added `assertNotIn("modification_check_auto_run", summary)` regression assertions in `tests/unit/test_tools_flow.py`.
- Follow-up validation: resource checks showed about 423 GiB available memory; GPUs were occupied by unrelated jobs. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.ToolFlowTest.test_modification_check_backtest_and_finish_fold tests.unit.test_tools_flow.ToolFlowTest.test_backtest_runs_strategy_program_trade_intents tests.unit.test_tools_flow.ToolFlowTest.test_strategy_program_context_does_not_expose_workspace_dir` -> 3 tests OK. `git diff --check` -> clean. `find src tests scripts -type d -name __pycache__ -print` -> no output.

## 2026-06-26 Meta Learning prompt structure cleanup

Task: reorganize the meta-learning system prompt so Pipeline flow carries the sample-data and first-run-empty-history context, while Taste writing constraints are simplified and moved into prohibitions.

Changes:
- `src/hl_trader/agent/prompts.py`: `META_LEARNING_INSTRUCTION` `Pipeline流程` now states that the visible data is only the first Fold's example visible window; subsequent Folds roll forward in time and use their own windows.
- Merged the previous standalone `首轮空历史` section into `Pipeline流程`, keeping the behavior but reducing duplicated structure.
- Removed the two long `注意：...` paragraphs after the Taste contract and replaced them with concise `禁止事项` bullets covering non-fixed template filenames, time-window-agnostic Taste writing, done-before-self-check, and failed-direction continuation rules.
- Regenerated `configs/prompts/PROMPTS.md` from the prompt source.

Validation:
- Resource checks before and after prompt export showed about 423 GiB available memory. GPUs were occupied by unrelated jobs; this task used CPU-only scripts.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py scripts/dev/export_prompts.py` -> OK.
- `git diff --check` -> clean.
- Removed `src/hl_trader/agent/__pycache__` and `scripts/dev/__pycache__`; `find src tests scripts -type d -name __pycache__ -print` -> no output.

Follow-up:
- Moved the positive guidance about continuing a weak-but-mechanistic direction back into the Taste output contract. `禁止事项` now only forbids encouraging repeated failed directions that rely on stock/month/window memory or lack a verifiable mechanism.
- Regenerated `configs/prompts/PROMPTS.md`; `git diff --check` -> clean; `find src tests scripts -type d -name __pycache__ -print` -> no output.
- Reworked the meta-learning `Pipeline流程` paragraph into a concise bullet list covering Epoch/Fold order, sample-window visibility, Taste injection, artifact inheritance, first-run-empty-history handling, and Taste quality expectations.
- Regenerated `configs/prompts/PROMPTS.md`; `git diff --check` -> clean; `find src tests scripts -type d -name __pycache__ -print` -> no output.
- Removed the remaining prohibition bullet `不得鼓励重复已失败、依赖个股/月度/时间窗口记忆或缺少可验证机制的方向。` from `META_LEARNING_INSTRUCTION`; regenerated `configs/prompts/PROMPTS.md`.

Meta-learning rerun:
- Started a Docker meta-learning-only run with `experiment_id=meta_learning_prompt_cleanup_20260626_180640`, `run_id=run_77940b553de6`, DeepSeek V4 Pro with `reasoning_effort=max`, compact/explore on DeepSeek V4 Flash, quarterly WF, 21-month PIT windows, 21 visible intraday trading days, and web search engines `tavily, semantic_scholar`.
- Result: `finish_status=meta_learning_done`, ledger status `taste_only`, `taste_chars=3368`, no frozen artifact because this was a first-parent template Taste-only run. Token usage: total 426,371; prompt 416,437; completion 9,934; reasoning 2,943; cache hit ratio 0.5158. Trace counts: 15 LLM calls, 24 shell calls, 9 web_search calls, 10 context_edit events, 2 tool events, 1 session_end, 0 context compactions.
- Key paths: log `logs/meta_learning_prompt_cleanup_20260626_180640.log`; trace `experiments/meta_learning_prompt_cleanup_20260626_180640/artifacts/run_77940b553de6/agent_trace.jsonl`; manifest `experiments/meta_learning_prompt_cleanup_20260626_180640/artifacts/run_77940b553de6/run_manifest.json`; ledger `experiments/meta_learning_prompt_cleanup_20260626_180640/ledgers/experiment_ledger.jsonl`; Taste `experiments/meta_learning_prompt_cleanup_20260626_180640/meta_learning/epoch_001/taste.md`.
- Non-fatal runtime issue: one shell data-check command failed with pandas mixed str/float date comparison while inspecting fundamentals; the Agent read stderr, continued, and completed normally.
- Taste audit note: despite the Prompt prohibition, the produced Taste still contains time/Fold-specific content (`Fold 1`, `Q4 2021`, and `2020` in a COVID-overfit warning). The current system has no hard Taste-content guard, so the run is operationally successful but the Taste should be treated as having a portability defect before injecting into later Fold prompts.
- Validation: resource checks before and after showed roughly 423-425 GiB available memory; GPUs were occupied by unrelated jobs. `py_compile` for prompt/export scripts passed; `git diff --check` -> clean; generated `__pycache__` directories were removed; `find src tests scripts -type d -name __pycache__ -print` -> no output; no matching Docker sandbox remained running after completion.

Follow-up prompt contract cleanup:
- Updated `src/hl_trader/agent/prompts.py` so the sample-window explanation lives in `# 角色与目标`, emphasizing cross-period reuse and real-world investment relevance; removed the same explanation from `Pipeline流程`.
- Rewrote `Taste 输出合同` as a fixed three-section format: `投资理念与机制假设`, `重点技术与资源使用建议`, and `历史经验、失败教训与正则化原则`.
- Removed concrete method examples from the technology section and neutralized remaining Fold/date cues such as `Fold 1/2/3` examples and `第一个 Fold` wording in meta-learning prompt text.
- Regenerated `configs/prompts/PROMPTS.md`.
- Validation: resource checks before and after prompt export showed about 425-426 GiB available memory; GPUs were occupied by unrelated jobs. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> OK. `git diff --check -- src/hl_trader/agent/prompts.py configs/prompts/PROMPTS.md` -> clean.

## 2026-06-26 Compact and Explore prompt refinement

Task: execute the accepted OpenCode/Claude-Code-inspired improvements for context compaction and Explore SubAgent behavior, while only advising on Meta Taste, tool-detail placement, and experiment-fact injection.

Changes:
- `src/hl_trader/agent/compact.py`: context compaction request now carries `previous_summary` plus `messages_since_previous_summary`; previous compaction summaries are not re-embedded as ordinary conversation history. The requested output schema is an anchored continuation state: `goal`, `constraints_and_preferences`, `progress`, `key_decisions`, `errors_and_fixes`, `next_steps`, `critical_context`, `relevant_files`, and `recent_user_feedback`.
- `src/hl_trader/agent/compact.py`: normalizer accepts the new schema and maps old compact payloads into it for compatibility.
- `src/hl_trader/environment/explore.py`: Explore prompt now frames the sub-agent as a read-only investigator that answers the delegated question, preserves evidence, avoids hidden errors, avoids full-table reads, and does not design final strategies, write Taste, or do global synthesis for the main Agent.
- `docs/agent_design.md` and `docs/environment_design.md`: documented the anchored compact summary and Explore digest boundary.
- `tests/unit/test_tools_flow.py`: added regression coverage for compact previous-summary anchoring and Explore prompt boundary.

Validation:
- Resource check before tests: about 429 GiB available memory; GPUs 5 and 6 were free, other GPUs had unrelated jobs.
- First targeted unittest command used the wrong class name for Explore tests; compact tests passed, and unittest reported `AttributeError` for the misnamed Explore test targets. Retried with `AgentSessionRunnerTest`, then ran the corrected combined target set.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest.test_context_compactor_reserves_time_for_next_main_call tests.unit.test_tools_flow.AgentSessionRunnerTest.test_context_compaction_request_strips_runner_internal_fields tests.unit.test_tools_flow.AgentSessionRunnerTest.test_context_compaction_request_anchors_previous_summary tests.unit.test_tools_flow.AgentSessionRunnerTest.test_runner_compacts_long_context_with_dedicated_proxy tests.unit.test_tools_flow.AgentSessionRunnerTest.test_runner_recomputes_deadline_after_compaction_before_main_call tests.unit.test_tools_flow.AgentSessionRunnerTest.test_runner_traces_compaction_failure_and_opens_circuit tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_subagent_returns_digest_via_dispatch tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_salvages_digest_after_length_cutoff tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_subagent_runs_read_only_tools tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_subagent_handles_parallel_grep_glob_calls tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_uses_fold_deadline_for_proxy_timeout tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_search_helpers_reject_expired_deadline tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_llm_error_trace_redacts_bearer_token` -> 13 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/compact.py src/hl_trader/environment/explore.py tests/unit/test_tools_flow.py` -> OK.
- `git diff --check -- src/hl_trader/agent/compact.py src/hl_trader/environment/explore.py tests/unit/test_tools_flow.py docs/agent_design.md docs/environment_design.md` -> clean.
- Removed py_compile-generated caches under `src/hl_trader/environment/__pycache__`, `src/hl_trader/agent/__pycache__`, and `tests/unit/__pycache__`; `find src tests -name __pycache__ -o -name '*.pyc'` -> no output.

## 2026-06-26 Tool schema detail sinking

Task: keep the system Prompt tool table, but move operational tool details closer to each tool's native schema and error contract.

Changes:
- `src/hl_trader/environment/tools/base.py`: added `ActionField.description`; field descriptions now appear in both `tool_spec` records and provider-native JSON Schema. Optional defaults are appended to the same schema description instead of replacing tool guidance.
- `src/hl_trader/environment/tools/shell.py`: clarified shell usage, large parquet access guidance, stderr visibility, `command`, `max_output_chars`, and `timeout_seconds`.
- `src/hl_trader/environment/tools/search.py`: added grep/glob field descriptions for root/path/glob/output mode/pagination/context/multiline behavior.
- `src/hl_trader/environment/tools/artifact_io.py`: added write/edit field descriptions for root, relative path, content, old/new strings, and replace-all semantics.
- `src/hl_trader/environment/tools/web_search.py`: added engine, perspective, query, and max-results descriptions; search results are framed as evidence, not trading labels.
- `src/hl_trader/agent/runner.py`: added descriptions for note, explore task/max_rounds, and meta-learning done; descriptions for backtest/modification_check/finish_fold were clarified in their tool modules.
- `docs/environment_design.md`: documented the split: system Prompt keeps the tool table and key boundaries, while parameter semantics, output budgets, pagination, retry hints, and failure reasons live in tool schema and structured `ToolError`.
- `tests/unit/test_tools_flow.py`: added regression coverage proving field descriptions reach native tool schemas.

Validation:
- Resource check before work showed about 415 GiB available memory. GPUs were occupied by unrelated jobs; this task used CPU-only tests.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_tools_flow.AgentSessionRunnerTest.test_tool_schemas_include_actionable_field_descriptions tests.unit.test_tools_flow.AgentSessionRunnerTest.test_runner_validates_action_schema_and_records_deadline_cancellation tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_subagent_returns_digest_via_dispatch` -> 3 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py src/hl_trader/environment/tools/artifact_io.py src/hl_trader/environment/tools/web_search.py src/hl_trader/environment/tools/backtest.py src/hl_trader/environment/tools/modification_check.py src/hl_trader/environment/tools/finish_fold.py src/hl_trader/agent/runner.py tests/unit/test_tools_flow.py` -> OK.
- `git diff --check -- src/hl_trader/environment/tools/base.py src/hl_trader/environment/tools/shell.py src/hl_trader/environment/tools/search.py src/hl_trader/environment/tools/artifact_io.py src/hl_trader/environment/tools/web_search.py src/hl_trader/environment/tools/backtest.py src/hl_trader/environment/tools/modification_check.py src/hl_trader/environment/tools/finish_fold.py src/hl_trader/agent/runner.py tests/unit/test_tools_flow.py docs/environment_design.md` -> clean.
- Removed py_compile-generated caches under `src/hl_trader/environment/tools/__pycache__`, `src/hl_trader/agent/__pycache__`, and `tests/unit/__pycache__`; `find src tests -name __pycache__ -o -name '*.pyc'` -> no output.

## 2026-06-26 Meta sandbox image rebuild and manifest cleanup

Task: implement the accepted two-layer dependency inheritance design: meta-learning declares the desired environment, then Pipeline rebuilds a derived Sandbox image for subsequent Fold/held-out runs. Also complete the previous prompt/fact/manifest cleanup and report the final state.

Changes:
- `src/hl_trader/pipelines/config.py`: added `meta_sandbox_rebuild_enabled` and `meta_sandbox_rebuild_timeout_seconds`.
- `scripts/experiments/run_experiment.py`: added `--disable-meta-sandbox-rebuild`; default behavior is to honor meta-learning `workspace/sandbox_environment.json`.
- `src/hl_trader/pipelines/experiment.py`: tracks `_active_sandbox_spec`; meta-learning still uses `meta_learning_sandbox_spec`, while ordinary Fold and held-out runs use the active ordinary spec. After meta-learning, `_maybe_rebuild_sandbox_image()` reads `workspace/sandbox_environment.json`, validates the minimal JSON schema, renders a derived Dockerfile from the current ordinary base image, runs `docker build`, and switches `_active_sandbox_spec.image` on success. Non-empty build requests fail the experiment on timeout or Docker build failure; no silent fallback.
- `src/hl_trader/pipelines/experiment.py`: `sandbox_image_update` is recorded in the run manifest and meta-learning ledger. Build artifacts are stored under `experiments/<id>/sandbox_images/<epoch>/` with the sanitized request copy and generated Dockerfile.
- Removed the previously considered `models/python_packages` inheritance path. `models/` remains only for model parameters, weights, and model metadata; Python/npm/apt dependencies belong to the Sandbox image layer.
- `src/hl_trader/environment/runtime.py`: preserved two manifest views. `/mnt/artifacts/run_manifest.json` is the Agent-visible public projection; `runtime/host_run_manifest.json` is host-only and keeps full audit/test scheduling data.
- `src/hl_trader/environment/sandbox.py`: artifact collection copies the host-only manifest to collected run artifacts as `host_run_manifest.json`; runtime policy wording now points persistent dependencies to the image layer.
- `src/hl_trader/pipelines/experiment.py`: meta development inputs use Agent-visible ledger records only. The public ledger projection is allowlist-based, strips test/held-out result fields, strips host-only paths, and maps `fold_id` / strategy artifact IDs to opaque refs to avoid indirect time-window leakage.
- `src/hl_trader/agent/prompts.py`: meta-learning prompt now documents the `sandbox_environment.json` contract and clarifies that workspace-only installs/cache do not inherit.
- `docs/agent_design.md`, `docs/environment_design.md`, and `docs/pipeline_design.md`: documented the two-layer model: output/model artifacts inherit as files, dependencies inherit only by rebuilding the Sandbox image from meta-learning's environment request.
- `configs/prompts/PROMPTS.md`: regenerated from prompt source.
- `check.md`: overwritten with complete rendered Fold Agent and Meta Learning Agent example prompts for manual audit.
- Tests updated in `tests/unit/test_pipeline_e2e.py`, `tests/unit/test_step_tree.py`, `tests/unit/test_artifacts.py`, and `tests/unit/test_tools_flow.py` to cover public/host manifest separation, meta experiment facts, opaque Agent-visible ledger refs, sandbox image rebuild request handling, model artifact boundaries, and tool schema descriptions.

SubAgent audit:
- Opened a GPT-5.5 High SubAgent for code/design audit. Main conclusion: public manifest + host manifest are not redundant because they serve different trust boundaries; public is the Agent contract, host is audit/orchestration truth. The recommended dependency inheritance mechanism is the derived Sandbox image, not copying installed packages into artifacts. The audit also flagged indirect raw Fold labels in Agent-visible development records; this was fixed by opaque refs and stricter allowlists.

Validation:
- Resource checks before tests: about 415 GiB available memory; GPUs were occupied by unrelated jobs. After tests: about 414 GiB available memory; no new unsafe resource pressure.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/pipelines/experiment.py src/hl_trader/pipelines/config.py scripts/experiments/run_experiment.py src/hl_trader/environment/runtime.py src/hl_trader/agent/prompts.py` -> OK.
- Targeted Pipeline tests:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_single_epoch_runs_meta_learning_before_fold_and_heldout tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_injects_full_records_and_prior_epoch_logs tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_development_history_uses_compact_fold_summaries tests.unit.test_pipeline_e2e.PipelineEndToEndTest.test_meta_learning_environment_request_builds_derived_sandbox_image` -> 4 tests OK.
- Broader targeted tests:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_pipeline_e2e` -> 24 tests OK.
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_step_tree tests.unit.test_artifacts tests.unit.test_tools_flow.AgentSessionRunnerTest.test_tool_schemas_include_actionable_field_descriptions tests.unit.test_tools_flow.AgentSessionRunnerTest.test_explore_subagent_returns_digest_via_dispatch` -> 21 tests OK.
- Full suite:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m unittest discover -s tests` -> 290 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> regenerated `configs/prompts/PROMPTS.md`.
- `git diff --check` -> clean.
- `find src tests scripts -type d -name __pycache__ -print` -> no output after cleanup.

Notes:
- The retained host manifest is intentionally not mounted to the Agent. It preserves full run details for host audit and debugging while keeping the Agent-visible manifest/test boundary concise.
- Step tree node IDs still use internal lineage names in the host-side tree. Agent prompt and public manifest projections use opaque refs where they are part of prompt/development inputs; a deeper StepTree ID rewrite was not taken in this task because it would change artifact lineage semantics and was not required for the requested dependency inheritance work.

## 2026-06-26 Prompt export readability cleanup

Task: inspect `configs/prompts/PROMPTS.md` readability after the prompt/schema refactor and clean up the generated Markdown structure without changing the underlying prompt contents.

Changes:
- `scripts/dev/export_prompts.py`: added explicit anchors, a navigation list, numbered sections, and `<details>` wrappers around each full prompt body. The first full Fold prompt remains open by default; other long prompt blocks are collapsed to reduce page noise.
- `configs/prompts/PROMPTS.md`: regenerated from the exporter. The prompt bodies remain inside `text` fences so reviewers can audit exactly what the model receives, but the surrounding document is now scannable.

Validation:
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile scripts/dev/export_prompts.py` -> OK.
- `git diff --check -- scripts/dev/export_prompts.py configs/prompts/PROMPTS.md` -> clean.

Follow-up:
- Opened GPT-5.5 High Explorer SubAgent `019f0437-890c-7101-b09d-49d44fc61268` for read-only audit of `configs/prompts/PROMPTS.md` section 7 and the exporter. It confirmed section 7 should not be a standalone “追加片段” prompt, because it is only appended to the meta-learning system prompt when an experiment directive is present; it also flagged nested Markdown fence breakage caused by outer ```text blocks containing inner ```json blocks. The SubAgent was closed after completion.
- `scripts/dev/export_prompts.py`: replaced the standalone directive fragment export with a full `build_meta_learning_prompt(..., experiment_directive=...)` example titled `元学习 Agent System Prompt（含实验级探索方向示例）`.
- `scripts/dev/export_prompts.py`: changed exported prompt fences from triple backticks to four-backtick fences so inner prompt fences such as ```json stay literal and do not break the Markdown structure.
- `src/hl_trader/agent/prompts.py`: added `/mnt/agent/workspace/sandbox_environment.json` to the meta-learning prompt's `可读写文件` table as an optional writable environment request file; removed a line-continuation artifact that made the next section header too close.
- Regenerated `configs/prompts/PROMPTS.md` and `check.md`.
- Validation: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python scripts/dev/export_prompts.py` -> OK. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /home/lzp/miniconda3/envs/quant/bin/python -m py_compile src/hl_trader/agent/prompts.py scripts/dev/export_prompts.py` -> OK.

## 2026-06-26 Rename project hl_trader → AutoTrade (full runtime ABI)

- Task: first step of the AutoTrade feature line — rename the package and the agent-facing runtime ABI before the per-minute `main(ctx)` engine work, so the rename diff does not collide with the feature diff. Approved plan: `/home/coder/.claude/plans/fluttering-petting-boot.md`.
- Base: committed the in-flight decouple-broker working tree as `c3f6a2c` on `refactor/decouple-broker-strategies` (full suite 290 green), then branched `refactor/rename-autotrade`.
- Package: `git mv src/hl_trader src/autotrade` (history preserved as git renames); rewrote every `hl_trader.` import across src/tests/scripts/ops; `pyproject` name `macroquant-hl-trader` → `autotrade`; reinstalled editable (`pip install -e .`).
- Runtime ABI: sandbox tools module `mq_tools` → `at_tools`; env vars `MQ_*` → `AT_*` (SNAPSHOT_DIR, AGENT_OUTPUT_DIR, MODEL_DIR, DECISION_TIME, REPLAY_GRANULARITY, NL_*, FORBIDDEN_PATHS, WRITE_FORBIDDEN_PATHS, DISABLE_LINKS, WORKSPACE_DIR, PROXY_*); Docker image tag `macroquant-sandbox` → `autotrade-sandbox` (sandbox.py DEFAULT_IMAGE, experiment.py derived tag, ops/docker/sandbox.Dockerfile, e2e test assertions).
- Branding: `MacroQuant`/`macroquant` → `AutoTrade`/`autotrade` in code docstrings, the 5 living design docs, `configs/agent_output_template/*`, and regenerated `configs/prompts/PROMPTS.md`.
- Deliberately preserved: live-cron block markers `# BEGIN/END MacroQuant TuShare update` and `MACROQUANT_ROOT` in `ops/cron/*` (renaming them would orphan the installed crontab block and create duplicates on next install); filesystem path `/Data/lzp/MacroQuant` (82 refs); LOGBOOK.md / DETAILED_LOGBOOK.md historical entries (chronicle of old names).
- Follow-up required: rebuild the sandbox image under the new tag before any Docker experiment/meta-learning run, else the pipeline cannot find the image: `docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile ops/docker`.
- Validation: `python -m compileall src tests scripts ops` OK; `python -c "import autotrade"` OK and `import hl_trader` now fails (expected); full `python -m unittest discover -t . -s tests -p "test_*.py"` 290 passing (skipped=2, Docker-gated); `git grep` shows no residual `hl_trader`/`mq_tools`/`MQ_` in tracked files except preserved logbook history; `PROMPTS.md` regenerated with only `at_tools`/`AT_` tokens. Memory ~387Gi available before/after; no GPU work.

## 2026-06-26 PR2: unified per-minute main(ctx) execution engine

- Task: replace the "decide-once run_strategy → fixed trade_intents → per-stock trade_strategy(ctx) per bar" model with a single persistent main(ctx) process the Environment calls once per replay minute (market-level ctx, ts_code-keyed Broker primitives), so the Agent can open/adjust positions at any minute. Approved plan: `/home/coder/.claude/plans/fluttering-petting-boot.md`. Branch `feat/main-ctx-engine` off `refactor/rename-autotrade`.
- Engine (`df662c3`): new `src/autotrade/environment/main_ctx_engine.py` — `_MAIN_DRIVER` (one persistent sandbox process serving a per-minute RPC), `MainPolicyRunner` (step(state)->actions, pumps NL while waiting), `run_main_ctx_replay` (minute loop building market state, applying actions via SimBroker.execute). Reuses MarketData/MinuteMarketData/minute-fallback/compute_return_stats/path-guard/NL-RPC from backtest_engine. New `tests/unit/test_main_ctx_replay.py` proves a main(ctx) opening a new long mid-replay (day 2) via ctx.broker.buy.
- Tool wiring (`28ebb67`): `tools/backtest.py` replays via MainPolicyRunner + run_main_ctx_replay; results = `detailed_return.json` + `orders.parquet` (dropped trade_intents/candidates/strategy_metadata). `artifacts.py` now requires `main` (was `run_strategy`). Agent output template rewritten to `main(ctx)` (`main.py`/`candidate.py`/`trading.py`); `fixtures_sandbox.py` + `test_tools_flow.py` + `test_artifacts.py` migrated; obsolete inverted-premise tests removed (decision-stage main(ctx) legitimately exposes nl/model_dir).
- Old-path removal (`14170af`): deleted the two old drivers, run_strategy_program, StrategyPolicyRunner, run_trade_intent_replay, validate_trade_intents, and helpers via an AST pass (`backtest_engine.py` 1568→639 lines). `test_broker_engine.py`: SimBroker primitive tests unchanged; replay tests migrated to run_main_ctx_replay via FakeMainPolicy; trade-intent validation tests dropped.
- Docs (WS6): Fold system prompt (`agent/prompts.py`) trading-contract section, artifact-format lines, backtest tool row, submit contract, and `strategy_entry_function` fact → `main(ctx)`; regenerated `configs/prompts/PROMPTS.md`. Rewrote `configs/agent_output_template/README.md`, `docs/agent_design.md` §5, `docs/environment_design.md` §6.1/§7.1/§7.2/§7.6, `docs/pipeline_design.md` §4.2 to the main(ctx) contract.
- ctx surface: cur_date/cur_time, account/positions/cash, price(ts_code)/bar(ts_code)/bars (current minute only), broker.buy/sell/short/cover/close(ts_code, amount|weight)/position(ts_code), nl(ts_code, prompt=...), snapshot_dir/model_dir/state_dir/params. Minute-level PIT: ctx exposes only bars with close ≤ cur_time.
- Not yet implemented (next): full rolling per-day as-of view (WS2 — screening currently reads the Fold-decision-time frozen snapshot for daily domains); pre-open auction 09:15 + Broker auction fill (PR3); host NL hard cap nl_max_calls_per_backtest + auction knobs (PR4 / config); backtest_tool replay_window debug param (PR4).
- Validation: `python -m compileall src tests scripts ops` OK; full `python -m unittest discover -t . -s tests -p "test_*.py"` 282 passing (skipped=2, Docker-gated); `scripts/dev/export_prompts.py` regenerated PROMPTS.md with only at_tools/AT_ and main(ctx) terms. Memory ~387Gi available; no GPU work.

## 2026-06-26 main(ctx) engine audit fixes + pre-open call-auction (PR3)

- Audit: launched a read-only general-purpose SubAgent to review main_ctx_engine.py / tools/backtest.py / broker.py / backtest_engine.py for correctness before extending. Findings actioned in commit `04c8283` (branch `feat/main-ctx-engine`):
  - H1 (look-ahead, HIGH): `_synthetic_daily_minutes` built the 09:30 "open" bar from a full row copy, leaking day high/low and full-day vol/amount through `ctx.bar()/ctx.bars`. Fixed: the 09:30 open bar exposes only the opening price (high=low=open, vol=amount=NaN); the 15:00 close bar keeps day extremes/aggregates. Regression test `test_open_bar_has_no_intraday_lookahead`.
  - H2 (deadlock, HIGH): the persistent driver redirects the Agent's stdout to stderr, but the host only read stdout; a full 64KB stderr pipe blocked the driver mid-step → spurious deadline kill. Fixed: a daemon thread drains `proc.stderr` into a bounded `deque(maxlen=400)`; `_drain_stderr` reads that buffer; `close()` joins the drainer and closes all pipes instead of `communicate()` (which would race the drainer on stderr).
  - M1 (timeout headroom, MED): the per-step deadline is now an inactivity timer reset whenever an NL request is served, so one slow `nl()` cannot exhaust the per-minute budget.
  - L2 (consistency, LOW): an Agent `main.py` import error is captured and returned as a structured error on the first request instead of crashing the persistent process.
  - Not changed (rationale recorded): M2 O(N^2) NL file re-reads (bounded by usage + the coming NL cap), L1 optimistic intra-minute broker view (by design; host stays authoritative), L3 raw os.write(1) (exotic; Docker/path-guard bound).
- Pre-open auction (commit `436222c`, branch `feat/preopen-auction` off the fixed PR2 branch): `run_main_ctx_replay` prepends, per replay day, a call-auction tick at `auction_decision_time` (default `09:25`) built by `_auction_rows` from each code's first bar — open price only (high=low=open, vol=amount=NaN), sorted before 09:30. Entries placed there fill at the open; the Broker reuses the daily-bar limit checks (so a one-sided limit-up open rejects buys, limit-down rejects shorts) and the fill is labelled `price_label="auction"`. `09:15` has no matched auction price (price discovery 09:15–09:25), so the priced backtest tick is `09:25`; `09:15` is a live/QMT pre-commit point only. Config knobs `auction_enabled` (default True), `auction_decision_time`, and `nl_max_calls_per_backtest` (PR4 backstop, plumbing only so far) flow `ExperimentConfig` → Fold run manifest → `backtest_tool` (`manifest.get(...)`) → `run_main_ctx_replay`. Docs: `environment_design.md` §7.2 updated.
- Validation: `python -m compileall` OK; full `unittest discover` 285 passing (skipped=2). Memory ~387Gi available; no GPU work. Remaining: PR4 NL hard cap enforcement + `backtest_tool` `replay_window` debug param; WS2 full rolling per-day as-of view.

## 2026-06-26 PR4: NL call cap + backtest_tool replay_window (feat/agent-controls)

- NL hard cap: `_StrategyNLService` (tools/backtest.py) counts calls and, past `nl_max_calls_per_backtest` (read from the Fold run manifest), returns an audited `state="budget_exhausted"` error result instead of calling the provider; strategy code degrades per the prompt's low-frequency guidance. The backtest summary reports `nl_calls` and `nl_max_calls_per_backtest`. Default cap is None (prompt-guided only). Test: `test_nl_scoring.NLBudgetTest`.
- `backtest_tool` `replay_window`: new optional Agent ActionField; when set, `_execute` keeps only the first N trade days of `replay_daily`/minutes, sets `complete_validation=False`, and skips the step-tree record, so a short debug run is never accept-eligible/freezable. The full run stays default and `frozen_eval` forces full. The Runner threads the Agent's `replay_window` arg through `self.backtest.run(mode="valid", replay_window=(args or {}).get("replay_window"))`. Test: `test_tools_flow.ToolFlowTest.test_replay_window_is_a_non_freezable_debug_run`.
- Docs: Fold system prompt (`agent/prompts.py`) backtest tool row + NL cost note updated; `PROMPTS.md` regenerated.
- Validation: full `unittest discover` 287 passing. Remaining: WS2 full rolling per-day as-of view (screening currently reads the Fold-decision-time frozen snapshot for daily domains; minute-level PIT is already enforced via ctx).
