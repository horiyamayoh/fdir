from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools import generate_rust_contract, validate_baseline

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RustModelContractTests(unittest.TestCase):
    def test_generated_typed_projection_is_deterministic(self) -> None:
        outputs = generate_rust_contract.outputs(REPOSITORY_ROOT)
        for relative, expected in outputs.items():
            self.assertEqual(
                expected,
                (REPOSITORY_ROOT / relative).read_text(encoding="utf-8"),
                relative.as_posix(),
            )
        manifest = json.loads(outputs[generate_rust_contract.MANIFEST_PATH])
        self.assertEqual("fdir/rust-generated-contract/2", manifest["schema"])
        self.assertTrue(manifest["typedProjection"])
        self.assertEqual(20, manifest["entityCount"])
        self.assertEqual(8, manifest["enumCount"])
        self.assertGreaterEqual(manifest["strongIdCount"], manifest["entityCount"])

    def test_every_machine_entity_and_identity_has_a_generated_type(self) -> None:
        model = json.loads(
            (REPOSITORY_ROOT / "machine/logical-model.yaml").read_text(encoding="utf-8")
        )
        generated = (
            REPOSITORY_ROOT / "crates/fdir-contract/src/generated.rs"
        ).read_text(encoding="utf-8")
        for entity in model["entities"]:
            self.assertIn(f"pub struct {entity['name']} {{", generated)
            identity_type = generate_rust_contract.pascal_case(entity["identity"])
            self.assertIn(f"define_strong_id!({identity_type});", generated)
        self.assertIn("define_strong_id!(OpaqueId);", generated)

    def test_hand_edit_is_rejected_by_generator_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fdir-rust-contract-") as directory:
            root = Path(directory)
            for relative in (
                Path("machine/logical-model.yaml"),
                Path("fixtures/canonical/vector.json"),
                generate_rust_contract.GENERATED_PATH,
                generate_rust_contract.MANIFEST_PATH,
            ):
                source = REPOSITORY_ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            generated = root / generate_rust_contract.GENERATED_PATH
            generated.write_text(
                generated.read_text(encoding="utf-8") + "// hand edit\n",
                encoding="utf-8",
            )
            self.assertEqual(1, generate_rust_contract.run(root, True))

    def test_shared_python_fixture_oracle_accepts_extensions_and_statuses(self) -> None:
        fixture = json.loads(
            (
                REPOSITORY_ROOT / "fixtures/positive/extensions-and-statuses.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual([], validate_baseline.validate_document(fixture))

    def test_rust_parity_suite_tracks_every_negative_fixture_code(self) -> None:
        manifest = json.loads(
            (REPOSITORY_ROOT / "fixtures/negative/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        rust_test = (
            REPOSITORY_ROOT
            / "crates/fdir-contract/tests/model_fixture_parity.rs"
        ).read_text(encoding="utf-8")
        for fixture in manifest["fixtures"]:
            self.assertIn(fixture["path"], rust_test)
            self.assertIn(fixture["expectedCode"], rust_test)

    def test_neutral_core_preserves_all_explicit_states_and_evidence_lanes(self) -> None:
        foundation = (
            REPOSITORY_ROOT / "crates/fdir-core/src/foundation.rs"
        ).read_text(encoding="utf-8")
        for state in (
            "incomplete",
            "partial",
            "unsupported",
            "unresolved",
            "cancelled",
            "failed",
            "unreadable",
            "resource-limited",
            "policy-excluded",
        ):
            self.assertIn(f'"{state}"', foundation)
        for lane in (
            "native-substrate",
            "semantic-candidate",
            "renderer",
            "ocr-inference",
            "storage-codec",
        ):
            self.assertIn(f'"{lane}"', foundation)


if __name__ == "__main__":
    unittest.main()
