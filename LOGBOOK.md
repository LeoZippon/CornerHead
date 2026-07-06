2026-07-06 第四轮全面审计：文档 + 代码 + 实盘保真（feat/qmt-credit-broker）

- 六个并行只读子代理审计（文档交叉逻辑/去重/可读性、broker/引擎、Agent 运行时与工具、数据层、pipeline、两融/行业保真——细则与官方 QMT 文档原文核验）；High 与关键 Medium 项全部人工在源码复核后采信。env_design §1–§2 按用户指示视为已审计跳过。
- 实盘保真 High×2：①代码允许融券**当日**还券（`sellable_quantity` 对空头不设 T+1 锁），细则 2.13 自 2015-08 起为 T+1，且 env_design §3.2 写的是正确规则——码与文档相反；②多头公司行为完全缺失：raw 价回放下除权日跌幅记为纯亏损，broker/engine 四个模块 grep 无任何 dividend/adj_factor 消费（空头侧已文档化 disabled，多头侧无声）。
- 代码 High×2：①引擎 `_int_or_none` 静默 floor 小数股数、NaN/垃圾→None→reduce 动词按"全仓卖出"执行，违反 §3.2"拒单不取整"契约；②`run_fold` 吞掉 `_frozen_test_eval` 全部异常（含 artifact 篡改 RuntimeError），`state_changed_during_test` 硬编码 False，与 held-out 路径的 fail-fast 不一致。
- 两融 Medium：细则 2.14（卖出融资标的所得应先偿融资欠款）未建模；负债合约无 6 个月期限；强平所得不偿融资、负债继续计息；保证金比例下限静态（对 2026-07 实盘正确，2023-09~2026-01 历史回放偏紧，属保守）；利息 /365 vs 券商惯例 /360；科创板 200 股+1 股递增未建模（现按 100 整数倍误拒/误受）；无部分成交与流动性约束。核对为**正确**的：维保比例公式、300% 提取线（含现金+证券分子口径）、130/140 券商约定线定位、保证金可用余额（浮亏 100% 折算）、融券限价+uptick、冻结所得、卖券还款先息后本 FIFO、自然日计息、印花税 2023-08-28 切换、opType/prType/1101/1102/m_* 字段逐一与官方文档一致。
- 其余 Medium 簇：uptick 拒单 Order 缺 `account="credit"`（信用 ORDER 查询/统计漏计）；`pending()` submit-lag 记录缺 account/op_type 字段（与文档映射表不符）；警戒线 1.40 文档称"仅审计记录"但无任何记录代码；token 估算 chars/3 对中文低估约 3×（compact 可能晚触发）；确定性 trim token 触发时可能一字不删却每轮重写 summary（打散前缀缓存）；`"./README.md"` 绕过 output/README 只读守卫；数据层：stk_limit/suspend_d 当日行结构上进不了 daily.parquet 但 manifest 声称可见、shibor_quote 掉出夜间刷新且 audit 测不出年内陈旧、share_float `source_file` 宿主绝对路径泄入 agent 可见 events.parquet、`_daily_join` 对 adj_factor 缺重复键断言、`_names_as_of`/`_industry_membership` 缺文件静默返回空（违反 fail-fast）。
- 文档面：parameters_reference **全部默认值与代码核验一致**，所有文件/CLI/函数引用有效；主要债务为 agent_design §3.2 近拷贝 env §3.2–3.4 语义（应缩为签名+指针）、三处跨文档矛盾（盘外禁报单规则未给 transfer 留 09:14 前豁免；验收清单 `available_at <= decision_time` 措辞早于 Timeview 逐 tick 模型；QMT §2.2 执行器草图 1101-only 未提 op32 用 1102 金额口径）、prompts.py 残留已删除的"日 Fold"周期、PROMPTS.md margin 可见时间写节点启动时刻（应为就绪 ~09:07/09:17）。
- 版本标签清除（本次唯一代码改动）：`scripted-v0`→`scripted`（llm/proxy.py）、`compact-v0`→`compact-model`（test_tools_flow）、删测试注释"V1:"标签（test_pipeline_e2e）、暂存测试载荷 "v1"/"v2"→"seed"/"update"（test_main_ctx_replay）；living docs 与 src 本已干净（profile_id 已是 gjzq_dual）；logbook 历史条目中的 V1/V2 为审计发现编号，有意不改。三个被改测试文件 174 测试全过。
- 结论：QMT 接口层与两融数学核心保真度高；缺陷集中在融券 T+1、多头公司行为、引擎输入强转、frozen-test 篡改检测四个 High 及上述可小步修复的 Medium。修复未实施，待排期为独立工作项。

2026-07-06 双账户拆分：普通现金账户 + 信用两融账户（feat/qmt-credit-broker）

- 按用户要求把 SimBroker 与 QMT 实盘环境从"单账户（account_type 选型）"改为**固定双账户**：`stock` 普通账户（long-only 现金，opType 23/24）+ `credit` 信用账户（担保品买卖 33/34 + 融资 27 + 融券 28/29/31/32），如同真实投资者在同一券商的两户。现金、持仓、T+1 各自独立、互不担保；opType 自身决定账户归属，`passorder` 无需账户选择器；`get_trade_detail_data` 的 `account_type` 变为必填（STOCK/CREDIT）。
- SimBroker 内部重构为 `AccountState`（name/cash/initial_equity/positions/contracts）×2：维保比例/保证金可用余额/利息/强平**只计信用账户资产**（普通账户不作担保、强平只清信用户）；组合权益 = 两账户之和；`max_total_holdings` 按跨账户去重代码数、单票权重按跨账户合并名义执行；同一票允许普通做多 + 信用融券做空（账户内仍单票单侧，opType 30 维持不支持）。`weight` 改为按下单目标账户初始资金（`stock_initial_cash`/`credit_initial_cash` 各默认 500k，替代 `initial_cash`+`account_type`；profile_id → `gjzq_dual_v1`）。
- 新增 `transfer(amount, from, to)` 账户间现金划转（银证转账式、提交 tick 即时结算、substep 延迟语义一致）：融券冻结所得不可划出；信用账户有负债时划出后维保比例必须 ≥ 提取线 3.00——`maintenance_withdraw_ratio` 从"仅审计记录"变为实际执行的约束。实盘侧 transfer 不在策略 API 内，payload 中的划转指令只生成人工银证转账工单。
- Agent 面：`ctx.broker` 新增 `credit_buy`/`credit_sell`/`transfer`/`stock` 视图；`buy`/`sell` 语义改为普通账户；顶层 `cash`/`available_cash` 移除（改 `ctx.broker.stock["..."]`/`credit["..."]`，无兼容别名）；`close(code, account=None)` 双账户同持时 driver 端抛错要求显式 `account=`（引擎按提交 tick 唯一持有账户解析）；`position(code, account=None)` 缺省跨账户净额；`ctx.account` 变为 `{stock, credit, total_assets, risk_limits}`，持仓行带 `account`。修复顺带发现的 `_limit_fill_price` 动作集缺口（credit_buy/fin_buy 限价单曾走卖方分支）。
- 实盘（QMT_documentation）：执行器按 op_type 在 `CQ_STOCK_ACCOUNT_ID`/`CQ_CREDIT_ACCOUNT_ID` 间路由（两者必填）；§2.1/§2.2/§6.3/§9 同步。五份 living docs + parameters_reference + 提示词动作表/facts（`stock_initial_cash`/`credit_initial_cash`/`maintenance_withdraw_ratio`）+ 模板同步，PROMPTS.md 重导出（幂等）。
- 验证：full suite 472 OK（468→472：新增独立现金池/跨账户对冲/划转提取线/维保不计普通账户/双账户回放解析等测试，删单账户拒绝类测试）；`git diff --check` clean；沙箱镜像重建（driver 变更）+ Docker e2e 复验。

2026-07-05 文档格式标准化 + 新增参数速查文档

- 以用户已审计的 environment_design §1–§2 为格式与信息密度基准，核对其余全部文档：env §3–§4、agent、pipeline、data 四份基本已合规（五轮收敛的结果），仅修掉信用重构遗留的过期表述（agent ctx 注释 `FIX_PRICE`→指定价+short 需 limit、"借券费"→信用利息；env §3.3 与 data 官方索引的中信来源→SSE 细则解读+国金页；data §3.3 margin_secs 节点描述补融资资格）。
- QMT_documentation 结构对齐参考格式：`## 术语说明`/`## 导航` 降为与其他四份一致的加粗块；新增其余文档都有的 **职责边界** 块（本机/执行器分工表）；删除 8 处与小节标题重复的加粗标签；§2.2 分工改表格、开放问题改编号列表；术语表补"执行器/文件桥 QMTBroker"；§6.2 官方参考由 XtQuant/XtTrader 链接改为客户端内置 Python API 文档（仓库副本 + 迅投文档），清除 `qmt_executor.py`/XtMiniQmt 残留指向。
- 新增 `docs/parameters_reference.md`（派生速查，非第六份 living 设计文档）：汇总五份文档引用的全部参数/超参数，默认值逐项对照代码核验——快照窗口、实验编排/验收/修改约束、回放执行与预算、Broker profile（含信用参数）、Agent 会话与上下文管理、Sandbox 资源与工具预算、数据层任务参数（限频/分页/哨兵/刷新节点）、报告与常量；五份文档"相关边界"各加一行指向。
- 验证：`git diff --check` clean；纯 .md 改动，不涉代码与 prompt；PROMPTS.md 无变化。

2026-07-05 QMT 官方 API 对齐重构：股票/信用账户分离（feat/qmt-credit-broker）

- 依官方全功能 QMT 客户端内 Python 策略 API（`external_references/gjzq-da-qmt`，12k 行接口文档逐节提取）重构 Broker 边界：`TraderProtocol` 由 xtquant 6 方法改为 `passorder`（官方 opType 码）/`cancel`/`get_trade_detail_data`(ACCOUNT/POSITION/ORDER/DEAL)/信用查询（`get_debt_contract`/`get_assure_contract`/`get_enable_short_contract`）；旧 `order_stock`/`query_stock_*` 全部移除（无兼容 shim），m_* 字段映射表进 env docs §3.2。
- 账户分离：`broker_profile.account_type ∈ {stock, credit}`（默认 credit）。信用账户全量落地：融资买入 27（开 `DebtContract` 负债合约，本金+佣金计息、开仓不动现金）、融券卖出 28、买券还券 29、卖券还款 31（净所得先息后本 FIFO 还融资）、直接还款 32（现金即时结算、官方 1102 金额口径）、担保品买卖 33/34；30 直接还券有意不支持（单票单侧持仓下结构性不可达，docs 注明）。普通账户仅 23/24，信用原语 driver 层抛错。
- 信用会计按交易所实施细则精确实现（broker_core 纯函数 + 引用 SSE 解读 PDF）：维持担保比例 =(现金+证券市值)/(融资+融券市值+利息)，跌破 1.30 强平（融资负债不因清仓消失、继续计息，权益已净额）；保证金可用余额 = 现金+担保品×折算率+浮盈浮亏项（亏侧 100%）−占用−利息，门控新融资/融券；利息按自然日计入合约、偿还时付现（替代旧的逐日现金扣借券费）；融券卖出所得冻结口径不变。新增：融券必须限价 + uptick 申报规则（低于激活 bar 参考价拒 `slo_sell_uptick_rule`）、融资标的门控（margin_secs 同集合、逐成交日）、授信额度 knobs。available_cash 改为"现金−融券冻结所得"（保证金占用不再冻结现金——更贴近真实信用账户）。
- Agent 面：`ctx.broker` 新增 `fin_buy`/`sell_repay`/`direct_repay`/`credit`/`debt_contracts()`；buy/sell/short/cover/close/cancel 语义不变。Fold 提示词动作表+信用经济学段、facts `broker_replay`（account_type/双保证金比例/利率/折算率/额度）、模板 README、PROMPTS.md 重导出（幂等）。
- 迁移架构（objective 2，QMT_documentation §2.2 重写）：**用户定案——实盘执行全走客户端内置 Python API（ContextInfo/passorder/get_trade_detail_data），xtquant/miniQMT 弃用**。落定架构 = 用户所提"远端常驻脚本轮询本地订单库"方案（经官方文档核验可行且为该运行时正确形态）：决策侧 `main(ctx)` 跑在自有 Python（现代依赖，不能进客户端内置 3.6.8），`QMTBroker` 实现为文件桥（passorder/cancel→inbox 订单文件；get_trade_detail_data→读回写快照）；执行侧客户端内常驻策略（标准库-only、零网络）`run_time` 定时轮询 inbox + `passorder(quickTrade=2)` + 投资备注幂等去重 + 回调回写 + 慢定时器权威快照（官方运行时单线程 → 禁阻塞/watchdog/HTTP）。零售 QMT 无原生文件单模块（证伪该备选）；7 项待真机验证的开放问题记录在案。§4.3/§6.3/§8 同步去 xtquant 化。
- 验证：full suite 468 OK（449→468，broker 测试重写 + 信用新测试）；`git diff --check` clean；PROMPTS.md 幂等；沙箱镜像重建（driver 变更；顺带修 .dockerignore 漏排 archive/ 44G 致构建上下文 46.8GB 的问题）+ Docker e2e 复验。五份 living docs 同步。

