"""Bounded stdlib XLSX adapter for real Office Open XML workbooks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from pathlib import Path
import posixpath
import re
from typing import Any
import zipfile
import xml.etree.ElementTree as ET

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id, validate_zip_members
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id, validate_zip_members


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_WORKSHEET = "/worksheet"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(root: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in root.iter() if _local(child.tag) == name]


def _attr(element: ET.Element, name: str, default: str = "") -> str:
    return element.attrib.get(name, default)


def _col_number(reference: str) -> int:
    match = re.match(r"([A-Z]+)", reference.upper())
    if not match:
        return 1
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - 64
    return value


def _row_number(reference: str) -> int:
    match = re.search(r"(\d+)$", reference)
    return int(match.group(1)) if match else 1


def _cell_range(value: str, sheet_id: str) -> dict[str, Any] | None:
    endpoints = [item.strip() for item in value.split(":", 1)]
    if len(endpoints) == 1:
        endpoints.append(endpoints[0])
    if any(not item for item in endpoints):
        return None
    return {"from": {"sheetId": sheet_id, "row": _row_number(endpoints[0]), "column": _col_number(endpoints[0])}, "to": {"sheetId": sheet_id, "row": _row_number(endpoints[1]), "column": _col_number(endpoints[1])}}


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(archive.read(name))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in _children(root, "si"):
        values.append("".join(text.text or "" for text in item.iter() if _local(text.tag) == "t"))
    return values


def _shared_string_details(archive: zipfile.ZipFile) -> dict[int, dict[str, Any]]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "xl/sharedStrings.xml")
    details: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(_children(root, "si")):
        runs: list[dict[str, Any]] = []
        for run in list(item):
            if _local(run.tag) != "r":
                continue
            text_node = next((child for child in run.iter() if _local(child.tag) == "t"), None)
            if text_node is None:
                continue
            rpr = next((child for child in run if _local(child.tag) == "rPr"), None)
            run_value: dict[str, Any] = {"text": text_node.text or ""}
            if rpr is not None:
                run_value["bold"] = any(_local(child.tag) == "b" for child in rpr)
                run_value["italic"] = any(_local(child.tag) == "i" for child in rpr)
                underline = next((child for child in rpr if _local(child.tag) == "u"), None)
                if underline is not None:
                    run_value["underline"] = _attr(underline, "val", "single")
            runs.append(run_value)
        phonetic = [child.text or "" for child in item.iter() if _local(child.tag) == "rPh"]
        if runs or phonetic:
            details[index] = {"runs": runs or [{"text": "".join(text.text or "" for text in item.iter() if _local(text.tag) == "t")}], "phonetic": phonetic}
    return details


def _rich_runs(element: ET.Element) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run in _children(element, "r"):
        text_node = next((child for child in run.iter() if _local(child.tag) == "t"), None)
        if text_node is None:
            continue
        value: dict[str, Any] = {"text": text_node.text or ""}
        rpr = next((child for child in run if _local(child.tag) == "rPr"), None)
        if rpr is not None:
            value["bold"] = any(_local(child.tag) == "b" for child in rpr)
            value["italic"] = any(_local(child.tag) == "i" for child in rpr)
            underline = next((child for child in rpr if _local(child.tag) == "u"), None)
            if underline is not None:
                value["underline"] = _attr(underline, "val", "single")
        runs.append(value)
    return runs


def _relationships(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name)
    return {_attr(item, "Id"): _attr(item, "Target") for item in root if _local(item.tag) == "Relationship"}


def _date_system(workbook: ET.Element) -> str:
    props = next(iter(_children(workbook, "workbookPr")), None)
    return "1904" if props is not None and _attr(props, "date1904") in {"1", "true"} else "1900"


def _calc_context(workbook: ET.Element) -> dict[str, str]:
    calc = next(iter(_children(workbook, "calcPr")), None)
    context = {"dateSystem": _date_system(workbook), "locale": "unknown", "mode": "automatic", "referenceStyle": "A1"}
    if calc is not None and _attr(calc, "calcMode") in {"manual", "autoNoTable"}:
        context["mode"] = "manual" if _attr(calc, "calcMode") == "manual" else "automatic-except-tables"
    return context


def inspect(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    with zipfile.ZipFile(path) as archive:
        names = validate_zip_members(archive, limits)
        if "xl/workbook.xml" not in names:
            raise AdapterError("XLSX package lacks xl/workbook.xml")
        workbook = _read_xml(archive, "xl/workbook.xml")
        sheets = [item for item in _children(workbook, "sheet")]
        return {
            "format": "xlsx",
            "version": "Office Open XML",
            "bytes": path.stat().st_size,
            "parts": len(names),
            "worksheets": [_attr(item, "name") for item in sheets],
            "capabilities": ["workbook", "worksheet", "cells", "formulas", "styles", "tables", "conditional-formatting"],
            "limits": {"maxInputBytes": limits.max_input_bytes, "maxXmlParts": limits.max_xml_parts},
        }


def _typed(value: str | None, cell_type: str, status: str = "preserved", *, number_format: str | None = None, date_system: str = "1900") -> dict[str, Any]:
    if value is None:
        return {"type": "blank", "value": None, "status": status}
    if cell_type == "b":
        return {"type": "boolean", "value": value in {"1", "true", "TRUE"}, "status": status}
    if cell_type == "e":
        return {"type": "error", "value": value, "status": status}
    if cell_type in {"str", "inlineStr", "s"}:
        return {"type": "string", "value": value, "status": status}
    if value == "":
        return {"type": "blank", "value": None, "status": status}
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return {"type": "string", "value": value, "status": "unsupported"}
    if number.is_nan() or number.is_infinite():
        raise AdapterError(f"non-finite XLSX numeric token: {value}")
    exact = decimal(value)
    format_text = number_format.lower() if number_format else ""
    if format_text and any(token in format_text for token in ("yy", "mm", "dd")):
        value_type = "datetime" if any(token in format_text for token in ("h", "s")) else "date"
        return {"type": value_type, "value": _date_value(exact, date_system, include_time=value_type == "datetime"), "status": status}
    if number == number.to_integral_value():
        return {"type": "integer", "value": exact, "status": status}
    return {"type": "decimal", "value": exact, "status": status}


def _date_value(serial: str, date_system: str, *, include_time: bool = False) -> str:
    value = Decimal(serial)
    day_number = int(value)
    fraction = value - day_number
    if date_system == "1904":
        epoch = date(1904, 1, 1)
        result_date = epoch + timedelta(days=day_number)
    elif value == 60:
        # Excel's non-existent leap day has no Python date representation.
        return "1900-02-29" if not include_time else "1900-02-29T00:00:00"
    else:
        epoch = date(1899, 12, 31)
        offset = day_number - (1 if value > 60 else 0)
        result_date = epoch + timedelta(days=offset)
    if not include_time:
        return result_date.isoformat()
    total_seconds = int((fraction * Decimal(86400)).to_integral_value())
    if total_seconds >= 86400:
        result_date += timedelta(days=1)
        total_seconds -= 86400
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{result_date.isoformat()}T{hours:02d}:{minutes:02d}:{seconds:02d}"


def _displayed(value: str | None, number_format: str | None, date_system: str = "1900") -> str:
    if value is None:
        return ""
    if number_format and any(token in number_format.lower() for token in ("yy", "mm", "dd")):
        try:
            return _date_value(decimal(value), date_system)
        except (TypeError, ValueError, OverflowError, AdapterError):
            pass
    return str(value)


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: dict[str, Any], *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"xlsx-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item("extensions", {"extensionId": extension_id, "targetId": target_id, "namespace": "urn:fdir:format:xlsx", "type": extension_type, "schemaVersion": "1.0.0", "schemaId": f"urn:fdir:schema:xlsx-{extension_type}", "payload": payload, "criticality": criticality}, "extensionId")


def _xlsx_color(element: ET.Element | None) -> dict[str, Any] | None:
    if element is None:
        return None
    rgb = _attr(element, "rgb")
    if len(rgb) >= 6:
        value = rgb[-6:]
        return {"kind": "rgb", "r": int(value[0:2], 16), "g": int(value[2:4], 16), "b": int(value[4:6], 16), "a": int(rgb[-8:-6], 16) / 255 if len(rgb) >= 8 else 1}
    theme = _attr(element, "theme")
    if theme:
        return {"kind": "theme", "themeId": safe_id("theme", "xlsx-theme"), "slot": f"theme:{theme}"}
    return None


def _style_table(archive: zipfile.ZipFile, builder: DocumentBuilder) -> tuple[dict[int, str], dict[int, str]]:
    style_ids: dict[int, str] = {}
    formats: dict[int, str] = {0: "General"}
    if "xl/styles.xml" not in archive.namelist():
        return style_ids, formats
    root = _read_xml(archive, "xl/styles.xml")
    custom = {_attr(item, "numFmtId"): _attr(item, "formatCode") for item in _children(root, "numFmt")}
    fonts = _children(next(iter(_children(root, "fonts")), root), "font")
    fills = _children(next(iter(_children(root, "fills")), root), "fill")
    cell_xfs_root = next(iter(_children(root, "cellXfs")), None)
    xfs = _children(cell_xfs_root, "xf") if cell_xfs_root is not None else _children(root, "xf")
    for item in xfs:
        parent = item
        if _local(parent.tag) != "xf":
            continue
        num_id = int(_attr(item, "numFmtId", "0") or 0)
        formats[len(style_ids)] = custom.get(str(num_id), {14: "yyyy-mm-dd", 164: "yyyy-mm-dd"}.get(num_id, "General"))
        style_id = safe_id("style", f"xlsx-cell-{len(style_ids)}")
        declaration: dict[str, Any] = {"numberFormat": {"code": formats[len(style_ids)], "locale": "unknown"}}
        font_id = int(_attr(item, "fontId", "0") or 0)
        if 0 <= font_id < len(fonts):
            font = fonts[font_id]
            name = next((child for child in font if _local(child.tag) == "name"), None)
            size = next((child for child in font if _local(child.tag) == "sz"), None)
            color = next((child for child in font if _local(child.tag) == "color"), None)
            if name is not None and _attr(name, "val"):
                declaration["fontFamily"] = _attr(name, "val")
            if size is not None and _attr(size, "val"):
                declaration["fontSize"] = {"value": decimal(_attr(size, "val")), "unit": "pt"}
            if any(_local(child.tag) == "b" for child in font):
                declaration["weight"] = 700
            if any(_local(child.tag) == "i" for child in font):
                declaration["italic"] = True
            foreground = _xlsx_color(color)
            if foreground:
                declaration["foreground"] = foreground
        fill_id = int(_attr(item, "fillId", "0") or 0)
        if 0 <= fill_id < len(fills):
            pattern = next((child for child in fills[fill_id] if _local(child.tag) == "patternFill"), None)
            if pattern is not None:
                pattern_kind = _attr(pattern, "patternType", "none")
                fill_color = _xlsx_color(next((child for child in pattern if _local(child.tag) in {"fgColor", "bgColor"}), None))
                if pattern_kind in {"solid", "pattern"} and fill_color:
                    declaration["fill"] = {"kind": "solid" if pattern_kind == "solid" else "pattern", "color": fill_color}
                elif pattern_kind in {"none", ""}:
                    declaration["fill"] = {"kind": "none"}
        alignment = next((child for child in item if _local(child.tag) == "alignment"), None)
        if alignment is not None and _attr(alignment, "horizontal") in {"left", "center", "right", "justify"}:
            declaration["paragraphAlignment"] = _attr(alignment, "horizontal")
        builder.add_item("styles", {"styleId": style_id, "role": "cell", "origin": "authored", "declaration": declaration, "authored": declaration, "resolved": declaration, "status": "preserved"}, "styleId")
        style_ids[len(style_ids)] = style_id
    return style_ids, formats


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "xlsx", "Office Open XML", limits=limits)
    try:
        with zipfile.ZipFile(path) as archive:
            names = validate_zip_members(archive, limits)
            if len(names) > limits.max_xml_parts:
                diagnostic = builder.add_diagnostic("DFIR-XLSX-PACKAGE-LIMIT", f"package has {len(names)} parts; limit is {limits.max_xml_parts}", severity="error", phase="parse")
                builder.add_feature("package-validation", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            if "xl/workbook.xml" not in names:
                diagnostic = builder.add_diagnostic("DFIR-XLSX-WORKBOOK-MISSING", "XLSX package lacks xl/workbook.xml", severity="error", phase="parse")
                builder.add_feature("workbook", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            workbook = _read_xml(archive, "xl/workbook.xml")
            workbook_rels = _relationships(archive, "xl/_rels/workbook.xml.rels")
            shared = _shared_strings(archive)
            shared_details = _shared_string_details(archive)
            style_ids, number_formats = _style_table(archive, builder)
            context = _calc_context(workbook)
            package_part = safe_id("part", "xlsx-package")
            builder.add_item("parts", {"partId": package_part, "kind": "package", "name": "OOXML package", "contentType": "application/vnd.openxmlformats-package", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
            workbook_part = safe_id("part", "xlsx-workbook")
            builder.add_item("parts", {"partId": workbook_part, "kind": "workbook", "name": "xl/workbook.xml", "parentPartId": package_part, "rootNodeIds": [builder.root_id], "relationshipIds": [], "status": "preserved"}, "partId")
            part_ids: dict[str, str] = {"xl/workbook.xml": workbook_part, "[package]": package_part}
            for package_name in sorted(names):
                normalized_name = package_name.replace("\\", "/")
                if normalized_name in {"[Content_Types].xml", "xl/workbook.xml"} or normalized_name.endswith(".rels"):
                    continue
                package_part_id = safe_id("part", f"xlsx-{normalized_name}")
                part_ids[normalized_name] = package_part_id
                suffix = normalized_name.rsplit(".", 1)[-1].lower() if "." in normalized_name else ""
                kind = "worksheet" if normalized_name.startswith("xl/worksheets/") else "image" if normalized_name.startswith("xl/media/") else "xml" if suffix == "xml" else "embeddedObject"
                builder.add_item("parts", {"partId": package_part_id, "kind": kind, "name": normalized_name, "parentPartId": package_part, "rootNodeIds": [], "relationshipIds": [], "status": "preserved"}, "partId")
            for sheet_ordinal, sheet in enumerate(_children(workbook, "sheet")):
                sheet_name = _attr(sheet, "name", f"Sheet{sheet_ordinal + 1}")
                relationship = _attr(sheet, f"{{{NS_REL}}}id") or _attr(sheet, "r:id")
                target = workbook_rels.get(relationship, f"worksheets/sheet{sheet_ordinal + 1}.xml")
                target = posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
                if target not in names:
                    diagnostic = builder.add_diagnostic("DFIR-XLSX-SHEET-MISSING", f"worksheet part is missing: {target}", target_id=builder.root_id)
                    builder.add_feature("worksheet", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
                    continue
                part_id = part_ids.get(target, safe_id("part", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}"))
                surface_id = safe_id("surface", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}")
                section_id = safe_id("node", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}")
                sheet_part = builder.find("parts", "partId", part_id)
                if sheet_part is not None:
                    sheet_part.update({"kind": "worksheet", "parentPartId": workbook_part, "name": target, "rootNodeIds": [], "surfaceIds": [surface_id], "status": "preserved"})
                table_node_id = safe_id("node", f"xlsx-grid-{sheet_ordinal}-{sheet_name}")
                builder.add_item("surfaces", {"surfaceId": surface_id, "partId": part_id, "kind": "sheet", "ordinal": sheet_ordinal, "gridId": table_node_id, "status": "preserved"}, "surfaceId")
                builder.add_node("section", section_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
                builder.add_node("table", table_node_id, parent_id=section_id, part_id=part_id, status="preserved")
                if sheet_part is not None:
                    sheet_part["rootNodeIds"] = [section_id]
                    workbook_relation_id = safe_id("relation", f"xlsx-workbook-{relationship or sheet_name}")
                    builder.add_item("relations", {"relationId": workbook_relation_id, "kind": "references", "fromId": workbook_part, "toId": part_id, "status": "preserved"}, "relationId")
                    builder.find("parts", "partId", workbook_part).setdefault("relationshipIds", []).append(workbook_relation_id)
                builder.add_source_map(section_id, {"path": target, "worksheet": sheet_name})
                sheet_root = _read_xml(archive, target)
                for view in _children(sheet_root, "sheetView"):
                    pane = next(iter(_children(view, "pane")), None)
                    pane_payload = {key: _attr(pane, key) for key in ("xSplit", "ySplit", "topLeftCell", "activePane", "state") if pane is not None and _attr(pane, key)}
                    selections = [{"pane": _attr(selection, "pane", "topLeft"), "activeCell": _attr(selection, "activeCell", "A1"), "sqref": _attr(selection, "sqref", "A1")} for selection in _children(view, "selection")]
                    _extension(builder, section_id, "view", {"viewId": _attr(view, "workbookViewId", str(sheet_ordinal)), "pane": pane_payload, "selection": selections})
                    builder.add_feature("worksheet-view", "preserved", target_id=section_id)
                for validation in _children(sheet_root, "dataValidation"):
                    formulas = [child.text or "" for child in validation if _local(child.tag) in {"formula1", "formula2"}]
                    _extension(builder, section_id, "data-validation", {"range": _attr(validation, "sqref"), "type": _attr(validation, "type"), "operator": _attr(validation, "operator", "between"), "allowBlank": _attr(validation, "allowBlank") in {"1", "true"}, "formulas": formulas})
                    builder.add_feature("data-validation", "preserved", target_id=section_id)
                for defined_name in _children(workbook, "definedName"):
                    local_sheet = _attr(defined_name, "localSheetId")
                    if local_sheet == str(sheet_ordinal) or (local_sheet == "" and sheet_ordinal == 0):
                        _extension(builder, section_id, "defined-name", {"name": _attr(defined_name, "name"), "localSheetId": local_sheet, "hidden": _attr(defined_name, "hidden") in {"1", "true"}, "formula": defined_name.text or ""})
                        builder.add_feature("defined-name", "preserved", target_id=section_id)
                rows = [row for row in _children(sheet_root, "row")]
                max_column = max((_col_number(_attr(cell, "r")) for row in rows for cell in _children(row, "c")), default=1)
                column_visibility: dict[int, dict[str, Any]] = {}
                for column_definition in _children(sheet_root, "col"):
                    minimum = int(_attr(column_definition, "min", "1") or 1)
                    maximum = int(_attr(column_definition, "max", str(minimum)) or minimum)
                    state = {"declared": "hidden" if _attr(column_definition, "hidden") in {"1", "true"} else "visible"}
                    for column_number in range(minimum, maximum + 1):
                        column_visibility[column_number] = state
                column_ids: list[str] = []
                for column in range(1, max_column + 1):
                    column_id = safe_id("node", f"xlsx-column-{sheet_ordinal}-{column}")
                    builder.add_node("column", column_id, parent_id=table_node_id, status="preserved", visibility=column_visibility.get(column))
                    column_ids.append(column_id)
                row_ids: list[str] = []
                cell_ids: list[str] = []
                for row_element in rows:
                    row_number = int(_attr(row_element, "r", "1") or 1)
                    row_id = safe_id("node", f"xlsx-row-{sheet_ordinal}-{row_number}")
                    row_visibility = {"declared": "hidden" if _attr(row_element, "hidden") in {"1", "true"} else "visible"}
                    builder.add_node("row", row_id, parent_id=table_node_id, status="preserved", visibility=row_visibility)
                    row_ids.append(row_id)
                    for cell in _children(row_element, "c"):
                        reference = _attr(cell, "r", f"A{row_number}")
                        column_number = _col_number(reference)
                        cell_id = safe_id("node", f"xlsx-cell-{sheet_ordinal}-{reference}")
                        style_index = int(_attr(cell, "s", "0") or 0)
                        style_id = style_ids.get(style_index)
                        style_fields = {"styleIds": [style_id], "directStyleId": style_id} if style_id else {}
                        cell_type = _attr(cell, "t", "n")
                        value_element = next(iter(_children(cell, "v")), None)
                        raw_value = value_element.text if value_element is not None else None
                        rich_details: dict[str, Any] | None = None
                        if cell_type == "s" and raw_value is not None:
                            shared_index = raw_value
                            try:
                                raw_value = shared[int(raw_value)]
                                rich_details = shared_details.get(int(shared_index))
                            except (ValueError, IndexError):
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-SHARED-STRING-MISSING", f"shared string index is unavailable: {raw_value}", target_id=cell_id)
                                builder.add_feature("shared-string", "failed", target_id=cell_id, diagnostic_ids=[diagnostic])
                        elif cell_type == "inlineStr":
                            raw_value = "".join(text.text or "" for text in cell.iter() if _local(text.tag) == "t")
                            inline_runs = _rich_runs(cell)
                            if inline_runs:
                                rich_details = {"runs": inline_runs, "phonetic": [item.text or "" for item in cell.iter() if _local(item.tag) == "rPh"]}
                        formula_element = next(iter(_children(cell, "f")), None)
                        formula_source = formula_element.text if formula_element is not None else None
                        cell_format = number_formats.get(style_index)
                        typed_value = _typed(raw_value, cell_type, number_format=cell_format, date_system=context["dateSystem"])
                        cell_status = "unsupported" if typed_value.get("status") == "unsupported" else "preserved"
                        builder.add_node("cell", cell_id, parent_id=row_id, part_id=part_id, address={"sheetId": surface_id, "row": row_number, "column": column_number}, value=typed_value, status=cell_status, **style_fields)
                        if cell_status == "unsupported":
                            diagnostic = builder.add_diagnostic("DFIR-XLSX-NUMERIC-TOKEN-UNSUPPORTED", f"numeric cell token is not a supported exact decimal: {raw_value}", target_id=cell_id, phase="normalize")
                            builder.add_feature("exact-cell-value", "unsupported", target_id=cell_id, diagnostic_ids=[diagnostic])
                        if rich_details:
                            _extension(builder, cell_id, "rich-text", rich_details)
                            builder.add_feature("rich-text", "preserved", target_id=cell_id)
                        displayed = _displayed(raw_value, cell_format, context["dateSystem"])
                        source_text_id = safe_id("text", f"xlsx-source-{sheet_ordinal}-{reference}")
                        builder.add_text(source_text_id, raw_value or "", representation="source", provenance="authored", status="preserved")
                        builder.link_text(cell_id, source_text_id)
                        display_text_id = safe_id("text", f"xlsx-displayed-{sheet_ordinal}-{reference}")
                        builder.add_text(display_text_id, displayed, representation="displayed", provenance="formatter", source_text_id=source_text_id, status="preserved")
                        builder.link_text(cell_id, display_text_id)
                        builder.add_source_map(cell_id, {"path": target, "worksheet": sheet_name, "cell": reference})
                        cell_ids.append(cell_id)
                        if formula_source is not None:
                            formula_id = safe_id("formula", f"xlsx-{sheet_ordinal}-{reference}")
                            computed_diagnostic = builder.add_diagnostic(
                                "DFIR-XLSX-COMPUTED-VALUE-UNAVAILABLE",
                                "The adapter preserves the authored formula and stored/cache lanes but does not execute workbook calculation.",
                                phase="observe",
                                target_id=cell_id,
                            )
                            cached_value = _typed(raw_value, cell_type, number_format=cell_format, date_system=context["dateSystem"])
                            values: dict[str, Any] = {
                                "raw": _typed(formula_source, "str"),
                                "stored": deepcopy(typed_value),
                                "cached": cached_value,
                                "computed": {"type": "blank", "value": None, "status": "unavailable"},
                                "displayed": {"text": displayed, "status": "preserved"},
                                "laneProvenance": {
                                    "stored": "worksheet-cell-value",
                                    "cached": "worksheet-formula-cache",
                                    "computed": "calculation-engine-unavailable",
                                    "displayed": "number-format-renderer",
                                },
                            }
                            builder.add_item("formulas", {"formulaId": formula_id, "ownerCellId": cell_id, "kind": "spreadsheetFormula", "expression": {"source": formula_source, "language": "excel-a1", "status": "preserved"}, "values": values, "numberFormat": {"code": number_formats.get(style_index, "General"), "locale": "unknown"}, "calculationContext": context, "status": "preserved"}, "formulaId")
                            builder.find("nodes", "nodeId", cell_id)["formulaId"] = formula_id
                            builder.add_feature("formula", "preserved", target_id=cell_id)
                            builder.add_feature("computed-value", "unavailable", target_id=cell_id, diagnostic_ids=[computed_diagnostic])
                merged_ranges = [_cell_range(_attr(item, "ref"), surface_id) for item in _children(sheet_root, "mergeCell")]
                merged_ranges = [item for item in merged_ranges if item is not None]
                builder.add_item("tables", {"tableId": safe_id("table", f"xlsx-grid-{sheet_ordinal}"), "nodeId": table_node_id, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "mergedRanges": merged_ranges, "status": "preserved"}, "tableId")
                sheet_rel_name = f"xl/worksheets/_rels/{target.rsplit('/', 1)[-1]}.rels"
                table_names_for_sheet = [posixpath.normpath(posixpath.join("xl/worksheets", table_target)) for table_target in _relationships(archive, sheet_rel_name).values() if table_target.startswith("../tables/")]
                if not table_names_for_sheet and len([name for name in names if name.startswith("xl/tables/") and name.endswith(".xml")]) == 1:
                    table_names_for_sheet = [name for name in names if name.startswith("xl/tables/") and name.endswith(".xml")]
                for table_name in sorted(name for name in table_names_for_sheet if name in names):
                    table_root = _read_xml(archive, table_name)
                    table_id = safe_id("table", f"xlsx-{sheet_ordinal}-{_attr(table_root, 'name', table_name)}")
                    builder.add_item("tables", {"tableId": table_id, "nodeId": table_node_id, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "status": "preserved"}, "tableId")
                    table_part_id = part_ids.get(table_name)
                    if table_part_id is not None:
                        table_relation_id = safe_id("relation", f"xlsx-{target}-{table_name}")
                        builder.add_item("relations", {"relationId": table_relation_id, "kind": "references", "fromId": part_id, "toId": table_part_id, "status": "preserved"}, "relationId")
                        if sheet_part is not None:
                            sheet_part.setdefault("relationshipIds", []).append(table_relation_id)
                    _extension(builder, section_id, "table-definition", {"path": table_name, "name": _attr(table_root, "name"), "range": _attr(table_root, "ref")})
                for cf in _children(sheet_root, "conditionalFormatting"):
                    _extension(builder, section_id, "conditional-formatting", {"range": _attr(cf, "sqref"), "rules": [{"type": _attr(rule, "type"), "operator": _attr(rule, "operator"), "priority": _attr(rule, "priority"), "formula": [child.text or "" for child in rule if _local(child.tag) == "formula"]} for rule in _children(cf, "cfRule")]})
                for name in names:
                    if "pivot" in name.casefold() or "externalLink" in name:
                        diagnostic = builder.add_diagnostic("DFIR-XLSX-FEATURE-UNSUPPORTED", f"XLSX feature is retained only as a diagnostic: {name}", target_id=section_id)
                        builder.add_feature("package-extension", "unsupported", target_id=section_id, diagnostic_ids=[diagnostic])
                builder.add_item("orders", {"orderId": safe_id("order", f"xlsx-grid-{sheet_ordinal}"), "kind": "grid", "ownerId": section_id, "items": [{"id": item, "ordinal": index} for index, item in enumerate(cell_ids)], "status": "preserved"}, "orderId")
                builder.add_feature("worksheet", "preserved", target_id=section_id)
            workbook_part_item = builder.find("parts", "partId", workbook_part)
            for relationship_key, relationship_target in workbook_rels.items():
                target_name = posixpath.normpath(posixpath.join("xl", relationship_target))
                target_part_id = part_ids.get(target_name)
                if target_part_id is None:
                    diagnostic = builder.add_diagnostic("DFIR-XLSX-RELATION-TARGET-MISSING", f"workbook relationship target is missing: {target_name}", target_id=workbook_part, phase="parse")
                    builder.add_feature("package-relationship", "ambiguous", target_id=workbook_part, diagnostic_ids=[diagnostic])
                    continue
                relation_id = safe_id("relation", f"xlsx-workbook-package-{relationship_key}")
                if builder.find("relations", "relationId", relation_id) is None:
                    builder.add_item("relations", {"relationId": relation_id, "kind": "references", "fromId": workbook_part, "toId": target_part_id, "status": "preserved"}, "relationId")
                    if workbook_part_item is not None:
                        workbook_part_item.setdefault("relationshipIds", []).append(relation_id)
            if "_rels/.rels" in names:
                relation_id = safe_id("relation", "xlsx-package-workbook")
                builder.add_item("relations", {"relationId": relation_id, "kind": "references", "fromId": package_part, "toId": workbook_part, "status": "preserved"}, "relationId")
                package_item = builder.find("parts", "partId", package_part)
                if package_item is not None:
                    package_item.setdefault("relationshipIds", []).append(relation_id)
            for package_name in sorted(names):
                if not (package_name.startswith("xl/media/") or package_name.startswith("xl/charts/") or package_name.startswith("xl/drawings/") or package_name.startswith("xl/pivotTables/") or package_name.startswith("xl/externalLinks/")):
                    continue
                source_part_id = part_ids.get(package_name)
                if source_part_id is None:
                    continue
                if package_name.startswith("xl/media/"):
                    resource_kind = "image"
                    availability = "available"
                elif package_name.startswith("xl/charts/"):
                    resource_kind = "chart"
                    availability = "available"
                elif package_name.startswith("xl/externalLinks/"):
                    resource_kind = "linkedObject"
                    availability = "unavailable"
                else:
                    resource_kind = "embeddedObject"
                    availability = "available"
                relation_id = safe_id("relation", f"xlsx-resource-{package_name}")
                resource_id = safe_id("resource", f"xlsx-resource-{package_name}")
                if builder.find("resources", "resourceId", resource_id) is None:
                    builder.add_item("resources", {"resourceId": resource_id, "kind": resource_kind, "mediaType": "application/octet-stream", "availability": availability, "derivedHandle": package_name, "sourceRelationshipId": relation_id}, "resourceId")
                builder.add_item("relations", {"relationId": relation_id, "kind": "usesResource", "fromId": source_part_id, "toId": resource_id, "status": "preserved" if availability == "available" else "unavailable"}, "relationId")
                source_part = builder.find("parts", "partId", source_part_id)
                if source_part is not None:
                    source_part.setdefault("relationshipIds", []).append(relation_id)
            builder.add_item("orders", {"orderId": safe_id("order", "xlsx-tabs"), "kind": "tab", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"]) if node["kind"] == "section"], "status": "preserved"}, "orderId")
            builder.add_feature("workbook", "preserved", target_id=builder.root_id)
            return builder.finish()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-XLSX-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("workbook", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
