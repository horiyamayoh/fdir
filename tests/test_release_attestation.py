from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from tools import release_attestation as attestation


class ReleaseAttestationTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40
    RUN_ID = "123"
    ATTEMPT = 2
    DIGEST = "b" * 64

    def _environment(self, **overrides: str) -> dict[str, str]:
        value = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": attestation.REPOSITORY,
            "GITHUB_SHA": self.SOURCE_SHA,
            "GITHUB_RUN_ID": self.RUN_ID,
            "GITHUB_RUN_ATTEMPT": str(self.ATTEMPT),
            "GITHUB_JOB": "qualification",
        }
        value.update(overrides)
        return value

    def _artifact_record(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "status": "uploaded",
            "name": f"{attestation.QUALIFICATION_BUNDLE_PREFIX}{self.SOURCE_SHA}-attempt-{self.ATTEMPT}",
            "id": "456",
            "digest": f"sha256:{self.DIGEST}",
            "sha256": self.DIGEST,
            "url": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/artifacts/456",
            "sizeInBytes": 1,
            "expired": False,
            "expiresAt": "2099-01-01T00:00:00Z",
            "producerJob": attestation.QUALIFICATION_JOB_NAME,
            "producerStep": attestation.UPLOAD_BUNDLE_STEP,
            "workflowRun": {"id": self.RUN_ID, "head_sha": self.SOURCE_SHA},
        }
        value.update(overrides)
        return value

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
                artifact_id="456",
                artifact_digest="b" * 64,
                artifact_url=f"https://github.com/{attestation.REPOSITORY}/actions/runs/123/artifacts/456",
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

    def test_local_environment_cannot_fabricate_final_ci_metadata(self) -> None:
        with self.assertRaises(attestation.AttestationError) as raised:
            attestation._ci_metadata(
                self.SOURCE_SHA,
                artifact_id="456",
                artifact_digest=self.DIGEST,
                artifact_url=f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/artifacts/456",
                environment={
                    "GITHUB_ACTIONS": "false",
                    "GITHUB_REPOSITORY": attestation.REPOSITORY,
                    "GITHUB_SHA": self.SOURCE_SHA,
                    "GITHUB_RUN_ID": self.RUN_ID,
                    "GITHUB_RUN_ATTEMPT": str(self.ATTEMPT),
                    "GITHUB_JOB": "qualification",
                },
            )
        self.assertEqual(raised.exception.code, "ATTESTATION_CI_PROVIDER")

    def test_synthetic_or_wrong_run_identity_is_rejected(self) -> None:
        for field, value, code in (
            ("GITHUB_RUN_ID", "local", "ATTESTATION_RUN_ID"),
            ("GITHUB_RUN_ID", "0", "ATTESTATION_RUN_ID"),
            ("GITHUB_REPOSITORY", "someone/else", "ATTESTATION_REPOSITORY_MISMATCH"),
            ("GITHUB_SHA", "c" * 40, "ATTESTATION_SHA_MISMATCH"),
            ("GITHUB_RUN_ATTEMPT", "0", "ATTESTATION_ATTEMPT"),
            ("GITHUB_JOB", "synthetic", "ATTESTATION_JOB"),
        ):
            with self.subTest(field=field, value=value):
                environment = self._environment(**{field: value})
                with self.assertRaises(attestation.AttestationError) as raised:
                    attestation._ci_metadata(
                        self.SOURCE_SHA,
                        artifact_id="456",
                        artifact_digest=self.DIGEST,
                        artifact_url=f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/artifacts/456",
                        environment=environment,
                    )
                self.assertEqual(raised.exception.code, code)

    def test_wrong_or_unfinished_workflow_and_step_are_rejected(self) -> None:
        run = {
            "id": self.RUN_ID,
            "attempt": self.ATTEMPT,
            "status": "completed",
            "conclusion": "success",
            "headSha": self.SOURCE_SHA,
            "path": attestation.WORKFLOW_PATH,
            "url": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}",
            "attemptUrl": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/attempts/{self.ATTEMPT}",
        }
        failed = dict(run, status="in_progress", conclusion=None)
        with self.assertRaises(attestation.AttestationError) as workflow_error:
            attestation._validate_run_record(
                failed,
                repository=attestation.REPOSITORY,
                source_sha=self.SOURCE_SHA,
                expected_run_id=self.RUN_ID,
                expected_attempt=self.ATTEMPT,
            )
        self.assertEqual(workflow_error.exception.code, "ATTESTATION_WORKFLOW_NOT_SUCCESS")

        step = {
            "number": 9,
            "name": attestation.UPLOAD_BUNDLE_STEP,
            "status": "completed",
            "conclusion": "failure",
            "url": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/job/789#step:9",
        }
        with self.assertRaises(attestation.AttestationError) as step_error:
            attestation._validate_step_record(
                step,
                repository=attestation.REPOSITORY,
                run_id=self.RUN_ID,
                job_id="789",
                expected_name=attestation.UPLOAD_BUNDLE_STEP,
            )
        self.assertEqual(step_error.exception.code, "ATTESTATION_STEP_NOT_SUCCESS")

    def test_artifact_digest_empty_and_retention_bindings_are_fail_closed(self) -> None:
        valid = self._artifact_record()
        for mutation, code in (
            ({"sizeInBytes": 0}, "ATTESTATION_ARTIFACT_EMPTY"),
            ({"digest": "sha256:" + "c" * 64}, "ATTESTATION_ARTIFACT_DIGEST_MISMATCH"),
            ({"expired": True}, "ATTESTATION_ARTIFACT_RETENTION"),
            ({"expiresAt": "2020-01-01T00:00:00Z"}, "ATTESTATION_ARTIFACT_RETENTION"),
            ({"workflowRun": {"id": "999", "head_sha": self.SOURCE_SHA}}, "ATTESTATION_ARTIFACT_RUN_MISMATCH"),
        ):
            with self.subTest(mutation=mutation):
                candidate = dict(valid)
                candidate.update(mutation)
                with self.assertRaises(attestation.AttestationError) as raised:
                    attestation._validate_evidence_artifact(
                        candidate,
                        repository=attestation.REPOSITORY,
                        source_sha=self.SOURCE_SHA,
                        run_id=self.RUN_ID,
                        attempt=self.ATTEMPT,
                        expected_name=valid["name"],
                        expected_id="456",
                        expected_digest=self.DIGEST,
                        expected_url=valid["url"],
                        expected_producer_step=attestation.UPLOAD_BUNDLE_STEP,
                        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
                    )
                self.assertEqual(raised.exception.code, code)

    def test_missing_upload_receipt_and_local_url_are_rejected(self) -> None:
        with self.assertRaises(attestation.AttestationError) as missing:
            attestation._validate_upload_receipt(
                None,
                repository=attestation.REPOSITORY,
                source_sha=self.SOURCE_SHA,
                run_id=self.RUN_ID,
                attempt=self.ATTEMPT,
                job_id="qualification",
                artifact_id="456",
                artifact_digest=self.DIGEST,
                artifact_url=f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/artifacts/456",
            )
        self.assertEqual(missing.exception.code, "ATTESTATION_UPLOAD_RECEIPT_MISSING")

        run = {
            "id": self.RUN_ID,
            "attempt": self.ATTEMPT,
            "status": "completed",
            "conclusion": "success",
            "headSha": self.SOURCE_SHA,
            "path": attestation.WORKFLOW_PATH,
            "url": "local://run",
            "attemptUrl": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/attempts/{self.ATTEMPT}",
        }
        with self.assertRaises(attestation.AttestationError) as local_url:
            attestation._validate_run_record(
                run,
                repository=attestation.REPOSITORY,
                source_sha=self.SOURCE_SHA,
                expected_run_id=self.RUN_ID,
                expected_attempt=self.ATTEMPT,
            )
        self.assertEqual(local_url.exception.code, "ATTESTATION_RUN_URL")

    def test_supply_chain_requires_external_non_circular_bindings(self) -> None:
        artifact = {
            "id": "456",
            "name": f"{attestation.QUALIFICATION_BUNDLE_PREFIX}{self.SOURCE_SHA}-attempt-{self.ATTEMPT}",
            "digest": self.DIGEST,
            "url": f"https://github.com/{attestation.REPOSITORY}/actions/runs/{self.RUN_ID}/artifacts/456",
        }
        bundle = {"manifestDigest": "c" * 64, "manifestFileDigest": "d" * 64}
        with self.assertRaises(attestation.AttestationError) as missing:
            attestation._validate_supply_chain(
                None,
                source_sha=self.SOURCE_SHA,
                run_id=self.RUN_ID,
                attempt=self.ATTEMPT,
                bundle=bundle,
                artifact=artifact,
                now=datetime(2026, 8, 21, tzinfo=timezone.utc),
            )
        self.assertEqual(missing.exception.code, "ATTESTATION_SUPPLY_CHAIN_MISSING")

        supply_chain = {
            "candidateBundle": {
                "kind": "candidate-bundle",
                "provider": "github-actions",
                "sourceSha": self.SOURCE_SHA,
                "runId": self.RUN_ID,
                "attempt": self.ATTEMPT,
                "jobId": "qualification",
                "artifactId": "456",
                "artifactName": artifact["name"],
                "artifactDigest": self.DIGEST,
                "artifactUrl": artifact["url"],
                "retention": {"expired": False, "expiresAt": "2099-01-01T00:00:00Z"},
                "manifestDigest": "c" * 64,
                "manifestFileDigest": "d" * 64,
                "verification": {"status": "verified", "method": "independent-ci"},
                "selfAttested": False,
                "circular": False,
            },
        }
        for field, kind, method in (
            ("package", "package", "independent-ci"),
            ("sbom", "sbom", "independent-ci"),
            ("dependencyLock", "dependency-lock", "independent-ci"),
            ("signature", "signature", "signature-verification"),
            ("provenance", "provenance", "provenance-verification"),
        ):
            supply_chain[field] = {
                "kind": kind,
                "provider": "github-actions",
                "sourceSha": self.SOURCE_SHA,
                "path": f"artifacts/supply-chain/{field}.json",
                "sha256": "e" * 64,
                "verification": {"status": "verified", "method": method},
                "selfAttested": False,
                "circular": False,
            }
        result = attestation._validate_supply_chain(
            supply_chain,
            source_sha=self.SOURCE_SHA,
            run_id=self.RUN_ID,
            attempt=self.ATTEMPT,
            bundle=bundle,
            artifact=artifact,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        self.assertIs(result, supply_chain)
        supply_chain["signature"]["circular"] = True
        with self.assertRaises(attestation.AttestationError) as circular:
            attestation._validate_supply_chain(
                supply_chain,
                source_sha=self.SOURCE_SHA,
                run_id=self.RUN_ID,
                attempt=self.ATTEMPT,
                bundle=bundle,
                artifact=artifact,
                now=datetime(2026, 8, 21, tzinfo=timezone.utc),
            )
        self.assertEqual(circular.exception.code, "ATTESTATION_SUPPLY_CHAIN_CIRCULAR")


if __name__ == "__main__":
    unittest.main()