2026-07-02 GPT 四文档结构合并的核验与修复

- GPT 在外部把 data/env/agent/pipeline 四份 living docs 的章节大幅合并重编号（如 env 9 章→4 章；术语表降级为加粗块、导航改为紧凑 TOC），并同步改了 13 个源文件的文档引用与 PROMPTS.md。按用户要求核验三问：结构是否合理、是否引发 doc↔码不一致、是否遗漏实现细节。
- 结构结论：**接受**。四位 Opus 审阅代理（每文档一位，逐条走查全部删除行 + 对源码抽验改写句）一致判定合并方案连贯：数据/管道/agent 三份为纯结构性改动（正文逐字保留、零漂移）；env 有若干处收敛到姊妹文档权威节（正确去重），且 Shell guard 移入 §2.2、可信日志移入 §4、LLM 边界并入 §2.4 属改进。改写句对码抽验（env 8 条、pipeline 14 条、agent 10 条）全部匹配，**零语义漂移**。
- 已修复的问题：①GPT 自己的引用重编号内部错乱——12 处源码 docstring 指向其新结构中不存在的节（env §1.8/§2.5/§2.6/§2.8/§2.9/§3.5/§3.8、#35 锚点、broker queries 误指 §3.3、pipeline 8.4/10.1、units 的 data §2.1、runner 会话合同指向），全部重映射并经自动检查器复核为 0 问题；②env TOC 一处锚点笔误（#24-nllm→#24-nlllm）；③data 文档"见第 6 章"残留（→第 4 章）；④ledger.py 双重过期的 "chapter 7" docstring（→§4.1）。
- 已回补的少量真实遗漏：pipeline 元学习可见数据 bullet 恢复显式挂载点与 test/held-out 排除声明；env §1.3 恢复紧凑"PIT 支撑机制"块（fundamental_events 行级 available_at=公告日 18:00【fundamental_events.py:361 核验】、build_pit_events.py 构建入口、status 文件 fail-fast 门禁指向 data §3.1、缺 available_at 列必须报错【snapshot.py:533 核验】、manifest 记录 build/data_profile）；env §1.4 补 units.py 实现指针；agent 提交自检补死代码条款（与 prompt 对齐）；data 哨兵段补 revision_monitor.sentinel_* 单源说明。
- 验证：引用/锚点检查器 0 问题；full suite 422 OK；PROMPTS.md 重导出与 GPT 版本字节一致（幂等）；`git diff --check` clean。

2026-07-01 轻量冗余清扫 + docs 四文档精修

- 两个 Opus 只读扫描（src / scripts+configs+ops+tests）+ 本人对每个符号全仓 grep 复核后落地：
  - 死代码约 45 行：`pit.py` 弃用的按分区可见性簇（`assert_visible`/`latest_visible_trade_date`/`iter_visible_trade_dates`/`_normalize_decision_time`；现行 PIT 模型 = contracts 刷新节点 + `available_at` 列过滤）；`contracts.py` 死 `tradable_from` + 只写字段 `tradable_lag_days`（含 6 处构造 kwargs）；孤儿常量 `MAJOR_NEWS_SOURCES`/`DEFAULT_WRITABLE_FILES`/`TERMINAL_STATES`。
  - scripts 残留重复：`_cli.py` 新增 `build_pipeline` + `resolve_meta_learning_directive`，两入口各删约 17 行字节相同的 provider/pipeline 接线与 directive 解析。
  - 配置单源：`cn_daily_revision_sentinel.extra_args` 硬编码的 `--sample-size/--datasets` 删除，`revision_monitor.sentinel_*` 成为唯一生效来源（cron_update 回退分支生成完全相同命令；仅下次 cron 一次非 skip 重跑）；Dockerfile 无效 `USER root` 改为语义注释（镜像行为不变，无需重建）。
  - 有意保留：`DatasetContract.partition_key/pit_notes/unit_rules`（内联 PIT 注释）、schedule JSON `interfaces` 数组（人读权限参考清单）、两处 2 行 CN_TZ 归一化（低于抽 helper 阈值）。
- docs 精修（data/env/agent/pipeline，Opus 单代理跨文档统一裁决）：9 处保守去重——compact 阈值/锚点收敛至 env §4.3、三视角检索收敛至 pipeline §6.2、diff 基准信任规则收敛至 env §5.2、agent §5.3 substep 预算复述删、data §3.3/§5.2/§5.3 复述合并、env §7.2 竞价自引用段删；标题与编号零变化、交叉引用全部核验可达、无事实丢失（每条被删内容在权威节逐字保留）。pipeline_design 无可删。结论：四份文档经五轮迭代已收敛，本轮净减约 500 字符，再无可安全去除的冗余。
- 验证：full suite 422 OK；`git diff --check` clean；两个实验 CLI `--help` 正常；PROMPTS.md 无变化（本轮不涉 prompt）。

2026-07-01 GPT 复审三项跟进（经核验均属实，已修）

- finish_fold 硬门槛：原只查修改检查+轻合同，Agent 只跑过 `replay_window` 调试回放（甚至零回测）也能结束 Fold，整轮静默回退父产物。现与 Pipeline 冻结同口径把关：当前 `output`/`models` hash 必须已有成功**完整验证**回测，否则 ToolError 可修复拒绝（runner 仅在成功时视为终止，会话可继续修复）；提交合同、工具表、wrap-up 提示词同步（恢复已完整验证的 Step 可免重跑），backtest 工具描述澄清 replay_window 仅调试、不满足冻结/finish_fold，PROMPTS.md 重导出。
- 审计 CLI 周期护栏对齐：`run_audit_session` 原硬编码 2022Q1/2025Q4 默认值，`--fold-period month/week/year` 时会静默流入排期（pandas 可把 "2022Q1" 误解析为日期）。新增共享 `require_generic_period_args`（`_cli.py`），两入口非 quarter 缺周期参数即 `parser.error`；quarter 默认值只在 quarter 下填充。新增审计入口子进程回归测试。
- 快照缓存文案纠偏（实现不动）：回放槽缓存键含 label（label 内嵌视图 manifest），同区间 valid/test 槽不跨 label 复用；真正跨 Fold 复用的是昂贵的决策快照（Fold N+1 验证锚点 == Fold N 测试锚点）。config.py/experiment.py 注释、pipeline_design §8.2 与下条日志已改准；跨 label 复用需改写内嵌 manifest/snapshot_id 别名，低收益不做。
- 验证：full suite 422 OK（420→422）；`git diff --check` clean；PROMPTS.md 同步。

2026-07-01 三轮 fresh-eyes 审计 + 15 项整改落地（7 个 Opus 子代理）

- 审计：5 维并行（docs↔码、执行核、Agent 层/沙箱、pipelines/scripts/ops、CPCV 优化评估）+ 最近两轮 Fold 取证复盘（`regular_fold_last_taste_gpu`/`cancel_prompt_audit_fold_day`，四方面均给证据结论）。全部 HIGH/CRITICAL 指控本人逐条核验；1 条证伪：data_summary date_ranges 并非“未门控源文件泄漏”，ranges 来自 PIT 门控快照视图（available_at 封顶正确），未来 ann_date/end_date 是合法前瞻披露字段。
- HIGH 修复：① `parent_strategy_artifact_id`（=`strategy_<epoch>_fold_<period>`）在 agent 可见 run_manifest 白名单与系统提示词 facts 两处未去敏（ledger 视图早已去敏）——统一 `agent_visible_ref(prefix="strategy_ref")` + 泄漏测试（公开 manifest/渲染 facts 无 `fold_<label>` 子串）；② 折叠排期新增 ≥2 交易日守卫（valid/test/held-out 全覆盖，引擎末日强制清仓需两天）并删除结构性不可回测的 day 周期——cancel_prompt_audit 曾为此整轮报废（沙箱+LLM+冻结评估全空转）。
- 其余整改：`no_update_timeout` 拆分 `no_valid_backtest`/`no_update`；`run()` 入口拒绝重跑已冻结实验（原在 `_freeze` 深处 FileExistsError 绕过 always-append ledger）；meta 原始 trace 记忆限最近 `meta_memory_max_epochs=3` 轮（原 O(epochs²) 级联）；新增 `CachingSnapshotProvider` 实验内容寻址缓存（相邻 Fold 共享决策快照锚点、epoch 不变量，命中硬链接进沙箱；回放槽按 label 各建一次）；报告“主动收益”统一权益比值口径 ∏(1+r)/∏(1+b)−1（表/摘要/图一致）并新增 `std_test_return`/`std_active_return`/`active_return_tstat`；shell 守卫报错回映射 /mnt 命名空间（不再漏宿主路径）；modification_check 有父模型必须给 manifest hash（与策略侧对称 fail-fast）；broker_core/.dockerignore/broker 过期“烤入沙箱、投影一致”文案改正 + 空头死 T+1 记账清理 + `holdings_count`→`full_close_count`；run_experiment/run_audit_session ~130 行重复 CLI 收敛至 `scripts/experiments/_cli.py`（--help 字节一致）；Fold 提示词补两条（NL 证据要权衡而非遗漏；禁装饰性死代码，放弃方向须删残留并说明）；五份 living docs 同步本轮行为变化 + 可读性梳理（保完整度）。
- CPCV 结论（用户定：本轮不实施、后续迭代引入）：不把 agent-in-the-loop Fold 改 CPCV（破坏 walk-forward 因果与 Taste 链、session 成本组合爆炸）；推荐对冻结策略做 CPCV 式多路径重评估层（纯算力零 token，~16 产物 × 8 块三角 OOS 矩阵 → PBO/DSR 进报告）。
- 验证：full suite 420 OK（406→420，+14 测试）；`git diff --check` clean；PROMPTS.md 重导出幂等；CPU-only，RAM ~395Gi available。提交：b0a25e0（pipeline 加固）/ bcedab9（去敏+提示词）/ 40e3bf1（报告）/ de591cd（工具守卫）/ a92fde2（broker 文档+死码）/ 05ee5fe（CLI 提取）+ docs、logbook 提交。

2026-07-01 二轮 fresh-eyes 全量审计 + 11 项整改落地并复核

