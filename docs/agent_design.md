# Agent 设计

本文档记录 Fold Agent 的工作合同：它在已准备好的 Sandbox 内能看到什么、能写什么、如何迭代策略、正式产物应是什么格式，以及哪些行为禁止。PIT 数据、Sandbox、Broker 和回测由 `docs/environment_design.md` 定义；Step / Fold / Epoch / Held-out 编排由 `docs/pipeline_design.md` 定义。

**相关边界**

- 数据下载、单位和 raw 审计见 `docs/data_documentation.md`。
- PIT 窗口、Sandbox、Agent 工具、回测和 NL 服务见 `docs/environment_design.md`。
- Step / Fold / Epoch 编排、策略产物冻结和实验账本见 `docs/pipeline_design.md`。
- 实盘部署和 QMT 流程见 `docs/QMT_documentation.md`。
- 全部参数/超参数默认值速查见 `docs/parameters_reference.md`。

**术语说明**

| 术语 | 含义 |
|---|---|
| Agent | 在一个 Fold 内读取 Sandbox 数据、写策略代码、调用受控工具并输出策略产物的模型驱动执行者 |
| Sandbox | Agent 运行的隔离环境，只能读可见数据窗口，只能写本次运行产物 |
| PIT | Point-in-time，只允许使用决策时点已经可见的数据 |
| `available_at` | 某条数据最早可以被 Agent 使用的时间 |
| Step | 一个 Fold 内的一次策略修改和验证尝试 |
| Taste | Epoch 开始前元学习会话生成的探索偏好，会注入本 Epoch 的 Fold Agent Prompt |
| 策略产物 | 跨 Fold 共享的 `output/` 正式策略产物目录，根目录固定入口为 `main.py` |
| 模型参数产物 | 跨 Fold 共享的 `models/` 可继承模型产物目录，用于保存可复现模型参数和权重 |
| NL Sub Agent 工具 | 决策代码可显式调用的 `at_tools.nl(ts_code?, prompt=...)` PIT 文本分析服务；`ts_code` 可选 |
| Held-out | 所有训练结束后才运行的冻结测试区间；Agent 不可读 |

**职责边界**

**Agent 负责**

- 读取训练窗口、验证回放区间、父策略产物和当前 Fold 的历史验证结果。
- 在 `/mnt/agent/workspace/` 写临时代码、数据探查脚本和草稿。
- 在 `/mnt/agent/output/` 写正式策略产物。
- 可在 `/mnt/agent/models/` 保存正式模型参数产物。
- 调用 `modification_check`，确认正式产物满足修改约束。
- 调用 `backtest`，读取验证回测结果，并决定是否继续修改。
- 参考 Pipeline 注入的 Taste、阶段指引和提交验收规则。
- 在收益、风险、修改量、策略复杂度和剩余时间之间做取舍。
- 在当前 Fold 准备结束时调用 `finish_fold`。

**Agent 不负责**

| 事项 | 归属 |
|---|---|
| raw 数据下载、补齐、审计和 sentinel | Data 层 |
| 构造 PIT snapshot、切换 `/mnt/snapshot` | Environment |
| 执行冻结测试或 held-out | Pipeline / Environment |
| 记录现金、持仓、成交、收益和实验账本 | Broker / Environment / Pipeline |
| 真实下单、连接券商或管理 QMT | QMT 流程 |
| 直接访问外部网络、LLM provider API key 或真实券商凭据 | 禁止 |

**会话隔离与可信日志**

同一个 Fold 内多个 Step 共享同一个 Agent 会话和 `conversation_id`。下一个 Fold 会启动新的 Agent 会话。Agent 可以看到当前父产物和当前工作副本，但不能看到上一 Fold 的对话历史、工具调用（含 shell）、LLM 调用日志、测试回测结果或测试 conversation log。

如果某个历史区间在当前 Fold 中成为验证区间，Agent 只能读取当前 Fold 重新生成的验证结果；不能复用它在上一 Fold 作为测试区间时保存的结果文件。

当同一个 Fold 会话变长时，Runner 可以用低成本 compact 模型把较早对话压缩为结构化 continuation state，并保留最近原始消息继续主对话（触发阈值与摘要锚点见 `environment_design.md` §2.2）。它不改变 `conversation_id`、数据可见范围、写权限或测试隔离，只替代旧的对话上下文，完整可信记录仍以 trace 和 provider conversation log 为准。

可信日志只能由 Environment / Pipeline 记录。Agent 可以输出解释、理由和结构化策略结果，但不能替代工具调用（含 shell）、回测、Broker 和 LLM 调用日志。

**导航**

