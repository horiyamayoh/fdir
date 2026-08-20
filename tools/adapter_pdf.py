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
PDF_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)")


OBJECT_HEADER_RE = re.compile(rb"(?m)(\d+)\s+(\d+)\s+obj\b")
STREAM_MARKER_RE = re.compile(rb">>\s*stream\r?\n")

# These are deliberately local to the PDF adapter.  AdapterLimits predates
# format-specific stream/token budgets, so the existing input/text/object
# budgets are used as the caller-controlled ceiling for these conservative
# PDF work limits.
_PDF_DEFAULT_MAX_STREAM_BYTES = 8 * 1024 * 1024
_PDF_DEFAULT_MAX_CMAP_BYTES = 2 * 1024 * 1024
_PDF_DEFAULT_MAX_CMAP_ENTRIES = 100_000
_PDF_DEFAULT_MAX_CMAP_CODE_CHARS = 256
_PDF_DEFAULT_MAX_TOKENS = 250_000
_PDF_DEFAULT_MAX_LEXEME_CHARS = 1 * 1024 * 1024
_PDF_DEFAULT_MAX_NESTING = 64
_PDF_DEFAULT_MAX_ANNOTATIONS_PER_PAGE = 1_024
_PDF_DEFAULT_MAX_ANNOTATION_TEXT = 4_096


def _pdf_budget(limits: AdapterLimits | None, attribute: str, default: int) -> int:
    value = default if limits is None else int(getattr(limits, attribute, default))
    return max(1, min(default, value))


def _pdf_limit_event(events: list[tuple[str, str]] | None, code: str, message: str) -> None:
    if events is None:
        return
    event = (code, message)
    if event not in events:
        events.append(event)


def _pdf_objects(
    data: bytes,
    *,
    max_objects: int | None = None,
    events: list[tuple[str, str]] | None = None,
) -> dict[tuple[int, int], bytes]:
    """Extract indirect objects without treating stream data as structure.

    A plain ``.*?endobj`` regex is unsafe: literal stream bytes can contain
    the marker and truncate the object graph.  Direct ``/Length`` is honored
    when present; otherwise ``endstream`` is used as the bounded fallback.
    """

    objects: dict[tuple[int, int], bytes] = {}
    cursor = 0
    while cursor < len(data):
        match = OBJECT_HEADER_RE.search(data, cursor)
        if match is None:
            break
        object_start = match.end()
        prefix = data[object_start:]
        stream_marker = STREAM_MARKER_RE.search(prefix)
        first_endobj = data.find(b"endobj", object_start)
        is_stream = stream_marker is not None and (first_endobj < 0 or object_start + stream_marker.start() < first_endobj)
        endobj = -1
        if not is_stream:
            endobj = first_endobj
        else:
            assert stream_marker is not None
            stream_start = object_start + stream_marker.end()
            dictionary = data[object_start:object_start + stream_marker.start()]
            length_match = re.search(rb"/Length\s+(\d+)\b", dictionary)
            if length_match is not None:
                expected_end = stream_start + int(length_match.group(1))
                endstream = data.find(b"endstream", expected_end)
            else:
                endstream = data.find(b"endstream", stream_start)
            search_from = (endstream + len(b"endstream")) if endstream >= 0 else stream_start
            endobj = data.find(b"endobj", search_from)
        end = len(data) if endobj < 0 else endobj
        identifier = (int(match.group(1)), int(match.group(2)))
        if identifier in objects:
            _pdf_limit_event(
                events,
                "DFIR-PDF-DUPLICATE-OBJECT",
                f"PDF contains a duplicate indirect object identifier {identifier[0]} {identifier[1]} R; the first bounded occurrence is retained.",
            )
        elif max_objects is not None and len(objects) >= max_objects:
            _pdf_limit_event(
                events,
                "DFIR-PDF-OBJECT-LIMIT",
                f"PDF object limit exceeded while indexing indirect objects ({max_objects}).",
            )
            break
        else:
            objects[identifier] = data[object_start:end]
        cursor = len(data) if endobj < 0 else endobj + len(b"endobj")
    return objects


def _pdf_references(object_data: bytes) -> list[tuple[int, int]]:
    dictionary = object_data
    stream_marker = STREAM_MARKER_RE.search(dictionary)
    if stream_marker is not None:
        dictionary = dictionary[:stream_marker.start()]
    return list(dict.fromkeys((int(number), int(generation)) for number, generation in re.findall(rb"(\d+)\s+(\d+)\s+R\b", dictionary)))


