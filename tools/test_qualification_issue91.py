"""Focused tests for the issue-91 aggregate integration runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
TEST_OUTPUT_ROOT = ROOT / ".qualification-issue-91-test-output"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from qualification_issue91 import (  # noqa: E402
    FORMAT_NAMES,
    PRODUCER_REPORT_NAME,
    REPORT_NAMES,
    REPORT_SCHEMA_PATH,
    git_head_sha,
    qualify_corpus,
)
from qualification_evidence import validate_producer_report_shape  # noqa: E402


def _output_directory(name: str) -> Path:
    """Use deterministic workspace output directories; managed temp mounts are read-only."""

    output = TEST_OUTPUT_ROOT / name
    output.mkdir(parents=True, exist_ok=True)
    return output


class QualificationIssue91Tests(unittest.TestCase):
    def test_checked_in_corpus_has_all_four_formats(self) -> None:
        aggregate, reports = qualify_corpus(out_dir=_output_directory("formats"))
        self.assertEqual(set(FORMAT_NAMES), set(aggregate["formats"]))
        self.assertTrue(all(set(FORMAT_NAMES) == set(reports[name]["formats"]) for name in REPORT_NAMES))
        self.assertEqual(16, aggregate["caseCount"])
        self.assertGreater(aggregate["sourceOccurrenceCount"], 0)

    def test_reports_are_complete_and_source_sha_bound(self) -> None:
        output = _output_directory("reports")
        aggregate, reports = qualify_corpus(out_dir=output)
        self.assertEqual(
            set(REPORT_NAMES) | {PRODUCER_REPORT_NAME},
            {path.name for path in output.iterdir() if path.is_file()},
        )
        for name in REPORT_NAMES:
            document = json.loads((output / name).read_text(encoding="utf-8"))
            self.assertEqual("fdir/qualification-issue-91-aggregate-report", document["schema"])
            self.assertEqual(91, document["issueNumber"])
            self.assertEqual(git_head_sha(ROOT), document["sourceSha"])
            self.assertEqual(aggregate["sourceSha"], document["sourceSha"])
            self.assertRegex(document["sourceSha"], r"^[0-9a-f]{40}$")
            self.assertEqual(aggregate["caseSourceDigest"], document["caseSourceDigest"])
            self.assertRegex(document["caseSourceDigest"], r"^[0-9a-f]{64}$")
            self.assertEqual("git-head", document["sourceShaKind"])
            self.assertEqual("sha256(canonical-case-source-digest-manifest)", document["caseSourceDigestKind"])
            for summary in document["caseSummaries"]:
                self.assertNotIn("sourceSha", summary)
                self.assertRegex(summary["caseSourceSha"], r"^[0-9a-f]{64}$")
            self.assertEqual(reports[name]["status"], document["status"])
            self.assertEqual(reports[name], document)

        producer = json.loads((output / PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([], validate_producer_report_shape(producer))
        self.assertEqual(aggregate["sourceSha"], producer["sourceSha"])
        self.assertFalse(producer["independence"]["expectedDerivedFromActual"])
        self.assertEqual(len(aggregate["caseSummaries"]), len(producer["testCases"]))
        self.assertEqual(
            {case["caseId"] for case in producer["testCases"]},
            {assertion["testCaseId"] for assertion in producer["assertions"]},
        )
        self.assertIn("positive", {case["classification"] for case in producer["testCases"]})
        self.assertTrue({"negative", "mutation"} & {case["classification"] for case in producer["testCases"]})
        expected_status = (
            "passed"
            if all(document["status"] == "passed" for document in reports.values())
            and all(case["result"] == "passed" for case in producer["testCases"])
            and all(assertion["status"] == "passed" for assertion in producer["assertions"])
            else "failed"
        )
        self.assertEqual(expected_status, producer["status"])
        self.assertEqual(
            sum(assertion["status"] != "passed" for assertion in producer["assertions"])
            + sum(case["result"] != "passed" for case in producer["testCases"]),
            producer["failureCount"],
        )

        report_schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual("^[0-9a-f]{40}$", report_schema["properties"]["sourceSha"]["pattern"])
        self.assertEqual("^[0-9a-f]{64}$", report_schema["properties"]["caseSourceDigest"]["pattern"])

    def test_adapter_bindings_are_real_and_all_occurrences_are_accounted(self) -> None:
        aggregate, reports = qualify_corpus(out_dir=_output_directory("bindings"))
        self.assertTrue(aggregate["adapterIrProvided"])
        self.assertTrue(aggregate["accountingInputProvided"])
        self.assertEqual(0, len(aggregate["unaccountedOccurrenceIds"]))
        self.assertEqual(0, len(aggregate["duplicateSourceOccurrenceIds"]))
        self.assertGreater(aggregate["sourceBindingCount"], 0)
        self.assertEqual(aggregate["status"], reports[REPORT_NAMES[2]]["aggregateStatus"])
        self.assertFalse(reports[REPORT_NAMES[2]]["completeAllowed"])

    def test_noneligible_occurrences_override_adapter_complete_self_report(self) -> None:
        aggregate, _reports = qualify_corpus(out_dir=_output_directory("strict-status"))
        summaries = {item["caseId"]: item for item in aggregate["caseSummaries"]}
        for case_id, expected_status in {
            "docx-story-independent": "partial",
            "xlsx-independent": "partial",
        }.items():
            self.assertEqual(expected_status, summaries[case_id]["expectedStatus"])
            self.assertEqual("partial", summaries[case_id]["conversionStatus"])
            self.assertIn(
                summaries[case_id]["adapterReportedConversionStatus"],
                {"complete", "complete-with-warnings"},
            )
            self.assertTrue(summaries[case_id]["caseQualified"])

    def test_cli_emits_qualified_reports(self) -> None:
        output = _output_directory("cli")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "qualification_issue91.py"),
                "--out-dir",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        semantic_statuses = {
            json.loads((output / name).read_text(encoding="utf-8"))["status"]
            for name in REPORT_NAMES
        }
        self.assertEqual(0 if semantic_statuses == {"passed"} else 1, result.returncode)
        self.assertEqual(
            set(REPORT_NAMES) | {PRODUCER_REPORT_NAME},
            {path.name for path in output.iterdir() if path.is_file()},
        )
        producer = json.loads((output / PRODUCER_REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([], validate_producer_report_shape(producer))
        self.assertEqual(
            "passed" if semantic_statuses == {"passed"} else "failed",
            producer["status"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
