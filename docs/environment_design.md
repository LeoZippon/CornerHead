# Environment 设计

本文档记录 Environment 层。Environment 负责准备 PIT 数据窗口、启动 Sandbox、提供 Agent 常用工具与受控服务入口、执行回测并写审计信息。Agent 可以在 Sandbox 内探索和写代码，但只能使用 Environment 暴露的数据、工具和受控服务。

**相关边界**

- Agent 行为、可写产物和输出格式见 `docs/agent_design.md`。
- Step / Fold / Epoch 编排见 `docs/pipeline_design.md`。
- 原始数据下载、单位和审计见 `docs/data_documentation.md`。
- QMT 实盘流程见 `docs/QMT_documentation.md`。
- 全部参数/超参数默认值速查见 `docs/parameters_reference.md`。

**术语说明**

| 术语 | 含义 |
|---|---|
| Environment | 准备 PIT 窗口、启动 Sandbox、提供 Agent 工具与受控服务入口、执行回测和写审计信息的环境层 |
| PIT | Point-in-time，只使用决策时点已经可见的数据 |
| Sandbox | Agent 运行的隔离容器或本地开发执行环境 |
| Runner | 负责驱动 Agent 会话、切换 snapshot、调用工具和记录日志的程序 |
| Execution Gateway | Sandbox 与工具/可信服务之间的入口，负责权限、路径、运行约束和日志 |
| LLM Proxy | 宿主侧大模型接口代理，保存 API key 并记录调用 |
| artifact | 单次运行产生的策略文件、回测结果、Broker 事件和 NL 日志 |
| manifest | 记录输入、输出、时间范围、配置和关键版本的文件 |
| Broker | 模拟券商，接收交易意图、生成成交/拒单和持仓状态 |
| Held-out | 所有训练结束后才运行的冻结测试区间 |

**职责边界**

**Environment 负责**

- 按决策时点构造 PIT 数据窗口。
- 把窗口数据放入 Sandbox 的固定只读路径。
- 提供 Agent 常用工具（只读检索、文件读写、Sandbox shell、explore/web_search/web_fetch 等）、修改约束检查、回测入口、NL 服务和模拟 Broker。
- 统一 snapshot 可见字段单位，记录数据覆盖、版本、hash 和转换规则。
- 执行交易约束、订单模拟、成交模拟、拒单记录和收益统计。
- 记录 Agent 工具调用（含 shell）、Broker、回测、LLM 和关键 manifest。
- 提供策略产物的受控读写、修改量统计、冻结产物审计和 hash 校验。

**Environment 不负责**

| 事项 | 归属 |
|---|---|
| 决定投资逻辑或策略内容 | Agent |
| 判断哪个候选、prompt 或交易函数更好 | Agent / Pipeline |
| 读取 held-out 后参与训练 | 禁止 |
| 真实下单或连接券商 | QMT 流程 |
| 下载 raw 数据或决定数据源口径 | Data 层 |

**导航**