- [1. 可见数据、工作区与工具边界](#1-可见数据工作区与工具边界)
  - [1.1 可见性、路径与数据域](#11-可见性路径与数据域)
  - [1.2 工具入口与调用原则](#12-工具入口与调用原则)
- [2. Fold 工作流](#2-fold-工作流)
  - [2.1 Step 节奏、探索与收敛](#21-step-节奏探索与收敛)
- [3. 正式策略产物、main(ctx) 与 NL](#3-正式策略产物mainctx-与-nl)
  - [3.1 目录、入口与代码边界](#31-目录入口与代码边界)
  - [3.2 main(ctx)、ctx 接口与时序](#32-mainctxctx-接口与时序)
  - [3.3 NL 调用、日志与复盘](#33-nl-调用日志与复盘)
- [4. 修改约束、禁止行为与验收](#4-修改约束禁止行为与验收)
  - [4.1 修改检查与收敛标准](#41-修改检查与收敛标准)
  - [4.2 禁止行为与提交前自检](#42-禁止行为与提交前自检)

## 1. 可见数据、工作区与工具边界

### 1.1 可见性、路径与数据域

**输入约束**

```text
available_at <= decision_time
```

**主要路径**

| 路径 | 权限 | 用途 |
|---|---|---|
| `/mnt/snapshots/train/` | 只读 | 训练和探索输入，是 `valid_decision_input` 的 Agent-visible alias |
| `/mnt/snapshots/valid/` | 只读 | 验证回放区间和复盘材料 |
| `/mnt/snapshots/test/` | Agent 不可读 | 冻结测试或 held-out 回放 |
| `/mnt/snapshot/` | 正式策略只读 | 当前决策时点 PIT 输入视图 |
| `/mnt/artifacts/parent_output/` | 只读 | 父策略产物基准 |
| `/mnt/artifacts/parent_models/` | 只读 | 父模型参数产物基准 |
| `/mnt/artifacts/results/` | 只读 | 当前 Fold 的验证回测结果 |
| `/mnt/artifacts/steps/` | 只读 | 通过验证的 Step 产物树 |
| `/mnt/agent/workspace/` | 可写 | 临时代码、草稿、数据探查 |
| `/mnt/agent/output/` | 可写直到锁定 | 正式策略产物 |
| `/mnt/agent/models/` | 可写直到锁定 | 正式模型参数、权重和轻量模型元数据 |

`/mnt/agent/` 根目录本身不是写入面；临时文件放入 `workspace/`，正式代码放入 `output/`，模型参数放入 `models/`。

`/mnt/snapshots/{train,valid,test}` 是按阶段命名的回放区间，`/mnt/snapshot` 是 `backtest` 调用正式策略时绑定的当前决策输入视图；`train` 与当前 Fold 的 `valid_decision_input` 是同一 PIT 输入的 alias（run manifest 记录相同 snapshot hash）。元学习会话与第一个 Fold 共用同一可见数据，test/held-out 始终不可见。PIT 快照构造、绑定与可见性的权威定义见 `environment_design.md` §1。

正式策略执行时只能依赖 `/mnt/snapshot`、`output` 自身和 `/mnt/agent/models`。不能在正式代码中硬编码读取 `/mnt/snapshots/`、`/mnt/artifacts`、`/mnt/runtime`、主仓库路径或测试区间。

Agent 系统提示词会注入一段 `当前实验事实`，这是 Environment 从 `/mnt/artifacts/run_manifest.json`、`runtime_env.json` 和 `data_summary.json` 抽取的低风险摘要，用于减少开局反复读取成本。三者仍是 Agent 可见事实源：public run manifest 只记录训练/验证可用配置、Broker/约束和可见 snapshot；runtime env 记录 Sandbox Python/CLI/网络；data summary 记录可见数据轻量索引。事实块不替代源 JSON；如果冲突，以源 JSON 为准。正式策略代码仍不得硬编码读取 `/mnt/artifacts`。父产物 ID 和 Fold 标签在 public run manifest 与事实块里都投影成不透明引用 `strategy_ref_*` / `fold_ref_*`，不暴露原始周期标签。

**数据域和产物边界**

**数据域用途**

| 数据域 | Agent 可怎么用 |
|---|---|
| `daily` | 日频行情、每日指标、交易约束和日线参考 |
| `intraday_1min` | 分钟级回放参考，日内、打板、开收盘和做 T 策略研究 |
| `fundamentals` | 财务、分红、业绩预告/快报和主营构成 |
| `events` | 资金流、两融、股东、回购、解禁、大宗交易、龙虎榜等事件 |
| `macro` | 宏观、政策、利率、全球事件和跨市场背景 |
| `text` | 公告、新闻、研报、政策文本索引和正文库 |

窗口由实验启动前的 `SnapshotConfig` 决定，并由 Environment 准备。Agent 可以少用窗口内数据，但不能请求超出窗口的数据。字段、单位、可见时间和覆盖范围由 snapshot `manifest.json` 记录。

`workspace/` 不冻结、不回放、不复制到下一 Fold。`output/` 是正式策略代码来源；`models/` 是可选正式模型参数来源。`/mnt/artifacts/results/` 由 `backtest` 写入；Agent 在训练/验证期只读验证结果，测试和 held-out 结果不反馈给 Agent。

启用 Step 产物树时，`/mnt/artifacts/steps/` 对 Agent 只读可见。`tree.json` 记录本 Experiment 内通过验证回测的 Step 产物谱系，节点含父指针、Fold、验证指标和产物 hash；`current_node_id` 标记当前工作副本的起点节点。`tree.txt` 是同一棵树的可读渲染（含收益、当前位置和 `[failed]` 标记）。成功节点目录（`steps/<node_id>/`）保存对应 `output` 全量产物，并附带该次验证的 `detailed_return.json`。启用失败尝试记录时，未通过的验证回测会写入轻量 `[failed]` 节点（无产物快照，不改变当前位置），用于提示已是死路的方向。新增节点只由回测流程自动记录。

### 1.2 工具入口与调用原则

**Agent 工具入口**

| 入口 | 何时使用 | 结果 |
|---|---|---|
| `grep` / `glob` | 快速检索可见目录和日志 | 只读结构化结果；不访问测试或隐藏路径 |
| `read` | 按行号读取文件（可分页），读要编辑的代码优先用它 | 只读；`cat`/`head` 仍可用于管道 |
| `note` | 记录推理/复盘，不执行任何操作 | 无副作用，仅进 trace |
| `shell` | 探查数据、调试、执行命令、写二进制模型权重；可用 `max_output_chars` 和 `timeout_seconds` 主动缩小内联输出和单次运行时间 | 写入 `workspace/`、可写的 `output/` 或 `models/`；长输出通过 trace 路径复核 |
| `write_file` / `edit_file` | 维护正式文本产物（优先于 shell heredoc）；`edit_file` 做精确字符串替换 | 只写 `workspace`/`output`/`models`；`edit_file` 的 `old_string` 须唯一匹配 |
| `explore` | 把大量只读数据探查委托给更便宜模型的 Sub Agent | 返回结论、证据、风险与限制、建议下一步，节省主上下文与成本；只读 |
| `web_search` | 元学习阶段检索外部资料 | 仅 Epoch 前元学习可用；普通 Fold 不可用 |
| `web_fetch` | 元学习阶段读取公开网页 | 仅 Epoch 前元学习可用；宿主侧只读 GET，默认直连，`use_proxy=true` 才允许使用 active 代理；不支持登录、认证、POST、浏览器渲染、JS 或 PDF/二进制解析 |
| `modification_check` | 修改正式产物后主动检查 | 返回是否允许进入回测，并写审计摘要 |
| `backtest` | 验证当前正式产物；可选参数只有 `replay_window` | 执行前自动复核最近一次修改检查和当前 hash；缺失或过期时自动补跑，然后执行 `output/main.py`，写入 `results/valid_<idx>/` |
| `finish_fold` | 当前 Fold 准备结束时 | 要求当前 hash 已有成功完整验证回测（`replay_window` 调试不算），否则直接拒绝；通过后锁定 `output/` 和 `models/`，清理 Sandbox 内 Agent 后台进程，并等待 Pipeline 冻结和测试 |

普通 Fold Agent 的能力边界：不直接调用外部 LLM provider，不具备联网搜索入口，不直接访问真实券商，不修改 Environment 或 Pipeline；普通 Fold 默认不联网、不安装新包。

**元学习额外能力**

- `web_search`：宿主暴露可用搜索引擎，元学习每次调用自选 `engine` 并用 `perspective` 标记研究视角；结束前须完成金融/量化/经济、其他自然科学/工程、哲学/方法论三类视角的非空成功检索。
- `web_fetch`：元学习可对公开 http/https 网页做宿主侧只读抓取，用于阅读 `web_search` 找到的资料；默认直连，只有直连失败、明显卡顿或任务明确需要代理时才设置 `use_proxy=true`；结果以受限 markdown 摘录进入上下文，完整有界文本落在日志目录并写 trace。
- 元学习联网：按当前实验事实可联网时，可经 `shell` 用 `git`/`pip`/`npm`/`hf` 下载公开代码、资料或模型。只放在 `workspace` 的试装不继承；要让后续 Fold 继承的新依赖写入 `workspace/sandbox_environment.json` 交 Pipeline 构建派生镜像（见 §1.2、§3.1）。
- 代理与凭据：托管 XRay 有配置时默认启动并暴露 active `AT_PROXY_*` 别名，但命令默认直连；无托管配置时不提供代理别名。只有直连失败、明显卡顿或任务明确需要代理时，才按单条命令临时映射 active 代理别名。token 只按 active 环境变量名使用，明文不得进入 prompt、产物、日志或账本。Runner/CLI 联网与代理配置的权威定义见 `environment_design.md` §2.1。

元学习写出非空 `taste.md` 后调用 `done` 结束。Taste 是注入本 Epoch 后续所有 Fold 的方向性约束，必须跨周期通用、与具体时间窗口无关（不得写季度/年份/Fold 标签或复述 valid/test/held-out 区间）；`meta_learning_directive` 注入的研究方向应当作待检验假设。Taste 写作合同与三视角要求的权威定义见 `pipeline_design.md` §3.1。

**工具调用原则**

- 工具通过原生 function calling 调用，工具名和参数 schema 由 Environment 提供；不要在正文里手写 JSON 动作。一轮可并行发起多个互相独立的只读工具调用（如多个 grep/glob），有状态工具（modification_check/backtest/finish_fold）按因果顺序单独调用。
- Agent 回复、Taste 和复盘主语言使用中文；代码标识、库名、论文标题和英文专有名词可以保留原文。
- 先用 `grep/glob` 查找文件，避免在 Shell 中全目录扫描。
- 写正式代码或 Taste 前先读取 `当前实验事实`；需要精确字段、完整 schema、路径、窗口或依赖细节时，再读取 `run_manifest.json`、`data_summary.json` 和 `runtime_env.json`，不假设未列出的包可用。
- Prompt 只描述稳定协议，不承载当前数据事实。行数、关键列、日期覆盖和完整 schema 以本 run 动态生成的 `data_summary.json`、`run_manifest.json`、snapshot `manifest.json` 和 parquet metadata 为准；未来数据变动后由 Pipeline 重新生成摘要。
- 对 `events.parquet`、`text_index.parquet`、`intraday_1min.parquet` 等大表，先用 Parquet metadata 判断结构和规模；需要抽样或聚合时，再用 DuckDB、pyarrow 或 pandas 按列/日期过滤读取；不要在未知规模时直接 `pd.read_parquet()` 全量读取。
- Meta Learning 应通过 Shell 调用 Python，对可见 snapshot 做只读详细检查和分析，例如 parquet 文件、schema、行数、日期覆盖、关键空值和单位，不只凭历史记录或网页检索判断数据特征。
- Shell 在普通 Fold 只做可见数据探查、临时代码和策略文件编辑；普通 Fold 默认无外网，新增稳定依赖应由元学习写入 `workspace/sandbox_environment.json` 后交 Pipeline 构建派生镜像（请求 schema 与派生镜像构建见 `pipeline_design.md` §3.1），或把最小可审计源码整理进 `output` 通过修改检查。元学习 Sandbox 额外允许公开资料/模型下载与依赖试装；只放 `workspace` 的试装不继承。
- Shell 命令不要用 `2>/dev/null` 隐藏错误（stdout/stderr 是审计输入）。Shell 命令自身失败会以 `exit_code`、`stderr`、`stdout_path` / `stderr_path` 返回；Tool 层拒绝才会返回 `error_type`、`reason` 和 `retry_hint`。Explore Sub Agent 与主 Agent 共用同一执行边界。
- 每次正式回测前都必须通过修改检查。Agent 应主动调用 `modification_check` 以提前暴露格式和修改量问题；若检查缺失或过期，`backtest` 会自动补跑。
- 读取回测结果时优先看 `detailed_return.json`、`orders.parquet`、Broker 事件、拒单统计和 NL 工具日志。
- 关键决策前应先从机制假设、可见数据、执行约束、反证路径和失败模式充分推理；最终动作、代码和 Taste 仍保持简洁、可验证。
- `finish_fold` 只表示 Agent 停止本 Fold 修改；是否冻结仍由 Pipeline 复核。

## 2. Fold 工作流

### 2.1 Step 节奏、探索与收敛

一个 Fold 内可以有多个 Step。Step 是同一个 Agent 会话中的一次“修改 -> 检查 -> 验证回测”迭代，不会重启 Agent，也不会创建新的对话上下文。Agent 每跑完一次验证回测，就会得到一个新的 `results/valid_<idx>/`，可据此继续下一 Step。

**初始 Step 建议**

1. 读取训练窗口、父产物、数据 manifest 和可见文本样本。
2. 在 `workspace/` 中做基础数据探查。
3. 建立少量候选筛选逻辑、交易策略和可选 NL prompt。
4. 写入正式 `output/`。
5. 调用修改检查和验证回测。

**常规 Step 建议**

1. 读取当前 `output/`、父产物和历史 `results/valid_<idx>/`。
2. 在 `workspace/` 中复盘收益、拒单、持仓集中度、long/short 拆分、回撤和换手。
3. 修改候选筛选、交易策略、NL prompt 或参数。
4. 将当前版本写入 `output/`。
5. 调用 `modification_check`。
6. 调用 `backtest` 验证。
7. 若继续改进有明确假设，则进入下一 Step；否则调用 `finish_fold`。

**探索、收敛和结束**

探索期允许有假设、可检验的自由探索；从配置的收敛阶段开始，Agent 应优先保持收益和风险指标，其次减少代码改动、文件数量和策略复杂度。无假设的随机改动、针对特定验证月份/股票的记忆式规则、以及为了通过验证而硬编码数据都应避免。

`finish_fold` 成功后，当前 Fold 的 Agent 会话停止，`output/` 和 `models/` 保持只读，Sandbox 内 Agent 后台进程会被清理，Agent 不再写入产物。冻结、测试执行、账本记录和下一 Fold 启动由 Pipeline 处理；测试结果不反馈给 Agent。

## 3. 正式策略产物、main(ctx) 与 NL

### 3.1 目录、入口与代码边界

`output` 保存正式策略代码和轻量文本配置，可按功能拆分 helper 文件或子包。根目录必需文件：

```text
output/
  README.md        # 只读说明
  main.py          # 必需，正式入口
  candidate.py     # 推荐，候选筛选 helper
  trading.py       # 推荐，交易策略 helper
  nl_prompt.md     # 可选，NL prompt 片段
  helpers/
    signals.py     # 可选，Agent 自定义 helper
```

允许新增有清晰用途的子目录和 helper 文件，但只能使用受支持的文本/代码后缀。禁止提交 `__pycache__`、`.pyc`、`.pyo`、日志、数据 dump、模型权重、notebook、密钥或隐藏文件/目录。

`models` 保存需要跨 Fold 继承的正式模型参数、权重和轻量模型元数据，可按模型或组件分子目录：

```text
models/
  ranker/
    model.joblib
    scaler.json
  weights/
    weights.pt
```

支持常见参数/权重后缀（如 `.json`、`.txt`、`.csv`、`.joblib`、`.pkl`、`.npy`、`.npz`、`.pt`、`.pth`、`.onnx`、`.safetensors`、`.cbm`、`.ubj`、`.model`）。依赖包不写入 `models/`；新增 Python/npm/apt 依赖属于 Sandbox 镜像层，由元学习输出 `workspace/sandbox_environment.json` 后交给 Pipeline 构建派生镜像。禁止隐藏文件/目录、缓存、日志、数据 dump、notebook 和密钥。需要跨 Fold 继承的参数必须在 `backtest` 前由 Agent 工具阶段写入 `models/`；正式 `main(ctx)` 回放中 `ctx.model_dir` 只读，临时训练中间产物留在内存或 `ctx.state_dir`。

`main.py` 必须定义唯一正式入口，Environment 按回放 tick 逐 tick 调用一次：

```python
def main(ctx) -> None:
    ...
```

### 3.2 main(ctx)、ctx 接口与时序

Environment 按回放 tick 逐 tick 调用一次 `main(ctx)`（一次覆盖全市场），没有 `trade_intents` 映射。`main` 自己决定时序：每个 tick 都可核对持仓/在途，在 `ctx.substep` 内维护 `ctx.state_dir` 计划，并在选定时点（如盘前或收盘前）筛选新目标；只有显式可报单 tick（09:15/09:25/14:57）或有真实行情的交易分钟 tick 才在 `ctx.substep` 内调用 `ctx.broker` 的 `ts_code` 原语开/平仓或撤销未成交委托。普通 off-session tick 只做研究、状态更新和计划交接，不报单。Broker 执行约束并记录成交/撤单。建议把横截面筛选/开仓放在 `candidate.py`，把按 `ts_code` 的持仓管理/做T/平仓/撤单放在 `trading.py`，由 `main` 在合适时点调用，并让每个实质步骤都有明确 substep 名和正预算。

**成交时序与延迟（Agent 合同要点；执行/撮合/延迟的完整权威定义见 `environment_design.md` §3.2，本节不重复）**：订单不在决策 bar 内成交，而是延后若干 bar 进入撮合。Agent 侧要遵守的契约是：所有会读写 `ctx.state_dir`、调用 `ctx.broker`/`ctx.nl()` 或做实质筛选/推理的步骤都必须包进 `with ctx.substep(name, budget_minutes=B):`（`B>0`、tick 内 `name` 唯一、低报会 fail-fast，未包裹的实质计算会被拒绝）；`0 < B < 1` 用于轻量逐 tick 管理（视为本决策分钟内完成），`B>=1` 表示跨分钟——其 `ctx.state_dir` 写入与 broker action 都延迟到 `ready_at = 该 tick + B` 之后（broker action 再等第一个可报单 tick 提交），等待期间经 `ctx.broker.pending()` 以 `pending_stage="substep_delay"` 暴露。off-session tick 只写计划、不报单。`execution_lag_bars`、限价/竞价撮合、主动撤单、`ready_at` 后无成交 bar 的 `main_actions_unfilled/no_fill_bar_ahead` 记账等执行细节均由 `environment_design.md` §3.2 定义。

成本与频率：`main(ctx)` 每 tick 调用，但筛选、模型推理和 `ctx.nl()` 等重操作应只在少数选定时点执行、模型在首个 tick 加载/缓存，不要每个 tick 跑。`ctx.nl()` 受 NL 调用配额约束（见 §3.3 与 `environment_design.md` §2.4）；`backtest` 独立计时、单 Fold 有回测次数上限。跨 tick 暂存（如当日目标）写入 `ctx.state_dir`；Broker 是持仓真相源，`state_dir` 只存策略自身的规则/目标。

**ctx 接口**

`ctx` 是市场级上下文，每个 tick 重建，暴露：

```python
ctx.cur_date          # "YYYYMMDD"
ctx.cur_time          # "HH:MM"
ctx.cur_datetime      # ISO 时间戳（含 +08:00 时区）；同一 main(ctx) 循环也驱动实盘
ctx.account                               # 只读账户级快照（可用现金见 ctx.broker.cash / available_cash）
ctx.positions                             # 只读逐标的持仓快照列表
ctx.price(ts_code), ctx.bar(ts_code), ctx.bars   # 仅当前 tick、PIT 可见的 bar（09:15 和 off-session 无价）
ctx.broker.buy/fin_buy/short(ts_code, amount=None, weight=None, limit=None, valid_bars=1, reason=None)
                                          # 买入(担保品)/融资买入/融券卖出；weight 为名义比例；返回 order_id
                                          #   fin_buy 开融资负债合约（本金+佣金计息）；short 必须限价且申报价不低于参考最新价
ctx.broker.sell/cover/sell_repay(ts_code, amount=None, limit=None, valid_bars=1, reason=None)
                                          # 卖出/买券还券/卖券还款，只接受 amount 股数（无 weight）
ctx.broker.direct_repay(amount, reason=None)   # 直接还款（金额元，先息后本、最老合约优先；提交 tick 即时结算）
ctx.broker.close(ts_code, reason=None)                                             # 市价平掉可平持仓（恒市价，无 limit）
ctx.broker.cancel(order_id, reason=None)                                           # reason= 为可选审计注记，driver 原样记录、不影响撮合
ctx.broker.cash                           # 现金视图（每 tick 反映已成交结果，含真实成本；未成交计划不改变它）
ctx.broker.available_cash                 # 可用于买入的现金（扣融券冻结所得；保证金占用经保证金可用余额约束信用操作）
ctx.broker.credit                         # 信用账户视图（维保比例/保证金可用余额/负债/利息/额度）；普通账户为 None
ctx.broker.debt_contracts(ts_code=None)   # 未了结融资/融券负债合约明细
ctx.broker.position(ts_code)
ctx.broker.pending(ts_code=None)          # 在途/延迟提交单；无参返回全量，记录含 order_id/submitted_at/age_minutes/status，可能含 pending_stage
                                          #   substep_delay 的 age 从生成 tick 起算；submit_lag 从实际提交 tick 起算
# limit=P -> 限价单（指定价），开盘优于 P 时按 open，否则触及 P 时按 P；close 恒市价；short 必须给 limit
ctx.substep(name, budget_minutes=B)       # 上下文管理器：声明策略步骤计算时长（B>0，tick 内 name 唯一）
                                          #   所有 broker/state/NL/实质筛选推理步骤都应包裹；B<1 视为本分钟内完成，B>=1 跨 tick 延迟
ctx.nl(ts_code?, prompt=...)              # 决策阶段 NL 工具；可做单股或事件/主题/行业/宏观文本检索
ctx.asof_dir              # 逐 tick 滚动、节点门控的五个 parquet PIT 域
                          #   pd.read_parquet(ctx.asof_dir / "<域名>")，域名 daily/events/macro/fundamentals/intraday_1min
ctx.asof_version          # 仅当视图真正滚动（跨过刷新节点）才变化的版本串；按它缓存 asof 读取
ctx.snapshot_dir          # 冻结研究基线快照（不随回放滚动）
ctx.state_dir             # 宿主托管的跨 tick 状态目录；只能在 substep 内访问，写入暂存至 ready_at 才可见
ctx.model_dir, ctx.params
```

`ctx.asof_dir` 是逐 tick 滚动、按各数据集真实刷新节点门控的 PIT 视图，包含五个 parquet parts 域：`daily`、`events`、`macro`、`fundamentals` 和 `intraday_1min` 分钟历史。用 `pd.read_parquet(ctx.asof_dir / "<域名>")` 读出拼接（域名如 `"daily"`、`"events"`、`"macro"`、`"fundamentals"`、`"intraday_1min"`）。文本语料不在 `ctx.asof_dir` 下；公告/新闻经宿主侧 `ctx.nl()` 检索，并按同一刷新节点门控，冻结研究语料始终可见。可见性合同：一个 tick 只看到落库它的 cron job 已跑完的数据——交易日内横截面日频只到上一交易日（当日实时行情走 `ctx.bars`/`ctx.price`）。每个数据集的可见时点见 `data_documentation.md` §3.3，执行/Timeview 滚动机制见 `environment_design.md` §3.2。`ctx.asof_version` 只在视图真正滚动（跨过节点）时变化，策略据此缓存一次 asof 读取、仅在版本变化时重算（模板即如此）。`ctx.snapshot_dir` 是不随回放滚动的冻结研究基线。

`ctx.state_dir` 是宿主托管的跨 tick 状态目录，只能在 `ctx.substep(name, B)` 内访问：进入 substep 时会以当前可见状态为种子，块内读取看的是进入该块前的可见值；块内写入被暂存，`ready_at = 该 tick + B` 才并入可见目录（建模重计算块产出可用前的时延）。块内 broker action 按同一预算延迟提交（见 §3.2 与 `environment_design.md` §3.2）。`state_dir` 每次回测都清空重建；需跨回测持久的数据应在回测前写入 `models`，正式回放中只通过 `ctx.model_dir` 读取。完整机制见 `environment_design.md` §3.2。

Broker 原语和 `ctx` 完整语义由 `docs/environment_design.md` §3 定义。`ctx.bars` 只含当前 tick、bar close 时点已可见的行情，未来 bar 不可见（09:15 信息 tick 与 off-session tick 无价）；off-session tick 不报单，只写研究状态或 `ctx.state_dir` 计划。正式回放进程只读加载 `output/` 策略代码和 `models/` 模型产物，禁止写 `output/` / `models/`、创建软/硬链接，且按真实路径阻断经链接访问测试槽或 `/mnt/artifacts`。

`amount` 是股数（按 100 股，即 1 手，向下对齐），`weight` 是初始权益名义比例。函数只表达意图；现金、保证金可用余额、T+1 可卖余额、手数、涨跌停、停牌、融资融券标的池/额度/限价规则、负债利息和维保强平由 Broker 执行。账户类型（`credit` 信用账户默认 / `stock` 普通账户）见 experiment facts 的 `broker_replay.account_type`；普通账户下信用原语（`fin_buy`/`short`/`cover`/`sell_repay`/`direct_repay`）在 driver 层直接抛错。最大持仓数、单票权重上限和仓位集中度默认由 Agent 自行控制；只有 run config 显式设置 Broker 附加风控时才由 Broker 额外拦截（正式代码不得有死循环、网络访问或不可控写入，见 §3.1）。信用账户经济学（负债合约、利息、保证金公式、强平）见 `environment_design.md` §3.3。

**正式代码边界**

**正式策略应保持**

- 输入来自 `context`、`/mnt/snapshot`、`/mnt/agent/models` 或 `output/` 内 helper。
- 输出字段结构稳定，可被 `backtest` 解析。
- 股票代码来自可见 universe。
- 交易理由和 `source_artifacts` 可审计。
- 自定义策略函数没有死循环、网络访问或不可控写入。

### 3.3 NL 调用、日志与复盘

**调用形式**

**调用示例**

```python
from at_tools import nl

result = nl("000001.SZ", prompt="只依据可见公告和新闻评估治理风险")
event_result = nl(prompt="检索当前可见文本中是否有机器人、算力或军工相关政策催化")
content = result.get("content", "")
```

`nl()` 返回 Sub Agent result dict，而不是固定评分。常用字段：

| 字段 | 含义 |
|---|---|
| `status` | `ok` 或 `error` |
| `scope` | `stock` 或 `general` |
| `content` | NL Sub Agent 的最终回答，格式不限定 |
| `tool_calls` | Sub Agent 发起的 `text_retrieve` 检索记录 |
| `evidence` | 检索到并返回给 Sub Agent 的 PIT 文本证据 |
| `error` | 失败原因；失败策略允许时由 Agent 代码自行处理 |

宿主 Environment 负责启动 NL Sub Agent、提供 PIT `text_retrieve` 工具、构造可选公司上下文、调用 LLM provider 和日志落盘。传入 `ts_code` 时，`ts_code` 作为单股上下文和检索排序提示，不是硬过滤；不传时，Sub Agent 按 prompt 在当前可见文本库中做事件、主题、行业、宏观或市场级检索。API key 和 provider client 不进入 Sandbox。若策略需要数值分、标签或过滤条件，必须在 `main.py` / `candidate.py` / 决策 helper 中自行从 `result["content"]` 提取并用于下单决策。`ctx.nl()` 是决策阶段工具，应只在选定的少数时点调用以控制成本，不要每分钟调用。NL 分析可能受到文本发布时间/入库时间误差、检索召回偏差、模型常识污染、自由文本解析不稳定和前视泄露影响；策略应按 PIT evidence 质量降权、过滤或放弃证据不足的结论。

**日志和复盘**

**NL 日志内容**

NL 日志写入本次回测结果目录的 `nl_tool/`。

- `nl_requests.jsonl`
- `search_requests.jsonl`
- `evidence.jsonl`
- `nl_llm_calls.jsonl`

Agent 可以用验证结果和 `nl_tool/` 日志调整 prompt 或策略逻辑。若 Agent 改写 `output` 并希望改动影响当前 Step，必须重新通过修改检查并重新回测，保证策略文件、NL 调用、交易意图和回测 manifest 一致。

## 4. 修改约束、禁止行为与验收

### 4.1 修改检查与收敛标准

`modification_check` 比较父产物和当前 `output`，并校验当前 `models`：

- 文件数。
- 总 diff 行数。
- Python 代码 diff 行数。
- 总字节数。
- 只读文件修改。
- 非法文件、隐藏文件/目录和缓存。
- 模型参数文件数、总字节数、非法后缀、隐藏文件/目录和缓存。

度量修改量前，`modification_check` 先确认 diff 基准可信：父策略产物 hash（初始 Fold 用初始模板 hash）、非空父模型参数目录的父模型 hash 都必须与 run manifest 一致，否则直接报错（对称规则与空目录例外见 `environment_design.md` §2.3）。

**默认策略**

- 初始和前两个 Epoch 允许更宽松的文件数、总 diff 行数和 Python 代码 diff 行数。
- 后续 Epoch 自动收紧，鼓励保留更小、更稳定的策略。
- README 只读；修改会被拒绝。
- 总文件数和总字节数始终受限。

**收敛标准**

**结束判断维度**

- 验证总收益、long/short 收益拆分、Sharpe、最大回撤和胜率。
- 拒单原因、成交数量、持仓集中度、turnover、费用和信用利息。
- 当前改动是否比父产物更简单、可解释、可迁移。
- 最近 Step 的边际收益是否值得继续消耗 Fold 时间。
- 是否已有通过修改检查和完整验证回放的当前 hash。

每个 Fold 默认 1 小时。临近 deadline 时应收敛到当前最好、最小的可运行版本；若历史 Step 中有更好且已完整验证的版本，先恢复它为当前 `output`/`models`，再尽快完成修改检查、验证回测或 `finish_fold`。

### 4.2 禁止行为与提交前自检

**禁止行为**

- 读取测试或 held-out 数据。
- 在正式策略中引用 `/mnt/snapshots/`、`/mnt/artifacts`、`/mnt/runtime`、主仓库路径或宿主绝对路径。
- 直接调用外部网络、LLM API 或真实券商。
- 写入成交、持仓、现金、收益或账本。
- 在 `output/` 提交缓存、日志、二进制数据、模型文件、密钥或临时实验文件；模型参数只能进入 `models/`。
- 用当前验证/测试收益硬编码具体股票、日期、题材或行情事件。
- 修改只读 README、父产物、结果目录、Step 树或测试数据槽。

**提交前自检**

- `output/main.py` 存在并定义 `main(ctx)`，能驱动 `ctx.broker` 原语下单。
- 所有正式 helper 都在 `output/` 树内，入口保持根目录 `output/main.py`。
- 模型参数只放在 `models/`，且当前模型 hash 已通过最近一次修改检查。
- 持仓管理与开仓 helper 都在 `output/` 树内，由 `main(ctx)` 调用。
- NL 调用只在决策阶段通过 `at_tools.nl()`。
- `modification_check` 已通过。
- 当前 `output`/`models` 是准备提交的最好已验证版本；若不是，先恢复最佳 Step 并重新完成检查与验证。
- 最近一次验证回测成功，当前 `output` hash 和 `models` hash 未变。
- 没有缓存、日志、数据 dump、密钥、notebook 或隐藏文件/目录。
- `output`/`models` 不含从不被调用的模型、import 或死代码路径；被放弃的研究方向删除残留产物并在 finish 说明中写明原因。
