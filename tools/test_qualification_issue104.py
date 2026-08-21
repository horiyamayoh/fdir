"""Focused tests for the independent issue #104 qualification boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import unittest
import uuid

try:
    import qualification_issue104 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue104 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue104Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification._load_corpus(qualification.DEFAULT_CORPUS_PATH)

    @staticmethod
    def _output_dir(label: str) -> Path:
        return qualification.ROOT / "e2e" / ".run" / f"qualification-issue-104-test-{label}-{uuid.uuid4().hex[:10]}"

    def test_corpus_has_authored_oracle_grades_and_all_required_reports(self) -> None:
        self.assertEqual(self.corpus["reportNames"], list(qualification.REPORT_NAMES.values()))
        self.assertTrue(self.corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertTrue(self.corpus["oracle"]["expectedFactsAreAuthored"])
        self.assertFalse(self.corpus["oracle"]["adapterOutputUsedForExpected"])
        self.assertFalse(self.corpus["oracle"]["adapterOutputUsedToCreateCorpus"])
        self.assertGreaterEqual(len(self.corpus["fixtures"]), 4)
        self.assertTrue(all(item["independenceGrade"] in {"A", "B", "C"} for item in self.corpus["fixtures"]))
        required_missing = [
            item for item in self.corpus["producerMatrix"]
            if item["required"] and item["availability"] == "missing"
        ]
        self.assertGreaterEqual(len(required_missing), 4)
        self.assertTrue(all(item["missingReason"] for item in required_missing))
        self.assertTrue(self.corpus["gradePolicy"]["gradeDIsNonQualifying"])

    def test_source_digest_and_oracle_are_checked_without_adapter_import(self) -> None:
        for fixture in self.corpus["fixtures"]:
            source = qualification._safe_repo_path(fixture["source"]["path"])
            actual = qualification._actual_source_digest(source)
            self.assertEqual(qualification._digest_mismatches(fixture["sourceDigest"], actual), [], fixture["fixtureId"])
            actual_oracle = qualification._sha256_text(qualification._canonical(fixture["expectedFacts"]))
            self.assertEqual(actual_oracle, fixture["oracleDigest"], fixture["fixtureId"])

        import_audit = qualification._runner_import_audit()
        self.assertTrue(import_audit["independent"])
        self.assertEqual(import_audit["adapterImports"], [])
        self.assertFalse(import_audit["expectedFactsFromAdapterOutput"])

    def test_legacy_manifest_is_explicitly_grade_d_and_non_qualifying(self) -> None:
        report = qualification._legacy_manifest_audit(self.corpus)
        self.assertEqual(report["grade"], "D")
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["selfDeclaredIndependent"])
        self.assertFalse(report["producerMatrixPresent"])
        self.assertFalse(report["expectedFactOraclePresent"])
        self.assertTrue(report["scopeAssessment"]["small"])
        self.assertTrue(report["scopeAssessment"]["generic"])
        self.assertGreaterEqual(len(report["reasons"]), 3)

    def test_grade_d_fixture_is_rejected_by_corpus_loader(self) -> None:
        mutated = copy.deepcopy(self.corpus)
        mutated["fixtures"][0]["independenceGrade"] = "D"
        directory = self._output_dir("invalid-corpus")
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "corpus.json"
        try:
            path.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(qualification.QualificationError):
                qualification._load_corpus(path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_runner_reports_bounded_scope_but_fails_on_missing_official_corpus(self) -> None:
        output = self._output_dir("runner")
        try:
            exit_code = qualification.run_qualification(out_dir=output)
            self.assertEqual(exit_code, 1)
            for report_name in qualification.REPORT_NAMES.values():
                report_path = output / report_name
                self.assertTrue(report_path.is_file(), report_name)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["issueNumber"], 104)
                self.assertEqual(report["status"], "failed", report_name)
                self.assertEqual(report["completionStatus"], "incomplete-strict-gate")
                self.assertTrue(report["implementedScope"])

            digest = json.loads((output / qualification.REPORT_NAMES["digests"]).read_text(encoding="utf-8"))
            self.assertEqual(digest["sectionStatus"], "passed")
            self.assertEqual(digest["mismatchCount"], 0)

            metamorphic = json.loads((output / qualification.REPORT_NAMES["metamorphic"]).read_text(encoding="utf-8"))
            self.assertEqual(metamorphic["sectionStatus"], "passed")
            self.assertEqual(metamorphic["failedRelationCount"], 0)

            hostile = json.loads((output / qualification.REPORT_NAMES["hostile"]).read_text(encoding="utf-8"))
            self.assertEqual(hostile["sectionStatus"], "passed")
            self.assertEqual(hostile["failedCaseCount"], 0)

            producers = json.loads((output / qualification.REPORT_NAMES["producers"]).read_text(encoding="utf-8"))
            self.assertTrue(producers["requiredUnavailable"])
            self.assertTrue(all(item["status"] == "unavailable" for item in producers["producerMatrix"] if item["required"]))
            self.assertTrue(all(item["missingReason"] for item in producers["producerMatrix"] if item["status"] == "unavailable"))

            coverage = json.loads((output / qualification.REPORT_NAMES["coverage"]).read_text(encoding="utf-8"))
            self.assertIn("QUAL-104-OFFICIAL-CORPUS", coverage["unmetRequirements"])
            self.assertIn("QUAL-104-PRODUCER-MATRIX", coverage["unmetRequirements"])
            self.assertIn("QUAL-104-DIFFERENTIAL-ADJUDICATION", coverage["unmetRequirements"])
            self.assertFalse(coverage["oracleImportAudit"]["adapterImports"])
            self.assertTrue(next(item for item in coverage["assertions"] if item["id"] == "no-grade-d-only-pass")["status"] == "passed")

            producer = json.loads((output / qualification.PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
            self.assertEqual(validate_producer_report_shape(producer), [])
            self.assertEqual(producer["status"], "blocked")
            self.assertEqual(producer["failureCount"], 0)
            self.assertTrue(producer["uncoveredItems"])
            self.assertTrue(any("required official producer unavailable" in item for item in producer["uncoveredItems"]))
            self.assertEqual({item["caseId"] for item in producer["testCases"]}, {item["testCaseId"] for item in producer["assertions"]})
        finally:
            shutil.rmtree(output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
