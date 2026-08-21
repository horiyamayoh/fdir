"""Dependency-free contract tests for the issue #105 qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import qualification_issue105 as issue105  # noqa: E402


class Issue105ContractTests(unittest.TestCase):
    def test_authored_corpus_covers_release_scope_and_required_reports(self) -> None:
        corpus = issue105.load_json(ROOT / "machine" / "qualification-issue-105-corpus.json")
        issue105.validate_corpus(corpus)
        self.assertEqual(corpus["releaseScopeIssues"], list(range(88, 105)))
        self.assertEqual(len(corpus["releaseEvidence"]), 17)
        self.assertEqual(tuple(corpus["reportNames"]), issue105.REPORT_NAMES)

    def test_report_cannot_pass_from_a_dirty_tree(self) -> None:
        result = issue105.report(
            "requirement-traceability.json",
            "0" * 40,
            [" M tools/example.py"],
            assertions=[issue105._assertion("authored", True, True)],
            cases=[],
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["dirtyTree"])

    def test_case_requires_semantic_oracle_fields(self) -> None:
        result = issue105.public_case(
            "case-1",
            "compare structured output and filesystem side effects",
            {"status": "passed", "inputDigest": "a" * 64, "durationMilliseconds": 1, "diagnostics": []},
            target="cli",
            expected={"status": "passed"},
            actual={"status": "passed"},
        )
        self.assertTrue(result["oracle"])
        self.assertEqual(result["inputDigest"], "a" * 64)
        self.assertIn("assertionIds", result)

    def test_schema_is_draft_2020_and_report_names_are_unique(self) -> None:
        schema = json.loads((ROOT / "schemas" / "qualification-issue-105-report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(len(schema["properties"]["reportName"]["enum"]), len(set(schema["properties"]["reportName"]["enum"])))

    def test_public_cli_starts_from_external_isolated_working_directory(self) -> None:
        scratch = ROOT / "e2e" / ".run"
        scratch.mkdir(parents=True, exist_ok=True)
        external = scratch / "issue-105-external-workdir"
        external.mkdir(parents=True, exist_ok=True)
        result = issue105.run_command(
            [sys.executable, "-I", str(ROOT / "tools" / "convert_document.py"), "--help"],
            expected_exit=0,
            timeout=30,
            cwd=external,
            input_paths=[ROOT / "tools" / "convert_document.py"],
        )
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
