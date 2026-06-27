"""Cross-sectional screening and new entries, called by ``main(ctx)``.

``screen_and_open(ctx)`` is where you rank the cross-section and open positions
with ``ctx.broker.buy(code, weight=...)`` / ``ctx.broker.short(code, ...)``. Call
it only on the ticks you choose inside ``main`` (e.g. once pre-open).

Read daily history from ``ctx.asof_dir`` — the rolling daily as-of view (daily
bars visible by the current replay day's pre-open, including replay-period days
that have already closed). Other domains (events/text/fundamentals/intraday
history) live on ``ctx.snapshot_dir`` (frozen at the fold decision time). You may
call ``ctx.nl(code, prompt=...)`` and parse ``result["content"]`` yourself; keep
``nl()`` frequency low because it is the main API cost.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd  # noqa: F401 - available for screening reads


def screen_and_open(ctx) -> None:
    """Return value is ignored; open positions directly via ``ctx.broker``.

    The template screens nothing and opens nothing. A strategy reads
    ``ctx.asof_dir`` (rolling daily as-of for ``ctx.cur_date``), ranks the
    cross-section, and opens new positions, e.g.::

        asof_dir = Path(str(ctx.asof_dir))
        daily = pd.read_parquet(asof_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, weight=0.1, reason="example_top")
    """
    asof_dir = Path(str(ctx.asof_dir))
    if not (asof_dir / "daily.parquet").exists():
        raise FileNotFoundError(f"missing as-of dir: {asof_dir}")
    return
