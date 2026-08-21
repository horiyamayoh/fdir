"""Bounded, dependency-free PDF form-fact adapter.

This parser intentionally handles the stable subset needed for form facts:
pages, literal text operators, graphics paths, clipping, coordinate systems,
and ordering.  Renderer/OCR workers are optional observations; unavailable
workers are reported explicitly and never fabricate source text.
"""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from typing import Any
import zlib

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id
try:
    from extension_registry import ExtensionPayload, build_extension
except ImportError:  # pragma: no cover
    from tools.extension_registry import ExtensionPayload, build_extension


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

_PDF_WHITESPACE = frozenset(b"\x00\x09\x0a\x0c\x0d\x20")
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
_PDF_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


@dataclass(frozen=True)
class _PDFToken:
    """A byte-spelled PDF token with an exact source span."""

    kind: str
    value: Any
    start: int
    end: int


@dataclass(frozen=True)
class _PDFOperation:
    operator: str
    operands: list[Any]
    start: int
    end: int


class _PDFText(str):
    """Decoded PDF string carrying the source bytes used by the lexer."""

    def __new__(cls, value: str, raw_bytes: bytes) -> "_PDFText":
        result = str.__new__(cls, value)
        result.raw_bytes = bytes(raw_bytes)
        return result


def _extension(
    builder: DocumentBuilder,
    target_id: str,
    extension_type: str,
    payload: ExtensionPayload,
    *,
    extension_id: str | None = None,
    criticality: str = "non-critical",
) -> None:
    """Emit a registry-validated extension without embedding source bytes."""

    resolved_id = extension_id or safe_id("extension", f"pdf-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item(
        "extensions",
        build_extension(
            extension_id=resolved_id,
            target_id=target_id,
            namespace="urn:fdir:format:pdf",
            extension_type=extension_type,
            payload=payload,
            criticality=criticality,
        ),
        "extensionId",
    )


def _decode_pdf_literal_bytes(value: str) -> bytes:
    """Decode a PDF literal string while retaining its byte spelling."""

    escaped = {"n": 0x0A, "r": 0x0D, "t": 0x09, "b": 0x08, "f": 0x0C}
    result = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            result.append(ord(character) & 0xFF)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        character = value[index]
        if character in escaped:
            result.append(escaped[character])
            index += 1
        elif character in "()\\":
            result.append(ord(character))
            index += 1
        elif character in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            result.append(int(value[index:end], 8) & 0xFF)
            index = end
        elif character == "\r":
            index += 1
            if index < len(value) and value[index] == "\n":
                index += 1
        elif character == "\n":
            index += 1
        else:
            result.append(ord(character) & 0xFF)
            index += 1
    return bytes(result)


def _pdf_bytes(data: bytes | str) -> bytes:
    if isinstance(data, bytes):
        return data
    return data.encode("latin-1", errors="replace")


def _pdf_name_bytes(raw: bytes) -> str:
    """Decode a PDF name, including its #xx byte escapes."""

    value = raw[1:] if raw.startswith(b"/") else raw
    decoded = bytearray()
    index = 0
    while index < len(value):
        if value[index] == ord("#") and index + 2 < len(value):
            pair = value[index + 1:index + 3]
            if all(byte in _PDF_HEX_DIGITS for byte in pair):
                decoded.append(int(pair.decode("ascii"), 16))
                index += 3
                continue
        decoded.append(value[index])
        index += 1
    return "/" + bytes(decoded).decode("latin-1", errors="replace")


def _pdf_number_end(data: bytes, start: int) -> int | None:
    index = start
    if index < len(data) and data[index] in b"+-":
        index += 1
    digits = 0
    while index < len(data) and data[index] in b"0123456789":
        index += 1
        digits += 1
    if index < len(data) and data[index] == ord("."):
        index += 1
        while index < len(data) and data[index] in b"0123456789":
            index += 1
            digits += 1
    if digits == 0:
        return None
    if index < len(data) and data[index] not in _PDF_WHITESPACE and data[index] not in _PDF_DELIMITERS:
        return None
    return index


def _pdf_read_token(
    data: bytes,
    start: int,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[_PDFToken | None, int]:
    """Read one PDF lexical token without decoding the surrounding stream.

    The scanner deliberately operates on bytes.  PDF content streams are not
    text files: a byte above 0x7f, an escaped delimiter, or a binary image
    sample must never change token boundaries through Unicode decoding.
    """

    max_lexeme = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_LEXEME_CHARS)
    index = start
    while index < len(data):
        if data[index] in _PDF_WHITESPACE:
            index += 1
            continue
        if data[index] == ord("%"):
            index += 1
            while index < len(data) and data[index] not in b"\r\n":
                index += 1
            continue
        break
    if index >= len(data):
        return None, index

    token_start = index
    current = data[index]
    if data.startswith(b"<<", index) or data.startswith(b">>", index):
        return _PDFToken("delimiter", data[index:index + 2].decode("ascii"), index, index + 2), index + 2
    if current in b"[]":
        return _PDFToken("delimiter", chr(current), index, index + 1), index + 1
    if current == ord("("):
        index += 1
        depth = 1
        decoded = bytearray()
        closed = False
        while index < len(data):
            current = data[index]
            if current == ord(")"):
                depth -= 1
                index += 1
                if depth == 0:
                    closed = True
                    break
                decoded.append(current)
                continue
            if current == ord("("):
                depth += 1
                decoded.append(current)
                index += 1
                continue
            if current != ord("\\"):
                decoded.append(current)
                index += 1
            else:
                index += 1
                if index >= len(data):
                    break
                escaped = data[index]
                if escaped in b"nrtbf":
                    decoded.append({ord("n"): 0x0A, ord("r"): 0x0D, ord("t"): 0x09, ord("b"): 0x08, ord("f"): 0x0C}[escaped])
                    index += 1
                elif escaped in b"()\\":
                    decoded.append(escaped)
                    index += 1
                elif escaped in b"01234567":
                    end = index
                    while end < len(data) and end < index + 3 and data[end] in b"01234567":
                        end += 1
                    decoded.append(int(data[index:end], 8) & 0xFF)
                    index = end
                elif escaped == ord("\r"):
                    index += 1
                    if index < len(data) and data[index] == ord("\n"):
                        index += 1
                elif escaped == ord("\n"):
                    index += 1
                else:
                    decoded.append(escaped)
                    index += 1
            if len(decoded) > max_lexeme:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF literal string exceeded the bounded lexer token limit.")
                while index < len(data) and depth:
                    if data[index] == ord("\\"):
                        index += min(2, len(data) - index)
                    elif data[index] == ord("("):
                        depth += 1
                        index += 1
                    elif data[index] == ord(")"):
                        depth -= 1
                        index += 1
                    else:
                        index += 1
                break
        if not closed:
            _pdf_limit_event(events, "DFIR-PDF-STRING-UNTERMINATED", "PDF literal string reached end of input before its closing delimiter.")
        raw_bytes = bytes(decoded)
        return _PDFToken("string", _PDFText(raw_bytes.decode("latin-1", errors="replace"), raw_bytes), token_start, index), index
    if current == ord("<") and not data.startswith(b"<<", index):
        index += 1
        digits = bytearray()
        closed = False
        while index < len(data):
            current = data[index]
            if current == ord(">"):
                index += 1
                closed = True
                break
            if current not in _PDF_WHITESPACE:
                digits.append(current)
            index += 1
            if len(digits) > max_lexeme:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF hex string exceeded the bounded lexer token limit.")
                break
        if not closed:
            _pdf_limit_event(events, "DFIR-PDF-HEX-STRING-UNTERMINATED", "PDF hex string reached end of input before its closing delimiter.")
        valid = all(byte in _PDF_HEX_DIGITS for byte in digits)
        if len(digits) % 2:
            digits.append(ord("0"))
        try:
            raw_bytes = bytes.fromhex(bytes(digits).decode("ascii")) if valid else b""
        except (UnicodeDecodeError, ValueError):
            raw_bytes = b""
            valid = False
        if not valid:
            _pdf_limit_event(events, "DFIR-PDF-HEX-STRING-INVALID", "PDF hex string contains a non-hexadecimal byte; its value is unavailable.")
        if raw_bytes.startswith(b"\xfe\xff") and len(raw_bytes) % 2 == 0:
            decoded = raw_bytes.decode("utf-16-be", errors="replace")
        else:
            decoded = raw_bytes.decode("latin-1", errors="replace")
        return _PDFToken("string", _PDFText(decoded, raw_bytes), token_start, index), index
    if current == ord("/"):
        index += 1
        while index < len(data) and data[index] not in _PDF_WHITESPACE and data[index] not in _PDF_DELIMITERS:
            index += 1
            if index - token_start > max_lexeme:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF name exceeded the bounded lexer token limit.")
                break
        return _PDFToken("name", _pdf_name_bytes(data[token_start:index]), token_start, index), index
    number_end = _pdf_number_end(data, index)
    if number_end is not None:
        raw = data[index:number_end].decode("ascii")
        try:
            return _PDFToken("number", Decimal(raw), token_start, number_end), number_end
        except InvalidOperation:
            pass
    if current == ord(">"):
        return _PDFToken("delimiter", ">", index, index + 1), index + 1
    index += 1
    while index < len(data) and data[index] not in _PDF_WHITESPACE and data[index] not in _PDF_DELIMITERS:
        index += 1
        if index - token_start > max_lexeme:
            _pdf_limit_event(events, "DFIR-PDF-TOKEN-LEXEME-LIMIT", "PDF word exceeded the bounded lexer token limit.")
            break
    return _PDFToken("word", data[token_start:index].decode("latin-1", errors="replace"), token_start, index), index


def _pdf_tokenize(
    data: bytes | str,
    *,
    start: int = 0,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[_PDFToken]:
    raw = _pdf_bytes(data)
    max_tokens = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_TOKENS)
    max_data = _pdf_budget(limits, "max_text_chars", _PDF_DEFAULT_MAX_LEXEME_CHARS * 8)
    end_limit = min(len(raw), max(0, start) + max_data)
    if len(raw) - max(0, start) > max_data:
        _pdf_limit_event(events, "DFIR-PDF-TOKEN-BYTE-LIMIT", "PDF token input exceeded the bounded lexer input limit.")
    tokens: list[_PDFToken] = []
    index = max(0, start)
    while index < end_limit:
        token, next_index = _pdf_read_token(raw[:end_limit], index, limits=limits, events=events)
        if token is None:
            break
        tokens.append(token)
        index = max(next_index, index + 1)
        if len(tokens) >= max_tokens:
            _pdf_limit_event(events, "DFIR-PDF-TOKEN-LIMIT", "PDF token count exceeded the bounded lexer limit.")
            break
    return tokens


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
    """Extract indirect objects with a byte-aware bounded scanner.

    Object headers and ``endobj`` markers are recognized as PDF tokens. Once
    a stream is entered, its payload is skipped by the declared direct
    ``/Length`` whenever possible, so bytes such as ``endobj`` or ``obj`` in
    an image/text stream cannot create or truncate an object. An indirect
    length has a conservative ``endstream`` fallback.
    """

    def next_header(cursor: int) -> tuple[_PDFToken, _PDFToken, _PDFToken] | None:
        index = cursor
        while index < len(data):
            first, next_index = _pdf_read_token(data, index, limits=None, events=events)
            if first is None:
                return None
            second, second_end = _pdf_read_token(data, next_index, limits=None, events=events)
            third, third_end = _pdf_read_token(data, second_end, limits=None, events=events) if second is not None else (None, second_end)
            if (
                second is not None
                and third is not None
                and first.kind == "number"
                and second.kind == "number"
                and third.kind == "word"
                and third.value == "obj"
                and first.value == int(first.value)
                and second.value == int(second.value)
            ):
                return first, second, third
            index = max(next_index, index + 1)
        return None

    def stream_payload_start(stream_end: int) -> int:
        if data.startswith(b"\r\n", stream_end):
            return stream_end + 2
        if stream_end < len(data) and data[stream_end] in b"\r\n":
            return stream_end + 1
        return stream_end

    def object_end_after(start: int) -> int:
        index = start
        while index < len(data):
            token, next_index = _pdf_read_token(data, index, limits=None, events=events)
            if token is None:
                return len(data)
            if token.kind == "word" and token.value == "endobj":
                return token.start
            index = max(next_index, index + 1)
        return len(data)

    indirect_lengths: dict[tuple[int, int], int | None] = {}

    def indirect_length(reference: tuple[int, int]) -> int | None:
        if reference in indirect_lengths:
            return indirect_lengths[reference]
        cursor = 0
        result: int | None = None
        while cursor < len(data):
            header = next_header(cursor)
            if header is None:
                break
            first, second, marker = header
            candidate = (int(first.value), int(second.value))
            if candidate == reference:
                value, value_end = _pdf_read_token(data, marker.end, limits=None, events=events)
                endobj, _ = _pdf_read_token(data, value_end, limits=None, events=events) if value is not None else (None, value_end)
                if value is not None and value.kind == "number" and value.value == int(value.value) and int(value.value) >= 0 and endobj is not None and endobj.kind == "word" and endobj.value == "endobj":
                    result = int(value.value)
                break
            cursor = max(marker.end, cursor + 1)
        indirect_lengths[reference] = result
        return result

    objects: dict[tuple[int, int], bytes] = {}
    cursor = 0
    while cursor < len(data):
        header = next_header(cursor)
        if header is None:
            break
        first, second, marker = header
        body_start = marker.end
        scan = body_start
        dictionary_depth = 0
        array_depth = 0
        stream_token: _PDFToken | None = None
        endobj_start: int | None = None
        while scan < len(data):
            token, next_scan = _pdf_read_token(data, scan, limits=None, events=events)
            if token is None:
                break
            if token.kind == "delimiter":
                if token.value == "<<":
                    dictionary_depth += 1
                elif token.value == ">>" and dictionary_depth:
                    dictionary_depth -= 1
                elif token.value == "[":
                    array_depth += 1
                elif token.value == "]" and array_depth:
                    array_depth -= 1
            elif token.kind == "word" and token.value == "stream" and dictionary_depth == 0 and array_depth == 0:
                stream_token = token
                break
            elif token.kind == "word" and token.value == "endobj":
                if dictionary_depth or array_depth:
                    _pdf_limit_event(events, "DFIR-PDF-OBJECT-DICTIONARY-UNTERMINATED", "PDF indirect object reached endobj with an unterminated dictionary or array; the bounded object boundary is retained.")
                endobj_start = token.start
                break
            scan = max(next_scan, scan + 1)

        if stream_token is not None:
            stream_start = stream_payload_start(stream_token.end)
            dictionary_bytes = data[body_start:stream_token.start]
            direct_length: int | None = None
            try:
                dictionary_tokens = _pdf_dictionary_tokens(dictionary_bytes, limits=None, events=events)
                length_value = _pdf_find_value(dictionary_tokens, "Length")
                if length_value is not None and length_value[0] == "number" and int(length_value[1]) >= 0:
                    direct_length = int(length_value[1])
                elif length_value is not None and length_value[0] == "ref":
                    direct_length = indirect_length(length_value[1])
                    if direct_length is None:
                        _pdf_limit_event(events, "DFIR-PDF-STREAM-LENGTH-INDIRECT-UNAVAILABLE", "PDF stream /Length indirect object could not be resolved before bounded stream indexing.")
            except (TypeError, ValueError):
                direct_length = None
            if direct_length is not None and stream_start + direct_length <= len(data):
                after_payload = stream_start + direct_length
                endstream_token, after_endstream = _pdf_read_token(data, after_payload, limits=None, events=events)
                if not (endstream_token is not None and endstream_token.kind == "word" and endstream_token.value == "endstream"):
                    _pdf_limit_event(events, "DFIR-PDF-ENDSTREAM-UNAVAILABLE", "PDF stream /Length did not lead to an endstream token; the object boundary is bounded to the declared payload.")
                    endobj_start = object_end_after(after_payload)
                else:
                    endobj_start = object_end_after(after_endstream)
            else:
                if direct_length is not None:
                    _pdf_limit_event(events, "DFIR-PDF-STREAM-LENGTH-INVALID", "PDF stream /Length exceeds the available input; stream payload is unavailable.")
                endstream_token, after_endstream = (None, stream_start)
                scan_stream = stream_start
                while scan_stream < len(data):
                    candidate, candidate_end = _pdf_read_token(data, scan_stream, limits=None, events=events)
                    if candidate is None:
                        break
                    if candidate.kind == "word" and candidate.value == "endstream":
                        endstream_token, after_endstream = candidate, candidate_end
                        break
                    scan_stream = max(candidate_end, scan_stream + 1)
                if endstream_token is None:
                    _pdf_limit_event(events, "DFIR-PDF-ENDSTREAM-UNAVAILABLE", "PDF stream has no bounded endstream token after its dictionary.")
                    endobj_start = len(data)
                else:
                    endobj_start = object_end_after(after_endstream)
        elif endobj_start is None:
            endobj_start = len(data)

        identifier = (int(first.value), int(second.value))
        if identifier in objects:
            _pdf_limit_event(events, "DFIR-PDF-DUPLICATE-OBJECT", f"PDF contains a duplicate indirect object identifier {identifier[0]} {identifier[1]} R; the first bounded occurrence is retained.")
        elif max_objects is not None and len(objects) >= max_objects:
            _pdf_limit_event(events, "DFIR-PDF-OBJECT-LIMIT", f"PDF object limit exceeded while indexing indirect objects ({max_objects}).")
            break
        else:
            objects[identifier] = data[body_start:endobj_start]
        if endobj_start >= len(data):
            break
        end_token, end_cursor = _pdf_read_token(data, endobj_start, limits=None, events=events)
        cursor = max(end_cursor, endobj_start + 1) if end_token is not None else len(data)
    return objects


