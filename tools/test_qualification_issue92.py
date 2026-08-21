"""Focused positive and mutation tests for the issue #92 qualification slice."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

try:
    import qualification_issue92 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue92 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover - package-style test execution.
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue92Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)

    def test_corpus_declares_independent_expected_authority(self) -> None:
        self.assertFalse(self.corpus["oracle"]["adapterHelpersUsedForExpected"])
        self.assertEqual(self.corpus["oracle"]["expectedValues"], "checked-in-authored-vectors")
        self.assertEqual(set(self.corpus["lanes"]), set(qualification.LANES))
        self.assertTrue(all(self.corpus["lanes"][lane] for lane in qualification.LANES))

    def test_positive_compare_accepts_an_authored_exact_projection(self) -> None:
        case = self.corpus["lanes"]["scalar"][0]
        actual = json.loads(json.dumps(case["expected"], ensure_ascii=False))
        self.assertEqual(qualification._compare(case["expected"], actual), [])

    def test_mutation_suite_detects_every_declared_drift(self) -> None:
        by_id = {
            case["id"]: case
            for lane in qualification.LANES
            for case in self.corpus["lanes"][lane]
        }
        for mutation in self.corpus["mutations"]:
            expected = by_id[mutation["caseId"]]["expected"]
            mutated = json.loads(json.dumps(expected, ensure_ascii=False))
            qualification._set_path(mutated, mutation["path"], mutation["mutatedValue"])
            mismatches = qualification._compare(expected, mutated)
            self.assertTrue(mismatches, mutation["id"])
            self.assertEqual(mismatches[0]["path"], "$/" + mutation["path"].replace(".", "/"))

    def test_fabricated_preserved_is_counted_for_missing_cmap(self) -> None:
        expected = self.corpus["lanes"]["glyph"][1]["expected"]
        actual = dict(expected)
        actual["unicode"] = "\u0080"
        self.assertEqual(qualification._fabricated_preserved_count("glyph", expected, actual), 1)

    def test_actual_runner_writes_all_reports_and_fails_honestly(self) -> None:
        output = qualification.ROOT / "e2e" / ".run" / "qualification-issue-92-test-output"
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 1)
        for name in qualification.REPORT_NAMES.values():
            report = json.loads((output / name).read_text(encoding="utf-8"))
            self.assertIn(report["status"], {"passed", "failed"})
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertIn("assertions", report)
            self.assertIn("fabricatedPreservedCount", report)
            self.assertIn("unmetRequirements", report)
        producer = json.loads((output / qualification.PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([], validate_producer_report_shape(producer))
        expected_case_count = sum(
            len(self.corpus["lanes"][lane])
            for lane in qualification.LANES
        ) + len(self.corpus["mutations"])
        self.assertEqual(expected_case_count, len(producer["testCases"]))
        self.assertEqual(5 + expected_case_count, len(producer["assertions"]))
        self.assertEqual(
            {case["caseId"] for case in producer["testCases"]},
            {assertion["testCaseId"] for assertion in producer["assertions"]},
        )
        self.assertEqual(
            {"positive", "mutation"},
            {case["classification"] for case in producer["testCases"]},
        )
        self.assertEqual("passed", producer["status"])
        self.assertEqual(0, producer["failureCount"])
        text_report = json.loads((output / qualification.REPORT_NAMES["text"]).read_text(encoding="utf-8"))
        self.assertEqual(
            text_report["mismatchCount"],
            sum(len(case.get("mismatches", [])) for case in text_report["cases"]),
        )
        self.assertEqual(0, text_report["mismatchCount"])
        glyph_report = json.loads((output / qualification.REPORT_NAMES["glyph"]).read_text(encoding="utf-8"))
        self.assertEqual(glyph_report["status"], "passed")
        self.assertEqual(glyph_report["mismatchCount"], 0)
        self.assertEqual(glyph_report["fabricatedPreservedCount"], 0)


if __name__ == "__main__":
    unittest.main()