- 审计：7 个 Opus 子代理并行（agent/prompt、执行核、broker、tools/snapshot/NL、pipelines、data+docs、两轮单 Fold 复盘）+ 本人逐条核验。结论：核心撮合/PIT/隔离无高危缺陷——broker 撮合与前一交易日收盘锚点、Timeview 两层 PIT、沙箱隔离、meta finalize 顺序、fail-fast 列表、config 默认值均与文档一致，PROMPTS.md 与 prompts.py 字节一致。
- 已修复（含用户 11 项）：
  - M1 PIT 泄漏：step tree 节点名把 `fold_<period>`（=held-out 季度）透给 Agent。新增 `environment/identity.py` 单一 `agent_visible_ref`（去重 runtime/experiment/prompts 三处 sha256 副本），backtest 记录 step 时 opaque fold_id；data_summary 早前已 opaque，`host_run_manifest` 保留明文。
  - M2 复现性：`enforce_substep_coverage` 改为随 `mode=="valid"` 分档，frozen/held-out 不再因负载抖动的墙钟覆盖检查误杀已接受策略。
  - M3 死代码/过度设计：substep 延迟提交改造后 driver 的 tick 内成交投影全不可达——删 `_project_open/_reduce`、`_cost_model`、`_order/close/cancel` 的 `_cur_substep` 死分支（约 90 行）及 driver 对 `broker_core` 的依赖；镜像不再烤入 `broker_core`（Dockerfile/executor/env docs 同步），driver 变纯标准库；顺带修好同 tick cancel 不进 `pending()`（B4）与 `available_cash` 过时 docstring。`broker_core.py` 仍供宿主 SimBroker。
  - M4 误导开关：删无效 `--allow-incomplete-validation`（冻结候选池本就只取完整验证），两 CLI 恒 `require_complete_validation=True`。
  - item5：确认无需为单次回测新增全局墙钟总上限（现状=探索 deadline + 回测按天上限两套独立计时），未新增。
  - L1–L10 + 极低项：helper SyntaxError→ArtifactError；`auction_close_time`/final-eval 上限内联默认；删死常量 `SNAPSHOT_FILES`；timeview docstring 六→五域；报告 y 轴 Fold return；data doc 补 `cron_update.py`；units 交叉引用改 §2.4；`initial_template_hash` 硬校验；agent_design §5.3 只 buy/short 带 weight；env §7.2 09:25 竞价标注更正；两 doc 工具表补 `note`；audit-session 派生镜像说明澄清；嵌套 substep 覆盖不重复计时；shell heredoc 二次剥离确认为防御性保留；rolling_asof/quarter 兼容别名作为 resume 兼容保留。
  - item8/9 fail-fast + prompt：Fold/meta prompt + 模板禁止 `except: pass` 静默兜底；新增“固定日内时间表”（贴近真人交易日常：`ctx.cur_time` 门控 08:00 研究→09:15/09:25 下单→14:57 收尾），模板 `candidate.research()` 加固定盘前时点门控；agent_design §5.2 收敛为要点并指向 env §7.2、去四处重复；现金视图措辞去“投影”。
- 复核（item11）：Opus 子代理二次审计确认 11 项全部 RESOLVED，仅 3 处遗漏/风格（timeview 六→五 docstring、money/cash “投影”措辞、runtime/experiment mid-file import）已一并修好。
- 两轮单 Fold 复盘（`regular_fold_last_taste_gpu` / `substep_gnn_fold`）：Agent 输入合理、轨迹合规、环境交互正确、策略 PIT 安全但收益为负且 GNN 过拟合回落简单因子；暴露 fold deadline 不覆盖完整回放墙钟、`pids_limit` 触顶、NL 全程未用（后续单独治理）。
- 验证：full `unittest discover -t . -s tests` 406 OK；`git diff --check` clean；PROMPTS.md 重新导出且 sha256 幂等；`autotrade-sandbox:latest` 重建（driver 纯标准库、镜像不含 broker_core、容器内 import OK）。CPU-only；内存约 401Gi available，GPU 为既有任务占用。

2026-06-30 审计跟进：盘外 tick、工具合同与 audit 入口对齐

- 用户复核后定案：`margin_secs` 缺失回退保持不动，`use_docker=False` 仅本地开发风险不处理；Fold 冻结“最近完整 valid”通过 Prompt/docs 要求 Agent 结束前恢复自己认为最好的已验证 Step。
- 盘外 tick 问题确认真实存在：盘前 off-session tick 原会把订单排到首根真实 bar。修为所有 off-session tick 仅研究/状态、不成交；显式 09:15/09:25/14:57 竞价 tick 语义不变；新增 06:00 回归测试。
- `run_audit_session.py` 补齐正式入口已有的 per-domain snapshot window 参数（daily/fundamentals/events/macro/text）并传入 `SnapshotConfig`；不做脚本架构重抽象。
- living docs + Fold Prompt 改为实际 function action 名（`shell`/`web_search`/`modification_check`/`backtest`/`finish_fold`），修正 `backtest(mode="valid")` 旧写法、`ctx.asof_dir` 五个 parquet 域 + `ctx.nl()` 文本滚动说明；PROMPTS.md 重新导出。
- 追加 Prompt/模板优化：明确普通 off-session tick 不调用 `ctx.broker`/`order_stock`，盘前下单走 `ctx.state_dir` 计划交接后在 09:15/09:25 显式盘前 tick 提交；修正“任意时点下单”和逐 tick 热路径禁用 `model_dir` 的歧义。
- 元学习 Prompt 结构微调：`当前实验事实（可信运行事实，不是交易证据）` 改为插入 `# 环境与配置` 内、`# 动作与流程` 前，与 Fold Agent 系统提示词一致；新增位置回归测试。
- 简单清理 `tushare_update_schedule.json` 中未被代码消费的 `recent_force_refresh_datasets` / `dataset_policies`，保留真实生效的 `sentinel_datasets` 和 job `extra_args`。
- 验证：全套 388 OK；`git diff --check` clean；JSON/py_compile/run_audit_session --help/旧工具名与旧 Prompt 语义扫描 OK；无 GPU。

2026-06-30 最终综合审计 + 修复（RA5；chore/post-audit-reaudit）

- 三个并行 Opus 子代理按用户要求审计：docs↔源一致性、业务/设计逻辑正确性、冗余/重复/命名。维度1（docs↔码）全绿；维度3（冗余/命名）除两处死 import 外全绿。
- 死 import 清理：`broker.py` 未用 `LOT_SIZE`、`main_ctx_driver.py` 未用 `pandas`（并修文档串）。executor 的 env 4 处重复收敛为 `_base_env`/`_merged_env`（run+popen 共用）。
- 维度2：无任何缺陷会错记宿主 P&L 或前视；修复三处驱动盘中投影忠实度 + 一处竞价滑点：
  * Finding3（关键）：RA2 误把所有 `is_auction` 免滑点，但 09:25 单成于首根连续 bar（09:31）属 taker 连续成交，并非集合竞价；仅 09:15→09:30 开盘、14:57→15:00 收盘是单一价清算。改 09:25 tick `is_auction=False`（计滑点、label `minute:HH:MM`），开/收竞价仍免滑点；env_design §7.2 澄清，3 个 09:25 测试改断言滑点价（09:15/14:57 测试不变）。
  * Finding1：驱动 `_project_reduce` 只按持仓符号分支，导致对做空调 `sell`（或对多头调 `cover`）投影出幻影减仓而宿主按 side_mismatch 拒；改为按 action 门控，新增回归测试。
  * Finding2：驱动同 tick 未消耗 `_sellable`，多次 sell/close 可能投影超卖 T+1 可卖量；改为每次多头减仓递减 `_sellable`。
  * 可接受（已记）：投影仅做资金门控（停牌/涨跌停/可融券/持仓数上限由宿主在真实成交处强制）；借券费属研究假设。
- 驱动改动→重建沙箱镜像（缓存复用）并经 `DockerizedFoldE2ETest` 端到端复验。
- 验证：全套 387 OK；`git diff --check` clean；PROMPTS.md 同步；镜像已重建。

2026-06-30 RA1/RA3/RA4 再审计（chore/post-audit-reaudit）

- RA1（券商账务端到端 R4–R8 交互）确认正确，新增 `test_combined_long_short_accounting_and_forced_close`：多空并存时 `equity`/`maintenance_ratio` 用字面现金（做空所得作担保），`available_cash` 扣保证金+冻结所得；做空价 100→250 击穿 1.30 维持线，`mark_to_market` 强平两腿（R4 日滚解 T+1、R6 收盘价平、R8 借券费）。决策：R8(b) 保持锁“净”所得（available 恰降 margin），费级宽松属有意（净锁保实现盈亏正确）；R5 同日平空已有测试、`locked_today` 对做空设而不读、无不一致。
- RA3（`ctx.state_dir` 进 substep 拷贝种子成本）文档化：env_design §7.2 注明 state_dir 仅适合小体量跨 tick 状态，大数据放 models/；hardlink+CoW 待真成瓶颈再做。
- RA4（24h 网格 Docker 基准 offsession 15 vs 0）不自动跑：真实 fold 走外部 DeepSeek API + GPU，需用户显式触发；冒烟由 `DockerizedFoldE2ETest` 覆盖（去字符串化驱动端到端）。按需基准命令见详细 logbook。RA5 并入最终审计。
- 验证：全套绿；`git diff --check` clean；docs + 一个券商测试，无 GPU。

2026-06-30 R16 T1 盘中券商视图忠实化（refactor/t1-driver-and-broker-core）

- 收尾任务（重建沙箱镜像），两次提交 + Opus 子代理对抗审计（抓到并修复一处真缺陷）。Part1：新增纯 stdlib `broker_core`（CostModel + lot_floor/resolve_shares + project_open/project_reduce），SimBroker 委托之为单一成交真相源（`_fill_long_open/_fill_short_open/_reduce_position` 等），行为不变 + 单测。
- Part2：660 行 `_MAIN_DRIVER` 字符串（含 286 行路径 guard）去字符串化为真实模块 `main_ctx_driver.py`，按文件加载（`executor.runtime_path`→`/opt/at_runtime`），`import broker_core` 同目录解析；驱动内 `_Broker` 改用 broker_core 做盘中投影（佣金/滑点/整手/融券保证金/冻结所得、按 `available_cash` 门控开仓、平仓释放买力、T+1），新增 `ctx.broker.available_cash`；`_market_state` 带 `cost_model`/`entry_cost`/`available_cash`；删除 backtest_engine 里现已死的 `_STRATEGY_PATH_GUARD`。
- 对抗审计：核心数学==原 SimBroker、去字符串化字节级一致、wiring 均 CLEAN；唯一缺陷=做空开仓把 `available_cash` 多扣了 `fee+duty`（实际只锁 `margin`，净所得抵消费用/印花税）→ 修为做空只扣 `margin`（开仓门控仍用 `required_cash`，与 SimBroker 拒单口径一致）；并把同 tick 新开多头的 T+1 可卖默认改 0。新增做空投影测试（旧逻辑会 fail）+ 两笔买入 parity 测试。
- 镜像：`sandbox.Dockerfile` 把两个运行时模块烤入 `/opt/at_runtime`（构建上下文改仓库根 + 新 `.dockerignore`；`chmod 0644` 让 agent 可读）；重建基础镜像（缓存复用 pip/apt 层）并验证容器内 agent 可 import、`DockerizedFoldE2ETest` 端到端通过。
- 验证：全套 385 OK（含 Docker e2e）；`git diff --check` clean；PROMPTS.md 同步；env/agent docs 更新。

2026-06-30 R18 结构去重 T4/T6/T2（refactor/t2-t4-t6-dedup）

- T4 可写根单源：`SandboxPaths` 加 `writable_roots`（元组）/`writable_root_map`（名→路径），shell 写守卫与 `ArtifactIOTool._roots` 改引用之；`WRITE_ROOT_CHOICES = AGENT_TOP_LEVEL`。Python 缓存子集单源：`runtime.RUNTIME_CACHE_DIR_NAMES/SUFFIXES`，`artifacts._is_runtime_cache` 与 `sandbox._COLLECT_IGNORE` 共用（广义 VCS/venv 列表仍只在采集忽略表，按审计不并入窄谓词）。
- T6 派发改 handler map：`runner._dispatch` 的 if/elif 链改为 `_action_handlers`（键与 `action_specs` 一致），每动作一个 `_do_<action>`；删 4 处死 mode 守卫（`spec.validate(mode=)` 的 `allowed_modes` 已先于 handler 拒绝跨 mode 调用，且无测试依赖旧错误串）；新增漂移测试断言 spec 键集==handler 键集。
- T2 RunManifest：抽 `_replay_config_fields()`（16 个回放/执行旋钮），spread 进 fold 与 held-out 两处 `RunManifest.create`（元学习清单不含、不动）；check.md “3 处含元学习” 经审计更正为“2 处”。e2e 断言 host_run_manifest 含该块代表字段。
- T2 download/audit 模板化：**有意推迟**（审计建议）。download.py/audit.py(~3000 行)已按 5 个 family spec + ~25 个 `spec.strategy` 分派，仅 ~20-30% 是 skip/query/write/log 样板，其余真异构；且属 PIT 摄取路径（曾致夜间 cron 中断），统一模板高风险低收益，违背原则#3。结论记于详细 logbook。
- 验证：全套 378 OK（+1 T6 漂移测试）；`git diff --check` clean；行为保持（清单/采集/派发不变）。

