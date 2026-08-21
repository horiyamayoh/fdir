from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "design.yml"


class DesignWorkflowEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_required_platform_matrix_is_real_and_explicit(self) -> None:
        for profile in ("ubuntu-py3.12", "windows-py3.12", "macos-py3.12"):
            self.assertIn(f"profile: {profile}", self.text)
        self.assertIn("fail-fast: false", self.text)
        self.assertIn("runs-on: ${{ matrix.runner }}", self.text)

    def test_required_uploads_are_success_gated(self) -> None:
        self.assertIn("- name: Upload qualification bundle\n        if: ${{ success() }}", self.text)
        self.assertIn("- name: Upload platform candidate evidence\n        if: ${{ success() }}", self.text)
        self.assertIn("- name: Upload release attestation\n        if: ${{ success()", self.text)
        self.assertNotIn("continue-on-error:", self.text)

    def test_evidence_collects_immutable_actions_fields(self) -> None:
        for field in ("run_attempt", "head_sha", "conclusion", "artifact.digest", "artifact.workflow_run", "sourceSha"):
            self.assertIn(field, self.text)
        self.assertIn("listJobsForWorkflowRunAttempt", self.text)
        self.assertIn("listWorkflowRunArtifacts", self.text)
        self.assertIn("RUN_NOT_SUCCESS", self.text)
        self.assertIn("REQUIRED_STEP_NOT_SUCCESS", self.text)
        self.assertIn("ARTIFACT_RUN_BINDING", self.text)
        self.assertIn("name: Validate qualification contract", self.text)
        self.assertIn("run: python tools/validate_qualification_contract.py", self.text)
        self.assertIn('"Validate qualification contract"', self.text)

    def test_final_attestation_requires_a_completed_target_run(self) -> None:
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("Require completed successful target run for final attestation", self.text)
        self.assertIn("Run candidate qualification gate", self.text)
        self.assertIn("python tools/strict_completion_gate.py --bundle", self.text)
        self.assertIn("Build final release attestation", self.text)
        self.assertIn("python tools/release_gate.py --bundle \"$BUNDLE_MANIFEST\" --attestation \"$ATTESTATION\"", self.text)
        self.assertIn("from tools import release_attestation", self.text)
        self.assertIn("Upload release attestation publication receipt", self.text)


if __name__ == "__main__":
    unittest.main()
