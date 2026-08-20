"""Independent source oracle for the bounded format qualification profiles.

The oracle intentionally does not import an adapter, the IR builder, the
canonicalizer, or the query layer.  It reads the source container with small
format-specific routines and returns facts that a converted document must
preserve or diagnose.  It is a qualification oracle, not a claim of complete
ECMA-376, PDF, or CommonMark implementation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any


class OracleFailure(AssertionError):
    """Raised when source facts and converted facts disagree."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _xml_text(root: ET.Element) -> list[str]:
    return [item.text or "" for item in root.iter() if _local(item.tag) in {"t", "delText", "instrText"} and item.text is not None]


def _docx_oracle(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = sorted(name.replace("\\", "/") for name in archive.namelist())
        xml_parts = [name for name in names if name.lower().endswith(".xml")]
        content_parts = [name for name in xml_parts if name != "[Content_Types].xml" and "/_rels/" not in name and not name.endswith(".rels")]
        texts: list[str] = []
        style_ids: list[str] = []
        counts = {"tables": 0, "fields": 0, "revisions": 0, "annotations": 0}
        for name in xml_parts:
            try:
                root = _xml(archive.read(name))
            except (KeyError, ET.ParseError):
                continue
            texts.extend(_xml_text(root))
            for item in root.iter():
                local = _local(item.tag)
                if local == "style" and item.attrib:
                    style = next((value for key, value in item.attrib.items() if key.rsplit("}", 1)[-1] == "styleId"), None)
                    if style:
                        style_ids.append(style)
                counts["tables"] += local == "tbl"
                counts["fields"] += local in {"fldSimple", "fldChar"}
                counts["revisions"] += local in {"ins", "del", "moveFrom", "moveTo"}
                counts["annotations"] += local in {"comment", "commentRangeStart", "commentReference", "footnote", "endnote", "hyperlink"}
        relationships: list[dict[str, str]] = []
        for name in names:
            if not name.endswith(".rels"):
                continue
            try:
                root = _xml(archive.read(name))
            except (KeyError, ET.ParseError):
                continue
            for relation in root:
                if _local(relation.tag) == "Relationship":
                    relationships.append({"id": relation.attrib.get("Id", ""), "target": relation.attrib.get("Target", ""), "type": relation.attrib.get("Type", "")})
        return {
            "format": "docx",
            "sourceDigest": _sha256(path),
            "packageParts": names,
            "contentParts": content_parts,
            "texts": texts,
            "styleIds": sorted(set(style_ids)),
            "relationships": relationships,
            **counts,
        }


def _xlsx_oracle(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = sorted(name.replace("\\", "/") for name in archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _xml(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.text or "" for item in si.iter() if _local(item.tag) == "t") for si in root if _local(si.tag) == "si"]
        workbook = _xml(archive.read("xl/workbook.xml")) if "xl/workbook.xml" in names else None
        workbook_properties = next((item for item in workbook.iter() if _local(item.tag) == "workbookPr"), None) if workbook is not None else None
        date_system_explicit = workbook_properties is not None and "date1904" in workbook_properties.attrib
        date_system = "1904" if date_system_explicit and workbook_properties.attrib.get("date1904") in {"1", "true"} else "1900"
        cells: list[dict[str, Any]] = []
        formula_count = 0
        for name in names:
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            root = _xml(archive.read(name))
            for cell in root.iter():
                if _local(cell.tag) != "c":
                    continue
                reference = cell.attrib.get("r", "")
                cell_type = cell.attrib.get("t", "n")
                value_node = next((item for item in cell if _local(item.tag) == "v"), None)
                raw = value_node.text if value_node is not None else None
                if cell_type == "s" and raw is not None:
                    try:
                        value: Any = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = raw
                else:
                    value = raw
                formula_node = next((item for item in cell if _local(item.tag) == "f"), None)
                formula = formula_node.text if formula_node is not None else None
                formula_count += formula is not None
                cells.append({"ref": reference, "type": cell_type, "value": value, "raw": raw, "formula": formula, "style": cell.attrib.get("s", "0")})
        tables: list[dict[str, str]] = []
        for name in names:
            if name.startswith("xl/tables/") and name.endswith(".xml"):
                root = _xml(archive.read(name))
                tables.append({"name": root.attrib.get("name", ""), "ref": root.attrib.get("ref", "")})
        relationships = sum(1 for name in names if name.endswith(".rels") for item in _xml(archive.read(name)) if _local(item.tag) == "Relationship")
        return {
            "format": "xlsx",
            "sourceDigest": _sha256(path),
            "packageParts": names,
            "cells": cells,
            "formulas": [item["formula"] for item in cells if item.get("formula") is not None],
            "formulaCount": formula_count,
            "dateSystem": date_system,
            "dateSystemExplicit": date_system_explicit,
            "tables": tables,
            "relationships": relationships,
        }


def _pdf_oracle(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("latin-1", errors="replace")
    objects = re.findall(rb"(?m)^\s*(\d+)\s+(\d+)\s+obj\b", raw)
    cmap_ranges = [match.span() for match in re.finditer(r"(?ms)\d+\s+\d+\s+obj\b.*?endobj", text) if "/CMapName" in match.group(0)]
    literals = []
    for match in re.finditer(r"\((?:\\.|[^()\\])*\)", text):
        # CMap names are dictionary metadata, not displayed PDF text.  Keep
        # the source oracle independent, but do not turn `/CMapName
        # /Adobe-Identity-UCS` into a false text-literal requirement.
        if any(start <= match.start() < end for start, end in cmap_ranges):
            continue
        value = match.group(0)[1:-1]
        value = re.sub(r"\\([\\()])", r"\1", value)
        if value:
            literals.append(value)
    operators = sorted(set(re.findall(r"(?<![A-Za-z])(?:BT|ET|Tf|Td|TD|Tm|Tj|TJ|cm|m|l|c|v|y|re|h|W\*?|n|S|s|f\*?|B\*?|b\*?)\b", text)))
    return {
        "format": "pdf",
        "sourceDigest": _sha256(path),
        "objectCount": len(objects),
        "pageCount": len(re.findall(rb"/Type\s*/Page\b", raw)),
        "textLiterals": literals,
        "operators": operators,
        "hasFontResource": b"/Font" in raw,
    }


def _markdown_oracle(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    source_tokens: set[str] = set()

    def add_token(value: str) -> None:
        value = re.sub(r"`([^`]*)`", r"\1", value)
        value = re.sub(r"[*_~]", "", value).replace(r"\|", "|").strip()
        if value:
            source_tokens.add(value)

    in_front_matter = bool(lines and lines[0].strip() == "---")
    for line in lines:
        stripped = line.strip()
        if in_front_matter:
            if stripped == "---":
                in_front_matter = False
            elif ":" in stripped:
                value = stripped.split(":", 1)[1].strip()
                if value:
                    add_token(value)
            continue
        if not stripped or stripped == "---":
            continue
        heading = re.match(r" {0,3}#{1,6}\s+(.*?)\s*$", line)
        if heading:
            add_token(heading.group(1))
        for match in re.finditer(r"!?\[([^\]]+)\]\(([^)]+)\)", line):
            add_token(match.group(1))
            add_token(match.group(2))
        for pattern in (r"\*\*([^*]+)\*\*", r"`([^`]+)`"):
            for match in re.finditer(pattern, line):
                add_token(match.group(1))
        if re.search(r"(?<!\\)\|", line) and not re.fullmatch(r"\s*[|:\-\s]+\s*", line):
            cells = line.replace(r"\|", "<escaped-pipe>").split("|")
            for cell in cells:
                cell = cell.strip().replace("<escaped-pipe>", "|")
                if cell:
                    add_token(cell)
        marker = re.match(r"^\s{0,3}(?:>\s*|(?:[-+*]|\d+[.)])\s+)", line)
        if marker:
            quote_or_list = line[marker.end():].strip()
            quote_or_list = re.sub(r"^(?:[-+*]|\d+[.)])\s+", "", quote_or_list)
            add_token(quote_or_list)
    return {
        "format": "markdown",
        "sourceDigest": _sha256(path),
        "lineCount": len(lines),
        "headings": sum(bool(re.match(r" {0,3}#{1,6}(?:\s|$)", line)) for line in lines),
        "lists": sum(bool(re.match(r"\s{0,3}(?:[-+*]|\d+[.)])\s+", line)) for line in lines),
        "tables": sum("|" in line for line in lines),
        "fences": sum(bool(re.match(r"\s{0,3}(?:```|~~~)", line)) for line in lines),
        "links": len(re.findall(r"!?\[[^\]]*\]\([^)]*\)", source)),
        "references": len(re.findall(r"^\s*\[[^\]]+\]:\s+", source, re.MULTILINE)),
        "directives": sum(line.lstrip().startswith(":::") for line in lines),
        "taskMarkers": sum(bool(re.match(r"\s{0,3}(?:[-+*]|\d+[.)])\s+\[[ xX]\](?:\s|$)", line)) for line in lines),
        "sourceTokens": sorted(token for token in source_tokens if len(token) >= 3),
        "rawText": source,
    }


def source_oracle(path: Path, format_name: str) -> dict[str, Any]:
    path = Path(path)
    if format_name == "docx":
        return _docx_oracle(path)
    if format_name == "xlsx":
        return _xlsx_oracle(path)
    if format_name == "pdf":
        return _pdf_oracle(path)
    if format_name == "markdown":
        return _markdown_oracle(path)
    raise OracleFailure(f"unsupported oracle format: {format_name}")


def _all_strings(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_all_strings(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_all_strings(child) for child in value)
    return str(value)


def compare_source_to_document(oracle: dict[str, Any], document: dict[str, Any], expected_tokens: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Compare independently extracted facts with the typed conversion."""

    format_name = str(oracle.get("format"))
    blob = _all_strings(document)
    assertions: list[dict[str, Any]] = []
    failures: list[str] = []

    def check(name: str, condition: bool, expected: Any, actual: Any) -> None:
        assertions.append({"id": name, "status": "passed" if condition else "failed", "expected": expected, "actual": actual})
        if not condition:
            failures.append(name)

    for token in expected_tokens:
        check(f"source-token:{token}", token in blob, True, token in blob)

    if format_name == "docx":
        actual_names = {str(item.get("name")) for item in document.get("parts", []) if isinstance(item, dict)}
        missing = sorted(set(oracle["contentParts"]) - actual_names)
        check("docx-content-parts", not missing, [], missing)
        check("docx-relationships", len(document.get("relations", [])) >= len(oracle["relationships"]), f">={len(oracle['relationships'])}", len(document.get("relations", [])))
        check("docx-text-facts", all(value in blob for value in oracle["texts"] if value), len([value for value in oracle["texts"] if value]), sum(value in blob for value in oracle["texts"] if value))
        check("docx-tables", len(document.get("tables", [])) >= oracle["tables"], f">={oracle['tables']}", len(document.get("tables", [])))
        check("docx-style-identities", all(value in blob for value in oracle["styleIds"]), oracle["styleIds"], [value for value in oracle["styleIds"] if value in blob])
        if oracle["fields"]:
            check("docx-fields", len(document.get("fields", [])) >= oracle["fields"], f">={oracle['fields']}", len(document.get("fields", [])))
        if oracle["revisions"]:
            check("docx-revisions", "revision" in blob, True, "revision" in blob)
    elif format_name == "xlsx":
        cells = [item for item in document.get("nodes", []) if isinstance(item, dict) and item.get("kind") == "cell"]
        formulas = [str(item.get("expression", {}).get("source")) for item in document.get("formulas", []) if isinstance(item, dict) and isinstance(item.get("expression"), dict)]
        check("xlsx-cells", len(cells) == len(oracle["cells"]), len(oracle["cells"]), len(cells))
        check("xlsx-formulas", formulas == oracle["formulas"], oracle["formulas"], formulas)
        date_system_present = oracle["dateSystem"] in blob
        if oracle.get("dateSystemExplicit"):
            check("xlsx-date-system", date_system_present, oracle["dateSystem"], date_system_present)
        check("xlsx-table-definitions", all(item["name"] in blob and item["ref"] in blob for item in oracle["tables"]), oracle["tables"], [item for item in oracle["tables"] if item["name"] in blob and item["ref"] in blob])
        check("xlsx-relationships", len(document.get("relations", [])) >= oracle["relationships"], f">={oracle['relationships']}", len(document.get("relations", [])))
        check("xlsx-cell-value-facts", all(str(item.get("value")) in blob for item in oracle["cells"] if item.get("value") is not None), len(oracle["cells"]), sum(str(item.get("value")) in blob for item in oracle["cells"] if item.get("value") is not None))
    elif format_name == "pdf":
        object_parts = [item for item in document.get("parts", []) if isinstance(item, dict) and item.get("kind") == "object"]
        pages = [item for item in document.get("surfaces", []) if isinstance(item, dict) and item.get("kind") == "page"]
        check("pdf-objects", len(object_parts) == oracle["objectCount"], oracle["objectCount"], len(object_parts))
        check("pdf-pages", len(pages) == oracle["pageCount"], oracle["pageCount"], len(pages))
        check("pdf-text-literals", all(value in blob for value in oracle["textLiterals"]), oracle["textLiterals"], [value for value in oracle["textLiterals"] if value in blob])
        if oracle["hasFontResource"]:
            check("pdf-font-lane", any(item.get("kind") == "font" for item in document.get("resources", []) if isinstance(item, dict)), True, any(item.get("kind") == "font" for item in document.get("resources", []) if isinstance(item, dict)))
        check("pdf-operator-evidence", bool(document.get("extensions") or document.get("diagnostics")), True, bool(document.get("extensions") or document.get("diagnostics")))
    elif format_name == "markdown":
        source_tokens = oracle.get("sourceTokens", [])
        check(
            "markdown-source-text",
            all(token in blob for token in source_tokens),
            source_tokens,
            [token for token in source_tokens if token in blob],
        )
        check("markdown-source-maps", len(document.get("sourceMaps", [])) >= max(1, oracle["lineCount"] // 2), f">={max(1, oracle['lineCount'] // 2)}", len(document.get("sourceMaps", [])))
        if oracle["headings"]:
            check("markdown-headings", "heading" in blob, True, "heading" in blob)
        if oracle["lists"]:
            check("markdown-lists", "list" in blob, True, "list" in blob)
        if oracle["tables"] >= 2:
            check("markdown-tables", len(document.get("tables", [])) >= 1, True, len(document.get("tables", [])) >= 1)
        if oracle["fences"]:
            check("markdown-fences", "code-block" in blob, True, "code-block" in blob)
        if oracle["references"]:
            check("markdown-references", "reference-definition" in blob, True, "reference-definition" in blob)
        if oracle["taskMarkers"] or oracle["directives"]:
            unsupported = any(item.get("status") == "unsupported" for item in document.get("conversion", {}).get("features", []) if isinstance(item, dict))
            check("markdown-unsupported-diagnostic", unsupported and bool(document.get("diagnostics")), True, unsupported and bool(document.get("diagnostics")))
    else:
        raise OracleFailure(f"no comparison contract for {format_name}")

    if failures:
        raise OracleFailure(f"independent oracle mismatches: {', '.join(failures)}")
    return {
        "status": "passed",
        "oracle": "independent-source-parser",
        "oracleGrade": "A-structure-literals",
        "format": format_name,
        "sourceDigest": oracle.get("sourceDigest"),
        "assertions": assertions,
        "failures": [],
    }


__all__ = ["OracleFailure", "compare_source_to_document", "source_oracle"]
