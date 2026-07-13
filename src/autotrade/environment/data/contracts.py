from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DatasetContract:
    dataset: str
    partition_key: str
    available_time: time
    lag_days: int = 0
    unit_rules: dict[str, str] | None = None
    pit_notes: str = ""

    def available_at(self, partition_date: date) -> datetime:
        return datetime.combine(partition_date + timedelta(days=self.lag_days), self.available_time, tzinfo=CN_TZ)


def default_tushare_contracts() -> dict[str, DatasetContract]:
    return {
        "daily": DatasetContract(
            dataset="daily",
            partition_key="trade_date",
            available_time=time(17, 30),
            unit_rules={"vol": "hands", "amount": "thousand_cny"},
            pit_notes="Use for close-to-close research or next-trade-date decisions, not same-day 09:25 decisions.",
        ),
        "daily_basic": DatasetContract(
            dataset="daily_basic",
            partition_key="trade_date",
            available_time=time(18, 0),
            unit_rules={"total_share": "ten_thousand_shares", "total_mv": "ten_thousand_cny"},
            pit_notes="Valuation and share fields are available after market close; use next trade date for decisions.",
        ),
        "adj_factor": DatasetContract(
            dataset="adj_factor",
            partition_key="trade_date",
            available_time=time(9, 30),
            unit_rules={"adj_factor": "ratio"},
            pit_notes="Raw trade_date alone is not enough for intraday PIT; conservative daily replay should use prior close factors.",
        ),
        "stk_limit": DatasetContract(
            dataset="stk_limit",
            partition_key="trade_date",
            available_time=time(8, 45),
            unit_rules={"up_limit": "cny_per_share", "down_limit": "cny_per_share"},
            pit_notes="Can be used before the trading session if the source timestamp is trusted.",
        ),
        "suspend_d": DatasetContract(
            dataset="suspend_d",
            partition_key="trade_date",
            available_time=time(8, 45),
            pit_notes="Use as a trading constraint; zero rows mean no suspended names for that partition.",
        ),
        "limit_list_d": DatasetContract(
            dataset="limit_list_d",
            partition_key="trade_date",
            available_time=time(17, 30),
            pit_notes="Event table starts in 2020 locally; use as next-day event evidence unless source timing is proven earlier.",
        ),
    }


@lru_cache(maxsize=16384)
def sim_datetime(trade_date: str, minute_key: str) -> datetime:
    """Beijing-time simulation clock for one replay tick.

    ``trade_date`` is ``YYYYMMDD`` and ``minute_key`` is ``HH:MM`` (24h). Every
    replay tick -- intraday bar, pre-open/close auction, and off-session -- binds to
    this clock. It is the single basis for off-session grid spacing, auction/fill
    mapping, ``available_at`` visibility in the Timeview, staged-write ``ready_at``,
    and the daily post-close refresh. The live loop reuses ``main(ctx)`` against the
    real Asia/Shanghai system clock, so the semantics carry over unchanged.

    Cached: a replay calls this once per tick over a small (date, minute) grid,
    and the returned datetime is immutable.
    """
    hour_text, _, minute_text = str(minute_key).partition(":")
    return datetime.strptime(str(trade_date), "%Y%m%d").replace(
        hour=int(hour_text or 0), minute=int(minute_text or 0), tzinfo=CN_TZ
    )


# ---- Timeview refresh nodes (docs/environment_design.md, check.md W3) ----
#
# The per-tick Timeview replays the real local-DB refresh cadence: a dataset row
# is visible only once the cron job that lands it has finished writing. Each node
# below mirrors one data-landing job in configs/tushare_update_schedule.json:
# ``start`` is the installed crontab launch time (Asia/Shanghai), and
# ``duration_minutes`` is the measured refresh cost, so the view does not see the
# data until ``ready_at = start + duration_minutes``. Audit-only jobs (the nightly
# full audit, the revision sentinel, the 09:20 event-flow audit) land no new data
# and are deliberately NOT nodes. ``test_*`` drift guards assert every node name is
# a real cron job and that the audit jobs stay excluded.


