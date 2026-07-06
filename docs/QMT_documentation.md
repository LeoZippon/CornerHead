# QMT 文档

本文档记录 AutoTrade 项目接入阿里云 Windows + 国金全功能 QMT 客户端的部署状态、迁移架构、当前日常流程和未来实盘上线门槛。**已定决策：实盘执行使用 QMT 客户端内置 Python 策略 API（`ContextInfo`/`passorder`/`get_trade_detail_data` 等），不采用 xtquant/miniQMT 外接方案。** 当前阶段模型尚未训练完成，仓库也没有活动的 live 下单脚本，因此 QMT 侧只能作为已部署的执行环境保持 standby、只读检查和 dry-run 准备；不得启动自动实盘交易。

**相关边界**

- 数据下载、单位和 raw 审计见 `docs/data_documentation.md`。
- Agent 工作合同和策略产物格式见 `docs/agent_design.md`。
- PIT 窗口、Sandbox、Agent 工具和回测见 `docs/environment_design.md`。
- 研究侧 Pipeline、冻结和 held-out 流程见 `docs/pipeline_design.md`。
- 全部参数/超参数默认值速查见 `docs/parameters_reference.md`。

**术语说明**

| 术语 | 含义 |
|---|---|
| PIT | 只使用决策时点已经可见的数据，避免未来信息进入订单 |
| WFO | Walk-Forward 训练和测试流程；只有冻结后的结果可以进入实盘候选 |
| LLM shadow | 大模型只做影子审计或建议，不直接改订单 |
| ledger | 研究侧实验账本；不等于券商成交和持仓 |
| payload | 本机生成并传输到远端 inbox 的订单 JSON |
| 执行器 | QMT 客户端内常驻 Python 策略脚本：轮询 inbox、用内置 API 下单/撤单、回写 ack/fill/state |
| 文件桥 `QMTBroker` | 本机侧 `TraderProtocol` 适配器：把 `passorder`/`cancel` 写成 inbox 订单文件，把回写快照读成查询结果 |
| dry-run | 只检查解析、风控和预算，不向券商发真实委托 |
| state | 远端记录的策略持仓、待处理委托和已处理订单状态 |

**职责边界**

**本机 Linux 负责**

- 数据更新、审计、PIT snapshot、冻结策略推理和订单 payload 生成。
- 运行与回测相同的 `main(ctx)` 环路，经文件桥 `QMTBroker` 表达交易意图。
- 从回写的 `state/` 快照对账；本机实验 ledger 只是研究记录，不替代 broker 成交状态。

**远端 QMT 客户端内执行器负责**

| 事项 | 说明 |
|---|---|
| 订单执行 | 轮询 inbox，校验后用内置 `passorder`/`cancel` 落地 |
| 查询与回写 | `get_trade_detail_data` 快照、`order_callback`/`deal_callback` ack/fill 回写 |
| 状态持久化 | `pending_orders`、`strategy_positions`、`inbox`/`archive` |
| 禁止 | 网络访问、阻塞循环、线程、研究语义判断（rebalance 拆单在本机完成） |

**导航**

