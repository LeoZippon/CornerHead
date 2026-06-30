"""De-stringed persistent ``main(ctx)`` sandbox driver (R16; docs/environment_design.md).

Loaded by file inside the Agent sandbox (``python main_ctx_driver.py <main.py> <marker>``)
and shipped into the image alongside ``broker_core``. It serves one per-tick RPC over
stdin/stdout: build a ``ctx``, call the Agent's ``main(ctx)``, and return the orders,
declared sub-steps, and staged state writes. The intra-tick broker view projects fills
with the SAME ``broker_core`` math the host SimBroker uses, so the agent-visible cash /
buying power / position after an order match the broker's real fill.

This module imports only stdlib + the sibling ``broker_core`` (no ``autotrade`` or
pandas import), so it runs unchanged in the dependency-light sandbox image.
"""

import builtins, contextlib, filecmp, importlib.util, json, os, re, shutil, sys, time, types, uuid
from pathlib import Path

import broker_core

_PROTOCOL_STDOUT = sys.stdout

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
_DISABLE_LINKS = os.environ.get("AT_DISABLE_LINKS", "") == "1"


def _is_under(path, root):
    return path == root or root in path.parents


def _guard_path(value, *, write=False):
    aliases = _path_aliases(value)
    if not aliases:
        return
    roots = _FORBIDDEN_PATHS + (_WRITE_FORBIDDEN_PATHS if write else ())
    for path in aliases:
        for forbidden in roots:
            if _is_under(path, forbidden):
                action = "write" if write else "access"
                raise PermissionError(f"formal strategy cannot {action} forbidden path: {value}")


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


os.remove = _guarded_os_unlink


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


builtins.open = _guarded_open
Path.open = _guarded_path_open
os.open = _guarded_os_open
os.mkdir = _guarded_os_mkdir
os.makedirs = _guarded_os_makedirs
Path.mkdir = _guarded_path_mkdir
os.unlink = _guarded_os_unlink
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


def _cost_model(state):
    """Rebuild the host CostModel from the per-tick state so the sandbox projection
    uses identical commission/duty/slippage/short-margin economics."""
    params = state.get("cost_model") or {}
    return broker_core.CostModel(**params) if params else broker_core.CostModel()


