"""Unit tests for real-input regression manifest validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

try:
    from real_input_corpus import CorpusError, expected_limit_match, load_manifest, validate_zip_member
except ImportError:  # pragma: no cover
    from tools.real_input_corpus import CorpusError, expected_limit_match, load_manifest, validate_zip_member


class RealInputCorpusTests(unittest.TestCase):
    def _write_manifest(self, root: Path, *, archive_bytes: bytes = b"not-a-real-docx") -> Path:
        corpus = root / "corpus"
        archives = corpus / "archives"
        archives.mkdir(parents=True)
        archive_path = archives / "input.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Project/word/document.docx", archive_bytes)
        archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        document_sha = hashlib.sha256(archive_bytes).hexdigest()
        manifest = {
            "schema": "fdir/real-input-corpus-manifest",
            "version": "1.0.0",
            "id": "test",
            "archives": [{"id": "input", "path": "archives/input.zip", "bytes": archive_path.stat().st_size, "sha256": archive_sha, "documentCount": 1, "formats": {"docx": 1, "xlsx": 0}}],
            "documents": [{"id": "test-001", "archiveId": "input", "path": "Project/word/document.docx", "format": "docx", "bytes": len(archive_bytes), "sha256": document_sha}],
            "expected": {"documents": 1, "formats": {"docx": 1, "xlsx": 0}, "expectedLimitDiagnostics": [{"format": "docx", "code": "LIMIT", "messageContains": "bounded"}]},
        }
        manifest_path = corpus / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest_path

    def test_manifest_pins_archive_and_document_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, documents = load_manifest(self._write_manifest(Path(directory)))
            self.assertEqual(manifest["id"], "test")
            self.assertEqual([item["id"] for item in documents], ["test-001"])

    def test_archive_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = self._write_manifest(Path(directory))
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            value["archives"][0]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(CorpusError, "archive digest mismatch"):
                load_manifest(manifest_path)

    def test_expected_limit_policy_matches_declared_diagnostic(self) -> None:
        manifest = {"expected": {"expectedLimitDiagnostics": [{"format": "docx", "code": "LIMIT", "messageContains": "bounded"}]}}
        self.assertTrue(expected_limit_match(manifest, "docx", [{"code": "LIMIT", "message": "bounded input limit"}]))
        self.assertFalse(expected_limit_match(manifest, "docx", [{"code": "OTHER", "message": "bounded input limit"}]))
        self.assertFalse(expected_limit_match(manifest, "xlsx", [{"code": "LIMIT", "message": "bounded input limit"}]))

    def test_zip_member_paths_are_canonical_and_confined(self) -> None:
        self.assertEqual(validate_zip_member("folder/input.docx"), "folder/input.docx")
        for invalid in ("../input.docx", "/input.docx", "C:/input.docx", "folder\\input.docx", "folder//input.docx"):
            with self.assertRaises(CorpusError):
                validate_zip_member(invalid)


if __name__ == "__main__":
    unittest.main()
