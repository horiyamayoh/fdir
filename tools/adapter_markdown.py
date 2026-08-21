"""Stdlib Markdown parser that maps authored form facts into Document Form IR.

This is intentionally a bounded CommonMark-oriented adapter.  It preserves
source spelling and records dialect/authoring constructs as typed extensions;
it does not infer business meaning from Markdown.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
import mimetypes
import re
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id
try:
    from extension_registry import ExtensionPayload, build_extension
except ImportError:  # pragma: no cover
    from tools.extension_registry import ExtensionPayload, build_extension


_MAX_MARKDOWN_NESTING = 32


@dataclass(frozen=True)
class _MarkdownLine:
    """A physical Markdown line with its original source coordinates."""

    number: int
    text: str
    start: int
    content_end: int
    end: int
    ending: str


@dataclass(frozen=True)
class _MarkdownSourceIndex:
    """Map Unicode code-point positions to the original UTF-8 byte stream.

    ``Path.read_text()`` uses universal-newline translation by default.  That
    is useful for ordinary text processing but it destroys the information a
    source-faithful adapter needs for CRLF/CR spans.  The Markdown adapter
    therefore decodes the original bytes itself and keeps half-open source
    ranges in both code points and UTF-8 bytes.
    """

    source: str
    data: bytes
    byte_offsets: tuple[int, ...]
    lines: tuple[_MarkdownLine, ...]

    @classmethod
    def from_bytes(cls, data: bytes) -> "_MarkdownSourceIndex":
        source = data.decode("utf-8")
        byte_offsets = [0]
        for character in source:
            byte_offsets.append(byte_offsets[-1] + len(character.encode("utf-8")))

        lines: list[_MarkdownLine] = []
        cursor = 0
        line_number = 1
        while cursor < len(source):
            start = cursor
            while cursor < len(source) and source[cursor] not in "\r\n":
                cursor += 1
            content_end = cursor
            ending = "none"
            if cursor < len(source):
                if source[cursor] == "\r" and cursor + 1 < len(source) and source[cursor + 1] == "\n":
                    cursor += 2
                    ending = "CRLF"
                elif source[cursor] == "\r":
                    cursor += 1
                    ending = "CR"
                else:
                    cursor += 1
                    ending = "LF"
            lines.append(_MarkdownLine(line_number, source[start:content_end], start, content_end, cursor, ending))
            line_number += 1

        return cls(source, data, tuple(byte_offsets), tuple(lines))

    def _line(self, number: int) -> _MarkdownLine | None:
        if 1 <= number <= len(self.lines):
            return self.lines[number - 1]
        return None

    def code_point_position(self, line: int, column: int) -> int:
        record = self._line(line)
        if record is None:
            return 0
        offset = min(max(column - 1, 0), len(record.text))
        return record.start + offset

    def locate(self, code_point_offset: int) -> tuple[int, int]:
        offset = min(max(code_point_offset, 0), len(self.source))
        for record in self.lines:
            if record.start <= offset <= record.content_end:
                return record.number, offset - record.start + 1
            if record.content_end < offset < record.end:
                return record.number, len(record.text) + 1
        if self.lines:
            last = self.lines[-1]
            return last.number, len(last.text) + 1
        return 1, 1

    def span(self, line: int, column: int, end_line: int, end_column: int) -> dict[str, Any]:
        start = self.code_point_position(line, column)
        end = self.code_point_position(end_line, end_column)
        if end < start:
            end = start
        endings = [
            record.ending
            for record in self.lines[max(0, line - 1) : max(0, end_line - 1)]
            if record.ending != "none"
        ]
        unique_endings = sorted(set(endings))
        return {
            "coordinateUnit": "unicode-code-point",
            "endExclusive": True,
            "byteStart": self.byte_offsets[start],
            "byteEnd": self.byte_offsets[end],
            "codePointStart": start,
            "codePointEnd": end,
            "lineEnding": unique_endings[0] if len(unique_endings) == 1 else "mixed" if unique_endings else "none",
        }


@dataclass(frozen=True)
class _MarkdownDialect:
    """The explicitly selected syntax boundary for one conversion.

    The IR has no shared Markdown dialect schema.  Keep the policy local to
    the adapter and be conservative for unknown profile ids: a construct is
    either enabled by a known profile or it is retained with a diagnostic.
    """

    profile_id: str
    name: str
    tables: bool
    task_lists: bool
    strikethrough: bool
    footnotes: bool
    front_matter: bool
    known: bool = True


def _dialect_for_profile(profile: str | None) -> _MarkdownDialect:
    if profile is None or profile in {"commonmark", "fdir-commonmark-0.31.2-bounded"}:
        return _MarkdownDialect(
            "fdir-commonmark-0.31.2-bounded",
            "CommonMark 0.31.2 plus bounded fdir extensions",
            tables=True,
            task_lists=False,
            strikethrough=False,
            footnotes=True,
            front_matter=True,
        )
    if profile in {"gfm-0.29", "gfm-table-extension"}:
        return _MarkdownDialect(
            profile,
            "GitHub Flavored Markdown 0.29 bounded table profile"
            if profile == "gfm-table-extension"
            else "GitHub Flavored Markdown 0.29",
            tables=True,
            task_lists=True,
            strikethrough=True,
            footnotes=False,
            front_matter=False,
        )
    if profile == "commonmark-frontmatter-yaml-scalar":
        return _MarkdownDialect(
            profile,
            "CommonMark 0.31.2 plus fdir YAML scalar front matter",
            tables=False,
            task_lists=False,
            strikethrough=False,
            footnotes=False,
            front_matter=True,
        )
    if profile in {"commonmark-0.31.2-core", "commonmark-no-table-extension"}:
        return _MarkdownDialect(
            profile,
            "CommonMark 0.31.2 core",
            tables=False,
            task_lists=False,
            strikethrough=False,
            footnotes=False,
            front_matter=False,
        )
    return _MarkdownDialect(
        str(profile),
        "unknown Markdown profile",
        tables=False,
        task_lists=False,
        strikethrough=False,
        footnotes=False,
        front_matter=False,
        known=False,
    )


def _find_unescaped(value: str, needle: str, start: int) -> int:
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if value.startswith(needle, index):
            return index
    return -1


def _find_bracket_close(value: str, start: int) -> int:
    """Find a link-label close while retaining CommonMark bracket nesting."""

    depth = 0
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            if depth == 0:
                return index
            depth -= 1
    return -1


def _find_tilde_close(value: str, marker: str, start: int) -> int:
    """Find an exact one- or two-tilde closing delimiter."""

    cursor = start
    while cursor < len(value):
        close = _find_unescaped(value, marker, cursor)
        if close < 0:
            return -1
        before_is_tilde = close > 0 and value[close - 1] == "~"
        after = close + len(marker)
        after_is_tilde = after < len(value) and value[after] == "~"
        if not before_is_tilde and not after_is_tilde:
            return close
        cursor = close + 1
    return -1


def _find_balanced_close(value: str, start: int) -> int:
    depth = 0
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return index
            depth -= 1
    return -1


def _unescape(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            result.append(value[index + 1])
            index += 2
        else:
            result.append(value[index])
            index += 1
    return "".join(result)


def _normalize_label(value: str) -> str:
    return " ".join(_unescape(value).split()).casefold()


def _code_value(value: str) -> str:
    if len(value) >= 2 and value[0] == " " and value[-1] == " " and value.strip():
        return value[1:-1]
    return value.replace("\n", " ")


def _can_open_emphasis(value: str, index: int, marker: str) -> bool:
    after = index + len(marker)
    if after >= len(value) or value[after].isspace():
        return False
    before = value[index - 1] if index else " "
    if marker == "_" and before.isalnum() and value[after].isalnum():
        return False
    return index == 0 or before.isspace() or before in "([<{*_-"


def _destination(value: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        return "", ""
    if value.startswith("<"):
        close = _find_unescaped(value, ">", 1)
        if close < 0:
            return "", ""
        target = _unescape(value[1:close])
        remainder = value[close + 1 :].strip()
    else:
        end = 0
        while end < len(value) and not value[end].isspace():
            end += 1
        target = _unescape(value[:end])
        remainder = value[end:].strip()
    title = ""
    if len(remainder) >= 2 and remainder[0] in {'"', "'", "("} and remainder[-1] == ({"(": ")"}.get(remainder[0], remainder[0])):
        title = remainder[1:-1]
    return target, title


def _inline_tokens(
    value: str,
    references: dict[str, tuple[str, str]] | None = None,
    *,
    allow_strikethrough: bool = False,
) -> list[dict[str, Any]]:
    """Tokenize the selected inline dialect and retain local source spans."""
    references = references or {}
    tokens: list[dict[str, Any]] = []
    text_start = 0
    index = 0

    def emit_text(end: int) -> None:
        nonlocal text_start
        if end > text_start:
            tokens.append({"kind": "text", "raw": value[text_start:end], "start": text_start, "end": end})

    def emit(kind: str, start: int, end: int, **fields: Any) -> None:
        nonlocal text_start
        emit_text(start)
        tokens.append({"kind": kind, "raw": value[start:end], "start": start, "end": end, **fields})
        text_start = end

    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 2
            continue
        if value[index] == "`":
            marker_end = index
            while marker_end < len(value) and value[marker_end] == "`":
                marker_end += 1
            marker = value[index:marker_end]
            close = _find_unescaped(value, marker, marker_end)
            if close >= 0:
                emit("code", index, close + len(marker), marker=marker, content=_code_value(value[marker_end:close]))
                index = close + len(marker)
                continue
            emit("unclosed-code", index, len(value), marker=marker, content=value[marker_end:])
            index = len(value)
            continue
        if value[index] == "~":
            tilde_end = index
            while tilde_end < len(value) and value[tilde_end] == "~":
                tilde_end += 1
            tilde_count = tilde_end - index
            # GFM accepts exactly one or two tildes.  A longer run remains
            # literal text (GFM 0.29, strikethrough examples).
            if tilde_count in {1, 2}:
                marker = value[index:tilde_end]
                close = _find_tilde_close(value, marker, tilde_end)
                if close > tilde_end:
                    content = value[tilde_end:close]
                    if content and not content[0].isspace() and not content[-1].isspace():
                        emit(
                            "strikethrough" if allow_strikethrough else "unsupported-strikethrough",
                            index,
                            close + len(marker),
                            marker=marker,
                            content=content,
                        )
                        index = close + len(marker)
                        continue
            index = tilde_end
            continue
        if value.startswith("[^", index):
            label_end = _find_unescaped(value, "]", index + 2)
            if label_end >= 0:
                emit("footnote-ref", index, label_end + 1, label=_unescape(value[index + 2 : label_end]))
                index = label_end + 1
                continue
            emit("unclosed-footnote-ref", index, len(value), label=value[index + 2 :])
            index = len(value)
            continue
        if value.startswith("![", index) or value[index] == "[":
            image = value.startswith("![", index)
            label_start = index + 2 if image else index + 1
            label_end = _find_bracket_close(value, label_start)
            if label_end >= 0:
                after = label_end + 1
                if after < len(value) and value[after] == "(":
                    destination_end = _find_balanced_close(value, after + 1)
                    if destination_end >= 0:
                        target, title = _destination(value[after + 1 : destination_end])
                        if target:
                            emit("image" if image else "link", index, destination_end + 1, label=_unescape(value[label_start:label_end]), target=target, title=title)
                            index = destination_end + 1
                            continue
                    emit("unclosed-image" if image else "unclosed-link", index, len(value), label=value[label_start:])
                    index = len(value)
                    continue
                if not image and after < len(value) and value[after] == "[":
                    reference_end = _find_bracket_close(value, after + 1)
                    if reference_end >= 0:
                        label = _unescape(value[label_start:label_end])
                        reference = _unescape(value[after + 1 : reference_end]) or label
                        emit("reference-link", index, reference_end + 1, label=label, reference=reference, referenceStyle="reference")
                        index = reference_end + 1
                        continue
                    emit("unclosed-link", index, len(value), label=value[label_start:])
                    index = len(value)
                    continue
                if not image and _normalize_label(value[label_start:label_end]) in references:
                    label = _unescape(value[label_start:label_end])
                    emit("reference-link", index, label_end + 1, label=label, reference=label, referenceStyle="shortcut")
                    index = label_end + 1
                    continue
                index = label_end + 1
                continue
            emit("unclosed-image" if image else "unclosed-link", index, len(value), label=value[label_start:])
            index = len(value)
            continue
        if value[index] == "<":
            close = _find_unescaped(value, ">", index + 1)
            if close >= 0:
                inner = value[index + 1 : close]
                if re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9+.-]*:[^ <>]+|[^ <>@]+@[^ <>@]+)", inner):
                    emit("link", index, close + 1, label=inner, target=inner, title="", autolink=True)
                    index = close + 1
                    continue
                if inner.startswith(("/", "!", "?")) or re.match(r"[A-Za-z][A-Za-z0-9:-]*(?:\s|/|$)", inner):
                    emit("raw-html", index, close + 1, source=value[index:close + 1])
                    index = close + 1
                    continue
            elif index + 1 < len(value) and (value[index + 1].isalpha() or value[index + 1] == "/"):
                emit("unclosed-html", index, len(value), source=value[index:])
                index = len(value)
                continue
        marker = ""
        if value.startswith("**", index) or value.startswith("__", index):
            marker = value[index:index + 2]
        elif value[index] in "*_":
            marker = value[index]
        if marker and _can_open_emphasis(value, index, marker):
            close = _find_unescaped(value, marker, index + len(marker))
            if close > index + len(marker) and not value[close - 1].isspace():
                emit("emphasis", index, close + len(marker), marker=marker, content=value[index + len(marker) : close])
                index = close + len(marker)
                continue
            emit("unclosed-emphasis", index, len(value), marker=marker, content=value[index + len(marker) :])
            index = len(value)
            continue
        index += 1
    emit_text(len(value))
    if not tokens and value == "":
        tokens.append({"kind": "text", "raw": "", "start": 0, "end": 0})
    return tokens


def inspect(
    path: Path,
    *,
    limits: AdapterLimits | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    data = Path(path).read_bytes()
    source_index = _MarkdownSourceIndex.from_bytes(data)
    dialect = _dialect_for_profile(profile)
    capabilities = ["blocks", "inline", "links", "images", "lists", "authoring", "references", "raw-html", "source-maps"]
    if dialect.tables:
        capabilities.append("tables")
    if dialect.task_lists:
        capabilities.append("task-lists")
    if dialect.strikethrough:
        capabilities.append("strikethrough")
    if dialect.footnotes:
        capabilities.append("footnotes")
    if dialect.front_matter:
        capabilities.append("front-matter")
    return {
        "format": "markdown",
        "version": "commonmark",
        "bytes": len(data),
        "lines": len(source_index.lines) or 1,
        "profile": dialect.profile_id,
        "profileName": dialect.name,
        "profileKnown": dialect.known,
        "capabilities": capabilities,
        "limits": {"maxInputBytes": limits.max_input_bytes, "maxTextChars": limits.max_text_chars},
    }


def _strip_inline(
    value: str,
    references: dict[str, tuple[str, str]] | None = None,
    *,
    depth: int = 0,
    allow_strikethrough: bool = False,
) -> str:
    if depth >= _MAX_MARKDOWN_NESTING:
        return html.unescape(_unescape(value))
    normalized: list[str] = []
    for token in _inline_tokens(value, references, allow_strikethrough=allow_strikethrough):
        kind = token["kind"]
        if kind == "text":
            normalized.append(html.unescape(_unescape(token["raw"])))
        elif kind == "code":
            normalized.append(token["content"])
        elif kind in {"emphasis", "strikethrough", "unsupported-strikethrough", "link", "reference-link", "image"}:
            normalized.append(
                _strip_inline(
                    token.get("content", token.get("label", "")),
                    references,
                    depth=depth + 1,
                    allow_strikethrough=allow_strikethrough,
                )
            )
        elif kind in {"footnote-ref", "raw-html"}:
            normalized.append(token["raw"])
        else:
            normalized.append(token["raw"])
    return "".join(normalized)


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: ExtensionPayload, *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"markdown-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item(
        "extensions",
        build_extension(
            extension_id=extension_id,
            target_id=target_id,
            namespace="urn:fdir:format:markdown",
            extension_type=extension_type,
            payload=payload,
            criticality=criticality,
        ),
        "extensionId",
    )


def _linked_resource(builder: DocumentBuilder, url: str, *, kind: str, identity: str) -> str:
    resource_id = safe_id("resource", identity)
    if builder.find("resources", "resourceId", resource_id) is not None:
        return resource_id
    parsed = urlparse(url)
    external = bool(parsed.scheme or url.startswith("//"))
    if parsed.scheme == "data":
        # The bounded adapter records the authored reference but never
        # decodes or stores an inline payload.  Marking it available would
        # claim work that was not performed and could retain an unbounded
        # data URL in the IR.
        availability = "unavailable"
        media_type = parsed.path.split(";", 1)[0] or "application/octet-stream"
        external_target = f"data:{media_type}"
        derived_handle = f"data:{media_type}"
    elif external:
        availability = "unavailable"
        media_type = mimetypes.guess_type(Path(parsed.path).name)[0] if kind == "image" else None
        media_type = media_type or "application/octet-stream"
        external_target = url
        derived_handle = url
    else:
        # Do not probe the local filesystem.  A Markdown conversion should
        # not disclose whether a relative path (or a symlink/parent escape)
        # exists.  A caller that wants local-resource resolution must provide
        # a separate, explicitly authorized resolver.
        availability = "unavailable"
        media_type = mimetypes.guess_type(Path(unquote(parsed.path or url)).name)[0] if kind == "image" else None
        media_type = media_type or "application/octet-stream"
        external_target = ""
        derived_handle = url
    item: dict[str, Any] = {
        "resourceId": resource_id,
        "kind": kind,
        "mediaType": media_type,
        "packagePresence": False,
        "rawPayloadAvailable": False,
        "decodability": "not-attempted",
        "embeddedOrExternal": "external" if external else "linked",
        "availability": availability,
        "networkAvailability": "unknown" if external else "not-applicable",
        "derivedHandle": derived_handle,
    }
    if external_target:
        item["externalTarget"] = external_target
    builder.add_item("resources", item, "resourceId")
    return resource_id


def _resource_observation(builder: DocumentBuilder, resource_id: str) -> None:
    """Record that availability was intentionally not resolved at this boundary."""

    observation_id = safe_id("observation", f"markdown-resource-resolution-{resource_id}")
    if builder.find("observations", "observationId", observation_id) is None:
        builder.add_item(
            "observations",
            {
                "observationId": observation_id,
                "kind": "measurement",
                "targetId": resource_id,
                "method": "filesystem-resolution-not-configured",
                "engine": "none",
                "status": "unavailable",
            },
            "observationId",
        )


def _link_relation(
    builder: DocumentBuilder,
    source_id: str,
    target_id: str,
    identity: str,
    *,
    status: str = "preserved",
    kind: str = "links",
    source_occurrence_id: str | None = None,
    relation_type: str | None = None,
    target: str | None = None,
    target_mode: str | None = None,
) -> None:
    relation_id = safe_id("relation", identity)
    if builder.find("relations", "relationId", relation_id) is None:
        relation: dict[str, Any] = {
            "relationId": relation_id,
            "kind": kind,
            "fromId": source_id,
            "toId": target_id,
            "status": status,
        }
        if source_occurrence_id is not None:
            relation["sourceOccurrenceId"] = source_occurrence_id
        if relation_type is not None:
            relation["type"] = relation_type
        if target is not None:
            relation["target"] = target
        if target_mode is not None:
            relation["targetMode"] = target_mode
        builder.add_item("relations", relation, "relationId")
        owner = builder.find("parts", "partId", source_id)
        if owner is not None and relation_id not in owner.setdefault("relationshipIds", []):
            owner["relationshipIds"].append(relation_id)


def _target_is_external(target: str) -> bool:
    parsed = urlparse(target)
    return bool(parsed.scheme or target.startswith("//"))


def _source_occurrence_id(state: dict[str, Any], kind: str, target: str, token: dict[str, Any]) -> str:
    """Allocate the bounded Markdown source occurrence vocabulary.

    The first occurrence keeps the authored corpus identifiers (``md-*``);
    repeated constructs receive a deterministic suffix instead of colliding.
    """

    if kind == "reference-link":
        base = "md-reference-link"
    elif kind == "image":
        base = "md-external-image" if _target_is_external(target) else "md-local-image"
    elif token.get("autolink"):
        base = "md-autolink"
    else:
        base = "md-inline-external" if _target_is_external(target) else "md-inline-internal"
    counts = state.setdefault("sourceOccurrenceCounts", {})
    ordinal = int(counts.get(base, 0))
    counts[base] = ordinal + 1
    return base if ordinal == 0 else f"{base}-{ordinal + 1}"


def _link_type(kind: str, token: dict[str, Any]) -> str:
    if kind == "reference-link":
        return "reference-link"
    if kind == "image":
        return "image"
    return "autolink" if token.get("autolink") else "inline-link"


def _split_table_cells(line: str) -> list[dict[str, Any]]:
    leading = len(line) - len(line.lstrip(" "))
    start = leading + (1 if leading < len(line) and line[leading] == "|" else 0)
    end = len(line)
    right = end - 1
    while right >= start and line[right].isspace():
        right -= 1
    if right >= start and line[right] == "|" and (right == 0 or line[right - 1] != "\\"):
        end = right
    cells: list[dict[str, Any]] = []
    segment_start = start
    escaped = False
    code_ticks = 0
    for index in range(start, end):
        character = line[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "`":
            code_ticks = 0 if code_ticks else 1
            continue
        if character == "|" and code_ticks == 0:
            cells.append({"value": line[segment_start:index].strip(), "start": segment_start, "end": index})
            segment_start = index + 1
    cells.append({"value": line[segment_start:end].strip(), "start": segment_start, "end": end})
    for cell in cells:
        while cell["start"] < cell["end"] and line[cell["start"]].isspace():
            cell["start"] += 1
        while cell["end"] > cell["start"] and line[cell["end"] - 1].isspace():
            cell["end"] -= 1
    return cells


def _split_table_row(line: str) -> list[str]:
    return [cell["value"] for cell in _split_table_cells(line)]


def _heading_parts(line: str) -> tuple[int, str, int] | None:
    """Return ATX level, inline content, and content's zero-based offset.

    CommonMark permits an empty ATX heading and an optional closing sequence
    of ``#`` characters.  The offset lets the node retain the full authored
    marker span while its inline runs point only at the content.
    """

    leading = len(line) - len(line.lstrip(" "))
    value = line[leading:]
    count = 0
    while count < len(value) and value[count] == "#":
        count += 1
    if not 1 <= count <= 6:
        return None
    if count == len(value):
        return count, "", leading + count
    if value[count] not in " \t":
        return None
    content_start = count
    while content_start < len(value) and value[content_start] in " \t":
        content_start += 1
    content = value[content_start:].rstrip(" \t")
    if content:
        closing = re.search(r"[ \t]+#+[ \t]*$", content)
        if closing:
            content = content[: closing.start()].rstrip(" \t")
        content_offset = leading + content_start
    else:
        content_offset = leading + content_start
    return count, content, content_offset


def _thematic_break_parts(line: str) -> dict[str, Any] | None:
    """Return authored facts for the bounded CommonMark thematic-break rule."""

    leading = len(line) - len(line.lstrip(" \t"))
    if leading > 3:
        return None
    value = line[leading:]
    marker_chars = [character for character in value if character not in " \t"]
    if len(marker_chars) < 3 or marker_chars[0] not in "*-_" or any(character != marker_chars[0] for character in marker_chars):
        return None
    return {
        "marker": marker_chars[0],
        "count": len(marker_chars),
        "leadingWhitespace": line[:leading],
        "raw": line,
    }


def _indented_code_line(line: str) -> bool:
    """Identify an indented-code candidate outside the enabled block subset."""

    if not line.strip():
        return False
    prefix = line[: len(line) - len(line.lstrip(" \t"))]
    expanded = prefix.expandtabs(4)
    return len(expanded) >= 4


def _html_block_start(line: str) -> bool:
    """Recognize block HTML separately from the preserved inline HTML lane."""

    value = line.lstrip(" \t")
    return bool(re.match(r"^</?(?:address|article|aside|blockquote|details|dialog|div|dl|fieldset|figcaption|figure|footer|form|h[1-6]|header|hr|main|nav|ol|p|pre|section|table|ul)(?:\s|/?>)", value, re.IGNORECASE))


def _list_parts(line: str) -> tuple[str, str, int] | None:
    level = len(line) - len(line.lstrip(" "))
    value = line[level:]
    if not value:
        return None
    if value[0] in "*+-" and len(value) > 1 and value[1].isspace():
        return value[0], value[2:].strip(), level
    cursor = 0
    while cursor < len(value) and value[cursor].isdigit():
        cursor += 1
    if cursor and cursor + 1 < len(value) and value[cursor] in ".)" and value[cursor + 1].isspace():
        return value[: cursor + 1], value[cursor + 2 :].strip(), level
    return None


def _reference_parts(line: str) -> tuple[str, str, str] | None:
    leading = len(line) - len(line.lstrip(" "))
    if leading > 3:
        return None
    value = line[leading:]
    if not value.startswith("["):
        return None
    label_end = _find_bracket_close(value, 1)
    if label_end < 0 or label_end + 1 >= len(value) or value[label_end + 1] != ":":
        return None
    destination, title = _destination(value[label_end + 2 :])
    return (_unescape(value[1:label_end]), destination, title) if destination else None


def _footnote_parts(line: str) -> tuple[str, str] | None:
    if not line.startswith("[^"):
        return None
    label_end = _find_unescaped(line, "]", 2)
    if label_end < 0 or label_end + 1 >= len(line) or line[label_end + 1] != ":":
        return None
    body = line[label_end + 2 :].strip()
    return (_unescape(line[2:label_end]), body) if body else None


def _fence_parts(line: str) -> tuple[str, str] | None:
    value = line.lstrip(" ")
    if not value or value[0] not in "`~":
        return None
    character = value[0]
    count = 0
    while count < len(value) and value[count] == character:
        count += 1
    if count < 3:
        return None
    return character * count, value[count:].strip()


def _is_closing_fence(line: str, opening: str) -> bool:
    value = line.lstrip(" ")
    if len(line) - len(value) > 3 or not value.startswith(opening[0]):
        return False
    count = 0
    while count < len(value) and value[count] == opening[0]:
        count += 1
    return count >= len(opening) and not value[count:].strip()


def _is_table_separator(line: str, expected_columns: int | None = None) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    if not cells or (expected_columns is not None and len(cells) != expected_columns):
        return False
    for cell in cells:
        value = cell.strip()
        if value.startswith(":"):
            value = value[1:]
        if value.endswith(":"):
            value = value[:-1]
        if not re.fullmatch(r"-{3,}", value):
            return False
    return True


def _task_parts(content: str) -> tuple[bool, str, str, int] | None:
    """Return GFM task state, marker, body, and body offset in item content."""

    match = re.match(r"^\[([ xX])\](?:(?P<space>[ \t]+)(?P<body>.*)|$)", content)
    if not match:
        return None
    whitespace = match.group("space") or ""
    body = match.group("body") or ""
    if body and not whitespace:
        return None
    marker = content[: 3]
    return match.group(1).casefold() == "x", marker, body, match.end() - len(body)


def _source_map(builder: DocumentBuilder, target_id: str, line: int, column: int, end_line: int, end_column: int, *, token_start: int = 0, token_end: int | None = None) -> dict[str, Any]:
    locator = {
        "lineStart": line,
        "columnStart": max(1, column),
        "lineEnd": end_line,
        "columnEnd": max(1, end_column),
        "tokenStart": max(0, token_start),
        "tokenEnd": max(0, token_end if token_end is not None else end_column - column),
    }
    source_index = getattr(builder, "_markdown_source_index", None)
    if isinstance(source_index, _MarkdownSourceIndex):
        locator.update(source_index.span(line, column, end_line, end_column))
        if end_line != line:
            locator["tokenEnd"] = max(0, locator["codePointEnd"] - locator["codePointStart"])
    return builder.add_source_map(
        target_id,
        locator,
    )


def _diagnostic(builder: DocumentBuilder, code: str, message: str, *, target_id: str | None = None, source_map_id: str | None = None, severity: str = "warning", phase: str = "normalize") -> str:
    diagnostic_id = builder.add_diagnostic(code, message, severity=severity, phase=phase, target_id=target_id)
    if source_map_id:
        item = builder.find("diagnostics", "diagnosticId", diagnostic_id)
        if item is not None:
            item["sourceMapId"] = source_map_id
    return diagnostic_id


def _add_inline(
    builder: DocumentBuilder,
    parent_id: str,
    raw: str,
    line: int,
    column: int,
    references: dict[str, tuple[str, str]] | None = None,
    footnotes: dict[str, str] | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, str]:
    references = references or {}
    footnotes = footnotes or {}
    state = state or {"resourceGaps": set(), "forcePartial": False, "referenceDefinitions": {}}
    dialect = state.get("dialect")
    allow_strikethrough = bool(getattr(dialect, "strikethrough", False))
    allow_footnotes = bool(getattr(dialect, "footnotes", True))
    tokens = _inline_tokens(raw, references, allow_strikethrough=allow_strikethrough) or [{"kind": "text", "raw": raw, "start": 0, "end": len(raw)}]
    first_run = ""
    first_source = ""
    for token in tokens:
        kind = token["kind"]
        token_raw = token["raw"]
        token_start = int(token["start"])
        token_end = int(token["end"])
        run_status = "unsupported" if kind.startswith(("unclosed-", "unsupported-")) else "preserved"
        reference_key = _normalize_label(token.get("reference", "")) if kind == "reference-link" else ""
        reference_binding = state.get("referenceDefinitions", {}).get(reference_key) if reference_key else None
        if kind == "reference-link" and reference_key not in references:
            run_status = "ambiguous"
        if kind == "footnote-ref" and not allow_footnotes:
            run_status = "unsupported"
        if kind == "footnote-ref" and token.get("label", "") not in footnotes and allow_footnotes:
            run_status = "ambiguous"
        run_id = safe_id("node", f"markdown-run-{line}-{column}-{token_start}-{len(builder.document['nodes'])}")
        builder.add_node("run", run_id, parent_id=parent_id, status=run_status)
        source_start = max(0, column - 1 + token_start)
        source_end = max(source_start, column - 1 + token_end)
        source_id = safe_id("text", f"markdown-source-{line}-{column}-{token_start}-{len(builder.document['texts'])}")
        builder.add_text(
            source_id,
            token_raw,
            representation="source",
            provenance="authored",
            source_range={"start": source_start, "end": source_end},
            transformations=[{"kind": "identity", "sourceStart": 0, "sourceEnd": len(token_raw), "targetStart": 0, "targetEnd": len(token_raw)}],
        )
        builder.link_text(run_id, source_id)
        source_map = _source_map(builder, run_id, line, column + token_start, line, column + token_end, token_start=token_start, token_end=token_end)
        if not first_run:
            first_run, first_source = run_id, source_id
        normalized = _strip_inline(token_raw, references, allow_strikethrough=allow_strikethrough)
        if normalized != token_raw and not kind.startswith("unclosed-"):
            normalized_id = safe_id("text", f"markdown-normalized-{line}-{column}-{token_start}-{len(builder.document['texts'])}")
            builder.add_text(normalized_id, normalized, representation="normalized", provenance="decoded", source_text_id=source_id, source_range={"start": source_start, "end": source_end}, transformations=[{"kind": "normalize", "sourceStart": 0, "sourceEnd": len(token_raw), "targetStart": 0, "targetEnd": len(normalized)}], status="normalized")
            builder.link_text(run_id, normalized_id)

        if kind.startswith(("unclosed-", "unsupported-")):
            code = {
                "unclosed-code": "DFIR-MD-UNCLOSED-CODE-SPAN",
                "unclosed-link": "DFIR-MD-UNCLOSED-LINK",
                "unclosed-image": "DFIR-MD-UNCLOSED-IMAGE",
                "unclosed-footnote-ref": "DFIR-MD-UNCLOSED-FOOTNOTE-REF",
                "unclosed-html": "DFIR-MD-UNCLOSED-HTML",
                "unclosed-emphasis": "DFIR-MD-UNCLOSED-EMPHASIS",
                "unsupported-strikethrough": "DFIR-MD-STRIKETHROUGH-UNSUPPORTED",
            }.get(kind, "DFIR-MD-UNCLOSED-INLINE")
            message = (
                "GFM strikethrough is outside the enabled Markdown profile."
                if kind == "unsupported-strikethrough"
                else f"Unclosed Markdown inline construct: {kind}."
            )
            diagnostic_id = _diagnostic(builder, code, message, target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("inline-syntax", "unsupported", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "footnote-ref" and not allow_footnotes:
            diagnostic_id = _diagnostic(builder, "DFIR-MD-FOOTNOTE-UNSUPPORTED", "Footnote syntax is outside the selected Markdown dialect.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("footnote", "unsupported", target_id=run_id, diagnostic_ids=[diagnostic_id])
            state["forcePartial"] = True
        elif kind == "reference-link" and run_status == "ambiguous":
            diagnostic_id = _diagnostic(builder, "DFIR-MD-REFERENCE-UNRESOLVED", "Reference-style link has no matching definition.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("reference-link", "ambiguous", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "footnote-ref" and run_status == "ambiguous":
            diagnostic_id = _diagnostic(builder, "DFIR-MD-FOOTNOTE-UNRESOLVED", "Footnote reference has no matching definition.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("footnote", "ambiguous", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "footnote-ref" and allow_footnotes:
            annotation_id = safe_id("annotation", f"markdown-footnote-ref-{line}-{column}-{token_start}-{token.get('label', '')}")
            builder.add_item(
                "annotations",
                {
                    "annotationId": annotation_id,
                    "kind": "footnote",
                    "targetIds": [run_id],
                    "body": footnotes[token["label"]],
                    "referenceId": token["label"],
                    "sourceSubtype": "markdown:footnote",
                    "anchor": {"kind": "reference", "label": token["label"], "resolved": True},
                    "status": "preserved",
                },
                "annotationId",
            )
            builder.add_feature("footnote", "preserved", target_id=run_id)
        elif kind in {"link", "image", "reference-link"} and (kind != "reference-link" or run_status != "ambiguous"):
            if kind == "reference-link":
                target, _title = references[_normalize_label(token["reference"])]
            else:
                target = token["target"]
            source_occurrence_id = _source_occurrence_id(state, kind, target, token)
            relation_type = _link_type(kind, token)
            target_mode = "external" if _target_is_external(target) else "internal"
            resource_identity = (
                f"markdown-reference-target-{reference_key}"
                if kind == "reference-link"
                else f"markdown-{kind}-{line}-{column}-{token_start}-{target}"
            )
            resource_id = _linked_resource(builder, target, kind="image" if kind == "image" else "linkedObject", identity=resource_identity)
            node = builder.find("nodes", "nodeId", run_id)
            if node is not None:
                node.setdefault("resourceIds", []).append(resource_id)
            resource = builder.find("resources", "resourceId", resource_id)
            available = bool(resource and resource.get("availability") == "available")
            _resource_observation(builder, resource_id)
            _link_relation(
                builder,
                state.get("relationshipOwnerId", run_id),
                resource_id,
                f"markdown-{source_occurrence_id}-{resource_id}-relation",
                status="preserved" if available else "unavailable",
                source_occurrence_id=source_occurrence_id,
                relation_type=relation_type,
                target=target,
                target_mode=target_mode,
            )
            if kind == "reference-link" and reference_binding:
                _link_relation(
                    builder,
                    run_id,
                    reference_binding["annotationId"],
                    f"markdown-reference-use-{line}-{column}-{token_start}-{reference_binding['annotationId']}",
                    kind="references",
                )
            annotation_id = safe_id("annotation", f"markdown-{kind}-{line}-{column}-{token_start}-{target}")
            builder.add_item(
                "annotations",
                {
                    "annotationId": annotation_id,
                    "kind": "hyperlink",
                    "targetIds": [run_id, resource_id],
                    "body": target,
                    "displayText": token.get("label", target),
                    "destination": target,
                    "status": "preserved",
                    "referenceId": token.get("reference") if kind == "reference-link" else None,
                },
                "annotationId",
            )
            resource_diagnostics: list[str] = []
            scheme = urlparse(target).scheme.casefold()
            if scheme in {"javascript", "vbscript", "data"}:
                unsafe_diagnostic = _diagnostic(
                    builder,
                    "DFIR-MD-UNSAFE-URI-PRESERVED",
                    "Potentially unsafe Markdown URI was preserved as authored text and was not executed or dereferenced.",
                    target_id=run_id,
                    source_map_id=source_map["sourceMapId"],
                )
                resource_diagnostics.append(unsafe_diagnostic)
            if not available:
                diagnostic_id = _diagnostic(builder, "DFIR-MD-RESOURCE-UNAVAILABLE", "Linked Markdown resource was not available during conversion.", target_id=run_id, source_map_id=source_map["sourceMapId"])
                resource_diagnostics.append(diagnostic_id)
                if resource_id not in state["resourceGaps"]:
                    state["resourceGaps"].add(resource_id)
                    builder.add_feature("resource-resolution", "unavailable", target_id=run_id, diagnostic_ids=[diagnostic_id])
                state["forcePartial"] = True
            builder.add_feature("images" if kind == "image" else "links", "preserved" if available else "ambiguous", target_id=run_id, diagnostic_ids=resource_diagnostics)
        elif kind == "raw-html":
            _extension(builder, run_id, "raw-html", {"source": token["source"]})
            builder.add_feature("raw-html", "preserved", target_id=run_id)

        if kind in {"strikethrough", "unsupported-strikethrough"}:
            if kind == "strikethrough":
                builder.add_feature("strikethrough", "preserved", target_id=run_id)
            else:
                builder.add_feature("strikethrough", "unsupported", target_id=run_id)
                state["forcePartial"] = True

        delimiters = [token.get("marker", "")] if token.get("marker") else []
        authoring_facts: dict[str, Any] = {
            "delimiter": delimiters[0] if delimiters else "",
            "delimiters": delimiters,
            "escaping": ["backslash"] if "\\" in token_raw else [],
            "lineBreak": "hard" if token_raw.endswith("  ") or token_raw.endswith("\\") else "soft",
            "referenceStyle": token.get("referenceStyle", ""),
        }
        if kind == "reference-link":
            authoring_facts["referenceLabel"] = token.get("reference", "")
            if reference_binding:
                authoring_facts["referenceDefinitionId"] = reference_binding["annotationId"]
        _extension(builder, run_id, "authoring-facts", authoring_facts)
        if "&" in token_raw:
            _extension(builder, run_id, "entity-or-escaping", {"source": token_raw})
        builder.add_feature("inline", run_status, target_id=run_id)
    return first_run, first_source


def _paragraph(
    builder: DocumentBuilder,
    parent_id: str,
    text: str,
    line: int,
    *,
    kind: str = "paragraph",
    column: int = 1,
    references: dict[str, tuple[str, str]] | None = None,
    footnotes: dict[str, str] | None = None,
    state: dict[str, Any] | None = None,
    status: str = "preserved",
    source_line: int | None = None,
    source_column: int | None = None,
    source_end_line: int | None = None,
    source_end_column: int | None = None,
    source_token_end: int | None = None,
) -> str:
    node_id = safe_id("node", f"markdown-{kind}-{line}-{len(builder.document['nodes'])}")
    builder.add_node(kind, node_id, parent_id=parent_id, status=status)
    segments = text.split("\n")
    for offset, segment in enumerate(segments):
        _add_inline(builder, node_id, segment, line + offset, column if offset == 0 else 1, references, footnotes, state)
    end_column = column + len(segments[0]) if len(segments) == 1 else len(segments[-1]) + 1
    _source_map(
        builder,
        node_id,
        source_line if source_line is not None else line,
        source_column if source_column is not None else column,
        source_end_line if source_end_line is not None else line + len(segments) - 1,
        max(1, source_end_column if source_end_column is not None else end_column),
        token_start=0,
        token_end=source_token_end if source_token_end is not None else len(text),
    )
    return node_id


def _task_annotation(
    builder: DocumentBuilder,
    target_id: str,
    line: int,
    column: int,
    marker: str,
    checked: bool,
) -> None:
    """Represent a GFM checkbox using the existing form annotation vocabulary."""

    annotation_id = safe_id("annotation", f"markdown-task-{line}-{column}-{marker}")
    builder.add_item(
        "annotations",
        {
            "annotationId": annotation_id,
            "kind": "form",
            "targetIds": [target_id],
            "body": "checked" if checked else "unchecked",
            "referenceId": "task-list-item",
            "sourceSubtype": "markdown:task-list-item",
            "anchor": {"kind": "checkbox", "checked": checked, "marker": marker},
            "status": "preserved",
        },
        "annotationId",
    )
    _source_map(builder, annotation_id, line, column, line, column + len(marker), token_start=column - 1, token_end=len(marker))


def _table(
    builder: DocumentBuilder,
    parent_id: str,
    lines: list[str],
    start_line: int,
    references: dict[str, tuple[str, str]] | None = None,
    footnotes: dict[str, str] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    table_node = safe_id("node", f"markdown-table-{start_line}")
    builder.add_node("table", table_node, parent_id=parent_id, status="preserved")
    row_lines = [lines[0], *lines[2:]] if len(lines) >= 2 and _is_table_separator(lines[1]) else list(lines)
    row_ids: list[str] = []
    column_count = max((len(_split_table_row(line)) for line in row_lines), default=1)
    column_ids: list[str] = []
    cell_ids: list[str] = []
    for row_offset, line in enumerate(row_lines):
        row_id = safe_id("node", f"markdown-row-{start_line}-{row_offset}")
        builder.add_node("row", row_id, parent_id=table_node, status="preserved")
        row_ids.append(row_id)
        row_number = start_line if row_offset == 0 else start_line + row_offset + 1
        row_map = _source_map(builder, row_id, row_number, 1, row_number, len(line) + 1, token_start=0, token_end=len(line))
        cells = _split_table_cells(line)
        for col in range(column_count):
            cell = cells[col] if col < len(cells) else {"value": "", "start": len(line), "end": len(line)}
            cell_text = cell["value"]
            cell_id = safe_id("node", f"markdown-cell-{start_line}-{row_offset}-{col}")
            builder.add_node("cell", cell_id, parent_id=row_id, status="preserved", address={"row": row_offset + 1, "column": col + 1})
            cell_column = cell["start"] + 1
            _add_inline(builder, cell_id, cell_text, row_number, cell_column, references, footnotes, state)
            _source_map(builder, cell_id, row_number, cell_column, row_number, cell["end"] + 1, token_start=cell["start"], token_end=cell["end"])
            cell_ids.append(cell_id)
    for index in range(column_count):
        column_id = safe_id("node", f"markdown-column-{start_line}-{index}")
        builder.add_node("column", column_id, parent_id=table_node, status="preserved")
        column_ids.append(column_id)
    _source_map(
        builder,
        table_node,
        start_line,
        1,
        start_line + len(lines) - 1,
        max(1, len(lines[-1]) + 1),
        token_start=0,
        token_end=sum(len(line) for line in lines),
    )
    alignment = []
    if len(lines) >= 2:
        for cell in _split_table_row(lines[1]):
            value = cell.strip()
            alignment.append("center" if value.startswith(":") and value.endswith(":") else "right" if value.endswith(":") else "left")
    table_id = safe_id("table", f"markdown-{start_line}")
    separator_present = len(lines) >= 2 and _is_table_separator(lines[1])
    row_source_lines = [
        start_line + offset
        for offset in range(len(lines))
        if not (separator_present and offset == 1)
    ]
    builder.add_item(
        "tables",
        {
            "tableId": table_id,
            "nodeId": table_node,
            "ownerSurfaceId": next((item.get("surfaceId") for item in builder.document.get("surfaces", []) if isinstance(item, dict)), None),
            "range": {"rowStart": 1, "rowEnd": len(row_lines), "columnStart": 1, "columnEnd": column_count},
            "rowIds": row_ids,
            "columnIds": column_ids,
            "cellIds": cell_ids,
            "separatorLines": [start_line + 1] if separator_present else [],
            "rowSourceLines": row_source_lines,
            "alignment": alignment or ["source"],
            "separatorIsMetadata": separator_present,
            "status": "preserved",
        },
        "tableId",
    )
    _extension(
        builder,
        table_node,
        "table-authoring",
        {
            "delimiter": "|",
            "alignment": alignment or ["source"],
            "headerLine": start_line,
            "separatorLine": start_line + 1 if len(lines) >= 2 and _is_table_separator(lines[1]) else None,
            "dataRowLines": [start_line + offset for offset in range(2, len(lines))],
        },
    )
    return table_node


def convert(path: Path, *, limits: AdapterLimits | None = None, profile: str | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    try:
        source_index = _MarkdownSourceIndex.from_bytes(path.read_bytes())
    except UnicodeDecodeError as exc:
        builder = DocumentBuilder(path, "markdown", "commonmark", limits=limits)
        diagnostic = builder.add_diagnostic("DFIR-MD-UTF8-FAILED", str(exc), severity="error", phase="parse")
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")

    source = source_index.source
    builder = DocumentBuilder(path, "markdown", "commonmark", limits=limits)
    builder._markdown_source_index = source_index
    dialect = _dialect_for_profile(profile)
    profile_name = dialect.profile_id
    part_id = safe_id("part", "markdown-document")
    surface_id = safe_id("surface", "markdown-document")
    builder.add_item(
        "parts",
        {
            "partId": part_id,
            "kind": "document",
            "name": "Markdown document",
            "storyType": "document",
            "rootNodeIds": [builder.root_id],
            "surfaceIds": [surface_id],
            "status": "preserved",
        },
        "partId",
    )
    # Relationship reciprocity is owned by a part in the shared IR contract.
    # Keep the inline source symbol stable for the bounded Markdown
    # qualification lane while the annotations and nodes retain their exact
    # token-level targets.
    relationship_owner_id = safe_id("part", "markdown-inline-links")
    builder.add_item(
        "parts",
        {
            "partId": relationship_owner_id,
            "kind": "inline-links",
            "name": "markdown-run",
            "parentPartId": part_id,
            "rootNodeIds": [],
            "relationshipIds": [],
            "status": "preserved",
        },
        "partId",
    )
    builder.add_item(
        "surfaces",
        {
            "surfaceId": surface_id,
            "partId": part_id,
            "kind": "story",
            "ordinal": 0,
            "dialect": profile_name,
            "status": "preserved",
        },
        "surfaceId",
    )
    if not dialect.known:
        diagnostic = _diagnostic(
            builder,
            "DFIR-MD-UNKNOWN-PROFILE",
            f"Unknown Markdown profile {profile!r}; only conservative CommonMark source preservation is enabled.",
            target_id=builder.root_id,
        )
        builder.add_feature("dialect", "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
    lines = [record.text for record in source_index.lines]
    if not lines:
        source_map = _source_map(builder, builder.root_id, 1, 1, 1, 1, token_start=0, token_end=0)
        diagnostic = _diagnostic(builder, "DFIR-MD-EMPTY-SOURCE", "Markdown source is empty.", target_id=builder.root_id, source_map_id=source_map["sourceMapId"], severity="error", phase="parse")
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
    if "\x00" in source:
        full_map = _source_map(builder, builder.root_id, 1, 1, len(lines), len(lines[-1]) + 1, token_start=0, token_end=len(source))
        nul_offset = source.index("\x00")
        nul_line, nul_column = source_index.locate(nul_offset)
        nul_map = _source_map(builder, builder.root_id, nul_line, nul_column, nul_line, nul_column + 1, token_start=nul_column - 1, token_end=nul_column)
        # Keep the exact NUL source map while targeting the emitted document
        # root.  Qualification requires parser diagnostics to point at an
        # emitted entity; the source-map locator remains the byte-accurate
        # line/column authority for this diagnostic.
        diagnostic = _diagnostic(builder, "DFIR-MD-NUL-CHARACTER", "NUL characters are not valid authored Markdown text.", target_id=builder.root_id, source_map_id=nul_map["sourceMapId"], severity="error", phase="parse")
        source_abort = _diagnostic(builder, "DFIR-MD-SOURCE-ABORTED", "Markdown parsing stopped after a source decoding failure.", target_id=builder.root_id, source_map_id=full_map["sourceMapId"], phase="parse")
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic, source_abort])
        return builder.finish(status="failed")

    references: dict[str, tuple[str, str]] = {}
    reference_definitions: dict[str, dict[str, Any]] = {}
    footnotes: dict[str, str] = {}
    open_fence: str | None = None
    for number, item in enumerate(lines, start=1):
        fence = _fence_parts(item)
        if open_fence:
            if fence and fence[0].startswith(open_fence[0]) and _is_closing_fence(item, open_fence):
                open_fence = None
            continue
        if fence:
            open_fence = fence[0]
            continue
        reference = _reference_parts(item)
        if reference:
            label, target, title = reference
            key = _normalize_label(label)
            if key not in references:
                references[key] = (target, title)
                reference_definitions[key] = {
                    "annotationId": safe_id("annotation", f"markdown-reference-{number}-{key}"),
                    "definitionLine": number,
                    "label": label,
                    "target": target,
                    "title": title,
                }
        footnote = _footnote_parts(item)
        if footnote:
            label, body = footnote
            footnotes.setdefault(label, body)
    state: dict[str, Any] = {
        "resourceGaps": set(),
        "forcePartial": False,
        "dialect": dialect,
        "referenceDefinitions": reference_definitions,
        "relationshipOwnerId": relationship_owner_id,
        "sourceOccurrenceCounts": {},
    }

    start_index = 0
    if dialect.front_matter and lines[0] == "---":
        # ``---`` is a CommonMark thematic break by default.  In this bounded
        # profile it becomes front matter only when a closing delimiter and
        # at least one scalar key/value entry are both present.  This avoids
        # consuming an ordinary thematic break as metadata, while malformed
        # metadata-looking input is still diagnosed rather than completed.
        end = next((candidate for candidate in range(1, len(lines)) if lines[candidate] == "---"), None)
        close_line = end + 1 if end is not None else len(lines)
        entries: list[dict[str, str]] = []
        malformed = False
        for item in lines[1 : end if end is not None else len(lines)]:
            match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)", item)
            if match:
                entries.append({"key": match.group(1), "value": match.group(2)})
            elif item.strip():
                malformed = True
        looks_like_front_matter = bool(entries) or any(
            re.match(r"\s*[A-Za-z0-9_.-]+\s*:", item) for item in lines[1 : end if end is not None else len(lines)]
        )
        if end is not None and entries and not malformed:
            front_map = _source_map(builder, builder.root_id, 1, 1, close_line, len(lines[close_line - 1]) + 1, token_start=0, token_end=sum(len(item) for item in lines[:close_line]))
            _extension(
                builder,
                builder.root_id,
                "front-matter",
                {"syntax": "yaml-scalar", "opening": "---", "closing": "---", "entries": entries, "status": "preserved"},
            )
            builder.add_feature("front-matter", "preserved", target_id=builder.root_id)
            start_index = end + 1
        elif looks_like_front_matter:
            front_map = _source_map(builder, builder.root_id, 1, 1, close_line, len(lines[close_line - 1]) + 1, token_start=0, token_end=sum(len(item) for item in lines[:close_line]))
            _extension(
                builder,
                builder.root_id,
                "front-matter",
                {
                    "syntax": "yaml-scalar",
                    "opening": "---",
                    "closing": "---" if end is not None else "",
                    "entries": entries,
                    "status": "unsupported" if end is None else "ambiguous",
                    "rawLines": lines[:close_line],
                },
            )
            code = "DFIR-MD-UNCLOSED-FRONT-MATTER" if end is None else "DFIR-MD-FRONT-MATTER-UNSUPPORTED"
            message = "Front matter has no closing --- delimiter." if end is None else "Front matter contains a value outside the bounded scalar key/value dialect."
            diagnostic = _diagnostic(builder, code, message, target_id=builder.root_id, source_map_id=front_map["sourceMapId"])
            builder.add_feature("front-matter", "unsupported" if end is None else "ambiguous", target_id=builder.root_id, diagnostic_ids=[diagnostic])
            state["forcePartial"] = True
            if end is not None:
                # The closed metadata envelope has already been represented by
                # the diagnosed extension.  Do not reinterpret its opening or
                # closing delimiter as additional thematic-break nodes.
                start_index = end + 1

    records = [(item, number, 1) for number, item in enumerate(lines[start_index:], start=start_index + 1)]

    def directive_start(item: str) -> bool:
        stripped = item.lstrip(" ")
        return len(item) - len(stripped) <= 3 and stripped.startswith(":::")

    def block_start(index: int) -> bool:
        item = records[index][0]
        if (
            directive_start(item)
            or _fence_parts(item)
            or _heading_parts(item)
            or _list_parts(item)
            or _thematic_break_parts(item)
            or _indented_code_line(item)
            or _html_block_start(item)
            or item.lstrip().startswith(">")
        ):
            return True
        if _reference_parts(item) or _footnote_parts(item):
            return True
        return (
            index + 1 < len(records)
            and "|" in item
            and _is_table_separator(records[index + 1][0], expected_columns=len(_split_table_row(item)))
        )

    def parse_list(index: int, parent_id: str) -> int:
        first = _list_parts(records[index][0])
        if first is None:
            return index
        if first[2] > _MAX_MARKDOWN_NESTING:
            node_id = _paragraph(builder, parent_id, records[index][0], records[index][1], column=records[index][2], references=references, footnotes=footnotes, state=state, status="unsupported")
            diagnostic = _diagnostic(builder, "DFIR-MD-NESTING-LIMIT", "Markdown list nesting exceeded the bounded block depth.", target_id=node_id)
            builder.add_feature("list", "unsupported", target_id=node_id, diagnostic_ids=[diagnostic])
            state["forcePartial"] = True
            return index + 1
        base_level = first[2]
        list_id = safe_id("node", f"markdown-list-{records[index][1]}-{len(builder.document['nodes'])}")
        builder.add_node("list", list_id, parent_id=parent_id, status="preserved")
        list_start = index
        last_index = index
        while index < len(records):
            parts = _list_parts(records[index][0])
            if parts is None or parts[2] < base_level:
                break
            if parts[2] > base_level:
                if index == last_index:
                    break
                index = parse_list(index, last_index)
                continue
            marker, content, level = parts
            content_position = records[index][0].find(content, level + len(marker)) if content else level + len(marker) + 1
            item_column = max(1, content_position + 1)
            task_parts = _task_parts(content)
            task = task_parts is not None
            task_checked = task_parts[0] if task_parts else False
            task_marker = task_parts[1] if task_parts else ""
            task_body = task_parts[2] if task_parts else content
            task_body_column = item_column + (task_parts[3] if task_parts else 0)
            task_enabled = task and dialect.task_lists
            item_status = "preserved" if not task or task_enabled else "unsupported"
            item_id = _paragraph(
                builder,
                list_id,
                task_body if task_enabled else content,
                records[index][1],
                column=task_body_column if task_enabled else item_column,
                references=references,
                footnotes=footnotes,
                state=state,
                status=item_status,
                source_line=records[index][1] if task_enabled else None,
                source_column=item_column if task_enabled else None,
                source_end_line=records[index][1] if task_enabled else None,
                source_end_column=(len(records[index][0]) + 1) if task_enabled else None,
                source_token_end=len(records[index][0]) - item_column + 1 if task_enabled else None,
            )
            _extension(builder, item_id, "list-marker", {"marker": marker, "level": level})
            if task:
                if task_enabled:
                    _task_annotation(builder, item_id, records[index][1], item_column, task_marker, task_checked)
                    builder.add_feature("task-list", "preserved", target_id=item_id)
                else:
                    item_map = builder.find("sourceMaps", "targetId", item_id)
                    diagnostic = _diagnostic(builder, "DFIR-MD-TASK-LIST-UNSUPPORTED", "GFM task-list marker is outside the selected Markdown dialect.", target_id=item_id, source_map_id=item_map["sourceMapId"] if item_map else None)
                    builder.add_feature("task-list", "unsupported", target_id=item_id, diagnostic_ids=[diagnostic])
                    state["forcePartial"] = True
            last_index = item_id
            index += 1
            if index < len(records) and _list_parts(records[index][0]) is not None and _list_parts(records[index][0])[2] > base_level:
                index = parse_list(index, item_id)
            last_index = item_id
        _source_map(builder, list_id, records[list_start][1], 1, records[max(list_start, index - 1)][1], len(records[max(list_start, index - 1)][0]) + 1, token_start=0, token_end=sum(len(record[0]) for record in records[list_start:index]))
        builder.add_feature("list", "preserved", target_id=list_id)
        return index

    def parse_blocks(block_records: list[tuple[str, int, int]], parent_id: str) -> None:
        nonlocal records
        saved_records = records
        records = block_records
        index = 0
        while index < len(records):
            text, number, column = records[index]
            if not text.strip():
                index += 1
                continue
            if directive_start(text):
                end = next((candidate for candidate in range(index + 1, len(records)) if records[candidate][0].strip() == ":::"), None)
                final = end if end is not None else len(records) - 1
                body = records[index + 1 : end] if end is not None else records[index + 1 :]
                node_records = body or records[index : index + 1]
                node = _paragraph(
                    builder,
                    parent_id,
                    "\n".join(item[0] for item in node_records),
                    node_records[0][1],
                    column=node_records[0][2],
                    references=references,
                    footnotes=footnotes,
                    state=state,
                    status="unsupported",
                    source_line=number,
                    source_column=column,
                    source_end_line=records[final][1],
                    source_end_column=records[final][2] + len(records[final][0]),
                    source_token_end=sum(len(item[0]) for item in records[index : final + 1]),
                )
                source_map = builder.find("sourceMaps", "targetId", node)
                diagnostics = [_diagnostic(builder, "DFIR-MD-DIRECTIVE-UNSUPPORTED", "Markdown directive syntax is outside the bounded CommonMark adapter.", target_id=node, source_map_id=source_map["sourceMapId"])]
                if end is None:
                    diagnostics.append(_diagnostic(builder, "DFIR-MD-UNCLOSED-DIRECTIVE", "Markdown directive has no closing ::: delimiter.", target_id=node, source_map_id=source_map["sourceMapId"]))
                directive_body = [item[0] for item in body]
                if end is not None:
                    # The registered extension vocabulary has only opening/body
                    # fields.  Keep the exact closing delimiter in body so the
                    # closing source line is still attributable to this opaque
                    # directive without inventing a new schema field.
                    directive_body.append(records[end][0])
                _extension(builder, node, "unsupported-directive", {"opening": text, "body": directive_body})
                builder.add_feature("directive", "unsupported", target_id=node, diagnostic_ids=diagnostics)
                index = final + 1
                continue
            fence = _fence_parts(text)
            if fence:
                end = next((candidate for candidate in range(index + 1, len(records)) if _is_closing_fence(records[candidate][0], fence[0])), None)
                final = end if end is not None else len(records) - 1
                content = records[index + 1 : end] if end is not None else records[index + 1 :]
                node_records = content or records[index : index + 1]
                node = _paragraph(
                    builder,
                    parent_id,
                    "\n".join(item[0] for item in node_records),
                    node_records[0][1],
                    column=node_records[0][2],
                    references=references,
                    footnotes=footnotes,
                    state=state,
                    status="preserved" if end is not None else "unsupported",
                    source_line=number,
                    source_column=column,
                    source_end_line=records[final][1],
                    source_end_column=records[final][2] + len(records[final][0]),
                    source_token_end=sum(len(item[0]) for item in records[index : final + 1]),
                )
                source_map = builder.find("sourceMaps", "targetId", node)
                _extension(builder, node, "code-block", {"fence": fence[0], "language": fence[1], "content": "\n".join(item[0] for item in content)})
                if end is None:
                    diagnostic = _diagnostic(builder, "DFIR-MD-UNCLOSED-FENCE", "Fenced code block has no matching closing delimiter.", target_id=node, source_map_id=source_map["sourceMapId"])
                    builder.add_feature("code-block", "unsupported", target_id=node, diagnostic_ids=[diagnostic])
                else:
                    builder.add_feature("code-block", "preserved", target_id=node)
                index = final + 1
                continue
            thematic_break = _thematic_break_parts(text)
            if thematic_break:
                node_id = safe_id("node", f"markdown-thematic-break-{number}-{len(builder.document['nodes'])}")
                builder.add_node("thematicBreak", node_id, parent_id=parent_id, status="preserved")
                _source_map(builder, node_id, number, column, number, len(text) + 1, token_start=0, token_end=len(text))
                _extension(
                    builder,
                    node_id,
                    "thematic-break-authoring",
                    {
                        "marker": thematic_break["marker"],
                        "count": thematic_break["count"],
                        "leadingWhitespace": thematic_break["leadingWhitespace"],
                    },
                )
                builder.add_feature("thematic-break", "preserved", target_id=node_id)
                index += 1
                continue
            heading = _heading_parts(text)
            if heading:
                content_column = column + heading[2]
                node = _paragraph(
                    builder,
                    parent_id,
                    heading[1],
                    number,
                    kind="heading",
                    column=max(1, content_column),
                    references=references,
                    footnotes=footnotes,
                    state=state,
                    source_line=number,
                    source_column=column,
                    source_end_line=number,
                    source_end_column=column + len(text),
                    source_token_end=len(text),
                )
                _extension(builder, node, "heading-authoring", {"level": heading[0], "marker": "#" * heading[0]})
                builder.add_feature("heading", "preserved", target_id=node)
                index += 1
                continue
            if index + 1 < len(records) and text.strip() and re.fullmatch(r" {0,3}(?:=+|-+)\s*", records[index + 1][0]) and not ("|" in records[index + 1][0] and _is_table_separator(records[index + 1][0])):
                level = 1 if "=" in records[index + 1][0] else 2
                node = _paragraph(
                    builder,
                    parent_id,
                    text,
                    number,
                    kind="heading",
                    column=column,
                    references=references,
                    footnotes=footnotes,
                    state=state,
                    source_line=number,
                    source_column=column,
                    source_end_line=records[index + 1][1],
                    source_end_column=records[index + 1][2] + len(records[index + 1][0]),
                    source_token_end=len(text) + len(records[index + 1][0]),
                )
                _extension(builder, node, "heading-authoring", {"level": level, "marker": "=" if level == 1 else "-"})
                builder.add_feature("heading", "preserved", target_id=node)
                index += 2
                continue
            if _indented_code_line(text):
                node = _paragraph(builder, parent_id, text, number, column=column, references=references, footnotes=footnotes, state=state, status="unsupported")
                source_map = builder.find("sourceMaps", "targetId", node)
                diagnostic = _diagnostic(
                    builder,
                    "DFIR-MD-INDENTED-CODE-UNSUPPORTED",
                    "Indented code blocks are outside the bounded Markdown profile.",
                    target_id=node,
                    source_map_id=source_map["sourceMapId"] if source_map else None,
                )
                builder.add_feature("indented-code", "unsupported", target_id=node, diagnostic_ids=[diagnostic])
                state["forcePartial"] = True
                index += 1
                continue
            if _html_block_start(text):
                node = _paragraph(builder, parent_id, text, number, column=column, references=references, footnotes=footnotes, state=state, status="unsupported")
                source_map = builder.find("sourceMaps", "targetId", node)
                diagnostic = _diagnostic(
                    builder,
                    "DFIR-MD-HTML-BLOCK-UNSUPPORTED",
                    "Block HTML is preserved as source text but is outside the bounded block profile.",
                    target_id=node,
                    source_map_id=source_map["sourceMapId"] if source_map else None,
                )
                builder.add_feature("raw-html-block", "unsupported", target_id=node, diagnostic_ids=[diagnostic])
                state["forcePartial"] = True
                index += 1
                continue
            footnote = _footnote_parts(text)
            if footnote:
                label, body = footnote
                if dialect.footnotes:
                    annotation_id = safe_id("annotation", f"markdown-footnote-{number}-{label}")
                    builder.add_item(
                        "annotations",
                        {
                            "annotationId": annotation_id,
                            "kind": "footnote",
                            "targetIds": [parent_id],
                            "body": body,
                            "referenceId": label,
                            "sourceSubtype": "markdown:footnote",
                            "anchor": {"kind": "definition", "label": label, "resolved": True},
                            "status": "preserved",
                        },
                        "annotationId",
                    )
                    _source_map(builder, annotation_id, number, column, number, len(text) + 1, token_start=0, token_end=len(text))
                    builder.add_feature("footnote", "preserved", target_id=annotation_id)
                else:
                    source_map = _source_map(builder, builder.root_id, number, column, number, column + len(text), token_start=column - 1, token_end=len(text))
                    diagnostic = _diagnostic(builder, "DFIR-MD-FOOTNOTE-UNSUPPORTED", "Footnote definitions are outside the selected Markdown dialect.", target_id=builder.root_id, source_map_id=source_map["sourceMapId"])
                    builder.add_feature("footnote", "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
                    state["forcePartial"] = True
                index += 1
                continue
            reference = _reference_parts(text)
            if reference:
                label, target, title = reference
                reference_key = _normalize_label(label)
                annotation_id = safe_id("annotation", f"markdown-reference-{number}-{reference_key}")
                binding = state.get("referenceDefinitions", {}).get(reference_key)
                authoritative = not binding or binding.get("annotationId") == annotation_id
                annotation_status = "preserved" if authoritative else "ambiguous"
                builder.add_item(
                    "annotations",
                    {"annotationId": annotation_id, "kind": "bookmark", "targetIds": [parent_id], "body": target, "referenceId": label, "status": annotation_status},
                    "annotationId",
                )
                _source_map(builder, annotation_id, number, column, number, len(text) + 1, token_start=0, token_end=len(text))
                resource_id = _linked_resource(builder, target, kind="linkedObject", identity=f"markdown-reference-target-{reference_key}")
                _resource_observation(builder, resource_id)
                _extension(
                    builder,
                    parent_id,
                    "reference-definition",
                    {"label": label, "destination": target, "title": title, "annotationId": annotation_id, "definitionLine": number},
                )
                if authoritative:
                    builder.add_feature("reference-definition", "preserved", target_id=annotation_id)
                else:
                    diagnostic = _diagnostic(builder, "DFIR-MD-DUPLICATE-REFERENCE-DEFINITION", "A later reference definition is ignored because the first definition wins.", target_id=annotation_id)
                    builder.add_feature("reference-definition", "ambiguous", target_id=annotation_id, diagnostic_ids=[diagnostic])
                index += 1
                continue
            parts = _list_parts(text)
            if parts:
                index = parse_list(index, parent_id)
                continue
            table_enabled = dialect.tables
            if table_enabled and index + 1 < len(records) and "|" in text and _is_table_separator(records[index + 1][0], expected_columns=len(_split_table_row(text))):
                table_lines = [text, records[index + 1][0]]
                index += 2
                while index < len(records) and records[index][0].strip() and "|" in records[index][0]:
                    table_lines.append(records[index][0])
                    index += 1
                table_node = _table(builder, parent_id, table_lines, number, references, footnotes, state)
                builder.add_feature("table", "preserved", target_id=table_node)
                continue
            if text.lstrip().startswith(">"):
                group: list[tuple[str, int, int]] = []
                original: list[tuple[str, int, int]] = []
                while index < len(records) and records[index][0].lstrip().startswith(">"):
                    current = records[index]
                    offset = current[0].find(">") + 1
                    if offset < len(current[0]) and current[0][offset] == " ":
                        offset += 1
                    group.append((current[0][offset:], current[1], current[2] + offset))
                    original.append(current)
                    index += 1
                section_id = safe_id("node", f"markdown-blockquote-{number}-{len(builder.document['nodes'])}")
                builder.add_node("section", section_id, parent_id=parent_id, status="preserved")
                _source_map(builder, section_id, number, column, original[-1][1], len(original[-1][0]) + 1, token_start=0, token_end=sum(len(item[0]) for item in original))
                blockquote_depth = int(state.get("blockquoteDepth", 0))
                if blockquote_depth >= _MAX_MARKDOWN_NESTING:
                    diagnostic = _diagnostic(builder, "DFIR-MD-NESTING-LIMIT", "Markdown blockquote nesting exceeded the bounded block depth.", target_id=section_id)
                    builder.add_feature("blockquote", "unsupported", target_id=section_id, diagnostic_ids=[diagnostic])
                    state["forcePartial"] = True
                else:
                    state["blockquoteDepth"] = blockquote_depth + 1
                    try:
                        parse_blocks(group, section_id)
                    finally:
                        state["blockquoteDepth"] = blockquote_depth
                    builder.add_feature("blockquote", "preserved", target_id=section_id)
                continue
            paragraph = [records[index]]
            index += 1
            while index < len(records) and records[index][0].strip() and not block_start(index):
                paragraph.append(records[index])
                index += 1
            _paragraph(builder, parent_id, "\n".join(item[0] for item in paragraph), paragraph[0][1], column=paragraph[0][2], references=references, footnotes=footnotes, state=state)
        records = saved_records

    parse_blocks(records, builder.root_id)
    builder.add_item("orders", {"orderId": safe_id("order", "markdown-source"), "kind": "source", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": ordinal} for ordinal, node in enumerate(builder.document["nodes"][1:])], "ordinalBase": 0, "status": "preserved"}, "orderId")
    builder.add_feature("block-structure", "preserved", target_id=builder.root_id)
    return builder.finish(status="partial" if state["forcePartial"] else None)