class _Broker:
    """Market-wide, ts_code-keyed view of the host Broker primitives.

    Calls are recorded as deferred actions applied by the trusted host Broker with full
    market constraints. The intra-tick view of cash, buying power, and per-code position
    is projected with the SAME shared ``broker_core`` math the host SimBroker fills with
    (commission, slippage, lot sizing, short margin / locked proceeds), so several orders
    in one tick stay self-consistent with the real fills: a second buy is sized against
    buying power already reduced by the first buy's cost, and an order the broker would
    reject for insufficient funds leaves the optimistic view unchanged."""

    def __init__(self, state, prices, cost):
        account = state.get("account") or {}
        self.account = account
        self.positions = state.get("positions") or []
        self._cost = cost
        self._trade_date = str(state.get("cur_date", "") or "")
        self._cash = float(state.get("cash", account.get("cash", 0.0)) or 0.0)
        self._available_cash = float(account.get("available_cash", self._cash) or 0.0)
        self._initial_equity = float(state.get("initial_equity", 0.0) or 0.0)
        self._prices = prices
        self._working = state.get("pending") or {}
        self._pos = {}
        self._sellable = {}
        self._short = {}
        for item in self.positions:
            code = str(item.get("ts_code"))
            qty = int(item.get("quantity", 0) or 0)
            side = str(item.get("side", "long"))
            self._pos[code] = qty if side == "long" else -qty
            self._sellable[code] = int(item.get("sellable_quantity", qty) or 0)
            if side == "short":
                self._short[code] = {
                    "qty": qty,
                    "entry_cost": float(item.get("entry_cost", 0.0) or 0.0),
                    "entry_price": float(item.get("entry_price", 0.0) or 0.0),
                }
        self._actions = []
        self._substeps = []      # [{name, budget_minutes, real_wall_s}] declared this tick
        self._substep_names = set()
        self._cur_substep = None  # name of the open ctx.substep, tagged onto each order
        self._staged = []        # [{staging_rel, state_rel, substep, budget_minutes}] this tick

    @property
    def cash(self):
        return self._cash

    @property
    def money(self):
        return self._cash

    @property
    def available_cash(self):
        """Deployable buying power (cash minus short margin and locked proceeds),
        projected intra-tick so successive orders size against the real headroom."""
        return self._available_cash

    def position(self, ts_code):
        return self._pos.get(str(ts_code), 0)

    def pending(self, ts_code):
        """Still-working orders for ``ts_code`` — those queued on earlier ticks and
        not yet filled, plus any submitted this tick. Mirrors the live order query
        so re-entry/exit logic can skip codes with an order already in flight."""
        code = str(ts_code)
        working = list(self._working.get(code, []))
        working.extend(action for action in self._actions if str(action.get("ts_code")) == code)
        return working

    def buy(self, ts_code, amount=None, weight=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("buy", ts_code, amount, weight, limit, valid_bars, reason)

    def sell(self, ts_code, amount=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("sell", ts_code, amount, None, limit, valid_bars, reason)

    def short(self, ts_code, amount=None, weight=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("short", ts_code, amount, weight, limit, valid_bars, reason)

    def cover(self, ts_code, amount=None, limit=None, valid_bars=None, reason=None, **kwargs):
        self._order("cover", ts_code, amount, None, limit, valid_bars, reason)

    def close(self, ts_code, reason=None, **kwargs):
        code = str(ts_code)
        self._actions.append({"action": "close", "ts_code": code, "reason": reason, "_substep": self._cur_substep})
        held = self._pos.get(code, 0)
        price = self._prices.get(code)
        if held == 0 or price is None or price <= 0:
            return
        self._project_reduce("close", code, price, abs(held))

    def _order(self, action, ts_code, amount, weight, limit, valid_bars, reason):
        code = str(ts_code)
        record = {"action": action, "ts_code": code}
        if amount is not None:
            record["amount"] = amount
        if weight is not None:
            record["weight"] = weight
        if limit is not None:
            record["limit"] = limit
        if valid_bars is not None:
            record["valid_bars"] = valid_bars
        if reason is not None:
            record["reason"] = reason
        record["_substep"] = self._cur_substep
        self._actions.append(record)
        if limit is not None:
            return  # a resting limit order may not fill; leave the optimistic view unchanged
        price = self._prices.get(code)
        if price is None or price <= 0:
            return
        shares = broker_core.resolve_shares(amount, weight, price, self._initial_equity)
        if shares <= 0:
            return
        if action in ("buy", "short"):
            self._project_open(action, code, price, shares)
        else:
            self._project_reduce(action, code, price, shares)

    def _project_open(self, action, code, price, shares):
        side = "long" if action == "buy" else "short"
        held = self._pos.get(code, 0)
        if (side == "long" and held < 0) or (side == "short" and held > 0):
            return  # the broker rejects opening opposite to the held side
        fill = broker_core.project_open(
            self._cost, side=side, raw_price=price, shares=shares, trade_date=self._trade_date
        )
        if fill.required_cash > self._available_cash + 1e-6:
            return  # the broker would reject for insufficient cash/margin; view unchanged
        self._cash += fill.cash_delta
        # Buying power gates on required_cash (margin+fee+duty for a short) but a short
        # only LOCKS margin: its banked net proceeds offset the fee/duty, so available
        # cash drops by margin, not required_cash — matching SimBroker.available_cash().
        self._available_cash -= fill.margin if side == "short" else fill.required_cash
        self._pos[code] = held + (shares if side == "long" else -shares)
        if side == "short":
            st = self._short.setdefault(code, {"qty": 0, "entry_cost": 0.0, "entry_price": fill.price})
            total = st["qty"] + shares
            st["entry_price"] = (st["entry_price"] * st["qty"] + fill.price * shares) / total if total else fill.price
            st["qty"] = total
            st["entry_cost"] += fill.cost_basis

    def _project_reduce(self, action, code, price, shares):
        held = self._pos.get(code, 0)
        if held == 0 or shares <= 0:
            return
        if held > 0:
            if action == "cover":
                return  # the broker rejects cover on a long-held code (side mismatch)
            # A code absent from the snapshot was opened earlier THIS tick, so it is
            # T+1 locked (0 sellable) — default to 0, not the held count.
            sellable = self._sellable.get(code, 0)
            shares = min(shares, sellable, held)
            if shares <= 0:
                return  # T+1: shares acquired today are not yet sellable
            fill = broker_core.project_reduce(
                self._cost, side="long", raw_price=price, shares=shares, trade_date=self._trade_date
            )
            self._cash += fill.cash_delta
            self._available_cash += fill.cash_delta  # selling a long frees its cash
            self._pos[code] = held - shares
            self._sellable[code] = sellable - shares  # consume the sellable balance this tick
        else:
            if action == "sell":
                return  # the broker rejects sell on a short-held code (side mismatch)
            shares = min(shares, -held)
            fill = broker_core.project_reduce(
                self._cost, side="short", raw_price=price, shares=shares, trade_date=self._trade_date
            )
            st = self._short.get(code) or {"qty": -held, "entry_cost": 0.0, "entry_price": price}
            qty = st["qty"] or -held
            released_proceeds = st["entry_cost"] * shares / qty if qty else 0.0
            released_margin = st["entry_price"] * shares * self._cost.short_margin_ratio
            self._cash += fill.cash_delta
            # covering releases the locked proceeds + margin and pays the buyback.
            self._available_cash += released_margin + released_proceeds + fill.cash_delta
            self._pos[code] = held + shares
            st["qty"] = qty - shares
            st["entry_cost"] -= released_proceeds
            self._short[code] = st


class _Ctx(types.SimpleNamespace):
    """main(ctx) view. ``state_dir`` is a property so that, inside ctx.substep(),
    it resolves to a hidden staging directory; outside it resolves to the managed,
    visible state directory. Writing via ctx.state_dir therefore stages a heavy
    block's output regardless of the write mechanism (json, parquet, native), and
    the host merges it into the visible directory only once the block's declared
    duration has elapsed (ready_at = tick + B)."""

    @property
    def state_dir(self):
        holder = self._state_holder
        return holder["active"] or holder["visible"]


def _safe_name(text):
    return re.sub(r"[^0-9A-Za-z._-]", "_", str(text or "tick"))


def _build_ctx(state, snapshot_dir, model_dir, state_dir, staging_root):
    bars = {str(b.get("ts_code", "")): dict(b) for b in (state.get("bars") or [])}
    prices = {code: _bar_price(bar) for code, bar in bars.items()}
    broker = _Broker(state, prices, _cost_model(state))
    state_holder = {"active": None, "visible": state_dir}
    tick_key = _safe_name(state.get("cur_datetime") or state.get("cur_time") or "tick")

    def price(ts_code):
        return prices.get(str(ts_code))

    def bar(ts_code):
        return bars.get(str(ts_code))

    @contextlib.contextmanager
    def substep(name, budget_minutes=None):
        # Declared compute duration (minutes) for a heavy block. B is the block's
        # real-time ceiling (the host aborts the backtest if real wall-time exceeds B),
        # it is bounded by decision_max_sim_minutes, and it gates when in-block writes
        # to ctx.state_dir become visible (ready_at = tick + B). It does NOT move the
        # order fill bar: orders fill at the normal decision-bar lag regardless of B.
        # A wrapped block MUST declare B > 0; wrapping with 0 is identical to not
        # wrapping, so it is rejected. Leave trivial per-tick code unwrapped.
        try:
            budget = float(budget_minutes) if budget_minutes is not None else 0.0
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            raise ValueError(
                "ctx.substep(name, budget_minutes=B) requires B > 0 minutes (the time this "
                "block may take, which is also its real-time ceiling); use a small value such "
                "as 0.5 for light work. Leave trivial per-tick code unwrapped for the default lag."
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
        prev = broker._cur_substep
        prev_active = state_holder["active"]
        # ctx.state_dir resolves to this staging dir inside the block. Seed it with a
        # copy of the current visible state so reads see the old visible value (the
        # contract: reads always see the visible directory); writes land here and the
        # host merges them into the visible state dir once ready_at = this tick + B.
        staging_subdir = os.path.join(staging_root, tick_key, _safe_name(step_name))
        os.makedirs(staging_subdir, exist_ok=True)
        visible_dir = state_holder["visible"]
        if visible_dir and os.path.isdir(visible_dir):
            shutil.copytree(visible_dir, staging_subdir, dirs_exist_ok=True)
        state_holder["active"] = staging_subdir
        broker._cur_substep = step_name
        start = time.monotonic()
        try:
            yield
        finally:
            broker._substeps.append({
                "name": step_name,
                "budget_minutes": budget,
                "real_wall_s": time.monotonic() - start,
            })
            # Stage only files the block created or changed vs the visible copy; the
            # unchanged seeded copies are not writes and must not re-merge.
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
        params=dict(state.get("params") or {}),
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
    main_path = Path(sys.argv[1])
    snapshot_dir = os.environ.get("AT_SNAPSHOT_DIR", "/mnt/snapshot")
    model_dir = os.environ.get("AT_MODEL_DIR", "/mnt/agent/models")
    state_dir = os.environ.get("AT_STATE_DIR", "/mnt/agent/workspace/.state")
    staging_root = os.environ.get("AT_STATE_STAGING_DIR", "/mnt/agent/workspace/.state_staging")
    main_module = None
    main_load_error = None
    with contextlib.redirect_stdout(sys.stderr):
        try:
            main_module = _load_module(main_path, "agent_strategy_main")
        except Exception as exc:
            main_load_error = _sanitize_error("%s: %s" % (type(exc).__name__, exc))

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
                response = {"request_id": request_id, "status": "ok"}
            else:
                if not callable(main_fn):
                    raise AttributeError("main.py must define main(ctx)")
                ctx, broker = _build_ctx(request.get("state") or {}, snapshot_dir, model_dir, state_dir, staging_root)
                with contextlib.redirect_stdout(sys.stderr):
                    main_fn(ctx)
                response = {
                    "request_id": request_id,
                    "status": "ok",
                    "actions": broker._actions,
                    "substeps": broker._substeps,
                    "staged": broker._staged,
                }
        except Exception as exc:
            response = {"request_id": request_id, "status": "error", "error": _sanitize_error("%s: %s" % (type(exc).__name__, exc))}
        print(json.dumps(response, ensure_ascii=False, default=str), file=_PROTOCOL_STDOUT, flush=True)


if __name__ == "__main__":
    _serve()
