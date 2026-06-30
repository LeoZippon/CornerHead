# Environment 设计

本文档记录 Environment 层。Environment 负责准备 PIT 数据窗口、启动 Sandbox、提供受控执行入口和可信服务 Tool、执行回测并写审计信息。Agent 可以在 Sandbox 内探索和写代码，但只能使用 Environment 提供的数据、Shell 入口和受控 Tool。

相关边界：

- Agent 行为、可写产物和输出格式见 `docs/agent_design.md`。
- Step / Fold / Epoch 编排见 `docs/pipeline_design.md`。
- 原始数据下载、单位和审计见 `docs/data_documentation.md`。
- QMT 实盘流程见 `docs/QMT_documentation.md`。

## 术语说明

| 术语 | 含义 |
|---|---|
| Environment | 准备 PIT 窗口、启动 Sandbox、提供执行入口和可信服务 Tool、执行回测和写审计信息的环境层 |
| PIT | Point-in-time，只使用决策时点已经可见的数据 |
| Sandbox | Agent 运行的隔离容器或本地开发执行环境 |
| Runner | 负责驱动 Agent 会话、切换 snapshot、调用 Tool 和记录日志的程序 |
| Execution Gateway | Sandbox 与 Shell/可信服务 Tool 之间的入口，负责权限、路径、运行约束和日志 |
| LLM Proxy | 宿主侧大模型接口代理，保存 API key 并记录调用 |
| artifact | 单次运行产生的策略文件、回测结果、Broker 事件和 NL 日志 |
| manifest | 记录输入、输出、时间范围、配置和关键版本的文件 |
| Broker | 模拟券商，接收交易意图、生成成交/拒单和持仓状态 |
| Held-out | 所有训练结束后才运行的冻结测试区间 |

## 导航

