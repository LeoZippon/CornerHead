2026-06-26 QMT-faithful execution: lag knob + working-order query

- 背景：实盘 QMT 无券商侧条件单/止损单，故回测不引入引擎侧限价/止损单（否则学到实盘无法复现的成交）。改为「市价单 + 可配置成交延迟 + 在途单查询」，与实盘轮询控制器一致。
- `execution_lag_bars`（默认 2）：决策 bar 到成交 bar 的间隔，`_day_tick_plan` 连续 bar 用 `index + lag`（auction 两 tick 固定 09:15→开盘竞价、09:25→09:31，不受 lag 影响）；末尾无第 lag 根 bar 的决策记 `main_actions_unfilled`。成交价仍取成交 bar 的 open。经 ExperimentConfig→manifest→backtest_tool→引擎。
- `ctx.broker.pending(ts_code)`：暴露已报未成的在途单（与实盘委托查询一致）。引擎每 tick 把 `pending` 按 ts_code 聚合（`_working_orders`）放入 `state["pending"]`，驱动 `_Broker.pending()` 返回该码的在途单 + 本 tick 已提交动作；策略 `if not pending and position==0` 即可跨 tick 去重，替代 state_dir 记账（state_dir 仅存当日筛选目标）。
- 模板/文档同步：candidate/main/trading/README 用 pending 去重；environment_design/agent_design/prompt 改为「市价单 + lag + pending」，PROMPTS.md 重生成。
- 验证：全量单测 290 通过（新增 pending 去重测试；连续决策类测试改用更密分钟 fixture 或 auction tick 以适配 lag=2；开盘 bar 泄露测试显式用 lag=1）。

2026-06-26 Next-bar execution + screen/order split + NL offset reads + audit cleanup

- 审计死代码清理（commit `168908d`，分支 `feat/rolling-asof`）：删除未引用的 `SimBroker.buy/sell/short/cover/close` 便捷封装、`gpu.select_gpu`、`folds.*_quarter`、`audit.json_group_counts`；清理无用 import；修 `price_label="auction"` 仅在 `auction_enabled` 时标注（避免误标 09:15/09:25 真实 bar）。`common.has_pagination_probe` 是 audit.py 复用的 re-export，保留。
- 次一根 bar 成交（重写 `run_main_ctx_replay`）：用 `_day_tick_plan` 排出每日决策 tick 与各自的成交 bar——真实 bar i 的单成交于 bar i+1 开盘；09:15 信息 tick（空 group、`ctx.price=None`）成交于首根真实 bar（09:30 开盘竞价），09:25 tick（撮合开盘价、清零 vol/amount）成交于次根（09:31 首根连续）。成交价改用成交 bar 的 `open`；删除 `_auction_rows`/`auction_open`/`_fill_price`；删除死参 `decision_time_iso`。`ctx.positions` 只反映已成交持仓 → 入场意图应单 tick 下一次、跨 tick 在途意图由策略在 `ctx.state_dir` 跟踪。
- 模板按推荐节奏改写：`main` 09:15 调 `screen_targets`（筛选 + NL，目标写 `state_dir/targets.json`），09:25 调 `open_targets`（读目标统一下单，成交于 09:31）；`trading.py` 注明成交滞后、管理规则需幂等。
- M2 NL 文件按字节偏移增量读：`_serve_nl_requests`（宿主）与驱动 `_read_responses` 都改为只读 offset 之后的完整行、保留尾部半行，消除 O(N²) 重读且不丢数据；Runner 持 `_nl_offset`。
- 验证：全量单测 289 通过（更新 main_ctx/broker/tools_flow 的成交时点与价格断言为次一根 bar）；同步 environment_design/agent_design/prompt + 重生成 PROMPTS.md。

2026-06-26 09:15 pre-open info tick + WS2 rolling daily as-of view

- 09:15 信息决策 tick（commit `74e75eb`，分支 `feat/rolling-asof`）：盘前两 tick——09:15（竞价未撮合，`ctx.price=None`，~10 分钟决策窗）+ 09:25（撮合开盘价）；两者下单都经 `auction_open` 在开盘价成交。`auction_preopen_time`（默认 `09:15`，None 关闭）。注意：信息 tick 无价时乐观 broker 视图不更新持仓，按持仓条件的多笔下单不会自去重（agent 自行注意）。
- WS2 滚动日频 as-of：每个回放日 Environment 用冻结快照日线历史 ∪ 回放期 `trade_date < D` 的日线（盘前可见、当日/未来不可见）构造 `daily.parquet`（universe 从快照复制），写入沙箱可读 `workspace/.asof/<date>`，经 `ctx.asof_dir` 暴露供横截面日频筛选；事件/文本/财务/分钟历史仍读冻结 `ctx.snapshot_dir`（v1 不滚动）。`collect_artifacts` 跳过 `.asof`。config `rolling_asof_enabled`（默认开）经 manifest→tool→引擎。
- 验证：全量单测 288 通过 + 泄露回归（as-of 不含当日/未来）；模板 `candidate.py` 改读 `ctx.asof_dir`；prompt/agent_design/environment_design 同步重生成。待办：closing auction（15:00 收盘竞价对称 tick）、events/text 滚动、M2 NL 文件偏移读。

2026-06-26 PR4: NL hard cap + backtest_tool replay_window

- NL 成本硬上限：`_StrategyNLService` 按 run manifest 的 `nl_max_calls_per_backtest` 限制本次回测 NL 调用次数，超出返回审计 `budget_exhausted` 错误结果（策略自行降级）；backtest summary 记 `nl_calls`。
- `backtest_tool` 新增可选 Agent 字段 `replay_window`：只回放前 N 个交易日做快速调试，标记 `complete_validation=False`、不记 step tree、不可冻结；默认整段回放，`frozen_eval` 强制整段。经 ActionSpec field + Runner 从 args 透传 `run(mode="valid", replay_window=...)`。
- 验证：全量单测 287 通过（新增 NL 上限 budget_exhausted + replay_window 非冻结调试 各 1）；分支 `feat/agent-controls`（commit `3e750f7`）。

2026-06-26 Audit fixes + pre-open call-auction (PR3)

- 用 SubAgent 审计 `main(ctx)` 引擎，落地修复（commit `04c8283`，分支 `feat/main-ctx-engine`）：H1 合成 09:30 开盘 bar 泄露当日 high/low/vol（改为只暴露开盘价：high=low=open、vol=amount=NaN，加回归测试）；H2 常驻驱动把 Agent stdout 重定向到 stderr 但宿主只读 stdout → stderr 管道占满死锁 → 伪超时（加守护线程持续排空 stderr，`close()` 不再用 `communicate()` 抢同一 fd）；M1 步超时改为“NL 被服务即重置”的无活动超时，单次慢 `nl()` 不再吃光本分钟预算；L2 Agent `main.py` import 错误改为首个请求返回结构化错误、不崩常驻进程。M2（NL 文件 O(N²) 重读）/L1（乐观 broker 视图）/L3（裸 fd-1 写）评估为受限/设计内，暂不改。
- 盘前集合竞价（commit `436222c`，分支 `feat/preopen-auction`）：每个回放日在常规分钟前插入 `09:25` 竞价 tick（A 股撮合出开盘价的时点），价=当日开盘价、不含日内 high/low/vol；`main` 可在此筛选下单，Broker 复用当日涨跌停规则在开盘价成交（一字涨停买单/跌停空单被拒），成交 `price_label="auction"`。`09:15` 无撮合价、仅用于实盘/QMT 盘前预提交。config 旋钮 `auction_enabled`/`auction_decision_time`/`nl_max_calls_per_backtest` 经 ExperimentConfig→Fold run manifest→`backtest_tool`→引擎。
- 验证：全量单测 285 通过（新增 H1 泄露回归 + 竞价成交/一字涨停拒单各 1）。

2026-06-26 PR2: unified per-minute main(ctx) execution model

- 把"decide-once + 每股逐 bar `trade_strategy`"改为单一常驻 `main(ctx)` 引擎：Environment 每个回放分钟调用一次市场级 `main(ctx)`，Agent 用 `ts_code` 原语在任意分钟开/平仓；删除旧 `trade_intents` 一次性映射与第二个驱动。
- 提交序列（分支 `feat/main-ctx-engine`，基于 rename 分支）：`df662c3` 引擎核心 `main_ctx_engine.py`+测试；`28ebb67` `backtest_tool` 接入（结果改 `detailed_return.json`+`orders.parquet`，artifact 入口改 `main(ctx)`，模板+fixtures+test 迁移）；`14170af` 删除旧引擎（`backtest_engine.py` 1568→639 行）并把 `test_broker_engine` 迁到 `run_main_ctx_replay`。
- WS6 文档：Fold prompt（`prompts.py`/`PROMPTS.md`）、`agent_output_template/README`、`agent_design`/`environment_design`/`pipeline_design` 改为 `main(ctx)` 合约。
- PIT：分钟级 PIT 由 `ctx` 每 tick 只给 ≤`cur_time` 的 bar；横截面筛选读 `ctx.snapshot_dir`（当前为 Fold 决策时点冻结快照）。滚动 per-day as-of（WS2 完整版）、盘前竞价（PR3）、NL 硬上限 + `backtest_tool` `replay_window`（PR4）尚未实现。
- 验证：全量单测 282 通过（移除 5 个前提反转的旧测试）；`PROMPTS.md` 重生成 in sync；py_compile OK。

2026-06-26 Rename project hl_trader → autotrade (full runtime ABI)

- 先把工作区累积的 decouple-broker 重构提交为基线 `c3f6a2c`（全量单测 290 通过），再开分支 `refactor/rename-autotrade` 做全量改名（AutoTrade 新功能线第一步）。
- 包改名：`src/hl_trader/` → `src/autotrade/`（git rename 保留历史），全部 import 与 `pyproject.name` 改为 `autotrade`，editable 重装。
- 运行时 ABI：沙箱模块 `mq_tools` → `at_tools`，环境变量 `MQ_*` → `AT_*`（`AT_SNAPSHOT_DIR`/`AT_NL_*`/`AT_PROXY_*` 等），Docker 镜像 tag `macroquant-sandbox` → `autotrade-sandbox`。
- 品牌：代码 docstring、5 个 living docs、`configs/agent_output_template/*`、重生成的 `PROMPTS.md` 中 `MacroQuant`/`hl_trader` → `AutoTrade`/`autotrade`。
- 刻意保留（操作面/历史）：live cron 块标记 `# BEGIN/END MacroQuant TuShare update`、`MACROQUANT_ROOT`、文件系统路径 `/Data/lzp/MacroQuant`、LOGBOOK/DETAILED_LOGBOOK 历史条目。
- 待办：Docker 镜像需按新 tag 重建（`docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile ops/docker`），否则正式实验/元学习找不到镜像。
- 验证：compileall OK；`import autotrade` OK、`import hl_trader` 已失效（预期）；全量单测 290 通过（skipped=2，Docker 门控）；tracked 文件除 logbook 历史外无旧 token；`PROMPTS.md` 仅含 `at_tools`/`AT_`。

2026-06-25 run_027521b81c60 audit: Explore robustness, Taste period-agnostic, collect_artifacts scope

- 据 run_027521b81c60（首轮元学习，meta_learning_done）trace 审计，修复用户报告的三类问题。
- Explore 失败：`finish_reason=length`（`max_tokens=3000` 对工具调用轮太小）被 deepseek 当硬错误，导致整次探查 digest 为空。修复：`max_tokens` 3000→6000；探查循环把单轮 length/瞬时错误降级为“停止并强制一次简洁最终摘要”，不再让整次失败。
- Taste 写入 Fold/季度标签（`2022Q1`、`验证期 2021Q4`、`Fold_2022Q1 计划`）：Taste 注入之后所有 WF Fold，既不可迁移又泄露测试排程。最终处理为强化 meta prompt，要求 Agent 在 `done` 前自行检查并改写；Runner 不再用内容型正则兜底，只要求 `taste.md` 存在且非空。
- collect_artifacts 过宽：整目录拷贝 workspace 含 `.cache/pip`（容器用户 0600，宿主 lzp 不可读）→ `shutil.copytree` PermissionError。修复：`_copy_path` 加 `ignore`，归档时跳过 `.cache`/`__pycache__`/各类缓存与工具目录。
- 顺带核实：本 run 的 47 次 path_guard 误报（`agent/0`、`/CAST` 等来自 `duckdb -c "...> 0...CAST..."`）是旧 guard；已用上一轮未提交的 heredoc 剥除 + 引号屏蔽修复，离线复核该命令现返回干净（此 run 跑的是旧代码，待部署）。web_search 三视角齐全（1 次 semantic_scholar 瞬时失败已自动换引擎重试）。
- 验证：全量单测 282 OK（新增 3 个回归：explore length 救援、taste 标签拒绝、collect 跳过缓存）；PROMPTS.md 重生成 in sync；py_compile OK。

