"""Creation-form parameter schema for the HITL console.

Field keys mirror ``autotrade.pipelines.interactive.PARAM_DEFAULTS`` (which in
turn mirror the run_experiment.py CLI dests); defaults are read from there so
this schema can never drift from the worker. Descriptions follow
docs/parameters_reference.md. ``experiments_root`` and ``work_root`` are
server-managed and intentionally absent from the form.
"""

from __future__ import annotations

from autotrade.pipelines.interactive import PARAM_DEFAULTS

SERVER_MANAGED_KEYS = ("experiments_root", "work_root")

# (key, group, label, type, extra) — type in {"string","text","int","float","bool","choice","multi"}
_FIELDS: list[dict[str, object]] = [
    # 基本与排程
    {"key": "experiment_id", "group": "基本与排程", "label": "实验名称（ID）", "type": "string", "required": True,
     "help": "唯一实验标识，仅限字母、数字、下划线和连字符；对应 experiments/<id>/ 目录。"},
    {"key": "fold_period", "group": "基本与排程", "label": "Fold 周期", "type": "choice",
     "choices": ["week", "month", "quarter", "year"],
     "help": "每个 Fold 的验证/测试周期粒度。验证区间取测试区间的前一个同频周期。"},
    {"key": "first_test_period", "group": "基本与排程", "label": "首个测试周期", "type": "string", "required": True,
     "help": "development 首个测试周期标签，如 2022Q1（quarter）、202201（month）、20220103（week）、2022（year）。"},
    {"key": "last_test_period", "group": "基本与排程", "label": "末个测试周期", "type": "string", "required": True,
     "help": "development 末个测试周期标签；与首个周期共同决定 Fold 数。"},
    {"key": "heldout_first_period", "group": "基本与排程", "label": "Held-out 起始周期", "type": "string", "required": True,
     "help": "最终冻结测试的起始周期；实验开始前冻结，不得与 development 重叠。"},
    {"key": "heldout_last_period", "group": "基本与排程", "label": "Held-out 结束周期", "type": "string", "required": True,
     "help": "最终冻结测试的结束周期。"},
    {"key": "epochs", "group": "基本与排程", "label": "Epoch 数", "type": "int",
     "help": "从首个 Fold 到末个 Fold 完整滚动的轮数；每个 Epoch 开始前运行一次元学习。"},
    # 运行控制（HITL）
    {"key": "initial_control_mode", "group": "运行控制", "label": "初始运行模式", "type": "choice",
     "choices": ["step", "auto"],
     "help": "step：每个会话（元学习/Fold/Held-out）开始前等待人工批准并可注入指令；auto：全自动连续执行，可随时暂停。"},
    {"key": "analysis_enabled", "group": "运行控制", "label": "Fold 完成后自动生成策略分析", "type": "bool",
     "help": "每个 Fold 结束后用预定义模板调用 LLM 生成自然语言策略分析（仅基于验证期证据）。"},
    {"key": "analysis_model", "group": "运行控制", "label": "策略分析模型", "type": "choice",
     "choices": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
     "help": "生成 Fold 策略分析所用的 DeepSeek 模型。"},
    {"key": "meta_learning_directive", "group": "运行控制", "label": "元学习探索方向（可选）", "type": "text",
     "help": "实验级研究方向，注入每个 Epoch 的元学习提示词；各 Epoch 也可在运行前单独覆盖。"},
    # 预算与验收
    {"key": "max_fold_minutes", "group": "预算与验收", "label": "单 Fold 推理时长（分钟）", "type": "int",
     "help": "每个 Fold 和元学习会话的推理墙钟上限；回测耗时独立计算并回补。"},
    {"key": "convergence_start_epoch", "group": "预算与验收", "label": "收敛起始 Epoch", "type": "int",
     "help": "从该 Epoch（1 起）开始 Fold 提示词进入收敛阶段：优先更小更稳的策略。"},
    {"key": "min_return", "group": "预算与验收", "label": "验收最低验证收益", "type": "float",
     "help": "冻结策略所需的最低验证总收益（AcceptanceRules.min_return）。"},
    {"key": "min_sharpe", "group": "预算与验收", "label": "验收最低 Sharpe", "type": "float",
     "help": "冻结策略所需的最低验证 Sharpe。"},
    {"key": "max_drawdown", "group": "预算与验收", "label": "验收最大回撤", "type": "float",
     "help": "冻结策略允许的最大验证回撤（0.25 = 25%）。"},
    {"key": "nl_failure_policy", "group": "预算与验收", "label": "NL 失败策略", "type": "choice",
     "choices": ["return_error_with_audit", "fail"],
     "help": "策略内 ctx.nl() 调用失败时：返回带审计的错误结果（默认）或使回测失败。"},
    {"key": "disable_step_tree", "group": "预算与验收", "label": "禁用 Step 产物树", "type": "bool", "advanced": True,
     "help": "关闭跨 Fold 的 Step 谱系树（仅用于消融实验）。"},
    # 数据窗口
    {"key": "window_months", "group": "数据窗口", "label": "基础历史窗口（月）", "type": "int",
     "help": "决策输入快照与 Fold 输入窗口的默认历史月数；各数据域未单独覆盖时回退此值。"},
    {"key": "daily_window_months", "group": "数据窗口", "label": "daily 域窗口（月）", "type": "int", "optional": True, "advanced": True,
     "help": "日线域单独窗口；留空回退基础窗口。"},
    {"key": "fundamentals_window_months", "group": "数据窗口", "label": "fundamentals 域窗口（月）", "type": "int", "optional": True, "advanced": True,
     "help": "基本面域单独窗口；留空回退基础窗口。"},
    {"key": "events_window_months", "group": "数据窗口", "label": "events 域窗口（月）", "type": "int", "optional": True, "advanced": True,
     "help": "事件域单独窗口；留空回退基础窗口。"},
    {"key": "macro_window_months", "group": "数据窗口", "label": "macro 域窗口（月）", "type": "int", "optional": True, "advanced": True,
     "help": "宏观域单独窗口；留空回退基础窗口。"},
    {"key": "text_window_months", "group": "数据窗口", "label": "text 域窗口（月）", "type": "int", "optional": True, "advanced": True,
     "help": "文本域单独窗口；留空回退基础窗口。"},
    {"key": "intraday_trade_days", "group": "数据窗口", "label": "分钟线交易日窗口", "type": "int",
     "help": "决策输入快照包含的最近可见分钟线交易日数。"},
    # 模型与上下文
    {"key": "model", "group": "模型与上下文", "label": "Agent 主模型", "type": "choice",
     "choices": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
     "help": "Fold/元学习 Agent 主对话模型。"},
    {"key": "nl_model", "group": "模型与上下文", "label": "NL 子代理模型", "type": "choice",
     "choices": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
     "help": "策略内 ctx.nl() 文本分析子代理模型。"},
    {"key": "compact_model", "group": "模型与上下文", "label": "上下文压缩模型", "type": "choice",
     "choices": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
     "help": "语义压缩长会话所用的低成本模型（无 thinking）。"},
    {"key": "reasoning_effort", "group": "模型与上下文", "label": "推理强度", "type": "choice",
     "choices": ["max", "xhigh", "high", "medium", "low"],
     "help": "启用 thinking 时 Agent 与 NL 调用的 DeepSeek 推理强度。"},
    {"key": "no_thinking", "group": "模型与上下文", "label": "禁用 thinking", "type": "bool", "advanced": True,
     "help": "关闭 provider 推理模式（Agent 与 NL 调用）。"},
    {"key": "disable_context_compact", "group": "模型与上下文", "label": "禁用语义压缩", "type": "bool", "advanced": True,
     "help": "关闭长会话语义上下文压缩。"},
    {"key": "compact_token_threshold", "group": "模型与上下文", "label": "压缩触发 token 阈值", "type": "int", "advanced": True,
     "help": "估算上下文 token 超过该值时触发语义压缩。"},
    {"key": "compact_keep_recent_messages", "group": "模型与上下文", "label": "压缩保留最近消息数", "type": "int", "advanced": True,
     "help": "语义压缩后保留的最近原始消息条数。"},
    {"key": "compact_max_tokens", "group": "模型与上下文", "label": "单次压缩输出 token 上限", "type": "int", "advanced": True,
     "help": "一次压缩摘要的最大输出 token。"},
    {"key": "compact_max_calls", "group": "模型与上下文", "label": "单会话压缩调用上限", "type": "int", "advanced": True,
     "help": "单个 Agent 会话的语义压缩调用次数上限。"},
    # 元学习联网与沙箱
    {"key": "web_search_engines", "group": "元学习联网", "label": "联网搜索引擎", "type": "multi",
     "choices": ["tavily", "semantic_scholar"],
     "help": "开放给元学习会话的搜索引擎（普通 Fold 不联网）。"},
    {"key": "meta_learning_network", "group": "元学习联网", "label": "元学习 Docker 网络", "type": "choice",
     "choices": ["bridge", "host", "none"],
     "help": "仅元学习会话的容器网络模式；bridge 直连公网，none 完全断网。"},
    {"key": "disable_meta_sandbox_rebuild", "group": "元学习联网", "label": "禁用派生镜像构建", "type": "bool", "advanced": True,
     "help": "忽略元学习写出的 sandbox_environment.json，不构建派生 Docker 镜像。"},
    {"key": "meta_learning_add_host_gateway", "group": "元学习联网", "label": "注入 host.docker.internal", "type": "bool", "advanced": True,
     "help": "bridge 模式下允许容器访问宿主托管 XRay 端口。"},
    {"key": "disable_meta_learning_host_proxy", "group": "元学习联网", "label": "禁用 AT_PROXY_* 代理别名", "type": "bool", "advanced": True,
     "help": "不把托管代理值以 AT_PROXY_* 别名注入元学习容器。"},
    {"key": "disable_meta_learning_managed_proxy", "group": "元学习联网", "label": "禁用宿主托管 XRay", "type": "bool", "advanced": True,
     "help": "即使存在 XRay 配置也不为元学习启动宿主托管代理进程。"},
    {"key": "meta_learning_xray_startup_timeout", "group": "元学习联网", "label": "XRay 启动超时（秒）", "type": "float", "advanced": True,
     "help": "等待托管 XRay 端口就绪的秒数，超时则元学习运行失败。"},
    {"key": "tavily_api_key_env", "group": "元学习联网", "label": "Tavily key 环境变量名", "type": "string", "advanced": True,
     "help": "只记录变量名，值永不写入 manifest。"},
    {"key": "semantic_scholar_api_key_env", "group": "元学习联网", "label": "Semantic Scholar key 环境变量名", "type": "string", "advanced": True,
     "help": "只记录变量名，值永不写入 manifest。"},
    # 路径（高级）
    {"key": "raw_dir", "group": "路径", "label": "raw 数据目录", "type": "string", "advanced": True,
     "help": "TuShare raw 数据根目录（相对仓库根）。"},
    {"key": "fundamental_events_root", "group": "路径", "label": "PIT 财务事件目录", "type": "string", "advanced": True,
     "help": "夜间构建的 PIT 财务事件层根目录。"},
    {"key": "fundamental_events_status", "group": "路径", "label": "财务事件状态文件", "type": "string", "advanced": True,
     "help": "财务事件构建状态 JSON（fail-fast 门禁输入）。"},
    {"key": "template_dir", "group": "路径", "label": "初始策略模板目录", "type": "string", "advanced": True,
     "help": "首个 Fold 无父产物时安装的初始 output/ 模板。"},
    {"key": "local_dev", "group": "路径", "label": "本地执行器（开发用）", "type": "bool", "advanced": True,
     "help": "不用 Docker 沙箱，仅本地开发/测试；正式实验必须关闭。"},
]

_GROUP_ORDER = ("基本与排程", "运行控制", "预算与验收", "数据窗口", "模型与上下文", "元学习联网", "路径")


def parameter_schema() -> dict[str, object]:
    """Grouped field schema with live defaults for the creation modal."""
    groups: dict[str, list[dict[str, object]]] = {name: [] for name in _GROUP_ORDER}
    for field in _FIELDS:
        entry = dict(field)
        key = str(entry["key"])
        default = PARAM_DEFAULTS.get(key)
        if isinstance(default, tuple):
            default = list(default)
        entry["default"] = default
        groups[str(entry.pop("group"))].append(entry)
    return {
        "schema_version": 1,
        "groups": [{"name": name, "fields": fields} for name, fields in groups.items()],
    }
