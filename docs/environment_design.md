# Environment 设计

本文档记录 Environment 层。Environment 负责准备 PIT 数据窗口、启动 Sandbox、提供受控执行入口和可信服务 Tool、执行回测并写审计信息。Agent 可以在 Sandbox 内探索和写代码，但只能使用 Environment 提供的数据、Shell 入口和受控 Tool。

相关边界：

- Agent 行为和输出格式见 `docs/agent_design.md`。
- Step / Fold / Epoch 编排见 `docs/pipeline_design.md`。
- 原始数据下载、单位和审计见 `docs/data_documentation.md`。
- QMT 实盘流程见 `docs/QMT_documentation.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| Environment | 准备 PIT 窗口、启动 Sandbox、提供执行入口和可信服务 Tool、执行回测和写审计信息的环境层 |
| PIT | Point-in-time，只使用决策时点已经可见的数据 |
| Sandbox | Agent 运行的隔离容器 |
| Runner | Sandbox 内负责执行 Shell/LLM 调用、记录日志和检查结果的程序 |
| Execution Gateway | Sandbox 与 Shell/可信服务 Tool 之间的入口，负责权限、路径、运行约束和日志 |
| LLM Proxy | 宿主侧大模型接口代理，保存 API key 并记录对话 |
| artifact | 单次运行产生的代码、订单计划、回测和文本分析结果 |
| manifest | 记录输入、输出、时间范围、配置和关键产物版本的文件 |
| hash | 文件或内容指纹；只在 snapshot、冻结策略产物、关键回测结果等边界使用，不要求每条 Shell 命令都生成 |
| Broker | 模拟券商接口，接收订单、生成成交/拒单和持仓状态 |
| provider | 大模型服务商，例如 DeepSeek 或其他兼容 API |
| schema | 结构化输入输出格式 |
| Held-out | 所有训练结束后才运行的冻结测试区间 |

## 目录

- [1. Environment 职责](#1-environment-职责)
- [2. PIT 数据窗口](#2-pit-数据窗口)
- [3. Sandbox](#3-sandbox)
- [4. 执行入口和可信服务 Tool](#4-执行入口和可信服务-tool)
- [5. 模拟 Broker、回测和交易约束](#5-模拟-broker回测和交易约束)
- [6. 文本和 LLM API](#6-文本和-llm-api)
- [7. 日志和审计](#7-日志和审计)
- [8. 验收清单](#8-验收清单)

## 1. Environment 职责

Environment 负责：

- 按决策时点构造 PIT 数据窗口。
- 把窗口数据放入 Sandbox 的固定只读路径。
- 提供 Sandbox Shell、策略修改约束检查、自然语言分析和模拟 Broker/回测 Tool。
- 以 Fold 运行时长为主控约束，并保留 CPU、内存、磁盘等基础护栏。
- 统一特征单位。
- 执行交易约束、订单模拟、成交模拟和回测。
- 记录 Shell、Tool、校验、回测调用、关键 manifest 和 LLM 日志。
- 提供策略产物的受控读写、修改量统计和冻结产物审计。

Environment 不负责：

- 决定投资逻辑。
- 判断哪个因子更好。
- 决定策略产物内容。
- 决定文本判断规则。
- 读取 Held-out 后参与训练。
- 真实下单。

可信日志只能由 Runner、Execution Gateway、LLM Proxy 和模拟 Broker 自动生成；具体日志合同见第 7 章。Agent 可以输出解释、原因和结构化结果，但不能替代可信日志。

## 2. PIT 数据窗口

### 2.1 时间墙

进入 Sandbox 的所有数据必须满足：

```text
available_at <= decision_time
```

如果数据没有可靠发布时间，Environment 必须使用保守规则延后可见，或者从本次窗口中排除。

### 2.2 可见数据窗口

每个 Fold 的窗口长度由配置预先定义。Agent 可以在代码中只使用其中一部分，但不能要求更长窗口。

例子：Agent 在 `2021-10-08 09:20:00+08:00` 做 2021 年 10 月到 12 月验证回测决策。2021 年 10 月 1 日至 10 月 7 日为国庆假期，因此使用节后第一个交易日前的决策时点。

| 数据域 | Snapshot 文件 | 默认准备窗口 | 2021-10-08 示例 | 可见边界 |
|---|---|---:|---|---|
| `daily` | `daily.parquet` | 最近 21 个月 | 2020-01 到 2021-09 的日频数据 | 不包含 2021-10 之后行情 |
| `intraday_1min` | `intraday_1min.parquet` | 最近 5 个交易日 | 2021-09 最后 5 个交易日的 1 分钟线 | 开盘前决策不含 2021-10-08 分钟线；盘中决策按 bar close 截到 `decision_time` |
| `fundamentals` | `fundamentals.parquet` | 最近 21 个月可见披露 | 已公告财报、分红、业绩预告/快报 | 保留公告日、报告期和多版本可见时间 |
| `events` | `events.parquet` | 最近 21 个月 | 截至 2021-10-08 09:20 已可见的资金、两融、股东、回购、解禁、大宗交易、龙虎榜 | T+1 数据按实际可见时间过滤 |
| `macro` | `macro.parquet` | 最近 21 个月 | 已发布宏观、政策、利率、全球事件和跨市场背景 | 不使用未来发布值 |
| `text` | `text_index.parquet`、`text_library/` | 最近 21 个月 | 可见文本索引和 as-of 文本库 | 正文检索必须引用文本 ID |
| `constraints` | `constraints.parquet` | 买入日、卖出日和相关持仓日 | 停牌、涨跌停、可交易性约束 | 用于订单计划和模拟成交判断 |

窗口数据可以比配置短，例如刚上市股票不足 21 个月历史，或研究数据保留下限晚于完整窗口起点。Environment 必须在 manifest 中记录实际覆盖。21 个月是默认最大可见窗口，Agent 可以在代码中只使用其中更短的一段。

### 2.3 单位合同

进入 `/mnt/snapshot` 的数值字段必须使用标准单位：

| 类型 | 标准单位 |
|---|---|
| 金额 | 元 |
| 成交量/股本 | 股 |
| 比例、收益、换手 | 小数，例如 5% 记为 `0.05` |
| 利率和费率 | 优先小数；确需 bps 时字段名必须带 `_bps` |

原始单位、转换规则和转换前字段必须写入 `manifest.json`。单位不明的字段不能进入模型可见数据；订单计划校验也必须拒绝依赖单位不明字段生成的交易意图。

### 2.4 Snapshot 数据路径

`/mnt/snapshot` 是只读输入目录，推荐路径：

```text
/mnt/snapshot/
  manifest.json
  daily.parquet
  intraday_1min.parquet
  fundamentals.parquet
  events.parquet
  macro.parquet
  text_index.parquet
  text_library/
  constraints.parquet
  universe.parquet