2026-06-25 Trace/context/guard/data fixes from run_de253393feea audit

- 据 run_de253393feea（首轮元学习，meta_learning_done，275 测试基线）trace 审计，落地 4 项主任务 + Codex 七问中确属真问题的 5 项。
- Trace 增量：`runner._next_turn` 改记 `new_messages`（按 `_seq` 首次出现的消息增量）+ `message_count`，不再每轮嵌入整段历史（原占 trace 83%）；拼接增量+各轮 content/tool_calls 可还原完整对话。
- 上下文触发改为以估算 token 为主：`_trim`（`trim_token_threshold=60k`）、`_clear_stale_tool_results`（`tool_result_clear_token_threshold=24k`），消息条数升到高位安全上限（`max_history_messages=150`），减少前缀改写导致的缓存重置。
- Prompt：`## 动作协议`→`## 可用工具` 并改为工具表格（FOLD+meta）；工作步骤加“当前为抽样数据、后续 Fold 扩大回测区间”说明；Taste 合同声明模板文件名非固定结构（只 `output/main.py` 必需）。
- 数据：`intraday_trade_days` 默认 5→21（约一交易月）；`data_summary.json` 只对主视图 `snapshot` 给关键列/空值、train/valid 仅规模+日期、改紧凑 JSON → 37.7KB→15KB，可单次 cat。
- Shell guard 误报修复：扫描前剥除 heredoc 正文 + 路径正则屏蔽引号内内容，`python3 -c`/heredoc 里的 `> 150`/`[:5]` 不再误判为重定向/越界（真实重定向/写命令仍拦截，加回归测试）。
- run_manifest 的 development_inputs/taste_output 改写 `/mnt/...` 挂载路径并去掉未挂载的 raw ledger；web_search 内容截断 1500 字符。
- Codex 七问判定：#3 快照构建 6.5min（1GB valid 分钟回放是回测必需，不可省）→ 不改；#6 `2>/dev/null` 已被 prompt 禁止、硬拦截属过度工程、根因是 #2 误导路径 → 不加守卫规则。
- 验证：全量单测 275 OK；PROMPTS.md 重生成 in sync；py_compile OK；data_summary/guard 经真实快照与命令离线复核。

2026-06-25 Post-Claude readonly_review audit + meta-learning Fold

- 采纳 Claude 建议的主线：`readonly_review` 静态只读 shell 审查已从源码/测试中移除，Explore 回到“提示词只读约束 + modification_check + freeze hash + Docker 挂载兜底”。本轮补齐一处提示词残留：`ExploreSubAgent` 不再声称 shell 会做“只读参数校验”，改为轻量合同 guard + 只读约定。
- 审计结论：`rg readonly_review|readonly_shell|只读参数校验 src tests configs docs/agent_design.md docs/environment_design.md` 无源码/测试/当前设计文档残留；Shell/Explore 逻辑未发现新的冗余或阻塞问题。
- 验证：`tests.unit.test_tools_flow` 62 tests OK；全量 `unittest discover -t . -s tests -p "test_*.py"` 275 tests OK；`git diff --check` OK；`find src tests scripts -type d -name __pycache__` 清理后无输出。
- 已启动并完成真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_after_readonly_revert_20260626_0053`，run_id=`run_de253393feea`，DeepSeek V4 Pro `reasoning_effort=max`，季度 WF，历史窗口 21 个月，分钟线 5 个交易日，Web Search engines=`tavily, semantic_scholar`。
- 结果：status=`taste_only`，`finish_status=meta_learning_done`，Taste 3709 chars；trace 22 次 LLM、37 次 shell、9 次 web_search、4 次 glob、0 次 context compact、0 错误；token_usage total=645434，其中 prompt=628009、completion=17425、cache_hit=324608、cache_miss=303401。
- 产物：日志 `logs/meta_learning_after_readonly_revert_20260626_0053.log`；trace `experiments/meta_learning_after_readonly_revert_20260626_0053/artifacts/run_de253393feea/agent_trace.jsonl`；manifest `experiments/meta_learning_after_readonly_revert_20260626_0053/artifacts/run_de253393feea/run_manifest.json`；Taste `experiments/meta_learning_after_readonly_revert_20260626_0053/meta_learning/epoch_001/taste.md`。

2026-06-25 Revert unsound readonly_review shell guard; keep good Codex changes

- 移除 Codex 的 `readonly_review` 静态写检测（shell.py ~169 行：`_guard_readonly_segment` + `_readonly_*` 助手 + `READONLY_REVIEW_COMMANDS`/`FIND/SORT/RG_DANGEROUS`/`GIT_*` 常量、`READONLY_FORBIDDEN_*` 正则）及 `shell.run`/`_guard_paths` 的 `readonly_review` 参数；Explore 回到“只读约定 + 兜底”。
- 理由：静态解析无法可靠拦截所有写入（`sed w`/命令替换/解释器内写文件可绕过，自带测试 FAIL），且白名单把 Explore 探查 parquet 必需的 python/duckdb 一并拦截；既不安全又破坏 Explore 用途，违背“轻量合同层、不是完整 Bash 解析器、Docker/权限/产物检查兜底”的设计。硬写隔离仍由 modification_check + 冻结 hash + Docker 只读挂载承担。
- 保留 Codex 的合理改动：deepseek `_redact_secrets` 改用 `sanitize_for_log`（更广脱敏）；NL engine 原生解析把非法参数/未知工具显式上报为 error tool_result（保留消息配对）并对 NL error 脱敏；Explore 增加 deadline/并行硬化。
- 审计结论：其余宽 `except`（runner/compact/explore/nl/proxy 的“工具/子代理失败转审计 observation，不杀 Fold”）是设计要求的弹性边界，pipeline 仍 fail-fast，非冗余 fallback，故保留；不强行抽象三处原生工具循环（关注点不同）。docs 纠正为只读“约定”而非硬保证；重生成 PROMPTS.md（Codex 漏 export）。
- 验证：全量单测 275 OK；shell.py 987→818 行；src/tests 无 readonly 残留；PROMPTS.md in sync；py_compile OK。

2026-06-25 Adopt six Claude-Code-style agent optimizations (Tier 1+2)

- #1 token/缓存计量：Runner 累计 prompt/completion/reasoning 与 DeepSeek 缓存命中/未命中，写入 session 摘要 `token_usage.cache_hit_ratio`；裁剪/压缩重置前缀缓存，据此调参。
- #2 只读 Explore Sub Agent：新增 `explore(task,max_rounds?)` 工具与 `ExploreSubAgentEngine`（原生工具循环，只读 shell/grep/glob），默认跑 flash（CLI 复用 nl_proxy），返回摘要把数据探查移出主上下文。
- #3 `write_file`/`edit_file`：新增 `ArtifactIOTool`，受控写 workspace/output/models，`edit_file` 做唯一匹配 staleness 检查、`output/README.md` 只读、写锁后拒绝；优先于 shell heredoc 维护正式产物。
- #4 NL Sub Agent 迁移原生工具调用：`engine.py` 用 `complete_tools` + `text_retrieve` schema 取代文本 JSON 解析，删除最后一个文本协议解析器。
- #5 流式长轮次：DeepSeek 客户端对工具路径走 SSE 流（重组 content/reasoning/分片 tool_calls/usage），避免空闲读超时；实测分片重组与真实流式 turn 通过。
- #6 上下文编辑：原地清理超大旧 `tool` 结果（保留 `tool_call_id`），在压缩前降低上下文，发 `context_edit` trace。
- 验证：全量单测 257 OK（新增 explore×2、artifact_io×4）；SSE 合并 + 真实 deepseek-v4-pro 流式工具调用通过；PROMPTS.md 重生成在 sync；env/agent 设计文档更新。GPU 为既有外部负载，本次未启动训练。

2026-06-25 Migrate Agent loop to DeepSeek V4 native tool calling

- Spike：deepseek-v4-pro/flash 原生 function calling 全部通过——返回标准 `tool_calls`（finish_reason=tool_calls），一轮可并行多个 tool_calls，`reasoning_effort=max` 思考与工具调用共存；`tool_calls` 在场时 `content` 为空。
- 实现：`ActionSpec`/`ActionField` 增 `to_tool_schema()`/`to_json_schema()`；DeepSeek 客户端增 `chat_tools` + tools payload，`DeepSeekResponse`/`ProviderResponse` 增 `tool_calls`，`_parse_response` 允许工具 turn 空 content；`LLMProxy.complete_tools`（DeepSeekProxy/ScriptedLLM + `tool_call`/`tool_call_response` 测试构件）。
- Runner 改原生工具循环：一轮处理全部 `tool_calls`（每个回一条 `tool` 结果），只读 `concurrency_safe` 工具批量并行、有状态工具串行；`_trim`/compact 防止 `tool` 结果脱离其 `assistant` 工具调用；删除单 JSON 动作解析。
- Prompt 动作协议（Fold + 元学习）改为原生工具调用并支持并行只读，重生成 `configs/prompts/PROMPTS.md`；compact 估算计入 `tool_calls`；env/agent 设计文档补充原生工具调用与并行批处理说明。
- 验证：全量单测 251 OK；真实 DeepSeek V4 集成校验通过（一轮并行返回 grep+shell 两个 tool_calls，reasoning 存在，第二轮承接 tool 结果继续）。临时 spike/集成脚本已删除；GPU 为既有外部负载，本次未启动训练任务。

2026-06-24 Meta-learning JSON retry hint and overfit boundary fix

- 分析本轮 `meta_learning_rerun_20260625_0238` 的两次 `LLMProxyError`: DeepSeek HTTP 返回正常，但 action 内容把多行 shell/Python 命令写成未正确转义的 JSON 字符串，导致 JSON 解析失败；Runner 继续重试后完成。
- Runner 对 `invalid_action` 追加更具体的 retry hint，提示必须返回合法 JSON，多行 Python/shell 优先用 heredoc 或正确转义换行和引号。
- Prompt 修复：普通 Fold 和元学习 Taste 明确区分训练输入、验证反馈、测试和 held-out；验证结果属于 development 反馈，可用于复盘/模型选择，但不能硬编码验证期具体结果；test/held-out 始终不可见。
- 验证：Prompt export OK；py_compile OK；Runner/meta prompt 定向单测 2 tests OK。

2026-06-24 Meta-learning rerun audit trace

- 重新运行一次真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_rerun_20260625_0238`，run_id=`run_1b509f529ccf`，DeepSeek V4 Pro，reasoning_effort=max，季度 WF，默认历史窗口 21 个月，分钟线 5 个交易日，Web Search engines=`tavily, semantic_scholar`。
- 结果：`finish_status=meta_learning_done`，status=`taste_only`，Taste 1538 chars；trace 记录 24 次 LLM 调用、16 次 shell、3 次 web_search、1 次 glob、1 次 modification_check、1 次 session_end；context compact 未触发。
- 审计整理：已覆盖写入 `check.md`，以对话形式整理本轮过程、工具结果、LLM 非 JSON 错误、DuckDB 字段修正和最终 Taste。运行日志 `logs/meta_learning_rerun_20260625_0238.log`；canonical trace `experiments/meta_learning_rerun_20260625_0238/artifacts/run_1b509f529ccf/agent_trace.jsonl`。
- 运行前后资源检查完成：可用内存约 447Gi -> 445Gi；GPU 为既有外部负载，本次未启动训练任务。

2026-06-24 Backtest engine lightweight slimming

- `backtest_engine.py` 抽出两个 sandbox driver 共用的路径 guard bootstrap，减少重复维护面；删除策略 action 执行里的旧 `target_weight` 兼容读取，仅保留当前契约 `weight`。
- 验证：py_compile OK；`tests.unit.test_broker_engine` 25 tests OK；`tests.unit.test_tools_flow.ToolFlowTest` 23 tests OK；缓存已清理。测试前后可用内存约 446Gi -> 445Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Runtime env package field clarification

- `/mnt/artifacts/runtime_env.json` 的 Python 依赖字段从 `important_packages` 改为 `python_packages`，避免 Agent 把 Python import 能力误判为 CLI 可执行命令；`tools` 仍表示可直接调用的命令行工具。
- Prompt 和 living docs 仅做术语级同步：runtime env 记录 Sandbox Python 包、CLI 工具、网络/安装策略和资源摘要。
- 验证：Prompt export OK；py_compile OK；runtime env 定向单测 OK；目标文件 `git diff --check` OK；缓存已清理。测试前后可用内存约 446Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Formal meta-learning Fold audit run

