"""Compatibility imports for TuShare shared data-source helpers."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hl_trader.data_sources.tushare.common import *  # noqa: F401,F403