@dataclass(frozen=True)
class RefreshNode:
    """One data-landing refresh job: launches at ``start`` and is queryable from
    ``ready_at = start + duration_minutes`` (possibly the next calendar day)."""

    name: str
    start: time
    duration_minutes: int

    def start_at(self, day: date) -> datetime:
        return datetime.combine(day, self.start, tzinfo=CN_TZ)

    def ready_at(self, day: date) -> datetime:
        return self.start_at(day) + timedelta(minutes=self.duration_minutes)


REFRESH_NODES: dict[str, RefreshNode] = {
    # Evening rolling-window update: A-share daily core, minute history, money flow,
    # block trade, holders/repurchase/float/top-list, macro, and bulk text. Launches
    # Real dispatches have exceeded the former 150-minute estimate (one reached
    # 169 minutes). Historical replays have no per-run completion ledger, so use
    # a conservative 210-minute fallback (03:05) rather than knowingly exposing
    # rows before the observed job completed.
    "cn_evening_full": RefreshNode("cn_evening_full", time(23, 35), 210),
    # Fundamental PIT event index build (financial filings become queryable).
    "cn_nightly_pit_event_build": RefreshNode("cn_nightly_pit_event_build", time(3, 35), 15),
    # Pre-open board-trading backfill (kpl_list etc.).
    "cn_preopen_board_backfill_0850": RefreshNode("cn_preopen_board_backfill_0850", time(8, 50), 5),
    # Pre-open short-text backfill (cctv_news / news).
    "cn_preopen_text_backfill_0855": RefreshNode("cn_preopen_text_backfill_0855", time(8, 55), 5),
    # Same-day margin_secs (shortable universe) first attempt + retry.
    "cn_preopen_margin_secs_backfill_0903": RefreshNode("cn_preopen_margin_secs_backfill_0903", time(9, 3), 2),
    "cn_preopen_margin_secs_retry_0913": RefreshNode("cn_preopen_margin_secs_retry_0913", time(9, 13), 2),
    # Previous-day margin / margin_detail first attempt + retry.
    "cn_preopen_margin_backfill_0905": RefreshNode("cn_preopen_margin_backfill_0905", time(9, 5), 2),
    "cn_preopen_margin_retry_0915": RefreshNode("cn_preopen_margin_retry_0915", time(9, 15), 2),
    # Same-day exact opening-auction capture. Agent visibility uses each
    # partition's observed row-level available_at; the node remains the cron
    # drift/lifecycle record and covers the 09:27 polling window.
    "cn_open_auction_capture_0927": RefreshNode("cn_open_auction_capture_0927", time(9, 27), 4),
}

EVENING_NODE = "cn_evening_full"

# Whole-domain node assignment (a domain file's rows all roll on the same node).
DOMAIN_REFRESH_NODES: dict[str, tuple[str, ...]] = {
    "daily": (EVENING_NODE,),
    "intraday_1min": (EVENING_NODE,),
    "macro": (EVENING_NODE,),
    # Auction is deliberately not node-gated: each row carries the partition's
    # observed first availability and Timeview advances on that timestamp.
    "auction": (),
    "fundamentals": ("cn_nightly_pit_event_build",),
}

# Per-dataset overrides inside the events domain (default = cn_evening_full).
EVENT_DATASET_REFRESH_NODES: dict[str, tuple[str, ...]] = {
    "margin_secs": ("cn_preopen_margin_secs_backfill_0903", "cn_preopen_margin_secs_retry_0913"),
    "margin": ("cn_preopen_margin_backfill_0905", "cn_preopen_margin_retry_0915"),
    "margin_detail": ("cn_preopen_margin_backfill_0905", "cn_preopen_margin_retry_0915"),
    # Board-trading sources publishing next-day ~08:30: the 08:50 pre-open
    # backfill is their real landing job (the evening node refines backfills).
    "kpl_list": (EVENING_NODE, "cn_preopen_board_backfill_0850"),
    "kpl_concept_cons": (EVENING_NODE, "cn_preopen_board_backfill_0850"),
    "limit_step": (EVENING_NODE, "cn_preopen_board_backfill_0850"),
    "limit_cpt_list": (EVENING_NODE, "cn_preopen_board_backfill_0850"),
    # limit_list_ths / ths_hot / dc_hot / hm_detail / hm_list land in the
    # evening window only — the default node is already correct for them.
}