- 按上一轮正式审计配置启动一次真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_formal_20260624_2153`，run_id=`run_c68b0781704c`，主 Agent=`deepseek-v4-pro`，NL/compact=`deepseek-v4-flash`，reasoning_effort=max，季度 WF，默认历史窗口 21 个月，分钟线 5 个交易日，Fold deadline 60 分钟，Web Search engines=`tavily, semantic_scholar`，compact 阈值 200k。
- 结果：`finish_status=meta_learning_done`，status=`taste_only`，Taste 761 chars；trace 记录 22 次 `llm_call`、12 次 `shell`、3 次 `web_search`、1 次 `tool`、1 次 `session_end`；三类检索视角均成功，context compact 未触发。
- 审计路径：运行日志 `logs/meta_learning_formal_20260624_2153.log`；ledger `experiments/meta_learning_formal_20260624_2153/ledgers/experiment_ledger.jsonl`；Taste `experiments/meta_learning_formal_20260624_2153/meta_learning/epoch_001/taste.md`；canonical trace `experiments/meta_learning_formal_20260624_2153/artifacts/run_c68b0781704c/agent_trace.jsonl`；manifest `experiments/meta_learning_formal_20260624_2153/artifacts/run_c68b0781704c/run_manifest.json`；runtime sandbox `.runtime/sandboxes/run_c68b0781704c/`。
- 运行前后资源检查完成：可用内存约 272Gi -> 273Gi；GPU 为既有外部负载，本次未启动训练任务。运行产物密钥/代理字符串扫描无匹配；缓存扫描为空。

2026-06-24 Meta Learning network prompt simplification

- 元学习联网是默认能力，联网与代理规则已并入系统提示词 `# 环境与配置 / ## 运行环境、联网与代理`；删除单独的 `network_guidance` Prompt 片段、`ExperimentConfig.meta_learning_network_guidance` 字段和 manifest 重复字段。
- 具体 Docker 网络、透传环境变量和 `MQ_PROXY_*` 代理别名以 `runtime_env.json` 的 `sandbox_spec` 和 run manifest 为准；Prompt 只保留通用规则：默认直连，卡顿/失败时才临时映射代理别名，不打印或持久化 token/proxy 值。
- 文档同步：`PROMPTS.md` 重新导出；`agent_design.md`、`pipeline_design.md` 改为指向 `runtime_env.json`/manifest，不再描述额外注入段。
- 验证：Prompt export OK；py_compile OK；Prompt render 一致性 OK；`test_sandbox_isolation + test_pipeline_e2e + test_tools_flow` 90 tests OK；`run_experiment.py --help` OK；目标文件 `git diff --check` OK；密钥/代理字符串扫描无匹配；缓存已清理。测试前后可用内存约 271-272Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Prompt structure cleanup

- 按用户要求重排 Fold Agent 与 Meta Learning Agent Prompt：顶层统一为 `# 角色与目标`、`# 环境与配置`、`# 动作与流程`；动态 Fold 信息、验收规则、Taste、阶段指引收敛为二级标题。
- 元学习 Prompt 中 `工作顺序` 改为非强制的 `工作步骤`，明确可随时重新调用 `shell`、`grep/glob` 和 `web_search`；`shell` 数据检查措辞改为“详细检查和分析”，不再写“再形成 Taste”。
- 普通 Fold 仍不默认开放 `pip/npm/git/hf/curl/wget` 和联网；理由是正式回测可复现性、依赖冻结和测试隔离。Meta Learning 继续承担联网研究和依赖可行性验证。
- 文档同步：`agent_design.md`、`pipeline_design.md` 中元学习数据检查措辞改为详细检查和分析；`PROMPTS.md` 重新导出。
- 验证：Prompt export OK；py_compile OK；Prompt render 一致性 OK；`test_sandbox_isolation + test_pipeline_e2e` 49 tests OK；目标文件 `git diff --check` OK；密钥/代理字符串扫描无匹配；缓存扫描为空。测试前后可用内存约 271-272Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Meta Learning audit fixes + runtime cleanup

- 按 SubAgent 审计修复元学习硬边界：真实 Runner session summary 必须是 `meta_learning_done` 才能被 Pipeline 采纳；`done` 前必须有非空 `workspace/taste.md`；启用 web_search 时三类 perspective 只有非空结果才算成功。
- 普通 Fold 的 `sandbox_shell_tool` 增加工具层拦截，拒绝常见安装/下载/联网入口（如 `pip install`、`npm install`、`git clone`、`hf download`、`curl/wget`）；Meta Learning 保留开放网络和依赖试装能力。
- 扩展日志脱敏：trace 和大输出文件覆盖 OpenAI/HF/GitHub token、带凭据代理 URL 和 VLESS 链接；Prompt action schema 不再固定写死 web_search 引擎列表。
- 删除仓库根 `.runtime`：从约 222GiB 清理到不存在。第一次删除遇到历史只读 sandbox 权限，已对剩余 2.3MiB 当前用户文件加写权限后完成删除。
- 文档同步 `agent_design.md`、`environment_design.md`、`pipeline_design.md`，并重新导出 `PROMPTS.md`。
- 验证：Prompt export OK；py_compile OK；`test_sandbox_isolation + test_pipeline_e2e + test_tools_flow` 89 tests OK；Prompt render 一致性 OK；密钥/代理字符串扫描（排除 `.env` 与 external references）无匹配；目标文件 `git diff --check` OK；缓存扫描为空。测试前后可用内存约 271Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Meta Learning system prompt cleanup

- 整理元学习 Agent System Prompt：按角色目标、工作顺序、首轮空历史、可读写文件、运行环境与联网、动作协议、研究协议、Taste 输出合同、探索容忍、可选正则化和禁止事项重排；去掉旧式重复标题和容易混淆的 “development 摘要” 表述。
- `scripts/dev/export_prompts.py` 的审计快照标题改为 `元学习 Agent System Prompt（基础模板）`，`configs/prompts/PROMPTS.md` 已重新导出，旧的 `Web Search Engines` / `development 摘要` / 重复“实验级探索方向注入示例”命名未再出现。
- 验证：`export_prompts.py` OK；`py_compile` OK；`tests.unit.test_sandbox_isolation` 26 tests OK；密钥/代理字符串扫描（排除 `.env` 与 external references）无匹配；目标文件 `git diff --check` OK；生成缓存已清理。测试前后可用内存约 271Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Runtime env + experiment parameter visibility

- Sandbox 每个 run 现在写 `/mnt/artifacts/runtime_env.json`，记录 Python、关键依赖、网络/安装策略和资源摘要；文件随 run artifacts 收集。Docker 模式写 Dockerfile contract，本地模式写当前 Python probe。
- 普通 Fold manifest 新增 `runtime_env_ref`；meta-learning manifest 新增 `experiment_parameters`，汇总 Fold 周期、开发/held-out period、snapshot 窗口、验收规则、Broker profile、deadline、Step tree 和 Sandbox 资源。正式 CLI 还写 `agent_session_config` 与脱敏 `llm_config_summary`。
- Fold/Meta Prompt、`PROMPTS.md` 和 living docs 同步：Agent 先读 `run_manifest.json` 与 `runtime_env.json`，不假设未列出的包可用，不在 Fold 内安装新包；结构化检索 root 对齐 `models` 和 `parent_models`。
- 验证：`export_prompts.py` OK；py_compile OK；`test_sandbox_isolation + test_pipeline_e2e + test_tools_flow + test_step_tree` 87 tests OK；`run_experiment.py --help` OK；目标文件 `git diff --check` OK；生成的 Python cache 已清理。测试前后可用内存约 272Gi -> 275Gi；未启动 GPU 工作。

2026-06-24 Models directory + Tavily prompt alignment

- Agent 可见模型参数目录收敛为 `/mnt/agent/models/`，父模型基准为 `/mnt/artifacts/parent_models/`，冻结模型参数 sibling 目录为 `<strategy_artifact_id>.models/`；`output/` 仍只放单层轻量策略代码。
- 决策阶段暴露 `context["model_dir"]`、`context["workspace_dir"]`、`MQ_MODEL_DIR`、`MQ_WORKSPACE_DIR` 和 `mq_tools.nl()`；逐分钟交易 `ctx` 不再暴露 `model_dir`、`workspace_dir` 或 `nl`，需要的模型/NL 结果必须提前写入 `trade_intents.params`。
- Artifact 校验、modification_check、finish_fold、backtest summary、StepTree、Pipeline freeze/fallback/frozen_eval/held-out 均复核 strategy hash、model artifact hash 和 combined hash；`models/` 仍禁止子目录、缓存、日志、数据 dump、notebook 和密钥。
- Meta Learning 的 System Prompt 现在渲染 `# Web Search Provider`，CLI 传入 `tavily`、`semantic_scholar` 或 `disabled`；提示词分别说明 Tavily 通用网页检索和 Semantic Scholar 论文检索的 query 写法。
- 文档和模板同步：Agent prompt、PROMPTS.md、模板 README/main.py/trading.py、agent/environment/pipeline docs 均使用 `models/` 与决策期 `model_dir`。
- 验证：资源检查完成；定向 `test_tools_flow test_broker_engine test_pipeline_e2e` 80 tests OK；完整 `unittest discover -s tests` 228 tests OK；`git diff --check` OK；源码/脚本/测试/模板缓存扫描为空。测试前后可用内存约 212-214Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-24 Context compact layer

- 参考 `external_references/claude-code-main` 的 token-window/autocompact 思路，新增 Runner context compact 层：按粗略 token 阈值触发，默认用独立 `deepseek-v4-flash` 且关闭 thinking，输出结构化继续状态，保留最近原始消息。
- `agent_trace.jsonl` 新增 `context_compaction` 事件，记录 provider/model、触发估算、usage、summary hash、压缩前后消息数、状态和错误摘要；主 `llm_calls` 仍表示 Agent 行动轮次，`context_compaction_calls` 单独统计并有 `--compact-max-calls` 上限。
- SubAgent 迭代审计 3 轮均已关闭；按审计修复 deadline stale remaining、`max_calls=0` 语义、compact 调用上限、失败熔断测试和 provider error 脱敏。最终审计无阻断问题。compact 会为主 LLM 调用预留时间片，完成后重新计算 deadline，若超时则不再启动主 LLM。
- 文档同步：`agent_design.md`、`environment_design.md`、`pipeline_design.md` 补充 context compact 的会话语义、LLM 边界、trace 字段和调用预算。
- 验证：`AgentSessionRunnerTest` 12 tests OK；`test_pipeline_e2e` 16 tests OK；`run_experiment.py --help` OK；相关文件 `py_compile` OK；`git diff --check` OK；提交面缓存扫描为空。测试前后可用内存约 221-222Gi；GPU 为既有外部负载，本次未启动 GPU 工作。

2026-06-23 Epoch smoke run with configurable windows

- 配置并执行一轮小规模 Epoch：month Fold，development `2022-01`，held-out `2022-02`，`SnapshotConfig` 为 daily 6m / fundamentals 12m / events 6m / macro 24m / text 3m / intraday 2 trading days。
- 真实 DeepSeek flash + Docker 运行暴露两个问题：`2>&1` fd 合并曾被 shell guard 误判为写重定向（已修复并加测）；`deepseek-v4-flash` 在 Fold 内出现重复 shell 写文件直到 deadline 的不稳定行为，未产出完整 validation backtest。
- 新增实验 CLI 验收阈值参数 `--min-return`、`--min-sharpe`、`--max-drawdown`、`--allow-incomplete-validation`，默认生产规则不变；`pipeline_design.md` 同步说明。
- 受控 `ScriptedLLM` + Docker 跑通完整闭环：实验 `epoch_smoke_scripted_20260623_0419` 产出 `strategy_epoch_001_fold_202201`，held-out runs=1；模板无候选/无交易，validation/test/held-out 均 total_return=0、sharpe=0、max_drawdown=0、order_count=0。
- 验证：`ShellToolTest + test_pipeline_e2e + test_sandbox_isolation` 38 tests OK；`git diff --check` OK；提交面缓存扫描为空。运行前后可用内存约 395Gi -> 394Gi；未启动新的 GPU 工作负载。

2026-06-23 Configurable data preparation windows + dual SubAgent audit

- 数据准备窗口改为每次 Experiment Config 决定：`SnapshotConfig` 支持统一 `window_months`，也支持 daily/fundamentals/events/macro/text 分域月份窗口和 `intraday_trade_days`；CLI 增加对应参数，run/snapshot manifest 记录生效配置和分域窗口。
- 两个 SubAgent 已完成并关闭：代码审计覆盖快照、Fold 调度、Sandbox guard、Broker 分钟回放和工具链；文档审计覆盖 living docs 简约性、完整性和代码匹配度。
- 按审计修复：Shell local-dev guard 禁止绕过 `/mnt/snapshot` 读取宿主 `runtime/snapshot_views`；day Fold 使用交易日而非自然日；非 quarter CLI 必须显式给出 generic period；fundamental datasets 分区按每个配置数据集 fail-fast；策略 proxy 支持 weight 下单后的同 bar 乐观持仓视图；Prompt 快照恢复 Step 产物树段落，living docs 修正手数、Broker/NL/准备窗口表述。
- 验证：资源检查完成；定向单测 96 tests OK；完整 `unittest discover -s tests` 214 tests OK；`run_experiment.py --help` OK；非 quarter CLI 参数校验 OK；`git diff --check` OK；缓存扫描为空。测试前后可用内存约 377Gi -> 352Gi；未启动新的 GPU 工作负载。