2026-06-30 R19 小合规 + RA2 竞价无滑点（fix/minor-compliance）

- 子代理审后逐项修，两处 check.md 断言被更正。R19-1 `step_tree` `[failed]` 标记改按 `status=="failed"`（仅 record_failed_attempt 设此字段），不再误标 `complete_validation is False` 的部分/调试节点。R19-2（证伪）：`artifacts.FORBIDDEN_CODE_REFERENCES` 不改——`/mnt/runtime/` 仍在 prompts/docs 须防硬编码、`/mnt/snapshot`（单数）是合法正式读根不可禁，加注释说明。R19-3 `broker.query_stock_orders` 文档串改准（当日可挂单簿 + 全回测累计已结/拒单；实盘 xtquant 仅返当日，差异已注）。
- R19-4 分钟域 schema 对齐：`snapshot._read_minutes_range`（回放分片）补 `apply_open_auction_correction`，与冻结 `_build_intraday` 同列，Timeview 滚动回放行不再 NaN 回填 7 个竞价校正列；并从冻结分钟域丢弃内部 `available_at`（分钟 `available_at==trade_time`，是门控列非 agent 信息，与 daily 一致），回放分片仍保留 `available_at` 作 Timeview 门控。R19-5 `deepseek` 会话日志 append 加注（>PIPE_BUF 非原子，但调用串行，并行才需锁）。R19-6 `shell` timeout 硬上限 120→600s（默认仍 120），解耦“缺省值/硬上限”，重活可调大（原则#2 探索自由），prompt 工具表措辞改准并重导出 PROMPTS.md。
- RA2（兼修 doc/code 漂移）：集合竞价（开 09:25/收 15:00）单一价清算无 taker 滑点——`match_bar` 改 `apply_slippage = not is_limit and not is_auction`；env_design §7.2 早已写“不计滑点”，本次代码对齐文档，更新 4 个竞价价格断言。
- 验证：全套 377 OK（+2：shell 上限、Timeview 分钟 schema）；`git diff --check` clean；PROMPTS.md 同步。

2026-06-30 R17 NL 性能（refactor/nl-perf）

- 接续 check.md 剩余任务（R16–R19/RA1–RA5），子代理先审后改。R17.1（真问题）：`_StrategyNLService` 每次 `ctx.nl()` 都经 `build_company_contexts` 全量重读 `universe.parquet`+`fundamentals.parquet`；快照回测期冻结，故新增 `CompanyContextStore`（懒加载一次 + 按 ts_code 记忆化），服务持一份、`run()` 传 `{ts_code: store.context(ts_code)}`，行为不变、两文件每回测至多读一次。
- R17.2（前提证伪、保留并加注）：`DeepSeekClient` 重建近乎零成本（仅存校验过的 config，urllib 每次 POST 本就新建连接），且按 timeout 重建能让会话日志记录的 `timeout_seconds` 准确；改为只把 timeout 透传 urlopen 反而丢日志保真度，故保留 per-timeout 重建（加注释）。连接池化是另一非等价改动，未做。
- R17.3（部分属实、最小去重）：NL/Explore 两个原生工具循环仅“形状”相同，工具集/解析/派发/截止/错误收尾各异，强抽一个循环要约 6 个回调，属过度工程（违背原则#3），故仅抽出字节级相同的 assistant 轮构造为 `llm.proxy.assistant_tool_turn`，两引擎共用。
- 验证：全套 375 OK（+1 记忆化断言：`pd.read_parquet` 跨重复/异码调用恰好 2 次、同码返回缓存对象）；`git diff --check` clean；内部性能、无契约变更、无 docs 改动。

2026-06-30 24h tick-replay W2–W9 完成（feat/24h-tick-replay）

- 接续 check.md（W1/W6/W7 已提交），完成剩余 W2–W9，单测 335→363。每步绿灯单独提交。
- REFRESH_NODES（`data/contracts.py`）：镜像 `tushare_update_schedule.json` 的落库 cron 任务（start+刷新耗时→ready_at）+ 域/数据集可见性 helper；漂移守卫测试（节点名∈cron、审计任务非节点）。
- 回放分片扩展（`snapshot.py`）：`build_replay_slot` 补宏观/基本面域 + 日线行级 `available_at`，六域齐备。
- W3 逐 tick 六域时序视图（`timeview.py`）：替换旧滚动日频 as-of。`ctx.asof_dir/<域>` 为 parquet 目录，part0 硬链冻结快照（零拷贝）、增量 write-once 仅在跨节点时追加；盘中视图冻结、零重建；`ctx.asof_version` 滚动时才变（缓存键）。配置 `rolling_asof_enabled`→`timeview_enabled`（保留别名）。
- W3.5 NL 文本滚动：`TextRetriever` 同读冻结+回放索引/库（零拷贝，1.6GB 语料就地按 `available_at` 门控），`as_of` 逐 tick 绑定；冻结研究语料恒可见。
- W4/W5 托管 `ctx.state_dir` 暂存（`state_staging.py`）：子步骤内写入路径式重定向到隐藏暂存目录（捕获 parquet 等任意写法，规避 path-guard 抓不到原生写的缺陷——经用户确认采用），主机在 `ready_at=tick+B` 合并、后写覆盖、审计 ledger（含未合并）；每次回测重置。`ctx.substep` B 不再改成交 bar。
- W9 分阶段耗时：summary/ReplayResult 增 `phase_seconds`（策略/大模型/时序视图/状态合并/券商撮合）+ 盘中/非交易切片数，并入 agent 白名单。
- W2/W9 文档+提示词+模板：Fold 提示词加数据可见性表（按节点）、24h 网格/尾盘竞价、substep 双重语义、暂存计划节奏、动态做空、phase 指标；五份 living docs 更新；模板 main/candidate 改为目录读取+asof_version 缓存+暂存计划；重生成 PROMPTS.md。W8 QMT 文档：统一 tick 轮询=实盘环路，下单前重校验当日融券+约束。
- 验证：全套 363 单测 OK；`git diff --check` clean；PROMPTS.md in sync。基准：metrics 已落地+轻量单测；完整 Docker 回放基准待用户按需触发（见 DETAILED_LOGBOOK 命令与对比口径）。

2026-06-28 GPT 实验审计 + Opus 深审修复（C1/O3/H2/V1/V2/M3 + 模板）

- GPT 对最近一轮实验的审计：3 个 Explore 核验 5 项均属实，并更正两处前提（compaction 0 次=设计内、非 bug；read 工具实被用 10 次=该指控不实），且 GPT 把两次实验混淆（core dump/垃圾脚本在 `regular_fold_last_taste_gpu_20260629_034005/run_c6d6e61dd4cb`）。随后 Opus 深审发现更高危缺陷，按序修复：
  - **C1（CRITICAL）**：`run_meta_learning` 的“收集失败仍落 ledger 再抛”耐久模式从未施于 `run_fold`/`run_heldout`——冻结后 collect 抛错即丢 ledger 记录、实验不可续跑（已损坏上轮 run）。两者改为：守护 collect、**总是** `ledger.append`（带 `finalize_error`）再抛；`run_fold` 另把冻结后 `_frozen_test_eval` 失败设为**非致命**（OOS test 仅诊断，不弃验证已接受的策略，符合 H2）。
  - **O3（HIGH，曾致真实数据丢失）**：`docker run` 加 `--ulimit core=0:0`（禁 core dump）；`_COLLECT_IGNORE` 加精确 `core.[0-9]*`（不误伤 `core.py`/`core/`）；`collect_artifacts` 改为**先收 output/models**（必成功）、workspace 末位 best-effort try/except（失败写 `*.collect_error.txt` 不中止）。放弃过度工程的 per-entry collect_errors 框架。
  - **H2（HIGH，确定性）**：紧墙钟上限（180/600）是随负载浮动的实墙钟，仅约束 `mode="valid"` 验证；最终评估（冻结 `test_000` + held-out，`frozen_eval`）改用宽松防挂死兜底 `backtest_final_eval_max_seconds_per_*`（900/3000），保证已过验证的策略能跑完且 accept/held-out 可复现。Q1 结论：不引入分钟级仿真时间预算（单 tick 死循环耗 0 仿真分钟，只有墙钟兜底能拦）。
  - **V1**：`data_summary.json` 与 `agent_trace.jsonl` 均 agent 可读（`/mnt/artifacts:ro` + SEARCH_ROOTS），raw `fold_2022Q1` 经其泄露日历期、使 manifest 不透明化形同虚设。5 处调用点（fold/meta/held-out）改传 `_agent_visible_ref(fold_id, prefix="fold_ref")`，host 关联靠 run_id + host_run_manifest。
  - **V2**：删除从不被读回、误导性的 `SandboxSpec.max_fold_minutes`（字段 + to_record）；真实界仍是 `fold_deadline_at`（env_design 既有表述无需改）。
  - **M3（MEDIUM）**：`DockerExecutor.kill_marker` 在标记 driver 后再 `pkill -KILL -u agent` 扫掉其衍生子进程（容器 PID1=root，安全），即便 driver 已退出也能回收孤儿，堵 pids/GPU 泄漏。
- 模板（用户追加“适度优化，让 Agent 充分理解用法且不诱导重复全表读”）：`configs/agent_output_template/main.py` 改为按 `ctx.asof_dir` 每日缓存筛选（每日仅读一次 daily.parquet，非每 tick），docstring 列全 `ctx` 关键面（substep/pending/nl/asof 等）作自文档示例。
- 推迟（用户确认）：Additional thought #2 固定日内推理窗口另立设计。不修（Agent 质量，非 harness）：GNN 过拟合/未用、CSV 杂乱等。
- 验证：全套 330 单测通过（C1/O3/H2/V1 新增/扩展测试）；`git diff --check` clean；env_design §7.2 同步；清理误生成的模板 `__pycache__`。

2026-06-28 GPT 审计修复（5 项）+ read 工具 + 工具文档微调

- GPT 审计 5 项经 Explore 核验全部属实并修复：
  - Fix1（High，我上轮引入的回归）：回放后 `_refresh_modification_check_after_replay` 抛 `ToolError`（line 368）经 `except ToolError: raise` 未发终止事件、留下未闭合的 `backtest_start`。改为 run() 维护 `_backtest_started` 标志，仅在已发 start 时由 ToolError 臂补发 `status="error"` 终止事件（pre-flight 拒绝仍干净抛出）。新增 test。
  - Fix2（Med，doc）：env_design 预算表 `per_call_timeout_seconds` 行仍写“单个 main(ctx) tick RPC 超时”——实为 Agent 主 LLM 调用 + contract_check 校验；回放 tick 用 `backtest_max_seconds_per_decision`。已改。
  - Fix3（Med）：`_python_import_names` 对未在别名表的连字符/点号包（`umap-learn`→应为 `umap`、`opencv-contrib-python`→`cv2`）会生成错误 import 烟测、误拒已装包。改为仅对“高置信”名（别名表或无 `-`/`.` 的简单名）发 import 行，歧义名跳过（仍验证 pip install 成功）；补两个常见别名。新增 test。
  - Fix4（Low/Med）：substep 预算对盘前竞价 tick 的影响未文档化/无测试。经实测：小预算（`ceil(B) ≤ lag_floor`）不改变竞价成交；大预算像连续单一样顺延成交 bar（`price_label` 仍 `auction`）。已在 §7.2 文档化并加两测。
  - Fix5（Low，提交卫生）：`run_audit_session.py` 未被 git 跟踪但被 pipeline_design 引用——是正式工具，应在提交时 git add（无代码改动）。
