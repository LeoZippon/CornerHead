# Agent Design

整理日期：2026-05-28

本文档记录 MacroQuant 的决策者层：公式化 Agent、LLM shadow、evidence pack、prompt/response 合同、provider adapter 和未来可交易 Agent 的升级边界。Environment 市场环境见 `docs/environment_design.md`；Pipeline 运行编排见 `docs/pipeline_design.md`；数据下载和 raw PIT 规则见 `docs/data_documentation.md`。

## 边界原则

Agent 回答的是：“给定 Environment 产出的 PIT observation 或 evidence，我建议什么 action 或评分。”

Agent 可以：

- 消费 PIT feature、checkpoint、evidence pack 和 ledger 基础工具。
- 输出公式化候选股票。
- 输出 shadow-only 自然语言判断。
- 调用 provider API 生成 JSON shadow decision。

Agent 不可以：

- 不直接读取 `data/raw`。
- 不自己判断 raw 数据可见性。
- 不修改 BrokerSimulator 或 PortfolioState。
- 不直接写真实订单。
- 在当前阶段不改变组合、权重、成交或 PnL。

当前安全边界：

- `nl_weight=0.0`。
- `action_impact=shadow_only`。
- `can_affect_trading=False`。
- LLM 输出只写 JSONL ledger，不进入订单生成。

## 代码组织

Agent 层代码集中在 `src/hl_trader/agent/`。

| 模块 | 职责 |
|---|---|
| `formulaic.py` | 公式化参数、参数网格、横截面打分、候选股选择 |
| `evidence/pack.py` | PIT evidence pack 构造、hash、校验 |
| `shadow/nl_shadow.py` | NL shadow decision 记录、动作约束、secret 脱敏、recorder |
| `shadow/llm_shadow.py` | prompt 构造、JSON chat client 调用、response 校验 |
| `shadow/prompts.py` | LLM shadow system prompt |
| `llm/deepseek.py` | DeepSeek provider adapter，兼容 OpenAI JSON mode |

Environment 中的 `events/checkpoints.py` 可以给 Agent 提供上下文，但事件检测本身属于市场环境，不属于 Agent。

## 公式化 Agent

实现：

```text
src/hl_trader/agent/formulaic.py
```

核心对象：

- `FormulaicParameters`
- `FormulaicScoreRule`

参数：

- `top_n`：持仓股票数。
- `max_pe_ttm_quantile`：PE 分位过滤上限。
- `max_pb_quantile`：PB 分位过滤上限。
- `min_amount_quantile`：成交额流动性分位过滤下限。

`parameter_grid(space)` 从 template parameter space 生成冻结参数组合；实际 train/test 选择由 Pipeline 执行。

`select_formulaic_candidates(cross_section, params)` 逻辑：

1. 排除停牌。
2. 要求 `pe_ttm`、`pb`、`amount_ma20`、`ret_20d` 存在。
3. 过滤正 PE、正 PB、非空成交额。
4. 保留低 PE、低 PB、高成交额分位。
5. 横截面打分：
   - PE 升序，权重 1.0。
   - PB 升序，权重 0.8。
   - 20 日收益降序，权重 0.4。
   - 20 日成交额均值降序，权重 0.2。
6. 输出前 `top_n` 个 `ts_code`。

公式化 Agent 只输出候选列表；下单、换手预算、成本、涨跌停和 T+1 由 Environment + Pipeline 执行。

## Evidence Pack

实现：

```text
src/hl_trader/agent/evidence/pack.py
```

Evidence Pack 是 Agent 输入边界，不允许 LLM 直接读取 raw 数据或任意 feature 文件。

Pack 关键字段：

- `schema_version`
- `pack_id`
- `decision_date`
- `tradable_date`
- `ts_codes`
- `items`
- `pack_hash`
- `created_at`

Item 关键字段：

- `name`
- `source`
- `as_of`
- `payload`
- `payload_hash`

PIT 校验：

- 横截面内 `ts_code` 必须唯一。
- 所选股票必须都存在于同一 `feature_date/tradable_date` 横截面。
- 拒绝未来 `feature_date`、`source_trade_date`、`available_at`。
- `tradable_date` 必须等于本次 shadow review 的目标交易日。
- payload 保留 PIT 元数据、单位字典和特征快照。

Hash 规则：

- `payload_hash` 覆盖 item 内容。
- `pack_hash` 覆盖决策日期、可交易日期、股票列表和 payload hash。
- `created_at` 不参与 `pack_hash`，保证同一内容重复构造时可对账。
- JSONL 读取时复核 `payload_hash` 和 `pack_hash`。

## LLM Shadow Advisor

实现：

```text
src/hl_trader/agent/shadow/llm_shadow.py
```

`LLMShadowAdvisor` 输入：

- 已验证的 evidence pack record。
- 可选 event checkpoints。
- JSON chat client。

Prompt payload：

- `task`
- `allowed_actions`
- `cannot_affect_trading=True`
- `evidence_pack`
- `event_checkpoints`

默认 allowed actions：

- `hold`
- `enter`
- `exit`
- `trim`
- `add`
- `rebalance`
- `human_review`

