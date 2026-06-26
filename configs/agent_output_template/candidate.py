"""Cross-sectional screening and new entries, called by ``main(ctx)``.

``screen_and_open(ctx)`` is where you read point-in-time data from
``ctx.snapshot_dir``, pick candidates, and open positions with
``ctx.broker.buy(code, weight=...)`` / ``ctx.broker.short(code, ...)``. Call it
only on the ticks you choose inside ``main`` (e.g. once pre-open). You may call
``ctx.nl(code, prompt=...)`` and parse ``result["content"]`` yourself; keep
``nl()`` frequency low because it is the main API cost.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd  # noqa: F401 - available for screening reads


def screen_and_open(ctx) -> None:
    """Return value is ignored; open positions directly via ``ctx.broker``.

    The template screens nothing and opens nothing. A strategy reads
    ``ctx.snapshot_dir`` (point-in-time as of ``ctx.cur_date``/``cur_time``),
    ranks the cross-section, and opens new positions, e.g.::

        snapshot_dir = Path(str(ctx.snapshot_dir))
        daily = pd.read_parquet(snapshot_dir / "daily.parquet")
        code = sorted(daily["ts_code"].astype(str).unique())[0]
        if ctx.broker.position(code) == 0 and ctx.price(code) is not None:
            ctx.broker.buy(code, weight=0.1, reason="example_top")
    """
    snapshot_dir = Path(str(ctx.snapshot_dir))
    if not (snapshot_dir / "daily.parquet").exists():
        raise FileNotFoundError(f"missing snapshot dir: {snapshot_dir}")
    return
