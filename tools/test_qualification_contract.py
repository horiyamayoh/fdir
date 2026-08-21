"""Tests for the independent qualification-contract evaluator."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

try:
    import validate_qualification_contract as validator
except ImportError:  # pragma: no cover
    from tools import validate_qualification_contract as validator


ROOT = Path(__file__).resolve().parents[1]


class QualificationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            (ROOT / "machine" / "qualification-contract.json").read_text(encoding="utf-8")
        )

    def test_checked_in_contract_is_valid(self) -> None:
        findings = validator.validate_contract_document(self.contract)
        self.assertEqual([], findings, "\n".join(findings))

    def test_missing_producer_output_is_rejected(self) -> None:
        mutated = deepcopy(self.contract)
        outputs = mutated["defaultEvidence"][0]["outputs"]
        outputs[0]["producerReport"] = False
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("producer-report output" in finding for finding in findings), findings)

    def test_generic_exit_assertion_is_rejected(self) -> None:
        mutated = deepcopy(self.contract)
        mutated["behavioralReportContract"]["requirements"][0]["requiredAssertionIds"].append(
            "qualification-command-exits-zero"
        )
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("generic pass assertion" in finding for finding in findings), findings)

    def test_unknown_case_class_is_rejected(self) -> None:
        mutated = deepcopy(self.contract)
        requirement = mutated["behavioralReportContract"]["requirements"][0]
        requirement["requiredCaseClasses"].append("not-a-case-class")
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("invalid requiredCaseClasses" in finding for finding in findings), findings)


if __name__ == "__main__":
    unittest.main()
