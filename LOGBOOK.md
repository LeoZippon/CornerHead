2026-07-22 修正账本 v2、比较器假阳性修复、日志治理与快照 schema 稳定

- 核实 14 项问题并逐项裁决。修正账本：全部 15,599 行 `api_name==dataset`、三个恒定字段（record_type/schema_version=1/downstream_status）、4,049 条 duplicate_key_rows 中 2,248 条新旧 source_hash 相同——比较器在键重复时未做任何内容比较即判"已修正"。修复：`compare_keyed_frames` 键重复分支先做全表内容等价比较，内容一致不再落账；事件 schema v2 删除 api_name/record_type/downstream_status（保留 schema_version 作演化判别）；账本原子迁移为 13,348 行（清除 2,248 条假阳性 + 3 条新 id 合并），原件归档 SHA `a43a5c8a...c3343`；REVISION_ALERT 改为紧凑单行（event_id/dataset/partition/severity/issue），不再向 cron 日志重复完整 JSON。单文件+尾部增量去重判定为当前规模合理成本，活跃文件≈256MB 时再评估按数据年分片（边界已写入数据文档）。
- margin `trade_date=20260717` 真实缺失已按加锁通道回补（3 行/11 列，sidecar 完整），event_flow 报告重跑后 error→warning/0 errors；凌晨越排程跑审计会把当日尚未发布的 margin 判为缺失，属预期时点问题，按 as-of 0720 出正式报告。
- 快照 schema 稳定（worker 日志 22+21 列被滚动视图丢弃的根因）：决策窗口内无可见行的配置数据集此前不贡献任何列，回放分区含其数据时被 Timeview 丢列。现空窗数据集从最新分区 footer 贡献零行带类型 schema（不进 concat，顺带消除 1323 处 deprecated 空表 concat 告警），缓存格式 v5→v6；pit.read_trade_range 过滤空分区 concat。剩余 FutureWarning 类别为真实宽表中的全 NA 类型化列，parquet 类型使未来推断结果不变，判定不改。
- 日志治理：keepalive ensure 改非阻塞锁静默跳过+时间戳（消除 358 条重复锁行）；dispatch.log 混合格式历史归档（SHA `a6655309...f755`）后从空文件起步，现行 writer 输出统一 JSONL；QMT monitor 改状态转移+每小时聚合日志（附抑制计数）；crontab 备份迁至 archive/crontab/（运维备份非运行日志），安装器路径同步。
- CUDA 静态库按用户裁决全量恢复（编译工具链→完整 CUDA 开发库），镜像重建；runtime_env/容器启动改为先解析不可变 image id 再探针+启动（消除 latest 重指向竞态），benchmark 同步。

2026-07-22 沙箱 CUDA 内核修复、runtime_env 动态化与镜像瘦身

- 证实用户指出的三个问题并全部修复。①PyG 扩展此前确为 CPU-only：docker build 无 GPU，setup 脚本静默降级（scatter/sparse cuda_version()=-1，scatter_max 在 GPU 上抛 "Not compiled with CUDA support"；此前的 scatter_mean 验证走的是纯 torch 合成路径，未触发编译内核）。现镜像 ENV 固定 `FORCE_CUDA=1`（派生镜像构建同样继承，杜绝同类问题上移一层），编译层内置 `cuda_version()>0` 构建期强制断言；期间还修复了 trixie glibc 2.41 与 CUDA 12.8 头文件的 sinpi/cospi noexcept 冲突（镜像内按发行版标准做法为四个声明补 noexcept，附计数守卫，先交互复现+验证再入 Dockerfile）。真实 L20 验证：三个扩展 cuda_version=12080，scatter_max/spmm/knn_graph 全部通过。
- ②runtime_env 弃用静态合同：删除 PYTHON_PACKAGES 硬编码清单（本日已两次人工同步、一次钉版失配，漂移风险实证），Docker 模式改为会话启动前对本次实际镜像做一次离线容器探针（~1s，失败即 fail-fast），发布完整发行版→版本清单（93 项）与工具可用性（含 nvcc）；派生镜像新增包自动出现在合同中，Agent 可见信息更真实完整。schema v2；本地模式同构改为全量安装清单；IMPORTANT_TOOLS 保留为受策展的探针候选集。
- ③镜像瘦身：CUDA 静态库 3.3GiB 对 torch 扩展构建无用（链接共享库），仅保留 libcudart_static.a 与 libcudadevrt.a，镜像 25.1GB→19.7GB（实际文件系统约 13GB）；其余体积为完整离线 DL 环境的真实成本，宿主磁盘充裕且 overlay 层跨容器/派生镜像共享，边际成本≈0，判定接受。
- 验证：全量 tests/ 921 tests + 45 subtests（167.16s，含真实 Docker 路径实跑镜像探针）通过；PROMPTS.md 双次导出保持 `fd74c1fe...` 不变；`git diff --check` 通过；镜像内 driver SHA 与源一致。环境设计文档同步 runtime_env 探针语义与 FORCE_CUDA 说明。

2026-07-22 控制台修复、Prompt 合同纠偏与完整深度学习沙箱

- 修复首页 `consoleCodeBadge` 为 null 时原生 append 渲染出 "null" 标签（filter 后再 append）；「揭示测试结果」改为成功后整页重渲染（原局部刷新不更新页头封存徽章）。Held-out 语义升级：排程内全部 Held-out 区间落账后测试/Held-out 结果自动揭示并触发同等封存（registry 单一谓词，manager 封存门与 detail/list 载荷共用），部分完成不揭示以保留 worker resume 补跑；新增回归测试覆盖部分/完整/封存三态。
- 依据 lzp-test29 证据修正两处 Prompt 误导：taste 出现"推荐季度调仓，匹配 fold period"，根源是元学习 Prompt"适配周期粒度、交易频率"措辞——现明确 Fold 周期只是评估窗口推进节奏、非调仓频率，且与 Fold 周期同级的低频调仓在单窗口内无法被验证（Meta 与 Fold Prompt 同步）。Taste 定位改为"服务研究者方向的可证伪先验"：注入 Fold 时新增框架（硬约束>研究者指令>Taste），容量/参数上限只能是附反证条件的初始默认，Fold Agent 可凭 Validation 证据突破；输出合同与禁止事项同步，agent_design 术语表更新。PROMPTS.md 新哈希 `fd74c1fe...d16252`。
- 沙箱升级为完整深度学习环境：torch 2.5.1→2.10.0（cu128 线最成熟版本），新增 CUDA 12.8 完整工具链（NVIDIA apt 源 SHA1 签名被 trixie Sequoia 策略拒绝，改用 sha256 自校验 runfile，md5 与官方 canonical 一致；TORCH_CUDA_ARCH_LIST=8.9 单架构编译），并预装 torchvision/torch_geometric 2.8 + 源码编译 torch_scatter/sparse/cluster（构建期即端到端验证 nvcc）、transformers 5.14/accelerate/safetensors/einops、lightgbm/xgboost、scipy 显式钉版；PYTHON_PACKAGES/IMPORTANT_TOOLS（含 nvcc）合同同步。镜像 25.1GB；真实 L20 上 cuda 可用、scatter_mean+autograd 通过，lzp-test29 的 PyG 缺 nvcc 失败模式已消除。
- 验证：全量 tests/ 921 tests + 45 subtests（158.53s）通过；node --check、Prompt 双次导出幂等、`git diff --check` 通过；镜像内 driver SHA 与源一致；pipeline_design/environment_design/agent_design 同步。合并入 main 后控制台已重新 deploy。

2026-07-21 全仓一致性复扫与层边界依赖倒置

- 逐类复扫代码/文档/日志/质量产物。核实三个真实边界问题并修复：Environment 反向依赖 TuShare（snapshot/broker 引 tushare.common/io）——竞价容差、观测起始日、打板数据集清单移入 environment/data/contracts.py、sidecar 读取移入 data/pit.py，ingest 适配层反向引用环境合同，environment 现零 data_sources 依赖并新增边界守卫测试；explore.py 迁至 agent/（唯一消费方为主会话 Runner）；style_analysis.py 迁至 replay/style.py。补充发现 nl/extraction.py 实为 provider 响应解析且 NL 栈从未使用，迁至 llm/extraction.py。
- 跨模块下划线私有导入全部转正：backtest 的 formal 产物只读开关（finish_fold 使用）与 read_replay_auction/nl_call_budget（replay_benchmark 使用）；脚本自带 _optional_float。判定为不改的项：timeview/sandbox 两个 _link_or_copy 语义不同（copyfile 覆盖 vs copy2 copytree 回调）、三个原子写各对应 JSON/文本/parquet 三种内容、broker/driver 动作表重复受 stdlib-only 驱动边界约束、experiment/interactive 循环形态属行为重构不在本次范围。
- 运行产物核查：results/data_quality 八份 JSON 全部通过自身 v2 reader 校验，修正账本 15,135 行 35 字段全同构、抽样 event_id 全唯一，无需修复；六份设计文档共享统一骨架（相关边界/职责边界/术语说明/导航/编号章节），仅小节深度随内容量差异，判定合规；parameters_reference 一处旧模块简称更新。日志布局统一：qmt_live_monitor.log 迁入 logs/qmt/（脚本与部署文档同步），39 份已删实验遗留的 webui 会话日志归档（archive/logs/20260721_webui_session_logs.tar.gz，SHA-256 `e2070efb...`）后清除，logs/ 现仅 qmt/tushare/webui 三个已文档化目录。
- 验证：compileall 通过；双向导入顺序冒烟（data_sources↔environment.data 无环）通过；全量 tests/ 920 tests + 45 subtests（158.25s，含新增边界守卫）通过；Prompt 双次导出 SHA-256 保持 `821a608b...c4d6a3fe`；`git diff --check` 通过。合并入 main 后控制台已重新 deploy。

2026-07-21 分支合流至 main 与单主干化

- 经用户确认合流：工作主干 fix/case-study-remediation（领先 origin/main 301 提交）合并入 main，三方基底与 origin/main 树逐字节一致故零冲突；PR #1（environment 重构）改基至 main 后合并（79598d6）。GitHub 默认分支切换为 main，此后只维护 main。
- 分支清理：36 个本地分支与 2 个远端分支全部删除；唯一未合并分支 fix/tushare-daily-update-policy-20260601 的实质内容（PR 拆分指南四条）经逐行比对确认已由后续 docs 分支进入 main，按已废弃处理。现仓库仅存 main / origin/main。
- 兼容回退项返工完成：沙箱镜像按合并后 main 重建，`/opt/at_runtime/driver.py` 与源文件 SHA-256 一致（`4666a61b...`）；受管 console 经 `webui_stack.sh deploy` 切换至 main 代码（PID 2943783，`code_current=true`，版本 79598d6），tunnel PID 828388 保持不变，无运行中实验。
- 验证：合并后 main 全量 tests/ 919 tests + 45 subtests（150.79s）通过，仅既有 warning；工作树干净。crontab 指向的检出即 main，夜间数据任务按新布局代码运行。

2026-07-21 environment 包结构重构与命名统一

- 六路全量读档确认 src/autotrade 分层依赖严格无环，问题集中在 environment/ 顶层 28 个条目混排五个子系统，以及 web_fetch/web_search 在顶层（宿主服务）与 tools/（Agent 工具合同）双层同名。按"子包=多模块子系统、顶层模块=单一职责组件"重构：新增 web/（fetch/search 宿主服务）与 replay/（engine/driver/market/stats/timeview/state_staging 回放核心）；误名的 features/ 并入 data/，data/ 同时吸收 snapshot、research_release 与 data_summary→summary，成为完整 PIT 数据子系统。broker/broker_core、sandbox 四件套等命名清晰的单模块保持顶层不动，不拆分热点大文件。
- `to_cn_timestamps` 从 snapshot 下沉到 data/pit，timeview 与 nl/retrieval 不再为一个时区助手拉取 2000 行 snapshot 模块。同批边界小修：agent 层跨模块下划线私有导入转正（compact_mapping、drop_leading_orphan_tools）、deepseek 冗余导入删除、tools/__init__ 补齐 ArtifactIOTool 惰性导出。仓库内除 tests 的 mock 字符串外无任何模块路径字面量，历史 logbook 不改写。
- 沙箱镜像因 driver 烘焙路径改为 `/opt/at_runtime/driver.py` 而重建（缓存层复用），镜像内与源文件 SHA-256 一致（`4666a61b...`）；验证完成后共享镜像已按待评审的 base 分支回建以保持在线机器自洽，合并后需再执行一次缓存重建。无运行中实验，受管 console 进程不受磁盘改名影响，下次显式 deploy 生效。
- 验证：compileall 全量通过；35 个关键模块导入冒烟通过；全量 tests/ 919 tests + 45 subtests（161.66s，含真实 Docker Fold/沙箱生命周期）通过，仅既有 warning；Prompt 双次导出 SHA-256 保持 `821a608b...c4d6a3fe` 不变；`git diff --check` 通过。验证前后可用内存 427→430GiB，GPU 占用未变。

2026-07-20 控制台加载最终代码的可验证部署

- 现场证据定位为部署顺序与版本盲区，而非 nginx/路径故障：远端 SPA 三个文件 hash 与本地完全一致且 `Cache-Control: no-cache`，console cwd/解释器也指向当前仓库；但 PID 1716086 于 03:37:44 启动，`main_ctx_engine.py` 最终修改发生于 03:41:04。旧 health 不暴露进程加载版本，动态字段又只返回未变化的 HEAD `77979f5`，因此进程早于最终源码却仍看似“当前”。
- `repo_code_version()` 现对 dirty tracked diff 和 untracked 文件内容生成短 hash，console 在创建 app 时冻结加载版本并与 30 秒缓存的当前版本比较；health、列表、详情、轻量 status 及 SPA 均暴露/提示 `code_current`。`deploy` 只有在 UDS 可用且两者相等时才成功，`status` 明确报告 current/stale。
- 受管 console 及其继承环境的 worker 以 `-B` + 禁写 bytecode + 仓库外空 cache prefix 启动，不读写仓库 `.pyc`。没有加入 autoreload、文件 watcher、额外 daemon 或自动中断 worker；完成修改后一次显式 deploy 仍是唯一切换点，保持控制面简单且不加载编辑中的半成品。
- 服务/回放定向 135 tests 与全量 905 tests（161.594s）通过；构造同 mtime/同大小 stale pyc 的实测为普通启动读取旧值、受管启动读取新源码。Bash/JS 语法及 `git diff --check` 通过；真实 `deploy` 后 health 的服务版本与仓库版本相等、`code_current=true`，tunnel 保持原 PID 且无运行中实验；验证前 428GiB 内存可用，GPU 占用未变。

2026-07-20 统一固定交易分钟时钟与可选行情事件

- 确认真实架构缺口：`include_intraday=false` 本应只关闭分钟行情，却通过“无分钟文件”隐式把回放压缩为 09:30/15:00 两个日线事件，使 PIT、substep、状态、订单年龄和 14:57 收盘竞价都丢失中间时钟。取消这条数据可用性→时间结构耦合，不保留第二套日线 timeline。
- 所有正式策略日现共用 242 个固定交易分钟（09:30–11:30、13:00–15:00）和 09:15/09:25 竞价 tick；分钟条只是可选市场事件。订单按固定分钟激活，空分钟可报单/撤单且 `ctx.price()` 为空，工作单等待下一事件；无分钟行情时仍在 09:30 日线开盘、15:00 日线收盘撮合，14:57 固定提交收盘竞价。
- 部分分钟日同时补齐缺失的 09:30/15:00 逐票日线事件；`intraday_decision_minutes` 只过滤 `main(ctx)` 调用而不减时钟，`replay_granularity` 统一报告 `minute`。Agent facts、Fold/Meta Prompt、模板、环境/参数文档和前端参数帮助同步，没有新增配置、调度器或独立引擎。
- 定向 385 tests 与全量 902 tests（144.148s）通过；修改 Python 编译、JS 语法、Prompt 双次导出（SHA-256 `821a608b...c4d6a3fe`）和 `git diff --check` 通过。CornerHead console 已部署为 PID 1716086，既有 tunnel PID 828388 保持不变，本地/API/前端端到端健康且无运行中实验 worker。

2026-07-20 固定竞价合同与冗余配置清理

- 全量核对 196 份持久化 run/host manifest（含 Sandbox 副本共 408 处 JSON 字段）后，四项配置唯一取值始终为 `True / 09:15 / 09:25 / 14:57`；9 份实验 `params.json`、CLI、HITL defaults 和新建实验前端均无覆盖入口，确认它们只是重复传递固定市场合同。
- 删除 `ExperimentConfig` 的 `auction_enabled`、`auction_preopen_time`、`auction_decision_time`、`auction_close_time`，并移除 manifest、Agent facts、runtime 公开视图、正式回测和 benchmark 的对应 plumbing；回放引擎固定保留 09:15→开盘竞价、09:25→首根连续 bar、14:57→15:00 收盘竞价语义，不新增兼容分支。
- 前端参数 schema 继续不暴露这些字段，并在日内决策粒度帮助文案明确固定时点；活文档、Prompt 样例和负向回归同步。轻量独立审计未发现高价值遗漏或行为回归。
- 定向 215 tests、独立审计 136+9 tests、全量 896 tests（123.058s）、前端语法、Prompt 双次导出（SHA-256 `5e20e948...cbe67b0`）及 `git diff --check` 通过；未启动或修改真实实验。

2026-07-20 Fold Prompt 稳定前缀与核心合同重构

- 参考本地 Claude Code Prompt 装配并经独立高需求审计确认，只采纳“稳定协议前置、动态上下文置尾、工具 schema 单源”三项；不引入 section registry、memoization、provider 专属 cache marker、附件系统或额外 Agent 读取门。
- Fold Prompt 新增前置核心执行合同，保留 Test/PIT/单位、position 键、substep/state、竞价/延迟/T+1、离线资源和完整 Valid/hash 收尾；工具参数表、18 行路径表、罕用 Broker 细节和样例下沉到现有 native schema/只读 README。稳定协议由 21,520 缩至 15,466 字符。
- run facts、Step 树、Taste、实验/Fold 指令和阶段策略统一追加到“本 Fold 动态上下文”；不同事实、指令、验收规则和 phase 的 Prompt 在该边界前逐字节相同。lzp-test24 实际 Q1 资料重构渲染约 32,487→26,203 字符（原始记录为 64,276）。
- 定向 187 tests、全量 896 tests（122.034s）、Prompt 双次导出（SHA-256 `83ab3874...b5abf2c`）和 `git diff --check` 通过；文本/装配变更不需要 Docker 或真实实验，未改动运行中实验状态。

2026-07-20 lzp-test26 Meta 主题与系统 Prompt 合同修复

- Agent Trace 确认 `fold_exploration_directive` 只进入普通 Fold，lzp-test26 首个 Meta manifest/Prompt 完全缺失“事件驱动 + 知识图谱 + GNN + 自然语言”主线。现已将该指令独立透传至 Meta manifest/账本、Runner、Prompt 预览与运行事实，不与单会话 `meta_learning_directive` 混合。
- 精简修正四项提示合同：普通 Fold 不得下载/安装外部模型；compact Test 只作多 Fold 失效诊断，所有产物/参数选择仍只看 Validation；`intraday_trade_days` 只限制决策 snapshot，不限制完整 Valid replay；09:15 参与开盘集合竞价，09:25 新单进入首根连续 bar。不新增模型下载器或脆弱的 Taste 关键词拦截。
- 完数据 profile、单位表、包/CLI 清单和静态路径仍保留在可审计 JSON，但不再重复内联进系统 Prompt。lzp-test26 实际 Meta Prompt 重构渲染从 40,196 字符降至约 17,216（-57.2%），同时包含主题与全部修正合同。
- 定向 186 tests 与全量 895 tests（121.457s）通过；Python/JS 语法、Prompt 双次导出（SHA-256 `f9f05620...8aaf27`）、`git diff --check` 通过。验证前后可用内存均为 421GiB，GPU 占用未变。未修改/重启 lzp-test26 或任何实验状态。

2026-07-20 历史 Fold 重跑回退与分区安全审计

- 回退未提交的“任意历史 Fold 重跑并删除其后结果”组合功能，恢复低复杂度边界：只允许重跑最新完成 Fold；更早历史修正继续走独立回滚，再按正常排程前进。前端、控制面、测试和活文档同步移除该入口与语义。
- 三路只读审计分别覆盖控制面/产物完整性、Agent/评估边界、运维/性能。修复真实高优先级缺口：auto 会话强制终止后可绕过重批、auto 模式最新 Fold 重跑不进入可编辑批准门、`launching` 窗口仍允许回滚/重跑；未增加新状态机或交互实体。
- 以低复杂度补齐四项有利防线：Meta 正则化产物在下一普通 Fold 完整 Validation 通过前不得作为 fallback/Test，通过后清除临时门槛且连续运行/恢复语义一致；回滚先唯一备份 ledger 并校验产物目录归属；worker 按独立进程组终止并回收残留容器；控制台日志目录私有且部署等待 UDS 健康。未引入守护进程、自动 GC、额外数据扫描或常驻监控。
- 全量 894 tests（最终 121.893s）、Python/JS/Bash 语法、Prompt 双次导出（SHA-256 `665f61fe...a0bbe3a8`）和 `git diff --check` 通过。仅部署 SPA/API：console PID 774113、tunnel PID 828388；`lzp-test24` 保持 stopped/manual，`lzp-test25` 保持 waiting_user/step。
- 仓库外仍有一个需宿主管理员处理的高风险：共享机 root 运行的旧 `cloudflared` 把认证材料放在进程参数中。未读取/记录该材料，也未越权停止进程；应确认用途后停用旧进程并轮换凭据，改用权限受控的凭据文件或服务配置。

2026-07-20 lzp-test24 缓存污染恢复与默认 Fold 探索方向

- `epoch_001/fold_2024Q2` 在安装父策略时因 Q1 冻结产物内新增 `__pycache__/candidate.cpython-311.pyc` fail-fast；仅删除该生成目录后，产物 hash 恢复为账本原值 `sha256:1ab0334ac1e4ec12dc96adc96afbeedb697137abeb95aa6bcce646861b5afc8f`。保留完整性拒绝，不加入掩盖污染的自动清理；宿主分析应使用 `python -B` 或 `PYTHONDONTWRITEBYTECODE=1`。
- 新建实验弹窗增加可选“默认 Fold 探索方向”；配置、CLI、Prompt 预览/运行装配、manifest/账本和详情展示全链路透传，与单 Fold 追加指令分离。Prompt 明确它是可证伪方向，Agent 可据证据降级或拒绝，完整系统 Prompt 覆盖语义不变。
- 为既有 `lzp-test24` 向 Q2 及其余 14 个 Fold 注入“事件驱动 + 知识图谱 + GNN”详细方向，经控制台恢复并批准 Q2 后切回自动模式；新 run `run_397dc3e9f048` 正常运行、心跳新鲜且 Trace 已产生 96 个事件，冻结产物和当前 Sandbox 均无 `__pycache__`。
- 相关模块 189 tests 与全量 892 tests 通过；Python/前端语法、Prompt 导出幂等和 `git diff --check` 通过；控制台本地 API 与前端端到端健康。

2026-07-14 Fold 结果过渡布局与策略软诊断（fix/auction-pit-performance）

- 修复 Fold 账本已写、后分析未结束时实时 Trace 与结果卡无间距；验收警告按收益百分比/Sharpe 两位小数显示，旧账本由前端只读格式化。
- 修改检查识别 09:15/09:25 分支直接或经一层 helper 调用 `ctx.price()`；零订单时返回针对性 advisory，仍不影响回测、验收或冻结。
- Pipeline config、ToolFlow 与 WebUI backend 相关 113 tests 通过；前端语法和 `git diff --check` 通过。

2026-07-14 数据更新与实验输入解耦（fix/auction-pit-performance）

- 新实验按需把 committed raw/PIT 发布为共享 hardlink research release，并保存实验 pin；更新锁忙时立即复用上一完整版本，所有 Fold、恢复和交易日历保持同一 generation。
- 真实 281,114 个 raw Parquet/sidecar、754 个 PIT 文件全量路径/inode 对照通过；7 个质量状态独立复制并校验 hash，打板状态作为 warning 接入 Snapshot。
- 首发 9.83 秒、增加约 23.3 MiB/232 inode；复用约 0.0056 秒。02:30 审计占锁时新 pin 约 0.0055 秒完成，不等待、不读半更新数据。
- Full suite 同时捕获并修复 driver 导入时改写宿主 `os`/`Path` 的副作用；路径守卫现仅在独立回放进程启动时安装。定向 95 tests、全量 771 tests、CLI/前端语法和 `git diff --check` 通过。
- 不改 cron/Docker，不增加数据湖、Fold 预启动或同步 GC。

2026-07-13 Experiment observability and hidden-evaluation audit

- 修复 HITL run/deadline 串线、轮询刷新、活跃预算文案/回补、Trace 重挂滚底与长回测首日进度；死亡的 ask_user 等待会转为 interrupted。
- 策略宽表读取、吞异常、峰值内存和零订单只返回 advisory，不新增验收硬门槛；隐藏 Test 从开发容器移除且最终评估时暂停至容器销毁。
- lap-test9 数据 generation/PIT/schema 正常；完整 Valid 61 日 1169.4 秒、0 订单源于策略 Path 类型错误被宽异常吞掉。受影响模块 394 tests、全量 743 tests 均通过；基础镜像已刷新并验证新 runtime driver。

