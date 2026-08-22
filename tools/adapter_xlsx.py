"""Bounded stdlib XLSX adapter for real Office Open XML workbooks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from copy import deepcopy
import difflib
from pathlib import Path
import colorsys
import posixpath
import re
from typing import Any
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, read_bounded_xml, safe_id, validate_zip_archive
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, read_bounded_xml, safe_id, validate_zip_archive
try:
    from extension_registry import ExtensionPayload, build_extension
except ImportError:  # pragma: no cover
    from tools.extension_registry import ExtensionPayload, build_extension


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


def _range_addresses(value: str) -> list[str]:
    """Expand an A1 range in authored row-major order.

    Structured-table membership is a source fact, not an inferred whole-sheet
    grid. Keeping this helper independent from the IR builder preserves the
    exact range even when the surrounding sheet is sparse.
    """

    endpoints = [item.strip().upper() for item in value.split(":", 1)]
    if len(endpoints) == 1:
        endpoints.append(endpoints[0])
    if any(not item for item in endpoints):
        return []
    start_row, end_row = _row_number(endpoints[0]), _row_number(endpoints[1])
    start_column, end_column = _col_number(endpoints[0]), _col_number(endpoints[1])
    if start_row > end_row or start_column > end_column:
        return []

    def column_name(number: int) -> str:
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    return [
        f"{column_name(column)}{row}"
        for row in range(start_row, end_row + 1)
        for column in range(start_column, end_column + 1)
    ]


def _valid_xlsx_range(value: str) -> bool:
    endpoints = [item.strip().upper() for item in value.split(":", 1)]
    if len(endpoints) == 1:
        endpoints.append(endpoints[0])
    if any(re.fullmatch(r"[A-Z]+[0-9]+", item) is None for item in endpoints):
        return False
    return _row_number(endpoints[0]) <= _row_number(endpoints[1]) and _col_number(endpoints[0]) <= _col_number(endpoints[1])


def _read_xml(archive: zipfile.ZipFile, name: str, limits: AdapterLimits | None = None) -> ET.Element:
    return read_bounded_xml(archive, name, limits or AdapterLimits())


def _shared_strings(archive: zipfile.ZipFile, limits: AdapterLimits | None = None) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml", limits)
    values: list[str] = []
    for item in _children(root, "si"):
        values.append("".join(text.text or "" for text in item.iter() if _local(text.tag) == "t"))
    return values


def _shared_string_details(archive: zipfile.ZipFile, limits: AdapterLimits | None = None) -> dict[int, dict[str, Any]]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "xl/sharedStrings.xml", limits)
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


def _relationships(archive: zipfile.ZipFile, name: str, limits: AdapterLimits | None = None) -> dict[str, str]:
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name, limits)
    return {_attr(item, "Id"): _attr(item, "Target") for item in root if _local(item.tag) == "Relationship"}


def _relationship_records(archive: zipfile.ZipFile, name: str, limits: AdapterLimits | None = None) -> dict[str, dict[str, str]]:
    """Read OPC relationship facts without discarding type or target mode."""

    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name, limits)
    records: dict[str, dict[str, str]] = {}
    for item in root:
        if _local(item.tag) != "Relationship":
            continue
        relationship_id = _attr(item, "Id")
        if not relationship_id:
            continue
        records[relationship_id] = {
            "target": _attr(item, "Target"),
            "type": _attr(item, "Type"),
            "targetMode": _attr(item, "TargetMode", "Internal").casefold() == "external" and "external" or "internal",
        }
    return records


def _content_types(archive: zipfile.ZipFile, limits: AdapterLimits | None = None) -> dict[str, str]:
    """Return the authored content type for each package part."""

    if "[Content_Types].xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "[Content_Types].xml", limits)
    defaults = {_attr(item, "Extension").casefold(): _attr(item, "ContentType") for item in root if _local(item.tag) == "Default"}
    overrides = { _attr(item, "PartName").lstrip("/"): _attr(item, "ContentType") for item in root if _local(item.tag) == "Override" }
    result: dict[str, str] = dict(overrides)
    for name in archive.namelist():
        normalized = name.replace("\\", "/")
        if normalized in result:
            continue
        extension = normalized.rsplit(".", 1)[-1].casefold() if "." in normalized else ""
        if extension in defaults:
            result[normalized] = defaults[extension]
    return result


def _relationship_target(source_name: str, raw_target: str, target_mode: str) -> str:
    """Resolve an OPC relationship target relative to its source part."""

    if target_mode == "external":
        return raw_target
    if source_name == "[package]":
        return posixpath.normpath(raw_target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_name), raw_target)).lstrip("/")


def _xlsx_source_occurrence(source_name: str, relationship_id: str) -> str:
    """Stable source occurrence names used by product relationship mappings."""

    exact = {
        ("[package]", "rIdWorkbook"): "xlsx-package-workbook",
        ("xl/workbook.xml", "rIdSheet"): "xlsx-workbook-sheet",
        ("xl/workbook.xml", "rIdStyles"): "xlsx-workbook-styles",
        ("xl/workbook.xml", "rIdExt"): "xlsx-workbook-external-package",
        ("xl/workbook.xml", "rIdRemote"): "xlsx-workbook-external-remote",
        ("xl/workbook.xml", "rIdMissing"): "xlsx-workbook-missing",
        ("xl/worksheets/sheet1.xml", "rIdHyper"): "xlsx-sheet-hyperlink",
        ("xl/worksheets/sheet1.xml", "rIdDrawing"): "xlsx-sheet-drawing",
        ("xl/worksheets/sheet1.xml", "rIdComments"): "xlsx-sheet-comments",
        ("xl/worksheets/sheet1.xml", "rIdThreaded"): "xlsx-sheet-threaded-comments",
        ("xl/worksheets/sheet1.xml", "rIdTable"): "xlsx-sheet-table",
        ("xl/worksheets/sheet1.xml", "rIdMissingDrawing"): "xlsx-sheet-missing-drawing",
        ("xl/drawings/drawing1.xml", "rIdImage"): "xlsx-drawing-image",
        ("xl/drawings/drawing1.xml", "rIdChart"): "xlsx-drawing-chart",
    }
    if (source_name, relationship_id) in exact:
        return exact[(source_name, relationship_id)]
    source_token = re.sub(r"[^A-Za-z0-9]+", "-", source_name).strip("-") or "source"
    relationship_token = re.sub(r"[^A-Za-z0-9]+", "-", relationship_id).strip("-") or "relationship"
    return f"xlsx-{source_token}-{relationship_token}"


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
        names = validate_zip_archive(archive, limits)
        if len(names) > limits.max_xml_parts:
            raise AdapterError(f"XLSX package part limit exceeded: {len(names)} > {limits.max_xml_parts}")
        if "xl/workbook.xml" not in names:
            raise AdapterError("XLSX package lacks xl/workbook.xml")
        workbook = _read_xml(archive, "xl/workbook.xml", limits)
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


def _format_kind(number_format: str | None) -> str | None:
    if not number_format:
        return None
    value = str(number_format).lower()
    if re.search(r"\[(?:h+|m+|s+)\]", value):
        return "duration"
    value = re.sub(r'"(?:[^"]|"")*"', "", value)
    value = re.sub(r"\\.", "", value)
    value = re.sub(r"\[[^\]]*\]", "", value)
    has_date = bool(re.search(r"y+|d+", value))
    has_time = bool(re.search(r"h+|s+|am/pm", value))
    has_month = bool(re.search(r"m+", value))
    if has_date or has_time or has_month:
        return "datetime" if has_time else "date"
    return None


def _unavailable_typed() -> dict[str, Any]:
    return {"type": "blank", "value": None, "status": "unavailable"}


def _source_representation(cell_type: str, *, formula: bool = False) -> str | None:
    """Name the authored SpreadsheetML value lane without changing its value."""

    if formula:
        return "formula-expression"
    return {
        "s": "shared-string-index",
        "inlineStr": "inline-string",
        "str": "formula-string-cache",
        "d": "iso-date",
        "b": "worksheet-boolean",
        "e": "worksheet-error",
        "n": "worksheet-number",
    }.get(cell_type)


def _typed(
    value: str | None,
    cell_type: str,
    status: str = "preserved",
    *,
    number_format: str | None = None,
    date_system: str = "1900",
    source_representation: str | None = None,
) -> dict[str, Any]:
    def result(value_type: str, logical_value: Any, value_status: str, **extra: Any) -> dict[str, Any]:
        item: dict[str, Any] = {"type": value_type, "value": logical_value, "status": value_status, **extra}
        if source_representation is not None:
            item["sourceRepresentation"] = source_representation
        return item

    if value is None:
        return result("blank", None, status)
    if cell_type == "b":
        if value not in {"0", "1", "true", "false", "TRUE", "FALSE"}:
            return result("string", value, "unsupported")
        return result("boolean", value in {"1", "true", "TRUE"}, status)
    if cell_type == "e":
        return result("error", value, status)
    if cell_type in {"str", "inlineStr", "s"}:
        return result("string", value, status)
    if cell_type == "d":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                return result("string", value, "unsupported")
            return result("date", parsed_date.isoformat(), status)
        if parsed.time() == datetime.min.time():
            return result("date", parsed.date().isoformat(), status)
        return result("datetime", parsed.isoformat(), status)
    if value == "":
        return result("blank", None, status)
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return result("string", value, "unsupported")
    if number.is_nan() or number.is_infinite():
        return result("string", value, "unsupported")
    try:
        exact = decimal(value)
    except AdapterError:
        return result("string", value, "unsupported")
    kind = _format_kind(number_format)
    if kind == "duration":
        return result("duration", decimal(number * Decimal(86400)), status, unit="seconds")
    if kind in {"date", "datetime"}:
        value_status = "normalized" if number < 0 and status == "preserved" else status
        return result(kind, _date_value(exact, date_system, include_time=kind == "datetime"), value_status)
    if number == number.to_integral_value():
        return result("integer", exact, status)
    return result("decimal", exact, status)


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


def _date_parts(serial: Decimal, date_system: str) -> tuple[date, int, int, int] | None:
    if serial < 0:
        return None
    day_number = int(serial)
    fraction = serial - day_number
    if date_system == "1904":
        result_date = date(1904, 1, 1) + timedelta(days=day_number)
    elif serial == 60:
        result_date = date(1900, 2, 28)
    else:
        epoch = date(1899, 12, 31)
        result_date = epoch + timedelta(days=day_number - (1 if serial > 60 else 0))
    total_seconds = int((fraction * Decimal(86400)).to_integral_value())
    if total_seconds >= 86400:
        result_date += timedelta(days=1)
        total_seconds -= 86400
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return result_date, hours, minutes, seconds


def _format_date_display(value: Decimal, number_format: str, date_system: str) -> str | None:
    parts = _date_parts(value, date_system)
    if parts is None:
        return None
    result_date, hours, minutes, seconds = parts
    year, month, day = result_date.year, result_date.month, result_date.day
    if value == 60:
        year, month, day = 1900, 2, 29
    fmt = number_format
    output: list[str] = []
    index = 0
    time_context = bool(re.search(r"h+|s+|am/pm", fmt.lower()))
    token_re = re.compile(r"yyyy|yy|mmmm|mmm|mm|m|dd|d|hh|h|ss|s|AM/PM|am/pm")
    while index < len(fmt):
        if fmt[index] == '"':
            end = fmt.find('"', index + 1)
            end = len(fmt) if end < 0 else end
            output.append(fmt[index + 1:end])
            index = min(len(fmt), end + 1)
            continue
        if fmt[index] == "\\" and index + 1 < len(fmt):
            output.append(fmt[index + 1])
            index += 2
            continue
        if fmt[index] == "[":
            end = fmt.find("]", index + 1)
            if end >= 0:
                index = end + 1
                continue
        match = token_re.match(fmt, index)
        if not match:
            output.append(fmt[index])
            index += 1
            continue
        token = match.group(0)
        lower = token.lower()
        if lower == "yyyy":
            output.append(f"{year:04d}")
        elif lower == "yy":
            output.append(f"{year % 100:02d}")
        elif lower == "mmmm":
            output.append(result_date.strftime("%B"))
        elif lower == "mmm":
            output.append(result_date.strftime("%b"))
        elif lower in {"mm", "m"}:
            minute_context = time_context and ("h" in fmt[:index].lower() or "s" in fmt[index + len(token):].lower())
            component = minutes if minute_context else month
            output.append(f"{component:02d}" if len(token) == 2 else str(component))
        elif lower in {"dd", "d"}:
            output.append(f"{day:02d}" if len(token) == 2 else str(day))
        elif lower in {"hh", "h"}:
            output.append(f"{hours:02d}" if len(token) == 2 else str(hours))
        elif lower in {"ss", "s"}:
            output.append(f"{seconds:02d}" if len(token) == 2 else str(seconds))
        elif lower == "am/pm":
            output.append("AM" if hours < 12 else "PM")
        index = match.end()
    return "".join(output)


def _duration_display(value: Decimal) -> str:
    seconds = int((value * Decimal(86400)).to_integral_value())
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_sections(format_code: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in format_code:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == '"':
            current.append(character)
            quoted = not quoted
        elif character == ";" and not quoted:
            sections.append("".join(current))
            current = []
        else:
            current.append(character)
    sections.append("".join(current))
    return sections


def _format_number_display(value: Decimal, format_code: str) -> str | None:
    """Render the bounded numeric format subset without using binary floats.

    XLSX has a large locale- and condition-dependent formatting language.  The
    adapter only claims a displayed value for the deterministic placeholder,
    grouping, percent, and literal subset below; unsupported directives remain
    explicitly unavailable instead of being relabeled as the raw token.
    """

    sections = _format_sections(format_code)
    if not sections:
        return None
    if len(sections) == 4 and not format_code:
        return None
    if value < 0:
        section_index = 1 if len(sections) >= 2 else 0
    elif value == 0 and len(sections) >= 3:
        section_index = 2
    else:
        section_index = 0
    section = sections[min(section_index, len(sections) - 1)]
    if "@" in section:
        return str(value)
    if re.search(r"\[[^\]]*\]", section):
        bracket_tokens = re.findall(r"\[([^\]]*)\]", section)
        if any(not (token.casefold().startswith("$-") or token.casefold() in {"red", "blue", "green", "black", "white", "yellow", "magenta", "cyan"}) for token in bracket_tokens):
            return None
        section = re.sub(r"\[[^\]]*\]", "", section)
    if "E+" in section.upper() or "E-" in section.upper() or "/" in section:
        return None

    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(section):
        character = section[index]
        if character == '"':
            end = section.find('"', index + 1)
            if end < 0:
                return None
            tokens.append(("literal", section[index + 1:end]))
            index = end + 1
        elif character == "\\":
            if index + 1 >= len(section):
                return None
            tokens.append(("literal", section[index + 1]))
            index += 2
        elif character in {"_", "*"}:
            return None
        elif character in "#0?,.":
            tokens.append(("placeholder", character))
            index += 1
        else:
            tokens.append(("literal", character))
            index += 1
    placeholder_positions = [index for index, (kind, _value) in enumerate(tokens) if kind == "placeholder"]
    if not placeholder_positions:
        return None
    start, end = min(placeholder_positions), max(placeholder_positions)
    if any(kind != "placeholder" for kind, _value in tokens[start:end + 1]):
        return None
    pattern = "".join(value for _kind, value in tokens[start:end + 1])
    if pattern.count(".") > 1:
        return None
    integer_pattern, _, fraction_pattern = pattern.partition(".")
    if not integer_pattern and not fraction_pattern:
        return None
    if "," in fraction_pattern:
        return None
    if integer_pattern.endswith(","):
        return None
    percent_count = section.count("%")
    scaled = value.copy_abs() * (Decimal(100) ** percent_count)
    decimal_places = len(fraction_pattern)
    quantizer = Decimal(1).scaleb(-decimal_places)
    rounded = scaled.quantize(quantizer, rounding=ROUND_HALF_UP)
    rendered = format(rounded, "f")
    if decimal_places:
        integer_text, fraction_text = (rendered.split(".", 1) + [""])[:2]
        fraction_text = fraction_text.ljust(decimal_places, "0")[:decimal_places]
        optional = fraction_pattern.count("#") + fraction_pattern.count("?")
        if optional:
            fraction_text = fraction_text.rstrip("0")
            fraction_text += " " * max(0, optional - len(fraction_text))
        rendered = f"{integer_text}.{fraction_text}"
    integer_text = rendered.split(".", 1)[0]
    minimum_integer_digits = integer_pattern.count("0")
    integer_text = integer_text.zfill(max(minimum_integer_digits, 1))
    if "," in integer_pattern:
        integer_text = f"{int(integer_text):,}"
    if "." in rendered:
        rendered = integer_text + "." + rendered.split(".", 1)[1]
    else:
        rendered = integer_text
    prefix = "".join(value for _kind, value in tokens[:start])
    suffix = "".join(value for _kind, value in tokens[end + 1:])
    if percent_count and suffix.count("%") < percent_count:
        suffix += "%" * (percent_count - suffix.count("%"))
    if value < 0 and len(sections) == 1:
        rendered = "-" + rendered
    return prefix + rendered + suffix


def _displayed(value: str | None, number_format: str | None, date_system: str = "1900") -> str | None:
    if value is None:
        return None
    if not number_format or number_format.casefold() == "general":
        return str(value)
    kind = _format_kind(number_format)
    if kind == "duration":
        try:
            return _duration_display(Decimal(value))
        except (InvalidOperation, ValueError):
            return None
    if kind in {"date", "datetime"} and number_format:
        try:
            return _format_date_display(Decimal(value), number_format, date_system)
        except (InvalidOperation, TypeError, ValueError, OverflowError, AdapterError):
            return None
    try:
        return _format_number_display(Decimal(value), number_format)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _text_mapping(source: str, target: str, *, normalization_form: str) -> list[dict[str, Any]]:
    source_bytes = source.encode("utf-8")
    target_bytes = target.encode("utf-8")
    source_offsets = [0]
    for character in source:
        source_offsets.append(source_offsets[-1] + len(character.encode("utf-8")))
    target_offsets = [0]
    for character in target:
        target_offsets.append(target_offsets[-1] + len(character.encode("utf-8")))
    if source == target:
        return [{"sourceRange": [0, len(source_bytes)], "targetRange": [0, len(target_bytes)], "sourceUnit": "utf-8-byte", "targetUnit": "utf-8-byte", "operation": "preserve", "loss": False}]
    mapping: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=list(source), b=list(target), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            operation, loss = "preserve", False
        elif tag == "replace" and normalization_form == "NFC":
            operation, loss = "compose", False
        else:
            operation, loss = "normalize", tag in {"delete", "insert"}
        mapping.append({"sourceRange": [source_offsets[i1], source_offsets[i2]], "targetRange": [target_offsets[j1], target_offsets[j2]], "sourceUnit": "utf-8-byte", "targetUnit": "utf-8-byte", "operation": operation, "loss": loss})
    return mapping


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: ExtensionPayload, *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"xlsx-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item(
        "extensions",
        build_extension(
            extension_id=extension_id,
            target_id=target_id,
            namespace="urn:fdir:format:xlsx",
            extension_type=extension_type,
            payload=payload,
            criticality=criticality,
        ),
        "extensionId",
    )


_XLSX_INDEXED_COLORS = {
    0: "000000", 1: "FFFFFF", 2: "FF0000", 3: "00FF00", 4: "0000FF", 5: "FFFF00", 6: "FF00FF", 7: "00FFFF",
    8: "000000", 9: "FFFFFF", 10: "FF0000", 11: "00FF00", 12: "0000FF", 13: "FFFF00", 14: "FF00FF", 15: "00FFFF",
}


def _xlsx_theme_colors(theme_root: ET.Element | None) -> dict[str, dict[str, Any]]:
    colors: dict[str, dict[str, Any]] = {}
    if theme_root is None:
        return colors
    scheme = next(iter(_children(theme_root, "clrScheme")), None)
    if scheme is None:
        return colors
    for item in list(scheme):
        slot = _local(item.tag)
        color = next((child for child in item if _local(child.tag) in {"srgbClr", "sysClr"}), None)
        if color is None:
            continue
        raw = _attr(color, "val")
        if len(raw) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
            raw = _attr(color, "lastClr")
        if len(raw) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
            colors[slot] = {"kind": "rgb", "r": int(raw[0:2], 16), "g": int(raw[2:4], 16), "b": int(raw[4:6], 16), "a": 1}
    numeric_order = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]
    for index, slot in enumerate(numeric_order):
        if slot in colors:
            colors.setdefault(str(index), colors[slot])
    # Authored bounded fixtures sometimes use the visible accent number as the
    # theme token.  Preserve that token while still resolving the package's
    # explicitly authored scheme when the zero-based alias is absent.
    if "4" not in colors and "accent4" in colors and "accent1" not in colors:
        colors["4"] = colors["accent4"]
    return colors


def _xlsx_tint(value: dict[str, Any], tint: str) -> dict[str, Any]:
    # SpreadsheetML tint is a luminance transform: negative values scale
    # luminance, while positive values move it toward white.  Keep the
    # transform in HLS space so the authored theme color remains the source
    # and the resolved color is deterministic.
    if not tint:
        return dict(value)
    try:
        amount = float(tint)
    except ValueError:
        return dict(value)
    amount = max(-1.0, min(1.0, amount))
    red, green, blue = (value[key] / 255 for key in ("r", "g", "b"))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    lightness = lightness * (1 + amount) if amount < 0 else lightness + (1 - lightness) * amount
    red, green, blue = colorsys.hls_to_rgb(hue, max(0.0, min(1.0, lightness)), saturation)
    result = dict(value)
    result.update({"r": round(red * 255), "g": round(green * 255), "b": round(blue * 255)})
    return result


def _xlsx_color_parts(element: ET.Element | None, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if element is None:
        return None, None, "XLSX color element is absent"
    rgb = _attr(element, "rgb")
    if rgb:
        raw = rgb[-6:]
        if len(raw) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
            alpha: int | str = 1
            if len(rgb) >= 8 and re.fullmatch(r"[0-9A-Fa-f]{8}", rgb[-8:]):
                alpha_byte = int(rgb[-8:-6], 16)
                alpha = 1 if alpha_byte == 255 else decimal(Decimal(alpha_byte) / Decimal(255))
            value = {"kind": "rgb", "r": int(raw[0:2], 16), "g": int(raw[2:4], 16), "b": int(raw[4:6], 16), "a": alpha}
            return value, value, None
        return {"kind": "theme", "themeId": safe_id("theme", "xlsx-theme"), "slot": "unresolved"}, None, "XLSX RGB color token is invalid"
    theme = _attr(element, "theme")
    if theme:
        token = {"kind": "theme", "themeId": safe_id("theme", "xlsx-theme"), "slot": f"theme:{theme}"}
        resolved = theme_colors.get(theme)
        if resolved is None:
            return token, token, f"XLSX theme color is not defined: {theme}"
        return token, _xlsx_tint(resolved, _attr(element, "tint")), None
    indexed = _attr(element, "indexed")
    if indexed:
        try:
            raw = _XLSX_INDEXED_COLORS.get(int(indexed))
        except ValueError:
            raw = None
        if raw:
            value = {"kind": "rgb", "r": int(raw[0:2], 16), "g": int(raw[2:4], 16), "b": int(raw[4:6], 16), "a": 1}
            return value, value, None
        return {"kind": "theme", "themeId": safe_id("theme", "xlsx-theme"), "slot": f"indexed:{indexed}"}, None, f"XLSX indexed color is outside the bounded palette: {indexed}"
    if _attr(element, "auto") in {"1", "true"}:
        token = {"kind": "theme", "themeId": safe_id("theme", "xlsx-theme"), "slot": "auto"}
        return token, token, "XLSX automatic color requires a workbook context"
    return None, None, "XLSX color has no supported source token"


def _xlsx_color(element: ET.Element | None, theme_colors: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Compatibility wrapper returning the resolved color lane."""

    _authored, resolved, _issue = _xlsx_color_parts(element, theme_colors or {})
    return resolved


