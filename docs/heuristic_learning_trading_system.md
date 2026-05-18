# Heuristic Learning 安全边际交易系统实践蓝图

> 目标：构建一个可复现、可审计、可进化的交易研究系统。系统通过 Heuristic Learning 同时学习**公式化规则**与**自然语言分析逻辑**，并通过严格时间切分、Walk-Forward Optimization、事件驱动复核和 held-out 检验评估其真实可用性。

本文保留实践落地所需结构。具体阈值、模型、prompt、仓位、交易成本、数据源评分和参数空间在实验中确定。

---

## 1. 核心定位

系统学习的对象不是单个固定因子，而是一套可迁移的 **Heuristic System**：

```text
公式化 Heuristics：因子、阈值、过滤器、组合约束、调仓规则。
自然语言 Heuristics：估值逻辑、价值陷阱诊断、事件解释、舆情可信度、宏观冲击判断、催化验证、退出逻辑。
```

基本边界：

```text
2010–2024：development WFO，用于学习和筛选 Heuristics。
2025+：冻结版本 held-out / quasi-forward，用于检验公式化策略与自然语言决策增益。
```

在 2010–2024 的测试窗口中，自然语言逻辑可以运行并记录，但不改变订单、权重和 PnL。这样可以验证自然语言诊断能力，同时避免模型预训练知识抬高历史回测收益。

---

## 2. 核心对象

### 2.1 Horizon Track

持仓周期不是一个简单参数，而是一个独立研究轨道。短周期和长周期可能需要不同的 Heuristic Template、变量族、自然语言逻辑和评价指标。

```text
Horizon Track = HorizonSpec + Template Bank + Protocol + Fitness
```

示例：

```text
3m track：更偏财报反应、事件错杀、舆情变化、短期风险释放。
6m track：更偏估值修复、基本面确认、政策/行业催化。
12m track：更偏安全边际、现金流、分红回购、资产价值、周期修复。
```

不同 track 可以共享工程框架和部分可复用 skill，但不应默认使用同一个 Heuristic Template 比较 3m / 6m / 12m。跨周期实验的目的，是判断 HL 更适合短周期还是中长周期，并观察不同周期下 Heuristics 的结构性差异。

### 2.2 Heuristic Template

外层 Agent 学习的抽象投资逻辑。Template 应声明目标持仓周期或适用的 Horizon Track。

Template 包含：

```text
目标 horizon
变量族
参数搜索空间
公式化规则结构
自然语言分析逻辑
事件/舆情/宏观解释逻辑
正则项
禁止项
评价指标
```

### 2.3 Heuristic Instance

内层 Agent 在某个训练窗口内，根据 Template 拟合出的具体规则。

例子：

```text
PE 阈值 = 10
PB 分位数上限 = 20%
持仓数量 = 50
舆情可信度阈值 = 某训练窗口内拟合结果
事件触发后的降权规则 = 某训练窗口内拟合结果
```

Instance 进入测试窗口后冻结执行。

### 2.4 Protocol

Protocol 控制验证和交易方式：

```text
训练窗口长度
测试窗口长度 / 持仓周期
滚动步长
决策锚点
普通交易日事件触发规则
复核规则
成本模型
自然语言权重
held-out 锁定规则
```

Template 负责“学什么投资逻辑”，Protocol 负责“怎么训练、怎么持有、怎么执行、怎么评估”。

---

## 3. 持仓周期实验设计

持仓周期应作为 Horizon Track 的核心配置，而不是同一模板的附属参数。

示例配置：

```yaml
horizon_3m_track:
  target_holding_months: 3
  train_length_months: 24-36
  test_length_months: 3
  step_months: 1-3
  template_bank: "templates/horizon_3m/"

horizon_6m_track:
  target_holding_months: 6
  train_length_months: 36-48
  test_length_months: 6
  step_months: 3
  template_bank: "templates/horizon_6m/"

horizon_12m_track:
  target_holding_months: 12
  train_length_months: 36-60
  test_length_months: 12
  step_months: 3-6
  template_bank: "templates/horizon_12m/"
```

需要区分：

```text
decision_anchor：fold 从哪天启动，例如 4/30、8/31、10/31、季度末、月末、事件日。
step_months：fold 多久滚动一次。
test_length_months：测试窗口长度，也就是该 track 的主要持仓周期。
review_dates：持仓期内的复核日期。
event_checkpoint：普通交易日由新增信息触发的临时复核。
```

同一 Horizon Track 内，fold 的测试窗口冻结执行；不同 Horizon Track 之间，允许模板、变量、逻辑和评价指标发生系统性偏移。

---

## 4. 数据时间切分与事件驱动决策

所有数据必须有严格时间边界：

```text
available_at：系统可看到该信息的时间。
tradable_from：该信息可用于下单的最早时间。
source：来源。
source_quality：来源质量或可信度特征。
data_hash / document_hash：数据或文档版本。
evidence_id：自然语言判断引用的证据编号。
```

任何决策只能使用：

```text
available_at <= decision_time
tradable_from <= order_time
```

