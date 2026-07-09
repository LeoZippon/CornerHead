"""Timeview refresh-node table: cron drift guard + visibility-cutoff helpers."""

import json
import re
import unittest
from datetime import date, datetime, time
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


CRONTAB = REPO_ROOT / "ops" / "cron" / "tushare_update.cron"

# A managed crontab line: "MM HH * * * ... --job <name> ...".
_CRON_LINE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s+.*--job\s+(\S+)")


def _crontab_job_times() -> dict[str, time]:
    times: dict[str, time] = {}
    for line in CRONTAB.read_text(encoding="utf-8").splitlines():
        match = _CRON_LINE.match(line)
        if match:
            minute, hour, name = int(match.group(1)), int(match.group(2)), match.group(3)
            times[name] = time(hour, minute)
    return times


class RefreshNodeDriftGuardTest(unittest.TestCase):
    def test_every_node_is_a_real_cron_job(self) -> None:
        jobs = _cron_jobs()
        for name in REFRESH_NODES:
            self.assertIn(name, jobs, f"REFRESH_NODES[{name!r}] is not a cron job in the schedule")

    def test_audit_only_jobs_are_not_nodes(self) -> None:
        for job in AUDIT_ONLY_JOBS:
            self.assertNotIn(job, REFRESH_NODES, f"audit-only job {job!r} must not be a refresh node")

    def test_node_start_times_match_crontab(self) -> None:
        # Node ``start`` must equal the real installed crontab launch time, so the
        # Timeview ``ready_at`` cadence cannot silently drift from ingestion.
        cron_times = _crontab_job_times()
        for name, node in REFRESH_NODES.items():
            self.assertIn(name, cron_times, f"REFRESH_NODES[{name!r}] has no managed crontab line")
            self.assertEqual(
                node.start,
                cron_times[name],
                f"REFRESH_NODES[{name!r}].start {node.start} != crontab launch {cron_times[name]}",
            )

    def test_every_landing_job_has_a_node(self) -> None:
        # The crontab and the JSON schedule must list the same jobs, and every job
        # that lands data (not audit-only) must have a Timeview refresh node.
        cron_jobs = set(_crontab_job_times())
        schedule_jobs = _cron_jobs()
        self.assertEqual(
            cron_jobs,
            schedule_jobs,
            "ops/cron/tushare_update.cron jobs differ from configs/tushare_update_schedule.json jobs",
        )
        for job in schedule_jobs - AUDIT_ONLY_JOBS:
            self.assertIn(job, REFRESH_NODES, f"data-landing job {job!r} has no Timeview refresh node")

    def test_evening_node_ready_at_matches_duration_fixture(self) -> None:
        # Lock the calibrated refresh duration: 23:35 launch + 150 min -> 02:05 next day.
        node = REFRESH_NODES["cn_evening_full"]
        self.assertEqual(
            node.ready_at(date(2022, 1, 5)),
            datetime(2022, 1, 6, 2, 5, tzinfo=CN_TZ),
        )

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

    def test_board_datasets_visible_from_preopen_backfill(self) -> None:
        # kpl_list/limit_step/limit_cpt_list publish next-day ~08:30 and land in
        # the 08:50 pre-open backfill: visible from 08:55, not the prior evening.
        for dataset in ("kpl_list", "limit_step", "limit_cpt_list"):
            after = event_dataset_visible_cutoff(dataset, datetime(2022, 1, 5, 8, 56, tzinfo=CN_TZ))
            self.assertEqual(after, datetime(2022, 1, 5, 8, 50, tzinfo=CN_TZ), dataset)
            before = event_dataset_visible_cutoff(dataset, datetime(2022, 1, 5, 8, 40, tzinfo=CN_TZ))
            self.assertEqual(before, datetime(2022, 1, 4, 23, 35, tzinfo=CN_TZ), dataset)
        # Hot lists land in the evening window only: default node applies.
        self.assertEqual(
            event_dataset_visible_cutoff("dc_hot", datetime(2022, 1, 5, 8, 56, tzinfo=CN_TZ)),
            datetime(2022, 1, 4, 23, 35, tzinfo=CN_TZ),
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
