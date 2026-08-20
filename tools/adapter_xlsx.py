"""Bounded stdlib XLSX adapter for real Office Open XML workbooks."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any
import zipfile
import xml.etree.ElementTree as ET

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id


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
        names = archive.namelist()
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


def _typed(value: str | None, cell_type: str, status: str = "preserved") -> dict[str, Any]:
    if value is None or value == "":
        return {"type": "blank", "value": None, "status": status}
    if cell_type == "b":
        return {"type": "boolean", "value": value in {"1", "true", "TRUE"}, "status": status}
    if cell_type == "e":
        return {"type": "error", "value": value, "status": status}
    if cell_type in {"str", "inlineStr", "s"}:
        return {"type": "string", "value": value, "status": status}
    try:
        number = float(value)
        if number.is_integer():
            return {"type": "integer", "value": int(number), "status": status}
        return {"type": "number", "value": number, "status": status}
    except ValueError:
        return {"type": "string", "value": value, "status": status}


def _displayed(value: str | None, number_format: str | None) -> str:
    if value is None:
        return ""
    if number_format and any(token in number_format.lower() for token in ("yy", "mm", "dd")):
        try:
            serial = float(value)
            epoch = datetime(1899, 12, 30)
            return (epoch + timedelta(days=serial)).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return str(value)


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: dict[str, Any], *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"xlsx-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item("extensions", {"extensionId": extension_id, "targetId": target_id, "namespace": "urn:fdir:format:xlsx", "type": extension_type, "schemaVersion": "1.0.0", "schemaId": f"urn:fdir:schema:xlsx-{extension_type}", "payload": payload, "criticality": criticality}, "extensionId")


def _style_table(archive: zipfile.ZipFile, builder: DocumentBuilder) -> tuple[dict[int, str], dict[int, str]]:
    style_ids: dict[int, str] = {}
    formats: dict[int, str] = {0: "General"}
    if "xl/styles.xml" not in archive.namelist():
        return style_ids, formats
    root = _read_xml(archive, "xl/styles.xml")
    custom = {_attr(item, "numFmtId"): _attr(item, "formatCode") for item in _children(root, "numFmt")}
    for item in _children(root, "xf"):
        parent = item
        if _local(parent.tag) != "xf":
            continue
        num_id = int(_attr(item, "numFmtId", "0") or 0)
        formats[len(style_ids)] = custom.get(str(num_id), {14: "yyyy-mm-dd", 164: "yyyy-mm-dd"}.get(num_id, "General"))
        style_id = safe_id("style", f"xlsx-cell-{len(style_ids)}")
        declaration: dict[str, Any] = {"numberFormat": {"code": formats[len(style_ids)], "locale": "unknown"}}
        builder.add_item("styles", {"styleId": style_id, "role": "cell", "origin": "authored", "declaration": declaration, "authored": declaration, "resolved": declaration, "status": "preserved"}, "styleId")
        style_ids[len(style_ids)] = style_id
    return style_ids, formats


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "xlsx", "Office Open XML", limits=limits)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
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
            style_ids, number_formats = _style_table(archive, builder)
            context = _calc_context(workbook)
            workbook_part = safe_id("part", "xlsx-workbook")
            builder.add_item("parts", {"partId": workbook_part, "kind": "workbook", "name": "xl/workbook.xml", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
            for sheet_ordinal, sheet in enumerate(_children(workbook, "sheet")):
                sheet_name = _attr(sheet, "name", f"Sheet{sheet_ordinal + 1}")
                relationship = _attr(sheet, f"{{{NS_REL}}}id") or _attr(sheet, "r:id")
                target = workbook_rels.get(relationship, f"worksheets/sheet{sheet_ordinal + 1}.xml")
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target.lstrip("./")
                if target not in names:
                    diagnostic = builder.add_diagnostic("DFIR-XLSX-SHEET-MISSING", f"worksheet part is missing: {target}", target_id=builder.root_id)
                    builder.add_feature("worksheet", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
                    continue
                part_id = safe_id("part", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}")
                surface_id = safe_id("surface", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}")
                section_id = safe_id("node", f"xlsx-sheet-{sheet_ordinal}-{sheet_name}")
                builder.add_item("parts", {"partId": part_id, "kind": "worksheet", "parentPartId": workbook_part, "name": target, "rootNodeIds": [section_id], "surfaceIds": [surface_id], "status": "preserved"}, "partId")
                builder.add_item("surfaces", {"surfaceId": surface_id, "partId": part_id, "kind": "sheet", "ordinal": sheet_ordinal, "gridId": section_id, "status": "preserved"}, "surfaceId")
                builder.add_node("section", section_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
                builder.add_source_map(section_id, {"path": target, "worksheet": sheet_name})
                sheet_root = _read_xml(archive, target)
                rows = [row for row in _children(sheet_root, "row")]
                max_column = max((_col_number(_attr(cell, "r")) for row in rows for cell in _children(row, "c")), default=1)
                column_ids: list[str] = []
                for column in range(1, max_column + 1):
                    column_id = safe_id("node", f"xlsx-column-{sheet_ordinal}-{column}")
                    builder.add_node("column", column_id, parent_id=section_id, status="preserved")
                    column_ids.append(column_id)
                row_ids: list[str] = []
                cell_ids: list[str] = []
                for row_element in rows:
                    row_number = int(_attr(row_element, "r", "1") or 1)
                    row_id = safe_id("node", f"xlsx-row-{sheet_ordinal}-{row_number}")
                    builder.add_node("row", row_id, parent_id=section_id, status="preserved")
                    row_ids.append(row_id)
                    for cell in _children(row_element, "c"):
                        reference = _attr(cell, "r", f"A{row_number}")
                        column_number = _col_number(reference)
                        cell_id = safe_id("node", f"xlsx-cell-{sheet_ordinal}-{reference}")
                        style_index = int(_attr(cell, "s", "0") or 0)
                        style_id = style_ids.get(style_index)
                        style_fields = {"styleIds": [style_id], "directStyleId": style_id} if style_id else {}
                        builder.add_node("cell", cell_id, parent_id=row_id, part_id=part_id, address={"sheetId": surface_id, "row": row_number, "column": column_number}, status="preserved", **style_fields)
                        cell_type = _attr(cell, "t", "n")
                        value_element = next(iter(_children(cell, "v")), None)
                        raw_value = value_element.text if value_element is not None else None
                        if cell_type == "s" and raw_value is not None:
                            try:
                                raw_value = shared[int(raw_value)]
                            except (ValueError, IndexError):
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-SHARED-STRING-MISSING", f"shared string index is unavailable: {raw_value}", target_id=cell_id)
                                builder.add_feature("shared-string", "failed", target_id=cell_id, diagnostic_ids=[diagnostic])
                        elif cell_type == "inlineStr":
                            raw_value = "".join(text.text or "" for text in cell.iter() if _local(text.tag) == "t")
                        formula_element = next(iter(_children(cell, "f")), None)
                        formula_source = formula_element.text if formula_element is not None else None
                        displayed = _displayed(raw_value, number_formats.get(style_index))
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
                            values: dict[str, Any] = {
                                "raw": _typed(formula_source, "str"),
                                "stored": _typed(raw_value, cell_type),
                                "cached": _typed(raw_value, cell_type),
                                "displayed": {"text": displayed, "status": "preserved"},
                            }
                            builder.add_item("formulas", {"formulaId": formula_id, "ownerCellId": cell_id, "kind": "spreadsheetFormula", "expression": {"source": formula_source, "language": "excel-a1", "status": "preserved"}, "values": values, "numberFormat": {"code": number_formats.get(style_index, "General"), "locale": "unknown"}, "calculationContext": context, "status": "preserved"}, "formulaId")
                            builder.find("nodes", "nodeId", cell_id)["formulaId"] = formula_id
                            builder.add_feature("formula", "preserved", target_id=cell_id)
                table_nodes: list[str] = []
                for table_name in sorted(name for name in names if name.startswith("xl/tables/") and name.endswith(".xml")):
                    table_root = _read_xml(archive, table_name)
                    table_id = safe_id("table", f"xlsx-{sheet_ordinal}-{_attr(table_root, 'name', table_name)}")
                    builder.add_item("tables", {"tableId": table_id, "nodeId": section_id, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "status": "preserved"}, "tableId")
                    _extension(builder, section_id, "table-definition", {"path": table_name, "name": _attr(table_root, "name"), "range": _attr(table_root, "ref")})
                    table_nodes.append(table_id)
                for cf in _children(sheet_root, "conditionalFormatting"):
                    _extension(builder, section_id, "conditional-formatting", {"range": _attr(cf, "sqref"), "rules": [{"type": _attr(rule, "type"), "operator": _attr(rule, "operator"), "priority": _attr(rule, "priority"), "formula": [child.text or "" for child in rule if _local(child.tag) == "formula"]} for rule in _children(cf, "cfRule")]})
                for name in names:
                    if "pivot" in name.casefold() or "externalLink" in name:
                        diagnostic = builder.add_diagnostic("DFIR-XLSX-FEATURE-UNSUPPORTED", f"XLSX feature is retained only as a diagnostic: {name}", target_id=section_id)
                        builder.add_feature("package-extension", "unsupported", target_id=section_id, diagnostic_ids=[diagnostic])
                builder.add_item("orders", {"orderId": safe_id("order", f"xlsx-grid-{sheet_ordinal}"), "kind": "grid", "ownerId": section_id, "items": [{"id": item, "ordinal": index} for index, item in enumerate(cell_ids)], "status": "preserved"}, "orderId")
                builder.add_feature("worksheet", "preserved", target_id=section_id)
            builder.add_item("orders", {"orderId": safe_id("order", "xlsx-tabs"), "kind": "tab", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"]) if node["kind"] == "section"], "status": "preserved"}, "orderId")
            builder.add_feature("workbook", "preserved", target_id=builder.root_id)
            return builder.finish()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-XLSX-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("workbook", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
