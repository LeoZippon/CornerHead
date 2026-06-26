"""backtest_tool internals: strategy execution, intent validation, replay, stats.

The orchestration order lives in tools/backtest.py. Formal backtests execute the
root ``output/main.py`` contract to obtain candidate-to-strategy mappings,
then replay them minute-by-minute. Each mapped stock's ``trade_strategy`` is an
Agent-defined function that receives a per-bar ``ctx`` and drives the Broker's
fundamental primitives (``ctx.broker.buy/sell/short/cover/close``). The Broker
owns no strategy logic; it only enforces market rules and records fills.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from autotrade.environment.broker import BrokerProfile, MarketData, SimBroker
from autotrade.environment.executor import ExecResult
from autotrade.environment.runtime import sanitize_for_log

CANDIDATE_COLUMNS = ("ts_code", "reason", "source_artifacts")
TRADE_INTENT_REQUIRED_COLUMNS = ("ts_code", "trade_strategy")
INTENT_RESERVED_COLUMNS = (
    "ts_code",
    "code",
    "trade_strategy",
    "strategy",
    "params",
    "start_date",
    "end_date",
    "reason",
    "source_artifacts",
)
TRADING_DAYS_PER_YEAR = 252

_STRATEGY_PATH_GUARD = """\
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

"""

_STRATEGY_DRIVER = """\
import builtins, importlib.util, json, os, sys, time, types, uuid
from pathlib import Path

import pandas as pd

""" + _STRATEGY_PATH_GUARD + """\
def _frame_payload(value, *, name):
    if value is None:
        return None
    if isinstance(value, pd.DataFrame):
        frame = value
    else:
        frame = pd.DataFrame(value)
    return {"columns": list(frame.columns), "rows": frame.to_dict("records")}


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\\n")


def _read_responses(path):
    responses = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                responses[str(record.get("request_id"))] = record
    except FileNotFoundError:
        return responses
    return responses


def _nl(ts_code="", prompt="", *, timeout_seconds=None, content_only=False, **kwargs):
    request_path = os.environ.get("AT_NL_REQUESTS_PATH", "")
    response_path = os.environ.get("AT_NL_RESPONSES_PATH", "")
    if not request_path or not response_path:
        raise RuntimeError("nl tool is not configured for this backtest")
    request_id = uuid.uuid4().hex
    request = {
        "request_id": request_id,
        "ts_code": str(ts_code),
        "prompt": str(prompt or ""),
        "kwargs": kwargs,
    }
    _append_jsonl(request_path, request)
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
    raise TimeoutError(f"nl tool timed out after {timeout}s for {ts_code}")


tools_module = types.ModuleType("at_tools")
tools_module.nl = _nl
sys.modules["at_tools"] = tools_module


main_path = Path(sys.argv[1])
sys.path.insert(0, str(main_path.parent))
spec = importlib.util.spec_from_file_location("agent_strategy_main", main_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

context = {
    "snapshot_dir": os.environ.get("AT_SNAPSHOT_DIR", "/mnt/snapshot"),
    "agent_output_dir": os.environ.get("AT_AGENT_OUTPUT_DIR", "/mnt/agent/output"),
    "model_dir": os.environ.get("AT_MODEL_DIR", "/mnt/agent/models"),
    "decision_time": os.environ.get("AT_DECISION_TIME", ""),
    "replay_granularity": os.environ.get("AT_REPLAY_GRANULARITY", "minute"),
    "nl": _nl,
}

if not hasattr(module, "run_strategy"):
    raise AttributeError("main.py must define run_strategy(context)")
raw = module.run_strategy(context)

if isinstance(raw, pd.DataFrame):
    raw = {"trade_intents": raw}
elif isinstance(raw, list):
    raw = {"trade_intents": raw}
if not isinstance(raw, dict):
    raise TypeError(f"strategy entrypoint must return a dict, list, or pandas.DataFrame, got {type(raw)!r}")

metadata = raw.get("metadata") or {}
if not isinstance(metadata, dict):
    raise TypeError("strategy metadata must be an object when provided")
payload = {
    "candidates": _frame_payload(raw.get("candidates", raw.get("candidate_pool")), name="candidates"),
    "trade_intents": _frame_payload(
        raw.get("trade_intents", raw.get("trades", raw.get("orders", raw.get("strategies")))),
        name="trade_intents",
    ),
    "metadata": metadata,
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, default=str)
"""

_STRATEGY_POLICY_DRIVER = """\
import builtins, contextlib, importlib.util, json, os, re, sys, types
from pathlib import Path

_PROTOCOL_STDOUT = sys.stdout

""" + _STRATEGY_PATH_GUARD + """\
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"Bearer\\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (
        re.compile(r"(authorization\\s*[:=]\\s*)(?:Bearer\\s+)?[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        r"\\1[REDACTED]",
    ),
)


def _sanitize_error(value):
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _nl(*_args, **_kwargs):
    raise RuntimeError("nl tool is only available during the decision stage; pass decision outputs through params")


tools_module = types.ModuleType("at_tools")
tools_module.nl = _nl
sys.modules["at_tools"] = tools_module