def _cmap_unicode(raw: str) -> str:
    token = raw.strip().strip("<>")
    try:
        data = bytes.fromhex(token)
    except ValueError:
        return ""
    if not data:
        return ""
    try:
        return data.decode("utf-16-be" if len(data) % 2 == 0 else "latin-1", errors="replace")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _parse_cmap(
    data: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Parse the portable bfchar/bfrange subset of a ToUnicode CMap."""

    max_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_CMAP_BYTES)
    if len(data) > max_bytes:
        _pdf_limit_event(
            events,
            "DFIR-PDF-CMAP-BYTE-LIMIT",
            f"ToUnicode CMap exceeds the bounded parser input limit ({len(data)} > {max_bytes} bytes).",
        )
        return []
    max_entries = _pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_CMAP_ENTRIES)
    text = data.decode("latin-1", errors="replace")
    mappings: dict[str, str] = {}
    max_blocks = min(max_entries, 1_024)
    for block_index, block_match in enumerate(re.finditer(r"beginbfchar(.*?)endbfchar", text, flags=re.S)):
        if block_index >= max_blocks:
            _pdf_limit_event(events, "DFIR-PDF-CMAP-BLOCK-LIMIT", "ToUnicode CMap block count exceeded the bounded parser limit.")
            return [{"sourceCode": source, "unicode": mappings[source]} for source in sorted(mappings)]
        block = block_match.group(1)
        for entry_match in re.finditer(r"(<[0-9A-Fa-f]+>)\s+(<[0-9A-Fa-f]+>)", block):
            if len(mappings) >= max_entries:
                _pdf_limit_event(events, "DFIR-PDF-CMAP-ENTRY-LIMIT", "ToUnicode CMap entry count exceeded the bounded parser limit.")
                return [{"sourceCode": source_code, "unicode": mappings[source_code]} for source_code in sorted(mappings)]
            source, target = entry_match.groups()
            if len(source) > _PDF_DEFAULT_MAX_CMAP_CODE_CHARS or len(target) > _PDF_DEFAULT_MAX_CMAP_CODE_CHARS:
                _pdf_limit_event(events, "DFIR-PDF-CMAP-CODE-LIMIT", "ToUnicode CMap code width exceeded the bounded parser limit.")
                continue
            source_code = source[1:-1].upper()
            value = _cmap_unicode(target)
            if value:
                mappings[source_code] = value
    for block_index, block_match in enumerate(re.finditer(r"beginbfrange(.*?)endbfrange", text, flags=re.S)):
        if block_index >= max_blocks:
            _pdf_limit_event(events, "DFIR-PDF-CMAP-BLOCK-LIMIT", "ToUnicode CMap block count exceeded the bounded parser limit.")
            break
        block = block_match.group(1)
        for range_match in re.finditer(r"(<[0-9A-Fa-f]+>)\s+(<[0-9A-Fa-f]+>)\s+(<[^>]+>)", block):
            start, end, target = range_match.groups()
            if max(len(start), len(end), len(target)) > _PDF_DEFAULT_MAX_CMAP_CODE_CHARS:
                _pdf_limit_event(events, "DFIR-PDF-CMAP-CODE-LIMIT", "ToUnicode CMap code width exceeded the bounded parser limit.")
                continue
            try:
                start_code = int(start[1:-1], 16)
                end_code = int(end[1:-1], 16)
                target_code = int(target[1:-1], 16)
            except ValueError:
                continue
            span = end_code - start_code + 1
            if span < 0 or span > max_entries - len(mappings):
                _pdf_limit_event(events, "DFIR-PDF-CMAP-ENTRY-LIMIT", "ToUnicode CMap range exceeds the bounded parser entry limit.")
                break
            for offset, code in enumerate(range(start_code, end_code + 1)):
                value = _cmap_unicode(f"<{target_code + offset:0{len(target) - 2}X}>")
                if value:
                    mappings[f"{code:0{len(start) - 2}X}"] = value
    return [{"sourceCode": source, "unicode": mappings[source]} for source in sorted(mappings)]


def _pdf_font_mappings(
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    fonts: list[dict[str, Any]] = []
    for font_object, object_data in sorted(objects.items()):
        if not re.search(rb"/Type\s*/Font\b", object_data):
            continue
        has_to_unicode = re.search(rb"/ToUnicode\s+(\d+)\s+(\d+)\s+R\b", object_data)
        mapping: list[dict[str, str]] = []
        status = "unavailable"
        cmap_object: tuple[int, int] | None = None
        if has_to_unicode:
            cmap_object = (int(has_to_unicode.group(1)), int(has_to_unicode.group(2)))
            cmap_data = objects.get(cmap_object)
            if cmap_data is not None:
                mapping = _parse_cmap(_decode_stream(cmap_data, limits=limits, events=events), limits=limits, events=events)
                status = "preserved" if mapping else "unavailable"
        fonts.append({"object": font_object, "toUnicodeObject": cmap_object, "mappingStatus": status, "mapping": mapping})
    return fonts


def _pdf_font_resource_map(objects: dict[tuple[int, int], bytes], fonts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve resource names such as ``/F1`` to parsed font mappings."""

    by_object = {f"{font['object'][0]} {font['object'][1]}": font for font in fonts}
    result: dict[str, dict[str, Any]] = {}
    for object_data in objects.values():
        for match in re.finditer(rb"/Font\s*<<(?P<body>.*?)>>", object_data, flags=re.S):
            for name, number, generation in re.findall(rb"/([A-Za-z0-9_.-]+)\s+(\d+)\s+(\d+)\s+R\b", match.group("body")):
                font = by_object.get(f"{int(number)} {int(generation)}")
                if font is not None:
                    result[name.decode("latin-1")] = font
    return result


def _decode_stream(
    object_data: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> bytes:
    marker = STREAM_MARKER_RE.search(object_data)
    if marker is None:
        return b""
    end = object_data.find(b"endstream", marker.end())
    if end < 0:
        return b""
    value = object_data[marker.end():end]
    max_stream_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    if len(value) > max_stream_bytes:
        _pdf_limit_event(
            events,
            "DFIR-PDF-STREAM-BYTE-LIMIT",
            f"PDF stream exceeds the bounded decoder input limit ({len(value)} > {max_stream_bytes} bytes).",
        )
        return b""
    max_decoded_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    if b"/FlateDecode" in object_data:
        try:
            decompressor = zlib.decompressobj()
            output = bytearray()
            pending = value
            while pending:
                remaining = max_decoded_bytes - len(output)
                if remaining <= 0:
                    _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "FlateDecode output exceeded the bounded decoder limit.")
                    return b""
                output.extend(decompressor.decompress(pending, remaining + 1))
                if len(output) > max_decoded_bytes or decompressor.unconsumed_tail:
                    _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "FlateDecode output exceeded the bounded decoder limit.")
                    return b""
                pending = decompressor.unconsumed_tail
            remaining = max_decoded_bytes - len(output)
            output.extend(decompressor.flush(remaining + 1))
            if len(output) > max_decoded_bytes:
                _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "FlateDecode output exceeded the bounded decoder limit.")
                return b""
            if not decompressor.eof:
                _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODE-FAILED", "FlateDecode stream ended before a complete zlib member was decoded.")
                return b""
            return bytes(output)
        except (zlib.error, ValueError):
            _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODE-FAILED", "FlateDecode stream could not be decoded safely.")
            return b""
    if b"/ASCIIHexDecode" in object_data:
        try:
            compact = re.sub(rb"\s+", b"", value.replace(b">", b""))
            if len(compact) > max_decoded_bytes * 2 + 1:
                _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "ASCIIHexDecode output exceeded the bounded decoder limit.")
                return b""
            decoded = bytes.fromhex(compact.decode("ascii"))
            if len(decoded) > max_decoded_bytes:
                _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "ASCIIHexDecode output exceeded the bounded decoder limit.")
                return b""
            return decoded
        except (ValueError, UnicodeDecodeError):
            _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODE-FAILED", "ASCIIHexDecode stream could not be decoded safely.")
            return b""
    if len(value) > max_decoded_bytes:
        _pdf_limit_event(events, "DFIR-PDF-STREAM-DECODED-LIMIT", "Unfiltered PDF stream exceeded the bounded decoder limit.")
        return b""
    if b"/Filter" in object_data:
        _pdf_limit_event(events, "DFIR-PDF-STREAM-FILTER-UNSUPPORTED", "PDF stream uses a filter outside the bounded adapter subset.")
        return b""
    return value


