"""Regression checks for the committed LynxGate input inventory."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from tools.convert_document import convert_path
from tools.ir_validation import validate_document
from tools.real_input_corpus import expected_limit_match, load_manifest


class LynxGateCorpusTests(unittest.TestCase):
    def test_manifest_and_archives_are_usable_regression_inputs(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "e2e" / "corpus" / "real-world" / "lynxgate" / "manifest.json"
        manifest, documents = load_manifest(manifest_path)
        self.assertEqual(manifest["expected"]["documents"], 161)
        self.assertEqual(len(documents), 161)
        self.assertEqual(Counter(item["format"] for item in documents), Counter({"xlsx": 101, "docx": 60}))
        self.assertTrue(all(Path(item["archivePath"]).is_file() for item in documents))
        self.assertTrue(all(item["sha256"] == item["extractedSha256"] for item in documents))

    def test_every_manifest_document_converts_in_a_temporary_workspace(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "e2e" / "corpus" / "real-world" / "lynxgate" / "manifest.json"
        manifest, documents = load_manifest(manifest_path)
        with tempfile.TemporaryDirectory(prefix="fdir-lynxgate-") as directory:
            workspace = Path(directory)
            for item in documents:
                input_path = workspace / f"{item['id']}.{item['format']}"
                with zipfile.ZipFile(item["archivePath"], "r") as archive, archive.open(item["memberName"], "r") as source, input_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                document, metadata = convert_path(input_path, str(item["format"]))
                validate_document(document)
                self.assertTrue(metadata["input"]["consumed"], item["id"])
                self.assertEqual(metadata["input"]["bytes"], item["bytes"], item["id"])
                if metadata["input"].get("sha256") is not None:
                    self.assertEqual(metadata["input"]["sha256"], item["sha256"], item["id"])
                status = document.get("conversion", {}).get("status")
                if status == "failed":
                    diagnostics = document.get("diagnostics", [])
                    self.assertTrue(
                        expected_limit_match(manifest, str(item["format"]), diagnostics),
                        item["id"],
                    )
                else:
                    self.assertIn(status, {"complete", "complete-with-warnings", "partial"}, item["id"])


if __name__ == "__main__":
    unittest.main()
