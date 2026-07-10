"""Creation-form parameter schema for the HITL console.

Field keys mirror ``autotrade.pipelines.hitl_state.PARAM_DEFAULTS`` (which in
turn mirror the run_experiment.py CLI dests); defaults are read from there so
this schema can never drift from the worker. Descriptions follow
docs/parameters_reference.md.

Deliberately NOT exposed in the form (server-managed or operator-only; the API
still accepts them for headless use): ``experiments_root``, ``work_root``,
``raw_dir``, ``fundamental_events_root``, ``fundamental_events_status``,
``template_dir``, ``local_dev``, ``tavily_api_key_env``,
``semantic_scholar_api_key_env``.

Period labels are error-prone to type, so the four period fields render as
dropdowns whenever the server can enumerate valid labels from the SSE trading
calendar (``build_period_options``); without a calendar they degrade to plain
text inputs.
"""

from __future__ import annotations

import bisect

import pandas as pd

from autotrade.pipelines.folds import MIN_REGION_TRADE_DAYS, period_bounds
from autotrade.pipelines.hitl_state import PARAM_DEFAULTS

SERVER_MANAGED_KEYS = ("experiments_root", "work_root")
HIDDEN_KEYS = (
    "raw_dir",
    "fundamental_events_root",
    "fundamental_events_status",
    "template_dir",
    "local_dev",
    "tavily_api_key_env",
    "semantic_scholar_api_key_env",
    "meta_learning_env",
    "meta_learning_host_proxy",
    "meta_learning_xray_bin",
)
PERIOD_KEYS = ("first_test_period", "last_test_period", "heldout_first_period", "heldout_last_period")
# The in-client sandbox agent stack is wired for the v4 interfaces; chat and
# reasoner are legacy endpoints and stay out of the console choices.
MODEL_CHOICES = ["deepseek-v4-pro", "deepseek-v4-flash"]
DEV_DEFAULT_PERIODS = 4  # suggested development length (test periods) per cadence