2026-06-23 Full audit follow-up + configurable Fold period

- SubAgent 审计已完成并关闭；主要发现为 `train_snapshot` alias 未在 manifest 记录、Step 摘要文档过度承诺、历史分数阈值/候选数配置残留、实验 CLI 默认路径依赖 cwd、`external_references/` 需按外部参考材料对待。
- 修复：run manifest 明确记录 `train_snapshot.alias_of=valid_decision_input` 且 hash 一致；Pipeline docs 的 Step 摘要字段改为代码实际写入字段；实验 CLI 默认路径改为仓库根目录绝对路径；`ExperimentConfig` 支持 `fold_period=day/week/month/quarter/year`，主字段改为 `*_period`，旧 `*_quarter` 仅作兼容别名；`folds.py` 新增通用 period range/bounds/held-out 生成；Broker/Profile/Pipeline 去除无用 `long_score_threshold`、`short_score_threshold`、`max_candidates`、`candidates_truncated` 残留。
- 文档同步：`agent_design.md` / `environment_design.md` / `pipeline_design.md` 明确 `/mnt/snapshots/train` 是 `valid_decision_input` 的 Agent-visible alias，Fold 可按配置周期滚动，living docs 不再残留“测试季度/历史季度”旧语义。
- 验证：资源检查完成；`test_pipeline_e2e test_broker_engine test_tools_flow` 66 tests OK；`run_experiment.py --help` OK；周期边界专项 OK；完整 `unittest discover -s tests` 210 tests OK；`git diff --check` OK；缓存扫描为空。内存前后约 421-422Gi 可用；未新增 GPU 工作负载。

2026-06-23 Living docs consistency audit

- 审计五份 living docs 与当前代码一致性：工具名/模式、snapshot 文件、Broker 原语、`ctx` 接口、NL 类名、09:30 竞价因子（0.76/0.58）、11 个 cron 任务、CLI 子命令、6 个状态文件名、result-dir 文件均一致；无残留过时 token 或迁移/版本注释。
- 修正两处：`agent_design.md` §5.3 删除与 `environment_design.md` §7.2 重复的 `ctx` 接口清单，改为指向 env 第 7 章（去冗余）；`pipeline_design.md` §8.3 fold 账本示例删除代码未写入的 `parent_strategy_artifact_hash`，并把 `snapshot_ids` 键改为实际的 `valid_decision_input`/`test_decision_input`/`valid_replay`/`test_replay`（一致性修正）。
- 流程逻辑复核：下载/更新/审计门禁、Fold/Epoch/Held-out 编排、freeze-before-test、PIT 可见性、QMT standby 流程均合理且内部自洽。保留的 §2/§5.2 可见时间速查与双视角路径表为有意设计（不同读者/用途），未删。

2026-06-23 Step tree visibility + meta-learning full records

- Step 产物树三项可见性增强：每次 `save()` 同步写可读渲染 `steps/tree.txt`（含收益、当前位置、`[failed]` 标记）；成功节点复用 attachments 附带验证 `detailed_return.json` / `strategy_metadata.json`；失败验证回测写轻量 `[failed]` 死路节点（无产物快照、不改变 `current_node_id`，`position_for_hash` 跳过失败节点），由 `record_failed_attempts` 配置开关控制（默认开）。
- 元学习输入修复与增强：原 `meta_learning_memory.jsonl` 误指向当前 Epoch 尚未写入的 trace（恒空），改为按 Epoch 顺序拼接此前所有 Epoch 元学习会话的 `agent_trace.jsonl`；新增 `experiment_ledger_full.jsonl`（完整原始 fold/meta 账本，排除 held-out）。两者均注入 `workspace/`。
- 同步更新 `agent_design.md`（steps 树描述）、`pipeline_design.md`（6.1 元学习输入）、`prompts.py` 并重新生成 `PROMPTS.md`（快照此前未随近期重构刷新，本次一并对齐）。
- 验证：`unittest discover` 204 tests OK（含新增 step_tree 失败节点/渲染 2 项、pipeline_e2e 元学习注入 1 项、扩展 prompt 断言）；`git diff --check` 干净；无缓存泄漏。内存前后 ~420Gi 可用。

2026-06-23 Agent output path rename

- Sandbox 内正式策略产物路径从 `/mnt/agent/agent_output/` 改为 `/mnt/agent/output/`；`workspace/` 仍是临时探索区，`output/` 是唯一正式策略产物来源。
- `MQ_AGENT_OUTPUT_DIR` 默认值、Docker 路径映射、结构化检索 root、living docs、Agent prompt、`PROMPTS.md` 生成快照、模板说明和测试期望已同步；结构化检索公开 root 使用 `output`。
- 验证：`tests.unit.test_sandbox_isolation tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_step_tree` 60 tests OK；`git diff --check` OK；缓存扫描为空。
- 集中度规则跟进：默认 Broker profile 不再强制最大持仓数或单票权重上限，profile id 更新为 `citic_default_v3`；Agent 通过候选筛选、股数/权重和交易策略自行控制集中度。只有显式配置 `broker_profile.max_total_holdings` / `max_single_name_weight` 时，Broker 才作为附加风控执行。验证：相关 61 tests OK；最终补跑 `test_broker_engine` 24 tests OK；`git diff --check` OK；缓存扫描为空。
- NL 服务重构：`mq_tools.nl()` / `ctx.nl` 不再返回固定评分，改为启动宿主侧 NL Sub Agent；Sub Agent 可通过 `text_retrieve` 读取 PIT 文本证据，最终 `content` 不限定格式，Agent 代码自行解析。失败默认返回可审计 error result，不再生成中性分。验证：`test_nl_scoring test_tools_flow test_pipeline_e2e` 50 tests OK；`git diff --check` OK；缓存扫描为空。
- 策略冻结 manifest 收敛：artifact `manifest.json` 只保留身份、血缘、hash 和来源 run/fold/step；验证结果、run manifest 引用和修改检查摘要保留在 Fold ledger Step 记录。验证：`test_pipeline_e2e test_artifacts test_step_tree` 23 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-22 Broker/策略解耦

- 把交易策略从 Broker/Environment 内置语义中剥离：Broker 只暴露按股数的基础原语（`execute`/`buy`/`sell`/`short`/`cover`/`close` + `get_account`/`get_positions`/`query_orders`/`trades_for`），不再内置 `target_weight`/`low_buy`/`close_buy`/`high_short`/`t`/`flat`/`none`。
- 所有策略改为 Agent 在 `output` 中定义的函数 `def 名字(ctx): ...`；`main.py` 的 `trade_intents` 把每只股票映射到一个策略函数（`{code, trade_strategy, params}`）。Environment 按分钟逐 bar 调用，缺分钟数据时退化为日线合成 09:30/15:00。
- Broker 新增按股数部分建/减仓、加权平均成本、T+1 可卖余额（`locked_today`）、运行期 `max_total_holdings`（`max_holdings_reached`）、单票权重上限钳制、`trades_for` 历史成交、`position_reduced`/`position_closed` 盈亏事件，支撑做 T/波段。
- Docker `popen` 补 `-i`，使常驻策略 RPC 进程在容器内可用（旧内置路径从未触发该进程，掩盖了此问题）。
- 模板、prompts、PROMPTS.md 与 `environment_design.md`/`agent_design.md` 同步更新；分支 `refactor/decouple-broker-strategies`，改动保留在工作区未提交。
- 跟进修补：模板策略函数统一改为 `example_*` 样例名，文档明确样例不是内置策略；做 T 示例增加空成交历史保护。
- 验证：`unittest discover` 208 通过；broker/tools/pipeline 四模块 66 通过（含 Docker e2e）；`git diff --check` 通过；无缓存泄漏。内存前后 ~404Gi 可用。

2026-06-22 数据更新与审计修复

- TuShare cron 日期语义已修复：交易日数据 job 支持 `end_date_mode=sse_open_on_or_before`，周末/节假日自然目标日会回落到最近 SSE 开市日；PIT event job 的滚动起点按月初对齐，避免月分区审计误报。
- 系统 crontab 已刷新：实际任务不再调用旧 `cn_nightly_feature_build`，改为 `cn_nightly_pit_event_build`。
- `margin` / `margin_detail` 最近应可见交易日缺口已补齐；`cn_evening_full`、`cn_preopen_event_flow_audit_0920`、`cn_nightly_pit_event_build`、`cn_nightly_full_audit` 均已通过 cron 编排层，最新状态为 `ok`。
- 晚间更新后已重跑 PIT 事件构建和全量审计。6 个原始数据状态文件及 `fundamental_events_status.json` 均无 error，warning 保留为覆盖率或语义提示。
- 五份 living docs 顶部的 `更新时间` 元信息已移除；`docs/data_documentation.md` 改为通用状态文件验收规则，不保留一次性运行日期结论。

2026-06-20 当前状态摘要

- 本机开发、脚本、测试和 cron 使用 `~/miniconda3/envs/quant`；Docker Sandbox Python 独立，依赖变更需重建 `ops/docker/sandbox.Dockerfile`。
- 当前事实源为 `docs/data_documentation.md`、`docs/agent_design.md`、`docs/environment_design.md`、`docs/pipeline_design.md`、`docs/QMT_documentation.md`。
- 数据更新流程已修复到 `scripts/data/tushare_cron_update.py` + quant env。顶层状态仍有 warning，但 error 为 0；warning 主要是口径/语义风险。
- 正式 revision ledger 已清理 `/tmp` 测试污染；临时 raw/test raw 默认只写本地 `revision_events.jsonl`，不得污染 `results/data_quality/revision_events.jsonl`。
- Revision sentinel 结论：`20260612-20260618` 当前缺口检查已无新旧数据差异事件；历史抽样仅发现 `limit_list_d.limit_amount` 源端回写为空。该字段已标记为 `raw_audit_only_until_field_versioned`，不得进入冻结交易输入。
- Agent 可见数据主路径已收敛为 PIT snapshot/history window、标准单位和可见性约束；固定滚动收益、均线、波动率等预构建 alpha 列已移除。财务事件只保留 `data/pit/fundamental_events` 作为 PIT 可见性索引；snapshot 构造会在审计 status 为 error、errors>0、根目录缺失或无可用分区时 fail fast。
- Agent 正式产物为单层 `output/`，入口为 `main.py`，可搭配 `candidate.py`、`trading.py`、`nl_prompt.md` 和少量文本/代码辅助文件。
- `backtest_tool` 运行 `output/main.py` 并接收其交易意图。策略代码显式调用 `mq_tools.nl(ts_code, prompt=...)` 时，宿主侧提供 PIT 文本检索、LLM 评分和审计日志；NL JSON schema 为 `ts_code`、`nl_score`、`confidence`、`risk_tags`、`evidence_ids`，不再依赖 prior 规则。
- `modification_check_tool` 只检查 `output` 的文件数、diff 行数、Python 代码 diff 行数、总字节数、只读文件和非法文件；首个产物和前两个 Epoch 更宽松，后续 Epoch 收紧。
- Broker 支持日线和分钟线回放；不再内置策略名。`trade_strategy` 会调用 Sandbox 中 `trading.py` / `main.py` 的同名函数，每个 due bar 返回 buy/sell/short/cover/close 动作，宿主 Broker 仍执行现金、T+1、涨跌停、停牌和做空约束。
- 默认 Fold 时间为 60 分钟。每次 formal backtest 写 `detailed_return.json`、`trade_intents.parquet`、`strategy_metadata.json`、可选 `candidates.parquet` 和可选 `nl_tool/`。
- SubAgent 迭代审计已覆盖：revision sentinel 临时 raw ledger 隔离、BacktestTool schema、Sandbox Fold 时间、交易计划路径清理、`flat`/`none` no-op 边界、策略/NL RPC 临时文件异常清理、PIT event fail-fast、NL prior 残留和 sandbox 空值解释。
- Agent、Environment、Pipeline 三份 living docs 已恢复到 `###` 导航，保留 PIT 数据窗口、Sandbox、Agent 工具、Broker、Fold/Epoch 和报告等运行细节；当前文档使用正向契约描述，避免保留版本对比文字。
- `.runtime/sandboxes/run_00add6d7173e/snapshots/train/macro.parquet` 的 `cn_gdp` 大量空值是多 dataset 宽表 union 的结构性空列；GDP 自身字段完整可见，其他 sandbox 大表的结构性稀疏也符合当前 schema。旧 `data/features/daily_alpha`、`data/features/fundamental_events` 和空的 `data/features/` 父目录均已删除。
- 最新验证：`build_pit_events.py --help` OK；`cn_nightly_pit_event_build --dry-run` OK；`data/pit/fundamental_events` 已构建 742 个 parquet，审计 `status=warning/errors=0/rows=1,828,774`；`tests.unit.test_features` 7 tests OK；`TuShareDownloadUpdateGuardsTest` 57 tests OK；`test_snapshot_builder` 7 tests OK；`test_pipeline_e2e` 13 tests OK；`test_nl_scoring + test_tools_flow` 45 tests OK；完整 `unittest discover` 208 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Meta Learning Web Search engine 选择

