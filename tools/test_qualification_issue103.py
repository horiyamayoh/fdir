"""Focused tests for the independent issue #103 qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
import uuid

try:
    import qualification_issue103 as qualification
except ImportError:  # pragma: no cover
    from tools import qualification_issue103 as qualification

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


class QualificationIssue103Tests(unittest.TestCase):
    def test_corpus_declares_independent_literal_oracle(self) -> None:
        corpus = qualification._read_json(qualification.DEFAULT_CORPUS_PATH)
        qualification._validate_corpus(corpus)
        self.assertTrue(corpus["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertTrue(corpus["oracle"]["expectedFactsAreHandReviewed"])
        self.assertFalse(corpus["oracle"]["expectedFactsGeneratedFromCollectionKeys"])
        self.assertFalse(corpus["oracle"]["directAndIndexUsedToGenerateExpected"])
        self.assertGreaterEqual(len(corpus["expectedFacts"]), 8)
        self.assertGreaterEqual(len(corpus["expectedReferences"]), 4)

    def test_runner_writes_all_reports_and_requires_zero_survivors(self) -> None:
        output = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-103-test-{uuid.uuid4().hex[:10]}"
        exit_code = qualification.run_qualification(out_dir=output)
        self.assertEqual(exit_code, 0)
        for report_name in qualification.REPORT_NAMES:
            path = output / report_name
            self.assertTrue(path.is_file(), report_name)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["issueNumber"], 103)
            self.assertEqual(report["status"], "passed", report_name)
            self.assertEqual(report["mismatches"], [], report_name)
            self.assertTrue(report["assertions"], report_name)
        producer = json.loads((output / qualification.PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(validate_producer_report_shape(producer), [])
        self.assertEqual(producer["status"], "passed")
        self.assertEqual(producer["failureCount"], 0)
        self.assertEqual({item["caseId"] for item in producer["testCases"]}, {item["testCaseId"] for item in producer["assertions"]})

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        self.assertFalse(keys(producer) & {"exitCode", "returnCode", "outputFile", "evidenceFile"})

    def test_runner_does_not_import_adapters(self) -> None:
        source = Path(qualification.__file__).read_text(encoding="utf-8")
        for adapter in ("adapter_docx", "adapter_xlsx", "adapter_pdf", "adapter_markdown"):
            self.assertNotIn(f"from {adapter}", source)


if __name__ == "__main__":
    unittest.main()
