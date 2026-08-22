"""Independent bounded tests for PDF glyph provenance."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

try:
    from convert_document import convert_path
    from ir_validation import validate_document
except ImportError:  # pragma: no cover - package-style test execution.
    from tools.convert_document import convert_path
    from tools.ir_validation import validate_document


def _cmap_target(value: str) -> str:
    return value.encode("utf-16-be").hex().upper()


def _write_pdf(path: Path, text_hex: str, mappings: list[tuple[str, str]] | None = None) -> None:
    """Write a tiny authored PDF without importing production adapters."""

    stream = f"BT /F1 12 Tf 72 720 Td <{text_hex}> Tj ET".encode("ascii")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica",
        f"<< /Length {len(stream)} >>\nstream\n{stream.decode('ascii')}\nendstream",
    ]
    if mappings is not None:
        objects[3] += " /ToUnicode 6 0 R >>"
        pairs = "\n".join(f"<{source.upper()}> <{_cmap_target(unicode)}>" for source, unicode in mappings)
        cmap = (
            "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            f"{len(mappings)} beginbfchar\n{pairs}\nendbfchar\n"
            "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
        ).encode("ascii")
        objects.append(f"<< /Length {len(cmap)} >>\nstream\n{cmap.decode('ascii')}\nendstream")
    chunks = [b"%PDF-1.7\n"]
    for number, body in enumerate(objects, start=1):
        chunks.extend([f"{number} 0 obj\n".encode("ascii"), body.encode("latin-1"), b"\nendobj\n"])
    chunks.append(b"%%EOF\n")
    path.write_bytes(b"".join(chunks))


def _write_identity_h_pdf(path: Path) -> None:
    stream = b"BT /F1 12 Tf 72 720 Td <00410042> Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type0 /BaseFont /Identity-H /Encoding /Identity-H /DescendantFonts [7 0 R] >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream.decode('ascii')}\nendstream",
        "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Identity-H /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>",
    ]
    chunks = [b"%PDF-1.7\n"]
    for number, body in ((1, objects[0]), (2, objects[1]), (3, objects[2]), (4, objects[3]), (5, objects[4]), (7, objects[5])):
        chunks.extend([f"{number} 0 obj\n".encode("ascii"), body.encode("latin-1"), b"\nendobj\n"])
    chunks.append(b"%%EOF\n")
    path.write_bytes(b"".join(chunks))


class PDFGlyphProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fdir-pdf-glyph-")
        self.addCleanup(self._tmp.cleanup)

    def _convert(self, text_hex: str, mappings: list[tuple[str, str]] | None) -> dict:
        path = Path(self._tmp.name) / f"{self._testMethodName}.pdf"
        _write_pdf(path, text_hex, mappings)
        document, evidence = convert_path(path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)
        return document

    @staticmethod
    def _glyph(document: dict) -> tuple[dict, dict, dict]:
        extension = next(item for item in document["extensions"] if item.get("type") == "glyph-provenance")
        node = next(item for item in document["nodes"] if item.get("nodeId") == extension["targetId"])
        source_map = next(item for item in document["sourceMaps"] if item.get("targetId") == extension["targetId"])
        return extension["payload"], node, source_map

    @staticmethod
    def _diagnostic_codes(document: dict) -> set[str]:
        return {item["code"] for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("code"), str)}

    def test_positive_tounicode_preserves_exact_mapping_identity_and_locator(self) -> None:
        document = self._convert("80", [("80", "fi")])
        payload, node, source_map = self._glyph(document)

        self.assertNotIn("rawStringBytesHex", payload)
        self.assertEqual(payload["characterCodes"], ["80"])
        self.assertEqual(payload["fontResource"], "F1")
        self.assertEqual(payload["fontObject"], "4 0 R")
        self.assertEqual(payload["encoding"], "StandardEncoding")
        self.assertEqual(payload["glyphIdentity"]["status"], "preserved")
        self.assertEqual(payload["glyphIdentity"]["baseFont"], "Helvetica")
        self.assertEqual(payload["glyphIdentity"]["sourceCodes"], ["80"])
        self.assertEqual(payload["mappingSource"], {"kind": "ToUnicode", "object": "6 0 R"})
        self.assertEqual(payload["mappingStatus"], "preserved")
        self.assertEqual(payload["unicode"], "fi")
        self.assertEqual(payload["sourceLocator"], {"kind": "pdf", "page": 1, "object": 5, "operator": 4})
        self.assertEqual(payload["sourceLocatorStatus"], "exact")
        self.assertEqual(source_map["locator"], payload["sourceLocator"])
        self.assertEqual(payload["provenance"]["unicode"], "ToUnicode")
        self.assertEqual(node["status"], "preserved")
        geometry = next(item for item in document["geometries"] if item["geometryId"] == node["geometryId"])
        self.assertEqual(geometry["status"], "approximated")
        self.assertIn({"feature": "glyph-text-mapping", "status": "preserved", "targetId": node["nodeId"]}, document["conversion"]["features"])

    def test_missing_cmap_is_unavailable_without_fabricated_unicode(self) -> None:
        document = self._convert("80", None)
        payload, node, _ = self._glyph(document)

        self.assertEqual(payload["mappingStatus"], "unavailable")
        self.assertEqual(payload["unicode"], "")
        self.assertEqual(payload["mappingSource"], {"kind": "unavailable"})
        self.assertEqual(payload["mappingCoverage"], {"mappedCharacterCodes": [], "unmappedCharacterCodes": ["80"]})
        self.assertIn("DFIR-PDF-GLYPH-MAPPING-UNAVAILABLE", self._diagnostic_codes(document))
        self.assertIn({"feature": "glyph-text-mapping", "status": "unavailable", "targetId": node["nodeId"], "diagnosticIds": next(item["diagnosticIds"] for item in document["conversion"]["features"] if item.get("feature") == "glyph-text-mapping")}, document["conversion"]["features"])
        self.assertTrue(any(item.get("feature") == "text-glyph" and item.get("status") == "approximated" for item in document["conversion"]["features"]))

    def test_partial_cmap_preserves_codes_but_does_not_join_partial_unicode(self) -> None:
        document = self._convert("8081", [("80", "A")])
        payload, _, _ = self._glyph(document)

        self.assertNotIn("rawStringBytesHex", payload)
        self.assertEqual(payload["characterCodes"], ["80", "81"])
        self.assertEqual(payload["mappingStatus"], "unavailable")
        self.assertEqual(payload["unicode"], "")
        self.assertEqual(payload["mappingCoverage"], {"mappedCharacterCodes": ["80"], "unmappedCharacterCodes": ["81"]})
        self.assertIn("DFIR-PDF-GLYPH-CMAP-PARTIAL", self._diagnostic_codes(document))

    def test_identity_h_without_tounicode_preserves_two_byte_codes_only(self) -> None:
        path = Path(self._tmp.name) / f"{self._testMethodName}.pdf"
        _write_identity_h_pdf(path)
        document, evidence = convert_path(path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)
        payload, _, _ = self._glyph(document)
        self.assertEqual(payload["encoding"], "Identity-H")
        self.assertEqual(payload["characterCodes"], ["0041", "0042"])
        self.assertEqual(payload["mappingStatus"], "unavailable")
        self.assertEqual(payload["unicode"], "")
        self.assertEqual(payload["mappingCoverage"], {"mappedCharacterCodes": [], "unmappedCharacterCodes": ["0041", "0042"]})

    def test_single_mutation_of_exact_projection_is_detected(self) -> None:
        document = self._convert("80", [("80", "fi")])
        payload, _, source_map = self._glyph(document)
        expected = {
            "characterCodes": ["80"],
            "fontResource": "F1",
            "fontObject": "4 0 R",
            "unicode": "fi",
            "mappingStatus": "preserved",
            "sourceLocator": source_map["locator"],
        }
        actual = {key: payload[key] for key in expected}
        self.assertEqual(actual, expected)
        mutated = deepcopy(actual)
        mutated["unicode"] = "f"
        mismatches = [key for key in expected if expected[key] != mutated[key]]
        self.assertEqual(mismatches, ["unicode"])
        self.assertNotEqual(actual, mutated)


if __name__ == "__main__":
    unittest.main()