2026-07-13 Snapshot/Replay 构建性能优化（fix/auction-pit-performance）

- 竞价分钟修正改为唯一值字典编码后批量展开；真实 111 万行修正由 1.03 秒降至 0.40 秒。
- Replay 分钟改为逐日读取/修正并流式写单文件；真实 2112 万行耗时 32.9 秒、峰值约 2.5 GiB，两日结果与旧路径逐列一致。
- 独立数据域固定最多 2 路并行并保留 daily→intraday 依赖；交互式 Fold 只在人工门控期间预取数据缓存，进入 Fold 前 join，不创建容器且不与回测并行。
- Full suite 731 tests OK in 118.9s；测试后主机仍有 433 GiB 可用内存，8 张 GPU 均空闲。

2026-07-13 项目开发原则精简落文档（fix/auction-pit-performance）

- `CLAUDE.md` 新增三条唯一原则：正确完备后保持最简；现实合同优先后优化实测热路径；由环境承担机械复杂度并在 PIT/安全边界内给 Agent 最大自由度。

2026-07-13 空竞价 schema 回测修复与共享机资源审计（fix/auction-pit-performance）

- `lzp-test8` 首个 Fold 证明：0 行 `auction.parquet` 被写为 Arrow `null` 列，带字符串日期过滤时在策略执行前报 `null vs string`；空策略和父基线同样失败。
- Snapshot 现写固定 string/double 空 schema；Backtest 对已有 0 行缓存先查 footer 并返回无竞价数据。现有失败缓存实测通过，full suite 726 tests OK。
- 主机 192 逻辑核/503 GiB，采样时 99% CPU idle、433 GiB 可用；实验热路径约单核、宿主峰值 RSS 约 52 GiB。维持 19.2 vCPU/50 GiB 容器配额，先向量化，再仅用 4–8 核有界并行，不并发两个重型分钟构建。

2026-07-13 Formal 回放隔离、Probe 脱敏与回测热路径优化（fix/auction-pit-performance）

- Probe/Valid/Test/Held-out 改为一次性 formal 容器；开发容器在完整工具调用期间 pause，workspace/阶段槽/结果不挂载；pause/unpause 不确定状态会终止会话。
- Probe `ctx.nl()` 返回 `withheld_probe`，动态 substep 名与所有异常类统一脱敏；竞价结果 tick/不可撤阶段合同和 ask_user attempt nonce 补齐。
- state 合并以 dirfd/O_NOFOLLOW/O_NONBLOCK 防换链与 FIFO，单文件64 MiB；竞价量价差超过0.005元三层 fail-fast。
- Probe 日期谓词读取、分钟日切片、Timeview next-boundary、流式 hash/大表 footer profile 与加载 telemetry 落地；full suite 723 tests OK，实际 Docker lifecycle OK。

2026-07-13 数据刷新、世代一致性与正式容器基础边界（fix/auction-pit-performance）

- 晚间通用任务不再写 `stk_auction`，23:20 走强制严格复核；竞价量价校验、cron 安装 marker/全文核验和 job-scoped config identity 补齐。
- Fold/Meta/Held-out 输入启动前强制同一 raw generation；Snapshot cache 增加显式格式版本、label-neutral manifest 和跨进程 single-flight。
- 增加一次性 formal 容器挂载白名单与开发容器 pause guard，visible state 只读；数据/刷新/cron/pipeline/Docker 定向回归通过。

2026-07-13 Replay 缓存去重与跨进程 single-flight（fix/auction-pit-performance）

- Replay 内容键不再包含 valid/test label；同区间只构建一次，label 以新 inode 原子覆盖到本次 out_dir manifest，不改写缓存或已硬链接的其他运行。
- 同 cache key 以 `flock` 跨进程合并构建并在获锁后复查；原子 rename 异常不再被当成竞态吞掉。Pipeline/config 回归 60 tests OK，`git diff --check` 通过。

2026-07-13 数据 generation 失败封锁与刷新合同修复（fix/auction-pit-performance）

- `.raw_generation.json` 升级为兼容旧格式的 v2 状态机：mutating job 先标 updating，失败为 dirty，只有同 job/区间/命令可精确恢复；Snapshot/cache 对非 committed fail-closed。下载子进程继承 flock，runner 被杀后不再失锁继续写。
- revision sentinel 不再推进 generation；08:50 job 补抓 kpl_concept_cons，并新增“刷新映射必须由对应 job 实际下载”的漂移测试；02:30 text audit 使用保守截止日，避免在 08:55 回补前固定报红。
- 数据源、Snapshot 与刷新节点定向测试 102 OK；JSON 与 diff check 通过。未运行真实数据更新。

2026-07-13 修复 Meta ask_user 公开入口透传（fix/auction-pit-performance）

- ExperimentPipeline.run_meta_learning 现在把 user_question_hook 原样传入内部 Meta 会话；此前内部接线完整，但公开 wrapper 漏参，交互模式会错误退化为 unattended。
- 新增真实公开入口回归，验证同一 hook 进入 ToolContext、可被调用并返回研究者答复；定向测试 1 OK。

2026-07-13 飞书通知升级为交互式卡片（feat/step-tree-rollback）

- FeishuBot 新增 send_card（interactive 卡片：彩色标题 + lark_md 正文 + 可选 URL 按钮；纯展示型，无需回调基础设施——带回写动作的按钮需要公网回调端点，刻意不做）。
- 三类通知全部转卡片：决策提醒（待批准橙/提问蓝/失败红，正文 实验/会话/进度 加粗字段；.env 配 CONSOLE_BASE_URL 后附「打开控制台」按钮）；成交通知（A 股配色：买入红/卖出绿，量价金额加粗）；链路告警（红）。两张样例卡片已实测送达群。
- 监控守护已重启生效；full suite 657 OK（新增卡片 payload 结构断言，原文本断言全部迁移到卡片契约）。

2026-07-13 客户端内文件桥（弃用 miniQMT）+ 提醒消息增强 + 模块精简复查（feat/step-tree-rollback）

- **qmt_client_bridge.py（大 QMT 内置 Python 3.6、仅标准库、单脚本）**：一个 5 秒 run_time 回调同时完成 ①实时导出（恒开启：原子快照 + 增量 orders/deals JSONL，m_ 字段归一化为监控端消费的键，去重状态持久化重启不重发）；②订单执行（配置闸门默认关：轮询 inbox 显式订单 payload（schema_version 2），校验白名单/当日/整手/名义上限/交易时段，三重独立闸门 enabled ∧ execute ∧ confirm==payload_id 全真才 passorder，幂等 remark 对照柜台当日委托，结果回写 execute_/error_，payload 归档）。**仓位测算留在决策侧**（本机已有同步账户快照），客户端只收显式 code/side/volume/price——符合部署文档"决策侧生成、执行侧落地"分工。柜台 opType/prType 映射全部配置化（开放问题 #4，实测不符只改配置）。已部署 C:\xquant 并双端语法验证；标准配置骨架已放 config\qmt_bridge.json（待填真实账户 ID）。README 重写为完整导入/配置/payload/交易日测试指南（含建议测试顺序：只读验收→dry_run→最小单实测→幂等重投验证）。
- **消息增强**：决策提醒全部改为多行结构（标题 emoji + 实验/会话/进度 completed/total + 详情行）；监控端成交方向映射兼容 xtquant(23/24)、客户端内(m_nOffsetFlag 0/1/48/49)与 BUY/SELL 三种编码。
- **精简复查（miniQMT/飞书/连通模块）**：删除 qmt_realtime_export.py（xtquant 方案废弃，远端副本一并清除）与 qmt_readonly_bridge.py（被新桥只读默认态完全吸收）；全仓库引用清扫（monitor docstring、deployment 文档 4 处）；notify/live 模块职责单一（feishu=发送、qmt_monitor=同步+去重+格式化、脚本仅是薄壳），未见冗余。
- full suite 656 OK；deployment 文档 §2/§6 对齐新架构（随用户在途文档批次提交）。

2026-07-12 强制终止反馈 + 飞书决策提醒 + QMT 实时导出/同步/成交通知（feat/step-tree-rollback）

- **强制终止反馈**：SIGKILL 升级后管理端在 status.json 落 `terminated` 终态（新徽标「已强制终止」，纳入可恢复状态）；终止按钮先提示"正在终止（宽限约 10 秒）"，完成后按响应提示优雅退出或强杀（sendControlAction 支持结果函数式提示）。此前 SIGKILL 后状态残留 running、无任何回馈。
- **飞书决策提醒（bot cli_aad99084d2b9dce9，已实测连通）**：StatusReporter 新增状态迁移钩子（守护线程、best-effort）；交互式 worker 在 waiting_user（会话待批准）、waiting_step_user（Step 门控，附验证收益）、waiting_user_reply（Agent 提问，附原文）、failed（附错误）时向群 oc_8c39…e30c 推送。凭据在 .env（FEISHU_*），缺失自动禁用。已向群发送两条连通性测试消息确认双 bot 可达。
- **QMT 实时导出+同步+成交通知（bot cli_aad9886d7a381cef）**：实测确认 QMT 节点为用户自建 xtquant/miniQMT 执行体系（C:\xquant\qmt_executor.py，Python 3.8）。新增：① `ops/qmt/qmt_realtime_export.py` 部署至 C:\xquant（沿用其 CQ_* 约定，只读、无网络、敏感字段脱敏），周期原子写 account_snapshot.json + 增量 orders/deals JSONL（traded_id/状态变化去重，重启不重发），MiniQMT 断开写 ok=false 快照退避重连——已远端语法验证 + 12s 实跑（当前客户端未登录，正确产出错误快照）；② B 侧 `qmt_monitor.sh` 守护（scripts/live/qmt_live_monitor.py，核心在 src/autotrade/live/qmt_monitor.py）：20s scp 拉回 data/qmt_live/ + 每笔新成交推送（量价/金额/委托号+账户资产/持仓摘要）+ 链路告警按错误去重推一次——已实测拉回真实错误快照并向群推送告警，守护已启动；③ ssh 身份外置 .env（QMT_SSH_DEST），仓库不存节点身份。
- 待用户操作：QMT 客户端登录后在 Windows 侧启动 `C:\xquant\Python38\python.exe C:\xquant\qmt_realtime_export.py`（可 schtasks 自启，见 ops/qmt/README.md）；多账户需设 CQ_EXPECTED_ACCOUNT_ID。
- full suite 656 OK（新增 8 项：飞书 token 缓存/失败吞噬/from_env、监控去重/告警去重/消息格式、StatusReporter 迁移钩子、决策文案）；控制台重启+同步；deployment 文档新增 §6（随用户在途文档批次提交）。

2026-07-12 中文选项标签 + 值守语义澄清 + 冗余复查（feat/step-tree-rollback）

- **中文选项标签**：`choice` 字段补 Chinese `choice_labels`——NL 失败策略（"返回可审计错误，策略自行降级（推荐）"/"任一 NL 调用失败即终止回测"）、Fold 周期、推理力度、元学习联网；模型名保留原文。数据集子集 chips 也评估后采用中文名：标签取自 tushare_update_schedule.json 每接口描述的首个分句（注册表本就是数据集运维事实源，零新增维护面；覆盖 100%，缺失时自动回退 API 名），chip 悬停提示保留 API 名便于对照文档；板块 chips 同步中文（主板/创业板/科创板/北交所）。
- **值守语义**：Agent 无法预知研究者是否在线——判定在环境侧：ask_user 钩子只存在于交互式运行；调用时宿主读 control.json，mode=auto → 立即返回 unattended，manual/step → 挂起等研究者答复（选择了值守模式=承诺值守）。CLI 运行无钩子恒 unattended。例程指引措辞已按此机制改写（"直接提问，环境判定值守"，替代"研究者在线时提问"）。
- **冗余复查**（本会话全部改动）：interactive.py 两个死导入（Mapping/SnapshotConfig）删除；registry.experiment_detail 三次读 control.json 去重（复用已读对象）；app.js 六处重复的 control-POST 闭包合并为 sendControlAction 单一助手（含 reveal 弹窗）。其余新增经复查均为必需（PIT/安全/数据完整性），未见可削减项。
- full suite 648 OK；PROMPTS.md 重导出；控制台重启+同步。

2026-07-12 四项跟进：筛选布局 / NL 失败反馈 / 数据域过滤 / ask_user 核查（feat/step-tree-rollback）

- **ask_user 核查（lzp-testauto trace 实证）**：接线全部正确——worker 运行最新代码（code_version=1ee3e8c），首次 llm_call 的工具表已含 ask_user，step 模式钩子在位。未提问的原因：①回测执行中 Agent 结构上无法调用任何工具（backtest 同步阻塞，属设计）；②该 run 至今只跑了探针（replay_window=5，其一失败），尚无成功完整验证 → step 门控还没到触发点；③ask_user 是 Agent 自主选择，模型偏保守。已在 Fold 例程加一条轻量指引（真正的方向分叉→附分析建议提问一次；unattended 不再问），不强制。注意：运行中的 lzp-testauto worker 用的是旧提示词，重启后生效。
- **NL 失败反馈**：失败的 nl() 返回解释性 `feedback`（原因+退化建议，按 state 区分：配额耗尽/未配置代理不要重试，超时/偶发失败可后续 tick 重试一次），status/state/error 保持稳定；提示词与 env 文档同步。
- **数据域过滤**：创建表单新增「数据域」组——5 个域开关（事件/宏观/文本/财务/分钟，关闭=决策快照与回放槽均不加载，分钟关闭回放退化日线）+ 4 个高级数据集子集多选（全不选=该域全部默认集，未知名称 fail-fast）。build_snapshot_config 统一派生（域关→空集+replay_include_*=False）。
- **筛选布局**：multi 字段支持 `wide: false`——板块范围（4 chips）改占半行，与「剔除 ST」同排（权衡：两者同属成员资格筛选、语义配对，4 个短 chip 半宽不换行；窄屏本就单列无回退问题）。
- full suite 648 OK（新增域过滤+NL feedback 断言）；PROMPTS.md 重导出；控制台重启+同步。

2026-07-12 用户裁决落地：P1-6 性能 / P1-7 测试封存 / P1-8 最小化 / P2-3 删回退 + 沙箱清理（feat/step-tree-rollback）

- **P1-6 Timeview 性能**：roll 从整列布尔掩码（每次节点跨越 O(行数)，分钟域 4400 万行）改为 available_at 排序游标 + searchsorted（O(log n)，分片内容与原实现逐行一致）；冻结域空表/schema 探测改读 parquet footer（不再整读 GB 级文件）；`to_cn_timestamps` 加统一 +08:00 快路径（避开 ZoneInfo 逐行 tz_localize，5M 行 32.8s→3.0s，≈11×）；phase_seconds 拆分 `timeview_init`/`timeview_roll`，回放墙钟起点移到构建之前（覆盖完整生命周期）。合成基准：5M 行 roll 701 tick 共 0.06s，可见行数与暴力参考一致。
- **P1-7 测试结果封存**：控制台默认全隐藏测试/Held-out（首页汇总、逐 Fold 摘要、测试明细 test_audit、测试/Held-out 权益曲线全走 `test_results_revealed` 单闸）；新增「揭示测试结果」动作（确认弹窗）——揭示不可逆、实验封存，13 类学习类控制动作（批准/重跑/回滚/逐 Step 放行/答复提问/指令/Prompt 覆盖/父产物覆盖/提前收官/resume/restart 等）统一拒绝，查看/停止/删除保留；legacy 实验（无控制面）只读全示。
- **P1-8 最小修复（按用户裁决不加硬验收门槛）**：broker 区间末 close_all 逐仓位记 `exit_liquidated_by_host` 事件 → 统计 `host_exit_liquidation_count` 进 detailed_return 与回测 summary（探针也返回，属生命周期统计）；提示词新增跨周期生命周期指引（计划带调仓周期键、对照 Broker 真相源做卖出/再平衡、宿主强平只是安全网）。
- **P2-3**：请求时读 mutable raw 的 `_legacy_benchmark_year` 回退整体删除（含 server 线程参数与旧测试）；历史图表只读冻结 rollup，缺失基准=仅策略曲线。旧实验已全部清理，无迁移对象。
- **沙箱清理**：delete_experiment 的 work_root 删除改 `_remove_sandbox_tree`——rmtree 残留（rootless docker subuid 文件宿主删不掉）时经沙箱镜像容器（userns root）rm -rf 后重删，未删净则报错；work_root 缺 params 时也按实验名推导删除。实测：经 API 删除 lap-test2/lzp-test（旧代码留下 subuid 残留），新清理器把 `.runtime/sandboxes` 下 8 个目录全部清空（含 8.6G lap-test2）。
- **原则复核**（最小护栏/最大自由/精简）：本批与前两批的新增约束均为 PIT/宿主安全/数据完整性必需（探针脱敏、staging 收容、删键阻断）；P1-8 明确不加验收硬门槛、只加可见性；P1-7 是用户点名的产品级隔离；未发现可再削减的 Agent 侧限制。
- full suite 647 OK；PROMPTS.md 重导出；控制台重启+静态同步。

2026-07-12 lap-test2 逐 Step 门控无提示排查 + ask_user 工具 + 过期代码徽标（feat/step-tree-rollback）

- **排查结论（trace 实证）**：lap-test2 的 worker 于 bbebfd3（复活 step 模式的修复批次，00:34 落地）之前约 36 分钟启动，长驻进程全程运行旧代码——门控序号按全部回测计数（#7）、stop 在门控处被旧 catch-all 吞掉两次（19:38、21:18 两个 ExperimentStopped 变成喂回模型的工具错误），Fold 照常 finish + 冻结测试跑到 22:51 才停。且用户按 stop（01:25）时 Agent 只跑了 6 个探针（replay_window=5）——探针按设计不触发门控，所以此前确实"从未提示"。当前代码行为已验证正确：正式验证必挂起、stop 立即中止、序号只计成功正式验证。
- **修复 1：过期代码徽标**：worker 启动时把仓库 HEAD 写入 status.code_version；服务端暴露当前 HEAD（30s 缓存）；详情页在 worker 存活且版本不一致时显示「代码过期」徽标（悬停提示重启生效）。防止"改了代码但长驻进程没重启"再次无声发生。
- **修复 2：`ask_user` 工具（用户方案 2）**：Agent 可在关键分叉点暂停，提交方向性问题+现状总结（≤4000 字符），status=waiting_user_reply；研究者详情页答复（reply_question，空答复=放行自决），答复以"研究者答复"标注注入工具观察；等待回补推理预算并写 ask_user trace 事件；stop 在等待处立即生效；auto 模式/CLI 无人值守立即返回 unattended。控制字段 user_replies["<session>#q<n>"]，重跑/回滚清除对应会话答复；会话列表挂起徽标「提问待答复」。fold 与元学习会话均可用。
- 方案 1（每次成功回测强制挂起含探针）未采用：P1-1 后探针只返回耗时/生命周期统计，无可评审内容，强制挂起只有打断成本；正式验证的确定性挂起已由 step 门控覆盖（等待同样不计预算）。
- full suite 646 OK；PROMPTS.md 重导出；控制台重启 + 静态同步。

2026-07-12 check.md 审计 P1/P2 修复批次（feat/step-tree-rollback）

- **P1-1 探针脱敏**：replay_window 探针回放的是决策时点的未来窗口，此前完整返回收益五项+benchmark 并把 detailed_return/style/orders/positions 写进 Agent 可读结果目录（真实 Agent 已据此调参）。现探针只返回耗时/tick/substep/订单生命周期统计；结果目录除 nl_tool 外为空；prompts + PROMPTS.md + env 文档同步。
- **P1-5 统一 substep 合同**：声明预算推进仿真时间（ready_at=tick+B），冻结/held-out 关闭实测检查会让超预算计算拿到不真实成交时点。现 valid/test/held-out 全部强制 substep 超时+覆盖检查（超限=无效不评分）；粗粒度防挂死墙钟保持 3× 宽松；引擎开关仅留给 dev 基准脚本（推翻早期 D-R7 裁决，理由=成交时点可比性优先，check.md 论证成立）。
- **P1-2(d) state_staging 收容**：staging_rel/state_rel 来自 Agent 侧驱动报告，此前直接拼路径（../ 或符号链接可逃逸到宿主任意可写路径）。现 register 时 resolve+收容校验（违规=BacktestError），merge 时拒绝符号链接/非常规文件（防 TOCTOU 换链）。
- **P1-3 世代新鲜度（部分）**：六大域审计状态 created_at 早于当前 raw 世代 → 快照 manifest 记 warning。硬门禁刻意未采用：审计按夜间节奏在每个落库任务后重跑，硬拒会每晚锁死实验数小时；硬保证由共享 flock + 世代缓存键承担。
- **P2-1 报表去重**：latest_fold_records/latest_heldout_records 下沉 pipelines/ledger.py（reporting 与 webui 共用）；正式报告 rerun 后只计最新记录（此前同 fold 重跑会重复计入均值/复合收益）。
- **P2-2 措辞**：AcceptanceRules docstring 与创建表单 min_return/min_sharpe 帮助文案改为“目标值：低于记警告不阻止冻结”。
- **P2-4 清理保障**：引擎 close() 无条件 kill_marker 清扫（此前只在 terminate 路径）；fold/held-out 容器启动纳入 try/finally；沙箱容器带 mq.experiment 标签，SIGKILL 升级与删除实验后按标签 docker rm -f 回收。
- **待用户决策**：P2-3（_legacy_benchmark_year 是用户在途代码，建议冻结迁移替代请求时读 raw）；P1-8 跨周期验收合同；P1-7 HITL test 展示策略；P1-6 Timeview 性能批次。P1-4 维持部分成交缓办裁决。
- full suite 642 OK；PROMPTS.md 重导出；无需重建沙箱镜像（驱动未动）。

2026-07-12 check.md 审计 P0 数据完整性修复（feat/step-tree-rollback）

- **P0-1 forecast_vip/express_vip PIT 前视（已确认真实泄漏）**：available_at 此前优先 first_ann_date，把修订版本回溯到首告日（lap-test2 决策快照实含 22 条未来公告，600157.SH 2026-01 修订标 2024-08 可见）。改为每版本只用自身 ann_date（first_ann_date 保留为序列属性）；审计新增硬规则 available_at≥本行 ann_date（forecast/express，违反=error）；audit.py 指引文本同步。全量重建 data/pit/fundamental_events（20200101–20260711，754 分区 207.5 万行，13m20s），实测 63042 行 forecast_vip 0 条回溯；审计 errors=0。
- **P0-2 公告月滚动窗破坏性覆盖**：month_windows 起始月截断+整月覆盖已真实删除 52 个业务键（202605/202606，修正账本可证）。修复：ann_month 触达月一律拉全自然月（fundamental_ann_month_windows）；write_parquet_revision_aware 检测到删除已有业务键默认阻断覆盖（skipped_key_removal_overwrite + REVISION_ALERT；接受源端撤回=先删分区文件）。全覆盖拉取族（trade_date/period/ts_code/merge 路径）显式 allow——该处删除只能是源端修正，沿用告警+账本工作流。受损月已重拉修复（202605 早前已被整月 refresh 自愈，202606 本次 +2 键复原）。
- **P0-3 PIT 当前月只 merge 不 replace**：build-fundamental-events 改月对齐整月替换（month_aligned_replace_window，含当前月；cron 起点本就月对齐）；sidecar source hash 不匹配从 warning 升为 error。
- **P0-4 宏观 range 文件堆积 2–3 倍重复**：quarter_once/month_once 固定写唯一 `range=<下界>_latest.parquet`（MACRO_RETAINED_FLOOR=20200101 单一来源），写后清理旧的按结束期命名文件（本次实清 cn_gdp/cn_cpi/cn_ppi/cn_pmi/cn_m/sf_month 各 3–6 个旧文件）；审计期望路径改 canonical 且多余 range 文件=error（顺带消灭了 index_daily 之外的宏观期望路径长期噪声，macro 审计 errors=0）；快照 _build_available_at_domain 防御性去重并记 manifest duplicate_rows_dropped。
- **P0-5 raw 世代边界**：cron 落库类任务（update/download_tier/download_event_flow/pit_event_pipeline/revision_sentinel）成功后在排他 flock 内原子写 data/raw/.raw_generation.json；快照构建全程持共享 flock + 世代双读（变化即 RuntimeError），manifest 记 raw_generation，快照缓存键并入世代 id（夜间落库后旧缓存自动失效）。
- 污染处置：lap-test2 已发 graceful stop（产物只留审计）；lzp-test snapshot_cache 已删除。check.md P0 条目已标注修复状态。full suite 638 OK；文档同步 data/environment/pipeline 三处。

2026-07-11 控制台跟进批次：弹窗解析/闪烁/布局/耗时（feat/step-tree-rollback，d36ee80 + e1ea857）

- 创建参数弹窗「默认值（9）+ 破折号」根因：/api/parameter-schema 的 groups 是 {name, fields} 列表，前端误当 dict 展平，把 9 个分组对象当字段渲染 → 改 flatMap(group.fields)。
- 闪烁修复：批准/切模式/门控开关此前走 route() 整页重建 → 控制条纳入 refreshDetail() 原位替换（detailView.barHost），两处 send 改原位刷新。
- 模式 vs 门控卡辨析（用户问是否重复）：非重复——模式 step=全局默认、卡片=本 Fold 例外；卡片文案按当前模式动态说明关系。门控卡下方缺间距：实时 trace 面板补 section-gap。
- 已完成 Fold 显示总耗时：账本新增 run_wall_seconds（沙箱启动→记账，含快照/会话/冻结/测试评估），面板 kv 行「总耗时」（旧记录无字段则不显示）。
- 股票筛选布局：板块选择改全宽 chip 药丸（勾选高亮描边），复选组通用样式升级。
- full suite 630 OK；控制台已同步重启。

