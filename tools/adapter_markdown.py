"""Stdlib Markdown parser that maps authored form facts into Document Form IR.

This is intentionally a bounded CommonMark-oriented adapter.  It preserves
source spelling and records dialect/authoring constructs as typed extensions;
it does not infer business meaning from Markdown.
"""

from __future__ import annotations

from pathlib import Path
import mimetypes
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


def _inline_tokens(value: str) -> list[dict[str, Any]]:
    """Tokenize the bounded inline dialect without regex-driven authority."""
    tokens: list[dict[str, Any]] = []
    text_start = 0
    index = 0

    def emit_text(end: int) -> None:
        nonlocal text_start
        if end > text_start:
            tokens.append({"kind": "text", "raw": value[text_start:end], "start": text_start, "end": end})

    def emit(kind: str, end: int, **fields: Any) -> None:
        nonlocal text_start
        emit_text(index)
        tokens.append({"kind": kind, "raw": value[index:end], "start": index, "end": end, **fields})
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
                emit("code", close + len(marker), marker=marker, content=value[marker_end:close])
                index = close + len(marker)
                continue
        if value.startswith("![", index) or value[index] == "[":
            image = value.startswith("![", index)
            label_start = index + 2 if image else index + 1
            label_end = _find_unescaped(value, "]", label_start)
            if label_end >= 0:
                after = label_end + 1
                if after < len(value) and value[after] == "(":
                    destination_end = _find_unescaped(value, ")", after + 1)
                    if destination_end >= 0:
                        target, title = _destination(value[after + 1 : destination_end])
                        if target:
                            emit("image" if image else "link", destination_end + 1, label=_unescape(value[label_start:label_end]), target=target, title=title)
                            index = destination_end + 1
                            continue
                if not image and after < len(value) and value[after] == "[":
                    reference_end = _find_unescaped(value, "]", after + 1)
                    if reference_end >= 0:
                        emit("reference-link", reference_end + 1, label=_unescape(value[label_start:label_end]), reference=_unescape(value[after + 1 : reference_end]))
                        index = reference_end + 1
                        continue
        if value[index] == "<":
            close = _find_unescaped(value, ">", index + 1)
            if close >= 0 and ("/" in value[index:close + 1] or value[index + 1 : close].isalpha()):
                emit("raw-html", close + 1, source=value[index:close + 1])
                index = close + 1
                continue
        marker = ""
        if value.startswith("**", index) or value.startswith("__", index):
            marker = value[index:index + 2]
        elif value[index] in "*_":
            marker = value[index]
        if marker:
            close = _find_unescaped(value, marker, index + len(marker))
            if close > index + len(marker):
                emit("emphasis", close + len(marker), marker=marker, content=value[index + len(marker) : close])
                index = close + len(marker)
                continue
        index += 1
    emit_text(len(value))
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
        "capabilities": ["blocks", "inline", "authoring", "references", "raw-html", "source-maps"],
        "limits": {"maxInputBytes": limits.max_input_bytes, "maxTextChars": limits.max_text_chars},
    }


def _strip_inline(value: str) -> str:
    normalized: list[str] = []
    for token in _inline_tokens(value):
        kind = token["kind"]
        if kind == "text":
            normalized.append(_unescape(token["raw"]))
        elif kind == "code":
            normalized.append(token["content"])
        elif kind in {"emphasis", "link", "reference-link", "image"}:
            normalized.append(_strip_inline(token.get("content", token.get("label", ""))))
        elif kind == "raw-html":
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


def _split_table_row(line: str) -> list[str]:
    value = line.strip()
    has_outer_left = value.startswith("|")
    has_outer_right = value.endswith("|") and not value.endswith("\\|")
    if has_outer_left:
        value = value[1:]
    if has_outer_right:
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_ticks = 0
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == "`":
            code_ticks = 0 if code_ticks else 1
            current.append(character)
            continue
        if character == "|" and code_ticks == 0:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    cells.append("".join(current).strip())
    return cells


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


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    if len(cells) < 2:
        return False
    for cell in cells:
        value = cell.strip()
        if value.startswith(":"):
            value = value[1:]
        if value.endswith(":"):
            value = value[:-1]
        if not value or any(character != "-" for character in value):
            return False
    return True


