from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.build_qualification_bundle import BundleBuildError, _validate_producer_report
from tools.qualification_evidence import selected_artifact_digest, selected_artifact_value
from tools.validate_qualification_bundle import (
    _validate_producer_report as _validate_bundle_producer_report,
    sha256_file,
)


class ProducerReportConstructionTests(unittest.TestCase):
    source_sha = "a" * 40
    evidence_id = "issue-88-qualification-contract"
    requirement_id = "QUAL-88-EVIDENCE-OBJECT"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bundle_root = Path(self.temp_dir.name)
        self.roles = {
            "artifacts/input.json": "producer-input",
            "artifacts/authority.json": "oracle",
            "artifacts/actual-positive.json": "behavioral-output",
            "artifacts/actual-mutation.json": "mutation-output",
            "artifacts/support.json": "supporting-record",
        }
        self._write_json("artifacts/input.json", {"fixture": "input"})
        self._write_json("artifacts/authority.json", {"value": "expected"})
        self._write_json("artifacts/actual-positive.json", {"value": "expected"})
        self._write_json("artifacts/actual-mutation.json", {"value": "mutated"})
        self._write_json(
            "artifacts/support.json",
            {
                "records": [
                    self._support("positive", "fixture-positive", "expected"),
                    self._support("mutation", "fixture-mutation", "mutated"),
                ]
            },
        )
        self.report = self._valid_report()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_json(self, relative: str, value: object) -> None:
        path = self.bundle_root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def _ref(self, relative: str, pointer: str) -> dict[str, object]:
        path = self.bundle_root / Path(*relative.split("/"))
        selector = {"kind": "json-pointer", "pointer": pointer}
        value = selected_artifact_value(path, selector)
        return {
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "selector": selector,
            "selectedSha256": selected_artifact_digest(value, selector),
        }

    @staticmethod
    def _support(kind: str, case_id: str, actual: object) -> dict[str, object]:
        return {
            "assertionId": case_id,
            "caseId": case_id,
            "actual": actual,
            "target": {"fixture": kind},
            "status": "passed",
        }

    def _valid_report(self) -> dict[str, object]:
        input_ref = self._ref("artifacts/input.json", "")
        authority_ref = self._ref("artifacts/authority.json", "/value")
        positive_ref = self._ref("artifacts/actual-positive.json", "/value")
        mutation_ref = self._ref("artifacts/actual-mutation.json", "/value")
        positive_support = self._ref("artifacts/support.json", "/records/0")
        mutation_support = self._ref("artifacts/support.json", "/records/1")
        target_positive = {"fixture": "positive"}
        target_mutation = {"fixture": "mutation"}
        cases = [
            {
                "caseId": "fixture-positive",
                "requirementId": self.requirement_id,
                "classification": "positive",
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": positive_ref,
                "expected": "expected",
                "actual": "expected",
                "comparison": {"operator": "equal"},
                "result": "passed",
                "target": target_positive,
                "diagnostic": {"code": "FIXTURE_POSITIVE", "message": "positive case was evaluated"},
                "supportingArtifact": positive_support,
            },
            {
                "caseId": "fixture-mutation",
                "requirementId": self.requirement_id,
                "classification": "mutation",
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": mutation_ref,
                "expected": "expected",
                "actual": "mutated",
                "comparison": {"operator": "not-equal"},
                "result": "passed",
                "target": target_mutation,
                "diagnostic": {"code": "FIXTURE_MUTATION", "message": "mutation differs from authority"},
                "supportingArtifact": mutation_support,
            },
        ]
        assertions = [
            {
                "assertionId": "fixture-positive",
                "requirementId": self.requirement_id,
                "assertionType": "json-value-equals",
                "testCaseId": "fixture-positive",
                "classification": "positive",
                "authorityArtifact": authority_ref,
                "actualArtifact": positive_ref,
                "expected": "expected",
                "actual": "expected",
                "comparison": {"operator": "equal"},
                "status": "passed",
                "target": target_positive,
                "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "positive assertion was evaluated"},
                "supportingArtifact": self._ref("artifacts/support.json", "/records/0"),
            },
            {
                "assertionId": "fixture-mutation",
                "requirementId": self.requirement_id,
                "assertionType": "mutation-killed",
                "testCaseId": "fixture-mutation",
                "classification": "mutation",
                "authorityArtifact": authority_ref,
                "actualArtifact": mutation_ref,
                "expected": "expected",
                "actual": "mutated",
                "comparison": {"operator": "not-equal"},
                "status": "passed",
                "target": target_mutation,
                "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "mutation assertion was evaluated"},
                "supportingArtifact": self._ref("artifacts/support.json", "/records/1"),
            },
        ]
        return {
            "schema": "fdir/qualification-producer-report",
            "version": "1.0.0",
            "evidenceId": self.evidence_id,
            "requirementIds": [self.requirement_id],
            "sourceSha": self.source_sha,
            "inputDigests": [self._digest("artifacts/input.json")],
            "producerId": "fixture-producer",
            "authorityId": "fixture-authority",
            "independence": {
                "producerComponentDigest": "b" * 64,
                "authorityComponentDigest": "c" * 64,
                "evaluatorComponentDigest": "d" * 64,
                "expectedDerivedFromActual": False,
                "sharedComponentDigests": [],
            },
            "assertions": assertions,
            "testCases": cases,
            "uncoveredItems": [],
            "unsupportedItems": [],
            "waivedItems": [],
            "status": "passed",
            "failureCount": 0,
        }

    def _digest(self, relative: str) -> str:
        return hashlib.sha256((self.bundle_root / Path(*relative.split("/"))).read_bytes()).hexdigest()

    def _validate(self, report: dict[str, object] | None = None, roles: dict[str, str] | None = None) -> None:
        _validate_producer_report(
            report or self.report,
            evidence_id=self.evidence_id,
            issue_numbers=[88],
            requirement_ids=[self.requirement_id],
            source_sha=self.source_sha,
            input_digests={self._digest("artifacts/input.json")},
            bundle_root=self.bundle_root,
            output_roles=roles or self.roles,
        )

    def test_typed_producer_report_is_accepted(self) -> None:
        self._validate()

    def test_no_op_producer_is_rejected(self) -> None:
        report = deepcopy(self.report)
        report["testCases"][1]["expected"] = "expected"
        report["testCases"][1]["actual"] = "expected"
        report["testCases"][1]["comparison"] = {"operator": "equal"}
        report["assertions"][1]["assertionType"] = "json-value-equals"
        report["assertions"][1]["expected"] = "expected"
        report["assertions"][1]["actual"] = "expected"
        report["assertions"][1]["comparison"] = {"operator": "equal"}
        self._write_json("artifacts/actual-mutation.json", {"value": "expected"})
        self._write_json(
            "artifacts/support.json",
            {
                "records": [
                    self._support("positive", "fixture-positive", "expected"),
                    self._support("mutation", "fixture-mutation", "expected"),
                ]
            },
        )
        for item in (report["testCases"][0], report["assertions"][0]):
            item["supportingArtifact"] = self._ref("artifacts/support.json", "/records/0")
        for item in (report["testCases"][1], report["assertions"][1]):
            item["actualArtifact"] = self._ref("artifacts/actual-mutation.json", "/value")
            item["supportingArtifact"] = self._ref("artifacts/support.json", "/records/1")
        with self.assertRaisesRegex(BundleBuildError, "PRODUCER_NO_OP"):
            self._validate(report)

    def test_success_only_producer_is_rejected(self) -> None:
        report = deepcopy(self.report)
        report["testCases"] = report["testCases"][:1]
        report["assertions"] = report["assertions"][:1]
        with self.assertRaisesRegex(BundleBuildError, "PRODUCER_CASE_COVERAGE"):
            self._validate(report)

    def test_source_snapshot_cannot_be_a_behavioral_actual(self) -> None:
        roles = dict(self.roles)
        roles["artifacts/actual-mutation.json"] = "source-snapshot"
        with self.assertRaisesRegex(BundleBuildError, "PRODUCER_CASE_SOURCE_SNAPSHOT"):
            self._validate(roles=roles)

    def test_typed_assertion_fields_are_required(self) -> None:
        report = deepcopy(self.report)
        del report["assertions"][0]["diagnostic"]
        with self.assertRaisesRegex(BundleBuildError, "PRODUCER_REPORT_SCHEMA"):
            self._validate(report)

    def test_evidence_binding_is_required(self) -> None:
        report = deepcopy(self.report)
        report["evidenceId"] = "other-evidence"
        with self.assertRaisesRegex(BundleBuildError, "PRODUCER_REPORT_EVIDENCE_ID"):
            self._validate(report)


