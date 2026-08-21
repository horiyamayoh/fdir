"""Focused tests for the strict issue #101 qualification lane."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import uuid
import unittest

try:
    import qualification_issue101 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue101 as qualification


class QualificationIssue101Tests(unittest.TestCase):
    @staticmethod
    def _output_dir() -> Path:
        path = qualification.ROOT / "e2e" / ".run" / f"qualification-issue101-test-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_corpus_declares_independent_source_and_all_required_reports(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        self.assertTrue(corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(corpus["oracle"]["adapterHelpersUsedForExpected"])
        self.assertGreaterEqual(len(corpus["fixtures"]), 2)
        self.assertEqual(corpus["reportNames"], qualification.REPORT_NAMES)
        self.assertTrue({88, 89, 91, 92, 94, 96} <= {
            item["issueNumber"] for item in corpus["bindings"]["requiredIssues"]
        })
        self.assertGreaterEqual(len(corpus["producerMatrix"]), 5)

    def test_independent_source_facts_match_authored_bytes_without_adapter_import(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        output = self._output_dir()
        for fixture in corpus["fixtures"]:
            path = output / "inputs" / f"{fixture['fixtureId']}.pdf"
            raw = qualification._write_authored_pdf(fixture, path)
            self.assertEqual(qualification._sha256_bytes(raw), fixture["sha256"])
            facts = qualification._source_facts(raw)
            self.assertEqual(qualification._compare(fixture["expectedSourceFacts"], facts), [], fixture["fixtureId"])

        tree = ast.parse((qualification.ROOT / "tools" / "qualification_issue101.py").read_text(encoding="utf-8"))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("adapter_pdf", imported_names)
        self.assertNotIn("adapter_common", imported_names)

    def test_all_negative_oracle_mutations_are_detected(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        results = qualification._run_negative_mutations(corpus)
        self.assertEqual(len(results), len(corpus["negativeCases"]))
        self.assertTrue(all(item["detected"] for item in results))
        self.assertTrue(all(item["status"] == "passed" for item in results))
        self.assertTrue(all(item["adapterMutationExecuted"] is False for item in results))

    def test_runner_passes_bounded_lanes_but_fails_closed_on_external_bindings(self) -> None:
        output = self._output_dir()
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 1)
        reports = []
        for report_name in qualification.REPORT_NAMES:
            path = output / report_name
            self.assertTrue(path.is_file(), report_name)
            report = json.loads(path.read_text(encoding="utf-8"))
            reports.append(report)
            self.assertEqual(report["issueNumber"], 101)
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertEqual(report["negativeDefectFailureCount"], 0, report_name)
            self.assertEqual(report["undetectedDefectCount"], 0, report_name)
            self.assertEqual(report["unaccountedOccurrenceCount"], 0, report_name)
            self.assertEqual(report["completionStatus"], "incomplete-strict-gate")
            self.assertEqual(report["status"], "failed")
            bounded = {item["fixtureId"]: item for item in report["fixtureResults"]}
            self.assertTrue(all(item["status"] == "passed" for item in bounded.values()), report_name)
            self.assertIn("PDF-101-REAL-PRODUCERS", report["unmetRequirements"])
            self.assertIn("PDF-101-EVIDENCE-BUNDLE", report["unmetRequirements"])
            self.assertIn("PDF-101-CI-BINDING", report["unmetRequirements"])
            differential = report["independentParserDifferential"]
            self.assertIn("independentFromAdapter", differential)
            self.assertEqual(differential["independentFromAdapter"], differential.get("available") is True)
            if differential.get("available") is not True:
                self.assertEqual(differential["status"], "failed")
                self.assertIn("PDF-101-MULTI-PARSER-RENDERER", report["unmetRequirements"])


if __name__ == "__main__":
    unittest.main()
