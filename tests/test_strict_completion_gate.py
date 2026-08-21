import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from tools import release_gate, strict_completion_gate, validate_qualification_bundle


class StrictCompletionGateTests(unittest.TestCase):
    def _write_bundle_fixture(
        self,
        root: Path,
        *,
        issue_105_command: list[str] | None = None,
        manifest_updates: dict[str, object] | None = None,
    ) -> Path:
        reports = root / "reports"
        reports.mkdir(parents=True)
        manifest_value: dict[str, object] = {
            "schema": "fdir/qualification-bundle-manifest",
            "version": "1.0.0",
            "repository": "horiyamayoh/fdir",
            "sourceSha": "a" * 40,
            "dirtyTree": False,
            "generatedAt": "2026-08-21T00:00:00Z",
            "manifestDigest": "b" * 64,
            "files": [],
            "evidenceIds": [],
            "issueNumbers": list(range(88, 106)),
            "targetIssueNumbers": list(strict_completion_gate.LIVE_ISSUES),
            "barrierCoverage": {
                "issue-88-qualification-contract": {
                    "role": "integrity-report",
                    "issueNumbers": list(strict_completion_gate.BARRIER_ISSUES),
                },
                "issue-105-release-quality": {
                    "role": "final-release-report",
                    "issueNumbers": [87, *strict_completion_gate.BARRIER_ISSUES],
                },
            },
        }
        if manifest_updates:
            manifest_value.update(manifest_updates)
        for issue in range(88, 106):
            evidence_id = f"issue-{issue}-fixture"
            report: dict[str, object] = {
                "schema": "fdir/qualification-evidence",
                "evidenceId": evidence_id,
                "issueNumbers": [issue],
                "sourceSha": "a" * 40,
                "status": "passed",
                "failureCount": 0,
            }
            if issue == 88:
                report["evidenceId"] = "issue-88-qualification-contract"
            if issue == 105:
                report["evidenceId"] = "issue-105-release-quality"
                report["command"] = issue_105_command or ["python", "tools/qualification_issue105.py"]
                report["outputs"] = [{"path": "artifacts/105/producer-report.json"}]
                producer = root / "artifacts" / "105" / "producer-report.json"
                producer.parent.mkdir(parents=True)
                producer.write_text("{}", encoding="utf-8")
            (reports / f"{evidence_id}.json").write_text(json.dumps(report), encoding="utf-8")
        manifest_value["evidenceIds"] = sorted(
            json.loads(path.read_text(encoding="utf-8"))["evidenceId"]
            for path in reports.glob("*.json")
        )
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
        return manifest

    def _full_live_snapshot(
        self,
        *,
        umbrella_state: str = "closed",
        umbrella_reason: str | None = "completed",
        umbrella_closed_at: str | None = "2026-08-21T02:00:00Z",
        child_closed_at: str | None = "2026-08-21T01:00:00Z",
    ) -> dict[str, object]:
        issues = []
        for issue in strict_completion_gate.LIVE_ISSUES:
            if issue == 87:
                issues.append(
                    {
                        "issueNumber": issue,
                        "state": umbrella_state,
                        "stateReason": umbrella_reason,
                        "closedAt": umbrella_closed_at,
                        "updatedAt": "2026-08-21T02:00:00Z",
                    }
                )
            else:
                issues.append(
                    {
                        "issueNumber": issue,
                        "state": "closed",
                        "stateReason": "completed",
                        "closedAt": child_closed_at,
                        "updatedAt": "2026-08-21T01:00:00Z",
                    }
                )
        return {"snapshotDigest": "c" * 64, "issues": issues}

    def _run_release_gate(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = release_gate.main(argv)
        return exit_code, json.loads(output.getvalue())

    def test_bundle_manifest_binds_target_scope_and_barrier_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_bundle_fixture(root)
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            contract = json.loads(
                (strict_completion_gate.ROOT / "machine" / "qualification-contract.json").read_text(
                    encoding="utf-8"
                )
            )
            diagnostics: list[dict[str, str]] = []
            validate_qualification_bundle._validate_manifest_header(
                manifest_value,
                diagnostics,
                strict_scope=True,
            )
            report_by_id = {
                report["evidenceId"]: (path.name, report)
                for path in sorted((root / "reports").glob("*.json"))
                for report in [json.loads(path.read_text(encoding="utf-8"))]
            }
            validate_qualification_bundle._validate_manifest_barrier_coverage(
                manifest_value,
                contract,
                report_by_id,
                diagnostics,
                strict_scope=True,
            )

            self.assertEqual(manifest_value["targetIssueNumbers"], list(strict_completion_gate.LIVE_ISSUES))
            self.assertEqual(
                manifest_value["barrierCoverage"],
                {
                    "issue-88-qualification-contract": {
                        "role": "integrity-report",
                        "issueNumbers": list(strict_completion_gate.BARRIER_ISSUES),
                    },
                    "issue-105-release-quality": {
                        "role": "final-release-report",
                        "issueNumbers": [87, *strict_completion_gate.BARRIER_ISSUES],
                    },
                },
            )
            self.assertEqual(diagnostics, [])

    def test_release_gate_rejects_bundle_without_final_attestation(self) -> None:
        exit_code, summary = self._run_release_gate(["--bundle", "candidate/manifest.json"])

        self.assertEqual(exit_code, 1)
        self.assertFalse(summary["releaseReady"])
        self.assertEqual(summary["diagnostics"][0]["code"], "FINAL_ATTESTATION_REQUIRED")

    def test_release_gate_rejects_attestation_without_candidate_bundle(self) -> None:
        exit_code, summary = self._run_release_gate(["--attestation", "candidate/attestation.json"])

        self.assertEqual(exit_code, 1)
        self.assertFalse(summary["releaseReady"])
        self.assertEqual(summary["diagnostics"][0]["code"], "BUNDLE_REQUIRED")

    def test_barrier_issues_cannot_be_bound_to_duplicate_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_bundle_fixture(root)
            duplicate = {
                "schema": "fdir/qualification-evidence",
                "evidenceId": "issue-108-duplicate-report",
                "issueNumbers": [108],
                "sourceSha": "a" * 40,
                "status": "passed",
                "failureCount": 0,
            }
            (root / "reports" / "issue-108-duplicate-report.json").write_text(
                json.dumps(duplicate),
                encoding="utf-8",
            )

            _, _, scope_blockers = strict_completion_gate._load_bundle_scope(manifest)
            self.assertIn("BUNDLE_REPORT_SCOPE", {item["code"] for item in scope_blockers})

            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            contract = json.loads(
                (strict_completion_gate.ROOT / "machine" / "qualification-contract.json").read_text(
                    encoding="utf-8"
                )
            )
            report_by_id = {
                report["evidenceId"]: (path.name, report)
                for path in sorted((root / "reports").glob("*.json"))
                for report in [json.loads(path.read_text(encoding="utf-8"))]
            }
            diagnostics: list[dict[str, str]] = []
            validate_qualification_bundle._validate_manifest_barrier_coverage(
                manifest_value,
                contract,
                report_by_id,
                diagnostics,
                strict_scope=True,
            )

            self.assertIn("BARRIER_REPORT_FORBIDDEN", {item["code"] for item in diagnostics})

    def test_final_live_state_requires_completed_issue_87(self) -> None:
        complete = strict_completion_gate._live_issue_state(self._full_live_snapshot())
        self.assertEqual(complete["status"], "verified")
        self.assertNotIn("ISSUE_NOT_COMPLETED", {item["code"] for item in complete["blockers"]})

        incomplete = strict_completion_gate._live_issue_state(
            self._full_live_snapshot(
                umbrella_state="open",
                umbrella_reason=None,
                umbrella_closed_at=None,
            )
        )
        self.assertEqual(incomplete["status"], "blocked")
        self.assertIn("ISSUE_NOT_COMPLETED", {item["code"] for item in incomplete["blockers"]})

    def test_issue_87_must_be_the_final_closure(self) -> None:
        live_by_number = release_gate._validate_live_issue_scope(self._full_live_snapshot())
        release_gate._require_umbrella_closed_last(live_by_number)

        with self.assertRaises(release_gate.GateError) as context:
            release_gate._require_umbrella_closed_last(
                release_gate._validate_live_issue_scope(
                    self._full_live_snapshot(
                        umbrella_closed_at="2026-08-21T00:30:00Z",
                        child_closed_at="2026-08-21T01:00:00Z",
                    )
                )
            )
        self.assertEqual(context.exception.code, "UMBRELLA_NOT_LAST")

    def test_no_bundle_mode_is_visibly_blocked_and_never_release_ready(self) -> None:
        report = strict_completion_gate.run()

        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["releaseReady"])
        self.assertEqual(report["issues"], list(strict_completion_gate.LIVE_ISSUES))
        self.assertEqual(report["qualificationIssues"], list(strict_completion_gate.QUALIFICATION_ISSUES))
        self.assertEqual(report["barrierIssues"], list(strict_completion_gate.BARRIER_ISSUES))
        self.assertIn("LEGACY_COMPLETION_PATH_DISABLED", {item["code"] for item in report["blockers"]})

    def test_bundle_manifest_must_declare_exact_recovery_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_bundle_fixture(root, manifest_updates={"issueNumbers": [88]})

            _, _, blockers = strict_completion_gate._load_bundle_scope(manifest)

            self.assertIn("BUNDLE_ISSUE_SCOPE", {item["code"] for item in blockers})

    def test_bundle_rejects_issue_105_self_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_bundle_fixture(
                root,
                issue_105_command=["python", "tools/strict_completion_gate.py", "--bundle", "candidate.json"],
            )
            manifest_value, reports, scope_blockers = strict_completion_gate._load_bundle_scope(manifest)

            self.assertEqual(scope_blockers, [])
            blockers = strict_completion_gate._candidate_105_blockers(manifest_value, reports, root)
            self.assertIn("CIRCULAR_105_EVIDENCE", {item["code"] for item in blockers})

    def test_bundle_rejects_static_release_ready_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_bundle_fixture(root, manifest_updates={"releaseReady": True})
            manifest_value, reports, scope_blockers = strict_completion_gate._load_bundle_scope(manifest)

            self.assertEqual(scope_blockers, [])
            blockers = strict_completion_gate._candidate_105_blockers(manifest_value, reports, root)
            self.assertIn("STATIC_RELEASE_READY_CONTRADICTION", {item["code"] for item in blockers})

    def test_bundle_issue_reports_explain_missing_issue_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir(parents=True)
            (reports / "issue-88.json").write_text(
                json.dumps(
                    {
                        "schema": "fdir/qualification-evidence",
                        "evidenceId": "issue-88-fixture",
                        "issueNumbers": [88],
                        "sourceSha": "a" * 40,
                        "status": "passed",
                        "failureCount": 0,
                        "assertions": [{"id": "assertion-88"}],
                        "cases": [{"id": "case-88"}],
                        "outputs": [{"path": "artifacts/88/result.json", "role": "result", "sha256": "b" * 64}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"sourceSha": "a" * 40}), encoding="utf-8")

            issue_reports = strict_completion_gate._bundle_issue_reports(manifest)

            self.assertEqual([item["issueNumber"] for item in issue_reports], list(range(88, 106)))
            self.assertEqual(issue_reports[0]["status"], "passed")
            self.assertEqual(issue_reports[0]["evidenceIds"], ["issue-88-fixture"])
            self.assertEqual(issue_reports[0]["assertionCount"], 1)
            self.assertEqual(issue_reports[0]["testCaseCount"], 1)
            self.assertEqual(issue_reports[0]["liveState"], "pending-final-attestation")
            self.assertEqual(issue_reports[1]["status"], "blocked")
            self.assertEqual(issue_reports[1]["blockers"][0]["code"], "ISSUE_EVIDENCE_MISSING")

            snapshot = {
                "snapshotDigest": "c" * 64,
                "issues": [
                    {
                        "issueNumber": 88,
                        "state": "closed",
                        "stateReason": "completed",
                        "closedAt": "2026-08-21T00:00:00Z",
                        "updatedAt": "2026-08-21T00:00:00Z",
                    },
                    {
                        "issueNumber": 89,
                        "state": "open",
                        "stateReason": None,
                        "closedAt": None,
                        "updatedAt": "2026-08-21T00:00:00Z",
                    },
                ],
            }
            attested = strict_completion_gate._attested_issue_reports(manifest, snapshot)
            self.assertEqual(attested[0]["liveState"], "verified")
            self.assertEqual(attested[0]["snapshotDigest"], "c" * 64)
            self.assertEqual(attested[1]["liveState"], "blocked")
            self.assertIn("ISSUE_NOT_COMPLETED", {item["code"] for item in attested[1]["blockers"]})


if __name__ == "__main__":
    unittest.main()