- 用户追加（经询问选定）：A — 新增 `read` 工具（带行号、可分页），复用 `StructuredSearchTool._resolve_search_path`+`SEARCH_ROOTS` 守卫，**不禁用 cat**；注册进 runner，prompt/env/agent 工具表加行 + “读要编辑代码优先于 cat/head”提示；新增 test。B — 轻量工具文档：仅给 `glob` 补 use-for 提示。C — 工具架构保持不变（已是标准模块化）。
- 验证：全套 322 单测通过（+5）；PROMPTS.md 同步含 read 行；`git diff --check` clean。

2026-06-28 回测成本模型改 per-day/per-decision + 模板简化 + 审计修复

- 成本模型（用户提议）：去掉固定总上限 `backtest_max_wall_seconds`，改两道随回放天数伸缩的真实墙钟硬上限——`backtest_max_seconds_per_decision`（180s，作 `MainPolicyRunner` 每 tick 硬截止、去掉 NL 不活跃重置，超时立即杀驱动）+ `backtest_max_seconds_per_trading_day`（600s，引擎按天累计 step() 墙钟、超限中止）。config/manifest/facts/白名单/docs/tests 全部改名；新增 per-day/per-decision 两测。
- 模板简化（issue #2）：`configs/agent_output_template/main.py` 由“导入 candidate/trading 的高级节奏 + 默认不交易”改为最小可用默认（持平时按 asof 等权买 top-N 并持有到末日清仓，自包含、含 pending 去重）；`candidate.py`/`trading.py` 标注“可选高级 helper”，README 先讲最小默认再讲可选节奏。
- 工具框架（issue #3）：现已是标准原生 function calling（`ActionSpec.to_tool_schema` → OpenAI 兼容 function tools → 结构化 tool_calls → Runner 硬校验），约 10 个工具；无需 Tool Search（那是给几十/上百工具的延迟加载，本系统加它属过度工程）。
- Opus 审计（issue #4）修复：Defect2 `except BaseException` 把普通 `ToolError`（重复结果目录/修改检查拒绝）误记为 `aborted`——加 `except ToolError: raise` 前置（新增 test）；Defect3 删除“NL 可在 substep 内并发”的不实表述（NL 宿主串行服务，墙钟为各 NL 之和）；Defect4 NL 单次超时设为决策上限的 0.8 留余量。
- 审计 Defect1（HIGH）已决：用户确认**不加**绝对总上限——总耗时上界即 `交易日数 × per-day 上限`，随回放长度自然伸缩，正是设计意图；已在 `environment_design.md` §7.2 记录“有意不设固定总上限”。过度工程评估：各 governor 互不冗余，`--init`/`timeout`/`kill_marker` 三件各管不同进程类；substep 声明式延迟模型最重但属既定设计。
- 验证：全套 317 单测通过；PROMPTS.md 同步；`git diff --check` clean。

2026-06-28 gnn_env_transfer_smoke 审计修复（G1–G6）

- 背景：一次 meta+标准 Fold smoke 跑出 7 个问题；3 个 Opus SubAgent 对实际产物核验后修复。两处用户判断被证据更正：①问题1 是“透明度缺口”非执行失效——BacktestTool 读内存 manifest（含 `backtest_max_wall_seconds=3600`，墙钟检查本就生效），只是 agent-visible `run_manifest.json` 投影白名单剥掉了这些预算字段；② Fold deadline 仅用于推理、回测时间独立计算（现有 deadline-exclusion 已实现，保留）。
- G1 透明度：`runtime.py` agent-visible 白名单补入 `decision_max_sim_minutes`/`backtest_max_wall_seconds`/`max_backtests_per_fold`/`execution_lag_bars`/`nl_max_calls_*`/`auction_*`/`rolling_asof_enabled`。
- G3 进程清理：一次性 shell 在容器内 `timeout --kill-after=5 setsid -w` 包裹（超时整组杀），宿主 deadline 仅作更长兜底；容器 `--init` 回收孤儿；常驻回放 driver 加唯一 cmdline marker，超时/teardown 经 `docker exec pkill -f` 回收（避免 torch 子进程残留）。
- G4 DuckDB CLI：`sandbox.Dockerfile` 装 duckdb CLI 1.1.3（curl release zip，带 `--retry`），`IMPORTANT_TOOLS` 加 `duckdb`（与镜像安装耦合）。镜像构建网络对 GitHub CDN 不稳→改用 `docker build --network=host`（host 可下，已在 Dockerfile 注释）。
- G2 可观测：回测发 `backtest_start` + 节流 `backtest_progress` 心跳 + 保证终止事件（`BaseException`→`aborted`，解决“卡死无 outcome”）；replay 跨 tick 聚合 substep 墙钟，summary 增 `started_at`/`replay_wall_seconds`/`replayed_trade_days`/`substep_runtime`（并入 backtest-summary 白名单）。
- G5 成本：默认 `backtest_max_wall_seconds` 3600→1800（完整验证超限即 `BacktestError`、不可冻结）；backtest 工具描述+prompt 指导“小 `replay_window` 试探→外推→再跑完整”，并要求缓存重计算、压低调仓/图构建成本。
- G6 提示：shell schema+响应增 `2>/dev/null` 提醒（命中附非阻断 `stderr_suppression_reminder`）；FOLD 工作步骤加“最小数据契约”一步。
- 验证：全套 314 单测通过（+5）；PROMPTS.md 同步；`git diff --check` clean；env_design 同步更新。镜像重建中（host 网络）。FYI（不属 harness）：实验里生成的 `candidate.py` `_trading_day_count` 从不自增、`REBALANCE_GAP_DAYS` 死路——属 Agent 代码。

2026-06-28 docs/ 跨文档去重（单一权威 + 交叉引用）

- 执行 docs 审计的跨文档去重：把重复内容收敛到单一权威文档，其余改为简洁摘要 + 交叉引用，保持各文档可独立阅读。
- 权威归属：执行/Broker/延迟模型 → env §7.2（agent §5.2 收为 Agent 合同 + 引用）；ctx 字段清单 → agent §5.3（env §7.2 收为指针）；快照路径 → env §3（agent §2.1 / pipeline §6.1 引用）；shell guard → env §6.1（agent §3.2 引用）；联网/代理 → env §4.1（agent §3.1 / pipeline §6.2 引用）；派生镜像 schema/构建/GC → pipeline §6.1（env §8.1 引用，并把 schema 补进 pipeline §6.1）。
- data_documentation 经核验本就健康：`available_at` 的 §5.2/§6 已是 `见 2.7` 指针；审计所指 §3.3 绝对路径 Nit 系误报（全文统一 `~/miniconda3/bin/conda run -n quant`）；§6 风险表单位行属风险视角的自包含表述，保留。
- 校验：新增 12 处跨文档引用全部指向真实小节标题；`git diff --check` clean；纯 .md 改动，无代码/prompt 变更，单测不受影响。

2026-06-28 docs/ Opus 审计与精确修复

- 启动 Opus 4.8 审计 docs/ 五份 living docs（逻辑重构、简洁 vs 完整）。结论：单文档结构良好、内容完整，主要问题是跨文档重复（执行模型/快照路径/shell guard/元学习联网在 2–3 份文档近重复），非单文档冗余。
- 已修正（确认无误后）：env §7.2 + agent §5.2/§5.3 三处坏 TOC 锚点（标题已改名，锚点未同步）；env §7.2 残留的“正预算（B>0）”措辞（B>0 已在 API 强制、fail-fast 已无条件）；补文档化 `ctx.substep` 同一 tick 重名拒绝（外部改动新增、此前无文档）——env §7.2 + agent ctx 注 + Fold prompt 同步，重导出 PROMPTS.md。
- 待定（已呈报，属架构判断未自动执行）：跨文档去重（以 env §7 为执行模型唯一权威、env §3 为快照路径权威、env §6.1 为 shell guard 权威，agent/pipeline 改为交叉引用）；env §7.2 过载，建议把“决策延迟+资源预算”块拆为独立小节。
- 验证：全套 309 单测通过；`git diff --check` clean；PROMPTS.md 同步。

2026-06-28 NL SubAgent 提示词补齐证据纪律

- 追问“NL SubAgent 工具/提示词是否对齐主 Agent”后的结论：工具不对齐（`text_retrieve` 是 PIT 证据检索，不是文件 grep；Explore SubAgent 才与主 Agent 共用 grep/glob，因同一文件语料）。仅对齐“实质”——把 Fold/元学习提示词与 `environment_design.md` §6.3 已规定的 NL 证据纪律补进 NL SubAgent 自身提示词。
- 改动：`nl/engine.py` `SUB_AGENT_SYSTEM_PROMPT` 的 Data Boundary 增补一句——优先最近 PIT 证据、警惕发布/入库时间与召回偏差、证据不足时显式说明并降置信而非用模型先验填补、自由文本只作可权衡证据。约 30 token，不改其精简 4 段结构（该提示词每次 `ctx.nl()` 都发送，避免膨胀）。
- 验证：重导出 PROMPTS.md（NL 提示词第 8 节同步）；NL scoring 19 测 + 全套 309 单测通过；`git diff --check` clean。

2026-06-28 substep 禁止零预算（决策追问）

- 决策：`ctx.substep` 现要求 `budget_minutes > 0`；包裹但 B=0 直接被拒（ValueError → 经驱动 surfaced 为 BacktestError）。理由：包裹+B=0 与“不包裹”完全等价（无延迟、无实时上限），是无意义写法；要求正预算让每个 substep 诚实，并对“轻量块”也重新启用实时安全网。
- 关键事实：`B ≤ execution_lag_bars`（默认 2）时 `extra = max(0, ceil(B)-lag_floor) = 0`，成交 bar 与默认一致——所以轻量决策给 0.5–1 的小预算几乎不影响成交时点，却获得逐块实时上限。
- 实现：驱动 `substep` 入口校验 `B>0`（default 改 None）；host fail-fast 去掉 `budget_min>0` 特判（API 已保证正），模型统一。逐 tick 琐碎代码仍可不包裹（默认 lag、无逐块上限）。
- 测试：`test_substep_zero_budget_is_rejected`（B=0 → BacktestError）、`test_substep_light_positive_budget_fills_at_default_bar`（B=1 → 仍 09:32 成交）。prompt + environment/agent docs 同步“B 必须为正/轻量用小值/不包裹=默认 lag”。
- 验证：全套 306 单测通过；PROMPTS.md 同步；`git diff --check` clean。

2026-06-28 WS A/B/D Opus 审计与修复

- 启动 Opus 4.8 SubAgent 全量审计（doc/code 对齐、逻辑合理性、过度工程）；全套 305 单测通过，PROMPTS.md 与 prompts.py 完全一致。
- 已修高优问题：`budget_minutes=0` 退化 bug——fail-fast `real > B·60` 在 B=0 时令任何被包裹的真实工作都中止回测，且与 prompt/docs“budget 0 = 默认 lag 成交”矛盾。改为仅对正预算做 fail-fast；budget 0 = 不设上限、按默认 lag 成交（新增 `test_substep_zero_budget_does_not_abort_on_real_work`，overrun 测试改用 0.001min 正预算）。
- 其它修复：`backtest_max_wall_seconds` 改为自回放开始的真实墙钟（含 NL/撮合），不再只累计 tick 计算；派生镜像 GC 改按 `CreatedAt` 排序而非依赖 `docker images` 默认序；substep 名为 "None" 不再与未包裹单冲突（用 None 哨兵跳过查找）；`run_audit_session.py` 补 `max_backtests_per_fold`。
- 文档：prompt + environment_design 明确 budget 0 语义与“协作式”延迟模型（未包裹重决策不被建模），新增“执行与资源预算一览”表汇总 7 个时间/成本护栏。
- 接受不改（已记录）：延迟模型 opt-in（按已批准设计，不引入 Environment 侧自动延迟）；auction tick 大预算会减去连续 lag 的边角不一致（低风险、修复会增分支）。
- 验证：全套 305 单测通过；`git diff --check` clean；PROMPTS.md 重新导出同步。

2026-06-28 延迟感知回测 + 资源护栏 + Pipeline 加固（WS A/B/D）