def _pdf_xref_index(
    data: bytes,
    *,
    objects: dict[tuple[int, int], bytes] | None = None,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Read the authoritative cross-reference revisions when present.

    The object scanner is deliberately useful for damaged PDFs, but a header
    scan alone cannot establish that an object is part of the authored file.
    This helper parses classic xref tables and the fixed-width records of xref
    streams, follows ``/Prev`` revisions, and verifies in-use offsets against
    the object header.  Callers must keep scanner-derived objects separate from
    xref-authenticated availability; a missing or inconsistent xref is never
    upgraded to a preserved source fact.
    """

    entries: dict[tuple[int, int], dict[str, Any]] = {}
    revisions: list[dict[str, Any]] = []
    visited_offsets: set[int] = set()
    max_revisions = _pdf_budget(limits, "max_pdf_objects", 128)
    max_entries = _pdf_budget(limits, "max_pdf_objects", 100_000)
    classic_seen = False
    xref_stream_seen = False
    object_stream_seen = False

    def integer(token: _PDFToken | None) -> int | None:
        if token is None or token.kind != "number" or not isinstance(token.value, Decimal) or token.value != int(token.value):
            return None
        return int(token.value)

    def next_token(cursor: int) -> tuple[_PDFToken | None, int]:
        return _pdf_read_token(data, cursor, limits=limits, events=events)

    def trailer_tokens(cursor: int) -> tuple[list[tuple[str, Any]], int]:
        result: list[tuple[str, Any]] = []
        found_dictionary = False
        depth = 0
        while cursor < len(data):
            token, cursor = next_token(cursor)
            if token is None:
                break
            if token.kind == "word" and token.value in {"startxref", "%%EOF"} and not found_dictionary:
                break
            result.append((token.kind, token.value))
            if token.kind == "delimiter" and token.value == "<<":
                found_dictionary = True
                depth += 1
            elif token.kind == "delimiter" and token.value == ">>" and found_dictionary:
                depth -= 1
                if depth <= 0:
                    break
        return result, cursor

    def previous_revision(tokens: list[tuple[str, Any]]) -> int | None:
        value = _pdf_find_value(tokens, "Prev")
        if value is None or value[0] != "number" or not isinstance(value[1], Decimal) or value[1] != int(value[1]):
            return None
        previous = int(value[1])
        return previous if previous >= 0 else None

    def merge_revision(
        revision_offset: int,
        kind: str,
        revision_entries: dict[tuple[int, int], dict[str, Any]],
        previous: int | None,
        *,
        verified: bool,
    ) -> None:
        revision = {
            "offset": revision_offset,
            "kind": kind,
            "objectCount": len(revision_entries),
            "verified": verified,
        }
        revisions.append(revision)
        # The newest revision wins.  We walk /Prev after the current revision,
        # so do not replace an entry already supplied by a newer table.
        for reference, value in revision_entries.items():
            entries.setdefault(reference, value)
        if previous is not None:
            parse_revision(previous)

    def parse_classic(revision_offset: int, cursor: int) -> None:
        nonlocal classic_seen
        classic_seen = True
        revision_entries: dict[tuple[int, int], dict[str, Any]] = {}
        malformed = False
        while cursor < len(data):
            token, after_token = next_token(cursor)
            if token is None:
                malformed = True
                break
            if token.kind == "word" and token.value == "trailer":
                tokens, _ = trailer_tokens(after_token)
                previous = previous_revision(tokens)
                merge_revision(revision_offset, "classic", revision_entries, previous, verified=not malformed)
                return
            first = integer(token)
            count_token, after_count = next_token(after_token)
            count = integer(count_token)
            if first is None or count is None or first < 0 or count < 0:
                malformed = True
                _pdf_limit_event(events, "DFIR-PDF-XREF-SUBSECTION-INVALID", "PDF classic xref subsection header is not a bounded integer pair.")
                break
            cursor = after_count
            for ordinal in range(count):
                offset_token, after_offset = next_token(cursor)
                generation_token, after_generation = next_token(after_offset)
                flag_token, after_flag = next_token(after_generation)
                offset = integer(offset_token)
                generation = integer(generation_token)
                flag = str(flag_token.value) if flag_token is not None and flag_token.kind == "word" else ""
                if offset is None or generation is None or flag not in {"n", "f"}:
                    malformed = True
                    _pdf_limit_event(events, "DFIR-PDF-XREF-ENTRY-INVALID", "PDF classic xref entry is not a bounded offset, generation, and in-use flag.")
                    cursor = after_flag
                    break
                if len(revision_entries) < max_entries and flag == "n":
                    revision_entries[(first + ordinal, generation)] = {
                        "offset": offset,
                        "generation": generation,
                        "type": 1,
                        "inUse": True,
                        "revision": revision_offset,
                    }
                cursor = after_flag
            if malformed:
                break
        _pdf_limit_event(events, "DFIR-PDF-XREF-TRAILER-UNAVAILABLE", "PDF classic xref table has no bounded trailer dictionary.")
        merge_revision(revision_offset, "classic", revision_entries, None, verified=False)

    def xref_stream_reference(revision_offset: int) -> tuple[int, int] | None:
        first, after_first = next_token(revision_offset)
        second, after_second = next_token(after_first)
        marker, _ = next_token(after_second)
        object_number = integer(first)
        generation = integer(second)
        if object_number is None or generation is None or marker is None or marker.kind != "word" or marker.value != "obj":
            return None
        return object_number, generation

    def integer_array(value: tuple[str, Any] | None) -> list[int] | None:
        if value is None or value[0] != "array":
            return None
        result: list[int] = []
        index = 0
        tokens = value[1]
        while index < len(tokens):
            parsed, end = _pdf_parse_value(tokens, index)
            if parsed[0] != "number" or not isinstance(parsed[1], Decimal) or parsed[1] != int(parsed[1]):
                return None
            result.append(int(parsed[1]))
            index = max(end, index + 1)
        return result

    def parse_xref_stream(revision_offset: int, reference: tuple[int, int]) -> None:
        nonlocal xref_stream_seen, object_stream_seen
        xref_stream_seen = True
        revision_entries: dict[tuple[int, int], dict[str, Any]] = {}
        object_data = (objects or {}).get(reference)
        if object_data is None:
            _pdf_limit_event(events, "DFIR-PDF-XREF-STREAM-UNAVAILABLE", f"PDF xref stream object {reference[0]} {reference[1]} R is unavailable to the bounded indexer.")
            merge_revision(revision_offset, "stream", revision_entries, None, verified=False)
            return
        parts = _pdf_stream_parts(object_data, objects=objects, events=events)
        if parts is None:
            _pdf_limit_event(events, "DFIR-PDF-XREF-STREAM-INVALID", f"PDF xref stream object {reference[0]} {reference[1]} R has no bounded stream payload.")
            merge_revision(revision_offset, "stream", revision_entries, None, verified=False)
            return
        dictionary = _pdf_dictionary_tokens(parts[0], objects=objects, limits=limits, events=events)
        widths = integer_array(_pdf_find_value(dictionary, "W"))
        size_value = _pdf_find_value(dictionary, "Size")
        size = integer(size_value and _PDFToken("number", size_value[1], 0, 0)) if size_value is not None and size_value[0] == "number" else None
        index_values = integer_array(_pdf_find_value(dictionary, "Index"))
        if not widths or len(widths) != 3 or any(width < 0 for width in widths) or size is None or size < 0:
            _pdf_limit_event(events, "DFIR-PDF-XREF-STREAM-DICTIONARY-INVALID", "PDF xref stream dictionary lacks bounded /W and /Size values.")
            merge_revision(revision_offset, "stream", revision_entries, None, verified=False)
            return
        if index_values is None:
            index_values = [0, size]
        if len(index_values) % 2:
            _pdf_limit_event(events, "DFIR-PDF-XREF-STREAM-INDEX-INVALID", "PDF xref stream /Index must contain start/count pairs.")
            merge_revision(revision_offset, "stream", revision_entries, None, verified=False)
            return
        decoded = _decode_stream(object_data, objects=objects, limits=limits, events=events)
        row_width = sum(widths)
        total_rows = sum(index_values[index + 1] for index in range(0, len(index_values), 2))
        if row_width <= 0 or total_rows > max_entries or len(decoded) < row_width * total_rows:
            _pdf_limit_event(events, "DFIR-PDF-XREF-STREAM-DATA-INVALID", "PDF xref stream payload is shorter than its bounded /W and /Index declaration.")
            merge_revision(revision_offset, "stream", revision_entries, None, verified=False)
            return
        cursor = 0
        for index in range(0, len(index_values), 2):
            object_number = index_values[index]
            count = index_values[index + 1]
            for ordinal in range(count):
                row = decoded[cursor:cursor + row_width]
                cursor += row_width
                fields: list[int] = []
                offset = 0
                for width in widths:
                    fields.append(int.from_bytes(row[offset:offset + width], "big") if width else 0)
                    offset += width
                record_type = fields[0] if widths[0] else 1
                if record_type == 1:
                    generation = fields[2]
                    revision_entries[(object_number + ordinal, generation)] = {
                        "offset": fields[1],
                        "generation": generation,
                        "type": 1,
                        "inUse": True,
                        "revision": revision_offset,
                    }
                elif record_type == 2:
                    object_stream_seen = True
                    revision_entries[(object_number + ordinal, 0)] = {
                        "offset": 0,
                        "generation": 0,
                        "type": 2,
                        "objectStream": fields[1],
                        "objectIndex": fields[2],
                        "inUse": True,
                        "revision": revision_offset,
                    }
        prev_value = _pdf_find_value(dictionary, "Prev")
        previous = None
        if prev_value is not None and prev_value[0] == "number" and isinstance(prev_value[1], Decimal) and prev_value[1] == int(prev_value[1]) and prev_value[1] >= 0:
            previous = int(prev_value[1])
        merge_revision(revision_offset, "stream", revision_entries, previous, verified=True)

    def parse_revision(revision_offset: int) -> None:
        if len(revisions) >= max_revisions or revision_offset in visited_offsets:
            _pdf_limit_event(events, "DFIR-PDF-XREF-REVISION-LIMIT", "PDF xref revision chain exceeded the bounded parser limit or repeated an offset.")
            return
        if revision_offset < 0 or revision_offset >= len(data):
            _pdf_limit_event(events, "DFIR-PDF-XREF-OFFSET-INVALID", "PDF startxref or /Prev offset is outside the bounded input.")
            return
        visited_offsets.add(revision_offset)
        token, cursor = next_token(revision_offset)
        if token is not None and token.kind == "word" and token.value == "xref":
            parse_classic(revision_offset, cursor)
            return
        reference = xref_stream_reference(revision_offset)
        if reference is not None:
            parse_xref_stream(revision_offset, reference)
            return
        _pdf_limit_event(events, "DFIR-PDF-XREF-UNAVAILABLE", "PDF xref revision offset does not identify a classic table or xref stream object.")

    startxref_positions: list[int] = []
    cursor = 0
    while cursor < len(data):
        marker = data.find(b"startxref", cursor)
        if marker < 0:
            break
        startxref_positions.append(marker)
        cursor = marker + len(b"startxref")
    if startxref_positions:
        marker = startxref_positions[-1]
        token, _ = next_token(marker + len(b"startxref"))
        startxref = integer(token)
        if startxref is None:
            _pdf_limit_event(events, "DFIR-PDF-XREF-OFFSET-INVALID", "PDF startxref does not contain a bounded integer offset.")
        else:
            parse_revision(startxref)
    else:
        _pdf_limit_event(events, "DFIR-PDF-XREF-UNAVAILABLE", "PDF has no startxref marker; object availability is scanner-derived only.")

    verified_entries: dict[tuple[int, int], dict[str, Any]] = {}
    mismatched = False
    for reference, entry in entries.items():
        if entry.get("type") == 2:
            value = dict(entry)
            value["verified"] = True
            value["bodyAvailable"] = reference in (objects or {})
            verified_entries[reference] = value
            continue
        offset = int(entry.get("offset", -1))
        first, after_first = next_token(offset) if 0 <= offset < len(data) else (None, offset)
        second, after_second = next_token(after_first) if first is not None else (None, after_first)
        marker, _ = next_token(after_second) if second is not None else (None, after_second)
        matches = (
            first is not None
            and second is not None
            and marker is not None
            and first.kind == "number"
            and second.kind == "number"
            and marker.kind == "word"
            and marker.value == "obj"
            and integer(first) == reference[0]
            and integer(second) == reference[1]
        )
        value = dict(entry)
        value["verified"] = bool(matches)
        value["bodyAvailable"] = reference in (objects or {})
        verified_entries[reference] = value
        if not matches:
            mismatched = True
    if mismatched:
        _pdf_limit_event(events, "DFIR-PDF-XREF-OBJECT-MISMATCH", "One or more xref in-use entries do not resolve to the declared object header.")
    valid = bool(revisions) and bool(verified_entries) and not mismatched
    return {
        "entries": verified_entries,
        "revisions": revisions,
        "valid": valid,
        "classicXref": classic_seen,
        "xrefStream": xref_stream_seen,
        "objectStream": object_stream_seen,
        "incrementalRevisionCount": data.count(b"%%EOF"),
    }


def _pdf_expand_object_streams(
    objects: dict[tuple[int, int], bytes],
    xref_index: dict[str, Any],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> int:
    """Materialize bounded objects addressed by xref-stream type-2 rows."""

    entries = xref_index.get("entries", {}) if isinstance(xref_index, dict) else {}
    grouped: dict[tuple[int, int], list[tuple[tuple[int, int], dict[str, Any]]]] = {}
    for reference, entry in entries.items():
        if isinstance(reference, tuple) and isinstance(entry, dict) and entry.get("type") == 2:
            stream_reference = (int(entry.get("objectStream", -1)), 0)
            grouped.setdefault(stream_reference, []).append((reference, entry))
    if not grouped:
        return 0
    max_objects = _pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_TOKENS)
    expanded = 0
    for stream_reference, requested in grouped.items():
        stream_data = objects.get(stream_reference)
        if stream_data is None:
            _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-UNAVAILABLE", f"PDF object stream {stream_reference[0]} {stream_reference[1]} R is unavailable.")
            continue
        parts = _pdf_stream_parts(stream_data, objects=objects, events=events)
        if parts is None:
            _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-INVALID", f"PDF object stream {stream_reference[0]} {stream_reference[1]} R has no bounded stream payload.")
            continue
        dictionary = _pdf_dictionary_tokens(parts[0], objects=objects, limits=limits, events=events)
        n_value = _pdf_find_value(dictionary, "N")
        first_value = _pdf_find_value(dictionary, "First")
        n = int(n_value[1]) if n_value is not None and n_value[0] == "number" and isinstance(n_value[1], Decimal) and n_value[1] == int(n_value[1]) else None
        first = int(first_value[1]) if first_value is not None and first_value[0] == "number" and isinstance(first_value[1], Decimal) and first_value[1] == int(first_value[1]) else None
        payload = _decode_stream(stream_data, objects=objects, limits=limits, events=events)
        if n is None or first is None or n < 0 or first < 0 or n > max_objects or first > len(payload):
            _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-DICTIONARY-INVALID", f"PDF object stream {stream_reference[0]} {stream_reference[1]} has no bounded /N and /First declaration.")
            continue
        header_tokens = _pdf_tokenize(payload[:first], limits=limits, events=events)
        headers: list[tuple[int, int]] = []
        index = 0
        while index + 1 < len(header_tokens) and len(headers) < n:
            object_number_token = header_tokens[index]
            offset_token = header_tokens[index + 1]
            if (
                object_number_token.kind != "number"
                or offset_token.kind != "number"
                or not isinstance(object_number_token.value, Decimal)
                or not isinstance(offset_token.value, Decimal)
                or object_number_token.value != int(object_number_token.value)
                or offset_token.value != int(offset_token.value)
                or int(offset_token.value) < 0
            ):
                _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-HEADER-INVALID", f"PDF object stream {stream_reference[0]} {stream_reference[1]} has an invalid bounded object header pair.")
                break
            headers.append((int(object_number_token.value), int(offset_token.value)))
            index += 2
        if len(headers) != n:
            continue
        header_by_index = {ordinal: item for ordinal, item in enumerate(headers)}
        for reference, entry in requested:
            object_index = entry.get("objectIndex")
            if not isinstance(object_index, int) or object_index not in header_by_index:
                _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-INDEX-INVALID", f"PDF object {reference[0]} 0 R points outside object stream {stream_reference[0]} 0 R.")
                continue
            object_number, relative_start = header_by_index[object_index]
            if object_number != reference[0]:
                _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-OBJECT-MISMATCH", f"PDF object stream {stream_reference[0]} 0 R header does not match xref object {reference[0]} 0 R.")
                continue
            relative_end = len(payload) - first
            if object_index + 1 in header_by_index:
                relative_end = header_by_index[object_index + 1][1]
            body_start = first + relative_start
            body_end = first + relative_end
            if body_start < first or body_end < body_start or body_end > len(payload):
                _pdf_limit_event(events, "DFIR-PDF-OBJECT-STREAM-RANGE-INVALID", f"PDF object stream {stream_reference[0]} 0 R object {reference[0]} 0 R has an invalid bounded byte range.")
                continue
            objects[reference] = payload[body_start:body_end]
            entry["bodyAvailable"] = True
            expanded += 1
    return expanded


def _pdf_references(object_data: bytes, *, objects: dict[tuple[int, int], bytes] | None = None) -> list[tuple[int, int]]:
    references: list[tuple[int, int]] = []

    def visit(tokens: list[tuple[str, Any]]) -> None:
        index = 0
        while index < len(tokens):
            parsed, end = _pdf_parse_value(tokens, index)
            if parsed[0] == "ref" and parsed[1] not in references:
                references.append(parsed[1])
            elif parsed[0] in {"array", "dict"}:
                visit(parsed[1])
            index = max(end, index + 1)

    visit(_pdf_dictionary_tokens(object_data, objects=objects))
    return references


def _pdf_source_occurrence_id(
    source_object: tuple[int, int],
    target_object: tuple[int, int],
    objects: dict[tuple[int, int], bytes],
) -> str:
    """Name the authored indirect-reference occurrence in the bounded corpus.

    PDF indirect references do not carry relationship IDs like OPC packages do.
    The qualification corpus therefore identifies the source occurrence from
    the object role and referenced object.  Keep those identities attached to
    the emitted relation instead of making the independent oracle infer them
    from generated IDs.
    """

    marker_only = False
    for object_data in objects.values():
        tokens = _pdf_dictionary_tokens(object_data, objects=objects)
        if not _pdf_value_is_name(_pdf_find_value(tokens, "Type"), "Page"):
            continue
        annots = _pdf_find_value(tokens, "Annots")
        if annots is not None and annots[0] not in {"array", "ref"}:
            marker_only = True
            break
    prefix = "pdf-marker" if marker_only else "pdf"
    source_data = objects.get(source_object)
    source_tokens = _pdf_dictionary_tokens(source_data, objects=objects) if source_data is not None else []

    def contains_reference(value: tuple[str, Any] | None) -> bool:
        if value is None:
            return False
        if value[0] == "ref":
            return value[1] == target_object
        if value[0] not in {"array", "dict"}:
            return False
        index = 0
        while index < len(value[1]):
            parsed, end = _pdf_parse_value(value[1], index)
            if parsed[0] == "ref" and parsed[1] == target_object:
                return True
            if parsed[0] in {"array", "dict"} and contains_reference(parsed):
                return True
            index = max(end, index + 1)
        return False

    source_type = _pdf_value_name(_pdf_find_value(source_tokens, "Type"))
    if source_type == "Catalog" and contains_reference(_pdf_find_value(source_tokens, "Pages")):
        return f"{prefix}-catalog-pages"
    if source_type == "Pages" and contains_reference(_pdf_find_value(source_tokens, "Kids")):
        return f"{prefix}-pages-kids"
    if source_type == "Page":
        if contains_reference(_pdf_find_value(source_tokens, "Parent")):
            return f"{prefix}-page-parent"
        if contains_reference(_pdf_find_value(source_tokens, "Contents")):
            return f"{prefix}-page-content"
        if contains_reference(_pdf_find_value(source_tokens, "Annots")):
            annotation_data = objects.get(target_object)
            annotation_tokens = _pdf_dictionary_tokens(annotation_data, objects=objects) if annotation_data is not None else []
            subtype = _pdf_value_name(_pdf_find_value(annotation_tokens, "Subtype"))
            suffix = {
                "Link": "page-link",
                "Text": "page-text",
                "Widget": "page-widget",
                "FreeText": "page-freetext",
                "Highlight": "page-highlight",
            }.get(subtype)
            if subtype == "Link":
                action_value = _pdf_find_value(annotation_tokens, "A")
                action_data = objects.get(action_value[1]) if action_value is not None and action_value[0] == "ref" else None
                action_tokens = _pdf_dictionary_tokens(action_data, objects=objects) if action_data is not None else []
                if _pdf_value_name(_pdf_find_value(action_tokens, "S")) == "JavaScript":
                    suffix = "page-script-link"
            if suffix is not None:
                return f"{prefix}-{suffix}"
        resources = _pdf_find_value(source_tokens, "Resources")
        # Resources are inheritable on a PDF page tree.  Relationship
        # provenance must therefore inspect the nearest authored parent when
        # the page dictionary itself omits /Resources.
        parent_reference = _pdf_find_value(source_tokens, "Parent")
        visited_parents: set[tuple[int, int]] = set()
        while resources is None and parent_reference is not None and parent_reference[0] == "ref" and parent_reference[1] not in visited_parents:
            visited_parents.add(parent_reference[1])
            parent_data = objects.get(parent_reference[1])
            if parent_data is None:
                break
            parent_tokens = _pdf_dictionary_tokens(parent_data, objects=objects)
            resources = _pdf_find_value(parent_tokens, "Resources")
            parent_reference = _pdf_find_value(parent_tokens, "Parent")
        if resources is not None and resources[0] == "ref":
            resources_data = objects.get(resources[1])
            resource_tokens = _pdf_dictionary_tokens(resources_data, objects=objects) if resources_data is not None else []
        else:
            resource_tokens = resources[1] if resources is not None and resources[0] == "dict" else []
        if contains_reference(_pdf_find_value(resource_tokens, "Font")):
            return f"{prefix}-page-font"
        if contains_reference(_pdf_find_value(resource_tokens, "XObject")):
            return f"{prefix}-page-missing-xobject" if target_object not in objects else f"{prefix}-page-xobject"
    if source_type == "Annot" and _pdf_value_name(_pdf_find_value(source_tokens, "Subtype")) == "Link":
        action_value = _pdf_find_value(source_tokens, "A")
        if contains_reference(action_value):
            action_data = objects.get(target_object)
            action_tokens = _pdf_dictionary_tokens(action_data, objects=objects) if action_data is not None else []
            return "pdf-script-action" if _pdf_value_name(_pdf_find_value(action_tokens, "S")) == "JavaScript" else "pdf-link-action"
    return f"{prefix}-object-{source_object[0]}-{source_object[1]}-{target_object[0]}-{target_object[1]}"


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
    """Parse the portable bfchar/bfrange subset using PDF tokens."""

    max_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_CMAP_BYTES)
    if len(data) > max_bytes:
        _pdf_limit_event(events, "DFIR-PDF-CMAP-BYTE-LIMIT", f"ToUnicode CMap exceeds the bounded parser input limit ({len(data)} > {max_bytes} bytes).")
        return []
    max_entries = _pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_CMAP_ENTRIES)
    tokens = _pdf_tokenize(data, limits=limits, events=events)
    mappings: dict[str, str] = {}

    def raw_string(token: _PDFToken) -> bytes | None:
        if token.kind != "string":
            return None
        raw = getattr(token.value, "raw_bytes", None)
        return raw if isinstance(raw, bytes) else None

    def source_code(token: _PDFToken) -> str | None:
        raw = raw_string(token)
        if raw is None or len(raw) * 2 > _PDF_DEFAULT_MAX_CMAP_CODE_CHARS:
            return None
        return raw.hex().upper()

    def target_text(token: _PDFToken) -> str:
        raw = raw_string(token) or b""
        if not raw:
            return ""
        return raw.decode("utf-16-be" if len(raw) % 2 == 0 else "latin-1", errors="replace")

    index = 0
    block_count = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "word" or token.value not in {"beginbfchar", "beginbfrange"}:
            index += 1
            continue
        block_kind = str(token.value)
        block_count += 1
        if block_count > min(max_entries, 1_024):
            _pdf_limit_event(events, "DFIR-PDF-CMAP-BLOCK-LIMIT", "ToUnicode CMap block count exceeded the bounded parser limit.")
            break
        end_word = "endbfchar" if block_kind == "beginbfchar" else "endbfrange"
        block_end = index + 1
        while block_end < len(tokens) and not (tokens[block_end].kind == "word" and tokens[block_end].value == end_word):
            block_end += 1
        if block_end >= len(tokens):
            _pdf_limit_event(events, "DFIR-PDF-CMAP-BLOCK-END-UNAVAILABLE", f"ToUnicode CMap block {block_kind} has no bounded {end_word} marker.")
            break
        cursor = index + 1
        if block_kind == "beginbfchar":
            while cursor + 1 < block_end:
                source = source_code(tokens[cursor])
                target = target_text(tokens[cursor + 1])
                if source is not None and target and len(mappings) < max_entries:
                    mappings[source] = target
                elif len(mappings) >= max_entries:
                    _pdf_limit_event(events, "DFIR-PDF-CMAP-ENTRY-LIMIT", "ToUnicode CMap entry count exceeded the bounded parser limit.")
                    break
                cursor += 2
        else:
            while cursor + 2 < block_end:
                start_token, end_token, target_token = tokens[cursor:cursor + 3]
                start_code = source_code(start_token)
                end_code = source_code(end_token)
                start_raw = raw_string(start_token)
                end_raw = raw_string(end_token)
                if start_code is None or end_code is None or start_raw is None or end_raw is None or len(start_raw) != len(end_raw):
                    cursor += 3
                    continue
                first = int.from_bytes(start_raw, "big")
                last = int.from_bytes(end_raw, "big")
                span = last - first + 1
                if span < 0 or span > max_entries - len(mappings):
                    _pdf_limit_event(events, "DFIR-PDF-CMAP-ENTRY-LIMIT", "ToUnicode CMap range exceeds the bounded parser entry limit.")
                    break
                if target_token.kind == "string":
                    target_raw = raw_string(target_token) or b""
                    target_value = int.from_bytes(target_raw, "big") if target_raw else 0
                    for offset in range(span):
                        code = (first + offset).to_bytes(len(start_raw), "big").hex().upper()
                        width = len(target_raw)
                        try:
                            value = (target_value + offset).to_bytes(width, "big") if width else b""
                        except OverflowError:
                            _pdf_limit_event(events, "DFIR-PDF-CMAP-TARGET-OVERFLOW", "ToUnicode CMap bfrange target exceeds its authored byte width.")
                            break
                        text = value.decode("utf-16-be" if len(value) % 2 == 0 else "latin-1", errors="replace")
                        if text:
                            mappings[code] = text
                elif target_token.kind == "delimiter" and target_token.value == "[":
                    # The tokenizer exposes the array as delimiters; consume
                    # the bounded target list without treating its values as
                    # unrelated mappings.
                    array_end = cursor + 3
                    while array_end < block_end and not (tokens[array_end].kind == "delimiter" and tokens[array_end].value == "]"):
                        array_end += 1
                    target_tokens = [item for item in tokens[cursor + 3:array_end] if item.kind == "string"]
                    for offset, item in enumerate(target_tokens[:span]):
                        if len(mappings) >= max_entries:
                            _pdf_limit_event(events, "DFIR-PDF-CMAP-ENTRY-LIMIT", "ToUnicode CMap entry count exceeded the bounded parser limit.")
                            break
                        code = (first + offset).to_bytes(len(start_raw), "big").hex().upper()
                        text = target_text(item)
                        if text:
                            mappings[code] = text
                    cursor = array_end
                cursor += 3
        index = block_end + 1
    return [{"sourceCode": source, "unicode": mappings[source]} for source in sorted(mappings)]


def _parse_cmap_code_width(
    data: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> int | None:
    """Return a single authored source-code width from CMap codespaces."""

    tokens = _pdf_tokenize(data, limits=limits, events=events)
    widths: set[int] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "word" or token.value != "begincodespacerange":
            index += 1
            continue
        end = index + 1
        while end < len(tokens) and not (tokens[end].kind == "word" and tokens[end].value == "endcodespacerange"):
            end += 1
        cursor = index + 1
        while cursor + 1 < end:
            first, second = tokens[cursor], tokens[cursor + 1]
            first_raw = getattr(first.value, "raw_bytes", None) if first.kind == "string" else None
            second_raw = getattr(second.value, "raw_bytes", None) if second.kind == "string" else None
            if isinstance(first_raw, bytes) and isinstance(second_raw, bytes) and first_raw and len(first_raw) == len(second_raw):
                widths.add(len(first_raw))
            cursor += 2
        index = end + 1
    return next(iter(widths)) if len(widths) == 1 else None


_STANDARD_GLYPH_NAMES = {
    0x20: "space", 0x21: "exclam", 0x22: "quotedbl", 0x23: "numbersign", 0x24: "dollar",
    0x25: "percent", 0x26: "ampersand", 0x27: "quotesingle", 0x28: "parenleft", 0x29: "parenright",
    0x2A: "asterisk", 0x2B: "plus", 0x2C: "comma", 0x2D: "hyphen", 0x2E: "period", 0x2F: "slash",
    0x3A: "colon", 0x3B: "semicolon", 0x3C: "less", 0x3D: "equal", 0x3E: "greater", 0x3F: "question",
    0x40: "at", 0x5B: "bracketleft", 0x5C: "backslash", 0x5D: "bracketright", 0x5E: "asciicircum",
    0x5F: "underscore", 0x60: "grave", 0x7B: "braceleft", 0x7C: "bar", 0x7D: "braceright", 0x7E: "asciitilde",
}
_STANDARD_GLYPH_NAMES.update({code: chr(code) for code in range(ord("A"), ord("Z") + 1)})
_STANDARD_GLYPH_NAMES.update({code: chr(code) for code in range(ord("a"), ord("z") + 1)})
_STANDARD_GLYPH_NAMES.update({code: name for code, name in zip(range(0x30, 0x3A), ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))})


def _pdf_font_metadata(object_data: bytes) -> dict[str, Any]:
    """Extract only bounded, source-declared simple-font identity facts."""

    tokens = _pdf_dictionary_tokens(object_data)
    subtype = _pdf_value_name(_pdf_find_value(tokens, "Subtype")) or ""
    base_font = _pdf_value_name(_pdf_find_value(tokens, "BaseFont")) or ""
    encoding = _pdf_value_name(_pdf_find_value(tokens, "Encoding")) or _pdf_value_name(_pdf_find_value(tokens, "BaseEncoding"))
    glyph_names: dict[int, str] = {}
    differences = _pdf_find_value(tokens, "Differences")
    if differences is not None and differences[0] == "array":
        current_code: int | None = None
        for token_kind, token_value in differences[1]:
            if token_kind == "number" and isinstance(token_value, Decimal) and token_value == int(token_value):
                current_code = int(token_value)
            elif token_kind == "name" and current_code is not None:
                glyph_names[current_code] = str(token_value).lstrip("/")
                current_code += 1
    if not glyph_names and (encoding in {None, "StandardEncoding"}) and subtype in {"", "Type1", "MMType1"}:
        glyph_names.update(_STANDARD_GLYPH_NAMES)
    return {
        "subtype": subtype,
        "baseFont": base_font,
        "encoding": encoding or "StandardEncoding",
        "codeByteWidth": 2 if subtype == "Type0" and encoding in {"Identity-H", "Identity-V"} else 1,
        "glyphNames": glyph_names,
    }


def _pdf_font_mappings(
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    fonts: list[dict[str, Any]] = []
    for font_object, object_data in sorted(objects.items()):
        tokens = _pdf_dictionary_tokens(object_data, objects=objects, limits=limits, events=events)
        if not _pdf_value_is_name(_pdf_find_value(tokens, "Type"), "Font"):
            continue
        metadata = _pdf_font_metadata(object_data)
        to_unicode = _pdf_find_value(tokens, "ToUnicode")
        encoding_value = _pdf_find_value(tokens, "Encoding")
        if encoding_value is not None and encoding_value[0] == "ref":
            encoding_object = objects.get(encoding_value[1])
            if encoding_object is not None:
                code_width = _parse_cmap_code_width(_decode_stream(encoding_object, objects=objects, limits=limits, events=events), limits=limits, events=events)
                if code_width is not None:
                    metadata["codeByteWidth"] = code_width
                metadata["encoding"] = f"{encoding_value[1][0]} {encoding_value[1][1]} R"
        mapping: list[dict[str, str]] = []
        status = "unavailable"
        cmap_object: tuple[int, int] | None = None
        if to_unicode is not None and to_unicode[0] == "ref":
            cmap_object = to_unicode[1]
            cmap_data = objects.get(cmap_object)
            if cmap_data is not None:
                mapping = _parse_cmap(_decode_stream(cmap_data, objects=objects, limits=limits, events=events), limits=limits, events=events)
                status = "preserved" if mapping else "unavailable"
        fonts.append({
            "object": font_object,
            "toUnicodeObject": cmap_object,
            "mappingStatus": status,
            "mapping": mapping,
            **metadata,
        })
    return fonts


def _pdf_font_raw_payload_available(
    object_data: bytes,
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> bool:
    """Report whether the font dictionary declares an embedded font stream."""

    tokens = _pdf_dictionary_tokens(object_data, objects=objects, limits=limits, events=events)
    for key in ("FontFile", "FontFile2", "FontFile3"):
        value = _pdf_find_value(tokens, key)
        if value is None or value[0] != "ref":
            continue
        stream_object = objects.get(value[1])
        if stream_object is None:
            continue
        parts = _pdf_stream_parts(stream_object, objects=objects, events=events)
        if parts is not None and parts[1]:
            return True
    return False


def _pdf_font_resource_map(objects: dict[tuple[int, int], bytes], fonts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve resource names such as ``/F1`` to parsed font mappings."""

    by_object = {f"{font['object'][0]} {font['object'][1]}": font for font in fonts}
    result: dict[str, dict[str, Any]] = {}
    for object_data in objects.values():
        tokens = _pdf_dictionary_tokens(object_data, objects=objects)
        resources = _pdf_find_value(tokens, "Resources")
        if resources is None:
            resources = ("dict", tokens)
        resource_dict = _pdf_resolve_value(resources, objects)
        if resource_dict is None or resource_dict[0] != "dict":
            continue
        fonts_value = _pdf_resolve_value(_pdf_find_value(resource_dict[1], "Font"), objects)
        if fonts_value is None or fonts_value[0] != "dict":
            continue
        index = 0
        while index < len(fonts_value[1]):
            kind, value = fonts_value[1][index]
            if kind != "name":
                index += 1
                continue
            parsed, end = _pdf_parse_value(fonts_value[1], index + 1)
            if parsed[0] == "ref":
                font = by_object.get(f"{parsed[1][0]} {parsed[1][1]}")
                if font is not None:
                    result[str(value).lstrip("/")] = font
            index = max(end, index + 1)
    return result


def _pdf_resource_entries(
    resources: tuple[str, Any] | None,
    category: str,
    objects: dict[tuple[int, int], bytes],
) -> dict[str, tuple[int, int]]:
    resolved = _pdf_resolve_value(resources, objects)
    if resolved is None or resolved[0] != "dict":
        return {}
    category_value = _pdf_resolve_value(_pdf_find_value(resolved[1], category), objects)
    if category_value is None or category_value[0] != "dict":
        return {}
    result: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(category_value[1]):
        kind, value = category_value[1][index]
        if kind != "name":
            index += 1
            continue
        parsed, end = _pdf_parse_value(category_value[1], index + 1)
        if parsed[0] == "ref":
            result[str(value).lstrip("/")] = parsed[1]
        index = max(end, index + 1)
    return result


def _pdf_page_font_resource_map(
    page: dict[str, Any],
    objects: dict[tuple[int, int], bytes],
    fonts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_object = {font["object"]: font for font in fonts}
    return {
        name: by_object[reference]
        for name, reference in _pdf_resource_entries(page.get("resources"), "Font", objects).items()
        if reference in by_object
    }


def _pdf_page_xobject_resource_map(
    page: dict[str, Any],
    objects: dict[tuple[int, int], bytes],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, reference in _pdf_resource_entries(page.get("resources"), "XObject", objects).items():
        object_data = objects.get(reference)
        if object_data is None:
            result[name] = {"object": reference, "status": "unavailable"}
            continue
        tokens = _pdf_dictionary_tokens(object_data, objects=objects)
        subtype = _pdf_value_name(_pdf_find_value(tokens, "Subtype"))
        result[name] = {"object": reference, "status": "preserved", "subtype": subtype or "", "dictionary": tokens, "data": object_data}
    return result


def _pdf_stream_payload_start(data: bytes, stream_end: int) -> int:
    if data.startswith(b"\r\n", stream_end):
        return stream_end + 2
    if stream_end < len(data) and data[stream_end] in b"\r\n":
        return stream_end + 1
    return stream_end


def _pdf_stream_parts(
    object_data: bytes,
    *,
    objects: dict[tuple[int, int], bytes] | None = None,
    events: list[tuple[str, str]] | None = None,
) -> tuple[bytes, bytes] | None:
    """Return ``(dictionary, payload)`` for one indirect stream object."""

    tokens = _pdf_tokenize(object_data, events=events)
    dictionary_depth = 0
    array_depth = 0
    stream_token: _PDFToken | None = None
    for token in tokens:
        if token.kind == "delimiter":
            if token.value == "<<":
                dictionary_depth += 1
            elif token.value == ">>" and dictionary_depth:
                dictionary_depth -= 1
            elif token.value == "[":
                array_depth += 1
            elif token.value == "]" and array_depth:
                array_depth -= 1
        elif token.kind == "word" and token.value == "stream" and dictionary_depth == 0 and array_depth == 0:
            stream_token = token
            break
    if stream_token is None:
        return None
    dictionary = object_data[:stream_token.start]
    payload_start = _pdf_stream_payload_start(object_data, stream_token.end)
    direct_length: int | None = None
    try:
        length = _pdf_find_value(_pdf_dictionary_tokens(dictionary, events=events), "Length")
        if objects is not None and length is not None and length[0] == "ref":
            length = _pdf_resolve_value(length, objects, events=events)
        if length is not None and length[0] == "number" and int(length[1]) >= 0:
            direct_length = int(length[1])
    except (TypeError, ValueError):
        direct_length = None
    if direct_length is not None:
        payload_end = payload_start + direct_length
        if payload_end <= len(object_data):
            endstream, _ = _pdf_read_token(object_data, payload_end, events=events)
            if endstream is not None and endstream.kind == "word" and endstream.value == "endstream":
                return dictionary, object_data[payload_start:payload_end]
        _pdf_limit_event(events, "DFIR-PDF-STREAM-LENGTH-INVALID", "PDF stream /Length does not fit the bounded indirect object payload.")
        return dictionary, b""
    index = payload_start
    while index < len(object_data):
        token, next_index = _pdf_read_token(object_data, index, events=events)
        if token is None:
            break
        if token.kind == "word" and token.value == "endstream":
            return dictionary, object_data[payload_start:token.start]
        index = max(next_index, index + 1)
    _pdf_limit_event(events, "DFIR-PDF-ENDSTREAM-UNAVAILABLE", "PDF stream has no bounded endstream token.")
    return dictionary, b""


def _pdf_filter_names(value: tuple[str, Any] | None) -> list[str]:
    if value is None:
        return []
    if value[0] == "name":
        return [str(value[1]).lstrip("/")]
    if value[0] == "array":
        names: list[str] = []
        index = 0
        while index < len(value[1]):
            parsed, end = _pdf_parse_value(value[1], index)
            if parsed[0] == "name":
                names.append(str(parsed[1]).lstrip("/"))
            index = max(end, index + 1)
        return names
    return []


def _decode_stream(
    object_data: bytes,
    *,
    objects: dict[tuple[int, int], bytes] | None = None,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> bytes:
    parts = _pdf_stream_parts(object_data, objects=objects, events=events)
    if parts is None:
        return b""
    dictionary, value = parts
    max_stream_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    if len(value) > max_stream_bytes:
        _pdf_limit_event(
            events,
            "DFIR-PDF-STREAM-BYTE-LIMIT",
            f"PDF stream exceeds the bounded decoder input limit ({len(value)} > {max_stream_bytes} bytes).",
        )
        return b""
    max_decoded_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    filters = _pdf_filter_names(_pdf_find_value(_pdf_dictionary_tokens(dictionary, limits=limits, events=events), "Filter"))
    if filters == ["FlateDecode"]:
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
    if filters == ["ASCIIHexDecode"]:
        try:
            compact = bytes(byte for byte in value if byte not in _PDF_WHITESPACE and byte != ord(">"))
            if compact.endswith(b">"):
                compact = compact[:-1]
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
    if filters:
        _pdf_limit_event(events, "DFIR-PDF-STREAM-FILTER-UNSUPPORTED", "PDF stream uses a filter outside the bounded adapter subset.")
        return b""
    return value


def _pdf_value_is_name(value: tuple[str, Any] | None, expected: str) -> bool:
    return value is not None and value[0] == "name" and str(value[1]).lstrip("/") == expected


def _pdf_array_references(
    value: tuple[str, Any] | None,
    *,
    max_count: int,
    unique: bool = True,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[int, int]]:
    if value is None or value[0] != "array":
        return []
    references: list[tuple[int, int]] = []
    index = 0
    tokens = value[1]
    while index < len(tokens):
        parsed, end = _pdf_parse_value(tokens, index)
        if parsed[0] == "ref":
            reference = parsed[1]
            if not unique or reference not in references:
                references.append(reference)
            if len(references) >= max_count:
                _pdf_limit_event(events, "DFIR-PDF-REFERENCE-LIMIT", "PDF indirect reference array reached the bounded parser limit.")
                return references[:max_count]
        index = max(end, index + 1)
    return references


def _pdf_page_tree(
    objects: dict[tuple[int, int], bytes],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve Catalog -> Pages -> Kids order and inherited page attributes."""

    catalog_ref: tuple[int, int] | None = None
    for object_ref, object_data in sorted(objects.items()):
        tokens = _pdf_dictionary_tokens(object_data, limits=limits, events=events)
        if _pdf_value_is_name(_pdf_find_value(tokens, "Type"), "Catalog"):
            catalog_ref = object_ref
            break
    root_ref: tuple[int, int] | None = None
    if catalog_ref is not None:
        catalog_tokens = _pdf_dictionary_tokens(objects[catalog_ref], limits=limits, events=events)
        pages_value = _pdf_find_value(catalog_tokens, "Pages")
        if pages_value is not None and pages_value[0] == "ref":
            root_ref = pages_value[1]
        else:
            _pdf_limit_event(events, "DFIR-PDF-PAGES-ROOT-UNAVAILABLE", "PDF Catalog has no bounded /Pages indirect reference.")
    else:
        _pdf_limit_event(events, "DFIR-PDF-CATALOG-UNAVAILABLE", "PDF has no bounded Catalog dictionary; page order is not authoritative.")

    pages: list[dict[str, Any]] = []
    visited: set[tuple[int, int]] = set()
    inheritable = ("MediaBox", "CropBox", "Rotate", "Resources")

    def walk(reference: tuple[int, int], inherited: dict[str, tuple[str, Any]]) -> None:
        if reference in visited:
            _pdf_limit_event(events, "DFIR-PDF-PAGE-TREE-CYCLE", f"PDF page tree repeats indirect object {reference[0]} {reference[1]} R; the repeated branch is omitted.")
            return
        visited.add(reference)
        object_data = objects.get(reference)
        if object_data is None:
            _pdf_limit_event(events, "DFIR-PDF-PAGE-TREE-REFERENCE-UNAVAILABLE", f"PDF page tree references unavailable object {reference[0]} {reference[1]} R.")
            return
        tokens = _pdf_dictionary_tokens(object_data, limits=limits, events=events)
        effective = dict(inherited)
        for key in inheritable:
            direct = _pdf_find_value(tokens, key)
            if direct is not None:
                effective[key] = direct
        type_value = _pdf_find_value(tokens, "Type")
        type_name = str(type_value[1]).lstrip("/") if type_value is not None and type_value[0] == "name" else None
        kids_value = _pdf_resolve_value(_pdf_find_value(tokens, "Kids"), objects, limits=limits, events=events)
        if type_name == "Pages" or kids_value is not None:
            kids = _pdf_array_references(
                kids_value,
                max_count=_pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_ANNOTATIONS_PER_PAGE),
                unique=False,
                events=events,
            )
            if not kids:
                _pdf_limit_event(events, "DFIR-PDF-PAGE-TREE-KIDS-UNAVAILABLE", f"PDF Pages node {reference[0]} {reference[1]} R has no bounded /Kids array.")
            for child in kids:
                walk(child, effective)
            return
        if type_name != "Page":
            _pdf_limit_event(events, "DFIR-PDF-PAGE-TREE-NODE-UNSUPPORTED", f"PDF page tree object {reference[0]} {reference[1]} R has no /Page or /Pages type.")
            return
        contents = _pdf_find_value(tokens, "Contents")
        media_box = effective.get("MediaBox")
        crop_box = effective.get("CropBox", media_box)
        if media_box is None:
            _pdf_limit_event(events, "DFIR-PDF-MEDIABOX-UNAVAILABLE", f"PDF page {reference[0]} {reference[1]} R has no inherited or direct /MediaBox.")
        pages.append({
            "object": reference,
            "dictionary": object_data,
            "tokens": tokens,
            "contents": contents,
            "mediaBox": _pdf_resolve_value(media_box, objects, limits=limits, events=events),
            "cropBox": _pdf_resolve_value(crop_box, objects, limits=limits, events=events),
            "rotate": _pdf_resolve_value(effective.get("Rotate", ("number", Decimal(0))), objects, limits=limits, events=events),
            "resources": _pdf_resolve_value(effective.get("Resources"), objects, limits=limits, events=events),
            "catalogObject": catalog_ref,
            "treeOrdinal": len(pages),
        })

    if root_ref is not None:
        walk(root_ref, {})
    if not pages:
        # Keep malformed/legacy PDFs observable, but do not pretend this is a
        # Catalog/Pages traversal. Valid PDFs use the branch above.
        for reference, object_data in sorted(objects.items()):
            tokens = _pdf_dictionary_tokens(object_data, limits=limits, events=events)
            if _pdf_value_is_name(_pdf_find_value(tokens, "Type"), "Page"):
                pages.append({
                    "object": reference,
                    "dictionary": object_data,
                    "tokens": tokens,
                    "contents": _pdf_find_value(tokens, "Contents"),
                    "mediaBox": _pdf_find_value(tokens, "MediaBox"),
                    "cropBox": _pdf_find_value(tokens, "CropBox") or _pdf_find_value(tokens, "MediaBox"),
                    "rotate": _pdf_find_value(tokens, "Rotate") or ("number", Decimal(0)),
                    "resources": _pdf_find_value(tokens, "Resources"),
                    "catalogObject": catalog_ref,
                    "treeOrdinal": len(pages),
                })
    return pages


def _page_content_sources(
    objects: dict[tuple[int, int], bytes],
    fallback: bytes,
    *,
    page_records: list[dict[str, Any]] | None = None,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    page_records = page_records if page_records is not None else _pdf_page_tree(objects, limits=limits, events=events)
    sources: list[dict[str, Any]] = []
    for page in page_records:
        contents = page.get("contents")
        source_status = "preserved"
        references: list[tuple[int, int]] = []
        if contents is not None and contents[0] == "ref":
            references = [contents[1]]
        elif contents is not None and contents[0] == "array":
            references = _pdf_array_references(
                contents,
                max_count=_pdf_budget(limits, "max_pdf_objects", _PDF_DEFAULT_MAX_ANNOTATIONS_PER_PAGE),
                unique=False,
                events=events,
            )
        elif contents is not None:
            _pdf_limit_event(events, "DFIR-PDF-CONTENTS-UNAVAILABLE", "PDF page /Contents is outside the bounded indirect-stream subset.")
            source_status = "unavailable"
        streams: list[dict[str, Any]] = []
        for reference in references:
            object_data = objects.get(reference)
            if object_data is None:
                _pdf_limit_event(events, "DFIR-PDF-CONTENTS-OBJECT-UNAVAILABLE", f"PDF page /Contents references unavailable object {reference[0]} {reference[1]} R.")
                source_status = "unavailable"
                continue
            stream_parts = _pdf_stream_parts(object_data, objects=objects, events=events)
            if stream_parts is None:
                _pdf_limit_event(events, "DFIR-PDF-CONTENTS-NOT-STREAM", f"PDF page /Contents object {reference[0]} {reference[1]} R is not a bounded stream.")
                source_status = "unavailable"
                continue
            decoded = _decode_stream(object_data, objects=objects, limits=limits, events=events)
            if stream_parts[1] and not decoded:
                source_status = "unavailable"
            streams.append({"object": reference, "data": decoded, "status": "unavailable" if stream_parts[1] and not decoded else "preserved"})
        if streams:
            joined: list[bytes] = []
            spans: list[dict[str, int]] = []
            offset = 0
            for stream_index, stream in enumerate(streams):
                if stream_index:
                    joined.append(b"\n")
                    offset += 1
                data = stream["data"]
                start = offset
                joined.append(data)
                offset += len(data)
                spans.append({"index": stream_index, "object": stream["object"][0], "start": start, "end": offset})
            sources.append({"data": b"".join(joined), "streamObjects": [item["object"] for item in streams], "streams": streams, "streamSpans": spans, "status": source_status})
        else:
            _pdf_limit_event(events, "DFIR-PDF-CONTENTS-UNAVAILABLE", "PDF page has no bounded /Contents stream; page metadata is not treated as source text.")
            sources.append({"data": b"", "streamObjects": [], "streams": [], "streamSpans": [], "status": "unavailable"})
    return sources


def _page_content_streams(
    objects: dict[tuple[int, int], bytes],
    fallback: bytes,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    """Return page streams while retaining the legacy private helper shape."""

    return [item["data"] for item in _page_content_sources(objects, fallback, limits=limits, events=events)]


def _pdf_lex_legacy(
    data: str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, Any]]:
    # Keep the historical private entry point safe for callers that still
    # reach it; the byte-aware lexer is the only implementation in use.
    return _pdf_lex(data, limits=limits, events=events)

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
            raw_bytes = _decode_pdf_literal_bytes("".join(value))
            if not append_token("string", _PDFText(raw_bytes.decode("latin-1"), raw_bytes)):
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
            raw = "".join(character for character in raw if character in "\x00\t\n\x0c\r ")
            try:
                raw_bytes = bytes.fromhex(raw + ("0" if len(raw) % 2 else ""))
                decoded = raw_bytes.decode("utf-16-be" if raw.lower().startswith("feff") else "latin-1", errors="replace")
            except ValueError:
                raw_bytes = raw.encode("latin-1", errors="replace")
                decoded = raw
            if not append_token("string", _PDFText(decoded, raw_bytes)):
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
        raw_data = data.encode("latin-1", errors="replace")
        number_end = _pdf_number_end(raw_data, index)
        if number_end is not None:
            raw = data[index:number_end]
            try:
                if not append_token("number", Decimal(raw)):
                    return tokens
            except InvalidOperation:
                if not append_token("word", raw):
                    return tokens
            index = number_end
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


def _pdf_lex(
    data: bytes | str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, Any]]:
    """Compatibility view of the byte-aware lexer."""

    return [(token.kind, token.value) for token in _pdf_tokenize(data, limits=limits, events=events)]


def _pdf_inline_image_expected_length(
    dictionary: list[tuple[str, Any]],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> int | None:
    """Return the encoded byte count for the common unfiltered BI subset."""

    def integer(key: str) -> int | None:
        value = _pdf_find_value(dictionary, key)
        if value is None or value[0] != "number" or not isinstance(value[1], Decimal) or value[1] != int(value[1]):
            return None
        result = int(value[1])
        return result if result >= 0 else None

    width = integer("W")
    height = integer("H")
    bits = integer("BPC") or 8
    color = _pdf_find_value(dictionary, "CS")
    color_name = _pdf_value_name(color)
    components = {"G": 1, "Gray": 1, "RGB": 3, "CMYK": 4, "Indexed": 1}.get(color_name or "RGB", 3)
    filters = _pdf_filter_names(_pdf_find_value(dictionary, "F") or _pdf_find_value(dictionary, "Filter"))
    if width is None or height is None or bits <= 0 or color_name is None or filters:
        return None
    if bits not in {1, 2, 4, 8, 16}:
        return None
    expected = ((width * bits * components + 7) // 8) * height
    max_bytes = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    if expected > max_bytes:
        _pdf_limit_event(events, "DFIR-PDF-INLINE-IMAGE-LENGTH-LIMIT", "PDF inline image sample length exceeds the bounded inline-image scan limit.")
        return None
    return expected


def _pdf_inline_image_end(
    data: bytes,
    id_end: int,
    dictionary: list[tuple[str, Any]],
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> int:
    """Find the bounded end of a BI/ID inline image without lexing samples."""

    payload_start = id_end
    while payload_start < len(data) and data[payload_start] in _PDF_WHITESPACE:
        payload_start += 1
    expected = _pdf_inline_image_expected_length(dictionary, limits=limits, events=events)
    if expected is not None:
        candidate, candidate_end = _pdf_read_token(data, payload_start + expected, limits=limits, events=events)
        if candidate is not None and candidate.kind == "word" and candidate.value == "EI":
            return candidate_end
    elif not _pdf_filter_names(_pdf_find_value(dictionary, "F") or _pdf_find_value(dictionary, "Filter")):
        _pdf_limit_event(events, "DFIR-PDF-INLINE-IMAGE-LENGTH-UNAVAILABLE", "PDF inline image dimensions or color space are unavailable; the adapter uses a bounded EI delimiter scan.")

    max_scan = _pdf_budget(limits, "max_input_bytes", _PDF_DEFAULT_MAX_STREAM_BYTES)
    scan_end = min(len(data), payload_start + max_scan)
    cursor = payload_start
    while cursor < scan_end:
        marker = data.find(b"EI", cursor, scan_end)
        if marker < 0:
            break
        before_ok = marker > payload_start and data[marker - 1] in _PDF_WHITESPACE
        after = marker + 2
        after_ok = after >= len(data) or data[after] in _PDF_WHITESPACE or data[after] in _PDF_DELIMITERS
        if before_ok and after_ok:
            return after
        cursor = marker + 2
    _pdf_limit_event(events, "DFIR-PDF-INLINE-IMAGE-END-UNAVAILABLE", "PDF inline image has no bounded EI delimiter; binary samples are not interpreted as operators.")
    return len(data)


def _pdf_operations_detailed(
    data: bytes | str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[_PDFOperation]:
    raw = _pdf_bytes(data)
    operations: list[_PDFOperation] = []
    operands: list[Any] = []
    arrays: list[list[Any]] = []
    tokens = _pdf_tokenize(raw, limits=limits, events=events)
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token.kind == "word" and token.value == "BI" and not arrays:
            id_index = token_index + 1
            while id_index < len(tokens):
                candidate = tokens[id_index]
                if candidate.kind == "word" and candidate.value == "ID":
                    break
                id_index += 1
            if id_index >= len(tokens):
                _pdf_limit_event(events, "DFIR-PDF-INLINE-IMAGE-ID-UNAVAILABLE", "PDF inline image has no bounded ID delimiter; its binary payload is not interpreted as content operators.")
                operations.append(_PDFOperation("BI", [], token.start, token.end))
                token_index += 1
                continue
            dictionary = [(item.kind, item.value) for item in tokens[token_index + 1:id_index]]
            image_end = _pdf_inline_image_end(raw, tokens[id_index].end, dictionary, limits=limits, events=events)
            operations.append(_PDFOperation("BI", [], token.start, image_end))
            token_index = id_index + 1
            while token_index < len(tokens) and tokens[token_index].start < image_end:
                token_index += 1
            operands = []
            continue
        if token.kind == "delimiter" and token.value == "[":
            if len(arrays) >= _PDF_DEFAULT_MAX_NESTING:
                _pdf_limit_event(events, "DFIR-PDF-TOKEN-NESTING-LIMIT", "PDF array nesting exceeded the bounded operator parser limit.")
                return operations
            arrays.append([])
        elif token.kind == "delimiter" and token.value == "]":
            if arrays:
                completed = arrays.pop()
                (arrays[-1] if arrays else operands).append(completed)
            else:
                _pdf_limit_event(events, "DFIR-PDF-ARRAY-UNEXPECTED-CLOSE", "PDF content stream contains an unexpected closing array delimiter.")
        elif token.kind in {"number", "string", "name"}:
            (arrays[-1] if arrays else operands).append(token.value)
        elif token.kind == "word":
            operations.append(_PDFOperation(str(token.value), list(operands), token.start, token.end))
            operands = []
        token_index += 1
    if arrays:
        _pdf_limit_event(events, "DFIR-PDF-ARRAY-UNTERMINATED", "PDF content stream ended with an unterminated operand array.")
    return operations


def _pdf_operations(
    data: str,
    *,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, list[Any]]]:
    return [(operation.operator, operation.operands) for operation in _pdf_operations_detailed(data, limits=limits, events=events)]

    # Retained below only as historical context for the old operand contract;
    # all callers use the byte-aware implementation above.
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


def _text_raw_bytes(value: Any) -> bytes:
    raw_bytes = getattr(value, "raw_bytes", None)
    if isinstance(raw_bytes, bytes):
        return raw_bytes
    if isinstance(value, bytes):
        return value
    return str(value).encode("latin-1", errors="replace")


def _text_character_codes(value: str | bytes) -> list[str]:
    """Return the source byte spelling available to this bounded lexer.

    The bounded parser does not claim to decode an arbitrary PDF composite
    font. It does, however, retain the byte spelling for the simple literal and
    hex-string lane so a registered ToUnicode map can be applied without
    replacing the authored source text.
    """

    return [f"{byte:02X}" for byte in _text_raw_bytes(value)]


def _source_character_codes(raw_bytes: bytes, font: dict[str, Any] | None) -> list[str]:
    """Split source bytes using the widths declared by the available CMap."""

    mapping = {
        str(item.get("sourceCode", "")).upper(): item.get("unicode", "")
        for item in (font or {}).get("mapping", [])
        if isinstance(item, dict) and isinstance(item.get("sourceCode"), str)
    }
    widths = sorted({len(code) // 2 for code in mapping if len(code) % 2 == 0 and code}, reverse=True)
    if not widths:
        preferred_width = (font or {}).get("codeByteWidth") if isinstance(font, dict) else None
        if not isinstance(preferred_width, int) or preferred_width <= 0:
            return _text_character_codes(raw_bytes)
        return [raw_bytes[offset:offset + preferred_width].hex().upper() for offset in range(0, len(raw_bytes), preferred_width)]
    default_width = widths[0] if len(set(widths)) == 1 else min(widths)
    codes: list[str] = []
    offset = 0
    while offset < len(raw_bytes):
        selected_width: int | None = None
        for width in widths:
            if offset + width <= len(raw_bytes):
                candidate = raw_bytes[offset:offset + width].hex().upper()
                if candidate in mapping:
                    selected_width = width
                    break
        if selected_width is None:
            selected_width = min(default_width, len(raw_bytes) - offset)
        codes.append(raw_bytes[offset:offset + selected_width].hex().upper())
        offset += selected_width
    return codes


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
    paint_events: list[dict[str, Any]] | None = None,
    stream_spans: list[dict[str, int]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    operations = _pdf_operations_detailed(data, limits=limits, events=events)
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
    supported = {"BT", "ET", "Tf", "Td", "TD", "Tm", "Tj", "TJ", "'", '"', "T*", "m", "l", "c", "v", "y", "h", "W", "W*", "n", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "q", "Q", "cm", "re", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh", "Do"}

    def source_info(operation: _PDFOperation) -> dict[str, int]:
        info = {"operatorIndex": operation_index, "operatorOffset": operation.start}
        for span in stream_spans or []:
            if span.get("start", 0) <= operation.start < span.get("end", 0):
                info["streamIndex"] = int(span.get("index", 0))
                info["streamObject"] = int(span.get("object", 0))
                info["streamOffset"] = operation.start - int(span.get("start", 0))
                break
        return info

    def finish_path(paint_operation: _PDFOperation | None = None) -> None:
        nonlocal current, clip_pending
        if current:
            record: dict[str, Any] = {"segments": current, "clipping": clip_pending, "painted": paint_operation is not None}
            if paint_operation is not None:
                record["operatorIndex"] = operation_index
            paths.append(record)
            if paint_operation is not None and paint_events is not None:
                paint_events.append({"kind": "path", "pathIndex": len(paths) - 1, **source_info(paint_operation)})
            current = []
            clip_pending = False
    graphics_operators = {"q", "Q", "cm", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    for operation_index, operation in enumerate(operations, start=1):
        operator, operands = operation.operator, operation.operands
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
            value = operands[-1]
            raw_bytes = _text_raw_bytes(value)
            texts.append(value)
            text_positions.append({"x": text_x, "y": text_y, "size": text_size, "font": current_font, "rawBytesHex": raw_bytes.hex().upper(), "characterCodes": _text_character_codes(raw_bytes), **source_info(operation)})
            if paint_events is not None:
                paint_events.append({"kind": "text", "textIndex": len(texts) - 1, **source_info(operation)})
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            text_items = [item for item in operands[-1] if isinstance(item, str)]
            value = "".join(text_items)
            if value:
                raw_bytes = b"".join(_text_raw_bytes(item) for item in text_items)
                texts.append(value)
                text_positions.append({"x": text_x, "y": text_y, "size": text_size, "font": current_font, "rawBytesHex": raw_bytes.hex().upper(), "characterCodes": _text_character_codes(raw_bytes), **source_info(operation)})
                if paint_events is not None:
                    paint_events.append({"kind": "text", "textIndex": len(texts) - 1, **source_info(operation)})
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
        elif operator == "Do":
            if operands and isinstance(operands[-1], str) and paint_events is not None:
                paint_events.append({"kind": "xobject", "name": operands[-1].lstrip("/"), "matrix": list(matrix), **source_info(operation)})
            elif not operands or not isinstance(operands[-1], str):
                unsupported.append("Do")
        elif operator == "n":
            # n terminates the current path without painting it.  A clipping
            # path is still retained as source geometry, but it is not a
            # paint-order event.
            finish_path()
        elif operator in {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}:
            finish_path(operation)
    finish_path()
    return texts, paths, unsupported, text_positions, graphics_states


def inspect(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF-"):
        raise AdapterError("input does not start with a PDF header")
    text = data.decode("latin-1", errors="replace")
    events: list[tuple[str, str]] = []
    objects = _pdf_objects(data, max_objects=limits.max_pdf_objects, events=events)
    xref_index = _pdf_xref_index(data, objects=objects, limits=limits, events=events)
    pages = _pdf_page_tree(objects, limits=limits, events=events)
    streams = sum(1 for object_data in objects.values() if _pdf_stream_parts(object_data, objects=objects, events=events) is not None)
    return {
        "format": "pdf",
        "version": text[5:8].split("\n", 1)[0],
        "bytes": len(data),
        "pages": len(pages),
        "streams": streams,
        "xref": {
            "valid": bool(xref_index.get("valid")),
            "classicXref": bool(xref_index.get("classicXref")),
            "xrefStream": bool(xref_index.get("xrefStream")),
            "objectStream": bool(xref_index.get("objectStream")),
            "incrementalRevisionCount": int(xref_index.get("incrementalRevisionCount", 0)),
        },
        "capabilities": ["pages", "text", "glyphs", "paths", "clipping", "paint-order", "bounded-observations"],
        "limits": {"maxInputBytes": limits.max_input_bytes, "maxPdfObjects": limits.max_pdf_objects},
    }


def _decode_literal(value: str) -> str:
    return _decode_pdf_literal_bytes(value).decode("latin-1")


def _stream_text(data: bytes | str) -> list[str]:
    values: list[str] = []
    for operator, operands in _pdf_operations(data):
        if operator in {"Tj", "'", '"'} and operands and isinstance(operands[-1], str):
            values.append(str(operands[-1]))
        elif operator == "TJ" and operands and isinstance(operands[-1], list):
            values.append("".join(str(value) for value in operands[-1] if isinstance(value, str)))
    return values


def _pdf_number_array(
    value: tuple[str, Any] | None,
    objects: dict[tuple[int, int], bytes],
) -> list[Decimal] | None:
    resolved = _pdf_resolve_value(value, objects)
    if resolved is None or resolved[0] != "array":
        return None
    numbers: list[Decimal] = []
    index = 0
    while index < len(resolved[1]):
        parsed, end = _pdf_parse_value(resolved[1], index)
        if parsed[0] != "number" or not isinstance(parsed[1], Decimal):
            return None
        numbers.append(parsed[1])
        index = max(end, index + 1)
    return numbers


def _coordinate(builder: DocumentBuilder, page_number: int, page: dict[str, Any] | None = None, objects: dict[tuple[int, int], bytes] | None = None) -> str:
    space_id = safe_id("space", f"pdf-page-{page_number}")
    if builder.find("coordinateSpaces", "coordinateSpaceId", space_id) is None:
        item: dict[str, Any] = {"coordinateSpaceId": space_id, "unit": "pt", "origin": {"x": "0", "y": "0"}}
        if page is not None and objects is not None:
            crop = _pdf_number_array(page.get("cropBox"), objects)
            rotate_value = _pdf_resolve_value(page.get("rotate"), objects)
            rotate = int(rotate_value[1]) % 360 if rotate_value is not None and rotate_value[0] == "number" and rotate_value[1] == int(rotate_value[1]) else 0
            if crop is not None and len(crop) == 4 and rotate in {90, 180, 270}:
                width = crop[2] - crop[0]
                height = crop[3] - crop[1]
                matrices = {
                    90: (Decimal(0), Decimal(1), Decimal(-1), Decimal(0), height, Decimal(0)),
                    180: (Decimal(-1), Decimal(0), Decimal(0), Decimal(-1), width, height),
                    270: (Decimal(0), Decimal(-1), Decimal(1), Decimal(0), Decimal(0), width),
                }
                matrix = matrices[rotate]
                item["transformToParent"] = {key: decimal(value) for key, value in zip(("a", "b", "c", "d", "e", "f"), matrix)}
        builder.add_item("coordinateSpaces", item, "coordinateSpaceId")
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


def _pdf_dictionary_bytes(
    object_data: bytes,
    *,
    objects: dict[tuple[int, int], bytes] | None = None,
    events: list[tuple[str, str]] | None = None,
) -> bytes:
    parts = _pdf_stream_parts(object_data, objects=objects, events=events)
    return object_data if parts is None else parts[0]


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
    objects: dict[tuple[int, int], bytes] | None = None,
    limits: AdapterLimits | None = None,
    events: list[tuple[str, str]] | None = None,
) -> list[tuple[str, Any]]:
    dictionary = _pdf_dictionary_bytes(object_data, objects=objects, events=events)
    return _pdf_lex(dictionary, limits=limits, events=events)


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
    tokens = _pdf_dictionary_tokens(object_data, objects=objects, limits=limits, events=events)
    if not tokens:
        return ("missing", value[1])
    return _pdf_parse_value(tokens, 0)[0]


def _pdf_reference_values(
    value: tuple[str, Any] | None,
    *,
    max_count: int,
    unique: bool = True,
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
            if not unique or reference not in references:
                references.append(reference)
            if len(references) >= max_count:
                _pdf_limit_event(events, "DFIR-PDF-REFERENCE-LIMIT", "PDF indirect reference count reached the bounded page limit; additional references are not parsed.")
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


def _pdf_annotation_geometry(
    tokens: list[tuple[str, Any]],
    objects: dict[tuple[int, int], bytes],
) -> dict[str, list[str]]:
    """Preserve bounded annotation geometry in canonical decimal spelling."""

    geometry: dict[str, list[str]] = {}
    rect = _pdf_number_array(_pdf_find_value(tokens, "Rect"), objects)
    if rect is not None and len(rect) == 4:
        geometry["rect"] = [decimal(value) for value in rect]
    quad_points = _pdf_number_array(_pdf_find_value(tokens, "QuadPoints"), objects)
    if quad_points is not None and len(quad_points) == 8:
        geometry["quadPoints"] = [decimal(value) for value in quad_points]
    return geometry


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
        annotation_type = _pdf_value_name(_pdf_find_value(tokens, "Type"))
        if annotation_type is None:
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-TYPE-UNAVAILABLE",
                f"PDF annotation reference {reference_id} has no bounded /Type /Annot dictionary identity; no annotation is fabricated from marker-like keys.",
                phase="parse",
                target_id=page_id,
            )
            builder.add_feature("annotation", "unavailable", target_id=page_id, diagnostic_ids=[diagnostic])
            continue
        if annotation_type != "Annot":
            diagnostic = builder.add_diagnostic(
                "DFIR-PDF-ANNOTATION-TYPE-UNSUPPORTED",
                f"PDF /Annots reference {reference_id} resolves to /Type /{annotation_type}, not an annotation dictionary; it is not relabeled.",
                phase="normalize",
                target_id=page_id,
            )
            builder.add_feature("annotation", "unsupported", target_id=page_id, diagnostic_ids=[diagnostic])
            continue
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

        unsupported_subtype_diagnostic: str | None = None
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
            # The common IR has no generic annotation kind.  Keep the source
            # subtype on the bounded comment bucket so the occurrence remains
            # observable without pretending that (for example) Highlight is a
            # Text or FreeText PDF subtype.
            kind = "comment"
            unsupported_subtype_diagnostic = diagnostic

        status = "unsupported" if unsupported_subtype_diagnostic else "preserved"
        body: str | None = None
        diagnostic_ids: list[str] = [unsupported_subtype_diagnostic] if unsupported_subtype_diagnostic else []
        action_fact: dict[str, Any] | None = None
        destination_fact: str | None = None

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
                            action_fact = {"kind": "URI", "target": body_value}
                            destination_fact = body_value
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
                            action_fact = {"kind": "GoTo", "target": body_value}
                            destination_fact = body_value
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
                            action_fact = {"kind": "GoToR", "target": destination_text or file_text}
                            destination_fact = destination_text or file_text
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
                        action_target = _pdf_scalar_text(_pdf_find_value(action_value[1], "JS"), objects, limits=limits, events=events)
                        if action_target:
                            action_fact = {"kind": action_name, "target": action_target}
                            destination_fact = action_target
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
                    action_fact = {"kind": "destination", "target": destination_text}
                    destination_fact = destination_text
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
            if field_name:
                action_fact = {"kind": "field", "name": field_name}
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
            "sourceSubtype": subtype,
            "anchor": {
                "kind": "page",
                "surfaceId": safe_id("surface", f"pdf-page-{page_number}"),
            },
            "status": status,
        }
        geometry = _pdf_annotation_geometry(tokens, objects)
        if geometry:
            item["geometry"] = geometry
        if action_fact is not None:
            item["action"] = action_fact
        if destination_fact is not None:
            item["destination"] = destination_fact
        if subtype == "Widget":
            item["range"] = {"start": reference_id, "end": reference_id, "balanced": True}
            if field_name:
                builder.add_item(
                    "fields",
                    {
                        "fieldId": safe_id("field", f"pdf-field-{page_number}-{object_number}-{generation}"),
                        "kind": "form",
                        "instruction": field_name,
                        "owner": f"{object_number} {generation} obj",
                        "referenceId": reference_id,
                        "range": {"begin": reference_id, "end": reference_id, "balanced": True},
                        "status": status,
                    },
                    "fieldId",
                )
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
    source_locator: dict[str, Any] | None = None,
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
    # The glyph/text entity is source-preserved; only its renderer-dependent
    # geometry is approximated. Keeping these statuses separate prevents a
    # geometry observation from being mistaken for a text-mapping claim.
    builder.add_node("glyph", glyph_id, parent_id=page_id, textIds=[text_id], geometryId=geometry_id, status="preserved")
    locator = {"kind": "pdf", "page": page_number}
    if isinstance(source_locator, dict):
        for key in ("object", "operator"):
            value_at_locator = source_locator.get(key)
            if isinstance(value_at_locator, int) and value_at_locator >= 0:
                locator[key] = value_at_locator
    locator_status = "exact" if isinstance(locator.get("object"), int) and isinstance(locator.get("operator"), int) else "unavailable"
    builder.add_source_map(glyph_id, {key: value_at_locator for key, value_at_locator in locator.items() if key != "kind"})
    _observation(builder, "renderer", glyph_id, geometry_id=geometry_id)
    _observation(builder, "ocr", glyph_id, geometry_id=geometry_id)
    font_by_resource = font_by_resource or {}
    position_font = position.get("font")
    font_resource = position_font if isinstance(position_font, str) else ""
    font = font_by_resource.get(font_resource)
    raw_bytes: bytes
    raw_hex = position.get("rawBytesHex")
    if isinstance(raw_hex, str):
        try:
            raw_bytes = bytes.fromhex(raw_hex)
        except ValueError:
            raw_bytes = _text_raw_bytes(value)
    else:
        raw_bytes = _text_raw_bytes(value)
    character_codes = _source_character_codes(raw_bytes, font)
    mapping = {item.get("sourceCode", "").upper(): item.get("unicode", "") for item in (font or {}).get("mapping", []) if isinstance(item, dict)}
    mapped_values = [mapping.get(code, "") for code in character_codes]
    if not character_codes or font is None:
        mapping_status = "unsupported"
    elif mapping and all(mapped_values):
        mapping_status = "preserved"
    else:
        mapping_status = "unavailable"
    unicode_value = "".join(mapped_values) if mapping_status == "preserved" else ""
    character_code = character_codes[0] if character_codes else ""
    font_object = ""
    if isinstance(font, dict) and isinstance(font.get("object"), tuple):
        object_number, generation = font["object"]
        font_object = f"{object_number} {generation} R"
    glyph_name = ""
    if character_code and isinstance(font, dict):
        try:
            glyph_name = str(font.get("glyphNames", {}).get(int(character_code, 16), ""))
        except (TypeError, ValueError):
            glyph_name = ""
    identity_status = "preserved" if font is not None else "unsupported"
    glyph_identity = {
        "kind": "font-character-code",
        "status": identity_status,
        "fontResource": font_resource,
        "fontObject": font_object,
        "baseFont": str(font.get("baseFont", "")) if isinstance(font, dict) else "",
        "sourceCodes": character_codes,
        "glyphName": glyph_name,
    }
    if isinstance(font, dict) and font.get("toUnicodeObject") is not None:
        cmap_number, cmap_generation = font["toUnicodeObject"]
        mapping_source = {"kind": "ToUnicode", "object": f"{cmap_number} {cmap_generation} R"}
    else:
        mapping_source = {"kind": "unavailable"}
    mapped_codes = [code for code, mapped in zip(character_codes, mapped_values) if mapped]
    unmapped_codes = [code for code, mapped in zip(character_codes, mapped_values) if not mapped]
    mapping_diagnostic_ids: list[str] = []
    if mapping_status == "unsupported":
        mapping_diagnostic_ids.append(builder.add_diagnostic(
            "DFIR-PDF-GLYPH-MAPPING-UNSUPPORTED",
            f"PDF glyph text mapping is outside the bounded font/resource subset for source codes {character_codes!r}; Unicode is unavailable.",
            phase="normalize",
            target_id=glyph_id,
        ))
    elif mapping_status == "unavailable":
        diagnostic_code = "DFIR-PDF-GLYPH-CMAP-PARTIAL" if mapped_codes else "DFIR-PDF-GLYPH-MAPPING-UNAVAILABLE"
        mapping_diagnostic_ids.append(builder.add_diagnostic(
            diagnostic_code,
            f"PDF ToUnicode mapping does not resolve every source character code {character_codes!r}; Unicode is not fabricated.",
            phase="normalize",
            target_id=glyph_id,
        ))
    if locator_status != "exact":
        mapping_diagnostic_ids.append(builder.add_diagnostic(
            "DFIR-PDF-GLYPH-SOURCE-LOCATOR-UNAVAILABLE",
            "The bounded PDF parser could not identify an exact content-stream operator for this glyph occurrence.",
            phase="parse",
            target_id=glyph_id,
        ))
    extension_id = safe_id("extension", f"pdf-glyph-provenance-{page_number}-{fragment}")
    _extension(
        builder,
        glyph_id,
        "glyph-provenance",
        {
            "characterCode": character_code,
            "characterCodes": character_codes,
            "fontResource": font_resource,
            "fontObject": font_object,
            "encoding": str(font.get("encoding", "")) if isinstance(font, dict) else "",
            "glyphIdentity": glyph_identity,
            "glyphName": glyph_name,
            "unicode": unicode_value,
            "mappingStatus": mapping_status,
            "mappingSource": mapping_source,
            "mappingCoverage": {"mappedCharacterCodes": mapped_codes, "unmappedCharacterCodes": unmapped_codes},
            "sourceLocator": locator,
            "sourceLocatorStatus": locator_status,
            "provenance": {
                "rawStringBytes": "pdf-content-stream",
                "characterCodes": "pdf-content-stream",
                "glyphIdentity": "font-resource-and-character-code",
                "unicode": "ToUnicode" if mapping_status == "preserved" else "unavailable",
            },
        },
        extension_id=extension_id,
    )
    builder.add_feature("glyph-text-mapping", mapping_status, target_id=glyph_id, diagnostic_ids=mapping_diagnostic_ids)
    builder.add_feature("text-glyph", "approximated", target_id=glyph_id, diagnostic_ids=[approximation_diagnostic])
    return glyph_id


def _pdf_scalar_number(value: tuple[str, Any] | None, objects: dict[tuple[int, int], bytes]) -> Decimal | None:
    resolved = _pdf_resolve_value(value, objects)
    return resolved[1] if resolved is not None and resolved[0] == "number" and isinstance(resolved[1], Decimal) else None


def _add_pdf_image(
    builder: DocumentBuilder,
    page_id: str,
    page_number: int,
    event: dict[str, Any],
    xobject: dict[str, Any],
    space_id: str,
    objects: dict[tuple[int, int], bytes],
) -> str:
    reference = xobject.get("object")
    object_number, generation = reference if isinstance(reference, tuple) else (0, 0)
    image_key = f"pdf-image-{page_number}-{object_number}-{generation}-{event.get('operatorIndex', 0)}"
    image_id = safe_id("node", image_key)
    geometry_id = safe_id("geometry", image_key)
    resource_id = safe_id("resource", f"pdf-image-{object_number}-{generation}")
    if builder.find("resources", "resourceId", resource_id) is None:
        builder.add_item(
            "resources",
            {
                "resourceId": resource_id,
                "kind": "image",
                "mediaType": "application/octet-stream",
                "availability": "available",
                "derivedHandle": f"object:{object_number} {generation}",
            },
            "resourceId",
        )
    dictionary = xobject.get("dictionary", [])
    width = _pdf_scalar_number(_pdf_find_value(dictionary, "Width"), objects)
    height = _pdf_scalar_number(_pdf_find_value(dictionary, "Height"), objects)
    diagnostics: list[str] = []
    if width is None or height is None:
        diagnostics.append(builder.add_diagnostic(
            "DFIR-PDF-IMAGE-DIMENSIONS-UNAVAILABLE",
            f"PDF image XObject {object_number} {generation} R has no bounded numeric /Width and /Height.",
            phase="parse",
            target_id=page_id,
        ))
        width, height = width or Decimal(0), height or Decimal(0)
    builder.add_item(
        "geometries",
        {
            "geometryId": geometry_id,
            "spaceId": space_id,
            "kind": "rectangle",
            "primitives": [{"kind": "rectangle", "x": "0", "y": "0", "width": {"value": decimal(width), "unit": "pt"}, "height": {"value": decimal(height), "unit": "pt"}}],
            "transform": {key: decimal(value) for key, value in zip(("a", "b", "c", "d", "e", "f"), event.get("matrix", [Decimal(1), Decimal(0), Decimal(0), Decimal(1), Decimal(0), Decimal(0)]) )},
            "status": "approximated" if diagnostics else "preserved",
        },
        "geometryId",
    )
    builder.add_node("image", image_id, parent_id=page_id, geometryId=geometry_id, resourceIds=[resource_id], status="preserved")
    locator = {"page": page_number, "object": int(object_number), "operator": int(event.get("operatorIndex", 0))}
    builder.add_source_map(image_id, locator)
    if diagnostics:
        builder.add_feature("image-xobject", "unavailable", target_id=image_id, diagnostic_ids=diagnostics)
    else:
        builder.add_feature("image-xobject", "preserved", target_id=image_id)
    return image_id


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
        limit_events: list[tuple[str, str]] = []
        objects = _pdf_objects(raw, max_objects=limits.max_pdf_objects, events=limit_events)
        xref_index = _pdf_xref_index(raw, objects=objects, limits=limits, events=limit_events)
        _pdf_expand_object_streams(objects, xref_index, limits=limits, events=limit_events)
        if xref_index.get("objectStream") and any(
            isinstance(entry, dict) and entry.get("type") == 2 and not entry.get("bodyAvailable")
            for entry in xref_index.get("entries", {}).values()
        ):
            _pdf_limit_event(limit_events, "DFIR-PDF-OBJECT-STREAM-UNSUPPORTED", "One or more xref-stream object bodies could not be materialized within the bounded object-stream parser.")
        page_records = _pdf_page_tree(objects, limits=limits, events=limit_events)
        page_count = len(page_records)
        if page_count == 0:
            _emit_pdf_limit_events(builder, limit_events, target_id=builder.root_id)
            diagnostic = builder.add_diagnostic("DFIR-PDF-PAGE-MISSING", "PDF contains no page object", severity="error", phase="parse")
            builder.add_feature("pages", "failed", diagnostic_ids=[diagnostic])
            return builder.finish(status="failed")
        document_part = safe_id("part", "pdf-document")
        builder.add_item("parts", {"partId": document_part, "kind": "document", "name": "PDF document", "storyType": "document", "rootNodeIds": [builder.root_id], "relationshipIds": [], "status": "preserved"}, "partId")
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
            for target_object in _pdf_references(object_data, objects=objects):
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
                                "packagePresence": False,
                                "rawPayloadAvailable": False,
                                "decodability": "not-decodable",
                                "embeddedOrExternal": "embedded",
                                "networkAvailability": "not-applicable",
                            },
                            "resourceId",
                        )
                    relation_status = "unavailable"
                relation_id = safe_id("relation", f"pdf-object-{source_object[0]}-{source_object[1]}-{target_object[0]}-{target_object[1]}")
                builder.add_item(
                    "relations",
                    {
                        "relationId": relation_id,
                        "kind": "references",
                        "fromId": source_part_id,
                        "toId": target_id,
                        "sourceOccurrenceId": _pdf_source_occurrence_id(source_object, target_object, objects),
                        "type": "indirect-reference",
                        "target": f"{target_object[0]} {target_object[1]} obj" if target_object in object_part_ids else f"{target_object[0]} {target_object[1]} R",
                        "targetMode": "internal",
                        "status": relation_status,
                    },
                    "relationId",
                )
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
        if xref_index.get("valid"):
            builder.add_feature("pdf-xref", "preserved", target_id=document_part)
        else:
            xref_diagnostic = builder.add_diagnostic(
                "DFIR-PDF-XREF-UNAVAILABLE",
                "PDF object availability is not xref-authenticated; scanner-derived objects remain observable but are not promoted by this fact.",
                phase="parse",
                target_id=document_part,
            )
            builder.add_feature("pdf-xref", "unavailable", target_id=document_part, diagnostic_ids=[xref_diagnostic])
        font_mappings = _pdf_font_mappings(objects, limits=limits, events=limit_events)
        font_resource_ids: dict[tuple[int, int], str] = {}
        for font in font_mappings:
            object_number, generation = font["object"]
            font_object_name = f"{object_number} {generation}"
            resource_id = safe_id("resource", f"pdf-font-{object_number}-{generation}")
            font_resource_ids[(object_number, generation)] = resource_id
            raw_payload_available = _pdf_font_raw_payload_available(objects[(object_number, generation)], objects, limits=limits, events=limit_events)
            xref_entry = xref_index.get("entries", {}).get((object_number, generation), {})
            xref_object_available = bool(xref_entry.get("verified") and xref_entry.get("bodyAvailable", True))
            builder.add_item(
                "resources",
                {
                    "resourceId": resource_id,
                    "kind": "font",
                    "mediaType": "application/x-font",
                    # Resource-object availability is an xref-authenticated
                    # source fact.  Embedded font bytes remain a separate
                    # rawPayloadAvailable fact and are not fabricated here.
                    "availability": "available" if raw_payload_available or xref_object_available else "unavailable",
                    "derivedHandle": f"object:{font_object_name}",
                    "packagePresence": True,
                    "rawPayloadAvailable": raw_payload_available,
                    "decodability": "not-decodable",
                    "embeddedOrExternal": "embedded",
                    "networkAvailability": "not-applicable",
                },
                "resourceId",
            )
            extension_id = safe_id("extension", f"pdf-font-cmap-{object_number}-{generation}")
            _extension(
                builder,
                builder.root_id,
                "font-cmap",
                {
                    "fontObject": font_object_name,
                    "mappingStatus": font["mappingStatus"],
                    "mappings": font["mapping"],
                    "sourceLocator": {"kind": "pdf", "object": object_number},
                    "provenance": {
                        "mappingSource": "ToUnicode" if font.get("toUnicodeObject") is not None else "unavailable",
                        "rawBytes": "pdf-tounicode-stream" if font.get("toUnicodeObject") is not None else "unavailable",
                    },
                },
                extension_id=extension_id,
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
        for page in page_records:
            page_reference = page.get("object")
            if not isinstance(page_reference, tuple) or page_reference not in object_part_ids:
                continue
            page_part_id = object_part_ids[page_reference]
            for font_reference in sorted(set(_pdf_resource_entries(page.get("resources"), "Font", objects).values())):
                resource_id = font_resource_ids.get(font_reference)
                if resource_id is None:
                    resource_id = safe_id("resource", f"pdf-missing-object-{font_reference[0]}-{font_reference[1]}")
                    if builder.find("resources", "resourceId", resource_id) is None:
                        builder.add_item(
                            "resources",
                            {
                                "resourceId": resource_id,
                                "kind": "embeddedObject",
                                "mediaType": "application/pdf-object",
                                "availability": "unavailable",
                                "derivedHandle": f"{font_reference[0]} {font_reference[1]} R",
                                "packagePresence": False,
                                "rawPayloadAvailable": False,
                                "decodability": "not-decodable",
                                "embeddedOrExternal": "embedded",
                                "networkAvailability": "not-applicable",
                            },
                            "resourceId",
                        )
                resource = builder.find("resources", "resourceId", resource_id)
                relation_id = safe_id("relation", f"pdf-resource-use-{page_reference[0]}-{page_reference[1]}-{font_reference[0]}-{font_reference[1]}")
                builder.add_item(
                    "relations",
                    {
                        "relationId": relation_id,
                        "kind": "usesResource",
                        "fromId": page_part_id,
                        "toId": resource_id,
                        "sourceOccurrenceId": _pdf_source_occurrence_id(page_reference, font_reference, objects),
                        "type": "indirect-reference",
                        "target": f"{font_reference[0]} {font_reference[1]} R",
                        "targetMode": "internal",
                        "status": "preserved" if resource is not None else "unavailable",
                    },
                    "relationId",
                )
                page_part = builder.find("parts", "partId", page_part_id)
                if page_part is not None:
                    page_part.setdefault("relationshipIds", []).append(relation_id)
            for xobject_name, xobject_reference in sorted(_pdf_resource_entries(page.get("resources"), "XObject", objects).items()):
                xobject_data = objects.get(xobject_reference)
                xobject_tokens = _pdf_dictionary_tokens(xobject_data, objects=objects, limits=limits, events=limit_events) if xobject_data is not None else []
                if not _pdf_value_is_name(_pdf_find_value(xobject_tokens, "Subtype"), "Image"):
                    continue
                resource_id = safe_id("resource", f"pdf-image-{xobject_reference[0]}-{xobject_reference[1]}")
                resource = builder.find("resources", "resourceId", resource_id)
                if resource is None:
                    resource = builder.add_item(
                        "resources",
                        {
                            "resourceId": resource_id,
                            "kind": "image",
                            "mediaType": "application/octet-stream",
                            "availability": "available",
                            "derivedHandle": f"object:{xobject_reference[0]} {xobject_reference[1]}",
                            "packagePresence": True,
                            "rawPayloadAvailable": True,
                            "decodability": "not-decodable",
                            "embeddedOrExternal": "embedded",
                            "networkAvailability": "not-applicable",
                        },
                        "resourceId",
                    )
                relation_id = safe_id("relation", f"pdf-resource-use-xobject-{page_reference[0]}-{page_reference[1]}-{xobject_reference[0]}-{xobject_reference[1]}")
                builder.add_item(
                    "relations",
                    {
                        "relationId": relation_id,
                        "kind": "usesResource",
                        "fromId": page_part_id,
                        "toId": resource_id,
                        "sourceOccurrenceId": _pdf_source_occurrence_id(page_reference, xobject_reference, objects),
                        "type": "indirect-reference",
                        "target": f"{xobject_reference[0]} {xobject_reference[1]} R",
                        "targetMode": "internal",
                        "status": "preserved" if xobject_data is not None else "unavailable",
                    },
                    "relationId",
                )
                page_part = builder.find("parts", "partId", page_part_id)
                if page_part is not None:
                    page_part.setdefault("relationshipIds", []).append(relation_id)
        page_sources = _page_content_sources(objects, raw, page_records=page_records, limits=limits, events=limit_events)
        pages_seen = 0
        draw_order_items: list[dict[str, Any]] = []
        draw_order_ambiguous = False
        reading_order_ids: list[str] = []
        page_tree_order_items: list[dict[str, Any]] = []
        for page_index in range(page_count):
            pages_seen += 1
            page = page_records[pages_seen - 1]
            page_id = safe_id("node", f"pdf-page-{pages_seen}")
            surface_id = safe_id("surface", f"pdf-page-{pages_seen}")
            space_id = _coordinate(builder, pages_seen, page, objects)
            page_reference = page.get("object", (0, 0))
            rotation_value = _pdf_resolve_value(page.get("rotate"), objects, limits=limits, events=limit_events)
            rotation = (
                int(rotation_value[1]) % 360
                if rotation_value is not None and rotation_value[0] == "number" and isinstance(rotation_value[1], Decimal) and rotation_value[1] == int(rotation_value[1])
                else 0
            )
            media_box_values = _pdf_number_array(page.get("mediaBox"), objects)
            crop_box_values = _pdf_number_array(page.get("cropBox"), objects)
            surface: dict[str, Any] = {
                "surfaceId": surface_id,
                "partId": document_part,
                "kind": "page",
                "ordinal": pages_seen - 1,
                "pageTreeIndex": int(page.get("treeOrdinal", pages_seen - 1)),
                "sourceObject": f"{page_reference[0]} {page_reference[1]}",
                "rotation": rotation,
                "coordinateSpaceId": space_id,
                "status": "preserved",
            }
            if media_box_values is not None and len(media_box_values) == 4:
                surface["mediaBox"] = [decimal(value) for value in media_box_values]
            if crop_box_values is not None and len(crop_box_values) == 4:
                surface["cropBox"] = [decimal(value) for value in crop_box_values]
            builder.add_item("surfaces", surface, "surfaceId")
            builder.add_node("section", page_id, parent_id=builder.root_id, part_id=document_part, status="preserved")
            page_tree_order_items.append({"id": page_id, "ordinal": pages_seen - 1})
            builder.add_source_map(page_id, {"page": pages_seen, "object": int(page_reference[0])})
            page_object = page.get("dictionary", b"")
            page_source = page_sources[pages_seen - 1] if pages_seen <= len(page_sources) else {"data": b"", "streamObjects": [], "streamSpans": [], "status": "unavailable"}
            if page_source.get("status") not in {None, "preserved"}:
                draw_order_ambiguous = True
            page_text = page_source["data"]
            content_paint_events: list[dict[str, Any]] = []
            fragments, parsed_paths, unsupported_operators, text_positions, graphics_states = _interpret_content(
                page_text,
                limits=limits,
                events=limit_events,
                paint_events=content_paint_events,
                stream_spans=page_source.get("streamSpans", []),
            )
            if not fragments:
                fragments = _stream_text(page_text)
                for fallback_index in range(len(fragments)):
                    content_paint_events.append({"kind": "text", "textIndex": fallback_index, "operatorIndex": fallback_index + 1})
            page_font_by_resource = _pdf_page_font_resource_map(page, objects, font_mappings)
            text_node_ids: list[str] = []
            for fragment, value in enumerate(fragments, start=1):
                position = text_positions[fragment - 1] if fragment <= len(text_positions) else None
                source_locator: dict[str, Any] = {"page": pages_seen}
                stream_objects = page_source.get("streamObjects", [])
                if isinstance(position, dict) and isinstance(position.get("streamObject"), int):
                    source_locator["object"] = position["streamObject"]
                elif len(stream_objects) == 1 and isinstance(stream_objects[0], tuple):
                    source_locator["object"] = stream_objects[0][0]
                if isinstance(position, dict) and isinstance(position.get("operatorIndex"), int):
                    source_locator["operator"] = position["operatorIndex"]
                text_node_ids.append(_add_text(builder, page_id, value, pages_seen, fragment, space_id, position, page_font_by_resource, source_locator))
                reading_order_ids.append(text_node_ids[-1])
            for state_index, state_record in enumerate(graphics_states, start=1):
                extension_id = safe_id("extension", f"pdf-graphics-state-{pages_seen}-{state_index}")
                _extension(
                    builder,
                    page_id,
                    "graphics-state",
                    {
                        "page": pages_seen,
                        "operator": state_record["operator"],
                        "operands": state_record["operands"],
                        "state": state_record["state"],
                    },
                    extension_id=extension_id,
                )
            if graphics_states:
                builder.add_feature("graphics-state", "preserved", target_id=page_id)
            if unsupported_operators:
                draw_order_ambiguous = True
                diagnostic = builder.add_diagnostic("DFIR-PDF-OPERATOR-UNSUPPORTED", f"PDF operators are not interpreted: {', '.join(sorted(set(unsupported_operators)))}", phase="normalize", target_id=page_id)
                builder.add_feature("unsupported-operator", "unsupported", target_id=page_id, diagnostic_ids=[diagnostic])
            page_path_ids: list[str] = []
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
                operator_index = path_record.get("operatorIndex") if isinstance(path_record.get("operatorIndex"), int) else path_index
                builder.add_source_map(path_id, {"page": pages_seen, "operator": operator_index})
                builder.add_feature("clipping" if geometry_kind == "clippingPath" else "path", geometry_status, target_id=path_id)
                page_path_ids.append(path_id)
                reading_order_ids.append(path_id)
            page_xobjects = _pdf_page_xobject_resource_map(page, objects)
            image_node_ids: dict[int, str] = {}
            unsupported_xobjects: list[str] = []
            for event in content_paint_events:
                if event.get("kind") != "xobject":
                    continue
                name = str(event.get("name", ""))
                xobject = page_xobjects.get(name)
                if xobject is None:
                    unsupported_xobjects.append(name or "<unnamed>")
                    continue
                if xobject.get("subtype") == "Image":
                    image_node_ids[int(event.get("operatorIndex", 0))] = _add_pdf_image(builder, page_id, pages_seen, event, xobject, space_id, objects)
                    reading_order_ids.append(image_node_ids[int(event.get("operatorIndex", 0))])
                else:
                    unsupported_xobjects.append(f"{name}({xobject.get('subtype') or 'unknown'})")
            if unsupported_xobjects:
                draw_order_ambiguous = True
                diagnostic = builder.add_diagnostic(
                    "DFIR-PDF-XOBJECT-UNSUPPORTED",
                    f"PDF content references XObject resources outside the bounded image subset: {', '.join(sorted(set(unsupported_xobjects)))}.",
                    phase="normalize",
                    target_id=page_id,
                )
                builder.add_feature("xobject", "unsupported", target_id=page_id, diagnostic_ids=[diagnostic])
                operator_diagnostic = builder.add_diagnostic(
                    "DFIR-PDF-OPERATOR-UNSUPPORTED",
                    f"PDF Do operators reference XObjects outside the bounded image subset: {', '.join(sorted(set(unsupported_xobjects)))}.",
                    phase="normalize",
                    target_id=page_id,
                )
                builder.add_feature("unsupported-operator", "unsupported", target_id=page_id, diagnostic_ids=[operator_diagnostic])
            _add_pdf_annotations(builder, page_id, pages_seen, page_object, objects, limits=limits, events=limit_events)
            page_order: list[dict[str, Any]] = []
            for ordinal, event in enumerate(content_paint_events):
                target_id: str | None = None
                if event.get("kind") == "text":
                    index = int(event.get("textIndex", -1))
                    if 0 <= index < len(text_node_ids):
                        target_id = text_node_ids[index]
                elif event.get("kind") == "path":
                    index = int(event.get("pathIndex", -1))
                    if 0 <= index < len(page_path_ids):
                        target_id = page_path_ids[index]
                elif event.get("kind") == "xobject":
                    target_id = image_node_ids.get(int(event.get("operatorIndex", 0)))
                if target_id is not None:
                    page_order.append({"id": target_id, "ordinal": ordinal})
                    draw_order_items.append({"id": target_id, "ordinal": len(draw_order_items)})
            builder.add_item(
                "orders",
                {
                    "orderId": safe_id("order", f"pdf-paint-page-{pages_seen}"),
                    "kind": "draw",
                    "ownerId": page_id,
                    "items": page_order,
                    "context": "PDF content-stream source paint order",
                    "status": "preserved" if not unsupported_operators and len(page_order) == len(content_paint_events) else "ambiguous",
                },
                "orderId",
            )
            # A page with bounded content bytes has page/operator source
            # occurrences and can carry an observation disposition directly.
            # When bounded content is unavailable, the source occurrence
            # inventory cannot name a page operator without inventing one.
            # Bind the explicit unavailable observation to the PDF container
            # instead; this preserves the renderer/OCR limitation without
            # fabricating a render, OCR result, or unsupported operator.
            observation_target_id = page_id if page_source.get("data") else builder.root_id
            renderer_diagnostic = builder.add_diagnostic("DFIR-PDF-RENDERER-UNAVAILABLE", "No renderer worker is configured; renderer result is unavailable and source facts are unchanged.", phase="observe", target_id=page_id)
            ocr_diagnostic = builder.add_diagnostic("DFIR-PDF-OCR-UNAVAILABLE", "No OCR worker is configured; OCR result is unavailable and source text is unchanged.", phase="observe", target_id=page_id)
            builder.add_feature("renderer-observation", "unavailable", target_id=observation_target_id, diagnostic_ids=[renderer_diagnostic])
            builder.add_feature("ocr-observation", "unavailable", target_id=observation_target_id, diagnostic_ids=[ocr_diagnostic])
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-page-tree"), "kind": "page-tree", "ownerId": builder.root_id, "items": page_tree_order_items, "ordinalBase": 0, "status": "preserved"}, "orderId")
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-paint"), "kind": "draw", "ownerId": builder.root_id, "items": draw_order_items, "context": "PDF content-stream source paint order", "status": "preserved" if draw_order_items and not draw_order_ambiguous else "ambiguous"}, "orderId")
        builder.add_item("orders", {"orderId": safe_id("order", "pdf-reading"), "kind": "reading", "ownerId": builder.root_id, "items": [{"id": node_id, "ordinal": index} for index, node_id in enumerate(reading_order_ids)], "status": "ambiguous"}, "orderId")
        has_font_reference = any(_pdf_find_value(_pdf_dictionary_tokens(object_data), "Font") is not None for object_data in objects.values())
        if has_font_reference and not font_mappings:
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