```

`text_library/` 是 as-of 文本库目录，必须只读挂载到 Sandbox；正文或正文片段必须由 `text_index.parquet` 引用。

## 3. Sandbox

### 3.1 运行环境

默认使用本地 Docker。隔离边界由非 root 用户、关闭网络、只读 snapshot、可写 artifacts、运行时长 deadline、基础资源护栏和自动日志共同实现。当前设计不要求额外容器运行时。

Sandbox 应包含：

| 项目 | 要求 |
|---|---|
| Python | 固定版本和依赖，不在运行中安装新包 |
| 用户 | Agent 进程使用非 root 用户；Runner/root 只用于冻结执行和权限管理 |
| 网络 | 默认关闭；Sandbox 内 Python 和 Shell 不联网；LLM 调用只能经宿主侧 LLM Proxy 发起 |
| 训练/验证数据 | `/mnt/snapshot` 只读，Agent 用户可读 |
| 测试数据 | 可挂载到 root-only 路径，例如 `/mnt/test_snapshot`；Agent 用户和 `sandbox_shell_tool` 不可读、不列目录，只有 Runner/root 在冻结后执行测试回测 |
| 输出 | `/mnt/artifacts` 顶层结构由 Environment 创建；Agent 可自由写 `workspace/`，并按约定更新 `agent_output/` 下的正式产物文件；`results/` 只由 `backtest_tool` 写入 |
| 运行时长 | 由 Pipeline 下发 `fold_deadline_at`，每个 Fold 默认 20 分钟；Step 不设单独时长限制 |
| 基础护栏 | CPU、内存、磁盘、进程数和输出大小保留上限，防止单次运行在 deadline 前耗尽机器资源 |

### 3.2 运行产物路径

`/mnt/artifacts` 是本次运行的受控产物目录；顶层结构固定，不得挂载历史产物或主仓库。

```text
/mnt/artifacts/
  workspace/
  agent_output/
    factor/
      main.py
      factors.json
    nl_prior/
      prior.md
      prior.json
  results/
    valid_000/
      summary.json
      detailed_return.json
      order_plan.parquet
      nl_output/
  logs/
