from __future__ import annotations

import importlib.util
import json
import math
import unittest
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def import_canonical_tool() -> Any:
    path = REPOSITORY_ROOT / "tools/canonical_json.py"
    spec = importlib.util.spec_from_file_location("fdir_canonical_json_tests", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canonical = import_canonical_tool()


class CanonicalIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors = json.loads(
            (REPOSITORY_ROOT / "fixtures/canonical/vector.json").read_text(encoding="utf-8")
        )

    def test_legacy_and_expanded_positive_vectors(self) -> None:
        self.assertEqual(
            canonical.canonical_text(self.vectors["value"]),
            self.vectors["canonical"],
        )
        self.assertEqual(
            canonical.sha256_digest(self.vectors["value"]),
            self.vectors["digest"],
        )
        for vector in self.vectors["positive"]:
            with self.subTest(vector=vector["id"]):
                actual = canonical.canonicalize_json(vector["input"]).decode("utf-8")
                self.assertEqual(actual, vector["canonical"])
                self.assertEqual(
                    canonical.sha256_digest_json(vector["input"]),
                    vector["digest"],
                )
                self.assertTrue(canonical.is_canonical_json(vector["canonical"]))

    def test_negative_vectors_preserve_diagnostic_codes(self) -> None:
        for vector in self.vectors["negative"]:
            with self.subTest(vector=vector["id"]):
                with self.assertRaises(canonical.CanonicalError) as context:
                    canonical.canonicalize_json(vector["input"])
                self.assertEqual(context.exception.code, vector["expectedCode"])

    def test_api_rejects_non_string_keys_and_non_finite_numbers(self) -> None:
        cases = [
            ({1: "value"}, "FDIR-CANONICAL-OBJECT-KEY"),
            ({"value": math.nan}, "FDIR-CANONICAL-NUMBER-NON-FINITE"),
            ({"value": math.inf}, "FDIR-CANONICAL-NUMBER-NON-FINITE"),
            ({"value": -(2**63) - 1}, "FDIR-CANONICAL-INTEGER-RANGE"),
            ({"value": 2**64}, "FDIR-CANONICAL-INTEGER-RANGE"),
            ({"value": ("unsupported",)}, "FDIR-CANONICAL-TYPE"),
        ]
        for value, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(canonical.CanonicalError) as context:
                    canonical.canonical_bytes(value)
                self.assertEqual(context.exception.code, expected_code)

    def test_determinism_does_not_depend_on_insertion_order(self) -> None:
        first = {"z": 1, "a": 2, "middle": {"b": True, "a": False}}
        second = {"middle": {"a": False, "b": True}, "a": 2, "z": 1}
        self.assertEqual(canonical.canonical_bytes(first), canonical.canonical_bytes(second))
        self.assertEqual(canonical.sha256_digest(first), canonical.sha256_digest(second))

    def test_unicode_is_not_normalized(self) -> None:
        precomposed = canonical.canonical_bytes({"value": "é"})
        decomposed = canonical.canonical_bytes({"value": "é"})
        self.assertNotEqual(precomposed, decomposed)
        self.assertNotEqual(
            canonical.sha256_digest({"value": "é"}),
            canonical.sha256_digest({"value": "é"}),
        )

    def test_identity_vectors_are_domain_separated(self) -> None:
        digests: set[str] = set()
        for vector in self.vectors["identity"]:
            with self.subTest(vector=vector["id"]):
                self.assertEqual(
                    canonical.canonical_text(vector["envelope"]),
                    vector["canonical"],
                )
                actual = canonical.domain_separated_digest(
                    vector["kind"], vector["envelope"]
                )
                self.assertEqual(actual, vector["digest"])
                digests.add(actual)
        self.assertEqual(len(digests), len(self.vectors["identity"]))


if __name__ == "__main__":
    unittest.main()