- 元学习 `web_search` 从“外层选择单一 provider + Agent 填 category”改为“外层暴露 engines，Agent 每次 action 自选 `engine`”。默认 engines 为 `tavily` 和 `semantic_scholar`，无 disabled 默认状态。
- `WebSearchTool` 改为多 engine wrapper，trace 记录 `engine`、实际 provider、query、result_count；`done` 不再强制三类 category，只要求配置了 engines 时至少执行一次 web_search。
- `run_experiment.py` 参数改为 `--web-search-engines`，manifest/ledger 记录 `web_search_engines`。Prompt、PROMPTS.md、Agent/Environment/Pipeline docs 已同步。
- 验证：py_compile OK；`run_experiment.py --help` OK；`tests.unit.test_sandbox_isolation` 19 tests OK；`test_sandbox_isolation + test_pipeline_e2e + test_tools_flow` 75 tests OK；完整 `unittest discover -s tests` 228 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 PROMPTS 去重与 Meta Learning Step Tree

- `PROMPTS.md` 初步删除了单独重复的 Web Search Engine 示例段；动态注入段在下一条 Prompt 收敛中继续删除。
- 修复元学习 run 未安装历史 `steps/tree.json` 的问题：`run_meta_learning()` 现在在 step tree 启用时同步 experiment-level step tree，并把当前位置指向父产物节点。
- 新增 pipeline 回归测试确认 Meta Learning 可读取 `ctx.paths.steps/tree.json` 和 `tree.txt`。
- 验证：`export_prompts.py` OK；新增/相邻 3 个 pipeline tests OK；`tests.unit.test_pipeline_e2e` 18 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Meta Learning Prompt 可读文件表

- 元学习 Prompt 直接删除动态注入的 `# Web Search Engines` 和 `# development 摘要` 段；联网检索和可见文件已在静态 Prompt 中说明。
- 第一段明确要求先读 `/mnt/artifacts/steps/tree.txt` 或 `tree.json`。
- “可读文档和组织结构”改为表格，汇总 `steps`、development history、full ledger、meta memory、parent output/models、run manifest、当前 output/models 和 `taste.md`。
- 验证：`export_prompts.py` OK；`tests.unit.test_sandbox_isolation + test_meta_learning_can_read_existing_step_tree + test_step_tree` 26 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Meta Learning 首轮空历史 Prompt 分支

- 元学习 Prompt 新增“首轮空历史处理”：当 `tree.txt` 为 `(empty step tree)`、`tree.json.nodes` 为空、development 账本为空或 `meta_learning_memory.jsonl` 为空时，明确这是首轮正常状态。
- 首轮要求不追查缺失历史、不编造已验证结论、不正则化不存在的过拟合经验；重点阅读初始 `output/`、`models/`、`run_manifest.json` 和可见数据/工具契约，并结合联网检索形成首轮 Taste。
- 可读文件表同步标记 `steps`、`parent_output`、`parent_models` 首轮可能为空。
- 验证：`export_prompts.py` OK；Prompt grep 命中首轮分支；相关 26 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Meta Learning Prompt 整合与探索容忍

- `PROMPTS.md` 不再导出重复的 `元学习协议模板（META_LEARNING_INSTRUCTION）`；只保留一个 `元学习 + 正则化系统提示词（完整渲染示例）`。
- 元学习 Prompt 新增“探索容忍”：允许当前方案或上一轮结果不好但仍可能通过有假设、有复盘、有可检验改进路径的探索变好；同时要求降级重复失败、个股/月度记忆或缺少机制的方向。
- 验证：`export_prompts.py` OK；Prompt grep 确认重复段已删除且探索容忍已渲染；相关 26 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Meta Learning 首轮提示去重

- 删除第一段里“若为空，这是第一轮正常情况”的重复提示，只保留“首轮空历史处理”小节集中说明空 `steps`/账本/meta memory 的行为。
- 验证：`export_prompts.py` OK；Prompt grep 确认重复句消失；`test_sandbox_isolation + test_step_tree` 25 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Fold Agent 文件结构 Prompt

- Fold Agent `PROTOCOL_INSTRUCTION` 新增“文件结构和读写边界”表，汇总 `/mnt/agent/workspace`、`output`、`models`、`snapshot`、`snapshots/train|valid|test`、`run_manifest`、`parent_output`、`parent_models`、`results`、`steps`、`logs`、`agent_trace` 的权限、内容和用途。
- 表格明确区分 Agent 工具可读写边界和正式策略代码运行边界：正式策略代码只能读取 `/mnt/snapshot`、`/mnt/agent/output`、`/mnt/agent/models`。
- 验证：`export_prompts.py` OK；Prompt grep 确认表格已导出；`test_sandbox_isolation + test_tools_flow + test_step_tree` 64 tests OK；`git diff --check` OK；缓存扫描为空。

2026-06-24 Agent 正式产物目录自由度调整

- 将正式产物约束从单层 `output/` / `models/` 改为受控多层正式产物目录：`output/main.py` 仍是唯一固定入口，helper/子包和模型参数子目录由 Agent 按方案自由组织。
- `artifacts.py` 递归校验/哈希/复制策略代码和模型产物，继续拒绝符号链接、隐藏文件/目录、运行缓存、非法后缀、数据 dump/日志类产物和正式代码越界路径引用。
- 默认约束放宽到受控项目尺度：strategy files/bytes 64/1MB，model files/bytes 64/1GiB，仍可由实验 Config 覆盖。
- 同步 template README、Fold Prompt、PROMPTS.md、Agent/Environment/Pipeline docs 和 artifact tests。
- 跟进命名澄清：当前文档和 Prompt 统一称 `output/` 为“正式策略产物目录”、`models/` 为“可继承模型产物目录”，避免和 Step 产物树混淆。
- 验证：`export_prompts.py` OK；`test_artifacts + test_tools_flow + test_step_tree + test_sandbox_isolation` 73 tests OK；旧 flat/单层强约束关键词扫描无命中；`git diff --check` OK。

2026-06-24 策略入口收敛为 run_strategy

- `output/main.py` 正式入口收敛为唯一函数 `run_strategy(context)`；删除 `main(context)` 兼容分支和模板转发函数，减少 Agent 和 Environment 的双入口歧义。
- `load_strategy_artifact()` 和 backtest 驱动均只接受/调用 `run_strategy(context)`；`main(context)` only 会被拒绝。
- Prompt、PROMPTS.md、Agent 文档和单元测试同步。
- 验证：`export_prompts.py` OK；`test_artifacts + test_tools_flow + test_broker_engine` 72 tests OK；入口残留搜索仅剩 main-only 拒绝测试；`git diff --check` OK。

2026-06-24 Meta Learning Docker audit run

- 启动真实 Docker sandbox 元学习 Fold：`experiment_id=meta_learning_audit_20260624_1458`，run_id=`run_15b5d81f61d0`，主 Agent=`deepseek-v4-pro`，compact model=`deepseek-v4-flash`，Web Search engines=`tavily, semantic_scholar`，实验配置为季度 WF、默认历史窗口 21 个月、分钟线 5 个交易日。
- 产物路径：`experiments/meta_learning_audit_20260624_1458/meta_learning/epoch_001/taste.md`、`experiments/meta_learning_audit_20260624_1458/meta_learning/epoch_001/agent_trace.jsonl`、`experiments/meta_learning_audit_20260624_1458/artifacts/run_15b5d81f61d0/run_manifest.json`、运行日志 `logs/meta_learning_audit_20260624_1458.log`。
- 结果：状态 `taste_only`，Taste 3132 chars，修改检查通过且正式产物无改动；Docker runtime 记录容器 `mqsbx_b9df49936564`、镜像 `macroquant-sandbox:latest`、分配 GPU `[1]`。
- Trace 摘要：13 次 DeepSeek V4 Pro 主对话调用，2 次 web_search（Tavily 5 条、Semantic Scholar 5 条），7 次 shell，1 次 glob，1 次 modification_check。上下文压缩层已配置但未触发（会话短，`context_compactions=0`）。

2026-06-24 Meta Learning Trace 去重

- 元学习 trace 收敛为单一 canonical 文件：只保留 `artifacts/run_<id>/agent_trace.jsonl`，不再复制到 `meta_learning/<epoch>/agent_trace.jsonl`。
- `meta_learning` 账本记录新增 `agent_trace_ref`，下一轮 `meta_learning_memory.jsonl` 从该引用拼接；旧账本没有该字段时按 `run_id` 推导 canonical trace。
- Pipeline 文档同步说明 `meta_learning/<epoch>/` 只保留 `taste.md`，避免 API/token 统计重复计数。
- 已删除 `meta_learning_audit_20260624_1458/meta_learning/epoch_001/agent_trace.jsonl` 的历史重复副本，canonical trace 保留在 `artifacts/run_15b5d81f61d0/agent_trace.jsonl`。
- 验证：`test_pipeline_e2e` 18 tests OK；`git diff --check` OK；资源复查无本次新增 GPU 占用。

2026-06-24 Context Compact 阈值调整

- 默认 semantic compact 触发阈值从估算 50k tokens 调整为 200k tokens，更接近 Claude Code 在 200k context 下的自动压缩区间，同时避免 DeepSeek V4 过早丢失原始上下文。
- `ContextCompactionConfig`、实验 CLI `--compact-token-threshold` 默认值、CLI help、Agent/Environment 文档已同步。
- 验证：py_compile OK；`run_experiment.py --help` 显示 default 200000；默认配置断言为 200000；`tests.unit.test_tools_flow` 39 tests OK；`git diff --check` OK。测试未启动 GPU。

2026-06-24 Meta Learning 三视角检索 Prompt

- 元学习系统提示词新增多轮检索要求：鼓励围绕同一探索问题从金融/量化/经济、其他自然科学/工程、哲学/方法论三类视角互相校验。
- Taste 输出要求说明探索方向为何适配本次 run manifest 的 Fold 周期、数据窗口、日线/分钟线交易频率、做多/做空能力、回放成本和验证指标。
- Pipeline/Agent 文档和 `PROMPTS.md` 已同步。
- 验证：`export_prompts.py` OK；`py_compile` OK；`test_sandbox_isolation + test_pipeline_e2e` 37 tests OK；新 Prompt 关键词 grep 命中；`git diff --check` OK。测试未启动 GPU。

2026-06-24 Meta Learning Docker audit rerun

- 按上一轮配置重跑真实 Docker sandbox 元学习 Fold：`experiment_id=meta_learning_audit_20260624_160835`，run_id=`run_ba28c68398b5`，主 Agent=`deepseek-v4-pro`，compact model=`deepseek-v4-flash`，Web Search engines=`tavily, semantic_scholar`，季度 WF、默认历史窗口 21 个月、分钟线 5 个交易日，compact 阈值 200k。
- 产物路径：`experiments/meta_learning_audit_20260624_160835/meta_learning/epoch_001/taste.md`，canonical trace `experiments/meta_learning_audit_20260624_160835/artifacts/run_ba28c68398b5/agent_trace.jsonl`，manifest `experiments/meta_learning_audit_20260624_160835/artifacts/run_ba28c68398b5/run_manifest.json`，运行日志 `logs/meta_learning_audit_20260624_160835.log`。
- 结果：状态 `taste_only`，Taste 2514 chars，修改检查通过且正式产物无改动；Docker runtime 记录容器 `mqsbx_9e6110b1c00d`、镜像 `macroquant-sandbox:latest`、分配 GPU `[1]`。
- Trace 摘要：17 次 DeepSeek V4 Pro 主对话调用，2 次 web_search（Agent 两次均选择 Tavily；Semantic Scholar 已暴露但未被使用），11 次 shell，2 次 tool。总 token 125,162，其中 prompt 117,045、completion 8,117、reasoning 6,355；上下文压缩未触发（`context_compactions=0`）。
- 验证：`meta_learning/epoch_001/` 只保留 `taste.md`，未生成重复 trace；`git diff --check -- LOGBOOK.md docs/logbook/DETAILED_LOGBOOK.md` OK；运行容器结束后已清理。
- 已将本轮 canonical `agent_trace.jsonl` 整理为对话式审计摘要写入 `check.md`：覆盖 17 轮动作、首轮空历史、两次 Tavily 成功检索、一次 Semantic Scholar 429、Taste 内容、modification_check 和审计关注点。

