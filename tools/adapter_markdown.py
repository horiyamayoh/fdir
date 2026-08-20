"""Stdlib Markdown parser that maps authored form facts into Document Form IR.

This is intentionally a bounded CommonMark-oriented adapter.  It preserves
source spelling and records dialect/authoring constructs as typed extensions;
it does not infer business meaning from Markdown.
"""

from __future__ import annotations

import html
from pathlib import Path
import mimetypes
import re
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id


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


def _inline_tokens(value: str, references: dict[str, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Tokenize the bounded inline dialect and retain local source spans."""
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
            label_end = _find_unescaped(value, "]", label_start)
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
                    reference_end = _find_unescaped(value, "]", after + 1)
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


def inspect(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    data = Path(path).read_bytes()
    text = data.decode("utf-8")
    return {
        "format": "markdown",
        "version": "commonmark",
        "bytes": len(data),
        "lines": text.count("\n") + 1,
        "capabilities": [
            "blocks", "inline", "links", "images", "tables", "lists", "footnotes",
            "front-matter", "authoring", "references", "raw-html", "source-maps",
        ],
        "limits": {"maxInputBytes": limits.max_input_bytes, "maxTextChars": limits.max_text_chars},
    }


def _strip_inline(value: str, references: dict[str, tuple[str, str]] | None = None) -> str:
    normalized: list[str] = []
    for token in _inline_tokens(value, references):
        kind = token["kind"]
        if kind == "text":
            normalized.append(html.unescape(_unescape(token["raw"])))
        elif kind == "code":
            normalized.append(token["content"])
        elif kind in {"emphasis", "link", "reference-link", "image"}:
            normalized.append(_strip_inline(token.get("content", token.get("label", "")), references))
        elif kind in {"footnote-ref", "raw-html"}:
            normalized.append(token["raw"])
        else:
            normalized.append(token["raw"])
    return "".join(normalized)


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: dict[str, Any], *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"markdown-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item(
        "extensions",
        {
            "extensionId": extension_id,
            "targetId": target_id,
            "namespace": "urn:fdir:format:markdown",
            "type": extension_type,
            "schemaVersion": "1.0.0",
            "schemaId": f"urn:fdir:schema:markdown-{extension_type}",
            "payload": payload,
            "criticality": criticality,
        },
        "extensionId",
    )


def _linked_resource(builder: DocumentBuilder, url: str, *, kind: str, identity: str) -> str:
    resource_id = safe_id("resource", identity)
    if builder.find("resources", "resourceId", resource_id) is not None:
        return resource_id
    parsed = urlparse(url)
    external = bool(parsed.scheme or url.startswith("//"))
    if parsed.scheme == "data":
        availability = "available"
        media_type = parsed.path.split(";", 1)[0] or "application/octet-stream"
        external_target = None
    elif external:
        availability = "unavailable"
        media_type = "application/octet-stream"
        external_target = url
    else:
        candidate = (builder.path.parent / unquote(parsed.path or url)).resolve()
        availability = "available" if candidate.is_file() else "unavailable"
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        external_target = None if availability == "available" else url
    item: dict[str, Any] = {"resourceId": resource_id, "kind": kind, "mediaType": media_type, "availability": availability, "derivedHandle": url}
    if external_target:
        item["externalTarget"] = external_target
    builder.add_item("resources", item, "resourceId")
    return resource_id


def _link_relation(builder: DocumentBuilder, source_id: str, target_id: str, identity: str, *, status: str = "preserved") -> None:
    relation_id = safe_id("relation", identity)
    if builder.find("relations", "relationId", relation_id) is None:
        builder.add_item("relations", {"relationId": relation_id, "kind": "links", "fromId": source_id, "toId": target_id, "status": status}, "relationId")


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


def _heading_parts(line: str) -> tuple[int, str] | None:
    value = line.lstrip(" ")
    count = 0
    while count < len(value) and value[count] == "#":
        count += 1
    if not 1 <= count <= 6 or count == len(value) or not value[count].isspace():
        return None
    content = value[count:].strip()
    return (count, content) if content else None


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
    if not line.startswith("["):
        return None
    label_end = _find_unescaped(line, "]", 1)
    if label_end < 0 or label_end + 1 >= len(line) or line[label_end + 1] != ":":
        return None
    destination, title = _destination(line[label_end + 2 :])
    return (_unescape(line[1:label_end]), destination, title) if destination else None


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


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    if not cells:
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


def _source_map(builder: DocumentBuilder, target_id: str, line: int, column: int, end_line: int, end_column: int, *, token_start: int = 0, token_end: int | None = None) -> dict[str, Any]:
    return builder.add_source_map(
        target_id,
        {
            "lineStart": line,
            "columnStart": max(1, column),
            "lineEnd": end_line,
            "columnEnd": max(1, end_column),
            "tokenStart": max(0, token_start),
            "tokenEnd": max(0, token_end if token_end is not None else end_column - column),
        },
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
    state = state or {"resourceGaps": set(), "forcePartial": False}
    tokens = _inline_tokens(raw, references) or [{"kind": "text", "raw": raw, "start": 0, "end": len(raw)}]
    first_run = ""
    first_source = ""
    for token in tokens:
        kind = token["kind"]
        token_raw = token["raw"]
        token_start = int(token["start"])
        token_end = int(token["end"])
        run_status = "unsupported" if kind.startswith("unclosed-") else "preserved"
        if kind == "reference-link" and _normalize_label(token.get("reference", "")) not in references:
            run_status = "ambiguous"
        if kind == "footnote-ref" and token.get("label", "") not in footnotes:
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
        normalized = _strip_inline(token_raw, references)
        if normalized != token_raw and not kind.startswith("unclosed-"):
            normalized_id = safe_id("text", f"markdown-normalized-{line}-{column}-{token_start}-{len(builder.document['texts'])}")
            builder.add_text(normalized_id, normalized, representation="normalized", provenance="decoded", source_text_id=source_id, source_range={"start": source_start, "end": source_end}, transformations=[{"kind": "normalize", "sourceStart": 0, "sourceEnd": len(token_raw), "targetStart": 0, "targetEnd": len(normalized)}], status="normalized")
            builder.link_text(run_id, normalized_id)

        if kind.startswith("unclosed-"):
            code = {
                "unclosed-code": "DFIR-MD-UNCLOSED-CODE-SPAN",
                "unclosed-link": "DFIR-MD-UNCLOSED-LINK",
                "unclosed-image": "DFIR-MD-UNCLOSED-IMAGE",
                "unclosed-footnote-ref": "DFIR-MD-UNCLOSED-FOOTNOTE-REF",
                "unclosed-html": "DFIR-MD-UNCLOSED-HTML",
                "unclosed-emphasis": "DFIR-MD-UNCLOSED-EMPHASIS",
            }.get(kind, "DFIR-MD-UNCLOSED-INLINE")
            diagnostic_id = _diagnostic(builder, code, f"Unclosed Markdown inline construct: {kind}.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("inline-syntax", "unsupported", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "reference-link" and run_status == "ambiguous":
            diagnostic_id = _diagnostic(builder, "DFIR-MD-REFERENCE-UNRESOLVED", "Reference-style link has no matching definition.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("reference-link", "ambiguous", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "footnote-ref" and run_status == "ambiguous":
            diagnostic_id = _diagnostic(builder, "DFIR-MD-FOOTNOTE-UNRESOLVED", "Footnote reference has no matching definition.", target_id=run_id, source_map_id=source_map["sourceMapId"])
            builder.add_feature("footnote", "ambiguous", target_id=run_id, diagnostic_ids=[diagnostic_id])
        elif kind == "footnote-ref":
            annotation_id = safe_id("annotation", f"markdown-footnote-ref-{line}-{column}-{token_start}-{token.get('label', '')}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "footnote", "targetIds": [run_id], "body": footnotes[token["label"]], "referenceId": token["label"], "status": "preserved"}, "annotationId")
            builder.add_feature("footnote", "preserved", target_id=run_id)
        elif kind in {"link", "image", "reference-link"} and (kind != "reference-link" or run_status != "ambiguous"):
            if kind == "reference-link":
                target, _title = references[_normalize_label(token["reference"])]
            else:
                target = token["target"]
            resource_id = _linked_resource(builder, target, kind="image" if kind == "image" else "linkedObject", identity=f"markdown-{kind}-{line}-{column}-{token_start}-{target}")
            node = builder.find("nodes", "nodeId", run_id)
            if node is not None:
                node.setdefault("resourceIds", []).append(resource_id)
            resource = builder.find("resources", "resourceId", resource_id)
            available = bool(resource and resource.get("availability") == "available")
            _link_relation(builder, run_id, resource_id, f"markdown-{kind}-{line}-{column}-{token_start}-{resource_id}-relation", status="preserved" if available else "unavailable")
            annotation_id = safe_id("annotation", f"markdown-{kind}-{line}-{column}-{token_start}-{target}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [run_id, resource_id], "body": target, "status": "preserved" if available else "ambiguous", "referenceId": token.get("reference") if kind == "reference-link" else None}, "annotationId")
            resource_diagnostics: list[str] = []
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

        delimiters = [token.get("marker", "")] if token.get("marker") else []
        _extension(builder, run_id, "authoring-facts", {"delimiter": delimiters[0] if delimiters else "", "delimiters": delimiters, "escaping": ["backslash"] if "\\" in token_raw else [], "lineBreak": "hard" if token_raw.endswith("  ") or token_raw.endswith("\\") else "soft", "referenceStyle": token.get("referenceStyle", "")})
        if "&" in token_raw:
            _extension(builder, run_id, "entity-or-escaping", {"source": token_raw})
        builder.add_feature("inline", "preserved", target_id=run_id)
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
) -> str:
    node_id = safe_id("node", f"markdown-{kind}-{line}-{len(builder.document['nodes'])}")
    builder.add_node(kind, node_id, parent_id=parent_id, status=status)
    segments = text.split("\n")
    for offset, segment in enumerate(segments):
        _add_inline(builder, node_id, segment, line + offset, column if offset == 0 else 1, references, footnotes, state)
    end_column = column + len(segments[0]) if len(segments) == 1 else len(segments[-1]) + 1
    builder.add_source_map(node_id, {"lineStart": line, "columnStart": column, "lineEnd": line + len(segments) - 1, "columnEnd": max(1, end_column), "tokenStart": 0, "tokenEnd": len(text)})
    return node_id


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
    for index in range(column_count):
        column_id = safe_id("node", f"markdown-column-{start_line}-{index}")
        builder.add_node("column", column_id, parent_id=table_node, status="preserved")
        column_ids.append(column_id)
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
    builder.add_item("tables", {"tableId": safe_id("table", f"markdown-{start_line}"), "nodeId": table_node, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "status": "preserved"}, "tableId")
    builder.add_source_map(table_node, {"lineStart": start_line, "columnStart": 1, "lineEnd": start_line + len(lines) - 1, "columnEnd": max(1, len(lines[-1]) + 1), "tokenStart": 0, "tokenEnd": sum(len(line) for line in lines)})
    alignment = []
    if len(lines) >= 2:
        for cell in _split_table_row(lines[1]):
            value = cell.strip()
            alignment.append("center" if value.startswith(":") and value.endswith(":") else "right" if value.endswith(":") else "left")
    _extension(builder, table_node, "table-authoring", {"delimiter": "|", "alignment": alignment or ["source"]})
    return table_node


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        builder = DocumentBuilder(path, "markdown", "commonmark", limits=limits)
        diagnostic = builder.add_diagnostic("DFIR-MD-UTF8-FAILED", str(exc), severity="error", phase="parse")
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")

    builder = DocumentBuilder(path, "markdown", "commonmark", limits=limits)
    lines = source.splitlines()
    if not lines:
        source_map = _source_map(builder, builder.root_id, 1, 1, 1, 1, token_start=0, token_end=0)
        diagnostic = _diagnostic(builder, "DFIR-MD-EMPTY-SOURCE", "Markdown source is empty.", target_id=builder.root_id, source_map_id=source_map["sourceMapId"], severity="error", phase="parse")
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
    if "\x00" in source:
        full_map = _source_map(builder, builder.root_id, 1, 1, len(lines), len(lines[-1]) + 1, token_start=0, token_end=len(source))
        nul_offset = source.index("\x00")
        nul_line = source.count("\n", 0, nul_offset) + 1
        nul_column = nul_offset - (source.rfind("\n", 0, nul_offset) + 1) + 1
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
    footnotes: dict[str, str] = {}
    for item in lines:
        reference = _reference_parts(item)
        if reference:
            label, target, title = reference
            references.setdefault(_normalize_label(label), (target, title))
        footnote = _footnote_parts(item)
        if footnote:
            label, body = footnote
            footnotes.setdefault(label, body)
    state: dict[str, Any] = {"resourceGaps": set(), "forcePartial": False}

    start_index = 0
    if lines[0].strip() == "---":
        end = next((candidate for candidate in range(1, len(lines)) if lines[candidate].strip() == "---"), None)
        close_line = end + 1 if end is not None else len(lines)
        front_map = _source_map(builder, builder.root_id, 1, 1, close_line, len(lines[close_line - 1]) + 1, token_start=0, token_end=sum(len(item) for item in lines[:close_line]))
        entries: list[dict[str, str]] = []
        malformed = False
        for item in lines[1 : end if end is not None else len(lines)]:
            match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)", item)
            if match:
                entries.append({"key": match.group(1), "value": match.group(2)})
            elif item.strip():
                malformed = True
        _extension(builder, builder.root_id, "front-matter", {"entries": entries})
        if end is None:
            diagnostic = _diagnostic(builder, "DFIR-MD-UNCLOSED-FRONT-MATTER", "Front matter has no closing --- delimiter.", target_id=builder.root_id, source_map_id=front_map["sourceMapId"])
            builder.add_feature("front-matter", "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
            state["forcePartial"] = True
            start_index = len(lines)
        elif malformed:
            diagnostic = _diagnostic(builder, "DFIR-MD-FRONT-MATTER-UNSUPPORTED", "Front matter contains a value outside the bounded scalar key/value dialect.", target_id=builder.root_id, source_map_id=front_map["sourceMapId"])
            builder.add_feature("front-matter", "ambiguous", target_id=builder.root_id, diagnostic_ids=[diagnostic])
            state["forcePartial"] = True
            start_index = end + 1
        else:
            builder.add_feature("front-matter", "preserved", target_id=builder.root_id)
            start_index = end + 1

    records = [(item, number, 1) for number, item in enumerate(lines[start_index:], start=start_index + 1)]

    def directive_start(item: str) -> bool:
        stripped = item.lstrip(" ")
        return len(item) - len(stripped) <= 3 and stripped.startswith(":::")

    def block_start(index: int) -> bool:
        item = records[index][0]
        if directive_start(item) or _fence_parts(item) or _heading_parts(item) or _list_parts(item) or item.lstrip().startswith(">"):
            return True
        if _reference_parts(item) or _footnote_parts(item):
            return True
        return index + 1 < len(records) and "|" in item and _is_table_separator(records[index + 1][0])

    def parse_list(index: int, parent_id: str) -> int:
        first = _list_parts(records[index][0])
        if first is None:
            return index
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
            task = bool(re.match(r"^\[(?: |x|X)\](?:\s+|$)", content))
            item_id = _paragraph(builder, list_id, content, records[index][1], column=item_column, references=references, footnotes=footnotes, state=state, status="unsupported" if task else "preserved")
            _extension(builder, item_id, "list-marker", {"marker": marker, "level": level})
            if task:
                item_map = builder.find("sourceMaps", "targetId", item_id)
                diagnostic = _diagnostic(builder, "DFIR-MD-TASK-LIST-UNSUPPORTED", "GFM task-list marker is outside the declared Markdown dialect.", target_id=item_id, source_map_id=item_map["sourceMapId"] if item_map else None)
                builder.add_feature("task-list", "unsupported", target_id=item_id, diagnostic_ids=[diagnostic])
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
                node = _paragraph(builder, parent_id, "\n".join(item[0] for item in node_records), node_records[0][1], column=node_records[0][2], references=references, footnotes=footnotes, state=state, status="unsupported")
                source_map = _source_map(builder, node, number, column, records[final][1], len(records[final][0]) + 1, token_start=0, token_end=sum(len(item[0]) for item in records[index : final + 1]))
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
                node = _paragraph(builder, parent_id, "\n".join(item[0] for item in node_records), node_records[0][1], column=node_records[0][2], references=references, footnotes=footnotes, state=state, status="preserved" if end is not None else "unsupported")
                source_map = _source_map(builder, node, number, column, records[final][1], len(records[final][0]) + 1, token_start=0, token_end=sum(len(item[0]) for item in records[index : final + 1]))
                _extension(builder, node, "code-block", {"fence": fence[0], "language": fence[1], "content": "\n".join(item[0] for item in content)})
                if end is None:
                    diagnostic = _diagnostic(builder, "DFIR-MD-UNCLOSED-FENCE", "Fenced code block has no matching closing delimiter.", target_id=node, source_map_id=source_map["sourceMapId"])
                    builder.add_feature("code-block", "unsupported", target_id=node, diagnostic_ids=[diagnostic])
                else:
                    builder.add_feature("code-block", "preserved", target_id=node)
                index = final + 1
                continue
            heading = _heading_parts(text)
            if heading:
                node = _paragraph(builder, parent_id, heading[1], number, kind="heading", column=max(1, text.find(heading[1]) + 1 if heading[1] else 2), references=references, footnotes=footnotes, state=state)
                _extension(builder, node, "heading-authoring", {"level": heading[0], "marker": "#" * heading[0]})
                builder.add_feature("heading", "preserved", target_id=node)
                index += 1
                continue
            if index + 1 < len(records) and text.strip() and re.fullmatch(r" {0,3}(?:=+|-+)\s*", records[index + 1][0]) and not ("|" in records[index + 1][0] and _is_table_separator(records[index + 1][0])):
                level = 1 if "=" in records[index + 1][0] else 2
                node = _paragraph(builder, parent_id, text, number, kind="heading", column=column, references=references, footnotes=footnotes, state=state)
                _extension(builder, node, "heading-authoring", {"level": level, "marker": "=" if level == 1 else "-"})
                builder.add_feature("heading", "preserved", target_id=node)
                _source_map(builder, node, number, column, records[index + 1][1], len(records[index + 1][0]) + 1, token_start=0, token_end=len(text) + len(records[index + 1][0]))
                index += 2
                continue
            footnote = _footnote_parts(text)
            if footnote:
                label, body = footnote
                annotation_id = safe_id("annotation", f"markdown-footnote-{number}-{label}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "footnote", "targetIds": [parent_id], "body": body, "referenceId": label, "status": "preserved"}, "annotationId")
                _source_map(builder, annotation_id, number, column, number, len(text) + 1, token_start=0, token_end=len(text))
                builder.add_feature("footnote", "preserved", target_id=annotation_id)
                index += 1
                continue
            reference = _reference_parts(text)
            if reference:
                label, target, title = reference
                annotation_id = safe_id("annotation", f"markdown-reference-{number}-{_normalize_label(label)}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "bookmark", "targetIds": [parent_id], "body": target, "referenceId": label, "status": "preserved"}, "annotationId")
                _source_map(builder, annotation_id, number, column, number, len(text) + 1, token_start=0, token_end=len(text))
                _extension(builder, parent_id, "reference-definition", {"label": label, "destination": target, "title": title})
                builder.add_feature("reference-definition", "preserved", target_id=annotation_id)
                index += 1
                continue
            parts = _list_parts(text)
            if parts:
                index = parse_list(index, parent_id)
                continue
            if index + 1 < len(records) and "|" in text and _is_table_separator(records[index + 1][0]):
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
                parse_blocks(group, section_id)
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
    builder.add_item("orders", {"orderId": safe_id("order", "markdown-source"), "kind": "source", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": ordinal} for ordinal, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
    builder.add_feature("block-structure", "preserved", target_id=builder.root_id)
    return builder.finish(status="partial" if state["forcePartial"] else None)