def _page_content_streams(
    objects: dict[tuple[int, int], bytes],
    fallback: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    pages = [(key, value) for key, value in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", value)]
    streams: list[bytes] = []
    for _, page in pages:
        tokens = _pdf_dictionary_tokens(page, limits=limits, events=events)
        contents = _pdf_find_value(tokens, "Contents")
        references: list[tuple[int, int]] = []
        if contents is not None and contents[0] == "ref":
            references = [contents[1]]
        elif contents is not None and contents[0] == "array":
            references = _pdf_reference_values(
                contents,
                max_count=_pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_ANNOTATIONS_PER_PAGE),
                events=events,
            )
        elif contents is not None:
            _pdf_limit_event(events, "DFIR-PDF-CONTENTS-UNAVAILABLE", "PDF page /Contents is outside the bounded indirect-stream subset.")
        page_streams = [_decode_stream(objects[ref], limits=limits, events=events) for ref in references if ref in objects and b"stream" in objects[ref]]
        if page_streams:
            streams.append(b"\n".join(page_streams))
        else:
            _pdf_limit_event(events, "DFIR-PDF-CONTENTS-UNAVAILABLE", "PDF page has no bounded /Contents stream; page metadata is not treated as source text.")
            streams.append(b"")
    return streams


def _pdf_lex(
    data: str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    index = 0
    delimiters = "()<>[]{}/%"
    max_tokens = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_TOKENS)
    max_lexeme_chars = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_LEXEME_CHARS)
    max_data_chars = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_LEXEME_CHARS * 8)
    if len(data) > max_data_chars:
        _pdf_limit_event(events, "DFIR-PDF-TOKEN-BYTE-LIMIT", "PDF token input exceeded the bounded lexer input limit.")
        data = data[:max_data_chars]

    def append_token(kind: str, value: Any) -> bool:
        if len(tokens) >= max_tokens:
            _pdf_limit_event(events, "DFIR-PDF-TOKEN-LIMIT", "PDF token count exceeded the bounded lexer limit.")
            return False
        tokens.append((kind, value))
        return True

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
            if not append_token("delimiter", data[index:index + 2]):
                return tokens
            index += 2
            continue
        if character in "[]":
            if not append_token("delimiter", character):
                return tokens
            index += 1
            continue
        if character == "(":
            index += 1
            depth = 1
            value: list[str] = []
            while index < len(data) and depth:
                current = data[index]
                if current == "\\" and index + 1 < len(data):
                    if len(value) + 2 > max_lexeme_chars:
                        _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF literal string exceeded the bounded lexer token limit.")
                        return tokens
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
                if len(value) > max_lexeme_chars:
                    _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF literal string exceeded the bounded lexer token limit.")
                    return tokens
            if not append_token("string", _decode_literal("".join(value))):
                return tokens
            continue
        if character == "<" and not data.startswith("<<", index):
            end = data.find(">", index + 1)
            if end < 0 and len(data) - index > max_lexeme_chars:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF hex string exceeded the bounded lexer token limit.")
                return tokens
            if end >= 0 and end - index - 1 > max_lexeme_chars:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF hex string exceeded the bounded lexer token limit.")
                return tokens
            raw = data[index + 1: len(data) if end < 0 else end]
            raw = re.sub(r"\s+", "", raw)
            try:
                decoded = bytes.fromhex(raw + ("0" if len(raw) % 2 else "")).decode("utf-16-be" if raw.lower().startswith("feff") else "latin-1", errors="replace")
            except ValueError:
                decoded = raw
            if not append_token("string", decoded):
                return tokens
            index = len(data) if end < 0 else end + 1
            continue
        if character == "/":
            end = index + 1
            while end < len(data) and not data[end].isspace() and data[end] not in delimiters:
                end += 1
                if end - index > max_lexeme_chars:
                    _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF name exceeded the bounded lexer token limit.")
                    return tokens
            if not append_token("name", data[index:end]):
                return tokens
            index = end
            continue
        number = PDF_NUMBER_RE.match(data, index)
        if number:
            raw = number.group(0)
            try:
                if not append_token("number", Decimal(raw)):
                    return tokens
            except InvalidOperation:
                if not append_token("word", raw):
                    return tokens
            index += len(raw)
            continue
        end = index + 1
        while end < len(data) and not data[end].isspace() and data[end] not in delimiters:
            end += 1
            if end - index > max_lexeme_chars:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF word exceeded the bounded lexer token limit.")
                return tokens
        if not append_token("word", data[index:end]):
            return tokens
        index = end
    return tokens


def _pdf_operations(
    data: str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, list[Any]]]:
    operations: list[tuple[str, list[Any]]] = []
    operands: list[Any] = []
    arrays: list[list[Any]] = []
    max_array_depth = 64
    for kind, value in _pdf_lex(data, limits=limits, events=events):
        if kind == "delimiter" and value == "[":
            if len(arrays) >= max_array_depth:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-NESTING-LIMIT", "PDF array nesting exceeded the bounded operator parser limit.")
                return operations
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


def _text_character_codes(value: str) -> list[str]:
    """Return the one-byte character-code spelling available to this lexer.

    The bounded parser does not claim to decode an arbitrary PDF composite
    font.  It does, however, retain the byte spelling for the simple literal
    and hex-string lane so a registered ToUnicode map can be applied without
    replacing the authored source text.
    """

    return [f"{byte:02X}" for byte in value.encode("latin-1", errors="replace")]


def _json_operands(values: list[Any]) -> list[Any]:
    if isinstance(values, list):
        return [_json_operands(value) if isinstance(value, list) else decimal(value) if isinstance(value, Decimal) else str(value) for value in values]
    return values