2026-06-24 Meta Learning search hardening

- `web_search` action 新增 `perspective` 字段，Runner 在启用搜索时要求 `finance_quant_econ`、`natural_science_engineering`、`philosophy_methodology` 三类视角各有一次成功检索后才允许 meta-learning `done`；`engine` 仍由 Agent 自选。
- Semantic Scholar provider 增加每 key 文件锁节流（默认 1.25s 间隔）、429/5xx 指数退避重试、较轻字段集；Tavily 也加入有限 HTTP 重试。错误继续红act API key。
- 元学习 Prompt 和 Fold Prompt 增加 NL 前视/数据泄露、检索召回、模型常识污染和自由文本解析风险提示；Taste 要保持方向性和简洁，不写具体模板函数名或过细实现计划。Runner 对 `taste.md` 中模板样例前缀和过长内容做保护。
- 文档和 `PROMPTS.md` 已同步；验证：py_compile OK；`test_sandbox_isolation + test_pipeline_e2e` 39 tests OK；`test_tools_flow` 39 tests OK；`git diff --check` OK。

2026-06-24 DeepSeek max reasoning and meta-learning rerun

- `DeepSeekProxy.from_env()` 默认在 thinking 启用时传 `reasoning_effort=max`；compact 仍关闭 thinking 且不传 reasoning effort。
- Fold/Meta Prompt 增加“深入思考要求”：关键决策前从机制假设、可见数据、执行约束、反证路径和失败模式充分推理，但输出保持简洁可验证。
- 验证：`export_prompts.py` OK；`py_compile` OK；`test_llm_deepseek + test_sandbox_isolation` 38 tests OK。
- 真实 Docker meta-learning rerun：`experiment_id=meta_learning_audit_20260624_171700`，run_id=`run_877f23366817`，trace `experiments/meta_learning_audit_20260624_171700/artifacts/run_877f23366817/agent_trace.jsonl`，taste `experiments/meta_learning_audit_20260624_171700/meta_learning/epoch_001/taste.md`。
- 结果：状态 `taste_only`，Taste 2951 chars，三视角检索全部满足（Semantic Scholar 4 次），LLM total tokens 200,087、reasoning tokens 7,047，context compact 未触发（0 次），修改检查通过且正式产物无改动。

2026-06-24 Meta Learning directive interface

- 后续正式 Agent/NL 路径默认使用 DeepSeek `reasoning_effort=max`；CLI 保留 `--reasoning-effort` 和 `--no-thinking` 作为显式消融/调试覆盖，compact 仍不启用 thinking。
- 新增实验级 `meta_learning_directive`：可通过 `ExperimentConfig`、`--meta-learning-directive` 或 `--meta-learning-directive-file` 注入研究者想探索的方向，只进入 Epoch-start Meta Learning Prompt，并写入 run manifest 与 meta-learning 账本。
- Meta Prompt 新增“实验级探索方向（用户注入）”段，要求 Agent 将其视为待检验假设，可采纳、细化、降级或拒绝，仍需遵守 PIT、三视角检索、NL 风险和过拟合约束。
- 文档和 `PROMPTS.md` 已同步；验证：`export_prompts.py` OK；`py_compile` OK；`run_experiment.py --help` OK；`test_llm_deepseek + test_sandbox_isolation + test_pipeline_e2e` 58 tests OK；`git diff --check` OK。

2026-06-24 Meta Learning shell network and dataset probe prompt

- Meta Learning Prompt 改为建议 Agent 通过 `shell` 调 Python 对可见 snapshot 做只读抽样检查，再写 Taste；没有增加硬门禁。
- 实验 CLI 新增 `--meta-learning-network`、`--meta-learning-env`、`--meta-learning-add-host-gateway`，只对 Epoch-start Meta Learning sandbox 生效。Docker 启动只透传环境变量名，不把 token/代理值写入 manifest、trace 或命令记录。
- Sandbox 镜像契约补充 `git`、`npm`、`hf`/`huggingface-cli`；Dockerfile 安装 `git/curl/npm` 和 HuggingFace CLI。普通 Fold 默认仍离线。
- Prompt 导出不再保留两份重复完整 Meta Learning 模板，实验方向示例改为追加片段。
- 验证：Prompt export OK；py_compile OK；`test_sandbox_isolation + test_pipeline_e2e + test_tools_flow + test_step_tree` 89 tests OK；secret/代理链接 grep 无命中；`git diff --check` OK；测试缓存已清理。

2026-06-24 Meta Learning direct network and optional host proxy

- Meta Learning Docker network default changed to `bridge` direct internet; ordinary Fold/held-out sandbox policy remains unchanged.
- `GITHUB_TOKEN` and `HF_TOKEN` are now default meta-learning env passthrough names. The host did not currently expose `GITHUB_TOKEN`, so no token usability check was run and no raw token was embedded in commands or files.
- Added `--meta-learning-host-proxy`; when enabled it passes proxy values as `MQ_PROXY_*` aliases and records the alias names in runtime metadata. Direct internet remains the default path.
- Prompt export/docs/tests updated. Verification: py_compile OK; `test_sandbox_isolation + test_pipeline_e2e` 45 tests OK; generic secret scan clean; `git diff --check` OK; caches cleaned.

2026-06-24 Meta Learning proxy aliases and GitHub token check

- `.env` 中的 `GITHUB_TOKEN` 通过 GitHub `/user` API 鉴权，登录名为 `LeoZippon`，rate limit 为 5000/小时；检查过程未打印或写入 token。
- `--meta-learning-host-proxy` 改为把宿主标准代理变量映射为 `MQ_PROXY_*` 非标准别名，不直接注入 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`，因此 Agent 默认直连；需要时可单条命令临时映射代理。
- 实验 CLI 会从仓库根目录 `.env` 选择性加载本次允许透传的变量名（如 `GITHUB_TOKEN`、`HF_TOKEN` 和 host proxy 需要的宿主代理变量），不打印、不记录变量值。
- Docker bridge 下本地回环代理地址会改写为 `host.docker.internal`，并只通过子进程环境传入，不进入 docker run 参数。
- 验证：Prompt export OK；py_compile OK；`test_sandbox_isolation + test_pipeline_e2e` 46 tests OK；`.env` 已被 gitignore；排除 `.env` 后通用 secret scan clean；`git diff --check` OK；缓存清理完成。

2026-06-24 Meta Learning visible data and Shell guard simplification

- Meta Learning 现在拿到与第一个 Fold 相同的 `valid_decision_input`：绑定为 `/mnt/snapshot`，并作为 `/mnt/snapshots/train` 的只读 alias；不创建验证回放、test 或 held-out 可见槽。
- Prompt 主语言统一为中文，Runner 初始消息也改为中文；Fold/Meta Prompt 明确不要用 `2>/dev/null` 隐藏错误。
- Shell path guard 改为按真实写入目标判断：只读列目录、`os.listdir('/mnt')`、读取 artifacts/snapshot 和复制只读文件到 workspace 被允许；写入只读目录、test、runtime 或 sandbox 外路径仍拒绝。
- 为简化 manifest，元学习只新增可见 Fold 和 snapshot id/hash，继续复用 `experiment_parameters`，不重复写 fold/snapshot 配置。
- 文档和 `PROMPTS.md` 已同步。验证：Prompt export OK；py_compile OK；`test_pipeline_e2e + ShellToolTest + MetaLearningSessionTest` 38 tests OK；`git diff --check` OK。

2026-06-24 Standard Meta Learning Fold rerun

- 按标准正式环境重跑一次 Docker meta-learning-only Fold：`experiment_id=meta_learning_formal_20260624_230548`，run_id=`run_2ce27d85d933`，主 Agent=`deepseek-v4-pro`，NL/compact=`deepseek-v4-flash`，reasoning_effort=max，季度 WF，默认历史窗口 21 个月，分钟线 5 个交易日，Fold deadline 60 分钟，Web Search engines=`tavily, semantic_scholar`，compact 阈值 200k。
- 本轮使用 direct `ExperimentPipeline.run_meta_learning()`，只跑元学习，不跑普通 Fold replay 或 held-out；已传入第一个 Fold，manifest 记录 `fold_2022Q1` 的 `valid_decision_input`，`/mnt/snapshot` 与 `/mnt/snapshots/train` snapshot hash 相同。
- 结果：状态 `taste_only`，`finish_status=meta_learning_done`，Taste 969 chars，modification_check 通过且正式产物无改动；context compact 0 次。
- Trace：20 次主 LLM 调用，11 次 shell，3 次 web_search，1 次 tool；token 汇总 total 217,917，prompt 204,750，completion 13,167，cache hit 186,752。
- 三视角搜索均成功：Tavily 金融/量化/经济 5 条，Semantic Scholar 自然科学/工程 5 条，Tavily 哲学/方法论 5 条。
- 关键路径：taste `experiments/meta_learning_formal_20260624_230548/meta_learning/epoch_001/taste.md`；trace `experiments/meta_learning_formal_20260624_230548/artifacts/run_2ce27d85d933/agent_trace.jsonl`；manifest `experiments/meta_learning_formal_20260624_230548/artifacts/run_2ce27d85d933/run_manifest.json`；日志 `logs/meta_learning_formal_20260624_230548.log`；runtime sandbox `.runtime/sandboxes/run_2ce27d85d933/`。
- 验证：运行前后资源检查完成；Docker 容器已退出；敏感信息扫描无匹配；`find src scripts tests -name __pycache__` 为空。运行后可用内存约 443Gi；GPU 使用为既有外部任务和 Docker 分配记录，本轮未启动本地训练。

2026-06-24 Meta Learning first Fold visible data parity

- 修复 Meta Learning 可见数据口径：后续运行会与第一个 Fold Agent 一样看到 `/mnt/snapshot`、`/mnt/snapshots/train` 和 `/mnt/snapshots/valid`；其中 `valid` 是第一个 Fold 的验证回放槽，`test` 和 held-out 仍不可见。
- 文档、系统提示词快照和 pipeline manifest 字段已同步；旧的 `.runtime/sandboxes/run_2ce27d85d933/` 是修复前创建的 sandbox，不会自动回填 `snapshots/valid`。
- 验证：Prompt snapshot consistency OK；`py_compile` OK；`test_pipeline_e2e` 3 个相关用例 OK；`git diff --check` OK；缓存清理完成。

2026-06-24 Shell output budget and template path cleanup

- `sandbox_shell_tool` schema 新增可选 `max_output_chars`，只允许缩小内联 stdout/stderr 上限；长输出仍落盘并返回路径，避免把大输出塞进上下文。
- 初始模板现在复制到只读 `parent_output` 作为 sandbox 内 diff 基线；run manifest 不再记录宿主 `template_dir` 绝对路径，只记录 `template_ref` 和 `initial_template_hash`。
- 参考 Claude Code 的 BashTool：其 schema 不暴露 `max_output_chars`，但采用固定结果上限和大输出持久化；本项目在已有落盘机制上增加显式预算字段，更适合当前 JSON action 协议。
- 验证：Prompt export/consistency OK；`py_compile` OK；`test_tools_flow` 41 tests OK；`test_pipeline_e2e + test_sandbox_isolation` 50 tests OK；`git diff --check` OK；缓存清理完成。

2026-06-25 Claude Code tool-pattern selective adoption

- 选择性引入 Claude Code 值得借鉴且维护成本低的 Tool 机制：`ActionSpec` 增加 `schema_version` 和 `result_policy`；`sandbox_shell_tool` 增加 `command_kind` 审计标签和受限 `timeout_seconds`；`grep/glob` 标注分页预算策略；`web_search_tool` 明确作为元学习 Tool。
- 代码结构调整：`environment/web_search.py` 保留 Tavily/Semantic Scholar provider 和 `WebSearchService`，新增 `environment/tools/web_search.py` 放 Agent-facing `AgentWebSearchTool`、schema 和 trace 逻辑；Runner 不再内联 web_search spec/trace。
- 未引入原生 tool-use 协议、任意后台任务和交互式权限确认，因为这些会显著增加 provider 适配和实验复现复杂度。
- 验证：Prompt export/consistency OK；`py_compile` OK；`test_tools_flow + test_sandbox_isolation + test_pipeline_e2e` 93 tests OK；`git diff --check` OK；缓存清理完成。

2026-06-25 Tool guard audit fixes and meta-learning Fold rerun

- 多轮 SubAgent 只读复审发现并闭环了 Shell guard 边界：普通 Fold 安装/联网绕过、裸相对写入、Python `open/Path/to_csv` 写入、`bash/sh -c` 和 `find -exec sh -c` 嵌套写目标、无空格重定向 `x>target`。最终复审结论：Blocking/Should Fix/Nice To Have 均无。
- `web_search_tool` 失败现在也记录脱敏 trace；Runner 的 ToolError/WebSearchError/generic Exception observation 统一脱敏，避免错误摘要把 token 带入下一轮上下文或 trace。
- 验证：`ShellToolTest + MetaLearningSessionTest` 22 tests OK；`test_tools_flow + test_sandbox_isolation + test_pipeline_e2e` 95 tests OK；`git diff --check` OK。
- 真实 Docker meta-learning-only Fold 已重跑：`experiment_id=meta_learning_tool_audit_20260625_013641`，run_id=`run_4c7511878785`，状态 `taste_only`，`finish_status=meta_learning_done`，Taste 1005 chars；trace 22 次 LLM、16 次 shell、3 次 web_search、0 次 compact。
- 关键路径：日志 `logs/meta_learning_tool_audit_20260625_013641.log`；manifest `experiments/meta_learning_tool_audit_20260625_013641/artifacts/run_4c7511878785/run_manifest.json`；trace `experiments/meta_learning_tool_audit_20260625_013641/artifacts/run_4c7511878785/agent_trace.jsonl`；Taste `experiments/meta_learning_tool_audit_20260625_013641/meta_learning/epoch_001/taste.md`。

2026-06-25 Shell guard slimming and structured failure hints

- Shell guard 从重型嵌套写入解析收缩为轻量合同层：保留阶段锁、明确越界路径、明确写只读根、写未管理目录、普通 Fold 安装/下载入口、输出和超时预算；复杂 shell 细节交给 Docker 只读挂载、目录权限和后续产物检查兜底。
- `/mnt/agent` 根目录改为不可写，只开放 `workspace/`、`output/`、`models/` 三个写入面；修复 `rg -i` / `grep -i` 这类只读搜索被全局 `-i` 误判为写入的问题。
- ToolError/Runner observation 增加 `error_type`、`reason`、`retry_hint`、`blocked_target`，保留原 `error` 字段兼容旧日志和测试；Prompt 和 living docs 已同步。
- 验证：Prompt export OK；`py_compile` OK；`ShellToolTest + MetaLearningSessionTest + test_pipeline_e2e` 44 tests OK；`test_tools_flow + test_sandbox_isolation` 75 tests OK；`git diff --check` OK；缓存已清理。

2026-06-25 Meta Learning rerun after Claude prompt/docs update

- 以当前 Claude 优化后的文档和 Prompt 重跑真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_after_claude_20260625_1113`，run_id=`run_2bdfdf1a4375`，DeepSeek V4 Pro，`reasoning_effort=max`，季度 WF，历史窗口 21 个月，分钟线 5 个交易日，Web Search engines=`tavily, semantic_scholar`。
- 结果：`finish_status=meta_learning_done`，状态 `taste_only`，Taste 1627 chars；trace 记录 37 次 LLM、28 次 shell、6 次 web_search、7 条 context_summary，语义 compact 0 次；token 汇总约 770,023 total，其中 prompt 747,450、completion 22,573。
- 非致命问题：2 次 DeepSeek 返回内容不是合法 JSON，Runner 重试后恢复；1 次 web_search 空结果；大 parquet 读取和 DuckDB 查询错误由 Agent 后续调整处理。
- Taste 对训练/验证/测试边界比上一轮更清楚，但仍提到 2022Q1 下跌行情和“测试期可能下跌”的外部历史知识，后续应在 Prompt 中约束不得用模型内置世界知识推断隐藏测试/held-out 结果。
- 关键路径：日志 `logs/meta_learning_after_claude_20260625_1113.log`；trace `experiments/meta_learning_after_claude_20260625_1113/artifacts/run_2bdfdf1a4375/agent_trace.jsonl`；Taste `experiments/meta_learning_after_claude_20260625_1113/meta_learning/epoch_001/taste.md`；ledger `experiments/meta_learning_after_claude_20260625_1113/ledgers/experiment_ledger.jsonl`。

