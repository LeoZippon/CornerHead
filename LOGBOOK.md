2026-06-12 当前状态摘要

- 根目录 `LOGBOOK.md` 只保留近期可操作结论；完整旧版已归档到 `/Data/lzp/MacroQuant_archive/logbook/LOGBOOK_before_20260612_simplification.md`。详细历史仍在 `docs/logbook/DETAILED_LOGBOOK.md`。
- 本地开发环境切换为 `~/miniconda3/envs/quant`，Python 3.11。历史 `stock` 环境只作为旧记录存在；新脚本、测试、cron 和非 Docker 工具默认使用 `quant`。
- Docker Sandbox Python 与本机 conda 独立，镜像由 `ops/docker/sandbox.Dockerfile` 构建；变更 Sandbox 依赖后需要重建镜像。
- TuShare 当前命令入口为 `scripts/data/tushare_download.py`、`scripts/data/tushare_audit.py`、`scripts/data/tushare_cron_update.py`；核心实现在 `src/hl_trader/data_sources/tushare/`。
- Agent/Environment/Pipeline 已按单 Agent + Docker Sandbox + PIT snapshot + backtest tool + NL scoring + Step tree 的设计实现。正式实验默认 Docker；`--local-dev` 仅用于开发和单元测试。
- 策略产物只包含 `factor/` 和 `nl_prior/`。Agent 可写目录为 `/mnt/agent/workspace` 与 `/mnt/agent/agent_output`；`/mnt/artifacts` 对 Agent 只读。
- 正式 `generate_candidates()` 运行时只应依赖 `/mnt/snapshot`。Environment 会在运行期间隐藏 `/mnt/snapshots/train|valid|test`，并拒绝正式策略代码中的阶段目录常量。
- Step artifact tree 可通过 `step_tree_enabled` 开关控制；成功完整验证回测会保存 Step 产物节点，供后续 Fold 读取谱系。
- 自然语言评分使用 grep/regex 式本地文本检索，LLM 输出必须解析为严格 JSON；Agent 与 NL 分析可配置不同模型。
- 回测默认本金 100 万，佣金万 1，持仓上限 10 支，总分阈值 `>= +0.7` 做多、`<= -0.7` 做空，短侧当前使用 `margin_secs` 代理券源假设。
- 因子贡献分析支持可选 Shapley attribution；完整验证回测要求登记因子列与 `factors.json` 对齐，并写出 `factor_attribution.json`。

2026-06-12 本轮维护

