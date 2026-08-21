"""Focused tests for the independent issue #100 XLSX qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import unittest
import uuid

try:
    import qualification_issue100 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue100 as qualification


class QualificationIssue100Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification.load_corpus()

    @staticmethod
    def _output_dir(label: str) -> Path:
        path = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-100-test-{label}-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_corpus_binds_all_required_reports_and_external_producers(self) -> None:
        self.assertEqual(list(qualification.REPORT_NAMES.values()), self.corpus["reportNames"])
        matrix = {item["producerId"]: item for item in self.corpus["producerMatrix"]}
        self.assertTrue(set(qualification.REQUIRED_PRODUCERS).issubset(matrix))
        self.assertTrue(all(matrix[item]["required"] for item in qualification.REQUIRED_PRODUCERS))
        self.assertGreaterEqual(len(self.corpus["fixtures"]), 2)
        self.assertTrue(self.corpus["oracle"]["expectedFactsAreAuthored"])
        self.assertFalse(self.corpus["oracle"]["adapterHelpersUsedForExpected"])
        self.assertNotIn("requiredUnmetRequirements", self.corpus)
        self.assertTrue(all(isinstance(item.get("provenance"), dict) for item in matrix.values()))
        self.assertTrue(all(matrix[item]["provenance"]["availability"] == "missing" for item in qualification.REQUIRED_PRODUCERS))
        self.assertTrue(self.corpus["defectCampaign"]["status"] == "missing")

    def test_source_oracle_reads_real_ooxml_without_adapter_import(self) -> None:
        fixture = next(item for item in self.corpus["fixtures"] if item["fixtureId"] == "xlsx-profile-rich-authored")
        output = self._output_dir("source")
        try:
            source_path = qualification._materialize_fixture(fixture, output)
            facts = qualification._source_facts(source_path)
            self.assertEqual(facts["workbook"]["calculationMode"], "manual")
            self.assertEqual(facts["workbook"]["dateSystem"], "1900")
            self.assertIn("xl/calcChain.xml", facts["unsupportedPaths"])
            self.assertIn("xl/externalLinks/externalLink1.xml", facts["unsupportedPaths"])
            self.assertEqual(facts["grids"][0]["styledEmptyCells"], ["F1", "H20"])
            self.assertEqual(facts["tables"][0]["ref"], "B1:C3")
            source = (qualification.ROOT / "tools" / "qualification_issue100.py").read_text(encoding="utf-8")
            self.assertNotRegex(source, re.compile(r"(?:from|import)\s+adapter_xlsx"))
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_public_converter_is_used_and_input_evidence_is_bound(self) -> None:
        fixture = next(item for item in self.corpus["fixtures"] if item["fixtureId"] == "xlsx-independent-minimal")
        output = self._output_dir("converter")
        try:
            run = qualification._run_converter(fixture, output)
            self.assertEqual(run["inspect"]["returnCode"], 0)
            self.assertTrue(run["evidence"]["input"]["consumed"])
            self.assertEqual(run["evidence"]["input"]["sha256"], run["inputSha256"])
            result = qualification._fixture_result(fixture, run)
            self.assertEqual(result["sourceMismatches"], [])
            if run["converter"]["returnCode"] != 0:
                self.assertTrue(any(item["code"] == "PUBLIC-CONVERTER-FAILED" for item in result["mismatches"]))
                self.assertTrue(result["adapterDiagnostics"])
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_authored_negative_mutations_are_all_detected(self) -> None:
        results = qualification.run_oracle_mutations(self.corpus)
        self.assertEqual(len(results), len(self.corpus["negativeCases"]))
        self.assertEqual({item["caseId"] for item in results}, {item["id"] for item in self.corpus["negativeCases"]})
        self.assertTrue(all(item["detected"] and item["status"] == "passed" for item in results))
        self.assertEqual(
            {item["expectedDefectCode"] for item in results},
            {
                "XLSX-RELATIONSHIP-OCCURRENCE-DROPPED",
                "XLSX-FORMULA-CACHE-LANE-DROPPED",
                "XLSX-EMPTY-STYLED-CELL-DROPPED",
                "XLSX-TABLE-REF-WIDENED",
                "XLSX-UNPARSED-PART-HIDDEN",
            },
        )
        self.assertTrue(all(item["classification"] == "oracle-projection-mutation" for item in results))
        self.assertTrue(all(item["executableAdapterMutation"] is False for item in results))

    def test_runner_emits_every_report_and_fails_closed(self) -> None:
        output = self._output_dir("runner")
        try:
            exit_code = qualification.run_qualification(out_dir=output)
            self.assertEqual(exit_code, 1)
            for report_name in qualification.REPORT_NAMES.values():
                report_path = output / report_name
                self.assertTrue(report_path.is_file(), report_name)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["issueNumber"], 100)
                self.assertEqual(report["version"], "1.1.0")
                self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
                self.assertEqual(report["status"], "failed")
                self.assertIn("mismatchCount", report)
                self.assertIn("unmetRequirements", report)
                self.assertIn("requirements", report)
                self.assertFalse(report["sourceTreeClean"])
                self.assertEqual(report["undetectedDefectCount"], 0)
                self.assertTrue(all(item["status"] == "passed" for item in report["negativeDefectResults"]))
            producer_report = json.loads((output / qualification.REPORT_NAMES["producers"]).read_text(encoding="utf-8"))
            self.assertIn("QUAL-100-PRODUCER-DIFFERENTIAL-ORACLE", producer_report["unmetRequirements"])
            self.assertEqual(
                next(item for item in producer_report["producerMatrix"] if item["producerId"] == "microsoft-excel")["status"],
                "unavailable",
            )
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_semantic_diff_reports_missing_occurrences_without_position_aliasing(self) -> None:
        findings = qualification._diff(
            {"relationships": [{"source": "xl/workbook.xml", "id": "rIdSheet"}]},
            {"relationships": [{"source": "xl/workbook.xml", "id": "rIdStyles"}]},
        )
        self.assertEqual({item["code"] for item in findings}, {"MISSING-OCCURRENCE", "UNEXPECTED-OCCURRENCE"})
        self.assertTrue(any(item["expected"]["id"] == "rIdSheet" for item in findings if item["code"] == "MISSING-OCCURRENCE"))

    def test_external_fixture_provenance_cannot_claim_bound_without_authored_sha(self) -> None:
        corpus_path = qualification.ROOT / "machine" / "qualification-issue-100-corpus.json"
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        entry = next(item for item in corpus["producerMatrix"] if item["producerId"] == "microsoft-excel")
        entry["provenance"]["availability"] = "bound"
        with self.assertRaises(qualification.QualificationError):
            temporary = self._output_dir("invalid-corpus") / "corpus.json"
            temporary.write_text(json.dumps(corpus), encoding="utf-8")
            qualification.load_corpus(temporary)


if __name__ == "__main__":
    unittest.main()
