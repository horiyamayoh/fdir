"""Focused PDF source-fact regressions for issue #96."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

try:
    from convert_document import convert_path
    from ir_validation import validate_document
except ImportError:  # pragma: no cover - package-style test execution.
    from tools.convert_document import convert_path
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]

PDF_FIXTURES = {
    "pdf-rich-annotations": r"""%PDF-1.7
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> /XObject << /Im1 99 0 R >> >> /Contents 6 0 R /Annots [7 0 R 8 0 R 9 0 R 10 0 R 11 0 R 13 0 R] >>
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
6 0 obj
<< /Length 39 >>
stream
BT /F1 12 Tf 72 720 Td (PDF closure) Tj ET
endstream
endobj
7 0 obj
<< /Type /Annot /Subtype /Link /Rect [10 20 100 40] /A 12 0 R >>
endobj
8 0 obj
<< /Type /Annot /Subtype /Text /Rect [110 20 150 50] /Contents (A comment) >>
endobj
9 0 obj
<< /Type /Annot /Subtype /Widget /Rect [160 20 240 50] /T (CustomerName) /FT /Tx >>
endobj
10 0 obj
<< /Type /Annot /Subtype /FreeText /Rect [250 20 350 50] /Contents (Free text) >>
endobj
11 0 obj
<< /Type /Annot /Subtype /Highlight /Rect [20 60 120 80] /QuadPoints [20 80 120 80 20 60 120 60] >>
endobj
12 0 obj
<< /Type /Action /S /URI /URI (https://example.invalid/pdf) >>
endobj
13 0 obj
<< /Type /Annot /Subtype /Link /Rect [360 20 500 50] /A 14 0 R >>
endobj
14 0 obj
<< /Type /Action /S /JavaScript /JS (app.alert) >>
endobj
%%EOF
""",
    "pdf-marker-only": r"""%PDF-1.7
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Annots marker >>
endobj
%%EOF
""",
}


def _fixture(fixture_id: str) -> str:
    return PDF_FIXTURES[fixture_id]


class PDFIssue96Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fdir-pdf-annotations-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / f"{self._testMethodName}.pdf"

    def _convert(self, fixture_id: str) -> dict:
        self.path.write_bytes(_fixture(fixture_id).encode("latin-1"))
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
