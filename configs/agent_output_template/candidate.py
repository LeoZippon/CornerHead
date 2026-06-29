"""Optional advanced helper — the minimal default ``main.py`` does not import this.

Cross-sectional screening (09:15) then order submission (09:25).

The two stages are split because orders use next-bar execution and the 09:15
info tick exposes no price:

* ``screen_targets(ctx)`` runs at the 09:15 info tick. Rank the cross-section from
  ``ctx.asof_dir`` (the rolling daily as-of view: daily bars visible by the
  current replay day's pre-open, including replay-period days that have already
  closed) and from ``ctx.snapshot_dir`` (events/text/fundamentals/intraday history
  frozen at the fold decision time). Optionally call ``ctx.nl(code, prompt=...)``
  and parse ``result["content"]`` yourself — keep its frequency low; it is the
  main API cost. Write the chosen targets to ``ctx.state_dir`` (no price is
  available yet to size or submit an order).
* ``open_targets(ctx)`` runs at the 09:25 tick, when ``ctx.price`` carries the
  matched open. Read the targets back and submit them with
  ``ctx.broker.buy(code, weight=...)`` / ``.short(...)``; the orders fill at the
  09:31 open. Submitting from the persisted list keeps the order set deduplicated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd  # noqa: F401 - available for screening reads

_TARGETS = "targets.json"


def screen_targets(ctx) -> None:
    """Screen the cross-section and persist the day's targets to ``state_dir``.

    The template selects nothing. A strategy reads ``ctx.asof_dir``, ranks the
    cross-section, and writes a ``{ts_code: weight}`` map, e.g.::

        asof_dir = Path(str(ctx.asof_dir))
        daily = pd.read_parquet(asof_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        targets = {code: 0.1}
    """
    asof_dir = Path(str(ctx.asof_dir))
    if not (asof_dir / "daily.parquet").exists():
        raise FileNotFoundError(f"missing as-of dir: {asof_dir}")
    targets: dict[str, float] = {}
    Path(str(ctx.state_dir), _TARGETS).write_text(json.dumps(targets), encoding="utf-8")


def open_targets(ctx) -> None:
    """Submit the targets persisted at 09:15; the orders fill at the 09:31 open."""
    path = Path(str(ctx.state_dir), _TARGETS)
    if not path.exists():
        return
    targets = json.loads(path.read_text(encoding="utf-8"))
    for code, weight in targets.items():
        # Skip codes already held or with an order still working (mirrors the live
        # order query), so the multi-bar fill lag cannot create a duplicate entry.
        if ctx.broker.position(code) == 0 and not ctx.broker.pending(code) and ctx.price(code) is not None:
            ctx.broker.buy(code, weight=float(weight), reason="screen_target")