def _add_inline(builder: DocumentBuilder, parent_id: str, raw: str, line: int, column: int) -> tuple[str, str]:
    run_id = safe_id("node", f"markdown-run-{line}-{column}-{len(builder.document['nodes'])}")
    builder.add_node("run", run_id, parent_id=parent_id, status="preserved")
    source_range = {"start": max(0, column - 1), "end": max(0, column - 1) + len(raw)}
    source_id = safe_id("text", f"markdown-source-{line}-{column}-{len(builder.document['texts'])}")
    builder.add_text(source_id, raw, representation="source", provenance="authored", source_range=source_range, transformations=[{"kind": "identity", "sourceStart": 0, "sourceEnd": len(raw), "targetStart": 0, "targetEnd": len(raw)}])
    builder.link_text(run_id, source_id)
    normalized = _strip_inline(raw)
    if normalized != raw:
        normalized_id = safe_id("text", f"markdown-normalized-{line}-{column}-{len(builder.document['texts'])}")
        builder.add_text(normalized_id, normalized, representation="normalized", provenance="decoded", source_text_id=source_id, source_range=source_range, transformations=[{"kind": "normalize", "sourceStart": 0, "sourceEnd": len(raw), "targetStart": 0, "targetEnd": len(normalized)}], status="normalized")
        builder.link_text(run_id, normalized_id)
    builder.add_source_map(run_id, {"lineStart": line, "columnStart": column, "lineEnd": line, "columnEnd": column + len(raw), "tokenStart": 0, "tokenEnd": len(raw)})
    delimiters: list[str] = []
    reference_style = ""
    for token in _inline_tokens(raw):
        kind = token["kind"]
        if kind in {"emphasis", "code"}:
            delimiters.append(token.get("marker", ""))
        if kind in {"link", "image"}:
            target = token["target"]
            label = token.get("label", "")
            resource_id = _linked_resource(builder, target, kind="image" if kind == "image" else "linkedObject", identity=f"markdown-{kind}-{line}-{column}-{len(builder.document['resources'])}")
            node = builder.find("nodes", "nodeId", run_id)
            if node is not None:
                node.setdefault("resourceIds", []).append(resource_id)
            resource = builder.find("resources", "resourceId", resource_id)
            _link_relation(builder, run_id, resource_id, f"markdown-{kind}-{line}-{column}-{resource_id}-relation", status="preserved" if resource and resource.get("availability") == "available" else "unavailable")
            annotation_id = safe_id("annotation", f"markdown-{kind}-{line}-{column}-{label}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [run_id, resource_id], "body": target, "status": "preserved" if resource and resource.get("availability") != "unavailable" else "ambiguous"}, "annotationId")
        elif kind == "reference-link":
            reference_style = "reference"
        elif kind == "raw-html":
            _extension(builder, run_id, "raw-html", {"source": token["source"]})
    _extension(builder, run_id, "authoring-facts", {"delimiter": delimiters[0] if delimiters else "", "delimiters": delimiters, "escaping": ["backslash"] if "\\" in raw else [], "lineBreak": "hard" if raw.endswith("  ") or raw.endswith("\\") else "soft", "referenceStyle": reference_style})
    if "&" in raw:
        _extension(builder, run_id, "entity-or-escaping", {"source": raw})
    return run_id, source_id


def _paragraph(builder: DocumentBuilder, parent_id: str, text: str, line: int, *, kind: str = "paragraph") -> str:
    node_id = safe_id("node", f"markdown-{kind}-{line}-{len(builder.document['nodes'])}")
    builder.add_node(kind, node_id, parent_id=parent_id, status="preserved")
    _add_inline(builder, node_id, text, line, 1)
    builder.add_source_map(node_id, {"lineStart": line, "columnStart": 1, "lineEnd": line, "columnEnd": max(1, len(text) + 1), "tokenStart": 0, "tokenEnd": len(text)})
    return node_id


def _table(builder: DocumentBuilder, parent_id: str, lines: list[str], start_line: int) -> str:
    table_node = safe_id("node", f"markdown-table-{start_line}")
    builder.add_node("table", table_node, parent_id=parent_id, status="preserved")
    row_ids: list[str] = []
    column_count = max((len(_split_table_row(line)) for line in lines), default=1)
    column_ids: list[str] = []
    for index in range(column_count):
        column_id = safe_id("node", f"markdown-column-{start_line}-{index}")
        builder.add_node("column", column_id, parent_id=table_node, status="preserved")
        column_ids.append(column_id)
    cell_ids: list[str] = []
    for row_offset, line in enumerate(lines):
        row_id = safe_id("node", f"markdown-row-{start_line}-{row_offset}")
        builder.add_node("row", row_id, parent_id=table_node, status="preserved")
        row_ids.append(row_id)
        cells = _split_table_row(line)
        for col, cell_text in enumerate(cells):
            cell_id = safe_id("node", f"markdown-cell-{start_line}-{row_offset}-{col}")
            builder.add_node("cell", cell_id, parent_id=row_id, status="preserved", address={"row": row_offset + 1, "column": col + 1})
            _add_inline(builder, cell_id, cell_text, start_line + row_offset, 1)
            cell_ids.append(cell_id)
    builder.add_item("tables", {"tableId": safe_id("table", f"markdown-{start_line}"), "nodeId": table_node, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "status": "preserved"}, "tableId")
    builder.add_source_map(table_node, {"lineStart": start_line, "columnStart": 1, "lineEnd": start_line + len(lines) - 1, "columnEnd": max(1, len(lines[-1]) + 1), "tokenStart": 0, "tokenEnd": sum(len(line) for line in lines)})
    _extension(builder, table_node, "table-authoring", {"delimiter": "|", "alignment": ["source"]})
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
    if "\x00" in source:
        diagnostic = builder.add_diagnostic("DFIR-MD-NUL-CHARACTER", "NUL characters are not valid authored Markdown text.", severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("source-decoding", "failed", diagnostic_ids=[diagnostic])
    lines = source.splitlines()
    index = 0
    front_matter: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            for item in lines[1:end]:
                if ":" in item:
                    key, value = item.split(":", 1)
                    front_matter[key.strip()] = value.strip()
            _extension(builder, builder.root_id, "front-matter", {"entries": [{"key": key, "value": value} for key, value in sorted(front_matter.items())]})
            builder.add_feature("front-matter", "preserved", target_id=builder.root_id)
            index = end + 1

    paragraph_lines: list[str] = []
    paragraph_start = index + 1

    def flush() -> None:
        nonlocal paragraph_lines, paragraph_start
        if paragraph_lines:
            _paragraph(builder, builder.root_id, "\n".join(paragraph_lines), paragraph_start)
            paragraph_lines = []

    while index < len(lines):
        line = lines[index]
        number = index + 1
        if not line.strip():
            flush()
            index += 1
            paragraph_start = index + 1
            continue
        if line.startswith(":::"):
            flush()
            end = next((j for j in range(index + 1, len(lines)) if lines[j].startswith(":::")), None)
            body_lines = lines[index + 1 : end] if end is not None else lines[index + 1 :]
            node_id = _paragraph(builder, builder.root_id, "\n".join(body_lines), number)
            diagnostic = builder.add_diagnostic(
                "DFIR-MD-DIRECTIVE-UNSUPPORTED",
                "Markdown directive syntax is outside the bounded CommonMark adapter.",
                target_id=node_id,
                phase="normalize",
            )
            _extension(builder, node_id, "unsupported-directive", {"opening": line, "body": body_lines}, criticality="non-critical")
            builder.add_feature("directive", "unsupported", target_id=node_id, diagnostic_ids=[diagnostic])
            index = len(lines) if end is None else end + 1
            paragraph_start = index + 1
            continue
        fence_parts = _fence_parts(line)
        if fence_parts:
            flush()
            fence, language = fence_parts
            end = next((j for j in range(index + 1, len(lines)) if _fence_parts(lines[j]) and lines[j].lstrip().startswith(fence)), None)
            if end is None:
                diagnostic = builder.add_diagnostic("DFIR-MD-UNCLOSED-FENCE", "Fenced code block has no closing delimiter.", target_id=builder.root_id)
                code_lines = lines[index + 1 :]
                index = len(lines)
            else:
                code_lines = lines[index + 1 : end]
                index = end + 1
            node_id = _paragraph(builder, builder.root_id, "\n".join(code_lines), number)
            _extension(builder, node_id, "code-block", {"fence": fence, "language": language, "content": "\n".join(code_lines)})
            builder.add_feature("code-block", "preserved", target_id=node_id)
            continue
        heading = _heading_parts(line)
        if heading:
            flush()
            level, content = heading
            node_id = _paragraph(builder, builder.root_id, content, number, kind="heading")
            _extension(builder, node_id, "heading-authoring", {"level": level, "marker": "#" * level})
            builder.add_feature("heading", "preserved", target_id=node_id)
            index += 1
            paragraph_start = index + 1
            continue
        footnote = _footnote_parts(line)
        if footnote:
            flush()
            label, body = footnote
            annotation_id = safe_id("annotation", f"markdown-footnote-{label}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "footnote", "targetIds": [builder.root_id], "body": body, "status": "preserved"}, "annotationId")
            index += 1
            continue
        reference = _reference_parts(line)
        if reference:
            flush()
            label, target, title = reference
            annotation_id = safe_id("annotation", f"markdown-reference-{label}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "bookmark", "targetIds": [builder.root_id], "body": target, "status": "preserved"}, "annotationId")
            _extension(builder, builder.root_id, "reference-definition", {"label": label, "destination": target, "title": title})
            index += 1
            continue
        list_parts = _list_parts(line)
        if list_parts:
            flush()
            list_id = safe_id("node", f"markdown-list-{number}")
            builder.add_node("list", list_id, parent_id=builder.root_id, status="preserved")
            while index < len(lines):
                item_parts = _list_parts(lines[index])
                if not item_parts:
                    break
                marker, content, level = item_parts
                item_node = _paragraph(builder, list_id, content, index + 1)
                _extension(builder, item_node, "list-marker", {"marker": marker, "level": level})
                index += 1
            builder.add_feature("list", "preserved", target_id=list_id)
            paragraph_start = index + 1
            continue
        if "|" in line and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            flush()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_lines.append(lines[index])
                index += 1
            table_node = _table(builder, builder.root_id, table_lines, number)
            builder.add_feature("table", "preserved", target_id=table_node)
            paragraph_start = index + 1
            continue
        if line.lstrip().startswith(">"):
            flush()
            node_id = _paragraph(builder, builder.root_id, line.lstrip()[1:].lstrip(), number)
            _extension(builder, node_id, "blockquote", {"marker": ">"})
            index += 1
            continue
        if not paragraph_lines:
            paragraph_start = number
        paragraph_lines.append(line)
        index += 1
    flush()

    builder.add_item("orders", {"orderId": safe_id("order", "markdown-source"), "kind": "source", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": ordinal} for ordinal, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
    builder.add_feature("block-structure", "preserved", target_id=builder.root_id)
    return builder.finish()
