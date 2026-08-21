"""Focused PDF source-fact regressions for issue #96."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

try:
    from convert_document import convert_path
    from ir_validation import validate_document
except ImportError:  # pragma: no cover - package-style test execution.
    from tools.convert_document import convert_path
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]


def _fixture(fixture_id: str) -> dict:
    corpus = json.loads((ROOT / "machine" / "qualification-issue-96-corpus.json").read_text(encoding="utf-8"))
    return next(item for item in corpus["fixtures"] if item["fixtureId"] == fixture_id)


class PDFIssue96Tests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = ROOT / "e2e" / ".run" / "qualification-issue-96-pdf-fixtures"
        scratch.mkdir(parents=True, exist_ok=True)
        self.path = scratch / f"{self._testMethodName}.pdf"

    def _convert(self, fixture_id: str) -> dict:
        self.path.write_bytes(_fixture(fixture_id)["value"].encode("latin-1"))
        document, evidence = convert_path(self.path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)
        return document

    def test_rich_annotations_retain_authored_relationship_and_annotation_facts(self) -> None:
        document = self._convert("pdf-rich-annotations")

        relations = {
            item["relationId"]: item
            for item in document["relations"]
            if item.get("kind") == "references"
        }
        self.assertEqual(relations["relation-pdf-object-3-0-11-0"]["sourceOccurrenceId"], "pdf-page-highlight")
        self.assertEqual(relations["relation-pdf-object-3-0-11-0"]["type"], "indirect-reference")
        self.assertEqual(relations["relation-pdf-object-3-0-11-0"]["targetMode"], "internal")

        font = next(item for item in document["resources"] if item.get("derivedHandle") == "object:5 0")
        self.assertFalse(font["rawPayloadAvailable"])
        self.assertEqual(font["availability"], "unavailable")
        self.assertEqual(font["packagePresence"], True)
        self.assertEqual(font["embeddedOrExternal"], "embedded")
        self.assertEqual(font["decodability"], "not-decodable")
        self.assertEqual(font["networkAvailability"], "not-applicable")
        self.assertEqual(
            sum(1 for relation in document["relations"] if relation.get("toId") == font["resourceId"]),
            1,
        )

        annotations = {item["referenceId"]: item for item in document["annotations"]}
        self.assertEqual(len(annotations), 6)
        self.assertEqual(annotations["7 0 R"]["sourceSubtype"], "Link")
        self.assertEqual(annotations["7 0 R"]["action"], {"kind": "URI", "target": "https://example.invalid/pdf"})
        self.assertEqual(annotations["13 0 R"]["action"], {"kind": "JavaScript", "target": "app.alert"})
        self.assertEqual(annotations["13 0 R"]["destination"], "app.alert")
        self.assertEqual(annotations["11 0 R"]["sourceSubtype"], "Highlight")
        self.assertEqual(annotations["11 0 R"]["status"], "unsupported")
        self.assertEqual(
            annotations["11 0 R"]["geometry"],
            {
                "rect": ["20", "60", "120", "80"],
                "quadPoints": ["20", "80", "120", "80", "20", "60", "120", "60"],
            },
        )
        self.assertEqual(annotations["9 0 R"]["action"], {"kind": "field", "name": "CustomerName"})
        self.assertEqual(
            annotations["9 0 R"]["range"],
            {"start": "9 0 R", "end": "9 0 R", "balanced": True},
        )

    def test_marker_only_annots_value_does_not_fabricate_annotations(self) -> None:
        document = self._convert("pdf-marker-only")
        self.assertEqual(document["annotations"], [])
        self.assertEqual(
            [item["sourceOccurrenceId"] for item in document["relations"] if item.get("kind") == "references"],
            ["pdf-marker-catalog-pages", "pdf-marker-pages-kids", "pdf-marker-page-parent"],
        )


if __name__ == "__main__":
    unittest.main()