适用数据包括行情、财务、公告、新闻、舆情、政策、宏观、国际事件和 RAG evidence pack。

普通交易日也可以触发决策。若某日舆情、公告、政策、国际事件或行业供需信息发生显著变化，系统应生成 `event_checkpoint`：

```text
1. 判断新信息是否在当前时间可用。
2. 更新 evidence pack。
3. 调用当前 Horizon Track 对应的 Heuristic Instance。
4. 运行公式化规则与自然语言逻辑。
5. 在允许的行动空间内输出 hold / exit / trim / add / rebalance / human_review 等动作。
6. 记录触发原因、证据、模型输出和最终订单。
```

在 2010–2024 development WFO 中，自然语言输出只 shadow 记录；在 2025+ held-out 中，可以按冻结协议测试自然语言逻辑是否参与最终决策。

---

## 5. WFO + Heuristic Learning 流程

每个 Horizon Track 独立运行。

```text
1. 外层 Agent 生成该 track 的 Template T_k。
2. Protocol 根据 target horizon 生成 folds。
3. 每个 fold：
   a. 内层 Agent 只使用 train_f 中可用且结果已知的数据。
   b. 根据 T_k 拟合参数 θ_f。
   c. 得到 Instance h_f = T_k(θ_f)。
   d. 在 test_f 中冻结执行 h_f。
   e. 按 Protocol 做定期复核和普通交易日事件复核。
   f. 自然语言逻辑在 development WFO 中只 shadow 记录。
   g. 记录收益、回撤、成本、换手、参数、事件、证据和 NL 输出。
4. 所有 folds 完成后，外层 Agent 基于 trial ledger 改进该 track 的 Template。
5. 多轮进化受 trial budget 限制。
```

训练样本必须满足：

```text
result_available_time <= train_end
```

测试窗口禁止：

```text
修改 Template
修改参数
修改 prompt
修改 Protocol
修改持仓周期
使用 test_f 结果反向调参
```

---

## 6. 自然语言 Heuristics

自然语言分析不是额外装饰，而是 Heuristic Learning 的学习对象之一。

可学习内容包括：

```text
为什么便宜？
为什么不是价值陷阱？
财务数据是否可能误导？
舆情来源是否可信？
舆情是风险、催化还是噪声？
宏观或国际事件会影响哪些行业和公司暴露？
催化链是否真实？
当前 thesis 如何被证伪？
触发事件后应持有、退出、降权还是复核？
```

在 development WFO 中，自然语言逻辑可以被验证：

```text
被标记为 value-trap 的股票后续是否更容易跑输？
被标记为 source_low_confidence 的舆情是否更少转化为真实事件？
被标记为 catalyst_valid 的股票是否更容易估值修复？
NL confidence 是否与真实结果校准？
事件触发后的 shadow action 是否改善左尾风险？
```

2025+ held-out 中，可测试：

```text
Control：公式化 Heuristics only。
Treatment：公式化 Heuristics + 自然语言最终复核。
```

自然语言行动空间、权重和证据要求必须在 held-out 前冻结。

---

## 7. 数据误导、舆情和宏观事件

财务、舆情、宏观和国际事件既可能提供 alpha，也可能误导系统。这里不预设它们只能用于风险或只能用于买入，而是让 Heuristics 学习其可信度、作用方向和适用条件。

系统应至少记录：

```text
source_quality
independent_source_count
official_confirmation
text_sentiment
event_type
company_exposure
industry_exposure
historical_reliability
market_reaction
subsequent_confirmation
```

学习目标不是简单判断“好消息 / 坏消息”，而是判断：

```text
该信息是否可信？
它是否已经被市场定价？
它影响的是短期情绪、基本面、估值、流动性还是尾部风险？
它对不同 horizon 的作用是否不同？
它应该触发买入、加仓、降权、退出，还是只触发 human_review？
```

这部分规则可以由 Agent 学习，但必须服从时间切分、证据引用、trial ledger 和 held-out 锁定约束。

---

## 8. 并行进化实验

可以并行运行多轮进化实验，但并行维度应清晰区分。

推荐的顶层并行维度：

```text
Horizon Track：3m / 6m / 12m / 18m
Strategy Family：asset_value / cashflow_yield / quality_value / event_driven / policy_catalyst / cyclical_value
Random Seed / Prompt Variant / Model Variant
```

注意：`Horizon Track` 是顶层实验轨道，不是同一 island 内的附属对照。3m 和 12m 的 Heuristics 可以不同，因为市场反应机制、数据滞后、舆情作用和催化兑现速度都可能不同。

Fitness 不只看收益：

```text
fold 中位数超额收益
正收益 fold 占比
最差分位表现
最大回撤
换手率
交易成本敏感性
参数稳定性
复杂度
行业/风格集中度
事件触发效果
自然语言诊断有效性
```

并行实验会增加多重测试风险，必须记录所有失败试验，并限制 trial budget。2010–2024 被反复使用后只能视为 development set。

---

## 9. Held-out 检验

进入 2025+ held-out 前冻结：