- [1. Environment 职责](#1-environment-职责)
  - [1.1 职责边界](#11-职责边界)
  - [1.2 可信日志](#12-可信日志)
- [2. PIT 数据窗口](#2-pit-数据窗口)
  - [2.1 可见性原则](#21-可见性原则)
  - [2.2 准备窗口](#22-准备窗口)
  - [2.3 数据域](#23-数据域)
  - [2.4 单位与特殊口径](#24-单位与特殊口径)
- [3. Snapshot 路径与裸数据窗口](#3-snapshot-路径与裸数据窗口)
  - [3.1 路径概念](#31-路径概念)
  - [3.2 数据槽](#32-数据槽)
  - [3.3 当前决策输入](#33-当前决策输入)
  - [3.4 PIT 支撑机制](#34-pit-支撑机制)
- [4. Sandbox 与 Runner](#4-sandbox-与-runner)
  - [4.1 Sandbox 要求](#41-sandbox-要求)
  - [4.2 运行产物](#42-运行产物)
  - [4.3 Runner 责任](#43-runner-责任)
- [5. 执行入口和可信 Tool](#5-执行入口和可信-tool)
  - [5.1 工具列表](#51-工具列表)
  - [5.2 修改检查和锁定](#52-修改检查和锁定)
- [6. 策略执行和 NL 服务](#6-策略执行和-nl-服务)
  - [6.1 正式流程](#61-正式流程)
  - [6.2 Valid 与 Frozen Eval](#62-valid-与-frozen-eval)
  - [6.3 NL 服务](#63-nl-服务)
- [7. Broker、回放和做空规则](#7-broker回放和做空规则)
  - [7.1 Broker 基础原语](#71-broker-基础原语)
  - [7.2 main(ctx) 与逐 tick 回放](#72-mainctx-与逐-tick-回放)
  - [7.3 回放 Profile](#73-回放-profile)
  - [7.4 Broker 强制约束](#74-broker-强制约束)
  - [7.5 做空模式](#75-做空模式)
  - [7.6 结果目录](#76-结果目录)
- [8. LLM API 边界](#8-llm-api-边界)
  - [8.1 Provider 边界](#81-provider-边界)
  - [8.2 调用日志](#82-调用日志)
- [9. 运行日志、审计和验收](#9-运行日志审计和验收)
  - [9.1 核心文件](#91-核心文件)
  - [9.2 Manifest 和 Trace](#92-manifest-和-trace)
  - [9.3 读取权限](#93-读取权限)
  - [9.4 审计检查](#94-审计检查)
  - [9.5 验收清单](#95-验收清单)

## 1. Environment 职责

### 1.1 职责边界

Environment 负责：

- 按决策时点构造 PIT 数据窗口。
- 把窗口数据放入 Sandbox 的固定只读路径。
- 提供结构化检索、Sandbox Shell、修改约束检查、回测 Tool、NL 服务和模拟 Broker。
- 统一 snapshot 可见字段单位，记录数据覆盖、版本、hash 和转换规则。
- 执行交易约束、订单模拟、成交模拟、拒单记录和收益统计。
- 记录 Shell、Tool、Broker、回测、LLM 和关键 manifest。
- 提供策略产物的受控读写、修改量统计、冻结产物审计和 hash 校验。

Environment 不负责：

| 事项 | 归属 |
|---|---|
| 决定投资逻辑或策略内容 | Agent |
| 判断哪个候选、prompt 或交易函数更好 | Agent / Pipeline |
| 读取 held-out 后参与训练 | 禁止 |
| 真实下单或连接券商 | QMT 流程 |
| 下载 raw 数据或决定数据源口径 | Data 层 |

### 1.2 可信日志

可信日志只能由 Runner、Execution Gateway、LLM Proxy 和 Broker 自动生成。Agent 的解释、note 或输出字段不能替代可信日志。

## 2. PIT 数据窗口

### 2.1 可见性原则

进入正式策略的所有输入必须满足：

```text
available_at <= decision_time
```

如果数据没有可靠发布时间，Environment 必须使用保守规则延后可见，或从本次窗口中排除。窗口数据可以比配置短，例如刚上市股票不足完整历史，或某个研究数据保留下限晚于窗口起点；实际覆盖必须写入 manifest。

### 2.2 准备窗口

可见窗口由实验启动前的 `SnapshotConfig` 冻结并写入 run manifest；默认值用于未显式覆盖的数据域。窗口可以按数据域分别调整，实际覆盖仍必须写入 snapshot manifest。

| 数据域 | Snapshot 文件 | 配置项 | 默认准备窗口 | 可见边界 |
|---|---|---|---:|---|
| `daily` | `daily.parquet` | `daily_window_months`，缺省回退 `window_months` | 最近 21 个月 | 不包含决策时点之后行情 |
| `intraday_1min` | `intraday_1min.parquet` | `intraday_trade_days` | 最近 21 个交易日 | 开盘前不含当日分钟线；盘中按 bar close 截到 `decision_time` |
| `fundamentals` | `fundamentals.parquet` | `fundamentals_window_months`，缺省回退 `window_months` | 最近 21 个月可见披露 | 按公告日、披露日、报告期和版本字段筛选 |
| `events` | `events.parquet` | `events_window_months`，缺省回退 `window_months` | 最近 21 个月 | 按 `available_at` 满足可见性；T+1 或盘后数据保守处理 |
| `macro` | `macro.parquet` | `macro_window_months`，缺省回退 `window_months` | 最近 21 个月 | 按发布时间或保守可见时间过滤 |
| `text` | `text_index.parquet`、`text_library/` | `text_window_months`，缺省回退 `window_months` | 最近 21 个月 | 正文检索必须引用可见 `text_id` 或 `source_hash` |

run manifest 记录 `snapshot_config.decision_windows`，snapshot manifest 记录 `window_config` 和各数据域 `domain_windows`。Pipeline 的 Fold `input_window` 默认使用同一个 `window_months`，用于描述 Agent 可见历史的最大跨度；各数据域可以在该默认值上单独收缩或扩展。

### 2.3 数据域

数据域拼接方式：

| 数据域 | 主要来源 | 输出边界 |
|---|---|---|
| `daily` | 日线、每日指标、复权因子和交易日历 | 日频行情、横向排序输入和 Broker 回放参考 |
| `intraday_1min` | 1 分钟线和交易日历 | 日内策略、开收盘和做 T 研究 |
| `fundamentals` | 财报、财务指标、分红、业绩预告/快报、披露计划和主营构成 | 财务和经营质量窗口，保留可追溯版本字段 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等 | 事件和资金状态窗口 |
| `macro` | 宏观、政策、利率、全球事件和跨市场数据 | 市场背景窗口 |
| `text` | 公告、新闻、研报、政策文本 | PIT 文本检索库，必须可追溯到文本 ID |

### 2.4 单位与特殊口径

单位合同：

| 类型 | 标准单位 |
|---|---|
| 金额 | 元 |
| 成交量/股本 | 股 |
| 比例、收益、换手 | 小数，例如 5% 记为 `0.05` |
| 利率和费率 | 优先小数；确需 bps 时字段名必须带 `_bps` |

原始单位、转换规则和转换前字段必须写入 manifest。单位不明的字段不能进入模型可见数据；依赖单位不明字段生成的交易意图必须被校验拒绝。

特殊口径修正：

| 场景 | 派生字段规则 | 边界 |
|---|---|---|
| 历史 09:30 分钟条被用作实盘 `stk_auction` 近似输入 | 对 09:30 的深圳股票生成校正后的成交量/成交额字段：`00*.SZ` 乘 `0.76`，`30*.SZ` 乘 `0.58`；沪市、北交所和其他时点保持 `1.0` | 只用于开盘竞价近似输入；raw 分钟线、日内成交汇总、15:00 收盘竞价不改写 |

manifest 至少记录校正规则 ID、适用字段、倍率、适用市场/代码前缀和生成时间。策略代码应读取校正后的派生字段；如果派生字段不存在，不能静默退回未校正字段来模拟开盘竞价。

## 3. Snapshot 路径与裸数据窗口

### 3.1 路径概念

先区分两个概念：

- `/mnt/snapshots/<stage>`：Agent 可见或回放用的数据槽。`train` 是 `valid_decision_input` 的 Agent-visible alias，供训练/探索使用；`valid` 是验证回放数据区间，`test` 是测试类回放区间。
- `/mnt/snapshot`：`backtest_tool` 正式执行时绑定的当前决策输入视图，只包含本次决策时点前已可见的数据。

### 3.2 数据槽

Sandbox 数据槽：

```text
/mnt/snapshots/
  train/
  valid/
  test/
```

权限和用途：

| 路径 | 用途 | Agent 权限 | Tool 用法 |
|---|---|---|---|
| `/mnt/snapshots/train/` | 训练和探索输入，等同 `valid_decision_input` 的只读 alias | 只读可见 | 不作为正式策略入口输入 |
| `/mnt/snapshots/valid/` | 验证回放区间 | 只读可见 | 验证回放读取 |
| `/mnt/snapshots/test/` | 测试或 held-out 回放区间 | Agent 不可读 | 冻结评估读取 |
| `/mnt/snapshot/` | 当前决策输入视图 | 正式策略只读 | `main.py` 正式运行输入 |

`valid` 和 `test` 回放槽可以包含回放期行情、事件、文本索引+文本库和可选分钟线。正式策略代码不直接选择 `train`、`valid` 或 `test`；Runner/root 在调用 `backtest_tool` 前把对应 decision input view 镜像为当前 `/mnt/snapshot`。

元学习会话使用与第一个 Fold Agent 相同的可见数据：第一个 Fold 的 `valid_decision_input` 绑定到 `/mnt/snapshot` 并复制为 `/mnt/snapshots/train` 的只读 alias，第一个 Fold 的验证回放安装到 `/mnt/snapshots/valid`。`/mnt/snapshots/test` 和 held-out 不进入元学习可见输入。

### 3.3 当前决策输入

`/mnt/snapshot` 内容：

```text
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

规则：

- `valid_decision_input` 和 `test_decision_input` 不能包含对应回放期未来行情、成交和收益。
- `text_library/` 是 as-of 正文库，正文或片段必须由 `text_index.parquet` 引用。
- `universe.parquet` 使用决策日在市口径，避免使用当前上市名单带来幸存者偏差。
- 宿主 `runtime/snapshot_views/` 保存多个决策输入视图，不挂载给 Agent。
- 宿主 `runtime/current_snapshot/` 是当前镜像，容器内只读挂载为 `/mnt/snapshot`。

示例：

```text
/mnt/snapshots/
  train/   2020-01 到 2021-09（valid_decision_input alias）
  valid/   2021-10 到 2021-12
  test/    2022-01 到 2022-03

宿主 runtime/snapshot_views/
  valid_decision_input/  2020-01 到 2021-09
  test_decision_input/   2020-04 到 2021-12
```

### 3.4 PIT 支撑机制

PIT 支撑机制与裸数据窗口：

| 机制/产物 | 路径/来源 | 作用 | 进入 snapshot 的规则 |
|---|---|---|---|
| 日频数据可见性合约 | `src/autotrade/environment/data/contracts.py`、`data/raw/{daily,daily_basic,adj_factor,stk_limit,suspend_d}` | 定义日频分区在决策时点是否可见 | 只读取 `available_at(partition_date) <= decision_time` 的分区，并按各数据集自己的可见日期拼接 |
| raw `available_at` | `data/raw` 中带 `available_at` 的事件、宏观、文本和分钟数据 | 提供逐行 PIT 过滤依据 | 只保留 `window_start <= available_at <= decision_time` 的行；缺少 `available_at` 的配置数据集必须报错 |
| `fundamental_events` | `data/pit/fundamental_events/<dataset>/available_month=<YYYYMM>.parquet` | 财务、分红、审计、主营构成和披露计划的多版本可见性索引；每行带 `available_at`、`available_at_rule`、`business_key`、`source_path`、`source_hash`、`source_row_id` | `fundamentals.parquet` 只读取 `available_at <= decision_time` 且落在窗口内的可见版本 |
| `fundamental_events` 审计状态 | `results/data_quality/fundamental_events_status.json` | 阻断不可用的财务 PIT 事件索引 | 启用 `fundamentals` 时必须存在且不是非法 JSON、`status=error` 或 `errors>0` |
| 标准单位归一化 | `src/autotrade/environment/features/units.py` | 统一 `daily.parquet` 中成交量、成交额、比例类字段单位 | 只做单位规范化，不生成 alpha 因子；转换记录写入 `manifest.json` |
| Snapshot manifest | `/mnt/snapshot/manifest.json` | 记录窗口、数据域、行数、单位转换、hash、覆盖范围、构建耗时和轻量数据 profile | `backtest_tool` 校验绑定的 snapshot id/hash 必须与 Pipeline 记录一致 |
| Agent data summary | `/mnt/artifacts/data_summary.json` | 预生成 Agent 可见轻量数据索引，含文件规模、行数、列数、关键列、日期覆盖和大表访问提示 | Agent 工具可读；正式策略代码不得硬编码读取 |

构造规则：

- Agent 可见输入是 `/mnt/snapshot` 下的 PIT 裸数据窗口：`daily.parquet`、`intraday_1min.parquet`、`fundamentals.parquet`、`events.parquet`、`macro.parquet`、`text_index.parquet`、`text_library/` 和 `universe.parquet`。
- Snapshot 只做标准单位归一化、PIT 可见性过滤、跨 raw 表的同键拼接和交易约束字段对齐；不预构建 alpha 因子、滚动收益、均线、波动率、综合分数或候选排名。
- Snapshot 构建会在 manifest 中记录 `build_profile` 和 `data_profile`，用于宿主侧定位构建瓶颈和核对行数/字段/关键日期覆盖；这些 profile 不改变 parquet 内容，也不参与 Agent 策略逻辑。Agent 可见的 `data_summary.json` 只保留轻量索引，不暴露 build timing。
- `daily.parquet` 来自 `daily`、`daily_basic`、`adj_factor`、`stk_limit`、`suspend_d` 等 raw 表的可见分区，保留裸行情、估值、复权因子、涨跌停和停牌约束字段。
- `fundamental_events.available_at` 取公告日（`f_ann_date`/`ann_date` 等，列基准见 `data_documentation.md` §5.2）当日 18:00 作为行级可见时间；缺失公告日的数据必须有保守回退规则或被排除。
- join 财务/分红事件时只允许 `available_at <= decision_time` 的版本。
- `limit_list_d` 不进入预先计算的 alpha 列；稳定状态和不稳定字段都保留在 raw 或审计层，Agent 如需使用必须从可见窗口自行解释。
- PIT 事件索引构建与审计入口是 `scripts/data/build_pit_events.py`；启用 `fundamentals` 时必须提供 `results/data_quality/fundamental_events_status.json`，缺失、非法 JSON、`status=error` 或 `errors>0` 都必须阻断后续 snapshot 构造。

## 4. Sandbox 与 Runner

### 4.1 Sandbox 要求

正式实验默认使用 Docker Sandbox。CLI 只有显式传 `--local-dev` 时才使用本地执行器；本地模式只用于开发和单元测试，不作为正式安全边界。

Sandbox 要求：

| 项目 | 要求 |
|---|---|
| 用户 | Agent 代码以非 root `agent` 用户执行 |
| 网络 | 普通 Fold 默认 `--network none`；元学习默认 `bridge` 直连互联网，可由实验配置改为 `none` 或 `host` |
| Python | Docker 镜像内 Python 3.11，依赖由 `ops/docker/sandbox.Dockerfile` 固定 |
| 本机环境 | 本机脚本、测试和 cron 使用 `~/miniconda3/envs/quant`，与 Docker Python 独立 |
| 包安装 | 普通 Fold 不安装新包，Shell 工具会拦截常见安装/下载命令；元学习可在开放网络时把实验依赖安装到 `/mnt/agent/workspace` 用户目录 |
| 环境事实源 | `/mnt/artifacts/runtime_env.json` 记录 Python 包、CLI 工具、网络/安装策略和资源摘要 |
| 命令行工具 | 镜像内预装 `rg`、`git`、`pip`、`npm`、`hf`/`huggingface-cli`、`duckdb`（CLI，与 Python 包同版本）和基础 Unix 工具；docker 模式 `runtime_env.json.tools` 按 Dockerfile 合同声明这些工具，故新增 CLI 必须先装进镜像再登记 |
| 编译工具链 | 基础镜像预装 `build-essential`/g++/gfortran/python3-dev，使源码编译的 wheel（如 `torch_scatter`/`torch_sparse`）无需声明 `apt_packages` 即可构建，消除“缺编译器”的构建失败类 |
| 运行时模块 | 镜像把可信宿主侧运行时模块烤入 `/opt/at_runtime/`：常驻 `main(ctx)` 驱动 `main_ctx_driver.py` 与它共享的依赖轻量 `broker_core.py`（盘中成交投影=宿主 `SimBroker` 同一撮合算法）。基础镜像构建上下文为仓库根（`docker build -f ops/docker/sandbox.Dockerfile .`）以便 COPY 这两个文件；`/opt/at_runtime` 路径须与 `executor.CONTAINER_RUNTIME_DIR` 一致。改动这两个模块需重建基础镜像 |
| 工具缓存 | pip/HF/torch/CUDA 等缓存经容器环境变量（`XDG_CACHE_HOME`/`PIP_CACHE_DIR`/`HF_HOME`/`CUDA_CACHE_PATH` 等）重定向到 `/tmp`，不落在被采集的 `/mnt/agent`，避免 root 拥有的缓存目录令 `collect_artifacts` 失败 |
| GPU/资源 | 分配结果和资源限制写入 run manifest |
| 写入面 | 仅 `/mnt/agent/workspace`、未锁定的 `/mnt/agent/output` 和未锁定的 `/mnt/agent/models` |
| 可信产物 | `/mnt/artifacts` 由 Environment 写，Agent 只读 |
| Fold 时间 | 默认 60 分钟；Runner 接近 deadline 时最多发一次收尾提示 |

rootless Docker 下，容器内 `agent` 用户映射为宿主 subuid。Agent 可写目录在宿主侧按运行需要放行；只读与不可见约束仍由只读挂载、只读文件和测试槽权限承担。

元学习网络默认使用 Docker `bridge` 直连互联网。GitHub/HuggingFace token 只通过环境变量名传递；CLI 会从仓库根目录 `.env` 选择性加载本次允许透传的变量名（如 `GITHUB_TOKEN`、`HF_TOKEN`），但不会打印或写入变量值。常见配置：

```bash
scripts/experiments/run_experiment.py ... \
  --meta-learning-network bridge \
  --meta-learning-env HF_TOKEN \
  --meta-learning-env GITHUB_TOKEN
```

代理默认关闭；需要通过宿主 XRay/代理端口访问时，再显式开启 host proxy 选项：

```bash
scripts/experiments/run_experiment.py ... \
  --meta-learning-network bridge \
  --meta-learning-host-proxy
```

若使用本机 XRay/代理客户端，先在宿主导入配置并启动代理端口，再把宿主 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 或 `NO_PROXY` 配好。`--meta-learning-host-proxy` 不把这些标准变量直接注入容器，而是映射为非标准别名 `AT_PROXY_HTTP`、`AT_PROXY_HTTPS`、`AT_PROXY_ALL`、`AT_PROXY_NO_PROXY`，因此 Agent 默认仍然直连。若 direct GitHub/HuggingFace/PyPI/npm 访问卡顿或失败，Agent 可在单条 shell 命令前临时启用代理，例如 `HTTPS_PROXY="$AT_PROXY_HTTPS" ALL_PROXY="$AT_PROXY_ALL" hf download ...`。`bridge` 网络下本地回环代理地址会改写为 `host.docker.internal`；`--meta-learning-host-proxy` 会自动添加 host gateway。仓库、manifest、trace 和 system prompt 只记录环境变量名、网络模式和使用边界，不记录 token、订阅链接或代理凭据。GitHub token 由用户用 GitHub/`gh auth` 创建；流程不会自动生成第三方 token。

### 4.2 运行产物

运行产物路径：

```text
/mnt/artifacts/
  run_manifest.json
  runtime_env.json
  data_summary.json
  agent_trace.jsonl
  parent_output/
  parent_models/
  results/
  steps/
  logs/

/mnt/agent/
  workspace/
  output/
  models/
```

约束：

- `/mnt/agent/workspace/` 是临时探索区，不冻结、不回放、不复制到下一 Fold。会话结束后宿主 `collect_artifacts` 归档 workspace/output/models 时会跳过 `.cache`、`__pycache__` 等临时缓存与工具目录：它们不是实验产物，且常由容器用户以受限权限写出（如 pip 0600 缓存），宿主采集用户无法读取，归档它们既错误又会导致拷贝失败。
- `/mnt/agent/output/` 是正式策略代码写入面，根目录固定 `main.py`，可包含受控文本/代码子目录。
- `/mnt/agent/models/` 是正式模型参数写入面，可包含受控模型参数子目录。新增 Python/npm/apt 依赖属于 Sandbox 镜像层，不写入 `models/`。
- `/mnt/agent/` 根目录不是写入面；临时文件、缓存和下载内容应放入 `workspace/`，正式产物分别放入 `output/` 或 `models/`。
- `/mnt/artifacts/parent_output/` 是父产物基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/parent_models/` 是父模型参数基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/runtime_env.json` 是 Sandbox 运行环境契约，记录 Python 包、CLI 工具、网络和包安装策略；Agent 可读，正式策略代码不得硬编码读取。
- `/mnt/artifacts/data_summary.json` 是 Agent 可见轻量数据索引，记录各可见 view 的文件规模、行数、日期覆盖和大表访问提示；只有主决策视图 `snapshot` 给出关键列与关键列空值计数，`train`/`valid` 只给规模与日期覆盖（schema 与 `snapshot` 一致）。它以紧凑 JSON（不缩进）写出，可单次 `cat` 读取且 token 占用低；Agent 工具可读，正式策略代码不得硬编码读取。需要完整 schema、空值或更细字段时，Agent 应按需查询 snapshot manifest、Parquet metadata 或 DuckDB。
- Runner 在系统提示词中渲染 `当前实验事实`，只抽取上述 JSON 的常用运行事实：身份、可见性、窗口、预算、路径、产物合同、数据摘要、Broker/replay 和 runtime 工具能力。该事实块不渲染 `test_period`、`test_decision_time`、held-out 起止、下一 Fold 排程或测试 snapshot hash；这些字段即使在宿主账本或完整 manifest 中用于审计，也不能作为 Agent 首屏 Prompt 的交易证据。
- `/mnt/artifacts/results/` 由 Tool 写入，Agent 只读可见验证结果。
- 符号链接、隐藏文件/目录、缓存文件、日志、数据 dump、notebook、密钥和不支持后缀不能进入冻结 artifact。模型权重只能进入 `models/`，不能进入 `output/`。

### 4.3 Runner 责任

Runner 负责：

- 创建和锁定运行目录。
- 写入 run manifest。
- 启动 Agent 会话和 Tool 调用。
- 切换 `/mnt/snapshot`。
- 在正式策略执行期间隐藏阶段槽。
- 记录 Agent、Shell、Tool、Broker、NL 和错误摘要。
- 当主对话上下文过长时，按配置触发语义 compact；默认估算上下文达到 200,000 tokens 后触发。compact 会把最近一次 summary 作为锚点，只合并新增消息，输出目标、约束、进度、关键决策、错误修复、下一步和相关文件，并保留最近原始消息。
- 在 deadline 后停止新的 Shell、服务调用和 LLM 调用。

compact 调用必须遵守 Fold deadline：只使用预留给 compact 的时间片，结束后重新计算剩余时间；若 deadline 已到，不再启动新的主对话 LLM 调用。compact 失败只写 trace 并进入失败熔断，不应中断 Fold。

所有入口的路径、时间、Fold 信息和配置必须来自 run manifest。Agent 不能通过参数传入绝对路径、未来时间、外部网络地址或越权文件。

## 5. 执行入口和可信 Tool

### 5.1 工具列表

Agent 可用入口：

| 工具 | 作用 | 关键边界 |
|---|---|---|
| `grep` / `glob` | 结构化只读检索可见目录 | 不能写入，不能访问测试或隐藏路径 |
| `read` | 按行号读取文件（可分页）；读要编辑的代码优先于 shell `cat`/`head` | 只读；不访问测试或隐藏路径 |
| `sandbox_shell_tool` | 在 Sandbox 内读数据、写 `workspace`、`output` 或 `models`；元学习开放网络时可运行 `git`/`pip`/`npm`/`hf` | 不是宿主 shell；普通 Fold 拦截安装/下载/联网入口；可用 `max_output_chars` 和 `timeout_seconds` 主动缩小内联输出和单次运行时间；长输出落盘并记录路径 |
| `write_file` / `edit_file` | 在 `workspace`/`output`/`models` 下创建/覆盖或精确编辑文本产物 | 只写受控根；`edit_file` 的 `old_string` 必须唯一匹配（staleness 检查）；`output/README.md` 只读；写锁后拒绝 |
| `explore` | 委托数据探查 Sub Agent（更便宜模型）调查具体问题并返回摘要 | 按只读约定使用 `shell`/`grep`/`glob`（不写正式产物，改动由 modification_check/冻结 hash 兜底）；只回结论、证据、风险与限制、建议下一步，原始过程进 trace |
| `web_search_tool` | 元学习联网检索 | 仅元学习可用；每次调用声明 engine、perspective、query 和 max_results；结果写 trace |
| `modification_check_tool` | 校验正式 `output` 修改量、`models` 格式/大小和父产物 hash | 无业务参数；不检查 `workspace` 或结果目录 |
| `backtest_tool` | 执行 `output/main.py` 并回放交易 | 消费并校验当前 snapshot；每次调用创建唯一结果目录 |
| `finish_fold_tool` | 当前 Fold 停止修改 | 无业务参数；轻量合同检查后锁定写入 |

所有 Tool trace 都记录当前 `tool_spec` 的 `schema_version` 和 `result_policy`。`sandbox_shell_tool` 额外记录 `command_kind`，取值如 `read`、`list`、`search`、`write`、`install`、`network` 或 `unknown`；该字段用于审计和后续统计，权限判断仍由 Sandbox、路径 guard 和阶段策略执行。

工具调用采用 provider 原生 function calling：每个工具的名称和参数 JSON Schema 由 `ActionSpec` 生成并随请求下发，模型返回结构化 `tool_calls`，Runner 再按 `ActionSpec` 做硬校验后分发，不再要求模型把动作序列化成一段 JSON 文本。系统 Prompt 保留工具表和关键边界，具体参数语义、输出预算、分页方式、重试提示和失败原因尽量下沉到工具 schema、字段 description 和 `ToolError` 的 `error_type` / `reason` / `retry_hint`。一轮可以包含多个 `tool_calls`，每个都会单独返回一条 `tool` 结果；互相独立的只读工具（`concurrency_safe`，如 grep/glob/web_search）可在同一轮并行执行，有状态工具（write_file/edit_file/shell/explore/modification_check/backtest/finish_fold）按因果顺序串行执行。`done`/`finish_fold` 这类终止工具执行后，同一轮后续工具会被取消，避免终止验收后继续修改。Runner 的历史裁剪和上下文压缩保证 `tool` 结果不会脱离其 `assistant` 工具调用。NL Sub Agent 和 Explore Sub Agent 复用同一原生工具循环；Explore 按只读约定调用 shell/grep/glob，并继承 Fold deadline，只回答委托问题，不替主 Agent 做最终策略综合。Explore 单轮被 `finish_reason=length` 截断或遇瞬时 provider 错误时不会让整个探查失败，而是停止循环并强制一次简洁的最终摘要；其 `max_tokens` 留有容纳长工具调用（如 DuckDB SQL）与摘要的余量。

长 `reasoning_effort` 轮次默认请求 SSE 流式响应，并在客户端合并 tool-call delta 为统一完成结果。上下文管理分三层并依次升级：原地清理超大旧 `tool` 结果（context editing，保留 `tool_call_id`）、确定性 `_trim` 摘要、低成本模型语义压缩。三层都以估算 prompt token 为主触发阈值，消息条数只作为高位安全上限，避免在仍然很小的上下文上频繁改写前缀；因为裁剪/压缩会重置 DeepSeek 自动前缀缓存。主对话按 prompt/completion/reasoning 以及缓存命中/未命中累计 token，写入 session 摘要的 `token_usage`（含 `cache_hit_ratio`），可据此权衡裁剪/压缩强度。

### 5.2 修改检查和锁定

`modification_check_tool` 固定读取：

- 只读父产物 `/mnt/artifacts/parent_output/`。
- 只读父模型参数 `/mnt/artifacts/parent_models/`。
- 当前工作副本 `/mnt/agent/output/`。
- 当前模型参数 `/mnt/agent/models/`。
- run manifest 中的父产物 hash、初始模板 hash 和约束；manifest 不暴露宿主模板目录。

检查项：

- 父产物 hash。
- 当前策略 artifact hash、模型 artifact hash 和 combined hash。
- 文件数、总 diff 行数、Python diff 行数和总字节数。
- README/只读文件修改。
- 非法文件、隐藏文件/目录、缓存和不支持后缀。
- 模型参数文件数、总字节数、非法后缀、隐藏文件/目录和缓存。

产物变更后必须重新检查。失败时只能缩小正式修改后重试。

`finish_fold_tool` 成功后，Runner 锁定 `output/` 和 `models/` 写入，并要求 Pipeline 复核最近一次修改检查、验证结果和当前策略/model hash 是否一致。

## 6. 策略执行和 NL 服务

### 6.1 正式流程

`backtest_tool` 的正式流程：

1. 校验当前 `output` hash 和 `models` hash 与最近一次 modification check 一致。
2. 校验 `/mnt/snapshot` 与 run manifest 中的决策输入一致。
3. 创建唯一 `results/<phase>_<idx>/`。
4. 固定 `AT_SNAPSHOT_DIR=/mnt/snapshot`、`AT_AGENT_OUTPUT_DIR=/mnt/agent/output`、`AT_MODEL_DIR=/mnt/agent/models`、`AT_STATE_DIR=/mnt/agent/workspace/.state`（配套 `AT_STATE_STAGING_DIR=/mnt/agent/workspace/.state_staging` 暂存子步骤写入），在 Sandbox 启动一个常驻 `main(ctx)` 进程。该进程是镜像内 `/opt/at_runtime/main_ctx_driver.py` 这一真实模块（按文件加载，非 `python -c` 字符串），它 import 同目录的依赖轻量 `broker_core` 做盘中成交投影；二者随镜像构建烤入 `/opt/at_runtime`（见镜像合同）。
5. 按回放 tick 逐 tick 构造市场级 `ctx` 并调用一次 `main(ctx)`（盘中 1 分钟 tick，盘外按 `offsession_tick_minutes`（默认 15 分钟）spacing）；`main` 通过 `ctx.broker` 的 `ts_code` 原语下单。
6. 若 `main` 在决策时调用 `ctx.nl()`，通过 JSONL 文件 RPC 请求宿主侧 NL 服务（宿主在等待 `main` 返回时同时服务 NL 请求）。
7. 收集本 tick `main` 发出的 Broker 原语调用，宿主 Broker 按延迟进入订单簿，逐 bar 撮合并强制约束。
8. 按 tick 推进直到回放区间末日强制清仓。
9. 写结果（`detailed_return.json`、`orders.parquet`）、Broker 事件、NL 工具日志、策略/model hash 和 manifest 摘要。

临时 Python 回测、Shell 中的手工脚本和 notebook 只能作为探索，不构成正式 valid/test/held-out 结果。正式结果只能由 `backtest_tool` 写入。

Shell guard 是轻量合同层，不是完整 Bash 解析器。它提前拒绝明确越界路径、明确写只读根、写未管理目录和普通 Fold 安装/下载入口，并在失败 observation 中返回 `error_type`、`reason`、`retry_hint` 和可能的 `blocked_target`。守卫只检查 shell 骨架：扫描前先剥除 heredoc 正文（仅保留含真实重定向的起始行），并对路径正则屏蔽引号内内容，因此 `python3 -c "..."`、`python3 << 'EOF' ... EOF` 等解释器代码里的比较/切片（如 `> 150`、`[:5]`）不会被误判为重定向或越界路径；解释器内部的真实写入由 Docker 只读挂载与产物检查兜底，不靠静态解析。只读列目录、`os.listdir('/mnt')`、读取 `/mnt/artifacts` 或把只读文件复制到 `/mnt/agent/workspace` 属于允许探查。Explore Sub Agent 与主 Agent 共用同一 shell guard，并按只读约定只做数据探查（可用 DuckDB/python 等读取分析 parquet）；不再单建“只读 shell 静态解析”——它既无法可靠枚举所有写入向量（如 sed `w`、命令替换、解释器内写文件），又会拦截探查必需的解释器。Explore 对 output/models 的任何改动与主 Agent 一样，由 modification_check、冻结 hash 和 Docker 只读挂载兜底。更复杂的 shell 细节由 Docker 只读挂载、目录权限、无网络配置和后续产物检查兜底。Prompt 与工具 schema 都要求 Agent 不使用 `2>/dev/null` 隐藏错误（命中该模式时 shell 结果附带一条非阻断的 `stderr_suppression_reminder`），stderr 应原样进入 trace。超时清理在容器内执行：每条定时 shell 命令在容器内 `timeout` 下运行（其进程组在超时即被整组杀掉），宿主 `docker exec` 的截止时间只作更长的兜底；容器以 `--init` 启动由 tini 回收孤儿/僵尸，避免被杀的训练子进程残留并占满 `--pids-limit`。

### 6.2 Valid 与 Frozen Eval

`valid` 与 `frozen_eval`：

| 模式 | 策略输入 | 回放区间 | 结果目录 | Agent 可见性 |
|---|---|---|---|---|
| `valid` | 验证决策输入 `/mnt/snapshot` | `/mnt/snapshots/valid` | `results/valid_<idx>/` | Agent 可读 |
| `frozen_eval` | 测试或 held-out 决策输入 `/mnt/snapshot` | `/mnt/snapshots/test` | `results/test_<idx>/` 或 `heldout_<idx>/` | 不反馈给 Agent |

### 6.3 NL 服务

策略代码可写：

```python
from at_tools import nl
result = nl(ts_code, prompt="...")
content = result.get("content", "")
```

Sandbox 内的 `nl()` 只写请求并等待响应。宿主 Environment 使用：

- `TextRetriever` 读取 `text_index.parquet` 和 `text_library/`。
- `build_company_contexts()` 构造公司上下文。
- `NLSubAgentEngine` 启动一个可调用 `text_retrieve` 的宿主侧 Sub Agent，并调用宿主 `LLMProxy`。

NL Sub Agent 的最终回答不限定格式；只有它请求 `text_retrieve` 时使用约定 JSON 工具调用。Sandbox 只收到 result dict，常用字段为 `status`、`content`、`tool_calls`、`evidence` 和 `error`。策略若需要数值分、风险标签或交易过滤条件，必须在 Agent 代码中自行解析 `content`。

NL 结果写入：

```text
results/<phase>_<idx>/nl_tool/
  nl_requests.jsonl
  search_requests.jsonl
  evidence.jsonl
  nl_llm_calls.jsonl
```

NL evidence 必须来自 as-of `text_id` 或 `source_hash`。没有可见证据时，Sub Agent 必须说明证据不足；Agent 策略自行决定忽略、降权、重试或不交易，不能伪造引用。NL 结果还需要防范发布时间/入库时间误差、检索召回偏差、模型常识污染、自由文本解析不稳定和前视泄露，不能让 NL 结论覆盖 Broker 约束、交易成本或 PIT 可见性规则。

**NL 调用配额（成本，与延迟分开）。** 每次回测的 `ctx.nl()` 总配额默认按 `nl_max_calls_per_decision_day`（系统设定的日均上限）× 决策天数计算（一个日均预算），可由可选 `nl_max_calls_per_backtest` 进一步收紧（取 min）。超出后 NL 服务向策略返回 `budget_exhausted` 错误，策略需自行降级。该配额只约束调用次数（成本）；调用耗时由 §7.2 的声明式延迟预算（`ctx.substep`）单独约束。

## 7. Broker、回放和做空规则

### 7.1 Broker 基础原语

Broker 不内置任何交易策略，只暴露按股数操作的基础原语和查询接口；交易策略由 Agent 在 `output` 中以函数实现，并在回放时调用这些原语。Agent 不能直接写成交、持仓或收益。

`main(ctx)` 内可用的 `ctx.broker` 接口（均以 `ts_code` 为第一参数）：

| 接口 | 作用 |
|---|---|
| `buy(ts_code, amount\|weight, limit=None, valid_bars=1)` | 多头买入（建仓或加仓）；`limit` 为限价单 |
| `sell(ts_code, amount, limit=None, valid_bars=1)` | 卖出多头可卖（T+1）份额 |
| `short(ts_code, amount\|weight, limit=None, valid_bars=1)` | 融券开空 |
| `cover(ts_code, amount, limit=None, valid_bars=1)` | 买券还券（平空可平份额） |
| `close(ts_code)` | 平掉该股可平持仓（恒市价） |
| `position(ts_code)` / `pending(ts_code)` | 该股有符号持仓股数 / 在途已报未成单 |
| `money` / `cash` | 当前现金视图（盘中按成交投影更新） |
| `available_cash` | 当前可部署买力（现金扣融券保证金与冻结所得） |
| `account` / `positions` | 当前账户和持仓快照 |

盘中投影口径（R16）：`ctx.broker` 的 `cash`/`available_cash`/`position` 在同一 tick 内随每笔下单用与宿主 `SimBroker` 完全相同的 `broker_core` 撮合算法（佣金、滑点、整手、融券保证金、冻结所得）更新——因此同一 tick 的第二笔单按已扣减首单成本后的真实买力定量，宿主会因资金不足拒绝的单在投影里也不改变视图；这消除了旧“乐观视图”与真实成交的盘中背离。

这些便捷封装是 ergonomic sugar。底层 `SimBroker` 的接口与实盘 xtquant 1:1 对齐，便于 live 适配器（`QMTBroker` 封装 `xt_trader`）做机械映射，两者都满足 `TraderProtocol`：

| `SimBroker` | xtquant |
|---|---|
| `order_stock(order_type, stock_code, order_volume, price_type, price, …) -> order_id` | `order_stock(...)` |
| `cancel_order_stock(order_id)` | `cancel_order_stock` |
| `query_stock_orders(cancelable_only)` | `query_stock_orders` |
| `query_stock_trades(ts_code=None)` | `query_stock_trades` |
| `query_stock_positions()` / `query_stock_asset()` | `query_stock_positions` / `query_stock_asset` |

`order_type` 取 `xtconstant` 值：`STOCK_BUY`/`STOCK_SELL`/`CREDIT_SLO_SELL`（开空）/`CREDIT_BUY_SECU_REPAY`（平空），`close` 为约定的市价平仓；`price_type` 为 `FIX_PRICE`（限价）或 `MARKET_PEER_PRICE_FIRST`（市价）。引擎按决策 + `execution_lag_bars` 把订单 `order_stock` 进簿，逐 bar `match_bar` 撮合。

`amount` 是股数（按 100 股，即 1 手，向下对齐），`weight` 是初始权益的名义比例。所有拒单、部分成交、T+1 阻挡和强制平仓事件必须记录。

### 7.2 main(ctx) 与逐 tick 回放

交易逻辑全部由 Agent 定义。Environment 在 Sandbox 中启动一个常驻 `main(ctx)` 进程，按回放 tick 逐 tick 构造市场级 `ctx` 并调用一次 `main(ctx)`（一次覆盖全市场，而非每只股票一次）。回放是覆盖全天的 24h tick 网格：交易时段内按真实 1 分钟 bar 逐 tick 推进，时段外按 `offsession_tick_minutes`（默认 15 分钟）spacing 继续调用 `main(ctx)`（仅研究/状态用途，盘外 tick 不下单），同一循环既驱动回测也驱动实盘。`main` 自己决定时序：每个 tick 管理已有持仓、在选定时点筛选并开新仓，因此可以在任意 tick 开/平仓。`ctx` 暴露的字段清单（`broker` 原语、行情、时间/账户、`nl`、`substep`、`asof_dir`/`asof_version`/`snapshot_dir`/`model_dir`/`state_dir` 等）见 `agent_design.md` §5.3；本节定义其中由 Environment 决定的执行、延迟与可见性语义。

`ctx.asof_dir` 是逐 tick 滚动的 Timeview：它把六个数据域——`daily`、`events`、`macro`、`fundamentals`、Agent 可读的 `intraday_1min` 分钟历史，以及宿主侧经 `ctx.nl()` 可见的 NL 文本库——按各自本地库刷新任务的真实入库节奏回放，并以仿真时钟（`ctx.cur_datetime`，Asia/Shanghai）驱动可见性。每个域只在落它的 cron 任务按仿真时钟已跑完后才可见：可见性节点是 `data/contracts.py` 的 `REFRESH_NODES`，镜像 `configs/tushare_update_schedule.json` 的实际 crontab（`ready_at = start + duration_minutes`）。`daily`/`macro`/`intraday_1min`/多数 `events` 跟随 `cn_evening_full`（23:35 启动、约次日 02:05 完成），所以盘中横截面只到 D-1，当日实时行情走 `ctx.bars`/`ctx.price`、不进持久化视图；`margin_secs` 在盘前节点后当日可见（约 09:05/09:15）；`fundamentals` 在夜间 PIT 构建后可见（约 03:50）；`cctv_news`/`news` 在盘前文本回填后可见（约 09:00）。

每个域是一个普通 parquet parts 目录，用 `pd.read_parquet(ctx.asof_dir / "<域名>")` 读出拼接：part 0 是该域的冻结研究快照（硬链入，零拷贝），后续 parts 是 write-once 的回放增量，只在仿真时钟跨过覆盖该域的节点时追加；因此 09:20→次日 02:05 一整段没有节点完成，视图被冻结、零重建。`ctx.asof_version` 在视图滚动时自增，策略可缓存一次读取、仅在版本变化时重算。`ctx.snapshot_dir` 仍是冻结的研究基线。视图开关是 `timeview_enabled`（默认开）。

`ctx.nl()` 文本同模型滚动：公告/新闻只在其刷新节点完成后可见，冻结研究语料始终可见；约 1.6GB 文本库零拷贝就地读取，按查询时的 `available_at` 门控。

启用 `auction_enabled`（默认开）时，每个回放日插入盘前与收盘集合竞价决策 tick。两个盘前 tick 排在常规分钟 tick 之前：`09:15`（`auction_preopen_time`）信息 tick——集合竞价尚未撮合，`ctx.price` 为 None，用于筛选与 NL；`09:25`（`auction_decision_time`）tick——暴露撮合出的开盘价（不含日内最高/最低/成交量）。两者下的单按次一根 bar 成交：`09:15` 的单成交于 09:30 开盘集合竞价，`09:25` 的单成交于首根连续 bar（09:31）。`14:57`（`auction_close_time`，默认 `"14:57"`）是收盘集合竞价决策 tick：其下的单成交于当日最后一根 15:00 bar 的收盘价（close 印记，对应 15:00 收盘集合竞价）。Broker 按当日涨跌停规则成交（单边一字涨停开盘的买单、跌停的空单被拒）。只有真正的集合竞价成交（`09:15` 盲下单成于 09:30 开盘竞价、`14:57` 成于 15:00 收盘竞价）按单一竞价价清算、**不计滑点**，取竞价侧价格（开盘用 `open`、收盘用 `close`）、`price_label="auction"`；`09:25` 的单成于首根连续 bar（09:31），属连续撮合，**按 taker 滑点成交**、`price_label` 为该连续 bar（如 `minute:09:31`）。

`main` 是决策阶段，可用模型参数（`ctx.model_dir`）、滚动 PIT 视图（`ctx.asof_dir`/`ctx.snapshot_dir`）和 NL（`ctx.nl`）；重操作只应在少数选定时点执行，不要每分钟跑。跨 tick 暂存写入受宿主托管的可见目录 `ctx.state_dir`（Broker 仍是持仓真相源）：在 `ctx.substep(name, B)` 内经 `ctx.state_dir` 的写入被暂存，仅在 `ready_at = 生成 tick + B`（建模重计算块产出可用前的时延）才并入可见目录，后生成者在冲突时胜出；substep 外的写入立即落地，读取始终看可见目录。该机制按路径实现——`ctx.state_dir` 在 substep 内解析到隐藏暂存目录——因此能捕获任意写入方式（含 pandas/pyarrow parquet 的原生写），这也是它不走路径 guard 的原因。可见目录与暂存目录每次回测都清空重建（可复现；需跨回测持久的数据应放 `models/`）。进入 `ctx.substep` 时宿主会把当前可见状态拷贝进暂存目录作种子（保证块内读取仍看旧可见值），故 `ctx.state_dir` 只适合小体量跨 tick 状态：高频 substep × 大 `state_dir` 会付出 O(state) 的逐次拷贝开销，大数据应放 `models/` 或只在少数 tick 重算。仿真时钟（`ctx.cur_datetime`，Asia/Shanghai）统一驱动域可见性、暂存 `ready_at` 与成交映射，同一 `main(ctx)` 循环也驱动实盘。回放进程只读加载 `output/` 中的策略代码，禁止写 `output/`、创建软/硬链接，且按真实路径阻断经链接访问测试槽或 `/mnt/artifacts`。

`main` 每个 tick 发出的原语对齐实盘 QMT `order_stock`（QMT 无券商侧条件单/止损单，故不引入引擎侧触发单）。每日维护一个订单簿：决策在某根 bar，订单于其后第 `execution_lag_bars`（默认 2，经 manifest 配置）根 bar 起进入撮合，杜绝 bar 内前视（`1`=紧邻下一根，`2`=一根算/报单延迟 + 下一根成交；如 09:35 决策、09:37 起成交）。两类报价：

- **市价单**（默认，对应 `MARKET_PEER_PRICE_FIRST`）：在进入 bar 按 `open` + 滑点成交，单 bar 有效。
- **限价单**（`limit=P`，对应 `FIX_PRICE`）：挂单，自进入 bar 起最多 `valid_bars` 根 bar，无滑点；买/补在 `open<=P` 时按 open 成交，否则 `low<=P` 时按 P 成交；卖/空在 `open>=P` 时按 open 成交，否则 `high>=P` 时按 P 成交；窗口内未触及则自动撤单（对应 `cancel_order_stock`，记 `order_cancelled`）。`close` 恒市价。

**决策延迟与重计算可见性（Agent 声明式预算）。** 策略可用 `with ctx.substep(name, budget_minutes=B):` 包裹一段重决策（横截面筛选、模型推理、NL），声明该块的计算时长 `B>0`（分钟）。`B` 有两个作用：（1）**实测墙钟 fail-fast**——一旦该 substep 真实墙钟超过 `B·60s`，立即抛 `BacktestError` 中止本次回测，并向 `backtest` 工具返回精确失败（substep 名、日期、声明 `B` vs 实测），故低报（声称快却跑得慢）硬报错、不可利用；（2）**`ctx.state_dir` 写可见性门控**——块内经 `ctx.state_dir` 的写入在 `ready_at = 决策 tick + B` 才并入可见目录。`B` **不**改变订单成交 bar：无论 `B` 多大，订单都按常规 `execution_lag_bars` 进簿成交（盘前竞价的成交 bar 同样固定、不受 `B` 影响）。`B` 受 `decision_max_sim_minutes` 上限约束：超过即在 `ctx.substep` 初始化被拒（`ValueError`）；`B=0` 等同于不包裹、被拒；同一 tick 内 substep 名必须唯一、重名被拒，以保证预算到块的映射无歧义；轻量决策用较小的 `B`（如 0.5–1）。NL 由宿主串行服务（`_serve_nl_requests`），substep 真实墙钟约等于其中各 NL 时延之和，Agent 据此设定 `B`。该模型是**协作式**的：fail-fast 只保证“已声明的 substep 不会低报”，并不强制包裹每段重决策；未包裹逻辑无逐块声明上限（其真实墙钟仍受下文 `backtest_max_seconds_per_decision` / `backtest_max_seconds_per_trading_day` 硬上限兜底，但这些不改变成交 bar）。`ctx.substep` 是执行无关的，live QMT 控制器可把各 substep 作为真实异步任务运行、并以同样的预算超限作为错误。

**回测独立计时、成本上限与可观测性。** `backtest` 作为独立计时工具，其墙钟时间不计入 Fold 推理 deadline（`runner.py` 把回测耗时回补到 deadline），但单个 Fold 最多 `max_backtests_per_fold` 次回测，超出返回 `backtest budget exhausted`。两道随回放天数伸缩的真实墙钟硬上限（替代固定总上限）：单个决策（一次 `main(ctx)` tick，含其内 NL）超过 `backtest_max_seconds_per_decision` 由 `MainPolicyRunner` **立即杀掉**驱动并抛 `BacktestError`（该 tick 的硬截止，不再因 NL 重置）；某交易日累计 `main(ctx)` 计算超过 `backtest_max_seconds_per_trading_day` 在引擎层中止回放。完整验证触发任一上限即不可接受/冻结，迫使 Agent 缓存重计算、压低调仓/图构建成本。**这两道紧上限是真实墙钟、随机器负载浮动，故仅约束 Agent 迭代的 `mode="valid"` 验证回测**；最终评估（每个 Fold 的冻结 `test_000` 与 held-out，均 `mode="frozen_eval"`）改用更宽松的防挂死兜底 `backtest_final_eval_max_seconds_per_decision` / `backtest_final_eval_max_seconds_per_trading_day`（默认 900s / 3000s）：已在验证阶段满足紧上限的策略必须能跑完其最终评估，accept/held-out 结果不应因负载浮动而不可复现。该兜底只为杀掉真正的挂死——基于仿真时间的预算无法做到（单 tick 内死循环消耗 0 仿真分钟却占用无限墙钟，只有墙钟兜底能拦），且不作为接受门槛。**有意不设固定总上限**：单次回放的总耗时上界即 `交易日数 × backtest_max_seconds_per_trading_day`，随回放长度自然伸缩，无需另设一个不随长度变化的总墙钟上限。Environment 不预测回放耗时；Agent 用小 `replay_window` 试探得到的 `replay_wall_seconds` / `replayed_trade_days` 自行外推完整运行成本。回测可观测性：开始时发 `backtest_start`、回放期间按节流（≥30 天或≥30 秒）发 `backtest_progress` 心跳（含进度/已用时/累计订单数）、结束/中止保证有一条终止 `backtest` 事件（含外部中止的 `status="aborted"`）；summary 另含 `started_at`、`replay_wall_seconds`、`replayed_trade_days`、逐 substep 的 `substep_runtime`（count/total_real_wall_s/max_real_wall_s）、按阶段拆分的 `phase_seconds`（`strategy_compute`/`nl_service`/`timeview_build`/`state_merge`/`broker_match`）以及 `total_ticks`/`intraday_ticks`/`offsession_ticks` 计数，使 24h tick 网格的额外成本可审计。

执行与资源预算一览（默认值见 `pipelines/config.py`，逐项写入 run manifest）：

| 控制项 | 默认 | 约束对象 |
|---|---|---|
| `per_call_timeout_seconds` | 300s | Agent 主 LLM 调用与 contract_check 校验的单次超时（不约束正式回放 tick——回放 tick 见 `backtest_max_seconds_per_decision`） |
| `max_fold_minutes` / `fold_deadline_at` | 60min | Fold 推理墙钟（回测耗时已回补排除） |
| `max_backtests_per_fold` | 30 | 单 Fold 回测次数（独立计时豁免的上限） |
| `offsession_tick_minutes` | 15min | 盘外（09:15–15:00 交易时段外）tick 的分钟 spacing；`0` 关闭盘外 tick（盘外 tick 只更新研究/状态，不下单） |
| `auction_enabled` | True | 是否在每个回放日插入盘前/收盘集合竞价决策 tick（关闭则只跑连续竞价分钟 tick） |
| `auction_close_time` | "14:57" | 收盘集合竞价决策 tick 时点，其下单成交于当日 15:00 bar 收盘价；`None` 关闭 |
| substep `budget_minutes`（fail-fast） | Agent 声明（`B>0`，tick 内唯一） | substep 的真实墙钟上限 + `state_dir` 写可见性门控（不改变成交 bar） |
| `decision_max_sim_minutes` | 60min | 声明预算 `B` 的上限（超过在 substep 初始化即被拒） |
| `backtest_max_seconds_per_decision` | 180s | 单个决策（一次 `main(ctx)` tick，含 NL）的真实墙钟硬上限，超限立即杀（仅 `mode="valid"` 验证回测） |
| `backtest_max_seconds_per_trading_day` | 600s | 单交易日累计 `main(ctx)` 计算的真实墙钟硬上限，超限中止回放（仅 `mode="valid"` 验证回测） |
| `backtest_final_eval_max_seconds_per_decision` | 900s | 最终评估（`frozen_eval`：冻结 `test_000` 与 held-out）单决策的宽松防挂死兜底，非接受门槛 |
| `backtest_final_eval_max_seconds_per_trading_day` | 3000s | 最终评估单交易日累计计算的宽松防挂死兜底，非接受门槛 |
| `nl_max_calls_per_decision_day`×决策天数（min `nl_max_calls_per_backtest`） | 10/日 | 每次回测 `ctx.nl` 调用次数（成本，与延迟分开） |

宿主 Broker 据此执行下单、成交、拒单（现金/做空保证金/T+1/手数/涨跌停/停牌/券源）、约束和审计；隔离边界不变（策略只表达意图）。`ctx.positions` 只反映已成交持仓；在途（已报未成）单经 `ctx.broker.pending(ts_code)` 暴露（对应 `query_stock_orders(cancelable_only)`），策略据此对在途代码跳过重复下单。

分钟回放是默认口径：有非空 `intraday_1min.parquet` 时按真实分钟 bar 推进；缺失分钟数据的日期/股票，或缺少必要收盘分钟 bar 时，按日线合成 09:30/15:00 两根 bar 作为退化 fallback。`execution_lag_bars` 会按当日 bar 数收敛（`max(1, min(lag, n-1))`），使两根 bar 的退化日即便关闭盘前竞价也能在 15:00 成交、不至于整日零成交。盘前两 tick（09:15→09:30 开盘竞价、09:25→09:31 首根连续）与 14:57 收盘竞价 tick（成交于当日 15:00 bar 收盘）为批量集合竞价撮合，不受 `execution_lag_bars` 影响。回放区间最后一个交易日保留为剩余持仓的强制清仓日；临近收盘、其后无第 `execution_lag_bars` 根 bar 的决策无法成交，记 `main_actions_unfilled`；当日收盘仍挂着的限价单自动撤销。

### 7.3 回放 Profile

默认研究回放 profile 必须写入 run manifest：

| 项目 | 默认口径 |
|---|---|
| 初始本金 | run config 指定；未指定时使用研究默认值 |
| 佣金 | 按成交额 bps 计提，受最低佣金约束 |
| 印花税 | 按交易日期使用对应税率，只在卖出/开空相关方向计提 |
| 滑点 | bps 口径，买入上滑、卖出下滑 |
| 最大持仓数 | 默认不指定；由 Agent 在候选筛选、仓位和交易策略中自行控制 |
| 单票权重上限 | 默认不指定；由 Agent 在下单股数、权重和加减仓逻辑中自行控制 |
| 做空保证金 | 按 Broker profile 计提 |
| 维持担保比例 | 触发风险事件和强平审计 |
| 借券费 | 研究假设，按年化费率计提 |
| 空头公司行为 | 当前按研究假设处理，接入真实规则后需更新 |

集中度约束默认交给 Agent 策略决策。只有显式研究或实盘风控配置要求时，run config 才可设置 `broker_profile.max_total_holdings` 或 `broker_profile.max_single_name_weight`，并必须在 run manifest 写明来源。

### 7.4 Broker 强制约束

Broker 在每次原语调用时强制：

- 现金和做空保证金约束。
- A 股 lot size（100 股）、手续费（含最低佣金）、滑点和印花税。
- T+1 可卖余额：当日买入/开空份额当日不可卖出/还券，`sellable_quantity` 在交易日推进后释放，不足部分阻挡并记录。
- 停牌、涨跌停限制；做空券源、维持担保比例、借券费和强平事件。
- 如果 `broker_profile.max_total_holdings` 或 `broker_profile.max_single_name_weight` 被显式设置，Broker 会作为附加风控约束执行；默认 profile 不启用这两项限制。

### 7.5 做空模式

做空模式：

| 模式 | 规则 |
|---|---|
| `proxy_margin_secs` | 研究近似：可做空集合按成交日的真实 `margin_secs`（回放槽的逐日映射）门控，缺失该日时回退冻结决策日快照；不代表真实券源 |
| `broker_inventory` | 未来接入真实券源；应要求交易所融资融券资格、真实券源、数量覆盖和合约费率均可见 |
| `theoretical_short` | 研究模式，不检查券源；必须在 manifest 和结果中显式标记 |

默认 `proxy_margin_secs` 下，Broker 在成交那天判定可做空：成交前 `current_date` 推进到成交日，按 `shortable_by_date`（来自回放槽的逐 `trade_date` 集合）查该日真实 `margin_secs`，缺失则回退冻结的决策日 `shortable_codes`；这把 Agent 冻结的决策日快照与 Broker 的同日执行约束隔离开。不在该集合内的做空订单仍由 Broker 拒绝并记录 `margin_secs_not_shortable`。真实中信券源、费率和担保比例明细接入后需更新本节。

### 7.6 结果目录

每次正式回测写入：

```text
results/<phase>_<idx>/
  detailed_return.json
  orders.parquet            # 本次回放的全部 Broker 订单（成交/拒单）
  state_staging_audit.json  # 有 ctx.substep 暂存写入时：逐条 substep、ready_at、文件 hash、合并状态（区间结束仍未合并记 unmerged_at_region_end）
  nl_tool/                  # 策略调用 ctx.nl() 时有内容
```

`detailed_return.json` 至少包含总收益、long/short 收益、年化收益、Sharpe、最大回撤、胜率、turnover、订单状态、拒单统计、费用、借券费、权益曲线、逐笔平仓/减仓和 Broker 事件。回测 summary 另记 `state_staged_writes`/`state_unmerged_writes` 计数，以及 §7.2 的 `phase_seconds` 和 tick 计数。

## 8. LLM API 边界

### 8.1 Provider 边界

Agent 主对话、Runner context compact 和 NL 工具调用都只能经宿主 `LLMProxy`：

- Agent 主对话由 Runner 触发，记录到本地 conversation log。
- context compact 由 Runner 触发，默认使用低成本无 thinking 模型；它只生成继续会话所需摘要，不调用工具，不进入 Sandbox。
- NL 工具调用由宿主 NL 服务触发，记录到回测结果目录的 `nl_tool/`。
- DeepSeek 主对话和 NL 调用默认启用 thinking，并把 `reasoning_effort` 设为 `max`；这适用于普通 Fold Agent、Epoch 元学习 Agent 和 NL Sub Agent。实验 CLI 可显式用 `--reasoning-effort` 或 `--no-thinking` 做消融/调试覆盖。compact 默认关闭 thinking，因此不传 reasoning effort。
- 元学习 `web_search` 由宿主侧工具执行；可用引擎写入 manifest，Agent 在 action 中选择 `engine`，并用 `perspective` 标记金融/量化/经济、其他自然科学/工程、哲学/方法论三类研究视角。启用搜索时，Runner 要求三类视角各有一次非空成功检索后才允许 `done`。
- 元学习可由实验配置显式开放 Sandbox shell 网络做工作区内探索，并通过 `workspace/sandbox_environment.json` 申请后续 Fold 继承的依赖、由 Pipeline 构建派生镜像；该能力不替代 `web_search` 的三视角要求，也不开放给普通 Fold。网络/代理配置见 §4.1；请求 schema、构建烟测、镜像 tag 持久化与 GC 见 `pipeline_design.md` §6.1。
- Web Search provider 在宿主侧执行有限重试和限速；Semantic Scholar 使用每 key 共享的文件锁节流并对 429/5xx 做指数退避，避免单次短时限流直接结束元学习。
- API key、Authorization header 和 provider client 不进入 prompt、artifact 或日志。元学习 shell 需要用到的第三方 token 只允许通过显式列名的环境变量透传给容器，Environment 不记录变量值；trace 和大输出文件会对常见 OpenAI/HF/GitHub token、代理凭据和 VLESS 链接做脱敏。
- provider 超时不能无限阻塞 Fold；超时、重试和失败策略必须写入 trace。
- provider 返回的 reasoning 或内部思考只进入审计日志；正式结构化字段取最终 content。
- 测试和 held-out 的 LLM/NL 明细不反馈给 Agent。

### 8.2 调用日志

每次真实 provider 调用必须记录：

- `experiment_id`、`fold_id`、`run_id`、`conversation_id`、`call_id`。
- 调用来源：Agent 主会话、NL 工具、元学习或其他受控入口。
- 输入 messages / prompt。
- 原始 provider 响应。
- 模型、超时、耗时、token 或费用统计（如可用）。
- 错误、超时和修复策略。

`agent_trace.jsonl` 对主对话记录 `llm_call`，对 context compact 记录 `context_compaction`。每条 `llm_call` 只记录本轮首次出现的消息增量（`new_messages`）与 `message_count`，不再每轮重复嵌入整段历史；把各轮 `new_messages` 与该轮 `content`/`tool_calls` 顺序拼接即可还原完整对话，trace 体积随对话线性增长而非二次膨胀，完整 prompt 仍由 provider conversation log 承担。compact trace 至少包含 provider、model、触发 token 估算、调用次数、压缩前后消息数、summary hash、usage、状态和错误摘要。

## 9. 运行日志、审计和验收

### 9.1 核心文件

核心运行文件：

```text
/mnt/artifacts/run_manifest.json
/mnt/artifacts/agent_trace.jsonl
/mnt/artifacts/results/<phase>_<idx>/
/mnt/artifacts/logs/
experiments/<id>/artifacts/run_<id>/host_run_manifest.json  # 宿主审计副本，不挂载给 Agent
```

### 9.2 Manifest 和 Trace

Agent 可见的 `run_manifest.json` 至少记录：

- experiment、epoch、fold、run、conversation ID。
- 决策时点、训练/验证可见区间和 snapshot hash；测试和 held-out 调度不写入 Agent 可见 manifest。
- 父产物 ID/hash、当前 artifact hash、冻结标记。
- 父模型参数 hash、当前模型 artifact hash 和 combined hash。
- Broker profile、短券模式、成本参数和资源配置。
- runtime env 路径，以及普通 Fold 或元学习需要的实验参数摘要。
- 修改约束和 deadline。
- 关键结果目录和状态摘要。
- 元学习的 development 输入（`development_history`、`experiment_ledger_full`、`meta_learning_memory`）与 `taste_output` 一律写成 `/mnt/...` 沙箱挂载路径，不写宿主绝对路径，避免误导 Agent 去访问沙箱外不可见的位置。

宿主收集目录额外保留 `host_run_manifest.json`，用于完整审计测试/held-out 调度、测试 snapshot 和 frozen evaluation 结果；该文件不在 Sandbox 中挂载。

`agent_trace.jsonl` 是轻量事件流，包含 Shell、Tool、回测、Broker、LLM、context compact、NL、错误和锁定事件。事件共享 `experiment_id/fold_id/run_id/conversation_id/call_id/parent_call_id`，便于追溯。

### 9.3 读取权限

读取权限：

- Agent 在训练/验证期只读可见验证结果。
- 测试和 held-out 结果、日志、NL 明细和 Broker 事件不反馈给 Agent。
- 宿主可读完整审计目录。
- 冻结 artifact 的 `manifest.json` 是冻结元数据，不参与策略 artifact hash。

### 9.4 审计检查

审计检查：

- Tool exit code、stdout/stderr 和错误状态完整。
- run manifest 包含关键版本、hash、路径和时间。
- Shell/Tool 没有越权路径、网络访问或测试数据读取。
- `output` 无缓存、隐藏文件/目录和非法后缀。
- `models` 无缓存、隐藏文件/目录、非法后缀和超限文件。
- strategy/model hash、modification check hash、backtest hash 和 frozen eval hash 一致。
- Broker 拒单、未成交、强平和费用可追溯。
- NL evidence 能追溯到 as-of `text_id` 或 `source_hash`。
- API key 和 Authorization header 未进入日志。
- 失败显式报错，不能静默 fallback。

### 9.5 验收清单

验收清单：

- PIT 输入满足 `available_at <= decision_time`。
- `/mnt/snapshot` 与 run manifest 中的 decision input hash 一致。
- Sandbox 写入面只限 `workspace`、未锁定的 `output` 和未锁定的 `models`。
- `modification_check_tool` 在正式回测前通过。
- `backtest_tool` 写入完整结果目录和 manifest 摘要。
- Broker 对成交、拒单、费用、做空和强平事件有记录。
- 文本检索、NL Sub Agent 输出和 provider 调用可追溯。
- 冻结策略和模型产物在测试、held-out 前后 hash 不变。
- 所有失败条件显式报错并进入 trace。