```

子目录归属：

| 目录 | 写入方 | Agent 可读 | 说明 |
|---|---|---|---|
| `workspace/` | Agent | 是 | 临时代码、数据探查脚本、调试输出和草稿；不冻结为策略产物 |
| `agent_output/factor/` | Agent | 是 | 正式因子逻辑、入口函数和登记表；必须包含 `main.py` 和 `factors.json` |
| `agent_output/nl_prior/` | Agent | 是 | 正式自然语言投资先验；必须包含 `prior.md`，结构化规则写入 `prior.json`；自然语言分析步骤不能直接写入 |
| `results/<phase>_<idx>/` | `backtest_tool` | 训练/验证期可读；测试和 held-out 不反馈给 Agent | 单次回测调用结果目录，例如 `valid_000/`、`valid_001/`、`test_000/` |
| `logs/` | Environment Runner / Execution Gateway / LLM Proxy / 模拟 Broker | 默认不作为 Agent 输入 | 可信运行日志、Shell/LLM/服务调用、LLM conversation log、stdout/stderr、错误和故障复核输出 |

Fold 起点策略产物由 Pipeline 从 `experiments/<experiment_id>/strategy_artifacts/<epoch_id>/<strategy_artifact_id>/` 复制到 `/mnt/artifacts/agent_output/factor/` 和 `/mnt/artifacts/agent_output/nl_prior/`。如果是第一次创建策略产物，Environment 从 `configs/agent_output_template/` 初始化 `main.py`、`factors.json`、`prior.md` 和 `prior.json`。`/mnt/artifacts` 的顶层目录由 Environment 创建和维护，Agent 不能新增、删除或重命名顶层目录。

训练/验证时，Agent 应先在 `workspace/` 中写临时代码和调试脚本；确认可运行后，再把最终因子代码和投资先验写入 `agent_output/factor/` 和 `agent_output/nl_prior/` 的约定文件。`workspace/` 不冻结、不回放、不参与策略产物 diff；只有 `agent_output/factor/` 和 `agent_output/nl_prior/` 可能冻结为策略产物。

Environment 负责生成 `strategy_artifact_diff.json`，记录文件 hash、代码结构摘要、`factors.json` 因子变更和 `nl_prior` 结构化差异。Environment 按 Pipeline 下发的 `modification_constraints` 决定是否允许 `backtest_tool` 运行；Pipeline 只记录结果，并在验证后决定是否冻结。

`results/` 是回测结果根目录。每次 `backtest_tool` 调用都创建一个新的子目录，命名为 `<phase>_<idx>`，例如 `valid_000`、`valid_001`、`test_000` 或 `heldout_000`。该子目录由 `backtest_tool` 独占写入；Agent 在训练/验证期只读这些结果，在测试和 held-out 阶段不接收这些结果。

测试和 held-out 必须使用冻结内容，运行后校验冻结产物没有变化。运行结束后，Environment/Pipeline 将 `/mnt/artifacts` 收集到宿主机实验目录，例如 `experiments/<experiment_id>/artifacts/<run_id>/`，并写入 run manifest。Sandbox 内路径只是运行时挂载点，不是长期数据目录。

### 3.3 Python 环境

Python 环境要支持：

- pandas / numpy / pyarrow / duckdb。
- scikit-learn / statsmodels 等常用研究包。
- 项目内部工具包。
- 本地文本检索包。

Agent 可以把临时 Python 文件写到 `/mnt/artifacts/workspace/`。正式策略代码确认后写入 `/mnt/artifacts/agent_output/factor/main.py`；自然语言投资经验写入 `/mnt/artifacts/agent_output/nl_prior/prior.md` 和 `/mnt/artifacts/agent_output/nl_prior/prior.json`；不能修改主仓库文件。

### 3.4 Agent Runner

Agent Runner 是 Sandbox 内的执行框架。可以借鉴 LangGraph 等开源 Agent 框架的思路，但不要求直接依赖某个框架。

最小能力：

| 能力 | 说明 |
|---|---|
| 运行记录 | 记录当前 Step 输入、输出、Shell/Tool 调用、校验、回测和错误 |
| 入口注册 | 只允许使用白名单执行入口和可信服务 Tool |
| 检查点 | Step 中断后可复核，不自动跳过失败 |
| LLM 日志 | 每次 provider 调用都写 conversation log |
| Deadline 控制 | 只执行 Fold 级 deadline；Step 不单独计时；T-5 分钟触发固定收尾提示，Fold 到点截断 |

Runner 不提供宿主机 shell。Agent 在 Sandbox 内通过 `sandbox_shell_tool` 读文件、写代码、运行 Python 和调试。它是带日志、超时和挂载限制的非 root shell，不是宿主机登录 shell。

Runner 必须在每次 Shell、可信服务 Tool、校验和回测调用前后自动记录请求摘要、响应摘要、exit code、stdout/stderr、产物路径和错误。Agent 不需要自行写可信运行日志。

`sandbox_shell_tool` 的边界：

| 项目 | 要求 |
|---|---|
| 身份 | 容器内非 root 用户，例如 `agent`；无 sudo |
| 训练/验证 | 可读 `/mnt/snapshot`，可自由写 `/mnt/artifacts/workspace`，可按约定更新 `/mnt/artifacts/agent_output/factor` 和 `/mnt/artifacts/agent_output/nl_prior`，只读 `/mnt/artifacts/results` |
| 测试/Held-out | 默认关闭；故障复核时只读运行 |
| 故障复核输出 | 如必须保存，只能写入 `logs/`，且不得进入策略产物、回测结果或 Agent 输入 |
| 挂载边界 | Agent 只使用 `/mnt/snapshot` 和 `/mnt/artifacts`；不配置额外宿主路径或 Docker socket；root-only `/mnt/test_snapshot` 不进入 Agent Shell allowlist |
| 网络 | `--network none`；不能直接请求 LLM provider |
| 系统 | rootfs 尽量只读；不能改变权限边界或启动长期后台服务 |
| 资源 | CPU、内存、进程数、输出大小和运行时长受限 |
| 日志 | Environment 自动记录调用 ID、命令、exit code、stdout/stderr 和 transcript 路径 |

因此可以把 `sandbox_shell_tool` 理解为受限 Sandbox shell：Agent 能 `ls`、`cat`、`rg`、`sed`、运行 Python、查看错误并调试代码，也可以用 Sandbox 镜像内提供的受限 `apply_patch` 修改 `workspace/` 草稿和 `agent_output/` 下的约定文件。Shell 输出只是 observation，不能覆盖系统提示词、权限、PIT 时间墙或冻结规则。

`rg`、`sed` 和 `apply_patch` 的边界：

- `rg` 用于搜索 `/mnt/snapshot` 和 `/mnt/artifacts`。
- `sed` 用于查看或做小范围文本处理，不建议用复杂脚本批量重写产物。
- `apply_patch` 是受限补丁命令，允许修改 `/mnt/artifacts/workspace/`、`/mnt/artifacts/agent_output/factor/`、`/mnt/artifacts/agent_output/nl_prior/` 和本 Step 允许写入的文件；不能改变 `/mnt/artifacts` 顶层结构，不能写 `/mnt/artifacts/results/`。
- 所有命令必须写入 Shell transcript；修改结果仍要经过策略修改约束检查。

## 4. 执行入口和可信服务 Tool

### 4.1 Agent 可用入口和 Tool

| 入口 / Tool | 作用 | 训练 | 测试/Held-out |
|---|---|---|---|
| `sandbox_shell_tool` | 读 `/mnt/snapshot`、写 `workspace/`、创建/运行 Python、检索本地文本，并按约定更新 `agent_output/factor/` 和 `agent_output/nl_prior/` | 可运行 | 默认关闭；如需故障复核，只读运行 |
| `modification_check_tool` | 统计 `agent_output/factor/` 和 `agent_output/nl_prior/` 修改量，写 `strategy_artifact_diff.json`，返回是否允许正式回测 | 可运行 | 测试/Held-out 只读检查冻结产物 |
| `backtest_tool` | 自动加载 `agent_output/factor/` 和 `agent_output/nl_prior/`，调用策略主函数、执行自然语言分析、订单计划校验、模拟 Broker 和回放，并写入 `results/` | 可运行 | 由 Runner/root 冻结执行，不把测试结果反馈给 Agent |

Shell 已经可以创建 Python 文件、执行脚本、查看 stderr 并调试，因此不再拆分独立的 Python 执行入口或策略产物读写入口。Agent 的自由探索写在 `workspace/`；`agent_output/factor/` 和 `agent_output/nl_prior/` 是正式产物目录，只能按约定文件结构更新。

入口不能信任 Agent 自行传入的绝对路径或决策时间。`snapshot_dir`、`artifact_dir`、`decision_time`、`fold_id` 和可写目录必须来自 run manifest。入口必须拒绝 `/mnt/snapshot`、`/mnt/artifacts` 之外的路径。

### 4.2 修改约束检查 Tool

`modification_check_tool` 是正式回测前的确定性门禁。

| 项目 | 规则 |
|---|---|
| 调用方 | Agent 可在正式回测前自查；Pipeline 必须在正式 `backtest_tool` 前强制复查 |
| 调用参数 | Agent 调用时不传业务参数，只触发“检查当前正式工作副本” |
| 上下文来源 | 父产物、`/mnt/artifacts` 路径、是否初始产物、修改约束和 Fold 信息都来自 run manifest |
| 检查范围 | 只检查 `agent_output/factor/` 和 `agent_output/nl_prior/`；不检查 `workspace/` 或 `results/` |
| 计数依据 | 只使用文件、行数、`factors.json` 登记因子和 `nl_prior` 规则/字符数等可复核计数 |
| 放行结果 | Environment 返回 `allowed_to_backtest`，并直接决定 `backtest_tool` 是否允许运行 |
| 权限边界 | Agent 不能传入或放宽修改约束；Tool 必须拒绝 Agent 覆盖系统配置 |

`factors.json` 格式校验：

| 项目 | 要求 |
|---|---|
| 文件路径 | `agent_output/factor/factors.json` |
| 顶层结构 | JSON object，包含 `artifact_type="factor_registry"` 和 `factors` list |
| 必需字段 | 每条因子必须包含 `factor_id`、`status`、`function`、`description`、`inputs`、`lookback_days`、`output_column`、`direction`、`created_fold`、`last_modified_fold`、`tags` |
| ID 规则 | `factor_id` 必须唯一并使用稳定命名 |
| 状态枚举 | `status` 只能是 `active`、`disabled` 或 `draft` |
| 方向枚举 | `direction` 只能是 `positive`、`negative`、`neutral` 或 `nonlinear` |
| 类型要求 | `inputs` 和 `tags` 必须是 list |
| 代码同步 | `active` 因子的 `function` 应能在 `main.py` 中找到 |
| 失败处理 | 格式错误、ID 重复、父/当前登记表缺失或代码改动未同步登记表时，返回 `allowed_to_backtest=false` |

因子 ID 统计：

| 输出字段 | 统计规则 |
|---|---|
| `new_factor_ids` | `current_factor_ids - parent_factor_ids` |
| `deleted_factor_ids` | `parent_factor_ids - current_factor_ids` |
| `modified_factor_ids` | 保留 ID 的登记字段变化，或该因子登记的 `function` 代码结构摘要变化 |
| 无有效登记表 | 只能统计文件、行数和函数变化，不能输出因子 ID 级结论 |

输出：

```json
{
  "status": "ok",
  "allowed_to_backtest": true,
  "strategy_artifact_diff_path": "/mnt/artifacts/strategy_artifact_diff.json",
  "factor": {
    "files_changed": 2,
    "functions_changed": 3,
    "diff_lines": 84,
    "factor_registry_valid": true,
    "new_factor_ids": ["mom_60d"],
    "deleted_factor_ids": [],
    "modified_factor_ids": ["quality_profitability"],
    "new_factor_count": 1,
    "deleted_factor_count": 0,
    "modified_factor_count": 1
  },
  "nl_prior": {
    "rules_added": 1,
    "rules_deleted": 0,
    "rules_rewritten": 1,
    "rules_total": 12,
    "max_chars_per_rule": 180
  }
}
```

若 `allowed_to_backtest=false`，Environment 只返回超限原因和 diff 统计，并拒绝运行正式回测。Agent 可以继续修改 `/mnt/artifacts/agent_output/factor/` 和 `/mnt/artifacts/agent_output/nl_prior/` 后再次检查；Pipeline 只记录该 Step 未进入后续验证，或调度 Agent 缩小修改后重试。

### 4.3 Python 执行

Agent 通过 `sandbox_shell_tool` 运行 Python，不再拆出独立执行入口。临时文件应放在 `/mnt/artifacts/workspace/`；正式策略入口必须写入 `/mnt/artifacts/agent_output/factor/main.py`，登记因子必须同步写入 `/mnt/artifacts/agent_output/factor/factors.json`。Agent 可以自由决定 `workspace/` 内的文件名和函数名，但正式回测只调用 `agent_output/factor/main.py` 的主函数，不读取临时脚本输出。

Python 代码必须满足：

- 只能读取 `/mnt/snapshot` 和 `/mnt/artifacts`。
- 可以选择只用窗口的一部分，例如只取最近 100 行。
- 不能联网、不能安装依赖、不能访问 API key。
- 调试输出必须是结构化表格或 JSON；正式输出以策略主函数返回值为准。
- stdout、stderr、脚本路径、输出路径和 exit code 由 Environment 自动记录；代码和数据版本在 Fold/run manifest 里聚合记录。

### 4.4 策略主函数合同

`agent_output/factor/` 内必须提供固定主函数和因子登记表。固定主函数是回测和测试唯一策略入口：

```python
def generate_orders(context: dict) -> "pandas.DataFrame":
    ...