- [1. 当前状态](#1-当前状态)
- [2. 目标架构](#2-目标架构)
  - [2.1 统一逐 tick 实盘环路](#21-统一逐-tick-实盘环路)
  - [2.2 客户端内文件单执行架构](#22-客户端内文件单执行架构)
- [3. 当前日常流程](#3-当前日常流程)
  - [3.1 准备和健康检查](#31-准备和健康检查)
  - [3.2 只读检查命令](#32-只读检查命令)
- [4. 未来实盘流程](#4-未来实盘流程)
  - [4.1 上线后日常顺序](#41-上线后日常顺序)
  - [4.2 实盘下单前重校验](#42-实盘下单前重校验)
  - [4.3 实盘状态持久化](#43-实盘状态持久化)
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

- 远端阿里云 Windows 服务器和国金全功能 QMT 客户端已部署，可作为未来交易执行端（历史 miniQMT 组件仅作已部署遗留，不再是目标路径）。
- 本项目当前代码重点仍是数据、PIT snapshot、WFO/held-out、LLM shadow 和审计链路；尚未形成冻结可交易模型。
- 当前仓库没有 `scripts/live/` 实盘调度入口，也没有已冻结的 AutoTrade 订单生成器。
- 统一逐 tick 实盘环路（§2.1）是既定目标契约，但本轮不落地任何 live 下单代码；live `QMTBroker` 与本地 tick executor 尚未实现，回测仍是唯一运行路径。
- 任何 QMT 操作默认只读或 dry-run。真实委托必须等到模型、策略、订单合约、风控和对账流程全部冻结后，才允许人工双确认执行。

## 2. 目标架构

- 本机 Linux：负责 TuShare/本地 raw 数据更新、审计、PIT snapshot 构造、模型推理、信号审计、订单 payload 生成。
- 远端 Windows：运行国金全功能 QMT 客户端；客户端内置 Python 策略（§2.2 文件单执行器）负责账户/持仓/成交查询（`get_trade_detail_data`）、订单执行（`passorder`/`cancel`）、策略 state、pending 委托和 payload 归档。
- 通信：本机通过 `scp`/SMB 把 JSON payload 落入远端 inbox；执行由客户端内策略轮询接管，`ssh` 只用于文件搬运和状态快照读取等运维辅助。
- 状态：远端策略 state 是实盘对账的权威来源；本机实验 ledger 只能作为研究和审计记录，不能替代 broker 成交状态。

### 2.1 统一逐 tick 实盘环路

- 本地 executor 在 Asia/Shanghai 真实时钟上按与回测相同的 24h tick 网格逐时间片推进，每个 tick 调用同一个 `main(ctx)`，并通过同一套 `ctx.broker.*` 原语下单和撤销未成交委托：普通账户 `buy`/`sell`，信用账户 `credit_buy`/`credit_sell`/`fin_buy`/`short`/`cover`/`sell_repay`/`direct_repay`，加 `close`/`cancel`/`transfer`。实盘同样持有普通 + 信用两个账户，opType 决定订单归属账户。
- 回测的 `SimBroker` 已实现 `TraderProtocol`，其接口与官方全功能 QMT 客户端内 Python 策略 API 对齐：`passorder`（按官方 opType 码：普通 23/24；信用 27/28/29/31/32/33/34）、`cancel`、`get_trade_detail_data`（ACCOUNT/POSITION/ORDER/DEAL）与信用查询（`get_debt_contract`/`get_assure_contract`/`get_enable_short_contract`）。字段级映射表见 `environment_design.md` §3.2。实盘只需一个满足同一 protocol 的 `QMTBroker` 适配器，即可 drop-in 替换回测 broker，策略代码无需改动：`passorder` 以唯一 `user_order_id`（投资备注 `m_strRemark`）提交并立即按官方 `get_last_order_id` 语义返回委托号；`ctx.broker.pending()` 对应当日可撤委托查询，返回的 `order_id` 可传给 `ctx.broker.cancel(order_id)`（适配器按备注解析委托号）。
- `QMTBroker` 的实现形态是**文件桥**（xtquant 不采用）：`main(ctx)` 与决策环路运行在我们自己的 Python 环境（策略依赖现代 pandas/torch，不能跑在客户端内置 Python 3.6.8 里），`QMTBroker.passorder/cancel` 把委托/撤单写成 inbox 订单文件，`get_trade_detail_data` 读客户端内执行器回写的 ack/fill/state 快照；QMT 客户端内的常驻策略脚本（§2.2）用**内置 API 同名函数**逐条落地。协议两端函数名与语义一一对应，桥只做文件搬运。
- 盘前集合竞价（09:15 info tick / 09:25 撮合开盘）与 14:57 收盘集合竞价从回测原样沿用，实盘 tick 网格在这些节点上的决策与下单语义与回测一致。
- 普通非交易 off-session tick 不提交委托；它只更新本地研究状态、策略 state 或待报计划。若需要盘前下单，应先在 off-session 生成计划，再在 09:15/09:25 这类交易所接收委托的节点提交。

本节描述目标契约，本轮不落地 live 代码（见 §1）。

### 2.2 客户端内文件单执行架构

**已定决策**：实盘执行完全走全功能 QMT 客户端内置 Python 策略 API（`ContextInfo`/`passorder`/`get_trade_detail_data`/`cancel`/回调），不采用 xtquant/miniQMT 外接。零售 QMT 没有开箱即用的"文件单/文件扫单"模块（那是恒生/机构柜台与 Ptrade 的概念；国金 QMT 走 UFT/LDP 柜台），因此文件扫单由我们自己的客户端内策略脚本实现；经官方接口文档核验（`run_time` 定时器、本地文件读、`quickTrade=2` 即刻下单、实盘回调），该形态可行且是该运行时的正确形态。

**分工**

| 侧 | 运行环境 | 职责 |
|---|---|---|
| 决策侧 | 本地 Linux，自有 Python（现代 pandas/torch，不能跑在客户端内置 Python 3.6.8） | 跑与回测同一 `main(ctx)` 环路与冻结策略；经文件桥 `QMTBroker`（§2.1）生成订单 payload（§7 schema，订单带 `op_type`），由 OS 通道（scp/SMB）落入远端 `C:\xquant\inbox\` |
| 执行侧 | QMT 客户端内常驻策略脚本，仅标准库 | 轮询 inbox、校验并用内置 API 落地订单、回写 ack/fill/state；全程零网络、只碰本地文件（规避内置 Python 单线程阻塞与三方库白名单风险，也是否决"客户端内 HTTP"方案的原因） |

**客户端内执行器要点**

- 部署形态：策略加入模型交易、实盘模式，勾选"终端启动后自动执行"+账户自动登录。`init()` 里 `set_account(<账户>)` 并注册 `run_time("poll_inbox", "3nSecond", <过去时刻>, "SH")`；`handlebar` 置空。禁用阻塞循环、`watchdog` 和线程——官方运行时所有策略共享一个线程，任何阻塞会冻结全部策略；轮询定时器是唯一正确形态。
- `poll_inbox`：校验 payload（账户、schema、`payload_id` 去重、预算/涨跌停/停牌/T+1/手数、当日 margin_secs 重校验 §4.2）→ 逐单构造 `client_order_id = <payload_id>#<code>#<side>#<seq>` 作为 `userOrderId`（→`m_strRemark`，幂等键；提交前先扫 ORDER/DEAL 里同备注则跳过；同代码有待报单则暂缓防超单）→ `passorder(opType, 1101, acc, code, prType, price, volume, strategyName, quickTrade=2, client_order_id, ContextInfo)`（`quickTrade=2` 使定时器回调内即刻下单；`acc` 由订单 `op_type` 选择——23/24 用 `CQ_STOCK_ACCOUNT_ID`，27–34 用 `CQ_CREDIT_ACCOUNT_ID`）→ payload 归档。撤单指令文件同路径处理，映射到 `cancel(委托号, acc, accountType, ContextInfo)`（按备注解析委托号）。账户间现金划转（回测的 `transfer`）不在策略 API 内，payload 中的 transfer 指令只生成人工银证转账工单，不自动执行。
- 回写与对账：`order_callback` 写 `*.ack.json`（状态、委托号、拒因）；`deal_callback` 追加 `*.fills.jsonl` 并按 `(委托号, 备注, 成交序号)` 去重（断线重连会整日重推）；慢定时器周期性把 `get_trade_detail_data` ACCOUNT/ORDER/DEAL/POSITION 快照进 `state/` 作为权威对账源（本地 `QMTBroker.get_trade_detail_data` 即读这些快照）。交易接口为异步、查询读本地缓存（无推送柜台 1–6s 刷新）——提交后等一个缓存周期再对账，永不把提交当成交（§8.2）。
- 崩溃恢复：`init()` 先读 `state/` 重建，再用当日 ORDER/DEAL/POSITION 权威覆盖；客户端自启+自动登录+自动执行重挂定时器。时钟：Windows 本地时钟必须 Asia/Shanghai + NTP，回调内用 `get_tick_timetag()` 门控下单窗口。dry-run：运行(模拟)模式天然不发真实委托 + payload `execute:false` 标志双闸。
- 已否决备选：xtquant/miniQMT 外接（用户决策弃用）；原生文件单模块（零售版不存在）；客户端内 HTTP（单线程运行时+白名单摩擦，换取我们分钟级节奏用不到的时延）。

**需在真实国金客户端上验证的开放问题**

1. 客户端内策略对任意本地路径的写权限（读权限已有官方接口 `load_stk_list` 佐证）。
2. `run_time`/回调在实盘模式的稳定性与断线重连重推行为。
3. 隔夜重启自动恢复链路（自启 + 自动登录 + 自动执行）。
4. 各 op 在国金柜台的 opType/prType 实测映射（尤其信用 27–34）。
5. 内置 Python 3.6.8 与三方库白名单约束（执行器目标：仅标准库）。
6. GBK 源文件下 UTF-8 JSON 数据文件读写。
7. 程序化交易报备与申报速率阈值（分钟级节奏预计合规）。

## 3. 当前日常流程

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

```bash
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py status"
ssh Administrator@<server_ip> "C:\\xquant\\Python38\\python.exe C:\\xquant\\qmt_executor.py reconcile"
```

`reconcile` 不下单，只用 QMT 当日成交同步远端策略 state 和 pending 委托。这两条是 miniQMT 时期部署的遗留只读工具，仅用于 standby 期健康检查；客户端内执行器（§2.2）上线后，status/reconcile 改为直接读取其回写的 `state/` 快照文件。

## 4. 未来实盘流程

### 4.1 上线后日常顺序

1. 收盘后或指定决策时点构造 PIT snapshot。
   - 默认日频策略只允许使用当时已可见数据。
   - 09:25 盘前决策必须有独立的数据合同，不能使用当日 `daily` / `daily_basic`。
2. 运行冻结模型或冻结规则。
   - development 阶段结果不得直接下单。
   - held-out 通过后，必须记录模型版本、配置 hash、数据合同 hash、ledger hash。
3. 生成 AutoTrade 订单 payload。
   - 建议路径：`artifacts/live/orders/macroquant_<strategy_id>_<YYYYMMDD>_<action>.json`。
   - payload 必须包含策略 ID、交易日期、模型版本、输入数据 hash、订单列表和风险标签。
4. 上传远端并先 dry-run。
   - 检查预算、股数、涨跌停/停牌、可用持仓、重复 payload、账户 ID。
5. 人工确认后才允许真实执行。
   - 实盘命令必须带双确认参数。
   - 下单后必须运行对账，不能把委托号当成成交。

### 4.2 实盘下单前重校验

- 当日 `margin_secs`（融资/融券）资格，即该标的当日是否可融券做空 / 可融资买入。
- 信用账户约束：保证金可用余额、授信额度、融券卖出限价（申报价不低于最新成交价）。
- 全部交易约束：可用现金、T+1 可卖余额、涨跌停价限、停牌、最小交易单位（手）。

回测中 Broker 的同日动态资格校验，就是这一实盘下单前重校验的仿真等价物：`SimBroker` 用成交日真实 `margin_secs` 集合（`shortable_by_date[fill_date]`，而非 Agent 冻结的决策日快照）对融券开仓与融资买入 fill 设闸。实盘不得用决策时点的旧资格集合代替成交时刻的当日校验。

### 4.3 实盘状态持久化

回测中 `ctx.state_dir` 是每次 run 的临时 scratch（每次回测重置、不跨 run 保留），不能作为实盘的权威状态。实盘部署必须把在途委托、下单计划和持仓跟踪持久化在两处可恢复来源上：

- QMT 自身查询：`get_trade_detail_data` 的 ORDER / POSITION / DEAL 记录，作为成交与持仓的权威对账来源（客户端内执行器周期性快照进 `state/`）。
- executor 的持久状态：复用本文档已有的 `pending_orders`、`strategy_positions` 和 `inbox`/`archive` 处理（见 §3.1、§6.1），用于跨进程重启恢复策略 state 与待处理委托。

任何实盘状态判断都不得依赖回测 `ctx.state_dir`。

## 5. 上线门槛

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

**QMT 常见路径**

```text
C:\国金证券QMT交易端
C:\国金证券QMT交易端\userdata_mini
```

**官方参考**

- 客户端内 Python 策略 API 文档（本仓库副本）：`external_references/gjzq-da-qmt/qmt_python_api_doc.html`
- 迅投内置 Python 文档：http://docs.thinktrader.net/QMT/
- 迅投常见问题（单线程运行时、异步交易接口、查询缓存刷新）：https://dict.thinktrader.net/innerApi/question_answer.html

### 6.3 远端 Python 与环境变量

**远端 Python**

订单执行运行在 QMT 客户端内置 Python（3.6.8，标准库-only 执行器，不安装三方库；三方库受券商白名单约束）。独立 Python 3.8 仅用于文件搬运/运维辅助脚本（scp 落盘、归档清理），不参与下单：

```powershell
C:\xquant\Python38\python.exe -m pip install --upgrade pip
```

历史 xtquant 安装是 miniQMT 方案的遗留，不再是依赖；新机器无需安装。

**环境变量**

```powershell
setx CQ_QMT_DATA_PATH "C:\国金证券QMT交易端\userdata_mini"
setx CQ_XQUANT_ROOT "C:\xquant"
setx CQ_STOCK_ACCOUNT_ID "<普通账户 id>"
setx CQ_CREDIT_ACCOUNT_ID "<信用账户 id>"
```

执行器按订单 `op_type` 在两个账户 id 间路由（23/24 → 普通，27–34 → 信用）；两者都必须显式配置，缺失即拒绝启动。

### 6.4 本金口径

```powershell
setx CQ_MAX_PRINCIPAL "100000"
```

不设置 `CQ_MAX_PRINCIPAL` 时，执行器默认使用 `get_trade_detail_data` ACCOUNT 返回的总资产（`m_dBalance`）作为本金口径。是否采用这个口径必须与回测资金口径一致。

## 7. Payload 草案

当前 payload schema 尚未冻结；以下只作为 AutoTrade 后续实现参考。真实接入前必须与客户端内执行器的实际实现核对。

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
      "op_type": 33,
      "volume": 100,
      "price_type": "LATEST",
      "limit_price": null,
      "reason": "budgeted_buy",
      "risk_tags": []
    }
  ]
}
```

`op_type` 取官方 passorder 操作码（普通 23/24；信用 27/28/29/31/32/33/34，与 `environment_design.md` §3.2 一致）；`side` 保留为人读冗余，执行器以 `op_type` 为准并校验两者一致。

### 7.2 执行语义

- 无 `principal` 时，远端读取账户 `total_asset`。
- 每日买入预算可设为 `min(account_cash, total_asset * daily_buy_limit_ratio)`。
- 买入必须跳过 broker 已有持仓或策略 state 已有持仓的股票，除非 action 明确允许加仓。
- 卖出只根据远端策略 state 和 broker 可用持仓生成，不卖出非本策略仓位。
- `rebalance` 最终应在本机拆成明确的 BUY/SELL 订单，远端不负责理解研究语义。

## 8. Dry-run 与实盘执行

### 8.1 上传和执行命令

**上传 payload**（执行由客户端内轮询策略接管，本机只负责落盘）

```bash
scp order.json Administrator@<server_ip>:C:/xquant/inbox/order.json
```

**Dry-run 双闸**：①QMT 策略跑在 运行(模拟) 模式（交易函数不发真实委托）；②payload 携带 `"execute": false`——执行器只校验并写 ack，不调用 `passorder`。两闸独立，都通过后才考虑真实执行。

**真实下单双确认**：QMT 策略切换 实盘 模式（加入模型交易），且 payload 显式 `"execute": true` + `"confirm": "LIVE"` 字段；两者缺一执行器即拒绝。人工确认动作 = 生成并上传这样的 payload，本身必须由人完成。

### 8.2 成交对账

不要把 `passorder` 的提交（或委托号）当成交。委托号只标识委托；成交以 `deal_callback` 回写与 `get_trade_detail_data` DEAL 快照为准（查询读本地缓存，提交后等一个刷新周期再对账），用 `reconcile`（读 `state/` 快照）回填策略 state。

## 9. 故障处理

### 9.1 常见故障

- 执行器未运行：检查 QMT 已登录、策略在模型交易列表且"终端启动后自动执行"勾选、`run_time` 定时器在实盘模式下已挂起。
- 多账户：普通/信用账户 id 分别用 `CQ_STOCK_ACCOUNT_ID` / `CQ_CREDIT_ACCOUNT_ID` 显式配置，禁止自动选择。
- 重复 payload：默认拒绝；确需重跑时，先核对 state、委托、成交，再用显式 repeat 开关。
- pending 未清：运行 `reconcile`，确认当日成交和委托状态。
- inbox 有旧文件：移动到 `C:\xquant\archive\...`，不要直接手工执行。
- 本机模型或数据状态不确定：停止生成 live payload，只保留研究 ledger 和 dry-run。
