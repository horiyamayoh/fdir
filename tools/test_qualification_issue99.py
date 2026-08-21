"""Focused tests for the independent, fail-closed issue #99 DOCX lane."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import unittest

try:
    import qualification_issue99 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue99 as qualification


class QualificationIssue99Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification.load_corpus()

    def test_corpus_is_authored_independent_and_synthetic_only(self) -> None:
        oracle = self.corpus["oracle"]
        self.assertTrue(oracle["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(oracle["adapterHelpersUsedForExpected"])
        self.assertEqual(oracle["sourceConstruction"], "authored-stdlib-zip-xml-package")
        self.assertTrue(self.corpus["producerPolicy"]["syntheticOnly"])
        self.assertFalse(self.corpus["producerPolicy"]["realProducerCorpusAvailable"])
        self.assertEqual(self.corpus["producerPolicy"]["availableRealProducers"], [])
        self.assertGreaterEqual(len(self.corpus["producerPolicy"]["requiredRealProducers"]), 4)
        self.assertEqual({fixture["format"] for fixture in self.corpus["fixtures"]}, {"docx"})
        self.assertTrue(all("synthetic" in fixture["sourceKind"] for fixture in self.corpus["fixtures"]))
        source = (qualification.ROOT / "tools" / "qualification_issue99.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, re.compile(r"(?:from|import)\s+(?:adapter_docx|adapter_common)"))

    def test_required_issue_cases_are_present(self) -> None:
        fixture_cases = {case["caseId"] for fixture in self.corpus["fixtures"] for case in fixture["cases"]}
        fixture_cases.update(case["caseId"] for case in self.corpus["securityCases"])
        self.assertTrue(qualification.REQUIRED_REGRESSION_CASES <= fixture_cases)
        self.assertTrue(qualification.REQUIRED_NEGATIVE_CASES <= {case["caseId"] for case in self.corpus["negativeCases"]})
        self.assertEqual(self.corpus["reportNames"], qualification.REPORT_NAMES)
        self.assertGreaterEqual(len(self.corpus["requirements"]), 5)
        self.assertTrue(self.corpus["unmetRequirements"])
        self.assertTrue(any(item.startswith("DOCX99-REAL-PRODUCERS:") for item in self.corpus["unmetRequirements"]))

    def test_negative_mutations_are_detected_by_authored_oracle(self) -> None:
        results = qualification._run_negative_mutations(self.corpus)
        self.assertEqual(len(results), len(self.corpus["negativeCases"]))
        self.assertTrue(all(item["oracleMutationDetected"] for item in results))
        self.assertTrue(all(item["status"] == "passed" for item in results))

    def test_source_inspector_sees_opc_and_required_constructs(self) -> None:
        fixture = self.corpus["fixtures"][0]
        work = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-99-source-test-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
        path = qualification._materialize_fixture(fixture, work)
        facts = qualification._source_facts(path)
        self.assertGreaterEqual(len(facts["parts"]), 18)
        self.assertGreaterEqual(len(facts["relationships"]), 16)
        self.assertEqual(facts["hyperlinks"][0]["displayText"], "Click\t\nHere")
        self.assertEqual(facts["drawings"][0]["container"], "run")
        self.assertEqual(facts["tables"][0]["rows"][0]["cells"][0]["gridSpan"], 2)
        self.assertEqual(facts["tables"][0]["rows"][2]["cells"][0]["vMerge"], "continue")
        self.assertEqual(len(facts["tables"]), 2)
        self.assertEqual(len(facts["fields"]), 2)
        self.assertEqual({item["kind"] for item in facts["stories"]}, {"header", "footer", "footnote", "endnote", "comment"})
        self.assertEqual(len(facts["sections"]), 2)
        self.assertTrue(any(item["token"] == "AlternateContent:Choice" for item in facts["unsupported"]))

    def test_runner_emits_all_reports_and_fails_closed(self) -> None:
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-99-test-{os.getpid()}"
        output.mkdir(parents=True, exist_ok=True)
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 1)
        for report_name in qualification.REPORT_NAMES:
            path = output / report_name
            self.assertTrue(path.is_file(), report_name)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["issueNumber"], 99)
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertRegex(report["corpusSha256"], re.compile(r"^[0-9a-f]{64}$"))
            corpus_bytes = (qualification.ROOT / "machine" / "qualification-issue-99-corpus.json").read_bytes()
            self.assertEqual(report["corpusSha256"], hashlib.sha256(corpus_bytes).hexdigest())
            self.assertGreater(report["producerCount"], 0)
            self.assertEqual(report["profileCount"], 1)
            self.assertTrue(report["nonemptyAssertions"])
            self.assertTrue(report["assertions"])
            self.assertTrue(report["negativeMutationResults"])
            self.assertEqual(report["negativeMutationFailureCount"], 0)
            self.assertEqual(report["negativeDefectFailureCount"], 0)
            self.assertTrue(all(item["oracleMutationDetected"] for item in report["negativeMutationResults"]))
            self.assertEqual(report["realProducerCorpusCount"], 0)
            self.assertEqual(report["completionStatus"], "incomplete-bounded-lane")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["qualificationGate"], "fail-closed")
            self.assertTrue(report["unmetRequirements"])
            self.assertEqual(report["adapterFailureCount"], 0)
            self.assertEqual(report["unaccountedOccurrenceCount"], 0)
            self.assertEqual(report["falseCompleteCount"], 0)
            self.assertEqual(report["fabricatedRelationTargetCount"], 0)
            self.assertEqual(report["globalFailureCount"], 1)
            self.assertEqual(report["globalMismatchCount"], 1)
            self.assertEqual(
                [item for item in report["failureSummary"] if item.startswith("projection-mismatch:")],
                ["projection-mismatch:producer.real-corpus-required=1"],
            )
            self.assertTrue(any(item.startswith("DOCX99-REAL-PRODUCERS:") for item in report["unmetRequirements"]))
            self.assertEqual(report["realProducerCorpusCount"], 0)
            self.assertEqual(report["producerCounts"]["requiredReal"], 4)
            expected_selected_mismatch = 1 if report_name == "docx-multi-producer-differential.json" else 0
            self.assertEqual(report["mismatchCount"], expected_selected_mismatch)
            self.assertGreaterEqual(report["globalMismatchCount"], report["mismatchCount"])
            for assertion in report["assertions"]:
                if assertion["assertionId"] == "producer.real-corpus-required":
                    continue
                self.assertEqual(assertion["status"], "passed", assertion["assertionId"])
                self.assertEqual(assertion["mismatchCount"], 0, assertion["assertionId"])
            for detail in report["mismatchDetails"]:
                self.assertGreater(detail["mismatchCount"], 0)
                self.assertTrue(detail["details"])


if __name__ == "__main__":
    unittest.main()
