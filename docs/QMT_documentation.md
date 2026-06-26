# QMT 文档

本文档记录 MacroQuant 项目接入阿里云 Windows + MiniQMT 的部署状态、当前日常流程和未来实盘上线门槛。当前阶段模型尚未训练完成，仓库也没有活动的 live 下单脚本，因此 QMT 侧只能作为已部署的执行环境保持 standby、只读检查和 dry-run 准备；不得启动自动实盘交易。

相关边界：

- 数据下载、单位和 raw 审计见 `docs/data_documentation.md`。
- Agent 工作合同和策略产物格式见 `docs/agent_design.md`。
- PIT 窗口、Sandbox、Tool 和回测见 `docs/environment_design.md`。
- 研究侧 Pipeline、冻结和 held-out 流程见 `docs/pipeline_design.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| PIT | 只使用决策时点已经可见的数据，避免未来信息进入订单 |
| WFO | Walk-Forward 训练和测试流程；只有冻结后的结果可以进入实盘候选 |
| LLM shadow | 大模型只做影子审计或建议，不直接改订单 |
| ledger | 研究侧实验账本；不等于券商成交和持仓 |
| payload | 本机生成并上传给远端执行器的订单 JSON |
| dry-run | 只检查解析、风控和预算，不向券商发真实委托 |
| state | 远端记录的策略持仓、待处理委托和已处理订单状态 |

## 导航

- [1. 当前状态](#1-当前状态)
- [2. 目标架构](#2-目标架构)
- [3. 当前日常流程](#3-当前日常流程)
  - [3.1 准备和健康检查](#31-准备和健康检查)
  - [3.2 只读检查命令](#32-只读检查命令)
- [4. 未来实盘流程](#4-未来实盘流程)
  - [4.1 上线后日常顺序](#41-上线后日常顺序)
- [5. 上线门槛](#5-上线门槛)
- [6. 远端部署](#6-远端部署)
  - [6.1 固定目录](#61-固定目录)
  - [6.2 QMT 路径和官方参考](#62-qmt-路径和官方参考)
  - [6.3 远端 Python 与环境变量](#63-远端-python-与环境变量)
  - [6.4 本金口径](#64-本金口径)
- [7. Payload 草案](#7-payload-草案)
  - [7.1 Payload Schema](#71-payload-schema)
  - [7.2 执行语义](#72-执行语义)
- [8. Dry-run 与实盘执行](#8-dry-run-与实盘执行)
  - [8.1 上传和执行命令](#81-上传和执行命令)
  - [8.2 成交对账](#82-成交对账)
- [9. 故障处理](#9-故障处理)
  - [9.1 常见故障](#91-常见故障)

## 1. 当前状态

- 远端阿里云 Windows 服务器和 QMT/MiniQMT 环境已部署，可作为未来交易执行端。
- 本项目当前代码重点仍是数据、PIT snapshot、WFO/held-out、LLM shadow 和审计链路；尚未形成冻结可交易模型。
- 当前仓库没有 `scripts/live/` 实盘调度入口，也没有已冻结的 MacroQuant 订单生成器。
- 任何 QMT 操作默认只读或 dry-run。真实委托必须等到模型、策略、订单合约、风控和对账流程全部冻结后，才允许人工双确认执行。

## 2. 目标架构

- 本机 Linux：负责 TuShare/本地 raw 数据更新、审计、PIT snapshot 构造、模型推理、信号审计、订单 payload 生成。
- 远端 Windows：负责 QMT/MiniQMT 连接、账户/持仓/成交查询、订单执行、策略 state、pending 委托和 payload 归档。
- 通信：本机通过 `scp` 上传 JSON payload，通过 `ssh` 调用远端 Python 执行器。
- 状态：远端策略 state 是实盘对账的权威来源；本机实验 ledger 只能作为研究和审计记录，不能替代 broker 成交状态。

## 3. 当前日常流程

在模型未训练完成之前，日常流程只做准备和健康检查：

### 3.1 准备和健康检查

1. 盘前或盘中只读检查远端 QMT。
   - 确认 QMT 已登录，账户 ID 正确。
   - 确认 `cash`、`total_asset`、`market_value` 可读。
   - 确认远端 `pending_orders`、`strategy_positions` 和 `inbox` 没有无法解释的旧状态。
2. 本机继续维护研究数据。
   - 增量下载和审计 raw 数据。
   - 构造 PIT snapshot 和 evidence pack。
   - 运行 development / held-out / LLM shadow pipeline，不生成真实订单。
3. 仅在需要验证执行链路时，使用人工构造的小额测试 payload 做 dry-run。
   - dry-run 只验证远端解析、账户读取、预算计算和风险检查。
   - 未经单独批准，不提交真实委托。

### 3.2 只读检查命令

只读检查命令：

```bash
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py status"
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py reconcile"
```

`reconcile` 不下单，只用 QMT 当日成交同步远端策略 state 和 pending 委托。

## 4. 未来实盘流程

未来上线后，日常流程应按“先冻结、再生成、再 dry-run、最后人工确认”的顺序执行：

### 4.1 上线后日常顺序

1. 收盘后或指定决策时点构造 PIT snapshot。
   - 默认日频策略只允许使用当时已可见数据。
   - 09:25 盘前决策必须有独立的数据合同，不能使用当日 `daily` / `daily_basic`。
2. 运行冻结模型或冻结规则。
   - development 阶段结果不得直接下单。
   - held-out 通过后，必须记录模型版本、配置 hash、数据合同 hash、ledger hash。
3. 生成 MacroQuant 订单 payload。
   - 建议路径：`artifacts/live/orders/macroquant_<strategy_id>_<YYYYMMDD>_<action>.json`。
   - payload 必须包含策略 ID、交易日期、模型版本、输入数据 hash、订单列表和风险标签。
4. 上传远端并先 dry-run。
   - 检查预算、股数、涨跌停/停牌、可用持仓、重复 payload、账户 ID。
5. 人工确认后才允许真实执行。
   - 实盘命令必须带双确认参数。
   - 下单后必须运行对账，不能把委托号当成成交。

## 5. 上线门槛

真实交易前至少满足：

- 已有冻结的 strategy config、model ID、prompt/model provider 版本和数据合同。
- held-out 或 quasi-forward 评估结果已审计，并明确允许进入 paper/live 阶段。
- `can_affect_trading=true` 的组件必须经过单独审计；当前 LLM shadow 默认不能影响交易。
- 本机订单生成器和远端执行器的 payload schema 已冻结，并有单元测试或 dry-run 样例。
- 远端 `inbox`、`pending_orders`、`strategy_positions` 状态干净或可解释。
- 实盘规模、单票上限、行业/组合约束、跌停/停牌处理、T+1 约束和最大回撤停机规则已写入配置。
- 手工仓位和策略仓位边界明确，卖出逻辑不会误卖非策略仓位。
- 已完成小额或模拟 dry-run 全链路：生成 payload、上传、远端解析、预算计算、拒单检查、reconcile。

## 6. 远端部署

### 6.1 固定目录

远端建议固定目录：

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

`inbox` 只放待执行 payload；执行过、测试过或废弃的 payload 应移到 `archive`，避免误执行。

### 6.2 QMT 路径和官方参考

QMT 常见路径：

```text
C:\国金证券QMT交易端
C:\国金证券QMT交易端\userdata_mini
```

Windows 上应能看到 `XtMiniQmt.exe` 和 `miniquote.exe`。官方参考：

- XtQuant 快速开始: http://dict.thinktrader.net/nativeApi/start_now.html
- XtTrader 交易接口: http://dict.thinktrader.net/nativeApi/xttrader.html
- 代码示例: http://dict.thinktrader.net/nativeApi/code_examples.html

### 6.3 远端 Python 与环境变量

远端 Python 建议使用独立 Python 3.8，不改系统 PATH：

```powershell
C:\xquant\Python38\python.exe -m pip install --upgrade pip
C:\xquant\Python38\python.exe -m pip install xtquant pandas numpy requests tqdm
C:\xquant\Python38\python.exe -c "import xtquant; print('xtquant ok')"
```

环境变量：

```powershell
setx CQ_QMT_DATA_PATH "C:\国金证券QMT交易端\userdata_mini"
setx CQ_XQUANT_ROOT "C:\xquant"
setx CQ_EXPECTED_ACCOUNT_ID "<account_id>"
```

### 6.4 本金口径

如需限制策略总规模：

```powershell
setx CQ_MAX_PRINCIPAL "100000"
```

不设置 `CQ_MAX_PRINCIPAL` 时，执行器默认使用 MiniQMT 返回的 `total_asset` 作为本金口径。是否采用这个口径必须与回测资金口径一致。

## 7. Payload 草案

当前 payload schema 尚未冻结；以下只作为 MacroQuant 后续实现参考。真实接入前必须与远端 `qmt_executor.py` 实际代码核对。

### 7.1 Payload Schema

```json
{
  "schema_version": 1,
  "project_id": "macroquant",
  "strategy_id": "macroquant_hl_daily_rebalance",
  "payload_id": "macroquant_hl_daily_rebalance_20260601",
  "trade_date": "20260601",
  "decision_time": "2026-05-31T20:30:00+08:00",
  "action": "rebalance",
  "principal_mode": "account_total_asset",
  "daily_buy_limit_ratio": 0.5,
  "model_id": "frozen_model_or_rule_id",
  "config_hash": "<hash>",
  "data_contract_hash": "<hash>",
  "ledger_hash": "<hash>",
  "orders": [
    {
      "code": "000001.SZ",
      "side": "BUY",
      "volume": 100,
      "price_type": "LATEST",
      "limit_price": null,
      "reason": "budgeted_buy",
      "risk_tags": []
    }
  ]
}
```

### 7.2 执行语义

当前执行语义建议：

- 无 `principal` 时，远端读取账户 `total_asset`。
- 每日买入预算可设为 `min(account_cash, total_asset * daily_buy_limit_ratio)`。
- 买入必须跳过 broker 已有持仓或策略 state 已有持仓的股票，除非 action 明确允许加仓。
- 卖出只根据远端策略 state 和 broker 可用持仓生成，不卖出非本策略仓位。
- `rebalance` 最终应在本机拆成明确的 BUY/SELL 订单，远端不负责理解研究语义。

## 8. Dry-run 与实盘执行

### 8.1 上传和执行命令

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

### 8.2 成交对账

不要把 `order_stock` 返回值当成交。返回值只是委托号；成交后必须用 `reconcile` 根据 MiniQMT 当日成交回填策略 state。

## 9. 故障处理

### 9.1 常见故障

- 连接失败：检查 QMT 是否登录、`userdata_mini` 路径是否正确、`XtMiniQmt.exe` / `miniquote.exe` 是否运行。
- 多账户：设置 `CQ_EXPECTED_ACCOUNT_ID`，禁止自动选择。
- 重复 payload：默认拒绝；确需重跑时，先核对 state、委托、成交，再用显式 repeat 开关。
- pending 未清：运行 `reconcile`，确认当日成交和委托状态。
- inbox 有旧文件：移动到 `C:\xquant\archive\...`，不要直接手工执行。
- 本机模型或数据状态不确定：停止生成 live payload，只保留研究 ledger 和 dry-run。
