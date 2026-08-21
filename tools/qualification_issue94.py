"""Run the bounded independent qualification slice for issue #94.

Expected values and source facts are authored independently of the adapters.
The runner also invokes the public ``convert_document.py`` boundary for the
small geometry/order fixtures in the corpus and compares only a semantic
projection of the resulting IR.  It deliberately does not import adapter
modules, model contracts, or a renderer.

Every report is fail-closed: a mismatch, an unsupported vector, an
approximation reported as ``preserved``, a missing ambiguity declaration, or
unmet coverage makes the report failed.  The five reports are written even
when one category cannot be evaluated.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-94-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-94"
REPORT_NAMES = {
    "coordinate": "coordinate-transform-vectors.json",
    "geometry": "geometry-lane-report.json",
    "anchor": "anchor-resolution-report.json",
    "clip": "clip-and-paint-order-report.json",
    "reading": "reading-order-ambiguity-report.json",
}
EVIDENCE_ID = "issue-94-geometry-order"
REQUIREMENT_IDS = ["QUAL-94-GEOMETRY-ORDER"]
VERSION = "1.0.0"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
INTEGRATION_FORMATS = {"docx", "xlsx", "pdf", "markdown"}
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style execution.
    from tools.qualification_producer_report import write_producer_report


class QualificationError(RuntimeError):
    """Raised when the independent qualification cannot be evaluated safely."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON input {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise QualificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _source_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise QualificationError(f"cannot execute git: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or SHA_PATTERN.fullmatch(value) is None:
        raise QualificationError(f"git HEAD is not a 40-character SHA: {value!r}")
    return value


def _compare_exact(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    """Compare an authored projection strictly, including missing/extra keys."""

    if type(expected) is not type(actual):
        return [{"path": path, "expected": expected, "actual": actual, "kind": "type"}]
    if isinstance(expected, dict):
        mismatches: list[dict[str, Any]] = []
        expected_keys = set(expected)
        actual_keys = set(actual)
        for key in sorted(expected_keys - actual_keys):
            mismatches.append(
                {"path": f"{path}/{key}", "expected": expected[key], "actual": {"$missing": True}, "kind": "missing"}
            )
        for key in sorted(actual_keys - expected_keys):
            mismatches.append(
                {"path": f"{path}/{key}", "expected": {"$unexpected": True}, "actual": actual[key], "kind": "unexpected"}
            )
        for key in sorted(expected_keys & actual_keys):
            mismatches.extend(_compare_exact(expected[key], actual[key], f"{path}/{key}"))
        return mismatches
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [{"path": path, "expected": expected, "actual": actual, "kind": "length"}]
        mismatches: list[dict[str, Any]] = []
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            mismatches.extend(_compare_exact(expected_item, actual_item, f"{path}[{index}]"))
        return mismatches
    if expected != actual:
        return [{"path": path, "expected": expected, "actual": actual, "kind": "value"}]
    return []


def _write_authored_source(case: dict[str, Any], work: Path) -> Path:
    source = case.get("source")
    if not isinstance(source, dict):
        raise QualificationError(f"integration case {case.get('fixtureId')} has no source declaration")
    fixture_id = str(case["fixtureId"])
    format_name = str(case["format"])
    source_type = source.get("type")
    if source_type == "zip-parts":
        parts = source.get("parts")
        if not isinstance(parts, dict) or not parts:
            raise QualificationError(f"integration fixture {fixture_id} has no authored ZIP parts")
        destination = work / f"{fixture_id}.{format_name}"
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in sorted(parts):
                if not isinstance(name, str) or not name or name.startswith("/") or ".." in Path(name).parts:
                    raise QualificationError(f"unsafe authored package part {name!r}")
                value = parts[name]
                if not isinstance(value, str):
                    raise QualificationError(f"authored package part is not text: {name}")
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, value.encode("utf-8"))
        return destination
    if source_type == "repo-file":
        relative = source.get("path")
        if not isinstance(relative, str) or not relative:
            raise QualificationError(f"integration fixture {fixture_id} has no repository source path")
        source_path = (ROOT / relative).resolve()
        try:
            source_path.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise QualificationError(f"repository source escapes workspace: {relative}") from exc
        if not source_path.is_file():
            raise QualificationError(f"repository source is missing: {relative}")
        suffix = source_path.suffix or f".{format_name}"
        destination = work / f"{fixture_id}{suffix}"
        shutil.copyfile(source_path, destination)
        return destination
    raise QualificationError(f"unsupported integration source type: {source_type!r}")


