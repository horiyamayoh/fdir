"""Focused tests for the bounded issue #97 qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
import uuid

try:
    import qualification_issue97 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue97 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue97Tests(unittest.TestCase):
    def test_corpus_is_authored_independent_and_exhaustive(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        self.assertTrue(corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(corpus["oracle"]["adapterHelpersUsedForExpected"])
        self.assertEqual(corpus["reportNames"], list(qualification.REPORT_NAMES.values()))
        self.assertEqual(len(corpus["expectedRegistry"]), len(corpus["schemaCases"]))
        self.assertGreaterEqual(len(corpus["emissionSites"]), 20)
        negative_ids = {item["caseId"] for item in corpus["negativeMutations"]}
        self.assertTrue(qualification.REQUIRED_NEGATIVE_CASES <= negative_ids)
        source = Path(qualification.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from adapter_docx", source)
        self.assertNotIn("from adapter_xlsx", source)
        self.assertNotIn("from adapter_pdf", source)
        self.assertNotIn("from adapter_markdown", source)

    def test_static_emission_selector_is_line_tolerant_but_exhaustive(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        registry = qualification._read_json(qualification.REGISTRY_PATH)
        evidence = qualification._emission_evidence(corpus, registry)
        if evidence["authoredSiteCount"] == evidence["observedSiteCount"]:
            self.assertEqual(evidence["missingAuthoredSites"], [])
            self.assertEqual(evidence["unexpectedObservedSites"], [])
        else:
            self.assertTrue(evidence["missingAuthoredSites"] or evidence["unexpectedObservedSites"])
        self.assertEqual(evidence["unregisteredEmissionKeys"], [])
        self.assertIn("emissionSelectorLineDrift", evidence)

    def test_runtime_emission_and_diagnostic_links_are_qualified_without_source_bytes(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        registry = qualification._read_json(qualification.REGISTRY_PATH)
        evidence = qualification._emission_evidence(corpus, registry)
        runtime = evidence["runtimeEmission"]
        self.assertEqual(runtime["status"], "passed")
        self.assertEqual(runtime["failureCount"], 0)
        self.assertEqual(runtime["caseCount"], 4)
        self.assertTrue(any(item["format"] == "markdown" for item in runtime["cases"]))
        pdf_case = next(item for item in runtime["cases"] if item["format"] == "pdf")
        self.assertIn("glyph-provenance", pdf_case["observedExtensionTypes"])
        self.assertIn("font-cmap", pdf_case["observedExtensionTypes"])
        self.assertIn("graphics-state", pdf_case["observedExtensionTypes"])
        self.assertIn("DFIR-PDF-GLYPH-MAPPING-UNAVAILABLE", pdf_case["observedDiagnosticCodes"])
        self.assertIn("DFIR-PDF-FONT-CMAP-UNAVAILABLE", pdf_case["observedDiagnosticCodes"])

    def test_every_authored_negative_mutation_is_detected(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        results = qualification._run_negative_mutations(corpus)
        self.assertEqual(len(results), len(corpus["negativeMutations"]))
        self.assertEqual([item["caseId"] for item in results if item["status"] != "passed"], [])
        self.assertTrue(all(item["oracleMutationDetected"] for item in results))

    def test_runner_writes_all_reports_and_fails_closed_on_current_implementation(self) -> None:
        corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue97-test-{uuid.uuid4().hex[:10]}"
        output.mkdir(parents=True, exist_ok=True)
        try:
            exit_code = qualification.run_qualification(out_dir=output)
            reports = []
            for report_name in qualification.REPORT_NAMES.values():
                path = output / report_name
                self.assertTrue(path.is_file(), report_name)
                report = json.loads(path.read_text(encoding="utf-8"))
                reports.append(report)
                self.assertEqual(report["issueNumber"], 97)
                self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
                self.assertGreater(report["authoredCaseCount"], 0)
                self.assertTrue(report["assertions"])
                self.assertTrue(report["negativeMutationResults"])
                self.assertEqual(report["negativeMutationFailureCount"], 0)
                self.assertEqual(report["completionStatus"], "incomplete-bounded-lane")
                self.assertEqual(report["status"], "failed" if report["failureSummary"] else "passed")
            self.assertEqual(len(reports), 5)
            expected_exit = 1 if any(report.get("failureSummary") for report in reports) else 0
            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(any(report["status"] == "failed" for report in reports), expected_exit == 1)
            producer_path = output / "producer-report.json"
            self.assertTrue(producer_path.is_file())
            producer = json.loads(producer_path.read_text(encoding="utf-8"))
            self.assertEqual(validate_producer_report_shape(producer), [])
            self.assertRegex(producer["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertEqual(producer["uncoveredItems"], [])
            self.assertEqual(producer["unsupportedItems"], [])
            self.assertEqual(producer["waivedItems"], [])
            self.assertEqual(producer["status"], "passed")
            self.assertEqual(producer["failureCount"], 0)
            self.assertTrue({item["classification"] for item in producer["testCases"]} >= {"positive", "mutation"})
            self.assertEqual(
                {item["caseId"] for item in producer["testCases"]},
                {item["testCaseId"] for item in producer["assertions"]},
            )
            self.assertEqual(
                {item["assertionType"] for item in producer["assertions"]},
                {"extension-closure", "mutation-killed"},
            )
        finally:
            # Keep the workspace-local evidence available for failure diagnosis.
            pass


if __name__ == "__main__":
    unittest.main()