# Per-dataset overrides inside the text domain (default = cn_evening_full). The
# pre-open backfill refines the same natural day's short text before the open.
TEXT_DATASET_REFRESH_NODES: dict[str, tuple[str, ...]] = {
    "cctv_news": (EVENING_NODE, "cn_preopen_text_backfill_0855"),
    "news": (EVENING_NODE, "cn_preopen_text_backfill_0855"),
}


def node_visible_cutoff(node: RefreshNode, when: datetime) -> datetime | None:
    """Latest daily instance of ``node`` already finished by ``when``; returns its
    ``start`` instant (the availability cutoff) or None if none has completed yet.

    A row is visible under this node when its ``available_at`` is at or before the
    returned cutoff. Searching back three days covers any sub-day refresh duration
    (the longest node, the evening update, completes ~2.5h after launch).
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=CN_TZ)
    base_day = when.astimezone(CN_TZ).date()
    for delta in range(0, 3):
        day = base_day - timedelta(days=delta)
        if node.ready_at(day) <= when:
            return node.start_at(day)
    return None


def visible_cutoff(node_names: tuple[str, ...], when: datetime) -> datetime | None:
    """Availability cutoff under any of ``node_names`` at ``when``: the most recent
    completed node's start instant (later node refines an earlier one)."""
    cutoffs = [
        cutoff
        for name in node_names
        if (cutoff := node_visible_cutoff(REFRESH_NODES[name], when)) is not None
    ]
    return max(cutoffs) if cutoffs else None


def next_visible_boundary(node_names: tuple[str, ...], when: datetime) -> datetime | None:
    """Earliest future ``ready_at`` among ``node_names``.

    Timeview uses this as a fast gate between refreshes. The prior day's evening
    job can finish after midnight, so candidates include both the previous and
    current local calendar day. Boundaries are strict: a node ready exactly at
    ``when`` has already been processed and the next daily instance is returned.
    """
    if not node_names:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=CN_TZ)
    local_when = when.astimezone(CN_TZ)
    base_day = local_when.date()
    candidates = [
        ready_at
        for name in node_names
        for delta in (-1, 0, 1)
        if (ready_at := REFRESH_NODES[name].ready_at(base_day + timedelta(days=delta))) > local_when
    ]
    return min(candidates) if candidates else None


def domain_visible_cutoff(domain: str, when: datetime) -> datetime | None:
    """Timeview availability cutoff for a whole-domain file at ``when``."""
    return visible_cutoff(DOMAIN_REFRESH_NODES.get(domain, (EVENING_NODE,)), when)


def domain_next_visible_boundary(domain: str, when: datetime) -> datetime | None:
    """Next Timeview refresh boundary for a whole-domain file."""
    return next_visible_boundary(DOMAIN_REFRESH_NODES.get(domain, (EVENING_NODE,)), when)


def event_dataset_visible_cutoff(dataset: str, when: datetime) -> datetime | None:
    """Timeview availability cutoff for one events-domain dataset at ``when``."""
    return visible_cutoff(EVENT_DATASET_REFRESH_NODES.get(dataset, (EVENING_NODE,)), when)


def event_dataset_next_visible_boundary(dataset: str, when: datetime) -> datetime | None:
    """Next Timeview refresh boundary for one events-domain dataset."""
    return next_visible_boundary(EVENT_DATASET_REFRESH_NODES.get(dataset, (EVENING_NODE,)), when)


def text_dataset_visible_cutoff(dataset: str, when: datetime) -> datetime | None:
    """Timeview availability cutoff for one text-domain dataset at ``when``."""
    return visible_cutoff(TEXT_DATASET_REFRESH_NODES.get(dataset, (EVENING_NODE,)), when)


def text_dataset_next_visible_boundary(dataset: str, when: datetime) -> datetime | None:
    """Next Timeview refresh boundary for one text-domain dataset."""
    return next_visible_boundary(TEXT_DATASET_REFRESH_NODES.get(dataset, (EVENING_NODE,)), when)
