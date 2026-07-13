"""De-stringed persistent ``main(ctx)`` sandbox driver (R16; docs/environment_design.md).

Loaded by file inside the Agent sandbox (``python main_ctx_driver.py <main.py> <marker>``).
It serves one per-tick RPC over
stdin/stdout: build a ``ctx``, call the Agent's ``main(ctx)``, and return the orders,
declared sub-steps, and staged state writes. Broker actions are only accepted inside
``ctx.substep`` and are delayed-submit plans; the agent-visible cash / buying power /
position view reflects filled broker state, while in-flight plans are visible through
``ctx.broker.pending()``.

This module imports only the Python standard library (no ``autotrade``, ``pandas``, or
``broker_core`` import), so it runs unchanged in the dependency-light sandbox image.
"""

import builtins, contextlib, filecmp, importlib.util, json, math, os, re, resource, shutil, sys, threading, time, traceback, types, uuid
from datetime import datetime, timedelta
from pathlib import Path

_PROTOCOL_STDOUT = sys.stdout


def _public_strategy_error(exc, *, main_path, request, snapshot_dir):
    """Return only allowlisted, non-sensitive repair metadata for Probe feedback."""
    message = str(exc)
    if message.startswith("main.py failed to import:"):
        return {
            "public_error_type": "strategy_contract_error",
            "public_reason": "strategy_import_failed",
            "public_retry_hint": "Check output imports and keep module-level code limited to imports and constants.",
        }
    if "ctx.state_dir is only available inside ctx.substep" in message:
        return {
            "public_error_type": "strategy_contract_error",
            "public_reason": "state_dir_outside_substep",
            "public_retry_hint": "Access ctx.state_dir only inside ctx.substep(name, budget_minutes=B).",
        }
    if isinstance(exc, TypeError) and "'dict' object is not callable" in message:
        strategy_root = Path(main_path).parent
        for frame in traceback.extract_tb(exc.__traceback__):
            try:
                in_strategy = _is_under(_normalize_path(frame.filename), _normalize_path(strategy_root))
            except (OSError, TypeError):
                in_strategy = False
            if in_strategy and re.search(r"\.\s*broker\s*\.\s*(stock|credit)\s*\(", frame.line or ""):
                return {
                    "public_error_type": "strategy_contract_error",
                    "public_reason": "account_view_not_callable",
                    "public_retry_hint": (
                        "ctx.broker.stock and ctx.broker.credit are dict properties; "
                        "use ctx.broker.stock['available_cash'] without parentheses."
                    ),
                }
    state = request.get("state") or {}
    rolling_asof_dir = state.get("asof_dir") if isinstance(state, dict) else None
    asof_dir = rolling_asof_dir or snapshot_dir
    missing = _normalize_path(getattr(exc, "filename", None))

    def matches_known_path(path):
        normalized = _normalize_path(path)
        return normalized is not None and (
            missing == normalized
            or re.search(
                r"(?<![A-Za-z0-9_./-])" + re.escape(str(normalized)) + r"(?![A-Za-z0-9_./-])",
                message,
            ) is not None
        )

    wrong_universe = Path(str(asof_dir)) / "universe"
    if matches_known_path(wrong_universe):
        return {
            "public_error_type": "strategy_contract_error",
            "public_reason": "universe_path_mismatch",
            "public_retry_hint": "Read the frozen universe from Path(ctx.asof_dir) / 'universe.parquet'.",
        }
    # Timeview domains are partitioned Parquet dataset directories. Pandas raises
    # FileNotFoundError for a wrong suffix, while DuckDB raises IOException with
    # the path only in its message; match only trusted, allowlisted paths and never
    # return the raw exception text.
    if rolling_asof_dir:
        for domain in (
            "daily",
            "intraday_1min",
            "auction",
            "events",
            "macro",
            "fundamentals",
            "text_index",
        ):
            if matches_known_path(Path(str(asof_dir)) / (domain + ".parquet")):
                return {
                    "public_error_type": "strategy_contract_error",
                    "public_reason": "asof_path_mismatch",
                    "public_retry_hint": (
                        "Read rolling Timeview data from Path(ctx.asof_dir) / %r; "
                        "it is a partitioned dataset directory without a .parquet suffix."
                    ) % domain,
                }
    return {}


