from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools import validate_implementation_policy as policy


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ImplementationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy_model, cls.schema, cls.catalog = policy.load_models(REPOSITORY_ROOT)

    def manifest(self) -> dict[str, object]:
        return policy.valid_test_manifest()

    def diagnostics(self, manifest: dict[str, object]) -> list[str]:
        return policy.validate_manifest(
            manifest,
            self.schema,
            self.policy_model,
            "testDependency",
        )

    def test_repository_policy_passes(self) -> None:
        self.assertEqual([], policy.validate_repository(REPOSITORY_ROOT))

    def test_valid_admitted_manifest_passes(self) -> None:
        self.assertEqual([], self.diagnostics(self.manifest()))

    def test_unknown_lane_is_rejected(self) -> None:
        manifest = self.manifest()
        manifest["evidenceLanes"] = ["invented-lane"]
        result = self.diagnostics(manifest)
        self.assertTrue(any("unknown lane" in item for item in result), result)

    def test_floating_version_is_rejected(self) -> None:
        manifest = self.manifest()
        manifest["version"] = "latest"
        result = self.diagnostics(manifest)
        self.assertTrue(any("exact immutable version" in item for item in result), result)

    def test_semantic_helper_cannot_claim_native_authority(self) -> None:
        manifest = self.manifest()
        manifest["evidenceLanes"] = ["semantic-helper"]
        manifest["nativeAuthority"] = True
        manifest["independentCensus"] = True
        result = self.diagnostics(manifest)
        self.assertTrue(
            any("semantic-helper output cannot claim native authority" in item for item in result),
            result,
        )
        self.assertTrue(
            any("semantic-helper output cannot claim independent census" in item for item in result),
            result,
        )

    def test_unsafe_untrusted_in_process_dependency_is_rejected(self) -> None:
        manifest = self.manifest()
        manifest["unsafeCode"] = True
        manifest["processBoundary"] = "in-process"
        result = self.diagnostics(manifest)
        self.assertTrue(any("must be isolated" in item for item in result), result)

    def test_non_rust_untrusted_in_process_worker_is_rejected(self) -> None:
        manifest = self.manifest()
        manifest["implementationLanguage"] = "Python"
        manifest["processBoundary"] = "in-process"
        result = self.diagnostics(manifest)
        self.assertTrue(any("non-Rust document worker" in item for item in result), result)

    def test_complete_in_process_exception_is_accepted(self) -> None:
        manifest = self.manifest()
        manifest["unsafeCode"] = True
        manifest["processBoundary"] = "in-process"
        manifest["inProcessException"] = {
            "adr": "machine/adrs/0099-self-test-exception.md",
            "threatAnalysis": "security/self-test-threat-analysis.md",
            "boundedInputContract": "machine/self-test-input-contract.json",
            "qualificationEvidence": ["reports/self-test-qualification.json"],
        }
        result = self.diagnostics(manifest)
        self.assertFalse(any("must be isolated" in item for item in result), result)

    def test_non_deny_network_requires_isolated_worker(self) -> None:
        manifest = self.manifest()
        manifest["receivesUntrustedDocumentBytes"] = False
        manifest["processBoundary"] = "in-process"
        manifest["networkPolicy"] = "allowlisted"
        result = self.diagnostics(manifest)
        self.assertTrue(any("network policy requires" in item for item in result), result)

    def test_catalog_rejects_missing_in_process_exception_evidence(self) -> None:
        manifest = self.manifest()
        manifest["unsafeCode"] = True
        manifest["processBoundary"] = "in-process"
        manifest["inProcessException"] = {
            "adr": "missing/exception-adr.md",
            "threatAnalysis": "missing/threat-analysis.md",
            "boundedInputContract": "missing/input-contract.json",
            "qualificationEvidence": ["missing/qualification.json"],
        }
        catalog = copy.deepcopy(self.catalog)
        catalog["state"] = "admitted-dependencies"
        catalog["dependencies"] = [manifest]
        result = policy.validate_catalog(
            catalog,
            self.schema,
            self.policy_model,
            root=REPOSITORY_ROOT,
        )
        self.assertTrue(any("references missing evidence" in item for item in result), result)

    def test_self_tests_detect_all_mutations(self) -> None:
        failures, cases = policy.self_tests(REPOSITORY_ROOT)
        self.assertEqual([], failures)
        self.assertEqual(9, len(cases))
        self.assertTrue(all(item["status"] == "detected" for item in cases))

    def test_policy_model_rejects_canonical_authority_drift(self) -> None:
        model = copy.deepcopy(self.policy_model)
        model["canonicalIdentity"]["encoding"] = "canonical-cbor"
        result = policy.validate_policy_model(model, REPOSITORY_ROOT, paths=False)
        self.assertIn("canonical identity authority must be canonical-json", result)


if __name__ == "__main__":
    unittest.main()
