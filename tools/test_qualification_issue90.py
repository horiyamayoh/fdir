"""Focused tests for the fail-closed issue #90 qualification runner."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
from unittest import mock
import uuid

try:
    import qualification_issue90 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue90 as qualification


class QualificationIssue90Tests(unittest.TestCase):
    @staticmethod
    def _output_dir() -> Path:
        return (
            qualification.ROOT
            / "e2e"
            / ".run"
            / f"qualification-issue90-test-{uuid.uuid4().hex[:10]}"
        )

    def test_selector_and_index_mutations_are_applied_without_runtime_help(self) -> None:
        document = qualification._read_json(qualification.ROOT / "examples" / "markdown-authoring.json")
        qualification._apply_operation(
            document,
            {
                "op": "set",
                "path": "nodes[nodeId=node-run].textIds[0]",
                "value": "node-document",
            },
        )
        self.assertEqual(document["nodes"][2]["textIds"], ["node-document"])

        qualification._apply_operation(
            document,
            {
                "op": "set",
                "path": "nodes[nodeId=node-run]",
                "field": "qualificationMarker",
                "value": True,
            },
        )
        self.assertTrue(document["nodes"][2]["qualificationMarker"])

    def test_external_schema_report_has_zero_differential_mismatches(self) -> None:
        output = self._output_dir()
        exit_code = qualification.run_qualification(out_dir=output)
        schema_report = json.loads(
            (output / "schema-differential.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema_report["status"], "passed")
        self.assertEqual(schema_report["mismatchCount"], 0)
        self.assertEqual(
            schema_report["validator"]["class"], "Draft202012Validator"
        )
        self.assertGreater(schema_report["positiveCaseCount"], 0)
        self.assertGreater(schema_report["negativeCaseCount"], 0)
        self.assertRegex(schema_report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
        # The current runtime deliberately exposes the remaining #90 gaps.  A
        # non-zero process result is required until those gaps are fixed; a
        # passing schema report must not mask them.
        self.assertEqual(exit_code, 1)

    def test_graph_and_status_gaps_are_reported_as_failures(self) -> None:
        output = self._output_dir()
        qualification.run_qualification(out_dir=output)
        graph_report = json.loads(
            (output / "graph-invariants.json").read_text(encoding="utf-8")
        )
        status_report = json.loads(
            (output / "status-contract.json").read_text(encoding="utf-8")
        )

        self.assertEqual(graph_report["status"], "failed")
        self.assertIn("cardinality", graph_report["coverage"]["missingCategories"])
        relation_case = next(
            case
            for case in graph_report["cases"]
            if case["caseId"] == "relation-endpoint-kind"
        )
        self.assertFalse(relation_case["assertions"]["expectedDiagnostic"])
        self.assertIsNone(relation_case["runtime"].get("diagnostic"))

        self.assertEqual(status_report["status"], "failed")
        self.assertIn("status-enum", status_report["coverage"]["missingCategories"])
        status_case = next(
            case
            for case in status_report["cases"]
            if case["caseId"] == "complete-with-warnings-without-evidence"
        )
        self.assertTrue(status_case["assertions"]["expectedDiagnostic"])
        self.assertFalse(status_case["assertions"]["minimumFailingPath"])

    def test_missing_external_validator_writes_fail_closed_reports(self) -> None:
        output = self._output_dir()
        with mock.patch.object(
            qualification,
            "_load_external_validator",
            side_effect=qualification.ExternalValidatorUnavailable("not installed"),
        ):
            exit_code = qualification.run_qualification(out_dir=output)

        self.assertEqual(exit_code, 1)
        for name in qualification.REPORT_NAMES.values():
            report = json.loads((output / name).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failure"]["code"], "QUALIFICATION-SETUP-FAILED")


if __name__ == "__main__":
    unittest.main()