# (key, group, label, type, extra) — type in {"string","text","int","float","bool","choice","multi","period"}
_FIELDS: list[dict[str, object]] = [
    # 基本与排程
    {"key": "experiment_id", "group": "基本与排程", "label": "实验名称（ID）", "type": "string", "required": True,
     "help": "唯一实验标识，仅限字母、数字、下划线和连字符；对应 experiments/<id>/ 目录。"},
    {"key": "fold_period", "group": "基本与排程", "label": "Fold 周期", "type": "choice",
     "choices": ["week", "month", "quarter", "year"],
     "help": "每个 Fold 的验证/测试周期粒度。验证区间取测试区间的前一个同频周期；切换后下方周期选项随之变化。"},
    {"key": "first_test_period", "group": "基本与排程", "label": "首个测试周期（Fold 以测试周期命名）", "type": "period", "required": True,
     "help": "development 首个 Fold 的测试（样本外）周期；其前一个同频周期自动作为该 Fold 的验证区间，无需单独配置。"},
    {"key": "last_test_period", "group": "基本与排程", "label": "末个测试周期", "type": "period", "required": True,
     "help": "development 末个 Fold 的测试周期；与首个周期共同决定 Fold 数。每个 Fold 的验证区间 = 其测试周期的前一同频周期。"},
    {"key": "heldout_first_period", "group": "基本与排程", "label": "Held-out 起始周期", "type": "period", "required": True,
     "help": "最终冻结测试的起始周期；实验开始前冻结，必须晚于末个测试周期、不得重叠。"},
    {"key": "heldout_last_period", "group": "基本与排程", "label": "Held-out 结束周期", "type": "period", "required": True,
     "help": "最终冻结测试的结束周期。"},
    {"key": "epochs", "group": "基本与排程", "label": "Epoch 数", "type": "int",
     "help": "从首个 Fold 到末个 Fold 完整滚动的轮数；每个 Epoch 开始前运行一次元学习。"},
    {"key": "inherit_from", "group": "基本与排程", "label": "继承已有实验的 Agent Output", "type": "choice",
     "choices": [],  # filled at request time with experiments that have ≥1 recorded fold
     "help": "留空=从空白模板开始。选择后，新实验的首个 Fold 以该实验最新冻结的策略产物（output+models）为父产物起步；创建时拷贝并哈希校验，源实验之后删除也不受影响。"},
    # 运行控制（HITL）
    {"key": "initial_control_mode", "group": "运行控制", "label": "初始运行模式", "type": "choice",
     "choices": ["step", "auto"],
     "help": "step：每个会话（元学习/Fold/Held-out）开始前等待人工批准并可注入指令；auto：全自动连续执行，可随时暂停。"},
    {"key": "analysis_enabled", "group": "运行控制", "label": "Fold 完成后自动生成策略分析", "type": "bool",
     "help": "每个 Fold 结束后用预定义模板调用 LLM 生成自然语言策略分析（仅基于验证期证据）。"},
    {"key": "analysis_model", "group": "运行控制", "label": "策略分析模型", "type": "choice",
     "choices": list(MODEL_CHOICES),
     "help": "生成 Fold 策略分析所用的 DeepSeek 模型。"},
    {"key": "analysis_max_tokens", "group": "运行控制", "label": "策略分析输出 token 上限", "type": "int",
     "help": "单次分析调用的输出 token 基础配额（思考 token 计入）；遇 finish_reason=length 自动以 2 倍重试一次。"},
    # meta_learning_directive 有意不进创建表单：进入实验详情页后在元学习会话
    # 的指令面板填写（逐 Epoch 可覆盖），避免创建时与详情页两处重复输入。
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
    {"key": "max_steps_per_fold", "group": "预算与验收", "label": "单 Fold Step 数上限", "type": "int",
     "help": "单 Fold 完整验证回测驱动的 Step 数上限。"},
    {"key": "max_backtests_per_fold", "group": "预算与验收", "label": "单 Fold 回测次数上限", "type": "int",
     "help": "回测独立计时（墙钟回补推理 deadline），该值限制其总次数。"},
    {"key": "nl_failure_policy", "group": "预算与验收", "label": "NL 失败策略", "type": "choice",
     "choices": ["return_error_with_audit", "fail"],
     "help": "策略内 ctx.nl() 调用失败时：返回带审计的错误结果（默认）或使回测失败。"},
    {"key": "finalize_before_deadline_seconds", "group": "预算与验收", "label": "收尾提示窗口（秒）", "type": "int", "advanced": True,
     "help": "距推理 deadline 该秒数时注入一次收尾提示（wrap-up）。"},
    {"key": "per_call_timeout_seconds", "group": "预算与验收", "label": "单次 LLM 调用超时（秒）", "type": "int", "advanced": True,
     "help": "Agent 主对话单次 provider 调用的硬超时。"},
    {"key": "disable_step_tree", "group": "预算与验收", "label": "禁用 Step 产物树", "type": "bool", "advanced": True,
     "help": "关闭跨 Fold 的 Step 谱系树（仅用于消融实验）。"},
    {"key": "record_failed_attempts", "group": "预算与验收", "label": "记录失败尝试节点", "type": "bool", "advanced": True,
     "help": "Step 树中记录未通过验证的轻量 [failed] 节点，提示后续 Fold 避开死路。"},
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
    # 回放执行
    {"key": "nl_max_calls_per_decision_day", "group": "回放执行", "label": "NL 子代理日均配额", "type": "int",
     "help": "每回测 NL 调用配额 = 该值 × 决策天数；策略内 ctx.nl() 的总预算。"},
    {"key": "offsession_tick_minutes", "group": "回放执行", "label": "盘外研究 tick 间距（分钟）", "type": "int", "advanced": True,
     "help": "24h 网格中盘外时段调用 main(ctx) 的间距（默认 30）；0 关闭盘外 tick（盘外不下单）。"},
    {"key": "intraday_decision_minutes", "group": "回放执行", "label": "日内决策粒度（分钟）", "type": "int",
     "help": "普通日内 bar 上 main(ctx) 的调用间距（默认 1 = 每分钟）。Broker 仍逐分钟撮合、竞价与盘外 tick 不受影响；调大可大幅缩短回测耗时，但降低日内反应粒度。"},
    {"key": "execution_lag_bars", "group": "回放执行", "label": "执行滞后（bar 数）", "type": "int", "advanced": True,
     "help": "决策 bar 到撮合 bar 的固定滞后，模拟实盘提交延迟。"},
    {"key": "decision_max_sim_minutes", "group": "回放执行", "label": "substep 预算上限（分钟）", "type": "float", "optional": True, "advanced": True,
     "help": "ctx.substep 可声明预算 B 的上限；留空不限制。"},
    {"key": "backtest_max_seconds_per_decision", "group": "回放执行", "label": "单决策墙钟上限（秒）", "type": "float", "advanced": True,
     "help": "验证回测中单个 main(ctx) tick（含 NL）的真实墙钟硬上限，超限杀驱动；最终评估默认使用该值的 3 倍作为防挂死兜底。"},
    {"key": "backtest_max_seconds_per_trading_day", "group": "回放执行", "label": "单交易日墙钟上限（秒）", "type": "float", "advanced": True,
     "help": "验证回测中单交易日累计计算墙钟上限，超限中止回放；最终评估默认使用该值的 3 倍作为防挂死兜底。"},
    {"key": "nl_max_calls_per_backtest", "group": "回放执行", "label": "单回测 NL 上限（可选）", "type": "int", "optional": True, "advanced": True,
     "help": "在日均配额之外进一步收紧单次回测的 NL 总次数；留空不启用。"},
    # Broker 账户
    {"key": "stock_initial_cash", "group": "Broker 账户", "label": "普通账户初始资金（元）", "type": "float",
     "help": "long-only 现金账户初始资金；组合权益 = 两账户之和。"},
    {"key": "credit_initial_cash", "group": "Broker 账户", "label": "信用账户初始资金（元）", "type": "float",
     "help": "担保品买卖 + 融资融券账户初始资金；运行中可经 transfer 划转。"},
    {"key": "max_total_holdings", "group": "Broker 账户", "label": "最大持仓数（可选）", "type": "int", "optional": True,
     "help": "跨账户去重的最大同时持仓代码数；留空交给 Agent 自控。"},
    {"key": "max_single_name_weight", "group": "Broker 账户", "label": "单票权重上限（可选）", "type": "float", "optional": True,
     "help": "单只股票占组合权益的名义上限（0.2 = 20%）；留空交给 Agent 自控。"},
    {"key": "commission_bps", "group": "Broker 账户", "label": "佣金（bp）", "type": "float", "advanced": True,
     "help": "万一 = 1.0；受最低佣金 5 元/笔约束。"},
    {"key": "slippage_bps", "group": "Broker 账户", "label": "市价滑点（bp）", "type": "float", "advanced": True,
     "help": "市价 taker 成交滑点；限价/竞价成交不计滑点。"},
    {"key": "fin_rate_annual", "group": "Broker 账户", "label": "融资利率（年化）", "type": "float", "advanced": True,
     "help": "融资负债按自然日计息的年化利率（研究假设）。"},
    {"key": "slo_rate_annual", "group": "Broker 账户", "label": "融券费率（年化）", "type": "float", "advanced": True,
     "help": "融券负债按自然日计息的年化费率（研究假设）。"},
    # 模型与上下文
    {"key": "model", "group": "模型与上下文", "label": "Agent 主模型", "type": "choice",
     "choices": list(MODEL_CHOICES),
     "help": "Fold/元学习 Agent 主对话模型。"},
    {"key": "nl_model", "group": "模型与上下文", "label": "NL 子代理模型", "type": "choice",
     "choices": ["deepseek-v4-flash", "deepseek-v4-pro"],
     "help": "策略内 ctx.nl() 文本分析子代理模型。"},
    {"key": "compact_model", "group": "模型与上下文", "label": "上下文压缩模型", "type": "choice",
     "choices": ["deepseek-v4-flash", "deepseek-v4-pro"],
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
    {"key": "meta_memory_max_epochs", "group": "元学习联网", "label": "元学习原始记忆 Epoch 数", "type": "int", "advanced": True,
     "help": "拼接给下一次元学习的最近 Epoch 完整对话数（0 关闭原始记忆）。"},
    {"key": "meta_sandbox_rebuild_timeout_seconds", "group": "元学习联网", "label": "派生镜像构建超时（秒）", "type": "int", "advanced": True,
     "help": "元学习请求新依赖时 docker build 的超时上限。"},
    {"key": "meta_sandbox_image_keep", "group": "元学习联网", "label": "派生镜像保留数", "type": "int", "advanced": True,
     "help": "本实验保留的派生沙箱镜像数，更旧的尽力 GC。"},
]

_GROUP_ORDER = ("基本与排程", "运行控制", "预算与验收", "数据窗口", "回放执行", "Broker 账户", "模型与上下文", "元学习联网")


def build_period_options(trading_days: list[str]) -> dict[str, list[str]]:
    """Enumerate complete, backtestable period labels per cadence.

    A label qualifies when its calendar bounds are fully covered by the trading
    calendar and it holds at least MIN_REGION_TRADE_DAYS trading days (the
    replay reserves the final day for forced liquidation). Oldest -> newest.
    """
    days = sorted(str(day) for day in trading_days)
    if not days:
        return {}
    first, last = days[0], days[-1]

    def qualified(label: str, period: str) -> bool:
        start, end = period_bounds(label, period=period)
        # Only the end is policed: a period whose calendar start precedes the
        # first trading day is normal (new-year holidays), while end > last
        # means the period is not yet complete/covered.
        if end > last or end < first:
            return False
        count = bisect.bisect_right(days, end) - bisect.bisect_left(days, start)
        return count >= MIN_REGION_TRADE_DAYS

    options: dict[str, list[str]] = {}
    first_ts, last_ts = pd.Timestamp(first), pd.Timestamp(last)
    candidates: dict[str, list[str]] = {
        "quarter": [f"{p.year}Q{p.quarter}" for p in pd.period_range(first_ts, last_ts, freq="Q")],
        "month": [p.strftime("%Y%m") for p in pd.period_range(first_ts, last_ts, freq="M")],
        "year": [p.strftime("%Y") for p in pd.period_range(first_ts, last_ts, freq="Y")],
        "week": [ts.strftime("%Y%m%d") for ts in pd.date_range(first_ts, last_ts, freq="W-MON")],
    }
    for period, labels in candidates.items():
        qualified_labels = [label for label in labels if qualified(label, period)]
        if qualified_labels:
            options[period] = qualified_labels
    return options


def suggest_period_defaults(options: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """Safe defaults per cadence: recent development window + the latest complete
    period as held-out (held-out must follow development without overlap)."""
    defaults: dict[str, dict[str, str]] = {}
    for period, labels in options.items():
        if len(labels) < 3:
            continue
        heldout = labels[-1]
        last_test = labels[-2]
        first_test = labels[max(1, len(labels) - 1 - DEV_DEFAULT_PERIODS)]
        defaults[period] = {
            "first_test_period": first_test,
            "last_test_period": last_test,
            "heldout_first_period": heldout,
            "heldout_last_period": heldout,
        }
    return defaults


def parameter_schema(
    trading_days: list[str] | None = None, inherit_sources: list[str] | None = None
) -> dict[str, object]:
    """Grouped field schema with live defaults for the creation modal.

    With a trading calendar the four period fields become dependent dropdowns
    (``type: period`` + top-level ``period_options``/``period_defaults``);
    without one they degrade to required text inputs. ``inherit_sources``
    fills the inherit_from dropdown (experiments with ≥1 recorded fold).
    """
    period_options = build_period_options(trading_days or [])
    period_defaults = suggest_period_defaults(period_options)
    default_cadence = str(PARAM_DEFAULTS["fold_period"])
    groups: dict[str, list[dict[str, object]]] = {name: [] for name in _GROUP_ORDER}
    for field in _FIELDS:
        entry = dict(field)
        key = str(entry["key"])
        default = PARAM_DEFAULTS.get(key)
        if isinstance(default, tuple):
            default = list(default)
        if entry["type"] == "period":
            if period_options:
                default = period_defaults.get(default_cadence, {}).get(key)
            else:
                entry["type"] = "string"
        if key == "inherit_from":
            entry["choices"] = ["", *(inherit_sources or [])]
        entry["default"] = default
        groups[str(entry.pop("group"))].append(entry)
    return {
        "schema_version": 2,
        "groups": [{"name": name, "fields": fields} for name, fields in groups.items() if fields],
        "period_options": period_options,
        "period_defaults": period_defaults,
    }
