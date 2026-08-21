from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest import mock

from tools import release_attestation as attestation


class ReleaseAttestationTests(unittest.TestCase):
    def test_missing_artifact_provenance_is_rejected(self) -> None:
        with self.assertRaises(attestation.AttestationError) as raised:
            attestation._ci_metadata(
                "a" * 40,
                artifact_id=None,
                artifact_digest=None,
                artifact_url=None,
                environment={"GITHUB_ACTIONS": "false", "GITHUB_REPOSITORY": attestation.REPOSITORY},
            )
        self.assertEqual(raised.exception.code, "ATTESTATION_ARTIFACT_MISSING")

    def test_candidate_105_report_is_pending_until_external_attestation(self) -> None:
        root = attestation.ROOT
        producer = json.dumps({
            "schema": "fdir/qualification-producer-report",
            "version": "1.0.0",
            "evidenceId": "issue-105-release-quality",
            "requirementIds": ["QUAL-105-RELEASE-BARRIER"],
            "independence": {"expectedDerivedFromActual": False},
            "status": "passed",
            "failureCount": 0,
        })
        report = {
            "evidenceId": "issue-105-release-quality",
            "command": ["python", "tools/qualification_issue105.py", "--out-dir", "e2e/.run/qualification-issue-105"],
            "outputs": [{"path": "artifacts/105/producer-report.json"}],
        }
        with mock.patch.object(Path, "read_text", return_value=producer):
            receipt = attestation._candidate_105_receipt(root, report)
            self.assertEqual(receipt["status"], "pending-attestation")

    def test_release_gate_cannot_be_the_phase_a_105_producer(self) -> None:
        root = attestation.ROOT
        report = {
            "evidenceId": "issue-105-release-quality",
            "command": ["python", "tools/release_gate.py"],
            "outputs": [{"path": "artifacts/105/producer-report.json"}],
        }
        with mock.patch.object(Path, "read_text", return_value="{}"):
            with self.assertRaises(attestation.AttestationError) as raised:
                attestation._candidate_105_receipt(root, report)
            self.assertEqual(raised.exception.code, "CIRCULAR_105_EVIDENCE")

    def test_final_attestation_cannot_use_wrong_attempt(self) -> None:
        # The binding check is deliberately exercised independently of bundle
        # contents; a candidate with a mismatched CI attempt is never final.
        with self.assertRaises(attestation.AttestationError) as raised:
            attestation._ci_metadata(
                "a" * 40,
                artifact_id="artifact-1",
                artifact_digest="b" * 64,
                artifact_url="https://example.invalid/artifact",
                environment={
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_REPOSITORY": attestation.REPOSITORY,
                    "GITHUB_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "0",
                    "GITHUB_JOB": "design",
                },
            )
        self.assertEqual(raised.exception.code, "ATTESTATION_ATTEMPT")

    def test_local_ci_provider_is_not_final_authority(self) -> None:
        with self.assertRaises(attestation.AttestationError) as raised:
            attestation._validate_final_ci_provider({"provider": "local"})
        self.assertEqual(raised.exception.code, "ATTESTATION_CI_PROVIDER")


if __name__ == "__main__":
    unittest.main()
