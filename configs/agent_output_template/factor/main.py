"""Template strategy entrypoint for Sandbox Agent output.

The Environment copies this file to:

    /mnt/artifacts/agent_output/factor/main.py

Factor metadata must be registered in:

    /mnt/artifacts/agent_output/factor/factors.json

`backtest_tool` is the only formal caller. It constructs `context`; the Agent
must not hard-code dates, paths, or future data into this file.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


REQUIRED_CONTEXT_KEYS = (
    "decision_time",
    "buy_trade_date",
    "sell_trade_date",
    "snapshot_dir",
    "nl_prior_dir",
    "portfolio_state",
    "run_config",
)

REQUIRED_OUTPUT_COLUMNS = (
    "ts_code",
    "action",
    "target_weight",
    "score",
    "reason",
    "source_artifacts",
)

OPTIONAL_OUTPUT_COLUMNS = (
    "order_type",
    "amount",
    "volume",
    "risk_tags",
    "metadata",
)


def generate_orders(context: dict[str, Any]) -> pd.DataFrame:
    """Return candidate orders or target weights for `backtest_tool`.

    Required context keys:
    - decision_time: as-of decision timestamp.
    - buy_trade_date: first trade date for the replay.
    - sell_trade_date: exit date for the initial fixed-horizon replay.
    - snapshot_dir: read-only PIT data window.
    - nl_prior_dir: current natural-language prior directory.
    - portfolio_state: cash, positions, and available inventory before decision.
    - run_config: universe, cost, holding-period, and sizing config.

    Required output columns:
    - ts_code: stock code.
    - action: start with "target_weight"; future extensions may include
      buy, sell, short, cover, and hold.
    - target_weight: desired portfolio weight for initial long-only flow.
    - score: ranking or combined score.
    - reason: short reason string.
    - source_artifacts: JSON-serializable list of data/rule identifiers.

    The default implementation is intentionally empty but schema-valid. The
    Agent should replace the body with PIT-safe logic and keep factors.json in
    sync with any registered factor logic.
    """

    missing_context = [key for key in REQUIRED_CONTEXT_KEYS if key not in context]
    if missing_context:
        raise KeyError(f"missing required context keys: {missing_context}")

    return _empty_output()


def _empty_output() -> pd.DataFrame:
    return pd.DataFrame(columns=list(REQUIRED_OUTPUT_COLUMNS))


def validate_output(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate the minimum schema before returning custom output."""

    missing_columns = [
        column for column in REQUIRED_OUTPUT_COLUMNS if column not in frame.columns
    ]
    if missing_columns:
        raise ValueError(f"missing required output columns: {missing_columns}")
    return frame
