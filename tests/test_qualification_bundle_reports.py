from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from tools.validate_qualification_bundle import ROOT, _validate_behavioral_report_declarations


class BehavioralReportValidationTests(unittest.TestCase):
    def test_declared_report_payload_is_checked_beyond_evidence_envelope(self) -> None:
        root = ROOT / "e2e" / ".run" / f"behavioral-report-validator-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            target = root / "artifacts" / "93" / "style.json"
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "schema": "fdir/qualification-issue-93-report",
                        "version": "1.0.0",
                        "reportKind": "style-cascade-vectors",
                        "evidenceId": "issue-93",
                        "issueNumber": 93,
                        "requirementIds": ["QUAL-93"],
                        "sourceSha": "a" * 40,
                        "status": "passed",
                        "assertions": [{}],
                        "cases": [{}],
                        "independence": {"expectedDerivedFromActual": True},
                        "waivers": [{"waiverId": "forged"}],
                    }
                ),
                encoding="utf-8",
            )
            contract = {
                "scope": {"issueNumbers": list(range(88, 106))},
                "behavioralReportContract": {
                    "policies": {
                        "requiredReportFields": [
                            "schema", "version", "issueNumber", "evidenceId", "requirementIds",
                            "sourceSha", "status", "assertions", "cases", "independence", "waivers",
                        ],
                        "requiredAssertionFields": ["assertionId", "result"],
                        "requiredCaseFields": ["caseId", "result"],
                        "requiredIndependenceFields": ["grade", "expectedDerivedFromActual"],
                    },
                    "requirements": [
                        {
                            "ownerIssue": 93,
                            "evidenceId": "issue-93",
                            "requirementId": "QUAL-93",
                            "reports": [
                                {
                                    "reportId": "issue-93.style-cascade-vectors",
                                    "bundlePath": "artifacts/93/style.json",
                                    "outputRole": "style-cascade-report",
                                    "reportKind": "style-cascade-vectors",
                                    "schema": "fdir/qualification-issue-93-report",
                                    "schemaVersion": "1.0.0",
                                }
                            ],
                        }
                    ],
                },
            }
            report_by_id = {
                "issue-93": (
                    "reports/issue-93.json",
                    {"outputs": [{"path": "artifacts/93/style.json", "role": "style-cascade-report"}]},
                )
            }
            diagnostics: list[dict[str, str]] = []
            _validate_behavioral_report_declarations(
                root,
                report_by_id,
                {"sourceSha": "a" * 40},
                diagnostics,
                contract,
            )
        finally:
            shutil.rmtree(root)

        codes = {item["code"] for item in diagnostics}
        self.assertIn("BEHAVIORAL_ASSERTION_FIELDS", codes)
        self.assertIn("BEHAVIORAL_CASE_FIELDS", codes)
        self.assertIn("BEHAVIORAL_INDEPENDENCE_FIELDS", codes)
        self.assertIn("BEHAVIORAL_INDEPENDENCE_DERIVATION", codes)
        self.assertIn("BEHAVIORAL_WAIVER", codes)


if __name__ == "__main__":
    unittest.main()