2026-07-11 WebUI 与端到端流程全面复审 + 修复（feat/step-tree-rollback，bbebfd3）

- 双审计器（WebUI 实测 + 端到端组合设计）复审全部近期特性与在途改动，确认缺陷全部修复，full suite 630 OK：
  - **step 模式此前完全失效**：模式改名遗留的迁移块在 read_control/resolve_options 把 step→manual 逐次降级（worker 每轮重读即被清），门控默认永远看不到 step；连带发现旧测试断言的是降级语义。迁移块删除，step 为一等模式。
  - **挂起态 UI 消失**：批准面板与实时 trace 只在 running_session 渲染——恰好在 waiting_step_user 时不见（唯一的 approve_step 按钮无法到达）；会话批准按钮只在 manual 模式出现而 worker 在 step 模式同样门控（修模式后将死锁）。两处判定放宽。
  - **门控挂起中 stop 被吞**：runner 每动作 catch-all 把 ExperimentStopped 转成 internal tool failure 回喂模型。新增 SessionInterrupt 控制流异常（tools.base），dispatch 先行重抛，ExperimentStopped 继承之。
  - **rollback/rerun 步态残留**：step_gate/step_go/step_directives（rollback 还有 directives/prompt_overrides）不清理——重跑 Fold 前 N 个门控被旧 step_go 自动放行、旧 Step 指令重放。两路径均清理，配回归测试。
  - **筛选宇宙外订单静默作废**：全日挂单后 day_end_unfilled 与真实未成交不可区分；现按 code_not_in_universe 在申报时拒绝（MarketData.codes）。
  - 步序号只计成功正式验证（探针/失败不再使批准序号偏离 step 树 valid_NNN）；≥1s 的挂起写 step_gate trace 事件并计入倒计时回补（挂起中的 Fold 不再显示超期）；fold_analysis 受限投影补 accept_warnings（评审 LLM 此前看不到收益/Sharpe 警告）；警告冻结 Fold 徽标转琥珀色；空筛选集在快照构建时显式报错；_auction_print 对齐 open-only 模型；会话内切换清理实时定时器。
- 设计提示（未改码）：收益/Sharpe 改警告后走前链条失去了唯一的回归护栏（劣质更新会覆盖更好的父产物并向后传播）——按用户裁决保留警告语义；若需護栏可加可配置的"不劣于父产物才冻结"策略，待用户定夺。审计另确认：等待回补不会误触 wrap-up、approve_step 无丢唤醒、回滚归档/剪枝正确、竞价重构无 schema 失配（注意其为在途改动，broker/common/schedule/audit 需同 commit 落地）。
- 控制台已同步重启。文档同步：pipeline_design 验收措辞（硬校验=回撤/非有限/完整验证；收益/Sharpe=警告）、parameters_reference 补 screen_* 全组。

2026-07-11 三项 UX 裁决与落地（feat/step-tree-rollback）

- 创建参数弹窗：改为全量展示——显式设置（params.json 实际落盘）在前，其余按创建表单 schema 默认值灰色列出（根因：创建表单只持久化与默认不同的值，弹窗原先只见少数几项），元数据/继承产物单独分节。
- 运行模式增至三档：auto（连续）/ manual（逐会话批准）/ **step（逐 Step 批准，最细）**——step = manual + 全部 Fold 会话默认开启逐 Step 门控；逐 Fold `set_step_gate` 支持显式开/关覆盖与清空恢复默认；创建表单与详情页模式切换同步三档。
- 会话列表不加 Step 行（裁决）：Step 非可寻址会话，长 Fold 下 10+ 行/Fold 会淹没导航；Step 已有三处完整呈现（Step 历史表、Step 产物树、实时 trace）。折中：门控挂起时，该 Fold 行内显示「Step N 待批准」徽标（定位当下最需要的信息）。
- full suite 627 OK；控制台已同步。

2026-07-11 控制台四特性：创建参数查看、股票筛选、逐 Step 门控、验收改警告（feat/step-tree-rollback，72bc04d + 040af96）

- 创建参数查看（72bc04d）：详情页标题旁「创建参数」按钮 → params.json 全量弹窗（显式设置在前、元数据在后；未列项按系统默认，实际生效以 run manifest 为准）。
- 股票筛选（72bc04d）：SnapshotConfig `screen_*` 旋钮（剔 ST/剔新股 N 天/流通市值带（亿）/股价带/板块子集 main·gem·star·bj），全部按决策锚点已知信息计算并整区间冻结（缺属性 fail-closed）；单一筛选集合限制 universe/daily/分钟/竞价/事件/财务逐股域（决策快照 + 回放槽一致），manifest 记录配置与结果规模；CLI `--screen-*`、HITL PARAM_DEFAULTS、控制台「股票筛选」表单组（板块复选）。目的=收窄研究宇宙、减少数据量、加速回测。
- 验收改警告（72bc04d，特性 4）：min_return/min_sharpe 低于阈值不再重置 Fold——最新完整验证照常冻结，`accept_warnings` 记入账本（Agent 可见投影同步）并在 Fold 面板红字展示；非有限指标、完整验证与 max_drawdown 上限保持硬校验。提示词/facts 标注新语义。
- 逐 Step 门控（040af96，特性 3）：`step_gate_hook` 从交互 worker 经 run_fold（ctx.extra）进 Agent runner——每次正式（非探针）验证回测后查询 control.json：开启则 status 置 `waiting_step_user`+step 摘要并挂起，直到 `step_go` 放行（stop 即刻生效、关门即放行、等待回补推理预算）；放行可带 Step 级指令，注入该次回测工具观察（标注为用户假设）。控制台：逐 Fold 门控开关 + 挂起时批准面板（指标块+指令框+批准并继续）；`approve_step` 服务端从 status 解析 step 序号。批量管线零变化（无 hook）。
- 验证：full suite 627 OK；控制台已同步重启。注意：工作树含用户在途改动（控制模式改名 auto/manual、run_meta_learning 提示词覆盖、trace tail、预算默认调整、竞价改用 stk_auction 无后缀表等）——本批特性均基于当前工作树实现并全绿。

2026-07-11 扩容收尾：审计驱动的三轮数据修复 + 全域审计清零（feat/step-tree-rollback，f5ae22e/4d1e6b7/fef1942）

- 审计裁决修复（f5ae22e）：①cron 空转——cn_evening_full 固定子集漏掉全部新 macro/event 数据集（注册表有行、调度不调）→ MACRO_REGIME_DEFAULT_DATASETS +8、--event-datasets +15、reference 强刷 +ths_index/index_basic；②真实回归——竞价 print 覆盖使未成交开盘竞价限价单永久单价化、连续时段永不成交 → 未清算开盘竞价单降级为普通限价单（真实未撮合集合竞价语义；收盘竞价单无后续时段、日终清扫作废）+ 回归测试；③去重/文档 id 冲突/冗余默认清理；terminate 信号竞态防护；ths_daily 补 Agent 可见。
- 回填揭示的下载器缺陷（4d1e6b7）：month_loop 参数名硬编码 m（broker_recommend 要 month → MacroDataset.month_param）；date_year_by_ts_code 未知数据集静默回落 FX 代码（index_dailybasic 被拿汇率对查询）→ 显式宇宙映射 + 未映射 fail-fast。
- **审计抓到真实数据丢失**（4d1e6b7 + fef1942）：ths/sw/ci_daily 年度区间拉取被服务端截断在恰 3000/4000 行且忽略 offset 分页 → 新 MacroDataset trade_date 策略逐交易日拉取，清除截断分区重回填（1823 日×3）；竞价回填含 16.6万/34.2万 组内完全重复行（源分页重叠）→ TradeDateDataset.dedup_exact + 1959 分区就地去重 + parquet_sha256 边车刷新。
- 终态：base/event-flow/board-trading 审计 error=0（含全部新数据集）；macro 新数据集 error=0（存量 error 为既有校准噪声，维持 warn-only 方针）；顺带修复存量 index_daily 审计期望按 FX 代码计数的老校准 bug。full suite 615 OK。回填全部完成。parameters_reference 数据集清单行已同步。

2026-07-11 TuShare 全量接口扩容：33 数据集 + 竞价撮合真值 + 终止加固（feat/step-tree-rollback，7 commits）

- 全目录普查（Opus 检查器实测全部候选接口权限/字段/起始覆盖）+ 用户四项裁决（因子库不采、北向冻结跳过、MED 全纳、筹码只要汇总）后分 6 批落地，每批 full suite 613 绿：
  - 批1 竞价（c013034）：**普查更正——批量表是 `stk_auction_o`/`stk_auction_c`（按 trade_date 全市场，20100104 起）**，无 2025 断点；`stk_auction`（无后缀）为按码滚动窗不采。入 DAILY_SPECS 必需集；快照双侧写独立 `auction.parquet`（session 列，行级 available_at=撮合公开时刻 09:25/15:00，节点=晚间物流）；**SimBroker 竞价单按当日真实竞价 vwap 单价清算**（可成交限价恰按竞价价、不及则留待；融券 uptick 参考同源；无行日期回落 bar 近似——corporate_actions 先例）。回填 2010→今完成。
  - 批2/3 事件面板族（7073c5d）：moneyflow_dc/_ths/_ind_dc/_ind_ths/_cnt_ths、cyq_perf、bak_daily、stk_premarket（行级 09:00 盘前）、slb_len/_mm（转融券 2024-07 停，zero_rows_ok）——EventDataset(trade_date) 全走现成管道（spec/available_at 规则/审计 PIT/cron 注册/SnapshotConfig 默认）。
  - 批2 宏观市场级（43832ce）：index_dailybasic（核心指数估值，by_ts_code）、sw_daily/ci_daily（申万/中信行业指数）、daily_info/sz_daily_info（市场日度汇总）、moneyflow_mkt_dc——MacroDataset date_year 系。
  - 批4 板块概念（b652403 + 71aab90）：kpl_concept_cons（次日 08:30，随 kpl 盘前回补节点）、dc_index/dc_member（当日 20:00）、ths_daily（宏观域深史）；参考静态 ths_index/ths_member（N/I 型逐指数）、index_basic、hs_const、index_weight（核心 7 指数月频）入 download_reference 强刷族。
  - 批5 治理（c98b442）：top10_holders/top10_floatholders、pledge_detail、stk_surv、new_share（EventDataset）+ broker_recommend（宏观 month_loop）。**pledge_stat 缓采**（接口无批量路径，仅 end_date 单点查询）。
  - 批6 文本（627f903）：irm_qa_sh/_sz（互动易/e互动问答，pub_time 行级）入 TEXT 族；text day 策略参数名泛化为 spec.date_column（cctv_news 不变）。
- 终止加固（f97c3a7，任务 #16）：terminate 动作 SIGTERM→10s 宽限→进程组 SIGKILL（test6 证据：优雅信号被元学习阻塞工作无视 1 小时）。**权衡后不加持久化 terminating 状态**：升级后窗口 ≤10s 且被同步控制请求覆盖，轮询无法观测中间态；与 launching 情形（20s+ 无属主、可重复拉起）本质不同。控制台已同步重启。
- 回填后台进行中（竞价完成；事件/宏观/链式 board+治理+文本+参考在跑）；完成后跑全域审计。数据文档已补各数据集行与 PIT 口径汇总（含裁决不采清单）。

2026-07-11 实时分钟接入（提前集成）+ 运行中实验一键重启（feat/step-tree-rollback）

- 实测确认接口（当前 token）：`rt_min`（freq 必须 "1MIN"，返回 ts_code/freq/time/open/close/high/low/vol/amount，试用档非交易日也回最新 bar）；`stk_auction`（开盘集合竞价，2025 起，日频 price/vol/amount/pre_close/turnover_rate/volume_ratio/float_share）；`stk_auction_c`（收盘集合竞价 OHLC/vol/amount/vwap）均可访问。
- 新 `data_sources/tushare/realtime.py`：`normalize_rt_minutes`（对齐 STK_MINS_REQUIRED_COLUMNS，available_at=bar 收盘同历史打点规则）、`RealtimeMinuteFeed`（watchlist 轮询 + 去重，复用 TuShareClient 串行限速）、`RealtimeMinuteStore`（data/raw/rt_min_live/ 按日分区，(ts_code,trade_time) 去重原子替换，schema 与回放槽一致——统一 tick 环路/Timeview 可直接消费）。CLI `scripts/data/tushare_realtime.py --probe/--follow`（实测 probe 两码通过）。设计前提：实盘环路（QMT 文件桥执行器）尚未实现，本模块是数据获取侧的提前就位；接入决策环 = 把 live 分区喂给 MinuteMarketData/ctx.bars（schema 已即插即用）。
- 控制台：`restart` 动作（SIGTERM 活 worker → 有界等待退出 → 账本恢复重启）+ 运行态「重启」按钮（确认弹窗）；full suite 612 OK，已同步重启 console。
- 待办（用户已购全量接口权限）：全 TuShare 目录 vs 现有摄取盘点 + stk_auction/stk_auction_c 纳入下载层与 Broker 竞价撮合（2025 起数据驱动启用）——下一大批次。

2026-07-11 控制台三处细节跟进（feat/step-tree-rollback）

- transfer 行决策时点改为与普通订单一致的 `HH:MM`（上海时区；日期已有独立列，不再带 `10-09` 前缀）。
- 暗色模式交易明细滚动容器发白的根因：页面未声明 `color-scheme`，浏览器按浅色渲染原生滚动条轨道——`:root` 增加 `color-scheme: light`、`[data-theme=dark]` 增加 `color-scheme: dark`（滚动条/表单原生控件随主题），滚动容器补 `background: var(--panel)`。
- GPU 实时检测间隔 15s → 60s（门控页面无需高频刷新）。已同步前端静态资源（nginx no-cache 下普通刷新即生效）。

2026-07-11 前端看不到 UI 更新：nginx 静态资源缓存修复（feat/step-tree-rollback）

- 排查：三个静态文件在前端服务器与本地逐字节一致（sync 一直正常）；根因是 nginx 对 /static/ 与 /（index.html）都不带 Cache-Control，浏览器按启发式缓存长期不重验证 → 旧 app.js/style.css 一直生效。
- 修复：前端 nginx 两个 location 增加 `Cache-Control: no-cache`（ETag 重验证，重复访问 304 几乎零成本），已 reload 并实测两路径均下发该头；repo 内模板 ops/webui/nginx-cornerhead.conf 同步，重新 provision 不回退。原配置已备份 /root/cornerhead.nginx.bak.*。
- 用户侧需一次硬刷新（Ctrl/Cmd+Shift+R）摆脱既有缓存；此后普通刷新即可看到更新。

2026-07-10 test5 故障双修 + 控制台细节 + Agent 决策链审计修复（feat/step-tree-rollback，fe9ec20）

- test5 巡检（fold finalize 崩溃）确认两个系统缺陷并修复：① no_update 回退把父产物拷回 finish_fold 只读锁定的 output/ → unlink PermissionError（现在回退前 _chmod_tree 解锁，兼顾 Docker subuid 文件）；② 分析钩子 NameError ANALYSIS_DIR_NAME（hitl_state 拆分漏导入，该路径无单测覆盖）。
- 控制台四项：`.input` 类从未定义 → Step 树筛选框暗色模式白底（补主题化 .input）；Held-out phase-head 选中态去掉多余 3px inset 左描边；transfer 行 decision_time 原样渲染 ISO 串 → 上海时区 HH:MM:SS；GPU 分配面板确认原先仅单次探测 → 改为面板存续期每 15s 实时复测（liveTimers 随导航清理）、逐卡显存条 + 算力% + 温度（nvidia-smi 查询扩展 utilization/temperature）、panel 化样式。
- Agent 决策链审计（Opus 审计器全量核对 test2/test5 trace、账本、订单、策略产物）8 项发现，4 项系统缺陷修复：F1 replay_window 探针 P&L（前 N 日有偏样本）被 Agent 当调优信号（同一 hash 探针 sharpe 6.4 vs 全窗 -0.10）→ 概要携带不可比 note + 工具说明明令禁止按探针调质量；F2 50ms 未跟踪 substep 宽限连杀两次 16 分钟全量回测（0.2s pandas 胶水）→ 默认 0.25s + 报错点名阈值与整改方向；F3 提示词契约缺口（main.py 非包加载相对导入必炸、workspace/ 不进回放）连烧 5 次起步回测 → 明示绝对导入与数据必须放 output|models；F5 冻结只认最后一次完整验证 → 改为匹配当前 hash 的最近一次完整验证（回退/step_rollback 到已验证版本不再强制 16 分钟重跑）。记录不改：逐 tick fail-fast（原则优先，静默 except 诱因记录在案）、ctx.nl 两实验零使用（观察项）、margin_secs 担保品代理（等真实券源数据）。
- 经济性事实（审计）：单 Fold ~90-100 分钟墙钟由 ~16 分钟全量回测主导；explore 每 Fold 固定 ~25 万 token；两实验均验证过拟合（valid sharpe 2.92/0.72 → test -1.23/-0.74）。
- 验证：full suite 611 OK；PROMPTS.md 重导出；控制台已同步重启（GPU 端点实测返回 util/temp）。

2026-07-10 控制台全面巡检：launching 状态、收益曲线纠偏、实时视图修复（feat/step-tree-rollback，0a77e02）

- 「创建并启动」后详情页长时间显示未启动：worker 进程拉起到首次写 status.json 之间（解释器+导入耗秒级）状态读作 created，且该窗口 worker_alive=false 会放行重复拉起/删除。权衡后实现 launching 存根（成本=每次拉起一次原子写，无轮询开销）：manager 在 Popen 前写入（进程尚不存在，单写者不破坏）、保留进度字段；新鲜期显示脉动「启动中」徽标并计入并行上限、阻止重复启动/续跑/删除；超 180s 未接管降级 interrupted（worker 拉起失败可见）。
- 全面巡检（Opus 只读检查器对全部端点实测 + 前后端代码核对）确认 1 个正确性缺陷 + 6 个小缺陷，全部修复：
  - **收益曲线混拼（正确性）**：equity 把一个 run 里所有 valid_* 窗口（同一验证区间、不同策略版本的反复尝试）按日期拼接，test2 渲染 -3.92% 而账本头名 +0.98%。改为只取账本记录的选定 Step 窗口（`fold_valid_window`：selected_step 的 validation_result_ref，缺省回退最后验证 Step），实测曲线终值与账本一致（+0.983%）。
  - 详情页首次渲染丢 Held-out 收益图/风格卡（detailView 在面板构建后才赋值，且跨实验导航可能画到上一实验数据）→ 先赋值再建面板。
  - trace/stats 每 5s 全量重读 trace（test2 2MB）→ 按 path 增量聚合缓存（20.5ms→0.06ms）。
  - SSE trace 流断线后从 0 重放 → 页界 SSE id + Last-Event-ID 续传 + retry 提示。
  - 分析面板轮询 timer 不随导航清理 → 纳入 liveTimers；zip 端点构包异常泄漏临时文件 → 失败即删；无风格 rollup 显示「加载失败」→ 改为「该运行未落盘风格归因数据」。
- 有意缓办（记录理由）：list_experiments 逐轮全量汇总（当前仅 2 实验，失效键复杂度不值当）；分析 pending 状态持久化（重启恰逢生成中属罕见，恢复=再点一次）。
- 验证：full suite 611 OK；同步静态并重启 console（端到端 ok）；test2 实测 equity 三条曲线、trace/stats 正常。

2026-07-10 Step 树面板重设计：悬停修复、旧格式下载、规模化交互（feat/step-tree-rollback，7479d4c）

- 两个报告缺陷确诊并修复：① 悬停卡是每行内部的 absolute 子元素，被树容器 overflow-y 裁剪（且鼠标扫过行时闪烁）——重做为 document.body 上单个 position:fixed 共享 tooltip（视口收敛、pointer-events:none，大树零冗余 DOM）；② 历史节点无法下载——旧平铺布局快照（test2 全部节点）被判 has_snapshot=false 整行失活。控制台读取层改为布局无关：`node_layout()` 识别 split/flat，两种布局均可下载（旧格式按目录原样打包）；回滚仍仅限新布局（payload 增 `restorable`，set_parent_override 拒绝旧格式，UI 标「旧格式」徽标）。行时间从仅时刻改为完整日期时间。
- 规模化与全量可见（用户补充要求）：子树折叠（逐节点 ▸/▾ + 全部展开/折叠，折叠行显示 +N）、文本筛选（匹配 Fold/节点/结果名，保留祖先链上下文 + 命中计数，筛选时强制展开）、行内直接下载/回滚按钮（无需进弹窗）、62vh 滚动区、触屏隐藏 tooltip（弹窗承载全量字段）。节点弹窗补模型 hash 与冻结用途行。
- 实机验证（同步静态 + 重启 console）：test2 三个旧格式节点全部可下载（zip 含完整源码 + detailed_return.json），valid_007 正确标注「冻结 epoch_001/fold_202512」，回滚按钮对旧格式正确隐藏。full suite 608 OK。

2026-07-10 Step 树 GPT 审计裁决与修复（feat/step-tree-rollback，feb8739）

- 5 项审计意见逐项对码核验：2 项确认修复、2 项核实后否决、1 项文档补齐。full suite 607 OK。
- 确认 #1 重跑节点 ID 冲突（真实缺陷，且早于本特性——rerun_fold 自身即触发）：result_name（valid_NNN）每个 run 从 000 重排，同一 Fold 重跑（rerun_fold / rollback_fold 后重启）首次完整验证即撞已有 node_id，record_step 在回放成功后抛 ValueError → Agent 只见 internal tool failure、树停止累积。修复：node_id 纳入 run 段（epoch__fold_ref__run__result；run_id 本就 Agent 可见且无日历信息）。
- 确认 #2 回滚泄漏未来验证信息（真实泄漏）：rollback_fold 只改账本/归档冻结产物，实验级 steps 树保留被丢弃 Fold 的节点（未来区间上验证过的完整策略+指标），_install_step_tree 原样交给重跑 Fold 的沙箱。修复：回滚同步修剪树（被丢弃会话节点+后代连快照移入同一 rollback 归档，tree.json 备份，current 指向被剪节点时置空）；`set_parent_override` 增加 past-only 守卫（更晚 Fold 节点不得设为更早会话起点，本会话允许=从节点重跑场景）。
- 否决 #3 include_models=false 破坏谱系：每个节点记录自身 model hash，父指针语义=搜索谱系；代码/参数不配对的风险已写入工具契约警告，保留研究自由度。
- 否决 #4 恢复缺事务性：与全管线产物安装同构（copy_artifact 均为删后拷），失败恢复路径=重新调用（全部已验证态永在只读树内），树位置仅在双 hash 校验通过后移动；已在工具 docstring 明示，不为单一工具引入特例事务。
- #5 文档：pipeline_design §5（回滚剪树、past-only）、parameters_reference §9 同步（随在途文档 pass 待提交）。

2026-07-10 Step 树分支回滚：Agent 工具 + 控制台面板（feat/step-tree-rollback，dfa9400 + d3a2e2f）

- 动机：Step 树存储层早已树形完备（逐节点全量快照 + `set_position`），但 Agent 没有受认可的恢复/重定位通道，新节点父指针恒为最新节点——树在结构上只能是线性链。本批打通两侧：Agent 侧 `step_rollback` 工具，用户侧控制台 Step 树面板 + 父产物覆盖。
- Agent 工具 `step_rollback(node_id, include_models=true)`（dfa9400）：把 `output/`（默认含 `models/`）恢复为指定已验证节点快照、按节点记录 hash 校验、`set_position` 移树位置——之后通过验证的回测真实分支。失败节点/未启用树/写锁均拒绝；修改约束仍相对本 Fold 父产物度量（恢复远端分支可能超 diff 预算，reject-don't-clamp 一致语义）。节点目录重构为 `steps/<node_id>/{output/,models/}` + 根级验证附件（旧平铺布局会把 output 内 `models/` 目录与模型产物静默合并、附件可能遮蔽同名 output 文件）；附件新增 `orders.parquet`（回滚决策可对比成交而非只看曲线）；`tree.json/tree.txt` 改为新 inode 原子写（fold 副本与实验副本硬链接共 inode，中止的 fold 原地写会让实验树引用从未回拷的节点快照）；`tree.txt` 渲染增加 Sharpe。提示词 STEP_TREE_SECTION 同步（含收尾阶段恢复已验证节点再 finish_fold 的指引），PROMPTS.md 重导出。
- 控制台（d3a2e2f）：新 `webui/steps.py` 把 Agent 侧不透明 `fold_ref_*` 反映射回真实 Fold（schedule+ledger 重算 ref）、按冻结 hash 标注各 Fold 出厂节点；`GET /steps` + `GET /steps/<node>/source.zip`（完整 output/models 源码 + 验证明细）。详情页 Step 产物树面板：悬停指标卡（收益/多空/Sharpe/回撤/冻结用途）、失败节点置灰、当前位置/冻结徽标、点击下载或「从此节点回滚」。用户侧回滚固定在会话门控（绝不 mid-session 强改工作副本）：新控制动作 `set_parent_override`（session_key→node_id，空值清除），worker 在该 Fold 启动时以节点快照为父产物（hash 校验，账本父 id 带 `stepnode_` 前缀可审计）；未运行 Fold 下次启动生效，已完成 Fold 配「设置并重跑」；`rollback_fold` 清除被丢弃会话的覆盖。
- 验证：full suite 605 OK（新增 9 测试：工具恢复+分支+守卫、控制往返、worker 覆盖生效/拒绝失败节点、webui 视图/zip/控制校验）；前端 `node --check` 通过；实机烟测：console 重启后 `GET /api/experiments/test2/steps` 正确反映射 fold_202512（test2 旧布局节点 has_snapshot=false 优雅降级，无下载/回滚入口——符合测试期不兼容旧产物方针）；静态资源已 `webui_stack.sh sync` 到前端并重启 console/tunnel（端到端 ok）。驱动未变，无需重建沙箱镜像。