- 创建并验证 `quant` 环境，安装项目和必要科学计算/ML 包：pandas、numpy、pyarrow、duckdb、scikit-learn、statsmodels、matplotlib、requests、PyYAML、torch。
- 更新 `AGENTS.md`、`CLAUDE.md`、`docs/data_documentation.md`、`docs/environment_design.md`、`docs/agent_design.md`、`docs/pipeline_design.md`，同步 `quant` 环境、Docker 独立性、脚本结构、正式候选隔离和策略产物规则。
- 新增 `scripts/_bootstrap.py`，统一脚本直接运行时的 repo root / `src` 注入，并支持嵌套脚本。`scripts/` 现按职责归类为 `scripts/data/`、`scripts/experiments/`、`scripts/dev/`，脚本只保留薄 CLI 入口。
- 将 TuShare 底层 Parquet/sidecar/JSONL 读写拆到 `src/hl_trader/data_sources/tushare/io.py`；`download.py` 和 `audit.py` 保留显式 `pyarrow.parquet` 导入，避免隐式依赖。
- 将 Pipeline 配置、数据记录和默认 snapshot provider 拆到 `src/hl_trader/pipelines/config.py`，保持 `hl_trader.pipelines` 公共导出兼容。
- 在正式候选生成中加入阶段 snapshot 隐藏上下文，并补测试覆盖权限恢复。
- 策略产物 hash/diff/load/copy 前统一拒绝符号链接和特殊文件，并补单元测试。
- cron 模板和机器可读调度配置已改为 `~/miniconda3/envs/quant/bin/python`；当前只做 dry-run 校验，尚未安装刷新 crontab。
- 验证结果：脚本 help、cron dry-run、`unittest discover` 179 tests OK、`compileall` OK、`git diff --check` OK。未启动真实 LLM/Fold；本轮是结构与权限维护，单元和 Docker E2E 覆盖更直接。
- SubAgent 审计发现并已修复一个正式隔离漏洞：`snapshot_views` 不再挂载给 Docker Agent；Runner 改为刷新 `runtime/current_snapshot/` 并只读挂载为 `/mnt/snapshot`。补充测试覆盖 Agent 可读当前 `/mnt/snapshot`、不可读 `/mnt/runtime/snapshot_views`。
- 同步修复策略产物 symlink 的读前拒绝和 `modification_check_tool` 结构化失败返回；`.gitignore` 已忽略 `*.egg-info/` 和本地 `check.ipynb`。
- 复验结果：`unittest discover` 181 tests OK、`compileall` OK、`git diff --check` OK；受影响 Docker 隔离 E2E 通过。
- 已重跑上一轮实验 `exp_epoch_eval_001` 的报告生成：当前只保留跨 Epoch 总览图 `experiments/exp_epoch_eval_001/reports/epoch_comparison_returns.png`，以及每个 Epoch 单独收益图 `experiments/exp_epoch_eval_001/reports/epoch_returns/epoch_001_returns.png`；旧的 `fold_returns.png`、`cumulative_test_return.png` 和 `summary.json` 会由报告生成器清理。跨 Epoch 图同时展示单 Fold 收益和累积净值；单 Epoch 图将回撤标为 `Peak-to-current loss` 并移走遮挡性点位标注。
- `exp_epoch_eval_001` ledger 共 16 个开发 Fold、1 次 Epoch 正则化、1 个 Held-Out。开发冻结测试均值 +5.37%、中位数 +3.12%、正收益率 81.25%、累计 +107.73%；Held-Out 2026Q1 为 -1.65%。验证/测试收益相关性约 0.20，说明单轮结果还不能证明验证期选择具备稳定泛化。
- Held-out 支持按季度范围执行多段最终评估；例如 `--heldout-first-quarter 2026Q1 --heldout-last-quarter 2026Q2` 会生成两个 held-out run，前提是本地日历和 raw/snapshot 数据覆盖到 2026Q2。实验 CLI 默认主 Agent 模型为 `deepseek-v4-pro`，自然语言评分模型为 `deepseek-v4-flash`。
- 真实实验 `exp_real_smoke_20260612_191433` 已跑通一个常规 Fold、一个 Epoch 后正则化 Fold 和一个 Held-Out：常规 Fold 最终验证 `valid_002` 收益 +5.63%、Sharpe 2.16、最大回撤 7.44%；冻结测试 `test_000` 收益 -0.03%、Sharpe -0.13；Held-Out 2022Q2 收益 -0.81%、Sharpe -0.25。报告图已生成到 `experiments/exp_real_smoke_20260612_191433/reports/`。
- 本次真实跑前的首轮 `exp_real_smoke_20260612_180600` 在 100 候选 + `nl_mode=on` 下超过 60 分钟，被手动停止；它暴露了 result path 映射、NL 单票失败策略和候选池过大导致超时的问题。已改为默认 10 候选、NL 失败按中性审计处理，并将 Tool 返回路径映射为容器内 `/mnt/artifacts/...`。
- 复验结果：`git diff --check` 通过；`tests.unit.test_tools_flow tests.unit.test_nl_scoring tests.unit.test_pipeline_e2e tests.unit.test_reporting` 共 44 tests OK。
- 此前临时验证过“最后一个 Epoch 不再运行后置正则化”的方案；当前设计已切换为每个 Epoch 开始前的 meta-learning + optional regularization，下条记录为准。
- `exp_real_smoke_20260612_191433` trace 审计结论：常规 Fold 的 Agent 行为链路基本合理，能读取数据、写因子、处理 universe/NaN 错误、完成 `nl_mode=on` 验证并主动 `finish_fold`；NL 使用 `deepseek-v4-flash` 生成 grep/regex `pattern` 检索请求，结果文件完整。主要待优化点是通用风险词检索会混入非候选公司的文本，且 NL 输出没有实际填充 `applied_prior_ids`。
- 当前 Pipeline 采用 Epoch 开始前的 meta-learning + optional regularization：每个 Epoch 先生成 `meta_learning/<epoch_id>/taste.md` 并注入 Fold Agent Prompt；后续 Epoch 若元学习产物通过修改检查且确有策略改动，可冻结为本 Epoch 起点。
- 新增 host-side Tavily 搜索工具，只在 meta-learning Fold 开放；`TAVILY_API_KEY` 存在本地 ignored `.env`，小查询验证成功。普通 Fold Agent 和 NL scoring 不联网搜索。
- 强化 NL scoring：本地 grep 检索按候选公司相关证据优先排序，泛化 evidence 只能补背景且不能作为正式个股评分引用；最终 JSON 校验新增 `applied_prior_ids` 合法性检查，非中性或引用证据的评分必须引用 prior。文档、prompt export、CLI 和测试已同步。SubAgent 复审后补修：零改动 meta-learning 不再冻结新 artifact、Tavily HTTP 错误脱敏、移除旧 regularization session/ledger 主路径、meta-learning 使用紧凑 development history 摘要包。复验：相关 64 tests OK，`git diff --check` OK。
- 回测下单规则已调整：做多候选按 `final_score` 高分排序，做空候选按负分强度排序；默认 `proxy_margin_secs` 模式下，不可做空的短侧候选会在订单计划阶段跳过并顺延到下一个可做空候选，不再先占用持仓名额后由 Broker 直接拒单。正式回测和 Shapley attribution 路径共用同一规则，并在摘要记录 `short_unavailable_skipped_count`。复验：相关 64 tests OK，`py_compile` OK，`git diff --check` OK。
- 实验报告图已加入沪深300基准和相对收益：默认读取 `data/raw/index_daily/ts_code=000300.SH/`，按每个 Fold 回放区间首个交易日开盘到末个交易日收盘计算基准收益，并输出 `active_return = strategy_return - benchmark_return`。已通过 TuShare `index_daily` 本地补齐 000300.SH 的 20200102-20260612 年度分区，并重生成 `exp_epoch_eval_001` 报告图；基准覆盖 17/17 个周期，开发期平均相对收益 +6.03%，Held-Out 2026Q1 相对收益 +2.89%。复验：`tests.unit.test_reporting` OK，先前异常的 Pipeline E2E 单测复跑 OK，`py_compile` OK，`git diff --check` OK。
- 元学习搜索工具已新增 Semantic Scholar provider：`web_search` 仍只在 Epoch 前元学习 Fold 开放，CLI 可用 `--web-search-provider semantic_scholar` 切换，key 从本地 ignored `.env` 的 `SEMANTIC_SCHOLAR_API_KEY` 读取。实现使用 Semantic Scholar Graph API 小批量论文搜索，返回标题、摘要、作者、年份、引用数和链接，并做低频调用与错误脱敏。Live smoke 返回 2 条论文结果；复验：`tests.unit.test_sandbox_isolation tests.unit.test_pipeline_e2e` 共 30 tests OK，`py_compile` OK，`git diff --check` OK。
- PR 前完整复验：使用 package-aware discover 命令 `python -m unittest discover -s tests -t . -p 'test_*.py' -v` 跑通 190 tests OK；`compileall -q src scripts configs` OK；`git diff --check` OK。运行后已清理本地 `__pycache__` 等 ignored 缓存。注意：直接用 `unittest discover tests/unit` 会把部分测试作为顶层模块加载，导致相对导入失败；正确入口是 `-s tests -t .`。
