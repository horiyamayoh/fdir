"""Focused tests for the strict independent issue #102 Markdown lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest
import uuid

try:
    import qualification_issue102 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue102 as qualification


class QualificationIssue102Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = qualification.load_corpus()

    def test_corpus_is_independent_and_profile_authoritative(self) -> None:
        oracle = self.corpus["oracle"]
        self.assertTrue(oracle["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(oracle["adapterHelpersUsedForExpected"])
        self.assertFalse(oracle["expectedAstGeneratedByAdapter"])
        self.assertGreaterEqual(len(self.corpus["profiles"]), 4)
        self.assertEqual(
            {fixture["format"] for fixture in self.corpus["fixtures"]},
            {"markdown"},
        )
        source = (qualification.ROOT / "tools" / "qualification_issue102.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, re.compile(r"(?:from|import)\s+(?:adapter_markdown|adapter_common)"))
        self.assertIn("official CommonMark 0.31.2 example corpus is not vendored", "\n".join(self.corpus["unmetRequirements"]))

    def test_required_issue_categories_and_negative_mutations_are_present(self) -> None:
        self.assertEqual(
            set(self.corpus["requiredDefectCases"]),
            qualification.REQUIRED_DEFECT_CASES,
        )
        self.assertGreaterEqual(len(self.corpus["negativeMutations"]), 12)
        self.assertTrue(
            qualification.REQUIRED_NEGATIVE_MUTATIONS
            <= {item["mutationId"] for item in self.corpus["negativeMutations"]}
        )
        self.assertEqual(self.corpus["reportNames"], list(qualification.REPORT_NAMES))

    def test_source_span_literals_are_independently_recomputable(self) -> None:
        for fixture in self.corpus["fixtures"]:
            for assertion in fixture["expected"]["spanAssertions"]:
                calculated = qualification._source_span(fixture["source"]["value"], assertion)
                for key in ("byteStart", "byteEnd", "codePointStart", "codePointEnd", "lineEnding"):
                    if key in assertion:
                        self.assertEqual(calculated[key], assertion[key], fixture["fixtureId"])

    def test_public_inspect_reports_the_selected_profile(self) -> None:
        try:
            from tools import adapter_markdown
            from tools import convert_document
        except ImportError:  # pragma: no cover
            import adapter_markdown
            import convert_document

        source = qualification.ROOT / "e2e" / ".run" / f"issue-102-inspect-{uuid.uuid4().hex}.md"
        source.write_text("# profile\n", encoding="utf-8")
        report = adapter_markdown.inspect(source, profile="gfm-0.29")
        self.assertEqual(report["profile"], "gfm-0.29")
        self.assertTrue(report["profileKnown"])
        self.assertIn("task-lists", report["capabilities"])
        self.assertNotIn("footnotes", report["capabilities"])

        exit_code = convert_document.main(
            ["inspect", str(source), "--format", "markdown", "--profile", "gfm-0.29"]
        )
        self.assertEqual(exit_code, 0)

    def test_negative_mutations_are_all_detected_by_the_independent_oracle(self) -> None:
        results = qualification._run_negative_mutations(self.corpus)
        self.assertEqual(len(results), len(self.corpus["negativeMutations"]))
        self.assertTrue(all(item["oracleOnly"] for item in results))
        self.assertTrue(all(item["oracleMutationDetected"] for item in results))
        self.assertTrue(all(item["status"] == "passed" for item in results))

    def test_runner_emits_all_reports_and_fails_closed(self) -> None:
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-102-test-{uuid.uuid4().hex[:10]}"
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 1)
        corpus_bytes = (qualification.ROOT / "machine" / "qualification-issue-102-corpus.json").read_bytes()
        corpus_sha = hashlib.sha256(corpus_bytes).hexdigest()
        for report_name in qualification.REPORT_NAMES:
            path = output / report_name
            self.assertTrue(path.is_file(), report_name)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["issueNumber"], 102)
            self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
            self.assertEqual(report["corpusSha256"], corpus_sha)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["qualificationGate"], "fail-closed")
            self.assertTrue(report["fixtureResults"])
            self.assertTrue(report["negativeMutationResults"])
            self.assertEqual(report["negativeMutationFailureCount"], 0)
            self.assertEqual(report["undetectedDefectCount"], 0)
            self.assertTrue(report["unmetRequirements"])
            self.assertTrue(report["failureSummary"])
            self.assertTrue(report["ciBinding"]["unmet"])

    def test_defect_gate_executes_every_declared_markdown_case(self) -> None:
        result = qualification._run_defect_gates(self.corpus)
        self.assertTrue(result["contractAvailable"])
        self.assertEqual(result["caseCount"], len(qualification.REQUIRED_DEFECT_CASES))
        self.assertEqual(result["detectedCount"], len(qualification.REQUIRED_DEFECT_CASES))
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