```text
Horizon Track
Template T*
Protocol P*
模型版本
prompt 版本
数据合同
成本模型
自然语言行动空间
trial ledger 写入规则
```

held-out 评估至少包含：

```text
Control：公式化 WFO only。
Treatment：公式化 WFO + 自然语言最终复核。
```

held-out 一旦用于改模板、改 Protocol、改模型、改 prompt、改 horizon 或调自然语言权重，就不再是 final held-out。

---

## 10. 最小仓库结构

```text
heuristic-alpha-lab/
├── README.md
├── pyproject.toml
├── configs/
│   ├── tracks/
│   │   ├── horizon_3m.yaml
│   │   ├── horizon_6m.yaml
│   │   └── horizon_12m.yaml
│   ├── protocols/
│   ├── templates/
│   │   ├── horizon_3m/
│   │   ├── horizon_6m/
│   │   └── horizon_12m/
│   └── experiments/
├── data/
│   ├── pit/
│   ├── features/
│   └── evidence_packs/
├── src/hl_trader/
│   ├── schemas/
│   ├── data/
│   ├── tracks/
│   ├── protocols/
│   ├── heuristics/
│   ├── agents/
│   ├── wfo/
│   ├── backtest/
│   ├── portfolio/
│   ├── evaluation/
│   └── storage/
├── tests/
│   ├── unit/
│   ├── leakage/
│   └── regression/
├── experiments/
│   └── trial_ledger/
├── reports/
└── docs/
```

核心存储：

```text
PIT 数据：Parquet，按 trade_date / asof_date 分区。
Evidence Pack：JSONL，含 evidence_id、available_at、source_quality、document_hash。
Trial Ledger：JSONL 或 Parquet，记录 track_id、template_id、protocol_id、horizon、fold_id、参数、指标、事件、证据、NL 输出。
```

---

## 11. 关键测试

```text
Leakage Test：禁止读取 available_at 晚于 decision_time 的数据。
Result Availability Test：训练只能使用 result_available_time <= train_end 的样本。
Horizon Track Test：不同 horizon 使用独立 Template Bank 和 Protocol。
Fold Freeze Test：test phase 不得修改模板、参数、prompt、Protocol 和持仓周期。
NL Isolation Test：2010–2024 中 NL_weight=0，NL 输出不得影响订单和 PnL。
Event Checkpoint Test：普通交易日事件只能使用当时可用 evidence，并按冻结协议执行。
Held-out Lock Test：held-out 结果不得写回模板、Protocol、模型选择或 horizon 选择。
Trial Ledger Test：成功与失败实验都必须记录。
```

---

## 12. 实施优先级

```text
1. Data Contract + PIT 读取
2. Horizon Track schema
3. Protocol schema
4. WFO splitter / train optimizer / test executor
5. Leakage checker
6. Heuristic Template schema
7. Event checkpoint runner
8. Evidence pack builder
9. Trial Ledger
10. NL Shadow Auditor
11. Parallel evolution runner
12. 2025+ held-out A/B runner
```

---

## 13. 待实践确定

```text
哪个 Horizon Track 最适合 HL
各 track 的训练窗口长度
各 track 的滚动步长
各 track 的变量族和参数空间
自然语言逻辑是否主要贡献于短周期事件还是中长线排雷
舆情和宏观事件在不同 horizon 下的作用
事件触发后的行动空间
仓位上限
交易成本模型
模型选择与 prompt 设计
held-out 起点与样本量
```

---

## 14. 参考仓库

| 仓库 | 用途 |
|---|---|
| https://github.com/microsoft/qlib | 量化数据、模型、回测与研究平台参考 |
| https://github.com/microsoft/RD-Agent | 自动化研究与开发闭环参考 |
| https://github.com/RndmVariableQ/AlphaAgent | LLM alpha mining 与正则化探索参考 |
| https://github.com/QuantaAlpha/QuantaAlpha | 自进化 alpha trajectory 参考 |
| https://github.com/gta0804/AlphaPROBE | 检索与图结构因子进化参考 |
| https://github.com/RL-MLDM/alphagen | 公式化 alpha 生成参考 |
| https://github.com/dulyhao/alphaforge | 因子生成与组合参考 |
| https://github.com/Open-Finance-Lab/AgenticTrading | 交易管线参考 |
| https://github.com/ViktorAxelsen/MemSkill | Memory / Skill 学习参考 |
| https://github.com/EvoAgentX/EvoAgentX | Agent workflow evolution 参考 |
| https://github.com/algorithmicsuperintelligence/openevolve | 进化式代码生成参考 |

---

## 15. 总结

系统以 Horizon Track 为顶层实验单位。外层 Agent 学习适用于某一持仓周期的抽象 Heuristic Template，内层 Agent 在训练窗口内实例化参数，测试窗口冻结执行。自然语言逻辑与公式化规则一样属于 Heuristic Learning 的对象，但在 2010–2024 development WFO 中只做 shadow 记录；2025+ 使用冻结版本检验其是否能提高真实决策质量。
