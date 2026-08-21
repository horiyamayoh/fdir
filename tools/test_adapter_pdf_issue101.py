"""PDF-only regressions for issue #101's bounded parser and source facts."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

try:
    from adapter_pdf import (
        _page_content_sources,
        _pdf_inline_image_end,
        _pdf_number_array,
        _pdf_expand_object_streams,
        _pdf_objects,
        _pdf_operations_detailed,
        _pdf_page_tree,
        _pdf_resource_entries,
        _pdf_stream_parts,
        _pdf_xref_index,
        _parse_cmap,
    )
    from convert_document import convert_path
    from ir_validation import validate_document
    from qualification_evidence import case_evidence, validate_source_feature_closure
except ImportError:  # pragma: no cover - package-style test execution.
    from tools.adapter_pdf import (
        _page_content_sources,
        _pdf_inline_image_end,
        _pdf_number_array,
        _pdf_expand_object_streams,
        _pdf_objects,
        _pdf_operations_detailed,
        _pdf_page_tree,
        _pdf_resource_entries,
        _pdf_stream_parts,
        _pdf_xref_index,
        _parse_cmap,
    )
    from tools.convert_document import convert_path
    from tools.ir_validation import validate_document
    from tools.qualification_evidence import case_evidence, validate_source_feature_closure


def _dictionary(value: str) -> bytes:
    return f"<< {value} >>".encode("ascii")


def _stream(value: str, payload: bytes) -> bytes:
    return f"<< {value} /Length {len(payload)} >>\nstream\n".encode("ascii") + payload + b"\nendstream"


def _write_pdf(path: Path, *, unknown_operator: bool = False) -> None:
    first_stream = b"BT /F1 12 Tf 10 20 Td (first) Tj ET\n0 0 20 20 re S\n"
    second_stream = (b"1 2 UnknownOp\n" if unknown_operator else b"") + b"/Im1 Do\nBT /F1 12 Tf 10 40 Td (second) Tj ET\n"
    third_stream = b"BT /F1 12 Tf 10 60 Td (third) Tj ET\n"
    objects = [
        (20, _dictionary("/Type /Catalog /Pages 30 0 R")),
        (30, _dictionary("/Type /Pages /Kids [50 0 R 40 0 R] /Count 2 /MediaBox [0 0 600 800] /CropBox [10 20 500 700] /Rotate 90 /Resources << /Font << /F1 60 0 R >> /XObject << /Im1 70 0 R >> >>")),
        (50, _dictionary("/Type /Page /Parent 30 0 R /Contents [80 0 R 81 0 R] /Annots [90 0 R 91 0 R]")),
        (40, _dictionary("/Type /Page /Parent 30 0 R /Contents 82 0 R")),
        (60, _dictionary("/Type /Font /Subtype /Type1 /BaseFont /Helvetica")),
        (70, _stream("/Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8", b"\x00\x00\x00\xff\xff\xff")),
        (80, _stream("", first_stream)),
        (81, _stream("", second_stream)),
        (82, _stream("", third_stream)),
        (90, _dictionary("/Type /Annot /Subtype /Text /Rect [0 0 10 10] /Contents (actual comment)")),
        # A marker-like dictionary must not become an annotation merely because it is referenced from /Annots.
        (91, _dictionary("/Subtype /Text /Contents (marker-like)")),
    ]
    chunks = [b"%PDF-1.7\n"]
    for object_number, body in objects:
        chunks.extend([f"{object_number} 0 obj\n".encode("ascii"), body, b"\nendobj\n"])
    chunks.append(b"%%EOF\n")
    path.write_bytes(b"".join(chunks))


def _write_xref_stream_pdf(path: Path) -> None:
    content = b"BT (xref stream) Tj ET"
    bodies = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>"),
        (4, f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"),
    ]
    chunks = [b"%PDF-1.7\n"]
    offsets: dict[int, int] = {}
    for number, body in bodies:
        offsets[number] = sum(len(chunk) for chunk in chunks)
        chunks.extend([f"{number} 0 obj\n".encode("ascii"), body, b"\nendobj\n"])
    xref_offset = sum(len(chunk) for chunk in chunks)
    rows = bytearray()
    rows.extend(bytes([0]) + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big"))
    for number in range(1, 5):
        rows.extend(bytes([1]) + offsets[number].to_bytes(4, "big") + (0).to_bytes(2, "big"))
    rows.extend(bytes([1]) + xref_offset.to_bytes(4, "big") + (0).to_bytes(2, "big"))
    xref_body = (
        f"<< /Type /XRef /Size 6 /Root 1 0 R /W [1 4 2] /Length {len(rows)} >>\nstream\n".encode("ascii")
        + bytes(rows)
        + b"\nendstream"
    )
    chunks.extend([b"5 0 obj\n", xref_body, b"\nendobj\n", f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")])
    path.write_bytes(b"".join(chunks))


def _write_object_stream_pdf(path: Path) -> None:
    content = b"BT /F1 12 Tf (object stream) Tj ET"
    object_stream_payload = b"4 0 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    direct_bodies = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        (5, f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"),
        (6, f"<< /Type /ObjStm /N 1 /First 4 /Length {len(object_stream_payload)} >>\nstream\n".encode("ascii") + object_stream_payload + b"\nendstream"),
    ]
    chunks = [b"%PDF-1.7\n"]
    offsets: dict[int, int] = {}
    for number, body in direct_bodies:
        offsets[number] = sum(len(chunk) for chunk in chunks)
        chunks.extend([f"{number} 0 obj\n".encode("ascii"), body, b"\nendobj\n"])
    xref_offset = sum(len(chunk) for chunk in chunks)
    rows = bytearray()
    rows.extend(bytes([0]) + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big"))
    for number in range(1, 8):
        if number == 4:
            rows.extend(bytes([2]) + (6).to_bytes(4, "big") + (0).to_bytes(2, "big"))
        elif number == 7:
            rows.extend(bytes([1]) + xref_offset.to_bytes(4, "big") + (0).to_bytes(2, "big"))
        else:
            rows.extend(bytes([1]) + offsets[number].to_bytes(4, "big") + (0).to_bytes(2, "big"))
    xref_body = (
        f"<< /Type /XRef /Size 8 /Root 1 0 R /W [1 4 2] /Length {len(rows)} >>\nstream\n".encode("ascii")
        + bytes(rows)
        + b"\nendstream"
    )
    chunks.extend([b"7 0 obj\n", xref_body, b"\nendobj\n", f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")])
    path.write_bytes(b"".join(chunks))


class PDFIssue101Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(__file__).resolve().parents[1] / "e2e" / ".run" / "qualification-issue-101-pdf-fixtures"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.path = self.scratch / f"{self._testMethodName}.pdf"

    def test_page_tree_and_contents_are_byte_aware_and_authored_ordered(self) -> None:
        _write_pdf(self.path)
        raw = self.path.read_bytes()
        objects = _pdf_objects(raw)
        pages = _pdf_page_tree(objects)

        self.assertEqual([page["object"] for page in pages], [(50, 0), (40, 0)])
        self.assertEqual(_pdf_number_array(pages[0]["mediaBox"], objects), [Decimal(0), Decimal(0), Decimal(600), Decimal(800)])
        self.assertEqual(_pdf_number_array(pages[0]["cropBox"], objects), [Decimal(10), Decimal(20), Decimal(500), Decimal(700)])
        self.assertEqual(pages[0]["rotate"], ("number", Decimal(90)))
        self.assertEqual(_pdf_resource_entries(pages[0]["resources"], "Font", objects), {"F1": (60, 0)})
        self.assertEqual(_pdf_resource_entries(pages[0]["resources"], "XObject", objects), {"Im1": (70, 0)})

        sources = _page_content_sources(objects, raw, page_records=pages)
        self.assertEqual([source["streamObjects"] for source in sources], [[(80, 0), (81, 0)], [(82, 0)]])
        self.assertEqual([item["object"] for item in sources[0]["streams"]], [(80, 0), (81, 0)])
        self.assertEqual(sources[0]["streamSpans"][1]["object"], 81)

    def test_classic_xref_authenticates_object_offsets_and_rejects_mutation(self) -> None:
        chunks = [b"%PDF-1.7\n"]
        offsets: dict[int, int] = {}
        for number, body in ((1, b"<< /Type /Catalog >>"), (2, b"<< /Type /Font /Subtype /Type1 >>")):
            offsets[number] = sum(len(chunk) for chunk in chunks)
            chunks.extend([f"{number} 0 obj\n".encode("ascii"), body, b"\nendobj\n"])
        xref_offset = sum(len(chunk) for chunk in chunks)
        chunks.extend([
            b"xref\n0 3\n",
            b"0000000000 65535 f \n",
            f"{offsets[1]:010d} 00000 n \n".encode("ascii"),
            f"{offsets[2]:010d} 00000 n \n".encode("ascii"),
            b"trailer\n<< /Size 3 /Root 1 0 R >>\n",
            f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii"),
        ])
        raw = b"".join(chunks)
        objects = _pdf_objects(raw)
        events: list[tuple[str, str]] = []
        info = _pdf_xref_index(raw, objects=objects, events=events)
        self.assertTrue(info["valid"])
        self.assertTrue(info["entries"][(2, 0)]["verified"])
        self.assertEqual(events, [])

        mutated = raw.replace(f"{offsets[2]:010d} 00000 n".encode("ascii"), b"0000000001 00000 n")
        mutated_events: list[tuple[str, str]] = []
        mutated_info = _pdf_xref_index(mutated, objects=_pdf_objects(mutated), events=mutated_events)
        self.assertFalse(mutated_info["valid"])
        self.assertIn("DFIR-PDF-XREF-OBJECT-MISMATCH", {code for code, _ in mutated_events})

    def test_xref_stream_records_are_bounded_and_verified(self) -> None:
        _write_xref_stream_pdf(self.path)
        raw = self.path.read_bytes()
        objects = _pdf_objects(raw)
        events: list[tuple[str, str]] = []
        info = _pdf_xref_index(raw, objects=objects, events=events)
        self.assertTrue(info["valid"])
        self.assertTrue(info["xrefStream"])
        self.assertFalse(info["classicXref"])
        self.assertTrue(info["entries"][(3, 0)]["verified"])
        self.assertEqual(events, [])

    def test_xref_stream_object_entries_are_materialized_before_page_walk(self) -> None:
        _write_object_stream_pdf(self.path)
        raw = self.path.read_bytes()
        objects = _pdf_objects(raw)
        events: list[tuple[str, str]] = []
        info = _pdf_xref_index(raw, objects=objects, events=events)
        self.assertTrue(info["objectStream"])
        self.assertFalse(info["entries"][(4, 0)]["bodyAvailable"])
        self.assertEqual(_pdf_expand_object_streams(objects, info, events=events), 1)
        self.assertTrue(info["entries"][(4, 0)]["bodyAvailable"])
        self.assertIn((4, 0), objects)
        document, evidence = convert_path(self.path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)
        self.assertIn("object:4 0", {item.get("derivedHandle") for item in document["resources"]})

    def test_cmap_bfrange_array_preserves_all_independent_targets(self) -> None:
        cmap = b"""begincmap
