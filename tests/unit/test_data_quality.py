import json
import tempfile
import unittest
from pathlib import Path

from autotrade.data_quality import (
    FINDING_KEYS,
    QUALITY_REPORT_KEYS,
    build_quality_report,
    summarize_datasets,
    write_quality_report,
)


class DataQualityContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.findings = [
            {
                "severity": "warning",
                "check": "daily_partitions",
                "message": "daily partitions checked",
                "details": {"missing": 1},
            }
        ]
        self.scope = {
            "data_root": "/data/raw",
            "start_date": "20200101",
            "end_date": "20200131",
            "datasets": ["daily"],
        }

    def test_report_and_nested_records_have_one_exact_envelope(self) -> None:
        datasets = summarize_datasets(self.findings, ["daily"])
        report = build_quality_report(
            report_type="base_research",
            scope=self.scope,
            findings=self.findings,
            datasets=datasets,
        )

        self.assertEqual(set(report), QUALITY_REPORT_KEYS)
        self.assertEqual(set(report["findings"][0]), FINDING_KEYS)
        self.assertEqual(report["finding_counts"], {"error": 0, "warning": 1, "info": 0})
        self.assertEqual(report["datasets"]["daily"]["status"], "warning")

    def test_invalid_finding_shape_is_rejected(self) -> None:
        invalid = [{**self.findings[0], "extra": True}]
        with self.assertRaisesRegex(ValueError, "findings require exactly"):
            build_quality_report(
                report_type="base_research",
                scope=self.scope,
                findings=invalid,
            )

    def test_writer_publishes_parseable_contract(self) -> None:
        report = build_quality_report(
            report_type="base_research",
            scope=self.scope,
            findings=self.findings,
            datasets=summarize_datasets(self.findings, ["daily"]),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "status.json"
            write_quality_report(output, report)

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            self.assertFalse(output.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