2026-07-10 架构梳理重构：职责归位与边界拆分（refactor/architecture-cleanup，5 commits）

- 依四路 Opus 子代理逐子系统架构评审（environment 核心 / tushare 数据层 / pipelines+agent / tools+nl+webui）+ 人工裁决后全量实施；核心结论：分层本就干净（无向上引用），问题集中在"职责放错家"与四份手写重复。全程行为保持：每批 full suite 596 OK，PROMPTS.md 导出字节不变。
- 批次 1 零风险清理（7c32a2a）：删生产死代码（`broker_core.lot_floor/resolve_shares` 连测试、`_int_or_none`、common.py 尾部无效 `__main__`）；runner 复用 compact 的 `_drop_leading_orphan_tools`（原逐字节复制）；`runtime._write_json_atomic` 临时名唯一化（原固定 `.tmp` 有并发交错竞态）；broker.py 补 `Iterable` 导入并去引号（原缺导入使 `get_type_hints` 潜在报错）。
- 批次 2 归位搬移（f9255bd）：删除 `backtest_engine.py` 杂物间（engine 曾跨模块导它 6 个私有符号）——分钟行情+合成 bar → `replay_market.py`（助手转公开名）、`ReplayResult`+`compute_return_stats` → `replay_stats.py`、`BacktestError`/NL 泵/`_jsonable` 并回 `main_ctx_engine`、`hide_snapshot_slots_from_agent` → `sandbox.py`；NL RPC 服务（`StrategyNLService` ~180 行）从 tools/backtest.py 迁 `nl/service.py`，`TextRetriever` 拆 `nl/retrieval.py`；派生镜像生命周期 ~250 行从 experiment.py 迁 `environment/sandbox_images.py`；fold/meta/heldout 三份 finalize 尾（collect→必记账→再抛，耐久性不变量）收敛为 `_finalize_run(record_builder)`；`build_experiment_facts`+18 个投影助手从 prompts.py 迁 `agent/experiment_facts.py`（prompts 回归纯提示词 ~550 行），顺带发现并单源化 `META_SEARCH_PERSPECTIVES` 双定义；deepseek 会话日志+脱敏簇拆 `llm/conversation_log.py`；driver 不再携带 close op 提示——engine `_resolve_close` 成为 cover/sell_repay/credit_sell/sell 三分支唯一权威（pending 视图同源解析），删除 driver 侧漂移副本。**driver 变更 → 沙箱镜像已重建**。
- 批次 3 边界拆分（218136a）：新建 `pipelines/hitl_state.py` 承载 HITL 共享词汇（文件名常量、PARAM_DEFAULTS+派生、resolve_options、control/status 文件协议、StatusReporter、会话计划），interactive.py 只剩 worker（1004→550 行）——webui 五个模块此前读一个文件名常量都要连带导入 worker+threading；`write_json_atomic` 单源于 runtime.py；webui/server.py 回归纯路由（520→366 行）：`AnalysisService` 线程任务控制迁 `webui/analysis.py`、75 行 prompt-preview 路由体迁 `webui/prompt_preview.py`（顺带消除对 registry 私有函数的越权访问——`read_ledger_records` 转正）、数据覆盖/交易日钳制迁 registry。
- 批次 4 数据层收敛（b701d13）：`audit_{text,macro,event,board}_dataset/_keys` 四对手写副本（已发生真实漂移：分页探测排除不均、text 独有空表早退、event 独有 zero_rows_ok）收敛为 `DomainAuditProfile` 参数化的单实现 + 四个薄适配器；**对真实数据湖固定 end-date 前后对拍，四份 status JSON 逐字节一致**（modulo created_at）。有意不做：pit/unit rules 表挂 spec（多处引用、作为数据表内聚，搬移纯属搅动）。
- 评审明确否决的重构（记录以防反复）：不合并 runner 与 NL engine 两个工具循环；不做跨 fold/meta/heldout 的通用 run 脚手架；不拆 reporting.py/search.py/app.js/driver；broker 撮合引擎拆分仅当撮合模型要长大（部分成交/队列）时再做；driver 侧因 stdlib-only 隔离而复制的常量/写入器保留。
- 验证：全程五批各自 full suite 596 OK；四域审计实数据对拍一致；`git diff --check` clean；镜像按钉扎 digest 重建成功；pipeline_design/parameters_reference 模块引用同步。

2026-07-09 第五轮审计裁决与全量修复（fix/audit5-remediation，5 commits）

- 对 GPT 第五轮审计（check.md，22 项 P0–P2）先做 5 路并行代码核实（逐项 file:line 证据），再按三原则裁决：16 项采纳（多数缩水实施）、6 项拒绝（#6 盘后北交资格系既定设计、#19 分红 pay_date 系已记档残差、#22 状态 RPC 关不住内存绕过、#12d 部分成交维持实证推迟；三模式拆分/读路径全量哈希/NL 可终止进程以轻量替代覆盖）。
- 批次 A 回放正确性（8c3aa92）：指标 Day-0 基准（首日亏损与初始峰值进 Sharpe/回撤，堵住「首日 -30% 回撤=0 过验收」）；09:25 只暴露开盘时段首 bar（晚开票缺席，堵前视）；撮合前 open/撮合后 close 双阶段持仓重估（保证金准入不再用昨收）；收盘竞价与合成 bar（新 `synthetic=True` 标记）单一价撮合（堵合成 15:00 bar 携带早盘低价的追溯成交）；清算完整性显式报告（`liquidation_complete`/`unliquidated_positions`/`remaining_liabilities`，不入验收）；universe 只发布 as-of 名（缺记录=null 不回填当前名）、过滤后删 `delist_date`（未来信息）、缺 namechange 即 raise。
- 批次 B 校验门（c7f8f12）：验收指标非有限硬拒 + AcceptanceRules/ExperimentConfig/BrokerProfile 构造期范围校验（负滑点双向获利、倒置维保线等直接报错）；正式 JSON `allow_nan=False`；快照按启用域检查审计 status（daily/分钟/基本面硬失败，events/macro/text 降级 manifest `data_quality_warnings`——实测 macro/events 现存 error 属审计校准噪声，不应阻断实验）；margin_secs 逐日数据缺当日集合 fail-closed 拒 `margin_secs_data_missing`（不再沿用陈旧集合）。
- 批次 C 证据链（b265c37）：Order 记录补 `submitted_at`/`limit_price`/逐单 `fee`/`stamp_duty`，撤单保留为 `status="cancelled"`（对齐 live 按 status 过滤 ORDER）；Broker 落盘逐日 (date, account, ts_code, side) 日终持仓 `positions_eod.parquet`，style 归因改读它（强平/送股/对冲腿如实反映，删除成交单重建逻辑）；CLI 报告基准改读账本冻结 benchmark 块（删 raw 路径与三个 CLI flag）；运行中途抛错追加 `attempt_failed` 账本记录（异常/traceback，可重跑）；provider 日志按天+进程分文件 + `call_id` 关联 + payload 只存一次；`write_parquet` 补 `parquet_sha256` + sidecar 原子写 + 审计抽样哈希校验。
- 批次 D 运行时（c8312ef）：决策绝对 deadline 下传 NL——每轮 provider 超时钳制剩余墙钟（钳制时禁重试，`complete_tools` 新增 `max_retries` 覆盖），最坏超程从整个多轮 NL 任务缩到一次有界 HTTP 调用；文本检索改 DuckDB `regexp_matches`（RE2 线性时间，灾难回溯结构性不可能；列裁剪+LIMIT，删 1.6GB 常驻 Series+dict 正文副本），pattern 上限 256、不支持特性返回可修复错误（fail-fast，不再静默转字面量）。
- 批次 E ops/收敛（8da5125）：两个 crontab 安装器只认真正的 "no crontab"（其他读错误中止）+ 装前备份 + 装后验证；数据任务锁改内核 `flock`（进程退出自动释放，删 PID 复用死锁与陈旧锁启发式）；manifest 记录 docker image ID/RepoDigests、基础镜像钉 digest、DuckDB zip SHA-256 校验；pyproject 补 duckdb 依赖与 `[webui]` 可选组；PROMPTS.md 补导出 Explore/压缩/Fold 分析三提示词；PARAM_DEFAULTS 从域 dataclass 派生 + CLI/HITL 默认值漂移测试；单位合同显式限定 daily 域（域 meta 标 `units`，提示词加跨域单位警示）；env docs 记录 substep 延迟模型可强制性边界（内存绕过）与 RPC 方案否决理由。
- 验证：full suite 572→597 OK（+25 回归测试）；每批独立 commit；PROMPTS.md 随批重导出；`git diff --check` clean；五份 living docs + parameters_reference 同步。

2026-07-09 Barra-lite 行业 rollup 完整性与设计文档收口（fix/broker-fill-realism）

- 修复逐窗口展示只保留前 8 个行业时，跨窗口 rollup 会永久丢失其余行业暴露的问题：sidecar 额外保留完整日度累计量，展示仍保持前 8；旧 sidecar 读取路径保持可用。
- 补齐 Snapshot raw 来源、冻结输入归因、冻结 artifact manifest、审计/验收边界，并把 Pipeline 总流程改为可读分点。
- 验证：`tests.unit.test_style_analysis` 5 项通过；相关模块 `py_compile` 通过；`git diff --check` clean。

2026-07-09 指标计算层统一：回放时一次计算、Web 纯读、前端纯渲染（feat/hitl-webui 线）

- 动机（用户提出，核实成立）：Barra-lite 与前端事后计算各自读 raw——而 raw 会被源端回写（revision ledger 即证据），事后重算可能与 Agent 当时所见不一致；快照是哈希冻结的，回放时计算是唯一可复现口径。
- 落地单一计算点：`environment/style_analysis.py` 全部输入改冻结运行数据——基准 ← 回放槽 `macro.parquet` 的 `dataset=index_daily` 行（本日 index_daily 入 macro 域后才可行）、行业 ← 决策快照 `universe.parquet` 申万一级（决策日口径）、横截面 ← 回放槽 daily（原样）；raw 读取器（基准/行业/daily_basic 分位）与 run manifest `raw_dir` 字段全部删除。backtest 工具对**全部模式**（valid/frozen_eval/heldout）落盘逐窗口 `style_analysis.json`（含策略与基准日收益序列，使下游彻底脱离数据源）；Pipeline 在窗口链完成时聚合为 `results/style_<prefix>.json`（回归拼接重跑、暴露按天数加权），失败按前缀隔离并记入账本 `style_rollup_error` 字段。
- Web 层退化为读模型：style 端点原样返回落盘 rollup（零计算）；equity 端点策略序列读 `detailed_return.json`、基准读 rollup，**累计/回撤由服务端算好数组**；`app.js` equityChart 删除复利与回撤循环成纯渲染器。旧实验（无 rollup）按无兼容指令直接降级：策略曲线照常、基准线缺省、style 404（实机验证）。
- 全面复查（用户要求）：6 路审查（2 路完成 + 4 路因会话额度中断、由主线人工补齐关键追踪）产出 8 项发现，7 项修复（样式块拼装去重、死抽象/死过滤器清理、run_series 复用 _chain、server 改用 read_json、rollup 失败按前缀隔离 + 账本落痕替代裸 print）、1 项核实后驳回（rollup 撕裂读竞态不存在——控制台只读一次性写入的收集副本）；代码内版本信息（schema_version、带日期注释）按指令全部移除。
- 验证：full suite 572 OK（style 单测重写为冻结输入 fixture + rollup 天数加权/缺 sidecar 用例；equity 测试改断言服务端 cum/回撤数组与 rollup 基准来源）；实机重启：旧实验策略曲线正常、基准优雅缺省、style 干净 404；前端已同步。文档：environment_design §3.7（含用户扩写的指标口径表，输入口径改冻结数据源）、pipeline_design §5.3 单一计算点原则、parameters_reference。

2026-07-09 raw 覆盖审计落地：14 个数据集进 Agent 决策输入（feat/hitl-webui 线）

- 依据 check.md（GPT 整理的 76 数据集覆盖审计，48 进/28 未进——逐项核实无误）的分档结论实施。机制事实：events/macro/text 域无需 DatasetContract——行级 `available_at` 在下载时按规则打入（回填安全），暴露开关只是 `SnapshotConfig` 域元组 + 刷新节点映射；可见性为双重门（行级 available_at ∧ 节点完成），配置只会更保守、不会泄漏。
- 批次一（11 项）：events 增打板/热榜/游资 `kpl_list`/`limit_step`/`limit_cpt_list`/`limit_list_ths`/`ths_hot`/`dc_hot`/`hm_detail`/`hm_list`（打板三件套映射盘前 0850 节点=次日盘前可见，实测 cutoff 正确；热榜/游资走晚间；新增 ~6.7k 行/日，与现有 events 同量级），macro 增 `repo_daily`/`us_tycr`/`us_trycr`。`cn_schedule` 审后剔除：源端无历史（79 分区仅 7 非空），对回测零贡献。
- 批次二：`shibor_quote` 确认从未进晚间刷新（比"漂移风险"更糟），加入 `MACRO_REGIME_DEFAULT_DATASETS`、补齐 5/25→7/8 缺口（+557 行，revision ledger 记录）并暴露。
- 批次三（news 带闸门）：`SnapshotConfig` 新增 `news_sources`（默认 cls/wallstreetcn/eastmoney）与 `news_window_months`（默认 2），`_build_text` 按源子集读取、独立窗口截取、正文 hash 跨源去重（最早副本保留）；实测 21 个月文本窗口下 news 仅 +63.7k 行（全库 3.88M 的 1.6%）、零重复、库文件 ~10MB。
- 批次四（index_daily 程序化）：新建下载 spec（`date_year_by_ts_code`、`available_at=conservative_date_eod`）+ `DEFAULT_CN_INDEX_CODES` 七只核心宽基（000001/000016/000300/000905/000852/399006/000688）+ `--cn-index-code` CLI + 晚间 cron + schedule.json 接口表；回填 2020–2026 共 49 分区 11,039 行（存量沪深300 基准分区重写补 `available_at`，webui 基准读取兼容验证通过）；暴露进 macro 域。
- 提示词可见性表同步（打板盘前行、热榜/指数/新闻归行、情绪弱信号定性一段），PROMPTS.md 重导出；文档：data_documentation §1.4/§1.6/§1.7 暴露注记、parameters_reference §1 新旋钮。
- 验证：full suite 571 OK（+3：默认暴露漂移守卫、news 源过滤/窗口/去重/缺源 fail-fast、打板盘前节点 cutoff）；新 events/macro 域真实数据构建通过（heldout 期 188k 行 events、7 指数 301 行）；refresh-node 漂移守卫 15 OK。
- 用户复核后放宽 news 闸门（原则：测试阶段最大化 Agent 可见数据）：默认改为**全部来源 + 跟随文本域完整窗口**（`news_sources=()` 自动发现、`news_window_months=None`），仅保留正文 hash 去重——实测完整窗口 4.56M 行去重后 2.60M（去重率 43%）、库 ~0.4GB、扫描+去重 45s，可承受；两旋钮保留供收紧。同轮按"测试阶段不保历史实验兼容"指令收紧 `status_pid_alive`：`pid_start_ticks` 从可选校验改为必需匹配（旧 status 一律判死）。full suite 复跑 571 OK。

2026-07-09 逐 Step Barra-lite 归因进回测闭环（feat/hitl-webui 线）

- 动机（经评估采纳）：Step 结果原本只有绝对指标，Agent 无基准语境（大盘 +5% 月份的 +3% 会被当成功证据）；宿主侧预计算一次 ~2s（回放槽 daily.parquet 本就含全市场 circ_mv/pb/turnover_rate 横截面，风格暴露完全来自 Agent 可见数据），相对回放墙钟开销 1–3%。
- 落地：模块迁至 `environment/style_analysis.py`（webui 版删除，控制台事后端点与 equity 基准复用同一实现）；backtest 工具在 valid 模式回放后计算——完整载荷写 `results/valid_*/style_analysis.json`（Agent 可读、step_tree 附件），紧凑 `benchmark` 块（同窗沪深300收益、超额、β、n_days、市值倾斜；刻意不含年化 α/R²——20 日年化只放大噪声）进回测 summary/trace/账本 Step 摘要；run manifest 新增 `raw_dir`（经 snapshot provider 链解析）供宿主读基准与行业表；输入缺失一律降级 None 不失败（frozen_eval/held-out 不生成 Agent 可见归因）。
- 提示词加一条：归因是描述性诊断、非优化目标（防"追 β/风格数值"过拟合）；PROMPTS.md 重导出。控制台 Step 历史表增列 超额(vs 300)/β。
- 验证：full suite 568 OK（+4 style 单测：横截面倾斜边界、基准复利/超额、缺列降级、回归数学）；真实 test2 valid 窗口实测（β 0.44、基准 −2.46%、超额 +3.17%、20/20 日、~2s）；tools-flow 79 测试过（真实工具路径含新钩子）；控制台重启+事后端点+前端同步端到端 OK。文档：environment_design §3.7、parameters_reference。

2026-07-09 控制台七项功能批次：基准曲线/风格验证/流程控制（feat/hitl-webui 线）

- ①收益可视化重构：所有柱状收益图替换为**日度累计收益折线 + 回撤子图**，策略（验证/测试/held-out）对照沪深300（`data/raw/index_daily` 000300.SH，参考 external_references/example_reports.png 版式）。新增 `webui/equity.py`（复用每窗口 `detailed_return.json.equity_curve`，验证子窗口按日链接；端点 `equity` + `folds/<e>/<f>/equity` 只传日收益，前端复利+回撤）；新组件 equityChart（十字光标列命中、终值图例、Held-out 用第 3 分类色 #eda100/#c98500，validator 双模式 PASS）。首页卡片迷你曲线、hero/详情页全曲线、Fold 详情验证曲线（测试曲线留在折叠审计区）、held-out 曲线全部落地。
- ②Barra 评估结论：完整 Barra 不引入（无授权因子库/协方差，单 Fold ~20 交易日撑不起多因子回归）；落地 **Barra-lite**（`webui/style_analysis.py`）：CSI300 单因子回归 β/年化α/R² + 成交重构、逐日结转持仓的风格暴露（市值/PB/换手带符号分位偏离）与申万一级行业净权重；sidecar 持久化 `hitl/analysis/style/`，UI 卡片入 Fold（验证/测试）与 held-out。test2 实测：测试月 β 0.26、α_ann −7.9%、R² 0.29、23/23 日覆盖。
- ③提前收官：`skip_to_heldout` 控制（≥1 冻结 Fold；worker 在下一个将运行的会话处跳出，held-out 用最新冻结产物；可取消）。④回滚：`rollback_fold` 回退到任一已记录 Fold——其后 Fold/后续元学习/全部 held-out 记录移除（账本先备份 rollback_*.jsonl、冻结产物归档 _archive/ 以避开孤儿检查与 _freeze 冲突），批准/重跑/GPU 控制清理后自动重启 worker。⑤下载收敛为单一 ZIP：移除逐文件列表/strategy-file 端点。⑥继承创建：`inherit_from` 下拉（有记录 Fold 的实验），创建时拷贝+哈希校验最新冻结产物至 `_inherited/`，worker 每次启动 hash 门重建为首 Fold 父产物。⑦GPU：`GET /api/gpus`（nvidia-smi 实况）+ Fold 门控处 `set_gpu_count`（1..16）→ `run_fold(sandbox_gpu_count)` 覆盖 SandboxSpec.gpu_count。
- 验证：full suite 564 OK（+9：skip/GPU 透传/继承种子+防篡改/回滚丢弃与归档/控制校验/equity 链接数学/继承导入）；实机重启控制台后 equity/style/gpus/schema 端点对 test2 全部实测通过；静态资产已 sync 前端且端到端健康。文档：pipeline_design §5、parameters_reference §9。

2026-07-08 前端 WebUI 本地用户隔离（feat/hitl-webui 线）

- 需求：前端服务器上仅本人账户（admin）与厂商控制台可登录并控制 WebUI，其余前端本地用户一律拒绝。登录面上一条目（指定 key 白名单 + AllowUsers）已覆盖；本条补齐控制面——原先前端任意本地账户都能 curl 回环 8080/38889 直达无鉴权控制台。
- 方案：nftables owner-match（`/etc/nftables.conf`，服务已 enable 持久化）：回环 8080 仅 root/admin/cornerhead（sshd 替 Mac 的转发连接）可连、回环 38889 仅 root/www-data（nginx 反代）可连，其余 uid 一律 TCP reset；规则用数字 uid 防启动期 fail-open。`frontend_setup.sh` 幂等管理。
- 验证：六路实测——root→8080、www-data→38889、cornerhead→8080、admin→8080 全通；sync→8080、admin→38889 被拒；provisioning 重跑幂等；端到端健康（nginx→隧道→socket）不受影响。

2026-07-08 前端 sshd 指定 key 白名单（feat/hitl-webui 线）

- 核查：前端虽已 key-only，但 `AuthorizedKeysFile` 是各用户自有 dotfile（被入侵账户可自我加 key 持久化）、无 AllowUsers；指定 key 全集 8 条（root 4、admin 2、cornerhead 2 受限 key）。部分既有 root key 权限过宽，Mac key 可获 root shell；收紧项已提议，用户选择只做核心三层。
- 方案落地：`AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u`（key 指定权收归 root，dotfile 全部失效）+ `AllowUsers root admin cornerhead` + `AuthenticationMethods publickey`；8 条 key 原样迁入；`frontend_setup.sh` 幂等管理（cornerhead 文件重写，root/admin 仅缺失时自举）。
- 实施带 120s 自动回滚保险丝（reload 后新连接验证成功才解除）。验证：新 root 连接 OK；把 cornerhead 的 home dotfile 移走后重启隧道仍认证成功（证明中央目录唯一生效）；非白名单用户 `sync` 被拒；provisioning 重跑幂等；前端端到端健康。Mac 侧登录待用户自验。

2026-07-08 控制台本地访问控制：Unix socket 化（feat/hitl-webui 线）

- 威胁核实：计算主机有 30 个可登录用户（当时 4 人在线），控制台原来的 127.0.0.1:38888 回环 TCP 对全部本地用户开放且 API 无鉴权。前端另有 admin 账户，用户决策本轮只做计算主机侧。
- 方案：console 改绑 Unix socket `.runtime/webui/console.sock`（目录 chmod 700 为强制边界——uvicorn 会把 socket 本身 chmod 666），内核按文件权限限定 lzp；`run_webui.py`/`server.run()` 新增 `--uds`（`--port` 保留仅供显式本地调试）；隧道改 `-R 127.0.0.1:38889:<socket>`（OpenSSH 远端 TCP→本地 socket 转发），前端 nginx、Mac 接入、permitlisten 键限制全部不动；健康检查改 `curl --unix-socket`。
- 顺手修复自引入缺陷：`start` 拉起的 console/autossh 继承了 grab_lock 的 fd 9，长驻持有 ensure.lock 导致其后每次 cron ensure 超时失败（约 25 分钟，栈本体未受影响）；spawn 命令补 `9>&-`。
- 验证：38888 无 TCP 监听、run dir 700、锁不再被长驻进程持有、ensure 静默通过、前端端到端（nginx→隧道→socket）含 SSE 实测通过；tests.unit.test_webui_backend + test_interactive_pipeline 41 OK；文档（deployment §10/§11 + pipeline_design §5.3）同步。

2026-07-08 控制台稳定性审计 + WebUI UTC+8 显示（feat/hitl-webui）