class _Trade:
    \"\"\"Attribute- and dict-accessible record of one executed trade.\"\"\"

    def __init__(self, record):
        object.__setattr__(self, "_record", dict(record))

    def __getattr__(self, name):
        try:
            return self._record[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key):
        return self._record[key]

    def get(self, key, default=None):
        return self._record.get(key, default)

    def __repr__(self):
        return "Trade(%r)" % (self._record,)


class _BrokerProxy:
    \"\"\"Sandbox-facing Broker primitives. Calls are recorded as deferred actions
    applied by the trusted host Broker with full market constraints. An
    optimistic intra-bar view of cash/position keeps multiple actions in one bar
    self-consistent.\"\"\"

    def __init__(self, state):
        account = state.get("account") or {}
        self.account = account
        self.positions = state.get("positions") or []
        self._actions = []
        self._cash = float(state.get("money", account.get("cash", 0.0)) or 0.0)
        self._position = int(state.get("position", 0) or 0)
        self._initial_equity = float(state.get("initial_equity", 0.0) or 0.0)
        price = state.get("cur_price")
        self._price = float(price) if price not in (None, "") else None

    @property
    def money(self):
        return self._cash

    @property
    def cash(self):
        return self._cash

    @property
    def position(self):
        return self._position

    def buy(self, amount=None, weight=None, reason=None, **kwargs):
        self._record("buy", amount, weight, reason, sign=+1)

    def sell(self, amount=None, reason=None, **kwargs):
        self._record("sell", amount, None, reason, sign=-1)

    def short(self, amount=None, weight=None, reason=None, **kwargs):
        self._record("short", amount, weight, reason, sign=-1)

    def cover(self, amount=None, reason=None, **kwargs):
        self._record("cover", amount, None, reason, sign=+1)

    def close(self, reason=None, **kwargs):
        self._actions.append({"action": "close", "reason": reason})
        if self._price is not None:
            self._cash += abs(self._position) * self._price if self._position > 0 else -abs(self._position) * self._price
        self._position = 0

    def _record(self, action, amount, weight, reason, *, sign):
        record = {"action": action}
        if amount is not None:
            record["amount"] = amount
        if weight is not None:
            record["weight"] = weight
        if reason is not None:
            record["reason"] = reason
        self._actions.append(record)
        shares = self._resolve_shares(amount, weight)
        if shares > 0 and self._price is not None:
            self._position += sign * shares
            self._cash -= sign * shares * self._price

    def _resolve_shares(self, amount, weight):
        if self._price is None or self._price <= 0:
            return 0
        try:
            if amount is not None and str(amount).strip() != "":
                raw = int(float(amount))
            elif weight is not None and str(weight).strip() != "":
                raw = int(abs(float(weight)) * self._initial_equity / self._price)
            else:
                return 0
        except (TypeError, ValueError):
            return 0
        return (raw // 100) * 100


class _StockProxy:
    def __init__(self, state, broker_proxy):
        code = state.get("code") or state.get("ts_code") or ""
        self.code = code
        self.ts_code = code
        price = state.get("cur_price")
        self.price = float(price) if price not in (None, "") else None
        self.trades = [_Trade(item) for item in (state.get("trades") or [])]
        self._broker = broker_proxy

    @property
    def position(self):
        return self._broker.position


def _build_ctx(state):
    broker = _BrokerProxy(state)
    stock = _StockProxy(state, broker)
    price = state.get("cur_price")
    return types.SimpleNamespace(
        broker=broker,
        stock=stock,
        cur_price=float(price) if price not in (None, "") else None,
        cur_time=str(state.get("cur_time", "") or ""),
        cur_date=str(state.get("cur_date", "") or ""),
        bar=state.get("bar") or {},
        params=dict(state.get("params") or {}),
        reason=str(state.get("reason", "") or ""),
        account=broker.account,
        positions=broker.positions,
    ), broker


def _load_module(path, name):
    if not path.exists():
        return None
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main_path = Path(sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    trading_module = _load_module(main_path.parent / "trading.py", "agent_strategy_trading")
    main_module = _load_module(main_path, "agent_strategy_main_for_policy")
modules = [module for module in (trading_module, main_module) if module is not None]


def _resolve(name):
    for module in modules:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    request_id = str(request.get("request_id", ""))
    try:
        if request.get("op") == "validate":
            missing = [name for name in request.get("strategies", []) if _resolve(str(name)) is None]
            if missing:
                response = {"request_id": request_id, "status": "error", "error": f"missing strategy functions: {missing}"}
            else:
                response = {"request_id": request_id, "status": "ok"}
        else:
            strategy = str(request.get("strategy", ""))
            fn = _resolve(strategy)
            if fn is None:
                raise AttributeError(f"trade_strategy function not found: {strategy}")
            ctx, broker = _build_ctx(request.get("state") or {})
            with contextlib.redirect_stdout(sys.stderr):
                fn(ctx)
            response = {"request_id": request_id, "status": "ok", "actions": broker._actions}
    except Exception as exc:
        error = _sanitize_error(f"{type(exc).__name__}: {exc}")
        response = {"request_id": request_id, "status": "error", "error": error}
    print(json.dumps(response, ensure_ascii=False, default=str), file=_PROTOCOL_STDOUT, flush=True)
"""


class BacktestError(RuntimeError):
    """A formal backtest step failed; the error is explicit, never silent."""


@dataclass(frozen=True)
class StrategyProgramResult:
    """Normalized output from ``output/main.py``."""

    candidates: pd.DataFrame
    trade_intents: pd.DataFrame
    metadata: dict[str, object]


def run_strategy_program(
    executor,
    paths,
    *,
    timeout_seconds: float = 300.0,
    decision_time: str = "",
    replay_granularity: str = "minute",
    nl_service=None,
) -> StrategyProgramResult:
    """Call the formal strategy program through the sandbox executor.

    Formal artifacts expose ``run_strategy(context)`` from the root
    ``output/main.py``. The optional ``at_tools.nl`` helper is
    served by the host Environment over a JSONL request/response file pair, so
    API keys and provider clients never enter the sandbox.
    """
    out_host = paths.workspace / f".strategy_{uuid.uuid4().hex[:10]}.json"
    requests_host = paths.workspace / f".nl_requests_{uuid.uuid4().hex[:10]}.jsonl"
    responses_host = paths.workspace / f".nl_responses_{uuid.uuid4().hex[:10]}.jsonl"
    requests_host.write_text("", encoding="utf-8")
    responses_host.write_text("", encoding="utf-8")
    try:
        main_py = paths.agent_output / "main.py"
        env = {
            "AT_SNAPSHOT_DIR": executor.map_path(paths.snapshot),
            "AT_AGENT_OUTPUT_DIR": executor.map_path(paths.agent_output),
            "AT_MODEL_DIR": executor.map_path(paths.model_artifacts),
            "AT_DECISION_TIME": decision_time,
            "AT_REPLAY_GRANULARITY": replay_granularity,
            "AT_NL_REQUESTS_PATH": executor.map_path(requests_host),
            "AT_NL_RESPONSES_PATH": executor.map_path(responses_host),
            "AT_NL_TOOL_TIMEOUT_SECONDS": str(timeout_seconds),
            "AT_FORBIDDEN_PATHS": _executor_pathsep_join(
                executor,
                [paths.train, paths.valid, paths.test, paths.artifacts],
            ),
        }
        with hide_snapshot_slots_from_agent(paths):
            if hasattr(executor, "popen"):
                result = _run_strategy_with_rpc(
                    executor,
                    [executor.python, "-c", _STRATEGY_DRIVER, executor.map_path(main_py), executor.map_path(out_host)],
                    env=env,
                    cwd=paths.agent,
                    timeout_seconds=timeout_seconds,
                    nl_service=nl_service,
                    requests_path=requests_host,
                    responses_path=responses_host,
                )
            else:
                result = executor.run(
                    [executor.python, "-c", _STRATEGY_DRIVER, executor.map_path(main_py), executor.map_path(out_host)],
                    env=env,
                    cwd=paths.agent,
                    timeout_seconds=timeout_seconds,
                    user="agent",
                )
        if result.exit_code == 124:
            raise BacktestError(f"strategy program timed out after {timeout_seconds}s")
        if result.exit_code != 0:
            safe_stderr = str(sanitize_for_log(result.stderr.strip()))[-2000:]
            raise BacktestError(f"strategy program failed: {safe_stderr}")
        payload = json.loads(out_host.read_text(encoding="utf-8"))
    finally:
        out_host.unlink(missing_ok=True)
        requests_host.unlink(missing_ok=True)
        responses_host.unlink(missing_ok=True)

    candidates_payload = payload.get("candidates")
    if candidates_payload is None:
        candidates = pd.DataFrame(columns=list(CANDIDATE_COLUMNS))
    else:
        candidates = _payload_to_frame(candidates_payload)
    intents_payload = payload.get("trade_intents")
    trade_intents = pd.DataFrame() if intents_payload is None else _payload_to_frame(intents_payload)
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise BacktestError("strategy metadata must be an object")
    return StrategyProgramResult(
        candidates=candidates,
        trade_intents=trade_intents,
        metadata=metadata,
    )


class StrategyPolicyRunner:
    """Sandbox RPC runner for Agent-defined ``ctx`` trade strategy functions."""

    def __init__(
        self,
        executor,
        paths,
        *,
        timeout_seconds: float,
        decision_time: str,
        replay_granularity: str,
    ) -> None:
        self.executor = executor
        self.paths = paths
        self.timeout_seconds = timeout_seconds
        self.decision_time = decision_time
        self.replay_granularity = replay_granularity
        self.proc = None
        self._hide_cm = None
        self._hide_entered = False

    def __enter__(self) -> "StrategyPolicyRunner":
        try:
            main_py = self.paths.agent_output / "main.py"
            env = {
                "AT_SNAPSHOT_DIR": self.executor.map_path(self.paths.snapshot),
                "AT_AGENT_OUTPUT_DIR": self.executor.map_path(self.paths.agent_output),
                "AT_DECISION_TIME": self.decision_time,
                "AT_REPLAY_GRANULARITY": self.replay_granularity,
                "AT_FORBIDDEN_PATHS": _executor_pathsep_join(
                    self.executor,
                    [
                        self.paths.train,
                        self.paths.valid,
                        self.paths.test,
                        self.paths.artifacts,
                        self.paths.workspace,
                        self.paths.model_artifacts,
                    ],
                ),
                "AT_WRITE_FORBIDDEN_PATHS": self.executor.map_path(self.paths.agent_output),
                "AT_DISABLE_LINKS": "1",
            }
            self._hide_cm = hide_snapshot_slots_from_agent(self.paths)
            self._hide_cm.__enter__()
            self._hide_entered = True
            self.proc = self.executor.popen(
                [self.executor.python, "-c", _STRATEGY_POLICY_DRIVER, self.executor.map_path(main_py)],
                env=env,
                cwd=self.paths.agent,
                user="agent",
            )
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        finally:
            if self._hide_cm is not None and self._hide_entered:
                self._hide_cm.__exit__(exc_type, exc, tb)
                self._hide_entered = False

    def validate_functions(self, strategies: list[str]) -> None:
        if not strategies:
            return
        self._request({"op": "validate", "strategies": sorted(set(strategies))})

    def actions(self, *, strategy: str, state: dict[str, object]) -> list[dict[str, object]]:
        response = self._request({"op": "call", "strategy": strategy, "state": state})
        actions = response.get("actions") or []
        if not isinstance(actions, list):
            raise BacktestError(f"strategy {strategy} returned non-list actions")
        return [dict(action) for action in actions if isinstance(action, dict)]

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except Exception:  # noqa: BLE001 - best effort cleanup
                proc.kill()
                proc.communicate()
        self.proc = None

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise BacktestError("strategy policy runner is not running")
        request_id = uuid.uuid4().hex
        record = {"request_id": request_id, **payload}
        try:
            proc.stdin.write(json.dumps(_jsonable(record), ensure_ascii=False, default=str) + "\n")
            proc.stdin.flush()
        except BrokenPipeError as exc:
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            safe_stderr = str(sanitize_for_log(stderr))[-2000:]
            raise BacktestError(f"strategy policy runner exited early: {safe_stderr}") from exc
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                safe_stderr = str(sanitize_for_log(stderr))[-2000:]
                raise BacktestError(f"strategy policy runner failed: {safe_stderr}")
            ready, _, _ = select.select([proc.stdout], [], [], 0.05)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(response.get("request_id", "")) != request_id:
                continue
            if response.get("status") != "ok":
                error = sanitize_for_log(str(response.get("error", "strategy failed")))
                raise BacktestError(str(error))
            return response
        proc.kill()
        raise BacktestError(f"strategy policy runner timed out after {self.timeout_seconds}s")


def _run_strategy_with_rpc(
    executor,
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_seconds: float,
    nl_service,
    requests_path: Path,
    responses_path: Path,
) -> ExecResult:
    proc = executor.popen(argv, env=env, cwd=cwd, user="agent")
    served: set[str] = set()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while proc.poll() is None:
        _serve_nl_requests(requests_path, responses_path, served, nl_service)
        if time.monotonic() >= deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(0.05)
    _serve_nl_requests(requests_path, responses_path, served, nl_service)
    try:
        stdout, stderr = proc.communicate(timeout=2)
    except Exception:  # noqa: BLE001 - force cleanup and keep the strategy error visible
        proc.kill()
        stdout, stderr = proc.communicate()
    if timed_out:
        stderr = f"{stderr}\ntimeout after {timeout_seconds}s"
        return ExecResult(exit_code=124, stdout=stdout, stderr=stderr)
    return ExecResult(exit_code=int(proc.returncode or 0), stdout=stdout, stderr=stderr)


def _serve_nl_requests(
    requests_path: Path,
    responses_path: Path,
    served: set[str],
    nl_service,
) -> None:
    if not requests_path.exists():
        return
    for line in requests_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = str(request.get("request_id", ""))
        if not request_id or request_id in served:
            continue
        served.add(request_id)
        if nl_service is None:
            response = {"request_id": request_id, "status": "error", "error": "nl proxy is not configured"}
        else:
            try:
                result = nl_service.run(
                    str(request.get("ts_code", "")),
                    prompt=str(request.get("prompt", "") or ""),
                    kwargs=dict(request.get("kwargs") or {}),
                    request=dict(request),
                )
                response = {"request_id": request_id, "status": "ok", "result": result}
            except Exception as exc:  # noqa: BLE001 - strategy sees a fixable tool error
                error = sanitize_for_log(f"{type(exc).__name__}: {exc}")
                response = {"request_id": request_id, "status": "error", "error": error}
        with responses_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")


def _jsonable(value):
    if isinstance(value, pd.Series):
        return {str(k): _jsonable(v) for k, v in value.to_dict().items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:  # noqa: BLE001 - keep JSON conversion best-effort
            pass
    return value


def _payload_to_frame(payload: object) -> pd.DataFrame:
    if not isinstance(payload, dict):
        raise BacktestError("strategy output frame payload must be an object")
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise BacktestError("strategy output frame payload must contain columns and rows lists")
    return pd.DataFrame(rows, columns=[str(column) for column in columns])


@contextmanager
def hide_snapshot_slots_from_agent(paths):
    """Temporarily hide replay/exploration/artifact slots from strategy code.

    Docker runs candidate code as the non-root ``agent`` user. Making the slot
    roots owner-only is enough to prevent traversal while keeping the current
    `/mnt/snapshot` view and staged workspace inputs available.
    """
    slots: list[tuple[Path, int]] = []
    for path in (paths.train, paths.valid, paths.test, paths.artifacts):
        if path.exists():
            slots.append((path, stat.S_IMODE(path.stat().st_mode)))
    try:
        for path, _mode in slots:
            path.chmod(0o700)
        yield
    finally:
        for path, mode in slots:
            path.chmod(mode)


def _executor_pathsep_join(executor, paths: list[Path]) -> str:
    return os.pathsep.join(executor.map_path(path) for path in paths if path.exists())


def validate_trade_intents(intents: pd.DataFrame, *, universe: set[str]) -> pd.DataFrame:
    """Validate the candidate-to-strategy mapping before replay.

    Each row maps one stock to an Agent-defined strategy function name. The
    Environment resolves the function during replay and the Broker enforces
    cash, margin, T+1, limits, suspension, and short inventory at runtime, so
    validation is structural only.
    """
    if intents.empty:
        return pd.DataFrame(columns=["ts_code", "trade_strategy", "params", "start_date", "end_date", "reason", "source_artifacts"])
    frame = intents.copy()
    if "code" in frame.columns and "ts_code" not in frame.columns:
        frame = frame.rename(columns={"code": "ts_code"})
    if "strategy" in frame.columns and "trade_strategy" not in frame.columns:
        frame = frame.rename(columns={"strategy": "trade_strategy"})
    missing = [col for col in TRADE_INTENT_REQUIRED_COLUMNS if col not in frame.columns]
    if missing:
        raise BacktestError(f"trade_intents missing required columns: {missing}")
    frame["ts_code"] = frame["ts_code"].astype(str)
    unknown = sorted(set(frame["ts_code"]) - universe)
    if unknown:
        raise BacktestError(f"trade_intents contain codes outside the visible universe: {unknown[:5]}")
    duplicates = sorted(frame["ts_code"][frame["ts_code"].duplicated()].unique())
    if duplicates:
        raise BacktestError(f"trade_intents must map each stock to one strategy; duplicates: {duplicates[:5]}")

    strategy_values = frame["trade_strategy"].fillna("").astype(str).str.strip()
    if (strategy_values == "").any():
        raise BacktestError("trade_intents.trade_strategy must be non-empty")
    bad_strategy = [value for value in strategy_values if not _valid_strategy_name(value)]
    if bad_strategy:
        raise BacktestError(f"trade_intents have invalid trade_strategy function names: {bad_strategy[:5]}")
    frame["trade_strategy"] = strategy_values

    frame["params"] = _normalize_params(frame)

    for column in ("start_date", "end_date"):
        if column not in frame.columns:
            frame[column] = ""
        else:
            frame[column] = frame[column].fillna("").astype(str)
    bad_dates = [
        value
        for value in list(frame["start_date"]) + list(frame["end_date"])
        if value and not _is_valid_yyyymmdd(str(value))
    ]
    if bad_dates:
        raise BacktestError(f"trade_intents start_date/end_date must use YYYYMMDD: {bad_dates[:5]}")
    bad_ranges = [
        str(row.ts_code)
        for row in frame.itertuples()
        if row.start_date and row.end_date and str(row.start_date) > str(row.end_date)
    ]
    if bad_ranges:
        raise BacktestError(f"trade_intents start_date must be <= end_date for: {bad_ranges[:5]}")

    if "reason" not in frame.columns:
        frame["reason"] = ""
    else:
        frame["reason"] = frame["reason"].fillna("").astype(str)
    if "source_artifacts" not in frame.columns:
        frame["source_artifacts"] = [[] for _ in range(len(frame))]
    bad_sources = [
        code
        for code, sources in zip(frame["ts_code"], frame["source_artifacts"])
        if not isinstance(sources, (list, tuple))
    ]
    if bad_sources:
        raise BacktestError(f"trade_intents.source_artifacts must be a list for: {bad_sources[:5]}")
    frame["source_artifacts"] = frame["source_artifacts"].map(lambda value: list(value) if isinstance(value, (list, tuple)) else [])

    keep = ["ts_code", "trade_strategy", "params", "start_date", "end_date", "reason", "source_artifacts"]
    return frame[keep].reset_index(drop=True)


def _normalize_params(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Build the per-row params dict, folding in non-reserved top-level columns.

    A row may declare params inline (``{code, trade_strategy, amount: 200}``) or
    nested (``params={"amount": 200}``); both are merged into ``ctx.params``.
    """
    raw_params = frame["params"] if "params" in frame.columns else [None] * len(frame)
    extra_columns = [col for col in frame.columns if col not in INTENT_RESERVED_COLUMNS]
    params: list[dict[str, object]] = []
    for idx, base in enumerate(raw_params):
        merged: dict[str, object] = {}
        for column in extra_columns:
            value = frame.iloc[idx][column]
            if isinstance(value, (list, tuple, dict)) or pd.notna(value):
                merged[column] = value
        if isinstance(base, dict):
            merged.update(base)
        elif base is not None and not (isinstance(base, float) and math.isnan(base)):
            raise BacktestError("trade_intents.params must be a dict when provided")
        params.append(merged)
    return params


def strategy_function_names(intents: pd.DataFrame) -> list[str]:
    if intents.empty or "trade_strategy" not in intents.columns:
        return []
    return sorted({str(name) for name in intents["trade_strategy"].dropna().astype(str) if str(name).strip()})


def _valid_strategy_name(strategy: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(strategy)))


def _is_valid_yyyymmdd(value: str) -> bool:
    if not re.fullmatch(r"\d{8}", value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


@dataclass
class ReplayResult:
    equity_curve: pd.Series
    broker: SimBroker
    decision_date: str
    exit_date: str
    granularity: str = "minute"


class MinuteMarketData:
    """Minute replay bars indexed by trade date, minute, and code."""

    REQUIRED = ("trade_date", "ts_code", "close")
    TIME_COLUMNS = ("trade_time", "datetime", "timestamp", "time")

    def __init__(self, minutes: pd.DataFrame) -> None:
        if minutes.empty:
            raise ValueError("minute replay data is empty")
        missing = [col for col in self.REQUIRED if col not in minutes.columns]
        if missing:
            raise ValueError(f"replay minute data missing columns: {missing}")
        time_column = next((col for col in self.TIME_COLUMNS if col in minutes.columns), None)
        if time_column is None:
            raise ValueError(f"replay minute data missing one of time columns: {list(self.TIME_COLUMNS)}")
        frame = minutes.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["ts_code"] = frame["ts_code"].astype(str)
        frame["minute_key"] = frame[time_column].map(_minute_key)
        if frame["minute_key"].isna().any():
            bad = frame.loc[frame["minute_key"].isna(), time_column].head(5).tolist()
            raise ValueError(f"replay minute data has invalid trade_time values: {bad}")
        frame["minute_sort"] = frame["minute_key"].map(_minute_sort)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        else:
            frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
        if "high" not in frame.columns:
            frame["high"] = frame[["open", "close"]].max(axis=1)
        else:
            frame["high"] = pd.to_numeric(frame["high"], errors="coerce")
        if "low" not in frame.columns:
            frame["low"] = frame[["open", "close"]].min(axis=1)
        else:
            frame["low"] = pd.to_numeric(frame["low"], errors="coerce")
        frame = frame.sort_values(["trade_date", "minute_sort", "ts_code"], kind="stable").reset_index(drop=True)
        self._frame = frame

    def rows_for_date(self, trade_date: str) -> pd.DataFrame:
        return self._frame[self._frame["trade_date"] == str(trade_date)].copy()


def run_trade_intent_replay(
    intents: pd.DataFrame,
    replay_daily: pd.DataFrame,
    profile: BrokerProfile,
    *,
    decision_time_iso: str,
    shortable_codes: frozenset[str],
    replay_intraday_1min: pd.DataFrame | None = None,
    strategy_policy: StrategyPolicyRunner | None = None,
) -> ReplayResult:
    """Replay the candidate-to-strategy mapping minute-by-minute.

    Every mapped stock's ``trade_strategy`` function runs on each due bar and
    drives Broker primitives. Minute bars are used when present; otherwise a
    daily-synthesized 09:30/15:00 fallback is generated per code. The final
    trade date is reserved for mandatory liquidation of remaining holdings.
    """
    market = MarketData(replay_daily)
    if len(market.trade_dates) < 2:
        raise BacktestError("replay region needs at least two trade dates for entry/exit")
    minute_market = (
        MinuteMarketData(replay_intraday_1min)
        if replay_intraday_1min is not None and not replay_intraday_1min.empty
        else None
    )
    granularity = "minute" if minute_market is not None else "daily"
    entry_date, exit_date = market.trade_dates[0], market.trade_dates[-1]
    broker = SimBroker(profile, market, shortable_codes=shortable_codes)
    equity_by_date: dict[str, float] = {}

    for trade_date in market.trade_dates:
        due = _intents_due_on_date(intents, trade_date=trade_date, default_date=entry_date)
        if trade_date != exit_date and strategy_policy is not None and not due.empty:
            minute_seed = minute_market.rows_for_date(trade_date) if minute_market is not None else _empty_minute_rows()
            minute_rows = _minute_rows_with_daily_fallback(replay_daily, trade_date, minute_seed)
            for minute_key, minute_group in minute_rows.groupby("minute_key", sort=True):
                for row in due.itertuples():
                    bar = _minute_bar_for_code(minute_group, row.ts_code)
                    if bar is None:
                        continue
                    _run_strategy_for_bar(
                        row,
                        strategy_policy,
                        broker,
                        trade_date=trade_date,
                        bar=bar,
                        minute_key=str(minute_key),
                        price_label=f"{granularity}:{minute_key}",
                    )

        equity = broker.mark_to_market(trade_date)
        if trade_date == exit_date and broker.positions:
            broker.close_all(trade_date)
            equity = broker.equity()
        equity_by_date[trade_date] = equity

    return ReplayResult(
        equity_curve=pd.Series(equity_by_date).sort_index(),
        broker=broker,
        decision_date=entry_date,
        exit_date=exit_date,
        granularity=granularity,
    )


def _run_strategy_for_bar(
    row,
    strategy_policy: StrategyPolicyRunner,
    broker: SimBroker,
    *,
    trade_date: str,
    bar: pd.Series | None,
    minute_key: str,
    price_label: str,
) -> None:
    state = _strategy_state(row, broker, trade_date=trade_date, bar=bar, minute_key=minute_key)
    actions = strategy_policy.actions(strategy=str(row.trade_strategy), state=state)
    if not actions:
        return
    broker.record_event(
        "strategy_actions",
        ts_code=str(row.ts_code),
        trade_strategy=str(row.trade_strategy),
        trade_date=trade_date,
        minute_key=minute_key,
        action_count=len(actions),
        actions=_jsonable(actions),
    )
    raw_price = _bar_execution_price(bar)
    for action in actions:
        _execute_action(
            action,
            row,
            broker,
            trade_date=trade_date,
            raw_price=raw_price,
            price_label=price_label,
            minute_key=minute_key,
        )


_ACTION_ALIASES = {
    "long": "buy",
    "sell_short": "short",
    "close_long": "sell",
    "close_short": "cover",
    "exit": "close",
}
_SUPPORTED_ACTIONS = {"buy", "sell", "short", "cover", "close"}


def _execute_action(
    action: dict[str, object],
    row,
    broker: SimBroker,
    *,
    trade_date: str,
    raw_price: float | None,
    price_label: str,
    minute_key: str,
) -> None:
    name = str(action.get("action", "")).lower().strip()
    name = _ACTION_ALIASES.get(name, name)
    if name not in _SUPPORTED_ACTIONS:
        broker.record_event(
            "strategy_action_ignored",
            ts_code=str(row.ts_code),
            trade_date=trade_date,
            action=_jsonable(action),
            reason="unsupported_action",
        )
        return
    amount = _int_or_none(action.get("amount"))
    weight = _float_or_none(action.get("weight"))
    reason = str(action.get("reason") or getattr(row, "reason", "") or row.trade_strategy)
    broker.execute(
        str(row.ts_code),
        name,
        trade_date=trade_date,
        raw_price=raw_price,
        amount=amount,
        weight=weight,
        time=minute_key,
        reason=reason,
        source_artifacts=list(getattr(row, "source_artifacts", []) or []),
        price_label=price_label,
    )


def _strategy_state(row, broker: SimBroker, *, trade_date: str, bar: pd.Series | None, minute_key: str) -> dict[str, object]:
    intent = dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)
    code = str(intent.get("ts_code", ""))
    price = _bar_execution_price(bar)
    params = intent.get("params") if isinstance(intent.get("params"), dict) else {}
    return {
        "ts_code": code,
        "code": code,
        "trade_strategy": str(intent.get("trade_strategy", "")),
        "params": _jsonable(params),
        "reason": str(intent.get("reason", "") or ""),
        "start_date": str(intent.get("start_date", "") or ""),
        "end_date": str(intent.get("end_date", "") or ""),
        "bar": _jsonable(bar) if bar is not None else {},
        "account": _jsonable(broker.get_account()),
        "positions": _jsonable(broker.get_positions()),
        "trades": _jsonable(broker.trades_for(code)),
        "trade_date": str(trade_date),
        "cur_date": str(trade_date),
        "cur_time": str(minute_key or ""),
        "time": str(minute_key or ""),
        "minute": str(minute_key or ""),
        "cur_price": price,
        "price": price,
        "money": float(broker.cash),
        "initial_equity": float(broker.initial_equity),
        "position": int(broker.position_quantity(code)),
    }


def _int_or_none(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_execution_price(bar: pd.Series | None) -> float | None:
    if bar is None:
        return None
    for field in ("close", "open"):
        value = bar.get(field)
        if pd.notna(value):
            return float(value)
    return None


def _intents_due_on_date(intents: pd.DataFrame, *, trade_date: str, default_date: str) -> pd.DataFrame:
    if intents.empty:
        return intents
    frame = intents.copy()
    starts = frame["start_date"].fillna("").astype(str)
    starts = starts.where(starts != "", default_date)
    ends = frame["end_date"].fillna("").astype(str)
    due = (starts <= trade_date) & ((ends == "") | (trade_date <= ends))
    return frame[due]


def _minute_key(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 12:
        hour, minute = int(digits[8:10]), int(digits[10:12])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    if len(digits) in {4, 6}:
        hour, minute = int(digits[:2]), int(digits[2:4])
        return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%H:%M")


def _minute_sort(minute_key: str) -> int:
    hour, minute = str(minute_key).split(":", maxsplit=1)
    return int(hour) * 60 + int(minute)


def _empty_minute_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=["trade_date", "ts_code", "open", "close", "high", "low", "minute_key", "minute_sort"])


def _synthetic_daily_minutes(replay_daily: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Fallback minute bars (09:30 open, 15:00 close) for daily-only dates."""
    rows = replay_daily[replay_daily["trade_date"].astype(str) == str(trade_date)].copy()
    if rows.empty:
        return _empty_minute_rows()
    lows = rows.apply(_daily_low, axis=1)
    highs = rows.apply(_daily_high, axis=1)
    open_rows = rows.copy()
    open_rows["close"] = open_rows["open"]
    open_rows["high"] = highs
    open_rows["low"] = lows
    open_rows["minute_key"] = "09:30"
    close_rows = rows.copy()
    close_rows["open"] = close_rows["close"]
    close_rows["high"] = highs
    close_rows["low"] = lows
    close_rows["minute_key"] = "15:00"
    frame = pd.concat([open_rows, close_rows], ignore_index=True)
    frame["minute_sort"] = frame["minute_key"].map(_minute_sort)
    return frame.sort_values(["minute_sort", "ts_code"], kind="stable").reset_index(drop=True)


def _minute_rows_with_daily_fallback(
    replay_daily: pd.DataFrame,
    trade_date: str,
    minute_rows: pd.DataFrame,
) -> pd.DataFrame:
    fallback = _synthetic_daily_minutes(replay_daily, trade_date)
    if minute_rows.empty:
        return fallback
    present_codes = set(minute_rows["ts_code"].astype(str))
    missing_rows = fallback[~fallback["ts_code"].astype(str).isin(present_codes)]
    close_fallback = fallback[
        (fallback["minute_key"] == "15:00")
        & fallback["ts_code"].astype(str).isin(present_codes)
    ].copy()
    if not close_fallback.empty:
        existing_keys = set(zip(minute_rows["ts_code"].astype(str), minute_rows["minute_key"].astype(str)))
        close_fallback = close_fallback[
            [
                (str(row.ts_code), str(row.minute_key)) not in existing_keys
                for row in close_fallback.itertuples()
            ]
        ]
    if missing_rows.empty and close_fallback.empty:
        return minute_rows
    return pd.concat([minute_rows, missing_rows, close_fallback], ignore_index=True).sort_values(
        ["minute_sort", "ts_code"],
        kind="stable",
    ).reset_index(drop=True)


def _daily_low(bar: pd.Series) -> float:
    values = [bar.get("low"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return min(numeric) if numeric else math.nan


def _daily_high(bar: pd.Series) -> float:
    values = [bar.get("high"), bar.get("open"), bar.get("close")]
    numeric = [float(value) for value in values if pd.notna(value)]
    return max(numeric) if numeric else math.nan


def _minute_bar_for_code(minute_group: pd.DataFrame, ts_code: str) -> pd.Series | None:
    rows = minute_group[minute_group["ts_code"].astype(str) == str(ts_code)]
    if rows.empty:
        return None
    return rows.iloc[-1]


def compute_return_stats(result: ReplayResult) -> dict[str, object]:
    """The minimum return statistics from docs/environment_design.md 7.6."""
    broker = result.broker
    curve = result.equity_curve
    initial = broker.initial_equity
    total_return = curve.iloc[-1] / initial - 1.0 if len(curve) else 0.0
    daily_returns = curve.pct_change().dropna()
    sharpe = 0.0
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
    peak = curve.cummax()
    max_drawdown = float(((peak - curve) / peak).max()) if len(curve) else 0.0
    years = max(len(curve), 1) / TRADING_DAYS_PER_YEAR
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1.0 else -1.0
    realized = [event for event in broker.events if event["event_type"] in {"position_closed", "position_reduced"}]
    full_closes = [event for event in broker.events if event["event_type"] == "position_closed"]
    long_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "long")
    short_pnl = sum(e["realized_pnl"] for e in realized if e["side"] == "short")
    wins = sum(1 for e in realized if e["realized_pnl"] > 0)
    orders = broker.query_orders()
    per_stock = [
        {
            "ts_code": event["ts_code"],
            "side": event["side"],
            "exit_date": event["trade_date"],
            "exit_price": event["price"],
            "exit_price_label": event.get("price_label"),
            "quantity": event.get("quantity"),
            "realized_pnl": event["realized_pnl"],
            "kind": event["event_type"],
            "forced": event.get("forced", False),
        }
        for event in realized
    ]
    status_counts: dict[str, int] = {}
    for order in orders:
        status_counts[str(order["status"])] = status_counts.get(str(order["status"]), 0) + 1
    return {
        "initial_cash": initial,
        "final_equity": float(curve.iloc[-1]) if len(curve) else initial,
        "total_return": float(total_return),
        "long_return": float(long_pnl / initial),
        "short_return": float(short_pnl / initial),
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": float(wins / len(realized)) if realized else 0.0,
        "holdings_count": len(full_closes),
        "trade_count": len(realized),
        "turnover": float(broker.traded_notional / initial) if initial else 0.0,
        "order_count": len(orders),
        "order_status_counts": status_counts,
        "reject_counts": dict(broker.reject_counts),
        "margin_secs_reject_count": broker.reject_counts.get("margin_secs_not_shortable", 0),
        "broker_inventory_reject_count": broker.reject_counts.get("broker_inventory_unavailable", 0),
        "max_holdings_reject_count": broker.reject_counts.get("max_holdings_reached", 0),
        "fees_paid": float(broker.fees_paid),
        "stamp_duty_paid": float(broker.stamp_duty_paid),
        "slippage_bps_assumed": broker.profile.slippage_bps,
        "short_borrow_fees": float(broker.borrow_fees),
        "forced_close_events": sum(1 for e in broker.events if e["event_type"] == "forced_close_triggered"),
        "replay_granularity": result.granularity,
        "equity_curve": {str(k): float(v) for k, v in curve.items()},
        "decision_date": result.decision_date,
        "exit_date": result.exit_date,
        "per_stock": per_stock,
        "broker_events": broker.events,
    }
