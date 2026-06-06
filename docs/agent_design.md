# Agent Design

整理日期：2026-06-07

本文档记录 Agent 层。Agent 层只回答：如何提出交易逻辑、如何在训练窗口内实例化、如何读证据、如何调用 LLM、哪些输出不能直接影响交易。
数据可见性和回放属于 `docs/environment_design.md`；运行顺序、冻结和账本属于 `docs/pipeline_design.md`；raw 数据来源属于 `docs/data_documentation.md`。

## 导航

- [1. Agent 层职责](#1-agent-层职责)
  - [1.1 负责什么](#11-负责什么)
  - [1.2 禁止什么](#12-禁止什么)
  - [1.3 代码位置](#13-代码位置)
- [2. 外层和内层 Agent](#2-外层和内层-agent)
  - [2.1 角色分工](#21-角色分工)
  - [2.2 模板和实例](#22-模板和实例)
  - [2.3 输出合同](#23-输出合同)
  - [2.4 冻结后禁止](#24-冻结后禁止)
- [3. Evidence 与 Case](#3-evidence-与-case)
  - [3.1 Evidence Pack](#31-evidence-pack)
  - [3.2 Case Library](#32-case-library)
  - [3.3 LLM 输出校验](#33-llm-输出校验)
- [4. LLM 调用与日志](#4-llm-调用与日志)
  - [4.1 调用规则](#41-调用规则)
  - [4.2 对话日志](#42-对话日志)
  - [4.3 多 Provider](#43-多-provider)
- [5. 交易影响边界](#5-交易影响边界)
  - [5.1 默认隔离](#51-默认隔离)
  - [5.2 可交易前提](#52-可交易前提)
- [6. 验收清单](#6-验收清单)

## 1. Agent 层职责

### 1.1 负责什么

Agent 层负责：

- 外层 Agent 根据文档、历史实验、复盘经验和失败模式提出或修改 Template。
- 内层 Agent 在训练快照内，把冻结 Template 实例化为具体参数、权重、阈值和策略参数。
- LLM Agent 根据 Evidence Pack 输出 memo、风险标签或动作建议。
- 公式化控制组输出可复现的候选股票和参数组合。
- 所有真实 provider 调用都留下可审计对话记录。

### 1.2 禁止什么

Agent 层禁止：

- 直接读取 `data/raw`。
- 自行判断 raw 数据是否已经可见。
- 修改回放、撮合、成本、交易约束或持仓状态。
- 绕过 Data Gateway 或 Tool Gateway。
- 访问互联网搜索。
- 直接写真实订单。
- 在 test 或 held-out 阶段调参、改模板、改 prompt、改交易策略。

### 1.3 代码位置

Agent 层代码在 `src/hl_trader/agent/`。

| 模块 | 职责 |
|---|---|
| `formulaic.py` | 公式化参数、打分和候选股选择 |
| `evidence/pack.py` | Evidence Pack 构造、hash 和校验 |
| `shadow/nl_shadow.py` | shadow decision 记录和脱敏 |
| `shadow/llm_shadow.py` | prompt 构造、LLM 调用和 response 校验 |
| `shadow/prompts.py` | 通用 system prompt |
| `llm/deepseek.py` | DeepSeek adapter 示例 |
| `templates/` | Template schema 和复杂度约束 |
| `inner/` | 内层 Agent 的候选 Instance 输出 |
| `outer/` | 外层 Agent 的 Template 生成和 mutation |

## 2. 外层和内层 Agent

### 2.1 角色分工

| 角色 | 输入 | 输出 | 禁止 |
|---|---|---|---|
| 外层 Agent | living docs 摘要、development ledger、Case Library、失败模式、数据风险 | Template、模板修改建议、实验队列 | 读取 held-out 结果后改模板；直接跑 test；直接下单 |
| 内层 Agent | train snapshot、冻结 Template、允许工具、历史 case 子集 | Candidate Instance、参数、因子权重/阈值、自然语言规则参数、动作策略参数、训练评分 | 修改外层 Template；读取 test snapshot；联网搜索；修改回放函数 |
| Test LLM Agent | test snapshot 中的 evidence、冻结 Instance、冻结 prompt/model/settings | trader memo、action proposal、risk tag | 改 Instance；调参；重跑选择最好结果 |
| Post-review Agent | 已完成 trial、fills、metrics、case outcome | 复盘、经验、mutation candidate | 改写已完成结果；污染 held-out |

主线：

```text
外层 Agent -> Template
Pipeline 冻结 Template
内层 Agent -> Candidate Instance
Pipeline 选择并冻结 Instance
Test sandbox 执行冻结 Instance
Trial Ledger + Case Library -> 外层 Agent 学习
```

### 2.2 模板和实例

Template 是可迁移的交易逻辑；Instance 是某个训练窗口内调出来的具体实现。

| 对象 | 学习层级 | 典型内容 | 冻结时点 |
|---|---|---|---|
| Factor Template | 外层 | 因子定义、输入域/列、窗口、方向、参数空间、目标函数 | 进入训练前 |
| Natural Language Template | 外层 | 证据类型、可信度规则、催化/风险判断、memo schema | 进入训练前 |
| Trade Decision Template | 外层 | 因子分、文本判断、持仓状态的合成规则 | 进入训练前 |
| Trade Strategy Template | 外层 | `hold/add/trim/exit/rebalance/event_de_risk/inventory_trade` 的允许条件 | 进入训练前 |
| Heuristic Instance | 内层 | 参数取值、因子权重/阈值、自然语言规则参数、动作策略参数、训练评分 | 进入测试前 |

外层 Agent 可以提出窗口、变量族、股票池偏好和证据检索意图；Pipeline 校验并冻结；Environment 执行。Agent 文档只定义这些产物的语义，具体交接流程见 `docs/pipeline_design.md`。

### 2.3 输出合同

外层 Agent 输出必须是结构化 Template，不是散文式策略建议。

外层输出至少包括：

- `template_id`
- 四类 Template。
- 目标 horizon。
- 候选数据域和历史窗口。
- 股票池偏好。
- 参数空间。
- 复杂度说明。
- 生成理由。

内层输出至少包括：

- `template_id`
- 参数取值。
- 因子权重/阈值。
- 自然语言规则参数。
- 动作策略参数。
- 训练评分。
- 搜索轨迹和 artifact hash。

测试期 LLM 输出只能是 memo、risk tag 或 action proposal。是否转成订单由 Pipeline 和 Environment 按冻结策略判断。

### 2.4 冻结后禁止

进入 test 或 held-out 后，下列对象禁止修改：

| 对象 | 禁止修改内容 |
|---|---|
| Template | 因子定义、窗口、变量族、文本规则、交易策略 |
| Instance | 参数、权重、阈值、动作策略参数 |
| Prompt/model/settings | prompt、模型、温度、max tokens、工具权限 |
| 数据边界 | snapshot、数据域、历史窗口、股票池规则 |
| 回放规则 | 成本、撮合、换手预算、T+1、涨跌停、事件动作 |

内层 Agent 不能新增外层 Template 没定义的因子、窗口、自然语言规则或交易策略。

## 3. Evidence 与 Case

### 3.1 Evidence Pack

Evidence Pack 是本次决策可见材料的封装。LLM 只能读取 Evidence Pack，不能直接读取 raw 数据或任意文件。

关键字段：

- `pack_id`
- `decision_date`
- `tradable_date`
- `ts_codes`
- `items`
- `pack_hash`

每个 item 至少包含：

- `name`
- `source`
- `as_of`
- `payload`
- `payload_hash`

规则：

- 候选股票必须属于同一个决策时间和交易日。
- 拒绝未来 `feature_date`、`source_trade_date`、`available_at`。
- 每条文本证据必须有 `evidence_id`、source、时间和 hash。
- `pack_hash` 覆盖决策日期、股票列表和 item hash。

### 3.2 Case Library

Evidence 是“当时能看到的材料”；Case 是“事后复盘得到的经验”。二者不能混用。

Case Library 用于外层 Agent 学习模板和失败模式，必须满足：

```text
case_available_at <= outer_agent_decision_time
```

Case 不能作为某个历史决策日的“当时新闻”喂给内层 Agent 或 test LLM。

### 3.3 LLM 输出校验

LLM 输出必须是 JSON object，并包含 `decisions` list。

每条 decision 必须满足：

- `ts_code` 属于 Evidence Pack。
- 每个候选股票恰好一条 decision。
- `action` 属于允许动作；不支持的动作降级为 `human_review`。
- `confidence` 在 `[0, 1]`。
- 必须引用 evidence id，或明确说明没有可用证据。
- 不得包含 API key、Authorization header 或未脱敏凭据。

标准 action：

- `hold`
- `enter`
- `exit`
- `trim`
- `add`
- `rebalance`
- `margin_short_sell`
- `human_review`

`margin_short_sell` 只是做空侧观点，不自动生成融券订单。真实执行需要券商券源、担保品、费率、强平线和人工/风控确认。

## 4. LLM 调用与日志

### 4.1 调用规则

Sandbox 内可以实例化 API 驱动的 LLM Agent，但只能通过受控代理调用 provider。

允许：

- 调 provider API。
- 在 snapshot 内做本地关键词检索。
- 在 sandbox 内运行 Python 分析。
- 调用白名单确定性工具。

禁止：

- 任意互联网搜索。
- 直接访问 TuShare API。
- 读取主机 API key。
- 真实下单。
- 在测试期改变冻结参数。

### 4.2 对话日志

所有真实 provider 调用必须写入本地 JSONL conversation log。日志必须包含：

- provider
- model
- messages / prompt
- raw response
- usage
- request hash
- response hash
- error 或 retry 信息

日志不得包含：

- API key
- Authorization header
- `sk-...` 形态密钥
- 其他凭据

如果日志目录无法创建或写入，真实 provider 调用必须失败，避免“花费 token 但没有留痕”。

### 4.3 多 Provider

DeepSeek 是当前 adapter 示例，但通用 Agent 代码不应绑定 provider 名。

新增 provider 时，应复用：

- Evidence Pack。
- LLM shadow advisor。
- Response schema。
- Conversation log 合同。
- 脱敏规则。

新增 provider adapter 只处理鉴权、请求格式、错误解析和 JSON mode 差异。

## 5. 交易影响边界

### 5.1 默认隔离

当前默认安全边界：

```text
nl_weight = 0.0
action_impact = shadow_only
can_affect_trading = False
```

也就是说，LLM 输出默认只写日志和审计，不直接影响交易。

### 5.2 可交易前提

LLM 或 Agent 输出要影响交易，必须同时满足：

- 交易策略已冻结。
- 允许动作在 `TradeStrategyPolicy.allowed_actions` 内。
- 数据来自已冻结 snapshot。
- Evidence Pack 通过校验。
- Pipeline 明确允许 action impact。
- Environment 通过撮合、成本、仓位、涨跌停、停牌、T+1 和风控约束。
- 结果写入 ledger。

即使允许交易影响，Agent 也只输出 proposal，不直接写订单。

## 6. 验收清单

Agent 相关改动至少检查：

- 外层/内层/test/post-review 职责没有混淆。
- Template 和 Instance 的修改时点清楚。
- Test/held-out 阶段不能调参、改模板或选择最好结果。
- LLM 输入只能来自 Evidence Pack。
- Case Library 和 Evidence Pack 没有混用。
- 真实 provider 调用有完整 conversation log。
- 日志脱敏覆盖 API key 和 Authorization header。
- Agent 不 import Environment 的 broker state，也不实现撮合或 raw selector。
