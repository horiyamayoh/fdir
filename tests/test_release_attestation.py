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

    def test_bundleless_105_report_is_only_accepted_as_blocked_receipt(self) -> None:
        root = attestation.ROOT
        stdout = root / "tests" / "fixture-issue-105.stdout.txt"
        pending = json.dumps({
                "schema": "fdir/release-gate-summary",
                "status": "blocked",
                "mode": "smoke",
                "releaseReady": False,
                "diagnostics": [{"code": "RELEASE_AUTHORITY_REQUIRED"}],
            })
        report = {"evidenceId": "issue-105-release-quality", "command": ["python", "tools/release_gate.py"], "outputs": [{"path": "logs/issue-105.stdout.txt"}]}
        with mock.patch.object(Path, "read_text", return_value=pending):
            receipt = attestation._candidate_105_receipt(root, report)
            self.assertEqual(receipt["status"], "pending-attestation")

        passed = json.dumps({
                "schema": "fdir/release-gate-summary",
                "status": "passed",
                "mode": "release",
                "releaseReady": True,
                "diagnostics": [],
            })
        with mock.patch.object(Path, "read_text", return_value=passed):
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


if __name__ == "__main__":
    unittest.main()
