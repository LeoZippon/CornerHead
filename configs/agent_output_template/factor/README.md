# 因子产物说明

本目录会复制到：

```text
/mnt/artifacts/agent_output/factor/
```

`README.md` 是只读说明文件。Agent 只修改：

- `main.py`
- `factors.json`

## 目标

在 `main.py` 中编写因子逻辑，正式回测时由 `backtest_tool` 基于 `/mnt/snapshot/` 中的 PIT 可见数据输出候选股票和因子分。

这里不生成最终订单。最终权重、自然语言评分、交易约束和回测由 `backtest_tool` 处理。

Agent 需要在 `main.py` 内完成因子计算、排序和初筛，返回有限候选池，不返回全市场因子表。候选数量上限由运行配置给出，默认建议控制在 30-100 只；超过上限会被 Environment 拒绝，而不是由 Environment 替 Agent 截断。

## 工作方式

建议流程：

1. 先在 `/mnt/artifacts/workspace/` 写临时代码和分析脚本，读取 `/mnt/snapshots/train/` 做训练探索。
2. 确认逻辑可运行后，把正式函数整理到 `main.py`。
3. 同步更新 `factors.json`，登记生效因子。
4. 运行修改约束检查，再请求验证回测。

## `main.py` 要求

`main.py` 必须提供无参数入口：

```python
def generate_candidates():
    ...
```

正式代码只能读取：

- `/mnt/snapshot/`：`backtest_tool` 正式运行时绑定的当前决策输入窗口。
- `/mnt/artifacts/agent_output/nl_prior/`：当前自然语言投资经验。

Agent 调试时可以把模板环境变量 `MQ_SNAPSHOT_DIR` 临时设为 `/mnt/snapshots/train` 来运行同一套数据读取逻辑；正式 `backtest_tool` 必须固定使用 `/mnt/snapshot`。

不要在正式代码里硬编码读取 `/mnt/snapshots/train`、`/mnt/snapshots/valid`、`/mnt/snapshots/test`、回测结果、未来日期、held-out 信息或外部网络。

## 输出格式

`generate_candidates()` 返回 Agent 筛选后的候选池，必需列：

| 列 | 说明 |
|---|---|
| `ts_code` | 股票代码 |
| `factor_score` | 因子逻辑计算出的原始排序分 |
| `reason` | 简短理由 |
| `source_artifacts` | 使用的数据、函数或规则来源 ID |

`factor_score` 只能来自因子逻辑，不能使用自然语言评分、验证收益、测试结果或回测后验信息。

## `factors.json`

`factors.json` 登记当前策略中真正生效的因子，用于修改约束检查。

初始文件：

```json
{
  "factors": []
}
```

每个因子字段：

| 字段 | 说明 |
|---|---|
| `id` | 稳定唯一 ID，只用小写字母、数字和下划线 |
| `function` | `main.py` 中对应函数名 |
| `description` | 因子含义 |
| `lookback_days` | 实际使用的最大历史窗口天数 |
| `direction` | `positive`、`negative`、`neutral` 或 `nonlinear` |

示例：

```json
{
  "factors": [
    {
      "id": "mom_amount_60d",
      "function": "compute_mom_amount_60d",
      "description": "60日收益动量和成交额改善组合因子",
      "lookback_days": 60,
      "direction": "positive"
    }
  ]
}
```

`factors.json` 中存在的因子视为生效因子，必须能在 `main.py` 中找到对应函数。不使用的因子应直接删除，不保留禁用字段。新增、删除、改名或实质修改因子时，必须同步更新 `factors.json`。

## 好规则和坏规则

可以写：

- “最近 60 日收益改善且成交额同步放大时提高排序分。”
- “短期涨幅过高且成交额萎缩时降低排序分。”

不要写：

- “2021 年 10 月买某个行业。”
- “买某只具体股票。”
- “根据验证期表现直接调高某个股票分数。”
- “使用测试期或 held-out 结果。”