def _peak_rss_bytes():
    """Peak RSS of this one-shot formal driver (Linux ru_maxrss is KiB)."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024

_ACTION_ACCOUNT_OP = {
    "buy": ("stock", 23),
    "sell": ("stock", 24),
    "fin_buy": ("credit", 27),
    "short": ("credit", 28),
    "cover": ("credit", 29),
    "sell_repay": ("credit", 31),
    "direct_repay": ("credit", 32),
    "credit_buy": ("credit", 33),
    "credit_sell": ("credit", 34),
}

def _normalize_path(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _path_aliases(value):
    path = _normalize_path(value)
    if path is None:
        return ()
    aliases = [path]
    try:
        real_path = Path(os.path.realpath(str(path)))
    except OSError:
        real_path = path
    if real_path not in aliases:
        aliases.append(real_path)
    return tuple(aliases)


def _path_roots(env_name):
    roots = []
    for item in os.environ.get(env_name, "").split(os.pathsep):
        if not item:
            continue
        roots.extend(_path_aliases(item))
    return tuple(roots)


_FORBIDDEN_PATHS = _path_roots("AT_FORBIDDEN_PATHS")
_WRITE_FORBIDDEN_PATHS = _path_roots("AT_WRITE_FORBIDDEN_PATHS")
_STATE_VISIBLE_ROOTS = _path_roots("AT_STATE_DIR")
_STATE_STAGING_ROOTS = _path_roots("AT_STATE_STAGING_DIR")
_DISABLE_LINKS = os.environ.get("AT_DISABLE_LINKS", "") == "1"
_STATE_GUARD = {"internal": 0, "active": ()}


def _is_under(path, root):
    return path == root or root in path.parents


def _is_under_any(path, roots):
    return any(_is_under(path, root) for root in roots)


def _guard_path(value, *, write=False):
    aliases = _path_aliases(value)
    if not aliases:
        return
    if not _STATE_GUARD["internal"]:
        active_state_roots = tuple(_STATE_GUARD.get("active") or ())
        for path in aliases:
            if _is_under_any(path, _STATE_VISIBLE_ROOTS):
                raise PermissionError(
                    "formal strategy cannot access managed state path directly; "
                    "use ctx.state_dir inside ctx.substep(name, budget_minutes=B)"
                )
            if _is_under_any(path, _STATE_STAGING_ROOTS) and not _is_under_any(path, active_state_roots):
                raise PermissionError(
                    "formal strategy cannot access managed state staging path directly; "
                    "use ctx.state_dir inside ctx.substep(name, budget_minutes=B)"
                )
    roots = _FORBIDDEN_PATHS + (_WRITE_FORBIDDEN_PATHS if write else ())
    for path in aliases:
        for forbidden in roots:
            if _is_under(path, forbidden):
                action = "write" if write else "access"
                raise PermissionError(f"formal strategy cannot {action} forbidden path: {value}")


@contextlib.contextmanager
def _internal_state_access():
    _STATE_GUARD["internal"] += 1
    try:
        yield
    finally:
        _STATE_GUARD["internal"] -= 1


def _open_is_write(args, kwargs):
    mode = kwargs.get("mode")
    if mode is None and args:
        mode = args[0]
    if mode is None:
        return False
    mode_text = str(mode)
    return any(flag in mode_text for flag in ("w", "a", "x", "+"))


def _os_open_is_write(flags):
    write_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | os.O_TRUNC
    )
    return bool(int(flags) & write_flags)


def _deny_link_creation():
    if _DISABLE_LINKS:
        raise PermissionError("formal strategy cannot create links during replay")


_open = builtins.open


def _guarded_open(file, *args, **kwargs):
    _guard_path(file, write=_open_is_write(args, kwargs))
    return _open(file, *args, **kwargs)


_path_open = Path.open


def _guarded_path_open(self, *args, **kwargs):
    _guard_path(self, write=_open_is_write(args, kwargs))
    return _path_open(self, *args, **kwargs)


_os_open = os.open


def _guarded_os_open(path, *args, **kwargs):
    flags = kwargs.get("flags")
    if flags is None and args:
        flags = args[0]
    _guard_path(path, write=_os_open_is_write(flags or 0))
    return _os_open(path, *args, **kwargs)


_os_mkdir = os.mkdir


def _guarded_os_mkdir(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_mkdir(path, *args, **kwargs)


_os_makedirs = os.makedirs


def _guarded_os_makedirs(name, *args, **kwargs):
    _guard_path(name, write=True)
    return _os_makedirs(name, *args, **kwargs)


_path_mkdir = Path.mkdir


def _guarded_path_mkdir(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_mkdir(self, *args, **kwargs)


_os_unlink = os.unlink


def _guarded_os_unlink(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_unlink(path, *args, **kwargs)


_path_unlink = Path.unlink


def _guarded_path_unlink(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_unlink(self, *args, **kwargs)


_os_rmdir = os.rmdir


def _guarded_os_rmdir(path, *args, **kwargs):
    _guard_path(path, write=True)
    return _os_rmdir(path, *args, **kwargs)


_path_rmdir = Path.rmdir


def _guarded_path_rmdir(self, *args, **kwargs):
    _guard_path(self, write=True)
    return _path_rmdir(self, *args, **kwargs)


_os_rename = os.rename


def _guarded_os_rename(src, dst, *args, **kwargs):
    _guard_path(src, write=True)
    _guard_path(dst, write=True)
    return _os_rename(src, dst, *args, **kwargs)


_os_replace = os.replace


def _guarded_os_replace(src, dst, *args, **kwargs):
    _guard_path(src, write=True)
    _guard_path(dst, write=True)
    return _os_replace(src, dst, *args, **kwargs)


_path_rename = Path.rename


def _guarded_path_rename(self, target):
    _guard_path(self, write=True)
    _guard_path(target, write=True)
    return _path_rename(self, target)


_path_replace = Path.replace


def _guarded_path_replace(self, target):
    _guard_path(self, write=True)
    _guard_path(target, write=True)
    return _path_replace(self, target)


_os_symlink = os.symlink


def _guarded_os_symlink(src, dst, *args, **kwargs):
    _deny_link_creation()
    _guard_path(src)
    _guard_path(dst, write=True)
    return _os_symlink(src, dst, *args, **kwargs)


_path_symlink_to = Path.symlink_to


def _guarded_path_symlink_to(self, target, *args, **kwargs):
    _deny_link_creation()
    _guard_path(target)
    _guard_path(self, write=True)
    return _path_symlink_to(self, target, *args, **kwargs)


_os_link = os.link


def _guarded_os_link(src, dst, *args, **kwargs):
    _deny_link_creation()
    _guard_path(src)
    _guard_path(dst, write=True)
    return _os_link(src, dst, *args, **kwargs)


_path_hardlink_to = Path.hardlink_to


def _guarded_path_hardlink_to(self, target):
    _deny_link_creation()
    _guard_path(target)
    _guard_path(self, write=True)
    return _path_hardlink_to(self, target)


_os_listdir = os.listdir


def _guarded_os_listdir(path=None):
    if path is not None:
        _guard_path(path)
        return _os_listdir(path)
    return _os_listdir()


_os_scandir = os.scandir


def _guarded_os_scandir(path=None):
    if path is not None:
        _guard_path(path)
        return _os_scandir(path)
    return _os_scandir()


def _install_path_guards():
    """Install process-wide guards only in the standalone replay driver."""
    builtins.open = _guarded_open
    Path.open = _guarded_path_open
    os.open = _guarded_os_open
    os.mkdir = _guarded_os_mkdir
    os.makedirs = _guarded_os_makedirs
    Path.mkdir = _guarded_path_mkdir
    os.unlink = _guarded_os_unlink
    os.remove = _guarded_os_unlink
    Path.unlink = _guarded_path_unlink
    os.rmdir = _guarded_os_rmdir
    Path.rmdir = _guarded_path_rmdir
    os.rename = _guarded_os_rename
    os.replace = _guarded_os_replace
    Path.rename = _guarded_path_rename
    Path.replace = _guarded_path_replace
    os.symlink = _guarded_os_symlink
    Path.symlink_to = _guarded_path_symlink_to
    os.link = _guarded_os_link
    Path.hardlink_to = _guarded_path_hardlink_to
    os.listdir = _guarded_os_listdir
    os.scandir = _guarded_os_scandir

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
)


def _sanitize_error(value):
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


_RESP_STATE = {"offset": 0, "responses": {}}
_RUNTIME = {"active_substeps": 0}


def _read_responses(path):
    state = _RESP_STATE
    try:
        with open(path, "rb") as handle:
            handle.seek(state["offset"])
            chunk = handle.read()
    except FileNotFoundError:
        return state["responses"]
    head, sep, _partial = chunk.rpartition(b"\n")
    if sep:
        state["offset"] += len(head) + len(sep)
        for raw in head.splitlines():
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            state["responses"][str(record.get("request_id"))] = record
    return state["responses"]


def _nl(ts_code="", prompt="", *, timeout_seconds=None, content_only=False, **kwargs):
    if _RUNTIME["active_substeps"] <= 0:
        raise RuntimeError(
            "ctx.nl() must be called inside ctx.substep(name, budget_minutes=B); "
            "wrap text reads in a positive-budget substep so runtime is accounted consistently"
        )
    request_path = os.environ.get("AT_NL_REQUESTS_PATH", "")
    response_path = os.environ.get("AT_NL_RESPONSES_PATH", "")
    if not request_path or not response_path:
        raise RuntimeError("nl tool is not configured for this backtest")
    request_id = uuid.uuid4().hex
    _append_jsonl(request_path, {"request_id": request_id, "ts_code": str(ts_code), "prompt": str(prompt or ""), "kwargs": kwargs})
    timeout = float(timeout_seconds or os.environ.get("AT_NL_TOOL_TIMEOUT_SECONDS", "300"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _read_responses(response_path).get(request_id)
        if response is not None:
            if response.get("status") != "ok":
                raise RuntimeError(str(response.get("error", "nl tool failed")))
            result = response.get("result") or {}
            return str(result.get("content", "")) if content_only else result
        time.sleep(0.05)
    raise TimeoutError("nl tool timed out after %ss for %s" % (timeout, ts_code))


tools_module = types.ModuleType("at_tools")
tools_module.nl = _nl
sys.modules["at_tools"] = tools_module


def _bar_price(bar):
    if not bar:
        return None
    for field in ("close", "open"):
        value = bar.get(field)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


class _Broker:
    """Market-wide, ts_code-keyed view of the host Broker primitives.

    Calls are recorded as deferred actions applied by the trusted host Broker with full
    market constraints. Formal strategy actions must be issued inside ``ctx.substep``;
    inside a substep they are submission plans. Filled cash/position stay literal;
    available cash, credit buying power, and sellable shares may already reflect
    host-side reservations from orders submitted on earlier ticks. The in-flight
    plans are exposed through ``pending()`` for de-duplication and cancellation."""

    def __init__(self, state):
        account = state.get("account") or {}
        self.account = account
        self.positions = state.get("positions") or []
        self._debt_contracts = state.get("debt_contracts") or []
        self._cur_time = str(state.get("cur_time", "") or "")
        self._cur_datetime = str(state.get("cur_datetime", "") or "")
        self._tick_key = _safe_name(self._cur_datetime or self._cur_time or "tick")
        self._client_seq = 0
        # Filled cash/quantity reflect the broker truth at tick entry. Available
        # cash, credit buying power, and sellable shares can include host-side
        # reservations for orders submitted on earlier ticks; actions recorded in
        # this same substep are not projected back into these views.
        self._working = state.get("pending") or {}
        self._pos = {}
        for item in self.positions:
            key = (str(item.get("account", "")), str(item.get("ts_code")))
            qty = int(item.get("quantity", 0) or 0)
            side = str(item.get("side", "long"))
            self._pos[key] = qty if side == "long" else -qty
        self._actions = []
        self._cancelled_this_tick = set()
        self._substeps = []      # [{name, budget_minutes, real_wall_s}] declared this tick
        self._substep_names = set()
        self._substep_budgets = {}
        self._cur_substep = None  # name of the open ctx.substep, used for host delayed-submit routing
        self._staged = []        # [{staging_rel, state_rel, substep, budget_minutes}] this tick

    @property
    def stock(self):
        """普通账户 view. ``cash`` is filled truth; ``available_cash`` may already
        reserve orders submitted on earlier ticks. Same-substep plans are not
        projected back into this dict."""
        return self.account.get("stock") or {}

    def position(self, ts_code, account=None):
        """Signed filled shares; the default sums both accounts (a stock-account
        long hedged by a credit-account short nets to zero — pass account= to see
        each leg)."""
        code = str(ts_code)
        if account is not None:
            return self._pos.get((self._account_name(account), code), 0)
        return sum(qty for (_, held_code), qty in self._pos.items() if held_code == code)

    @staticmethod
    def _account_name(account):
        name = str(account or "").strip().lower()
        if name not in ("stock", "credit"):
            raise ValueError("account must be 'stock' or 'credit', got %r" % (account,))
        return name

    def pending(self, ts_code=None):
        """Still-working orders for ``ts_code`` — those queued on earlier ticks and
        not yet filled, plus any submitted this tick. Mirrors the live order query
        so re-entry/exit logic can skip codes with an order already in flight."""
        code = None if ts_code is None else str(ts_code)
        if code is None:
            working = [item for records in self._working.values() for item in records]
        else:
            working = list(self._working.get(code, []))
        working = [
            item for item in working
            if str(item.get("order_id", "")) not in self._cancelled_this_tick
        ]
        for action in self._actions:
            if action.get("action") == "cancel":
                continue
            if str(action.get("order_id", "")) in self._cancelled_this_tick:
                continue
            if code is not None and str(action.get("ts_code")) != code:
                continue
            record = self._pending_action_record(action)
            if record is not None:
                working.append(record)
        return working

    def _pending_action_record(self, action):
        if action.get("action") == "transfer":
            return None
        record = {
            key: value
            for key, value in dict(action).items()
            if key != "_substep"
        }
        substep = action.get("_substep")
        if substep is not None:
            step_name = str(substep)
            budget = self._substep_budgets.get(step_name)
            if budget is not None and float(budget) >= 1.0:
                return None
            record["pending_stage"] = "submit_lag"
            record["substep"] = step_name
            if budget is not None:
                record["ready_at"] = self._ready_at_iso(float(budget))
        record.setdefault("status", "pending")
        record.setdefault("age_minutes", 0.0)
        return record

    def _ready_at_iso(self, budget_minutes):
        try:
            base = datetime.fromisoformat(str(self._cur_datetime or ""))
        except ValueError:
            return ""
        return (base + timedelta(minutes=float(budget_minutes or 0.0))).isoformat()

    @property
    def credit(self):
        """信用账户 view (cash, available_cash, 维保比例, 保证金可用余额, 负债,
        利息, 额度, 利率). Deployable fields may reserve submitted pending orders;
        filled cash/debt remain broker truth."""
        return self.account.get("credit") or {}

    def debt_contracts(self, ts_code=None):
        """Open 融资/融券 负债合约 records (含未还金额/量、已计未付利息), optionally
        for one code. Empty with no open credit debt."""
        code = None if ts_code is None else str(ts_code)
        return [
            dict(record) for record in self._debt_contracts
            if code is None or str(record.get("ts_code")) == code
        ]

    def buy(self, ts_code, amount=None, limit=None, reason=None):
        """普通账户买入 (现金, long-only). ``amount`` is a share count."""
        self._require_substep("buy")
        return self._order("buy", ts_code, amount, limit, reason)

    def sell(self, ts_code, amount=None, limit=None, reason=None):
        """普通账户卖出 (T+1 可卖份额)."""
        self._require_substep("sell")
        return self._order("sell", ts_code, amount, limit, reason)

    def credit_buy(self, ts_code, amount=None, limit=None, reason=None):
        """信用账户担保品买入."""
        self._require_substep("credit_buy")
        return self._order("credit_buy", ts_code, amount, limit, reason)

    def credit_sell(self, ts_code, amount=None, limit=None, reason=None):
        """信用账户担保品卖出 (T+1 可卖份额; proceeds stay in the credit account)."""
        self._require_substep("credit_sell")
        return self._order("credit_sell", ts_code, amount, limit, reason)

    def fin_buy(self, ts_code, amount=None, limit=None, reason=None):
        """融资买入 (信用账户): opens a fin debt contract (notional+fee financed,
        daily interest); gated by 保证金可用余额, the margin_secs target set, and
        the fin quota."""
        self._require_substep("fin_buy")
        return self._order("fin_buy", ts_code, amount, limit, reason)

    def short(self, ts_code, amount, *, limit, reason=None):
        """融券卖出 (信用账户); ``limit=`` is required by the exchange rule."""
        self._require_substep("short")
        price = float(limit)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("short limit must be a finite positive price")
        return self._order("short", ts_code, amount, price, reason)

    def cover(self, ts_code, amount=None, limit=None, reason=None):
        """买券还券 (信用账户): reduces the short and repays 融券 contracts FIFO."""
        self._require_substep("cover")
        return self._order("cover", ts_code, amount, limit, reason)

    def sell_repay(self, ts_code, amount=None, limit=None, reason=None):
        """卖券还款 (信用账户): sells held shares and applies the net proceeds to
        融资 debt (interest first, oldest contract first); any surplus stays as
        credit-account cash."""
        self._require_substep("sell_repay")
        return self._order("sell_repay", ts_code, amount, limit, reason)

    def direct_repay(self, amount, reason=None, **kwargs):
        """直接还款 (信用账户): repays 融资 debt from credit-account cash (interest
        first, oldest contract first). ``amount`` is CNY and must not exceed
        deployable cash or outstanding financing debt; settles at its submission
        tick without bar matching."""
        self._require_substep("direct_repay")
        value = float(amount)
        if value <= 0:
            raise ValueError("direct_repay amount must be a positive CNY value")
        order_id = self._new_order_id()
        self._actions.append({
            "action": "direct_repay", "amount": value, "order_id": order_id,
            "account": "credit", "op_type": 32,
            "reason": reason, "submitted_at": self._cur_datetime,
            "submitted_time": self._cur_time, "_substep": self._cur_substep,
        })
        return order_id

    def transfer(self, amount, from_account, to_account, reason=None, **kwargs):
        """Request a same-day stock/credit cash transfer. Requests are accepted only
        before the 09:14 pre-open batch; the host confirms them at 09:14 using the
        same cash and withdraw-line checks as the Broker."""
        self._require_substep("transfer")
        src = self._account_name(from_account)
        dst = self._account_name(to_account)
        value = float(amount)
        if value <= 0:
            raise ValueError("transfer amount must be a positive CNY value")
        if src == dst:
            raise ValueError("transfer requires two different accounts")
        order_id = self._new_order_id()
        self._actions.append({
            "action": "transfer", "amount": value, "from_account": src, "to_account": dst,
            "order_id": order_id, "reason": reason, "submitted_at": self._cur_datetime,
            "submitted_time": self._cur_time, "_substep": self._cur_substep,
        })
        return order_id

    def close(self, ts_code, account=None, reason=None, **kwargs):
        """Market-exit the code's whole position. With both accounts holding the
        code (e.g. a stock-account long hedged by a credit-account short),
        ``account=`` is required — the driver rejects the ambiguity at call time
        rather than guessing which leg to flatten."""
        self._require_substep("close")
        code = str(ts_code)
        record = {
            "action": "close", "ts_code": code, "reason": reason,
            "submitted_at": self._cur_datetime, "submitted_time": self._cur_time,
            "_substep": self._cur_substep,
        }
        if account is not None:
            record["account"] = self._account_name(account)
        else:
            holders = [name for name in ("stock", "credit") if self._pos.get((name, code), 0) != 0]
            if len(holders) > 1:
                raise ValueError(
                    "close(%r) is ambiguous: both accounts hold the code; pass account='stock' or 'credit'" % code
                )
            if holders:
                record["account"] = holders[0]
        # No op_type hint for close: the host engine's _resolve_close is the
        # single authority for the cover/sell_repay/credit_sell/sell decision
        # (a driver-side copy of that rule would be a drift hazard).
        order_id = self._new_order_id()
        record["order_id"] = order_id
        self._actions.append(record)
        return order_id

    def cancel(self, order_id, reason=None, **kwargs):
        """Cancel a still-pending order returned by ``ctx.broker.pending()``.

        Mirrors the live ``cancel(order_id, ...)`` at the strategy layer. The host
        removes the order across the submit-lag / working-order queues; the
        cancelled id is also dropped from this tick's ``pending()`` view so a
        same-tick re-scan does not re-see it. Cross-minute substep actions are not
        broker orders until ready and therefore are not cancelable through pending().
        """
        oid = str(order_id or "").strip()
        if not oid:
            return False
        self._require_substep("cancel")
        self._cancelled_this_tick.add(oid)
        self._actions.append({
            "action": "cancel",
            "order_id": oid,
            "reason": reason or "agent_cancel",
            "_substep": self._cur_substep,
        })
        return True

    def _require_substep(self, op):
        if self._cur_substep is not None:
            return
        raise RuntimeError(
            "ctx.broker.%s() must be called inside ctx.substep(name, budget_minutes=B); "
            "wrap every broker action in a positive-budget substep so runtime and "
            "submission latency are accounted consistently" % op
        )


    def _new_order_id(self):
        self._client_seq += 1
        return "C%s_%03d" % (self._tick_key, self._client_seq)

    def _order(self, action, ts_code, amount, limit, reason):
        code = str(ts_code)
        order_id = self._new_order_id()
        record = {
            "action": action,
            "ts_code": code,
            "order_id": order_id,
            "submitted_at": self._cur_datetime,
            "submitted_time": self._cur_time,
        }
        account_op = _ACTION_ACCOUNT_OP.get(action)
        if account_op is not None:
            record["account"] = account_op[0]
            record["op_type"] = account_op[1]
        if amount is not None:
            record["amount"] = amount
        if limit is not None:
            record["limit"] = limit
        if reason is not None:
            record["reason"] = reason
        record["_substep"] = self._cur_substep
        self._actions.append(record)
        return order_id


class _Ctx(types.SimpleNamespace):
    """main(ctx) view. ``state_dir`` is available only inside ctx.substep(), where
    its first access resolves to a hidden staging directory seeded from visible state.
    Writing via ctx.state_dir therefore stages a block's output regardless of the
    write mechanism (json, parquet, native), and the host merges it into the
    visible directory only once the declared duration has elapsed. Broker-only
    blocks never pay for a directory copy."""

    @property
    def state_dir(self):
        holder = self._state_holder
        if not holder["active"]:
            raise RuntimeError(
                "ctx.state_dir is only available inside ctx.substep(name, budget_minutes=B); "
                "wrap state reads/writes in a positive-budget substep so visibility latency "
                "and wall time are accounted consistently"
            )
        return holder["active"]()


def _safe_name(text):
    return re.sub(r"[^0-9A-Za-z._-]", "_", str(text or "tick"))


def _chmod_dirs_for_host_cleanup(path, stop_at):
    """Make Docker-created staging dirs removable by the host-side test runner."""
    try:
        stop = os.path.abspath(stop_at)
        cur = os.path.abspath(path)
    except (TypeError, ValueError):
        return
    while cur.startswith(stop):
        try:
            os.chmod(cur, 0o777)
        except OSError:
            pass
        if cur == stop:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for root, dirs, _files in os.walk(path):
        try:
            os.chmod(root, 0o777)
        except OSError:
            pass
        for dirname in dirs:
            try:
                os.chmod(os.path.join(root, dirname), 0o777)
            except OSError:
                pass


class _LazyBars(dict):
    """dict-compatible view over columnar bar arrays.

    The host ships one tick's bars as ``{"ts_code": [...], "open": [...], ...}``
    (full-universe list-of-dicts JSON dominated the per-tick RPC cost); per-code
    dicts materialize only on access, so strategies that touch a handful of
    codes never pay for the whole universe. Subclassing ``dict`` keeps every
    dict idiom working (``dict(ctx.bars)``, ``json.dumps``, ``.items()``);
    materialized entries are stored in the underlying dict on first use.
    """

    def __init__(self, columns):
        codes = [str(code) for code in (columns.get("ts_code") or [])]
        super().__init__()
        self._codes = codes
        self._index = {code: i for i, code in enumerate(codes)}
        self._cols = {str(k): v for k, v in columns.items() if k != "ts_code"}

    def _materialize(self, key):
        i = self._index[key]
        bar = {"ts_code": key}
        for name, values in self._cols.items():
            bar[name] = values[i]
        super().__setitem__(key, bar)
        return bar

    def __missing__(self, key):
        key = str(key)
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return self._materialize(key)  # raises KeyError for unknown codes

    def get(self, key, default=None):
        try:
            return self[str(key)]
        except KeyError:
            return default

    def __contains__(self, key):
        return str(key) in self._index

    def __iter__(self):
        return iter(self._codes)

    def __len__(self):
        return len(self._codes)

    def _materialize_all(self):
        for code in self._codes:
            if not dict.__contains__(self, code):
                self._materialize(code)

    def keys(self):
        self._materialize_all()
        return dict.keys(self)

    def values(self):
        self._materialize_all()
        return dict.values(self)

    def items(self):
        self._materialize_all()
        return dict.items(self)

    def copy(self):
        self._materialize_all()
        return {code: dict.__getitem__(self, code) for code in self._codes}


def _build_ctx(state, snapshot_dir, model_dir, state_dir, staging_root):
    raw_bars = state.get("bars") or []
    if isinstance(raw_bars, dict):
        bars = _LazyBars(raw_bars)
    else:  # legacy list-of-dicts payload
        bars = {str(b.get("ts_code", "")): dict(b) for b in raw_bars}
    broker = _Broker(state)
    state_holder = {"active": None, "visible": state_dir}
    tick_key = _safe_name(state.get("cur_datetime") or state.get("cur_time") or "tick")

    def price(ts_code):
        found = bars.get(str(ts_code))
        return _bar_price(found) if found is not None else None

    def bar(ts_code):
        return bars.get(str(ts_code))

    @contextlib.contextmanager
    def substep(name, budget_minutes=None):
        # Declared compute duration (minutes) for a decision block. B is the block's
        # real-time ceiling (the host aborts the backtest if real wall-time exceeds B),
        # it is bounded by decision_max_sim_minutes, and it gates when in-block writes
        # to ctx.state_dir become visible (ready_at = tick + B). Broker actions issued
        # inside the block are also tagged for host-side submission timing: sub-minute
        # budgets finish inside the current decision minute, while B>=1 waits until
        # the first orderable tick at/after ready_at before normal execution lag.
        # A wrapped block MUST declare B > 0; wrapping with 0 is identical to not
        # wrapping, so it is rejected. Use a small positive B for light per-tick code.
        try:
            budget = float(budget_minutes) if budget_minutes is not None else 0.0
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            raise ValueError(
                "ctx.substep(name, budget_minutes=B) requires B > 0 minutes (the time this "
                "block may take, which is also its real-time ceiling); use a small value such "
                "as 0.5 for light per-tick work."
            )
        _cap = os.environ.get("AT_DECISION_MAX_SIM_MINUTES", "")
        if _cap:
            try:
                _cap_val = float(_cap)
            except ValueError:
                _cap_val = None
            if _cap_val is not None and budget > _cap_val:
                raise ValueError(
                    "ctx.substep budget_minutes=%.4g exceeds the decision_max_sim_minutes cap "
                    "(%.4g); split the work or declare a smaller budget" % (budget, _cap_val)
                )
        step_name = str(name)
        if step_name in broker._substep_names:
            raise ValueError(
                f"ctx.substep name {step_name!r} was already used in this tick; use a unique name "
                "for each decision block so its latency budget maps unambiguously to orders."
            )
        broker._substep_names.add(step_name)
        broker._substep_budgets[step_name] = budget
        prev = broker._cur_substep
        prev_active = state_holder["active"]
        prev_active_roots = tuple(_STATE_GUARD.get("active") or ())
        start = time.monotonic()
        staging_subdir = os.path.join(staging_root, tick_key, _safe_name(step_name))
        visible_dir = state_holder["visible"]
        state_seeded = {"value": False}
        state_seed_lock = threading.Lock()

        def resolve_state_dir():
            if not state_seeded["value"]:
                with state_seed_lock:
                    if not state_seeded["value"]:
                        # Seed only when the strategy actually asks for state. Reads
                        # see the old visible value; writes land in the block-local
                        # copy and merge at ready_at = tick + B.
                        with _internal_state_access():
                            os.makedirs(staging_subdir, exist_ok=True)
                            _chmod_dirs_for_host_cleanup(staging_subdir, staging_root)
                            if visible_dir and os.path.isdir(visible_dir):
                                shutil.copytree(visible_dir, staging_subdir, dirs_exist_ok=True)
                            _chmod_dirs_for_host_cleanup(staging_subdir, staging_root)
                        _STATE_GUARD["active"] = prev_active_roots + _path_aliases(staging_subdir)
                        state_seeded["value"] = True
            return staging_subdir

        state_holder["active"] = resolve_state_dir
        broker._cur_substep = step_name
        _RUNTIME["active_substeps"] += 1
        try:
            yield
        finally:
            try:
                if state_seeded["value"]:
                    # Stage only files the block created or changed vs the visible
                    # copy; unchanged seeded files are reads, not writes.
                    with _internal_state_access():
                        for _root, _dirs, _files in os.walk(staging_subdir):
                            for _fn in _files:
                                _abs = os.path.join(_root, _fn)
                                _state_rel = os.path.relpath(_abs, staging_subdir)
                                _visible = os.path.join(visible_dir, _state_rel) if visible_dir else ""
                                if _visible and os.path.exists(_visible) and filecmp.cmp(_abs, _visible, shallow=False):
                                    continue
                                broker._staged.append({
                                    "staging_rel": os.path.relpath(_abs, staging_root),
                                    "state_rel": _state_rel,
                                    "substep": step_name,
                                    "budget_minutes": budget,
                                })
                        _chmod_dirs_for_host_cleanup(staging_subdir, staging_root)
            finally:
                _RUNTIME["active_substeps"] -= 1
                broker._substeps.append({
                    "name": step_name,
                    "budget_minutes": budget,
                    "real_wall_s": time.monotonic() - start,
                    # A nested substep's wall-time is already inside its parent's, so the
                    # host excludes it from the coverage sum (still fail-fast-checked).
                    "nested": prev is not None,
                })
                _STATE_GUARD["active"] = prev_active_roots
                state_holder["active"] = prev_active
                broker._cur_substep = prev

    return _Ctx(
        broker=broker,
        account=broker.account,
        positions=broker.positions,
        bars=bars,
        bar=bar,
        substep=substep,
        price=price,
        cur_price=price,
        cur_date=str(state.get("cur_date", "") or ""),
        cur_time=str(state.get("cur_time", "") or ""),
        cur_datetime=str(state.get("cur_datetime", "") or ""),
        nl=_nl,
        snapshot_dir=snapshot_dir,
        asof_dir=(state.get("asof_dir") or snapshot_dir),
        asof_version=str(state.get("asof_version") or ""),
        model_dir=model_dir,
        _state_holder=state_holder,
    ), broker


def _load_module(path, name):
    if not path.exists():
        return None
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve():
    _install_path_guards()
    main_path = Path(sys.argv[1])
    snapshot_dir = os.environ.get("AT_SNAPSHOT_DIR", "/mnt/snapshot")
    model_dir = os.environ.get("AT_MODEL_DIR", "/mnt/agent/models")
    state_dir = os.environ.get("AT_STATE_DIR", "/mnt/agent/workspace/.state")
    staging_root = os.environ.get("AT_STATE_STAGING_DIR", "/mnt/agent/workspace/.state_staging")
    os.environ.pop("AT_STATE_DIR", None)
    os.environ.pop("AT_STATE_STAGING_DIR", None)
    main_module = None
    main_load_error = None
    import_start = time.monotonic()
    with contextlib.redirect_stdout(sys.stderr):
        try:
            main_module = _load_module(main_path, "agent_strategy_main")
        except Exception as exc:
            main_load_error = _sanitize_error("%s: %s" % (type(exc).__name__, exc))
    import_wall_s = time.monotonic() - import_start
    try:
        import_cap = float(os.environ.get("AT_IMPORT_MAX_WALL_SECONDS", "30"))
    except ValueError:
        import_cap = 30.0
    if main_load_error is None and import_wall_s > import_cap:
        main_load_error = (
            "strategy import exceeded its %.0fs wall-clock cap (%.1fs); "
            "keep module top level to imports/constants and do strategy work inside ctx.substep"
        ) % (import_cap, import_wall_s)

    main_fn = getattr(main_module, "main", None) if main_module is not None else None

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        request_id = str(request.get("request_id", ""))
        try:
            if main_load_error is not None:
                raise RuntimeError("main.py failed to import: " + main_load_error)
            if request.get("op") == "validate":
                if not callable(main_fn):
                    raise AttributeError("main.py must define main(ctx)")
                response = {"request_id": request_id, "status": "ok", "import_wall_s": import_wall_s}
            else:
                if not callable(main_fn):
                    raise AttributeError("main.py must define main(ctx)")
                ctx, broker = _build_ctx(request.get("state") or {}, snapshot_dir, model_dir, state_dir, staging_root)
                main_start = time.monotonic()
                with contextlib.redirect_stdout(sys.stderr):
                    main_fn(ctx)
                main_wall_s = time.monotonic() - main_start
                response = {
                    "request_id": request_id,
                    "status": "ok",
                    "actions": broker._actions,
                    "substeps": broker._substeps,
                    "staged": broker._staged,
                    "main_wall_s": main_wall_s,
                    # Informational only: lets the Agent spot accidental broad
                    # reads without the Environment rejecting its strategy.
                    "agent_peak_rss_bytes": _peak_rss_bytes(),
                }
        except Exception as exc:
            response = {
                "request_id": request_id,
                "status": "error",
                "error": _sanitize_error("%s: %s" % (type(exc).__name__, exc)),
                **_public_strategy_error(
                    exc,
                    main_path=main_path,
                    request=request,
                    snapshot_dir=snapshot_dir,
                ),
            }
        print(
            json.dumps(response, ensure_ascii=False, default=str, separators=(",", ":")),
            file=_PROTOCOL_STDOUT,
            flush=True,
        )


if __name__ == "__main__":
    _serve()
