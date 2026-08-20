"""Bounded, dependency-free PDF form-fact adapter.

This parser intentionally handles the stable subset needed for form facts:
pages, literal text operators, graphics paths, clipping, coordinate systems,
and ordering.  Renderer/OCR workers are optional observations; unavailable
workers are reported explicitly and never fabricate source text.
"""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal, InvalidOperation
import re
from typing import Any
import zlib

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id


LITERAL_RE = re.compile(r"\(((?:\\.|[^\\)])*)\)\s*(?:Tj|'|\")")
HEX_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*Tj")
NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
PATH_RE = re.compile(rf"(?P<x>{NUMBER})\s+(?P<y>{NUMBER})\s+(?P<op>m|l)\s*")


def _pdf_objects(data: bytes) -> dict[tuple[int, int], bytes]:
    return {(int(match.group(1)), int(match.group(2))): match.group(3) for match in re.finditer(rb"(?m)(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj\b", data, flags=re.S)}


def _decode_stream(object_data: bytes) -> bytes:
    marker = re.search(rb"\bstream\r?\n", object_data)
    if marker is None:
        return b""
    end = object_data.find(b"endstream", marker.end())
    if end < 0:
        return b""
    value = object_data[marker.end():end]
    if b"/FlateDecode" in object_data:
        try:
            return zlib.decompress(value)
        except zlib.error:
            return value
    if b"/ASCIIHexDecode" in object_data:
        try:
            return bytes.fromhex(value.replace(b">", b"").decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return value
    return value


def _page_content_streams(objects: dict[tuple[int, int], bytes], fallback: bytes) -> list[bytes]:
    pages = [(key, value) for key, value in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", value)]
    streams: list[bytes] = []
    for _, page in pages:
        references = [(int(number), int(generation)) for number, generation in re.findall(rb"(\d+)\s+(\d+)\s+R", page)]
        page_streams = [_decode_stream(objects[ref]) for ref in references if ref in objects and b"stream" in objects[ref]]
        streams.append(b"\n".join(page_streams) if page_streams else page)
    return streams or [fallback]


def _pdf_lex(data: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    index = 0
    delimiters = "()<>[]{}/%"
    while index < len(data):
        character = data[index]
        if character.isspace():
            index += 1
            continue
        if character == "%":
            newline = data.find("\n", index)
            index = len(data) if newline < 0 else newline + 1
            continue
        if data.startswith("<<", index) or data.startswith(">>", index):
            tokens.append(("delimiter", data[index:index + 2]))
            index += 2
            continue
        if character in "[]":
            tokens.append(("delimiter", character))
            index += 1
            continue
        if character == "(":
            index += 1
            depth = 1
            value: list[str] = []
            while index < len(data) and depth:
                current = data[index]
                if current == "\\" and index + 1 < len(data):
                    value.append(data[index:index + 2])
                    index += 2
                    continue
                if current == "(":
                    depth += 1
                elif current == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                value.append(current)
                index += 1
            tokens.append(("string", _decode_literal("".join(value))))
            continue
        if character == "<" and not data.startswith("<<", index):
            end = data.find(">", index + 1)
            raw = data[index + 1: len(data) if end < 0 else end]
            raw = re.sub(r"\s+", "", raw)
            try:
                decoded = bytes.fromhex(raw + ("0" if len(raw) % 2 else "")).decode("utf-16-be" if raw.lower().startswith("feff") else "latin-1", errors="replace")
            except ValueError:
                decoded = raw
            tokens.append(("string", decoded))
            index = len(data) if end < 0 else end + 1
            continue
        if character == "/":
            end = index + 1
            while end < len(data) and not data[end].isspace() and data[end] not in delimiters:
                end += 1
            tokens.append(("name", data[index:end]))
            index = end
            continue
        number = re.match(r"-?(?:\d+(?:\.\d*)?|\.\d+)", data[index:])
        if number:
            raw = number.group(0)
            try:
                tokens.append(("number", Decimal(raw)))
            except InvalidOperation:
                tokens.append(("word", raw))
            index += len(raw)
            continue
        end = index + 1
        while end < len(data) and not data[end].isspace() and data[end] not in delimiters:
            end += 1
        tokens.append(("word", data[index:end]))
        index = end
    return tokens


def _pdf_operations(data: str) -> list[tuple[str, list[Any]]]:
    operations: list[tuple[str, list[Any]]] = []
    operands: list[Any] = []
    arrays: list[list[Any]] = []
    for kind, value in _pdf_lex(data):
        if kind == "delimiter" and value == "[":
            arrays.append([])
        elif kind == "delimiter" and value == "]":
            if arrays:
                completed = arrays.pop()
                (arrays[-1] if arrays else operands).append(completed)
        elif kind in {"number", "string", "name"}:
            (arrays[-1] if arrays else operands).append(value)
        elif kind == "word":
            operations.append((str(value), operands))
            operands = []
    return operations


def _numeric_values(values: list[Any]) -> list[Decimal] | None:
    if not all(isinstance(value, Decimal) for value in values):
        return None
    return [value for value in values]


def _point(matrix: list[Decimal], x: Decimal, y: Decimal) -> dict[str, str]:
    return {"x": decimal(matrix[0] * x + matrix[2] * y + matrix[4]), "y": decimal(matrix[1] * x + matrix[3] * y + matrix[5])}


def _interpret_content(data: bytes) -> tuple[list[str], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    operations = _pdf_operations(data.decode("latin-1", errors="replace"))
    texts: list[str] = []
    paths: list[dict[str, Any]] = []
    unsupported: list[str] = []
    text_positions: list[dict[str, Any]] = []
    matrix = [Decimal(1), Decimal(0), Decimal(0), Decimal(1), Decimal(0), Decimal(0)]
    stack: list[list[Decimal]] = []
    current: list[dict[str, Any]] = []
    clip_pending = False
    text_x = Decimal(0)
    text_y = Decimal(0)
    text_size = Decimal(12)
    supported = {"BT", "ET", "Tf", "Td", "TD", "Tm", "Tj", "TJ", "'", '"', "T*", "m", "l", "c", "v", "y", "h", "W", "W*", "n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "q", "Q", "cm", "re", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    def finish_path() -> None:
        nonlocal current, clip_pending
        if current:
            paths.append({"segments": current, "clipping": clip_pending})
            current = []
            clip_pending = False
    for operator, operands in operations:
        if operator not in supported:
            unsupported.append(operator)
            continue
        if operator in {"Tj", "'", '"'} and operands and isinstance(operands[-1], str):
            texts.append(operands[-1])
            text_positions.append({"x": text_x, "y": text_y, "size": text_size})
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            value = "".join(item for item in operands[-1] if isinstance(item, str))
            if value:
                texts.append(value)
                text_positions.append({"x": text_x, "y": text_y, "size": text_size})
        elif operator == "Tf" and len(operands) >= 2 and isinstance(operands[-1], Decimal):
            text_size = operands[-1]
        elif operator in {"Td", "TD"}:
            values = _numeric_values(operands[-2:])
            if values:
                text_x += values[0]
                text_y += values[1]
        elif operator == "Tm":
            values = _numeric_values(operands[-6:])
            if values:
                matrix = values
                text_x, text_y = values[4], values[5]
        elif operator == "cm":
            values = _numeric_values(operands[-6:])
            if values:
                a, b, c, d, e, f = values
                old = matrix
                matrix = [a * old[0] + c * old[1], b * old[0] + d * old[1], a * old[2] + c * old[3], b * old[2] + d * old[3], a * old[4] + c * old[5] + e, b * old[4] + d * old[5] + f]
        elif operator == "q":
            stack.append(list(matrix))
        elif operator == "Q":
            if stack:
                matrix = stack.pop()
        elif operator in {"m", "l", "c", "v", "y", "re", "h"}:
            values = _numeric_values(operands)
            if operator == "m" and values and len(values) >= 2:
                if current:
                    finish_path()
                current.append({"kind": "move", "to": _point(matrix, values[-2], values[-1])})
            elif operator == "l" and values and len(values) >= 2:
                current.append({"kind": "line", "to": _point(matrix, values[-2], values[-1])})
            elif operator == "c" and values and len(values) >= 6:
                current.append({"kind": "bezier", "points": [_point(matrix, values[index], values[index + 1]) for index in (0, 2, 4)]})
            elif operator in {"v", "y"} and values and len(values) >= 4:
                current.append({"kind": "bezier", "points": [_point(matrix, values[index], values[index + 1]) for index in (0, 2) ]})
            elif operator == "re" and values and len(values) >= 4:
                x, y, width, height = values[-4:]
                current.extend([{"kind": "move", "to": _point(matrix, x, y)}, {"kind": "line", "to": _point(matrix, x + width, y)}, {"kind": "line", "to": _point(matrix, x + width, y + height)}, {"kind": "line", "to": _point(matrix, x, y + height)}, {"kind": "close"}])
            elif operator == "h":
                current.append({"kind": "close"})
        elif operator in {"W", "W*"}:
            clip_pending = True
        elif operator in {"n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}:
            finish_path()
    finish_path()
    return texts, paths, unsupported, text_positions


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


def _add_text(builder: DocumentBuilder, page_id: str, value: str, page_number: int, fragment: int, space_id: str, position: dict[str, Any] | None = None) -> str:
    text_id = safe_id("text", f"pdf-source-{page_number}-{fragment}")
    builder.add_text(text_id, value, representation="source", provenance="decoded", status="preserved")
    glyph_id = safe_id("node", f"pdf-glyph-{page_number}-{fragment}")
    geometry_id = safe_id("geometry", f"pdf-glyph-{page_number}-{fragment}")
    approximation_diagnostic = builder.add_diagnostic(
        "DFIR-PDF-GLYPH-BOX-APPROXIMATED",
        "Glyph geometry is a bounded estimate because no PDF text renderer is configured.",
        phase="normalize",
        target_id=glyph_id,
    )
    position = position or {}
    x = decimal(position.get("x", 72))
    y = decimal(position.get("y", 720 - fragment * 18))
    size = decimal(position.get("size", 18))
    width = Decimal(max(1, len(value))) * Decimal(str(size)) * Decimal("0.5")
    builder.add_item("geometries", {"geometryId": geometry_id, "spaceId": space_id, "kind": "glyphBoxes", "primitives": [{"kind": "rectangle", "x": x, "y": y, "width": {"value": decimal(width), "unit": "pt"}, "height": {"value": size, "unit": "pt"}}], "status": "approximated"}, "geometryId")
    builder.add_node("glyph", glyph_id, parent_id=page_id, textIds=[text_id], geometryId=geometry_id, status="approximated")
    builder.add_source_map(glyph_id, {"page": page_number, "object": fragment, "operator": fragment})
    _observation(builder, "renderer", glyph_id, geometry_id=geometry_id)
    _observation(builder, "ocr", glyph_id, geometry_id=geometry_id)
    extension_id = safe_id("extension", f"pdf-glyph-provenance-{page_number}-{fragment}")
    builder.add_item("extensions", {"extensionId": extension_id, "targetId": glyph_id, "namespace": "urn:fdir:format:pdf", "type": "glyph-provenance", "schemaVersion": "1.0.0", "schemaId": "urn:fdir:schema:pdf-glyph-provenance", "payload": {"characterCode": value[:1].encode("latin-1", errors="replace").hex(), "glyphName": "", "unicode": value[:1], "mappingStatus": "unavailable"}, "criticality": "non-critical"}, "extensionId")
    builder.add_feature("text-glyph", "approximated", target_id=glyph_id, diagnostic_ids=[approximation_diagnostic])
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
        objects = _pdf_objects(raw)
        page_objects = [(key, value) for key, value in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", value)]
        page_count = len(page_objects) or len(re.findall(r"/Type\s*/Page\b", content))
        if page_count == 0:
            diagnostic = builder.add_diagnostic("DFIR-PDF-PAGE-MISSING", "PDF contains no page object", severity="error", phase="parse")
            builder.add_feature("pages", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        document_part = safe_id("part", "pdf-document")
        builder.add_item("parts", {"partId": document_part, "kind": "document", "name": "PDF document", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
        streams = _page_content_streams(objects, raw)
        pages_seen = 0
        for page_index in range(page_count):
            pages_seen += 1
            page_id = safe_id("node", f"pdf-page-{pages_seen}")
            surface_id = safe_id("surface", f"pdf-page-{pages_seen}")
            space_id = _coordinate(builder, pages_seen)
            builder.add_item("surfaces", {"surfaceId": surface_id, "partId": document_part, "kind": "page", "ordinal": pages_seen - 1, "coordinateSpaceId": space_id, "status": "preserved"}, "surfaceId")
            builder.add_node("section", page_id, parent_id=builder.root_id, part_id=document_part, status="preserved")
            builder.add_source_map(page_id, {"page": pages_seen, "object": pages_seen})
            page_object = page_objects[pages_seen - 1][1] if pages_seen <= len(page_objects) else content.encode("latin-1", errors="replace")
            page_text = streams[pages_seen - 1] if pages_seen <= len(streams) else page_object
            fragments, parsed_paths, unsupported_operators, text_positions = _interpret_content(page_text)
            if not fragments:
                fragments = _stream_text(page_text.decode("latin-1", errors="replace"))
            if not fragments and page_text != raw:
                fragments = _stream_text(content)
            for fragment, value in enumerate(fragments, start=1):
                _add_text(builder, page_id, value, pages_seen, fragment, space_id, text_positions[fragment - 1] if fragment <= len(text_positions) else None)
            if unsupported_operators:
                diagnostic = builder.add_diagnostic("DFIR-PDF-OPERATOR-UNSUPPORTED", f"PDF operators are not interpreted: {', '.join(sorted(set(unsupported_operators)))}", phase="normalize", target_id=page_id)
                builder.add_feature("unsupported-operator", "unsupported", target_id=page_id, diagnostic_ids=[diagnostic])
            for path_index, path_record in enumerate(parsed_paths):
                path_id = safe_id("node", f"pdf-path-{pages_seen}-{path_index}")
                geometry_id = safe_id("geometry", f"pdf-path-{pages_seen}-{path_index}")
                segments = path_record["segments"]
                is_clip = bool(path_record.get("clipping"))
                has_bezier = any(segment.get("kind") == "bezier" for segment in segments)
                geometry_kind = "clippingPath" if is_clip else "bezier" if has_bezier else "polyline"
                primitive = {"kind": "clip" if is_clip else "bezier", "segments": segments} if is_clip or has_bezier else {"kind": "polyline", "points": [segment["to"] for segment in segments if segment.get("kind") in {"move", "line"}]}
                geometry_status = "normalized"
                builder.add_item("geometries", {"geometryId": geometry_id, "spaceId": space_id, "kind": geometry_kind, "primitives": [primitive], "status": geometry_status}, "geometryId")
                builder.add_node("path", path_id, parent_id=page_id, geometryId=geometry_id, status=geometry_status)
                builder.add_source_map(path_id, {"page": pages_seen, "operator": path_index})
                builder.add_feature("clipping" if geometry_kind == "clippingPath" else "path", geometry_status, target_id=path_id)
            page_has_annots = b"/Annots" in page_object if isinstance(page_object, bytes) else "/Annots" in page_object
            if page_has_annots:
                annotation_id = safe_id("annotation", f"pdf-link-{pages_seen}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [page_id], "body": "PDF annotation destination retained as form fact", "status": "preserved"}, "annotationId")
            renderer_diagnostic = builder.add_diagnostic("DFIR-PDF-RENDERER-UNAVAILABLE", "No renderer worker is configured; renderer result is unavailable and source facts are unchanged.", phase="observe", target_id=page_id)
            ocr_diagnostic = builder.add_diagnostic("DFIR-PDF-OCR-UNAVAILABLE", "No OCR worker is configured; OCR result is unavailable and source text is unchanged.", phase="observe", target_id=page_id)
            builder.add_feature("renderer-observation", "unavailable", target_id=page_id, diagnostic_ids=[renderer_diagnostic])
            builder.add_feature("ocr-observation", "unavailable", target_id=page_id, diagnostic_ids=[ocr_diagnostic])
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-paint"), "kind": "draw", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-reading"), "kind": "reading", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:]) if node["kind"] in {"glyph", "path"}], "status": "ambiguous"}, "orderId")
        if "/Font" in content:
            builder.add_item("resources", {"resourceId": safe_id("resource", "pdf-font"), "kind": "font", "mediaType": "application/x-font", "availability": "unavailable", "derivedHandle": "font-resource"}, "resourceId")
        builder.add_feature("pages", "preserved", target_id=builder.root_id)
        return builder.finish()
    except (OSError, ValueError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-PDF-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("document", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
