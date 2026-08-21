import json
from pathlib import Path
import shutil
import unittest

from tools import strict_completion_gate


class StrictCompletionGateTests(unittest.TestCase):
    def test_bundle_issue_reports_explain_missing_issue_evidence(self) -> None:
        root = Path(__file__).resolve().parents[1] / "e2e" / ".run" / "strict-completion-report-fixture"
        if root.exists():
            shutil.rmtree(root)
        try:
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
        finally:
            if root.exists():
                shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