```

`backtest_tool` 负责构造 `context`，Agent 不能自行传入或覆盖。`context` 至少包含：

| 字段 | 含义 |
|---|---|
| `decision_time` | 本次决策时点 |
| `buy_trade_date` | 回放买入或建仓交易日 |
| `sell_trade_date` | 当前初始流程的清仓交易日 |
| `snapshot_dir` | 只读 PIT 数据窗口路径 |
| `nl_prior_dir` | 当前冻结或工作副本中的自然语言投资先验路径 |
| `portfolio_state` | 决策前现金、持仓和可用库存 |
| `run_config` | 股票池、持有期、成本、仓位上限和回测参数 |

主函数返回 `pandas.DataFrame`，不写独立中间文件。返回列至少包含：

| 列 | 要求 |
|---|---|
| `ts_code` | 股票代码 |
| `action` | 当前可先支持 `target_weight`；后续可扩展 `buy/sell/short/cover/hold` |
| `target_weight` | 目标权重，当前初始长仓流程必须提供 |
| `score` | 因子或综合排序分 |
| `reason` | 简短理由 |
| `source_artifacts` | 使用的数据或规则来源 ID，可为空列表但字段必须存在 |

可选列包括 `order_type`、`amount`、`volume`、`risk_tags`、`metadata`。`backtest_tool` 会在内存中接收该返回值，随后执行自然语言分析、权重归一化、订单计划生成和交易约束校验，并把校验后的订单计划写入 `results/<phase>_<idx>/` 下的运行产物。Agent 不需要、也不应该单独维护中间目录。

### 4.5 `backtest_tool`、自然语言分析和订单计划校验

`backtest_tool` 是正式回测入口。它自动加载当前 `agent_output/factor/` 和 `agent_output/nl_prior/`，按 run manifest 指定的 snapshot、决策时点和持有期执行回放。正式验证和测试结果只能来自 `backtest_tool`，不能来自 Agent 自己写的临时 Python 回测。

回测内部顺序：

1. 加载 `agent_output/factor/` 和 `agent_output/nl_prior/`。
2. 调用 `agent_output/factor/main.py::generate_orders(context)`，得到候选股票、目标权重或订单意图。
3. 运行内部自然语言分析步骤，对候选股票做文本检索、LLM 分析和 evidence 绑定。
4. 合成因子分和自然语言分，生成最终订单计划。
5. 执行订单计划校验。
6. 调用模拟 Broker 回放成交、拒单、持仓、成本和收益。

运行前置条件：

- 最近一次 `modification_check_tool` 返回 `allowed_to_backtest=true`。
- `agent_output/factor/` 和 `agent_output/nl_prior/` 位于 `/mnt/artifacts/`，且 manifest 与本次 run 一致。
- run manifest 中的 `decision_time`、交易日、持有周期、成本模型和股票池有效。

输入：

```json
{
  "agent_output_dir": "/mnt/artifacts/agent_output",
  "result_dir": "/mnt/artifacts/results/valid_000",
  "snapshot_id": "snapshot_fold2022Q1_validation",
  "buy_trade_date": "2021-10-08",
  "sell_trade_date": "2021-12-31",
  "run_nl_analysis": true
}
```

输出：

```json
{
  "status": "ok",
  "candidate_pool_artifact_id": "candidate_pool_step02",
  "result_dir": "/mnt/artifacts/results/valid_000",
  "nl_output_path": "/mnt/artifacts/results/valid_000/nl_output/scores.jsonl",
  "order_plan_artifact_id": "order_plan_step02",
  "summary_path": "/mnt/artifacts/results/valid_000/summary.json",
  "detailed_return_path": "/mnt/artifacts/results/valid_000/detailed_return.json",
  "artifact_hash": "sha256:...",
  "selected_rows": 20,
  "return": 0.031,
  "max_drawdown": 0.042,
  "warnings": []
}
```

主函数返回值和最终订单计划至少检查：

| 字段 | 含义 |
|---|---|
| `ts_code` | 股票代码 |
| `target_weight` | 目标权重，非负且总和不超过配置上限 |
| `score` | Agent 代码和自然语言分析合成后的排序分 |
| `reason` | 简短原因，引用因子字段或文本 evidence |
| `source_artifacts` | 生成该行所依赖的代码、数据和文本产物 ID |

订单计划校验内容：

- 股票必须在当前可交易股票池内。
- 权重、持仓数量和集中度必须满足配置。
- 不能包含不在 snapshot 或 Environment 服务输出中的股票和文本引用。
- 不能包含未来日期、未来价格或未登记字段。
- 校验失败要返回错误，不能自动替 Agent 删除股票。

### 4.6 自然语言分析步骤和内部文本检索

自然语言分析是 `backtest_tool` 的内部评分步骤，不作为 Agent 单独可调用 Tool。它包含文本检索、LLM Proxy 调用、evidence 引用校验和 conversation log 记录，避免 Agent 自己拼接任意外部 API 调用。

| 项目 | 要求 |
|---|---|
| 输入要点 | 候选池 artifact、`nl_prior`、候选关键词、约束 prompt hash、最大并行数 |
| 内部检索 | 只检索 `/mnt/snapshot/text_index.parquet` 和 `/mnt/snapshot/text_library/` 中的可见文本 |
| Manifest 注入 | `decision_time`、真实候选池路径、日志路径、文本范围和 provider 配置 |
| 输出要点 | `results/<phase>_<idx>/nl_output/scores.jsonl`、conversation log id/hash、调用次数、错误 |

Agent 可以通过 Shell/Python 本地查看 `text_index.parquet` 和 `text_library/`，但进入正式验证/测试的自然语言评分必须来自 `backtest_tool` 内部。每条评分必须引用 `text_id/source_hash`；没有 evidence 的判断只能标记为低置信度或无证据，不能伪造引用。

`results/<phase>_<idx>/nl_output/` 中的结构化分数和标签可供 Agent 在当前训练 Step 内读取和合成；LLM conversation log 只通过 `conversation_log_id/hash` 进入 manifest，不作为 Agent 可读路径返回。每个文本分析任务都必须引用 `text_id`，不能凭空使用外部知识。

对于回测中的自然语言评分，`backtest_tool` 内部登记的请求是唯一入口。Sandbox 本身无网络；Python 和 Shell 不能直接调用外部 provider。Agent 主对话请求由 Runner 登记，回测文本评分请求由 `backtest_tool` 登记，二者都只能由宿主侧 LLM Proxy 发起。

自然语言分析步骤可以在每个训练 Step 的 `backtest_tool` 内部运行，用于当前候选股票打分，也可以为下一 Step 的 `nl_prior` 修改提供证据。它只能写 `results/<phase>_<idx>/nl_output/` 和 conversation log，不能直接改写 `nl_prior`；`nl_prior` 只能由 Agent 通过 Shell 修改，并由 `modification_check_tool` 统计和放行。

LLM Proxy 只负责 provider 请求边界：每次调用外部 provider 都必须设置最长等待时间，超过该时间未返回则记为 `timeout`，不能阻塞整个 Fold。该等待时间不得超过当前 Fold 剩余时间。若 provider 请求已经发出，只能等待返回、超时取消或丢弃结果，不能向同一个 in-flight 请求追加 prompt。Fold deadline、收尾提示和实验级 ledger 由 Pipeline/Runner 管理。

## 5. 模拟 Broker、回测和交易约束

### 5.1 模拟 Broker 边界

Environment 应尽量模拟 QMT 的实盘交互形态，但不连接真实券商账户。Agent 或冻结策略只能提交结构化订单，不能直接写成交、持仓或收益。

模拟 Broker 至少提供：

| 接口 | 含义 |
|---|---|
| `get_account()` | 返回现金、总资产、可用资金和风控限制 |
| `get_positions()` | 返回当前持仓、可卖数量和成本 |
| `submit_order()` | 接收买入、卖出或目标权重订单 |
| `cancel_order()` | 撤销尚未成交订单 |
| `query_orders()` | 查询订单状态、拒单原因和成交明细 |

订单提交示例：

```json
{
  "ts_code": "000001.SZ",
  "side": "buy",
  "order_type": "target_weight",
  "target_weight": 0.05,
  "limit_price": null,
  "reason": "score=0.81; nl_score=0.62",
  "source_artifacts": ["order_plan_step02", "nl_scores_step02"]
}
```

模拟 Broker 返回：

```json
{
  "order_id": "ord_...",
  "status": "accepted",
  "submitted_at": "2021-10-08T09:25:00+08:00",
  "fillable_from": "2021-10-08",
  "reason": null
}
```

拒单和未成交也必须写入回测结果，不能静默删除。

### 5.2 最小回测版本

初始流程可以使用最简单的订单规则：

1. 在决策交易日买入选中股票。
2. 按给定持有周期持有。
3. 周期结束卖出。
4. 中间不调仓、不做 T、不做空、不因事件临时修改。

做 T、融券做空、突发事件再决策不属于本流程，但未来也应通过同一个模拟 Broker 订单接口进入回放。

### 5.3 回测输入

```json
{
  "order_plan_artifact_id": "order_plan_step02",
  "order_submission_mode": "target_weight_batch",
  "buy_trade_date": "2021-10-08",
  "sell_trade_date": "2021-12-31",
  "cost_model": {
    "commission_bps": 3,
    "slippage_bps": 5
  }
}
```

`order_plan_artifact_id` 是推荐的审计入口：`backtest_tool` 调用策略主函数后，在内部完成自然语言分析、权重合成和订单计划校验，再把校验后的订单计划提交给模拟 Broker。测试或 held-out 也使用同一入口；冻结策略不能绕过 `backtest_tool` 直接提交外部订单文件。

### 5.4 交易约束

Environment 必须检查：

- 股票是否在可交易股票池。
- 订单提交时点和可成交交易日是否满足 PIT。
- 买入日是否停牌。
- 买入价是否受涨停限制。
- 卖出日是否停牌或跌停无法卖出。
- 权重是否超过上限。
- 股票数量是否超过配置。
- 现金、持仓、可卖数量和 A 股 T+1 规则是否满足。
- 是否使用了未来价格。

回测失败时必须返回明确错误，不能自动删除失败股票。

### 5.5 收益统计

至少输出：

- 总收益。
- 年化收益。
- 最大回撤。
- 胜率。
- 持仓数量。
- 换手率。
- 订单接受、拒单、撤单和成交统计。
- 每只股票的买入、卖出、成本和收益。

## 6. 文本和 LLM API

### 6.1 LLM API 安全合同

登记过的 Agent Runner 主对话请求和 `backtest_tool` 自然语言分析请求都可以通过宿主侧代理调用 LLM API。Sandbox 内的 Shell/Python 不能直接请求 provider，也不能读取 API key。

```text
Agent Runner main conversation -> local LLM Proxy -> provider
backtest_tool natural-language analysis step -> local LLM Proxy -> provider
```

API key 只在宿主侧读取，不进入 Sandbox、prompt、artifact 或日志。

### 6.2 Conversation Log 边界

所有真实 LLM 调用必须写入 `llm_conversations.jsonl`。日志必须包含 messages、原始响应、解析结果、用量、错误、provider、model、temperature、seed 和关联产物 ID；不能包含 Authorization header 或 API key。具体文件位置和 manifest 要求见第 7 章。

## 7. 日志和审计

Environment 只定义 Sandbox 运行时实际写哪些文件；实验级账本字段、Fold/run 聚合 hash 和输出路径由 `docs/pipeline_design.md` 第 7 章统一定义。

`execution_calls.jsonl` 和 `llm_conversations.jsonl` 属于同一个 Agent session / conversation trace，不是两套独立对话。二者必须共享 `experiment_id`、`epoch_id`、`fold_id`、`step_id`、`run_id`、`conversation_id`，并用 `call_id` / `parent_call_id` 串联顺序。`execution_calls.jsonl` 记录 Shell、Tool、回测和代理调用的摘要；`llm_conversations.jsonl` 记录真实 provider messages、原始响应和用量。LLM 调用应在 `execution_calls.jsonl` 中有摘要事件，并通过 `llm_call_id` 或 `conversation_log_id` 指向 `llm_conversations.jsonl` 中的完整记录。

每次 Sandbox 运行至少写：

| 文件 | 内容 |
|---|---|
| `run_manifest.json` | 本次 Sandbox 输入、输出、配置、deadline、镜像版本和关键产物版本 |
| `execution_calls.jsonl` | 同一 conversation trace 下的 Shell/Tool/回测/代理调用摘要、exit code、stdout/stderr、产物路径和错误 |
| `strategy_artifact_diff.json` | 父产物和当前工作副本的 diff、修改约束消耗和验收状态 |
| `results/<phase>_<idx>/summary.json` | 单次 `backtest_tool` 调用摘要、核心指标、状态和错误 |
| `results/<phase>_<idx>/detailed_return.json` | 详细收益、回撤、持仓、成交、拒单和成本统计 |
| `results/<phase>_<idx>/order_plan.parquet` | 策略主函数返回值经过校验和权重合成后的订单计划 |
| `results/<phase>_<idx>/nl_output/` | 自然语言分析分数、风险标签和 evidence 引用 |
| `llm_conversations.jsonl` | 同一 conversation trace 下的真实 LLM 调用完整记录，不包含 API key 或 Authorization header |

`snapshot_manifest`、`factor_manifest`、`order_plan_manifest` 等可以作为 `run_manifest.json` 的引用或子文件存在，不需要在本章重复定义字段。

主进程读取 artifact 前必须检查：

- exit code。
- run manifest 是否完整。
- 是否写入禁止路径。
- 关键 manifest 和冻结产物版本是否匹配。
- LLM 日志是否完整。

## 8. 验收清单

Environment 相关改动至少检查：

- Sandbox 只读窗口数据是否满足 PIT。
- Agent 是否能写 Python，但只能在 Sandbox 内运行。
- Shell/Python 运行是否记录命令、exit code、stdout/stderr、脚本路径和产物路径；关键版本是否汇总进 Fold/run manifest。
- 订单计划校验是否只校验 `backtest_tool` 生成的订单计划，不改写策略主函数。
- 模拟 Broker 是否只接受结构化订单，且拒单/成交都有日志。
- 文本检索是否只返回可见文本。
- LLM API key 是否只在宿主代理读取。
- 回测是否只使用决策后允许的交易日行情。
- 测试和 held-out 是否禁止修改代码和经验。
- 所有入口、校验和回测失败是否显式报错。
