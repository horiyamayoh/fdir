"""Bounded, dependency-free PDF form-fact adapter.

This parser intentionally handles the stable subset needed for form facts:
pages, literal text operators, graphics paths, clipping, coordinate systems,
and ordering.  Renderer/OCR workers are optional observations; unavailable
workers are reported explicitly and never fabricate source text.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id


LITERAL_RE = re.compile(r"\(((?:\\.|[^\\)])*)\)\s*(?:Tj|'|\")")
HEX_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*Tj")
NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
PATH_RE = re.compile(rf"(?P<x>{NUMBER})\s+(?P<y>{NUMBER})\s+(?P<op>m|l)\s*")


def inspect(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF-"):
        raise AdapterError("input does not start with a PDF header")
    text = data.decode("latin-1", errors="replace")
    return {
        "format": "pdf",
        "version": text[5:8].split("\n", 1)[0],
        "bytes": len(data),
        "pages": len(re.findall(r"/Type\s*/Page\b", text)),
        "streams": text.count("stream"),
        "capabilities": ["pages", "text", "glyphs", "paths", "clipping", "paint-order", "bounded-observations"],
        "limits": {"maxInputBytes": limits.max_input_bytes, "maxPdfObjects": limits.max_pdf_objects},
    }


def _decode_literal(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    value = re.sub(r"\\([nrtbf])", lambda match: {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}[match.group(1)], value)
    return value


def _stream_text(data: str) -> list[str]:
    values: list[str] = []
    for match in LITERAL_RE.finditer(data):
        values.append(_decode_literal(match.group(1)))
    for match in HEX_RE.finditer(data):
        try:
            values.append(bytes.fromhex(match.group(1)).decode("utf-16-be" if len(match.group(1)) % 4 == 0 else "latin-1", errors="replace"))
        except ValueError:
            continue
    return values


def _coordinate(builder: DocumentBuilder, page_number: int) -> str:
    space_id = safe_id("space", f"pdf-page-{page_number}")
    if builder.find("coordinateSpaces", "coordinateSpaceId", space_id) is None:
        builder.add_item("coordinateSpaces", {"coordinateSpaceId": space_id, "unit": "pt", "origin": {"x": "0", "y": "0"}}, "coordinateSpaceId")
    return space_id


def _observation(builder: DocumentBuilder, kind: str, target_id: str, *, geometry_id: str | None = None, text_id: str | None = None) -> str:
    observation_id = safe_id("observation", f"pdf-{kind}-{target_id}")
    item: dict[str, Any] = {"observationId": observation_id, "kind": kind, "targetId": target_id, "method": "worker-unavailable", "engine": "unavailable", "status": "unavailable"}
    if geometry_id:
        item["geometryId"] = geometry_id
    if text_id:
        item["textId"] = text_id
    builder.add_item("observations", item, "observationId")
    return observation_id


def _add_text(builder: DocumentBuilder, page_id: str, value: str, page_number: int, fragment: int, space_id: str) -> str:
    text_id = safe_id("text", f"pdf-source-{page_number}-{fragment}")
    builder.add_text(text_id, value, representation="source", provenance="decoded", status="preserved")
    glyph_id = safe_id("node", f"pdf-glyph-{page_number}-{fragment}")
    geometry_id = safe_id("geometry", f"pdf-glyph-{page_number}-{fragment}")
    builder.add_item("geometries", {"geometryId": geometry_id, "spaceId": space_id, "kind": "glyphBoxes", "primitives": [{"kind": "rectangle", "x": "72", "y": decimal(720 - fragment * 18), "width": {"value": decimal(max(1, len(value) * 9)), "unit": "pt"}, "height": {"value": "18", "unit": "pt"}}], "status": "preserved"}, "geometryId")
    builder.add_node("glyph", glyph_id, parent_id=page_id, textIds=[text_id], geometryId=geometry_id, status="preserved")
    builder.add_source_map(glyph_id, {"page": page_number, "object": fragment, "operator": fragment})
    _observation(builder, "renderer", glyph_id, geometry_id=geometry_id)
    _observation(builder, "ocr", glyph_id, geometry_id=geometry_id)
    builder.add_feature("text-glyph", "preserved", target_id=glyph_id)
    return glyph_id


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "pdf", "1.7", limits=limits, root_kind="document")
    try:
        raw = path.read_bytes()
        if not raw.startswith(b"%PDF-"):
            diagnostic = builder.add_diagnostic("DFIR-PDF-HEADER-FAILED", "input does not start with a PDF header", severity="error", phase="parse")
            builder.add_feature("header", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        content = raw.decode("latin-1", errors="replace")
        if content.count("obj") > limits.max_pdf_objects:
            diagnostic = builder.add_diagnostic("DFIR-PDF-OBJECT-LIMIT", "PDF object limit exceeded", severity="error", phase="parse")
            builder.add_feature("package-validation", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        page_count = len(re.findall(r"/Type\s*/Page\b", content))
        if page_count == 0:
            diagnostic = builder.add_diagnostic("DFIR-PDF-PAGE-MISSING", "PDF contains no page object", severity="error", phase="parse")
            builder.add_feature("pages", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        if re.search(r"\bDo\b", content):
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-OPERATOR-UNSUPPORTED",
                "PDF XObject invocation is retained only as an unsupported form feature.",
                phase="normalize",
                target_id=builder.root_id,
            )
            builder.add_feature("xobject-operator", "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        document_part = safe_id("part", "pdf-document")
        builder.add_item("parts", {"partId": document_part, "kind": "document", "name": "PDF document", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
        streams = re.findall(r"stream\r?\n(.*?)\r?\nendstream", content, flags=re.S)
        pages_seen = 0
        for page_match in re.finditer(r"/Type\s*/Page\b", content):
            pages_seen += 1
            page_id = safe_id("node", f"pdf-page-{pages_seen}")
            surface_id = safe_id("surface", f"pdf-page-{pages_seen}")
            space_id = _coordinate(builder, pages_seen)
            builder.add_item("surfaces", {"surfaceId": surface_id, "partId": document_part, "kind": "page", "ordinal": pages_seen - 1, "coordinateSpaceId": space_id, "status": "preserved"}, "surfaceId")
            builder.add_node("section", page_id, parent_id=builder.root_id, part_id=document_part, status="preserved")
            builder.add_source_map(page_id, {"page": pages_seen, "object": pages_seen})
            page_text = streams[pages_seen - 1] if pages_seen <= len(streams) else content
            fragments = _stream_text(page_text)
            if not fragments:
                fragments = _stream_text(content)
            for fragment, value in enumerate(fragments, start=1):
                _add_text(builder, page_id, value, pages_seen, fragment, space_id)
            path_nodes: list[str] = []
            for path_index, match in enumerate(PATH_RE.finditer(page_text)):
                path_id = safe_id("node", f"pdf-path-{pages_seen}-{path_index}")
                geometry_id = safe_id("geometry", f"pdf-path-{pages_seen}-{path_index}")
                geometry_kind = "clippingPath" if re.search(r"W\s*n", page_text[match.end() : match.end() + 20]) else "polyline"
                primitive = {"kind": "point", "x": decimal(match.group("x")), "y": decimal(match.group("y"))}
                builder.add_item("geometries", {"geometryId": geometry_id, "spaceId": space_id, "kind": geometry_kind, "primitives": [primitive], "status": "preserved"}, "geometryId")
                builder.add_node("path", path_id, parent_id=page_id, geometryId=geometry_id, status="preserved")
                builder.add_source_map(path_id, {"page": pages_seen, "operator": path_index})
                path_nodes.append(path_id)
                builder.add_feature("clipping" if geometry_kind == "clippingPath" else "path", "preserved", target_id=path_id)
            if "/Annots" in page_match.string[max(0, page_match.start() - 400) : page_match.end() + 400]:
                annotation_id = safe_id("annotation", f"pdf-link-{pages_seen}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [page_id], "body": "PDF annotation destination retained as form fact", "status": "preserved"}, "annotationId")
            renderer_diagnostic = builder.add_diagnostic("DFIR-PDF-RENDERER-UNAVAILABLE", "No renderer worker is configured; renderer result is unavailable and source facts are unchanged.", phase="observe", target_id=page_id)
            ocr_diagnostic = builder.add_diagnostic("DFIR-PDF-OCR-UNAVAILABLE", "No OCR worker is configured; OCR result is unavailable and source text is unchanged.", phase="observe", target_id=page_id)
            builder.add_feature("renderer-observation", "unavailable", target_id=page_id, diagnostic_ids=[renderer_diagnostic])
            builder.add_feature("ocr-observation", "unavailable", target_id=page_id, diagnostic_ids=[ocr_diagnostic])
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-paint"), "kind": "draw", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-reading"), "kind": "reading", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:]) if node["kind"] in {"glyph", "path"}], "status": "ambiguous"}, "orderId")
        if "/Font" in content:
            builder.add_item("resources", {"resourceId": safe_id("resource", "pdf-font"), "kind": "font", "mediaType": "application/x-font", "availability": "available", "derivedHandle": "font-resource"}, "resourceId")
        builder.add_feature("pages", "preserved", target_id=builder.root_id)
        return builder.finish()
    except (OSError, ValueError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-PDF-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("document", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
