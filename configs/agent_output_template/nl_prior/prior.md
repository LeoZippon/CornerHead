# 自然语言投资经验模板

该文件会复制到：

```text
/mnt/artifacts/agent_output/nl_prior/prior.md
```

它用于提示 Agent 如何维护可迁移的自然语言投资经验。结构化规则写在同目录的 `prior.json`，两者应保持一致。

## 用途

`nl_prior` 记录可以跨 Fold 复用的判断规则，用于文本、公告、研报、政策和事件信息的分析。规则应描述通用经验，不应记忆某个具体月份、题材或股票的结论。

## 规则字段

`prior.json` 中每条规则应包含：

| 字段 | 含义 |
|---|---|
| `prior_id` | 稳定 ID，例如 `prior_001`。 |
| `status` | `active`、`disabled` 或 `draft`。 |
| `scope` | 适用范围，例如 `all`、`earnings`、`policy`、`risk`。 |
| `text` | 一条简短、可迁移的判断规则。 |
| `evidence_required` | 规则生效前需要的证据类型。 |
| `effect` | 对排序、风险、剔除或仓位的影响方式。 |
| `created_fold` | 规则创建 Fold。 |
| `last_modified_fold` | 规则最后修改 Fold。 |
| `tags` | 便于审计和归类的标签。 |

## 约束

- 只使用 `decision_time` 前可见的证据。
- 使用文本证据时引用 `text_id` 或来源 ID。
- 优先沉淀稳定经验，例如治理风险、披露质量、资金压力、政策不确定性、盈利可信度。
- 不写入未来结果、测试结果、held-out 结果或具体股票结论。
- 规则应保持短小，便于修改约束检查和人工审计。

## 示例规则

```json
{
  "prior_id": "prior_example_risk_001",
  "status": "draft",
  "scope": "risk",
  "text": "近期存在监管问询、诉讼、重要股东减持或审计异常时，应降低置信度；只有后续可见证据明确修复该风险时才可恢复。",
  "evidence_required": ["announcement", "news"],
  "effect": {"risk_tag": "governance_or_disclosure_risk", "score_adjustment": "negative"},
  "created_fold": "template",
  "last_modified_fold": "template",
  "tags": ["risk", "governance", "disclosure"]
}
```
