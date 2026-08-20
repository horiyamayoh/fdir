"""Stdlib Markdown parser that maps authored form facts into Document Form IR.

This is intentionally a bounded CommonMark-oriented adapter.  It preserves
source spelling and records dialect/authoring constructs as typed extensions;
it does not infer business meaning from Markdown.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, input_limit_check, safe_id


INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|`[^`]+`|!\[[^]]*\]\([^)]*\)|\[[^]]+\]\([^)]*\)|\[[^]]+\]\[[^]]+\])")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
LIST_RE = re.compile(r"^([*+-]|\d+[.)])[ \t]+(.+?)\s*$")
REFERENCE_RE = re.compile(r"^\[([^]]+)\]:\s*(\S+)(?:\s+[\"']([^\"']+)[\"'])?\s*$")
FOOTNOTE_RE = re.compile(r"^\[\^([^]]+)\]:\s*(.+)$")


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
    value = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\[[^]]+\]", r"\1", value)
    value = re.sub(r"(`+|\*\*|__|\*|_)", "", value)
    value = value.replace("\\", "")
    return value


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


def _add_inline(builder: DocumentBuilder, parent_id: str, raw: str, line: int, column: int) -> tuple[str, str]:
    run_id = safe_id("node", f"markdown-run-{line}-{column}-{len(builder.document['nodes'])}")
    builder.add_node("run", run_id, parent_id=parent_id, status="preserved")
    source_id = safe_id("text", f"markdown-source-{line}-{column}-{len(builder.document['texts'])}")
    builder.add_text(source_id, raw, representation="source", provenance="authored")
    builder.link_text(run_id, source_id)
    normalized = _strip_inline(raw)
    if normalized != raw:
        normalized_id = safe_id("text", f"markdown-normalized-{line}-{column}-{len(builder.document['texts'])}")
        builder.add_text(normalized_id, normalized, representation="normalized", provenance="decoded", source_text_id=source_id, status="normalized")
        builder.link_text(run_id, normalized_id)
    builder.add_source_map(run_id, {"lineStart": line, "columnStart": column, "lineEnd": line, "columnEnd": column + len(raw), "tokenStart": 0, "tokenEnd": len(raw)})
    delimiters: list[str] = []
    for token in INLINE_RE.findall(raw):
        if token.startswith("**") or token.startswith("__"):
            delimiters.append(token[:2])
        elif token.startswith("*") or token.startswith("_"):
            delimiters.append(token[0])
        elif token.startswith("`"):
            delimiters.append("`")
        if token.startswith("!["):
            match = re.match(r"!\[([^]]*)\]\((\S+?)(?:\s+[\"']([^\"']+)[\"'])?\)", token)
            if match:
                alt, url, title = match.groups()
                resource_id = safe_id("resource", f"markdown-image-{line}-{column}-{len(builder.document['resources'])}")
                builder.add_item("resources", {"resourceId": resource_id, "kind": "image", "mediaType": "application/octet-stream", "availability": "available", "derivedHandle": url}, "resourceId")
                annotation_id = safe_id("annotation", f"markdown-image-{line}-{column}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [run_id, resource_id], "body": url, "status": "preserved"}, "annotationId")
        elif token.startswith("["):
            match = re.match(r"\[([^]]+)\]\(([^)]+)\)", token)
            if match:
                label, target = match.groups()
                annotation_id = safe_id("annotation", f"markdown-link-{line}-{column}-{label}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [run_id], "body": target, "status": "preserved"}, "annotationId")
    _extension(builder, run_id, "authoring-facts", {"delimiter": delimiters[0] if delimiters else "", "delimiters": delimiters, "escaping": ["backslash"] if "\\" in raw else [], "lineBreak": "hard" if raw.endswith("  ") or raw.endswith("\\") else "soft"})
    if "<" in raw and ">" in raw:
        _extension(builder, run_id, "raw-html", {"source": raw})
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
    column_count = max((len(line.strip().strip("|").split("|")) for line in lines), default=1)
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
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
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
            _extension(builder, builder.root_id, "front-matter", front_matter)
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
        if line.startswith("```") or line.startswith("~~~"):
            flush()
            fence = line[:3]
            language = line[3:].strip()
            end = next((j for j in range(index + 1, len(lines)) if lines[j].startswith(fence)), None)
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
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            node_id = _paragraph(builder, builder.root_id, heading.group(2), number, kind="heading")
            _extension(builder, node_id, "heading-authoring", {"level": len(heading.group(1)), "marker": heading.group(1)})
            builder.add_feature("heading", "preserved", target_id=node_id)
            index += 1
            paragraph_start = index + 1
            continue
        if REFERENCE_RE.match(line):
            flush()
            match = REFERENCE_RE.match(line)
            assert match is not None
            label, target, title = match.groups()
            annotation_id = safe_id("annotation", f"markdown-reference-{label}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "bookmark", "targetIds": [builder.root_id], "body": target, "status": "preserved"}, "annotationId")
            _extension(builder, builder.root_id, "reference-definition", {"label": label, "destination": target, "title": title or ""})
            index += 1
            continue
        if FOOTNOTE_RE.match(line):
            flush()
            match = FOOTNOTE_RE.match(line)
            assert match is not None
            label, body = match.groups()
            annotation_id = safe_id("annotation", f"markdown-footnote-{label}")
            builder.add_item("annotations", {"annotationId": annotation_id, "kind": "footnote", "targetIds": [builder.root_id], "body": body, "status": "preserved"}, "annotationId")
            index += 1
            continue
        if LIST_RE.match(line):
            flush()
            list_id = safe_id("node", f"markdown-list-{number}")
            builder.add_node("list", list_id, parent_id=builder.root_id, status="preserved")
            while index < len(lines):
                match = LIST_RE.match(lines[index])
                if not match:
                    break
                item_node = _paragraph(builder, list_id, match.group(2), index + 1)
                _extension(builder, item_node, "list-marker", {"marker": match.group(1), "level": len(lines[index]) - len(lines[index].lstrip())})
                index += 1
            builder.add_feature("list", "preserved", target_id=list_id)
            paragraph_start = index + 1
            continue
        if "|" in line and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", lines[index + 1]):
            flush()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_lines.append(lines[index])
                index += 1
            _table(builder, builder.root_id, table_lines, number)
            builder.add_feature("table", "preserved")
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