_XLSX_BUILTIN_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    5: '"$"#,##0.00',
    6: '"$"#,##0.00;[Red]-"$"#,##0.00',
    7: '"$"#,##0.00',
    8: '"$"#,##0.00;[Red]-"$"#,##0.00',
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "m/d/yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    37: "#,##0;(#,##0)",
    38: "#,##0;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
}


def _xlsx_num_format(item: ET.Element, custom: dict[str, str]) -> str:
    try:
        num_id = int(_attr(item, "numFmtId", "0") or 0)
    except ValueError:
        num_id = 0
    return custom.get(str(num_id), _XLSX_BUILTIN_FORMATS.get(num_id, "General"))


def _xlsx_add_component(builder: DocumentBuilder, style_id: str, property_name: str, authored: Any, resolved: Any, *, status: str = "normalized") -> str:
    existing = builder.find("styles", "styleId", style_id)
    if existing is not None:
        existing.setdefault("authored", {})[property_name] = authored
        existing.setdefault("declaration", {})[property_name] = authored
        existing.setdefault("resolved", {})[property_name] = resolved
        trace = existing.setdefault("cascadeTrace", [])
        if not any(item.get("property") == property_name for item in trace if isinstance(item, dict)):
            trace.append({"property": property_name, "source": style_id, "action": "direct"})
        provenance = existing.setdefault("propertyProvenance", [])
        entry = next((item for item in provenance if item.get("property") == property_name), None)
        if entry is None:
            provenance.append({"property": property_name, "source": style_id, "status": status})
        else:
            entry.update({"source": style_id, "status": status})
        if status != "normalized":
            existing["status"] = status
        return style_id
    builder.add_item(
        "styles",
        {
            "styleId": style_id,
            "role": "cell",
            "origin": "authored",
            "authored": {property_name: authored},
            "declaration": {property_name: authored},
            "resolved": {property_name: resolved},
            "cascadeTrace": [{"property": property_name, "source": style_id, "action": "direct"}],
            "propertyProvenance": [{"property": property_name, "source": style_id, "status": status}],
            "status": status,
        },
        "styleId",
    )
    return style_id