- 时区：前端所有显示时间戳统一按 UTC+8（Asia/Shanghai）渲染（app.js 新增 fmtTs/fmtTsTime，基于 Intl，与浏览器本地时区无关）；后端存储保持 UTC ISO 不变；registry 的 mtime 回退时间戳由 naive 本地时间改为 aware-UTC（否则会被前端按浏览器时区误读）。
- 稳定性审计（子代理深审代码 + 实机核查部署栈），修复 5 项真实缺陷：①SSE trace 流由同步生成器改 async——原实现空转睡眠也占用 anyio 线程池 token（仅 40 个），少量残留标签页即可饿死全部 API 含 pause/stop（最可能的无人值守宕机路径）；②ExperimentManager 加互斥锁序列化 create/start/control/delete——原 control.json 读改写并发丢更新（批准/指令静默丢失）、start_worker 检查-再-spawn 可双开 worker 撕裂账本；③write_json_atomic 临时文件名唯一化（原固定名并发写可把交错的坏 JSON os.replace 上位 → worker 直接 failed）；④pid 复用防护：status.json 记 `pid_start_ticks`（/proc stat 第 22 字段）且 status_pid_alive 校验、stack 脚本 alive() 校验 /proc/<pid>/cmdline——重启后 pid 号被复用不再误判"实验仍在跑"（死实验永远无法续跑）或"console 已在跑"（静默全站不启动）；⑤前端 sshd 配 ClientAliveInterval 30/CountMax 3（frontend_setup.sh 写入并已生效）——原 0 表示不探测，隧道非正常断开后 38889 旧监听可占用数小时使 autossh 重连一直撞墙。
- 运维强化：webui_stack.sh start/stop/ensure 共用 ensure.lock 文件锁（手动操作不再与 cron 竞态）、ensure 平时静默、console/keepalive 日志 >10MB copy-truncate 轮转、spawn 后确认进程存活否则报 FAILED；cron 块去掉外层 flock（脚本自锁）并已重装。
- 仅记录未实施的优化机会（详见审计报告）：trace/stats 每 5s 全量重读 JSONL、SSE 重连无退避、实验列表 O(实验数) 轮询无缓存、策略 zip 临时文件可能残留、run_* 沙箱目录随重跑累积（test2 沙箱已 9.3G）、analysis 重生成状态不落盘、沙箱 agent-uid 文件使删除实验后 work root 静默残留、/Data 已用 94%。
- 验证：full suite 555 OK（+1 pid 复用回归测试）；栈完整重启、cron 重装、前端端到端（nginx→隧道→API）SSE 流实测通过；deployment_documentation §12 同步（锁/轮转/ClientAlive/时区约定）。

2026-07-07 Broker 成交模型保真批次（fix/broker-fill-realism，叠在 afterhours 上）

- 部分成交评估（实证定案：**暂不实现**）：test2 全部 272 笔真实成交对照成交日真实分钟 bar——中位订单 ¥3.3 万、占日成交量中位 0.007%（p99 0.08%）、占中位分钟 bar 中位 2.8%（p99 42%），**无一超过整根 bar**；25% 参与率上限只会触及 ~5% 成交。¥100 万规模下全量成交假设的误差属二阶（低于固定滑点假设本身）；已记录重启条件（资金 ~千万级/单票集中/微盘策略 → 届时只做参与率上限档，不做队列/冲击伪精度）。
- 四项修复落地：①限价单改**严格击穿**成交（买 low<P / 卖 high>P；仅触及=排队未成交；开盘价优于限价照常按 open 成交）；②市价单激活分钟无成交 bar 不再过期——继续挂单至当日下一个有成交 bar（竞价单错过单一价 bar 转入连续撮合、失去免滑点；挂单期间按决策价 `reserve_price` 继续占用可用资金/保证金——顺带关闭了原先不可达的资金占用缺口）；③强平多头所得自动偿还融资负债（先息后本 FIFO，`debt_repaid(via="forced_close")`；主动担保品卖出与末日清仓不变）；④北交所手数规则改 100 股起、1 股递增（新 `broker_core.is_bse_market`）。
- 仅文档记录（无代码改动）：利息 /360 计息、无按月结息周期（应计挂合约、偿还时一次付清，风险公式已含应计、仅现金时序差异）；固定 5bps 滑点与订单规模/价差无关。env_design §3.2 + parameters_reference 落档。
- uptick 时点权衡（用户委托分析，结论：**不改**）：激活 bar 参考价不是缺陷而是延迟模型的正确语义——`execution_lag_bars` 建模决策→到达交易所的传输延迟，交易所在订单**到达时**对照最新成交价检查，激活 bar 开盘价即到达时点近似；改到决策时点反而引入回看失真。test2 的 21 次 uptick 拒单反映的是系统性做空面对的真实延迟风险；若认为分钟级延迟高估秒级现实，正确杠杆是 `execution_lag_bars=1` 而非移动检查时点。理据已写入 env_design §3.5。
- 验证：full suite 554 OK（+8 FillRealismTest）；`git diff --check` clean；PROMPTS.md 重导出（订单语义+手数规则）；提示词/模板同步严格击穿与市价挂单语义。

2026-07-07 盘后固定价格交易 tick 落地（feat/afterhours-fixed-price，叠在 fix/corporate-actions 上）

- 建模 2026-07-06 生效的交易规则修订：盘后固定价格交易扩展至全部 A 股（15:05–15:30 按收盘价撮合）。新增 `afterhours_decision_time`（默认 15:05，None 关）：最后一根真实 bar 后插入盘后定价 tick，`ctx.bars` 为已确认收盘 bar，订单**立即按官方收盘价结算**（无滑点/无延迟/不进簿，`price_label="afterhours_fixed"`）；逐票板块/日期资格 `broker_core.afterhours_available`（科创 2019-07-22 / 创业 2020-08-24 / 其余含北交 2026-07-06）；限价劣于收盘=无效申报；`short`/`fin_buy` 保守不支持（真实可用性未核验）；涨跌停/停牌/T+1/资金照常。旧 manifest 无该键按关闭回放（冻结评估可复现）。同修订的主板 ST 涨跌幅 5%→10% **无需改码**：涨跌停完全由 `stk_limit` 绝对价数据驱动（已实证 150 只主板 ST 在 07-03/07-06 的带宽切换正确）；基金收盘竞价/创业板做市/大宗确认不涉本系统。
- 数据口径两条已记入 data 文档风险表：2026-07-06 起日线量额含盘后成交（分钟量和≠日线量）；ST 涨跌幅制度断点使 `pct_chg≈±5%` 启发式失效。QMT 开放问题 +1：客户端内盘后定价申报未核验，核验前实盘执行器应忽略盘后订单。
- 验证：full suite 546 OK（+7 盘后 tick 测试：板块/日期资格矩阵、精确收盘价成交、关断与 5 分钟网格去重、不合格日期拒单、限价有效性、杠杆开仓拒单、T+1 次日 close）；`git diff --check` clean；PROMPTS.md 重导出；env/参数速查/QMT/data 四份文档同步。

2026-07-07 多空公司行为落地：除权日红利/送转进回放（fix/corporate-actions）

- 关闭第四轮审计 High「多头公司行为缺失」：raw 价回放把除权缺口记为多头纯亏损/空头意外盈利、红利不进收益与归因、且 PIT 可见的分红公告会教 Agent 在除权日前清仓（错误世界模型）。采用显式除权日事件（非复权价——涨跌停/手数/uptick/QMT 实盘全在 raw 价空间）。
- 回放槽新增 `corporate_actions.parquet`（已实施分红按 `ex_date` 入窗；公告晚于除权日的修订行剔除、最新公告版本胜出、同日同票求和；Environment 侧市场事实，Agent 可见性仍按公告日门控，无 PIT 泄漏）。`SimBroker.roll_to_date` 除权日盘前一次性处理：多头贷记现金（税前 ×(1−`dividend_tax_rate`，默认 0)）+ 送转增股（成本连续、红股按 `div_listdate` 解锁）；融券空头按税前全额补偿、逐张合约股数按送转调增（计费基数不变、持仓/合约不变量保持）；`last_price` 重定为理论除权价（停牌除权日权益也连续）。profile 弃 inert `short_corporate_actions`，新增被消费的 `corporate_actions="modeled"` + `dividend_tax_rate`；旧 manifest/旧回放槽兼容（缺文件=旧行为）。归因：红利入 long/short P&L（不进 trade_count/win_rate），summary 新增 `dividend_cash_received`/`dividend_compensation_paid`。已记录残差：配股、pay_date 滞后、零碎股取整。
- 验证：full suite 539 OK（+11：10 broker CA 测试含权益连续性/税率/锁定解锁/空头补偿与干净平仓/日历缺口/停牌除权/loader 回环/回放归因≈realized+红利；1 snapshot builder 测试）；`git diff --check` clean；PROMPTS.md 重导出（公司行为段+2 facts）；五份 living docs 中 env/data/参数速查同步。前置：上一会话遗留的 timeview 文本域 WIP 已按用户指示先行提交（8d5ac20）。

2026-07-07 HITL 交互式运行 + Web 控制台（feat/hitl-webui）

- 新增 human-in-the-loop 层：`pipelines/interactive.py` 的 `InteractiveExperimentRunner` 按 run() 同序驱动 run_meta_learning/run_fold/run_heldout，会话边界门控（auto/step 模式、pause/stop、逐会话批准+指令）；控制面 = `experiments/<id>/hitl/` 单写者原子 JSON（params/control/status/schedule）；账本级续跑（跳过已记录会话、父产物链按冻结路径重建+hash 核验、Taste 从 taste.md 恢复、held-out 按 `skip_labels` 只补缺失周期、孤儿冻结目录显式拒绝）。
- Pipeline 缝：`build_system_prompt` 新增「研究者本 Fold 指令（用户注入）」节（假设化措辞、不放宽硬约束），经 runner→run_fold(fold_directive=) 进 manifest+fold 账本（不进 agent 可见投影、不参与 hash）；`run_meta_learning(directive_override=)` 支持按 Epoch 覆盖实验级方向；`run_heldout(skip_labels=)`；审计 CLI 增 `--fold-directive-file`；provider/session 装配从 `scripts/experiments/_cli.py` 移至 `pipelines/assembly.py`（_cli 保留 argparse+re-export）。
- Web 控制台：FastAPI（`webui/`，默认 127.0.0.1:38888，无鉴权仅回环）+ 无构建 vanilla SPA（中文 UI，SVG 图表）；首页=实验卡片（进度/累计收益/逐 Fold 收益图/新建模态含全部参数中文说明/确认式删除）；详情页=会话导航、控制条、逐会话指令编辑与批准、SSE 实时尾随 agent_trace.jsonl、Step 历史、策略代码浏览+zip 下载；并行运行上限 4；worker 独立进程（start_new_session）与服务解耦。
- Fold 分析（guarded test view，用户定）：`pipelines/fold_analysis.py` 预定义中文模板经 LLMProxy 生成策略分析——输入只含验证期证据（投影排除 test_result/test_period），测试期结果在 UI 单独折叠「事后审计」区并警示不得写入后续指令；分析失败仅记 status 不阻断，可在控制台重新生成。
- 验证：full suite 511 OK（483→511：+15 interactive、+12 webui backend、+1 prompt）；`git diff --check` clean；PROMPTS.md 重导出（含 fold 指令示例节）；真实控制面 smoke（38888 端口）：列出 23 个历史实验只读、创建→step 门控 waiting_user→set_directive→stop 干净退出→resume 重启 worker→确认式删除全通；修 2 个 smoke 发现的缺陷（params 元数据键 `_created_at` 误拒；退出 worker 成僵尸致 pid 误判存活——status_pid_alive 识别 Z 态 + 服务 SIGCHLD=SIG_IGN）。
- 文档：pipeline_design 新增第 5 章（门控/续跑/控制台/防泄漏）；agent_design 补研究者 Fold 指令术语；parameters_reference 新增 §9 HITL 参数。后续部署形态（远端轻量服务器只承载交互层）已记入 §5.3。真实 DeepSeek+Docker 的完整 HITL Fold 运行待用户显式触发（与 RA4 同口径）。
- 前端服务器部署上线（三机架构）：①`docs/QMT_documentation.md` 重命名为 `docs/deployment_documentation.md`（git mv 保历史），新增 §10–§12 记录控制台部署（三机网络架构/前端部署与访问控制/保活与启动流程），QMT 章节 §1–§9 原样保留；五份 living docs 与 CLAUDE.md 引用同步（logbook 历史不改）。②架构：计算主机（教育网仅出站）--autossh -R 38889--> 前端 121.41.5.179（Debian12，仅 sshd 对外）nginx 127.0.0.1:8080（静态 SPA + /api 反代、SSE 不缓冲、hub 离线 502/504→503 中文 JSON）<--ssh -L 8888-- MacBook；访问控制=专用 nologin 用户 cornerhead 的 per-key authorized_keys（计算主机 key 仅 permitlisten 38889、Mac key 仅 permitopen 8080，restrict 无 shell/exec，实测 shell 被拒）。③保活：autossh -M0+ServerAlive 自愈断连，`ops/webui/webui_stack.sh {start|stop|status|ensure|sync|install-cron}` 单入口，cron */2 ensure（flock）+@reboot 已装；worker 分离进程不受重启影响。④上线验证：端到端健康检查过（nginx→隧道→本机 API）、SPA 资产 content-type 正确（修 tar 权限归一化与 /static/ alias 两个部署缺陷）、杀 console 后前端返回离线 JSON、ensure 自动拉起。Mac 侧一次性 ssh config 片段见部署文档 §12。
- 数字输入跟进：取消右对齐（保留 tabular-nums）；原生 WebKit spinner 不可样式化——统一隐藏，int 字段改自绘 ▲▼ 步进按钮（stepUp/Down + change 事件、hover/active 主题化、tabindex=-1 不抢焦点），float 字段（step=any 无法步进）保持纯输入。
- 表单区分度跟进：输入框与按钮此前同为近白底+同描边难以区分——输入类统一改「凹槽」视觉（--input-bg 调为页面底色调、--input-border 加深一档，聚焦时底色回白+accent 环），按钮字重升 600 保持白底动作感，数字输入右对齐+tabular-nums 与文本框区分，checkbox accent-color 主题化；.field 复合规则改 background-color 避免吞掉下拉 chevron。
- 按钮跟进：改扁平按钮体系——appearance:none 去除 WebKit 原生控件着色（Safari 上按钮/下拉的系统阴影与渐变即「显示缺陷」来源）、主按钮 hover 阴影移除（状态改走底色/描边加深，color-mix 计算按压色）、focus-visible 键盘环、下拉统一自绘 chevron（appearance:none 后补充可见性提示）。
- 导航栏跟进：topbar 设 min-height 4rem——首页含主按钮时最高，详情页右侧为空导致高度收缩、切页跳动；定高后跨页一致。
- 展示跟进：`20251101..20251130` 的 `..` 是账本/manifest 的内部序列化格式（period_bounds 亦接受其为输入），存储格式不动；前端新增 fmtPeriodRange/fmtDate，验证区间与 Held-out 起止统一渲染为 `2025-11-01 ～ 2025-11-30`。
- 详情页跟进：①「策略分析（LLM，仅验证期证据）」拆为独立卡片（与 Fold 结果平级，自取数：pending 自动轮询刷新、显示模型/生成时间/截断重试标记、重新生成后自动刷新）；②Held-out 冻结测试区加料——统计瓦片（累计收益/平均 Sharpe/最差单期回撤/正收益期数/多空累计贡献）+ 各期收益条形图 + 累计收益曲线（≥2 期时），表格增起止日期、多头/空头、订单数列。
- 配色跟进：盈亏色改 A 股惯例红涨绿跌——新增独立 --gain（红）/--loss（绿）变量（明暗双主题），.num/.tile-value/.metric 的 pos/neg 全部切换；状态语义色（completed 绿 / failed 红、danger 按钮等）保持不变，图表系列色（身份编码）不受影响。
- 图表跟进：逐 Fold 收益图（首页+详情共用组件）改为条内堆叠展示多/空——每根验证/测试条按多头（本色）/空头（同色系浅一档）正负向堆叠（2px 表面间隙、外端 4px 圆角），图例扩为四项，legacy 无拆解数据时回退纯色总量条、卡片 mini 图保持总量；撤销此前独立的多/空拆解图（首页+详情）。堆叠高度为多空归因之和，费用/未实现差额以悬浮提示中的总收益口径为准。
- 控制台五轮跟进（用户 8 点 + 1 追加）：①逐 Fold 收益图悬浮提示改多行富信息（验证/测试/多空拆解/状态），首页最佳区补多空拆解图，实验卡指标统一为与 hero/详情页同组件同顺序（Held-out→测试→验证）；②右上角残留 finish_reason=length 为已完成 worker 的陈旧 status.analysis_error——仅 worker 存活时展示；③最佳实验改按测试期平均 Sharpe 排序（缺失回退累计收益）；④会话切换改右栏原位替换（detailView+selectSession，不再整页重建/跳顶，SSE 与滚动保留；控制操作走 refreshDetail 原位刷新）；⑤修 .panel h4 压过 .section-gap 的优先级致 Taste 标题无上边距；⑥创建表单错误提示移至模态顶部并自动滚顶；⑦新增「编辑完整系统提示词」（fold 级 prompt_overrides 整体覆盖：runner 原样使用、不再注入运行时事实块，manifest/账本记 system_prompt_overridden；预览显示覆盖文本；删除冗余「保存指令」按钮）；⑧新增重跑最新 Fold（rerun_sessions[key]=rerun_id 幂等；仅最新已记录 Fold、需 worker 已停止；账本追加新记录、冻结产物 __r<id> 标签、held-out 自动重放；registry fold/held-out 均取每键最新记录）；⑨「策略分析（LLM…）」等小节标题升为 subsection-title 加重样式。full suite 527 OK（+3 测试），live 验证 override 设置/清除。
- 控制台四轮跟进（用户 6 点）：①收益图表增多/空拆解（fold 账本本就记 long/short_return，summary 透出 + 详情页新增测试期多空分组图；融资/担保品级归因 broker 未记账，留作后续）；②analysis_max_tokens 可配（默认 6000，PARAM_DEFAULTS→worker→analyze_fold→再生成服务全链路，length 重试取 max(16000,2×)）；③首页/详情页指标顺序统一为 Held-out→测试→验证→Sharpe→进度；④valid_00N 切换不再闪屏（旧内容降透明度保留至新数据到达）；⑤Fold 结果卡重排（状态徽标+指标瓦片+元信息 kv+指令折叠）；⑥信用账户拒单调查（test2 全部 10 次回测 271 单）：credit 3 成 36 拒且全为 short——21×uptick（策略有 0.5% 缓冲但参考价是滞后 2 bar 激活 bar，且专挑负动量票=监管规则本意阻断追跌做空）+15×保证金不足（策略用 available_cash 而非已暴露的 enable_bail_balance 定尺寸）→结论：Agent 逻辑缺陷为主、引擎忠实无误；系统侧改进=Fold 提示词 short 行补激活 bar 参考价与 enable_bail_balance 口径（PROMPTS 重导出）。full suite 525 OK。
- 控制台三修（用户 3 点）：①首页最佳实验区新增 Held-out 各期收益条形图 + Held-out 累计收益放大为首要瓦片（summary 增 `heldout_returns`；详情页 held-out 面板同图；新通用 `singleSeriesBarChart`）；②Fold 交易明细：`fold_orders[_csv]` 读 orders.parquet 出统计（单数/成交/拒单、成交额、按动作/账户、拒因、逐日金额、top 代码）+ 结果切换 chips + 80 行表格 + 完整 CSV 导出，测试期明细 CSV 收在防泄漏审计折叠区；③分析报错根因=DeepSeekConfig.max_tokens 默认 1200 而 analyze_fold 未覆盖（全文+思考必然截断 finish_reason=length）——显式 max_tokens=6000 + 模板限长 1500 字 + length 停止时 16000 重试一次；真实 DeepSeek 重生成此前失败的 fold_202512 分析成功（3127 completion tokens 无需重试）。full suite 525 OK。
- 回测加速 A/B/C 落地（用户批准全部四项，严格验收=完整 20 交易日前后对比）：新增引擎级基准 `scripts/dev/replay_benchmark.py`（复用 test2 冻结策略+已建快照视图，真实 Docker，零 token）。**基线 984.4s**（timeview 355 / strategy 327 / 未计 ~300，44 单，digest b90cb45…）。A（Timeview map_path 缓存、events 数据集签名缓存、sim_datetime lru_cache、去双重 _jsonable + bars 子树跳过深走）+ B（bars 列式传输 + 驱动端 `_LazyBars` dict 子类惰性视图，ctx.bars 语义不变，兼容旧列表载荷；镜像已重建 + Docker e2e 过）→ **616.9s（−37%），两次全窗对比 orders digest 与统计逐字节一致**。C1 盘外默认 15→30 分钟 + C2 新旋钮 `intraday_decision_minutes`（默认 1=逐分钟，竞价/盘外 tick 恒决策、Broker 仍逐 bar 撮合；engine `_is_decision_tick` + config/manifest/backtest 工具/agent 可见白名单/PARAM_DEFAULTS/表单/文档全链路）→ 30/5 档 **510.1s（−48%）**，且该策略订单流仍逐字节一致（决策点对齐竞价/调仓时点，不构成一般保证）。剩余大头 = timeview 262s（每 tick 5 域 cutoff 计算，refresh 有意逐 tick 保可见性时序）——后续项：按节点边界缓存 cutoff。full suite 522 OK（+3 测试；修 1 处读旧 bars 线格式的测试 fake）。
- UI 五轮 + 回测性能剖析（用户 5 点）：①全站改 rem 流式排版（html font-size = clamp(15px, 12px+0.3vw, 20px)，16" MBP ≈17.2px）+ 完整移动端适配（表格横滚、模态全屏、44px 触控目标、双档断点）；②token 统计拆输入/输出（trace/stats 增 llm_prompt/completion_tokens；`xxx k tokens`/`x.x M tokens` 格式）——test2 实测 3.76M 输入 / 39k 输出；③logo 精简为单直角括号+圆点；④创建表单移除元学习方向字段（详情页逐 Epoch 填写）；⑤回测耗时剖析（实测 phase_seconds + Opus 代码热点核对）：~147ms/tick × ~300 tick/日 是根因——timeview_build 60ms/tick（`timeview.refresh` 每 tick 重算不变的 `executor.map_path`（~6 次 Path.resolve 文件系统遍历）+ events 域每 tick 全表 `astype(str).unique()`）、strategy_compute 54ms/tick 中 Agent 代码仅 ~7ms（其余为全 universe bars 的 JSON 序列化→docker exec 管道 RPC→驱动重建×往返）、未计入的 ~33ms/tick 为 `_market_state` 装配（计时器外）；1368 个盘外 tick（23%）不能成交却走全路径；策略自身 substep 实测仅占墙钟 ~5%。五项优化建议已记录（缓存 map_path、瘦身 bars 载荷、盘外 tick 精简、events 签名缓存、sim_datetime 预解析），未实施待排期。full suite 519 OK。
- 实验复盘 + UI 四轮迭代（用户 12 点）：先核验 test2/run_ac0514ed9b35 trace 符合预期（44 LLM 调用中位 8s、72 shell、4 explore、6 回测其中一次 882s）——「收尾很久」实为倒计时未计回测回补：名义 deadline 已过 21min 但 6 次回测累计 ~2195s 墙钟全额回补推理额度，属设计内。修复与新增：①顶栏缩放选择器 90–150%（localStorage 按设备记忆，解决转发端口 vs VS Code 内嵌浏览器字号差异）+ CornerHead SVG logo（corner 括号+head 圆点渐变）；②批准前系统提示词审阅：`prompt-preview` 端点装配 Fold/元学习完整提示词（含 Taste/指令，运行时事实块以 Fold 信息+验收规则代替且同样不含测试排程），指令面板「预览完整系统提示词」→ 模态审阅 →「确认无误批准并启动」；③回放 trace 可收起 + 分批加载（每批 ~2MB）+ 原始 .jsonl 下载（`trace/download`）；④测试周期命名保留（Fold 以测试周期命名符合 pipeline 语义），表单标签/帮助澄清 + 动态提示「验证区间=前一周期」；⑤trace 内存三方案评估后采用方案三+一（LLM 自然语言全文渲染、推理过程与工具载荷惰性展开、完整 JSON 点击才注入 DOM、活动流上限 400 事件、附完整下载链），弃纯截断与全量渲染；⑥删首页「打开」按钮，卡片整体可点（次级按钮 stopPropagation）；⑦首页顶部最佳实验区（按累计测试收益，含瓦片+双图表）；⑧参数面板扩容：Step/回测/NL 预算、回放执行、Broker 资金/费用/持仓上限、元学习记忆/派生镜像共 ~20 项经 PARAM_DEFAULTS→build_config_from_options 单源生效（多数收「高级参数」折叠区，新组 回放执行/Broker 账户）；⑨主题切换只就地重绘 SVG 图表、会话导航复用缓存的实时 trace 面板（SSE/滚动位置/已累计事件跨渲染保留，仅离开实验才销毁）；⑩实时统计仪表（`trace/stats`：LLM/搜索/抓取/回测/Shell/Explore/压缩次数、回测累计墙钟、Σtokens），元学习与普通 Fold 通用，回放区也显示一次性统计；倒计时改为「名义 deadline+回测回补」，回测执行中显示独立计时徽标；⑪周期下拉按交易日历∩daily/分钟线分区覆盖裁剪（quarter 66→26：2020Q1..2026Q2，分钟线 2020-01-02 起），杜绝选到无数据区间；⑫全部经真实运行中的 test2 实验在线核验（stats/preview/schema）。full suite 519 OK。
- UI 三轮修复（用户 6 点）：①品牌改名 CornerHead（页面标题/顶栏/FastAPI title）；②全局 UI 再放大（基准 16px，按钮/表格/badge/表单同步加大）；③元学习指令不再需要重输——详情页指令框预填创建时的 meta_learning_directive（留空本就回退该默认，纯 UX 修复）；④会话批准后新增「沙箱与数据快照准备中（已 mm:ss）」旋转指示器（首个 trace 事件到达即隐藏）+ Fold 推理倒计时 badge——StatusReporter 心跳线程从活跃 run 的 run_manifest 读出 fold_deadline_at 并连同 session_started_at 写入 status.json（+1 单测）；⑤URL 中 %2F 经查功能无碍（hash 路由 decode 正确），仍改用 `~` 分隔（epoch_001~fold_2022Q1），旧链接兼容；⑥新增浅/深主题切换（localStorage 记忆、默认跟随系统、全 CSS 变量化），深色图表换用色板深色档 #3987e5/#199e70（validator 在深色面板 #1b1f28 上全项 PASS，CVD ΔE 69.8）。full suite 514 OK。
- UI 二轮优化（用户 5 点）：①四个周期参数改为依赖 Fold 周期的下拉选择器——服务端从 SSE 交易日历枚举完整可回测周期（实测 quarter66/month198/week836/year16 与 daily 数据 2010–2026 覆盖一致）并给推荐默认（近 4 期 development + 最新完整期 held-out），无日历时退化文本输入；修 web_search_engines 多选交互缺陷（multi-select→复选框组）；②表单隐藏运维参数（路径 4 项、两个 key 环境变量名、local_dev，API 仍可设）；③模型选项删 deepseek-chat/reasoner；④响应式+字号放大（15px 基准、三档断点、笔记本可读）；⑤图表按 dataviz 规范重做——校验过的双系列色板（验证 #2a78d6 / 测试 #1baf7a，CVD ΔE 73.6、aqua 亚 3:1 由表格/悬浮提示补偿）、≤24px 圆端条形+2px 间隙、2px 累计收益折线+8px 白环标记、发丝网格、悬浮 tooltip、图例，详情页新增统计瓦片行+累计收益曲线。full suite 513 OK。

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

