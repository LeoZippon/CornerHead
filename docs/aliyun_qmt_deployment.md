# Aliyun MiniQMT Deployment Guide

本文档用于把“本机生成信号，阿里云 Windows 服务器连接 MiniQMT 执行交易”的流程迁移到其他量化项目。

## 架构

- 本机：负责数据更新、模型推理、生成买卖 payload。
- 阿里云 Windows 服务器：运行国金证券 QMT/MiniQMT，通过 `xtquant` 查询账户并提交委托。
- 通信：本机用 `scp` 上传 JSON payload，用 `ssh` 调用远端 Python 执行器。
- 状态：远端维护策略持仓、pending 委托、已处理 payload，不能只依赖本机状态。

## 前置条件

- 阿里云 Windows 服务器已可 SSH 登录，例如：

```bash
ssh Administrator@<server_ip>
```

- QMT 已安装并登录，MiniQMT 可用，常见路径：

```text
C:\国金证券QMT交易端
C:\国金证券QMT交易端\userdata_mini
```

- Windows 上能看到 `XtMiniQmt.exe` 和 `miniquote.exe`。
- 官方参考：
  - XtQuant 快速开始: http://dict.thinktrader.net/nativeApi/start_now.html
  - XtTrader 交易接口: http://dict.thinktrader.net/nativeApi/xttrader.html
  - 代码示例: http://dict.thinktrader.net/nativeApi/code_examples.html

## 远端目录

建议固定使用：

```text
C:\xquant\
  Python38\
  qmt_executor.py
  inbox\
  outbox\
  logs\
  state\
  archive\
```

`inbox` 只放待执行 payload；执行过或测试 payload 应移到 `archive`，避免人工误执行。

## Python 与 xtquant

推荐远端独立安装 Python 3.8，不改系统 PATH：

```powershell
C:\xquant\Python38\python.exe -m pip install --upgrade pip
C:\xquant\Python38\python.exe -m pip install xtquant pandas numpy requests tqdm
```

如果服务器下载慢，可在本机下载 Windows/Python3.8 wheel 后 `scp` 上传，再离线安装。

验证：

```powershell
C:\xquant\Python38\python.exe -c "import xtquant; print('xtquant ok')"
```

## 远端执行器

把项目里的执行器同步到远端：

```bash
scp scripts/live/qmt_executor.py Administrator@<server_ip>:C:/xquant/qmt_executor.py
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe -m py_compile C:\\xquant\\qmt_executor.py"
```

建议设置环境变量：

```powershell
setx CQ_QMT_DATA_PATH "C:\国金证券QMT交易端\userdata_mini"
setx CQ_XQUANT_ROOT "C:\xquant"
setx CQ_EXPECTED_ACCOUNT_ID "<account_id>"
```

如需限制策略规模：

```powershell
setx CQ_MAX_PRINCIPAL "100000"
```

不设置 `CQ_MAX_PRINCIPAL` 时，执行器默认使用 MiniQMT 返回的 `total_asset` 作为本金口径。

## 只读验证

先做只读状态检查：

```bash
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py status"
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py reconcile"
```

检查点：

- `ok=true`
- 账户号符合预期
- `cash/total_asset/market_value` 可读
- `strategy_positions` 符合远端策略 state
- `pending_orders` 为空或能解释

## Payload 合约

买入 payload 示例：

```json
{
  "schema_version": 1,
  "payload_id": "strategy_20260518_buy",
  "trade_date": "20260518",
  "strategy_id": "strategy",
  "action": "buy",
  "principal_mode": "account_total_asset",
  "sizing_mode": "equal_weight_account",
  "daily_buy_limit_ratio": 0.5,
  "orders": [
    {"code": "000001.SZ", "side": "BUY", "volume": 100, "price": 10.0}
  ]
}
```

当前执行语义：

- 无 `principal` 时，远端读取账户 `total_asset`。
- 每日买入预算为 `min(account_cash, total_asset * daily_buy_limit_ratio)`。
- 默认 `sizing_mode=equal_weight_account`，远端按候选等权重新计算股数。
- 买入跳过 broker 已有持仓或策略 state 已有持仓的股票。
- 卖出只根据远端策略 state 生成，不卖出非本策略仓位。

## Dry-run 与实盘

上传 payload：

```bash
scp order.json Administrator@<server_ip>:C:/xquant/inbox/order.json
```

Dry-run：

```bash
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py execute C:\\xquant\\inbox\\order.json --dry-run"
```

真实下单必须双确认：

```bash
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py execute C:\\xquant\\inbox\\order.json --execute --confirm LIVE"
```

不要把 `order_stock` 返回值当成交。返回值只是委托号；成交后必须用 `reconcile` 根据 MiniQMT 当日成交回填策略 state。

## 本机自动化

项目可用本机 scheduler 做自动化：

```bash
scripts/live/run_live_qmt_scheduler_live.sh
scripts/live/ensure_live_qmt_scheduler.sh
```

当前模式：

- 北京时间交易日 09:25 起轮询竞价数据，竞价数据有效后生成买入 payload。
- 10:00 和 14:00 发送卖出 payload。
- 19:00 更新本机数据。
- cron 只负责 watchdog，实际交易时间由 Python 代码按 `Asia/Shanghai` 判断。

## 迁移清单

迁移到新项目时至少替换：

- `strategy_id`
- 信号生成命令
- payload 生成逻辑
- 买入预算规则
- 卖出规则
- 账户 ID 与 QMT 路径
- 远端 state 文件是否需要清空或继承

上线前检查：

```bash
git status --short
python -m py_compile scripts/live/live_qmt_workflow.py scripts/live/qmt_executor.py
bash -n scripts/live/*.sh
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py status"
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py reconcile"
```

上线前必须确认：

- 代码版本已冻结并可追踪。
- 远端 `inbox` 没有旧 payload。
- 远端策略 state 和 pending 委托符合预期。
- QMT 登录状态正常。
- dry-run 输出的股数、预算、价格类型符合预期。
- 实盘命令必须显式带 `--execute --confirm LIVE`。

## 故障处理

- 连接失败：检查 QMT 是否登录、`userdata_mini` 路径是否正确、`XtMiniQmt.exe/miniquote.exe` 是否运行。
- 多账户：设置 `CQ_EXPECTED_ACCOUNT_ID`，禁止自动选择。
- 重复 payload：默认拒绝；确需重跑时，先核对 state、委托、成交，再用显式 repeat 开关。
- pending 未清：运行 `reconcile`，确认当日成交和委托状态。
- inbox 有旧文件：移动到 `C:\xquant\archive\...`，不要直接手工执行。
