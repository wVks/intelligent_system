"""Meaningful aggregation invariants: denominator, failures, identity and provenance."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/study-eval/scripts/score_eval.py"
SPEC = importlib.util.spec_from_file_location("score_eval", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.dataset = {"dataset_id": "d1", "cases": [
            {"id": "known", "checks": ["fact", "source"], "critical": True},
            {"id": "unknown", "checks": ["no_fabrication"], "critical": True}]}
        self.results = {"dataset_id": "d1", "run_id": "r1", "mode": "offline", "cases": [
            {"id": "known", "error": None, "checks": [
                {"name": "fact", "passed": True, "evidence": "Both LMS links match page 1"},
                {"name": "source", "passed": True, "evidence": "Page 1 excerpt supports answer"}]},
            {"id": "unknown", "error": None, "checks": [
                {"name": "no_fabrication", "passed": True, "evidence": "No deadline asserted"}]}]}

    def test_all_pass_preserves_mode_and_counts(self):
        report = MODULE.score(self.dataset, self.results)
        self.assertEqual(report["check_summary"], {"passed": 3, "total": 3, "pass_rate": 1.0})
        self.assertEqual(report["mode"], "offline")
        self.assertTrue(report["all_critical_passed"])

    def test_missing_case_stays_in_denominator(self):
        self.results["cases"].pop()
        report = MODULE.score(self.dataset, self.results)
        self.assertEqual(report["case_summary"]["pass_rate"], 0.5)
        self.assertEqual(report["check_summary"]["total"], 3)
        self.assertFalse(report["all_critical_passed"])

    def test_missing_check_is_failure(self):
        self.results["cases"][0]["checks"].pop()
        report = MODULE.score(self.dataset, self.results)
        self.assertEqual(report["check_summary"]["passed"], 2)
        self.assertFalse(report["cases"][0]["passed"])

    def test_timeout_overrides_claimed_success(self):
        self.results["cases"][0]["error"] = "timeout"
        report = MODULE.score(self.dataset, self.results)
        self.assertEqual(report["check_summary"]["passed"], 1)
        self.assertEqual(report["cases"][0]["checks"][0]["evidence"], "timeout")

    def test_correct_fact_wrong_source_fails_case(self):
        self.results["cases"][0]["checks"][1]["passed"] = False
        report = MODULE.score(self.dataset, self.results)
        self.assertFalse(report["cases"][0]["passed"])

    def test_duplicate_cases_rejected(self):
        self.results["cases"].append(copy.deepcopy(self.results["cases"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            MODULE.score(self.dataset, self.results)

    def test_duplicate_checks_rejected(self):
        self.results["cases"][0]["checks"].append(copy.deepcopy(self.results["cases"][0]["checks"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate check"):
            MODULE.score(self.dataset, self.results)

    def test_unknown_case_rejected(self):
        self.results["cases"][0]["id"] = "invented"
        with self.assertRaisesRegex(ValueError, "unknown case"):
            MODULE.score(self.dataset, self.results)

    def test_different_dataset_rejected(self):
        self.results["dataset_id"] = "d2"
        with self.assertRaisesRegex(ValueError, "mismatch"):
            MODULE.score(self.dataset, self.results)

    def test_truthy_string_is_not_a_boolean(self):
        self.results["cases"][0]["checks"][0]["passed"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            MODULE.score(self.dataset, self.results)

    def test_evidence_is_required(self):
        self.results["cases"][0]["checks"][0]["evidence"] = " "
        with self.assertRaisesRegex(ValueError, "evidence"):
            MODULE.score(self.dataset, self.results)

    def test_empty_dataset_rejected(self):
        self.dataset["cases"] = []
        self.results["cases"] = []
        with self.assertRaisesRegex(ValueError, "at least one"):
            MODULE.score(self.dataset, self.results)

    def test_unknown_mode_rejected(self):
        self.results["mode"] = "real-probably"
        with self.assertRaisesRegex(ValueError, "mode"):
            MODULE.score(self.dataset, self.results)

    def test_non_string_mode_rejected(self):
        self.results["mode"] = []
        with self.assertRaisesRegex(ValueError, "mode"):
            MODULE.score(self.dataset, self.results)

    def test_duplicate_json_keys_rejected_before_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.json"
            path.write_text('{"passed":false,"passed":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                MODULE.read_json(path)

    def test_nonstandard_json_number_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nan.json"
            path.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonstandard"):
                MODULE.read_json(path)

    def test_cli_success_failure_invalid_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "dataset.json"
            results = Path(tmp) / "results.json"
            dataset.write_text(json.dumps(self.dataset), encoding="utf-8")
            for expected_code, data in [(0, self.results), (1, {**self.results, "cases": []}), (2, {})]:
                results.write_text(json.dumps(data), encoding="utf-8")
                run = subprocess.run([sys.executable, str(SCRIPT), str(dataset), str(results)],
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, expected_code, run.stderr)


if __name__ == "__main__":
    unittest.main()
