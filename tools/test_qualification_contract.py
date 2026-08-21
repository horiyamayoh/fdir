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

    def test_recovery_issues_have_explicit_one_to_one_report_mappings(self) -> None:
        behavioral = self.contract["behavioralReportContract"]
        requirements = {
            item["ownerIssue"]: item
            for item in behavioral["requirements"]
            if 91 <= item.get("ownerIssue", 0) <= 105
        }
        self.assertEqual(set(range(91, 106)), set(requirements))
        for issue, requirement in requirements.items():
            self.assertTrue(requirement["producer"]["command"], issue)
            self.assertTrue(requirement["requiredAssertionIds"], issue)
            self.assertTrue(requirement["requiredCases"], issue)
            self.assertTrue(requirement["requiredEvaluator"], issue)
            self.assertGreaterEqual(len(requirement["reports"]), 2, issue)
            report_bindings = {
                (report["path"], report["bundlePath"], report["outputRole"])
                for report in requirement["reports"]
            }
            default = next(item for item in self.contract["defaultEvidence"] if item["issueNumbers"] == [issue])
            output_bindings = {
                (output["sourcePath"], output["path"], output["role"])
                for output in default["outputs"]
            }
            self.assertEqual(report_bindings, output_bindings, issue)

    def test_generic_suite_command_is_rejected_for_recovery_issue(self) -> None:
        generic_command = ["python", "tools/run_acceptance.py", "--all"]
        mutated = deepcopy(self.contract)
        target_requirement = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 91
        )
        target_default = next(item for item in mutated["defaultEvidence"] if item["issueNumbers"] == [91])
        target_requirement["producer"]["command"] = generic_command
        target_default["command"] = generic_command
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("generic qualification suite" in finding for finding in findings), findings)

    def test_source_snapshot_output_is_rejected_for_recovery_issue(self) -> None:
        mutated = deepcopy(self.contract)
        requirement = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 92
        )
        default = next(item for item in mutated["defaultEvidence"] if item["issueNumbers"] == [92])
        report = next(item for item in requirement["reports"] if item.get("producerReport") is not True)
        output = next(item for item in default["outputs"] if item["path"] == report["bundlePath"])
        report["outputRole"] = "source-snapshot"
        output["role"] = "source-snapshot"
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("source-snapshot" in finding for finding in findings), findings)

    def test_orphan_report_mapping_is_rejected(self) -> None:
        mutated = deepcopy(self.contract)
        requirement = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 93
        )
        requirement["reports"][1]["bundlePath"] = "artifacts/93/orphan.json"
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("orphan report declaration" in finding for finding in findings), findings)

    def test_duplicate_cross_issue_report_mapping_is_rejected(self) -> None:
        mutated = deepcopy(self.contract)
        first = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 91
        )
        second = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 92
        )
        second["reports"].append(deepcopy(first["reports"][1]))
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("duplicated with issue 91" in finding for finding in findings), findings)

    def test_report_schema_version_and_evaluator_are_required(self) -> None:
        mutated = deepcopy(self.contract)
        requirement = next(
            item
            for item in mutated["behavioralReportContract"]["requirements"]
            if item.get("ownerIssue") == 94
        )
        requirement.pop("requiredEvaluator")
        requirement["reports"][1].pop("schemaVersion")
        findings = validator.validate_contract_document(mutated)
        self.assertTrue(any("must declare requiredEvaluator" in finding for finding in findings), findings)
        self.assertTrue(any("schemaVersion" in finding for finding in findings), findings)


if __name__ == "__main__":
    unittest.main()
