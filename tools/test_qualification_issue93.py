"""Focused tests for the bounded issue #93 qualification slice."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest
import uuid

try:
    import qualification_issue93 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue93 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue93Tests(unittest.TestCase):
    @staticmethod
    def _output_dir() -> Path:
        return (
            qualification.ROOT
            / "e2e"
            / ".run"
            / f"qualification-issue93-test-{uuid.uuid4().hex[:10]}"
        )

    @staticmethod
    def _first_case() -> dict:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        return corpus["fixtures"][0]["cases"][0]

    def test_corpus_declares_literal_independent_oracle(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        self.assertTrue(corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertNotIn(
            "tools/adapter_docx.py::_resolve_styles",
            corpus["oracle"]["identity"],
        )
        self.assertGreaterEqual(len(corpus["fixtures"]), 2)

    def test_expected_candidate_passes_without_importing_adapter_helpers(self) -> None:
        case = self._first_case()
        expected = case["expected"]
        candidate = {
            "styles": [
                {
                    "styleId": case["target"]["styleId"],
                    "origin": "resolved",
                    "resolved": deepcopy(expected["properties"]),
                    "propertyProvenance": [
                        {
                            "property": name,
                            "source": source,
                            "status": "preserved",
                        }
                        for name, source in expected["provenance"].items()
                    ],
                    "cascadeTrace": deepcopy(expected["trace"]),
                }
            ]
        }
        result = qualification._evaluate_case(case, candidate)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["propertyMismatchCount"], 0)
        self.assertEqual(result["provenanceMissingCount"], 0)
        self.assertEqual(result["traceMismatchCount"], 0)

    def test_property_mutation_is_detected(self) -> None:
        case = self._first_case()
        candidate = {
            "styles": [
                {
                    "styleId": case["target"]["styleId"],
                    "origin": "resolved",
                    "resolved": deepcopy(case["expected"]["properties"]),
                    "propertyProvenance": [
                        {
                            "property": name,
                            "source": source,
                            "status": "preserved",
                        }
                        for name, source in case["expected"]["provenance"].items()
                    ],
                    "cascadeTrace": deepcopy(case["expected"]["trace"]),
                }
            ]
        }
        candidate["styles"][0]["resolved"]["weight"] = 400
        result = qualification._evaluate_case(case, candidate)
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["propertyMismatchCount"], 0)
        self.assertTrue(
            any(item["kind"] == "property-mismatch" for item in result["failures"])
        )

    def test_provenance_and_trace_mutations_are_detected(self) -> None:
        case = self._first_case()
        candidate = {
            "styles": [
                {
                    "styleId": case["target"]["styleId"],
                    "origin": "resolved",
                    "resolved": deepcopy(case["expected"]["properties"]),
                    "propertyProvenance": [],
                    "cascadeTrace": [],
                }
            ]
        }
        result = qualification._evaluate_case(case, candidate)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["provenanceMissingCount"],
            len(case["expected"]["provenance"]),
        )
        self.assertEqual(result["traceMismatchCount"], 1)

    def test_cycle_and_missing_source_counts_are_fail_closed(self) -> None:
        document = {
            "styles": [
                {"styleId": "a", "basedOn": "b"},
                {"styleId": "b", "basedOn": "a"},
                {"styleId": "dangling", "basedOn": "missing"},
            ]
        }
        counts = qualification._graph_counts(document)
        self.assertEqual(counts["cycleCount"], 1)
        self.assertEqual(counts["missingSourceCount"], 1)

    def test_current_adapters_and_reports_record_counts(self) -> None:
        output = self._output_dir()
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 0)
        reports = []
        for name in qualification.REPORT_NAMES.values():
            report = json.loads((output / name).read_text(encoding="utf-8"))
            reports.append(report)
            self.assertEqual(report["status"], "passed")
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertIn("limitations", report)
            self.assertIn("unmet", report)
        producer = json.loads((output / qualification.PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([], validate_producer_report_shape(producer))
        expected_case_count = sum(len(fixture["cases"]) for fixture in self._corpus_fixtures()) + 1
        self.assertEqual(expected_case_count, len(producer["testCases"]))
        self.assertEqual(
            {case["caseId"] for case in producer["testCases"]},
            {assertion["testCaseId"] for assertion in producer["assertions"]},
        )
        self.assertEqual(
            {"positive", "mutation"},
            {case["classification"] for case in producer["testCases"]},
        )
        self.assertEqual("passed", producer["status"])
        self.assertEqual(
            sum(assertion["status"] != "passed" for assertion in producer["assertions"])
            + sum(case["result"] != "passed" for case in producer["testCases"]),
            producer["failureCount"],
        )
        self.assertEqual(0, sum(report["propertyMismatchCount"] for report in reports))
        self.assertEqual(
            sum(report["provenanceMissingCount"] for report in reports),
            0,
        )

    @staticmethod
    def _corpus_fixtures() -> list[dict]:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        return corpus["fixtures"]


if __name__ == "__main__":
    unittest.main()
