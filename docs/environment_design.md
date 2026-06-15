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

## 导航

- [1. Environment 职责](#1-environment-职责)
- [2. PIT 数据窗口](#2-pit-数据窗口)
  - [2.1 时间墙](#21-时间墙)
  - [2.2 可见数据窗口](#22-可见数据窗口)
  - [2.3 单位合同](#23-单位合同)
  - [2.4 Snapshot 数据路径](#24-snapshot-数据路径)
  - [2.5 PIT 特征层产物](#25-pit-特征层产物)
- [3. Sandbox](#3-sandbox)
  - [3.1 运行环境](#31-运行环境)
  - [3.2 运行产物路径](#32-运行产物路径)
  - [3.3 Python 环境](#33-python-环境)
  - [3.4 Agent Runner](#34-agent-runner)
- [4. 执行入口和可信服务 Tool](#4-执行入口和可信服务-tool)
  - [4.1 Agent 可用入口和 Tool](#41-agent-可用入口和-tool)
  - [4.2 `sandbox_shell_tool`](#42-sandbox_shell_tool)
  - [4.3 `modification_check_tool`](#43-modification_check_tool)
  - [4.4 `backtest_tool`](#44-backtest_tool)
  - [4.5 `finish_fold_tool`](#45-finish_fold_tool)
- [5. 模拟 Broker、回测和交易约束](#5-模拟-broker回测和交易约束)
  - [5.1 模拟 Broker 边界](#51-模拟-broker-边界)
  - [5.2 最小回测版本](#52-最小回测版本)
  - [5.3 回放配置](#53-回放配置)
  - [5.4 交易约束](#54-交易约束)
  - [5.5 收益统计](#55-收益统计)
- [6. LLM API 边界](#6-llm-api-边界)
  - [6.1 调用入口](#61-调用入口)
  - [6.2 安全和超时](#62-安全和超时)
  - [6.3 调用明细落点](#63-调用明细落点)
- [7. 运行日志和审计](#7-运行日志和审计)
  - [7.1 运行文件](#71-运行文件)
  - [7.2 读取权限](#72-读取权限)
  - [7.3 审计检查](#73-审计检查)
- [8. 验收清单](#8-验收清单)

## 1. Environment 职责

Environment 负责：

- 按决策时点构造 PIT 数据窗口。
- 把窗口数据放入 Sandbox 的固定只读路径。
- 提供 Sandbox Shell、策略修改约束检查、回测 Tool、自然语言评分步骤和模拟 Broker。
- 以 Fold 运行时长为主控约束，并保留 CPU、内存、磁盘等基础护栏。
- 统一特征单位。
- 执行交易约束、订单模拟、成交模拟和回测。
- 记录 Shell、Tool、校验、回测调用、关键 manifest 和 LLM 日志。
- 提供策略产物的受控读写、修改量统计和冻结产物审计。

Environment 不负责：

| 事项 | 归属文档 / 边界 |
|---|---|
| 决定投资逻辑 | `docs/agent_design.md` |
| 判断哪个因子更好 | `docs/agent_design.md` 和 `docs/pipeline_design.md`；Environment 只执行和记录结果 |
| 决定策略产物内容 | `docs/agent_design.md` |
| 决定文本判断规则 | `docs/agent_design.md`；Environment 只按规则执行自然语言评分 |
| 读取 Held-out 后参与训练 | 禁止；Held-out 边界由 `docs/pipeline_design.md` 维护 |
| 真实下单 | `docs/QMT_documentation.md`；Environment 只提供模拟 Broker 和回测 |

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

例子：Agent 在 `2021-10-08 09:25:00+08:00` 做 2021 年 10 月到 12 月验证回测决策。2021 年 10 月 1 日至 10 月 7 日为国庆假期，因此使用节后第一个交易日开盘前的决策时点。

| 数据域 | Snapshot 文件 | 默认准备窗口 | 2021-10-08 示例 | 可见边界 |
|---|---|---:|---|---|
| `daily` | `daily.parquet` | 最近 21 个月 | 2020-01 到 2021-09 的日频数据 | 不包含 2021-10 之后行情 |
| `intraday_1min` | `intraday_1min.parquet` | 最近 5 个交易日 | 2021-09 最后 5 个交易日的 1 分钟线 | 开盘前决策不含 2021-10-08 分钟线；盘中决策按 bar close 截到 `decision_time` |
| `fundamentals` | `fundamentals.parquet` | 最近 21 个月可见披露 | 已公告财报、分红、业绩预告/快报 | 保留公告日、报告期和多版本可见时间 |
| `events` | `events.parquet` | 最近 21 个月 | 截至 2021-10-08 09:25 已可见的资金、两融、股东、回购、解禁、大宗交易、龙虎榜 | T+1 数据按实际可见时间过滤 |
| `macro` | `macro.parquet` | 最近 21 个月 | 已发布宏观、政策、利率、全球事件和跨市场背景 | 不使用未来发布值 |
| `text` | `text_index.parquet`、`text_library/` | 最近 21 个月 | 可见文本索引和 as-of 文本库 | 正文检索必须引用文本 ID |

窗口数据可以比配置短，例如刚上市股票不足 21 个月历史，或研究数据保留下限晚于完整窗口起点。Environment 必须在 manifest 中记录实际覆盖。21 个月是默认最大可见窗口。

数据域拼接方式：

| 数据域 | 主要来源 | 拼接方式 | 输出边界 |
|---|---|---|---|
| `daily` | 日线、每日指标、复权因子和交易日历 | 按 `ts_code, trade_date` 对齐，统一金额、成交量、收益和比例单位 | 日频行情和横向排序输入；成交判断由模拟 Broker 使用环境数据完成 |
| `intraday_1min` | 1 分钟线和交易日历 | 按 `ts_code, trade_time` 对齐，使用 bar close 时间做可见过滤 | 日内研究输入；开盘前不含当日分钟线 |
| `fundamentals` | 财报、财务指标、分红、业绩预告/快报、披露计划和主营构成 | 按公告日、实际披露日、报告期和版本字段筛出决策时点可见版本 | 财务和经营质量窗口；保留多版本可追溯字段 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等 | 按事件日和 `available_at` 过滤，T+1 或盘后数据使用保守可见时间 | 事件和资金状态窗口 |
| `macro` | 宏观、政策、利率、全球事件和跨市场数据 | 按发布时间或保守可见时间过滤，并按序列/地区/频率整理 | 市场背景窗口 |
| `text` | 公告、新闻、研报、政策文本 | 先生成可检索索引，再把 as-of 正文或片段放入 `text_library/` | 供自然语言步骤检索，必须引用文本 ID |

字段来源、可见时间和单位规则由对应 `manifest.json` 记录。

### 2.3 单位合同

进入 Sandbox snapshot 的数值字段必须使用标准单位：

| 类型 | 标准单位 |
|---|---|
| 金额 | 元 |
| 成交量/股本 | 股 |
| 比例、收益、换手 | 小数，例如 5% 记为 `0.05` |
| 利率和费率 | 优先小数；确需 bps 时字段名必须带 `_bps` |

原始单位、转换规则和转换前字段必须写入 `manifest.json`。单位不明的字段不能进入模型可见数据；订单计划校验也必须拒绝依赖单位不明字段生成的交易意图。

特殊口径修正也属于 Environment 合同，不改写 raw 数据，只生成派生字段并写入 manifest。当前必须处理深圳 09:30 分钟条和开盘竞价接口的历史口径差异：

| 场景 | 派生字段规则 | 适用边界 |
|---|---|---|
| 历史 09:30 分钟条被用作实盘 `stk_auction` 近似特征 | 对 09:30 的深圳股票生成校正后的成交量/成交额字段：`00*.SZ` 乘 `0.76`，`30*.SZ` 乘 `0.58`；沪市、北交所和其他时点保持 `1.0` | 只用于开盘竞价近似特征；raw 分钟线、日内成交汇总、15:00 收盘竞价不改写 |

manifest 至少记录校正规则 ID、适用字段、倍率、适用市场/代码前缀和生成时间。策略代码应读取校正后的派生字段；如果派生字段不存在，不能静默退回未校正字段来模拟开盘竞价。

### 2.4 Snapshot 数据路径

先区分两个概念：

- `/mnt/snapshots/<stage>`：Agent 可见或回放用的数据槽。`train` 是训练/探索输入，`valid` 是验证回放数据区间，`test` 是测试类回放区间。
- `/mnt/snapshot`：`backtest_tool` 正式执行时绑定的当前决策输入视图。它只包含本次决策时点前已可见的数据，不作为 Agent Shell 的常规读取入口。

一个 Fold 的 Agent-facing Sandbox 内保留训练和验证数据槽；测试槽只给 Runner/root 和 `backtest_tool`：

```text
/mnt/snapshots/
  train/
  valid/
  test/
```

`train/` 是 Agent 的训练/探索输入。`valid/` 是验证回放数据区间，Agent 用户可读，用于复盘验证期可见的行情、事件和文本。验证回测产生的收益、成交、拒单和自然语言评分输出写入 `/mnt/artifacts/results/valid_<idx>/`，Agent 在训练/验证期只读这些结果。`test/` 对 Agent 用户不可读，只能由 Runner/root 在冻结回放阶段读取。Held-out 不需要单独目录；Pipeline 执行最终评估时，把 held-out 对应的数据作为测试类回放数据放入 `test/`，并用 ledger 标签区分。

正式策略代码不直接选择 `train`、`valid` 或 `test`。Runner 在调用 `backtest_tool` 前提供一个只读的当前决策输入视图：

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
  universe.parquet
```

验证回测时，`/mnt/snapshot` 包含验证决策日前可见的历史窗口，不包含验证期未来行情。冻结测试或 held-out 回放时，`/mnt/snapshot` 包含对应测试决策日前可见的历史窗口。这个视图由 Runner/root 管理，只供 `backtest_tool` 和它调用的 `generate_candidates()` 使用，Agent Shell 不依赖它做探索。`text_library/` 是 as-of 文本库目录，按数据集存储正文分片（`<dataset>.parquet`，`text_id -> body`），必须只读挂载到 Sandbox；正文或正文片段必须由 `text_index.parquet` 引用。`universe.parquet` 是按决策日在市的股票名录（上市日 ≤ 决策日 < 退市日，含此后才退市的股票），避免用当前上市名单引入幸存者偏差。

`valid/` 和 `test/` 回放数据槽除日频回放行情外，还包含该回放区间内发布的事件、文本（索引+文本库）和分钟线，供回放和验证期复盘使用；是否包含分钟线可按配置关闭。

实现上，Runner 在宿主侧保留多个决策输入视图，但 Docker 只挂载当前视图镜像。切换验证/测试时，Runner 把对应视图硬链接或复制到 `runtime/current_snapshot/`，容器内只读挂载为 `/mnt/snapshot`。`runtime/snapshot_views/` 不挂载给 Agent。

`valid_decision_input` 和 `test_decision_input` 是 Runner 按决策时点生成的可见输入视图，不能包含验证或测试期未来行情、成交和收益。如果 `valid` 或 `test` 目录包含完整回放数据，Runner 不应把它们直接作为正式 `generate_candidates()` 的运行输入。`/mnt/snapshot` 对 Agent 只读，Agent 用户不能删除、覆盖或切换该视图。

示例：某 Fold 的时间切分为训练输入 2020-01 到 2021-09，验证回放 2021-10 到 2021-12，测试回放 2022-01 到 2022-03。Sandbox 可以挂载该 Fold 所需的全部数据，但必须按用途拆开：

```text
/mnt/snapshots/
  train/   2020-01 到 2021-09
  valid/   2021-10 到 2021-12
  test/    2022-01 到 2022-03

宿主 runtime/snapshot_views/
  valid_decision_input/  2020-01 到 2021-09
  test_decision_input/   2020-04 到 2021-12

宿主 runtime/current_snapshot/  Runner 当前镜像；容器内挂载为 /mnt/snapshot
```

验证阶段：

```text
/mnt/snapshot  当前镜像 valid_decision_input
backtest_tool 调用 generate_candidates()，只读 /mnt/snapshot
Agent 可读 /mnt/snapshots/train 做训练探索
backtest_tool 读取 /mnt/snapshots/valid 做回放
Agent 可读 /mnt/snapshots/valid 复盘验证回放数据
```

测试阶段：

```text
/mnt/snapshot  当前镜像 test_decision_input
backtest_tool 调用 generate_candidates()，只读 /mnt/snapshot
backtest_tool 读取 /mnt/snapshots/test 做回放
Agent 不可读 /mnt/snapshots/test
```

### 2.5 PIT 特征层产物

Environment 维护两个夜间构建的 PIT 特征层产物（任务 `cn_nightly_feature_build`，调度见 `docs/data_documentation.md` 第 3.3 节）。它们是 raw 数据和 snapshot 数据域之间的中间层：`fundamentals` 域基于 `fundamental_events` 选择可见版本，`daily` 域可复用 `daily_alpha` 的对齐和单位处理。

| 产物 | 路径 | 内容 |
|---|---|---|
| `fundamental_events` | `data/features/fundamental_events/<dataset>/available_month=<YYYYMM>.parquet` | 财务、分红、审计、主营构成和披露计划的多版本事件层；每行带 `available_at`、`available_at_rule`、`business_key`、`source_path`、`source_hash`、`source_row_id` |
| `daily_alpha` | `data/features/daily_alpha/feature_date=<YYYYMMDD>.parquet` | 次一交易日可交易的日频 PIT 特征：行情、收益/波动、估值、流动性、停牌和涨跌停状态，以及决策时点可见的财务/分红衍生列 |

构造规则：

- `fundamental_events.available_at` 取公告日 18:00：财报优先 `f_ann_date` 其次 `ann_date`；分红只用 `imp_ann_date/ann_date`，缺失则标记不可见并排除；审计/主营业务缺公告日时回退到同报告期财报的可见时间。
- `daily_alpha` 行的 `available_at` 是来源交易日收盘后的 `daily_basic` 可见时间，`tradable_date` 是下一本地交易日；join 财务/分红事件时只允许 `available_at <= 决策时点` 的版本。
- 收益和波动特征使用 `daily.pct_chg`，不使用当前快照 `adj_factor`，避免后续除权回写产生非 PIT 输入。
- `limit_list_d` 只进入稳定的 `limit` 状态列；`limit_amount`、`fd_amount`、`first_time` 等不稳定日终字段保留在 raw，不进入特征。
- 构建与审计入口是 `scripts/data/build_features.py` 的 `build-fundamental-events`、`audit-fundamental-events`（输出 `results/data_quality/fundamental_events_status.json`，`error` 时阻断后续构建）和 `build-features`。

## 3. Sandbox

### 3.1 运行环境

正式实验默认使用本地 Docker。每个 Fold run 启动一个常驻容器（镜像 `macroquant-sandbox`，定义在 `ops/docker/sandbox.Dockerfile`）。Agent 侧执行（Shell 命令、`generate_candidates()` 子进程）经 `docker exec --user agent` 进入容器；可信服务（NL 评分、模拟 Broker、manifest/trace 写入）保留在宿主侧。冻结测试在 Agent 停止、写入锁定后复用同一容器。容器内 `/mnt/snapshot` 是只读挂载的当前决策输入镜像，其他决策输入视图不挂载给 Agent。正式 `generate_candidates()` 运行期间，Environment 会临时隐藏 `/mnt/snapshots/train|valid|test` 槽，使非 root Agent 用户只能依赖当前 `/mnt/snapshot` 决策输入视图。CLI 只有显式传 `--local-dev` 时才使用本地执行器，该模式只用于开发和单元测试，不作为正式实验安全边界。

宿主无 root 权限时使用 rootless Docker（`dockerd-rootless-setuptool.sh install`）。容器内 `agent` 用户映射为宿主 subuid，因此 Agent 可写目录（`workspace/`、`agent_output/` 可写文件）在宿主侧以全局可写权限放行；只读与不可见约束仍由只读挂载、`0444` 只读文件和 `0700` 测试槽承担。

Sandbox 要求：

| 项目 | 要求 |
|---|---|
| Python | Sandbox 镜像内使用 Python 3.11 和固定版本依赖（pandas/numpy/pyarrow/duckdb/scikit-learn/statsmodels/PyTorch），不在运行中安装新包；支持机器学习与神经网络方法 |
| 命令行检索 | 镜像内预装 `ripgrep`（命令 `rg`）和基础 Unix 工具，供 Agent 在可读目录内快速检索代码、文本索引和日志 |
| GPU | 每容器默认动态分配 1 块当前显存剩余最多的 L20（`SandboxSpec.gpu="auto"`、`gpu_count=1`）；可把 `gpu_count` 调大以分配多块 L20，也可固定编号/编号列表或设为 `None` 关闭；实际分配结果写入 run manifest 的 `sandbox_runtime` |
| 用户 | Agent 进程使用非 root 用户 `agent`；Runner/root 只用于冻结执行、链接绑定和权限管理 |
| 网络 | `--network none`；Sandbox 内 Python 和 Shell 不联网；LLM 调用只能经宿主侧 LLM Proxy 发起 |
| 训练输入数据 | `/mnt/snapshots/train` 只读，Agent 用户可读；只包含验证决策时点前可见数据 |
| 验证回放数据 | `/mnt/snapshots/valid` 只读，Agent 用户可读；`backtest_tool` 读取它做验证回放，并把结果写入 `results/valid_<idx>/` |
| 测试回放数据 | `/mnt/snapshots/test` 对 Agent 用户不可读（宿主目录 `0700`，容器内非 root 无权限）；冻结测试或 held-out 回放时由 Runner/root 和 `backtest_tool` 读取 |
| 正式回测输入 | `/mnt/snapshot` 由 Runner/root 在 `backtest_tool` 调用前刷新为当前决策输入镜像；正式策略入口只读该视图，候选生成期间 `train/valid/test` 槽对 Agent 用户隐藏 |
| Agent 输出 | `/mnt/agent` 由 Environment 创建；Agent 可写 `workspace/`，并按约定更新 `agent_output/` 下的正式产物文件 |
| 可信产物 | `/mnt/artifacts` 只读挂载给 Agent；`run_manifest`、`agent_trace`、`parent_output`、`results`、`steps` 和 `logs` 只由 Runner/Tool 写入 |
| 只读文件强制 | README、`parent_output/` 以只读权限/只读挂载强制；`finish_fold` 或冻结阶段后 `agent_output/` 整体锁为只读 |
| 运行时长 | 由 Pipeline 下发 `fold_deadline_at`，每个 Fold 默认 30 分钟；Step 不设单独时长限制 |
| 资源护栏 | 单容器默认限制为宿主 CPU/内存的约 10%（`SandboxSpec.from_host_fraction`，可调），并限制进程数和输出大小 |

### 3.2 运行产物路径

运行时拆成两个根目录：`/mnt/artifacts` 是可信产物与审计目录，Agent 只读；`/mnt/agent` 是 Agent 工作目录，只允许放临时代码和正式策略产物。两者都由 Environment 创建，不得挂载历史产物或主仓库。

```text
/mnt/artifacts/
  run_manifest.json
  agent_trace.jsonl
  parent_output/
    factor/
    nl_prior/
  results/
    valid_000/
      detailed_return.json
      order_plan.parquet
      nl_output/
  steps/
    tree.json
    <node_id>/
      factor/
      nl_prior/
  logs/

/mnt/agent/
  workspace/
  agent_output/
    factor/
      README.md
      main.py
      factors.json
    nl_prior/
      README.md
      prior.json
```

`parent_output/` 是当前 Fold 的父策略产物副本，只读可见；`agent_output/` 是 Agent 修改后的当前正式工作副本。Agent 可以读取 `parent_output/` 来理解上一 Fold 或上一轮接受产物的原始因子和投资先验，但不能修改或覆盖它。Environment 每次检查前必须校验 `parent_output/` hash 与 run manifest 中记录的 `parent_strategy_artifact_hash` 一致；不一致时直接失败。

子目录归属：

| 目录 | 写入方 | Agent 可读 | 说明 |
|---|---|---|---|
| `parent_output/factor/` | Environment / Runner | 是 | 父策略因子逻辑，只读 diff 基准 |
| `parent_output/nl_prior/` | Environment / Runner | 是 | 父策略自然语言投资先验，只读 diff 基准 |
| `/mnt/agent/workspace/` | Agent | 是 | 临时代码、数据探查脚本、调试输出和草稿；不冻结为策略产物 |
| `/mnt/agent/agent_output/factor/` | Agent | 是 | 正式因子逻辑、入口函数和登记表；`README.md` 只读，Agent 可写 `main.py` 和 `factors.json` |
| `/mnt/agent/agent_output/nl_prior/` | Agent | 是 | 正式自然语言投资先验；`README.md` 只读，Agent 可写 `prior.json`；自然语言评分步骤不能直接写入 |
| `results/<phase>_<idx>/` | `backtest_tool` | 训练/验证期可读；测试和 held-out 不反馈给 Agent | 单次回测调用结果目录，例如 `valid_000/`、`valid_001/`、`test_000/` |
| `steps/` | `backtest_tool` / Pipeline | 只读 | Step 产物树（开关 `step_tree_enabled`）：每次成功验证回测把当前 `factor/`+`nl_prior/` 快照为节点（含父指针、指标、hash），`tree.json` 的 `current_node_id` 标记当前工作副本起点；树跨 Fold 累积并由 Pipeline 传递 |
| `run_manifest.json` | Environment / Runner | 默认不作为 Agent 输入 | 本 Fold 的输入、配置、deadline、最近修改检查、回测摘要、结束状态和关键产物版本 |
| `agent_trace.jsonl` | Environment / Runner / LLM Proxy / 模拟 Broker | 训练/验证期只读；测试和 held-out 不反馈 | 同一 Agent 会话下的 Shell、Tool、回测、Broker、Agent 主对话 LLM 和批量自然语言评分摘要事件；不记录 API key 或 Authorization header |

Fold 起点策略产物由 Pipeline 从 `experiments/<experiment_id>/strategy_artifacts/<epoch_id>/<strategy_artifact_id>/` 同时复制到 `/mnt/artifacts/parent_output/` 和 `/mnt/agent/agent_output/`。如果是第一次创建策略产物，Environment 从 `configs/agent_output_template/` 初始化 `agent_output/`；此时使用初始化约束，`parent_output/` 可以为空并在 manifest 中标记 `is_initial_artifact=true`。`README.md` 文件由 Environment 设置为 Agent 只读；`/mnt/artifacts` 的顶层目录由 Environment 创建和维护，Agent 不能新增、删除或重命名顶层目录。

训练/验证时，Agent 应先在 `workspace/` 中写临时代码和调试脚本；确认可运行后，再把最终因子代码和投资先验写入 `agent_output/factor/` 和 `agent_output/nl_prior/` 的约定文件。`workspace/` 不冻结、不回放、不参与策略产物 diff；只有 `agent_output/factor/` 和 `agent_output/nl_prior/` 可能冻结为策略产物。

正式策略产物只允许普通文件和目录；符号链接、设备文件或其他特殊路径直接拒绝。冻结前的 hash、diff 和格式校验都基于这条规则。

Environment 负责执行策略修改约束检查。检查结果直接返回给 Agent，并追加到 `agent_trace.jsonl`；`run_manifest.json` 只保留最近一次检查摘要和是否允许回测。默认不再单独保存 diff 文件。

`results/` 是回测结果根目录。每次 `backtest_tool` 调用都创建一个新的子目录，命名为 `<phase>_<idx>`，例如 `valid_000`、`valid_001`、`test_000` 或 `heldout_000`。该子目录由 `backtest_tool` 独占写入，只保存收益明细、订单计划和自然语言评分等大块产物；调用状态、核心指标、错误和产物路径写入 `run_manifest.json` 的回测摘要，并追加到 `agent_trace.jsonl`。Agent 在训练/验证期只读这些结果，在测试和 held-out 阶段不接收这些结果。

测试和 held-out 必须使用冻结内容，运行后校验冻结产物没有变化。每次 run 结束后，Environment/Pipeline 将该次 `/mnt/artifacts` 和 `/mnt/agent` 的必要内容收集到宿主机实验目录，例如 `experiments/<experiment_id>/artifacts/<run_id>/`，并写入 run manifest。宿主收集目录保持 `run_manifest.json`、`agent_trace.jsonl`、`parent_output/`、`agent_output/`、`workspace/`、`results/`、`steps/` 的平铺结构，便于既有报告读取；Sandbox 内路径只是运行时挂载点，不是长期数据目录。

### 3.3 Python 环境

外层本机脚本、测试、cron 和非容器工具使用 conda 环境 `~/miniconda3/envs/quant`（Python 3.11）。Docker Sandbox 的 Python 环境与外层 conda 无直接关系，由 `ops/docker/sandbox.Dockerfile` 独立构建；修改 Sandbox 依赖后需要重建镜像。

Python 环境要支持：

- pandas / numpy / pyarrow / duckdb。
- scikit-learn / statsmodels 等常用研究包。
- 项目内部工具包。
- 本地文本检索包。

Agent 可以把临时 Python 文件写到 `/mnt/agent/workspace/`。正式策略代码确认后写入 `/mnt/agent/agent_output/factor/main.py`；自然语言投资逻辑写入 `/mnt/agent/agent_output/nl_prior/prior.json`；不能修改主仓库文件、`/mnt/artifacts` 或只读说明文件。

### 3.4 Agent Runner

Agent Runner 是 Sandbox 内的执行框架。可以借鉴 LangGraph 等开源 Agent 框架的思路，但不要求直接依赖某个框架。

最小能力：

| 能力 | 说明 |
|---|---|
| 运行记录 | 把当前 Fold 的输入、输出、Step 序列、Shell/Tool 调用、校验、回测和错误写入 `run_manifest.json` 与 `agent_trace.jsonl`；事件级日志带 `step_id` |
| 入口注册 | 只允许使用白名单执行入口和可信服务 Tool |
| 检查点 | Step 中断后可复核，不自动跳过失败 |
| LLM 日志 | Agent 主对话写 `agent_trace.jsonl`；批量自然语言评分明细写入对应回测结果目录的 `nl_output/nl_llm_calls.jsonl` |
| Deadline 控制 | 只执行 Fold 级 deadline；Step 不单独计时；T-5 分钟触发固定收尾提示，Fold 到点截断 |

Runner 不提供宿主机 shell。Agent 在 Sandbox 内通过 `sandbox_shell_tool` 读文件、写代码、运行 Python 和调试。它是带日志、超时和挂载限制的非 root shell，不是宿主机登录 shell；详细合同见第 4.2 节。

Runner 必须在当前 Fold 内为每次 Shell、可信服务 Tool、校验和回测调用自动记录请求摘要、响应摘要、`step_id`、exit code、stdout/stderr、产物路径和错误。Agent 不需要自行写可信运行日志。

## 4. 执行入口和可信服务 Tool

本章定义 Environment 提供的 Tool 和它们的硬接口。`backtest_tool` 是最复杂的 Tool，因此本章直接记录它如何调用策略主函数、自然语言评分、订单生成和回测。

### 4.1 Agent 可用入口和 Tool

| 入口 / Tool | 作用 | 训练/验证/元学习 | 测试/Held-out |
|---|---|---|---|
| `sandbox_shell_tool` | 读允许的 snapshot、写 `workspace/`、运行 Python，并按约定更新 `agent_output/factor/` 和 `agent_output/nl_prior/` | 可运行；元学习只能读 development history 和父产物，不能读 held-out | 默认关闭；如需故障复核，只读运行 |
| `web_search_tool` | Host 侧联网检索，用于元学习发散搜索理论、跨领域方法和哲学概念 | 只在元学习 Fold 开放；可选 Tavily 通用搜索或 Semantic Scholar 学术论文搜索，key 留在宿主机 | 关闭 |
| `modification_check_tool` | 检查正式策略产物修改量，返回是否允许继续，并写入 `agent_trace.jsonl` 和 `run_manifest.json` 最近检查摘要 | 可运行；元学习若修改产物也必须通过 | 只读检查父产物副本和当前正式产物 |
| `backtest_tool` | 加载正式策略产物，调用策略主函数，执行自然语言评分、订单生成、交易约束校验和模拟回测；运行前必须通过 `modification_check_tool` | Fold Agent 只能请求验证模式；元学习不能调用正式回测产生收益结果，只允许 Runner/root 在结束后执行只读合同校验 | 由 Runner/root 冻结执行，不反馈给 Agent |
| `finish_fold_tool` | 无参数结束当前 Fold；结束前触发一次轻量 `backtest_tool` 合同校验，通过后更新 `run_manifest.json` 并锁定写入 | 可运行 | 不可运行 |

所有入口的路径、决策时间、Fold 信息、可写目录和运行配置必须来自 run manifest。入口必须拒绝 Agent 自行传入的绝对路径、未来时间、外部网络或权限边界之外的文件。

`web_search_tool` 是例外的联网入口，但只服务于 Epoch 前元学习。它由 Runner 在宿主机调用 provider，Sandbox 内 Agent 不接触 API key，也不能在普通 Fold 中调用该动作。当前支持 Tavily 和 Semantic Scholar；请求和结果摘要写入 `agent_trace.jsonl`，用于审计 Taste 的来源。

### 4.2 `sandbox_shell_tool`

`sandbox_shell_tool` 是 Agent 在 Sandbox 内探索数据和修改策略产物的受控 Shell。它不等于普通用户登录 shell，也不等于宿主机 shell：命令只能在容器内以非 root 用户运行，路径、网络、资源、输出和日志都由 Environment 管理。

Agent 可以用它做三类事情：

| 用途 | 允许内容 |
|---|---|
| 查看数据和产物 | `ls`、`cat`、`rg`、`sed` 查看可读 snapshot、父产物、当前产物、Step 产物树和历史验证结果 |
| 调试代码 | 在 `/mnt/agent/workspace/` 写临时代码、运行 Python、查看报错和中间统计 |
| 更新正式产物 | 按约定修改 `/mnt/agent/agent_output/factor/` 和 `/mnt/agent/agent_output/nl_prior/` 中的可写文件 |

权限边界：

| 项目 | 要求 |
|---|---|
| 身份 | 容器内非 root 用户，例如 `agent`；无 sudo |
| 可读路径 | `/mnt/snapshots/train`、`/mnt/snapshots/valid`、`/mnt/artifacts/parent_output`、`/mnt/artifacts/results`、`/mnt/artifacts/steps`、当前 `/mnt/agent/agent_output` |
| 可写路径 | `/mnt/agent/workspace`；以及 `/mnt/agent/agent_output/factor`、`/mnt/agent/agent_output/nl_prior` 中约定允许 Agent 修改的正式产物文件 |
| 只读文件 | `/mnt/artifacts` 整体只读，`agent_output` 下的模板 `README.md` 只读 |
| 只读树 | `/mnt/artifacts/steps` 只能读取；新增节点和附件只能由正式验证回测与 Pipeline 自动写入 |
| 不可见路径 | `/mnt/snapshots/test`、`/mnt/runtime`、宿主机路径、Docker socket、未挂载数据目录和外部网络 |
| 网络 | `--network none`；不能直接请求 LLM provider，LLM 调用只能经宿主侧 LLM Proxy |
| 系统 | 不能改变权限边界，不能启动长期后台服务，不能安装系统级依赖 |
| 资源 | CPU、内存、进程数、输出大小和运行时长受限 |
| 日志 | 每次命令记录调用 ID、命令、exit code、stdout/stderr、相关产物路径和错误，并写入 `agent_trace.jsonl` |

常用命令边界：

| 命令 | 边界 |
|---|---|
| `rg` | 搜索 Agent 可读数据和产物；不得扫不可见测试区间或宿主机路径 |
| `sed` | 查看或小范围文本处理；不建议用复杂脚本批量重写正式产物 |
| `python` | 可运行探索脚本和策略调试；不能联网，不能把临时回测结果冒充正式验证结果 |
| `apply_patch` | 只允许修改 `/mnt/agent/workspace/` 草稿和 `/mnt/agent/agent_output/` 下约定可写文件；不能写 `/mnt/artifacts`、manifest、日志或只读 README |

Shell 输出只是 Agent observation，不能覆盖系统提示词、权限、PIT 时间墙、修改约束或冻结规则。Agent 通过 Shell 改了正式产物后，仍必须通过 `modification_check_tool`；Shell 里的临时统计、临时回测和解释文本都不能替代 `backtest_tool` 的正式验证结果。

测试和 held-out 阶段默认关闭 `sandbox_shell_tool`。若需要故障复核，只允许 Runner/root 以只读方式执行，并且输出只能作为审计事件或受控附件保存，不能反馈给 Agent 或进入策略产物。

### 4.3 `modification_check_tool`

`modification_check_tool` 是正式回测前的确定性门禁。它只检查 `agent_output/factor/` 和 `agent_output/nl_prior/`，不检查 `workspace/`、`results/` 或 Agent 的自然语言声明。

| 检查项 | 要求 |
|---|---|
| 调用参数 | Agent 调用时不传业务参数，只触发“检查当前正式工作副本” |
| 上下文 | 父产物 ID/hash、修改约束、是否初始产物、Fold 信息和路径都来自 run manifest |
| 父产物基准 | 非初始产物必须使用 `/mnt/artifacts/parent_output/` 作为只读 diff 基准，并在检查前校验其 hash；不能用 Agent 当前目录反推出父产物 |
| `factor/factors.json` | 顶层为 `{"factors": [...]}`；每条因子包含 `id`、`function`、`description`、`lookback_days`、`direction`、`rationale`（引入理由，提交前必须非空）；不用的因子应删除；登记因子的函数应能在 `main.py` 找到 |
| `nl_prior/prior.json` | 顶层为 `{"rules": [...]}`；每条规则包含 `id`、`text`、`evidence`、`effect`；不用的规则应删除 |
| 计数 | 使用文件数、diff 行数、登记因子变化、自然语言规则变化和单条文本长度等可复核计数 |
| 输出 | `allowed_to_backtest`、修改摘要、错误原因；同一结果写入 `agent_trace.jsonl`，并更新 `run_manifest.json` 的最近修改检查摘要 |

若 `allowed_to_backtest=false`，Environment 直接拒绝正式 `backtest_tool`。Agent 可以缩小正式产物改动后再次检查。元学习场景下，该结果表示是否允许冻结被清理后的策略产物，而不是是否允许进入回测搜索。具体阈值和接受规则由 Pipeline 下发和记录。

### 4.4 `backtest_tool`

`backtest_tool` 是唯一正式回测入口。Agent 自己写的临时 Python 回测只能用于调试，不能作为验证、测试或 held-out 结果。

`backtest_tool` 不负责构造 PIT 数据，也不做原始数据时间过滤。它只消费已经由 Environment 构造好的 snapshot，并检查 snapshot manifest 是否与本次 run manifest 对齐。验证区间、买入日、卖出日和固定买卖规则由 run manifest 指定，不传给策略主函数。

**回测模式切换**

`generate_candidates()` 不区分模式。Runner 调用 `backtest_tool` 时只选择两类模式。`/mnt/snapshot` 始终是本次决策前的输入视图；`valid/test` 是 `backtest_tool` 内部读取的回放区间。

| 模式 | 策略输入 | 回放区间 | 结果目录 | Agent 可见性 |
|---|---|---|---|---|
| `valid` | `/mnt/snapshot`，验证决策日前可见数据 | `/mnt/snapshots/valid` | `results/valid_<idx>/` | 结果对 Agent 只读可见 |
| `frozen_eval` | `/mnt/snapshot`，测试决策日前可见数据 | `/mnt/snapshots/test` | `results/test_<idx>/` 或 `results/heldout_<idx>/` | Agent 已停止，不反馈结果 |

`test` 和 `heldout` 共用 `frozen_eval` 模式；区别只是 Pipeline 放入 `/mnt/snapshots/test` 的回放区间、输出目录和 ledger 标签不同。

正式候选生成不是普通 Shell 探索。`backtest_tool` 调用 `generate_candidates()` 前，Environment 必须把 `MQ_SNAPSHOT_DIR` 指向当前 `/mnt/snapshot`，并让非 root Agent 用户无法遍历 `/mnt/snapshots/train`、`/mnt/snapshots/valid` 和 `/mnt/snapshots/test`。这样即使策略代码动态拼接路径，也无法读取训练/验证/测试槽作为正式输入。静态代码检查只能作为辅助提示，不能替代运行时文件系统隔离。

**因子和自然语言开关**

| 阶段 | 因子入口 | 自然语言评分 |
|---|---|---|
| 验证调试 | 默认开启；关闭时只允许做合同检查或链路压测，不写正式回测结果 | 可设为 `off`、`sample` 或 `on`，用于快速比较因子和控制 API 成本 |
| 正式验证 | 必须开启 | 建议开启；若关闭或抽样，结果必须在 `run_manifest.json` 的回测摘要中标记为非完整验证 |
| 测试/Held-out | 必须开启 | 必须开启 |

`nl=off` 时，`backtest_tool` 只使用归一化后的因子分生成验证结果；`nl=sample` 只对配置的候选样本调用自然语言评分，未抽样候选使用已抽样 `nl_score` 的平均值参与同一合成公式（`nl_scored=false` 供审计），结果标记为抽样验证；`nl=on` 执行完整自然语言评分。测试和 held-out 不允许关闭或抽样自然语言评分。

Runner 执行动作：

1. 容器启动前，把宿主机的 `train`、`valid`、`test` 数据目录只读映射到 `/mnt/snapshots/`；`train` 和 `valid` 给 Agent 读，`test` 只给 Runner/root 和 `backtest_tool` 读。
2. 从 Pipeline 的 Fold 配置读取本次模式、回放阶段、结果目录名和自然语言评分开关。
3. 在正式 `backtest_tool` 调用前刷新当前 `/mnt/snapshot` 决策输入镜像；该视图只包含本次决策前可见数据，其他决策输入视图不进入容器。
4. 把当前工作副本或冻结策略产物放到 `/mnt/agent/agent_output`。
5. 调用 `backtest_tool(mode="valid")` 或 `backtest_tool(mode="frozen_eval")`；正式执行环境必须把 `MQ_SNAPSHOT_DIR` 固定为 `/mnt/snapshot` 或清空到默认值。
6. 校验 `/mnt/snapshot/manifest.json` 与 Pipeline 记录的 snapshot ID/hash 一致。

因此，同一份正式策略代码始终在 `backtest_tool` 中读取 `/mnt/snapshot`；Agent 可以读取 `/mnt/snapshots/train` 做探索、读取 `/mnt/snapshots/valid` 做验证复盘，但不能通过函数参数选择 valid/test，也不能读取 `/mnt/snapshots/test`。

`valid` 可以在 Agent 活跃的 Sandbox 中执行，验证回放数据也可以被 Agent 用于复盘和下一 Step 修改。`frozen_eval` 在同一个 Fold Docker 中执行，但必须发生在 Agent 停止、写入锁定、测试数据仍对 Agent 用户不可读之后。

执行顺序：

1. 强制运行 `modification_check_tool`，或复用与当前正式产物 hash 完全一致的最近一次检查结果；产物在检查后有任何改动都必须重查；未通过则拒绝回测。
2. 加载 `agent_output/factor/` 和 `agent_output/nl_prior/`。
3. 调用 `generate_candidates()`，得到 Agent 筛选后的候选股票和 `factor_score`。
4. 校验候选池 schema、重复股票、非法股票和明显未来路径，并按 `abs(factor_score)` 截断到 `max_candidates`（默认 10）后才进入自然语言评分；截断数量写入回测摘要。Environment 不替 Agent 做策略筛选；截断和 Top 10 规则会写入 Agent 的系统提示词。
5. 按本次 `nl` 开关对候选池执行自然语言评分或跳过/抽样评分，输出 `nl_output/` 或标记跳过原因。
6. 归一化因子分，合成 `final_score`。
7. 根据 `final_score`、仓位规则、风险标签和券源近似生成订单计划。
8. 校验订单计划是否满足股票池、停牌、涨跌停、现金、权重、T+1 和 PIT 约束。
9. 调用模拟 Broker 回放成交、拒单、持仓、成本和收益。

**策略主函数子合同**

`backtest_tool` 只调用 `agent_output/factor/main.py` 中的固定主函数：

```python
def generate_candidates() -> "pandas.DataFrame":
    ...
```

当前基础流程是“决策交易日买入，周期结束卖出”，策略主函数只负责在固定 Sandbox 路径中读取可见数据、计算因子、排序并输出有限候选股票和因子分。`backtest_tool` 不向它传入参数。

固定可读路径：

| 路径 | 含义 |
|---|---|
| `/mnt/snapshot/` | `backtest_tool` 正式调用时绑定的只读 PIT 数据窗口 |
| `/mnt/snapshot/manifest.json` | Environment 已校验的正式回测输入元信息 |
| `/mnt/agent/agent_output/nl_prior/` | 当前策略产物中的自然语言投资先验 |

`backtest_tool` 自己校验 `/mnt/snapshot/manifest.json` 与 run manifest 是否一致。策略代码如需查看数据覆盖范围，应直接读取这份只读 manifest。Agent 可以在 Shell 中读取 `/mnt/snapshots/train` 做探索、读取 `/mnt/snapshots/valid` 做复盘，但正式 `generate_candidates()` 不得把 `/mnt/snapshots/train`、`/mnt/snapshots/valid` 或 `/mnt/snapshots/test` 作为运行输入；`modification_check_tool` 和 `backtest_tool` 应拒绝明显直接引用这些阶段目录的正式策略代码。

主函数返回 `pandas.DataFrame`，表示 Agent 筛选后的候选股票和因子分，不写独立中间文件。必需列：

| 列 | 要求 |
|---|---|
| `ts_code` | 股票代码 |
| `factor_score` | 因子逻辑输出的原始方向分，不包含自然语言分；正分偏多，负分偏空或回避 |
| `reason` | 简短理由 |
| `source_artifacts` | 使用的数据或规则来源 ID，可为空列表但字段必须存在 |

`backtest_tool` 先完成自然语言评分和分数合成，再按 run manifest 中的总分阈值、持仓上限和券源近似生成交易方向。做多候选按 `final_score` 从高到低排序；做空候选按负分强度排序。若做空候选不在当前可做空股票池中，订单计划阶段会跳过它并顺延到下一个满足阈值且可做空的候选，而不是占用持仓名额后直接拒单。超过持仓上限时按 `abs(final_score)` 在可执行候选中保留 Top N。权重分配规则：总敞口目标为 `选中数量 / max_total_holdings`（最大 1.0），按 `abs(final_score)` 比例分配到各标的，单标的不超过 `max_single_name_weight`，超出部分不重新分配。阈值和上限默认值见第 5.3 节回放 profile。被过滤候选只作为过程记录，不进入订单计划。模拟 Broker 仍保留券源、现金、停牌、涨跌停和风控拒单兜底。

**自然语言评分内部流程**

自然语言评分是 `backtest_tool` 的内部步骤，不作为 Agent 单独可调用 Tool。它负责公司上下文、文本检索、LLM Proxy 调用、JSON 解析、evidence 引用校验和日志记录。

`company_context` 用来回答“这家公司在决策时点已知是做什么的”。可用来源：

| 来源 | 用途 |
|---|---|
| 历史名称和基础股票信息 | 识别股票、交易所、证券简称和名称变化 |
| 行业成分 | 提供行业背景和可比对象 |
| `fina_mainbz_vip` | 提供主营业务构成和收入来源 |
| as-of 文本库 | 补充公告、研报、新闻和政策文本中的可见描述 |

缺少 PIT 发布时间的当前公司简介不能用于历史回测 Prompt。若公司业务信息不足，LLM 应降低置信度并扩大关键词检索范围，不能凭当前常识判断公司业务。

固定流程：

1. 读取候选股票、`company_context`、只读 `nl_prior/README.md` 评分说明和 `prior.json` 中的全部规则。
2. 为每只候选股票启动独立自然语言评分任务；任务之间可以用受限线程池并行执行。
3. 每个任务只持有本股票的最小候选身份、公司上下文、规则、检索请求、evidence 和 conversation trace，不共享其他股票的上下文窗口。
4. 调用 LLM Proxy 生成结构化 `search_requests`（grep 语义：大小写不敏感正则 `pattern`，在标题、代码和正文全文上匹配；旧 `keywords` 列表自动转为转义后的或式正则），从 `/mnt/snapshot/text_index.parquet` 和 `/mnt/snapshot/text_library/` 检索 evidence；候选股票自身代码、历史名称或公司名命中的标题/正文优先于泛化市场命中，标题命中优先于仅正文命中，其次按发布时间排序。检索结果会标记 `candidate` 或 `background`；只有 `candidate` evidence 可以被最终评分引用，泛化行业或宏观 evidence 只能作为背景补充。每条 evidence 默认返回完整存档正文（正文在快照构建时截断到 4000 字符）。
5. 每只股票最多允许 3 轮信息检索；第 1 轮为初始检索，第 2-3 轮为补充检索。任一轮 evidence 已经足够支持判断时，可以提前结束检索。
6. 拼接候选身份、公司上下文、规则、evidence 和打分表，再调用 LLM Proxy 输出最终严格 JSON。
7. 解析 `nl_score`、`confidence`、`risk_tags`、`applied_prior_ids` 和 `evidence_ids`。

自然语言评分输入采用 JSON object，是为了稳定日志复现、schema 校验和 provider adapter 处理；这不是让模型读取因子结果。传给 LLM 的候选对象只包含 `ts_code`。`task_id`、线程 ID 和调用 ID 只属于 `backtest_tool` 内部日志，不进入 Prompt。`factor_score`、`factor_rank`、因子理由、目标权重、验证收益、回测结果和其他股票结论不得进入自然语言评分 Prompt。`backtest_tool` 必须在 LLM 返回并校验 `nl_score` 后，才把自然语言分与因子分合成。

LLM 不直接写正式结果文件。LLM Proxy 把 provider 响应交给 `backtest_tool`，由 `backtest_tool` 提取、解析、校验并写入 `nl_output/scores.jsonl`。批量自然语言评分的完整 LLM/API 调用明细写入本次回测目录下的 `nl_output/nl_llm_calls.jsonl`；`agent_trace.jsonl` 只记录本次自然语言评分批次的摘要、状态、数量和 `nl_output/` 路径。

| 提取来源 | 处理方式 |
|---|---|
| provider 返回 tool/function call 参数 | 直接读取参数字符串作为 JSON 原文 |
| provider JSON mode 或结构化响应 | 读取响应内容中的 JSON object |
| 普通文本响应 | 只接受一个完整 JSON object；允许去掉一层 json 代码围栏，但不从长文本里搜索分数字段 |

不同 provider 的思考文本由 LLM Proxy 或 provider adapter 处理，不进入正式字段提取：

| 响应形态 | 处理方式 |
|---|---|
| provider 分离 `reasoning_content` 和最终 `content` | 记录 `reasoning_content` 到 `nl_output/nl_llm_calls.jsonl`；只把最终 `content` 交给 JSON 提取 |
| 普通文本中包含闭合 `<think>...</think>` | 剥离闭合 think 块并记录原文；只解析 think 块后的剩余内容 |
| `<think>` 未闭合，或剥离后仍不是唯一 JSON object | 该股票任务失败，或按 run config 触发一次固定修复调用 |

思考文本不能提供正式分数、风险标签或 evidence 引用；这些字段只能来自最终 JSON。

提取后统一执行 `json.loads` 和 schema 校验。自然语言评分字段使用固定区间：`nl_score ∈ [-1, 1]`，`0` 表示中性，正数表示支持做多，负数表示支持降权、回避或做空；`confidence ∈ [0, 1]`。`evidence_ids` 只能引用本任务检索返回且标记为 `candidate` 的 `text_id` 或 `source_hash`；`applied_prior_ids` 必须来自当前 `prior.json` 规则 ID。非中性或引用证据的评分至少引用一条适用规则；证据不足的中性评分可以不引用 prior。若响应包含额外解释、多个 JSON object、字段缺失、分数越界、`ts_code` 不一致、evidence 引用不合法或 prior 引用不合法，则该股票任务失败；可按 run config 允许一次固定“只输出严格 JSON”的修复调用，仍失败则进入失败处理。

`backtest_tool` 通过任务终态判断自然语言评分是否结束，而不是依赖 LLM 自行声明。每只股票任务只有进入以下状态之一才算完成：

| 终态 | 含义 |
|---|---|
| `completed` | 最终 JSON 可解析，必需字段存在，分数范围合法，`ts_code` 一致，evidence 引用通过校验 |
| `skipped_by_config` | 当前运行配置关闭或抽样跳过自然语言评分，结果已按配置标记 |
| `failed_with_policy` | LLM/检索失败，但 run config 明确允许可审计的失败处理 |
| `timeout` | 单股票任务超过本次自然语言评分超时，按 run config 处理 |
| `failed` | JSON 无效、字段缺失、分数越界、股票代码不一致或 evidence 引用不合法 |

线程池中的所有候选股票任务进入终态后，`backtest_tool` 才能合成 `final_score`。若存在 `failed` 且没有显式失败处理策略，本次正式回测失败；若任务提前输出合法 JSON，则该股票任务立即结束，不再发起后续检索轮。

自然语言输出不能靠字符串查找提取分数。若 JSON 解析失败、字段缺失、分数越界或 `ts_code` 不一致，正式回测必须失败，除非 run config 显式配置了可审计的失败处理策略。

默认合成规则：

```text
factor_score_norm = cross_section_normalize(factor_outputs.factor_score, range=[-1, 1])
final_score = 0.7 * factor_score_norm + 0.3 * nl_score
```

`factor_score_norm` 和 `nl_score` 使用同一 `[-1, 1]` 尺度，因此默认加权才有可比性。若某次运行配置改用其他自然语言评分尺度，必须先映射回 `[-1, 1]`，并在 `run_manifest.json` 中记录映射规则。

交易方向由 run manifest 中的总分阈值决定：`final_score` 达到做多阈值做多，低于做空阈值做空，中间区间不交易。做空候选会先通过当前券源模式过滤，不可做空的候选被跳过并顺延。默认做多/做空阈值和持仓上限是第 5.3 节默认回放/Broker profile 的一部分，本文档只在该表维护具体数值。

若自然语言风险标签包含 `hard_exclude`，默认剔除候选。正式订单计划至少包含 `ts_code`、`side`、`target_weight`、`final_score`、`reason` 和 `source_artifacts`。`side` 为 `long` 或 `short`；`target_weight` 是有符号组合权重，做多为正、做空为负。`backtest_tool` 必须同时检查总持仓数量、单边敞口、总敞口、现金和融券约束。

每次调用创建一个新的 `results/<phase>_<idx>/` 目录，例如 `valid_000` 或 `test_000`，并至少写入：

| 产物 | 内容 |
|---|---|
| `detailed_return.json` | 收益、回撤、持仓、成交、拒单和成本明细 |
| `order_plan.parquet` | 通过校验的订单计划 |
| `nl_output/` | 自然语言评分、检索请求、evidence 和风险标签 |
| `factor_attribution.json` | 因子贡献分析（开关 `factor_attribution_enabled`）：基于候选池中的单因子分列 `factor_<id>`，用 Shapley 方法（联盟分 = 成员归一化因子分均值，≤8 因子精确枚举、更多用置换抽样）在同一合成/订单/回放管线上评估各因子对回放总收益的贡献，复用已算好的自然语言分（不追加 LLM 调用）；逐因子附 Agent 填写的 `rationale`；完整验证启用该开关时，至少需要一个登记因子和对应 `factor_<id>` 列 |

调用状态、核心指标、错误、警告和结果路径不单独写摘要文件，统一写入 `run_manifest.json` 的回测摘要，并追加一条 `agent_trace.jsonl` 回测事件。启用 `step_tree_enabled` 时，每次成功的完整验证回测（`nl=on`）还会把当前正式产物快照为 `steps/` 树的新节点（父指针指向当前位置节点），并把本次 `factor_attribution.json` 作为该节点附件保存。

`nl_output/` 至少包含：

| 文件 | 内容 |
|---|---|
| `company_context.jsonl` | 每只候选股票在决策时点可见的公司身份和来源 |
| `search_requests.jsonl` | 各轮检索请求、轮次、提前结束原因和状态 |
| `evidence.jsonl` | 检索到的 as-of evidence 摘要和引用 |
| `scores.jsonl` | 最终 LLM JSON 打分结果 |
| `nl_llm_calls.jsonl` | 自然语言评分内部的完整 LLM/API 调用明细，包括 messages、原始响应、解析结果、用量和错误；不包含 API key 或 Authorization header |

每条评分必须引用 `text_id` 或 `source_hash`。没有 evidence 的判断只能标记为低置信度或无证据，不能伪造引用。训练/验证期的 `nl_output/` 可以供 Agent 读取并改进下一 Step 的 `prior.json`；测试和 held-out 的 `nl_output/` 不反馈给 Agent。

订单计划校验失败时必须返回明确错误，不能自动替 Agent 删除股票。测试和 held-out 由 Runner/root 调用同一 Tool，但结果不反馈给 Agent。

### 4.5 `finish_fold_tool`

`finish_fold_tool` 是 Agent 主动结束当前 Fold 的入口。它不接收业务参数。调用后 Environment 先做两项校验：

1. 检查当前正式产物是否已经通过 `modification_check_tool`，且产物没有在检查后被改动。
2. 调用 `backtest_tool` 的轻量合同校验模式，只检查策略主函数可加载、`generate_candidates()` 返回 schema 合法、`prior.json` 可解析、订单计划生成前置条件完整；不执行自然语言 LLM 评分、不写真实回测结果、不产生模拟成交。

两项校验都通过后，`finish_fold_tool` 更新 `run_manifest.json` 的 Fold 结束状态，追加一条 `agent_trace.jsonl` 结束事件，停止当前 Fold 的 Agent 调用并锁定写入。任一校验失败时，Tool 返回错误和可修复原因；如果 Fold deadline 尚未到达，Agent 可以继续修改或重新运行验证；如果 deadline 已到，Environment 不再开放新的修复调用，交由 Pipeline 按超时回退规则处理。

Fold 是否冻结策略产物、是否进入测试期，由 Pipeline 根据最近一次修改检查、最近一次正式验证回测和 `finish_fold_tool` 校验结果决定。

## 5. 模拟 Broker、回测和交易约束

### 5.1 模拟 Broker 边界

Environment 应尽量模拟 QMT 的实盘交互形态，但不连接真实券商账户。Agent 或冻结策略只能提交结构化订单，不能直接写成交、持仓或收益。

模拟 Broker 至少提供：

| 接口 | 含义 |
|---|---|
| `get_account()` | 返回现金、总资产、可用资金和风控限制 |
| `get_positions()` | 返回当前持仓、可卖数量和成本 |
| `submit_order()` | 接收买入、卖出、融券卖出、买券还券或目标权重订单 |
| `cancel_order()` | 撤销尚未成交订单 |
| `query_orders()` | 查询订单状态、拒单原因和成交明细 |

订单提交示例：

```json
{
  "ts_code": "000001.SZ",
  "side": "long",
  "order_type": "target_weight",
  "target_weight": 0.05,
  "limit_price": null,
  "reason": "score=0.81; nl_score=0.62",
  "source_artifacts": ["order_plan_step02", "nl_scores_step02"]
}
```

做空订单使用同一结构：

```json
{
  "ts_code": "000002.SZ",
  "side": "short",
  "order_type": "target_weight",
  "target_weight": -0.04,
  "limit_price": null,
  "reason": "score=-0.82; nl_score=-0.44",
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

初始流程使用固定持有期的多空订单规则：

1. 在决策交易日开仓：`final_score` 达到做多阈值的标的买入做多，低于做空阈值的标的在融券约束通过后融券卖出做空（默认阈值见第 5.3 节）。
2. 按给定持有周期持有，中间不调仓、不做 T、不因事件临时修改。
3. 周期结束时平多仓并买券还券平空仓。
4. 做空候选若不满足当前券源模式，会在订单计划阶段跳过并顺延到下一个可做空候选；若订单提交后仍因费率、保证金、风控线或行情约束不满足，模拟 Broker 必须拒单并记录原因，不能静默改成做多。

突发事件再决策和日内做 T 后续也应通过同一个模拟 Broker 订单接口进入回放。

### 5.3 回放配置

当前基础流程的买入日、卖出日和持有期来自 Fold 调度请求。成本模型、成交规则、仓位上限和拒单逻辑属于 Environment 的默认中信回放/Broker profile，由 Environment 在执行 `backtest_tool` 前解析，并把实际使用的版本、参数、券源模式、费率假设和风控规则来源写入 `run_manifest.json`。Pipeline 只引用这个 manifest，不直接维护 Broker 配置。

默认回放/Broker profile：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| 初始本金 | `1_000_000` 元 | 每个 Fold 回放从 100 万现金开始，除非 run manifest 显式覆盖 |
| 佣金费率 | `1.0 bps` | 即万 1，买卖双边收取，单笔最低 `5` 元 |
| 印花税 | 卖出 `10 bps`；`20230828` 起 `5 bps` | 只对卖出方收取（含融券开仓卖出），按成交日期取档 |
| 滑点 | `5 bps` | 买入按不利方向加价、卖出减价，作用于开仓和平仓价格 |
| 做多阈值 | `+0.7` | `final_score` 达到阈值才允许进入做多订单计划 |
| 做空阈值 | `-0.7` | `final_score` 低于阈值才允许进入融券做空订单计划 |
| 标的数量上限 | `10` | 多空合计最多 10 个可执行标的；做多按高分排序，做空按负分强度排序，不可做空候选自动顺延 |
| 单标的权重上限 | `20%` | 权重按分数比例分配后逐标的截断 |
| 默认券源模式 | `proxy_margin_secs` | 用决策日融资融券标的资格近似可融券源 |
| 融券保证金比例 | `100%` | 按中信公开规则，普通投资者新开融券合约不得低于 100%；私募证券投资基金用 `120%` |
| 维持担保比例线 | 平仓 `130%` / 安全 `140%` / 提取 `300%` | 中信公示基础档（T 日 16:00 维持担保比例高于 200% 时）；集中度分档变体未建模 |
| 融券费率 | 年化 `8.5%`（假设值） | 研究假设，manifest 标记 `is_assumed`；真实费率随 broker 文件接入 |
| 空头公司行为 | `disabled` | 分红/配股对空头的影响暂不建模，profile 显式记录该开关 |

如果覆盖默认值，`run_manifest.json` 必须写明覆盖后的参数和来源。

`backtest_tool` 调用策略主函数后，在内部完成自然语言评分、权重合成和订单计划校验，再把校验后的订单计划提交给模拟 Broker。测试或 held-out 也使用同一入口；冻结策略不能绕过 `backtest_tool` 直接提交外部订单文件。

融券做空流程按 broker profile 执行。当前研究默认先用 `proxy_margin_secs`，即用按日期融资融券标的资格近似可做空股票池：

| 步骤 | `proxy_margin_secs` 默认检查 |
|---|---|
| 标的资格/券源 | 股票必须在决策日 `margin_secs` 中；订单计划阶段先过滤并顺延，不在表内的做空候选不占用持仓名额 |
| 可融数量 | 暂不做个券数量限制，由组合权重和总持仓上限约束 |
| 融券费率 | 使用回放 profile 的研究假设费率或配置费率，并在 manifest 中标记为假设值 |
| 保证金占用 | 普通账户按不低于 100% 融券保证金比例占用，私募证券基金按不低于 120% 占用 |
| 风控线 | 按中信维持担保比例规则检查平仓线、安全线、提取线和集中度约束 |
| 成本计提 | 按持有天数计提融券费；空头公司行为按 profile 开关处理（当前 `disabled`，须显式记录） |
| 平仓 | 持有期结束买券还券；若触发强平规则，记录为强制平仓事件 |

`proxy_margin_secs` 只适合研究初筛，不代表中信账户真实可融数量。后续接入中信真实券源后，可以切换为 `broker_inventory` 模式：股票必须同时在交易所融资融券标的内、存在中信账户可见券源、可融数量覆盖目标订单、并有该股票/期限/合约的可见费率；缺失任一项即拒绝真实做空。没有 `margin_secs` 或真实 broker 文件时，可以显式运行 `theoretical_short` 研究模式，但必须在 `run_manifest.json` 和结果中标记。

### 5.4 交易约束

Environment 必须检查：

- 股票是否在可交易股票池。
- 订单提交时点和可成交交易日是否满足 PIT。
- 买入日是否停牌。
- 买入价是否受涨停限制。
- 卖出日是否停牌或跌停无法卖出。
- 权重是否超过上限。
- 持仓股票数量是否超过配置；按总分阈值过滤后，多空合计不超过 profile 持仓上限（默认值见第 5.3 节）。
- 现金、持仓、可卖数量和 A 股 T+1 规则是否满足。
- 做空订单是否满足当前券源模式：默认 `proxy_margin_secs` 在订单计划阶段按决策日 `margin_secs` 过滤并顺延；`broker_inventory` 要求真实中信券源、费率和数量可见；两种模式都要检查保证金比例、维持担保比例和集中度限制。
- 是否使用了未来价格。

已进入订单计划的订单如果提交或成交失败，必须返回明确拒单原因，不能静默删除。

### 5.5 收益统计

至少输出：

- 总收益。
- 做多收益。
- 做空收益。
- 年化收益。
- Sharpe。
- 最大回撤。
- 胜率。
- 持仓数量。
- 换手率。
- 订单接受、拒单、撤单和成交统计。
- 佣金、印花税、滑点假设、融券费、保证金占用、不可做空候选顺延数量、真实券源不足拒单、费率缺失拒单和强制平仓统计。
- 每只股票的买入、卖出、成本和收益。

## 6. LLM API 边界

### 6.1 调用入口

Sandbox 内的 Shell/Python 不直接请求 provider。所有 LLM 请求都必须经过宿主侧 LLM Proxy。

```text
Agent Runner main conversation -> local LLM Proxy (agent model) -> provider
backtest_tool natural-language scoring -> local LLM Proxy (nl model) -> provider
```

两类调用使用相互独立的接口：Agent 主对话和自然语言评分可分别配置不同的 provider/model。正式实验入口默认主 Agent 使用 `deepseek-v4-pro`，自然语言评分使用 `deepseek-v4-flash`；如底层调用没有单独传入 `nl_proxy`，才复用主对话 proxy。日志落点合同不变。

两类请求的含义不同：

| 调用类型 | 用途 | 是否进入正式回测结果 |
|---|---|---|
| Agent 主对话 | 驱动 Agent 在当前 Fold 内读数据、写代码、调用 Tool 和决定是否结束 Fold | 否 |
| `backtest_tool` 自然语言评分 | 对候选股票做文本检索、证据判断、风险标签和 `nl_score` 打分 | 是，结果进入本次回测的 `nl_output/` |

### 6.2 安全和超时

LLM API 的安全合同：

- API key 只在宿主侧读取。
- API key 不进入 Sandbox、prompt、artifact 或日志。
- Sandbox Shell/Python 不能直接联网调用 provider。
- 每次 provider 请求必须设置最长等待时间。
- 超时记为 `timeout`，不能阻塞整个 Fold。
- provider 原始响应可以记录，但不能包含 Authorization header 或 API key。
- 默认开启 provider 推理模式（DeepSeek `thinking`）；分离返回的 `reasoning_content` 只进入日志，正式字段始终从最终 `content` 提取。

### 6.3 调用明细落点

真实 LLM 调用必须可审计，但不同调用写入不同位置，避免全局 trace 过大。

| 调用类型 | 完整调用明细落点 | `agent_trace.jsonl` 记录内容 |
|---|---|---|
| Agent 主对话 | `agent_trace.jsonl` 的 `llm_call` 事件 | 完整明细本身：messages、原始响应、解析结果、用量、错误、provider、model、temperature、seed 和关联产物 ID |
| `backtest_tool` 自然语言评分 | `results/<phase>_<idx>/nl_output/nl_llm_calls.jsonl` | 只记批次摘要：候选数量、完成/失败/超时数量、结果目录和错误摘要 |

自然语言评分的正式结果以 `results/<phase>_<idx>/nl_output/scores.jsonl` 为准；`nl_llm_calls.jsonl` 用于审计和未来蒸馏，不作为回测分数表。

## 7. 运行日志和审计

Environment 只定义 Sandbox 运行时实际写哪些文件；实验级账本字段、Fold/run 聚合 hash 和输出路径由 `docs/pipeline_design.md` 第 7 章统一定义。

### 7.1 运行文件

每次 Sandbox 运行至少写：

| 文件 | 内容 |
|---|---|
| `run_manifest.json` | 本次 Sandbox 输入、输出、配置、deadline、镜像版本、关键产物版本、最近修改检查、回测摘要、结果路径和 Fold 结束状态 |
| `agent_trace.jsonl` | 同一 conversation trace 下的 Shell、Tool、回测、模拟 Broker、Agent 主对话 LLM、自然语言评分批次摘要、结束 Fold 和错误事件；不包含 API key 或 Authorization header |
| `results/<phase>_<idx>/detailed_return.json` | 详细收益、回撤、持仓、成交、拒单和成本统计 |
| `results/<phase>_<idx>/order_plan.parquet` | 策略主函数返回值经过校验和权重合成后的订单计划 |
| `results/<phase>_<idx>/nl_output/` | 自然语言评分结果、风险标签、检索请求、evidence 引用和自然语言评分 LLM 调用明细；这是回测使用的正式自然语言评分产物 |

`snapshot_manifest`、`factor_manifest`、`order_plan_manifest` 等可以作为 `run_manifest.json` 的引用或子文件存在，不需要在本章重复定义字段。

`agent_trace.jsonl` 是同一个 Agent session / conversation trace 的轻量事件流。它记录 Shell、Tool、回测、模拟 Broker、Agent 主对话 LLM、自然语言评分批次摘要、结束 Fold 和错误。事件必须共享 `experiment_id`、`epoch_id`、`fold_id`、`step_id`、`run_id`、`conversation_id`，并用 `call_id` / `parent_call_id` 串联顺序。

`agent_trace.jsonl` 是过程索引文件，不是自然语言评分结果表。批量自然语言评分只在这里保存摘要和 `nl_output/nl_llm_calls.jsonl` 路径；具体 messages、原始响应和解析结果写在 `nl_llm_calls.jsonl`。

### 7.2 读取权限

训练/验证期：

- Agent 可以只读 `agent_trace.jsonl`。
- Agent 可以只读对应验证结果目录下的 `nl_output/`。
- Agent 不能写入、截断或替换这些文件。

测试和 held-out：

- `agent_trace.jsonl` 不反馈给 Agent。
- `nl_output/` 不反馈给 Agent。
- Runner/root 和审计流程可以读取完整产物。

### 7.3 审计检查

主进程读取 artifact 前必须检查：

- exit code。
- run manifest 是否完整。
- 是否写入禁止路径。
- 关键 manifest 和冻结产物版本是否匹配。
- `agent_trace.jsonl` 是否完整，且自然语言评分批次能否追溯到对应 `nl_output/nl_llm_calls.jsonl`。

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