- 背景：三轮 `gnn_dependency_transfer` 实验 + 取证审计暴露执行模型与 Pipeline 缺陷；按已批准计划实现。
- WS A 延迟模型（LEAN 风格）：新增 `ctx.substep(name, budget_minutes=B)`，其内下单成交 bar 顺延到 `决策分钟 + max(execution_lag_bars, ceil(B))`，`B` 固定写码 ⇒ 成交可复现；Environment 实测每个 substep 真实墙钟，`real > B·60s` 立即 `BacktestError` fail-fast（低报不可利用）；`decision_max_sim_minutes` 上限 → `decision_too_slow` 可复现错过；`backtest_max_wall_seconds` 松总上限防失控。`backtest` 独立计时（耗时回补 Fold deadline），单 Fold 上限 `max_backtests_per_fold`。
- WS B 加固：`ops/docker/sandbox.Dockerfile` 预装 build-essential/g++/gfortran/python3-dev（消除“缺编译器”构建失败类）；容器缓存（pip/HF/torch/CUDA）经环境变量重定向到 `/tmp`，不落进被采集的 `/mnt/agent`；派生镜像 tag 从 ledger 回读（resume/fold-only 仍继承扩展镜像）；成功构建后按 `meta_sandbox_image_keep` 尽力 GC 旧派生镜像。
- WS D 文档：Fold Prompt 新增 `## Pipeline流程`、`## Broker 交易接口`表、`## ctx 接口与数据视图`，并写入延迟模型/独立计时/NL 配额；agent-visible facts 暴露 `execution_lag_bars`/`decision_max_sim_minutes`/`backtest_max_wall_seconds`/`nl_max_calls_*`/`max_backtests_per_fold`；regenerate `PROMPTS.md`；更新 environment/agent/pipeline 三份 living docs。
- 验证：全量单测 304 passed（新增 substep 成交延迟 + overrun fail-fast、镜像 tag resume、派生镜像 GC、缓存重定向 4 项）；`PROMPTS.md` 同步。

2026-06-28 最新元学习评估审计与 sandbox 清理

- 审计最新实验 `meta_learning_network_boundary_20260627_200447` / `run_10ff0af686d2`：运行 exit code 0，ledger status `taste_only`，17 次主 LLM、43 次 shell、7 次 web_search、1 次 explore，未触发 context compact；只有 1 次 Semantic Scholar `empty_results`，后续搜索仍满足执行流程。
- 发现 Taste 仍包含具体样本窗口日期/月度信息和模板文件名，和“跨周期迁移、避免写入时间窗口”的写作目标不完全一致；代码侧 Claude 新增的 trace `phase="pipeline_finalize"` 标记设计合理，但最新实验产物是在该改动前生成，trace 中尚无该字段。
- 已删除 `.runtime/sandboxes` 中除最新 `run_10ff0af686d2` 外的旧 sandbox；部分旧目录权限归 Docker/agent 用户，普通 `rm` 失败后用 Docker root 挂载清理完成。当前 `.runtime/sandboxes` 仅剩最新 run，约 4.4G。
- 清理 `src/`、`tests/`、`scripts/` 下 `__pycache__`；验证：7 条相关单测 OK，`git diff --check` OK。

2026-06-27 元学习 Sandbox 依赖模板

- 按 GPT 5.5 High SubAgent 只读审计建议，未在 workspace 默认创建会触发构建的 `sandbox_environment.json`，而是在元学习 Sandbox 初始化时写入非触发模板 `workspace/sandbox_environment.example.json`。
- 模板是合法 JSON，列出 `python_packages`、`apt_packages`、`npm_packages`、`reason`、`notes`；普通 Fold 不写入该模板，正式派生镜像仍只由元学习主动写 `sandbox_environment.json` 触发。
- 同步元学习 Prompt、`PROMPTS.md` 和 living docs；新增单测覆盖元学习有 example、普通 Fold 没有 example、真实请求文件不被自动创建。
- 验证：`py_compile` OK；3 条 pipeline sandbox environment 相关单测 OK；`scripts/dev/export_prompts.py` OK。

2026-06-27 单会话审计脚本

- 确认当前代码默认镜像为 `autotrade-sandbox:latest`，旧 `macroquant-sandbox:latest` 不再被当前代码路径引用；可保留用于历史复现，当前实验不再需要。
- 新增 `scripts/experiments/run_audit_session.py`：支持 `--mode meta-learning` 单独调用 `run_meta_learning()`，以及 `--mode fold` 单独调用 `run_fold()`；用于人工审计 Prompt、Trace、Sandbox 和单个 Fold 产物交接，不替代完整 `run_experiment.py`。
- 脚本默认不自动构建 Docker 镜像，缺少 `autotrade-sandbox:latest` 时显式报错；`fold` 模式不构造 Web Search provider，避免普通 Fold 对联网 key 的隐性依赖。
- 同步 `docs/pipeline_design.md`；验证：`py_compile` OK，`--help` OK。

2026-06-27 元学习 Fold 运行与 Trace 整理

- 运行前发现当前默认 Docker 镜像 `autotrade-sandbox:latest` 不存在；Docker Hub 直连拉取 `python:3.11-slim` 超时后，按历史做法从 `docker.m.daocloud.io/library/python:3.11-slim` 拉取并 retag，随后用清华 PyPI 源成功构建 `autotrade-sandbox:latest`。
- 第一次 meta-learning direct run 因 one-off 调用脚本误导入不存在的 `make_folds` 失败，未进入 Agent 会话；已标记为失败并用正确 `build_fold_schedule()` 重跑。
- 有效运行：`experiment_id=meta_learning_network_boundary_20260627_200447`，`run_id=run_10ff0af686d2`，真实 Docker meta-learning-only，DeepSeek V4 Pro `reasoning_effort=max`，季度周期，21 个月历史窗口，21 个交易日分钟线，Web Search engines=`tavily, semantic_scholar`。
- 结果：`finish_status=meta_learning_done`，ledger status=`taste_only`，Taste 4253 chars；trace 17 次主 LLM、43 次 shell、7 次 web_search、1 次 explore、0 次 context compact；token total=619316，cache_hit_ratio=0.64。日志 `logs/meta_learning_network_boundary_20260627_200447.log`，trace `experiments/meta_learning_network_boundary_20260627_200447/artifacts/run_10ff0af686d2/agent_trace.jsonl`。
- 已覆盖写入 `check.md`，按对话/工具调用格式整理完整会话过程、关键工具输出、Explore 摘要和最终 Taste；未逐字展开 provider `reasoning_content`。

2026-06-27 元学习 Prompt 轻量优化

- 元学习 System Prompt 改为明确“当前可见数据是本 Epoch 首个普通 Fold 的示例可见窗口”，后续普通 Fold 会滚动到各自窗口。
- 联网措辞改为“配置允许时”：后续普通 Fold 不允许联网或安装新包，元学习 Fold 是唯一可配置联网阶段；工具表也改为按 tool schema/run manifest 选择搜索参数。
- 在 `## Pipeline流程` 中直接补充后续普通 Fold 不可以联网、不安装新包；元学习期联网探索只能沉淀为可迁移 Taste，或通过 `sandbox_environment.json` 声明需要构建进后续 Sandbox 的稳定依赖。
- Taste 输出模板放入 `text` 代码块，并说明代码块围栏不要写入 `taste.md`；实验事实块移除 `generated_at`，减少 prompt 审计 diff 噪声。
- 验证：`scripts/dev/export_prompts.py` 重生成 `configs/prompts/PROMPTS.md`；`py_compile` 通过。

2026-06-27 回测摘要、撤单日期与限价口径修复

- 修复 `backtest_tool` 在 `main(ctx)` 回测期间生成/修改 `models/` 后仍使用旧 modification delta 的问题：回放后重新读取 output/models hash，必要时刷新 `ModificationCheckTool`，summary 增加模型侧 delta 计数。
- 修复日终撤销仍挂限价单时 `order_cancelled` 事件日期可能取旧 `current_date` 的问题：日终自动撤单显式传入当日 `trade_date`。
- 按现有 Broker 代码口径同步 Prompt/文档/模板：限价单无滑点；开盘已优于限价时按 open 成交，否则盘中触价按限价成交。同步修正少量旧注释。
- GPT 5.5 High SubAgent 复审无阻塞问题；按建议把 `TraderProtocol.cancel_order_stock` 保持 xtquant 风格签名，并补卖出限价 better-open 单测。
- 验证：定向测试通过；全量 `unittest discover` 294 通过（skipped=2）；`git diff --check` 通过。

2026-06-27 Opus 审计：执行模型确认正确，落地小修

- 用 Opus 4.8 子代理只读审计全仓代码 + 文档（基线 293 通过）。结论：次一根成交 / 订单簿 / 限价 / 竞价 / 滚动 as-of / pending 去重逻辑正确，无前视或数据完整性 bug，文档与代码一致。落地以下修复：
- 删两个死函数 `backtest_engine._bar_execution_price`、`_minute_bar_for_code`（全仓仅定义、无调用；后者与 `broker._bar_for_code` 重复）。
- `_day_tick_plan` 把 lag 收敛为 `max(1, min(execution_lag_bars, n-1))`：修复「日线退化日（仅 09:30/15:00）+ 关闭竞价 + lag≥2 → 整日零成交且无提示」的隐性边界；正常多 bar 日不受影响（全部测试不变）。
- 模板 `example_swing_t` 加 `if ctx.broker.pending(ts_code): return`，与文档「成交滞后需幂等」一致，示例不再示范跨 tick 重复下单。
- `BrokerProfile.maintenance_warning_ratio/withdraw_ratio` 注明仅记录用（只有 closeout 被执行）。
- `.gitignore` 忽略未跟踪的 `external_references/`（~10MB vendored repos）与 `check.md`，防误提交。
- 验证：全量 293 通过；`git check-ignore` 确认两项已忽略。

2026-06-27 Broker 底层接口对齐 xtquant（订单簿入 SimBroker）

- 把每日订单簿从引擎移入 `SimBroker`，暴露与实盘 xtquant 1:1 的底层接口：`order_stock(order_type, stock_code, order_volume, price_type, price, …) -> order_id`、`cancel_order_stock(order_id)`、`query_stock_orders(cancelable_only)`、`query_stock_trades`、`query_stock_positions`、`query_stock_asset`；`match_bar` 逐 bar 撮合（市价按 open + 滑点；限价触价按限价、做市无滑点；TIF 到期撤单）。`get_account/get_positions/query_orders/trades_for` 重命名为 query_stock_*（更新全部调用方）。
- 常量 `xtconstant`（STOCK_BUY/STOCK_SELL/CREDIT_SLO_SELL/CREDIT_BUY_SECU_REPAY/FIX_PRICE/MARKET_PEER_PRICE_FIRST）+ 内部 action↔order_type 映射；`Order` 复用 `order_id`，`execute(order_id=)` 透传，成交带原委托 id。`TraderProtocol`（typing.Protocol）定义 SimBroker 与未来 live `QMTBroker`（封装 `xt_trader`）共用的契约。
- 引擎瘦身：删除引擎本地 `_Order`/`_make_order`/`_fill_or_rest`/`_fill_price_for`/`_open_price`/`_working_orders`；改为按 `execution_lag_bars` 调 `broker.order_stock`（lag 内意图存引擎 `incoming`）+ 每 bar `broker.match_bar` + `query_stock_orders(cancelable_only)` 喂 `pending`。`ctx.broker.buy/sell/short/cover/close(limit=, valid_bars=)` 便捷封装不变。
- 验证：纯重构无行为变化，全量单测 293 通过（新增 order_stock 生命周期测试：下单/撤单/查询/触价成交/到期撤单）；同步 environment_design §7.1 含 xtquant 映射表。

2026-06-27 限价单（FIX_PRICE）+ 每日订单簿，对齐 xtquant