2026-07-07 Remove Agent-facing valid_bars

- 删除 `ctx.broker` 下单原语和 `SimBroker.passorder` 的 `valid_bars` 便捷参数；限价单改为当日有效，直到成交、策略显式 `cancel()` 或日终清扫。
- Prompt、模板 README、环境文档和相关测试同步改为用 `pending().age_minutes` + `cancel()` 管理“N 分钟后撤单”，更贴近 QMT `passorder` 没有 TIF 参数的真实接口。
- Validation: 非历史文件 grep 无 `valid_bars` / `remaining_bars` / `expired_unfilled` 残留；`~/miniconda3/envs/quant/bin/python -m unittest tests.unit.test_broker_engine tests.unit.test_main_ctx_replay` -> 143 OK；`py_compile` OK；`git diff --check` clean。

2026-07-10 Living documentation convergence audit

- 串行逐行审读 Data、Environment、Agent、Pipeline、Deployment 和参数参考共 3,266 行；统一职责、PIT/回放、完整验证、QMT 原子传输、条件产物和配置入口表述，并把非必要默认值集中到参数参考。
- 参数参考覆盖全部 `PARAM_DEFAULTS` 及 Snapshot、Experiment、Acceptance、Modification、Broker、Sandbox、Agent Session 和 Context Compaction 配置字段；明确仍存在的审计限制和代码级配置边界。
- Validation: `~/miniconda3/envs/quant/bin/python -m unittest discover -s tests -t .` -> 596 OK；Markdown 相对链接、表格结构和参数覆盖检查通过；`git diff --check` clean。

2026-07-11 Exact opening-auction source correction

- 实测确认 `stk_auction_o` 与09:30分钟 Bar同源，`stk_auction_c.vwap` 也不是收盘集合竞价单一清算价；移除两者的 Snapshot/Broker/调度集成，改用 `stk_auction`（2025-01-16起）提供精确开盘竞价价量，收盘竞价继续使用15:00官方收盘价。
- 2025-01-16以前保留带来源和规则标记的09:30量额代理；`stk_auction` 缺失价格但量额有效时用 `amount/vol` 恢复，明确零成交时不虚构竞价成交并将订单滚入连续交易。
- 回填357个交易日、1,967,391行，无重复键；完整619项单元测试、真实异常分区验证、配置JSON与 `git diff --check` 通过。

2026-07-11 test7 investor-QA snapshot fix

- `test7` 首次元学习在构造文本 Snapshot 时失败：新接入的上证/深证互动问答使用 `q/a` 字段，通用文本映射未识别。增加数据集专属结构映射，以问题为标题、问题和回答为正文，并加入回归测试。
- 重启 `test7` 后已越过原失败点，完成 Snapshot 构造并进入元学习 Agent 会话；worker 心跳和截止时间正常。针对性17项测试及 `git diff --check` 通过。

2026-07-12 CornerHead researcher SSH key authorization

- 前端 root-owned 中央白名单新增研究者 key 指纹 `SHA256:Yrf1ner18N/lR630/a4gVz23lYPKsNYOPYlO7V8ry6c`。
- 该 key 仅允许本地转发到 `127.0.0.1:8080`，不授予 shell、exec、PTY、agent forwarding 或任意目标转发能力。
- `frontend_setup.sh` 已同步，确保后续幂等 provisioning 不会移除授权；`sshd -t`、文件属主/权限、精确条目和脚本语法检查通过。

2026-07-12 QMT 节点边界与 SSH 接入文档纠正

- 确认 QMT Windows 节点为 `39.105.46.212`；`22/SSH`、`3389/RDP` 可达，SSH 服务为 OpenSSH for Windows 9.5 且仅接受公钥，本机现有密钥未获授权。
- 删除历史日志中把无关反向隧道归为 QMT 的错误表述；保留 `121.41.5.179`，其唯一项目职责是当前仍在线的 CornerHead 前端。
- `deployment_documentation.md` 补充复用本机前端 SSH 身份、Windows 管理员/普通用户 key 路径、ACL、主机指纹固定、本机 `qmt-node` 直连别名和只读验收步骤；仓库不保存远端账号、私钥或口令。
- Validation: 全仓运行时路径无旧跳板别名或端口依赖；`git diff --check`、Shell 语法和旧关联词检查通过。

2026-07-12 大 QMT 只读文件桥示例

- 实机确认大 QMT 内置 Python 库已安装：`bin.x64\Lib` 与 `site-packages` 均存在，`XtItClient` 正常响应。
- 新增 `ops/qmt/qmt_readonly_bridge.py`、远端账户配置样例和导入说明；QMT 内部每 15 秒查询 ACCOUNT/POSITION/ORDER/DEAL，原子写入 `C:\xquant\outbox\account_snapshot.json`。
- 示例不含协议版本号、`passorder`、撤单、线程、阻塞循环或网络服务，只用于模拟模式下验证大 QMT→文件→本机的只读链路。
- Validation: Python 3.6 语法解析、桩 QMT 对象查询/快照写入、ASCII/GBK 源码兼容检查和 `git diff --check` 通过；未上传远端、未导入 QMT、未执行交易操作。

2026-07-13 Opening-auction observed-availability PIT fix

- 新增09:27盘前 `stk_auction` 完整性轮询、09:31重试和晚间精确恢复；按业务键、稳定读、上一交易日行数下界验证，未就绪且未写盘时恢复原 committed generation，节假日在 generation fence 前跳过。
- Snapshot/Timeview 使用分区首次实际落地时间；旧历史保守记09:29，内容修订推进可见时间。09:25改为盲 tick，删除从未来09:30/09:31分钟条反推开盘价；盘前结果 tick 仅研究唤醒，Broker 隐藏清算真值仍可执行09:15委托。
- 同步 Prompt、模板和 living docs；相关 data/environment/tool 回归共374项通过，`git diff --check` 通过。生产 crontab 留待本提交后安装并复核。

2026-07-13 Raw data recent-week archive

- 按 `data/raw` 最新日期分区 `20260713` 取最近 7 个自然日（`20260707`–`20260713`），打包 47 个数据集的 576 个 Parquet/元数据文件。
- 产物 `data/exports/raw_last_7_days_20260707_20260713.zip` 为 98,480,953 bytes；7-Zip 完整性测试通过，SHA-256 为 `a9e3d7ffe1c57fc4e9f9204a61b236514fa014b9c929c61c1da2f67ded8253c2`。

2026-07-13 分钟回放逐日加载与单日前瞻

- 分钟时间解析改为唯一值字典展开；正式回放按交易日读取，只前瞻下一日，Timeview 放行后释放分片；不新增数据湖、容器或跨 Fold 预取。
- `lap-test9` 61 日观测回放由 1169.4 秒降至 910.9 秒，宿主峰值约 99.7 GiB 降至 34.7 GiB，预取等待合计 0.0025 秒；40 个非性能结果字段完全一致。20 日同代码 A/B 为 135.68/135.86 秒且订单哈希、收益一致，小窗口无实质加速或退化。
- 受影响模块 338 tests、全量 749 tests 通过；结束时主机 416 GiB 可用内存。

2026-07-13 新实验默认逐 Step 与启动监测

- 新实验 `initial_control_mode` 默认值由 `manual` 改为 `step`；运行时控制文件缺失/损坏时的安全回退不变。
- 重启 WebUI 后在线参数接口已返回 `step`；WebUI/HITL 相关 74 tests 通过。
- `lzp-test10` 决策快照约 260.9 秒、Replay 缓存约 150.7 秒，均与上一轮相近；无错误，资源余量充足，不提高并行度。

2026-07-13 Probe 安全错误反馈

- Probe 保持隐藏原始策略异常，但现在为账户视图误调用、substep 外访问状态、错误 universe 路径、策略导入失败和决策超时返回稳定错误码与安全修复提示；未知异常仍返回通用提示。
- 未新增 API、兼容路径或硬围栏；仅补充两条接口备注。工具流 99 tests、主回放 63 tests 通过，`py_compile` 与 `git diff --check` 通过。

2026-07-13 Probe 日期合同与回放热路径优化

- `replay_window=N` 现为 N 个策略日另加 1 个退出日，并分别报告 `replayed_trade_days` / `replayed_exit_days`；错误的 Timeview `.parquet` 路径返回安全 `asof_path_mismatch` 提示。
- Broker 每 Tick 只建立一次相关代码行情索引；行情数值列改为保持旧异常语义的向量化编码；主 RPC 使用紧凑 JSON。微基准分别约快 8.9 倍、6.6 倍，合成载荷缩小 6.5%。
- Timeview 不改：普通无边界调用约 4.9 微秒，条件刷新收益不足且会扩大合同风险。相关模块 272 tests、全量 756 tests 通过，`git diff --check` 通过。

2026-07-14 lzp-test13 回放生命周期修复

- `lzp-test13` 在 Q1 确认 Broker 同 Bar 重复冻结缺陷后正常终止，保留全部产物；仅 Meta 入账，未进入 Q2/Held-out。数据全程固定 generation `4346853b403a4eaeadb9a3cfe71aa939`，typed-empty auction、Timeview 与 Formal 挂载均正常。
- Broker 撮合改为 FIFO 即时释放已成交/拒绝单，保留更早仍挂单的冻结；Probe 增加未提交原因和三类安全策略拒单聚合；worker 恢复 `SIGCHLD` 默认处理；三类执行器固定 `PYTHONHASHSEED=0`。
- `ctx.state_dir` 改为 substep 首次访问才复制，纯 Broker block 不再建暂存树；Prompt、合同和模板明确 tick 入口账户快照及批量本地预算。
- 全量 787 tests、真实 Docker E2E 2 tests 通过；重建镜像 `af95fb629d7b`，内置 driver/source hash 均为 `cc2fe3e61e31`。分钟预取等待仍约 0.001 秒，不增加加载并行度、不复用 Formal 容器、不引入读优化湖。

2026-07-14 lzp-test14 诊断与首 Fold 预取

- `lzp-test14` 固定 generation `941d89e6afaf4cfdbfa295e139fb4a64`；Meta 总耗时 703.1 秒，Q1 数据串行准备约 461 秒。Q1 的 30 次回测额度全部用于 3 日 Probe，10 次零订单成功、20 次策略失败，未运行完整 Valid；确认不可恢复后优雅终止，所有产物保留。
- 数据、typed-empty auction、PIT、Docker、IPC 和 worker 均正常。15 次同类失败来自把字符串 `ctx.cur_datetime` 再调用 `.isoformat()`，3 次来自 Agent 的 Pandas 索引不对齐；分钟读取约 1.93 秒、归一化约 4.89 秒、预取等待近零，故不提高加载并行度。
- Probe 仅为直接 `ctx.cur_datetime.isoformat()` 增加固定安全合同提示，并同步 Prompt、模板和 Environment 文档；未知异常仍为 host-only。02:30 事件/资金审计统一按最晚发布的次晨两融边界使用 D-2，09:20 的 D-1 审计不变。
- `auto` 首 Fold 的现有四项宿主缓存改在 Meta Agent 就绪后预取；不创建/预启动容器，不挂载隐藏数据，进入 Fold 或 Held-out 前等待。rerun、动态 skip 和未使用预取失败均有回归。
- 真实调度/SSE 日历 Case Study 覆盖周二、周三、周六、周日：02:30 与 09:20 边界互补且周日重复审计可跳过；281,115 个 raw、754 个 PIT 文件及 release/pin/质量 hash 一致。当前 event-flow 的 2 个 error 是旧调度留下的当日两融误报，待下一次 cron 重写。
- 全量 794 tests、真实 Docker E2E 2 tests 通过；镜像 `sha256:6af1d9b40c27` 的 driver/source hash 均为 `bbbd8160a31b`。结束时 452 GiB 可用内存、8 张 L20 空闲、load 约 3/192 核。

2026-07-14 lzp-test15 NL 回测诊断与合同修复

- Q1 最终完成并冻结 `valid_018`；Q2 四次完整 Valid 分别因 30 分钟决策上限、substep 外计算上限、4 分钟和 20 分钟 research 声明上限失败。根因是策略每约 5 个交易日执行 4 次真实 `ctx.nl()`，Probe 的 30 次 NL 均被 withheld，52 秒结果只测到了退化路径，误导 Agent 外推正式耗时；数据、PIT、Docker 和 generation 均正常。
- Probe 新增 `runtime_representative=false` 和固定警告；正式 Valid Trace 显示当前 NL 序号与耗时。个股正文检索限定到候选关联文档，同一 PIT 时点复用可见索引；真实 378.9 万行文本基准的无命中个股检索由旧基线 15.77 秒降至首次 11.59 秒、同一时点后续 9.32 秒。
- Timeview 接受带明确 Arrow 类型的零行 Parquet，DuckDB 误读数据集目录时给出 `*.parquet` 安全提示。Q2 已优雅终止且产物保留；修复后无需重跑 Meta/Q1，只需按需续跑未冻结的 Q2。
- 全量 797 tests、真实 Docker E2E 2 tests 通过；新镜像 `a11c20f242da` 与源码 driver hash 均为 `ad343183a9cf`，结束时 445 GiB 内存可用、8 张 L20 空闲。

2026-07-14 Probe 迭代收敛提示

- 每次回测成功、ToolError 或额度耗尽均返回已用/上限/剩余次数；零订单且存在吞宽泛异常分支时返回一条关联的非阻断诊断。
- Fold Prompt 用一条最小垂直链路工作流替换笼统“小步修改”：先验证读取、选股、下单和 T+1 主动退出，再逐个加组件，并为完整 Valid 与必要复验保留额度。相关 161 tests、全量 799 tests 通过，提示词导出与 `git diff --check` 通过。

2026-07-14 准备阶段 Trace 状态修复

- `lap-test15` 首个 Meta 数据构建期间，心跳提前发布尚不存在的 Trace 路径，前端误显示“正在加载 Agent Trace”。后端改为文件实际出现前保持 `trace_path=null`；前端同时以 Trace 路径和 deadline 共同确认就绪，可覆盖已启动的旧 worker。复用现有准备文案，不新增状态或中断实验。Interactive/WebUI 83 tests 通过。

2026-07-14 研究者等待时间排除

- Step 门控与 ask_user 等待除继续回补 Agent deadline 外，现单列 `researcher_wait_seconds`；实时 Fold 耗时暂停，完成记录的 `run_wall_seconds` 扣除等待。回测、数据准备与 Environment 验收仍计入有效耗时。
- 新实验的 Fold/Meta 推理默认值在配置、交互创建和两个 CLI 入口统一由 60 改为 20 分钟，显式参数仍可覆盖。相关 Pipeline/Runner/WebUI 257 tests 通过。

2026-07-14 逐 Step 首 Fold 提前预取

- `lap-test15` 的 Q1 在审批后仍准备 6分39秒；Decision 与 Replay 分别完成于11:55:51和11:58:45，分钟 Replay 临时缓存约2.0 GiB，进程平均约1.2核且主机仍有432 GiB可用内存。
- 复用现有单线程预取 Future，取消仅 `auto` 模式可在 Meta Agent 就绪后启动的限制；逐 Step/手动模式也与 Meta 推理及研究者审批重叠，正式 Fold 前仍强制等待完成。Interactive Runner 27 tests 通过。

2026-07-14 ctx 路径字符串合同修复

- `lap-test15` Q1 按 Prompt 示例直接执行 `asof_dir / "daily"`，第二次 Probe 因 `ctx.asof_dir` 实际为字符串而失败；该实验随后正常停止，无残留进程。
- Prompt、模板和文档统一要求 `Path(str(ctx.asof_dir))`；Probe 只对策略栈内精确的路径变量字符串 `/` 错误返回固定提示，其他字符串错误仍保持通用脱敏反馈。
- 组合回归同时修复交互 worker 结束后未恢复调用方工作目录的问题；独立 worker 运行期目录不变，退出后不再污染同进程调用方。
- 全量 802 tests、真实 Docker E2E 2 tests 通过；新镜像 `e3efc73818c9` 的内置 driver 与源码 hash 均为 `3fd10ad5e74b`。

2026-07-14 WebUI 默认推理时长部署

- 源码已为20分钟，但06:53启动的旧 WebUI 参数接口仍返回60；重启 console 与反向隧道后接口返回20，本地和前端端到端健康检查均正常。

2026-07-14 lap-test16 首 Fold 预取实测

- Meta 准备约6分57秒、Agent约7分24秒；Q1 Decision/Replay全程与Meta Agent重叠，Meta完成后仅等约31秒。
- 相比 `lap-test15` 进入Q1后等待6分39秒，减少约6分08秒（约92%）；不增加容器、缓存层或加载并行度。

2026-07-14 策略冻结特征复用

- `lap-test16` 完整 Valid 的策略计算占 473.9 秒；默认模板现对冻结 `snapshot_dir` 只读一次、只投影需要列。
- Prompt 明确全局 `asof_version` 会被分钟域推进；滚动重型特征应在固定研究时点按实际日期或策略 key 缓存，避免无关分钟更新反复重算。
- 不新增 Environment 缓存、接口或硬围栏；模板与工具流 119 tests 通过，提示词导出、Python 编译和 `git diff --check` 通过。

2026-07-15 审计修复批次（check.md 全量落地）

- 按三轮审计合并清单 `check.md` 实施修复，共 9 个本地提交（fix/audit-remediation 分支）：Broker 零股一次性卖出与盘后/现金操作路由、Probe `missing_key_in_strategy` 提示、筛选放行指数级 events 行、回放槽以决策锚点为可见下界（缓存格式 v3）、冻结前统一收口+双 hash 验收+完整性异常终止、恢复期镜像 fail-fast 与 digest 校验、控制台 API allowlist 与 test 揭示门禁、QMT 桥/监控按日去重+严格 JSON+逐单日志、trim 空改写修复与 Explore 用量入账、遗留别名/死代码清理、逐 tick 行情二进制打包（RPC 编解码 23.3ms→2.2ms/tick，约省 390s/61 日回放）。
- 文档同步：环境/管线/数据/部署/参数六文档与提示词全部对齐现行为；QMT §4.2–4.3 改写为已实现 schema v2 三重闸门；PROMPTS.md 两次重导出。
- 三个切片显式缓行并记录触发条件（见 check.md 状态注记）：质量报告 changed_datasets 精确门禁、Timeview SSE 节假日日历、params.json 完整 resolved config 持久化。
- Validation: 全量 `~/miniconda3/envs/quant/bin/python -m unittest discover -t . -s tests` -> 826 OK（新增约 21 项回归）；真实 Docker E2E 2 tests OK；沙箱镜像重建且内置 driver 与源码 md5 一致；`git diff --check` clean。

2026-07-15 lap-test17 竞价无观测行崩溃修复与合规复审

- `lap-test17` 在 Q3 test_replay 预取构建时因 `stk_auction` 全 NaN 行触发 `invalid quantity combinations` 崩溃。根因：2025-08-18~20 北交所换号窗口，源端把停牌股、退役旧代码别名与正常交易的 920 新码统一以价量额全 NaN 列出（全库仅 4 个分区共 821 行，另 0908 一行停牌股）。定性为源端「无竞价观测」形态而非数据损坏。
- 修复：`_build_auction` 将三项全 NaN 行按缺行剔除并计入 `price_quality.unobserved_rows_dropped`（Broker 沿用带标记 09:30 代理），部分 NaN/负值等真不一致组合仍 fail-fast；raw 分区不改写。真实湖 20250815–20250910 重建通过（103,554 行保留、剔除 821）。实验可直接按账本续跑（Meta/Q1/Q2 已冻结，缓存完好，仅重建 Q3 test_replay）。
- Opus 合规复审（10 个提交 × 三原则 + 数据/文档扫描）：整体合规；修正其发现的 3 项——share_float union 成功路径 NameError（补 `existing_rows` 绑定 + 成功路径回归）、QMT 桥 `processed_payloads` 补按日键控与剪枝、部署文档 §2.2/§3「双确认」措辞统一为三重闸门。Timeview 新 schema 告警改为每域一次并豁免 `available_at_rule` 注记列（消除 469 次噪声）。
- lap-test17 案例复盘：trim 修复实测有效（Q1/Q2 缓存命中 91.9%/84.2%，未复现 lap-test16 的 31.2% 崩塌）；预取按新门控时点触发；崩溃前 Meta+Q1+Q2 均正常冻结。
- Validation: 全量 unittest -> 830 OK；`ast.parse(3.6)` 与 ASCII 校验通过；`git diff --check` clean。

2026-07-15 控制台「沙箱镜像」误报排查

- lzp-test18 详情页元学习面板下的 `沙箱镜像` 行貌似报错，实为成功构建：BuildKit 把正常构建进度写到 stderr，而前端将整条 `sandbox_image_update` 记录 `JSON.stringify` 原样倾倒，4KB 的 `stderr_tail` 日志墙读起来像错误。账本与实验状态核实无任何故障（status=ok、returncode=0、派生镜像已被 Q1 使用、worker error=null）。
- 修复：`app.js` 新增状态感知渲染——成功仅显示镜像 tag、短 digest、耗时、GC 数；skip/disabled 显示一句话说明；failed/timeout/rejected 才显示红色错误块 + 日志尾部。数据层不动（宿主侧审计记录合理，Agent 可见投影本就剔除 stderr_tail）。
- Validation: `node --check` OK；DOM shim 下对真实账本记录及 4 类失败态渲染断言通过。
- 部署：生产页面的静态 SPA 由前端机 nginx 本地副本提供，须 `ops/webui/webui_stack.sh sync` 推送；已同步并重启 console（旧进程早于本批 server.py 变更，缺 no-store 中间件）。端到端健康 ok，lzp-test18 worker 不受影响（独立会话，心跳正常）。

2026-07-15 策略分析审阅人 PIT 接地

- lcd-test1_ 的 Step 分析（deepseek-v4-pro）把 `ctx.snapshot_dir` 误判为前视泄露：审阅人只拿到策略代码+验证结果，没有 ctx 数据面契约，于是臆测冻结快照可能含未来数据。经核实该 Fold 决策锚点 2025-12-31 23:59:59 早于验证期 20260101–20260131，快照物理上看不到未来，属误诊；真正可疑处是近乎空仓致夏普虚高（其自身假设 #2）。
- 修复：在 `fold_analysis.py` 抽出共享常量 `_ENV_DATA_INVARIANTS`（snapshot_dir 冻结基线 / asof_dir 逐 tick 滚动 PIT / 实时走 bars·price / PIT·T+1·substep·筛选由环境强制），注入 STEP 与 FOLD 两个系统提示，并把审阅重心从「指控环境泄露」转向「策略是否误用接口、收益是过拟合还是结构性」。仅提示词接地、无新围栏、无契约变更。
- Validation: 导入与两提示均含该块、无 f-string 花括号泄漏；test_interactive_pipeline 44 OK；export_prompts OK。

2026-07-16 NL 检索可见性掩码向量化（lzp-test18 回测提速）

- lzp-test18 的 valid_002 回测耗时异常（~54min），逐层剖析：Q2 策略改用大量「泛市场」`ctx.nl()`（无 ts_code，~21 次/回测，每次~163s）。非 LLM 部分（~128s/次）主由 `TextRetriever._visible_index()` 主导：用 per-row `pd.Timestamp()` lambda 在 465 万行文本索引上重建可见性掩码 ~48s，且每个决策 tick（as_of 变化）都重算。
- 修复：把每个数据集的 cutoff 只转换一次，再用向量化 dict `.map` 广播，替换 per-row lambda。真实语料实测 47.9s→0.86s（~53x），可见行集合逐位相同（2,982,163 行，多个 as_of 验证）。单次 ctx.nl() ~163s→~70s，回测墙钟约减半。
- 过程更正：先试过用 semi-join 跳过 PIT 不可见 body 行，实测 0 收益（asof 语料已按 tick 裁剪，无未来行可跳）、且 Opus 交叉审计判定为回退（DuckDB 1.1.3 不把 semi-join 下推到 regex 之下，反而拖慢按股 IN-list 热路径）——已整体回退，仅保留向量化。补 PIT 不可见 replay 行排除回归测试。
- 合规：3 行改动、无新子系统/缓存（极简）；证据与 PIT 门控完全不变（贴近真实）；不动 NL 预算/接口，Agent 自主性不变。运行中的 worker 仍是旧码，重启后生效；未重启在跑实验。
- Validation: 全量 unittest 831 OK；Opus xhigh 审计判定「exactly semantics-preserving and safe」。

