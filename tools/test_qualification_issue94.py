"""Focused tests for the fail-closed issue #94 qualification slice."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import unittest
import uuid

try:
    import qualification_issue94 as qualification
except ImportError:  # pragma: no cover - package-style test execution.
    from tools import qualification_issue94 as qualification


class QualificationIssue94Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = qualification._read_json(qualification.DEFAULT_CORPUS_PATH)

    @staticmethod
    def _output_dir() -> Path:
        path = qualification.ROOT / "e2e" / ".run" / f"qualification-issue-94-test-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_corpus_declares_five_reports_and_unmet_coverage(self) -> None:
        self.assertEqual(
            set(qualification.REPORT_NAMES.values()),
            {
                "coordinate-transform-vectors.json",
                "geometry-lane-report.json",
                "anchor-resolution-report.json",
                "clip-and-paint-order-report.json",
                "reading-order-ambiguity-report.json",
            },
        )
        for key in (
            "coordinateTransforms",
            "geometryLanes",
            "anchorResolution",
            "clipAndPaintOrder",
            "readingOrderAmbiguity",
        ):
            self.assertTrue(self.corpus[key]["requiredLanes"])
            self.assertTrue(self.corpus[key]["unmetCoverage"])
        integration = self.corpus["adapterIntegration"]
        self.assertTrue(integration["requiredLanes"])
        self.assertEqual(integration["requiredLanes"], integration["coveredLanes"])
        self.assertTrue(integration["fixtures"])
        self.assertTrue(integration["mutations"])
        self.assertTrue(integration["unmetCoverage"])
        self.assertTrue(integration["oracle"]["expectedValuesAreRuntimeIndependent"])
        self.assertFalse(integration["oracle"]["adapterHelpersUsedForExpected"])

    def test_bounded_vectors_pass_but_report_cannot_claim_completion(self) -> None:
        output = self._output_dir()
        try:
            exit_code = qualification.run_qualification(out_dir=output)
            self.assertEqual(exit_code, 1)
            expected_adapter_cases = {
                "coordinate-transform-vectors.json": (0, 0, 3),
                "geometry-lane-report.json": (1, 2, 3),
                "anchor-resolution-report.json": (1, 2, 4),
                "clip-and-paint-order-report.json": (1, 2, 4),
                "reading-order-ambiguity-report.json": (3, 4, 4),
            }
            for name in qualification.REPORT_NAMES.values():
                report = json.loads((output / name).read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["vectorStatus"], "passed")
                self.assertEqual(report["mutationStatus"], "passed")
                self.assertIn(report["adapterStatus"], {"passed", "failed", "not-applicable"})
                self.assertIn(report["adapterMutationStatus"], {"passed", "not-applicable"})
                self.assertEqual(
                    report["counts"]["sourceFactMismatchCount"],
                    sum(len(case["sourceMismatches"]) for case in report["adapterCases"]),
                )
                self.assertEqual(
                    report["counts"]["adapterMismatchCount"],
                    sum(len(case["adapterMismatches"]) for case in report["adapterCases"]),
                )
                self.assertEqual(
                    report["counts"]["publicBoundaryFailureCount"],
                    sum(not case["conversionOk"] for case in report["adapterCases"]),
                )
                self.assertEqual(
                    (report["counts"]["adapterCases"], report["counts"]["adapterMutations"], report["counts"]["unmetCoverageCount"]),
                    expected_adapter_cases[name],
                )
                self.assertEqual(report["counts"]["adapterUnmetCoverageCount"], 8)
                self.assertEqual(
                    report["failure"]["unmetCoverageCount"],
                    report["counts"]["unmetCoverageCount"] + report["counts"]["adapterUnmetCoverageCount"],
                )
                self.assertFalse(report["coverage"]["complete"])
                self.assertTrue(report["coverage"]["unmet"])
                self.assertRegex(report["sourceSha"], re.compile(r"^[0-9a-f]{40}$"))
                self.assertEqual(report["sourceSha"], qualification._source_sha())
                self.assertEqual(report["counts"]["independentOracleMismatchCount"], 0)
                self.assertTrue(report["assertions"])
                self.assertEqual(report["failure"]["code"], "QUALIFICATION-COVERAGE-INCOMPLETE")
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_coordinate_operation_order_mutation_is_detected(self) -> None:
        vector = deepcopy(next(item for item in self.corpus["coordinateTransforms"]["vectors"] if item["id"] == "translate-then-scale"))
        qualification._mutate_coordinate(vector, "swap-operations")
        result = qualification._coordinate_result(vector)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(item["status"] == "failed" for item in result["assertions"]))

    def test_geometry_approximation_cannot_be_preserved(self) -> None:
        vector = deepcopy(next(item for item in self.corpus["geometryLanes"]["vectors"] if item["id"] == "bezier-control-hull"))
        qualification._mutate_geometry(vector, "mark-approximate")
        result = qualification._geometry_result(vector)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("no-false-preservation" in item["id"] for item in result["assertions"]))

    def test_projective_cubic_endpoint_and_interleaved_lanes_are_exact(self) -> None:
        projective = next(
            item
            for item in self.corpus["coordinateTransforms"]["vectors"]
            if item["id"] == "projective-perspective-exact"
        )
        projective_result = qualification._coordinate_result(deepcopy(projective))
        self.assertEqual(projective_result["status"], "passed")
        self.assertEqual(projective_result["actual"]["matrixModel"], "projective")
        self.assertEqual(projective_result["actual"]["point"], ["50/17", "50/17"])

        cubic = next(
            item
            for item in self.corpus["geometryLanes"]["vectors"]
            if item["id"] == "bezier-rational-extrema"
        )
        cubic_result = qualification._geometry_result(deepcopy(cubic))
        self.assertEqual(cubic_result["status"], "passed")
        self.assertEqual(cubic_result["actual"]["bounds"], [0, 0, "9/4", 3])

        endpoint = next(
            item
            for item in self.corpus["anchorResolution"]["vectors"]
            if item["id"] == "endpoint-pair-anchor"
        )
        endpoint_result = qualification._anchor_result(deepcopy(endpoint))
        self.assertEqual(endpoint_result["status"], "passed")
        self.assertEqual(endpoint_result["actual"]["globalStart"], [13, 21])
        self.assertEqual(endpoint_result["actual"]["globalEnd"], [16, 25])

        scene = next(
            item
            for item in self.corpus["clipAndPaintOrder"]["scenes"]
            if item["id"] == "nested-clip-paint-scene"
        )
        clip_result = qualification._clip_result(deepcopy(scene))
        self.assertEqual(clip_result["status"], "passed")
        self.assertEqual(
            clip_result["actual"]["paintEventOrder"],
            ["paint-background", "paint-shape", "paint-label"],
        )

    def test_anchor_clip_and_reading_mutations_are_all_detected(self) -> None:
        suites = (
            ("anchorResolution", "vectors", qualification._anchor_result, qualification._mutate_anchor),
            ("clipAndPaintOrder", "scenes", qualification._clip_result, qualification._mutate_clip),
            ("readingOrderAmbiguity", "scenes", qualification._reading_result, qualification._mutate_reading),
        )
        for section_name, item_key, evaluator, mutator in suites:
            section = self.corpus[section_name]
            mutation_by_id = {str(item["id"]): item for item in section[item_key]}
            with self.subTest(section=section_name):
                for mutation in section["mutations"]:
                    target_key = mutation.get("vectorId", mutation.get("sceneId"))
                    item = deepcopy(mutation_by_id[str(target_key)])
                    mutator(item, str(mutation["kind"]))
                    result = evaluator(item)
                    self.assertEqual(result["status"], "failed", mutation["id"])

    def test_every_report_has_nonempty_mutation_evidence(self) -> None:
        output = self._output_dir()
        try:
            qualification.run_qualification(out_dir=output)
            for name in qualification.REPORT_NAMES.values():
                report = json.loads((output / name).read_text(encoding="utf-8"))
                self.assertGreater(report["counts"]["mutations"], 0)
                self.assertEqual(report["counts"]["mutations"], report["counts"]["mutationsDetected"])
                if report["counts"]["adapterMutations"]:
                    self.assertEqual(report["counts"]["adapterMutations"], report["counts"]["adapterMutationsDetected"])
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_public_converter_integration_reports_real_boundary_failures(self) -> None:
        output = self._output_dir()
        try:
            self.assertEqual(qualification.run_qualification(out_dir=output), 1)
            seen = {}
            for name in qualification.REPORT_NAMES.values():
                report = json.loads((output / name).read_text(encoding="utf-8"))
                for case in report["adapterCases"]:
                    seen[case["fixtureId"]] = case
            self.assertEqual(
                set(seen),
                {item["fixtureId"] for item in self.corpus["adapterIntegration"]["fixtures"]},
            )
            self.assertTrue(all(not case["sourceMismatches"] for case in seen.values()))
            self.assertTrue(all(not case["independentOracleMismatches"] for case in seen.values()))
            self.assertTrue(
                all(
                    case["status"] == "passed"
                    or not case["conversionOk"]
                    or case["adapterMismatches"]
                    for case in seen.values()
                )
            )
        finally:
            shutil.rmtree(output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
