from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import quality


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class QualityGateTests(unittest.TestCase):
    def repository_copy(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="fdir-quality-test-")
        destination = Path(temporary.name) / "repo"
        quality.copy_repository(REPOSITORY_ROOT, destination)
        return temporary, destination

    def test_source_digest_ignores_receipts_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            before = quality.source_digest(root)
            (root / "reports/quality").mkdir(parents=True)
            (root / "reports/quality/full.json").write_text("{}\n", encoding="utf-8")
            (root / ".validation").mkdir()
            (root / ".validation/quality-cache.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(before, quality.source_digest(root))

    def test_text_format_gate_detects_trailing_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.md").write_text("bad  \n", encoding="utf-8")
            result = quality.gate_text_format(root)
            self.assertEqual("failed", result.status)
            self.assertIn("trailing whitespace: bad.md:1", result.diagnostics)

    def test_documentation_gate_detects_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("[missing](missing.md)\n", encoding="utf-8")
            result = quality.gate_docs_links(root)
            self.assertEqual("failed", result.status)
            self.assertIn("broken documentation link: README.md -> missing.md", result.diagnostics)

    def test_traceability_gate_detects_orphan_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "machine").mkdir()
            (root / "machine/requirements.yaml").write_text(
                json.dumps(
                    {
                        "requirements": [
                            {"id": "REQ-1", "level": "must", "text": "covered"},
                            {"id": "REQ-2", "level": "must", "text": "orphan"},
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "machine/acceptance-tests.yaml").write_text(
                json.dumps(
                    {
                        "tests": [
                            {
                                "id": "AT-1",
                                "command": "python3 existing.py",
                                "requirements": ["REQ-1"],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "existing.py").write_text("pass\n", encoding="utf-8")
            result = quality.gate_requirement_traceability(root)
            self.assertEqual("failed", result.status)
            self.assertIn("orphan normative requirement: REQ-2", result.diagnostics)

    def test_fixture_gate_detects_missing_negative_registration(self) -> None:
        temporary, root = self.repository_copy()
        with temporary:
            path = root / "fixtures/negative/manifest.json"
            value = quality.load_json(path)
            removed = value["fixtures"].pop()
            quality.write_json(path, value)
            result = quality.gate_fixture_registry(root)
            self.assertEqual("failed", result.status)
            self.assertIn(
                f"unregistered negative fixture: {removed['path']}",
                result.diagnostics,
            )

    def test_release_gate_fails_closed_for_unqualified_repository(self) -> None:
        result = quality.gate_release_qualification(REPOSITORY_ROOT)
        self.assertEqual("failed", result.status)
        self.assertIn("release is not qualified and production-ready", result.diagnostics)

    def test_read_only_cache_rejects_stale_source_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            quality.write_json(
                quality.cache_path(root),
                {
                    "schema": quality.CACHE_SCHEMA,
                    "qualityVersion": quality.QUALITY_VERSION,
                    "mode": "fast",
                    "sourceDigest": "0" * 64,
                    "gatePlanSha256": quality.gate_plan_digest("fast"),
                    "gateResultsSha256": "0" * 64,
                    "authoritativeGatesSkipped": False,
                },
            )
            digest, _ = quality.source_digest(root)
            result, _ = quality.cache_precheck(root, "fast", "read-only", digest)
            self.assertEqual("failed", result.status)
            self.assertIn("read-only cache source digest mismatch", result.diagnostics)

    def test_output_normalization_removes_unittest_duration(self) -> None:
        normalized = quality.normalize_output("Ran 8 tests in 0.123s\n", REPOSITORY_ROOT)
        self.assertEqual("Ran 8 tests in <duration>s", normalized)


if __name__ == "__main__":
    unittest.main()