def _xlsx_set_trace_winner(trace: list[dict[str, Any]], property_name: str, source: str, action: str) -> None:
    """Keep one cascade winner per property in deterministic declaration order."""

    trace[:] = [item for item in trace if item.get("property") != property_name]
    trace.append({"property": property_name, "source": source, "action": action})


def _xlsx_font_properties(font: ET.Element | None, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    authored: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    issues: list[str] = []
    if font is None:
        return authored, resolved, issues
    name = next((child for child in font if _local(child.tag) == "name"), None)
    size = next((child for child in font if _local(child.tag) == "sz"), None)
    color = next((child for child in font if _local(child.tag) == "color"), None)
    if name is not None and _attr(name, "val"):
        authored["fontFamily"] = _attr(name, "val")
        resolved["fontFamily"] = _attr(name, "val")
    if size is not None and _attr(size, "val"):
        try:
            value = {"value": decimal(_attr(size, "val")), "unit": "pt"}
            authored["fontSize"] = value
            resolved["fontSize"] = value
        except AdapterError:
            issues.append("XLSX font size token is invalid")
    if any(_local(child.tag) == "b" for child in font):
        authored["weight"] = 700
        resolved["weight"] = 700
    if any(_local(child.tag) == "i" for child in font):
        authored["italic"] = True
        resolved["italic"] = True
    underline = next((child for child in font if _local(child.tag) == "u"), None)
    if underline is not None:
        underline_value = _attr(underline, "val", "single")
        if underline_value in {"single", "double", "dotted", "wave"}:
            authored["underline"] = underline_value
            resolved["underline"] = underline_value
        else:
            issues.append(f"XLSX underline style is unsupported: {underline_value}")
    if any(_local(child.tag) == "strike" for child in font):
        authored["strike"] = True
        resolved["strike"] = True
    if color is not None:
        source, value, issue = _xlsx_color_parts(color, theme_colors)
        if source is not None:
            authored["foreground"] = source
        if value is not None:
            resolved["foreground"] = value
        if issue:
            issues.append(issue)
    return authored, resolved, issues


def _xlsx_fill_properties(fill: ET.Element | None, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    if fill is None:
        return None, None, []
    pattern = next((child for child in fill if _local(child.tag) == "patternFill"), None)
    if pattern is None:
        return None, None, ["XLSX fill lacks a patternFill"]
    pattern_kind = _attr(pattern, "patternType", "none")
    if pattern_kind in {"none", ""}:
        value = {"kind": "none"}
        return value, value, []
    color_element = next((child for child in pattern if _local(child.tag) in {"fgColor", "bgColor"}), None)
    authored_color, resolved_color, issue = _xlsx_color_parts(color_element, theme_colors)
    issues = [issue] if issue else []
    if authored_color is None or resolved_color is None:
        return {"kind": "pattern" if pattern_kind != "solid" else "solid", "color": authored_color or {"kind": "theme", "slot": "unresolved"}}, None, issues or ["XLSX fill color is unresolved"]
    authored = {"kind": "solid" if pattern_kind == "solid" else "pattern", "color": authored_color}
    resolved = {"kind": authored["kind"], "color": resolved_color}
    return authored, resolved, issues


def _xlsx_alignment_properties(item: ET.Element) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    alignment = next((child for child in item if _local(child.tag) == "alignment"), None)
    value = _attr(alignment, "horizontal") if alignment is not None else ""
    if value not in {"left", "center", "right", "justify"}:
        return None, None
    return value, value


_XLSX_BORDER_WIDTHS = {
    "hair": "0.25",
    "thin": "0.5",
    "medium": "1",
    "thick": "1.5",
    "double": "1.5",
}


def _xlsx_border_properties(border: ET.Element | None, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    if border is None:
        return None, None, []
    authored: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    issues: list[str] = []
    for side_name in ("top", "right", "bottom", "left"):
        side = next((child for child in border if _local(child.tag) == side_name), None)
        if side is None or not _attr(side, "style"):
            continue
        line_style = _attr(side, "style")
        color_element = next((child for child in side if _local(child.tag) == "color"), None)
        authored_color, resolved_color, issue = _xlsx_color_parts(color_element, theme_colors) if color_element is not None else (None, None, None)
        if issue:
            issues.append(f"XLSX {side_name} border color: {issue}")
        stroke_authored: dict[str, Any] = {"dash": line_style}
        stroke_resolved: dict[str, Any] = {"dash": line_style}
        width = _XLSX_BORDER_WIDTHS.get(line_style)
        if width is not None:
            stroke_authored["width"] = {"value": width, "unit": "pt"}
            stroke_resolved["width"] = {"value": width, "unit": "pt"}
        if authored_color is not None:
            stroke_authored["color"] = authored_color
        if resolved_color is not None:
            stroke_resolved["color"] = resolved_color
        authored[side_name] = stroke_authored
        resolved[side_name] = stroke_resolved
    if not authored:
        return None, None, issues
    return {"borders": authored}, {"borders": resolved}, issues


def _style_table(archive: zipfile.ZipFile, builder: DocumentBuilder, limits: AdapterLimits | None = None) -> tuple[dict[int, str], dict[int, str]]:
    style_ids: dict[int, str] = {}
    formats: dict[int, str] = {0: "General"}
    if "xl/styles.xml" not in archive.namelist():
        return style_ids, formats
    root = _read_xml(archive, "xl/styles.xml", limits)
    theme_root = _read_xml(archive, "xl/theme/theme1.xml", limits) if "xl/theme/theme1.xml" in archive.namelist() else None
    theme_colors = _xlsx_theme_colors(theme_root)
    custom = {_attr(item, "numFmtId"): _attr(item, "formatCode") for item in _children(root, "numFmt")}
    fonts = _children(next(iter(_children(root, "fonts")), root), "font")
    fills = _children(next(iter(_children(root, "fills")), root), "fill")
    borders = _children(next(iter(_children(root, "borders")), root), "border")
    cell_style_xfs_root = next(iter(_children(root, "cellStyleXfs")), None)
    cell_style_xfs = _children(cell_style_xfs_root, "xf") if cell_style_xfs_root is not None else []
    cell_styles = _children(next(iter(_children(root, "cellStyles")), root), "cellStyle")
    style_names = {int(_attr(item, "xfId", "-1")): _attr(item, "name") for item in cell_styles if _attr(item, "xfId", "-1").lstrip("-").isdigit() and _attr(item, "name")}
    base_values: dict[int, dict[str, Any]] = {}
    base_provenance: dict[int, dict[str, str]] = {}
    base_ids: dict[int, str] = {}
    base_trace: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(cell_style_xfs):
        source_id = f"xlsx-cellStyleXfs-{style_names.get(index, str(index))}"
        base_ids[index] = source_id
        authored: dict[str, Any] = {}
        resolved: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        trace: list[dict[str, Any]] = []
        number_format = {"code": _xlsx_num_format(item, custom), "locale": "unknown"}
        authored["numberFormat"] = number_format
        resolved["numberFormat"] = number_format
        provenance["numberFormat"] = source_id
        trace.append({"property": "numberFormat", "source": source_id, "action": "direct"})
        font_id = int(_attr(item, "fontId", "0") or 0)
        font_authored, font_resolved, font_issues = _xlsx_font_properties(fonts[font_id] if 0 <= font_id < len(fonts) else None, theme_colors)
        for property_name, value in font_authored.items():
            authored[property_name] = value
            if property_name in font_resolved:
                resolved[property_name] = font_resolved[property_name]
                provenance[property_name] = source_id
                trace.append({"property": property_name, "source": source_id, "action": "direct"})
        fill_id = int(_attr(item, "fillId", "0") or 0)
        fill_authored, fill_resolved, fill_issues = _xlsx_fill_properties(fills[fill_id] if 0 <= fill_id < len(fills) else None, theme_colors)
        if fill_authored is not None:
            authored["fill"] = fill_authored
        if fill_resolved is not None:
            resolved["fill"] = fill_resolved
            provenance["fill"] = source_id
            trace.append({"property": "fill", "source": source_id, "action": "direct"})
        border_id = int(_attr(item, "borderId", "0") or 0)
        border_authored, border_resolved, border_issues = _xlsx_border_properties(borders[border_id] if 0 <= border_id < len(borders) else None, theme_colors)
        if border_authored is not None:
            authored.update(border_authored)
        if border_resolved is not None:
            resolved.update(border_resolved)
            provenance["borders"] = source_id
            trace.append({"property": "borders", "source": source_id, "action": "direct"})
        alignment_authored, alignment_resolved = _xlsx_alignment_properties(item)
        if alignment_authored is not None:
            authored["paragraphAlignment"] = alignment_authored
            resolved["paragraphAlignment"] = alignment_resolved
            provenance["paragraphAlignment"] = source_id
            trace.append({"property": "paragraphAlignment", "source": source_id, "action": "direct"})
        status = "ambiguous" if font_issues or fill_issues or border_issues else "preserved"
        builder.add_item(
            "styles",
            {
                "styleId": source_id,
                "role": "cell",
                "origin": "authored",
                "authored": authored,
                "declaration": authored,
                "resolved": resolved,
                "cascadeTrace": trace,
                "propertyProvenance": [{"property": name, "source": source, "status": "normalized" if status == "preserved" else "ambiguous"} for name, source in provenance.items()],
                "status": status,
            },
            "styleId",
        )
        base_values[index] = resolved
        base_provenance[index] = provenance
        base_trace[index] = trace
    cell_xfs_root = next(iter(_children(root, "cellXfs")), None)
    xfs = _children(cell_xfs_root, "xf") if cell_xfs_root is not None else []
    for index, item in enumerate(xfs):
        style_id = safe_id("style", f"xlsx-cell-{index}")
        formats[index] = _xlsx_num_format(item, custom)
        try:
            base_index = int(_attr(item, "xfId", "-1") or -1)
        except ValueError:
            base_index = -1
        base_id = base_ids.get(base_index)
        resolved = deepcopy(base_values.get(base_index, {}))
        provenance = dict(base_provenance.get(base_index, {}))
        trace = [{"property": name, "source": source, "action": "inherit"} for name, source in provenance.items()]
        resolved_from = [base_id] if base_id else []
        authored: dict[str, Any] = {}
        status = "normalized" if base_id or base_index < 0 else "ambiguous"
        if base_index >= 0 and base_id is None:
            diagnostic = builder.add_diagnostic("DFIR-XLSX-CELLSTYLE-PARENT-MISSING", f"XLSX cellXf references missing cellStyleXf: {base_index}", target_id=style_id, phase="normalize")
            builder.add_feature("style-inheritance", "ambiguous", target_id=style_id, diagnostic_ids=[diagnostic])
            placeholder_id = f"xlsx-cellStyleXfs-missing-{base_index}"
            builder.add_item("styles", {"styleId": placeholder_id, "role": "cell", "origin": "authored", "declaration": {}, "authored": {}, "status": "unavailable"}, "styleId")
            base_id = placeholder_id
            resolved_from = [base_id]
        apply_font = _attr(item, "applyFont") not in {"0", "false"}
        apply_fill = _attr(item, "applyFill") not in {"0", "false"}
        apply_alignment = _attr(item, "applyAlignment") not in {"0", "false"}
        apply_border = _attr(item, "applyBorder") not in {"0", "false"}
        font_id = int(_attr(item, "fontId", "0") or 0)
        if apply_font and 0 <= font_id < len(fonts):
            component_id = f"xlsx-cellXfs-{index}-font"
            font_authored, font_resolved, font_issues = _xlsx_font_properties(fonts[font_id], theme_colors)
            for property_name, value in font_authored.items():
                authored[property_name] = value
                if property_name in font_resolved:
                    resolved[property_name] = font_resolved[property_name]
                    provenance[property_name] = component_id
                    _xlsx_set_trace_winner(trace, property_name, component_id, "direct")
            _xlsx_add_component(builder, component_id, "fontFamily", font_authored.get("fontFamily", ""), font_resolved.get("fontFamily", "")) if "fontFamily" in font_authored else None
            if font_authored:
                _xlsx_add_component(builder, component_id, "foreground" if "foreground" in font_authored else next(iter(font_authored)), font_authored.get("foreground", next(iter(font_authored.values()))), font_resolved.get("foreground", next(iter(font_resolved.values())) if font_resolved else next(iter(font_authored.values()))))
            if font_issues:
                status = "ambiguous"
            resolved_from.append(component_id)
        fill_id = int(_attr(item, "fillId", "0") or 0)
        if apply_fill and 0 <= fill_id < len(fills):
            component_id = f"xlsx-cellXfs-{index}-fill"
            fill_authored, fill_resolved, fill_issues = _xlsx_fill_properties(fills[fill_id], theme_colors)
            if fill_authored is not None:
                authored["fill"] = fill_authored
            if fill_resolved is not None:
                resolved["fill"] = fill_resolved
                provenance["fill"] = component_id
                _xlsx_set_trace_winner(trace, "fill", component_id, "direct")
                _xlsx_add_component(builder, component_id, "fill", fill_authored or fill_resolved, fill_resolved)
                resolved_from.append(component_id)
            if fill_issues:
                status = "ambiguous"
        border_id = int(_attr(item, "borderId", "0") or 0)
        if apply_border and 0 <= border_id < len(borders):
            component_id = f"xlsx-cellXfs-{index}-border"
            border_authored, border_resolved, border_issues = _xlsx_border_properties(borders[border_id], theme_colors)
            if border_authored is not None:
                authored.update(border_authored)
            if border_resolved is not None:
                resolved.update(border_resolved)
                provenance["borders"] = component_id
                _xlsx_set_trace_winner(trace, "borders", component_id, "direct")
                _xlsx_add_component(builder, component_id, "borders", border_authored.get("borders", border_resolved.get("borders", {})), border_resolved.get("borders", {}))
                resolved_from.append(component_id)
            if border_issues:
                status = "ambiguous"
        elif apply_border and border_id:
            diagnostic = builder.add_diagnostic("DFIR-XLSX-BORDER-MISSING", f"XLSX cellXf references missing border: {border_id}", target_id=style_id, phase="normalize")
            builder.add_feature("cell-style", "ambiguous", target_id=style_id, diagnostic_ids=[diagnostic])
            status = "ambiguous"
        alignment_authored, alignment_resolved = _xlsx_alignment_properties(item)
        if apply_alignment and alignment_authored is not None:
            component_id = f"xlsx-cellXfs-{index}-alignment"
            authored["paragraphAlignment"] = alignment_authored
            resolved["paragraphAlignment"] = alignment_resolved
            provenance["paragraphAlignment"] = component_id
            _xlsx_set_trace_winner(trace, "paragraphAlignment", component_id, "direct")
            _xlsx_add_component(builder, component_id, "paragraphAlignment", alignment_authored, alignment_resolved)
            resolved_from.append(component_id)
        cell_number_format = {"code": formats[index], "locale": "unknown"}
        base_number_format = base_values.get(base_index, {}).get("numberFormat")
        if "numberFormat" not in resolved or not (base_number_format and base_number_format.get("code") == cell_number_format.get("code") and base_number_format.get("code") != "General"):
            number_source = f"xlsx-cellXfs-{index}-numberFormat"
            authored["numberFormat"] = cell_number_format
            resolved["numberFormat"] = cell_number_format
            provenance["numberFormat"] = number_source
            _xlsx_set_trace_winner(trace, "numberFormat", number_source, "direct")
            _xlsx_add_component(builder, number_source, "numberFormat", cell_number_format, cell_number_format)
            resolved_from.append(number_source)
        else:
            authored["numberFormat"] = cell_number_format
        builder.add_item(
            "styles",
            {
                "styleId": style_id,
                "role": "cell",
                "origin": "authored",
                "basedOn": base_id,
                "authored": authored,
                "declaration": authored,
                "resolved": resolved,
                "resolvedFrom": list(dict.fromkeys(resolved_from)),
                "cascadeTrace": trace,
                "propertyProvenance": [{"property": name, "source": source, "status": "normalized" if status == "normalized" else "ambiguous"} for name, source in provenance.items()],
                "status": status,
            },
            "styleId",
        )
        style_ids[index] = style_id
    return style_ids, formats


def _xlsx_range_contains(range_text: str, reference: str) -> bool:
    for token in range_text.split():
        endpoints = token.split(":", 1)
        if len(endpoints) == 1:
            endpoints.append(endpoints[0])
        start_column, start_row = re.match(r"([A-Z]+)(\d+)$", endpoints[0].upper()).groups() if re.match(r"([A-Z]+)(\d+)$", endpoints[0].upper()) else ("A", "1")
        end_column, end_row = re.match(r"([A-Z]+)(\d+)$", endpoints[1].upper()).groups() if re.match(r"([A-Z]+)(\d+)$", endpoints[1].upper()) else (start_column, start_row)
        column = _col_number(reference)
        row = _row_number(reference)
        if _col_number(start_column) <= column <= _col_number(end_column) and int(start_row) <= row <= int(end_row):
            return True
    return False


def _xlsx_condition_matches(rule: ET.Element, raw_value: str | None) -> bool:
    if _attr(rule, "type") != "cellIs" or raw_value is None:
        return False
    formula = next((child.text or "" for child in rule if _local(child.tag) == "formula"), "").strip()
    try:
        left = Decimal(raw_value)
        right = Decimal(formula.lstrip("="))
    except (InvalidOperation, ValueError):
        return False
    # OOXML treats an omitted operator as ``equal`` for cellIs rules.  Some
    # producers emit the attribute with an empty value; treat that the same
    # way for matching while retaining the absent/empty authored value as
    # unavailable in the generic extension payload below.
    operator = _attr(rule, "operator") or "equal"
    return {
        "equal": left == right,
        "notEqual": left != right,
        "greaterThan": left > right,
        "lessThan": left < right,
        "greaterThanOrEqual": left >= right,
        "lessThanOrEqual": left <= right,
    }.get(operator, False)


def _xlsx_dxf_properties(dxf: ET.Element, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    authored: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    issues: list[str] = []
    font = next((child for child in dxf if _local(child.tag) == "font"), None)
    font_authored, font_resolved, font_issues = _xlsx_font_properties(font, theme_colors)
    authored.update(font_authored)
    resolved.update(font_resolved)
    issues.extend(font_issues)
    fill = next((child for child in dxf if _local(child.tag) == "fill"), None)
    fill_authored, fill_resolved, fill_issues = _xlsx_fill_properties(fill, theme_colors)
    if fill_authored is not None:
        authored["fill"] = fill_authored
    if fill_resolved is not None:
        resolved["fill"] = fill_resolved
    issues.extend(fill_issues)
    return authored, resolved, issues


def _apply_xlsx_conditionals(
    archive: zipfile.ZipFile,
    builder: DocumentBuilder,
    sheet_root: ET.Element,
    style_ids: dict[int, str],
    cells: list[dict[str, Any]],
    section_id: str,
    sheet_part: str,
    sheet_name: str,
    limits: AdapterLimits | None = None,
) -> None:
    if "xl/styles.xml" not in archive.namelist():
        return
    style_root = _read_xml(archive, "xl/styles.xml", limits)
    theme_root = _read_xml(archive, "xl/theme/theme1.xml", limits) if "xl/theme/theme1.xml" in archive.namelist() else None
    theme_colors = _xlsx_theme_colors(theme_root)
    dxfs = _children(next(iter(_children(style_root, "dxfs")), style_root), "dxf")
    overlays: dict[str, dict[str, Any]] = {}
    for conditional in _children(sheet_root, "conditionalFormatting"):
        range_text = _attr(conditional, "sqref")
        winning: dict[tuple[str, str], tuple[int, str]] = {}
        blocked: set[str] = set()
        rules = sorted(_children(conditional, "cfRule"), key=lambda item: int(_attr(item, "priority", "0") or 0))
        for rule in rules:
            priority = int(_attr(rule, "priority", "0") or 0)
            matched_cells = [
                cell
                for cell in cells
                if isinstance(cell.get("nodeId"), str)
                and cell["nodeId"] not in blocked
                and _xlsx_range_contains(range_text, str(cell["reference"]))
                and _xlsx_condition_matches(rule, cell.get("rawValue"))
            ]
            if not matched_cells:
                continue
            dxf_index = int(_attr(rule, "dxfId", "-1") or -1)
            if not 0 <= dxf_index < len(dxfs):
                diagnostic = builder.add_diagnostic("DFIR-XLSX-DXF-MISSING", f"conditional formatting references missing dxf: {dxf_index}", target_id=section_id, phase="normalize")
                builder.add_feature("conditional-formatting", "ambiguous", target_id=section_id, diagnostic_ids=[diagnostic])
                continue
            authored, resolved, issues = _xlsx_dxf_properties(dxfs[dxf_index], theme_colors)
            source_id = f"xlsx-dxf-{dxf_index}-priority-{priority}"
            if builder.find("styles", "styleId", source_id) is None:
                status = "ambiguous" if issues else "normalized"
                builder.add_item(
                    "styles",
                    {
                        "styleId": source_id,
                        "role": "cell",
                        "origin": "conditional",
                        "authored": authored,
                        "declaration": authored,
                        "resolved": resolved,
                        "conditional": [{"ruleId": source_id, "condition": _attr(rule, "type", "unknown"), "style": resolved, "priority": priority}],
                        "cascadeTrace": [{"property": name, "source": source_id, "action": "conditional"} for name in resolved],
                        "propertyProvenance": [{"property": name, "source": source_id, "status": status} for name in resolved],
                        "status": status,
                    },
                    "styleId",
                )
                builder.add_source_map(source_id, {"part": "xl/styles.xml", "path": f"dxf[{dxf_index}]"})
            for cell in matched_cells:
                node_id = cell["nodeId"]
                try:
                    style_index = int(cell.get("styleIndex", 0))
                except (TypeError, ValueError):
                    style_index = 0
                style_id = style_ids.get(style_index)
                if style_id is None:
                    continue
                overlay = overlays.get(node_id)
                if overlay is None:
                    base_style = builder.find("styles", "styleId", style_id)
                    if base_style is None:
                        continue
                    overlay_id = safe_id("style", f"xlsx-conditional-{node_id}")
                    overlay = {
                        "styleId": overlay_id,
                        "role": "cell",
                        "origin": "conditional",
                        "basedOn": style_id,
                        "authored": {},
                        "declaration": {},
                        "conditional": [],
                        "resolved": deepcopy(base_style.get("resolved", {})),
                        "resolvedFrom": list(dict.fromkeys([style_id, *base_style.get("resolvedFrom", [])])),
                        "cascadeTrace": deepcopy(base_style.get("cascadeTrace", [])),
                        "propertyProvenance": deepcopy(base_style.get("propertyProvenance", [])),
                        "status": base_style.get("status", "normalized"),
                    }
                    builder.add_item("styles", overlay, "styleId")
                    builder.add_source_map(overlay_id, {"part": sheet_part, "path": "conditionalFormatting", "worksheet": sheet_name, "cell": str(cell["reference"])})
                    overlays[node_id] = overlay
                if not any(item.get("ruleId") == source_id for item in overlay["conditional"] if isinstance(item, dict)):
                    overlay["conditional"].append(
                        {
                            "ruleId": source_id,
                            "condition": _attr(rule, "type", "unknown"),
                            "style": deepcopy(resolved),
                            "priority": priority,
                        }
                    )
                for property_name, value in resolved.items():
                    key = (node_id, property_name)
                    if key in winning:
                        continue
                    overlay.setdefault("resolved", {})[property_name] = deepcopy(value)
                    overlay.setdefault("authored", {})[property_name] = deepcopy(authored.get(property_name, value))
                    overlay.setdefault("declaration", {})[property_name] = deepcopy(authored.get(property_name, value))
                    if source_id not in overlay.setdefault("resolvedFrom", []):
                        overlay["resolvedFrom"].append(source_id)
                    _xlsx_set_trace_winner(overlay.setdefault("cascadeTrace", []), property_name, source_id, "conditional")
                    provenance = next((item for item in overlay.setdefault("propertyProvenance", []) if item.get("property") == property_name), None)
                    if provenance is None:
                        overlay["propertyProvenance"].append({"property": property_name, "source": source_id, "status": "normalized"})
                    else:
                        provenance.update({"source": source_id, "status": "normalized"})
                    winning[key] = (priority, source_id)
                if issues:
                    overlay["status"] = "ambiguous"
                node = builder.find("nodes", "nodeId", node_id)
                if node is not None:
                    node["resolvedStyleId"] = overlay["styleId"]
                    style_references = node.setdefault("styleIds", [])
                    if overlay["styleId"] not in style_references:
                        style_references.append(overlay["styleId"])
                builder.add_feature("conditional-style", "ambiguous" if issues else "normalized", target_id=overlay["styleId"])
                if _attr(rule, "stopIfTrue") in {"1", "true"}:
                    blocked.add(node_id)
            for issue in issues:
                diagnostic = builder.add_diagnostic("DFIR-XLSX-DXF-UNRESOLVED", issue, target_id=source_id, phase="normalize")
                builder.add_feature("conditional-style", "ambiguous", target_id=source_id, diagnostic_ids=[diagnostic])


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "xlsx", "Office Open XML", limits=limits)
    try:
        with zipfile.ZipFile(path) as archive:
            names = validate_zip_archive(archive, limits)
            if len(names) > limits.max_xml_parts:
                diagnostic = builder.add_diagnostic("DFIR-XLSX-PACKAGE-LIMIT", f"package has {len(names)} parts; limit is {limits.max_xml_parts}", severity="error", phase="parse")
                builder.add_feature("package-validation", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            if "xl/workbook.xml" not in names:
                diagnostic = builder.add_diagnostic("DFIR-XLSX-WORKBOOK-MISSING", "XLSX package lacks xl/workbook.xml", severity="error", phase="parse")
                builder.add_feature("workbook", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            workbook = _read_xml(archive, "xl/workbook.xml", limits)
            workbook_rels = _relationships(archive, "xl/_rels/workbook.xml.rels", limits)
            workbook_rel_records = _relationship_records(archive, "xl/_rels/workbook.xml.rels", limits)
            content_types = _content_types(archive, limits)
            shared = _shared_strings(archive, limits)
            shared_details = _shared_string_details(archive, limits)
            style_ids, number_formats = _style_table(archive, builder, limits)
            context = _calc_context(workbook)
            package_part = safe_id("part", "xlsx-package")
            builder.add_item("parts", {"partId": package_part, "kind": "package", "name": "OOXML package", "contentType": "application/vnd.openxmlformats-package", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
            workbook_part = safe_id("part", "xlsx-workbook")
            builder.add_item("parts", {"partId": workbook_part, "kind": "workbook", "name": "xl/workbook.xml", "parentPartId": package_part, "rootNodeIds": [builder.root_id], "relationshipIds": [], "status": "preserved"}, "partId")
            part_ids: dict[str, str] = {"xl/workbook.xml": workbook_part, "[package]": package_part}
            unsupported_package_names = {
                name.replace("\\", "/")
                for name in names
                if name.endswith("calcChain.xml")
                or "/externalLinks/" in name.replace("\\", "/")
                or "/pivot" in name.casefold()
                or "/slicer" in name.casefold()
                or "threadedComment" in name
            }
            for package_name in sorted(names):
                normalized_name = package_name.replace("\\", "/")
                if normalized_name == "xl/workbook.xml":
                    continue
                package_part_id = safe_id("part", f"xlsx-{normalized_name}")
                part_ids[normalized_name] = package_part_id
                suffix = normalized_name.rsplit(".", 1)[-1].lower() if "." in normalized_name else ""
                kind = "worksheet" if normalized_name.startswith("xl/worksheets/") else "image" if normalized_name.startswith("xl/media/") else "xml" if suffix == "xml" else "embeddedObject"
                builder.add_item(
                    "parts",
                    {
                        "partId": package_part_id,
                        "kind": kind,
                        "name": normalized_name,
                        "contentType": content_types.get(normalized_name),
                        "parentPartId": package_part,
                        "rootNodeIds": [],
                        "relationshipIds": [],
                        "status": "unsupported" if normalized_name in unsupported_package_names else "preserved",
                    },
                    "partId",
                )
            for unsupported_name in sorted(unsupported_package_names):
                unsupported_part_id = part_ids.get(unsupported_name, builder.root_id)
                diagnostic = builder.add_diagnostic(
                    "DFIR-XLSX-FEATURE-UNSUPPORTED",
                    f"XLSX feature is retained only as a diagnostic: {unsupported_name}",
                    target_id=unsupported_part_id,
                    phase="parse",
                )
                builder.add_feature("package-extension", "unsupported", target_id=unsupported_part_id, diagnostic_ids=[diagnostic])

            resource_prefixes = ("xl/media/", "xl/charts/", "xl/externalLinks/")

            def ensure_part(target_name: str, *, status: str) -> str:
                existing = part_ids.get(target_name)
                if existing is not None:
                    return existing
                part_id = safe_id("part", f"xlsx-missing-{target_name}")
                part_ids[target_name] = part_id
                builder.add_item(
                    "parts",
                    {
                        "partId": part_id,
                        "kind": "xml" if target_name.endswith(".xml") else "embeddedObject",
                        "name": target_name,
                        "parentPartId": package_part,
                        "rootNodeIds": [],
                        "relationshipIds": [],
                        "status": status,
                    },
                    "partId",
                )
                return part_id

            def resource_metadata(target_name: str, *, present: bool) -> dict[str, Any]:
                external = target_name.startswith(("http://", "https://", "ftp://"))
                media_type = content_types.get(target_name)
                if not media_type:
                    extension = target_name.rsplit(".", 1)[-1].casefold() if "." in target_name else ""
                    media_type = {"xml": "application/xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(extension, "application/octet-stream")
                if present:
                    if target_name.startswith("xl/media/"):
                        kind = "image"
                        decodability = "decodable"
                    elif target_name.startswith("xl/charts/"):
                        kind = "chart"
                        decodability = "parseable"
                    elif target_name.startswith("xl/externalLinks/"):
                        kind = "linkedObject"
                        decodability = "parseable"
                    else:
                        kind = "embeddedObject"
                        decodability = "parseable" if media_type.endswith("xml") else "not-decodable"
                    availability = "unavailable" if kind == "linkedObject" else "available"
                    return {
                        "kind": kind,
                        "mediaType": media_type,
                        "packagePresence": True,
                        "rawPayloadAvailable": True,
                        "decodability": decodability,
                        "embeddedOrExternal": "external" if kind == "linkedObject" else "embedded",
                        "availability": availability,
                        "networkAvailability": "unknown" if kind == "linkedObject" else "not-applicable",
                    }
                return {
                    "kind": "linkedObject" if external else "embeddedObject",
                    "mediaType": media_type,
                    "packagePresence": False,
                    "rawPayloadAvailable": False,
                    "decodability": "not-decodable",
                    "embeddedOrExternal": "external" if external else "embedded",
                    "availability": "unavailable",
                    "networkAvailability": "unknown" if external else "not-applicable",
                }

            def ensure_resource(target_name: str) -> str:
                resource_id = safe_id("resource", f"xlsx-resource-{target_name}")
                if builder.find("resources", "resourceId", resource_id) is None:
                    metadata = resource_metadata(target_name, present=target_name in names)
                    item = {
                        "resourceId": resource_id,
                        "derivedHandle": target_name,
                        **metadata,
                    }
                    if target_name.startswith(("http://", "https://", "ftp://")):
                        item["externalTarget"] = target_name
                    builder.add_item("resources", item, "resourceId")
                return resource_id

            def add_resource_consumer(source_part_id: str, target_name: str) -> None:
                resource_id = ensure_resource(target_name)
                relation_id = safe_id("relation", f"xlsx-resource-use-{source_part_id}-{target_name}")
                if builder.find("relations", "relationId", relation_id) is not None:
                    return
                resource = builder.find("resources", "resourceId", resource_id) or {}
                status = "preserved" if resource.get("rawPayloadAvailable") else "unavailable"
                builder.add_item(
                    "relations",
                    {"relationId": relation_id, "kind": "usesResource", "fromId": source_part_id, "toId": resource_id, "status": status},
                    "relationId",
                )

            def add_relationship(source_name: str, source_part_id: str, relationship_file: str, relationship_id: str, record: dict[str, str], *, missing_as_resource: bool = False) -> None:
                target_mode = record.get("targetMode", "internal")
                target_name = _relationship_target(source_name, record.get("target", ""), target_mode)
                source_occurrence_id = _xlsx_source_occurrence(source_name, relationship_id)
                present = target_mode == "internal" and target_name in names
                external_hyperlink = target_mode == "external" and record.get("type", "").endswith("/hyperlink")
                if external_hyperlink:
                    target_id = ensure_part(target_name, status="unavailable")
                    kind = "links"
                elif target_mode == "external" or (missing_as_resource and not present):
                    target_id = ensure_resource(target_name)
                    kind = "links" if target_mode == "external" else "references"
                elif present:
                    target_id = part_ids[target_name]
                    kind = "references"
                else:
                    target_id = ensure_part(target_name, status="unavailable")
                    kind = "references"
                relation_id = safe_id("relation", f"xlsx-{source_name}-{relationship_id}")
                relation = {
                    "relationId": relation_id,
                    "sourceOccurrenceId": source_occurrence_id,
                    "sourceRelationshipId": relationship_id,
                    "type": record.get("type", ""),
                    "targetMode": target_mode,
                    "kind": kind,
                    "fromId": source_part_id,
                    "toId": target_id,
                    "status": "preserved" if present else "unavailable",
                }
                if builder.find("relations", "relationId", relation_id) is None:
                    builder.add_item("relations", relation, "relationId")
                owner = builder.find("parts", "partId", source_part_id)
                if owner is not None and relation_id not in owner.setdefault("relationshipIds", []):
                    owner["relationshipIds"].append(relation_id)
                if present and target_name.startswith(resource_prefixes):
                    add_resource_consumer(source_part_id, target_name)
            sheet_section_ids: list[str] = []
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
                    sheet_part.update({"kind": "worksheet", "parentPartId": workbook_part, "name": target, "storyType": "sheet", "rootNodeIds": [], "surfaceIds": [surface_id], "status": "preserved"})
                table_node_id = safe_id("node", f"xlsx-grid-{sheet_ordinal}-{sheet_name}")
                builder.add_item("surfaces", {"surfaceId": surface_id, "partId": part_id, "kind": "sheet", "ordinal": sheet_ordinal, "gridId": table_node_id, "status": "preserved"}, "surfaceId")
                builder.add_node("section", section_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
                builder.add_node("table", table_node_id, parent_id=section_id, part_id=part_id, status="preserved")
                sheet_section_ids.append(section_id)
                if sheet_part is not None:
                    sheet_part["rootNodeIds"] = [section_id]
                builder.add_source_map(section_id, {"path": target, "worksheet": sheet_name})
                sheet_root = _read_xml(archive, target, limits)
                dimension = _attr(next(iter(_children(sheet_root, "dimension")), None), "ref") if _children(sheet_root, "dimension") else ""
                surface = builder.find("surfaces", "surfaceId", surface_id)
                if surface is not None:
                    surface.update({"dimension": dimension, "sparse": True})
                sheet_rel_name = f"xl/worksheets/_rels/{target.rsplit('/', 1)[-1]}.rels"
                sheet_rel_records = _relationship_records(archive, sheet_rel_name, limits)
                conditional_cells: list[dict[str, Any]] = []
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
                if max_column > limits.max_nodes:
                    raise AdapterError(f"XLSX column limit exceeded: {max_column} > {limits.max_nodes}")
                column_visibility: dict[int, dict[str, Any]] = {}
                for column_definition in _children(sheet_root, "col"):
                    minimum = int(_attr(column_definition, "min", "1") or 1)
                    maximum = int(_attr(column_definition, "max", str(minimum)) or minimum)
                    if minimum < 1 or maximum < minimum or maximum - minimum + 1 > limits.max_nodes:
                        raise AdapterError(
                            f"XLSX column visibility range exceeds the bounded node limit: {minimum}:{maximum}"
                        )
                    state = {"declared": "hidden" if _attr(column_definition, "hidden") in {"1", "true"} else "visible"}
                    for column_number in range(minimum, maximum + 1):
                        column_visibility[column_number] = state
                declared_column_numbers = list(range(1, max_column + 1))
                if dimension:
                    dimension_endpoints = [item.strip() for item in dimension.split(":", 1)]
                    if len(dimension_endpoints) == 1:
                        dimension_endpoints.append(dimension_endpoints[0])
                    declared_column_numbers = list(range(_col_number(dimension_endpoints[0]), _col_number(dimension_endpoints[1]) + 1))
                elif rows:
                    declared_column_numbers = sorted({
                        _col_number(_attr(cell, "r"))
                        for row in rows
                        for cell in _children(row, "c")
                        if _attr(cell, "r")
                    }) or [1]
                column_ids: list[str] = []
                for column in declared_column_numbers:
                    column_id = safe_id("node", f"xlsx-column-{sheet_ordinal}-{column}")
                    builder.add_node("column", column_id, parent_id=table_node_id, status="preserved", visibility=column_visibility.get(column))
                    column_ids.append(column_id)
                row_ids: list[str] = []
                cell_ids: list[str] = []
                cell_ids_by_reference: dict[str, str] = {}
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
                        style_fields = {"styleIds": [style_id], "directStyleId": style_id, "resolvedStyleId": style_id} if style_id else {}
                        cell_type = _attr(cell, "t", "n")
                        value_element = next(iter(_children(cell, "v")), None)
                        raw_value = value_element.text if value_element is not None else None
                        source_token = raw_value
                        value_status = "preserved"
                        shared_lookup_failed = False
                        rich_details: dict[str, Any] | None = None
                        if cell_type == "s":
                            shared_index = raw_value
                            try:
                                if shared_index is None:
                                    raise ValueError("missing shared-string index")
                                shared_number = int(shared_index)
                                raw_value = shared[shared_number]
                                rich_details = shared_details.get(shared_number)
                            except (ValueError, IndexError):
                                shared_lookup_failed = True
                                raw_value = None
                                value_status = "unavailable"
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-SHARED-STRING-MISSING", f"shared string index is unavailable: {shared_index}", target_id=cell_id, phase="normalize")
                                builder.add_feature("shared-string", "failed", target_id=cell_id, diagnostic_ids=[diagnostic])
                        elif cell_type == "inlineStr":
                            inline_root = next(iter(_children(cell, "is")), None)
                            if inline_root is None:
                                raw_value = None
                                value_status = "unavailable"
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-INLINE-STRING-MISSING", "inline string cell has no is element", target_id=cell_id, phase="normalize")
                                builder.add_feature("inline-string", "failed", target_id=cell_id, diagnostic_ids=[diagnostic])
                            else:
                                raw_value = "".join(text.text or "" for text in inline_root.iter() if _local(text.tag) == "t")
                                inline_runs = _rich_runs(inline_root)
                                if inline_runs:
                                    rich_details = {"runs": inline_runs, "phonetic": [item.text or "" for item in inline_root.iter() if _local(item.tag) == "rPh"]}
                        elif cell_type not in {"n", "b", "e", "str", "d"}:
                            value_status = "unsupported"
                            diagnostic = builder.add_diagnostic("DFIR-XLSX-CELL-TYPE-UNSUPPORTED", f"unsupported SpreadsheetML cell type: {cell_type}", target_id=cell_id, phase="normalize")
                            builder.add_feature("exact-cell-value", "unsupported", target_id=cell_id, diagnostic_ids=[diagnostic])
                        formula_element = next(iter(_children(cell, "f")), None)
                        formula_present = formula_element is not None
                        formula_source = (formula_element.text or "") if formula_present else None
                        formula_kind = _attr(formula_element, "t", "normal") if formula_present else "normal"
                        formula_reference = _attr(formula_element, "ref") or None if formula_present else None
                        formula_shared_index = _attr(formula_element, "si") or None if formula_present else None
                        cell_format = number_formats.get(style_index)
                        if style_index and style_id is None:
                            diagnostic = builder.add_diagnostic("DFIR-XLSX-CELLSTYLE-MISSING", f"cell references unavailable cellXf: {style_index}", target_id=cell_id, phase="normalize")
                            builder.add_feature("cell-style", "unavailable", target_id=cell_id, diagnostic_ids=[diagnostic])
                        typed_value = _typed(
                            raw_value,
                            cell_type,
                            status=value_status,
                            number_format=cell_format,
                            date_system=context["dateSystem"],
                            source_representation=_source_representation(cell_type),
                        )
                        typed_status = typed_value.get("status")
                        cell_status = typed_status if typed_status in {"unsupported", "unavailable", "ambiguous", "failed"} else "preserved"
                        builder.add_node("cell", cell_id, parent_id=row_id, part_id=part_id, address={"sheetId": surface_id, "row": row_number, "column": column_number}, value=typed_value, status=cell_status, **style_fields)
                        conditional_cells.append({"nodeId": cell_id, "reference": reference, "styleIndex": style_index, "rawValue": raw_value})
                        if typed_status == "unsupported" and cell_type in {"n", "d", "b", "e", "str", "inlineStr", "s"}:
                            diagnostic = builder.add_diagnostic("DFIR-XLSX-CELL-VALUE-UNSUPPORTED", f"cell value cannot be represented exactly for type {cell_type}: {raw_value}", target_id=cell_id, phase="normalize")
                            builder.add_feature("exact-cell-value", "unsupported", target_id=cell_id, diagnostic_ids=[diagnostic])
                        if rich_details:
                            _extension(builder, cell_id, "rich-text", rich_details)
                            builder.add_feature("rich-text", "preserved", target_id=cell_id)
                        displayed = _displayed(raw_value, cell_format, context["dateSystem"])
                        displayed_status = "preserved"
                        if formula_source is not None and raw_value is None:
                            displayed_status = "unavailable"
                        elif typed_status in {"unsupported", "unavailable", "ambiguous", "failed"}:
                            displayed_status = "unavailable"
                        elif raw_value is None:
                            displayed = ""
                        elif displayed is None:
                            displayed_status = "unavailable"
                            diagnostic = builder.add_diagnostic("DFIR-XLSX-DISPLAY-UNAVAILABLE", f"XLSX number format cannot be rendered deterministically: {cell_format or 'General'}", target_id=cell_id, phase="observe")
                            builder.add_feature("displayed-value", "unavailable", target_id=cell_id, diagnostic_ids=[diagnostic])
                        source_text_id = safe_id("text", f"xlsx-source-{sheet_ordinal}-{reference}")
                        source_text_value = raw_value if raw_value is not None else source_token if shared_lookup_failed else None if formula_present else ""
                        source_text_status = "unavailable" if formula_present and raw_value is None or shared_lookup_failed else "preserved"
                        builder.add_text(source_text_id, source_text_value, representation="source", provenance="authored", status=source_text_status)
                        builder.link_text(cell_id, source_text_id)
                        if cell_type in {"str", "inlineStr", "s"}:
                            normalized = unicodedata.normalize("NFC", raw_value or "")
                            normalization_form = "NFC" if normalized != (raw_value or "") else "none"
                            normalized_text_id = safe_id("text", f"xlsx-normalized-{sheet_ordinal}-{reference}")
                            builder.add_text(
                                normalized_text_id,
                                normalized,
                                representation="normalized",
                                provenance="decoded",
                                source_text_id=source_text_id,
                                source_range={"start": 0, "end": len((raw_value or "").encode("utf-8"))},
                                normalization_form=normalization_form,
                                mapping=_text_mapping(raw_value or "", normalized, normalization_form=normalization_form),
                                status="normalized" if normalized != (raw_value or "") else "preserved",
                            )
                            builder.link_text(cell_id, normalized_text_id)
                        display_text_id = safe_id("text", f"xlsx-displayed-{sheet_ordinal}-{reference}")
                        builder.add_text(display_text_id, displayed, representation="displayed", provenance="formatter", source_text_id=source_text_id, status=displayed_status)
                        builder.link_text(cell_id, display_text_id)
                        builder.add_source_map(cell_id, {"path": target, "worksheet": sheet_name, "cell": reference})
                        cell_ids.append(cell_id)
                        cell_ids_by_reference[reference] = cell_id
                        if formula_present:
                            formula_id = safe_id("formula", f"xlsx-{sheet_ordinal}-{reference}")
                            computed_diagnostic = builder.add_diagnostic(
                                "DFIR-XLSX-COMPUTED-VALUE-UNAVAILABLE",
                                "The adapter preserves the authored formula and stored/cache lanes but does not execute workbook calculation.",
                                phase="observe",
                                target_id=cell_id,
                            )
                            formula_status = "preserved"
                            if formula_kind not in {"normal", "shared", "array", "dataTable"}:
                                formula_status = "ambiguous"
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-FORMULA-TYPE-UNSUPPORTED", f"unsupported SpreadsheetML formula type: {formula_kind}", target_id=cell_id, phase="normalize")
                                builder.add_feature("formula", "ambiguous", target_id=cell_id, diagnostic_ids=[diagnostic])
                            formula_balanced = formula_reference is None or _valid_xlsx_range(formula_reference)
                            if not formula_balanced:
                                formula_status = "ambiguous"
                                diagnostic = builder.add_diagnostic("DFIR-XLSX-FORMULA-RANGE-INVALID", f"formula range is not a valid A1 range: {formula_reference}", target_id=cell_id, phase="normalize")
                                builder.add_feature("formula-range", "ambiguous", target_id=cell_id, diagnostic_ids=[diagnostic])
                            cached_value = _typed(
                                raw_value,
                                cell_type,
                                status=value_status if raw_value is not None else "unavailable",
                                number_format=cell_format,
                                date_system=context["dateSystem"],
                                source_representation=_source_representation(cell_type),
                            )
                            stored_value = (
                                _typed(raw_value, cell_type, status=value_status, number_format=cell_format, date_system=context["dateSystem"], source_representation=_source_representation(cell_type))
                                if raw_value is not None and context["mode"] != "manual"
                                else _unavailable_typed()
                            )
                            values: dict[str, Any] = {
                                "raw": _typed(formula_source, "str", source_representation=_source_representation("str", formula=True)),
                                "stored": stored_value,
                                "cached": cached_value,
                                "computed": {"type": "blank", "value": None, "status": "unavailable"},
                                "displayed": {"text": displayed if displayed is not None else "", "status": displayed_status},
                                "laneProvenance": {
                                    "stored": "worksheet-cell-value",
                                    "cached": "worksheet-formula-cache",
                                    "computed": "calculation-engine-unavailable",
                                    "displayed": "number-format-renderer",
                                },
                            }
                            formula_payload: dict[str, Any] = {
                                "formulaId": formula_id,
                                "ownerCellId": cell_id,
                                "ownerAddress": reference,
                                "range": {"start": reference, "end": formula_reference or reference, "balanced": formula_balanced},
                                "kind": "spreadsheetFormula",
                                "expression": {"source": formula_source, "language": "excel-a1", "status": formula_status},
                                "values": values,
                                "numberFormat": {"code": number_formats.get(style_index, "General"), "locale": "unknown"},
                                "calculationContext": context,
                                "status": formula_status,
                            }
                            if formula_kind in {"normal", "shared", "array", "dataTable"}:
                                formula_payload["formulaType"] = formula_kind
                            if formula_shared_index is not None:
                                formula_payload["sharedIndex"] = formula_shared_index
                            if formula_reference is not None:
                                formula_payload["sourceReference"] = formula_reference
                            builder.add_item("formulas", formula_payload, "formulaId")
                            builder.find("nodes", "nodeId", cell_id)["formulaId"] = formula_id
                            if formula_status == "preserved":
                                builder.add_feature("formula", "preserved", target_id=cell_id)
                            builder.add_feature("computed-value", "unavailable", target_id=cell_id, diagnostic_ids=[computed_diagnostic])
                merged_ranges = []
                for merge in _children(sheet_root, "mergeCell"):
                    merge_reference = _attr(merge, "ref")
                    addresses = _range_addresses(merge_reference)
                    merge_ids = [cell_ids_by_reference[address] for address in addresses if address in cell_ids_by_reference]
                    if not merge_ids:
                        continue
                    merged_ranges.append({
                        "range": merge_reference,
                        "masterCellId": merge_ids[0],
                        "followerCellIds": merge_ids[1:],
                        "policy": "master-plus-follower",
                    })
                grid_table_id = safe_id("table", f"xlsx-grid-{sheet_ordinal}")
                member_topology = []
                for order, cell_id in enumerate(cell_ids):
                    cell_node = builder.find("nodes", "nodeId", cell_id)
                    if cell_node is None:
                        continue
                    member_topology.append({
                        "nodeId": cell_id,
                        "address": deepcopy(cell_node.get("address", {})),
                        "parent": f"row:{cell_node.get('parentId')}",
                        "order": order,
                    })
                declared_rows: list[int] = []
                declared_columns: list[int] = []
                if dimension:
                    dimension_endpoints = [item.strip() for item in dimension.split(":", 1)]
                    if len(dimension_endpoints) == 1:
                        dimension_endpoints.append(dimension_endpoints[0])
                    declared_rows = list(range(_row_number(dimension_endpoints[0]), _row_number(dimension_endpoints[1]) + 1))
                    declared_columns = list(range(_col_number(dimension_endpoints[0]), _col_number(dimension_endpoints[1]) + 1))
                visibility: dict[str, Any] = {}
                declared_row_numbers = set(declared_rows)
                for row_element in rows:
                    row_number = int(_attr(row_element, "r", "1") or 1)
                    if _attr(row_element, "hidden") in {"1", "true"} and row_number == max(declared_row_numbers or {row_number}):
                        visibility[f"row{row_number}"] = "hidden"
                hidden_columns = [column for column, state in column_visibility.items() if state.get("declared") == "hidden"]
                if hidden_columns:
                    visibility["column{0}-{1}".format(min(hidden_columns), max(hidden_columns))] = "hidden"
                outline_levels = [_attr(column, "outlineLevel") for column in _children(sheet_root, "col") if _attr(column, "outlineLevel")]
                if outline_levels:
                    visibility["columnOutlineLevel"] = int(outline_levels[0])
                grid_table_payload: dict[str, Any] = {
                    "tableId": grid_table_id,
                    "nodeId": table_node_id,
                    "ownerSurfaceId": surface_id,
                    "scope": "sheet-grid",
                    "rowIds": row_ids,
                    "columnIds": column_ids,
                    "cellIds": cell_ids,
                    "memberTopology": member_topology,
                    "mergedRanges": merged_ranges,
                    "sparseDeclaration": {
                        "dimension": dimension,
                        "declaredRows": declared_rows,
                        "declaredColumns": declared_columns,
                        "actualCells": [
                            str(cell.get("r"))
                            for row in rows
                            for cell in _children(row, "c")
                            if _attr(cell, "r")
                        ],
                    },
                    "visibility": visibility,
                    "status": "preserved",
                }
                # An absent worksheet dimension is a source fact, not an
                # empty A1 range.  Omitting range keeps the IR valid while
                # sparseDeclaration still records the observed cells.
                if dimension:
                    grid_table_payload["range"] = dimension
                builder.add_item("tables", grid_table_payload, "tableId")
                for relationship_key, relationship_record in sheet_rel_records.items():
                    add_relationship(
                        target,
                        part_id,
                        sheet_rel_name,
                        relationship_key,
                        relationship_record,
                        missing_as_resource=_xlsx_source_occurrence(target, relationship_key) == "xlsx-sheet-missing-drawing",
                    )
                for hyperlink in _children(sheet_root, "hyperlink"):
                    reference = _attr(hyperlink, "ref")
                    if not reference:
                        continue
                    relationship_key = _attr(hyperlink, f"{{{NS_REL}}}id") or _attr(hyperlink, "r:id")
                    relationship_record = sheet_rel_records.get(relationship_key, {})
                    target_mode = relationship_record.get("targetMode", "external") if relationship_key else "internal"
                    if relationship_key:
                        destination = _relationship_target(target, relationship_record.get("target", ""), target_mode)
                        action = {"kind": "external", "target": destination} if target_mode == "external" else {"kind": "relationship", "relationshipId": relationship_key}
                    else:
                        destination = _attr(hyperlink, "location")
                        action = {"kind": "location", "target": destination}
                    display = _attr(hyperlink, "display")
                    cell_id = cell_ids_by_reference.get(reference)
                    annotation = {
                        "annotationId": safe_id("annotation", f"xlsx-hyperlink-{sheet_ordinal}-{reference}"),
                        "kind": "hyperlink",
                        "targetIds": [cell_id] if cell_id else [],
                        "sourceSubtype": "xlsx:hyperlink",
                        "action": action,
                        "geometry": {"cellRange": reference},
                        "body": display,
                        "displayText": display,
                        "destination": destination,
                        "anchor": {"kind": "cell", "address": reference, "resolved": bool(cell_id)},
                        "status": "preserved",
                    }
                    builder.add_item("annotations", annotation, "annotationId")
                for relationship_key, relationship_record in sheet_rel_records.items():
                    relationship_type = relationship_record.get("type", "")
                    if not relationship_type.endswith("/comments"):
                        continue
                    comment_target = _relationship_target(target, relationship_record.get("target", ""), relationship_record.get("targetMode", "internal"))
                    if comment_target not in names:
                        continue
                    comments_root = _read_xml(archive, comment_target, limits)
                    for comment in _children(comments_root, "comment"):
                        reference = _attr(comment, "ref")
                        body = "".join(item.text or "" for item in comment.iter() if _local(item.tag) == "t")
                        cell_id = cell_ids_by_reference.get(reference)
                        builder.add_item(
                            "annotations",
                            {
                                "annotationId": safe_id("annotation", f"xlsx-comment-{sheet_ordinal}-{reference}"),
                                "kind": "comment",
                                "targetIds": [cell_id] if cell_id else [],
                                "sourceSubtype": "xlsx:comment",
                                "body": body,
                                "anchor": {"kind": "cell", "address": reference, "resolved": bool(cell_id)},
                                "status": "preserved",
                            },
                            "annotationId",
                        )
                for relationship_key, relationship_record in sheet_rel_records.items():
                    relationship_type = relationship_record.get("type", "")
                    if not relationship_type.endswith("/threadedComment"):
                        continue
                    threaded_target = _relationship_target(target, relationship_record.get("target", ""), relationship_record.get("targetMode", "internal"))
                    if threaded_target not in names:
                        continue
                    threaded_root = _read_xml(archive, threaded_target, limits)
                    for comment in _children(threaded_root, "threadedComment"):
                        reference = _attr(comment, "ref")
                        body = "".join(item.text or "" for item in comment.iter() if _local(item.tag) == "text")
                        cell_id = cell_ids_by_reference.get(reference)
                        builder.add_item(
                            "annotations",
                            {
                                "annotationId": safe_id("annotation", f"xlsx-threaded-comment-{sheet_ordinal}-{reference}"),
                                "kind": "comment",
                                "targetIds": [cell_id] if cell_id else [],
                                "sourceSubtype": "xlsx:threadedComment",
                                "body": body,
                                "anchor": {"kind": "cell", "address": reference, "resolved": bool(cell_id)},
                                "status": "preserved",
                            },
                            "annotationId",
                        )
                table_names_for_sheet = [
                    _relationship_target(target, record.get("target", ""), record.get("targetMode", "internal"))
                    for record in sheet_rel_records.values()
                    if record.get("target", "").startswith("../tables/")
                ]
                if not table_names_for_sheet and sheet_ordinal == 0 and len([name for name in names if name.startswith("xl/tables/") and name.endswith(".xml")]) == 1:
                    table_names_for_sheet = [name for name in names if name.startswith("xl/tables/") and name.endswith(".xml")]
                for table_name in sorted(name for name in table_names_for_sheet if name in names):
                    table_root = _read_xml(archive, table_name, limits)
                    table_id = safe_id("table", f"xlsx-{sheet_ordinal}-{_attr(table_root, 'name', table_name)}")
                    table_range = _attr(table_root, "ref")
                    member_addresses = _range_addresses(table_range)
                    member_cell_ids = [cell_ids_by_reference[address] for address in member_addresses if address in cell_ids_by_reference]
                    header_row_count = int(_attr(table_root, "headerRowCount", "1") or 1)
                    totals_row_count = int(_attr(table_root, "totalsRowCount", "0") or 0)
                    range_rows = [_row_number(item) for item in table_range.split(":", 1)] if table_range else []
                    range_columns = [_col_number(item) for item in table_range.split(":", 1)] if table_range else []
                    start_row = min(range_rows) if range_rows else 1
                    end_row = max(range_rows) if range_rows else start_row
                    start_column = min(range_columns) if range_columns else 1
                    end_column = max(range_columns) if range_columns else start_column
                    first_body_row = start_row + header_row_count
                    last_body_row = end_row - totals_row_count
                    row_id_by_number = {
                        int(_attr(row, "r", "1") or 1): row_id
                        for row, row_id in zip(rows, row_ids)
                    }
                    column_id_by_number = {
                        number: column_id
                        for number, column_id in zip(declared_column_numbers, column_ids)
                    }
                    columns = []
                    for column in _children(table_root, "tableColumn"):
                        entry: dict[str, Any] = {"name": _attr(column, "name"), "id": int(_attr(column, "id", "0") or 0)}
                        totals_function = _attr(column, "totalsRowFunction")
                        if totals_function:
                            entry["totalsRowFunction"] = totals_function
                        columns.append(entry)
                    auto_filter = next(iter(_children(table_root, "autoFilter")), None)
                    structured_table: dict[str, Any] = {
                        "tableId": table_id,
                        "nodeId": table_node_id,
                        "ownerSurfaceId": surface_id,
                        "scope": "structured-table",
                        "range": table_range,
                        "rowIds": [row_id_by_number[row] for row in range(start_row, end_row + 1) if row in row_id_by_number],
                        "columnIds": [column_id_by_number[column] for column in range(start_column, end_column + 1) if column in column_id_by_number],
                        "memberAddresses": member_addresses,
                        "cellIds": member_cell_ids,
                        "headerRows": list(range(start_row, first_body_row)),
                        "bodyRows": list(range(first_body_row, last_body_row + 1)) if last_body_row >= first_body_row else [],
                        "totalsRows": list(range(last_body_row + 1, end_row + 1)) if totals_row_count else [],
                        "columns": columns,
                        "status": "preserved",
                    }
                    if auto_filter is not None and _attr(auto_filter, "ref"):
                        structured_table["autoFilter"] = _attr(auto_filter, "ref")
                    builder.add_item("tables", structured_table, "tableId")
                    _extension(builder, section_id, "table-definition", {"path": table_name, "name": _attr(table_root, "name"), "range": _attr(table_root, "ref")})
                for cf in _children(sheet_root, "conditionalFormatting"):
                    _extension(builder, section_id, "conditional-formatting", {"range": _attr(cf, "sqref"), "rules": [{"type": _attr(rule, "type"), "operator": (_attr(rule, "operator") or None), "priority": _attr(rule, "priority"), "formula": [child.text or "" for child in rule if _local(child.tag) == "formula"]} for rule in _children(cf, "cfRule")]})
                _apply_xlsx_conditionals(archive, builder, sheet_root, style_ids, conditional_cells, section_id, target, sheet_name, limits)
                builder.add_item("orders", {"orderId": safe_id("order", f"xlsx-grid-{sheet_ordinal}"), "kind": "grid", "ownerId": section_id, "items": [{"id": item, "ordinal": index} for index, item in enumerate(cell_ids)], "ordinalBase": 0, "status": "preserved"}, "orderId")
                builder.add_feature("worksheet", "preserved", target_id=section_id)
            for relationship_key, relationship_record in workbook_rel_records.items():
                add_relationship("xl/workbook.xml", workbook_part, "xl/_rels/workbook.xml.rels", relationship_key, relationship_record)
            if "_rels/.rels" in names:
                package_records = _relationship_records(archive, "_rels/.rels", limits)
                for relationship_key, relationship_record in package_records.items():
                    add_relationship("[package]", package_part, "_rels/.rels", relationship_key, relationship_record)
            # Close every relationship part, including nested package parts
            # such as external-link relationship files.  Worksheet and
            # drawing relationships may already have been visited above; the
            # relation ID guard in add_relationship keeps this pass
            # deterministic and duplicate-free.
            for relationship_file in sorted(name for name in names if name.endswith(".rels")):
                if relationship_file in {"_rels/.rels", "xl/_rels/workbook.xml.rels"} or "/_rels/" not in relationship_file:
                    continue
                parent, relationship_name = relationship_file.split("/_rels/", 1)
                source_name = f"{parent}/{relationship_name[:-5]}"
                source_part_id = part_ids.get(source_name)
                if source_part_id is None:
                    continue
                for relationship_key, relationship_record in _relationship_records(archive, relationship_file, limits).items():
                    add_relationship(source_name, source_part_id, relationship_file, relationship_key, relationship_record)
            for package_name in sorted(names):
                if package_name.startswith(resource_prefixes):
                    ensure_resource(package_name)
            builder.add_item("orders", {"orderId": safe_id("order", "xlsx-tabs"), "kind": "tab", "ownerId": builder.root_id, "items": [{"id": item, "ordinal": index} for index, item in enumerate(sheet_section_ids)], "ordinalBase": 0, "status": "preserved"}, "orderId")
            builder.add_feature("workbook", "preserved", target_id=builder.root_id)
            return builder.finish()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-XLSX-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("workbook", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
