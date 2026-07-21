"""PIT data layer: availability contracts, the raw-lake store, dataset
transforms, and snapshot provisioning.

Light contract/transform modules are re-exported below. The heavy provisioning
modules — ``snapshot`` (PIT snapshot & replay-slot builder), ``research_release``
(immutable raw-lake release pinning), and ``summary`` (agent-facing data
summary) — are imported directly as submodules.
"""

from .auction import AuctionCorrectionConfig, apply_open_auction_correction, market_bucket
from .contracts import DatasetContract, default_tushare_contracts
from .fundamental_events import (
    FUNDAMENTAL_EVENT_DATASETS,
    FundamentalEventsBuilder,
    FundamentalEventsConfig,
    audit_fundamental_events,
    month_aligned_replace_window,
)
from .pit import PITDataStore
from .units import DAILY_UNIT_CONVERSIONS, normalize_daily_units

__all__ = [
    "AuctionCorrectionConfig",
    "DAILY_UNIT_CONVERSIONS",
    "DatasetContract",
    "FUNDAMENTAL_EVENT_DATASETS",
    "FundamentalEventsBuilder",
    "FundamentalEventsConfig",
    "PITDataStore",
    "apply_open_auction_correction",
    "audit_fundamental_events",
    "default_tushare_contracts",
    "market_bucket",
    "month_aligned_replace_window",
    "normalize_daily_units",
]