2026-07-16 控制台：terminated 实验缺少「恢复运行」按钮

- lzp-test18 的 worker 在 Q2 自行 terminated 后，前端无「恢复运行」按钮。根因：后端 `manager.py` 的 `_TERMINAL_RESUMABLE_STATES` 已含 `terminated`（control:resume 会按账本重启），但前端 `app.js` 两处按钮门控用的是漏了 `terminated` 的过期状态列表，与后端漂移。
- 修复：抽出共享常量 `RESUMABLE_STATES`（镜像后端集合），列表页与详情页控制条统一引用；terminated 现可在 UI 恢复。已 `webui_stack.sh sync` 推送前端静态副本，浏览器刷新即见按钮。仅前端；无后端/环境/Agent 改动。
- Validation: `node --check` OK；前端副本含 RESUMABLE_STATES；后端 resume 早已支持 terminated（无需改动）。

2026-07-16 lzp-test18 NL 事件复用与候选检索缓存

- 用户暂停后的最新 worker 已为 `terminated`；未重启、未改实验产物。保留的 Q2 审计 workload 为 25 次单股 NL、18 个代码、130 次检索（`.runtime/sandboxes/lzp-test18/run_4caab624069f/.../.nl_tool_52818ae56e5b`），旧检索合计 2309.140s。
- 单股检索改为硬候选边界：精确代码行映射 + DuckDB 持久标题视图定位代码/名称关联材料；有界 LRU 缓存候选索引，并只批量读取当前 PIT 可见的候选正文，随回放时钟增量补入新行。后续模式不再扫描 454.3 万行索引或 1.6GB 正文；全市场无代码请求合同不变。
- 同代码/Prompt/参数/公司上下文的成功分析按 PIT 事件 revision 复用；过滤器来自 Sub Agent 自主形成的实质检索模式。纯代码/名称发现查询忽略，只有可安全分离的前置候选限定才转成事件部分，其余正则保守退回全候选 revision；新增匹配标题/正文即重跑。失败和 general 结果不缓存，summary 新增 executed/hit/miss 计数。
- 合规收口：单股任务强制沿用策略请求代码，Sub Agent 不能用工具参数跨候选；修复任意删实体可能生成非法 RE2 的问题；候选正文不预读未来行，避免答案虽过滤但耗时依赖未来候选量；持久 DuckDB 连接在回放后确定关闭。均无新 Agent 参数/服务/持久产物。
- 最终真实 workload 重放：130 个原始工具参数、同一 frozen/replay 快照和逐日 08:00 PIT 时钟下，检索 2309.140s→118.808s（19.44x），初始化 15.928s；5 个原始非法模式和 1,172 个返回命中保持一致，重复候选完整多轮 0.178–0.618s。事件门控保守命中 3/25，省 62.314s provider；含 cache revision 的检索/控制 123.540s + 剩余 provider 426.914s，25 次 NL 任务估算 2778.870s→550.454s（5.05x），168 次季度调用约 62min NL，仍需正式重跑实测。
- 剩余瓶颈为 provider 多轮（估算占 77.6%）。若重跑仍不满意，下一优先级为对回答质量做 A/B 后减少 provider 轮次；跨回测内容寻址缓存和正文 sidecar 的复杂度/收益比暂不成立。18 个候选缓存索引+正文仅约 12.9MiB，128 项 LRU 不构成当前内存压力。
- Validation：`py_compile`；NL + tools-flow 152 OK；全量 unittest 841 OK（119.816s）；Prompt 重导出；`git diff --check` clean。全量测试前后可用内存均 314GiB；未启动 GPU 工作负载。

2026-07-16 lzp-test18 显式事件门控、enum 快路径与 Q4 性能收口

- 以显式 `event_filter={patterns,lookback_days}` 取代从模型检索词猜事件：单股候选先做滚动 PIT 匹配，零证据直接返回 `no_matching_evidence` 且不调用 provider；匹配 revision 使成功/无证据结果在事件进入或移出窗口时精确失效。未声明过滤器的自由分析只复用到当前仿真日期结束。
- `response_format={type:"enum",values:[...]}` 限制为 1 次检索轮 + 1 次强制最终回答（128 token、5 条、provider 片段 1,000 字符），返回首个独立允许值，修复 `DOWNGRADE ... REJECT` 子串误判；通用自由文本 3 轮合同不变。
- Backtest `nl_cost` 细分 provider/检索/过滤墙钟、token、证据、缓存和零证据跳过；Probe 在继续 withholding 内容的同时给出完整窗口逻辑调用投影与 provider 结构上界，不把退化墙钟伪装成真实 NL 延迟。
- Q4 `valid_006` 离线只读复算：基线 1693.590s（NL 763.858s，105 provider/112 retrieval）；五类 30 日事件过滤命中 4/30、跳过 26/30，冷过滤 51.523s，结构上限 8 provider/4 retrieval。结合策略 exact-tail 基准 7.119→1.608s/日（4.43x，因子最大误差 2.3e-16），预计完整 Valid 约 682s；不做付费 provider 重跑。
- 不增加服务、数据库或跨回测缓存；正文 sidecar/持久缓存仅在正式复跑未达 `NL<=350s、总墙钟<=900s` 时再评估。最终 `tests/unit` 848 passed + 41 subtests，定向 158 passed；Prompt 重导出、`git diff --check` clean。全量测试前后可用内存 368/367GiB，GPU 未用于本任务。

2026-07-16 lzp-test18 优化版本前端恢复

- 源码提交 `d2dba11` 后，从前端恢复 `epoch_002/fold_2025Q1`；首个 worker 在建立 session 前发现冻结父策略目录遗留的 Python bytecode 缓存并退出。仅删除 Q3/Q4 父策略下两个生成的 `__pycache__`，未修改策略源码、结果账本或冻结产物。
- 第二次前端恢复成功：PID `2652768`、run `run_6efb54947eb3`、代码版本 `d2dba11`；最终检查为 `running_session`、`worker_alive=true`、心跳正常、`error=null`。优化 directive 已要求五类 30 日事件门控、enum 决策、无证据数值回退及 exact-tail 因子计算。

2026-07-16 控制台：Session 后 Environment 真实进度

- 原提示在 `session_end` 后统一显示“验收、评估或落盘”，无法区分可能持续数分钟的验收冻结、隐藏测试、持久化和 Fold 分析；冻结测试进度按防泄漏合同不会写 Agent Trace，因此不能复用 Agent SSE 猜测阶段。
- worker 现只在既有宿主 `status.json` 写 `environment_stage`、阶段开始时间和节流后的日序号/总日数；阶段覆盖验收冻结、冻结测试、落盘、分析、元学习收口/环境更新及 held-out。隐藏回放投影剔除日期、订单、NL 活动和结果，不进入 Prompt、Trace、Sandbox 或 Agent 预算。
- 前端显示真实阶段、`day_index/total_days`、百分比和阶段墙钟；旧 worker 无新字段时仍显示 Session 后累计墙钟。复用现有 5 秒状态轮询和回放节流回调，不新增服务、线程、数据扫描或逐 tick 写入。
- Validation：相关 Pipeline/WebUI/工具流 251 passed + 13 subtests；全量 `tests/unit` 849 passed + 41 subtests（26 个已知 warning，114.65s）；`py_compile`、`node --check`、`git diff --check` clean。测试前后可用内存 354/353GiB，GPU 未用于本任务。静态前端已同步，console/tunnel/API 端到端健康；当前 lzp-test18 worker 仍运行 `d2dba11`，旧字段兼容，详细宿主阶段在下次 worker 启动后生效。

2026-07-16 控制台策略分析文案去供应商化

- Step/Fold 分析标题统一为“策略分析（可选，仅基于验证期证据）”，移除 DeepSeek/LLM 品牌绑定；模型、推理模式、超时与上下文压缩帮助文案同步改为供应商中立术语。
- 保留当前真实可选模型 ID，不改 API、分析执行、实验参数或运行中的 worker；新增 WebUI 回归检查，禁止标签/帮助文案重新出现供应商品牌或内部 `provider` 术语。
- Validation：WebUI backend 44 passed；`node --check`、`py_compile`、`git diff --check` 通过。静态前端已同步并重启 console，端到端健康；lzp-test18 worker PID/run/session 均未改变。

2026-07-16 控制台待批准 Fold 误计时修复

- `lap-test19` 完成 Q2 后进入 Q3 人工批准门时，status 只切换 session key/state，仍携带 Q2 的 `session_started_at`/run/trace/deadline；前端又把任意存活 worker + 时间戳视为运行中，导致 Q3 从 Q2 起点持续计时。实际 Q3 尚未启动。
- 前端统一复用 active-session/wait-state 集合，只有运行中、Step 等待和 Agent 提问等待才计算会话墙钟；普通待批准/暂停只显示状态。Pipeline gate 统一清空上一会话身份与时钟，恢复 session 边界不变量。静态页面已同步；当前旧 worker 无需重启，浏览器刷新即可停止误计时。
- Validation：interactive pipeline 45 passed + 4 subtests；WebUI backend 44 passed；`node --check`、`git diff --check` 通过。实验 worker 未中断，仓库外实验状态未修改。

2026-07-16 lzp-test18 Q4 真实性能复跑与平仓故障定因

- `lzp-test18`/`lap-test19` 的策略无法自主平仓不是 Broker、T+1 或结果随机性：公开持仓行字段为 `quantity`、`sellable_quantity`、`entry_price` 等。lzp 冻结策略读取不存在的 `volume`（Q4 还读取不存在的 `cost_basis`/`avg_price`），lap Q1–Q3 读取不存在的 `qty`；`.get(..., 0)` 将接口误用静默变成空持仓。lzp Q4 `valid_006` 因而 73 笔买入、0 笔卖出、44 个宿主期末退出；lap Q2 修正为 `quantity` 的 `valid_003` 随即产生 237 笔已成交卖单，证明执行链正常。冻结策略、账本和实验状态均未改写。
- 扩展独立 `replay_benchmark.py`，可在原始 decision/replay snapshot、冻结镜像、真实竞价/分钟/盘后/Timeview/公司行动和生产超时下接入正式 NL RPC；记录策略/快照/镜像身份、逐阶段耗时、NL 成本与订单摘要。Q4 性能派生物仅位于 `.runtime/bench/`，不参与模型选择。
- `event_filter + enum` 直接复用门控已得到的最多 5 条候选证据，取消模型再次检索；每个逻辑调用最多 1 次 provider。该直答快路径的隐藏推理按调用关闭、预算调为 512 token；单独 enum 检索和通用自由分析继续沿用原推理配置。真实困难样本从“隐藏推理耗尽预算、无标签”恢复为一次 5.809s 的 `PASS`。
- exact-tail 因子在 2025-07-01/08-01/09-30 三个真实 PIT 状态与旧实现的最大误差为 `2.47e-16`、候选顺序完全一致，单次 6.42–7.45s 降至 1.55–2.03s（3.66–4.14x）。完整 Q4 重放：1693.590→826.108s（2.05x，-51.2%）；NL 763.858→58.695s（-92.3%）；策略计算 538.606→248.938s（-53.8%）；provider 105→3、检索 112→0。28 次逻辑调用中 25 次无证据直接跳过，3 次均完成。
- 达成 `NL<=350s`、完整重放 `<=900s` 等门槛。剩余主要成本为策略计算 248.9s、Timeview roll 165.6s、宿主开销 136.1s、IPC 120.0s；事件过滤仅 48.8s，再引入正文 sidecar/跨回测持久缓存最多节约约 6% 总墙钟且增加失效与复现复杂度，当前不值得。此复跑用于性能验证，因事件门控改变 NL 执行路径，不将其 5.19% 收益用于策略质量结论。
- Validation：NL + DeepSeek 定向 69 passed + 10 subtests；tools-flow 109 passed + 9 subtests；全量 `tests/unit` 850 passed + 41 subtests（26 个既有 warning，112.07s）；`git diff --check` clean。基准显式 CPU-only；共享主机上的正式实验负载保留，未改动其 worker。

2026-07-16 案例研究修复批次：数据 cron 不就绪契约、Agent 环境接口反馈、NL 正确性与控制台收益口径

- 盘前两融空响应不再打脏湖世代：event_flow 新增 `--zero-rows-not-ready`（4 个盘前两融作业启用），零写入时按竞价捕获同款退出码 75（无变更可重试）恢复 committed 世代并记 `not_ready`，部分写入正常提交、空分区留给重试任务；历史回补保持 fail-fast。
- Agent 环境接口（lap-test19/lzp-test18 案例研究定因的修复面）：模板 README 与系统提示词逐键列出 `ctx.positions` 行 schema（含 quantity 与 sellable_quantity 语义区分）；modification_check 新增 `unknown_position_row_key` advisory（对冻结的 lap `qty`、lzp `volume` 产物实测命中，模板与合法用法零误报）；回测 summary 新增 `order_lifecycle` 方向计数与 `strategy_exit_fill_count`（Probe 同样返回），全部平仓来自宿主强平或清算不完整时给 warn-only"退出路径检查"诊断；Probe 失败公开异常类名与策略文件行号（原始消息仍留宿主审计面）；Probe summary `result_path=null`；被拒 close() 保留策略自身 reason（不再被解析动词覆盖）；开发历史增记 trade_count/退出计数/liquidation_complete/benchmark 紧凑块；收尾提示改为先重读 steps/tree.txt，并按"本 run 内已完整验证才免重跑"的真实 finish_fold 契约表述。
- NL 正确性：通用正文检索先按 PIT 可见 text_id 半连接再 LIMIT，未来行不再挤占结果配额（新增回归测试对旧实现失败）；enum 解析对含 CJK 的值要求独立成词，"不减持"不再解析为"减持"（ASCII 值边界维持原样，`结论PASS` 仍可解析）。
- 控制台：/trace* 端点 run_id 单段校验堵住路径穿越（与 style 端点同款守卫 + 回归测试）；累计验证/测试收益只在单 Epoch 内复利（`metrics` 取最新 Epoch 并以 `epoch_id` 标注，`metrics_by_epoch` 逐 Epoch 并列），`/equity` 支持按 Epoch 切换并附服务端全周期统计（累计/年化收益、年化波动、Sharpe、最大回撤、日胜率、β、超额、跟踪误差、信息比率）；SIGCHLD 忽略导致 subprocess returncode 恒 0 已加显式注释；QMT README 三闸门失败语义（confirm 不匹配/时段外为 error 而非 dry_run）与幂等日界口径对齐代码；冻结评估进度负载只含运行时键的回归断言加固。
- Validation：全量 tests/unit 860 passed + 41 subtests（114.63s）；`node --check`、`git diff --check` clean。运行中的实验 worker 未受影响（提示词/环境变更在其下次会话启动后生效）。

2026-07-16 案例研究修复批次·对抗复审收口

- Fable 对抗复审在自批次中定位 3 个实质缺陷并已修复：(F1) `ensure_trade_cal_coverage` 在数据集循环前可能写湖，空响应下仍退出 75 恢复世代——现返回是否写入并计入无变更判定；(F2) 收缩覆盖被拒（`blocked_shrink_overwrite`，数据完整性告警）曾并入"零行未就绪"分类——现单独计数并无条件 fail-fast，不受 `--zero-rows-not-ready` 影响；(F3) `trade_count`/`benchmark` 因公开 manifest 白名单缺项从未真正落入开发历史——已补白名单。
- 低危加固：`order_lifecycle` 计数键 `submitted`→`total`（消除与订单状态同名的潜在重复计数）；退出路径诊断措辞不再断言"全部来自区间末强平"（维保强平另计）；`unknown_position_row_key` advisory 提示自建行可忽略；收尾提示注明 Step 树"若启用"。已确认收口项：run 内恢复免重跑的表述经 `covering_complete_validation` 双 hash + `step_rollback` 默认还原 models/ 验证成立；测试揭示封存、PIT 带宽预算均未回归。
- Validation：全量 tests/unit 862 passed + 41 subtests（112.57s）；`git diff --check` clean。

2026-07-16 全仓库设计原则合规扫描与修复（4 个 Sub Agent 并行扫描：环境核心/Agent+Pipeline/数据层/控制台运维）

- 前端更新：静态 SPA 已同步至前端服务器，console 重启加载新 API；实测按 Epoch 指标与全周期统计已在线（lzp-test18 E1 +14.87%/E2 +11.51%/E3 +0.08% 分列，E1 全年 Sharpe 0.60、β 0.88、超额 −0.64%）。
- Agent 会话稳态（扫描确认后修复）：主对话 provider 调用按剩余截止时间钳制内部重试（"最坏超时为一次有界调用"不再被 3 倍重试破坏）；连续 3 次失败熔断为 `llm_unavailable`（故障提供方不再烧光 200 次调用预算）；上下文超长错误强制一次压缩（绕过 CJK 低估 ~2x 的 token 估计阈值，保留熔断）；backtest 观察回显 `steps_used/limit/remaining`；`step_rollback` 仅在启用 Step 树时注册；meta 工具表补 `ask_user` 行。
- 环境核心：滑点成交价钳制进当日涨跌停区间（滑点是流动性假设，不打印交易所不可能的价格；含区间末强平路径）；删除无生产者的 `_ACTION_ALIASES` 兼容映射与死字段 `Order.source_artifacts`（orders.parquet 少一恒空列）；`_rank_cross_section` 向量化（每次回放每日 ~5k 行的纯 Python 循环消除）；NL 可见索引缓存改按数据集 cutoff 签名（节点间的每个 tick 复用同一物化切片，不再按 tick 重建百万行帧）。
- 数据层：`forecast_vip/express_vip` ann_month 整月重拉补 `allow_key_removal_overwrite=True`（与全部整分区兄弟路径一致；update_flag 原地替代/撤回不再永久卡死当月刷新，防缩水护栏保留）。经证据核查后跳过：09:20 事件/资金审计实测 2m40s 完成于 09:25 闸门前，且缩小数据集范围会破坏研究发布消费的全域状态文件契约——不改。
- 控制台：trace 分页对超过页预算的单行事件跳行前进（此前 SSE 与回放加载器会在同一 offset 永久空转）；创建实验的并发上限检查提前到建目录之前（拒绝不再留下半创建目录导致重试撞 "already exists"）。
- 复审回退：backtest 失败路径的 `host_peak_rss_bytes` 记录实为错误路径必需（非重复赋值），已恢复并加注释。
- 其余扫描结论：4 份报告均判定总体合规；明确不修（成本>收益）：快照分钟构建整窗内存（500G 主机非瓶颈）、竞价缺 Bar 市价单不对称（罕见数据缺口、方向保守）、NL legacy keywords 映射（有测试的受支持行为）、盲竞价 tick 市价单不冻结资金（提交-成交窗口小、方向保守，已知事项待后续评估按涨停价冻结）。
- Validation：全量 tests/unit 868 passed + 41 subtests（113s）；`git diff --check` clean；PROMPTS.md 重导出。

2026-07-16 控制台全周期统计布局压缩

- 收益图下方的"全周期统计"由 13 行竖表转置为每序列一行的紧凑条（验证/测试/Held-out × 短列头，悬停显示全名；累计收益列删除——图例已含各序列终值）；字号与内边距收紧，高度约 430px→90px。`node --check` 与 WebUI backend 48 passed；静态资产已同步前端。

2026-07-16 观察负载权衡与图表右缘刻度修复

- 权衡分析后移除成功回测观察中的 tool_spec 回显（~2KB/次、每 Fold 最多 30 次的纯上下文占用；schema 已随每轮 function-calling 原生下发；错误路径与宿主 manifest 审计保留不变）。收益曲线最右侧日期刻度改为 end 锚点并跳过会与其拥挤的模除刻度，首页与详情页不再被右边框裁切。tools-flow/pipeline-e2e/webui 216 passed；静态已同步。

2026-07-16 Step 树谱系轨道重绘

- Step 树改为显式谱系轨道（git-graph 风格）：分叉子节点用 ├/└ 肘线挂接父节点、链式子节点用竖轨连接上一行、兄弟分支轨道贯穿嵌套子树（延续状态沿链继承，已用 lzp-test18 真实 45 节点树的 ASCII 模拟逐行验证）、半高终止符收束分支；移除深度上限（列数只随分叉推进，现有最深树 4 列），超宽横向滚动而非裁切。node --check 通过，静态已同步。

2026-07-16 收益曲线下新增联动仓位子图

- /equity 与 Fold equity 负载新增逐日仓位序列（EOD 持仓总市值/权益、多空分列，取自 positions_eod.parquet，与收益同口径按 Epoch 成链、受揭示封存门控）；前端在回撤子图下方渲染联动仓位子图（同色系、共享 x 轴与悬浮提示，100% 参考线，做空虚线）。WebUI 49 passed；实测 lzp-test18 E1 244 日、多头仓位中位 99.1%；console 已重启、静态已同步。

2026-07-17 衍生品市场上下文数据接入（期货/期权/可转债/中债曲线）

- 新增 9 个不可交易上下文数据集（fut_basic/fut_mapping/fut_daily、opt_basic/opt_daily 限 SSE/SZSE/CFFEX、cb_basic/cb_daily/cb_call、yc_cb），全部复用宏观域机制：日频表 trade_date 策略 + 晚间节点可见（EOD 盖章、次晨可用），注册表新增 ~30 行 static_full 整表刷新策略；快照侧注册表豁免宏观窗口下限（老合约条款可见，逐行 available_at 照常执行 PIT）；审计/夜间更新/研究发布自动覆盖。零 Broker/工具/提示协议改动——信号（基差、PCR/IV、转股溢价、强赎事件）由 Agent 自行用 DuckDB 计算。
- 回填踩坑与修复：opt_daily 全市场 2.7 万行/日（多为商品期权）会使快照宏观帧膨胀 →限定金融交易所（~1.5-2k 行/日，15×缩减），并按场所上市日（SZSE/CFFEX 2019-12-23）生成审计预期；yc_cb 为 ~500 期限点/日的密集曲线且 API 拒绝 offset 分页（50101）→ 改为按交易日单次拉取（1,010 行/日单页），覆盖起点 2016-06-20 实测探明。
- 全量回填完成：fut_mapping/fut_daily 各 4,014 分区、opt_daily 5,957 文件 217 万行、cb_daily 74 万行、yc_cb 2,447 日，共 ~615MB；宏观审计（20260601-20260716）status=ok 0 错误 0 警告。
- 专项案例研究（.runtime/bench/derivatives_case_study*.py + summary json）：真实决策快照双构建对比 —— 构建 824.7s→886.0s（+7.4%），macro 帧 1.06M→3.04M 行 / 64.8→114MB；热路径确认无回归（Timeview 以 footer 硬链接冻结宏观文件、逐节点只写新可见回放行、Agent 侧 DuckDB 投影查询）；回放槽 8 日窗构建 40s；PIT 逐项断言通过（决策日只见 T-1 结算、区间内 06-29 结算当日全天不可见、06-30 盘前可见；注册表老合约可见/未来合约隐藏）；Agent 视角基差信号端到端验证（IF 主力 2026-06 末贴水 -2.9%，主力换月 06-18→06-22 可见）。
- Validation：全量 tests/unit 872+ 通过（新增 static_full/审计预期/窗口豁免/场所起始日回归测试）；git diff --check clean。

2026-07-17 收尾合规扫描（2 个子代理端到端复查）与修复

- 广域扫描（agent/pipeline/webui/ops）：三原则全部 PASS、零发现；PROMPTS.md 再生成逐字节一致；11 条文档关键断言逐一对码通过；872 tests 复跑通过。
- 衍生品深扫（Fable）确认两处 HIGH 并已修复：(1) 注册表窗口豁免泄漏到回放槽 → 与冻结快照在 Timeview 合并后注册表整表重复（Agent 侧 join 双倍行）——豁免改为仅决策快照路径，回放槽实测 82,719→41,535 行、窗口前注册表行=0（新增回归测试）；(2) cb_basic 为当前状态表，conv_price/delist_date/remain_size/newest_rating 按 list_date 盖章即历史前视——cb_price_chg 需逐券 ts_code 无法整表拉取，按最简方案改为文档硬禁令 + PIT 推导口径（转股价 = 100×正股close/cb_daily.cb_value，溢价用 cb_over_rate，赎回结局用 cb_call），同步写入数据文档与审计 PIT 规则。
- 文档订正：yc_cb 拉取方式（按交易日）、期权 SSE/SZSE/CFFEX 限定与场所起始日、fut_daily 全市场保留理由（商品期货作宏观背景）、连续合约行不可见提示；loop_start_dates 与 loop_values 长度不匹配现报明确错误。
- 其余低危发现按成本效益处置：yc_cb 页满崩溃保持 fail-fast（当前 ~1,010 行/日对 2000 上限有 2 倍余量）；30 天窗外空洞人工回补语义保留（审计红灯即人工介入信号，实测覆盖完整）。
- Validation：全量 tests/unit 复跑通过；回放槽重建 41.3s 验证修复；git diff --check clean。

