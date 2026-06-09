"""Template strategy entrypoint for Sandbox Agent output.

The Environment copies this file to:

    /mnt/artifacts/agent_output/factor/main.py

Factor metadata must be registered in:

    /mnt/artifacts/agent_output/factor/factors.json

`backtest_tool` is the only formal caller. It calls generate_candidates() inside the
fixed Sandbox layout; the Agent must not hard-code dates, paths, or future data
into this file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


SNAPSHOT_DIR = Path(os.environ.get("MQ_SNAPSHOT_DIR", "/mnt/snapshot"))
NL_PRIOR_DIR = Path("/mnt/artifacts/agent_output/nl_prior")

REQUIRED_OUTPUT_COLUMNS = (
    "ts_code",
    "factor_score",
    "reason",
    "source_artifacts",
)


def generate_candidates() -> pd.DataFrame:
    """Return a bounded candidate pool and factor scores for `backtest_tool`.

    Fixed input paths:
    - /mnt/snapshot/: Runner-managed current read-only PIT data window.
      Agent debugging may set MQ_SNAPSHOT_DIR=/mnt/snapshots/train.
    - /mnt/artifacts/agent_output/nl_prior/: current natural-language prior.

    Required output columns:
    - ts_code: stock code.
    - factor_score: numeric score computed only from PIT-visible factor logic.
    - reason: short reason string.
    - source_artifacts: JSON-serializable list of data/rule identifiers.

    The Agent is responsible for factor ranking and pre-screening. Return a
    bounded candidate pool, not the full market. The runtime max candidate count
    is provided by the run manifest; a practical default target is 30-100 names.

    The formal order plan is built by backtest_tool after NL scoring and
    trading-constraint checks.

    The default implementation is intentionally empty but schema-valid. The
    Agent should replace the body with PIT-safe logic and keep factors.json in
    sync with any registered factor logic.
    """

    if not SNAPSHOT_DIR.exists():
        raise FileNotFoundError(f"missing snapshot dir: {SNAPSHOT_DIR}")
    if not NL_PRIOR_DIR.exists():
        raise FileNotFoundError(f"missing nl prior dir: {NL_PRIOR_DIR}")

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