1 beginbfchar
<01> <0041>
endbfchar
1 beginbfrange
<02> <04> <0042>
endbfrange
1 beginbfrange
<05> <06> [<0045> <0046>]
endbfrange
endcmap"""
        self.assertEqual(
            _parse_cmap(cmap),
            [
                {"sourceCode": "01", "unicode": "A"},
                {"sourceCode": "02", "unicode": "B"},
                {"sourceCode": "03", "unicode": "C"},
                {"sourceCode": "04", "unicode": "D"},
                {"sourceCode": "05", "unicode": "E"},
                {"sourceCode": "06", "unicode": "F"},
            ],
        )

    def test_inline_image_samples_are_skipped_as_binary(self) -> None:
        content = b"BI /W 2 /H 1 /CS /RGB /BPC 8 ID \x00EI\xff\xff\xff EI BT (kept) Tj ET"
        events: list[tuple[str, str]] = []
        operations = _pdf_operations_detailed(content, events=events)
        self.assertEqual([operation.operator for operation in operations], ["BI", "BT", "Tj", "ET"])
        self.assertEqual(operations[1].start, content.index(b"BT"))
        self.assertNotIn("EI", [operation.operator for operation in operations])
        dictionary = [("name", "/W"), ("number", Decimal(2)), ("name", "/H"), ("number", Decimal(1)), ("name", "/CS"), ("name", "/RGB"), ("name", "/BPC"), ("number", Decimal(8))]
        self.assertEqual(_pdf_inline_image_end(content, content.index(b"ID") + 2, dictionary), content.rindex(b"EI") + 2)
        self.assertEqual(events, [])

    def test_indirect_stream_length_protects_binary_object_markers(self) -> None:
        payload = b"prefix endobj endstream suffix"
        body = b"<< /Length 3 0 R >>\nstream\n" + payload + b"\nendstream"
        raw = b"%PDF-1.7\n1 0 obj\n" + body + b"\nendobj\n3 0 obj\n" + str(len(payload)).encode("ascii") + b"\nendobj\n%%EOF\n"
        objects = _pdf_objects(raw)
        self.assertIn((1, 0), objects)
        self.assertIn((3, 0), objects)
        parts = _pdf_stream_parts(objects[(1, 0)], objects=objects)
        self.assertIsNotNone(parts)
        assert parts is not None
        self.assertEqual(parts[1], payload)

    def test_conversion_preserves_paint_order_parses_annotations_and_reports_markers(self) -> None:
        _write_pdf(self.path)
        document, evidence = convert_path(self.path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)

        page_id = next(node["nodeId"] for node in document["nodes"] if node.get("kind") == "section" and node.get("parentId") == document["rootNodeId"])
        page_order = next(order for order in document["orders"] if order.get("ownerId") == page_id)
        page_nodes = {node["nodeId"]: node for node in document["nodes"]}
        self.assertEqual([page_nodes[item["id"]]["kind"] for item in page_order["items"]], ["glyph", "path", "image", "glyph"])
        self.assertEqual(page_order["status"], "preserved")

        first_surface = next(surface for surface in document["surfaces"] if surface["ordinal"] == 0)
        coordinate_space = next(item for item in document["coordinateSpaces"] if item["coordinateSpaceId"] == first_surface["coordinateSpaceId"])
        self.assertEqual(coordinate_space["transformToParent"], {"a": "0", "b": "1", "c": "-1", "d": "0", "e": "680", "f": "0"})

        text_nodes = [node for node in document["nodes"] if node.get("kind") == "glyph" and node.get("parentId") == page_id]
        self.assertEqual(len(text_nodes), 2)
        second_map = next(item for item in document["sourceMaps"] if item.get("targetId") == text_nodes[1]["nodeId"])
        self.assertEqual(second_map["locator"]["object"], 81)

        annotations = [item for item in document["annotations"] if item.get("targetIds") == [page_id]]
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]["body"], "actual comment")
        self.assertIn("DFIR-PDF-ANNOTATION-TYPE-UNAVAILABLE", {item["code"] for item in document["diagnostics"]})
        image_relations = [
            item for item in document["relations"]
            if item.get("kind") == "usesResource" and item.get("target") == "70 0 R"
        ]
        self.assertEqual(len(image_relations), 2)
        self.assertEqual({item["status"] for item in image_relations}, {"preserved"})
        self.assertEqual({item["sourceOccurrenceId"] for item in image_relations}, {"pdf-page-xobject"})

    def test_unknown_content_operator_makes_order_ambiguous_with_diagnostic(self) -> None:
        _write_pdf(self.path, unknown_operator=True)
        document, evidence = convert_path(self.path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)
        page_id = next(node["nodeId"] for node in document["nodes"] if node.get("kind") == "section" and node.get("parentId") == document["rootNodeId"])
        page_order = next(order for order in document["orders"] if order.get("ownerId") == page_id)
        root_order = next(order for order in document["orders"] if order.get("ownerId") == document["rootNodeId"] and order.get("kind") == "draw")
        self.assertEqual(page_order["status"], "ambiguous")
        self.assertEqual(root_order["status"], "ambiguous")
        self.assertIn("DFIR-PDF-OPERATOR-UNSUPPORTED", {item["code"] for item in document["diagnostics"]})

    def test_unavailable_renderer_and_ocr_are_source_backed_without_claims(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "e2e" / "corpus" / "pdf-unsupported.pdf"
        document, evidence = convert_path(source_path, "pdf")
        self.assertEqual(evidence["outcome"], "success")
        validate_document(document)

        observations = {
            item["feature"]: item
            for item in document["conversion"]["features"]
            if item.get("feature") in {"renderer-observation", "ocr-observation"}
        }
        self.assertEqual(set(observations), {"renderer-observation", "ocr-observation"})
        self.assertEqual({item["status"] for item in observations.values()}, {"unavailable"})
        self.assertEqual({item["targetId"] for item in observations.values()}, {document["rootNodeId"]})
        self.assertFalse(
            [
                item
                for item in document.get("observations", [])
                if item.get("kind") in {"renderer", "ocr"} and item.get("status") != "unavailable"
            ]
        )

        evidence_report = case_evidence(source_path, "pdf", document)
        closure = validate_source_feature_closure(document, evidence_report)
        self.assertEqual(closure["status"], "passed", closure)
        self.assertFalse(
            [
                item
                for item in closure.get("mismatches", [])
                if item.get("code") == "IR_DISPOSITION_HAS_NO_SOURCE_OCCURRENCE"
            ]
        )


if __name__ == "__main__":
    unittest.main()