2026-06-25 Meta Learning no-lookahead prompt hardening

- 在元学习 Prompt 的 `## 禁止事项` 中加入前视约束：不得利用模型内置历史知识、公开搜索结果或日期标签推断测试/held-out 的真实行情、收益、板块轮动或个股表现；日期范围只是实验调度元信息，不是可用交易证据。
- 重新导出 `configs/prompts/PROMPTS.md`，并将本轮 `meta_learning_after_claude_20260625_1113` 的过程按对话形式整理到 `check.md`，包含关键工具调用、失败恢复、Taste 摘要和审计结论。
- 同步修正一个元学习 Prompt 单测断言，使其匹配当前统一中文文案。
- 验证：`export_prompts.py` OK；`py_compile` OK；`MetaLearningSessionTest` 15 tests OK；`git diff --check` OK；测试缓存已清理。

2026-06-25 Meta Learning rerun after no-lookahead hardening

- 使用修复后的元学习 Prompt 重跑真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_no_lookahead_20260625_1148`，run_id=`run_8caa7f451792`，DeepSeek V4 Pro，`reasoning_effort=max`，季度 WF，历史窗口 21 个月，分钟线 5 个交易日，Web Search engines=`tavily, semantic_scholar`。
- 结果：ledger 记录 `finish_status=meta_learning_done`，状态 `taste_only`，Taste 2429 chars；trace 记录 40 次 LLM、24 次 shell、5 次 web_search、11 条 context_summary、2 次 modification_check，语义 compact 0 次；token 汇总约 1,011,326 total，其中 prompt 990,218、completion 21,108。
- 非致命问题：1 次 pyarrow schema 探查代码错误、1 次 pandas 列访问错误、1 次 shell 超时、1 次 DeepSeek 空内容、1 次 Semantic Scholar 空结果，Agent 后续恢复并完成。
- 前视检查：Taste 未再写隐藏测试期真实行情/收益/板块表现，但仍把“验证期（2022Q1）或测试期（2022Q2）”的日期标签写得不准确；manifest 显示正确边界是 validation `20211001..20211231`、hidden test `20220101..20220331`、held-out config `2022Q2`。
- 关键路径：日志 `logs/meta_learning_no_lookahead_20260625_1148.log`；trace `experiments/meta_learning_no_lookahead_20260625_1148/artifacts/run_8caa7f451792/agent_trace.jsonl`；Taste `experiments/meta_learning_no_lookahead_20260625_1148/meta_learning/epoch_001/taste.md`；ledger `experiments/meta_learning_no_lookahead_20260625_1148/ledgers/experiment_ledger.jsonl`。

2026-06-25 Meta Learning runtime analysis

- 已覆盖写入 `check.md`，专项分析 `meta_learning_no_lookahead_20260625_1148` 为什么外层 wall time 约 49 分钟。
- 结论：Agent trace 仅约 508.6 秒；主要耗时在 trace 前的 snapshot/replay 准备，约 40 分 49 秒。decision fundamentals 约 17 分钟、decision daily 约 8.6 分钟、valid replay 分钟线约 6.5 分钟。
- 建议：优先实现 snapshot/replay cache 和分域耗时日志；其次考虑元学习轻量 valid replay、预生成 data_summary、强化大表 DuckDB/metadata 探查规则。

2026-06-25 Data build summary and large-table guidance

- 优化财务 PIT 事件读取：decision snapshot 现在按窗口下推 `available_month` 分区选择，再按 `available_at` 二次过滤，避免短窗口扫描全历史财务事件分区。
- Snapshot/replay manifest 新增 `build_profile` 和 `data_profile`；Pipeline 在 Agent 启动前写 `/mnt/artifacts/data_summary.json`，Fold 和元学习都只汇总当前可见数据视图，不暴露 test/held-out。
- Fold/元学习 Prompt、Runner 初始消息、Shell 工具提示和 living docs 已同步：大表优先 DuckDB limit/count、Parquet metadata、按列/按日期读取，避免未知规模全量 `pd.read_parquet()`；Prompt 只描述稳定协议，当前数据事实以本 run 动态生成的 data summary 和 manifest 为准。
- 验证：Prompt export OK；`py_compile` OK；`test_snapshot_builder` 10 tests OK；pipeline/meta prompt 组合 13 tests OK；`git diff --check` OK；缓存已清理。

2026-06-25 Meta Learning rerun with data summary

- 重跑真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_data_summary_20260625_1343`，run_id=`run_89f2ee1f54e4`，DeepSeek V4 Pro，reasoning_effort=max，季度 WF，历史窗口 21 个月，分钟线 5 个交易日，Web Search engines=`tavily, semantic_scholar`。
- 结果：`finish_status=meta_learning_done`，ledger 状态 `taste_only`，Taste 约 2110 chars；trace 记录 25 次 LLM、14 次 shell、4 次 web_search、0 次 compact。5 次 LLM 非致命错误（4 次 JSON 格式、1 次 length）后恢复完成。
- 新 data summary 已写入并收集：`experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/data_summary.json`。Agent 首轮读取了 data summary，并使用 DuckDB 查询可见 parquet。
- 构建 profile：decision snapshot 总计约 188 秒，其中 fundamentals 约 26 秒；valid replay 总计约 554 秒，瓶颈仍是 valid `intraday_1min.parquet`（build 约 452 秒、write 约 43 秒）。
- 关键路径：日志 `logs/meta_learning_data_summary_20260625_1343.log`；trace `experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/agent_trace.jsonl`；manifest `experiments/meta_learning_data_summary_20260625_1343/artifacts/run_89f2ee1f54e4/run_manifest.json`；Taste `experiments/meta_learning_data_summary_20260625_1343/meta_learning/epoch_001/taste.md`。

2026-06-25 Meta Learning prompt and data summary slimming

- 元学习 Taste 输出合同删除会诱导复述特定 Fold 周期/窗口/验证边界的两条要求，仅保留候选方向、NL 使用与风险、收益/风险/修改量取舍。
- Agent-visible `data_summary.json` 调整为轻量索引：保留文件规模、行数、列数、关键列、日期覆盖和大表提示；不再暴露完整 columns、build_profile 或重复的大表对象。完整 schema 由 Agent 按需用 Parquet metadata/DuckDB 查询。
- 用 `run_89f2ee1f54e4` 快照生成样例验证：旧摘要约 130,533 bytes，新摘要约 37,684 bytes。已完成 run 的原 artifact 未改写，以保留审计一致性。
- 验证：Prompt export OK；`py_compile` OK；`test_single_epoch_runs_meta_learning_before_fold_and_heldout` OK；2 个 MetaLearningSession Prompt tests OK。

2026-06-25 Explore Shell hardening after Claude Code comparison

- 参考 `external_references/claude-code-main` 的 BashTool/read-only validation 思路，保留 Explore SubAgent 的轻量只读 Shell，但从单纯命令名检查补到参数级拒绝：允许常见 read/list/search 和安全 git inspection，拒绝写入、解释器、`find -exec/-delete/-fprint`、`sort -o`、`rg --pre` 等副作用路径。
- 修复审计发现的脱敏缺口：DeepSeek conversation log、NL SubAgent 失败、strategy policy RPC 和 NL RPC error 都覆盖 Bearer/Authorization 类 secret；NL 原生 `text_retrieve` 参数 JSON 解析失败会返回明确 tool error，不再静默变成空检索。
- 文档同步：`docs/environment_design.md`、`docs/agent_design.md` 和 Explore SubAgent prompt 说明 Claude-Code-style 只读边界。
- 验证：受影响 58 tests OK；相关 183 tests OK；全量 `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -t . -s tests -p "test_*.py"` 跑 268 tests OK；`git diff --check` OK；未残留 `__pycache__`。
- SubAgent 复审发现并已修复：Explore readonly shell 禁止命令替换/进程替换/heredoc/env 覆盖，NL `text_retrieve` 缺 pattern 或 pattern 非字符串返回 tool error，policy import/early-exit stderr 脱敏，Explore grep/glob 受 fold deadline 约束。补充负例测试后，全量 272 tests OK；`git diff --check` OK；未残留 `__pycache__`。
- 第二轮 SubAgent 复审未发现 High；已修复剩余 Medium：`glob` 不再在 deadline 检查前全量 `sorted()`，改为增量窗口收集并逐项检查 deadline；`StepTree.save()` 对写盘 `tree.json` 做防御性脱敏；补 decision-stage strategy stderr 脱敏、StepTree 失败节点脱敏和稳定 deadline helper 测试。全量 274 tests OK；`git diff --check` OK；未残留 `__pycache__`。
- 第三轮 SubAgent 复审未发现 High/Medium；已处理其 Low 备注：`glob` 改为确定性递归遍历和增量分页，不再依赖 `Path.glob()` 的未定义顺序，也不再做窗口排序。补充 `*.py` 非递归、`**/*.py` 递归和跨页不重复测试。全量 274 tests OK；`git diff --check` OK；未残留 `__pycache__`。
- 后续复核发现并闭环两个边界问题：`glob` 跳过 symlink，避免目录链接递归；正式回放 policy guard 改为 realpath 检查，回放阶段禁止创建软/硬链接，禁止写 `output/`，并阻断通过外部 symlink 读取 `models/`。同步修复 NL 未知 native tool call 显式报错、DeepSeek SSE 文档表述。按用户要求不再继续迭代复核。验证：新增最小回归 4 tests OK；相关 193 tests OK；全量 278 tests OK；`git diff --check` OK；未残留 `__pycache__`。
- 针对近期修改做冗余/垃圾审计：代码区无缓存残留；发现 DeepSeek 本地 secret 正则与 runtime 脱敏重复，已改为复用 `sanitize_for_log`，保留 provider log 的敏感 key 判断。验证：DeepSeek/NL/关键 redaction 40 tests OK；`git diff --check` OK；未残留 `__pycache__`。

