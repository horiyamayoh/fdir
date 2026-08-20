"""Targeted real-input probes for format adapter mutation cases.

The broad E2E command proves that a valid input can complete, but a mutation
campaign also needs an assertion for the exact invariant being mutated.  This
module creates small real container inputs, invokes the actual adapter, and
checks the resulting IR.  It is copied into disposable campaign checkouts as
declared test infrastructure; product source remains the archived base SHA.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
from typing import Any

from generate_e2e_fixtures import MARKDOWN, UNSUPPORTED_MARKDOWN, docx_parts, pdf_bytes, write_zip, xlsx_parts
from ir_validation import validate_document

from adapter_common import DocumentBuilder
from adapter_docx import convert as convert_docx
from adapter_markdown import _source_map, convert as convert_markdown
from adapter_pdf import _interpret_content, convert as convert_pdf
from adapter_xlsx import _typed, convert as convert_xlsx


class ProbeFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def validate_result(document: dict[str, Any], label: str) -> dict[str, Any]:
    require(isinstance(document, dict), f"{label}: adapter did not return an object")
    require(document.get("conversion", {}).get("status") != "failed", f"{label}: conversion failed")
    validate_document(document)
    return document


def _docx_package(path: Path, *, header: bool = False, missing_relationship: bool = False) -> None:
    parts = docx_parts()
    if header:
        document = str(parts["word/document.xml"])
        document = document.replace(
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"',
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"',
            1,
        )
        parts["word/document.xml"] = document.replace(
            "</w:sectPr>",
            '<w:headerReference w:type="default" r:id="rIdHeader"/></w:sectPr>',
        )
        rels = str(parts["word/_rels/document.xml.rels"])
        rels = rels.replace(
            "</Relationships>",
            '<Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>',
        )
        parts["word/_rels/document.xml.rels"] = rels
        content_types = str(parts["[Content_Types].xml"])
        parts["[Content_Types].xml"] = content_types.replace(
            "</Types>",
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>',
        )
        parts["word/header1.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:p><w:hyperlink w:anchor="story-link"><w:r><w:t>Header story link</w:t></w:r></w:hyperlink></w:p>'
            "</w:hdr>"
        )
    if missing_relationship:
        rels = str(parts["word/_rels/document.xml.rels"])
        parts["word/_rels/document.xml.rels"] = rels.replace(
            "</Relationships>",
            '<Relationship Id="rIdMissing" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/not-present.png"/></Relationships>',
        )
    write_zip(path, parts)


def probe_docx(probe: str, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="fdir-docx-probe-") as temporary:
        path = Path(temporary) / "probe.docx"
        if probe in {"docx-hyperlink-run", "docx-story-processing"}:
            _docx_package(path, header=True)
        else:
            _docx_package(path, missing_relationship=probe == "docx-missing-relationship")
        document = validate_result(convert_docx(path), probe)
        if probe == "docx-hyperlink-run":
            require(any(item.get("kind") == "hyperlink" and item.get("body") == "story-link" for item in document.get("annotations", [])), "DOCX story hyperlink was not preserved")
        elif probe == "docx-drawing-handler":
            require(any(item.get("kind") in {"textBox", "shape", "image", "connector"} for item in document.get("nodes", [])), "DOCX drawing node was not emitted")
            require("Shape text" in _all_text(document), "DOCX drawing text was not consumed")
        elif probe == "docx-story-processing":
            header_parts = [item for item in document.get("parts", []) if item.get("name") == "word/header1.xml"]
            require(header_parts and header_parts[0].get("rootNodeIds"), "DOCX header story was not parsed")
        elif probe == "docx-missing-relationship":
            require(any(item.get("status") == "unavailable" for item in document.get("relations", [])), "DOCX missing relationship was relabeled as available")
            require(any(item.get("code") == "DFIR-DOCX-RELATION-TARGET-MISSING" for item in document.get("diagnostics", [])), "DOCX missing relationship diagnostic was not emitted")
        elif probe == "docx-style-inheritance":
            heading = next((item for item in document.get("styles", []) if item.get("styleId") == "style-docx-resolved-Heading1"), None)
            require(heading is not None and heading.get("resolved", {}).get("fontFamily") == "Aptos", "DOCX style inheritance did not resolve the base font into Heading1")


def probe_xlsx(probe: str, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="fdir-xlsx-probe-") as temporary:
        path = Path(temporary) / "probe.xlsx"
        write_zip(path, xlsx_parts())
        if probe == "xlsx-binary-float":
            value = _typed("9007199254740992.1", "n")
            require(value.get("type") == "decimal" and value.get("value") == "9007199254740992.1", f"XLSX decimal lane was not exact: {value!r}")
            return
        document = validate_result(convert_xlsx(path), probe)
        cells = [item for item in document.get("nodes", []) if item.get("kind") == "cell"]
        by_address = {(item.get("address", {}).get("row"), item.get("address", {}).get("column")): item for item in cells}
        if probe == "xlsx-shared-string":
            require(by_address.get((1, 1), {}).get("value", {}).get("value") == "Name", "XLSX shared-string lookup was not preserved")
        elif probe == "xlsx-date-system":
            require(by_address.get((4, 2), {}).get("value", {}).get("value") == "2024-01-01", "XLSX 1900 date system was not preserved")
        elif probe == "xlsx-formula-lanes":
            formulas = document.get("formulas", [])
            require(formulas and formulas[0].get("values", {}).get("cached", {}).get("value") == "84", "XLSX formula cached lane was not preserved")
        elif probe == "xlsx-table-relationship":
            require(any(item.get("tableId") == "table-xlsx-0-DataTable" for item in document.get("tables", [])), "XLSX table relationship did not produce a table entity")
            require(any(item.get("kind") == "references" and item.get("status") == "preserved" and "table1.xml" in item.get("toId", "") for item in document.get("relations", [])), "XLSX table relationship was not preserved")
        elif probe == "xlsx-displayed-lane":
            displayed = {item.get("textId"): item for item in document.get("texts", []) if item.get("representation") == "displayed"}
            cell = by_address.get((4, 2), {})
            display_ids = [item for item in cell.get("textIds", []) if item in displayed]
            require(display_ids and displayed[display_ids[0]].get("value") == "2024-01-01", "XLSX displayed lane was not formatted from the exact source value")


def _pdf_document(streams: list[bytes], *, object_order: list[int] | None = None) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{ ' '.join(f'{3 + index * 2} 0 R' for index in range(len(streams)))}] /Count {len(streams)} >>".encode("ascii"),
    }
    for index, stream in enumerate(streams):
        page_number = 3 + index * 2
        content_number = page_number + 1
        objects[page_number] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_number} 0 R >>".encode("ascii")
        objects[content_number] = b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
    order = object_order or sorted(objects)
    output = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in order:
        offsets[number] = len(output)
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {max(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for number in range(1, max(objects) + 1):
        output.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {max(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)


def probe_pdf(probe: str, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="fdir-pdf-probe-") as temporary:
        path = Path(temporary) / "probe.pdf"
        if probe == "pdf-page-order":
            path.write_bytes(_pdf_document([b"BT 12 Tf 10 700 Td (FIRST) Tj ET", b"BT 12 Tf 10 700 Td (SECOND) Tj ET", b"BT 12 Tf 10 700 Td (THIRD) Tj ET"], object_order=[1, 2, 5, 6, 7, 8, 3, 4]))
            document = validate_result(convert_pdf(path), probe)
            text_by_id = {item.get("textId"): item.get("value") for item in document.get("texts", [])}
            pages = [item for item in document.get("nodes", []) if item.get("kind") == "section"]
            page_text = [[text_by_id.get(text_id) for node in document.get("nodes", []) if node.get("parentId") == page.get("nodeId") for text_id in node.get("textIds", [])] for page in pages]
            require(page_text[:3] == [["FIRST"], ["SECOND"], ["THIRD"]], f"PDF page stream order changed: {page_text!r}")
        elif probe == "pdf-tounicode":
            document = validate_result(convert_pdf(root / "e2e" / "corpus" / "pdf-cmap.pdf"), probe)
            mappings = [item for item in document.get("extensions", []) if item.get("type") == "font-cmap"]
            require(mappings and mappings[0].get("payload", {}).get("mappingStatus") == "preserved", "PDF ToUnicode mapping was not preserved")
            require(any(item.get("value") == "A" for item in document.get("texts", [])), "PDF mapped text was not retained")
        elif probe == "pdf-graphics-restore":
            _, _, _, _, states = _interpret_content(b"q 2 0 0 2 0 0 cm Q 1 0 0 1 0 0 cm")
            restored = [item for item in states if item.get("operator") == "Q"]
            require(restored and restored[0].get("state", {}).get("ctm") == ["1", "0", "0", "1", "0", "0"], "PDF graphics state Q did not restore the saved CTM")
        elif probe == "pdf-unknown-operator":
            _, _, unsupported, _, _ = _interpret_content(b"/XUnsupported Do")
            require("Do" in unsupported, "PDF unknown operator was not recorded")
        elif probe == "pdf-annotation-target":
            raw = pdf_bytes().replace(b"/Contents 4 0 R >>", b"/Annots [7 0 R] /Contents 4 0 R >>")
            path.write_bytes(raw)
            document = validate_result(convert_pdf(path), probe)
            require(any(item.get("kind") == "hyperlink" and item.get("targetIds") for item in document.get("annotations", [])), "PDF annotation target was not typed as a hyperlink")
        elif probe == "pdf-interleaved-paint":
            path.write_bytes(_pdf_document([b"0 0 m 100 0 l S BT 12 Tf 10 700 Td (TEXT) Tj ET"]))
            document = validate_result(convert_pdf(path), probe)
            kinds = {item.get("nodeId"): item.get("kind") for item in document.get("nodes", [])}
            paint = next(item for item in document.get("orders", []) if item.get("kind") == "draw")
            reading = next(item for item in document.get("orders", []) if item.get("kind") == "reading")
            paint_kinds = [kinds.get(item.get("id")) for item in paint.get("items", [])]
            reading_kinds = [kinds.get(item.get("id")) for item in reading.get("items", [])]
            require(paint_kinds == ["section", "glyph", "path"], f"PDF paint order did not follow source interleaving: {paint_kinds!r}")
            require(reading_kinds == ["glyph", "path"], f"PDF reading order did not follow semantic order: {reading_kinds!r}")


def probe_markdown(probe: str, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="fdir-markdown-probe-") as temporary:
        path = Path(temporary) / "probe.md"
        source = UNSUPPORTED_MARKDOWN if probe == "markdown-unsupported-construct" else MARKDOWN
        if probe == "markdown-delimiter-resolution":
            source = source.replace("A paragraph with **bold**", "A paragraph with [ref] and **bold**")
        path.write_text(source, encoding="utf-8", newline="\n")
        document = validate_result(convert_markdown(path), probe)
        if probe == "markdown-span-end":
            builder = DocumentBuilder(path, "markdown", "commonmark")
            _source_map(builder, builder.root_id, 1, 1, 1, 10, token_start=0, token_end=None)
            locator = builder.document["sourceMaps"][-1]["locator"]
            require(locator.get("tokenEnd") == 9, f"Markdown source span end is not exact: {locator!r}")
        elif probe == "markdown-delimiter-resolution":
            require(any(item.get("representation") == "normalized" and item.get("value") == "bold" for item in document.get("texts", [])), "Markdown delimiter normalization was not preserved")
            require(any(item.get("representation") == "normalized" and item.get("value") == "emphasis" for item in document.get("texts", [])), "Markdown emphasis delimiter normalization was not preserved")
            require(sum(1 for item in document.get("annotations", []) if item.get("kind") == "hyperlink" and item.get("referenceId") == "ref" and item.get("body") == "https://example.invalid/reference") >= 2, "Markdown shortcut/reference links were not both resolved")
        elif probe == "markdown-reference-resolution":
            require(any(item.get("kind") == "hyperlink" and item.get("referenceId") == "ref" and item.get("body") == "https://example.invalid/reference" for item in document.get("annotations", [])), "Markdown reference link was not resolved")
        elif probe == "markdown-table-separator":
            tables = document.get("tables", [])
            require(tables and len(tables[0].get("rowIds", [])) == 2, "Markdown table separator was not removed from the data rows")
            first_row = next((item for item in document.get("nodes", []) if item.get("nodeId") == tables[0].get("rowIds", [""])[0]), {})
            cell_ids = first_row.get("childIds", [])
            cell_text = _all_text({"nodes": document.get("nodes", []), "texts": document.get("texts", [])})
            require(cell_ids and "Name" in cell_text, "Markdown table header was not retained")
        elif probe == "markdown-unsupported-construct":
            require(any(item.get("status") == "unsupported" for item in document.get("nodes", [])), "Markdown unsupported construct was marked preserved")
            require(any(item.get("status") == "unsupported" for item in document.get("conversion", {}).get("features", [])), "Markdown unsupported feature disposition was not emitted")


def _all_text(document: dict[str, Any]) -> str:
    return " ".join(str(item.get("value", "")) for item in document.get("texts", []) if isinstance(item, dict))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", required=True, choices=["docx", "xlsx", "pdf", "markdown"])
    parser.add_argument("--probe", required=True)
    args = parser.parse_args()
    probe = args.probe.split("-variant-", 1)[0]
    root = Path(__file__).resolve().parents[1]
    try:
        if args.format == "docx":
            probe_docx(probe, root)
        elif args.format == "xlsx":
            probe_xlsx(probe, root)
        elif args.format == "pdf":
            probe_pdf(probe, root)
        else:
            probe_markdown(probe, root)
        print(f"probe passed: {args.format}/{args.probe}")
        return 0
    except Exception as exc:
        print(f"probe failed: {args.format}/{args.probe}: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