这些 action 当前只是自然语言标签。即使模型返回 `enter` 或 `exit`，也只写 shadow ledger，不会转为订单。

为控制上下文长度，advisor 会压缩 evidence pack：

- 保留 pack 元数据、股票列表、item 元数据、payload hash。
- 每个 item 只保留前 `max_evidence_rows` 行。
- 超出部分记录 `truncated_rows`。

## Response 合同

模型必须返回 JSON object，且包含 `decisions` list。

每条 decision 必须满足：

- `ts_code` 属于 evidence pack。
- 每个 pack `ts_code` 恰好一条 decision。
- 不能缺失、重复或额外输出股票。
- `action` 必须在 allowed actions 中；不支持的 action 降级为 `human_review`。
- `confidence` 必须在 `[0, 1]`。
- `rationale` 可为空；为空时写入默认说明。
- `risk_flags` 若存在，会追加到 rationale 中。

写入前生成：

- `prompt_hash`
- `response_hash`
- `decision_id`
- `decision_hash`

## NL Shadow Recorder

实现：

```text
src/hl_trader/agent/shadow/nl_shadow.py
```

`NLShadowDecision` 强制：

- `prompt_hash` 和 `response_hash` 非空。
- `action` 在 shadow action 集合中。
- `nl_weight == 0.0`。
- `action_impact == shadow_only`。
- `can_affect_trading == False`。
- `confidence` 在 `[0, 1]`。

`NLShadowRecorder` 写入：

- `event_type=nl_shadow_decision`
- decision record
- `decision_hash`
- evidence pack id
- provider metadata
- `can_affect_trading=False`

Provider metadata 会脱敏：

- key 名包含 `api_key`、`token`、`secret`、`password` 等时替换为 `[REDACTED]`。
- 字符串中的 `sk-...` 形态密钥替换为 `sk-***`。

## Provider Conversation Log

所有真实 LLM provider API 调用都必须记录完整对话，用于后续审计、复盘和可能的蒸馏数据构造。日志属于本地敏感运行产物，默认写入 ignored 路径：

```text
data/llm_conversations/<provider>/<model>/<YYYYMMDD>.jsonl
```

每次 HTTP attempt 写一条 JSONL，而不是只记录最终结果。当前记录字段包括：

- provider、model、request/response 时间、耗时、attempt、HTTP status。
- 原始 request payload，包含 system/user/assistant messages 和 JSON mode 参数。
- 原始 provider response、response id、usage、request/response hash。
- 失败调用的错误类型、错误消息、retryable 标记和响应 body。
- provider metadata。

日志不得包含 API key 或 Authorization header。写入前会递归脱敏 `sk-...` 形态密钥；如果日志目录无法创建或写入，调用应 fail fast，避免产生“花费了 token 但没有留痕”的 shadow 决策。后续接入其他模型时，新 adapter 也必须遵循同一 conversation-log 合同。

## DeepSeek Adapter

实现：

```text
src/hl_trader/agent/llm/deepseek.py
```

配置：

- `api_key`：必需，只能来自环境或 ignored `.env`。
- `base_url`：必须是 HTTPS。
- `model`：支持 `deepseek-v4-flash`、`deepseek-v4-pro`、`deepseek-chat`、`deepseek-reasoner`。
- `max_tokens`、`temperature`、`timeout_seconds`、`max_retries`。
- `reasoning_effort`：只允许 adapter 支持的枚举值。

请求规则：

- 使用 OpenAI-compatible `/chat/completions`。
- 强制 `response_format={"type": "json_object"}`。
- message 中必须显式提到 JSON。
- 默认不 stream。
- 429、500、503 按重试策略处理。
- HTTP、JSON 和 finish_reason 异常会抛出明确错误，不写伪结果。

后续接入其他 provider 时，应新增 provider adapter，但复用 `LLMShadowAdvisor`、`EvidencePackBuilder` 和 ledger 合同，不把 provider 名写入通用文件名。

## 与交易系统的隔离

当前 Agent 和交易系统的唯一交集由 Pipeline 控制：

- 公式化 Agent 输出候选股，Pipeline 才能在冻结 `TradeStrategyPolicy` 下生成调仓订单。
- LLM shadow 读取 checkpoint 作为上下文，但不能产生可交易订单。
- LLM ledger 与实验 ledger 可以共存，但不能被 BrokerSimulator 读取为订单。

要从 shadow 升级为可影响交易的 Agent，至少需要新增：

- Agent policy schema：允许哪些 action、何时允许、最大影响额度、人工确认要求。
- Prompt/version freeze：模型、prompt、tools、evidence schema、temperature、max tokens 全部入 freeze hash。
- 决策仲裁层：公式化信号、事件规则、LLM 建议之间的优先级和冲突处理。
- 回放对照：Control、shadow、LLM-assisted 三组 held-out 对照。
- 可解释审计：每次改变订单的自然语言理由、证据引用、hash 和人工复核结果。
- 实盘禁用开关：任何 provider 异常、hash 异常、PIT 异常时 fail closed。

在这些边界实现前，LLM Agent 仍保持 shadow-only。
