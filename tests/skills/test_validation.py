"""Regression tests for false success found by independent tool review."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("validate_skills", ROOT / "tools/validate_skills.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill = self.root / ".agents/skills/study-eval"
        shutil.copytree(ROOT / ".agents/skills/study-eval", self.skill)
        # Keep the fixture portable while retaining its real skill structure.
        text = (self.skill / "SKILL.md").read_text()
        text = text.replace("[контекст проекта](../../../docs/project-brief.md)", "контекст проекта")
        (self.skill / "SKILL.md").write_text(text)

    def test_valid_project_passes(self):
        self.assertEqual(MODULE.validate_project(self.root)[1], [])

    def test_directory_without_skill_is_not_silently_skipped(self):
        (self.root / ".agents/skills/missing-skill").mkdir()
        errors = MODULE.validate_project(self.root)[1]
        self.assertTrue(any("missing SKILL.md" in e for e in errors), errors)

    def test_ui_requires_interface_mapping(self):
        path = self.skill / "agents/openai.yaml"
        path.write_text(path.read_text().replace("interface:\n", ""))
        errors = MODULE.validate(self.skill, self.root)
        self.assertTrue(any("interface" in e for e in errors), errors)

    def test_duplicate_ui_keys_rejected(self):
        path = self.skill / "agents/openai.yaml"
        path.write_text(path.read_text() + '  display_name: "Duplicate"\n')
        self.assertTrue(any("duplicate UI" in e for e in MODULE.validate(self.skill, self.root)))

    def test_wrong_eval_types_rejected(self):
        path = self.skill / "evals/evals.json"
        data = json.loads(path.read_text())
        data["evals"][0].update(prompt=123, expected_output=["bad"], expectations="wrong")
        path.write_text(json.dumps(data))
        errors = MODULE.validate(self.skill, self.root)
        self.assertTrue(any("incomplete eval" in e for e in errors), errors)
        self.assertTrue(any("list of strings" in e for e in errors), errors)

    def test_broken_reference_rejected(self):
        path = self.skill / "SKILL.md"
        path.write_text(path.read_text() + "\n[missing](references/missing.md)\n")
        self.assertTrue(any("broken/escaping" in e for e in MODULE.validate(self.skill, self.root)))


if __name__ == "__main__":
    unittest.main()
