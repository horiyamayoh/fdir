"""Focused producer-envelope checks for the Issue #89 campaign runner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

from jsonschema import Draft202012Validator, RefResolver

from tools import run_defect_injection_campaign as campaign
from tools.build_qualification_bundle import build_bundle
from tools.validate_qualification_bundle import validate_bundle


class DefectInjectionProducerTests(unittest.TestCase):
    def _scratch(self, prefix: str) -> Path:
        path = campaign._workspace_tempdir(campaign.ROOT, prefix)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    @staticmethod
    def _report() -> dict:
        return {
            "base_sha": "a" * 40,
            "base_suite": {"status": "completed", "exit_code": 0},
            "detector_baselines": [{"status": "completed", "exit_code": 0}],
            "base_worktree": {"dirty": False},
            "campaign_calculated": True,
            "status": "passed",
            "undetected": [],
            "completion": {
                "coverage_complete": True,
                "must_undetected_zero": True,
                "release_eligible": True,
            },
            "cases": [
                {
                    "id": "synthetic-mutation",
                    "expected_outcome": "non-equivalent",
                    "classification": "detected",
                    "patch_application": {"apply": {"status": "completed", "exit_code": 0}},
                    "syntax_check": {"status": "completed", "exit_code": 0},
                    "import_check": {"status": "completed", "exit_code": 0},
                    "baseline_gate": {"status": "completed", "exit_code": 0},
                    "gate": {"status": "completed", "exit_code": 1, "timeout": False},
                    "detector_observation": {"observable_failure": True},
                    "target_function": "synthetic_probe",
                    "target_selectors": ["/synthetic_probe"],
                }
            ],
        }

    @staticmethod
    def _validate_schema(path: Path) -> dict:
        schema = json.loads((campaign.ROOT / "schemas" / "qualification-evidence.schema.json").read_text(encoding="utf-8"))
        document = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator(schema["$defs"]["producerReportDocument"], resolver=RefResolver.from_schema(schema)).validate(document)
        return document

    def test_semantic_envelope_covers_cases_and_uses_typed_assertions(self) -> None:
        scratch = self._scratch("producer-test-")
        report_path = scratch / "producer-report.json"
        document = campaign._campaign_producer_report(self._report(), scratch / "artifacts", report_path)

        self.assertEqual(document["status"], "passed")
        self.assertEqual(document["failureCount"], 0)
        self.assertEqual({item["assertionType"] for item in document["assertions"]}, {"defect-injection"})
        self.assertEqual(
            {item["caseId"] for item in document["testCases"]},
            {item["testCaseId"] for item in document["assertions"]},
        )
        self.assertIn("positive", {item["classification"] for item in document["testCases"]})
        self.assertIn("mutation", {item["classification"] for item in document["testCases"]})
        mutation = next(item for item in document["testCases"] if item["caseId"] == "synthetic-mutation")
        self.assertEqual(
            set(mutation["actual"]),
            {"classification", "sourcePatched", "sourceValid", "baselineGatePassed", "gateCompleted", "detectorObserved"},
        )
        self.assertEqual(mutation["actual"]["classification"], "detected")
        self.assertTrue(mutation["actual"]["detectorObserved"])
        self.assertEqual(mutation["target"]["targetFunction"], "synthetic_probe")
        self.assertEqual(document["uncoveredItems"], [])
        self._validate_schema(report_path)

    def test_undetected_mutation_cannot_be_promoted(self) -> None:
        scratch = self._scratch("producer-fail-issue-")
        report = self._report()
        report["status"] = "failed"
        report["undetected"] = ["synthetic-mutation"]
        report["completion"]["must_undetected_zero"] = False
        report["completion"]["release_eligible"] = False
        report["cases"][0]["classification"] = "undetected"
        report["cases"][0]["detector_observation"] = {"observable_failure": False}
        document = campaign._campaign_producer_report(report, scratch / "artifacts", scratch / "producer-report.json")

        self.assertEqual(document["status"], "failed")
        self.assertGreater(document["failureCount"], 0)
        self.assertTrue(document["uncoveredItems"])
        self._validate_schema(scratch / "producer-report.json")

    def test_built_bundle_validates_producer_case_coverage(self) -> None:
        scratch = self._scratch("producer-bundle-")
        report = self._report()
        report["base_sha"] = campaign._git_sha(campaign.ROOT)
        artifact_dir = scratch / "artifacts"
        producer_path = scratch / "producer-report.json"
        campaign._campaign_producer_report(report, artifact_dir, producer_path)

        def relative(path: Path) -> str:
            return path.resolve().relative_to(campaign.ROOT.resolve()).as_posix()

        contract_path = scratch / "qualification-contract.json"
        contract = {
            "schema": "fdir/qualification-contract",
            "version": "1.0.0",
            "repository": "horiyamayoh/fdir",
            "scope": {
                "issueNumbers": [89],
                "requiredEvidenceIds": [campaign.ISSUE_89_EVIDENCE_ID],
                "requiredRequirementIds": [campaign.ISSUE_89_REQUIREMENT_ID],
            },
            "sourcePolicy": {"shaFormat": "git-40-lowercase-hex", "releaseEvidenceMustBeClean": False},
            "bundlePolicy": {"manifestName": "manifest.json"},
            "negativeFixtures": [{"id": "producer-envelope", "expectedDiagnostic": "PRODUCER_REPORT_MISSING"}],
            "defaultEvidence": [
                {
                    "evidenceId": campaign.ISSUE_89_EVIDENCE_ID,
                    "issueNumbers": [89],
                    "requirementIds": [campaign.ISSUE_89_REQUIREMENT_ID],
                    "command": ["python", "tools/run_defect_injection_campaign.py", "--self-test"],
                    "inputs": [
                        {"path": path, "role": "producer-input"}
                        for path in campaign.ISSUE_89_INPUT_PATHS
                    ],
                    "outputs": [
                        {
                            "sourcePath": relative(producer_path),
                            "path": "artifacts/89/producer-report.json",
                            "role": "producer-report",
                            "producerReport": True,
                        },
                        {
                            "sourceDirectory": relative(artifact_dir),
                            "path": campaign.ISSUE_89_BUNDLE_ARTIFACT_ROOT,
                            "role": "campaign-artifacts",
                        },
                    ],
                }
            ],
        }
        contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        bundle_dir = scratch / "bundle"
        built = build_bundle(
            bundle_dir,
            source_sha=report["base_sha"],
            contract_path=contract_path,
            allow_dirty=True,
            allow_repository_output=True,
        )
        self.assertEqual(built["status"], "passed")
        validation = validate_bundle(
            bundle_dir / "manifest.json",
            repo_root=campaign.ROOT,
            contract_path=contract_path,
            allow_dirty=True,
        )
        self.assertEqual(validation["status"], "passed", validation)


if __name__ == "__main__":
    unittest.main()
