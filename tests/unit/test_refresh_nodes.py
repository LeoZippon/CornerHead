"""Timeview refresh-node table: cron drift guard + visibility-cutoff helpers."""

import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from autotrade.environment.data.contracts import (
    DOMAIN_REFRESH_NODES,
    EVENT_DATASET_REFRESH_NODES,
    REFRESH_NODES,
    TEXT_DATASET_REFRESH_NODES,
    domain_visible_cutoff,
    event_dataset_visible_cutoff,
    text_dataset_visible_cutoff,
    visible_cutoff,
)

CN_TZ = ZoneInfo("Asia/Shanghai")
REPO_ROOT = Path(__file__).resolve().parents[2]
CRON_SCHEDULE = REPO_ROOT / "configs" / "tushare_update_schedule.json"

# Jobs that only audit/compare existing data and land nothing new — never nodes.
AUDIT_ONLY_JOBS = {
    "cn_nightly_full_audit",
    "cn_daily_revision_sentinel",
    "cn_preopen_event_flow_audit_0920",
}


def _cron_jobs() -> set[str]:
    schedule = json.loads(CRON_SCHEDULE.read_text(encoding="utf-8"))
    return set(schedule["jobs"])


class RefreshNodeDriftGuardTest(unittest.TestCase):
    def test_every_node_is_a_real_cron_job(self) -> None:
        jobs = _cron_jobs()
        for name in REFRESH_NODES:
            self.assertIn(name, jobs, f"REFRESH_NODES[{name!r}] is not a cron job in the schedule")

    def test_audit_only_jobs_are_not_nodes(self) -> None:
        for job in AUDIT_ONLY_JOBS:
            self.assertNotIn(job, REFRESH_NODES, f"audit-only job {job!r} must not be a refresh node")

    def test_dataset_overrides_reference_real_nodes(self) -> None:
        for mapping in (DOMAIN_REFRESH_NODES, EVENT_DATASET_REFRESH_NODES, TEXT_DATASET_REFRESH_NODES):
            for key, node_names in mapping.items():
                for name in node_names:
                    self.assertIn(name, REFRESH_NODES, f"{key!r} maps to unknown node {name!r}")


class VisibilityCutoffTest(unittest.TestCase):
    def test_daily_domain_visible_only_through_prior_day_during_session(self) -> None:
        # During day D's session the evening node that lands D's daily core has not
        # finished (it runs D 23:35 -> D+1 02:05), so the cutoff is D-1's evening
        # start: daily for D-1 is visible, daily for D is not.
        when = datetime(2022, 1, 5, 9, 31, tzinfo=CN_TZ)
        cutoff = domain_visible_cutoff("daily", when)
        self.assertEqual(cutoff, datetime(2022, 1, 4, 23, 35, tzinfo=CN_TZ))

    def test_daily_domain_rolls_after_evening_completes(self) -> None:
        # After 02:05 on D+1 the evening node that ran D 23:35 has completed.
        when = datetime(2022, 1, 6, 2, 30, tzinfo=CN_TZ)
        cutoff = domain_visible_cutoff("daily", when)
        self.assertEqual(cutoff, datetime(2022, 1, 5, 23, 35, tzinfo=CN_TZ))

    def test_margin_secs_visible_same_day_after_preopen_node(self) -> None:
        # By 09:31 both the 09:03 backfill and 09:13 retry have completed, so the
        # same-day shortable universe (available ~09:00) is visible.
        when = datetime(2022, 1, 5, 9, 31, tzinfo=CN_TZ)
        cutoff = event_dataset_visible_cutoff("margin_secs", when)
        self.assertEqual(cutoff, datetime(2022, 1, 5, 9, 13, tzinfo=CN_TZ))

    def test_margin_secs_not_yet_visible_before_preopen_node(self) -> None:
        # At 08:00 no same-day margin_secs node has completed; the cutoff falls back
        # to the prior day's retry instant (yesterday's universe only).
        when = datetime(2022, 1, 5, 8, 0, tzinfo=CN_TZ)
        cutoff = event_dataset_visible_cutoff("margin_secs", when)
        self.assertEqual(cutoff, datetime(2022, 1, 4, 9, 13, tzinfo=CN_TZ))

    def test_fundamentals_visible_after_pit_build_completes(self) -> None:
        when = datetime(2022, 1, 5, 4, 0, tzinfo=CN_TZ)
        cutoff = domain_visible_cutoff("fundamentals", when)
        self.assertEqual(cutoff, datetime(2022, 1, 5, 3, 35, tzinfo=CN_TZ))

    def test_cctv_news_refined_by_preopen_text_node(self) -> None:
        # The evening node lands the bulk; the 08:55 pre-open backfill refines the
        # same-day short text, so by 09:00 the later (08:55) cutoff wins.
        when = datetime(2022, 1, 5, 9, 0, tzinfo=CN_TZ)
        cutoff = text_dataset_visible_cutoff("cctv_news", when)
        self.assertEqual(cutoff, datetime(2022, 1, 5, 8, 55, tzinfo=CN_TZ))

    def test_unknown_dataset_defaults_to_evening_node(self) -> None:
        when = datetime(2022, 1, 5, 9, 31, tzinfo=CN_TZ)
        self.assertEqual(
            event_dataset_visible_cutoff("anns_d", when),
            domain_visible_cutoff("daily", when),
        )

    def test_visible_cutoff_none_before_any_node_completes(self) -> None:
        # Just after midnight on the very first day, no evening node has finished.
        when = datetime(2022, 1, 1, 0, 5, tzinfo=CN_TZ)
        # The prior day's evening node (2021-12-31 23:35 -> 2022-01-01 02:05) is not
        # done at 00:05, and the day-before-that completed 2021-12-31 02:05.
        cutoff = visible_cutoff(("cn_evening_full",), when)
        self.assertEqual(cutoff, datetime(2021, 12, 30, 23, 35, tzinfo=CN_TZ))


if __name__ == "__main__":
    unittest.main()
