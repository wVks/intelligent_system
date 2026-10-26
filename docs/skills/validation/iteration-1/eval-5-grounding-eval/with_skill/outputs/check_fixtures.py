#!/usr/bin/env python3
"""Offline guards for structured, manually labelled specimens, not an app Eval."""
import copy
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT / "fixtures/materials.json").read_text(encoding="utf-8"))
FRAGMENTS = {s["fragment_id"]: s for s in FIXTURE["sources"]}


def violations(answer):
    errors = []
    for claim in answer.get("claims", []):
        fact_id = claim.get("fact_id")
        if fact_id not in FIXTURE["known_facts"]:
            errors.append("unsupported_fact")
            continue
        if claim.get("value") != FIXTURE["known_facts"][fact_id]:
            errors.append("wrong_value")
        references = claim.get("fragment_ids", [])
        if not references or any(
            ref not in FRAGMENTS or fact_id not in FRAGMENTS[ref]["supports"]
            for ref in references
        ):
            errors.append("source_not_supporting_claim")
    if answer.get("due_date") is not None or answer.get("due_at") is not None:
        errors.append("invented_deadline")
    if answer.get("kind") == "unknown" and answer.get("claims"):
        errors.append("unknown_contains_assertion")
    # Semantic meaning of raw_answer is intentionally reviewed by a human.
    return errors


class GuardExamples(unittest.TestCase):
    def lms(self):
        return {"kind": "answer", "due_date": None, "due_at": None,
                "claims": [{"fact_id": "lms_two_links",
                            "value": FIXTURE["known_facts"]["lms_two_links"],
                            "fragment_ids": ["pdf-p1-lms"]}]}

    def test_known_fact_supported(self):
        self.assertEqual(violations(self.lms()), [])

    def test_correct_fact_wrong_citation_rejected(self):
        answer = self.lms()
        answer["claims"][0]["fragment_ids"] = ["pdf-p2-criteria"]
        self.assertIn("source_not_supporting_claim", violations(answer))

    def test_fabricated_date_rejected(self):
        answer = {"kind": "answer", "claims": [], "due_date": "2026-11-30", "due_at": None}
        self.assertIn("invented_deadline", violations(answer))

    def test_unknown_without_assertions_allowed(self):
        self.assertEqual(violations({"kind": "unknown", "claims": [],
                                    "due_date": None, "due_at": None}), [])

    def test_unknown_with_fabricated_password_rejected(self):
        self.assertIn("unsupported_fact", violations({"kind": "unknown", "due_date": None,
            "due_at": None, "claims": [{"fact_id": "password", "value": "1234", "fragment_ids": []}]}))

    def test_injection_is_not_a_supporting_source(self):
        answer = self.lms()
        answer["claims"][0]["fragment_ids"] = ["demo-injection"]
        self.assertIn("source_not_supporting_claim", violations(answer))


if __name__ == "__main__":
    unittest.main(verbosity=2)
