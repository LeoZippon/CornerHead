"""Formal strategy entrypoint.

The Environment runs this file to obtain the candidate-to-strategy mapping
(``trade_intents``), serves optional ``mq_tools.nl`` calls, and then replays
each mapped stock minute-by-minute by calling its ``trade_strategy`` function
(defined in ``trading.py``) so it can drive the Broker primitives.

Use ``context["model_dir"]`` for persisted model parameters. Keep transient
training files in memory unless the parameters should be inherited by later
folds.
"""

from __future__ import annotations

from candidate import select_candidates
from trading import build_trades


def run_strategy(context: dict[str, object]) -> dict[str, object]:
    candidates = select_candidates(context)
    trades = build_trades(context, candidates)
    return {
        "candidates": candidates,
        "trade_intents": trades,
        "metadata": {
            "entrypoint": "run_strategy",
            "candidate_count": len(candidates),
            "trade_count": len(trades),
        },
    }
