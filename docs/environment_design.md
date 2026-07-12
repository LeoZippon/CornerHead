# Environment 设计

本文档记录 Environment 层。Environment 负责准备 PIT 数据窗口、启动 Sandbox、提供 Agent 常用工具与受控服务入口、执行回测并写审计信息。Agent 可以在 Sandbox 内探索和写代码，但只能使用 Environment 暴露的数据、工具和受控服务。

**相关边界**

- 原始数据、源单位、刷新和审计见 [数据文档](data_documentation.md)。
- Agent 行为、可写产物和输出格式见 [Agent 设计](agent_design.md)。
- Step、Fold、Epoch 编排见 [Pipeline 设计](pipeline_design.md)。
- 控制台和 QMT 实盘边界见 [部署文档](deployment_documentation.md)。
- 参数默认值和范围见 [参数参考](parameters_reference.md)。

**职责边界**

Environment 负责把已准备的数据构造成 PIT Snapshot，运行 Sandbox、工具、受控服务、模拟 Broker 和回放，并记录可信运行证据。Environment 不负责下载或定义 raw 数据源、决定投资策略、编排跨 Fold 实验或连接真实券商。

**术语说明**

| 中文名 | 代码/英文名 | 含义 |
|---|---|---|
| 环境层 | `Environment` | 准备 PIT 窗口、启动 Sandbox、提供 Agent 工具与受控服务入口、执行回测和写审计信息的环境层 |
| 按时点可见 | PIT | Point-in-time，只使用决策时点已经可见的数据 |
| 沙箱 | `Sandbox` | Agent 运行的隔离容器或本地开发执行环境 |
| 运行驱动器 | `Runner` | 驱动 Agent 会话、切换 Snapshot、调用工具和记录日志的程序 |
| 执行网关 | `Execution Gateway` | Sandbox 与工具或可信服务之间的入口，负责权限、路径、运行约束和日志 |
| 大模型代理 | `LLM Proxy` | 宿主侧大模型接口代理，保存 API key 并记录调用 |
| 运行产物 | `artifact` | 单次运行产生的策略文件、回测结果、Broker 事件和 NL 日志 |
| 清单 | `manifest` | 记录输入、输出、时间范围、配置和关键版本的文件 |
| 模拟券商 | `Broker` | 接收交易意图并生成成交、拒单和持仓状态的模拟交易组件 |
| 最终留出评估 | `Held-out` | 全部 Development Fold 完成并冻结最终策略后运行的独立评估区间 |

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
  - [3.2 Broker 设计、账户模型与强制约束](#32-broker-设计账户模型与强制约束)
  - [3.3 main(ctx) 执行模型与 Timeview 可见性](#33-mainctx-执行模型与-timeview-可见性)
  - [3.4 ctx.broker 接口](#34-ctxbroker-接口)
  - [3.5 订单生命周期与撮合规则](#35-订单生命周期与撮合规则)
  - [3.6 substep、状态写入与计算延迟](#36-substep状态写入与计算延迟)
  - [3.7 回测限时、可观测性与结果摘要](#37-回测限时可观测性与结果摘要)
- [4. 运行日志、审计与验收](#4-运行日志审计与验收)
  - [4.1 可信日志与核心文件](#41-可信日志与核心文件)
  - [4.2 Manifest、Trace 与读取权限](#42-manifesttrace-与读取权限)
  - [4.3 审计检查与验收清单](#43-审计检查与验收清单)

## 1. 数据可见性与 Snapshot

本章定义 Snapshot 的数据内容、路径、PIT 可见性和单位口径。

所有进入正式策略的数据必须同时满足 PIT 合同和单位合同。


### 1.1 Snapshot 数据域与准备窗口

准备窗口在实验启动前冻结并写入运行清单。窗口可以按数据域分别调整；快照清单记录每个域的实际行数和日期覆盖。

| 数据域 | Snapshot 文件 | Agent 可见形态 | 用途与窗口 |
|---|---|---|---|
| `daily` | `daily.parquet` | 已对齐的日频行情、估值和交易约束 | 日线研究、估值、强制清仓和分钟缺失时的退化回放；按月配置历史窗口 |
| `intraday_1min` | `intraday_1min.parquet` | 按交易日整理的真实分钟 Bar | 分钟回放、竞价和日内研究；按交易日数配置样本窗口 |
| `fundamentals` | `fundamentals.parquet` | 按公告可见时间归并的多版本财务事件 | 财务和经营质量研究；按可见披露月份配置窗口 |
| `events` | `events.parquet` | 按来源标签区分的资金、两融、股东、榜单和事件 union | 事件研究；字段与源单位按来源解释，按月配置窗口 |
| `macro` | `macro.parquet` | 国内宏观、利率、政策、指数、外汇和全球上下文 union | 市场背景、择时和相对强弱；按月配置窗口 |
| `text` | `text_index.parquet`、`text_library/` | 可见文本索引与正文分片 | PIT 文本检索；每条证据在本次快照内有唯一标识，按月配置窗口 |
| `universe` | `universe.parquet` | 决策日在市股票、历史名称和当时行业归属 | 避免幸存者偏差与未来名称泄漏；按决策日生成，不使用月份窗口 |
| 股票筛选 | （SnapshotConfig `screen_*`） | 实验级研究宇宙：剔除 ST（按决策日在市名称）、剔除新股（上市 <N 天）、流通市值带（亿元）、股价带、板块子集 | 只用决策锚点已知信息计算，整个区间冻结不重筛（属性缺失 fail-closed）；同一集合限制 universe/daily/分钟/竞价/事件/财务全部逐股域（决策快照与回放槽一致），显著降低数据量与回测耗时；manifest 记录筛选配置与结果规模 |

精确 raw 成员、源单位和未纳入数据集见 Data 文档。

- 日终涨停榜单类研究标签因行级可见时间不完整和历史字段回写风险，不进入 Agent 可读域；daily 中的涨跌停价格约束仍正常提供。
- 基准和风格报告使用回放时冻结的运行数据，不在事后重新读取可变 raw 数据。

运行清单记录实验生效的数据域窗口，快照清单记录该次构建的实际窗口、行数和日期覆盖。Fold 输入窗口只是调度摘要；各域单独覆盖后，实际可见历史以生效配置和快照清单为准。


### 1.2 Snapshot 路径与数据槽

**路径概念**

- `/mnt/snapshots/<stage>`：Agent 可见或回放用的数据槽。`train` 是 `valid_decision_input` 的 Agent-visible alias，供策略探索使用；`valid` 是验证回放数据区间，`test` 是测试类回放区间。
- `/mnt/snapshot`：`backtest` 正式执行时绑定的当前决策输入视图，只包含本次决策时点前已可见的数据。

**Sandbox 数据槽**

```text
/mnt/snapshots/
  train/
  valid/
  test/
```

权限和用途：

| 路径 | 用途 | Agent 在策略探索阶段的权限 | 正式回放用途 |
|---|---|---|---|
| `/mnt/snapshots/train/` | 策略探索输入，等同 `valid_decision_input` 的只读 alias | 只读可见 | 不作为正式输入 |
| `/mnt/snapshots/valid/` | 验证回放区间 | 只读可见 | 不可依赖 |
| `/mnt/snapshots/test/` | 测试或 held-out 回放区间 | Agent 不可读 | 冻结评估读取，但不反馈给 Agent |
| `/mnt/snapshot/` | 当前决策输入视图 | 只读可见 | `main.py` 正式运行时只读 |

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

`/mnt/snapshot` 是正式策略可读的 PIT 裸数据窗口：

- 只做可见性过滤、daily 单位归一、同键拼接和交易约束字段对齐（跨分区重复行按数据集防御性去重并记入 manifest）。
- 构建全程对数据更新 runner 的锁持共享 flock，manifest 记录 raw 世代戳；`updating` / `dirty` 世代直接拒绝，构建期间数据湖世代变化也立即失败。
- 不预构建 alpha、滚动收益、均线、波动率、综合分数或候选排名。
- 未进入快照的数据不能由正式策略依赖；需要的派生特征由策略在可见窗口内计算。

规则：

- `text_library/` 是 as-of 正文库，正文或片段必须由 `text_index.parquet` 引用。
- 宿主 `runtime/snapshot_views/` 保存多个决策输入视图，不挂载给 Agent。
- 宿主 `runtime/current_snapshot/` 是从 `snapshot_views/` 中选定视图刷新出的当前镜像。

示例：

```text
/mnt/snapshots/
  train/   2020-01 至 2021-09（valid_decision_input alias）
  valid/   2021-10 至 2021-12
  test/    2022-01 至 2022-03

宿主 runtime/snapshot_views/
  valid_decision_input/  2020-01 至 2021-09
  test_decision_input/   2020-04 至 2021-12
```


### 1.3 PIT 可见性合同

**PIT 截止条件**

```text
available_at <= visibility_cutoff
```

如果数据没有可靠发布时间，Environment 必须使用保守规则延后可见，或从本次窗口中排除。窗口数据可以比配置短，例如刚上市股票不足完整历史，或某个研究数据保留下限晚于窗口起点。

逐 tick 数据视图只使用当前仿真时钟下已经完成的最新刷新节点，并仅暴露行级可见时间不晚于该节点的数据。文本索引、正文库和 NL 使用同一仿真时钟与文本门禁。

**行级 `available_at` 来源**

| 数据域 | 行级 `available_at` 来源 |
|---|---|
| `daily` | `daily.parquet` 内的日频分区从 `trade_date` 推出行级可见时间：日线按收盘后可见；每日指标、估值和股本字段按盘后更新完成后可见；复权因子、涨跌停价格和停牌约束按各自盘前可见时间进入。 |
| `intraday_1min` | 历史分钟线为该分钟 `trade_time`，表示 bar close 后可见；盘中当日实时行情走 `ctx.bars` / `ctx.price`，不写入持久 snapshot。 |
| `fundamentals` | 财报、指标、分红、业绩预告/快报、披露计划和主营构成按公告、报告期与版本生成可见时间；缺公告时间时保守回退或排除。 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等异构事件按来源字段生成 `available_at`；T+1 或盘后数据使用盘后或下一交易日的保守可见时间，事件发生日不等于可见日。 |
| `macro` | 有明确发布时间的宏观、政策、利率、全球事件和跨市场数据按发布时间生成 `available_at`；只有统计期或报告期而无发布时间的数据，按数据集配置的保守可见时间进入；没有可靠回退规则则排除。 |
| `text` | 公告、新闻、研报和政策文本按 `anns_d`、`rec_time` 或文本管线给出的 `available_at` 门控；回填采集的历史文本不得早于其可证明发布时间可见。 |
| `universe` | `stock_basic` 的上市、退市和状态字段按决策日在市口径生成：`list_date <= decision_date` 且未在决策日前退市；不得用当前上市名单回填历史。 |

**财务 PIT 索引**

财务表保留多版本记录，财务事件索引用公告时间确定每个版本最早何时可见：

- 公告日优先取明确的最终公告日，其次取普通公告日。
- 只有日期时，统一在公告日 18:00 后可见。
- 启用财务域时，索引缺失或审计失败会阻断快照构造。
- 其他需要行级可见时间的数据集缺少该字段时同样报错，不能静默放行。


### 1.4 单位与特殊口径

**单位合同**

raw 侧单位见 Data 文档“原始单位”。

**适用范围：仅 daily 域（执行关键字段与日线字段）**。快照构建只对 `daily.parquet`（决策快照与回放槽）做单位归一：

| 类型 | 标准单位 |
|---|---|
| 金额 | 元 |
| 成交量/股本 | 股 |
| 比例、收益、换手 | 小数，例如 5% 记为 `0.05` |
| 利率和费率 | 优先小数；确需 bps 时字段名必须带 `_bps` |

events、macro、fundamentals 和 text 保留源单位：

- 异构 union 的字段按来源解释；同名字段跨域不同单位是常态。
- 快照清单为 daily 附转换清单，为其他数值研究域标记源单位口径；文本由来源标签解释。
- Agent 不得把 daily 单位合同外推到其他域；跨域计算必须显式换算成派生列。
- 精确源单位见 Data 文档。

daily 域的原始单位和转换规则写入快照清单。研究域保留源单位，策略在跨域计算前必须显式换算；未知单位字段不得作为交易依据。这是策略与审计约束，Broker 无法根据字段血缘自动识别并拒绝订单。

**特殊口径修正**

历史 09:30 分钟条用作开盘竞价近似时，对深圳股票生成校正后的量额字段：

- `00*.SZ` 乘 0.76，`30*.SZ` 乘 0.58；其他市场和时点保持 1.0。
- 只改竞价近似派生字段，不改 raw 分钟线、日内汇总或收盘竞价。
- 快照清单记录规则、字段、倍率、市场和生成时间。
- 派生字段缺失时不得静默退回未校正字段模拟开盘竞价。

## 2. Sandbox、Runner 与 Agent 工具

本章定义 Sandbox 运行环境、Runner 工具合同、产物修改控制以及 NL、LLM 和联网边界。

### 2.1 Sandbox 环境与运行路径

正式实验默认使用 Docker Sandbox。CLI 只有显式传 `--local-dev` 时才使用本地执行器；本地模式只用于开发和单元测试，不作为正式安全边界。

**基础合同**

| 项目 | 要求 |
|---|---|
| 用户 | Agent 代码以非 root `agent` 用户执行；rootless Docker 下容器内 `agent` 映射为宿主 subuid |
| 挂载与权限 | 宿主准备并挂载快照、运行事实和 Agent 工作目录；只开放合同内写入面，其余用只读挂载、文件权限和测试槽权限限制 |
| 网络 | 普通 Fold 默认断网；元学习默认经桥接网络访问公网，但不能借此访问宿主回环地址。托管代理仅在存在配置时按会话启动并在结束后清理，默认不接管 Shell 命令；任何代理密钥或配置正文都不进入 Prompt 和审计产物 |
| 凭据与密钥 | 代码仓库和模型仓库 Token 是元学习的默认透传候选，研究者可追加变量名；平台只记录注入名称，普通 Fold 不注入。元学习 Shell 能读取获准变量，必须避免输出 |
| Python | Docker 镜像使用 Python 3.11；准确依赖、工具和版本由运行环境事实记录 |
| 本机环境 | 本机脚本、测试和 cron 使用 `~/miniconda3/envs/quant`，与 Docker Python 独立 |
| 包安装 | 普通 Fold 不安装新包，依赖由基础镜像或元学习派生镜像提供；元学习可在开放网络时临时试装依赖，需继承给后续 Fold 的稳定依赖必须声明为派生镜像请求 |
| 环境事实 | Agent 可读的运行环境记录列出 Python 包、CLI、网络、安装策略和资源摘要；镜像变化必须先落实到运行环境，再更新记录 |
| 运行时责任 | Sandbox 内的可信驱动只加载策略、提供受控上下文并传回交易动作；现金、持仓、负债、撮合和风控真相始终由宿主 Broker 维护 |
| 工具缓存 | 包、模型和编译缓存写入临时目录，不进入正式工作区或产物采集范围 |
| GPU/资源 | 分配结果和资源限制写入 run manifest |
| 写入面 | 仅 `/mnt/agent/workspace`、未锁定的 `/mnt/agent/output` 和未锁定的 `/mnt/agent/models` |
| 可信产物 | `/mnt/artifacts` 由 Environment 写，Agent 只读 |
| Fold 时间 | 按实验预算运行；Runner 接近 deadline 时最多发一次收尾提示，当前默认见 [参数参考](parameters_reference.md#3-回放执行与预算) |

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
    <phase>_<idx>/
      detailed_return.json
      orders.parquet              # 有订单记录时
      positions_eod.parquet       # 有日终持仓记录时
      state_staging_audit.json    # 有状态暂存写入时
      nl_tool/
  steps/                        # Step 产物树：tree.json/tree.txt + <node_id>/{output/,models/} 快照与该次验证的 detailed_return.json/style_analysis.json/orders.parquet
  logs/

/mnt/agent/
  workspace/
  output/
  models/
  .runtime/
```

约束：

- `/mnt/agent/workspace/` 是临时探索区，不冻结、不回放、不复制到下一 Fold。
- 归档工作区和正式产物时跳过缓存与工具临时目录；它们不是实验产物，且受限权限文件可能导致宿主采集失败。
- `/mnt/agent/output/` 是正式策略代码写入面，根目录固定 `main.py`，可包含受控文本/代码子目录。
- `/mnt/agent/models/` 是正式模型参数写入面，可包含受控模型参数子目录。新增 Python/npm/apt 依赖属于 Sandbox 镜像层，不写入 `models/`。
- `/mnt/agent/.runtime/` 是宿主预创建并锁定的隐藏运行目录，用于放置 NL RPC 等临时受控文件；Agent 不把它当作探索区或正式产物目录。
- `/mnt/agent/` 根目录不是写入面；临时文件、缓存和下载内容应放入 `workspace/`，正式产物分别放入 `output/` 或 `models/`。
- `/mnt/artifacts/parent_output/` 是父产物基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/parent_models/` 是父模型参数基准，只读且 hash 写入 manifest。
- `/mnt/artifacts/runtime_env.json` 是 Sandbox 运行环境契约，记录 Python 包、CLI 工具、网络和包安装策略；Agent 可读，正式策略代码不得硬编码读取。
- Agent 可见的数据摘要提供各视图的文件规模、行数、日期覆盖和大表访问提示。主决策视图另给关键列和空值计数；`train` 与 `valid` 数据槽只给规模和覆盖。
- 正式策略不得硬编码读取数据摘要。完整 schema 查快照清单或 Parquet metadata，大表抽样和聚合应使用列裁剪与日期过滤。
- 系统 Prompt 的“当前实验事实”只呈现身份、可见性、窗口、预算、路径、产物合同、数据摘要、Broker/replay 和工具能力。
- 测试时间、held-out 范围、下一 Fold 排程和测试快照身份只留在宿主审计面，不进入 Agent 首屏事实块。
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
- 主对话达到配置阈值后触发语义压缩，只合并上次摘要后的新增消息并保留最近原文。
- 压缩受 Fold 截止时间和独立超时约束，并为后续主模型调用预留最小时间。
- 单次失败写入 trace 并允许有限重试；连续失败后熔断，改用确定性裁剪，不中断 Fold。
- 在 deadline 后停止新的 Shell、服务调用和 LLM 调用。


**Agent 工具入口**

| 工具 | 作用 | 关键边界 |
|---|---|---|
| `grep` / `glob` | 在可见目录中按模式搜索文件名或内容，返回分页结构化结果 | 不能写入，不能访问测试或隐藏路径 |
| `read` | 按行号读取文件（可分页）；读要编辑的代码优先于 shell `cat`/`head` | 只读；不访问测试或隐藏路径 |
| `shell` | 在 Sandbox 内探索并写受控目录；元学习联网时可使用开发工具 | 不是宿主 Shell；普通 Fold 断网；调用可限制输出和超时，长输出落盘并返回引用 |
| `write_file` / `edit_file` | 在 `workspace`/`output`/`models` 下创建/覆盖或精确编辑文本产物 | 只写受控根；`edit_file` 的 `old_string` 必须唯一匹配（staleness 检查）；`output/README.md` 只读；写锁后拒绝 |
| `explore` | 委托数据探查 Sub Agent 调查具体问题并返回摘要 | Prompt 要求只读探查，但与主 Agent 共用 Shell 和写入权限，因此不是只读安全边界；workspace 误写只是临时副作用，正式产物误写才进入修改检查和冻结哈希 |
| `web_search` | 元学习联网检索 | 仅元学习可用；每次调用声明 engine、perspective、query 和 max_results；结果写 trace |
| `web_fetch` | 元学习读取公开网页 | 仅元学习可用；宿主侧只读 GET，默认直连，`use_proxy=true` 才允许使用 active 代理；只支持 http/https 文本或 HTML，跨 host redirect 不自动跟随，结果写 trace |
| `modification_check` | 校验正式 `output` 修改量、`models` 格式/大小和父产物 hash | 无业务参数；不检查 `workspace` 或结果目录 |
| `backtest` | 执行 `output/main.py` 并回放交易；Agent 可传的业务参数只有 `replay_window`（探针只返回耗时/tick/substep/订单生命周期统计，不产生收益指标、成交明细与归因文件——被探测窗口是策略的未来） | 消费并校验当前 snapshot；每次调用创建唯一结果目录 |
| `step_rollback` | 把 `output/`（默认含 `models/`）恢复为 Step 产物树指定成功节点的快照，并把 `current_node_id` 移到该节点，后续通过验证的回测记录为其子节点（形成分支谱系） | 仅 `step_tree_enabled` 时可用；失败节点无快照不可恢复；恢复后按节点记录 hash 校验快照完整性；未验证的工作副本修改被覆盖；修改约束仍相对本 Fold 父产物度量；写锁后拒绝 |
| `ask_user` | 暂停并向研究者提交方向性问题（附现状总结，≤4000 字符），等待答复注入工具观察 | 等待不消耗推理预算；无人值守（auto/CLI）立即返回 unattended；仅交互式运行由 worker 注入通道 |
| `finish_fold` | 当前 Fold 停止修改 | 无业务参数；要求当前 hash 已有成功完整验证回测（`replay_window` 调试不算）+ 修改检查 + 轻量合同检查，通过后只读锁定 `output/` / `models/` 并清理 Sandbox 内 Agent 后台进程 |

**Trace 与工具规格**

- 工具 trace 记录规格版本和结果策略。Shell 另记录读、列举、搜索、写、无状态或未知等粗粒度命令类型，仅供审计统计；权限仍由 Sandbox、文件系统、网络和阶段策略执行。
- 工具调用采用 provider 原生 function calling。工具名称和参数 schema 随请求下发；模型返回结构化调用后，Runner 按同一 schema 强校验再分发，不要求模型在正文中手写动作 JSON。
- 系统 Prompt 只保留工具表和关键边界；参数语义、输出预算、分页方式、重试提示和失败原因尽量下沉到工具 schema、字段 description 和 `ToolError.error_type/reason/retry_hint`。

**调度与并行**

- 一轮可以包含多个工具调用，每个调用单独返回结果。
- 互相独立且声明并发安全的只读工具可并行；写入、Shell、探索、检查、回测和结束等有状态工具按因果顺序串行。
- `done`/`finish_fold` 等终止工具执行后，同一轮后续工具会被取消，避免终止验收后继续修改。
- Runner 的历史裁剪和上下文压缩必须保持 `tool` 结果不脱离对应的 `assistant` 工具调用。

**Sub Agent 执行边界**

- NL Sub Agent 和 Explore Sub Agent 复用同一原生工具循环。Explore 继承 Fold 截止时间，只回答委托问题，不替主 Agent 做最终策略综合；“只读”是提示词约定，不是独立权限层。
- Explore 单轮被 `finish_reason=length` 截断或遇瞬时 provider 错误时，不让整个探查失败；Runner 停止循环并强制一次简洁最终摘要。Explore 的 `max_tokens` 需要留出长工具调用（如 DuckDB SQL）和摘要的余量。

**上下文管理**

- 长 `reasoning_effort` 轮次默认请求 SSE 流式响应，并在客户端合并 tool-call delta 为统一完成结果。
- 上下文管理依次使用三层：清理超大旧工具结果、确定性摘要、低成本模型语义压缩。
- 主要按估算 Prompt token 触发，消息数只作高位安全上限；裁剪或压缩会重置模型前缀缓存。
- 主对话按 prompt/completion/reasoning 以及缓存命中/未命中累计 token，并写入 session 摘要的 `token_usage`（含 `cache_hit_ratio`），用于权衡裁剪/压缩强度。


**Shell 执行边界**

- Shell 不尝试静态解析复杂 Bash、路径或写目标。硬边界由只读挂载、父目录权限、受控写入目录、测试槽不可读权限和普通 Fold 断网共同执行。
- Shell 工具层只负责运行控制和审计：检查当前阶段是否允许执行、`finish_fold` 后是否已写锁、参数是否合法，执行超时和输出预算，并把命令、退出码、输出位置和粗粒度 `command_kind` 写入 trace。
- Shell 命令自身失败不转成 `ToolError`，而是返回非零 `exit_code` 和 `stderr`。超过内联预算的已捕获内容落盘并返回引用；超过宿主捕获上限的尾部会被截断并显式标记。Tool 层拒绝才返回结构化错误、原因和重试提示。
- Explore Sub Agent 与主 Agent 共用同一 Shell 执行边界。workspace 误写不会冻结或继承；只有对正式策略或模型产物的误写才进入修改检查和冻结校验。提示词约定不是权限保证。
- Prompt 与工具 schema 都要求 Agent 不使用 `2>/dev/null` 隐藏错误。命中该模式时，shell 结果附带非阻断的 `stderr_suppression_reminder`；stderr 应原样进入 trace。
- Shell 超时首先在容器内终止整个进程组，宿主执行截止只作更长兜底。容器由 init 进程回收孤儿和僵尸，避免被杀子进程残留。

**受控文件工具的已知限制**

- 读、写、编辑、grep 和 glob 先拒绝绝对路径、父目录与隐藏组件，再解析真实路径并检查其位于批准根目录内。
- 校验与最终打开仍是分离的路径操作，没有通过目录文件描述符逐层打开并拒绝符号链接。可写进程可能在两步之间切换路径目标，因此这套检查不能视为抗符号链接竞态的安全边界。
- Docker 挂载和测试槽权限仍是主要隔离层。工具结果通常映射为 Sandbox 路径；映射失败或本地开发执行时可能退回宿主路径，仅供审计，不应进入策略代码。


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

Sandbox 内的 `nl()` 只提交请求并等待响应。宿主按以下顺序处理：

1. 校验请求、仿真时间、配额和决策截止时间。
2. 只检索当前时点可见的文本；单股请求可附加当时可见的公司上下文。
3. 启动有界的文本分析会话，允许继续调用只读文本检索。
4. 返回自由文本、证据引用、工具摘要和错误状态，并写入本次回测审计目录。

NL 最终回答是自由文本：

- 证券代码可选；传入时作为公司上下文和排序提示，不是硬过滤。
- 不传证券代码时，可检索事件、主题、行业、宏观或市场级文本。
- 返回状态、范围、正文、工具摘要、证据和错误等字段。
- 策略若需要分数、标签或过滤条件，必须自行解析正文并处理失败。

文本检索采用 DuckDB/RE2 原地扫描、列裁剪和 LIMIT：

- pattern 最长 256 字符，不支持反向引用和环视。
- 不支持或超长模式返回可修复错误。
- 大语料不整体载入内存，只缓存返回片段。

NL 共享当前决策的绝对墙钟截止时间：

- 每轮 provider 超时不超过剩余时间；发生钳制时禁用重试。
- 截止时间耗尽后不再发起调用，直接返回失败。
- 单决策最多被一次有界 HTTP 调用拖延，不会继续完整多轮任务。

**NL 结果写入**

```text
results/<phase>_<idx>/nl_tool/
  nl_requests.jsonl
  search_requests.jsonl
  evidence.jsonl
  nl_llm_calls.jsonl
```

NL 临时通信目录由宿主创建并锁定：请求侧只追加，响应侧只由宿主写入。回测后清理临时文件；正式审计只认结果目录中的 NL 记录。

失败的 nl() 调用返回解释性反馈而非裸错误：结果带 `feedback` 字段（失败原因 + 退化建议——配额耗尽/未配置代理不应重试，超时/偶发失败可在后续 tick 重试一次），`status`/`state`/`error` 保持稳定供程序分支；策略必须按 status 降级，不得因 NL 失败崩溃。

NL 证据必须来自当前时点可见的“本次快照内唯一文本标识”或截断文本载荷 hash：

- 没有可见证据时必须说明不足，不能伪造引用。
- 策略自行决定忽略、降权、重试或不交易。
- 发布时间误差、召回偏差、模型常识污染和自由文本解析失败都属于策略风险。
- NL 结论不能覆盖 Broker 约束、交易成本或 PIT 合同。

**NL 调用配额（成本，与延迟分开）**

每次回测的 NL 总配额按“日均上限 × 决策天数”计算，可再用单回测上限收紧。超出配额时返回预算耗尽，策略自行降级；调用仍必须位于 substep 内，耗时受其时间预算约束。


**LLM API 边界**

平台管理的 Agent 主对话、上下文压缩和 NL 模型调用只能经宿主代理。元学习 Sandbox 是单独的联网研究面：其中的 Shell 进程能够自行访问公网，若获准注入凭据也能调用外部服务；这类流量不属于宿主模型代理的完整审计范围。

- Agent 主对话由 Runner 触发，记录到本地 conversation log。
- context compact 由 Runner 触发，默认使用低成本无 thinking 模型；它只生成继续会话所需摘要，不调用工具，不进入 Sandbox。
- NL 工具调用由宿主 NL 服务触发，记录到回测结果目录的 `nl_tool/`。
- 主对话和 NL 默认使用 provider 支持的深度推理配置；实验可以显式覆盖用于消融或调试。上下文压缩默认使用低成本、无 thinking 的配置。
- 元学习的网页搜索和抓取由宿主工具执行。启用搜索时，三个规定研究视角都必须至少有一次非空成功检索，才能结束会话。
- 网页抓取默认直连，只有显式请求才使用当前代理；仅支持公开 HTTP(S) 文本的只读 GET 与有界落盘。
- 网页抓取不支持 cookie、认证或自定义 header、POST、登录、浏览器渲染、JavaScript、PDF 或二进制解析。
- 元学习可由实验配置显式开放 Sandbox shell 网络做工作区内探索，并通过 `workspace/sandbox_environment.json` 申请后续 Fold 继承的依赖、由 Pipeline 构建派生镜像；该能力不替代 `web_search` 的三视角要求，也不开放给普通 Fold。
- Web Search provider 在宿主侧执行有限重试和限速；Semantic Scholar 使用每 key 共享的文件锁节流并对 429/5xx 做指数退避，避免单次短时限流直接结束元学习。
- 平台管理的请求不把 API key、Authorization header 或 provider client 放入 Prompt、策略产物或日志。
- 元学习容器通过默认候选或显式追加的环境变量名接收第三方 Token。平台不主动记录值，并对常见格式做脱敏；任意 Token、编码后内容或 Shell 主动输出无法保证全部识别，使用方必须避免打印凭据。
- provider 超时不能无限阻塞 Fold；超时、重试和失败策略必须写入 trace。
- provider 返回的 reasoning 或内部思考只进入审计日志；正式结构化字段取最终 content。
- 测试和 held-out 的 LLM/NL 明细不反馈给 Agent。

**Provider 调用记录**

- provider conversation log 按天+进程分文件（`<dir>/deepseek/<model>/<YYYYMMDD>-p<pid>.jsonl`）：并行 HITL worker 各写各的文件，无跨进程交错风险；进程内调用串行。
- 每次逻辑调用生成 `call_id`：完整（脱敏后）payload 只随首次 attempt 的 `started` 记录保存一次，重试与终止记录经 `call_id` + `request_hash` 关联，不重复嵌入历史。
- 终止记录附原始 provider 响应、`response_hash`/`response_id`、usage、错误与重试信息；模型、超时、耗时齐备。
- run/fold/conversation 级归属由 `agent_trace.jsonl` 的 `llm_call` 事件与 `nl_tool/` 明细承担（见下）；conversation log 是原始请求/响应审计层。

Agent trace 按事件记录主对话和上下文压缩：

- 主对话只保存本轮新增消息、消息总数、最终内容和工具调用，不在每轮重复整段历史。
- 顺序拼接各轮增量即可恢复完整对话；完整原始请求与响应仍由 provider conversation log 承担。
- 压缩事件记录 provider、模型、触发 token 估算、调用次数、压缩前后消息数、摘要身份、用量、状态和错误摘要。

## 3. 策略执行、Broker 与回放

本章定义策略逐 tick 执行、模拟 Broker、订单撮合、计算延迟和回测结果合同。

### 3.1 回测流程与阶段

`backtest` 的正式流程：

1. 校验策略、模型和决策输入身份。正式回测要求当前修改检查通过；检查缺失或过期时，工具自动刷新检查，而不是沿用旧结果。
2. 创建唯一结果目录，隔离本次正式输出。
3. 启动常驻策略进程；策略只能通过受控上下文访问当前快照、只读模型和暂存状态。
4. 按仿真时钟推进 PIT 视图与市场 tick。盘中使用真实分钟 Bar，盘外按较疏的固定间隔提供研究和计划维护时点。
5. 每个决策 tick 调用一次市场级 `main(ctx)`；NL 请求由宿主并行服务，交易动作先形成意图。
6. 宿主 Broker 按提交延迟、账户约束和市场规则逐 Bar 撮合，并推进现金、持仓、负债和订单状态。
7. 区间末执行清算尝试和期末盯市，再写不可变结果与审计证据。

结果生成规则：

- 收益摘要、Broker 事件、策略/模型身份和清单摘要固定生成。
- 订单、日终持仓和状态暂存审计只在存在对应记录时生成，不创建无意义的空 Parquet。
- 风格归因直接消费 Broker 权威日终持仓，不从成交记录反推持仓。

临时 Python 回测、Shell 中的手工脚本和 notebook 只能作为探索，不构成正式 valid/test/held-out 结果。正式结果只能由 `backtest` 写入。


**Valid 与 Frozen Eval**

| 模式 | 策略输入 | 回放区间 | 结果目录 | Agent 可见性 |
|---|---|---|---|---|
| `valid` | 验证决策输入 `/mnt/snapshot` | `/mnt/snapshots/valid` | `results/valid_<idx>/` | Agent 可读 |
| `frozen_eval` | 测试或 held-out 决策输入 `/mnt/snapshot` | `/mnt/snapshots/test` | `results/test_<idx>/` 或 `heldout_<idx>/` | 不反馈给 Agent |


### 3.2 Broker 设计、账户模型与强制约束

Broker 不内置任何交易策略，只暴露按股数操作的基础原语和查询接口；交易策略由 Agent 在 `output` 中以函数实现，并在回放时调用这些原语。Agent 不能直接写成交、持仓或收益。

每次实验同时运行普通账户和信用账户：

- 两账户的现金、持仓和 T+1 独立，互不担保。
- 同一证券可以在普通账户做多、在信用账户融券做空；单个账户内同一证券只保留一侧。
- 现金只能按盘前划转合同在账户间移动。

**双账户研究口径**

- 普通账户和信用账户分别配置初始资金，组合权益为两者之和。
- 佣金、最低佣金、过户费、印花税和市价滑点由统一成本模型执行；固定滑点不随订单规模和买卖价差变化，是明确的研究近似。
- 融资融券参数覆盖保证金、利率、合约期限、折算率、授信额度和维保线；逐票折算率、真实券源与逐票费率尚未接入。
- 持仓数和单票权重默认交给策略控制；实验显式配置后由 Broker 强制执行。
- 公司行为可建模现金红利和送转；差别化红利税和配股尚未建模。

全部默认值、枚举和构造期范围见 [参数参考](parameters_reference.md#4-broker-profile账户成本与信用)。

集中度默认由策略控制。只有实验显式配置时，Broker 才执行组合持仓数或单票权重上限，并把生效值和来源写入运行清单。

**信用账户模型（负债合约、利息与保证金）**

信用账户会计按交易所融资融券规则建模：

- **负债合约**：每笔融资买入或融券卖出生成独立合约。融资记录本金和股份，融券记录股份和卖出金额。到期按配置决定是否展期；偿还按最老合约优先、先息后本，多余资金留在信用账户。
- **利息**：融资按未还本金计息，融券按未还股份乘开仓价计费，均按自然日 /360 累计。未付利息进入维保分母并扣减保证金可用余额，偿还或平仓时以现金支付。未模拟券商按月扣息，因此现金时序是研究近似。
- **维持担保比例**：分子只含信用账户现金和证券市值；分母含融资、按市价计算的融券负债及未付利息，普通账户不作担保。
- 低于配置的平仓线时只强平信用账户。融券负债随平仓归还；多头强平所得按最老合约优先、先息后本偿还融资。具体强平顺序仍是研究近似。
- **保证金可用余额**：由现金、折算后担保品、融资和融券浮动价值，扣除融券卖出金额、融资/融券保证金占用和利息后得到。浮亏按 100% 扣减。

```text
维持担保比例 = (信用现金 + 信用证券市值)
             / (融资未还 + 融券股份 × 市价 + 未付利息)

保证金可用余额 = 现金
               + 担保品市值 × 折算率
               + (融资证券市值 - 融资金额) × 折算率
               + (融券卖出金额 - 融券市值) × 折算率
               - 融券卖出金额
               - 融资金额 × 融资保证金比例
               - 融券市值 × 融券保证金比例
               - 未付利息
```

- 新融资或融券按保证金可用余额门控，担保品买入按可用现金门控。融券卖出所得冻结，只能用于买券还券。
- **标的池**：源数据不区分担保品、融资和融券名单，回放以成交日同一集合近似三类资格。存在逐日数据但缺当日集合时 fail-closed；理论做空模式才豁免资格。
- **账户间划转**：策略在 09:14 前提交当日申请，Environment 于 09:14 统一确认。冻结的融券所得不可划出；信用账户有负债时，划出后维保比例必须不低于配置的提取线。
- **普通账户**：无负债、保证金或维保概念。未成交买单冻结可用现金；现金、T+1、手数、价格限制、停牌与显式集中度约束照常执行。

**公司行为模型（除权日现金红利与送转）**

回放槽携带窗口内已实施的分红与送转事实：

- 它是 Broker 的执行事实，不应被正式策略当作研究输入。
- 验证回放槽在策略探索阶段物理可读，Agent 可能看到该文件；可用于决策的分红消息仍必须按公告时间门控。
- 启用公司行为建模时，Broker 在每个除权日首个 tick 前处理隔夜持仓一次。

- **多头**：现金红利按“数量 × 税前每股 × (1 - 统一税率)”入账；实际持股期限差别税未建模。送转按比例增股并重算均价，保持总成本连续；红股在上市日前锁定。
- **融券空头**：按**税前全额**补偿出借方现金红利（现金可因此承压，进入维保比例）；应还股数按送转比例调增——逐张融券合约就地缩放（`shares × open_price` 计费基数不变），持仓与合约股数不变量保持。
- **标记连续性**：持仓 `last_price` 重定为理论除权价 `(前收 − 每股现金)/(1 + 送转比例)`，除权日停牌的股票权益也不跳变。
- 现金红利和送转进入 Broker 事件与多空收益归因，但不计入交易次数或胜率。登记日与回放日历不相邻时记录告警，仍照常处理。
- 未建模并记录为已知近似：配股（除权缺口仍会计为盈亏）、红利到账日滞后（按除权日贷记而非 `pay_date`）、送转零碎股取整。

**Broker 强制约束**

- 各账户现金（买入/担保品买入）与保证金可用余额（融资买入/融券卖出）约束；授信额度上限（如设置）；两账户现金池互不透支。
- A 股手数规则（沪深普通 100 股整数倍；科创板 200 股起、1 股递增；北交所 100 股起、1 股递增）、手续费（含最低佣金、过户费）、滑点和印花税。
- T+1 可卖余额（逐账户）：当日买入/开空份额当日不可卖出/还券，`sellable_quantity` 在交易日推进后释放，不足部分阻挡并记录。
- 停牌、涨跌停限制；融资融券标的池、融券限价（uptick）规则、维持担保比例强平（只清信用账户）、负债利息与划转提取线。
- 除权日公司行为（上节）：多头贷记现金红利与送转股，融券空头补偿现金红利、应还股数按送转调增。
- 如果 `broker_profile.max_total_holdings` 或 `broker_profile.max_single_name_weight` 被显式设置，Broker 会作为附加风控约束执行；默认 profile 不启用这两项限制。


**做空模式**

| 模式 | 规则 |
|---|---|
| `proxy_margin_secs` | 按成交日融资融券标的集合近似三类信用资格；缺当日集合时 fail-closed，整个域缺失时才退化为决策日集合并留档 |
| `broker_inventory` | 未来接入真实券源；应要求交易所融资融券资格、真实券源、数量覆盖和合约费率均可见 |
| `theoretical_short` | 研究模式，不检查券源；必须在 manifest 和结果中显式标记 |

默认资格近似按订单成交日判断：

- Broker 使用回放槽中该交易日的融资融券标的集合，不使用 Agent 决策时冻结的旧集合。
- 逐日域存在但缺当日集合时 fail-closed，不沿用陈旧名单。
- 只有整个回放槽都没有该数据域时，才退化为决策日冻结集合，并在结果中标记。
- 不在集合内的担保品买入、融资买入和融券卖出分别记录对应拒单原因。
- 这不代表真实担保品池、融资池、券源、逐票费率或折算率；接入真实券商数据后必须拆分。


### 3.3 main(ctx) 执行模型与 Timeview 可见性

交易逻辑全部由 Agent 定义：

- Environment 启动一个常驻 `main(ctx)` 进程，每个决策 tick 调用一次；一次调用覆盖全市场，不按证券逐个调用。
- 非决策 Bar 上 Timeview、状态合并和 Broker 仍继续推进。
- 当前仓库只实现回放；同一策略接口只是未来实盘目标，不代表实盘执行器已存在。

执行节奏：

- 交易时段内按真实 1 分钟 bar 逐 tick 推进；普通盘中 bar 上 `main(ctx)` 的决策间距可配置，Broker 撮合、执行滞后与竞价 tick 不受影响。
- 时段外按配置间隔继续调用 `main(ctx)`，只用于研究、状态和计划维护。
- 普通 off-session tick 不提交交易所订单；`transfer` 是盘前资金划转申请，不是交易所委托。
- 只有显式可报单的竞价/盘后 tick 或有真实行情的交易分钟 tick，才应在 `ctx.substep` 内调用 `ctx.broker` 报单、平仓或撤单。当前时间表见 [参数参考](parameters_reference.md#3-回放执行与预算)。
- 若要盘前准备订单，应先在 off-session tick 的 substep 中写计划，再在 09:15/09:25 的 substep 中读取计划并调用 `ctx.broker`。

`main` 每个 tick 都可核对持仓、在途订单和滚动数据，并自行决定筛选、推理、下单时点。Environment 提供的 `ctx` surface 如下；Agent 文档只说明策略侧如何组织这些接口。

| ctx surface | Environment 合同 |
|---|---|
| `ctx.cur_datetime` | 权威仿真时间戳，Asia/Shanghai ISO 格式；驱动 Timeview、substep `ready_at`、延迟提交与撮合 |
| `ctx.cur_date` | 从 `cur_datetime` 派生的当前交易日，`YYYYMMDD`；用于每日逻辑、缓存 key 和状态文件名 |
| `ctx.cur_time` | 从 `cur_datetime` 派生的当前日内分钟，`HH:MM`；用于固定时点调度，如 09:25、14:57 |
| `ctx.account` | 只读双账户快照：`stock`、`credit`、`total_assets`、`risk_limits` |
| `ctx.positions` | 只读逐标的持仓快照列表；每行带 `account`，用于区分普通账户和信用账户持仓 |
| `ctx.price(ts_code)` | 当前 tick 该股票可见价格；未来价格不可见，09:15 和普通 off-session 通常为 `None` |
| `ctx.bar(ts_code)` | 当前 tick 该股票可见 bar；不存在可见行情时为 `None` |
| `ctx.bars` | 当前 tick 全市场可见 bar 列表；只包含当前 tick，不包含未来 bar |
| `ctx.broker` | Broker 查询、下单、撤单和两融原语；下单/撤单必须在 `ctx.substep` 内，接口见 §3.4 |
| `ctx.substep(name, budget_minutes=B)` | 策略步骤预算上下文；声明计算耗时、state 写入 `ready_at` 和 broker action 提交时点，详见 §3.6 |
| `ctx.nl(ts_code?, prompt=...)` | 决策阶段 NL 工具；必须在 `ctx.substep` 内，按仿真时钟和文本可见性门控 |
| `ctx.asof_dir` | 逐 tick 滚动、节点门控的 PIT 视图：`daily`、`events`、`macro`、`fundamentals`、`intraday_1min`、`text_index` 和 `text_library` |
| `ctx.asof_version` | Timeview 真正滚动时变化的版本串；策略可按它缓存 as-of 读取 |
| `ctx.snapshot_dir` | 冻结研究基线快照，不随回放 tick 滚动 |
| `ctx.state_dir` | 宿主管理的跨 tick 状态目录；只能在 `ctx.substep` 内访问，写入暂存至 `ready_at` 才可见 |
| `ctx.model_dir` | 只读模型产物目录；需要跨回测持久的数据应在回测前写入 `models/` |

`ctx.asof_dir` 是逐 tick 滚动的 PIT 视图，按 Asia/Shanghai 仿真时钟放行日频、事件、宏观、财务、分钟和文本数据。宿主 NL 使用同一时钟门控公告与新闻。

Timeview 可见性规则：

- 每个域的可见截止只取当前仿真时钟下已经完成的最新落库刷新节点。节点必须对应 Data 文档中的真实落库任务；只读审计任务不能推进可见性。
- 常见时点：多数日频、分钟历史、宏观、批量事件和批量文本跟随 `cn_evening_full`，盘中通常只到 D-1；`fundamentals` 约 03:50 可见；`margin_secs` 约 09:05/09:15 当日盘前可见；`cctv_news` / `news` 约 09:00 当日盘前可见。
- 当日实时行情走 `ctx.bars` / `ctx.price`，不进入持久化 Timeview。

滚动视图采用只追加分片：

- 初始分片是冻结研究快照；后续分片只在仿真时钟跨过对应刷新节点时追加。
- 没有节点完成时，视图保持不变，不因 tick 推进反复重建。
- 文本正文只保留当前可见索引引用的分片。
- 视图真正变化时版本号递增，策略可据此缓存并按需重算。
- 冻结研究基线始终不变；滚动视图默认开启。


### 3.4 ctx.broker 接口

下单原语均以 `ts_code` 为第一参数。

| 接口 | 作用 |
|---|---|
| `buy(ts_code, amount, limit=None, reason=None)` | 普通账户股票买入；`limit` 为空时为市价单 |
| `sell(ts_code, amount, limit=None, reason=None)` | 普通账户卖出多头可卖份额，受 T+1 约束 |
| `credit_buy(ts_code, amount, limit=None, reason=None)` | 信用账户担保品买入，受近似标的池门控 |
| `credit_sell(ts_code, amount, limit=None, reason=None)` | 信用账户担保品卖出；融资买入股份必须通过卖券还款卖出 |
| `fin_buy(ts_code, amount, limit=None, reason=None)` | 融资买入；本金和费用进入融资负债，受保证金、标的池与额度约束 |
| `short(ts_code, amount, *, limit, reason=None)` | 融券卖出；必须给出有限正限价，并通过到达时点的 uptick 检查 |
| `cover(ts_code, amount, limit=None, reason=None)` | 买券还券；按最老合约优先偿还，融券卖出当日不可还券 |
| `sell_repay(ts_code, amount, limit=None, reason=None)` | 卖券还款；净所得先息后本，余额留在信用账户 |
| `direct_repay(amount, reason=None)` | 从信用现金直接还融资，先息后本；即时结算，不经过撮合 |
| `transfer(amount, from_account, to_account, reason=None)` | 两账户间现金划转申请；仅接受每日 09:14 前提交的当日盘前申请，09:14 统一确认；金额超可用或触及提取线时拒单（`insufficient_cash` / `credit_withdraw_blocked_by_maintenance`） |
| `close(ts_code, account=None, reason=None)` | 平掉该股可平持仓（恒市价；引擎按持仓账户与方向在提交时转换）。两账户同时持有该票时必须显式 `account=`，缺省则 driver 抛错 |
| `cancel(order_id, reason=None)` | 撤销 `pending()` 返回的未成交委托（order_id 跨账户唯一） |
| `position(ts_code, account=None)` | 该股有符号已成交持仓股数（不含在途单）；缺省跨账户净额，`account=` 看单账户 |
| `pending(ts_code=None)` | 已提交但未成交/可撤的在途单（记录含 `account`）；有参返回该股在途单，无参返回全量 |
| `stock` | 普通账户视图 dict：`cash`、`available_cash`、`total_assets`、`market_value`；`cash` 是已成交真相，`available_cash` 扣已提交未成交买单冻结 |
| `credit` | 信用账户视图 dict：`cash`、`available_cash`、维保比例、保证金可用余额、融资/融券负债、应计利息、额度、利率；可用现金/保证金扣融券冻结所得和已提交未成交订单占用 |
| `account` | 双账户快照 `{stock, credit, total_assets, risk_limits}` |
| `positions` | 逐标的持仓快照列表（每行带 `account`；数量、可卖/可平数量、方向、成本和市值等） |
| `debt_contracts(ts_code=None)` | 未了结融资/融券负债合约明细（未还金额/量、开仓日、年利率、已计未付利息） |

接口通用规则：

- `amount` 是股数，必须是正整数；沪深主板/创业板必须为 100 股（1 手）的整数倍，科创板为 200 股起、之后 1 股递增，北交所为 100 股起、之后 1 股递增。
- Broker 不做向下取整、超可卖量截断或单票 cap 自动压量；金额/股数超出约束时直接拒单并记录原因。
- 仓位 sizing 由策略显式读取现金、价格和可卖量后自行计算；Broker 不接受 `weight` 下单参数。
- 所有下单/撤单原语都接受可选 `reason=`（默认 `None`）审计注记，Sandbox driver 原样记入 Broker 事件、不影响撮合。
- 下单原语返回可用于撤单的 `order_id`；`pending()` 记录包含 `order_id`、`account`、`op_type`、`submitted_at`、`age_minutes`、`status`，并可带 `pending_stage`（如 `submit_lag`）等字段。
- 所有拒单、撤单、T+1 阻挡、维保警戒、强制平仓和负债合约展期事件必须记录。

这些接口是当前回放的公共合同，并与未来 QMT 需要的账户、动作和查询语义保持对应。当前仓库没有实盘 Broker 或本地实盘 tick 执行器；券商映射和待验证问题只在 Deployment 维护。

### 3.5 订单生命周期与撮合规则

Environment 不引入券商侧条件单或止损单；所有订单都进入当日订单簿，由宿主 Broker 按交易规则、账户约束和行情 bar 撮合。

执行延迟与资金占用：

- 订单在决策后的配置 Bar 起进入撮合，用于表示计算和报单延迟。
- 例如延迟为 2 时，09:35 决策的订单最早从 09:37 Bar 撮合；延迟为 1 时从紧邻下一根 Bar 撮合。
- 已提交但未成交/未撤的订单会占用 `available_cash`、信用保证金可用余额和 `sellable_quantity`。
- `cash`、持仓 `quantity`、`position()` 只反映已成交真相。

在途订单与撤单：

- `ctx.broker.pending()` 返回已提交的 submit-lag / 工作订单，可在 `ctx.substep` 内用 `ctx.broker.cancel(order_id, reason=...)` 撤销。
- 典型撤单对象是已进 Broker 订单簿但尚未成交的限价挂单；如需“N 分钟后撤单”，策略应读取 `pending()` 的 `age_minutes` 并显式调用 `cancel()`。
- 撤单成功记录 `order_cancelled`；已经在当前激活 bar 成交的市价单不能事后撤销。

竞价 tick：

- 启用集合竞价后，每个回放日按配置插入盘前、开盘撮合和收盘竞价 tick。
- `09:15` 是盘前信息 tick：集合竞价尚未撮合，`ctx.price` 为 None，可用于筛选与 NL，其订单成交于 09:30 开盘集合竞价。
- `09:25` 是盲提交 tick，不能从未来09:30/09:31分钟条反推开盘价；此时提交的订单从首根连续 Bar 撮合，并按连续交易计滑点。
- `stk_auction` 完整结果按盘前任务的实际落地时间可见（通常09:27–09:29）。09:30前的结果 tick 只用于研究，不能报单；09:30后的到达合并到真实分钟 tick。Broker 可用隐藏清算真值执行09:15委托，但不得提前暴露给 Agent。
- `14:57` 是收盘集合竞价决策 tick，其订单成交于 15:00 bar 的收盘价。
- 真正的集合竞价成交（09:15→09:30、14:57→15:00）按单一竞价价清算，不计滑点，`price_label="auction"`。开盘竞价在2025-01-16以后使用最终竞价接口的成交价；更早日期或个别缺行使用09:30 Bar 的开盘价，量额代理保留来源和校正规则。收盘竞价始终使用15:00 Bar 的官方收盘价。限价未达到单一竞价价时不成交并按订单生命周期继续处理，融券 uptick 也使用同一清算价。

盘后固定价格 tick：

- 启用盘后固定价格交易后，在最后一根真实 bar 后插入盘后定价 tick：`ctx.bars` 为当日收盘 bar（已确认收盘价可见）。
- 该 tick 的订单**立即按当日官方收盘价结算**（`price_label="afterhours_fixed"`，无滑点、无成交延迟、不进订单簿），对应真实规则「15:05–15:30 按收盘价撮合」的 bar 级近似。
- 资格先按板块与生效日期判断：科创板自 2019-07-22、创业板自 2020-08-24、其余当前代码自 2026-07-06 起。
- 这一步不重复验证证券类型。当前数据合同外的证券或异常代码通常会在行情和市场约束中被拒；如果未来扩大证券覆盖，必须同时补齐明确的资格规则，不能依赖缺行情碰巧拒单。
- 限价申报劣于收盘价（买价低于收盘 / 卖价高于收盘）为无效申报，拒 `afterhours_price_invalid`（细则口径）。
- `short`/`fin_buy` 开新杠杆仓保守不支持（拒 `afterhours_op_unsupported`）：融资/融券开仓能否走盘后定价未经真实核验。
- 涨跌停、停牌（15:00 仍停牌的股票无盘后交易，经 `suspended` 拒单体现）、T+1、现金/保证金、手数照常执行；收盘价封板时保留涨跌停拒单作为对手方稀缺的保守近似。
- 实盘是否支持该交易方式仍是 Deployment 文档中的开放问题。

报价与成交：

- 市价单在激活 Bar 按开盘价加方向性滑点成交。当前分钟无该证券 Bar 时继续挂单，直到当日下一根可撮合 Bar；该 Bar 可以是合成 fallback。挂单期间继续占用估算资金或保证金。
- 收盘仍未成交的市价单撤销。开盘竞价委托错过开盘 Bar 后转入连续撮合并失去免滑点待遇；收盘竞价未成交则在日终撤销。
- 限价单：`limit=P`，对应 `prType=11` 指定价；默认当日有效，不计滑点，直到成交、策略主动撤单或日终清扫。
- 限价买入/补券：`open<=P` 时按 `open` 成交，否则须 `low<P`（严格击穿）按 `P` 成交——仅触及（`low==P`）视为排在该价位队列中未成交。
- 限价卖出/融券卖出：`open>=P` 时按 `open` 成交，否则须 `high>P`（严格击穿）按 `P` 成交。
- 收盘集合竞价采用单一价撮合：只以收盘竞价价判断限价是否可成交；可成交时按竞价价清算，否则不成交。
- 合成 fallback Bar（`synthetic=True`）也采用单一价撮合：只以参考价判断限价是否可成交，不使用 high/low。其 high/low 来自全日数据且没有分钟时间戳，不能证明订单提交后曾触及限价；忽略该区间可以避免追溯性成交。
- 回放使用 Bar 级全量成交模型，不模拟订单簿深度、排队、市场冲击或部分成交。满足价格和 Broker 约束后，全部剩余数量一次成交；订单和成交审计完整不代表具备容量模拟能力。
- 当日收盘仍未成交的限价单由日终清扫撤单并记录 `order_cancelled`。
- `close` 恒市价；Broker 仍按当日涨跌停、停牌、T+1、账户、保证金、券池和集中度等约束决定是否成交。
- 持仓估值每根 Bar 更新两次：撮合前按开盘价重估，使资金和保证金准入使用订单到达时价格；撮合后按收盘价重估，使 Agent 看到该 Bar 结束后的账户状态。
- 利息计提和强平检查仍只在日终执行。

分钟数据与 fallback：

- 回放槽存在非空 `intraday_1min.parquet` 时使用真实分钟 Bar。
- 某日或某证券缺分钟数据，或缺少必要收盘 Bar 时，按日线合成 09:30 和 15:00 两根退化 Bar。
- 开盘合成 Bar 只含开盘价以防前视；收盘合成 Bar 可供收盘后观察全日范围，但撮合只使用参考价，不使用全日 high/low。
- `execution_lag_bars` 会按当日 bar 数收敛为 `max(1, min(lag, n-1))`，使两根 bar 退化日仍能在 15:00 成交。
- `09:15`、`09:25`、`14:57` 的固定竞价/首根连续成交不受 `execution_lag_bars` 影响。

收尾规则：

- 回放区间最后一个交易日保留为剩余持仓强制清仓日。
- 清仓失败的持仓（停牌、涨跌停封板、T+1、缺价）留在账簿，并按可见价格计入期末权益；结果同时记录未清仓仓位、剩余负债和受阻原因。主收益使用期末盯市净值；清算完整性作为独立诊断，不参与验收判定。
- 临近收盘且无后续可成交 bar 的决策记录 `main_actions_unfilled`。
- 当日收盘仍挂着的限价单自动撤销。

融券卖出限价规则（实施细则）：

- 融券卖出必须限价申报；`ctx.broker.short()` 缺少 `limit=` 或价格不是有限正数会被策略接口拒绝，底层 Broker 仍保留 `slo_sell_requires_limit_price` 保底校验。
- 订单首次到达交易所时，若 `limit <` 激活 bar 参考价，按申报被拒并记录 `slo_sell_uptick_rule`。
- 通过检查后正常挂单，此后价格上穿限价属合法成交；典型用法是 `limit=ctx.price(code)` 或更高。
- 融券 uptick 检查使用订单激活 Bar 的参考价，因为提交延迟代表“决策到交易所”的时间，规则应在订单到达时校验。
- 决策价与到达价之间的漂移是该延迟模型的一部分。调整延迟只改变到达速度，不改变检查时点。


### 3.6 substep、状态写入与计算延迟

正式策略中的实质步骤都必须位于 `ctx.substep`：状态读写、持仓和在途管理、横截面筛选、模型推理、NL、批量下单、Broker 动作与撤单扫描。预算必须为正，同一 tick 内名称唯一，且不能超过单决策仿真上限。

substep 的声明预算 `B` 同时定义三件事：

1. **墙钟 fail-fast**：substep 真实墙钟超过 `B·60s` 时立即失败，并返回步骤、日期、声明预算和实测耗时。低报预算不可利用。验证、冻结测试和 held-out 执行同一合同（声明预算推进仿真时间，超限结果无效不评分）；仅开发基准脚本可关闭。
2. **`ctx.state_dir` 写可见性**：块内写入先落入暂存目录，在 `ready_at = 决策 tick + B` 后才并入可见目录。
3. **broker action 提交时点**：块内 action 先是提交计划，不会立刻投影到账户/持仓；到达提交时点后再进入常规订单生命周期与撮合规则。

提交时点：

| `B` | broker action 与 state 写入 |
|---|---|
| `0 < B < 1` | 视为本决策分钟内完成；broker action 以当前 tick 为提交 tick，再按提交延迟进入常规撮合或竞价规则；state 写入记录 `ready_at = 当前 tick + B 分钟`，在后续 tick 检查到 `ready_at` 已到后合并 |
| `B >= 1` | 动作等到就绪时刻；仅当生成、就绪和释放时点均处于可申报窗口才提交，否则记录未提交或未成交，不自动顺延 |

等待中的动作还不是 Broker 委托，不出现在 `pending()` 中，也不能通过 `cancel()` 撤销。

跨分钟动作到期后按当时市场窗口处理：

- 已无后续成交 Bar 时记录未成交，不顺延。
- 到期落在盘外、午休或其他不接收申报的时段时记录未提交，不自动排到开盘、午后或下一交易日。
- 盘前或午间准备订单时，先写策略计划，再在明确可申报 tick 重新读取并调用 Broker。
- 修改尚未到期的计划，应更新策略状态，使提交条件在到期时不再成立。
- 撤单扫描也放入小预算 substep，以统一记录耗时和提交时点。

状态目录由宿主管理，Broker 仍是持仓真相源：

- 进入 substep 时，当前可见状态复制到暂存视图。
- 块内读取看到旧可见值；写入到达就绪时间后才合并。
- 同一路径冲突时，后生成的写入胜出。
- 该机制按文件路径捕获写入，不依赖 Shell 命令解析。

**可强制性边界**

- 状态延迟首先是计算时延建模合同，常用文件访问入口的路径保护只用于防误用，不是完整安全边界。
- 未受保护的运行时或原生 I/O 入口可能绕过文件保护；常驻策略也可以把信息保存在模块内存中，从而绕过文件状态的延迟可见性。
- 状态目录每次回放重建，不冻结、不跨 Fold 继承，因此该限制影响的是单次回放内的算力时延真实性，不会形成跨运行 PIT 泄漏。
- 逐决策墙钟上限、每次回放重置、substep 实测超时与覆盖检查在验证、冻结测试和 held-out 全部强制（统一合同）；冻结评估仅逐决策与每日硬上限更宽松（防挂死，不做验收门槛）。

状态和产物约束：

- `ctx.state_dir` 与 `ctx.broker` 原语始终要求位于 substep。所有正式回放都会用 `main(ctx)` 总耗时减去 substep 耗时，拒绝实质未包裹计算。
- 可见目录与暂存目录每次回测都清空重建，保证可复现。
- 状态目录只适合小体量、跨 tick 可变状态；高频 substep 会反复复制整个状态目录。
- 大体量且回放中只读的特征或模型应在回测前写入模型产物；可变状态应保持小体量并减少写入频率。
- 需跨回测持久的数据应在回测前写入 `models/`；正式回放只读加载 `output/` 策略代码和 `models/` 模型产物，禁止写 `output/` / `models/`、创建软/硬链接，且按真实路径阻断经链接访问测试槽或 `/mnt/artifacts`。
- 仿真时钟（`ctx.cur_datetime`，Asia/Shanghai）统一驱动域可见性、state `ready_at`、延迟提交与成交映射。


### 3.7 回测限时、可观测性与结果摘要

`backtest` 独立计时：墙钟耗时不消耗 Fold 推理时间，Runner 在回测后回补相同时长；但每个 Fold 仍有固定回测次数上限。有效上限写入运行清单，默认值见参数参考。

验证回测（`mode="valid"`）使用两道随回放天数伸缩的真实墙钟硬上限：

- 单个决策超过配置的墙钟上限时，宿主立即终止策略驱动并让回测失败；该截止包含决策内 NL，不会因内部调用重置。
- 单交易日累计 `main(ctx)` 计算超过 `backtest_max_seconds_per_trading_day` 时，引擎层中止回放。
- 完整验证触发任一上限即不可接受/冻结，迫使 Agent 缓存重计算、降低调仓和图构建成本。

冻结测试与 held-out 使用更宽松的防挂死上限：

- 默认按验证回放对应上限的固定倍数派生；显式配置可以覆盖，当前值见 [参数参考](parameters_reference.md#3-回放执行与预算)。
- 这些上限只用于终止挂死，不是策略接受门槛。
- 策略已在验证回放中满足更紧上限，最终评估的放宽只用于降低机器负载波动造成的非确定性失败。

回放没有独立的固定总时长上限；总上界随交易日数和每日硬上限自然伸缩。小窗口调试结果可以帮助估算完整运行成本，但不构成验收门槛。仿真时间预算不能替代墙钟兜底，因为单个 tick 内的死循环不推进仿真时间。

回测可观测性：

- 开始时记录 `backtest_start`。
- 回放期间按节流（≥30 天或 ≥30 秒）记录 `backtest_progress` 心跳，包含进度、已用时和累计订单数。
- 结束或中止保证有一条终止 `backtest` 事件；外部中止记录 `status="aborted"`。
- 结果目录结构集中列在 §2.1。

`detailed_return.json` 至少包含：

| 类别 | 字段/内容 |
|---|---|
| 收益与风险 | 总收益、long/short 收益、年化收益、Sharpe、最大回撤、胜率、turnover |
| 权益序列 | `equity_curve`：每个交易日收盘后的账户权益时间序列 |
| 清算完整性 | `liquidation_complete`、`unliquidated_positions`（account/side/数量/市值/受阻原因）、`remaining_liabilities` |
| 订单与拒单 | 订单状态、拒单统计、逐笔平仓/减仓 |
| 成本与两融 | 费用、信用利息（`credit_interest_accrued` / `credit_interest_paid`） |
| Broker 审计 | Broker 事件 |

回测 summary 另含：

| 类别 | 字段/内容 |
|---|---|
| 开始与耗时 | `started_at`、`replay_wall_seconds` |
| 回放规模 | `replayed_trade_days`、`total_ticks`、`intraday_ticks`、`offsession_ticks` |
| 清算摘要 | `liquidation_complete`、`unliquidated_position_count` |
| 状态写入 | `state_staged_writes` / `state_unmerged_writes` |
| substep 统计 | `substep_runtime`，含 count、total_real_wall_s、max_real_wall_s |
| 阶段耗时 | `phase_seconds`，含 `strategy_compute`、`nl_service`、`timeview_init`、`timeview_roll`、`state_merge`、`broker_match`；回放墙钟覆盖完整生命周期（含构建） |

**逐窗口归因（Barra-lite，全部回放模式）**

每次验证、冻结测试和 held-out 回放结束后，宿主计算一次基准与风格归因：

- 持仓输入使用 Broker 按日期、账户、证券和方向记录的权威日终持仓，因此包含强平、送股和跨账户对冲腿。
- 有持仓记录时另存明细；空仓回放不创建空持仓文件。
- 多窗口完成后，Pipeline 在拼接后的日序列上重跑回归，并按有效天数合并风格暴露。
- 控制台和报告只读取冻结结果，不从可变 raw 数据重新计算。
- 输入不足时对应指标降级为空，不让归因失败破坏交易回放。
- 冻结测试和 held-out 在 Agent 会话结束后运行，因此 Agent 只能读取验证回放产生的归因结果。

输入口径（全部为冻结运行数据，不触及可被源端回写的 raw 数据湖，保证事后重读与 Agent 当时所见一致）：

- 风格输入来自回放槽的全市场日频横截面；持仓取 Broker 的逐日日终记录。
- 基准输入取同一回放槽中的沪深300窗口，行业归属取决策时点冻结的申万一级分类。
- sidecar 同时落盘策略与基准日收益序列，使下游（rollup、控制台收益曲线）完全脱离原始数据源。
- sidecar 保留用于 rollup 的完整行业累计；展示字段仍只保留绝对暴露较大的主要行业。

完整载荷指标：

| 指标 | 计算口径 |
|---|---|
| `benchmark_return` | 同窗沪深300复合收益。先按 `equity_curve` 日期取可匹配的沪深300日收益 `r_b`，再连乘计算 `prod(1 + r_b) - 1`；缺少基准日期不会参与连乘。 |
| `beta` | 策略对沪深300的市场暴露。先从 `equity_curve` 计算策略日收益 `r_s`，与同日沪深300日收益 `r_b` 配对，再按单因子回归斜率计算 `cov(r_s, r_b) / var(r_b)`。 |
| `alpha_annualized` | 单因子回归截距的年化值。先计算日 alpha：`mean(r_s) - beta * mean(r_b)`，再乘以年交易日数 `244`；短窗口下只作诊断参考。 |
| `r2` | 单因子回归解释度，表示策略日收益有多少波动可由沪深300解释。计算式为 `cov(r_s, r_b)^2 / (var(r_s) * var(r_b))`；它不是年化指标。 |
| `n_days` | 策略日收益和沪深300日收益都存在的配对交易日数量。少于最小回归天数时，`beta`、`alpha_annualized` 和 `r2` 降级为 None。 |
| 风格倾斜 | 用日终持仓与收盘价计算带符号权重；对市值、PB 和换手率做横截面分位，求持仓相对市场中位数的加权偏离并映射到约 `[-1, 1]`，再按有效日期平均。 |
| 行业净权重 | 使用同一套带符号持仓权重，按申万一级行业聚合并对窗口内有效日期求平均，保留绝对暴露较大的主要行业。 |

紧凑基准摘要只保留同窗基准收益、超额收益、β、配对天数和市值倾斜。超额收益等于策略总收益减基准收益；短窗年化 alpha 和 R² 只留在完整诊断中。Barra-lite 只描述市场、风格和行业暴露，不得作为直接优化目标。

## 4. 运行日志、审计与验收

本章定义可信日志、Manifest、Trace、读取权限和运行后验收要求。

### 4.1 可信日志与核心文件

可信日志只能由 Runner、Execution Gateway、LLM Proxy 和 Broker 自动生成。Agent 的解释或输出字段不能替代可信日志。


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
- 决策时点、研究输入窗口、验证区间和 snapshot hash；测试和 held-out 调度不写入 Agent 可见 manifest。
- 父产物、当前产物和冻结状态。任何可能内嵌未来周期标签的宿主身份，在 Agent 视图中都投影为稳定的不透明引用。
- 父模型参数 hash、当前模型 artifact hash 和 combined hash。
- Broker profile、做空/融券模式、成本参数和资源配置。
- runtime env 路径，以及普通 Fold 或元学习需要的实验参数摘要。
- 修改约束和 deadline。
- 关键结果目录和状态摘要。
- 元学习的 development 历史、账本投影、记忆和 Taste 只引用 Sandbox 内路径，不暴露宿主绝对路径。

宿主收集目录额外保留 `host_run_manifest.json`，用于完整审计测试/held-out 调度、测试 snapshot 和 frozen evaluation 结果；该文件不在 Sandbox 中挂载。

`agent_trace.jsonl` 是轻量事件流，包含工具调用（含 shell）、回测、Broker、LLM、context compact、NL、错误和锁定事件。事件共享 `experiment_id/fold_id/run_id/conversation_id/call_id/parent_call_id`，便于追溯。


**读取权限**

- Agent 在策略探索阶段只能读取当前 Fold 的验证结果。
- 测试和 held-out 结果、日志、NL 明细和 Broker 事件不反馈给 Agent。
- 宿主可读完整审计目录。
- 冻结 artifact 的 `manifest.json` 是冻结元数据，不参与策略 artifact hash。Pipeline 冻结策略时会在冻结 artifact 目录内写入该文件；Agent 工作副本 `/mnt/agent/output/` 不需要也不应自行创建。


### 4.3 审计检查与验收清单

本节是运行后复核清单，不新增执行入口。审计检查关注记录是否足以复盘运行过程；验收清单关注当前产物是否允许冻结、测试或进入 held-out。

**审计检查**

- `run_manifest.json`、`host_run_manifest.json`、`agent_trace.jsonl` 记录关键版本、hash、路径、时间、阶段和关联 ID。
- 工具调用（含 shell）记录状态、stdout/stderr（适用时）、长输出引用和错误摘要。
- 工具调用没有越权路径、越权网络访问或测试/held-out 数据读取。
- Sandbox 写入面只限 `workspace`、未锁定的 `output` 和未锁定的 `models`；锁定后正式产物保持只读。
- PIT 输入有可复核的 Timeview 证据：刷新节点 cutoff、行级 `available_at` 过滤和 `/mnt/snapshot` decision input hash 均与 manifest 一致。
- Broker 事件在发生时可追溯到订单、成交/拒单/撤单、费用、两融、维保和强平记录。
- 文本检索、NL Sub Agent 输出、NL evidence 和 provider 调用在发生时可追溯到 as-of `text_id` 或 `source_hash`。
- 审计未发现 API key、Authorization header 或代理配置正文进入 Prompt、Trace、产物或日志；元学习 Shell 输出凭据属于需要单独排查的风险。
- 合同关键失败显式报错并进入 trace。允许的降级必须有明确触发条件、范围和原因，并写入结果或清单；不得发生未记录的隐式 fallback。


**验收清单**

- `output` 包含可加载的 `main(ctx)`，且无缓存、隐藏文件/目录、非法后缀、日志、数据 dump、notebook、密钥或模型权重。
- `models` 无缓存、隐藏文件/目录、非法后缀和超限文件。
- `modification_check` 已在正式回测前通过，且其 strategy/model hash 与当前产物一致。
- 至少一次完整验证 `backtest` 成功，且验证 summary 的 strategy/model hash 与当前产物一致；调试 `replay_window` 不满足冻结条件。
- `finish_fold` 成功后，冻结策略和模型产物在 test / held-out 前后 hash 不变。
- 回测结果目录、manifest 摘要和固定结果文件已写入；订单、日终持仓、状态暂存和 NL 等条件文件按实际事件生成，不能缺失本次运行应有的记录。