2026-07-18 前端下拉/勾选样式修复 + E2E 工程性能优化批次

- 前端两修复：创建表单数据集下拉缺新数据集为「控制台旧进程 + interfaces 注册表缺标签」双因（已补 9 条标签并重启，34 项含中文标签实测在线）；勾选 chip 文本粗大且不居中的根因是 `.field label`（0,1,1）压过 `.check-item`（0,1,0）——选择器升为 `label.check-item` 后 flex 居中/字重 500/0.875rem 真正生效，静态已同步前端。
- E2E 性能评审（Fable 子代理、逐项证据核查）7 项发现，落地 5 项：(1) NL 事件门控逐行正则判定记忆化（此前每次 ctx.nl() 全量重扫语料，占全部回放墙钟 29.5%，预计单次完整回放省 ~60-95s；revision 不变，回归测试守护）；(2) 夜间任务调用量重构：基本面逐码窗口 90→10 天、namechange/ths_index 移出夜间，新增周六深扫双任务（预计夜间 2h35m→~1h10m，恢复 02:30 审计余量）；(3) 快照缓存移至 `<experiments_root>/.snapshot_cache` 跨实验共享（实测并行实验重复 8.1GB、每共享 Fold ~6.5min）；(4) 收盘回退键集只取 15:00 行（0.21s/日→0.001s/日）；(5) check.md 勘误「2 路并行」不实记录。缓存预归一化分钟帧一项经评估延后（格式版本升级成本 > 中置信收益）。
- Validation：全量 tests/unit 873 passed（新增门控记忆化回归测试；周六深扫在 cron 漂移守卫中显式豁免并注明可见性仍由既有节点治理）；git diff --check clean；crontab 已重装（cn_weekly_* ×2）。

2026-07-18 lzp-test21 根因复盘（元学习/Fold Agent/held-out 三路审计）

- 结论：held-out 崩盘（3 个月超额 -19.3pp）主因是评估设计而非信号衰减——单月验证爬山 + 跨 Epoch 复用同一 13 个月开发窗（dev "test" 仅 Epoch 内样本外）+ 元学习把窗口内结论固化为教义（资金流单因子垄断、"收敛期"指令致 epoch_003 11/13 个 Fold 零改动冻结、整个 Epoch 仅 2 个策略哈希）。资金流信号本身在 held-out 内部并未失效（对全域中位数超额 +0.13~+0.53pp/5d，优于开发期）；亏损来自结构性低仓位（现金先卖后买时序缺陷 → 平均仓位 ~40% 上限）、选股池与基准背离（中盘池中位数落后 HS300 -5.4/-8.5/-12.0pp）、~10x 换手成本 -2.3pp、陈旧参考价止损缺陷（78 次晨间止损中 ~74 次为假触发，4 月反噬 -2.2pp）。数据质量为次因：margin 交易所级行 5-6 月 3 天缺 2/3（12 个月首见）致 6 月市场状态抖动；NL 否决反而剔除赢家。开发期 +23.6% 属选择性过拟合：12 月冠军 Fold 的 β 捕获 -0.16%、逐笔无边际，收益靠 2 笔离群交易 +5.1pp。

2026-07-18 优化落地批次 + margin 修复 + lzp-test22 释放固定缺陷修复

- 按批准落地四项：(1) margin 交易所完整性守卫（缺所需交易所行的日期走 not-ready/重试契约、审计报错；已修复 20260529/0605/0626/0703 四天 1→3 行，全历史零残留）；(2) 回测 summary 新增 exposure（avg/max gross、零仓位天数）与周度收益分解（Probe 不返回）；(3) 元学习 Taste 合同加固（禁止"真实 alpha"级泛化断言、禁止整族因子禁令、收敛期须保留每 Fold 至少一次可检验验证）+ 双 Prompt 策略类别中立化（多因子/事件驱动/趋势择时/均值回归/股票内统计套利，交易范围限 A 股现货+两融）；(4) 新实验默认 epochs=3（ExperimentConfig 单一事实源）、季度建议开发窗 8 季（两年）。验证窗口问题采用季度 WF（零代码）。
- lzp-test22 故障根因：实验创建于夜间更新持锁期间，pin 回退到最新完整 release（2026-07-16 物化、早于衍生品回填），缺 fut_basic 等 9 数据集 → 首次快照构建深处 FileNotFoundError。修复：pin_research_release 增加 required_raw_datasets 合同（选择期跳过不完整 release、复用期给出可操作报错）；实验经控制台 API 删除重建（仅一条 attempt_failed 元学习记录，归档 .runtime/bench/），新 pin 世代 4175cdd8 含全部 121 数据集，实测通过。
- Validation：全量 tests/unit 879 passed；margin 修复经无 --force 自愈路径实测（21 天跳过、恰好 4 天重拉）。

2026-07-18 三子代理（Opus 4.8）三原则合规审计与择优修复

- 数据层/环境层/管线控制台三路审计共 10 发现（0 高危）。按"收益/开销"择优落地 6 项：pipeline 设计文档"同一实验内"陈述订正为跨实验；running_experiments 跳过点目录（不再 stat 遍历共享快照缓存）；release 数据集合同拒绝"存在但为空"目录（rglob 短路，兼容嵌套布局）；cron 漂移守卫接受 DOW 区间/列表/步进表达式；NL match_cache 模式键 FIFO 上限 32（防逐 tick 变模式策略无界增长）；Taste 备选方向条款注明服务同一机制假设。4 项经客观评估不改：共享缓存 GC（并发安全机制开销 > 磁盘压力）、周扫新鲜度（既定权衡）、margin 守卫范围（有意限定）、equity≤0 曝光标记（Broker 强平使状态不可达）。全量 tests/unit 880 passed。

2026-07-18 全库通读后的性价比择优修复（存储卫生 + 僵死进程）

- 全库通读（6 份活文档亲读 + 7 路子代理源码/测试/实验全量阅读）后按"收益/引入冗余"评估全部已知问题，仅落地零代码冗余的两项：
- 退役死数据：`data/raw/{slb_sec,slb_sec_detail,stk_auction_c,stk_auction_o}`（合计约 1.29G，6 月已从活动合同移除、当前代码/配置/审计零引用，用户确认永不再用）在 updater 排它锁内整目录迁至 `archive/data_raw/20260718_retired_datasets/`；已有 7 个 research release 的硬链接不受影响，未来 release 不再包含。
- 清理僵死进程：两个 7-14 遗留的 `tests.unit.test_interactive_pipeline` unittest 进程（挂在 control.json 轮询等待近 4 天）SIGTERM 正常退出。
- 复核后明确不修（低性价比）：snapshot 全量 hash（实测仅构建期一次，回测逐次只比对字符串）、`_read_dataset_window` 谓词下推（PIT 正确性敏感、构建已被跨实验缓存摊薄）、revision 账本轮转、share_float 全量重建、detailed_return 事件内嵌、reveal 门控中间件化、SIGCHLD 语义、NL 候选摘要——均为"引入代码冗余 > 现实收益"。

2026-07-18 lzp-test23 双修复：fold_2025Q3 快照构建卡死与 valid 风格汇总口径

- fold_2025Q3 备料卡死 4h16m 根因：b652403 引入的板块谱系数据（dc_member 等）7-18 00:39 回填落地、当晨 pin 首次含其全量，12 个月事件域窗口首次覆盖两个整季 2025 数据（~19M 行、dc_member 占 6.5M）；联合后再筛选使 rows×联合宽列的单次 frame[mask] take 进入小时级（py-spy 证实 4h+ 停在 _apply_screen 的 take_nd，RSS 65G）。修复：_build_available_at_domain 逐数据集先筛后并（输出行完全一致，决策快照与回放槽同路径生效）。杀停旧 worker、清 2.0G 残留 staging 后重启，重建峰值 RSS 12G，各域分钟级落盘。
- fold_2024Q2 "收益 +36.1% 而年化 α 低"结论：非计算错误。逐窗口回归正确（valid_014 α_ann=+1.33、β=2.345、超额 +33.0pp 与账本一致）；低 α 来自 style_valid.json 汇总把同 Fold 15 个验证尝试窗口按首见日期拼接（早期亏损版本主导 → α=-1.29、β=0.535）。现 valid 汇总只取账本选定验证窗口（write_style_rollup 新增 windows 限定），并已重生成 lzp-test23 全部 6 个已记录 Fold 的 style_valid.json，控制台风格卡与 Fold 头条结果一致。
- 验证：test_style_analysis 6 通过（含新增 windows 限定单测）、test_snapshot_builder 42 通过、test_pipeline_e2e 53 通过；环境/参数文档同步更新。

2026-07-18 四路 Opus 评审后的策略生成流程与回放性能优化批次

- 证据（lzp-test22/23 全部 78 次回测 + 6 个 Fold trace + 快照缓存 26 份 manifest）：完整验证 ≈5.85s/交易日，环境开销占 87%（timeview_roll 34%、host 开销 26%、IPC 19%）；回测墙钟占 Fold 总时长 ~75%，其中 ~13% 花在"行为等价"的重复验证；2/6 Fold 因 Step 上限硬终止且未收尾；元学习 Taste 确实主导了 Fold 实现，但看不到 test21 复盘点名的过拟合信号。
- 落地 7 项：①Timeview 同 run 分片暂存（硬链接复用，免重复切片编码，行数校验 fail-fast，杀掉 ~34% 回放墙钟的 15/16）；②timeview init 的 RangeIndex 守卫（reset_index 整帧拷贝致 fold_2025Q3 首次回测卡"首日处理"30+ 分钟——现场 py-spy 定位）；③`_read_dataset_window` 分区读取 8 线程化（events 域 47% 构建成本，保持串行字节序）；④Step 预算耗尽收尾提示（STEP_WRAP_UP_PROMPT，镜像 deadline 收尾）；⑤订单流签名 + `behaviorally_identical_validation` advisory；⑥`_compact_fold_history` 增投 exposure/turnover；⑦development_history 增逐 Epoch 冻结 hash 多样性。
- 决定不做：盘中决策间距放宽（研究者旋钮，改变行为）、跨 run 分片缓存/预归一分钟缓存（新增缓存层）、prefetch 与会话重叠（文档化的刻意串行，substep 墙钟合同有争用风险）、meta 记忆摘要化（dev history 已含 Taste 链）、无改进劝退 nudge（与收尾提示重叠）。
- 验证：全量 tests/unit 881 passed + 41 subtests（116s）。待补：stash 专项单测（现由行数校验 + 现场端到端复核兜底）。lzp-test23 worker 重启以载入修复，fold_2025Q3 重跑。

2026-07-19 lzp-test23 Q3/Q4 快照构建诊断补强

- 既有缓存证据确认增长集中于决策快照 events：2025-03-31/06-30/09-30 分别为 9.75M/14.03M/19.47M 行，events 构建 323.0/402.2/1104.3s；同期季度 replay 总构建稳定在 249.3/257.8/258.3s。按收益/复杂度权衡，仅给 events/macro 每数据集增加分区、行数与 discover/read_filter/concat/deduplicate/screen 墙钟，复用构建中已有计数，不做逐文件日志、额外扫描、新缓存层或 cache format bump。
- 诊断只写新构建 snapshot/replay manifest 的可选 `dataset_build_profile`，不进入轻量 data_summary，不改变 Parquet、PIT、snapshot hash/缓存键；旧缓存无需重建。lzp-test23 已推进至 fold_2025Q4 frozen_test，运行 worker 未受改动。验证：snapshot 37 tests、全量 tests/unit 880 tests 通过（111.8s），`git diff --check` clean。

2026-07-19 lzp-test23 epoch_001 fold_2025Q3 LLM 前缀缓存失效定因与修复

- Q3 并非 API 缓存字面归零，但在第 68/145 次主对话命中 150 条消息上限后实质崩溃：命中前 hit ratio 93.78%（miss 241,480），命中后 34.77%（miss 4,349,230），整体 56.47%；只剩约 27.5K 静态系统前缀持续命中。同期 Q2/Q4 整体为 92.76%/81.64%。145 次请求皆为同一 deepseek-v4-pro、status=ok，无重试故障与语义压缩，排除 provider 路由/过期与异常循环。
- 根因是确定性 `_trim` 紧贴 150 条上限：首次 `context_summary` 保留 150/显示丢弃 0，之后几乎逐轮重生位于前部的动态摘要，共 76 次，每次都使 provider 无法复用摘要之后的长前缀。Q3 的长自主探索是合法工作负载，只是首个长到持续暴露该缺陷的 Fold。
- 性价比判定为需修：会话允许 200 次主调用，该问题可重复且 Q3 一次即产生 4.35M 受影响 miss token。最小修复在既有确定性裁剪中批量留出 30 条 headroom（默认保留 system + summary + 118 条最近原始消息），将一次不可避免的前缀重写摊销到多轮；上限、语义压缩、真实 provider 和 Agent 工具协议均不变，不新增缓存层或 API 调用。调高上限、强制语义压缩和应用层缓存的风险/复杂度均高于收益，不采用。
- 验证：新增回归测试证明首次批量裁剪后的普通后续轮不再重生摘要；专项 3 tests 与全量 tests/unit 881 tests 通过（115.8s）。活跃 lzp-test23 worker 保持在 epoch_002/fold_2024Q1 且未重启，新行为仅在后续新 worker/会话生效。

2026-07-19 lzp-test23 epoch_002 fold_2024Q2 异常耗时定因与重跑恢复

- 原 run `run_3e7be249fdbc` 墙钟 25,600.0s，其中 17 次回测预算排除合计 23,959.844s（93.6%）；前一 Fold Q1 仅 7 次回测，6 次完整回放为 469.1–516.7s。Q2 分钟数据反而更少（43.02M vs 44.25M 行），排除数据量增长。
- 异常由三项叠加：3 次失败回放（1543.8/837.6/539.2s）和多轮 Probe/完整验证放大总耗时；前三次完整验证的 Timeview/分钟归一化/宿主阶段出现逐轮自愈的暂态慢化；Agent 又把每次 tick 调用的 `market_crash_guard` 实现为新建 DuckDB 连接并扫描 macro Parquet，使相同行为签名的策略计算从约 155s 升至 1535/1439s。Agent 后续改为稀疏调用并回滚，末次完整回放已恢复 468.5s，证明不是持久的数据或回放引擎故障。
- 性价比结论：不新增全局静态调用图、调度器或跨 run 缓存。坏热路径只存在于实验沙箱且已回滚，现有 `phase_seconds`、data-load timing 与 `orders_signature` 已足够定位；新增系统代码会增加噪声、失效面和与真实环境的偏差。最小处置是在当前 `d813958` worker 上重跑，并通过 directive 要求 Probe 后看阶段耗时、完整回放 >900s 视为异常、重 I/O 只在稀疏 tick 执行、订单签名相同即回滚或换假设。
- 原 Q2 落账后 worker 已自动进入 Q3；为避免链式推进，先发 stop、未在备料阶段及时生效后用受支持 terminate 优雅 SIGTERM（无 SIGKILL），Q3 未产生账本或冻结产物。随后重跑 Q2 为 `run_99a60a001cd2`（PID 1264264）。Probe 为 24.9s/3 日与 41.7s/5 日；首个真实完整验证 `valid_002` 回放 523.981s、发布全程 550.070s，覆盖 57 个分钟分区/43.02M 行，宿主 99.3s、分钟归一化 53.3s、策略 151.4s、Timeview 95.8s，全部回到 Q1 尺度且远低于 900s 门槛。验证后 worker 保持运行，由 Agent 自主继续 Fold。
- 仓库只补本次可审计记录，不改执行代码；文档批次以 `git diff --check` 验证，未为纯文档变更运行单元测试。`origin` 仅指向新仓库 `git@github.com:LeoZippon/CornerHead.git`。

2026-07-19 显式单位合同 + Meta Test 反馈 + Fold 间隔元学习

- Agent 事实与 `data_summary` 新增按“文件 + dataset + 字段”识别的紧凑单位合同，Prompt/模板/活文档同步给出可执行例子：daily 比例为小数、moneyflow 金额为万元、index_daily 涨跌幅为百分数值。审计发现 `daily_basic.dv_ttm` 漏归一，现与 `dv_ratio` 一致按 0.01 转为小数；snapshot cache format 升至 v4，防止旧缓存按 100 倍不同口径复用。
- 仅 Meta Agent 的显式 workspace 投影新增已完成 Fold 的 compact frozen Test 白名单指标，用于 Validation/Test 差距和跨 Fold 稳定性诊断；Fold Agent 不可见，Test 原始数据/排程/snapshot/路径/订单/Trace/Broker/NL 明细仍隔离，Held-out 仍是唯一最终未触碰评估。Prompt 禁止单 Fold Test 调参及把具体 Test 数值/身份/区间写入 Taste。
- 新增 `meta_learning_fold_interval`（默认 0）：保留每 Epoch 起始 Meta；N>0 时每完成 N 个 Fold 且仍有下一 Fold再触发，Taste 只影响后续 Fold。批量/HITL 排程、唯一会话身份、恢复、记忆、前端详情、Prompt 预览、重跑和回滚均按同一因果顺序更新；末 Fold 后不空跑，默认行为不变。
- 三路只读审计复核 Test 隔离、单位和周期排程，并修复回滚误归档共享父产物与 Prompt 预览越过未完成 Meta 复用旧 Taste 两个边界。验证：223 项扩展专项与全量 `tests/unit` 889 项通过（115.057s，Docker 条件满足并真实运行容器 Fold/沙箱生命周期测试）；`py_compile`、`node --check`、Prompt 双次导出哈希一致和 `git diff --check` 通过。未重启或修改任何运行中实验。

2026-07-20 单位文档边界评估与控制台部署修复

- 结论：不建立全 raw 表逐列单位百科；标识/文本/类别/无量纲字段无需重复供应商 schema，且人工全量表会漂移。保持风险分层：归一化 Agent 文件完整声明，异构 union 维护已用于交易或易跨表误用的高风险字段族，未映射 source 字段在核实并换算前禁止进入绝对阈值/跨表算术；新字段进入正式研究时必须同批补合同、数据文档和测试。
- 间隔选项不可见由双重部署陈旧导致：远端 `app.js` 未同步，且 7-18 启动的 uvicorn 仍持有旧 `params_schema`。已同步 SPA、只重启 console API（实验列表为空，tunnel PID 保持 828388），远端 API 现返回“元学习 Fold 间隔”默认 0，远端/本地 app.js SHA-256 一致。新增 `webui_stack.sh deploy` 一步完成静态同步+API 优雅回收，保持 tunnel/独立 worker 不动；部署文档补齐静态与 Python/schema 更新边界。
- 验证：WebUI+Pipeline 107 tests passed（52.272s）；`bash -n`、`node --check`、Prompt 双次导出哈希 `b0db0910...87d3` 一致、`git diff --check` 和真实 `deploy` 端到端健康检查通过。

2026-07-20 auction 完整单位合同与 Test 聚合诊断补齐

- `stk_auction` 实表核对确认 `turnover_rate` 是百分数值、`float_share` 与 raw `daily_basic.float_share` 同为万股、`volume_ratio` 已是无量纲倍数。为保持“归一化文件完整声明”真实有效，snapshot 现把前两者分别乘 `0.01`/`10000`，量比不变；Agent unit contract、manifest `unit_conversions`、Prompt 与活文档同步声明全部字段。snapshot cache format v4→v5，禁止新旧 auction 字节口径混用。
- Fold ledger 的紧凑指标 allowlist 新增 `trade_count`、`turnover` 与四项聚合 `exposure`（avg/max gross、空仓日、回放日）。后续 Meta 可据此解释低仓位与交易强度；订单、逐日 exposure/收益、结果路径和其他 Test 明细仍被投影层剔除，Fold Agent 与 Held-out 边界不变。
- 验证：真实 20260717 auction 单日 smoke check 与 manifest 倍率一致；专项 93 tests、全量 `tests/unit` 889 tests（114.303s）通过；`py_compile`、Prompt 双次导出哈希 `fcd5faf5...c53`、`git diff --check` 通过。前后可用 RAM 均约 427GiB，GPU 未参与；未启动或重启实验。

2026-07-20 强制终止未落账会话的批准恢复修复

- 强制终止现以确定性 schedule + 业务 ledger 判定当前会话是否已落账；只有未落账且已批准的当前会话会从 `approved_sessions` 移除，指令/完整 Prompt 草稿保留。原为 `auto` 时同时转为 `manual`，确保恢复后真正进入“等待批准”；已落账会话保持批准和模式不变，普通暂停/停止/重启语义不变。
- 控制台确认文案与终止结果提示同步更新；回归测试覆盖未完成 periodic Meta 的撤销→预览新指令→重新批准，以及已落账 Fold 不撤销的竞态边界。WebUI 已通过 `deploy` 更新，console PID 4058977、tunnel PID 828388，端到端健康。
- 现场核查时 `lzp-test24` 的首个 Meta 已在部署前完成落账并进入 `epoch_001/fold_2024Q1`，故未篡改历史记录或强杀有效 Fold。实验已切到 `manual`，下一 Meta `epoch_001/meta_learning_after_fold_001` 已通过前端 API 预览并保存英文新方向、保持未批准；当前 Fold PID 3977272 持续运行。
- 验证：相关 98 tests 通过，全量 `tests/unit` 890 tests（116.969s）通过；`py_compile`、`node --check`、`git diff --check` 通过。结束时可用 RAM 407GiB，GPU 占用未由本任务改变。

2026-07-20 修正账本语义去重与过程产物清理

- `revision_events.jsonl` 的 31,300 条均可解析；按既有稳定 `event_id` 只有 15,206 个逻辑事件，16,094 条（51.4%）只是不同 `detected_at` 下的重复观测，另有 71 条 `/tmp` 单测污染。原始账本已压缩备份至 `archive/data_quality/20260720_revision_ledger_dedup/`，正式账本原子压缩为 15,135 条，现 `event_id` 全唯一、无 `/tmp`、无坏行。
- 正式修正写入改为带内核文件锁的 `event_id` 唯一追加；每进程首次扫描当前 40.5MiB 账本实测 0.36s，之后只增量读取尾部，不引入 sidecar 索引、数据库、守护进程或定时清理。revision sentinel 与 revision-aware writer 共用该边界，重复探测仍打印告警但不重复落账。
- 修复 `write_share_float_union()` 被单测直接调用时绕过 ledger resolver 的路径，临时 raw 根现始终落本地账本。6 月 4/19 日的 14 份 `results/data_quality/process/` 一次性排查产物已有日志结论且无消费者，已删除并移除空目录。
- 验证：TuShare 数据层 89 tests 通过；全量 `tests/unit` 906 tests（162.175s）通过。两轮测试前后正式账本 SHA-256 均为 `6e2afa72e7e80001f5c28fb19555a438b473e09c3e188a92d0507e4e94d38be3`，证明测试未再污染正式账本；`py_compile` 与 `git diff --check` 通过。

2026-07-20 数据质量结构统一与运行日志收敛

- `results/data_quality` 的八份 JSON 统一为九字段 schema v1 envelope，finding 与 dataset summary 也固定外层字段；revision ledger 的 15,135 行统一为 35 字段事件记录，`event_id` 全唯一。文本审计拆为真正的 text-only 命令，`text_evidence_status.json` 从 75 条/123KiB 降至 20 条/24KiB，夜间全审计实跑由 21m23s 降至 15m49s（约快 26%），各域质量状态不变。
- TuShare 运行日志归入 `logs/tushare/{cron,dispatch.log,cron_backups}`，详细日志保留 14 天且保护 state 引用；WebUI 关闭逐请求 access log，资源探针改为紧凑 GPU 行。旧日志已可恢复归档后清理，`logs/` 从 74MiB 降至 29MiB；crontab 已重装到新路径。
- 恢复包：data-quality 迁移前归档 SHA-256 `54a940e7...e7a802`，日志归档 SHA-256 `611f1d88...66f45`。验证：canonical `tests/` 911 passed + 45 subtests（156.42s），focused 197 passed，`git diff --check` clean；正式报告/账本逐记录 schema 校验全部通过。

2026-07-20 数据质量 v2 严格门禁与日志保留闭环

- 统一质量报告升级为仅接受 schema v2；`datasets` 改由 builder 从 `scope.datasets` 和 findings 唯一派生，干净数据集也保留全零摘要。producer 写前、文件 reader、research-release pin/hash 校验和 snapshot gate 共用完整结构/计数/状态/报告类型验证；v1、无版本及未知版本均明确拒绝。
- 报告发布改为同目录 UUID 临时文件 + `os.replace()`，并发发布不再共享 `<output>.tmp`。八份 live JSON 已结构迁移并验证为 v2，状态未变，迁移前归档 SHA-256 `9882376b...714ef5`。
- cron dispatch 由 runner 加锁追加，按 5 MiB 保留两个副本；16 个托管任务已重装为 `--dispatch-log`。休市、已完成跳过和锁失败只沿用存在的真实日志，否则不输出/写入 `log_path`；当前 14 条 state 引用全部存在。
- 经用户确认无生产可用策略后，删除 9 个历史实验、74 GiB snapshot cache 和 9 个共享 research release（约 250 GiB 逻辑占用；未保留恢复副本），保留空根目录和 registry lock。验证：focused 176 tests + 7 subtests；canonical 919 tests + 45 subtests（154.65s），仅既有 warning。
