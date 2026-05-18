# Live QMT Workflow

本流程把推理和交易执行分开：

- 本机负责 Tushare 竞价数据、模型推理、信号 JSON 和订单 payload。
- 阿里云 Windows 服务器负责 MiniQMT 连接、账户/持仓查询和下单。
- 默认只 dry-run；真实委托必须同时传 `--execute-remote --confirm-live`。

通用阿里云 MiniQMT 部署和迁移步骤见 `docs/aliyun_qmt_deployment.md`。

## 每日流程

1. 09:25-09:31 中国时间，本机每 10 秒检查一次 Tushare `stk_auction`。
   - 同日沪深主板有效竞价行数默认至少 `3000` 行才视为可用。
   - 达标后运行 `scripts/live/run_wfB_signal.sh` 生成 `artifacts/live/signals/signal_YYYYMMDD.json`。
   - 再生成 `artifacts/live/orders/wfB_q50_agree60_YYYYMMDD_buy.json` 并传到阿里云。
2. 10:00 和 14:00 中国时间，本机生成卖出 payload 并传到阿里云。
   - 远端只卖 `C:\xquant\state\strategy_positions.json` 中的本策略持仓。
   - 手工持仓或非策略持仓不会自动卖出。
3. 19:00 中国时间，本机运行增量下载，只补最近窗口 raw 数据，不触发全量预处理。

## 启动

先做远端 dry-run：

```bash
scripts/live/run_live_qmt_scheduler.sh --send-remote
```

正式实盘执行：

```bash
scripts/live/run_live_qmt_scheduler.sh --execute-remote --confirm-live
```

本机 cron 守护方式会每 5 分钟检查一次 live scheduler 是否存在；不存在则启动：

```bash
scripts/live/ensure_live_qmt_scheduler.sh
```

该守护启动的是：

```bash
scripts/live/run_live_qmt_scheduler_live.sh
```

也就是固定携带 `--execute-remote --confirm-live` 的实盘模式。

单次手动命令：

```bash
/home/lzp/miniconda3/envs/stock/bin/python scripts/live/live_qmt_workflow.py buy-window --send-remote
/home/lzp/miniconda3/envs/stock/bin/python scripts/live/live_qmt_workflow.py sell --phase 10:00 --send-remote
/home/lzp/miniconda3/envs/stock/bin/python scripts/live/live_qmt_workflow.py sell --phase 14:00 --send-remote
/home/lzp/miniconda3/envs/stock/bin/python scripts/live/live_qmt_workflow.py update-data
```

把 `--send-remote` 换成 `--execute-remote --confirm-live` 才会真实委托。

## 风控边界

- 默认不写死本金；远端执行器以 MiniQMT 返回的账户 `total_asset` 作为本金口径。
- 为对齐回测，实际每日买入预算为 `min(可用现金, 账户总资产 * 0.5)`，并由远端按候选等权重新计算下单股数。
- 如需人工限制规模，可在本机传 `--principal`/`CQ_LIVE_PRINCIPAL`，或在远端设置 `CQ_MAX_PRINCIPAL` 作为硬上限。
- 买入会跳过账户里已有持仓的股票，避免和手工仓位混在一起。
- 卖出只从远端策略 state 生成，且再用 broker `can_use_volume` 截断。
- 远端下单前加状态锁并预占 payload；真实执行失败后默认不自动重试。
- `order_stock` 返回委托号不等于成交；远端只把委托放入 `pending_orders`，后续用 MiniQMT 当日成交对账后才更新策略持仓。
- 如需人工重跑同一 payload，必须先检查 `C:\xquant\state\strategy_positions.json`、当日委托和成交，再显式使用 `--allow-repeat --confirm-repeat`。

## 远端检查

```powershell
C:\xquant\Python38\python.exe C:\xquant\qmt_executor.py status
C:\xquant\Python38\python.exe C:\xquant\qmt_executor.py reconcile
```

`reconcile` 不下单，只根据 MiniQMT 当日成交同步远端策略持仓和 pending 委托。
