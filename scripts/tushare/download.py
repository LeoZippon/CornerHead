#!/usr/bin/env python3
"""Thin CLI wrapper for MacroQuant TuShare download/update commands."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hl_trader.data_sources.tushare.download import *  # noqa: F401,F403
from hl_trader.data_sources.tushare.download import main


if __name__ == "__main__":
    raise SystemExit(main())