- 据官方 xtquant 文档（dict.thinktrader.net/nativeApi/xttrader.html）确认：`FIX_PRICE` 是挂在交易所的限价单（触价成交、未成挂单、可 `cancel_order_stock`、当日有效），与回测「挂限价、bar high/low 触及成交、到期撤单」完全对应；QMT 缺的是券商侧条件单/止损单，普通限价单是支持的。早前「不上限价单」判断有误，纠正。
- 引擎把单 bar 的 `pending` 字典重构为每日订单簿（`_Order` + `_make_order`/`_fill_or_rest`/`_fill_price_for`）：决策 bar +`execution_lag_bars` 进入撮合（`activate_index`）。市价单在进入 bar 按 open + 滑点成交；限价单（`limit=P`）自进入 bar 起挂 `valid_bars` 根（默认 1），买/补 `open<=P` 或 `low<=P`、卖/空 `open>=P` 或 `high>=P` 时按 P 成交，窗口内未触及记 `order_cancelled`（expired_unfilled），当日收盘仍挂的也撤。
- 限价单做市无滑点：`SimBroker.execute(apply_slippage=)` 透传到 `_open`/`_reduce`/`_fill_*`，限价成交用 raw_price 不加滑点（仍收佣金）；市价单照旧加滑点。
- Agent API：`ctx.broker.buy/sell/short/cover(code, ..., limit=None, valid_bars=1)`（`close` 恒市价）；限价单不更新乐观持仓视图（成交不确定），跨 tick 去重靠 `pending()`。映射：buy/sell/short/cover/close↔STOCK_BUY/STOCK_SELL/CREDIT_SLO_SELL/CREDIT_BUY_SECU_REPAY/市价平仓。
- 验证：全量单测 292 通过（新增限价成交 + 限价到期撤单 2 测）；同步 environment_design/agent_design/prompt/template，PROMPTS.md 重生成。

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

2026-06-28 GNN dependency transfer audit

- 审计 Claude 年份/日期 Taste 兜底：逻辑只在 meta-learning `done` 检查 `taste.md`，无额外状态、文件或多层 guard，足够轻量；全量测试 299 OK。
- 修复两处实验暴露问题：sandbox image rebuild 失败时仍记录 meta artifacts/ledger；artifact 收集忽略 Docker/GPU 运行缓存 `.nv`，避免权限错误。
- 运行 `gnn_dependency_transfer_final_20260628_011339`：用户级 directive 已传入；meta-learning 写出 `sandbox_environment.json` 并成功构建派生镜像；第一个普通 Fold 使用该镜像且网络为 `none`，Taste 也已注入。
- 正式 Fold 未完成验证回测：Agent 生成策略在分钟回放中反复加载/计算较重日频特征，回测超过 70 分钟无 trace/manifest 更新后手动 TERM，并停止残留容器。实验产物保留供审计。

2026-06-28 Held-out/runtime audit fixes

