"""Focused, independent tests for the issue #95 bounded qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import unittest
import uuid

try:
    import qualification_issue95 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue95 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue95Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification.load_corpus()

    def test_authored_positive_manifests_are_internally_closed(self) -> None:
        self.assertEqual(
            {
                fixture["fixtureId"]: qualification._manifest_graph_findings(fixture)
                for fixture in self.corpus["fixtures"]
            },
            {fixture["fixtureId"]: [] for fixture in self.corpus["fixtures"]},
        )
        docx = next(item for item in self.corpus["fixtures"] if item["format"] == "docx")
        docx_table = next(item for item in docx["expected"]["tables"] if item["tableId"] == "table-docx-1")
        self.assertEqual(docx_table["gridColumns"], [1, 2, 3])
        self.assertEqual(docx_table["mergedRanges"][0]["masterCellId"], "node-docx-table-1-cell-2-1")
        self.assertEqual(docx_table["nestedTableIds"], ["table-docx-2"])

        xlsx = next(item for item in self.corpus["fixtures"] if item["format"] == "xlsx")
        sales = next(item for item in xlsx["expected"]["tables"] if item["tableId"] == "table-xlsx-0-Sales")
        self.assertEqual(sales["memberAddresses"], ["A1", "B1", "A2", "B2", "A3", "B3", "A4", "B4"])
        self.assertNotIn("F5", sales["memberAddresses"])

        markdown = next(item for item in self.corpus["fixtures"] if item["fixtureId"] == "markdown-gfm-table")
        markdown_table = markdown["expected"]["tables"][0]
        self.assertEqual(markdown_table["separatorLines"], [2])
        self.assertNotIn(2, markdown_table["rowSourceLines"])

    def test_every_required_negative_mutation_is_detected_by_authored_oracle(self) -> None:
        results = qualification.run_oracle_mutations(self.corpus)
        self.assertEqual(len(results), 13)
        self.assertEqual({item["caseId"] for item in results}, {item["id"] for item in self.corpus["negativeCases"]})
        self.assertTrue(all(item["detected"] and item["status"] == "passed" for item in results))
        self.assertEqual(
            {item["expectedDefectCode"] for item in results},
            {
                "CONTAINMENT-PARENT-MISMATCH",
                "TABLE-RANGE-MEMBER-MISMATCH",
                "MERGED-FOLLOWER-MASTER",
                "MARKDOWN-SEPARATOR-DATA",
                "XLSX-WHOLE-SHEET-TABLE",
                "XLSX-EMPTY-STYLED-OMITTED",
                "STORY-ROOT-WRONG-PARENT",
                "PDF-PAGE-TREE-ORDER",
                "UNPARSED-PART-PRESERVED",
                "ORPHAN",
                "CYCLE",
                "MULTI-PARENT",
                "RECIPROCITY-MISMATCH",
            },
        )

    def test_corpus_declares_runtime_independent_oracle(self) -> None:
        oracle = self.corpus["oracle"]
        self.assertTrue(oracle["expectedTopologyIsAuthored"])
        self.assertTrue(oracle["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(oracle["adapterHelpersUsedForExpected"])
        source = (qualification.ROOT / "tools" / "qualification_issue95.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, re.compile(r"from\s+adapter_(?:docx|xlsx|pdf|markdown)"))

    def test_docx_text_box_layout_is_linked_to_its_page_surface(self) -> None:
        fixture = next(item for item in self.corpus["fixtures"] if item["fixtureId"] == "docx-parts-stories-grid")
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue95-layout-test-{uuid.uuid4().hex[:12]}"
        output.mkdir(parents=True, exist_ok=False)
        try:
            run = qualification._run_converter(fixture, output)
            self.assertEqual(run["returnCode"], 0)
            document = run["document"]
            self.assertIsInstance(document, dict)
            text_box = next(item for item in document["nodes"] if item.get("kind") == "textBox")
            self.assertEqual(text_box["layoutIds"], ["layout-docx-node-docx-textBox-1-4"])
            layout = next(item for item in document["layouts"] if item["layoutId"] == text_box["layoutIds"][0])
            self.assertEqual(layout["surfaceId"], "surface-docx-page-1")
            page = next(item for item in document["surfaces"] if item["surfaceId"] == "surface-docx-page-1")
            self.assertIn(layout["layoutId"], page["layoutIds"])
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_runner_emits_all_reports_and_passes_current_qualified_target(self) -> None:
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue95-test-{uuid.uuid4().hex[:12]}"
        output.mkdir(parents=True, exist_ok=False)
        try:
            exit_code = qualification.run_qualification(out_dir=output)
            self.assertEqual(exit_code, 0)
            for report_name in qualification.REPORT_NAMES.values():
                report_path = output / report_name
                self.assertTrue(report_path.is_file(), report_name)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["issueNumber"], 95)
                self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
                self.assertIn("caseCounts", report)
                self.assertIn("assertions", report)
                self.assertIn("negativeDefectResults", report)
                self.assertEqual(report["caseCounts"]["negativeUndetected"], 0)
                self.assertEqual(len(report["negativeDefectResults"]), 13)
                self.assertTrue(all(item["status"] == "passed" for item in report["negativeDefectResults"]))
                self.assertEqual(report["status"], "passed")
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
            self.assertEqual(producer["status"], "passed")
            self.assertEqual(producer["failureCount"], 0)
            self.assertEqual(
                {item["assertionType"] for item in producer["assertions"]},
                {"topology", "mutation-killed"},
            )
        finally:
            shutil.rmtree(output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
