"""Regression tests that stop incomplete or inconsistent evidence being certified."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_user_study import ROOT, calculate, percentage, read_json, validate_aggregates, verification_supported
from scripts.check_user_study_materials import EVIDENCE_FILES, check_files, check_links, validate_manifest, write_manifest


class UserStudyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = copy.deepcopy(read_json(ROOT / "evals/user_study_results.json"))
        self.audit = copy.deepcopy(read_json(ROOT / "evidence/user_study/selection_audit.json"))

    def test_documented_aggregates_are_consistent(self):
        self.assertEqual(validate_aggregates(self.report, self.audit), [])
        result = calculate(self.report, self.audit)
        self.assertEqual(result["tasks"][3]["success_percent"], 60.0)

    def test_misreported_percentage_is_rejected(self):
        self.report["tasks"][3]["success_percent"] = 99.0
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_assisted_success_cannot_disappear(self):
        self.report["tasks"][0]["total_successes"] = 152
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_negative_count_is_rejected(self):
        self.report["tasks"][0]["not_successful"] = -1
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_zero_denominator_is_not_success(self):
        self.assertIsNone(percentage(0, 0))
        task = self.report["tasks"][0]
        removed_attempts = task["attempts"]
        for key in ("attempts", "independent_successes", "assisted_successes", "total_successes", "not_successful"):
            task[key] = 0
        self.report["task_rows"]["included_started_attempts"] -= removed_attempts
        self.assertIsNone(calculate(self.report, self.audit)["tasks"][0]["success_percent"])

    def test_duplicate_tasks_are_rejected(self):
        self.report["tasks"][1]["id"] = "T01"
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_recommendation_denominator_is_checked(self):
        self.report["recommendation_ratings"]["fully_verified_entries"] = 330
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_inconsistent_timing_is_rejected(self):
        self.report["paired_timing"]["mean_seconds_saved"] = 100.0
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_selection_mismatch_is_rejected(self):
        self.audit["removed_by_classification"][0]["count"] -= 1
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_missing_fingerprints_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "evidence/user_study/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"files": {}, "authenticity_verified": false}', encoding="utf-8")
            self.assertTrue(validate_manifest(root))

    def test_missing_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn("missing file: evals/user_study_results.json", check_files(Path(directory)))

    def test_aggregate_success_does_not_verify_students(self):
        self.assertFalse(verification_supported(self.report))
        self.report["verified"] = True
        self.report["verification"]["real_user_validation_flag"] = True
        self.assertTrue(validate_aggregates(self.report, self.audit))

    def test_modified_file_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in EVIDENCE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test fixture\n")
            write_manifest(root, "2026-10-09")
            self.assertEqual(validate_manifest(root), [])
            (root / EVIDENCE_FILES[0]).write_bytes(b"test fixture\r\n")
            self.assertEqual(validate_manifest(root), [])
            (root / EVIDENCE_FILES[0]).write_text("changed", encoding="utf-8")
            self.assertTrue(validate_manifest(root))

    def test_root_and_nested_links_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "README.md").write_text("[good](docs/report.md)", encoding="utf-8")
            report = root / "docs/report.md"
            report.write_text("[bad](../evals/missing.json)", encoding="utf-8")
            errors = check_links(root)
            self.assertEqual(len(errors), 1)
            self.assertIn("missing.json", errors[0])


if __name__ == "__main__":
    unittest.main()
