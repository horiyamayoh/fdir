"""Focused tests for the bounded issue #96 qualification lane."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest
import uuid

try:
    import qualification_issue96 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue96 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue96Tests(unittest.TestCase):
    @staticmethod
    def _output_dir() -> Path:
        path = qualification.ROOT / "e2e" / ".run" / f"qualification-issue96-test-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_corpus_is_authored_independent_and_has_required_negative_cases(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        self.assertTrue(corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(corpus["oracle"]["adapterHelpersUsedForExpected"])
        self.assertNotIn("adapter_docx.py::_resolve_styles", corpus["oracle"]["identity"])
        self.assertEqual(
            {fixture["format"] for fixture in corpus["fixtures"]},
            {"docx", "xlsx", "pdf", "markdown"},
        )
        self.assertGreaterEqual(len(corpus["fixtures"]), 5)
        negative_ids = {item["caseId"] for item in corpus["negativeCases"]}
        self.assertTrue(qualification.REQUIRED_NEGATIVE_CASES <= negative_ids)

    def test_authored_positive_oracle_round_trips_without_adapter_help(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        for fixture in corpus["fixtures"]:
            for projection in qualification.PROJECTIONS:
                expected = fixture["expected"][projection]
                result = qualification._compare_projection(expected, deepcopy(expected), projection)
                self.assertEqual(
                    result["status"],
                    "passed",
                    f"literal positive oracle did not self-qualify: {fixture['fixtureId']}:{projection}",
                )
                self.assertEqual(result["mismatchCount"], 0)

    def test_every_required_negative_mutation_is_detected(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        results = qualification._run_negative_mutations(corpus)
        self.assertEqual(len(results), len(corpus["negativeCases"]))
        self.assertEqual(
            [item["caseId"] for item in results if item["status"] != "passed"],
            [],
        )
        self.assertTrue(all(item["oracleMutationDetected"] for item in results))

    def test_report_structure_and_passes_current_qualified_target(self) -> None:
        output = self._output_dir()
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 0)
        reports = []
        report_projections = {
            "relationship-closure.json": "edges",
            "resource-availability.json": "resources",
            "annotation-link-field-report.json": "annotations",
            "revision-range-report.json": "revisions",
        }
        for report_name in qualification.REPORT_NAMES.values():
            report_path = output / report_name
            self.assertTrue(report_path.is_file(), report_name)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            reports.append(report)
            self.assertEqual(report["issueNumber"], 96)
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertIn("caseCounts", report)
            self.assertIn("assertions", report)
            self.assertIn("negativeDefectResults", report)
            self.assertEqual(report["negativeDefectFailureCount"], 0)
            self.assertEqual(report["completionStatus"], "incomplete-bounded-lane")
            self.assertEqual(report["status"], "passed")
            projection = report_projections[report_name]
            docx_result = next(
                item
                for item in report["details"][projection]
                if item["fixtureId"] == "docx-closure"
            )
            self.assertEqual(docx_result["status"], "passed", report_name)
            self.assertEqual(docx_result["mismatchCount"], 0, report_name)
        self.assertEqual({report["status"] for report in reports}, {"passed"})
        self.assertEqual(sum(report["mismatchCount"] for report in reports), 0)
        producer_path = output / "producer-report.json"
        self.assertTrue(producer_path.is_file())
        producer = json.loads(producer_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_producer_report_shape(producer), [])
        self.assertRegex(producer["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
        self.assertEqual(producer["uncoveredItems"], [])
        self.assertEqual(producer["unsupportedItems"], [])
        self.assertEqual(producer["waivedItems"], [])
        self.assertEqual(
            {item["caseId"] for item in producer["testCases"]},
            {item["testCaseId"] for item in producer["assertions"]},
        )
        self.assertTrue({item["classification"] for item in producer["testCases"]} >= {"positive", "mutation"})
        self.assertEqual(
            {item["assertionType"] for item in producer["assertions"]},
            {"relationship-closure", "mutation-killed"},
        )
        self.assertEqual(producer["status"], "passed")
        self.assertEqual(producer["failureCount"], 0)


if __name__ == "__main__":
    unittest.main()