- 修复 GPT-5.5 High SubAgent 审计发现的三处问题：held-out manifest 现在写入与 Fold 一致的回放/预算/NL 字段；Explore SubAgent 使用扣除 backtest 墙钟后的有效 deadline；`ctx.substep` 同一 tick 内重名会被拒绝，避免预算映射覆盖。
- 移除未使用的 `_TickResult.real_wall_s` / `tick_real_wall_s` 字段；substep 自身的 `real_wall_s` 仍保留用于预算 fail-fast。
- 验证：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_main_ctx_replay tests.unit.test_tools_flow tests.unit.test_pipeline_e2e tests.unit.test_sandbox_isolation` -> 148 tests OK；`git diff --check` OK。`scripts/dev/export_prompts.py --check` 不存在该参数，未执行。

2026-06-28 GNN meta-to-fold transfer smoke

- 启动 `gnn_env_transfer_smoke_20260628_143822`（meta-learning + 第一个普通 Fold），用户级指令要求安装/声明 GNN 相关依赖。
- Meta-learning 正常 `meta_learning_done`，写出 `torch-geometric==2.8.0`、`einops>=0.8.0`、`scikit-learn>=1.5.0` 的 `sandbox_environment.json`；Pipeline 成功构建派生镜像 `autotrade-sandbox:gnn_env_transfer_smoke_20260628_143822-epoch_001-d5e3c6b5ca67`。
- 普通 Fold 使用派生镜像、`network=none`、1 张 L20 GPU；容器内验证 `torch.cuda.is_available()==True`，可 import `torch_geometric/einops/sklearn`。
- 普通 Fold 完成 debug 回测和两次完整 valid：`valid_001` total_return=3.33%、sharpe=1.36 但拒单 331；修复后 `valid_002` total_return=2.10%、sharpe=0.75、拒单 0。
- 稳定性结论：环境传递和 GPU 分配正常，但普通 Fold 未干净完成。Agent 在 step_003 又重训并启动第三次完整回测，整体超过 60 分钟 Fold 预算；一次 shell timeout 还留下了需手动清理的子进程。已 TERM 实验进程并停止容器，GPU 释放。日志 `logs/gnn_env_transfer_smoke_20260628_143822.log`，trace/产物见 `experiments/gnn_env_transfer_smoke_20260628_143822/`。

2026-06-28 Text evidence audit refresh

- 补跑本地文本证据审计，刷新凌晨审计在文本回填前留下的 error 状态；不重新下载数据。
- 命令输出：`audit status=warning errors=0 warnings=20 output=/Data/lzp/MacroQuant/results/data_quality/text_evidence_status.json`。
- 日志：`logs/manual_text_evidence_audit_20260628_2021.log`。

2026-06-28 Regular Fold from previous Taste with multi-GPU sandbox

- 使用上一轮 `gnn_env_transfer_smoke_20260628_143822/meta_learning/epoch_001/taste.md` 启动单个常规 Fold，实验 `regular_fold_last_taste_gpu_20260629_034005`。
- 启动前发现并修复 Docker 多 GPU 参数渲染问题：多卡需要 `--gpus '"device=5,6,7"'`，否则 Docker 报 `cannot set both Count and DeviceIDs`。补充 `run_audit_session.py` 的 `--sandbox-image` / `--gpu-devices` / 宽松 acceptance 参数和单测。
- 当前运行：PID `2529022`，runtime `.runtime/sandboxes/run_c6d6e61dd4cb`，容器 `mqsbx_feba0ac75d17`，派生 GNN 镜像，`allocated_gpu_indices=[5,6,7]`，容器内 `torch.cuda.device_count()==3`。日志 `logs/regular_fold_last_taste_gpu_20260629_034005.log`。
- 复查结果：Fold Agent 已 `fold_finished` 并冻结 `strategy_epoch_001_fold_2022Q1`，但 CLI 最后收集 artifacts 时因 `workspace/core.7194`/`core.7449` 权限不足退出。训练出的 `gnn_model.pt` 很小且过拟合，最终策略实际调用 simple factor ranking 而非 GNN；完整 valid_011 return=-6.31%、Sharpe=-2.75，test_000 return=-3.91%、Sharpe=-0.76。实验无 ledger，runtime/partial artifacts 可审计。

2026-06-30 Full-repo audit + remediation plan (check.md R1–R19); Phase A landed

- 7 parallel Opus auditors + own verification over `feat/24h-tick-replay`: 363 tests green, no PIT/look-ahead leak (Timeview predicate, prior-day-close anchor, sim-clock, W7 fill-day short gate, `ctx.nl()` cron-gating, agent-readable `run_manifest` redaction all verified). Findings recorded as R1–R19 in `check.md` (replaced the landed W-plan); decisions D-R7/D-R8/D-R16 resolved with the user.
- Phase A `fix/audit-tier1-contracts` (commit c92aa81): R1 remove non-existent `ctx.cash` from prompts/docs/template, keep `ctx.broker.cash` (regenerated `PROMPTS.md`); R2 unify `offsession_tick_minutes` default to 15 (engine + tool fallback; explicit 0 still disables); R3 bind Timeview drift guard to `ops/cron/tushare_update.cron` launch times + assert every non-audit job has a node + evening `ready_at` fixture.
- Validation: full suite 366 OK (was 363; +3 drift-guard cases); `git diff --check` clean. CPU-only unit work, RAM ~401Gi free, no GPU/training run.

2026-06-30 Phase C — reporting + state-contract fixes (R9–R11)

- Branch `fix/reporting-and-state-contract` (on Phase A). R9: experiment report now sets top-level `status="warning"` when benchmark data is missing (missing_raw_dir/missing_data/no_period_coverage); `ok`/`disabled` stay `ok`; `report_experiment.py` surfaces it. R10: inside `ctx.substep`, `ctx.state_dir` is seeded with a copy of the visible state so reads return the old visible value (contract), while writes still stage for delayed merge (capture only changed/new files). R11: `AcceptanceRules.min_return` now uses `<` (inclusive), matching the Sharpe/drawdown bounds.
- Validation: full suite 368 OK (+2 cases: in-substep read, benchmark warning); `git diff --check` clean. CPU-only, no GPU/training.

2026-06-30 Phase B — broker faithfulness (R4–R8)

- Branch `fix/broker-faithfulness` (on Phase C). R4: `SimBroker.roll_to_date()` unlocks T+1 at each new trade date in the host day-loop before the first tick (overnight holds report correct `sellable_quantity` pre-fill). R5: shorts exempt from the T+1 sell lock (`sellable_quantity` side-aware) → same-day cover allowed; long T+1 unchanged. R6: the 14:57 close auction fills at the activation bar's CLOSE (threaded `is_close_auction`→`auction_close`→`_limit_fill_price use_close`); open auction unchanged. R7: per-substep wall fail-fast skipped under frozen/final eval (`enforce_substep_timeout = mode=="valid"`). R8: short borrow fee accrues per calendar-day gap (weekend carry); short proceeds locked as collateral (`available_cash = cash − short_margin − locked_proceeds`; long-buy and short-open gate on it) so a short no longer inflates buying power.
- Validation: full suite 374 OK (+6); `git diff --check` clean. CPU-only, no GPU/training.

2026-06-30 Phase E — repo hygiene (R14, R15)

- Branch `chore/repo-hygiene` (on Phase B). R14: deleted the reappeared untracked leftovers `scripts/data/{download,audit}_tushare_p0.py` (duplicated the official src-backed entrypoints), `scripts/data/test_write_marker.txt`, and `.mutagenignore.suggested`; added `.gitignore` guards so they can't be committed if regenerated. R15: replaced `AGENTS.md` body with a pointer to `CLAUDE.md` (single source of truth; the two ~8.5KB copies had begun to drift).
- No code change; `git diff --check` clean.

2026-06-30 Phase D — living-doc sync (R12, R13)

- Branch `docs/post-audit-sync` (on Phase E). R12: `environment_design.md` §6.1/§7.2 now describe the per-tick 24h grid (was "逐分钟"), document the 14:57 close-auction tick (fills at the 15:00 bar close, no slippage), and add `offsession_tick_minutes`/`auction_enabled`/`auction_close_time` to the budget table (defaults verified vs config.py); the QMT 14:57 reference is now grounded. R13: de-chronicled the `rolling_asof_enabled→timeview_enabled` rename note, removed the "旧 09:25" anchor comparison in `pipeline_design.md`, fixed a leftover "逐分钟" claim in §4.2, and clarified `fundamental_events.available_at`=公告日18:00 is the row-level rule (distinct from the ~03:50 PIT landing node).
- Docs only; `git diff --check` clean.

2026-06-30 Broker cancel API + Fold prompt/action split

- 开放 `ctx.broker.cancel(order_id, reason=None)`；`buy/sell/short/cover/close` 返回 `order_id`，委托记录携带 `submitted_at`/`submitted_time`，`pending(ts_code=None)` 可查全部 pending 并返回 `status`、`age_minutes`。cancel 同时支持尚未进入 Broker 的 submit-lag 队列和已在 Broker 工作簿里的限价单；同 tick 下单后 cancel 会从本 tick `main_actions` 中净掉。
- 优化 Fold Agent Prompt：把 broker/ctx 交易原语从“环境与配置”移入“动作与流程 / 策略代码接口”，保留环境章节只描述规则事实；补充每分钟取消 `age_minutes > 1` pending 订单的子步骤示例，并提示非交易时间不能直接下单、盘前计划应先写 `ctx.state_dir`，后续在 09:15/09:25 等可报单 tick 提交。模板策略同步加入 `cancel_stale_pending()`。
- GPT-5.5 xhigh SubAgent 审计后修复：same-tick `pending()` 记录补齐文档化字段且不泄漏 `_substep`；最后一个真实 bar 后立即做 day-end cancel，避免 post-close off-session 再看到可取消 working order；同 tick buy+cancel 的 `main_actions` 只记录净订单；README 的 off-session wording 改为“不提交新订单”而非禁止轻量 hygiene。
- 正式 Fold 暴露并修复一个 runtime 权限问题：Docker 内 `agent` 写 `.state_staging` 时 host 创建目录为 0775，导致 `PermissionError`；`StateStager` 初始化后显式 chmod 0777，并新增权限合同单测。
- 重新构建 `autotrade-sandbox:latest`。正式测试：meta-learning audit `cancel_prompt_audit_20260630_2304_meta` 返回 `status=ok`，Taste 5424 chars；普通 Fold day-period `cancel_prompt_audit_20260630_2359_fold_day` 返回 `status=ok`、`fold_status=no_update_timeout`、run `run_f43ae3e0ced3`（显式 parent fallback）。季度普通 Fold 诊断 run `run_1035d8ca1531` 到 `fold_finished`，3-day valid 回测成功（19 orders / 17 trades），但因初始无 parent 且无完整 validation 不形成正式 experiment 目录，改用 day-period rerun 闭环。
- Validation: full suite `unittest discover -t . -s tests -p 'test_*.py'` -> 396 OK; `git diff --check` clean. 资源复查：内存约 397 GiB available；GPU 5 空闲，其他 GPU 为既有任务占用。

2026-06-30 Backtest validation cap refresh + trace review

- 将验证回测默认硬上限从单 tick 180s / 单交易日 600s 调整为 300s / 900s；`ExperimentConfig`、`BacktestTool` 缺省兜底、`environment_design.md` 和相关单测 fixture 已对齐。最终评估兜底仍为 900s / 3000s。
- 清理 `.runtime/sandboxes`，仅保留最新两个 sandbox：`run_f43ae3e0ced3` 与 `run_1035d8ca1531`；目录大小降至约 16G。
- Trace 审计：meta-learning run `run_766797dac06a` 输入/输出正常，`session_end=meta_learning_done`；day-period Fold run `run_f43ae3e0ced3` Agent IO 正常并 `fold_finished`，但验证/冻结回放因 day-period 只有 1 个交易日被 `replay region needs at least two trade dates for entry/exit` 拒绝，最终走 parent fallback；季度诊断 run `run_1035d8ca1531` 在权限修复后 3-day debug backtest 正常成交。
- Validation: `python -m unittest` targeted 4 tests OK；`git diff --check` clean。`pytest` 在当前 `quant` 环境中不可用，未使用。

2026-06-30 ctx.substep broker action delayed-submit semantics

- 将 `ctx.substep(name, budget_minutes=B)` 内的 broker action 改为真实延迟提交：块内 `buy/sell/short/cover/close/cancel` 等到 `ready_at=tick+B` 后第一个可报单 tick 才提交，然后再走常规 `execution_lag_bars` / 竞价撮合。
- `ctx.broker.pending()` 现在同 tick 即可看到 substep 延迟单，记录 `pending_stage="substep_delay"` 和 `ready_at`；块内下单不再投影同 tick 现金/持仓，ready 后由宿主 Broker 真实约束。`auction_close_time` 默认与文档/配置对齐为 `"14:57"`。
- GPT-5.5 xhigh 子代理审计后修复边界：同 tick pending 可见性、delayed cancel、ready 落在无后续成交 bar 的真实 tick 时记录 `main_actions_unfilled/no_fill_bar_ahead` 而非静默顺延。
- 更新 `environment_design.md` / `agent_design.md` / Fold Prompt / 模板 README，并重新导出 `configs/prompts/PROMPTS.md`。
- Validation: `tests.unit.test_main_ctx_replay` -> 42 OK；full `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p 'test_*.py'` -> 400 OK；旧语义 grep 无非历史命中；`git diff --check` clean；`docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile .` cached rebuild OK。

2026-06-30 ctx.substep coverage enforcement + GNN Fold rerun

- 将策略执行约束收紧为“实质策略步骤必须进入 `ctx.substep`”：`ctx.broker` action、`ctx.state_dir`、`ctx.nl()` 均拒绝在 substep 外使用；宿主按 `main_wall_s - sum(substep.real_wall_s)` 检查未覆盖策略耗时，超过阈值 fail-fast；strategy import 也有 30s 上限。`0 < budget_minutes < 1` 作为轻量当前分钟步骤，`budget_minutes >= 1` 才延迟到 `ready_at`。
- Prompt、模板和 living docs 已更新：要求所有 research/screening/inference/state/broker/NL 步骤分段包裹；说明 substep 内 broker action 是提交计划，不立即投影现金/持仓；`ctx.state_dir` 只在 substep 内可见，直接访问宿主 state 路径会被 path guard 拒绝。
- GPT-5.5 xhigh 子代理审计发现并已修复：`AT_STATE_DIR` 环境变量绕过、import-time `ctx.nl()`/重计算绕过、state staging 开销被误算为 untracked、Prompt/文档仍残留旧 broker projection 语义、B<1/B>=1 边界缺测试。
- 清理旧测试 sandbox：删除 `.runtime/sandboxes/run_f43ae3e0ced3` 和 `.runtime/sandboxes/run_1035d8ca1531`。新运行保留 `.runtime/sandboxes/run_a7c0c383d1ba` 与 `.runtime/sandboxes/run_fa55845aec77` 供审计。
- Validation: full `PYTHONDONTWRITEBYTECODE=1 ~/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests -p 'test_*.py'` -> 406 OK；Dockerized Fold E2E OK；`git diff --check` clean；`autotrade-sandbox:latest` rebuilt.
- Meta-learning formal run `substep_gnn_meta_20260630` -> `status=ok`, `taste_chars=4241`; Taste 建议用现有 `networkx/scipy/torch` 做轻量图/知识图谱，不新增派生镜像依赖。
- Regular Fold `substep_gnn_fold_20260630` 生成并导入图策略，3 个 5-day valid 分别为 -6.51%、-0.88%、+1.40%，substep 预算均未超时；完整 61-day validation 估算约 80 分钟且实际未在本次 shell 会话内完成，已手动停止容器。结论：Agent IO/substep 轨迹正常，但当前 Fold deadline 不覆盖完整 backtest 墙钟，回放性能/总时限仍需单独治理。

2026-07-01 Formal torch-geometric meta + regular Fold rerun

- 按正式参数重跑一轮元学习和一轮普通 Fold；先重建 `autotrade-sandbox:latest`，再启动 `torchgeo_formal_meta_20260701` 和 `torchgeo_formal_fold_20260701`。普通 Fold 使用默认 60 分钟探索 deadline、季度 Fold、`max_backtests_per_fold=30`、`per_call_timeout_seconds=300`、单 tick 300s、单交易日 900s；普通 Fold Docker `network=none`，GPU 6，镜像来自元学习派生镜像。
- Meta-learning 用户级注入要求使用 torch-geometric。结果 `status=ok`，Taste 3926 chars，并写出 `workspace/sandbox_environment.json` 请求 `torch-geometric>=2.6,<3`；Pipeline 成功构建 `autotrade-sandbox:torchgeo_formal_meta_20260701-epoch_001-ed9e30de1151`，pip 安装 `torch-geometric 2.8.0` 成功。日志 `logs/audit_sessions/torchgeo_formal_meta_20260701.log`，Taste 位于 `experiments/torchgeo_formal_meta_20260701/meta_learning/epoch_001/taste.md`。
- 普通 Fold 成功使用派生镜像和 torch-geometric，训练出 `gnn_model.pt`/`gnn_meta.json`，最终策略为行业虚拟节点 + GATConv 的横截面排序：08:00 `gnn_research` 写计划、09:25 `gnn_execute` 下单、14:57 `gnn_exit` 轮出、固定监控时点 `gnn_monitor` 撤 stale pending。首轮 debug 暴露并修复了未完整 substep 包裹和 `KeyError: ts_code`；最终产物通过 modification/contract check。
- 普通 Fold 只完成非验收 replay：3-day `valid_001` return 0.49%、Sharpe -1.20、回放 229s；10-day `valid_002` return 3.01%、Sharpe 5.58、max drawdown 0.70%、replay_wall_seconds 979s、108 orders / 11 trades / 93 rejects（主要 `insufficient_cash`）。`valid_002` 的 substep 预算未超时，`gnn_research` 9 次、最大约 19.43s，预算 15 分钟。
- 正式实验最终失败且未生成 `experiments/torchgeo_formal_fold_20260701`：Agent 在 deadline 前 `finish_fold`，但没有完整 2021Q4 valid 回测；pipeline 按 `require_complete_validation=true` 拒绝初始 baseline，报 `RuntimeError: initial fold produced no acceptable baseline artifact: ['no successful complete validation backtest in this fold']`。结论与上一轮一致：Agent IO、依赖传递、substep 轨迹正常；阻塞仍是完整季度回放耗时约远超 60 分钟探索窗口/当前流程未保证完成验收回测。
- 资源复查：无运行中 Docker 容器；内存约 392 GiB available；GPU 6 回到约 7.8 GiB / 0%（其余 GPU 为既有任务占用）。本轮保留 runtime sandbox `.runtime/sandboxes/run_caf370907b69` 供审计。

2026-07-05 NL RPC runtime hardening

- 将策略 `ctx.nl()` 与宿主 NL 服务之间的临时 JSONL RPC 从 Agent `workspace` 移到宿主预创建并锁定的 `/mnt/agent/.runtime/nl_rpc/`；request 仅供 Agent 追加，response 由宿主写入、Agent 只读，回测结束删除本次临时文件，目录空时删除 `nl_rpc/`。
- Sandbox 初始化现在预创建只读 `.runtime`，避免 backtest 阶段打开 `/mnt/agent` 父目录写权限；环境文档同步说明临时 RPC 与正式 `results/.../nl_tool/` 审计产物的区别。
- Validation: `tests.unit.test_tools_flow` + `tests.unit.test_nl_scoring` 共 104 OK；真实 Docker 完整 valid smoke 调用 DeepSeek NL provider 成功（`nl_calls=1`、provider call log 2 条、`scope=general`、`nl_rpc` 已清理）；`py_compile` 与 `git diff --check` OK；临时容器和 smoke 目录已清理。

2026-07-06 Broker realism remediation batch

- 修复审计中列出的两融/撮合真实性问题：融券买券还券 T+1、非法 amount 严格拒单、融资股卖出必须走卖券还款、submit-lag/same-tick pending 补 `account/op_type`、uptick 拒单归入信用账户、非正 limit 拒单、维保警戒事件、180 日合约展期、利息 /360、科创板 200 股起 1 股递增、过户费 0.01‰。
- 同步 living docs、QMT 文档、Prompt 和模板 README：off-session 与 transfer 关系、Timeview 可见性验收口径、op32 direct_repay 使用 orderType=1102、Agent 文档去重。
- Validation: targeted 240 tests OK；full `~/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests` -> 483 OK；`git diff --check` clean；PROMPTS.md 已由 `scripts/dev/export_prompts.py` 重新导出。

2026-07-06 Real Docker broker interface smoke

- 启动真实 `autotrade-sandbox:latest` Docker Sandbox（`network=none`），加载 2024-01-02..05 的真实 daily / margin_secs 样本，并用真实宿主 `SimBroker` 跑通普通账户、信用账户、融资、融券、买券还券、卖券还款、直接还款、盘前 transfer、cancel、非法 amount/limit 拒单和 pending 查询。
- Smoke 暴露并修复 pending 字段细节：`close()` 同 tick pending 补 `op_type`，submit-lag pending 优先保留 action 自带 `account/op_type`，`direct_repay()` 同 tick pending 补 `account="credit"` / `op_type=32`。
- Validation: Docker smoke OK（15 orders / 28 broker events，filled actions 覆盖 `buy/sell/credit_buy/credit_sell/fin_buy/short/cover/sell_repay/direct_repay/transfer`，rejects 覆盖 `invalid_amount` / `invalid_limit_price`）；`tests.unit.test_main_ctx_replay` + `tests.unit.test_broker_engine` -> 115 OK；`git diff --check` clean。当前镜像 build 曾超时，smoke 通过 workspace overlay 使用最新 `main_ctx_driver.py`。
