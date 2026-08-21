"""Focused tests for the bounded GitHub issue #98 qualification lane."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import unittest

try:
    import qualification_issue98 as runner
except ImportError:  # pragma: no cover
    from tools import qualification_issue98 as runner

try:
    from qualification_evidence import validate_producer_report_shape
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_producer_report_shape


ROOT = Path(__file__).resolve().parents[1]
TEST_WORK_DIR = ROOT / "e2e" / ".run" / "qualification-issue-98-test"
TEST_REPORT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-98-test-reports"


class QualificationIssue98Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = runner._load_corpus()
        TEST_WORK_DIR.mkdir(parents=True, exist_ok=True)
        TEST_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    def test_authored_corpus_keys_and_expected_values_are_pinned(self) -> None:
        self.assertEqual(
            {"documents", "canonicalVectors", "stableEntityCases", "migrationCases"},
            {"documents", "canonicalVectors", "stableEntityCases", "migrationCases"}
            & set(self.corpus),
        )
        oracle = self.corpus["oracle"]
        self.assertTrue(oracle["expectedBytesAreAuthored"])
        self.assertTrue(oracle["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(oracle["adapterHelpersUsedForExpected"])
        self.assertNotIn("TemporaryDirectory", Path(runner.__file__).read_text(encoding="utf-8"))

        accepted = []
        for vector in self.corpus["canonicalVectors"]:
            values = vector.get("expectedByProjection", {}).values()
            if not values and vector.get("expectedRef") is None:
                values = (vector.get("expected", {}),)
            accepted.extend(value for value in values if value.get("outcome") == "accepted")
        self.assertTrue(accepted)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) for value in accepted))

        required_tags = runner.REQUIRED_NEGATIVE_TAGS
        actual_tags = {item["tag"] for item in self.corpus["negativeCases"]}
        self.assertTrue(required_tags.issubset(actual_tags))

    def test_python_and_independent_node_oracles_match_authored_vectors(self) -> None:
        node_path = shutil.which("node")
        results, assertions = runner._run_canonical_vectors(self.corpus, TEST_WORK_DIR, node_path)
        self.assertTrue(results)
        self.assertTrue(assertions)
        if node_path:
            self.assertTrue(all(item["status"] == "passed" for item in results), results)
            self.assertTrue(all(item["status"] == "passed" for item in assertions), assertions)
        else:
            self.assertTrue(all(item["node"].get("status") == "unavailable" for item in results), results)
            self.assertTrue(all(item["status"] == "failed" for item in results), results)

    def test_stable_ids_and_negative_mutations_are_checked(self) -> None:
        node_path = shutil.which("node")
        stable_results, stable_assertions = runner._run_stable_entity_cases(self.corpus, TEST_WORK_DIR, node_path)
        self.assertTrue(stable_assertions)
        self.assertTrue(all(item["status"] == "passed" for item in stable_assertions), stable_assertions)
        if node_path:
            self.assertTrue(all(item["status"] == "passed" for item in stable_results), stable_results)

        negative_results = runner._run_negative_mutations(self.corpus, TEST_WORK_DIR, node_path)
        self.assertEqual(len(negative_results), len(self.corpus["negativeCases"]))
        if node_path:
            self.assertTrue(all(item["status"] == "passed" for item in negative_results), negative_results)
        else:
            number_mutation = next(item for item in negative_results if item["tag"] == "float-negative-zero")
            self.assertEqual(number_mutation["status"], "failed")
            self.assertFalse(number_mutation["oracleMutationDetected"])

    def test_migration_matrix_emits_authored_loss_receipt(self) -> None:
        results = runner._run_migration_cases(self.corpus)
        self.assertEqual(len(results), len(self.corpus["migrationCases"]))
        by_id = {item["caseId"]: item for item in results}
        self.assertEqual(by_id["same-version-opaque-extension-preserved"]["status"], "passed")
        self.assertEqual(by_id["future-target-version-rejected"]["status"], "passed")
        legacy = by_id["legacy-version-requires-loss-receipt"]
        self.assertEqual(legacy["expected"]["outcome"], "migrated-with-loss-receipt")
        self.assertEqual(legacy["status"], "passed")
        self.assertEqual(legacy["actual"]["outcome"], "migrated-with-loss-receipt")
        self.assertEqual(legacy["actual"]["lossReceipt"], legacy["expected"]["lossReceipt"])

    def test_runner_leaves_all_workspace_reports_and_returns_success(self) -> None:
        result = runner.run_qualification(
            corpus_path=runner.DEFAULT_CORPUS_PATH,
            out_dir=TEST_REPORT_DIR,
        )
        self.assertEqual(result, 0)
        node_path = shutil.which("node")
        for name in runner.REPORT_NAMES.values():
            path = TEST_REPORT_DIR / name
            self.assertTrue(path.is_file(), path)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["issueNumber"], 98)
            self.assertRegex(report["sourceSha"], r"^[0-9a-f]{40}$")
            self.assertGreater(report["authoredVectorCount"], 0)
            self.assertGreater(report["authoredCaseCount"], 0)
            self.assertGreater(report["nonemptyAssertions"], 0)
            self.assertEqual(
                len(report["negativeMutationResults"]),
                len(self.corpus["negativeCases"]),
            )
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["completionStatus"], "incomplete-bounded-lane")
            self.assertEqual(report["qualificationGate"], "fail-closed")
            self.assertEqual(
                Path(report["fixtureWorkspace"]),
                TEST_REPORT_DIR / runner.FIXED_WORK_DIR_NAME,
            )
            if node_path:
                self.assertEqual(report["negativeMutationFailureCount"], 0)
            self.assertEqual(report["failureSummary"], [])
        producer_path = TEST_REPORT_DIR / "producer-report.json"
        self.assertTrue(producer_path.is_file())
        producer = json.loads(producer_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_producer_report_shape(producer), [])
        self.assertRegex(producer["sourceSha"], r"^[0-9a-f]{40}$")
        self.assertEqual(producer["uncoveredItems"], [])
        self.assertEqual(producer["unsupportedItems"], [])
        self.assertEqual(producer["waivedItems"], [])
        self.assertEqual(producer["status"], "passed")
        self.assertEqual(producer["failureCount"], 0)
        self.assertTrue({item["classification"] for item in producer["testCases"]} >= {"positive", "mutation"})
        self.assertEqual(
            {item["caseId"] for item in producer["testCases"]},
            {item["testCaseId"] for item in producer["assertions"]},
        )
        self.assertEqual(
            {item["assertionType"] for item in producer["assertions"]},
            {"canonical-identity", "mutation-killed"},
        )


if __name__ == "__main__":
    unittest.main()
