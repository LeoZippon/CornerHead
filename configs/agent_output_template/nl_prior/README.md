# 自然语言投资经验说明

本目录会复制到：

```text
/mnt/agent/agent_output/nl_prior/
```

`README.md` 是只读说明文件。Agent 只修改：

- `prior.json`

## 目标

`prior.json` 保存可跨 Fold 复用的自然语言投资经验。它帮助 `backtest_tool` 判断公告、新闻、研报、政策和公司背景中的正负面线索。

这里不记录单次回测结论，也不直接写自然语言分数。

## 工作方式

第一次创建 `prior.json` 时，还没有 `nl_output/`。此时先基于 `/mnt/snapshot/` 中已可见的公司背景、公告、新闻、研报、政策文本样本，以及不含未来事实的通用投资逻辑，写出少量可迁移初始规则。

后续 Step 才使用验证回测结果和 `nl_output/` 修正规则。

建议流程：

1. 检查当前是否已有 `prior.json` 和历史 `nl_output/`。
2. 如果没有历史 `nl_output/`，从可见文本和公司背景中总结初始规则。
3. 如果已有历史 `nl_output/`，结合验证回测结果，只提炼可迁移经验。
4. 把经验写入或更新 `prior.json`。
5. 运行修改约束检查，再请求验证回测。

## `prior.json`

初始文件：

```json
{
  "rules": []
}
```

每条规则字段：

| 字段 | 说明 |
|---|---|
| `id` | 稳定唯一 ID，只用小写字母、数字和下划线 |
| `text` | 一条可迁移投资逻辑 |
| `evidence` | 规则需要的证据类型，例如 `announcement`、`news`、`research` |
| `effect` | 规则触发后的影响，例如 `raise_score`、`lower_score`、`support_short`、`hard_exclude` |

示例：

```json
{
  "rules": [
    {
      "id": "risk_governance_disclosure",
      "text": "近期存在监管问询、诉讼、重要股东减持或审计异常时，应降低自然语言分；如果经营和治理风险相互印证，可支持做空候选。",
      "evidence": ["announcement", "news"],
      "effect": "lower_score_or_support_short"
    }
  ]
}
```

## 写规则的标准

可以写：

- “业绩改善如果伴随明显减持公告，应降低自然语言分。”
- “重大订单或回购需要有公告或多来源文本支持，单一传闻不应显著加分。”
- “监管问询、诉讼、处罚、审计异常在短窗口内应触发降权或剔除。”
- “经营恶化、现金流承压和负面披露相互印证时，可以给出负分，支持做空候选。”

不要写：

- “2021 年 10 月买某个题材。”
- “某只股票文本好就长期买。”
- “因为上一轮验证赚钱，所以保留某个具体结论。”
- “使用未来公告、测试期结果、held-out 结果或当前常识中的未来事实。”

## 自然语言评分边界

正式自然语言评分由 `backtest_tool` 内部执行。Agent 不直接调用外部 LLM API，也不写正式 `nl_score`。

`backtest_tool` 会在 PIT 可见边界内构造公司背景、检索文本证据，并使用 `prior.json` 中的全部规则进行评分。不使用的规则应直接删除，不保留禁用字段。传给 LLM 的候选身份只包含 `ts_code`，不包含因子分、因子排名、因子理由、目标权重、验证收益或回测结果。

评分结果会写入 `results/<phase>_<idx>/nl_output/`。Agent 可以读取训练/验证期的这些结果，用于下一次修改 `prior.json`。

大致流程：

1. `backtest_tool` 读取候选股票代码、公司背景和 `prior.json` 规则。
2. LLM 根据公司背景和规则生成文本检索方向，优先组合公司名、证券代码、主营业务/行业词和事件词。
3. `backtest_tool` 在当前可见文本库中检索公告、新闻、研报和政策证据；候选公司自身证据优先，泛化行业/宏观证据只能作背景补充。
4. LLM 只基于检索到的 evidence 和规则输出自然语言分、置信度、风险标签和简短理由。
5. `backtest_tool` 校验输出后，再把自然语言分和因子分合成；LLM 看不到因子分。

基准 Prompt 轮廓：

```text
你是一个只基于 point-in-time 可见信息工作的A股研究助手。
只能使用 decision_time 前可见的 company_context、prior_rules 和 evidence。
不得使用未来行情、未来公告、测试期结果、held-out 结果或当前常识中的未来事实。
如果证据不足，输出中性分并降低 confidence。
非中性或引用证据的评分必须引用 applied_prior_ids；ID 必须来自 prior_rules。
必须引用候选公司相关的 evidence_ids；没有可用候选公司证据时 evidence_ids 为空数组。
可以在内部分析证据和规则，但最终响应只能输出一个 JSON object。
reason 字段只写可审计的简短依据。

输入包括：
- decision_time
- candidate_identity: 只包含 ts_code
- company_context
- prior_rules
- visible_evidence

最终 JSON 字段包括：
- ts_code
- nl_score
- confidence
- risk_tags
- applied_prior_ids
- evidence_ids
```

## 评分含义

自然语言分 `nl_score` 的大致含义：

| 情况 | 分数范围 |
|---|---|
| 严重负面，例如处罚、重大诉讼、审计异常、造假或退市风险，可支持回避或做空 | `[-1.00, -0.60]` |
| 中等负面，例如减持、问询、业绩承压、舆情负面 | `[-0.60, -0.20]` |
| 中性或证据不足 | `[-0.10, 0.10]` |
| 轻度正面，例如订单、回购、政策受益、业绩改善 | `[0.10, 0.40]` |
| 强正面，例如多来源一致支持基本面改善或风险解除 | `[0.40, 0.80]` |

如果文本证据不足，评分应偏中性并降低置信度。规则必须要求 evidence 支持，不应让模型凭常识猜测公司业务或事件影响。