- [1. 数据可见性与 Snapshot](#1-数据可见性与-snapshot)
  - [1.1 Snapshot 数据域与准备窗口](#11-snapshot-数据域与准备窗口)
  - [1.2 Snapshot 路径与数据槽](#12-snapshot-路径与数据槽)
  - [1.3 PIT 可见性合同](#13-pit-可见性合同)
  - [1.4 单位与特殊口径](#14-单位与特殊口径)
- [2. Sandbox、Runner 与 Agent 工具](#2-sandboxrunner-与-agent-工具)
  - [2.1 Sandbox 环境与运行路径](#21-sandbox-环境与运行路径)
  - [2.2 Runner 与工具调用合同](#22-runner-与工具调用合同)
  - [2.3 产物修改、检查与锁定](#23-产物修改检查与锁定)
  - [2.4 NL、LLM 与联网边界](#24-nlllm-与联网边界)
- [3. 策略执行、Broker 与回放](#3-策略执行broker-与回放)
  - [3.1 回测流程与阶段](#31-回测流程与阶段)
  - [3.2 Broker 原语与策略执行](#32-broker-原语与策略执行)
  - [3.3 回放 Profile、强制约束与做空模式](#33-回放-profile强制约束与做空模式)
  - [3.4 结果目录](#34-结果目录)
- [4. 运行日志、审计与验收](#4-运行日志审计与验收)
  - [4.1 可信日志与核心文件](#41-可信日志与核心文件)
  - [4.2 Manifest、Trace 与读取权限](#42-manifesttrace-与读取权限)
  - [4.3 审计检查与验收清单](#43-审计检查与验收清单)

## 1. 数据可见性与 Snapshot

本章先定义 snapshot 的数据形态和路径，再统一给出 PIT 可见性合同，最后定义单位与特殊口径。所有进入正式策略的数据必须同时满足 PIT 合同和单位合同。


### 1.1 Snapshot 数据域与准备窗口

准备窗口由实验启动前的 `SnapshotConfig` 冻结并写入 run manifest；默认值用于未显式覆盖的数据域。窗口可以按数据域分别调整；snapshot manifest 必须记录各域实际行数和日期覆盖。

| 数据域 | Snapshot 文件 | 主要来源 | 用途 | 窗口配置 |
|---|---|---|---|---|
| `daily` | `daily.parquet` | 日线、每日指标、复权因子和交易日历 | 日频行情、交易约束、日线估值、强制清仓，以及分钟数据缺失时的退化回放 | `daily_window_months`，缺省回退 `window_months`；默认最近 21 个月 |
| `intraday_1min` | `intraday_1min.parquet` | 1 分钟线和交易日历 | 分钟级回放与撮合主输入，日内策略、开收盘和做 T 研究 | `intraday_trade_days`；默认最近 21 个交易日 |
| `fundamentals` | `fundamentals.parquet` | 财报、财务指标、分红、业绩预告/快报、披露计划和主营构成 | 财务和经营质量窗口，保留可追溯版本字段 | `fundamentals_window_months`，缺省回退 `window_months`；默认最近 21 个月可见披露 |
| `events` | `events.parquet` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等 | 异构事件 union；`dataset` 标记来源，其余列按来源表解释 | `events_window_months`，缺省回退 `window_months`；默认最近 21 个月 |
| `macro` | `macro.parquet` | 宏观、政策、利率、全球事件和跨市场数据 | 市场背景窗口 | `macro_window_months`，缺省回退 `window_months`；默认最近 21 个月 |
| `text` | `text_index.parquet`、`text_library/` | 公告、新闻、研报、政策文本 | PIT 文本检索库，必须可追溯到文本 ID | `text_window_months`，缺省回退 `window_months`；默认最近 21 个月 |
| `universe` | `universe.parquet` | `stock_basic` 的 L/D/P 状态和上市/退市日期 | 决策日在市股票池，避免当前上市名单造成幸存者偏差 | 不使用月份窗口；按决策日在市口径生成 |

run manifest 记录本次实验生效的 `snapshot_config.decision_windows`；snapshot manifest 记录该次构建的 `window_config`、各数据域 `domain_windows`、实际行数和日期覆盖。Pipeline 的 Fold `input_window` 是由基础 `window_months` 推出的调度摘要，用于说明验证期前的默认研究输入区间；当各数据域单独覆盖窗口时，实际可见历史以 `snapshot_config.decision_windows` 和 snapshot manifest 为准。


### 1.2 Snapshot 路径与数据槽

**路径概念**

- `/mnt/snapshots/<stage>`：Agent 可见或回放用的数据槽。`train` 是 `valid_decision_input` 的 Agent-visible alias，供训练/探索使用；`valid` 是验证回放数据区间，`test` 是测试类回放区间。
- `/mnt/snapshot`：`backtest` 正式执行时绑定的当前决策输入视图，只包含本次决策时点前已可见的数据。

**Sandbox 数据槽**

```text
/mnt/snapshots/
  train/
  valid/
  test/
```

权限和用途：

| 路径 | 用途 | Agent 权限 | 工具用法 |
|---|---|---|---|
| `/mnt/snapshots/train/` | 训练和探索输入，等同 `valid_decision_input` 的只读 alias | 只读可见 | 不作为正式策略入口输入 |
| `/mnt/snapshots/valid/` | 验证回放区间 | 只读可见 | 验证回放读取 |
| `/mnt/snapshots/test/` | 测试或 held-out 回放区间 | Agent 不可读 | 冻结评估读取 |
| `/mnt/snapshot/` | 当前决策输入视图 | 正式策略只读 | `main.py` 正式运行输入 |

`valid` 和 `test` 回放槽可以包含回放期行情、事件、文本索引+文本库和可选分钟线。正式策略代码不直接选择 `train`、`valid` 或 `test`；Runner/root 在调用 `backtest` 前把对应 decision input view 镜像为当前 `/mnt/snapshot`。

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

`/mnt/snapshot` 是 Agent 正式策略可读的 PIT 裸数据窗口。Snapshot 只做 PIT 可见性过滤、标准单位归一化、跨 raw 表同键拼接和交易约束字段对齐；不预构建 alpha 因子、滚动收益、均线、波动率、综合分数或候选排名。`limit_list_d` 等研究标签不进入预计算 alpha 列；Agent 如需使用，必须在可见窗口内自行解释。

规则：

- `text_library/` 是 as-of 正文库，正文或片段必须由 `text_index.parquet` 引用。
- 宿主 `runtime/snapshot_views/` 保存多个决策输入视图，不挂载给 Agent。
- 宿主 `runtime/current_snapshot/` 是从 `snapshot_views/` 中选定视图刷新出的当前镜像。

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


### 1.3 PIT 可见性合同

**PIT 截止条件**

```text
available_at <= visibility_cutoff
```

如果数据没有可靠发布时间，Environment 必须使用保守规则延后可见，或从本次窗口中排除。窗口数据可以比配置短，例如刚上市股票不足完整历史，或某个研究数据保留下限晚于窗口起点。

`ctx.asof_dir` 中的 parquet 域均需同时满足：覆盖该域或该 `dataset` 的刷新节点已完成，且行级 `available_at <= visibility_cutoff`。文本不在 `ctx.asof_dir` 下，`ctx.nl()` 检索时按文本刷新节点和文本行级 `available_at` 门控。

**行级 `available_at` 来源**

| 数据域 | 行级 `available_at` 来源 |
|---|---|
| `daily` | `daily.parquet` 内的日频分区从 `trade_date` 推出行级可见时间：日线按收盘后可见；每日指标、估值和股本字段按盘后更新完成后可见；复权因子、涨跌停价格和停牌约束按各自盘前可见时间进入。 |
| `intraday_1min` | 历史分钟线为该分钟 `trade_time`，表示 bar close 后可见；盘中当日实时行情走 `ctx.bars` / `ctx.price`，不写入持久 snapshot。 |
| `fundamentals` | 财报、财务指标、分红、业绩预告/快报、披露计划和主营构成按公告日、披露日、报告期和版本字段生成 `fundamental_events.available_at`；公告日字段优先使用 `f_ann_date` / `ann_date` 等明确披露字段。缺少公告日的数据必须有保守回退规则，否则排除；主营构成等自身缺少公告日的行，可以按同股票、同报告期已披露财报事件回退可见。 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等异构事件按来源字段生成 `available_at`；T+1 或盘后数据使用盘后或下一交易日的保守可见时间，事件发生日不等于可见日。 |
| `macro` | 有明确发布时间的宏观、政策、利率、全球事件和跨市场数据按发布时间生成 `available_at`；只有统计期或报告期而无发布时间的数据，按数据集配置的保守可见时间进入；没有可靠回退规则则排除。 |
| `text` | 公告、新闻、研报和政策文本按 `anns_d`、`rec_time` 或文本管线给出的 `available_at` 门控；回填采集的历史文本不得早于其可证明发布时间可见。 |
| `universe` | `stock_basic` 的上市、退市和状态字段按决策日在市口径生成：`list_date <= decision_date` 且未在决策日前退市；不得用当前上市名单回填历史。 |

**财务 PIT 索引**

财务表存在多版本记录，Environment 通过 `fundamental_events` 索引确定每条财务事件的可见时间：行级 `available_at` 取公告日当日 **18:00**，公告日优先使用 `f_ann_date`，其次使用 `ann_date`。启用 fundamentals 时，如果该索引缺失或审计失败，snapshot 构造必须直接失败；其他依赖 `available_at` 门控的数据集缺少该列时也必须报错，不能静默放行。


### 1.4 单位与特殊口径

**单位合同**

实现：`src/autotrade/environment/features/units.py`；raw 侧单位见 `data_documentation.md` §1.2。

| 类型 | 标准单位 |
|---|---|
| 金额 | 元 |
| 成交量/股本 | 股 |
| 比例、收益、换手 | 小数，例如 5% 记为 `0.05` |
| 利率和费率 | 优先小数；确需 bps 时字段名必须带 `_bps` |

原始单位、转换规则和转换前字段必须写入 manifest。单位不明的字段不能进入模型可见数据；依赖单位不明字段生成的交易意图必须被校验拒绝。

**特殊口径修正**

历史 09:30 分钟条被用作实盘 `stk_auction` 近似输入时，对深圳股票生成校正后的成交量/成交额字段：`00*.SZ` 乘 `0.76`，`30*.SZ` 乘 `0.58`；沪市、北交所和其他时点保持 `1.0`。该规则只用于开盘竞价近似输入；raw 分钟线、日内成交汇总和 15:00 收盘竞价不改写。manifest 至少记录校正规则 ID、适用字段、倍率、适用市场/代码前缀和生成时间。策略代码应读取校正后的派生字段；如果派生字段不存在，不能静默退回未校正字段来模拟开盘竞价。

## 2. Sandbox、Runner 与 Agent 工具

### 2.1 Sandbox 环境与运行路径

正式实验默认使用 Docker Sandbox。CLI 只有显式传 `--local-dev` 时才使用本地执行器；本地模式只用于开发和单元测试，不作为正式安全边界。

**基础合同**

| 项目 | 要求 |
|---|---|
| 用户 | Agent 代码以非 root `agent` 用户执行；rootless Docker 下容器内 `agent` 映射为宿主 subuid |
| 挂载与权限 | Docker 模式下，`/mnt/snapshot`、`/mnt/snapshots/*`、`/mnt/artifacts` 和 `/mnt/agent` 由宿主运行目录 bind mount 进容器；宿主负责准备数据、锁定权限、收集产物和写入审计，只给 Agent 合同内写入面放行写权限，其余路径由只读挂载、只读文件和测试槽权限限制 |
| 网络 | 普通 Fold 默认断网（Docker `--network none`）；元学习默认 Docker `bridge` 网络，经宿主 NAT 访问公网但不共享宿主 `localhost`；实验配置可改 Docker 网络模式（`none`/`bridge`/`host`）。托管 XRay 有配置时，Runner 为每个元学习 Sandbox 启动专属宿主 XRay 进程并在结束后清理，同时注入 inactive-by-default 的 active `AT_PROXY_*` 别名；无托管配置时不注入代理别名。Agent 只有在直连失败、明显卡顿或任务明确需要代理时，才在单条 shell 命令前临时启用 active `AT_PROXY_*`；manifest、trace 和 system prompt 不记录 token、订阅链接或代理配置正文 |
| 凭据与密钥 | GitHub/HuggingFace、web search 等凭据只按本次允许的环境变量名从 `.env` 选择性加载并透传；Docker 启动后只记录实际注入的 active 名称，不记录、打印或写入变量值 |
| Python | Docker 镜像内 Python 3.11，依赖由 `ops/docker/sandbox.Dockerfile` 固定 |
| 本机环境 | 本机脚本、测试和 cron 使用 `~/miniconda3/envs/quant`，与 Docker Python 独立 |
| 包安装 | 普通 Fold 不安装新包，依赖由基础镜像或元学习派生镜像提供；元学习可在开放网络时临时试装依赖，需继承给后续 Fold 的稳定依赖必须声明为派生镜像请求 |
| 环境事实源 | `/mnt/artifacts/runtime_env.json` 记录 Python 包、CLI 工具、网络/安装策略和资源摘要 |
| 命令行工具 | 镜像内预装 `rg`、`git`、`pip`、`npm`、`hf`/`huggingface-cli`、`duckdb`（CLI，与 Python 包同版本）和基础 Unix 工具；docker 模式 `runtime_env.json.tools` 按 Dockerfile 合同声明这些工具，故新增 CLI 必须先装进镜像再登记 |
| 编译工具链 | 基础镜像预装 `build-essential`/g++/gfortran/python3-dev，使源码编译的 wheel（如 `torch_scatter`/`torch_sparse`）无需声明 `apt_packages` 即可构建，消除“缺编译器”的构建失败类 |
| 运行时模块 | 镜像内预装可信 `main(ctx)` 驱动 `/opt/at_runtime/main_ctx_driver.py`。driver 只负责加载策略、执行受控 `ctx` 接口并把 broker action 交回宿主；撮合、现金、持仓和风控真相由宿主 `SimBroker` 统一维护 |
| 工具缓存 | pip/HF/torch/CUDA 等缓存经容器环境变量（`XDG_CACHE_HOME`/`PIP_CACHE_DIR`/`HF_HOME`/`CUDA_CACHE_PATH` 等）重定向到 `/tmp`，不落在被采集的 `/mnt/agent`，避免 root 拥有的缓存目录令 `collect_artifacts` 失败 |
| GPU/资源 | 分配结果和资源限制写入 run manifest |
| 写入面 | 仅 `/mnt/agent/workspace`、未锁定的 `/mnt/agent/output` 和未锁定的 `/mnt/agent/models` |
| 可信产物 | `/mnt/artifacts` 由 Environment 写，Agent 只读 |
| Fold 时间 | 默认 60 分钟；Runner 接近 deadline 时最多发一次收尾提示 |

**运行产物路径与约束**

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
  .runtime/
```

约束：

- `/mnt/agent/workspace/` 是临时探索区，不冻结、不回放、不复制到下一 Fold。会话结束后宿主 `collect_artifacts` 归档 workspace/output/models 时会跳过 `.cache`、`__pycache__` 等临时缓存与工具目录：它们不是实验产物，且常由容器用户以受限权限写出（如 pip 0600 缓存），宿主采集用户无法读取，归档它们既错误又会导致拷贝失败。
- `/mnt/agent/output/` 是正式策略代码写入面，根目录固定 `main.py`，可包含受控文本/代码子目录。
- `/mnt/agent/models/` 是正式模型参数写入面，可包含受控模型参数子目录。新增 Python/npm/apt 依赖属于 Sandbox 镜像层，不写入 `models/`。
- `/mnt/agent/.runtime/` 是宿主预创建并锁定的隐藏运行目录，用于放置 NL RPC 等临时受控文件；Agent 不把它当作探索区或正式产物目录。
- `/mnt/agent/` 根目录不是写入面；临时文件、缓存和下载内容应放入 `workspace/`，正式产物分别放入 `output/` 或 `models/`。
- `/mnt/artifacts/parent_output/` 是父产物基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/parent_models/` 是父模型参数基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/runtime_env.json` 是 Sandbox 运行环境契约，记录 Python 包、CLI 工具、网络和包安装策略；Agent 可读，正式策略代码不得硬编码读取。
- `/mnt/artifacts/data_summary.json` 是 Agent 可见轻量数据索引，记录各可见 view 的文件规模、行数、日期覆盖和大表访问提示；只有主决策视图 `snapshot` 给出关键列与关键列空值计数，`train`/`valid` 只给规模与日期覆盖（schema 与 `snapshot` 一致）。它以紧凑 JSON（不缩进）写出，可单次 `cat` 读取且 token 占用低；Agent 工具可读，正式策略代码不得硬编码读取。需要完整 schema、空值或更细字段时，Agent 应先查 snapshot manifest 或 Parquet metadata；需要抽样或聚合大表时，再用 DuckDB、pyarrow 或 pandas 按列/日期过滤读取。
- Runner 在系统提示词中渲染 `当前实验事实`，只抽取上述 JSON 的常用运行事实：身份、可见性、窗口、预算、路径、产物合同、数据摘要、Broker/replay 和 runtime 工具能力。该事实块不渲染 `test_period`、`test_decision_time`、held-out 起止、下一 Fold 排程或测试 snapshot hash；这些字段即使在宿主账本或完整 manifest 中用于审计，也不能作为 Agent 首屏 Prompt 的交易证据。
- `/mnt/artifacts/results/` 由工具写入，Agent 只读可见验证结果。
- 符号链接、隐藏文件/目录、缓存文件、日志、数据 dump、notebook、密钥和不支持后缀不能进入冻结 artifact。模型权重只能进入 `models/`，不能进入 `output/`。


### 2.2 Runner 与工具调用合同

**Runner 负责**

- 创建和锁定运行目录。
- 写入 run manifest，并保证所有入口的路径、时间、Fold 信息和配置都来自 run manifest；Agent 不能通过参数传入绝对路径、未来时间、外部网络地址或越权文件。
- 启动 Agent 会话和工具调用。
- 切换 `/mnt/snapshot`。
- 在正式策略执行期间隐藏阶段槽。
- 记录 Agent 会话、工具调用（含 shell）、Broker、NL 和错误摘要。
- 当主对话上下文过长时，按配置触发语义 compact；默认估算上下文达到 200,000 tokens 后触发，只合并最近一次 summary 后的新增消息，并保留最近原始消息。compact 遵守 Fold deadline 和独立超时，必须为后续主 LLM 调用保留最小剩余时间；失败只写 trace，后续轮次可按条件再试，连续失败达到上限后打开熔断并改用确定性 trim 兜底，不中断 Fold。
- 在 deadline 后停止新的 Shell、服务调用和 LLM 调用。


**Agent 工具入口**

| 工具 | 作用 | 关键边界 |
|---|---|---|
| `grep` / `glob` | 在可见目录中按模式搜索文件名或内容，返回分页结构化结果 | 不能写入，不能访问测试或隐藏路径 |
| `read` | 按行号读取文件（可分页）；读要编辑的代码优先于 shell `cat`/`head` | 只读；不访问测试或隐藏路径 |
| `note` | 记录推理/复盘，不执行任何操作 | 无副作用，仅进 trace |
| `shell` | 在 Sandbox 内读数据、写 `workspace`、`output` 或 `models`；元学习开放网络时可运行 `git`/`pip`/`npm`/`hf` | 不是宿主 shell；普通 Fold 默认由 Docker `--network none` 断网；可用 `max_output_chars` 和 `timeout_seconds` 主动缩小内联输出和单次运行时间；长输出落盘并记录路径 |
| `write_file` / `edit_file` | 在 `workspace`/`output`/`models` 下创建/覆盖或精确编辑文本产物 | 只写受控根；`edit_file` 的 `old_string` 必须唯一匹配（staleness 检查）；`output/README.md` 只读；写锁后拒绝 |
| `explore` | 委托数据探查 Sub Agent（更便宜模型）调查具体问题并返回摘要 | 按只读约定使用 `shell`/`grep`/`glob`（不写正式产物，改动由 modification_check/冻结 hash 兜底）；只回结论、证据、风险与限制、建议下一步，原始过程进 trace |
| `web_search` | 元学习联网检索 | 仅元学习可用；每次调用声明 engine、perspective、query 和 max_results；结果写 trace |
| `web_fetch` | 元学习读取公开网页 | 仅元学习可用；宿主侧只读 GET，默认直连，`use_proxy=true` 才允许使用 active 代理；只支持 http/https 文本或 HTML，跨 host redirect 不自动跟随，结果写 trace |
| `modification_check` | 校验正式 `output` 修改量、`models` 格式/大小和父产物 hash | 无业务参数；不检查 `workspace` 或结果目录 |
| `backtest` | 执行 `output/main.py` 并回放交易；Agent 可传的业务参数只有 `replay_window` | 消费并校验当前 snapshot；每次调用创建唯一结果目录 |
| `finish_fold` | 当前 Fold 停止修改 | 无业务参数；要求当前 hash 已有成功完整验证回测（`replay_window` 调试不算）+ 修改检查 + 轻量合同检查，通过后只读锁定 `output/` / `models/` 并清理 Sandbox 内 Agent 后台进程 |

**Trace 与工具规格**

- 所有工具 trace 都记录当前 `tool_spec` 的 `schema_version` 和 `result_policy`。`shell` 额外记录 `command_kind`（`read`、`list`、`search`、`write`、`neutral` 或 `unknown`），只用于审计和统计；权限判断由 Sandbox、文件系统权限、Docker 网络和阶段策略执行。
- 工具调用采用 provider 原生 function calling。工具名称和参数 JSON Schema 由 `ActionSpec` 生成并随请求下发，模型返回结构化 `tool_calls`，Runner 再按 `ActionSpec` 硬校验后分发；不再要求模型把动作序列化成 JSON 文本。
- 系统 Prompt 只保留工具表和关键边界；参数语义、输出预算、分页方式、重试提示和失败原因尽量下沉到工具 schema、字段 description 和 `ToolError.error_type/reason/retry_hint`。

**调度与并行**

- 一轮可以包含多个 `tool_calls`，每个调用单独返回一条 `tool` 结果。互相独立的只读工具（`concurrency_safe`，如 `grep`、`glob`、`web_search`、`web_fetch`）可并行执行；有状态工具（`write_file`、`edit_file`、`shell`、`explore`、`modification_check`、`backtest`、`finish_fold`）按因果顺序串行执行。
- `done`/`finish_fold` 等终止工具执行后，同一轮后续工具会被取消，避免终止验收后继续修改。
- Runner 的历史裁剪和上下文压缩必须保持 `tool` 结果不脱离对应的 `assistant` 工具调用。

**Sub Agent 执行边界**

- NL Sub Agent 和 Explore Sub Agent 复用同一原生工具循环。Explore 按只读约定调用 `shell`/`grep`/`glob`，继承 Fold deadline，只回答委托问题，不替主 Agent 做最终策略综合。
- Explore 单轮被 `finish_reason=length` 截断或遇瞬时 provider 错误时，不让整个探查失败；Runner 停止循环并强制一次简洁最终摘要。Explore 的 `max_tokens` 需要留出长工具调用（如 DuckDB SQL）和摘要的余量。

**上下文管理**

- 长 `reasoning_effort` 轮次默认请求 SSE 流式响应，并在客户端合并 tool-call delta 为统一完成结果。
- 上下文管理分三层依次升级：原地清理超大旧 `tool` 结果（context editing，保留 `tool_call_id`）、确定性 `_trim` 摘要、低成本模型语义压缩。三层都以估算 prompt token 为主触发阈值，消息条数只作为高位安全上限，避免在小上下文上频繁改写前缀；裁剪/压缩会重置 DeepSeek 自动前缀缓存。
- 主对话按 prompt/completion/reasoning 以及缓存命中/未命中累计 token，并写入 session 摘要的 `token_usage`（含 `cache_hit_ratio`），用于权衡裁剪/压缩强度。


**Shell 执行边界**

- Shell 不维护复杂 Bash、路径或写目标静态解析。读写边界由 Docker 只读挂载（如 `:ro`）、`/mnt/agent` 父目录不可写、`/mnt/agent/workspace` 可写、未锁定的 `/mnt/agent/output` 和 `/mnt/agent/models` 可写、test 槽不可读权限和普通 Fold `--network none` 执行。
- Shell 工具层只负责运行控制和审计：检查当前阶段是否允许执行、`finish_fold` 后是否已写锁、参数是否合法，执行超时和输出预算，并把命令、退出码、输出位置和粗粒度 `command_kind` 写入 trace。
- Shell 命令自身失败不转成 `ToolError`，而是返回非零 `exit_code` 和 `stderr`；当 stdout/stderr 超过本次内联输出预算时，超出内容落盘并返回 `stdout_path` / `stderr_path`。Tool 层拒绝才返回结构化 `error_type` / `reason` / `retry_hint`。
- Explore Sub Agent 与主 Agent 共用同一 shell 执行边界，并按只读约定只做数据探查；若误写正式产物，仍由 `modification_check`、冻结 hash、Docker 只读挂载和产物采集合同兜底。
- Prompt 与工具 schema 都要求 Agent 不使用 `2>/dev/null` 隐藏错误。命中该模式时，shell 结果附带非阻断的 `stderr_suppression_reminder`；stderr 应原样进入 trace。
- 超时清理在容器内执行：每条定时 shell 命令在容器内 `timeout` 下运行，其进程组在超时即被整组杀掉；宿主 `docker exec` 截止时间只作更长兜底。容器以 `--init` 启动，由 tini 回收孤儿/僵尸，避免被杀的训练子进程残留并占满 `--pids-limit`。


### 2.3 产物修改、检查与锁定

`modification_check` 固定读取：

- 只读父产物 `/mnt/artifacts/parent_output/`。
- 只读父模型参数 `/mnt/artifacts/parent_models/`。
- 当前工作副本 `/mnt/agent/output/`。
- 当前模型参数 `/mnt/agent/models/`。
- run manifest 中的父产物 hash、初始模板 hash 和约束；manifest 不暴露宿主模板目录。

**检查项**

- 父产物 hash。
- 当前策略 artifact hash、模型 artifact hash 和 combined hash。
- 文件数、总 diff 行数、Python diff 行数和总字节数。
- README/只读文件修改。
- 非法文件、隐藏文件/目录、缓存和不支持后缀。
- 模型参数文件数、总字节数、非法后缀、隐藏文件/目录和缓存。

**基准可信性**

- 修改量只能基于可信基准计算。`modification_check` 先校验父策略产物 hash 与 run manifest 一致；若没有父策略而使用初始模板，则校验 `initial_template_hash`。
- 父模型参数走同一规则：父模型目录非空时，manifest 必须提供 `parent_model_artifact_hash` 且与实际一致；只有空父模型目录才允许用计算出的空目录 hash 作基准。
- 任一基准不可信都 fail-fast，不会静默继续。

**重检要求**

- 产物变更后必须重新运行 `modification_check`。
- 检查失败时，Agent 只能缩小正式修改后重试。

**`finish_fold` 锁定流程**

1. 先从宿主侧把 `output/` 和 `models/` 切到只读，避免校验期间被后台进程竞态修改。
2. 若任一检查失败，目录恢复为可写，并返回可修复原因。
3. 按 Pipeline 冻结同口径把关：当前 `output`/`models` hash 必须已有一次成功的完整验证回测（`replay_window` 调试回放不算），最近一次修改检查必须仍匹配当前 hash，并执行轻量合同检查确认当前产物可加载且 `main(ctx)` 存在。
4. 成功后，Runner 清理 Sandbox 内 Agent 后台进程，复核当前策略/model hash 未变，保持 `output/` 和 `models/` 只读锁定；Pipeline 再复核验证结果和当前策略/model hash 是否一致。


### 2.4 NL、LLM 与联网边界

**NL 服务**

**策略代码可写**

```python
from at_tools import nl
result = nl(ts_code, prompt="...")       # 单股文本分析
event_result = nl(prompt="...")          # 事件/主题/行业/宏观文本检索
content = result.get("content", "")
```

**宿主 NL 服务**

Sandbox 内的 `nl()` 只写请求并等待响应。宿主 Environment 使用以下组件。

- `TextRetriever` 读取 `text_index.parquet` 和 `text_library/`。
- `CompanyContextStore` 在请求提供 `ts_code` 时构造并缓存公司上下文。
- `NLSubAgentEngine` 启动一个可调用 `text_retrieve` 的宿主侧 Sub Agent，并调用宿主 `LLMProxy`。

NL Sub Agent 的最终回答不限定格式；只有它请求 `text_retrieve` 时使用内部标准工具 schema。`ts_code` 可选：传入时作为单股上下文和检索排序提示，不是硬过滤；不传时按 prompt 在当前可见文本库中做事件、主题、行业、宏观或市场级检索。Sandbox 只收到 result dict，常用字段为 `status`、`scope`、`content`、`tool_calls`、`evidence` 和 `error`。策略若需要数值分、风险标签或交易过滤条件，必须在 Agent 代码中自行解析 `content`。

**NL 结果写入**

```text
results/<phase>_<idx>/nl_tool/
  nl_requests.jsonl
  search_requests.jsonl
  evidence.jsonl
  nl_llm_calls.jsonl
```

Sandbox 内 `nl()` 与宿主 NL 服务之间的临时 JSONL RPC 文件位于 `/mnt/agent/.runtime/nl_rpc/`。该目录由宿主创建和锁定：request 文件只用于 Agent 追加请求，response 文件只由宿主写入、Agent 只读；回测结束后删除本次临时文件，若 `nl_rpc/` 已空则删除目录。正式审计产物只以上述 `results/.../nl_tool/` 为准。

NL evidence 必须来自 as-of `text_id` 或 `source_hash`。没有可见证据时，Sub Agent 必须说明证据不足；Agent 策略自行决定忽略、降权、重试或不交易，不能伪造引用。NL 结果还需要防范发布时间/入库时间误差、检索召回偏差、模型常识污染、自由文本解析不稳定和前视泄露，不能让 NL 结论覆盖 Broker 约束、交易成本或 PIT 可见性合同。

**NL 调用配额（成本，与延迟分开）**

每次回测的 `ctx.nl()` 总配额默认按 `nl_max_calls_per_decision_day`（系统设定的日均上限）× 决策天数计算（一个日均预算），可由可选 `nl_max_calls_per_backtest` 进一步收紧（取 min）。超出后 NL 服务向策略返回 `budget_exhausted` 错误，策略需自行降级。配额只限制调用次数和成本；`ctx.nl()` 仍必须放在 `ctx.substep` 内，耗时由该 substep 的时间预算约束。


**LLM API 边界**

Agent 主对话、Runner context compact 和 NL 工具调用都只能经宿主 `LLMProxy`。

- Agent 主对话由 Runner 触发，记录到本地 conversation log。
- context compact 由 Runner 触发，默认使用低成本无 thinking 模型；它只生成继续会话所需摘要，不调用工具，不进入 Sandbox。
- NL 工具调用由宿主 NL 服务触发，记录到回测结果目录的 `nl_tool/`。
- 主对话和 NL 调用默认使用 provider 支持的深度推理配置；当前 DeepSeek 适配器映射为启用 thinking 且 `reasoning_effort=max`，后续其他 provider 按各自能力等价映射。实验 CLI 可显式用 `--reasoning-effort` 或 `--no-thinking` 做消融/调试覆盖。compact 默认使用低成本无 thinking 配置，因此不传 reasoning effort。
- 元学习 `web_search` 和 `web_fetch` 由宿主侧工具执行。`web_search` 可用引擎写入 manifest，Agent 在 action 中选择 `engine`，并用 `perspective` 标记研究视角；启用搜索时，Runner 要求三类研究视角各有一次非空成功检索后才允许 `done`。`web_fetch` 默认直连，`use_proxy=true` 才允许使用 Runner 当前 active 代理；它只做公开 http/https 网页的只读 GET、HTML/text 到 markdown 的确定性提取和有界落盘，不支持 cookies、认证 header、自定义 header、POST、登录、浏览器渲染、JS 执行或 PDF/二进制解析。
- 元学习可由实验配置显式开放 Sandbox shell 网络做工作区内探索，并通过 `workspace/sandbox_environment.json` 申请后续 Fold 继承的依赖、由 Pipeline 构建派生镜像；该能力不替代 `web_search` 的三视角要求，也不开放给普通 Fold。
- Web Search provider 在宿主侧执行有限重试和限速；Semantic Scholar 使用每 key 共享的文件锁节流并对 429/5xx 做指数退避，避免单次短时限流直接结束元学习。
- API key、Authorization header 和 provider client 不进入 prompt、artifact 或日志。元学习 shell 需要用到的第三方 token 只允许通过显式列名的环境变量透传给容器，Environment 不记录变量值；trace 和大输出文件会对常见 OpenAI/HF/GitHub token、代理凭据和 VLESS 链接做脱敏。
- provider 超时不能无限阻塞 Fold；超时、重试和失败策略必须写入 trace。
- provider 返回的 reasoning 或内部思考只进入审计日志；正式结构化字段取最终 content。
- 测试和 held-out 的 LLM/NL 明细不反馈给 Agent。

**Provider 调用记录**

- `experiment_id`、`fold_id`、`run_id`、`conversation_id`、`call_id`。
- 调用来源：Agent 主会话、NL 工具、元学习或其他受控入口。
- 输入 messages / prompt。
- 原始 provider 响应。
- 模型、超时、耗时、token 或费用统计（如可用）。
- 错误、超时和修复策略。

`agent_trace.jsonl` 对主对话记录 `llm_call`，对 context compact 记录 `context_compaction`。每条 `llm_call` 只记录本轮首次出现的消息增量（`new_messages`）与 `message_count`，不再每轮重复嵌入整段历史；把各轮 `new_messages` 与该轮 `content`/`tool_calls` 顺序拼接即可还原完整对话，trace 体积随对话线性增长而非二次膨胀，完整 prompt 仍由 provider conversation log 承担。compact trace 至少包含 provider、model、触发 token 估算、调用次数、压缩前后消息数、summary hash、usage、状态和错误摘要。

## 3. 策略执行、Broker 与回放

### 3.1 回测流程与阶段

`backtest` 的正式流程：

1. 校验当前 `output` hash 和 `models` hash 与最近一次 modification check 一致。
2. 校验 `/mnt/snapshot` 与 run manifest 中的决策输入一致。
3. 创建唯一 `results/<phase>_<idx>/`。
4. 固定 `AT_SNAPSHOT_DIR=/mnt/snapshot`、`AT_AGENT_OUTPUT_DIR=/mnt/agent/output`、`AT_MODEL_DIR=/mnt/agent/models`，并把宿主管理的 state 可见目录与 staging 目录作为 driver 私有路径传入，在 Sandbox 启动一个常驻 `main(ctx)` 进程。driver 在导入策略前移除 `AT_STATE_DIR` / `AT_STATE_STAGING_DIR`，并用 path guard 阻断策略硬编码访问托管 state 根；策略只能通过 `ctx.state_dir` 在 `ctx.substep` 内访问暂存视图。该进程是镜像内 `/opt/at_runtime/main_ctx_driver.py` 这一真实模块（按文件加载，非 `python -c` 字符串，Python 标准库实现，不依赖 `broker_core`）；随镜像构建烤入 `/opt/at_runtime`（见镜像合同）。
5. 按回放 tick 逐 tick 构造市场级 `ctx` 并调用一次 `main(ctx)`（盘中 1 分钟 tick，盘外按 `offsession_tick_minutes`（默认 15 分钟）spacing）；`main` 在显式竞价/交易分钟 tick 的 `ctx.substep` 内通过 `ctx.broker` 的 `ts_code` 原语下单，普通盘外 tick 只做研究/状态/计划维护。
6. 若 `main` 在决策时调用 `ctx.nl()`，通过宿主控制的 JSONL 文件 RPC 请求 NL 服务（宿主在等待 `main` 返回时同时服务 NL 请求）。
7. 收集本 tick `main` 发出的 Broker 原语调用，宿主 Broker 按延迟进入订单簿，逐 bar 撮合并强制约束。
8. 按 tick 推进直到回放区间末日强制清仓。
9. 写结果（`detailed_return.json`、`orders.parquet`）、Broker 事件、NL 工具日志、策略/model hash 和 manifest 摘要。

临时 Python 回测、Shell 中的手工脚本和 notebook 只能作为探索，不构成正式 valid/test/held-out 结果。正式结果只能由 `backtest` 写入。


**Valid 与 Frozen Eval**

| 模式 | 策略输入 | 回放区间 | 结果目录 | Agent 可见性 |
|---|---|---|---|---|
| `valid` | 验证决策输入 `/mnt/snapshot` | `/mnt/snapshots/valid` | `results/valid_<idx>/` | Agent 可读 |
| `frozen_eval` | 测试或 held-out 决策输入 `/mnt/snapshot` | `/mnt/snapshots/test` | `results/test_<idx>/` 或 `heldout_<idx>/` | 不反馈给 Agent |


### 3.2 Broker 原语与策略执行

Broker 不内置任何交易策略，只暴露按股数操作的基础原语和查询接口；交易策略由 Agent 在 `output` 中以函数实现，并在回放时调用这些原语。Agent 不能直接写成交、持仓或收益。

一次实验只运行一个账户，类型由 `broker_profile.account_type` 决定：`credit`（信用账户，默认）支持担保品买卖与全部融资融券原语；`stock`（普通账户）只支持 `buy`/`sell`/`close`/`cancel`，信用原语在 driver 层直接抛错（这是策略代码的类别错误，不是行情拒单）。

`main(ctx)` 内可用的 `ctx.broker` 接口（下单原语均以 `ts_code` 为第一参数）：

| 接口 | 作用 |
|---|---|
| `buy(ts_code, amount\|weight, limit=None, valid_bars=1, reason=None)` | 买入（信用账户=担保品买入 33，普通账户=股票买入 23）；`limit` 为限价单 |
| `sell(ts_code, amount, limit=None, valid_bars=1, reason=None)` | 卖出多头可卖（T+1）份额（信用=担保品卖出 34，普通=股票卖出 24） |
| `fin_buy(ts_code, amount\|weight, limit=None, valid_bars=1, reason=None)` | 融资买入（27，仅信用）：开仓不动用现金，本金+佣金计入融资负债合约、按日计息；受保证金可用余额、标的池与授信额度约束 |
| `short(ts_code, amount\|weight, limit=..., valid_bars=1, reason=None)` | 融券卖出（28，仅信用）；**必须限价申报且申报价不得低于参考最新价（uptick 规则）**，市价 short 拒单 |
| `cover(ts_code, amount, limit=None, valid_bars=1, reason=None)` | 买券还券（29，仅信用）：平空份额并按最老合约优先偿还融券负债，偿还部分的应计利息即时以现金支付 |
| `sell_repay(ts_code, amount, limit=None, valid_bars=1, reason=None)` | 卖券还款（31，仅信用）：卖出净所得先息后本偿还融资负债（最老合约优先），余额留作现金；无融资负债时拒单 |
| `direct_repay(amount, reason=None)` | 直接还款（32，仅信用）：从现金偿还融资负债（先息后本），金额截断到可用现金与负债余额；提交 tick 即时结算、无撮合 |
| `close(ts_code, reason=None)` | 平掉该股可平持仓（恒市价；引擎按持仓方向在提交时转为卖出/买券还券） |
| `cancel(order_id, reason=None)` | 撤销 `pending()` 返回的未成交委托 |
| `position(ts_code)` | 该股有符号已成交持仓股数（不含在途单） |
| `pending(ts_code=None)` | 在途/延迟提交单；有参返回该股在途单，无参返回全量 |
| `cash` | 当前现金视图，每 tick 反映已成交结果（含佣金/滑点）；未成交计划不改变它 |
| `available_cash` | 可用于买入/担保品买入的现金（现金扣融券卖出冻结所得）。保证金占用不冻结现金——它作为计算约束经保证金可用余额门控新的信用操作 |
| `account` | 当前账户级快照；信用账户含 `credit` 子块（维保比例、保证金可用余额、融资/融券负债、应计利息、额度、利率） |
| `positions` | 当前逐标的持仓快照列表（数量、可卖/可平数量、方向、成本和市值等） |
| `credit` | `account["credit"]` 的便捷视图；普通账户为 None |
| `debt_contracts(ts_code=None)` | 未了结融资/融券负债合约明细（未还金额/量、开仓日、年利率、已计未付利息） |

正式策略代码必须在 `ctx.substep` 内调用 `ctx.broker` 原语；子步骤外的下单、平仓或撤单会被 Sandbox driver 拒绝。substep 内 broker action 是按声明预算建模的提交计划，不会立刻投影到账户/持仓；它会先以 `pending_stage="substep_delay"` 暴露，ready 后提交并由宿主 Broker 真实约束。`ctx.broker` 的 `cash`/`available_cash`/`position` 只反映已成交状态（加上宿主撮合后的真实结果），因此同一 tick 内不要依赖未成交计划改变可用现金或持仓视图。

这些便捷封装是 ergonomic sugar。底层 `SimBroker` 的接口与官方全功能 QMT 客户端内 Python 策略 API 对齐（参考 `external_references/gjzq-da-qmt` 的官方接口文档），便于 live 适配器（`QMTBroker`）做机械映射，两者都满足 `TraderProtocol`：

| `SimBroker` | 官方 QMT 策略 API |
|---|---|
| `passorder(op_type, order_type, account_id, order_code, pr_type, price, volume, user_order_id=…) -> order_id` | `passorder(opType, orderType, accountid, orderCode, prType, modelprice, volume, strategyName, quickTrade, userOrderId, ContextInfo)`；返回值等价于提交后立即 `get_last_order_id`，`user_order_id` 即投资备注（`m_strRemark`）关联键 |
| `cancel(order_id, account_id, account_type)` | `cancel(orderId, accountId, accountType, ContextInfo)` |
| `get_trade_detail_data(account_id, account_type, data_type)` | `get_trade_detail_data(accountID, strAccountType, strDatatype)`，`data_type ∈ ACCOUNT/POSITION/ORDER/DEAL` |
| `get_debt_contract()` / `get_assure_contract()` / `get_enable_short_contract()` | 同名信用账户查询（负债合约 / 担保标的 / 当日可融券明细） |

`op_type` 取官方 opType 码（`optype` 常量）：普通账户 23 股票买入 / 24 股票卖出；信用账户 27 融资买入 / 28 融券卖出 / 29 买券还券 / 31 卖券还款 / 32 直接还款 / 33 担保品买入 / 34 担保品卖出。**30 直接还券有意不支持**：模拟保持单票单侧持仓（`opposite_side_position_open` 拒单），该操作在此约束下结构性不可达，买券还券覆盖其经济需求；未来如需支持须把持仓按 (code, side) 重新建键。`pr_type` 支持 11 指定价（限价，用 price）与 5 最新价 / 14 对手价（市价，回测口径相同）；`order_type` 仅支持 1101（单股/股数），直接还款按官方口径用 1102（金额元）。引擎按决策 + `execution_lag_bars` 把订单 `passorder` 进簿，逐 bar `match_bar` 撮合。

记录字段与官方对象的映射（live 适配器按此机械改名；sim 记录保持仓内数据层一致的命名）：`order_id`↔`m_strOrderSysID`（sim 中即 `user_order_id`/`m_strRemark`）、`op_type`↔passorder opType、`order_volume`↔`m_nVolumeTotalOriginal`、`status`↔`m_nOrderStatus`、`price`↔`m_dTradedPrice`/`m_dLimitPrice`、持仓 `quantity`↔`m_nVolume`、`sellable_quantity`↔`m_nCanUseVolume`、`entry_price`↔`m_dOpenPrice`、`entry_cost`↔`m_dOpenCost`、账户 `available_cash`↔`m_dAvailable`、`total_assets`↔`m_dBalance`、信用块 `maintenance_ratio`↔`m_dPerAssurescaleValue`、`enable_bail_balance`↔`m_dEnableBailBalance`、`fin_debt`/`slo_debt`↔`m_dFinDebt`/`m_dSloDebt`、合约 `compact_id`↔`m_strCompactId`、`real_compact_balance`/`real_compact_vol`↔同名官方字段、`year_rate`↔`m_dYearRate`。

`amount` 是股数（按 100 股，即 1 手，向下对齐），`weight` 是初始权益的名义比例。所有下单/撤单原语都接受可选 `reason=`（默认 `None`）审计注记，Sandbox driver 原样记入 Broker 事件、不影响撮合。下单原语返回可用于撤单的 `order_id`；`pending()` 记录包含 `order_id`、`op_type`、`submitted_at`、`age_minutes`、`status`，并可带 `pending_stage`（如 `substep_delay` / `submit_lag`）等字段。`substep_delay` 阶段的 `age_minutes` 从动作生成 tick 起算；ready 后进入 `submit_lag` 阶段则从实际提交 tick 起算。所有拒单、撤单、部分成交、T+1 阻挡和强制平仓事件必须记录。


**main(ctx) 与逐 tick 回放**

交易逻辑全部由 Agent 定义。Environment 在 Sandbox 中启动一个常驻 `main(ctx)` 进程，按回放 tick 逐 tick 构造市场级 `ctx` 并调用一次 `main(ctx)`（一次覆盖全市场，而非每只股票一次）。回放是覆盖全天的 24h tick 网格：交易时段内按真实 1 分钟 bar 逐 tick 推进，时段外按 `offsession_tick_minutes`（默认 15 分钟）spacing 继续调用 `main(ctx)`（仅研究/状态/计划维护用途，盘外 tick 不下单），同一循环既驱动回测也驱动实盘。`main` 自己决定时序：每个 tick 都可核对持仓/在途，在 `ctx.substep` 内维护 `ctx.state_dir` 计划，并在选定时点筛选新目标；只有显式可报单 tick（09:15/09:25/14:57）或有真实行情的交易分钟 tick 才在 `ctx.substep` 内报单开/平仓。若 Agent 想盘前准备订单，应在 off-session tick 的 substep 中先写计划，后续在 09:15/09:25 显式盘前 tick 的 substep 中读取计划并调用 `ctx.broker`。`ctx` 暴露的字段清单（`broker` 原语、行情、时间/账户、`nl`、`substep`、`asof_dir`/`asof_version`/`snapshot_dir`/`model_dir`/`state_dir` 等）见 `agent_design.md` §3.2；本节定义其中由 Environment 决定的执行、延迟与可见性语义。

`ctx.asof_dir` 是逐 tick 滚动的 Timeview：它把五个 Agent 可直接读取的 parquet 域——`daily`、`events`、`macro`、`fundamentals` 和 `intraday_1min` 分钟历史——按各自本地库刷新任务的真实入库节奏回放，并以仿真时钟（`ctx.cur_datetime`，Asia/Shanghai）驱动可见性。文本库不在 `ctx.asof_dir` 下；宿主侧 `ctx.nl()` 按同一仿真时钟门控公告/新闻可见性。每个域只在落它的 cron 任务按仿真时钟已跑完后才可见：可见性节点是 `data/contracts.py` 的 `REFRESH_NODES`，镜像 `configs/tushare_update_schedule.json` 的实际 crontab（`ready_at = start + duration_minutes`）。`daily`/`macro`/`intraday_1min`/多数 `events` 跟随 `cn_evening_full`（23:35 启动、约次日 02:05 完成），所以盘中横截面只到 D-1，当日实时行情走 `ctx.bars`/`ctx.price`、不进持久化视图；`margin_secs` 在盘前节点后当日可见（约 09:05/09:15）；`fundamentals` 在夜间 PIT 构建后可见（约 03:50）；`cctv_news`/`news` 在盘前文本回填后可经 `ctx.nl()` 检索（约 09:00）。

每个 parquet 域是一个普通 parts 目录，用 `pd.read_parquet(ctx.asof_dir / "<域名>")` 读出拼接：part 0 是该域的冻结研究快照（硬链入，零拷贝），后续 parts 是 write-once 的回放增量，只在仿真时钟跨过覆盖该域的节点时追加；读目录得到的是当前 tick 已可见的全量 as-of 表，不是仅新增增量。因此 09:20→次日 02:05 一整段没有节点完成，视图被冻结、零重建。`ctx.asof_version` 在视图滚动时自增，策略可缓存一次读取、仅在版本变化时重算。`ctx.snapshot_dir` 仍是冻结的研究基线。视图开关是 `timeview_enabled`（默认开）。

`ctx.nl()` 文本同模型滚动：公告/新闻只在其刷新节点完成后可见，冻结研究语料始终可见；约 1.6GB 文本库零拷贝就地读取，按查询时的 `available_at` 门控。

启用 `auction_enabled`（默认开）时，每个回放日插入盘前与收盘集合竞价决策 tick。两个盘前 tick 排在常规分钟 tick 之前：`09:15`（`auction_preopen_time`）信息 tick——集合竞价尚未撮合，`ctx.price` 为 None，用于筛选与 NL；`09:25`（`auction_decision_time`）tick——暴露撮合出的开盘价（不含日内最高/最低/成交量）。两者下的单按次一根 bar 成交：`09:15` 的单成交于 09:30 开盘集合竞价，`09:25` 的单成交于首根连续 bar（09:31）。`14:57`（`auction_close_time`，默认 `"14:57"`）是收盘集合竞价决策 tick：其下的单成交于当日最后一根 15:00 bar 的收盘价（close 印记，对应 15:00 收盘集合竞价）。Broker 按当日涨跌停规则成交（单边一字涨停开盘的买单、跌停的空单被拒）。只有真正的集合竞价成交（`09:15` 盲下单成于 09:30 开盘竞价、`14:57` 成于 15:00 收盘竞价）按单一竞价价清算、**不计滑点**，取竞价侧价格（开盘用 `open`、收盘用 `close`）、`price_label="auction"`；`09:25` 的单成于首根连续 bar（09:31），属连续撮合，**按 taker 滑点成交**、`price_label` 为该连续 bar（如 `minute:09:31`）。

`main` 是决策阶段，可读模型参数（`ctx.model_dir`）、滚动 PIT 视图（`ctx.asof_dir`/`ctx.snapshot_dir`）和 NL（`ctx.nl`）；重操作只应在少数选定时点执行，不要每分钟跑。策略里的状态读写、NL、筛选、模型推理、委托管理和 broker 动作都应写成 `ctx.substep(name, budget_minutes=B)`；`ctx.state_dir` 与 `ctx.broker` 原语在 substep 外会被 driver 拒绝，宿主还会用 `main(ctx)` 总耗时减去 substep 耗时，拒绝实质未包裹计算。跨 tick 暂存写入受宿主托管的可见目录 `ctx.state_dir`（Broker 仍是持仓真相源）：进入 `ctx.substep` 时宿主会把当前可见状态拷贝进暂存目录作种子（保证块内读取仍看旧可见值），块内写入在 `ready_at = 生成 tick + B` 后才并入可见目录，后生成者在冲突时胜出。该机制按路径实现——`ctx.state_dir` 在 substep 内解析到隐藏暂存目录——因此能捕获任意写入方式（含 pandas/pyarrow parquet 的原生写），不依赖 Shell 路径静态解析。可见目录与暂存目录每次回测都清空重建（可复现；需跨回测持久的数据应在回测前写入 `models/`）。`ctx.state_dir` 只适合小体量跨 tick 状态：高频 substep × 大 `state_dir` 会付出 O(state) 的逐次拷贝开销，大数据应预先整理进 `models/`，或只在少数 tick 重算并落入 `ctx.state_dir`。仿真时钟（`ctx.cur_datetime`，Asia/Shanghai）统一驱动域可见性、暂存 `ready_at`、延迟提交与成交映射，同一 `main(ctx)` 循环也驱动实盘。回放进程只读加载 `output/` 策略代码和 `models/` 模型产物，禁止写 `output/` / `models/`、创建软/硬链接，且按真实路径阻断经链接访问测试槽或 `/mnt/artifacts`。

`main` 每个 tick 发出的原语对齐官方 QMT `passorder` / `cancel`（QMT 无券商侧条件单/止损单，故不引入引擎侧触发单）。每日维护一个订单簿：决策在某根 bar，订单于其后第 `execution_lag_bars`（默认 2，经 manifest 配置）根 bar 起进入撮合，杜绝 bar 内前视（`1`=紧邻下一根，`2`=一根算/报单延迟 + 下一根成交；如 09:35 决策、09:37 起成交）。两类报价：

- **市价单**（默认，对应 prType 14 对手价）：在进入 bar 按 `open` + 滑点成交，单 bar 有效。
- **限价单**（`limit=P`，对应 prType 11 指定价）：挂单，自进入 bar 起最多 `valid_bars` 根 bar，无滑点；买/补在 `open<=P` 时按 open 成交，否则 `low<=P` 时按 P 成交；卖/空在 `open>=P` 时按 open 成交，否则 `high>=P` 时按 P 成交；窗口内未触及则自动撤单（记 `order_cancelled`）。`close` 恒市价。

**融券卖出限价规则（实施细则）**：融券卖出必须限价申报，市价 short 在 `passorder` 即拒（`slo_sell_requires_limit_price`）；申报价不得低于最新成交价——订单首次到达交易所（激活 bar）时若 `limit <` 该 bar 参考价，按申报被拒记 `slo_sell_uptick_rule`；通过检查后正常挂单，此后价格上穿限价属合法成交。典型用法是 `limit=ctx.price(code)`（当前可见价）或更高。

策略可在 `ctx.substep` 内对 `ctx.broker.pending()` 返回的未成交委托调用 `ctx.broker.cancel(order_id, reason=...)`。若委托仍在 substep 延迟提交队列中，宿主会在提交前移除并记录 `order_cancelled(pending_stage="substep_delay")`；若委托仍在提交延迟队列中，宿主会在进入撮合前移除并记录 `order_cancelled(pending_stage="submit_lag")`；若委托已进入 Broker 当日订单簿但尚未成交，则调用底层 `cancel` 移除。已经在当前激活 bar 先撮合成交的市价单不能事后撤销。轻量撤单扫描也应使用小预算 substep（如 0.5 分钟），以统一统计耗时和撤单提交时点。

**决策延迟与重计算可见性（Agent 声明式预算）**

策略用 `with ctx.substep(name, budget_minutes=B):` 包裹每段可观察决策（持仓/在途管理、横截面筛选、模型推理、NL、状态读写、批量下单计划），声明该块的计算时长 `B>0`（分钟）。`B` 有三个作用：（1）**实测墙钟 fail-fast**——一旦该 substep 真实墙钟超过 `B·60s`，立即抛 `BacktestError` 中止本次回测，并向 `backtest` 工具返回精确失败（substep 名、日期、声明 `B` vs 实测），故低报（声称快却跑得慢）硬报错、不可利用；（2）**`ctx.state_dir` 写可见性门控**——块内经 `ctx.state_dir` 的写入在 `ready_at = 决策 tick + B` 才并入可见目录；（3）**Broker action 提交时点**——`0 < B < 1` 的轻量块视为本决策分钟内完成，块内 `buy`/`sell`/`short`/`cover`/`close`/`cancel` 按当前 tick 提交并进入常规 `execution_lag_bars` 或竞价规则；`B>=1` 的动作先等到 `ready_at` 后第一个可报单 tick 再提交，然后再按该提交 tick 的常规规则进入撮合。等待期间（包括同 tick 内）这些动作通过 `ctx.broker.pending()` 暴露为 `pending_stage="substep_delay"`，供策略去重或撤销。若 ready 后的真实行情 tick 已无后续成交 bar，则按常规路径记录 `main_actions_unfilled/no_fill_bar_ahead`，不会静默顺延；若 `ready_at` 落在普通 off-session，则等下一个显式盘前/收盘竞价 tick 或交易分钟 tick。`B` 受 `decision_max_sim_minutes` 上限约束：超过即在 `ctx.substep` 初始化被拒（`ValueError`）；`B=0` 被拒；同一 tick 内 substep 名必须唯一、重名被拒，以保证预算映射无歧义。NL 由宿主串行服务（`_serve_nl_requests`），substep 真实墙钟约等于其中各 NL 时延之和，Agent 据此设定 `B`。除极小 Python 分支开销外，未包裹的实质 `main(ctx)` 时间会被 `enforce_substep_coverage` 拒绝；整 tick/整交易日仍受下文 `backtest_max_seconds_per_decision` / `backtest_max_seconds_per_trading_day` 硬上限兜底。`ctx.substep` 是执行无关的，live QMT 控制器可把各 substep 作为真实异步任务运行、并以同样的预算超限与 ready 后提交作为错误/执行合同。

**回测独立计时、成本上限与可观测性**

`backtest` 作为独立计时工具，其墙钟时间不计入 Fold 推理 deadline（`runner.py` 把回测耗时回补到 deadline），但单个 Fold 最多 `max_backtests_per_fold` 次回测，超出返回 `backtest budget exhausted`。两道随回放天数伸缩的真实墙钟硬上限（替代固定总上限）：单个决策（一次 `main(ctx)` tick，含其内 NL）超过 `backtest_max_seconds_per_decision` 由 `MainPolicyRunner` **立即杀掉**驱动并抛 `BacktestError`（该 tick 的硬截止，不再因 NL 重置）；某交易日累计 `main(ctx)` 计算超过 `backtest_max_seconds_per_trading_day` 在引擎层中止回放。完整验证触发任一上限即不可接受/冻结，迫使 Agent 缓存重计算、压低调仓/图构建成本。**这两道紧上限是真实墙钟、随机器负载浮动，故仅约束 Agent 迭代的 `mode="valid"` 验证回测**；最终评估（每个 Fold 的冻结 `test_000` 与 held-out，均 `mode="frozen_eval"`）改用更宽松的防挂死兜底 `backtest_final_eval_max_seconds_per_decision` / `backtest_final_eval_max_seconds_per_trading_day`（默认 900s / 3000s）：已在验证阶段满足紧上限的策略必须能跑完其最终评估，accept/held-out 结果不应因负载浮动而不可复现。该兜底只为杀掉真正的挂死——基于仿真时间的预算无法做到（单 tick 内死循环消耗 0 仿真分钟却占用无限墙钟，只有墙钟兜底能拦），且不作为接受门槛。**有意不设固定总上限**：单次回放的总耗时上界即 `交易日数 × backtest_max_seconds_per_trading_day`，随回放长度自然伸缩，无需另设一个不随长度变化的总墙钟上限。Environment 不预测回放耗时；Agent 用小 `replay_window` 试探得到的 `replay_wall_seconds` / `replayed_trade_days` 自行外推完整运行成本。回测可观测性：开始时发 `backtest_start`、回放期间按节流（≥30 天或≥30 秒）发 `backtest_progress` 心跳（含进度/已用时/累计订单数）、结束/中止保证有一条终止 `backtest` 事件（含外部中止的 `status="aborted"`）；summary 另含 `started_at`、`replay_wall_seconds`、`replayed_trade_days`、逐 substep 的 `substep_runtime`（count/total_real_wall_s/max_real_wall_s）、按阶段拆分的 `phase_seconds`（`strategy_compute`/`nl_service`/`timeview_build`/`state_merge`/`broker_match`）以及 `total_ticks`/`intraday_ticks`/`offsession_ticks` 计数，使 24h tick 网格的额外成本可审计。

**执行与资源预算一览**

默认值见 `pipelines/config.py`，逐项写入 run manifest。

| 控制项 | 默认 | 约束对象 |
|---|---|---|
| `per_call_timeout_seconds` | 300s | Agent 主 LLM 调用与 contract_check 校验的单次超时（不约束正式回放 tick——回放 tick 见 `backtest_max_seconds_per_decision`） |
| `max_fold_minutes` / `fold_deadline_at` | 60min | Fold 推理墙钟（回测耗时已回补排除） |
| `max_backtests_per_fold` | 30 | 单 Fold 回测次数（独立计时豁免的上限） |
| `offsession_tick_minutes` | 15min | 盘外（09:15–15:00 交易时段外）tick 的分钟 spacing；`0` 关闭盘外 tick（盘外 tick 只更新研究/状态，不下单） |
| `auction_enabled` | True | 是否在每个回放日插入盘前/收盘集合竞价决策 tick（关闭则只跑连续竞价分钟 tick） |
| `auction_close_time` | "14:57" | 收盘集合竞价决策 tick 时点，其下单成交于当日 15:00 bar 收盘价；`None` 关闭 |
| substep `budget_minutes`（fail-fast + submit timing） | Agent 声明（`B>0`，tick 内唯一） | substep 的真实墙钟上限 + `state_dir` 写可见性门控；块内 broker action 在 `0<B<1` 时按当前决策分钟提交，`B>=1` 时到 `ready_at` 后第一个可报单 tick 提交 |
| `decision_max_sim_minutes` | 60min | 声明预算 `B` 的上限（超过在 substep 初始化即被拒） |
| `backtest_max_seconds_per_decision` | 300s | 单个决策（一次 `main(ctx)` tick，含 NL）的真实墙钟硬上限，超限立即杀（仅 `mode="valid"` 验证回测） |
| `backtest_max_seconds_per_trading_day` | 900s | 单交易日累计 `main(ctx)` 计算的真实墙钟硬上限，超限中止回放（仅 `mode="valid"` 验证回测） |
| `backtest_final_eval_max_seconds_per_decision` | 900s | 最终评估（`frozen_eval`：冻结 `test_000` 与 held-out）单决策的宽松防挂死兜底，非接受门槛 |
| `backtest_final_eval_max_seconds_per_trading_day` | 3000s | 最终评估单交易日累计计算的宽松防挂死兜底，非接受门槛 |
| `nl_max_calls_per_decision_day`×决策天数（min `nl_max_calls_per_backtest`） | 10/日 | 每次回测 `ctx.nl` 调用次数（成本，与延迟分开） |

宿主 Broker 据此执行下单、撤单、成交、拒单（现金/保证金可用余额/T+1/手数/涨跌停/停牌/标的池/额度/融券限价）、约束和审计；隔离边界不变（策略只表达意图）。`ctx.positions` 只反映已成交持仓；在途（已报未成）单经 `ctx.broker.pending(ts_code)` / `ctx.broker.pending()` 暴露（当日可撤订单簿加提交延迟队列），策略据此对在途代码跳过重复下单或撤销过期委托。

分钟回放是默认口径：有非空 `intraday_1min.parquet` 时按真实分钟 bar 推进；缺失分钟数据的日期/股票，或缺少必要收盘分钟 bar 时，按日线合成 09:30/15:00 两根 bar 作为退化 fallback。`execution_lag_bars` 会按当日 bar 数收敛（`max(1, min(lag, n-1))`），使两根 bar 的退化日即便关闭盘前竞价也能在 15:00 成交、不至于整日零成交。盘前两 tick（09:15→09:30 开盘竞价、09:25→09:31 首根连续）与 14:57 收盘竞价 tick（成交于当日 15:00 bar 收盘）都在固定生效 bar 撮合、不受 `execution_lag_bars` 影响；各 tick 的竞价清算与滑点口径见本节前文竞价说明。回放区间最后一个交易日保留为剩余持仓的强制清仓日；临近收盘、其后无第 `execution_lag_bars` 根 bar 的决策无法成交，记 `main_actions_unfilled`；当日收盘仍挂着的限价单自动撤销。


### 3.3 回放 Profile、强制约束与做空模式

**默认研究回放 profile**（`gjzq_credit_v1`）

| 项目 | 默认口径 |
|---|---|
| 账户类型 `account_type` | `credit`（信用账户）；`stock` 为纯现金普通账户 |
| 初始本金 | run config 指定；未指定时使用研究默认值 |
| 佣金 | 按成交额 bps 计提，受最低佣金约束 |
| 印花税 | 按交易日期使用对应税率，只在卖出/开空相关方向计提 |
| 滑点 | bps 口径，买入上滑、卖出下滑 |
| 最大持仓数 | 默认不指定；由 Agent 在候选筛选、仓位和交易策略中自行控制 |
| 单票权重上限 | 默认不指定；由 Agent 在下单股数、权重和加减仓逻辑中自行控制 |
| 融资/融券保证金比例 | `fin_margin_ratio`/`slo_margin_ratio` 默认 1.0（交易所下限 100%；私募融券 1.2） |
| 融资利率 / 融券费率 | `fin_rate_annual` 0.0835 / `slo_rate_annual` 0.085，年化、按自然日计入合约（研究假设，记 `credit_rates_are_assumed`） |
| 担保品折算率 `assure_ratio` | 平坦近似 0.70（交易所上限：指数成份股 ≤70%、其他股票 ≤65%；未接入逐票折算表） |
| 授信额度 `fin_max_quota`/`slo_max_quota` | 默认 None（不设额度上限） |
| 维持担保比例 | 平仓线 1.30 强平；警戒/提取线仅审计记录 |
| 空头公司行为 | 当前按研究假设处理，接入真实规则后需更新 |

集中度约束默认交给 Agent 策略决策。只有显式研究或实盘风控配置要求时，run config 才可设置 `broker_profile.max_total_holdings` 或 `broker_profile.max_single_name_weight`，并必须在 run manifest 写明来源。


**信用账户模型（负债合约、利息与保证金）**

信用账户的会计遵循交易所《融资融券交易实施细则》（公式实现与来源见 `broker_core.py` 模块 docstring）：

- **负债合约**：每笔融资买入/融券卖出成交生成一份 `DebtContract`（对应官方 StkCompacts）。融资合约记未还本金（开仓名义+佣金，开仓不动现金）与归属的融资买入股份；融券合约记未还股份与融券卖出金额（毛额）。偿还按**最老合约优先、先息后本**；卖券还款的净所得偿还融资、余额留现金，直接还款从现金扣款并按偿还比例把融资股份释放为普通担保品。
- **利息**：融资利息按未还本金、融券费按未还股份×开仓价，均按**自然日**计入合约（周末/节假日照计），偿还时以现金支付；未付利息同时进入维保比例分母与保证金可用余额扣减项。
- **维持担保比例** = (现金 + 信用账户证券市值合计) / (融资未还金额 + 融券股份×市价 + 利息费用合计)；低于 `maintenance_closeout_ratio`（1.30）触发强制平仓（清仓所有持仓，融券负债随平仓即时归还）。**融资负债不会被清仓自动归还**：本金继续计息、由策略显式还款，权益已净额扣除，故不影响收益核算——这是有意的真实行为（欠款不会因平仓消失）。
- **保证金可用余额** = 现金 + Σ(担保品市值×折算率) + Σ[(融资买入证券市值−融资金额)×折算率] + Σ[(融券卖出金额−融券市值)×折算率] − Σ融券卖出金额 − Σ融资金额×融资保证金比例 − Σ融券市值×融券保证金比例 − 利息费用（浮亏侧按 100% 扣减）。新的融资买入/融券卖出按该余额门控（`insufficient_bail_balance`）；担保品买入仍按 `available_cash` 门控。融券卖出所得现金被冻结（只可用于买券还券），既不进 `available_cash` 也不作保证金。
- **标的池**：`margin_secs` 原始数据不区分融资/融券标的，同一集合（成交日逐日刷新、缺失回退决策日冻结集合）同时门控 `fin_buy`（`margin_secs_not_finable`）与 `short`（`margin_secs_not_shortable`）；`theoretical_short` 模式同时豁免两者。
- **普通账户**（`account_type="stock"`）：无负债/保证金/维保概念，`available_cash == cash`，只执行现金、T+1、手数、涨跌停、停牌与集中度约束。

**Broker 强制约束**

**Broker 每次调用强制项**

- 现金（担保品买入）与保证金可用余额（融资买入/融券卖出）约束；授信额度上限（如设置）。
- A 股 lot size（100 股）、手续费（含最低佣金）、滑点和印花税。
- T+1 可卖余额：当日买入/开空份额当日不可卖出/还券，`sellable_quantity` 在交易日推进后释放，不足部分阻挡并记录。
- 停牌、涨跌停限制；融资融券标的池、融券限价（uptick）规则、维持担保比例强平与负债利息。
- 如果 `broker_profile.max_total_holdings` 或 `broker_profile.max_single_name_weight` 被显式设置，Broker 会作为附加风控约束执行；默认 profile 不启用这两项限制。


**做空模式**

| 模式 | 规则 |
|---|---|
| `proxy_margin_secs` | 研究近似：可做空集合按成交日的真实 `margin_secs`（回放槽的逐日映射）门控，缺失该日时回退冻结决策日快照；不代表真实券源 |
| `broker_inventory` | 未来接入真实券源；应要求交易所融资融券资格、真实券源、数量覆盖和合约费率均可见 |
| `theoretical_short` | 研究模式，不检查券源；必须在 manifest 和结果中显式标记 |

默认 `proxy_margin_secs` 下，Broker 在成交那天判定可做空：成交前 `current_date` 推进到成交日，按 `shortable_by_date`（来自回放槽的逐 `trade_date` 集合）查该日真实 `margin_secs`，缺失则回退冻结的决策日 `shortable_codes`；这把 Agent 冻结的决策日快照与 Broker 的同日执行约束隔离开。不在该集合内的做空订单仍由 Broker 拒绝并记录 `margin_secs_not_shortable`。券商真实券源、逐票费率和担保比例明细接入后需更新本节。


### 3.4 结果目录

**正式回测输出**

```text
results/<phase>_<idx>/
  detailed_return.json
  orders.parquet            # 本次回放的全部 Broker 订单（成交/拒单）
  state_staging_audit.json  # 有 ctx.substep 暂存写入时：逐条 substep、ready_at、文件 hash、合并状态（区间结束仍未合并记 unmerged_at_region_end）
  nl_tool/                  # 策略调用 ctx.nl() 时有内容
```

`detailed_return.json` 至少包含总收益、long/short 收益、年化收益、Sharpe、最大回撤、胜率、turnover、订单状态、拒单统计、费用、信用利息（`credit_interest_accrued`/`credit_interest_paid`）、权益曲线、逐笔平仓/减仓和 Broker 事件。回测 summary 另记 `state_staged_writes`/`state_unmerged_writes` 计数，以及 §3.2 的 `phase_seconds` 和 tick 计数。

## 4. 运行日志、审计与验收

### 4.1 可信日志与核心文件

可信日志只能由 Runner、Execution Gateway、LLM Proxy 和 Broker 自动生成。Agent 的解释、note 或输出字段不能替代可信日志。


**核心运行文件**

```text
/mnt/artifacts/run_manifest.json
/mnt/artifacts/agent_trace.jsonl
/mnt/artifacts/results/<phase>_<idx>/
/mnt/artifacts/logs/
experiments/<id>/artifacts/run_<id>/host_run_manifest.json  # 宿主审计副本，不挂载给 Agent
```


### 4.2 Manifest、Trace 与读取权限

**Agent 可见 run manifest**

- experiment、epoch、fold、run、conversation ID。
- 决策时点、训练/验证可见区间和 snapshot hash；测试和 held-out 调度不写入 Agent 可见 manifest。
- 父产物 ID/hash、当前 artifact hash、冻结标记。Agent 可见 manifest 里的 `parent_strategy_artifact_id`（以及 `fold_id`）会投影为不透明引用 `strategy_ref_*`（和 `fold_ref_*`），因为原始 artifact id 内嵌 Fold 标签（`strategy_<epoch>_fold_<period>`）；该投影与 Agent 可见账本视图和系统提示词的实验事实一致，避免向 Fold Agent 泄漏原始周期标签。
- 父模型参数 hash、当前模型 artifact hash 和 combined hash。
- Broker profile、短券模式、成本参数和资源配置。
- runtime env 路径，以及普通 Fold 或元学习需要的实验参数摘要。
- 修改约束和 deadline。
- 关键结果目录和状态摘要。
- 元学习的 development 输入（`development_history`、`experiment_ledger_full`、`meta_learning_memory`）与 `taste_output` 一律写成 `/mnt/...` 沙箱挂载路径，不写宿主绝对路径，避免误导 Agent 去访问沙箱外不可见的位置。

宿主收集目录额外保留 `host_run_manifest.json`，用于完整审计测试/held-out 调度、测试 snapshot 和 frozen evaluation 结果；该文件不在 Sandbox 中挂载。

`agent_trace.jsonl` 是轻量事件流，包含工具调用（含 shell）、回测、Broker、LLM、context compact、NL、错误和锁定事件。事件共享 `experiment_id/fold_id/run_id/conversation_id/call_id/parent_call_id`，便于追溯。


**读取权限**

- Agent 在训练/验证期只读可见验证结果。
- 测试和 held-out 结果、日志、NL 明细和 Broker 事件不反馈给 Agent。
- 宿主可读完整审计目录。
- 冻结 artifact 的 `manifest.json` 是冻结元数据，不参与策略 artifact hash。


### 4.3 审计检查与验收清单

**审计检查**

- 工具调用状态、stdout/stderr（适用时）和错误状态完整。
- run manifest 包含关键版本、hash、路径和时间。
- 工具调用（含 shell）没有越权路径、网络访问或测试数据读取。
- `output` 无缓存、隐藏文件/目录和非法后缀。
- `models` 无缓存、隐藏文件/目录、非法后缀和超限文件。
- strategy/model hash、modification check hash、backtest hash 和 frozen eval hash 一致。
- Broker 拒单、未成交、强平和费用可追溯。
- NL evidence 能追溯到 as-of `text_id` 或 `source_hash`。
- API key 和 Authorization header 未进入日志。
- 失败显式报错，不能静默 fallback。


**验收清单**

- PIT 输入满足 `available_at <= decision_time`。
- `/mnt/snapshot` 与 run manifest 中的 decision input hash 一致。
- Sandbox 写入面只限 `workspace`、未锁定的 `output` 和未锁定的 `models`。
- `modification_check` 在正式回测前通过。
- `backtest` 写入完整结果目录和 manifest 摘要。
- Broker 对成交、拒单、费用、做空和强平事件有记录。
- 文本检索、NL Sub Agent 输出和 provider 调用可追溯。
- 冻结策略和模型产物在测试、held-out 前后 hash 不变。
- 所有失败条件显式报错并进入 trace。