def _run_public_converter(case: dict[str, Any], work: Path) -> dict[str, Any]:
    fixture_id = str(case["fixtureId"])
    source_path = _write_authored_source(case, work)
    output_path = work / f"{fixture_id}.json"
    evidence_path = work / f"{fixture_id}.evidence.json"
    command = [
        sys.executable,
        str(CONVERTER_PATH),
        "convert",
        str(source_path),
        "--format",
        str(case["format"]),
        "--out",
        str(output_path),
        "--evidence",
        str(evidence_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if not output_path.is_file() or not evidence_path.is_file():
        raise QualificationError(
            f"public converter produced no evidence for {fixture_id}; "
            f"rc={completed.returncode}; stderr={completed.stderr[-1000:]}"
        )
    document = _read_json(output_path)
    evidence = _read_json(evidence_path)
    if not isinstance(document, dict) or not isinstance(evidence, dict):
        raise QualificationError(f"public converter output is not an object: {fixture_id}")
    validation = subprocess.run(
        [sys.executable, str(CONVERTER_PATH), "validate", str(output_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    validation_report: Any = None
    try:
        validation_report = json.loads(validation.stdout)
    except json.JSONDecodeError:
        validation_report = {"status": "invalid-output", "stdout": validation.stdout[-1000:]}
    return {
        "fixtureId": fixture_id,
        "format": str(case["format"]),
        "sourcePath": str(source_path),
        "sourceSha256": _sha256_file(source_path),
        "commandExitCode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
        "document": document,
        "evidence": evidence,
        "validation": validation_report,
        "validationExitCode": validation.returncode,
    }


def _xml_member(path: Path, member: str) -> ET.Element:
    try:
        with zipfile.ZipFile(path) as archive:
            payload = archive.read(member)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise QualificationError(f"cannot read authored XML member {member}: {exc}") from exc
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise QualificationError(f"authored XML member is invalid: {member}: {exc}") from exc


def _docx_source_facts(path: Path) -> dict[str, Any]:
    root = _xml_member(path, "word/document.xml")
    body = root.find(f"{{{W_NS}}}body")
    if body is None:
        raise QualificationError("authored DOCX has no w:body")
    parent_by_child = {
        child: parent
        for parent in root.iter()
        for child in list(parent)
    }
    inline_extents: list[dict[str, Any]] = []
    drawing_parent_kinds: list[str] = []
    drawing_contexts: list[str] = []
    for drawing in root.iter(f"{{{W_NS}}}drawing"):
        parent = parent_by_child.get(drawing)
        parent_local = parent.tag.rsplit("}", 1)[-1] if parent is not None else "missing"
        drawing_parent_kinds.append("run" if parent_local == "r" else "paragraph" if parent_local == "p" else parent_local)
        drawing_contexts.append("run" if parent_local == "r" else "paragraph" if parent_local == "p" else parent_local)
    for inline in root.iter(f"{{{WP_NS}}}inline"):
        extent = inline.find(f"{{{WP_NS}}}extent")
        if extent is None:
            raise QualificationError("DOCX inline drawing has no wp:extent")
        primitive = inline.find(f".//{{{A_NS}}}prstGeom")
        if primitive is None or primitive.get("prst") is None:
            raise QualificationError("DOCX drawing has no authored preset geometry")
        try:
            cx, cy = int(extent.attrib["cx"]), int(extent.attrib["cy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationError("DOCX wp:extent is not integral") from exc
        inline_extents.append({"cx": cx, "cy": cy, "shape": primitive.attrib["prst"]})
    top_level_kinds: list[str] = []
    for child in list(body):
        local = child.tag.rsplit("}", 1)[-1]
        top_level_kinds.append("paragraph" if local == "p" else local)
    text_values = [item.text or "" for item in root.iter(f"{{{W_NS}}}t")]
    return {
        "inlineExtents": inline_extents,
        "drawingParentKinds": drawing_parent_kinds,
        "drawingContexts": drawing_contexts,
        "topLevelKinds": top_level_kinds,
        "textValues": text_values,
    }


def _xlsx_source_facts(path: Path) -> dict[str, Any]:
    root = _xml_member(path, "xl/worksheets/sheet1.xml")
    cell_order: list[str] = []
    for cell in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
        reference = cell.attrib.get("r")
        if not isinstance(reference, str) or not reference:
            raise QualificationError("XLSX source cell has no reference")
        cell_order.append(reference)
    merge_ranges = [
        str(item.attrib["ref"])
        for item in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}mergeCell")
        if isinstance(item.attrib.get("ref"), str)
    ]
    return {"cellOrder": cell_order, "mergeRanges": merge_ranges}


def _pdf_number(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _pdf_literal(value: str) -> str:
    return (
        value.replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\(", "(")
        .replace(r"\)", ")")
        .replace(r"\\", "\\")
    )


def _pdf_source_facts(path: Path) -> dict[str, Any]:
    try:
        source = path.read_bytes().decode("latin-1")
    except (OSError, UnicodeError) as exc:
        raise QualificationError(f"cannot read authored PDF: {exc}") from exc
    media = re.search(r"/MediaBox\s*\[\s*([^\]]+)\]", source)
    if media is None:
        raise QualificationError("authored PDF has no MediaBox")
    media_box = [_pdf_number(item) for item in media.group(1).split()]
    if len(media_box) != 4:
        raise QualificationError("authored PDF MediaBox is not four-dimensional")
    streams = re.findall(r"stream\s*\r?\n(.*?)\r?\nendstream", source, flags=re.DOTALL)
    if not streams:
        raise QualificationError("authored PDF has no content stream")
    stream = next((item for item in streams if " re " in item or " Tj" in item), streams[0])
    operator_pattern = re.compile(r"(?<![A-Za-z])(?:BT|ET|Tf|Td|Tj|q|Q|cm|rg|w|re|f)\b")
    operator_sequence = [item.group(0) for item in operator_pattern.finditer(stream)]
    graphics_state_operators = [
        item for item in operator_sequence
        if item in {"q", "Q", "cm", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M", "d", "ri", "gs", "sh"}
    ]
    events: list[tuple[int, dict[str, Any]]] = []
    rectangle = re.compile(
        r"(?P<x>-?\d+(?:\.\d+)?)\s+(?P<y>-?\d+(?:\.\d+)?)\s+"
        r"(?P<w>-?\d+(?:\.\d+)?)\s+(?P<h>-?\d+(?:\.\d+)?)\s+re\s+(?P<paint>[fF])"
    )
    for match in rectangle.finditer(stream):
        events.append(
            (
                match.start(),
                {
                    "kind": "path",
                    "rect": [
                        _pdf_number(match.group("x")),
                        _pdf_number(match.group("y")),
                        _pdf_number(match.group("w")),
                        _pdf_number(match.group("h")),
                    ],
                    "paint": "fill",
                },
            )
        )
    for match in re.finditer(r"\(((?:\\.|[^)])*)\)\s*Tj", stream):
        events.append((match.start(), {"kind": "text", "value": _pdf_literal(match.group(1))}))
    events.sort(key=lambda item: item[0])
    if not events:
        raise QualificationError("authored PDF content stream has no path or text paint event")
    return {
        "mediaBox": media_box,
        "graphicsStateOperators": graphics_state_operators,
        "paintEvents": [item[1] for item in events],
    }


def _markdown_source_facts(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QualificationError(f"cannot read authored Markdown: {exc}") from exc
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            index += 1
        if index >= len(lines):
            raise QualificationError("Markdown front matter is not closed")
        index += 1
    blocks: list[str] = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("#"):
            blocks.append("heading")
            index += 1
            continue
        if line.startswith(">"):
            blocks.append("blockquote")
            index += 1
            while index < len(lines) and (lines[index].startswith(">") or not lines[index].strip()):
                index += 1
            continue
        if line.startswith("|"):
            blocks.append("table")
            index += 1
            while index < len(lines) and lines[index].startswith("|"):
                index += 1
            continue
        if re.match(r"^(?:[-+*]\s+|\d+[.]\s+)", line):
            blocks.append("list")
            index += 1
            while index < len(lines) and (re.match(r"^(?:[-+*]\s+|\d+[.]\s+)", lines[index]) or not lines[index].strip()):
                index += 1
            continue
        blocks.append("paragraph")
        index += 1
        while index < len(lines) and lines[index].strip():
            if lines[index].startswith(("#", ">", "|")) or re.match(r"^(?:[-+*]\s+|\d+[.]\s+)", lines[index]):
                break
            index += 1
    return {"blockOrder": blocks}


def _source_facts(case: dict[str, Any], source_path: Path) -> dict[str, Any]:
    format_name = str(case["format"])
    if format_name == "docx":
        return _docx_source_facts(source_path)
    if format_name == "xlsx":
        return _xlsx_source_facts(source_path)
    if format_name == "pdf":
        return _pdf_source_facts(source_path)
    if format_name == "markdown":
        return _markdown_source_facts(source_path)
    raise QualificationError(f"unsupported integration format: {format_name}")


def _docx_adapter_projection(document: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    expected_geometry = expected["geometry"]
    expected_primitive = expected_geometry["primitive"]
    geometry: dict[str, Any] | None = None
    for candidate in document.get("geometries", []):
        if not isinstance(candidate, dict) or candidate.get("kind") != expected_geometry.get("kind"):
            continue
        primitives = candidate.get("primitives")
        if not isinstance(primitives, list) or len(primitives) != 1:
            continue
        if not _compare_exact(expected_primitive, primitives[0]):
            geometry = candidate
            break
    geometry_id = geometry.get("geometryId") if geometry is not None else None
    layout: dict[str, Any] | None = None
    if geometry_id is not None:
        for candidate in document.get("layouts", []):
            if not isinstance(candidate, dict):
                continue
            if candidate.get("declaredGeometryId") == geometry_id or candidate.get("resolvedGeometryId") == geometry_id:
                layout = candidate
                break
    surface_kind: str | None = None
    if layout is not None:
        layout_id = layout.get("layoutId")
        for surface in document.get("surfaces", []):
            if isinstance(surface, dict) and layout_id in surface.get("layoutIds", []):
                surface_kind = surface.get("kind")
                break
    nodes = {
        str(item.get("nodeId")): item
        for item in document.get("nodes", [])
        if isinstance(item, dict) and isinstance(item.get("nodeId"), str)
    }
    root = next((item for item in nodes.values() if item.get("kind") == "document"), None)
    top_level_order: list[str] = []
    if isinstance(root, dict):
        for node_id in root.get("childIds", []):
            node = nodes.get(str(node_id))
            if not isinstance(node, dict):
                continue
            kind = node.get("kind")
            if kind in {"heading", "paragraph", "table", "section"}:
                top_level_order.append(str(kind))
    drawing_parent_kinds: list[str] = []
    for node in nodes.values():
        if not isinstance(node, dict) or not node.get("geometryId"):
            continue
        parent = nodes.get(str(node.get("parentId")))
        if isinstance(parent, dict):
            drawing_parent_kinds.append(str(parent.get("kind")))
    return {
        "geometry": None
        if geometry is None
        else {
            "kind": geometry.get("kind"),
            "spaceId": geometry.get("spaceId"),
            "status": geometry.get("status"),
            "primitive": geometry.get("primitives", [None])[0],
        },
        "layout": None
        if layout is None
        else {
            "placement": layout.get("placement"),
            "anchorKind": (layout.get("anchor") or {}).get("kind") if isinstance(layout.get("anchor"), dict) else None,
            "status": layout.get("status"),
            "geometryLinked": layout.get("declaredGeometryId") == geometry_id or layout.get("resolvedGeometryId") == geometry_id,
            "surfaceKind": surface_kind,
        },
        "topLevelOrder": top_level_order,
        "drawingParentKinds": drawing_parent_kinds,
    }


def _column_letters(number: int) -> str:
    if number < 1:
        raise QualificationError(f"invalid spreadsheet column number: {number}")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _xlsx_adapter_projection(document: dict[str, Any]) -> dict[str, Any]:
    nodes = {
        str(item.get("nodeId")): item
        for item in document.get("nodes", [])
        if isinstance(item, dict) and isinstance(item.get("nodeId"), str)
    }
    order = next((item for item in document.get("orders", []) if isinstance(item, dict) and item.get("kind") == "grid"), None)
    cell_order: list[str] = []
    if isinstance(order, dict):
        for item in order.get("items", []):
            node = nodes.get(str(item.get("id"))) if isinstance(item, dict) else None
            address = node.get("address") if isinstance(node, dict) else None
            if not isinstance(address, dict):
                cell_order.append("<missing-address>")
                continue
            try:
                cell_order.append(f"{_column_letters(int(address['column']))}{int(address['row'])}")
            except (KeyError, TypeError, ValueError, QualificationError):
                cell_order.append("<invalid-address>")
    merged_ranges: list[str] = []
    for table in document.get("tables", []):
        if not isinstance(table, dict):
            continue
        for merged in table.get("mergedRanges", []):
            if not isinstance(merged, dict):
                continue
            if isinstance(merged.get("range"), str):
                merged_ranges.append(merged["range"])
                continue
            start = merged.get("from")
            end = merged.get("to")
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            try:
                if start.get("sheetId") != end.get("sheetId"):
                    raise QualificationError("merged range crosses sheets")
                merged_ranges.append(
                    f"{_column_letters(int(start['column']))}{int(start['row'])}:"
                    f"{_column_letters(int(end['column']))}{int(end['row'])}"
                )
            except (KeyError, TypeError, ValueError, QualificationError):
                merged_ranges.append("<invalid-merge-range>")
    return {
        "gridCellOrder": cell_order,
        "gridOrderStatus": order.get("status") if isinstance(order, dict) else None,
        "mergeRanges": merged_ranges,
    }


def _pdf_adapter_projection(document: dict[str, Any]) -> dict[str, Any]:
    nodes = {
        str(item.get("nodeId")): item
        for item in document.get("nodes", [])
        if isinstance(item, dict) and isinstance(item.get("nodeId"), str)
    }
    page_order = next(
        (
            item
            for item in document.get("orders", [])
            if isinstance(item, dict)
            and item.get("kind") == "draw"
            and "content-stream" in str(item.get("context", ""))
        ),
        None,
    )
    paint_order: list[str] = []
    if isinstance(page_order, dict):
        for item in page_order.get("items", []):
            node = nodes.get(str(item.get("id"))) if isinstance(item, dict) else None
            if isinstance(node, dict):
                kind = node.get("kind")
                if kind in {"path", "glyph", "image"}:
                    paint_order.append(str(kind))
    path_points: list[dict[str, Any]] = []
    path_node = next((item for item in nodes.values() if item.get("kind") == "path"), None)
    if isinstance(path_node, dict):
        geometry_id = path_node.get("geometryId")
        geometry = next(
            (
                item
                for item in document.get("geometries", [])
                if isinstance(item, dict) and item.get("geometryId") == geometry_id
            ),
            None,
        )
        if isinstance(geometry, dict) and isinstance(geometry.get("primitives"), list) and geometry["primitives"]:
            primitive = geometry["primitives"][0]
            if isinstance(primitive, dict) and isinstance(primitive.get("points"), list):
                path_points = primitive["points"]
    reading_order = next((item for item in document.get("orders", []) if isinstance(item, dict) and item.get("kind") == "reading"), None)
    graphics_state_operators = [
        str(extension.get("payload", {}).get("operator"))
        for extension in document.get("extensions", [])
        if isinstance(extension, dict)
        and extension.get("type") == "graphics-state"
        and isinstance(extension.get("payload"), dict)
        and isinstance(extension["payload"].get("operator"), str)
    ]
    return {
        "paintOrder": paint_order,
        "paintOrderStatus": page_order.get("status") if isinstance(page_order, dict) else None,
        "readingOrderStatus": reading_order.get("status") if isinstance(reading_order, dict) else None,
        "pathPoints": path_points,
        "graphicsStateOperators": graphics_state_operators,
    }


def _markdown_adapter_projection(document: dict[str, Any]) -> dict[str, Any]:
    nodes = {
        str(item.get("nodeId")): item
        for item in document.get("nodes", [])
        if isinstance(item, dict) and isinstance(item.get("nodeId"), str)
    }
    root = next((item for item in nodes.values() if item.get("kind") == "document"), None)
    block_order: list[str] = []
    if isinstance(root, dict):
        aliases = {"section": "blockquote"}
        for node_id in root.get("childIds", []):
            node = nodes.get(str(node_id))
            if isinstance(node, dict) and node.get("kind") in {"heading", "paragraph", "section", "table", "list"}:
                block_order.append(aliases.get(str(node.get("kind")), str(node.get("kind"))))
    source_order = next((item for item in document.get("orders", []) if isinstance(item, dict) and item.get("kind") == "source"), None)
    return {
        "blockOrder": block_order,
        "sourceOrderStatus": source_order.get("status") if isinstance(source_order, dict) else None,
    }


def _adapter_projection(case: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise QualificationError(f"integration case {case.get('fixtureId')} has no expected projection")
    format_name = str(case["format"])
    if format_name == "docx":
        return _docx_adapter_projection(document, expected)
    if format_name == "xlsx":
        return _xlsx_adapter_projection(document)
    if format_name == "pdf":
        return _pdf_adapter_projection(document)
    if format_name == "markdown":
        return _markdown_adapter_projection(document)
    raise QualificationError(f"unsupported integration format: {format_name}")


def _independent_expected_projection(case: dict[str, Any], source_facts: dict[str, Any]) -> dict[str, Any]:
    """Derive the expected public projection from source facts, independently.

    This is intentionally separate from every adapter projection helper.  A
    corpus author may describe source facts and the expected model shape, but
    cannot make the expected model shape self-validating by copying it from a
    converter result.
    """

    format_name = str(case["format"])
    if format_name == "docx":
        extents = source_facts.get("inlineExtents")
        if not isinstance(extents, list) or len(extents) != 1:
            raise QualificationError("DOCX independent projection requires exactly one inline extent")
        extent = extents[0]
        parent_kinds = source_facts.get("drawingParentKinds")
        contexts = source_facts.get("drawingContexts")
        if not isinstance(parent_kinds, list) or len(parent_kinds) != 1 or contexts != parent_kinds:
            raise QualificationError("DOCX independent projection requires exactly one drawing parent")
        top_level_order = []
        for kind in source_facts.get("topLevelKinds", []):
            if kind == "paragraph":
                top_level_order.append("paragraph")
            elif kind == "sectPr":
                top_level_order.append("section")
            else:
                top_level_order.append(str(kind))
        return {
            "geometry": {
                "kind": "rectangle",
                "spaceId": "space-docx-page",
                "status": "preserved",
                "primitive": {
                    "kind": "rectangle",
                    "x": "0",
                    "y": "0",
                    "width": {"value": str(extent["cx"]), "unit": "emu"},
                    "height": {"value": str(extent["cy"]), "unit": "emu"},
                },
            },
            "layout": {
                "placement": "inline",
                "anchorKind": "inline",
                "status": "preserved",
                "geometryLinked": True,
                "surfaceKind": "page",
            },
            "topLevelOrder": top_level_order,
            "drawingParentKinds": parent_kinds,
        }
    if format_name == "xlsx":
        cell_order = source_facts.get("cellOrder")
        merge_ranges = source_facts.get("mergeRanges")
        if not isinstance(cell_order, list) or not isinstance(merge_ranges, list):
            raise QualificationError("XLSX independent projection lacks cell or merge facts")
        return {
            "gridCellOrder": cell_order,
            "gridOrderStatus": "preserved",
            "mergeRanges": merge_ranges,
        }
    if format_name == "pdf":
        events = source_facts.get("paintEvents")
        operators = source_facts.get("graphicsStateOperators")
        if not isinstance(events, list) or not isinstance(operators, list):
            raise QualificationError("PDF independent projection lacks paint or graphics-state facts")
        kind_map = {"path": "path", "text": "glyph"}
        paint_order: list[str] = []
        path_points: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict) or event.get("kind") not in kind_map:
                raise QualificationError("PDF source paint event has an unsupported kind")
            paint_order.append(kind_map[str(event["kind"])])
            if event.get("kind") == "path" and not path_points:
                rect = event.get("rect")
                if not isinstance(rect, list) or len(rect) != 4:
                    raise QualificationError("PDF path event lacks an authored rectangle")
                x, y, width, height = rect
                path_points = [
                    {"x": str(x), "y": str(y)},
                    {"x": str(x + width), "y": str(y)},
                    {"x": str(x + width), "y": str(y + height)},
                    {"x": str(x), "y": str(y + height)},
                ]
        return {
            "paintOrder": paint_order,
            "paintOrderStatus": "preserved",
            "readingOrderStatus": "ambiguous",
            "pathPoints": path_points,
            "graphicsStateOperators": operators,
        }
    if format_name == "markdown":
        block_order = source_facts.get("blockOrder")
        if not isinstance(block_order, list):
            raise QualificationError("Markdown independent projection lacks block order")
        return {"blockOrder": block_order, "sourceOrderStatus": "preserved"}
    raise QualificationError(f"unsupported integration format: {format_name}")


def _integration_case_result(case: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    source_expected = case["sourceFacts"]
    try:
        source_actual = _source_facts(case, Path(execution["sourcePath"]))
        source_mismatches = _compare_exact(source_expected, source_actual)
    except Exception as exc:
        source_actual = {"error": f"{type(exc).__name__}: {exc}"}
        source_mismatches = [{"path": "$", "expected": source_expected, "actual": source_actual, "kind": "source-parser"}]
    try:
        independent_expected = _independent_expected_projection(case, source_actual)
        independent_oracle_mismatches = _compare_exact(independent_expected, case["expected"])
    except Exception as exc:
        independent_expected = {"error": f"{type(exc).__name__}: {exc}"}
        independent_oracle_mismatches = [{"path": "$", "expected": case["expected"], "actual": independent_expected, "kind": "independent-oracle"}]
    try:
        adapter_actual = _adapter_projection(case, execution["document"])
        adapter_mismatches = _compare_exact(case["expected"], adapter_actual)
    except Exception as exc:
        adapter_actual = {"error": f"{type(exc).__name__}: {exc}"}
        adapter_mismatches = [{"path": "$", "expected": case["expected"], "actual": adapter_actual, "kind": "adapter-projection"}]
    conversion_ok = (
        execution.get("commandExitCode") == 0
        and execution.get("validationExitCode") == 0
        and isinstance(execution.get("validation"), dict)
        and execution["validation"].get("status") == "valid"
        and execution["evidence"].get("input", {}).get("consumed") is True
        and execution["document"].get("conversion", {}).get("status") != "failed"
    )
    assertions = [
        _equal_assertion(f"{case['fixtureId']}-source-facts", "independent parser reproduces authored source facts", 0, len(source_mismatches)),
        _equal_assertion(f"{case['fixtureId']}-independent-expected", "expected public projection is derived from independent source facts", 0, len(independent_oracle_mismatches)),
        _equal_assertion(f"{case['fixtureId']}-adapter-projection", "public converter projection matches authored expected geometry/order facts", 0, len(adapter_mismatches)),
        _equal_assertion(f"{case['fixtureId']}-public-boundary", "public converter consumed and validated the real source", True, conversion_ok),
    ]
    return {
        "fixtureId": str(case["fixtureId"]),
        "format": str(case["format"]),
        "reports": list(case.get("reports", [])),
        "sourcePath": execution.get("sourcePath"),
        "sourceSha256": execution.get("sourceSha256"),
        "commandExitCode": execution.get("commandExitCode"),
        "conversionStatus": execution["document"].get("conversion", {}).get("status"),
        "sourceExpected": source_expected,
        "sourceActual": source_actual,
        "sourceMismatches": source_mismatches,
        "independentExpected": independent_expected,
        "independentOracleMismatches": independent_oracle_mismatches,
        "expected": case["expected"],
        "actual": adapter_actual,
        "adapterMismatches": adapter_mismatches,
        "conversionOk": conversion_ok,
        "status": "passed" if not source_mismatches and not independent_oracle_mismatches and not adapter_mismatches and conversion_ok else "failed",
        "assertions": assertions,
    }


def _mutate_integration(actual: dict[str, Any], kind: str) -> None:
    if kind == "change-geometry-width":
        actual["geometry"]["primitive"]["width"]["value"] = "0"
    elif kind == "remove-layout-link":
        actual["layout"]["geometryLinked"] = False
    elif kind == "swap-grid-order":
        actual["gridCellOrder"][0], actual["gridCellOrder"][1] = actual["gridCellOrder"][1], actual["gridCellOrder"][0]
    elif kind == "swap-paint-order":
        actual["paintOrder"] = list(reversed(actual["paintOrder"]))
    elif kind == "mark-reading-preserved":
        actual["readingOrderStatus"] = "preserved"
    elif kind == "swap-block-order":
        actual["blockOrder"][0], actual["blockOrder"][1] = actual["blockOrder"][1], actual["blockOrder"][0]
    else:
        raise QualificationError(f"unknown integration mutation {kind}")


def _fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise QualificationError(f"boolean is not a numeric vector value: {value!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise QualificationError(f"invalid rational vector value: {value!r}") from exc
    raise QualificationError(f"unsupported numeric vector value: {value!r}")


def _encoded(value: Any) -> int | str:
    fraction = value if isinstance(value, Fraction) else _fraction(value)
    if fraction.denominator == 1:
        return fraction.numerator
    return f"{fraction.numerator}/{fraction.denominator}"


def _encoded_point(point: Iterable[Any]) -> list[int | str]:
    return [_encoded(item) for item in point]


def _encoded_matrix(matrix: Iterable[Any]) -> list[int | str]:
    return [_encoded(item) for item in matrix]


def _matrix(value: Iterable[Any]) -> tuple[Fraction, ...]:
    result = tuple(_fraction(item) for item in value)
    if len(result) != 6:
        raise QualificationError(f"affine matrix must have 6 values: {value!r}")
    return result


def _matrix3(value: Iterable[Any]) -> tuple[Fraction, ...]:
    result = tuple(_fraction(item) for item in value)
    if len(result) != 9:
        raise QualificationError(f"projective matrix must have 9 values: {value!r}")
    return result


def _matrix_compose(left: tuple[Fraction, ...], right: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
    """Return the affine matrix ``left`` after ``right``."""

    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re + lc * rf + le,
        lb * re + ld * rf + lf,
    )


def _matrix_identity() -> tuple[Fraction, ...]:
    return (Fraction(1), Fraction(0), Fraction(0), Fraction(1), Fraction(0), Fraction(0))


def _matrix3_identity() -> tuple[Fraction, ...]:
    return (
        Fraction(1), Fraction(0), Fraction(0),
        Fraction(0), Fraction(1), Fraction(0),
        Fraction(0), Fraction(0), Fraction(1),
    )


def _matrix3_compose(left: tuple[Fraction, ...], right: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
    return tuple(
        sum(left[row * 3 + index] * right[index * 3 + column] for index in range(3))
        for row in range(3)
        for column in range(3)
    )


def _matrix_inverse(matrix: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
    a, b, c, d, e, f = matrix
    determinant = a * d - b * c
    if determinant == 0:
        raise QualificationError("singular affine matrix has no exact inverse")
    return (
        d / determinant,
        -b / determinant,
        -c / determinant,
        a / determinant,
        (c * f - d * e) / determinant,
        (b * e - a * f) / determinant,
    )


def _matrix3_inverse(matrix: tuple[Fraction, ...]) -> tuple[Fraction, ...]:
    a, b, c, d, e, f, g, h, i = matrix
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if determinant == 0:
        raise QualificationError("singular projective matrix has no exact inverse")
    cofactors = (
        e * i - f * h,
        c * h - b * i,
        b * f - c * e,
        f * g - d * i,
        a * i - c * g,
        c * d - a * f,
        d * h - e * g,
        b * g - a * h,
        a * e - b * d,
    )
    return tuple(value / determinant for value in cofactors)


def _apply_matrix(matrix: tuple[Fraction, ...], point: Iterable[Any]) -> tuple[Fraction, Fraction]:
    x, y = (_fraction(item) for item in point)
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _apply_projective(matrix: tuple[Fraction, ...], point: Iterable[Any]) -> tuple[Fraction, Fraction]:
    x, y = (_fraction(item) for item in point)
    a, b, c, d, e, f, g, h, i = matrix
    denominator = g * x + h * y + i
    if denominator == 0:
        raise QualificationError("projective point maps to infinity")
    return ((a * x + b * y + c) / denominator, (d * x + e * y + f) / denominator)


def _operation_matrix(operation: dict[str, Any]) -> tuple[Fraction, ...]:
    kind = operation.get("kind")
    if kind == "translate":
        return (Fraction(1), Fraction(0), Fraction(0), Fraction(1), _fraction(operation["x"]), _fraction(operation["y"]))
    if kind == "scale":
        return ( _fraction(operation["sx"]), Fraction(0), Fraction(0), _fraction(operation["sy"]), Fraction(0), Fraction(0))
    if kind == "rotate90":
        turns = int(operation.get("quarterTurns", 0)) % 4
        matrices = [
            _matrix_identity(),
            (Fraction(0), Fraction(-1), Fraction(1), Fraction(0), Fraction(0), Fraction(0)),
            (Fraction(-1), Fraction(0), Fraction(0), Fraction(-1), Fraction(0), Fraction(0)),
            (Fraction(0), Fraction(1), Fraction(-1), Fraction(0), Fraction(0), Fraction(0)),
        ]
        return matrices[turns]
    if kind == "shear":
        return (Fraction(1), Fraction(_fraction(operation.get("shy", 0))), _fraction(operation.get("shx", 0)), Fraction(1), Fraction(0), Fraction(0))
    if kind == "matrix":
        return _matrix(operation["values"])
    raise QualificationError(f"unsupported affine operation: {kind!r}")


def _operation_matrix3(operation: dict[str, Any]) -> tuple[Fraction, ...]:
    kind = operation.get("kind")
    if kind in {"translate", "scale", "rotate90", "shear", "matrix"}:
        a, b, c, d, e, f = _operation_matrix(operation)
        return (a, c, e, b, d, f, Fraction(0), Fraction(0), Fraction(1))
    if kind in {"projective", "matrix3"}:
        return _matrix3(operation["values"])
    raise QualificationError(f"unsupported projective operation: {kind!r}")


def _compose_operations(operations: Iterable[dict[str, Any]]) -> tuple[Fraction, ...]:
    result = _matrix_identity()
    for operation in operations:
        result = _matrix_compose(_operation_matrix(operation), result)
    return result


def _compose_projective_operations(operations: Iterable[dict[str, Any]]) -> tuple[Fraction, ...]:
    result = _matrix3_identity()
    for operation in operations:
        result = _matrix3_compose(_operation_matrix3(operation), result)
    return result


def _strict_preservation(report: dict[str, Any], context: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    status = report.get("status")
    exact = report.get("exact") is True
    approximation = report.get("approximation") is True
    if status == "preserved" and (not exact or approximation):
        failures.append(
            {
                "id": f"{context}-no-false-preservation",
                "oracle": "preserved requires exact=true and approximation=false",
                "expected": {"status": "preserved", "exact": True, "approximation": False},
                "actual": {"status": status, "exact": exact, "approximation": approximation},
                "status": "failed",
            }
        )
    return failures


def _equal_assertion(
    assertion_id: str, oracle: str, expected: Any, actual: Any
) -> dict[str, Any]:
    return {
        "id": assertion_id,
        "oracle": oracle,
        "expected": expected,
        "actual": actual,
        "status": "passed" if expected == actual else "failed",
    }


def _result(
    case_id: str,
    expected: Any,
    actual: Any,
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "caseId": case_id,
        "expected": expected,
        "actual": actual,
        "assertions": assertions,
        "status": "passed" if all(item["status"] == "passed" for item in assertions) else "failed",
    }


def _coordinate_result(vector: dict[str, Any]) -> dict[str, Any]:
    expected = vector["expected"]
    failures = _strict_preservation(vector.get("reported", {}), f"coordinate-{vector['id']}")
    actual: dict[str, Any] = {}
    try:
        is_projective = vector.get("transformModel") == "projective" or any(
            isinstance(operation, dict) and operation.get("kind") in {"projective", "matrix3"}
            for operation in vector["operations"]
        )
        if is_projective:
            matrix3 = _compose_projective_operations(vector["operations"])
            point = _apply_projective(matrix3, vector["point"])
            inverse_point = _apply_projective(_matrix3_inverse(matrix3), point)
            actual = {
                "matrix": _encoded_matrix(matrix3),
                "point": _encoded_point(point),
                "inversePoint": _encoded_point(inverse_point),
                "exact": True,
                "matrixModel": "projective",
            }
        else:
            matrix = _compose_operations(vector["operations"])
            point = _apply_matrix(matrix, vector["point"])
            inverse_point = _apply_matrix(_matrix_inverse(matrix), point)
            actual = {
                "matrix": _encoded_matrix(matrix),
                "point": _encoded_point(point),
                "inversePoint": _encoded_point(inverse_point),
                "exact": True,
            }
    except (KeyError, TypeError, ValueError, QualificationError) as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}", "exact": False}
    assertions = list(failures)
    assertions.extend(
        [
            _equal_assertion(f"coordinate-{vector['id']}-matrix", "compose authored affine operations exactly", expected.get("matrix"), actual.get("matrix")),
            _equal_assertion(f"coordinate-{vector['id']}-point", "apply composed matrix to authored point exactly", expected.get("point"), actual.get("point")),
            _equal_assertion(f"coordinate-{vector['id']}-inverse", "inverse transform round-trips the authored point", expected.get("inversePoint"), actual.get("inversePoint")),
            _equal_assertion(f"coordinate-{vector['id']}-exact", "vector stays in the rational exactness lane", expected.get("exact"), actual.get("exact")),
        ]
    )
    return _result(vector["id"], expected, actual, assertions)


def _geometry_observation(primitive: dict[str, Any]) -> dict[str, Any]:
    kind = primitive.get("kind")
    if kind == "rectangle":
        x, y = _fraction(primitive["x"]), _fraction(primitive["y"])
        width, height = _fraction(primitive["width"]), _fraction(primitive["height"])
        if width < 0 or height < 0:
            raise QualificationError("rectangle dimensions must be non-negative")
        return {"bounds": _encoded_point((x, y, x + width, y + height)), "area": _encoded(width * height), "exactFor": "primitive"}
    if kind == "polyline":
        points = [tuple(_fraction(item) for item in point) for point in primitive["points"]]
        if len(points) < 2 or any(len(point) != 2 for point in points):
            raise QualificationError("polyline needs at least two 2D points")
        lengths = []
        for first, second in zip(points, points[1:]):
            lengths.append(_encoded((second[0] - first[0]) ** 2 + (second[1] - first[1]) ** 2))
        xs, ys = zip(*points)
        return {
            "bounds": _encoded_point((min(xs), min(ys), max(xs), max(ys))),
            "pointCount": len(points),
            "segmentLengthSquared": lengths,
            "exactFor": "points-and-segments",
        }
    if kind == "bezier":
        names = ("p0", "p1", "p2", "p3")
        points = [tuple(_fraction(item) for item in primitive[name]) for name in names]
        if any(len(point) != 2 for point in points):
            raise QualificationError("bezier control points must be 2D")
        xs, ys = zip(*points)
        result: dict[str, Any] = {
            "endpoints": [_encoded_point(points[0]), _encoded_point(points[3])],
            "controlHull": _encoded_point((min(xs), min(ys), max(xs), max(ys))),
            "controlPoints": [_encoded_point(point) for point in points],
            "exactFor": "control-points-and-control-hull",
        }
        if primitive.get("measureExtrema") is True:
            extrema = {
                "x": _cubic_extrema(points[0][0], points[1][0], points[2][0], points[3][0]),
                "y": _cubic_extrema(points[0][1], points[1][1], points[2][1], points[3][1]),
            }
            candidates = {
                "x": [points[0][0], points[3][0]] + [_fraction(item["value"]) for item in extrema["x"]],
                "y": [points[0][1], points[3][1]] + [_fraction(item["value"]) for item in extrema["y"]],
            }
            result.update(
                {
                    "extrema": extrema,
                    "bounds": _encoded_point((min(candidates["x"]), min(candidates["y"]), max(candidates["x"]), max(candidates["y"]))),
                    "exactFor": "control-points-and-cubic-extrema",
                }
            )
        return result
    if kind == "clippingPath":
        points = [tuple(_fraction(item) for item in point) for point in primitive["points"]]
        if len(points) < 3 or any(len(point) != 2 for point in points):
            raise QualificationError("clipping path needs at least three 2D points")
        xs, ys = zip(*points)
        area_twice = sum(
            point[0] * next_point[1] - next_point[0] * point[1]
            for point, next_point in zip(points, points[1:] + points[:1])
        )
        return {
            "bounds": _encoded_point((min(xs), min(ys), max(xs), max(ys))),
            "signedArea": _encoded(area_twice / 2),
            "closed": primitive.get("closed") is True,
            "exactFor": "polygon-closure-and-area",
        }
    raise QualificationError(f"unsupported geometry primitive: {kind!r}")


def _fraction_sqrt(value: Fraction) -> Fraction:
    if value < 0:
        raise QualificationError("negative discriminant is not rational")
    numerator_root = math.isqrt(value.numerator)
    denominator_root = math.isqrt(value.denominator)
    if numerator_root * numerator_root != value.numerator or denominator_root * denominator_root != value.denominator:
        raise QualificationError("irrational geometry extrema are outside the exact rational lane")
    return Fraction(numerator_root, denominator_root)


def _cubic_value(p0: Fraction, p1: Fraction, p2: Fraction, p3: Fraction, t: Fraction) -> Fraction:
    one_minus = 1 - t
    return (
        p0 * one_minus * one_minus * one_minus
        + 3 * p1 * one_minus * one_minus * t
        + 3 * p2 * one_minus * t * t
        + p3 * t * t * t
    )


def _cubic_extrema(p0: Fraction, p1: Fraction, p2: Fraction, p3: Fraction) -> list[dict[str, int | str]]:
    cubic = -p0 + 3 * p1 - 3 * p2 + p3
    quadratic = 3 * p0 - 6 * p1 + 3 * p2
    linear = -3 * p0 + 3 * p1
    derivative_a = 3 * cubic
    derivative_b = 2 * quadratic
    derivative_c = linear
    roots: set[Fraction] = set()
    if derivative_a == 0:
        if derivative_b != 0:
            root = -derivative_c / derivative_b
            if 0 < root < 1:
                roots.add(root)
    else:
        discriminant = derivative_b * derivative_b - 4 * derivative_a * derivative_c
        if discriminant >= 0:
            square_root = _fraction_sqrt(discriminant)
            for root in (
                (-derivative_b - square_root) / (2 * derivative_a),
                (-derivative_b + square_root) / (2 * derivative_a),
            ):
                if 0 < root < 1:
                    roots.add(root)
    return [
        {"t": _encoded(root), "value": _encoded(_cubic_value(p0, p1, p2, p3, root))}
        for root in sorted(roots)
    ]


def _geometry_result(vector: dict[str, Any]) -> dict[str, Any]:
    primitive = vector["primitive"]
    expected = vector["expected"]
    failures = _strict_preservation(primitive, f"geometry-{vector['id']}")
    actual: dict[str, Any]
    try:
        actual = _geometry_observation(primitive)
    except (KeyError, TypeError, ValueError, QualificationError) as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}"}
    assertions = list(failures)
    assertions.extend(
        [
            _equal_assertion(f"geometry-{vector['id']}-facts", "derive exact primitive facts without adapter assistance", expected, actual),
            _equal_assertion(f"geometry-{vector['id']}-exactness-domain", "authored exactness domain is declared and preserved", expected.get("exactFor"), actual.get("exactFor")),
        ]
    )
    return _result(vector["id"], expected, actual, assertions)


def _space_matrices(spaces: list[dict[str, Any]]) -> dict[str, tuple[Fraction, ...]]:
    definitions = {str(item["id"]): item for item in spaces}
    resolved: dict[str, tuple[Fraction, ...]] = {}
    visiting: set[str] = set()

    def resolve(space_id: str) -> tuple[Fraction, ...]:
        if space_id in resolved:
            return resolved[space_id]
        if space_id in visiting:
            raise QualificationError(f"coordinate-space cycle at {space_id}")
        if space_id not in definitions:
            raise QualificationError(f"unknown coordinate space {space_id}")
        visiting.add(space_id)
        item = definitions[space_id]
        local = _matrix(item["matrix"])
        parent = item.get("parent")
        result = local if parent is None else _matrix_compose(resolve(str(parent)), local)
        visiting.remove(space_id)
        resolved[space_id] = result
        return result

    for space_id in definitions:
        resolve(space_id)
    return resolved


def _anchor_local_point(anchor: dict[str, Any]) -> tuple[Fraction, Fraction]:
    kind = anchor.get("kind")
    if kind in {"absolute", "inline", "relative"}:
        base = anchor.get("point") if kind == "absolute" else anchor.get("basePoint")
        if base is None:
            raise QualificationError(f"{kind} anchor has no base point")
        point = tuple(_fraction(item) for item in base)
        if len(point) != 2:
            raise QualificationError("anchor point must be 2D")
        if kind != "absolute":
            offset = tuple(_fraction(item) for item in anchor.get("offset", [0, 0]))
            point = (point[0] + offset[0], point[1] + offset[1])
        return point
    if kind == "grid":
        origin = tuple(_fraction(item) for item in anchor["gridOrigin"])
        cell = tuple(_fraction(item) for item in anchor["cellSize"])
        row, column = int(anchor["row"]), int(anchor["column"])
        offset = tuple(_fraction(item) for item in anchor.get("offset", [0, 0]))
        if row < 0 or column < 0 or len(origin) != 2 or len(cell) != 2 or len(offset) != 2:
            raise QualificationError("grid anchor has invalid dimensions")
        return (origin[0] + cell[0] * column + offset[0], origin[1] + cell[1] * row + offset[1])
    raise QualificationError(f"unsupported anchor kind: {kind!r}")


def _anchor_result(vector: dict[str, Any]) -> dict[str, Any]:
    anchor = vector["anchor"]
    expected = vector["expected"]
    failures = _strict_preservation(anchor, f"anchor-{vector['id']}")
    actual: dict[str, Any]
    try:
        spaces = _space_matrices(vector["coordinateSpaces"])
        space_id = str(anchor["spaceId"])
        if space_id not in spaces:
            raise QualificationError(f"unknown anchor space {space_id}")
        if anchor.get("kind") == "endpoint":
            start = tuple(_fraction(item) for item in anchor["start"])
            end = tuple(_fraction(item) for item in anchor["end"])
            offset = tuple(_fraction(item) for item in anchor.get("offset", [0, 0]))
            if len(start) != 2 or len(end) != 2 or len(offset) != 2:
                raise QualificationError("endpoint anchor must contain two 2D endpoints and a 2D offset")
            actual = {
                "globalStart": _encoded_point(_apply_matrix(spaces[space_id], (start[0] + offset[0], start[1] + offset[1]))),
                "globalEnd": _encoded_point(_apply_matrix(spaces[space_id], (end[0] + offset[0], end[1] + offset[1]))),
                "kind": anchor.get("kind"),
            }
        else:
            actual = {
                "globalPoint": _encoded_point(_apply_matrix(spaces[space_id], _anchor_local_point(anchor))),
                "kind": anchor.get("kind"),
            }
    except (KeyError, TypeError, ValueError, QualificationError) as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}"}
    assertions = list(failures)
    assertions.extend(
        [
            _equal_assertion(f"anchor-{vector['id']}-facts", "resolve authored anchor through exact coordinate-space chain", expected, actual),
            _equal_assertion(f"anchor-{vector['id']}-kind", "anchor kind remains explicit", expected.get("kind"), actual.get("kind")),
        ]
    )
    return _result(vector["id"], expected, actual, assertions)


def _clip_result(scene: dict[str, Any]) -> dict[str, Any]:
    expected = scene["expected"]
    failures = _strict_preservation(scene.get("reported", {}), f"clip-{scene['id']}")
    actual: dict[str, Any] = {}
    try:
        geometries = {str(item["id"]): item for item in scene["geometries"]}
        nodes = {str(item["id"]): item for item in scene["nodes"]}
        events = scene["events"]
        event_ids = [str(item["id"]) for item in events]
        if len(event_ids) != len(set(event_ids)):
            raise QualificationError("paint event IDs are not unique")
        active: list[str] = []
        bindings: list[dict[str, Any]] = []
        paint_event_order: list[str] = []
        for event in events:
            kind = event.get("kind")
            if kind == "clip":
                geometry_id = str(event["geometryId"])
                geometry = geometries.get(geometry_id)
                if geometry is None or geometry.get("kind") != "clippingPath":
                    raise QualificationError(f"clip event references non-clipping geometry {geometry_id}")
                if _strict_preservation(geometry, f"clip-geometry-{geometry_id}"):
                    raise QualificationError(f"clip geometry {geometry_id} is approximate but marked preserved")
                active.append(geometry_id)
            elif kind == "restore":
                geometry_id = str(event["geometryId"])
                if not active or active[-1] != geometry_id:
                    raise QualificationError(f"clip restore is not LIFO for {geometry_id}")
                active.pop()
            elif kind == "paint":
                target_id = str(event["targetId"])
                if target_id not in nodes:
                    raise QualificationError(f"paint event references unknown node {target_id}")
                declared = [str(item) for item in event.get("clipGeometryIds", [])]
                if any(item not in active for item in declared):
                    raise QualificationError(f"paint event {event['id']} uses an inactive clip")
                if declared != active:
                    raise QualificationError(f"paint event {event['id']} has incomplete clip stack")
                if declared:
                    bindings.append({"targetId": target_id, "geometryIds": declared})
                paint_event_order.append(str(event["id"]))
            else:
                raise QualificationError(f"unsupported paint event kind {kind!r}")
        if active:
            raise QualificationError("clip stack is not restored at end of scene")
        actual = {"eventOrder": event_ids, "paintEventOrder": paint_event_order, "clipBindings": bindings, "exact": True}
    except (KeyError, TypeError, ValueError, QualificationError) as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}"}
    assertions = list(failures)
    assertions.extend(
        [
            _equal_assertion(f"clip-{scene['id']}-order", "authored paint events remain in exact source order", expected.get("eventOrder"), actual.get("eventOrder")),
            _equal_assertion(f"clip-{scene['id']}-paint-order", "interleaved paint event order remains separate from clip stack events", expected.get("paintEventOrder"), actual.get("paintEventOrder")),
            _equal_assertion(f"clip-{scene['id']}-bindings", "every target clip reference is active and closed", expected.get("clipBindings"), actual.get("clipBindings")),
            _equal_assertion(f"clip-{scene['id']}-exact", "clip and paint facts are not approximations", expected.get("exact"), actual.get("exact")),
        ]
    )
    return _result(scene["id"], expected, actual, assertions)


def _intervals_overlap(first: list[Fraction], second: list[Fraction]) -> bool:
    return min(first[1], first[0] + first[2]) < max(first[0], second[0] + second[2]) and min(second[1], second[0] + second[2]) < max(first[0], first[0] + first[2])


def _vertical_overlap(first: list[Fraction], second: list[Fraction]) -> bool:
    return min(first[1] + first[3], second[1] + second[3]) > max(first[1], second[1])


def _horizontal_disjoint(first: list[Fraction], second: list[Fraction]) -> bool:
    return first[0] + first[2] <= second[0] or second[0] + second[2] <= first[0]


def _reading_result(scene: dict[str, Any]) -> dict[str, Any]:
    expected = scene["expected"]
    failures = _strict_preservation(scene.get("reported", {}), f"reading-{scene['id']}")
    actual: dict[str, Any] = {}
    try:
        items = {str(item["id"]): item for item in scene["items"]}
        if len(items) != len(scene["items"]):
            raise QualificationError("reading item IDs are not unique")
        boxes = {item_id: [_fraction(value) for value in item["box"]] for item_id, item in items.items()}
        if any(len(box) != 4 or box[2] < 0 or box[3] < 0 for box in boxes.values()):
            raise QualificationError("reading item boxes must be non-negative [x,y,w,h]")
        edges = {tuple(str(value) for value in edge) for edge in scene.get("explicitEdges", [])}
        ambiguous: set[tuple[str, str]] = set()
        item_ids = list(items)
        for index, first_id in enumerate(item_ids):
            for second_id in item_ids[index + 1 :]:
                if (first_id, second_id) in edges or (second_id, first_id) in edges:
                    continue
                first, second = boxes[first_id], boxes[second_id]
                if _vertical_overlap(first, second) and _horizontal_disjoint(first, second):
                    ambiguous.add(tuple(sorted((first_id, second_id))))
        ambiguous_pairs = [list(pair) for pair in sorted(ambiguous)]
        ambiguous_ids = {value for pair in ambiguous for value in pair}
        resolved_items = [item for item in items.values() if str(item["id"]) not in ambiguous_ids]
        if all(item.get("orderKey") is not None for item in resolved_items):
            resolved_items.sort(key=lambda item: (int(item["orderKey"]), str(item["id"])))
        else:
            resolved_items.sort(key=lambda item: (_fraction(item["box"][1]), _fraction(item["box"][0]), str(item["id"])))
        actual = {
            "ambiguousPairs": ambiguous_pairs,
            "resolvedOrder": [str(item["id"]) for item in resolved_items],
            "exact": True,
        }
    except (KeyError, TypeError, ValueError, QualificationError) as exc:
        actual = {"error": f"{type(exc).__name__}: {exc}"}
    reported = scene.get("reported", {})
    reported_pairs = sorted(
        [sorted(str(value) for value in item.get("pair", [])) for item in reported.get("ambiguities", [])]
    )
    expected_pairs = expected.get("ambiguousPairs", [])
    assertions = list(failures)
    assertions.extend(
        [
            _equal_assertion(f"reading-{scene['id']}-pairs", "same-band disjoint columns without an authored precedence edge are ambiguous", expected_pairs, actual.get("ambiguousPairs")),
            _equal_assertion(f"reading-{scene['id']}-resolved-order", "only non-ambiguous items may receive a deterministic reading order", expected.get("resolvedOrder"), actual.get("resolvedOrder")),
            _equal_assertion(f"reading-{scene['id']}-reported-pairs", "reported ambiguity set must equal the oracle ambiguity set", expected_pairs, reported_pairs),
            _equal_assertion(f"reading-{scene['id']}-reported-order", "reported order must not invent or reorder authored reading facts", expected.get("resolvedOrder"), reported.get("order")),
            _equal_assertion(f"reading-{scene['id']}-status", "ambiguous scenes cannot be reported as preserved", "ambiguous" if expected_pairs else "preserved", reported.get("status")),
            _equal_assertion(f"reading-{scene['id']}-exact", "reading relation is exact or explicitly ambiguous, never approximate", expected.get("exact"), actual.get("exact")),
        ]
    )
    return _result(scene["id"], expected, actual, assertions)


def _mutate_coordinate(vector: dict[str, Any], kind: str) -> None:
    if kind == "swap-operations":
        vector["operations"][0], vector["operations"][1] = vector["operations"][1], vector["operations"][0]
    elif kind == "round-results":
        vector["operations"][0]["sx"] = 2
        vector["operations"][0]["sy"] = 3
    elif kind == "replace-rotation-with-identity":
        vector["operations"][0] = {"kind": "matrix", "values": [1, 0, 0, 1, 0, 0]}
    elif kind == "drop-perspective":
        values = vector["operations"][0]["values"]
        values[6], values[7] = 0, 0
    else:
        raise QualificationError(f"unknown coordinate mutation {kind}")


def _mutate_geometry(vector: dict[str, Any], kind: str) -> None:
    primitive = vector["primitive"]
    if kind == "change-width":
        primitive["width"] = _fraction(primitive["width"]) + 1
        primitive["width"] = _encoded(primitive["width"])
    elif kind == "mark-approximate":
        primitive["approximation"] = True
    elif kind == "change-control-point":
        primitive["p1"][0] = _encoded(_fraction(primitive["p1"][0]) + 1)
    elif kind == "swap-points":
        primitive["points"][0], primitive["points"][1] = primitive["points"][1], primitive["points"][0]
    elif kind == "open-clip":
        primitive["closed"] = False
    else:
        raise QualificationError(f"unknown geometry mutation {kind}")


def _mutate_anchor(vector: dict[str, Any], kind: str) -> None:
    anchor = vector["anchor"]
    if kind == "unknown-space":
        anchor["spaceId"] = "missing-space"
    elif kind == "swap-offset-axes":
        anchor["offset"] = list(reversed(anchor["offset"]))
    elif kind == "wrong-grid-row":
        anchor["row"] = int(anchor["row"]) + 1
    elif kind == "mark-approximate":
        anchor["approximation"] = True
    elif kind == "swap-endpoints":
        anchor["start"], anchor["end"] = anchor["end"], anchor["start"]
    else:
        raise QualificationError(f"unknown anchor mutation {kind}")


def _mutate_clip(scene: dict[str, Any], kind: str) -> None:
    if kind == "swap-clip-and-target":
        scene["events"][1], scene["events"][2] = scene["events"][2], scene["events"][1]
    elif kind == "drop-clip-reference":
        next(event for event in scene["events"] if event["id"] == "paint-shape").pop("clipGeometryIds")
    elif kind == "mark-clip-approximate":
        next(item for item in scene["geometries"] if item["id"] == "clip-main")["approximation"] = True
    elif kind == "duplicate-event":
        scene["events"].append(deepcopy(scene["events"][0]))
    elif kind == "swap-paint-events":
        paint_indices = [index for index, event in enumerate(scene["events"]) if event.get("kind") == "paint"]
        if len(paint_indices) < 2:
            raise QualificationError("paint-order mutation needs at least two paint events")
        first, second = paint_indices[0], paint_indices[-1]
        scene["events"][first], scene["events"][second] = scene["events"][second], scene["events"][first]
    else:
        raise QualificationError(f"unknown clip mutation {kind}")


def _mutate_reading(scene: dict[str, Any], kind: str) -> None:
    reported = scene["reported"]
    if kind == "hide-ambiguity":
        reported["ambiguities"] = []
        reported["status"] = "preserved"
    elif kind == "add-arbitrary-order":
        reported["order"] = ["heading", "left-column", "right-column"]
        reported["status"] = "preserved"
    elif kind == "swap-resolved-order":
        reported["order"] = list(reversed(reported["order"]))
    elif kind == "mark-approximate":
        reported["approximation"] = True
    else:
        raise QualificationError(f"unknown reading mutation {kind}")


def _validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("issueNumber") != 94 or corpus.get("evidenceId") != EVIDENCE_ID:
        raise QualificationError("corpus is not bound to issue #94")
    for key in ("coordinateTransforms", "geometryLanes", "anchorResolution", "clipAndPaintOrder", "readingOrderAmbiguity"):
        section = corpus.get(key)
        if (
            not isinstance(section, dict)
            or not isinstance(section.get("requiredLanes"), list)
            or not section["requiredLanes"]
            or not isinstance(section.get("coveredLanes"), list)
            or set(section["coveredLanes"]) - set(section["requiredLanes"])
            or not isinstance(section.get("unmetCoverage"), list)
        ):
            raise QualificationError(f"corpus section {key} lacks required lanes or explicit unmet coverage")
    ids: set[str] = set()
    for section_key, vector_key in (("coordinateTransforms", "vectors"), ("geometryLanes", "vectors"), ("anchorResolution", "vectors"), ("readingOrderAmbiguity", "scenes"), ("clipAndPaintOrder", "scenes")):
        for item in corpus[section_key][vector_key]:
            item_id = str(item.get("id"))
            if not item_id or item_id in ids:
                raise QualificationError(f"duplicate or missing authored vector ID {item_id!r}")
            ids.add(item_id)
    integration = corpus.get("adapterIntegration")
    if not isinstance(integration, dict):
        raise QualificationError("corpus has no public converter integration section")
    required_lanes = integration.get("requiredLanes")
    covered_lanes = integration.get("coveredLanes")
    if not isinstance(required_lanes, list) or not required_lanes or not isinstance(covered_lanes, list):
        raise QualificationError("integration coverage lanes are missing")
    oracle = integration.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("expectedValuesAreRuntimeIndependent") is not True or oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("integration oracle is not independent of adapter helpers")
    fixtures = integration.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("corpus has no public converter integration fixtures")
    fixture_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("integration fixture is not an object")
        fixture_id = fixture.get("fixtureId")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate integration fixture id: {fixture_id!r}")
        if fixture.get("format") not in INTEGRATION_FORMATS:
            raise QualificationError(f"unsupported integration fixture format: {fixture.get('format')!r}")
        if not isinstance(fixture.get("reports"), list) or not fixture["reports"] or any(item not in REPORT_NAMES for item in fixture["reports"]):
            raise QualificationError(f"integration fixture has invalid report binding: {fixture_id}")
        if not isinstance(fixture.get("source"), dict) or not isinstance(fixture.get("sourceFacts"), dict) or not isinstance(fixture.get("expected"), dict):
            raise QualificationError(f"integration fixture lacks source facts or expected projection: {fixture_id}")
        source_type = fixture["source"].get("type")
        if source_type == "zip-parts" and not isinstance(fixture["source"].get("parts"), dict):
            raise QualificationError(f"integration fixture has no package parts: {fixture_id}")
        if source_type == "repo-file" and not isinstance(fixture["source"].get("path"), str):
            raise QualificationError(f"integration fixture has no repository source path: {fixture_id}")
        if source_type not in {"zip-parts", "repo-file"}:
            raise QualificationError(f"integration fixture has unsupported source type: {source_type!r}")
        fixture_ids.add(fixture_id)
    integration_mutations = integration.get("mutations")
    if not isinstance(integration_mutations, list) or not integration_mutations:
        raise QualificationError("integration mutation matrix is empty")
    for mutation in integration_mutations:
        if not isinstance(mutation, dict) or not isinstance(mutation.get("id"), str) or not isinstance(mutation.get("fixtureId"), str) or not isinstance(mutation.get("kind"), str):
            raise QualificationError("integration mutation declaration is invalid")
        if mutation["fixtureId"] not in fixture_ids:
            raise QualificationError(f"integration mutation references unknown fixture: {mutation['fixtureId']}")
    if not isinstance(integration.get("unmetCoverage"), list) or not integration["unmetCoverage"]:
        raise QualificationError("integration unmet coverage must be explicit")


def _coverage(section: dict[str, Any]) -> dict[str, Any]:
    unmet = [str(item) for item in section.get("unmetCoverage", [])]
    required = [str(item) for item in section.get("requiredLanes", [])]
    covered = [str(item) for item in section.get("coveredLanes", [])]
    return {
        "complete": not unmet and set(required).issubset(set(covered)),
        "requiredLanes": required,
        "coveredLanes": covered,
        "unmet": unmet,
    }


def _category_report(
    *,
    kind: str,
    section: dict[str, Any],
    source_sha: str,
    corpus_sha: str,
    cases: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
    adapter_cases: list[dict[str, Any]] | None = None,
    adapter_mutations: list[dict[str, Any]] | None = None,
    adapter_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter_cases = adapter_cases or []
    adapter_mutations = adapter_mutations or []
    adapter_coverage = adapter_coverage or {"complete": False, "requiredLanes": [], "coveredLanes": [], "unmet": ["integration coverage unavailable"]}
    assertions: list[dict[str, Any]] = []
    for case in cases:
        assertions.extend(case["assertions"])
    for case in adapter_cases:
        assertions.extend(case["assertions"])
    for mutation in mutations:
        assertions.append(
            _equal_assertion(
                mutation["mutationId"],
                "each authored single-defect mutation must be detected",
                True,
                mutation["detected"],
            )
        )
    for mutation in adapter_mutations:
        assertions.append(
            _equal_assertion(
                mutation["mutationId"],
                "each authored adapter projection mutation must be detected",
                True,
                mutation["detected"],
            )
        )
    coverage = _coverage(section)
    vector_passed = all(case["status"] == "passed" for case in cases)
    mutation_passed = all(item["detected"] for item in mutations)
    adapter_passed = all(case["status"] == "passed" for case in adapter_cases)
    adapter_mutation_passed = all(item["detected"] for item in adapter_mutations)
    adapter_mismatch_count = sum(len(case.get("adapterMismatches", [])) for case in adapter_cases)
    source_fact_mismatch_count = sum(len(case.get("sourceMismatches", [])) for case in adapter_cases)
    independent_oracle_mismatch_count = sum(len(case.get("independentOracleMismatches", [])) for case in adapter_cases)
    public_boundary_failures = sum(not case.get("conversionOk", False) for case in adapter_cases)
    assertions.append(_equal_assertion(f"{kind}-vector-matrix", "all positive authored vectors pass", True, vector_passed))
    assertions.append(_equal_assertion(f"{kind}-mutation-matrix", "all authored mutations are rejected", True, mutation_passed))
    assertions.append(_equal_assertion(f"{kind}-adapter-matrix", "all bound public converter projections pass", 0, sum(case["status"] != "passed" for case in adapter_cases)))
    assertions.append(_equal_assertion(f"{kind}-adapter-source-facts", "all bound source facts match the independent parser", 0, source_fact_mismatch_count))
    assertions.append(_equal_assertion(f"{kind}-independent-oracle", "all expected projections are derived from independent source facts", 0, independent_oracle_mismatch_count))
    assertions.append(_equal_assertion(f"{kind}-adapter-mutations", "all bound adapter mutations are rejected", True, adapter_mutation_passed))
    assertions.append(_equal_assertion(f"{kind}-coverage-complete", "bounded slice has complete required coverage", True, coverage["complete"]))
    assertions.append(_equal_assertion(f"{kind}-adapter-coverage-complete", "public converter integration has complete required coverage", True, adapter_coverage["complete"]))
    status = "passed" if vector_passed and mutation_passed and adapter_passed and adapter_mutation_passed and not source_fact_mismatch_count and not independent_oracle_mismatch_count and not adapter_mismatch_count and not public_boundary_failures and coverage["complete"] and adapter_coverage["complete"] and all(item["status"] == "passed" for item in assertions) else "failed"
    report: dict[str, Any] = {
        "schema": "fdir/qualification-evidence",
        "version": VERSION,
        "evidenceId": EVIDENCE_ID,
        "issueNumbers": [94],
        "requirementIds": REQUIREMENT_IDS,
        "reportKind": REPORT_NAMES[kind],
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "status": status,
        "vectorStatus": "passed" if vector_passed else "failed",
        "mutationStatus": "passed" if mutation_passed else "failed",
        "adapterStatus": "not-applicable" if not adapter_cases else ("passed" if adapter_passed else "failed"),
        "adapterMutationStatus": "not-applicable" if not adapter_mutations else ("passed" if adapter_mutation_passed else "failed"),
        "independentOracle": {
            "kind": "authored-source-facts-and-exact-vector",
            "expectedUsesAdapters": False,
            "actualUsesPublicConverter": bool(adapter_cases),
            "usesRenderer": False,
            "numericDomain": "rational-affine-and-integer-geometry",
            "expectedProjectionDerivedFromSourceFacts": bool(adapter_cases),
        },
        "counts": {
            "cases": len(cases),
            "caseFailures": sum(case["status"] != "passed" for case in cases),
            "mutations": len(mutations),
            "mutationsDetected": sum(item["detected"] for item in mutations),
            "adapterCases": len(adapter_cases),
            "adapterCaseFailures": sum(case["status"] != "passed" for case in adapter_cases),
            "adapterMutations": len(adapter_mutations),
            "adapterMutationsDetected": sum(item["detected"] for item in adapter_mutations),
            "sourceFactMismatchCount": source_fact_mismatch_count,
            "independentOracleMismatchCount": independent_oracle_mismatch_count,
            "adapterMismatchCount": adapter_mismatch_count,
            "publicBoundaryFailureCount": public_boundary_failures,
            "unmetCoverageCount": len(coverage["unmet"]),
            "adapterUnmetCoverageCount": len(adapter_coverage["unmet"]),
        },
        "coverage": coverage,
        "adapterCoverage": adapter_coverage,
        "cases": cases,
        "mutations": mutations,
        "adapterCases": adapter_cases,
        "adapterMutations": adapter_mutations,
        "assertions": assertions,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if status != "passed":
        report["failure"] = {
            "code": "QUALIFICATION-COVERAGE-INCOMPLETE" if not coverage["complete"] else "QUALIFICATION-VECTOR-OR-MUTATION-FAILED",
            "unmetCoverage": coverage["unmet"] + adapter_coverage["unmet"],
            "unmetCoverageCount": len(coverage["unmet"]) + len(adapter_coverage["unmet"]),
            "sourceFactMismatchCount": source_fact_mismatch_count,
            "independentOracleMismatchCount": independent_oracle_mismatch_count,
            "adapterMismatchCount": adapter_mismatch_count,
            "publicBoundaryFailureCount": public_boundary_failures,
        }
    return report


def _fatal_report(kind: str, source_sha: str, corpus_sha: str, message: str) -> dict[str, Any]:
    assertion = {
        "id": f"{kind}-setup",
        "oracle": "qualification setup must be available",
        "expected": "setup succeeds",
        "actual": message,
        "status": "failed",
    }
    return {
        "schema": "fdir/qualification-evidence",
        "version": VERSION,
        "evidenceId": EVIDENCE_ID,
        "issueNumbers": [94],
        "requirementIds": REQUIREMENT_IDS,
        "reportKind": REPORT_NAMES[kind],
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "status": "failed",
        "vectorStatus": "failed",
        "mutationStatus": "failed",
        "adapterStatus": "failed",
        "adapterMutationStatus": "failed",
        "independentOracle": {"kind": "authored-source-facts-and-exact-vector", "expectedUsesAdapters": False, "actualUsesPublicConverter": False, "usesRenderer": False, "expectedProjectionDerivedFromSourceFacts": False},
        "counts": {
            "cases": 0,
            "caseFailures": 0,
            "mutations": 0,
            "mutationsDetected": 0,
            "adapterCases": 0,
            "adapterCaseFailures": 0,
            "adapterMutations": 0,
            "adapterMutationsDetected": 0,
            "sourceFactMismatchCount": 0,
            "independentOracleMismatchCount": 0,
            "adapterMismatchCount": 0,
            "publicBoundaryFailureCount": 0,
        },
        "coverage": {"complete": False, "requiredLanes": [], "coveredLanes": [], "unmet": ["qualification setup"]},
        "adapterCoverage": {"complete": False, "requiredLanes": [], "coveredLanes": [], "unmet": ["qualification setup"]},
        "cases": [],
        "mutations": [],
        "adapterCases": [],
        "adapterMutations": [],
        "assertions": [assertion],
        "failure": {"code": "QUALIFICATION-SETUP-FAILED", "message": message},
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _run_category(
    kind: str,
    section: dict[str, Any],
    source_sha: str,
    corpus_sha: str,
    vector_key: str,
    evaluate: Callable[[dict[str, Any]], dict[str, Any]],
    mutate: Callable[[dict[str, Any], str], None],
    adapter_cases: list[dict[str, Any]] | None = None,
    adapter_mutations: list[dict[str, Any]] | None = None,
    adapter_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases = [evaluate(deepcopy(item)) for item in section[vector_key]]
    by_id = {str(item["id"]): item for item in section[vector_key]}
    mutations: list[dict[str, Any]] = []
    for mutation in section.get("mutations", []):
        vector_id = str(mutation.get("vectorId", mutation.get("sceneId")))
        if vector_id not in by_id:
            raise QualificationError(f"mutation {mutation.get('id')} references unknown vector {vector_id}")
        mutated = deepcopy(by_id[vector_id])
        mutate(mutated, str(mutation["kind"]))
        result = evaluate(mutated)
        mutations.append(
            {
                "mutationId": str(mutation["id"]),
                "targetId": vector_id,
                "kind": str(mutation["kind"]),
                "detected": result["status"] != "passed",
                "resultStatus": result["status"],
                "failedAssertionIds": [item["id"] for item in result["assertions"] if item["status"] != "passed"],
            }
        )
    return _category_report(
        kind=kind,
        section=section,
        source_sha=source_sha,
        corpus_sha=corpus_sha,
        cases=cases,
        mutations=mutations,
        adapter_cases=adapter_cases,
        adapter_mutations=adapter_mutations,
        adapter_coverage=adapter_coverage,
    )


def _run_adapter_integration(
    corpus: dict[str, Any],
    work: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    integration = corpus["adapterIntegration"]
    fixture_cases: dict[str, dict[str, Any]] = {
        str(item["fixtureId"]): item for item in integration["fixtures"]
    }
    evaluated: dict[str, dict[str, Any]] = {}
    for case in integration["fixtures"]:
        execution = _run_public_converter(case, work)
        evaluated[str(case["fixtureId"])] = _integration_case_result(case, execution)

    adapter_cases_by_report = {kind: [] for kind in REPORT_NAMES}
    for result in evaluated.values():
        for report_kind in result["reports"]:
            adapter_cases_by_report[report_kind].append(result)

    adapter_mutations_by_report = {kind: [] for kind in REPORT_NAMES}
    seen_mutation_ids: set[str] = set()
    for mutation in integration["mutations"]:
        mutation_id = str(mutation["id"])
        if mutation_id in seen_mutation_ids:
            raise QualificationError(f"duplicate integration mutation id: {mutation_id}")
        seen_mutation_ids.add(mutation_id)
        fixture_id = str(mutation["fixtureId"])
        case = fixture_cases[fixture_id]
        result = evaluated[fixture_id]
        mutated = deepcopy(result["actual"])
        mutation_error: str | None = None
        try:
            _mutate_integration(mutated, str(mutation["kind"]))
            mismatches = _compare_exact(case["expected"], mutated)
        except Exception as exc:
            mutation_error = f"{type(exc).__name__}: {exc}"
            mismatches = [{"path": "$", "expected": case["expected"], "actual": {"error": mutation_error}, "kind": "mutation"}]
        record = {
            "mutationId": mutation_id,
            "fixtureId": fixture_id,
            "kind": str(mutation["kind"]),
            "detected": bool(mismatches),
            "mismatchCount": len(mismatches),
            "mismatches": mismatches,
        }
        if mutation_error is not None:
            record["error"] = mutation_error
        for report_kind in case["reports"]:
            adapter_mutations_by_report[report_kind].append(record)
    return adapter_cases_by_report, adapter_mutations_by_report


def _producer_case_id(*parts: Any) -> str:
    value = "-".join(str(part) for part in parts)
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", value).strip("-")
    return value[:120] or "case"


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    return [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue94.py",
        ROOT / "tools" / "test_qualification_issue94.py",
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "tools" / "adapter_xlsx.py",
        ROOT / "tools" / "adapter_pdf.py",
        ROOT / "tools" / "adapter_markdown.py",
    ]


def _producer_rows(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report_kind, report in reports.items():
        diagnostic = {
            "code": f"ISSUE-94-{report_kind.upper()}",
            "message": "issue #94 typed geometry/order authority and actual values are compared independently",
        }
        for assertion in report.get("assertions", []):
            assertion_id = str(assertion.get("assertionId", assertion.get("id", "")))
            if not assertion_id:
                continue
            expected = {"assertionId": assertion_id, "value": deepcopy(assertion.get("expected"))}
            actual = {"assertionId": assertion_id, "value": deepcopy(assertion.get("actual"))}
            rows.append({
                "caseId": _producer_case_id("positive", report_kind, assertion_id),
                "classification": "positive",
                "evaluatorType": "geometry-order",
                "input": {"reportKind": report_kind, "assertionId": assertion_id},
                "expected": expected,
                "actual": actual,
                "result": "passed" if expected == actual else "failed",
                "target": {"reportKind": report_kind, "assertionId": assertion_id},
                "diagnostic": diagnostic,
                "oracleEvidence": {"identity": "issue-94-authored-source-facts-oracle"},
            })

        for mutation_group in ("mutations", "adapterMutations"):
            for mutation in report.get(mutation_group, []):
                mutation_id = str(mutation.get("mutationId", ""))
                if not mutation_id:
                    continue
                detected = bool(mutation.get("detected"))
                expected = {"mutationDetected": False, "mutationId": mutation_id}
                actual = {"mutationDetected": detected, "mutationId": mutation_id}
                rows.append({
                    "caseId": _producer_case_id("mutation", report_kind, mutation_group, mutation_id),
                    "classification": "mutation",
                    "evaluatorType": "mutation-killed",
                    "input": {"reportKind": report_kind, "mutationGroup": mutation_group, "mutationId": mutation_id},
                    "expected": expected,
                    "actual": actual,
                    "result": "passed" if detected else "failed",
                    "target": {"reportKind": report_kind, "mutationId": mutation_id, "group": mutation_group},
                    "diagnostic": diagnostic,
                    "oracleEvidence": {"identity": "issue-94-authored-mutation", "mutation": deepcopy(mutation)},
                })

    if not rows:
        rows = [{
            "caseId": "setup-positive", "classification": "positive", "evaluatorType": "geometry-order",
            "input": {"setup": "issue-94"}, "expected": {"setup": "available"}, "actual": {"setup": "unavailable"},
            "result": "failed", "target": {"phase": "qualification-setup"},
            "diagnostic": {"code": "ISSUE-94-SETUP", "message": "qualification setup was unavailable"},
            "oracleEvidence": {"setup": "unavailable"},
        }]
    if not any(row["classification"] in {"negative", "mutation"} for row in rows):
        rows.append({
            "caseId": "setup-mutation", "classification": "mutation", "evaluatorType": "mutation-killed",
            "input": {"setup": "issue-94"}, "expected": {"mutationDetected": False}, "actual": {"mutationDetected": False},
            "result": "failed", "target": {"phase": "qualification-setup"},
            "diagnostic": {"code": "ISSUE-94-SETUP", "message": "qualification setup was unavailable"},
            "oracleEvidence": {"setup": "unavailable"},
        })
    return rows


def _write_producer_report(
    *, out_dir: Path, reports: dict[str, dict[str, Any]], corpus_path: Path, source_sha: str | None,
) -> dict[str, Any]:
    rows = _producer_rows(reports)
    return write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names=REPORT_NAMES,
        artifact_report_names=tuple(list(REPORT_NAMES.values())[:4]),
        issue_number=94,
        evidence_id=EVIDENCE_ID,
        requirement_id=REQUIREMENT_IDS[0],
        source_sha=source_sha or "0" * 40,
        input_paths=_producer_input_paths(corpus_path),
        producer_id="issue-94-qualification-runner",
        authority_id="issue-94-authored-source-facts-oracle",
        producer_component_path=Path(__file__),
        authority_component_path=Path(corpus_path),
        evaluator_component_path=Path(__file__),
        rows=rows,
        shared_component_paths=[CONVERTER_PATH],
    )


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    """Write all five reports and return nonzero unless the slice is complete."""

    source_sha: str | None = None
    corpus_sha: str | None = None
    try:
        source_sha = _source_sha()
        corpus_sha = _sha256_file(corpus_path)
        corpus = _read_json(corpus_path)
        if not isinstance(corpus, dict):
            raise QualificationError("issue #94 corpus root must be an object")
        _validate_corpus(corpus)
        integration_work = out_dir / f"work-integration-{os.getpid()}"
        integration_work.mkdir(parents=True, exist_ok=True)
        adapter_cases, adapter_mutations = _run_adapter_integration(corpus, integration_work)
        adapter_coverage = _coverage(corpus["adapterIntegration"])
        reports = {
            "coordinate": _run_category("coordinate", corpus["coordinateTransforms"], source_sha, corpus_sha, "vectors", _coordinate_result, _mutate_coordinate, adapter_cases["coordinate"], adapter_mutations["coordinate"], adapter_coverage),
            "geometry": _run_category("geometry", corpus["geometryLanes"], source_sha, corpus_sha, "vectors", _geometry_result, _mutate_geometry, adapter_cases["geometry"], adapter_mutations["geometry"], adapter_coverage),
            "anchor": _run_category("anchor", corpus["anchorResolution"], source_sha, corpus_sha, "vectors", _anchor_result, _mutate_anchor, adapter_cases["anchor"], adapter_mutations["anchor"], adapter_coverage),
            "clip": _run_category("clip", corpus["clipAndPaintOrder"], source_sha, corpus_sha, "scenes", _clip_result, _mutate_clip, adapter_cases["clip"], adapter_mutations["clip"], adapter_coverage),
            "reading": _run_category("reading", corpus["readingOrderAmbiguity"], source_sha, corpus_sha, "scenes", _reading_result, _mutate_reading, adapter_cases["reading"], adapter_mutations["reading"], adapter_coverage),
        }
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        reports = {kind: _fatal_report(kind, source_sha or "0" * 40, corpus_sha or "", message) for kind in REPORT_NAMES}
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_producer_report(
        out_dir=out_dir,
        reports=reports,
        corpus_path=corpus_path,
        source_sha=source_sha,
    )
    return 0 if all(report["status"] == "passed" for report in reports.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