class BundleValidatorIntegrityTests(unittest.TestCase):
    """The bundle validator must recompute producer claims independently."""

    source_sha = "a" * 40
    evidence_id = "issue-110-integrity-fixture"
    requirement_id = "QUAL-110-INTEGRITY"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bundle_root = Path(self.temp_dir.name)
        self.paths = {
            "input": "artifacts/input.json",
            "authority": "artifacts/authority.json",
            "positive": "artifacts/actual-positive.json",
            "mutation": "artifacts/actual-mutation.json",
            "support": "artifacts/support.json",
        }
        self.roles = {
            self.paths["input"]: "producer-input",
            self.paths["authority"]: "oracle",
            self.paths["positive"]: "behavioral-output",
            self.paths["mutation"]: "mutation-output",
            self.paths["support"]: "supporting-record",
        }
        self._write("input", {"fixture": "issue-110"})
        self._write("authority", {"value": "expected"})
        self._write("positive", {"value": "expected"})
        self._write("mutation", {"value": "mutated"})
        self._write(
            "support",
            {
                "records": [
                    self._support("fixture:positive", "fixture-positive", "expected"),
                    self._support("fixture-positive", "fixture-positive", "expected"),
                    self._support("fixture:mutation", "fixture-mutation", "mutated"),
                    self._support("fixture-mutation", "fixture-mutation", "mutated"),
                ]
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _target(self, name: str) -> Path:
        return self.bundle_root / Path(*self.paths[name].split("/"))

    def _write(self, name: str, value: object) -> None:
        path = self._target(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _support(assertion_id: str, case_id: str, actual: str) -> dict[str, object]:
        return {
            "assertionId": assertion_id,
            "caseId": case_id,
            "actual": actual,
            "target": {"entity": "fixture", "field": "value"},
            "status": "passed",
        }

    def _ref(self, name: str, pointer: str) -> dict[str, object]:
        path = self._target(name)
        selector = {"kind": "json-pointer", "pointer": pointer}
        value = selected_artifact_value(path, selector)
        return {
            "path": self.paths[name],
            "sha256": sha256_file(path),
            "selector": selector,
            "selectedSha256": selected_artifact_digest(value, selector),
        }

    def _rebind(self, ref: dict[str, object]) -> None:
        path = self.bundle_root / Path(*str(ref["path"]).split("/"))
        selector = ref["selector"]
        value = selected_artifact_value(path, selector)
        ref["sha256"] = sha256_file(path)
        ref["selectedSha256"] = selected_artifact_digest(value, selector)

    def _report(self) -> dict[str, object]:
        input_ref = self._ref("input", "")
        authority = self._ref("authority", "/value")
        positive = self._ref("positive", "/value")
        mutation = self._ref("mutation", "/value")
        target = {"entity": "fixture", "field": "value"}
        cases = [
            {
                "caseId": "fixture-positive",
                "requirementId": self.requirement_id,
                "classification": "positive",
                "inputArtifact": deepcopy(input_ref),
                "authorityArtifact": deepcopy(authority),
                "actualArtifact": positive,
                "expected": "expected",
                "actual": "expected",
                "comparison": {"operator": "equal"},
                "result": "passed",
                "target": target,
                "diagnostic": {"code": "FIXTURE_CASE", "message": "recomputed"},
                "supportingArtifact": self._ref("support", "/records/1"),
            },
            {
                "caseId": "fixture-mutation",
                "requirementId": self.requirement_id,
                "classification": "mutation",
                "inputArtifact": deepcopy(input_ref),
                "authorityArtifact": deepcopy(authority),
                "actualArtifact": mutation,
                "expected": "expected",
                "actual": "mutated",
                "comparison": {"operator": "not-equal"},
                "result": "passed",
                "target": target,
                "diagnostic": {"code": "FIXTURE_CASE", "message": "recomputed"},
                "supportingArtifact": self._ref("support", "/records/3"),
            },
        ]
        assertions = [
            {
                "assertionId": "fixture:positive",
                "requirementId": self.requirement_id,
                "assertionType": "json-value-equals",
                "testCaseId": "fixture-positive",
                "classification": "positive",
                "authorityArtifact": deepcopy(authority),
                "actualArtifact": deepcopy(positive),
                "expected": "expected",
                "actual": "expected",
                "comparison": {"operator": "equal"},
                "status": "passed",
                "target": target,
                "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "recomputed"},
                "supportingArtifact": self._ref("support", "/records/0"),
            },
            {
                "assertionId": "fixture:mutation",
                "requirementId": self.requirement_id,
                "assertionType": "negative-rejection",
                "testCaseId": "fixture-mutation",
                "classification": "mutation",
                "authorityArtifact": deepcopy(authority),
                "actualArtifact": deepcopy(mutation),
                "expected": "expected",
                "actual": "mutated",
                "comparison": {"operator": "not-equal"},
                "status": "passed",
                "target": target,
                "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "recomputed"},
                "supportingArtifact": self._ref("support", "/records/2"),
            },
        ]
        return {
            "schema": "fdir/qualification-producer-report",
            "version": "1.0.0",
            "evidenceId": self.evidence_id,
            "requirementIds": [self.requirement_id],
            "sourceSha": self.source_sha,
            "inputDigests": [sha256_file(self._target("input"))],
            "producerId": "issue-110-producer",
            "authorityId": "issue-110-independent-oracle",
            "independence": {
                "producerComponentDigest": "b" * 64,
                "authorityComponentDigest": "c" * 64,
                "evaluatorComponentDigest": "d" * 64,
                "expectedDerivedFromActual": False,
                "sharedComponentDigests": [],
            },
            "assertions": assertions,
            "testCases": cases,
            "uncoveredItems": [],
            "unsupportedItems": [],
            "waivedItems": [],
            "status": "passed",
            "failureCount": 0,
        }

    def _codes(self, report: dict[str, object]) -> set[str]:
        diagnostics: list[dict[str, str]] = []
        input_digest = sha256_file(self._target("input"))
        _validate_bundle_producer_report(
            report,
            evidence_id=self.evidence_id,
            issue_numbers=[88],
            requirement_ids=[self.requirement_id],
            source_sha=self.source_sha,
            input_digests={input_digest},
            bundle_root=self.bundle_root,
            output_paths=set(self.paths.values()),
            output_roles=self.roles,
            diagnostics=diagnostics,
            report_path="reports/issue-110.json#producerReport",
        )
        return {item["code"] for item in diagnostics}

    def test_valid_fixture_is_accepted(self) -> None:
        self.assertEqual(self._codes(self._report()), set())

    def test_simultaneous_forgery_is_rejected(self) -> None:
        report = self._report()
        self._write("positive", {"value": "forged"})
        support_path = self._target("support")
        support = json.loads(support_path.read_text(encoding="utf-8"))
        support["records"][0]["actual"] = "forged"
        support["records"][1]["actual"] = "forged"
        support_path.write_text(json.dumps(support, sort_keys=True) + "\n", encoding="utf-8")
        assertion = report["assertions"][0]
        case = report["testCases"][0]
        assertion["expected"] = assertion["actual"] = "forged"
        case["expected"] = case["actual"] = "forged"
        for item in (assertion, case):
            for field in ("actualArtifact", "supportingArtifact"):
                self._rebind(item[field])
        codes = self._codes(report)
        self.assertIn("PRODUCER_EXPECTED_MISMATCH", codes)
        self.assertIn("ASSERTION_STATUS_MISMATCH", codes)

    def test_support_range_replacement_is_rejected(self) -> None:
        report = self._report()
        support = report["assertions"][0]["supportingArtifact"]
        support["selector"] = {"kind": "json-pointer", "pointer": "/records"}
        self._rebind(support)
        codes = self._codes(report)
        self.assertIn("SUPPORT_RANGE_REPLACEMENT", codes)
        self.assertIn("SUPPORT_SELECTOR_MISMATCH", codes)

    def test_unknown_assertion_type_has_no_evaluator(self) -> None:
        report = self._report()
        report["assertions"][0]["assertionType"] = "self-attested-equals"
        codes = self._codes(report)
        self.assertIn("PRODUCER_ASSERTION_TYPE", codes)
        self.assertIn("ASSERTION_EVALUATOR_MISSING", codes)

    def test_same_expected_actual_artifact_is_rejected(self) -> None:
        report = self._report()
        assertion = report["assertions"][0]
        case = report["testCases"][0]
        assertion["actualArtifact"] = deepcopy(assertion["authorityArtifact"])
        case["actualArtifact"] = deepcopy(case["authorityArtifact"])
        codes = self._codes(report)
        self.assertIn("PRODUCER_ASSERTION_SAME_ARTIFACT", codes)
        self.assertIn("PRODUCER_CASE_SAME_ARTIFACT", codes)


if __name__ == "__main__":
    unittest.main()