2026-06-26 Trace/context/data summary audit follow-up

- 审计 Claude trace/context/guard/data_summary 改动并开 SubAgent 交叉复核；修复 CLI 仍将 `intraday_trade_days` 覆盖为 5 的问题，默认链路现与 `SnapshotConfig=21` 和文档一致。
- 修复 SubAgent 发现的两项上下文问题：compact prompt 不再携带 runner 内部 `_seq` 字段；context_edit 不再清理同一轮刚产生、尚未返回给 LLM 的工具结果。
- 补充 data_summary 异常路径脱敏、Agent session manifest 中 trim/edit token 阈值记录，并把 Prompt “逐步扩大回测范围”改为按配置周期向后滚动。
- 验证：相关 115 tests OK；全量 279 tests OK；`git diff --check` OK；测试缓存已清理。

2026-06-26 Pipeline Taste inheritance clarification

- 补充 Pipeline 继承语义：每个 Epoch 的元学习 Taste 会直接注入本 Epoch 所有普通 Fold Prompt；策略和模型产物按 Fold 顺序链式继承上一 Fold 冻结结果。
- 同步更新 `docs/pipeline_design.md`、元学习 Prompt 和导出的 `configs/prompts/PROMPTS.md`；`git diff --check` OK；导出缓存已清理。

2026-06-26 Meta Learning trace detail run

- 启动真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_trace_detail_20260626_115832`，`run_id=run_027521b81c60`，DeepSeek V4 Pro，reasoning_effort=max，季度 WF，历史窗口 21 个月，分钟线 21 个交易日。
- Agent 会话正常 `done`，`finish_status=meta_learning_done`，写出 `workspace/taste.md`；Trace 记录 18 次 LLM、60 次 shell、3 次 explore、6 次 web_search、0 次 compact。
- 外层 artifact 收集阶段因 workspace `.cache/pip` 权限问题报错；Trace/Taste/manifest/data_summary 已落盘并可审计。
- 已覆盖生成 `check.md`，按对话格式整理 Agent 输出、工具调用返回和最终 Taste 全文。

2026-06-26 Meta Taste prompt cleanup

- 清理元学习 Taste 合同中的具体时间/Fold 示例，把“时间窗口无关”和“允许有机制的失败方向继续探索”合并为一条可迁移规则。
- 同步泛化 runner 注释并重新导出 `configs/prompts/PROMPTS.md`；`py_compile` 和 `git diff --check` OK，生成缓存已清理。
- 将元学习 Taste 合同中两条写作边界从“内容应覆盖”列表移出，改为列表后的两个“注意”段落，避免 Agent 误以为需要把这些约束原样写入 Taste；重新导出 Prompt，`py_compile` 和 `git diff --check` OK。

2026-06-26 Meta Learning rerun with prompt-only Taste constraints

- 先运行 `meta_learning_rerun_20260626_151833`：Agent 成功 `meta_learning_done`，但 Taste 仍写入具体决策日期/年份窗口，暴露出 done 前校验只拦截季度/Fold/held-out 标签、不拦截具体日期/年份。
- 修复策略调整：去掉 Taste 内容型硬 Guard，不再用正则拦截季度/Fold/held-out 或具体日期/年份；Prompt 明确禁止这些不可迁移内容，并要求 Agent 在 `done` 前自行检查改写。Runner 只检查 `taste.md` 存在且非空。
- 重跑有效结果：`meta_learning_rerun_strict_20260626_153215`，run_id=`run_c1b20ae82ed1`，Docker meta-learning-only，CPU-only sandbox，DeepSeek V4 Pro max effort，季度周期、21 个月窗口、21 个交易日分钟线。结果 `finish_status=meta_learning_done`，status=`taste_only`，Taste 2232 chars，无日期/年份/Fold/held-out 命中。
- Trace：10 次 LLM、25 次 shell、1 次 explore、6 次 web_search、0 次 compact、0 error；token total=239466，cache hit ratio=0.5303。日志 `logs/meta_learning_rerun_strict_20260626_153215.log`，Trace `experiments/meta_learning_rerun_strict_20260626_153215/artifacts/run_c1b20ae82ed1/agent_trace.jsonl`。

2026-06-26 Strategy context workspace cleanup

- 回撤上一轮 `modification_check_auto_run` backtest summary 字段及相关测试/文档表述，保留原有自动复核/补跑修改检查行为。
- 正式决策入口不再传入 `context["workspace_dir"]` 或 `MQ_WORKSPACE_DIR`；保留 `context["model_dir"]`、`MQ_MODEL_DIR` 和决策期 `mq_tools.nl()`。
- 同步更新 Fold Prompt、模板 README/main.py、agent/environment/pipeline docs，并新增测试覆盖 workspace 路径不泄漏到 `run_strategy(context)`。
- 验证：受影响 3 tests OK；`tests.unit.test_tools_flow` 67 tests OK；`git diff --check` OK；无 `__pycache__` 残留。
- GPT-5.5 High SubAgent 只读审计确认当前运行协议可接受；按建议修正 `docs/agent_design.md` 一处 workspace 决策期措辞，并用 `assertNotIn("modification_check_auto_run", summary)` 加固回归测试。复验受影响 3 tests OK、`git diff --check` OK、无 `__pycache__`。

2026-06-26 Meta Learning prompt structure cleanup

- 元学习 Prompt 的 `Pipeline流程` 增加说明：当前可见数据只是第一个 Fold 的示例窗口，用于理解结构和形成可迁移 Taste，后续 Fold 会沿时间递进并使用各自窗口。
- 将“首轮空历史”合并进 `Pipeline流程`，删除独立小节；将模板文件名边界、Taste 时间窗口无关、自检改写和失败方向取舍压缩进 `禁止事项`。
- 重新导出 `configs/prompts/PROMPTS.md`；`py_compile` OK；`git diff --check` OK；清理导出/编译产生的 `__pycache__`。
- 按反馈将“当前方案不好但有机制仍可继续探索”的正向指导移回 `Taste 输出合同`，`禁止事项` 只保留不得鼓励无机制重复失败方向；重新导出 Prompt，`git diff --check` OK，无 `__pycache__`。
- 将元学习 Prompt 的 `Pipeline流程` 从大段改为分条列表，突出 Epoch/Fold 顺序、示例窗口、Taste 注入、产物继承、首轮空历史和 Taste 质量要求；重新导出 Prompt，`git diff --check` OK，无 `__pycache__`。
- 删除 `禁止事项` 中“不得鼓励重复已失败、依赖个股/月度/时间窗口记忆或缺少可验证机制的方向。”；重新导出 Prompt。
- 重启真实 Docker meta-learning-only Fold：`experiment_id=meta_learning_prompt_cleanup_20260626_180640`，run_id=`run_77940b553de6`，DeepSeek V4 Pro max，季度、21 个月窗口、21 个交易日分钟线，Web Search engines=`tavily, semantic_scholar`。结果 `finish_status=meta_learning_done`，status=`taste_only`，Taste 3368 chars；trace 15 LLM、24 shell、9 web_search、0 compact，token total=426371。日志 `logs/meta_learning_prompt_cleanup_20260626_180640.log`。
- 审计备注：流程成功，但 Taste 仍写入 `Fold 1`、`Q4 2021`、`2020` 等时间/Fold 专属内容，违反当前 Prompt 的可迁移写作边界；系统无硬拦截。`git diff --check` OK，无 `__pycache__`。
- 修正元学习 Prompt：把样本窗口说明并入 `角色与目标`，从 `Pipeline流程` 删除；将 Taste 输出合同固定为三个章节（投资理念与机制假设、重点技术与资源使用建议、历史经验/失败教训/正则化原则），减少日期/Fold 诱导表达；重新导出 `configs/prompts/PROMPTS.md`，`git diff --check` OK。

2026-06-26 Compact and Explore prompt refinement

- 按 OpenCode/Claude Code 可借鉴点做轻量实现：context compact 改为 anchored continuation state，复用上一次 compact summary 作为锚点，只合并新增消息；Explore SubAgent prompt 明确只做只读调查和证据摘要，不替主 Agent 做最终策略综合。
- 文档同步 `docs/agent_design.md` 与 `docs/environment_design.md`；补 compact anchor 和 Explore 边界回归测试。
- 验证：compact/explore 相关 13 tests OK；`py_compile` OK；`git diff --check` OK；验证生成的 `__pycache__` 已清理。

2026-06-26 Tool schema detail sinking

- 保留系统 Prompt 工具表，同时把高频工具的参数语义下沉到 `ActionField.description` 和 provider 原生 tool schema：shell/search/artifact_io/web_search/explore/note，以及 backtest/modification_check/finish_fold/done 的工具描述。
- `ToolError` 的结构化错误字段保持为工具失败提示层；文档同步说明系统 Prompt 只保留工具导航和关键边界。
- 验证：工具 schema 相关 3 tests OK；`py_compile` OK；`git diff --check` OK；验证生成的 `__pycache__` 已清理。

2026-06-26 Meta sandbox image rebuild and manifest cleanup

- 按“两层”设计实现元学习依赖继承：元学习可写 `workspace/sandbox_environment.json` 声明稳定 Python/npm/apt 依赖，Pipeline 基于当前普通 Fold Sandbox image 构建派生镜像；成功后后续 Fold 与 held-out 使用新 image，失败则实验显式失败。
- 移除此前复杂的 `models/python_packages` 继承方案，`models/` 只保存模型参数/权重/元数据；依赖属于 Sandbox 镜像层，临时下载和缓存不进入正式产物。
- 保留 public run manifest + host-only manifest 双视图：Agent 只看训练/验证相关 allowlist 字段，宿主审计保留完整调度与测试信息；Agent 可见 development 账本同步改为 allowlist，并把 Fold/策略 ID 改为 opaque ref，去掉 Sandbox 内不可读的 host 路径引用。
- 同步 `agent_design`、`environment_design`、`pipeline_design`、导出 `configs/prompts/PROMPTS.md`，并用完整实例 Prompt 覆盖 `check.md`。
- GPT-5.5 High SubAgent 审计结论：manifest 双视图不算冗余，public 用于 Agent 边界、host 用于审计；依赖继承应通过 Sandbox image，而不是复制 site-packages。按审计修复 Agent 可见账本的间接时间标签和 host 路径残留。
- 验证：完整 `unittest discover -s tests` 290 tests OK；Pipeline/StepTree/artifact/tool targeted tests OK；`git diff --check` OK；测试生成的 `__pycache__` 已清理；资源复查约 414 GiB available memory，GPU 占用为既有任务。

2026-06-26 Prompt export readability cleanup

- `configs/prompts/PROMPTS.md` 原先把多个超长 Prompt 平铺为巨大代码块，审计阅读体验较乱；已调整 `scripts/dev/export_prompts.py`，生成导航、编号章节和可折叠完整 Prompt 块，仍保留模型实际接收文本的原样代码块。
- 重新导出 `configs/prompts/PROMPTS.md`；`py_compile scripts/dev/export_prompts.py` OK；`git diff --check` OK。
- 按 SubAgent 审计修复第 7 节：不再把实验级探索方向作为独立“追加片段”展示，而是导出“含实验级探索方向示例”的完整元学习 System Prompt；外层代码围栏改为四反引号，避免 Prompt 内部 ```json 提前闭合 Markdown。同步把 `/mnt/agent/workspace/sandbox_environment.json` 加入元学习 Prompt 的可读写文件表。