def _graphics_state_snapshot(state: dict[str, Any], matrix: list[Decimal]) -> dict[str, Any]:
    return {
        "ctm": [decimal(value) for value in matrix],
        "lineWidth": state["lineWidth"],
        "lineCap": state["lineCap"],
        "lineJoin": state["lineJoin"],
        "miterLimit": state["miterLimit"],
        "dashPattern": state["dashPattern"],
        "renderingIntent": state["renderingIntent"],
        "extGState": state["extGState"],
        "shading": state["shading"],
        "fillColor": state["fillColor"],
        "strokeColor": state["strokeColor"],
    }


def _interpret_content(
    data: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    operations = _pdf_operations(data.decode("latin-1", errors="replace"), limits=limits, events=events)
    texts: list[str] = []
    paths: list[dict[str, Any]] = []
    unsupported: list[str] = []
    text_positions: list[dict[str, Any]] = []
    graphics_states: list[dict[str, Any]] = []
    matrix = [Decimal(1), Decimal(0), Decimal(0), Decimal(1), Decimal(0), Decimal(0)]
    stack: list[list[Decimal]] = []
    state_stack: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "lineWidth": "1",
        "lineCap": 0,
        "lineJoin": 0,
        "miterLimit": "10",
        "dashPattern": {"array": [], "phase": "0"},
        "renderingIntent": None,
        "extGState": None,
        "shading": None,
        "fillColor": ["0"],
        "strokeColor": ["0"],
    }
    current: list[dict[str, Any]] = []
    clip_pending = False
    text_x = Decimal(0)
    text_y = Decimal(0)
    text_size = Decimal(12)
    current_font: str | None = None
    supported = {"BT", "ET", "Tf", "Td", "TD", "Tm", "Tj", "TJ", "'", '"', "T*", "m", "l", "c", "v", "y", "h", "W", "W*", "n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "q", "Q", "cm", "re", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    def finish_path() -> None:
        nonlocal current, clip_pending
        if current:
            paths.append({"segments": current, "clipping": clip_pending})
            current = []
            clip_pending = False
    graphics_operators = {"q", "Q", "cm", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    for operator, operands in operations:
        if operator not in supported:
            unsupported.append(operator)
            continue
        if operator in graphics_operators:
            if operator == "q":
                stack.append(list(matrix))
                state_stack.append(dict(state))
            elif operator == "Q":
                if stack:
                    matrix = stack.pop()
                if state_stack:
                    state = state_stack.pop()
            elif operator == "cm":
                values = _numeric_values(operands[-6:])
                if values:
                    a, b, c, d, e, f = values
                    old = matrix
                    matrix = [a * old[0] + c * old[1], b * old[0] + d * old[1], a * old[2] + c * old[3], b * old[2] + d * old[3], a * old[4] + c * old[5] + e, b * old[4] + d * old[5] + f]
            elif operator in {"rg", "RG"} and _numeric_values(operands[-3:]):
                state["fillColor" if operator == "rg" else "strokeColor"] = [decimal(value) for value in _numeric_values(operands[-3:]) or []]
            elif operator in {"g", "G"} and _numeric_values(operands[-1:]):
                state["fillColor" if operator == "g" else "strokeColor"] = [decimal(value) for value in _numeric_values(operands[-1:]) or []]
            elif operator in {"k", "K"} and _numeric_values(operands[-4:]):
                state["fillColor" if operator == "k" else "strokeColor"] = [decimal(value) for value in _numeric_values(operands[-4:]) or []]
            elif operator == "w" and _numeric_values(operands[-1:]):
                state["lineWidth"] = decimal((_numeric_values(operands[-1:]) or [Decimal(1)])[-1])
            elif operator == "J" and operands and isinstance(operands[-1], Decimal):
                state["lineCap"] = int(operands[-1])
            elif operator == "j" and operands and isinstance(operands[-1], Decimal):
                state["lineJoin"] = int(operands[-1])
            elif operator == "M" and _numeric_values(operands[-1:]):
                state["miterLimit"] = decimal((_numeric_values(operands[-1:]) or [Decimal(10)])[-1])
            elif operator == "d" and len(operands) >= 2:
                state["dashPattern"] = {"array": _json_operands([operands[-2]])[0], "phase": _json_operands([operands[-1]])[0]}
            elif operator == "ri" and operands and isinstance(operands[-1], str):
                state["renderingIntent"] = operands[-1]
            elif operator == "gs" and operands and isinstance(operands[-1], str):
                state["extGState"] = operands[-1]
            elif operator == "sh" and operands and isinstance(operands[-1], str):
                state["shading"] = operands[-1]
            graphics_states.append({"operator": operator, "operands": _json_operands(operands), "state": _graphics_state_snapshot(state, matrix)})
        if operator in {"Tj", "'", '"'} and operands and isinstance(operands[-1], str):
            texts.append(operands[-1])
            text_positions.append({"x": text_x, "y": text_y, "size": text_size, "font": current_font, "characterCodes": _text_character_codes(operands[-1])})
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            value = "".join(item for item in operands[-1] if isinstance(item, str))
            if value:
                texts.append(value)
                text_positions.append({"x": text_x, "y": text_y, "size": text_size, "font": current_font, "characterCodes": _text_character_codes(value)})
        elif operator == "Tf" and len(operands) >= 2 and isinstance(operands[-1], Decimal):
            text_size = operands[-1]
            current_font = operands[-2].lstrip("/") if isinstance(operands[-2], str) else None
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
    return texts, paths, unsupported, text_positions, graphics_states


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


def _pdf_dictionary_bytes(object_data: bytes) -> bytes:
    marker = STREAM_MARKER_RE.search(object_data)
    return object_data if marker is None else object_data[:marker.start()]


def _pdf_matching_delimiter(tokens: list[tuple[str, Any]], start: int) -> int:
    if start >= len(tokens) or tokens[start][0] != "delimiter" or tokens[start][1] not in {"[", "<<"}:
        return min(start + 1, len(tokens))
    closing = {"[": "]", "<<": ">>"}
    stack = [closing[tokens[start][1]]]
    for index in range(start + 1, len(tokens)):
        kind, value = tokens[index]
        if kind != "delimiter":
            continue
        if value in {"[", "<<"}:
            if len(stack) >= _PDF_DEFAULT_MAX_NESTING:
                return len(tokens)
            stack.append(closing[value])
        elif stack and value == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
    return len(tokens)


def _pdf_parse_value(tokens: list[tuple[str, Any]], start: int) -> tuple[tuple[str, Any], int]:
    if start >= len(tokens):
        return ("missing", None), start
    kind, value = tokens[start]
    if kind == "delimiter" and value in {"[", "<<"}:
        end = _pdf_matching_delimiter(tokens, start)
        return ("array" if value == "[" else "dict", tokens[start + 1:max(start + 1, end - 1)]), end
    if kind == "number" and start + 2 < len(tokens):
        generation_kind, generation = tokens[start + 1]
        marker_kind, marker = tokens[start + 2]
        if generation_kind == "number" and marker_kind == "word" and marker == "R" and value == int(value) and generation == int(generation):
            return ("ref", (int(value), int(generation))), start + 3
    return (kind, value), start + 1


def _pdf_value_end(tokens: list[tuple[str, Any]], start: int) -> int:
    return _pdf_parse_value(tokens, start)[1]


def _pdf_find_value(tokens: list[tuple[str, Any]], key: str) -> tuple[str, Any] | None:
    index = 1 if tokens and tokens[0] == ("delimiter", "<<") else 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "delimiter" and value == ">>":
            break
        if kind == "name":
            parsed, end = _pdf_parse_value(tokens, index + 1)
            if value == f"/{key}":
                return parsed
            index = end
        else:
            index += 1
    return None


def _pdf_value_name(value: tuple[str, Any] | None) -> str | None:
    if value is None or value[0] != "name" or not isinstance(value[1], str):
        return None
    return value[1].lstrip("/") or None


def _pdf_tokens_text(tokens: list[tuple[str, Any]], *, limit: int = _PDF_DEFAULT_MAX_ANNOTATION_TEXT) -> str:
    values: list[str] = []
    length = 0
    for kind, value in tokens:
        if kind == "delimiter":
            item = str(value)
        elif kind == "string":
            item = str(value)
        elif kind == "name":
            item = str(value)
        elif kind == "number":
            item = str(value)
        else:
            item = str(value)
        additional = len(item) if not values else len(item) + 1
        if length + additional > limit:
            break
        values.append(item)
        length += additional
    text = " ".join(values)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _pdf_value_text(value: tuple[str, Any] | None, *, limit: int = _PDF_DEFAULT_MAX_ANNOTATION_TEXT) -> str | None:
    if value is None or value[0] == "missing":
        return None
    kind, raw = value
    if kind in {"string", "word", "number"}:
        text = str(raw)
    elif kind == "name":
        text = str(raw).lstrip("/")
    elif kind == "ref":
        text = f"{raw[0]} {raw[1]} R"
    elif kind == "array":
        text = _pdf_tokens_text(raw, limit=limit)
    elif kind == "dict":
        text = "dictionary"
    else:
        return None
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _pdf_dictionary_tokens(
    object_data: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, Any]]:
    dictionary = _pdf_dictionary_bytes(object_data)
    return _pdf_lex(dictionary.decode("latin-1", errors="replace"), limits=limits, events=events)


def _pdf_resolve_value(
    value: tuple[str, Any] | None,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[str, Any] | None:
    if value is None or value[0] != "ref":
        return value
    object_data = objects.get(value[1])
    if object_data is None:
        return ("missing", value[1])
    tokens = _pdf_dictionary_tokens(object_data, limits=limits, events=events)
    if not tokens:
        return ("missing", value[1])
    return _pdf_parse_value(tokens, 0)[0]


def _pdf_reference_values(
    value: tuple[str, Any] | None,
    *,
    max_count: int,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[int, int]]:
    if value is None or value[0] != "array":
        return []
    references: list[tuple[int, int]] = []
    tokens = value[1]
    index = 0
    while index < len(tokens):
        parsed, end = _pdf_parse_value(tokens, index)
        if parsed[0] == "ref":
            reference = parsed[1]
            if reference not in references:
                references.append(reference)
            if len(references) >= max_count:
                _pdf_limit_event(events, "DFIR-PDF-ANNOTATION-REFERENCE-LIMIT", "PDF annotation reference count reached the bounded page limit; additional references are not parsed.")
                return references[:max_count]
            index = end
        else:
            index += 1
    return references


def _pdf_annotation_references(
    page_object: bytes,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[list[tuple[int, int]], bool]:
    tokens = _pdf_dictionary_tokens(page_object, limits=limits, events=events)
    annots = _pdf_find_value(tokens, "Annots")
    if annots is None:
        return [], False
    max_count = _pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_ANNOTATIONS_PER_PAGE)
    if annots[0] == "ref":
        array_object = objects.get(annots[1])
        if array_object is None:
            _pdf_limit_event(events, "DFIR-PDF-ANNOTATION-REFERENCE-UNAVAILABLE", "PDF /Annots references an unavailable annotation array object.")
            return [], True
        array_tokens = _pdf_dictionary_tokens(array_object, limits=limits, events=events)
        if array_tokens:
            annots = _pdf_parse_value(array_tokens, 0)[0]
    references = _pdf_reference_values(annots, max_count=max_count, events=events)
    if annots[0] != "array":
        _pdf_limit_event(events, "DFIR-PDF-ANNOTATION-REFERENCE-UNAVAILABLE", "PDF /Annots did not resolve to a bounded array of indirect annotation references.")
    return references, True


def _pdf_destination_text(
    value: tuple[str, Any] | None,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> str | None:
    resolved = _pdf_resolve_value(value, objects, limits=limits, events=events)
    if resolved is None or resolved[0] not in {"string", "name", "array"}:
        return None
    return _pdf_value_text(resolved)


def _pdf_scalar_text(
    value: tuple[str, Any] | None,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> str | None:
    resolved = _pdf_resolve_value(value, objects, limits=limits, events=events)
    if resolved is None or resolved[0] not in {"string", "name"}:
        return None
    return _pdf_value_text(resolved)


def _add_pdf_annotations(
    builder: DocumentBuilder,
    page_id: str,
    page_number: int,
    page_object: bytes,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> None:
    references, has_annots = _pdf_annotation_references(page_object, objects, limits=limits, events=events)
    if not has_annots:
        return
    if not references:
        return
    for object_number, generation in references:
        reference_id = f"{object_number} {generation} R"
        annotation_object = objects.get((object_number, generation))
        if annotation_object is None:
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-OBJECT-UNAVAILABLE",
                f"PDF annotation reference {reference_id} points to an unavailable object.",
                phase="parse",
                target_id=page_id,
            )
            builder.add_feature("annotation", "unavailable", target_id=page_id, diagnostic_ids=[diagnostic])
            continue
        tokens = _pdf_dictionary_tokens(annotation_object, limits=limits, events=events)
        subtype = _pdf_value_name(_pdf_find_value(tokens, "Subtype"))
        if subtype is None:
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-SUBTYPE-UNAVAILABLE",
                f"PDF annotation {reference_id} has no bounded /Subtype value.",
                phase="parse",
                target_id=page_id,
            )
            builder.add_feature("annotation", "unavailable", target_id=page_id, diagnostic_ids=[diagnostic])
            continue

        if subtype == "Link":
            kind = "hyperlink"
        elif subtype in {"Text", "FreeText", "Popup"}:
            kind = "comment"
        elif subtype == "Widget":
            kind = "form"
        else:
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-SUBTYPE-UNSUPPORTED",
                f"PDF annotation {reference_id} uses unsupported subtype /{subtype}; it is not relabeled as another annotation kind.",
                phase="normalize",
                target_id=page_id,
            )
            builder.add_feature("annotation", "unsupported", target_id=page_id, diagnostic_ids=[diagnostic])
            continue

        status = "preserved"
        body: str | None = None
        diagnostic_ids: list[str] = []

        if subtype == "Link":
            action = _pdf_find_value(tokens, "A")
            destination = _pdf_find_value(tokens, "Dest")
            if action is not None:
                action_value = _pdf_resolve_value(action, objects, limits=limits, events=events)
                if action_value is None or action_value[0] == "missing":
                    status = "unavailable"
                    diagnostic_ids.append(builder.add_diagnostic(
                        "DFIR-PDF-ANNOTATION-ACTION-UNAVAILABLE",
                        f"PDF link annotation {reference_id} references an unavailable action object.",
                        phase="parse",
                        target_id=page_id,
                    ))
                elif action_value[0] != "dict":
                    status = "unsupported"
                    diagnostic_ids.append(builder.add_diagnostic(
                        "DFIR-PDF-ANNOTATION-ACTION-UNSUPPORTED",
                        f"PDF link annotation {reference_id} has an action value outside the bounded dictionary subset.",
                        phase="normalize",
                        target_id=page_id,
                    ))
                else:
                    action_name = _pdf_value_name(_pdf_find_value(action_value[1], "S"))
                    if action_name == "URI":
                        body_value = _pdf_scalar_text(_pdf_find_value(action_value[1], "URI"), objects, limits=limits, events=events)
                        if body_value:
                            body = f"URI: {body_value}"
                        else:
                            status = "unavailable"
                            diagnostic_ids.append(builder.add_diagnostic(
                                "DFIR-PDF-ANNOTATION-DESTINATION-UNAVAILABLE",
                                f"PDF URI action {reference_id} has no bounded URI value.",
                                phase="parse",
                                target_id=page_id,
                            ))
                    elif action_name == "GoTo":
                        body_value = _pdf_destination_text(_pdf_find_value(action_value[1], "D"), objects, limits=limits, events=events)
                        if body_value:
                            body = f"Destination: {body_value}"
                        else:
                            status = "unavailable"
                            diagnostic_ids.append(builder.add_diagnostic(
                                "DFIR-PDF-ANNOTATION-DESTINATION-UNAVAILABLE",
                                f"PDF GoTo action {reference_id} has no bounded destination value.",
                                phase="parse",
                                target_id=page_id,
                            ))
                    elif action_name == "GoToR":
                        destination_text = _pdf_destination_text(_pdf_find_value(action_value[1], "D"), objects, limits=limits, events=events)
                        file_text = _pdf_destination_text(_pdf_find_value(action_value[1], "F"), objects, limits=limits, events=events)
                        if destination_text or file_text:
                            pieces = []
                            if file_text:
                                pieces.append(f"file={file_text}")
                            if destination_text:
                                pieces.append(f"destination={destination_text}")
                            body = "GoToR: " + "; ".join(pieces)
                            status = "unsupported"
                            diagnostic_ids.append(builder.add_diagnostic(
                                "DFIR-PDF-ANNOTATION-ACTION-UNSUPPORTED",
                                f"PDF GoToR action {reference_id} is parsed but external navigation is outside the bounded adapter slice.",
                                phase="normalize",
                                target_id=page_id,
                            ))
                        else:
                            status = "unavailable"
                            diagnostic_ids.append(builder.add_diagnostic(
                                "DFIR-PDF-ANNOTATION-DESTINATION-UNAVAILABLE",
                                f"PDF GoToR action {reference_id} has no bounded file or destination value.",
                                phase="parse",
                                target_id=page_id,
                            ))
                    elif action_name:
                        status = "unsupported"
                        body = f"Action: /{action_name}"
                        diagnostic_ids.append(builder.add_diagnostic(
                            "DFIR-PDF-ANNOTATION-ACTION-UNSUPPORTED",
                            f"PDF link annotation {reference_id} uses unsupported action /{action_name}.",
                            phase="normalize",
                            target_id=page_id,
                        ))
                    else:
                        status = "unavailable"
                        diagnostic_ids.append(builder.add_diagnostic(
                            "DFIR-PDF-ANNOTATION-ACTION-UNAVAILABLE",
                            f"PDF link annotation {reference_id} has no bounded action subtype.",
                            phase="parse",
                            target_id=page_id,
                        ))
            elif destination is not None:
                destination_text = _pdf_destination_text(destination, objects, limits=limits, events=events)
                if destination_text:
                    body = f"Destination: {destination_text}"
                else:
                    status = "unavailable"
                    diagnostic_ids.append(builder.add_diagnostic(
                        "DFIR-PDF-ANNOTATION-DESTINATION-UNAVAILABLE",
                        f"PDF link annotation {reference_id} has no bounded destination value.",
                        phase="parse",
                        target_id=page_id,
                    ))
            else:
                status = "unavailable"
                diagnostic_ids.append(builder.add_diagnostic(
                    "DFIR-PDF-ANNOTATION-ACTION-UNAVAILABLE",
                    f"PDF link annotation {reference_id} has neither /A nor /Dest.",
                    phase="parse",
                    target_id=page_id,
                ))
        elif subtype in {"Text", "FreeText", "Popup"}:
            contents = _pdf_find_value(tokens, "Contents")
            if subtype == "Popup" and contents is None:
                parent_value = _pdf_resolve_value(_pdf_find_value(tokens, "Parent"), objects, limits=limits, events=events)
                if parent_value is not None and parent_value[0] == "dict":
                    contents = _pdf_find_value(parent_value[1], "Contents")
            body = _pdf_scalar_text(contents, objects, limits=limits, events=events)
            if not body:
                status = "unavailable"
                diagnostic_ids.append(builder.add_diagnostic(
                    "DFIR-PDF-ANNOTATION-CONTENTS-UNAVAILABLE",
                    f"PDF {subtype} annotation {reference_id} has no bounded contents value.",
                    phase="parse",
                    target_id=page_id,
                ))
        elif subtype == "Widget":
            field_name = _pdf_scalar_text(_pdf_find_value(tokens, "T"), objects, limits=limits, events=events)
            body = f"Field: {field_name}" if field_name else None
            status = "unsupported"
            diagnostic_ids.append(builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-WIDGET-UNSUPPORTED",
                f"PDF widget annotation {reference_id} is identified but form behavior and appearance are outside the bounded adapter slice.",
                phase="normalize",
                target_id=page_id,
            ))

        item: dict[str, Any] = {
            "annotationId": safe_id("annotation", f"pdf-annotation-{page_number}-{object_number}-{generation}"),
            "kind": kind,
            "targetIds": [page_id],
            "referenceId": reference_id,
            "status": status,
        }
        if body:
            item["body"] = body[:_PDF_DEFAULT_MAX_ANNOTATION_TEXT]
        builder.add_item("annotations", item, "annotationId")
        builder.add_feature("annotation", status, target_id=page_id, diagnostic_ids=diagnostic_ids)


def _emit_pdf_limit_events(
    builder: DocumentBuilder,
    events: list[tuple[str, str]],
    *,
    target_id: str,
) -> None:
    for code, message in events:
        diagnostic = builder.add_diagnostic(code, message, phase="parse", target_id=target_id)
        builder.add_feature("bounded-pdf-work", "unsupported", target_id=target_id, diagnostic_ids=[diagnostic])


def _add_text(
    builder: DocumentBuilder,
    page_id: str,
    value: str,
    page_number: int,
    fragment: int,
    space_id: str,
    position: dict[str, Any] | None = None,
    font_by_resource: dict[str, dict[str, Any]] | None = None,
) -> str:
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
    font_by_resource = font_by_resource or {}
    font = font_by_resource.get(str(position.get("font", "")))
    character_codes = [str(code).upper() for code in position.get("characterCodes", []) if isinstance(code, str)]
    mapping = {item.get("sourceCode", "").upper(): item.get("unicode", "") for item in (font or {}).get("mapping", []) if isinstance(item, dict)}
    mapped_values = [mapping.get(code, "") for code in character_codes]
    mapping_status = "preserved" if character_codes and mapping and all(mapped_values) else "unavailable"
    unicode_value = "".join(mapped_values) if mapping_status == "preserved" else value[:1]
    character_code = character_codes[0] if character_codes else value[:1].encode("latin-1", errors="replace").hex().upper()
    extension_id = safe_id("extension", f"pdf-glyph-provenance-{page_number}-{fragment}")
    builder.add_item("extensions", {"extensionId": extension_id, "targetId": glyph_id, "namespace": "urn:fdir:format:pdf", "type": "glyph-provenance", "schemaVersion": "1.0.0", "schemaId": "urn:fdir:schema:pdf-glyph-provenance", "payload": {"characterCode": character_code, "glyphName": "", "unicode": unicode_value, "mappingStatus": mapping_status}, "criticality": "non-critical"}, "extensionId")
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
        limit_events: list[tuple[str, str]] = []
        objects = _pdf_objects(raw, max_objects=limits.max_pdf_objects, events=limit_events)
        page_objects = [(key, value) for key, value in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", value)]
        # A textual /Type /Page marker outside an extracted indirect object is
        # not enough evidence of a page; it may occur in a stream or comment.
        # Only materialized page objects may create page/surface entities.
        page_count = len(page_objects)
        if page_count == 0:
            _emit_pdf_limit_events(builder, limit_events, target_id=builder.root_id)
            diagnostic = builder.add_diagnostic("DFIR-PDF-PAGE-MISSING", "PDF contains no page object", severity="error", phase="parse")
            builder.add_feature("pages", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        document_part = safe_id("part", "pdf-document")
        builder.add_item("parts", {"partId": document_part, "kind": "document", "name": "PDF document", "rootNodeIds": [builder.root_id], "relationshipIds": [], "status": "preserved"}, "partId")
        object_part_ids: dict[tuple[int, int], str] = {}
        for object_number, generation in sorted(objects):
            object_part_id = safe_id("part", f"pdf-object-{object_number}-{generation}")
            object_part_ids[(object_number, generation)] = object_part_id
            builder.add_item(
                "parts",
                {
                    "partId": object_part_id,
                    "kind": "object",
                    "name": f"{object_number} {generation} obj",
                    "contentType": "application/pdf-object",
                    "parentPartId": document_part,
                    "rootNodeIds": [],
                    "relationshipIds": [],
                    "status": "preserved",
                },
                "partId",
            )
        unresolved_objects: list[tuple[int, int]] = []
        for source_object, object_data in sorted(objects.items()):
            source_part_id = object_part_ids[source_object]
            source_part = builder.find("parts", "partId", source_part_id)
            for target_object in _pdf_references(object_data):
                target_id = object_part_ids.get(target_object)
                relation_status = "preserved"
                if target_id is None:
                    unresolved_objects.append(target_object)
                    target_id = safe_id("resource", f"pdf-missing-object-{target_object[0]}-{target_object[1]}")
                    if builder.find("resources", "resourceId", target_id) is None:
                        builder.add_item(
                            "resources",
                            {
                                "resourceId": target_id,
                                "kind": "embeddedObject",
                                "mediaType": "application/pdf-object",
                                "availability": "unavailable",
                                "derivedHandle": f"{target_object[0]} {target_object[1]} R",
                            },
                            "resourceId",
                        )
                    relation_status = "unavailable"
                relation_id = safe_id("relation", f"pdf-object-{source_object[0]}-{source_object[1]}-{target_object[0]}-{target_object[1]}")
                builder.add_item("relations", {"relationId": relation_id, "kind": "references", "fromId": source_part_id, "toId": target_id, "status": relation_status}, "relationId")
                if source_part is not None:
                    source_part.setdefault("relationshipIds", []).append(relation_id)
        if unresolved_objects:
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-OBJECT-REFERENCE-MISSING",
                "One or more indirect PDF references target an unavailable object.",
                phase="parse",
                target_id=document_part,
            )
            builder.add_feature("pdf-object-graph", "ambiguous", target_id=document_part, diagnostic_ids=[diagnostic])
        else:
            builder.add_feature("pdf-object-graph", "preserved", target_id=document_part)
        font_mappings = _pdf_font_mappings(objects, limits=limits, events=limit_events)
        font_by_resource = _pdf_font_resource_map(objects, font_mappings)
        for font in font_mappings:
            object_number, generation = font["object"]
            font_object_name = f"{object_number} {generation}"
            resource_id = safe_id("resource", f"pdf-font-{object_number}-{generation}")
            builder.add_item("resources", {"resourceId": resource_id, "kind": "font", "mediaType": "application/x-font", "availability": "available", "derivedHandle": f"object:{font_object_name}"}, "resourceId")
            extension_id = safe_id("extension", f"pdf-font-cmap-{object_number}-{generation}")
            builder.add_item(
                "extensions",
                {
                    "extensionId": extension_id,
                    "targetId": builder.root_id,
                    "namespace": "urn:fdir:format:pdf",
                    "type": "font-cmap",
                    "schemaVersion": "1.0.0",
                    "schemaId": "urn:fdir:schema:pdf-font-cmap",
                    "payload": {"fontObject": font_object_name, "mappingStatus": font["mappingStatus"], "mappings": font["mapping"]},
                    "criticality": "non-critical",
                },
                "extensionId",
            )
            if font["mappingStatus"] == "preserved":
                builder.add_feature("font-mapping", "preserved", target_id=builder.root_id)
            else:
                diagnostic = builder.add_diagnostic(
                    "DFIR-PDF-FONT-CMAP-UNAVAILABLE",
                    f"PDF font object {font_object_name} has no usable ToUnicode CMap; glyph mapping is not fabricated.",
                    phase="normalize",
                    target_id=builder.root_id,
                )
                builder.add_feature("font-mapping", "unavailable", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        streams = _page_content_streams(objects, raw, limits=limits, events=limit_events)
        pages_seen = 0
        for page_index in range(page_count):
            pages_seen += 1
            page_id = safe_id("node", f"pdf-page-{pages_seen}")
            surface_id = safe_id("surface", f"pdf-page-{pages_seen}")
            space_id = _coordinate(builder, pages_seen)
            builder.add_item("surfaces", {"surfaceId": surface_id, "partId": document_part, "kind": "page", "ordinal": pages_seen - 1, "coordinateSpaceId": space_id, "status": "preserved"}, "surfaceId")
            builder.add_node("section", page_id, parent_id=builder.root_id, part_id=document_part, status="preserved")
            builder.add_source_map(page_id, {"page": pages_seen, "object": pages_seen})
            page_object = page_objects[pages_seen - 1][1]
            page_text = streams[pages_seen - 1] if pages_seen <= len(streams) else b""
            fragments, parsed_paths, unsupported_operators, text_positions, graphics_states = _interpret_content(page_text, limits=limits, events=limit_events)
            if not fragments:
                fragments = _stream_text(page_text.decode("latin-1", errors="replace"))
            for fragment, value in enumerate(fragments, start=1):
                _add_text(builder, page_id, value, pages_seen, fragment, space_id, text_positions[fragment - 1] if fragment <= len(text_positions) else None, font_by_resource)
            for state_index, state_record in enumerate(graphics_states, start=1):
                extension_id = safe_id("extension", f"pdf-graphics-state-{pages_seen}-{state_index}")
                builder.add_item(
                    "extensions",
                    {
                        "extensionId": extension_id,
                        "targetId": page_id,
                        "namespace": "urn:fdir:format:pdf",
                        "type": "graphics-state",
                        "schemaVersion": "1.0.0",
                        "schemaId": "urn:fdir:schema:pdf-graphics-state",
                        "payload": {"page": pages_seen, "operator": state_record["operator"], "operands": state_record["operands"], "state": state_record["state"]},
                        "criticality": "non-critical",
                    },
                    "extensionId",
                )
            if graphics_states:
                builder.add_feature("graphics-state", "preserved", target_id=page_id)
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
            _add_pdf_annotations(builder, page_id, pages_seen, page_object, objects, limits=limits, events=limit_events)
            renderer_diagnostic = builder.add_diagnostic("DFIR-PDF-RENDERER-UNAVAILABLE", "No renderer worker is configured; renderer result is unavailable and source facts are unchanged.", phase="observe", target_id=page_id)
            ocr_diagnostic = builder.add_diagnostic("DFIR-PDF-OCR-UNAVAILABLE", "No OCR worker is configured; OCR result is unavailable and source text is unchanged.", phase="observe", target_id=page_id)
            builder.add_feature("renderer-observation", "unavailable", target_id=page_id, diagnostic_ids=[renderer_diagnostic])
            builder.add_feature("ocr-observation", "unavailable", target_id=page_id, diagnostic_ids=[ocr_diagnostic])
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-paint"), "kind": "draw", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-reading"), "kind": "reading", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:]) if node["kind"] in {"glyph", "path"}], "status": "ambiguous"}, "orderId")
        if "/Font" in content and not font_mappings:
            resource_id = safe_id("resource", "pdf-font-unresolved")
            builder.add_item("resources", {"resourceId": resource_id, "kind": "font", "mediaType": "application/x-font", "availability": "unavailable", "derivedHandle": "font-resource"}, "resourceId")
            diagnostic = builder.add_diagnostic("DFIR-PDF-FONT-RESOURCE-UNAVAILABLE", "PDF font references could not be resolved to an indirect font object.", phase="parse", target_id=builder.root_id)
            builder.add_feature("font-mapping", "unavailable", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        _emit_pdf_limit_events(builder, limit_events, target_id=builder.root_id)
        builder.add_feature("pages", "preserved", target_id=builder.root_id)
        return builder.finish()
    except (OSError, ValueError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-PDF-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("document", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
