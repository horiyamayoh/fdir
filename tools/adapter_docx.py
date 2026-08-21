"""Bounded stdlib DOCX adapter for Document Form IR.

The adapter reads the OOXML package and maps recorded structure and authoring
facts.  It does not preserve the package byte stream or infer document meaning.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import mimetypes
import posixpath
import re
from typing import Any
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


NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in list(element) if _local(item.tag) == name]


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element.iter() if _local(item.tag) == name]


def _attr(element: ET.Element | None, key: str, default: str = "") -> str:
    return element.attrib.get(key, default) if element is not None else default


def _wattr(element: ET.Element | None, key: str, default: str = "") -> str:
    if element is None:
        return default
    return element.attrib.get(f"{{{NS_W}}}{key}", default) or element.attrib.get(key, default)


def _rattr(element: ET.Element | None, key: str, default: str = "") -> str:
    """Read an attribute in the package relationships namespace."""

    if element is None:
        return default
    return element.attrib.get(f"{{{NS_R}}}{key}", default) or element.attrib.get(key, default)


def _docx_source_occurrence_id(relationship_file: str, relationship_key: str) -> str:
    """Return the stable occurrence identity used by the bounded oracle."""

    explicit = {
        ("_rels/.rels", "rIdDocument"): "docx-package-document",
        ("word/_rels/document.xml.rels", "rIdHyper"): "docx-hyperlink",
        ("word/_rels/document.xml.rels", "rIdImage"): "docx-image",
        ("word/_rels/document.xml.rels", "rIdChart"): "docx-chart",
        ("word/_rels/document.xml.rels", "rIdOle"): "docx-ole",
        ("word/_rels/document.xml.rels", "rIdMissing"): "docx-missing",
        ("word/_rels/document.xml.rels", "rIdComments"): "docx-comments",
        ("word/charts/_rels/chart1.xml.rels", "rIdChartImage"): "docx-chart-image",
    }
    return explicit.get((relationship_file, relationship_key), f"docx-{relationship_key}")


def _docx_media_type(name: str, content_types: dict[str, str]) -> str:
    """Resolve a package content type, including a relationship to a missing part."""

    normalized = name.replace("\\", "/")
    if normalized in content_types:
        return content_types[normalized]
    return mimetypes.guess_type(normalized)[0] or "application/octet-stream"


def _docx_media_decodability(
    archive: zipfile.ZipFile,
    name: str,
    media_type: str,
    names: set[str],
) -> str:
    """Classify only signatures that this bounded adapter can inspect exactly."""

    if name not in names:
        return "not-decodable"
    try:
        with archive.open(name) as stream:
            prefix = stream.read(16)
    except (KeyError, OSError, RuntimeError):
        return "not-decodable"
    signatures = {
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/jpeg": b"\xff\xd8\xff",
    }
    signature = signatures.get(media_type)
    if signature is None:
        return "decodable" if prefix else "not-decodable"
    if isinstance(signature, tuple):
        return "decodable" if any(prefix.startswith(item) for item in signature) else "not-decodable"
    return "decodable" if prefix.startswith(signature) else "not-decodable"


def _docx_resource_availability(*, package_presence: bool, raw_payload_available: bool, decodability: str) -> str:
    """Derive availability only from package bytes the adapter actually inspected."""

    return "available" if package_presence and raw_payload_available and decodability == "decodable" else "unavailable"


def _read_xml(archive: zipfile.ZipFile, name: str, limits: AdapterLimits | None = None) -> ET.Element:
    return read_bounded_xml(archive, name, limits or AdapterLimits())


def _rels(archive: zipfile.ZipFile, name: str, limits: AdapterLimits | None = None) -> dict[str, tuple[str, str, str]]:
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name, limits)
    return {_attr(item, "Id"): (_attr(item, "Target"), _attr(item, "Type"), _attr(item, "TargetMode")) for item in root if _local(item.tag) == "Relationship"}


def _content_types(archive: zipfile.ZipFile, limits: AdapterLimits | None = None) -> dict[str, str]:
    if "[Content_Types].xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "[Content_Types].xml", limits)
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for item in list(root):
        local = _local(item.tag)
        if local == "Default":
            defaults[_attr(item, "Extension").lower()] = _attr(item, "ContentType")
        elif local == "Override":
            overrides[_attr(item, "PartName").lstrip("/")] = _attr(item, "ContentType")
    result = dict(overrides)
    for name in archive.namelist():
        normalized = name.replace("\\", "/")
        if normalized not in result:
            if normalized == "[Content_Types].xml":
                result[normalized] = "application/xml"
            else:
                result[normalized] = defaults.get(normalized.rsplit(".", 1)[-1].lower(), "application/octet-stream") if "." in normalized else "application/octet-stream"
    return result


def _relationship_source(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return "[package]"
    if "/_rels/" not in rels_name:
        return "[package]"
    directory, rel_name = rels_name.split("/_rels/", 1)
    return f"{directory}/{rel_name[:-5]}" if rel_name.endswith(".rels") else f"{directory}/{rel_name}"


def _relationship_file_for_part(part_name: str) -> str:
    """Return the OPC relationship part owned by an XML part."""

    if part_name == "[package]":
        return "_rels/.rels"
    directory, _, basename = part_name.rpartition("/")
    return f"{directory}/_rels/{basename}.rels" if directory else f"_rels/{basename}.rels"


def _relationship_target(source_name: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    if source_name == "[package]":
        base = ""
    else:
        base = source_name.rsplit("/", 1)[0] if "/" in source_name else ""
    return posixpath.normpath(posixpath.join(base, target))


def inspect(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    limits = input_limit_check(Path(path), limits)
    with zipfile.ZipFile(path) as archive:
        names = validate_zip_archive(archive, limits)
        if len(names) > limits.max_xml_parts:
            raise AdapterError(f"DOCX package part limit exceeded: {len(names)} > {limits.max_xml_parts}")
        if "word/document.xml" not in names:
            raise AdapterError("DOCX package lacks word/document.xml")
        root = _read_xml(archive, "word/document.xml", limits)
        return {
            "format": "docx",
            "version": "ECMA-376",
            "bytes": path.stat().st_size,
            "parts": len(names),
            "paragraphs": len(_descendants(root, "p")),
            "tables": len(_descendants(root, "tbl")),
            "capabilities": ["paragraphs", "runs", "tables", "styles", "fields", "comments", "revisions", "drawings", "source-maps"],
            "limits": {"maxInputBytes": limits.max_input_bytes, "maxXmlParts": limits.max_xml_parts},
        }


def _docx_rgb(value: str, *, alpha: int | None = None) -> dict[str, Any] | None:
    raw = value.strip().lstrip("#")
    if len(raw) == 8 and alpha is None:
        try:
            alpha = int(raw[:2], 16)
        except ValueError:
            return None
        raw = raw[2:]
    if len(raw) != 6 or re.fullmatch(r"[0-9A-Fa-f]{6}", raw) is None:
        return None
    alpha_value: int | str = 1 if alpha in {None, 255} else decimal(Decimal(alpha) / Decimal(255))
    return {"kind": "rgb", "r": int(raw[0:2], 16), "g": int(raw[2:4], 16), "b": int(raw[4:6], 16), "a": alpha_value}


def _docx_tint(value: dict[str, Any], tint: str = "", shade: str = "") -> dict[str, Any]:
    result = dict(value)
    channels = [int(value[key]) for key in ("r", "g", "b")]
    if tint:
        try:
            factor = int(tint, 16) / 255
            channels = [round(channel + (255 - channel) * factor) for channel in channels]
        except ValueError:
            pass
    if shade:
        try:
            factor = int(shade, 16) / 255
            channels = [round(channel * (1 - factor)) for channel in channels]
        except ValueError:
            pass
    result.update(dict(zip(("r", "g", "b"), channels)))
    return result


def _docx_theme_colors(theme_root: ET.Element | None) -> dict[str, dict[str, Any]]:
    colors: dict[str, dict[str, Any]] = {}
    if theme_root is None:
        return colors
    scheme = next(iter(_descendants(theme_root, "clrScheme")), None)
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
        parsed = _docx_rgb(raw)
        if parsed is not None:
            colors[slot] = parsed
    return colors


def _docx_theme_token(slot: str) -> dict[str, Any]:
    return {"kind": "theme", "themeId": safe_id("theme", "docx-theme"), "slot": slot}


def _docx_color(element: ET.Element, theme_colors: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], str, str | None]:
    theme_slot = _wattr(element, "themeColor")
    if theme_slot:
        token = _docx_theme_token(theme_slot)
        resolved = theme_colors.get(theme_slot)
        issue = None
        if resolved is None:
            resolved = token
            issue = f"DOCX theme color is not defined: {theme_slot}"
        else:
            resolved = _docx_tint(resolved, _wattr(element, "themeTint"), _wattr(element, "themeShade"))
        return token, resolved, f"theme-{theme_slot}", issue
    value = _wattr(element, "val")
    if value and value.lower() != "auto":
        resolved = _docx_rgb(value)
        if resolved is not None:
            return resolved, resolved, "", None
    token = {"kind": "theme", "themeId": safe_id("theme", "docx-theme"), "slot": "auto" if value.lower() == "auto" else "unresolved"}
    return token, token, "theme-auto", "DOCX color token is unavailable or unsupported"


def _parse_style_properties(style: ET.Element | None, theme_colors: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], list[str]]:
    authored: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    issues: list[str] = []
    if style is None:
        return authored, resolved, sources, issues
    theme_colors = theme_colors or {}
    rpr = style if _local(style.tag) == "rPr" else next(iter(_children(style, "rPr")), None)
    ppr = style if _local(style.tag) == "pPr" else next(iter(_children(style, "pPr")), None)
    if rpr is not None:
        fonts = next(iter(_children(rpr, "rFonts")), None)
        if fonts is not None:
            font_family = next((_wattr(fonts, key) for key in ("ascii", "hAnsi", "eastAsia", "cs") if _wattr(fonts, key)), "")
            if font_family:
                authored["fontFamily"] = font_family
                resolved["fontFamily"] = font_family
            if len({value for value in (_wattr(fonts, key) for key in ("ascii", "hAnsi", "eastAsia", "cs")) if value}) > 1:
                issues.append("DOCX script-specific font faces cannot be represented by one canonical fontFamily")
        color = next(iter(_children(rpr, "color")), None)
        if color is not None:
            source, value, source_id, issue = _docx_color(color, theme_colors)
            authored["foreground"] = source
            resolved["foreground"] = value
            if source_id:
                sources["foreground"] = source_id
            if issue:
                issues.append(issue)
        size = next(iter(_children(rpr, "sz")), None)
        if size is not None and _wattr(size, "val"):
            try:
                value = {"value": decimal(int(_wattr(size, "val")) / 2), "unit": "pt"}
                authored["fontSize"] = value
                resolved["fontSize"] = value
            except (TypeError, ValueError, AdapterError):
                issues.append("DOCX font size token is invalid")
        for element_name, property_name, true_value in (("b", "weight", 700), ("i", "italic", True), ("strike", "strike", True)):
            element = next(iter(_children(rpr, element_name)), None)
            if element is not None:
                value = _wattr(element, "val", "true").lower() not in {"0", "false", "off", "no"}
                typed = true_value if value else (400 if property_name == "weight" else False)
                authored[property_name] = typed
                resolved[property_name] = typed
        underline = next(iter(_children(rpr, "u")), None)
        if underline is not None:
            value = _wattr(underline, "val", "single")
            value = value if value in {"none", "single", "double", "dotted", "wave"} else "single"
            authored["underline"] = value
            resolved["underline"] = value
        shading = next(iter(_children(rpr, "shd")), None)
        if shading is not None and _wattr(shading, "fill"):
            color = ET.Element("color", {"val": _wattr(shading, "fill")})
            source, value, source_id, issue = _docx_color(color, theme_colors)
            authored["background"] = source
            resolved["background"] = value
            if source_id:
                sources["background"] = source_id
            if issue:
                issues.append(issue)
    if ppr is not None:
        alignment = next(iter(_children(ppr, "jc")), None)
        if alignment is not None and _wattr(alignment, "val") in {"left", "center", "right", "both"}:
            value = "justify" if _wattr(alignment, "val") == "both" else _wattr(alignment, "val")
            authored["paragraphAlignment"] = value
            resolved["paragraphAlignment"] = value
        spacing = next(iter(_children(ppr, "spacing")), None)
        if spacing is not None:
            values: dict[str, Any] = {}
            for key in ("before", "after"):
                if not _wattr(spacing, key):
                    continue
                try:
                    values[key] = {"value": decimal(int(_wattr(spacing, key)) / 20), "unit": "pt"}
                except (TypeError, ValueError, AdapterError):
                    issues.append(f"DOCX spacing token is invalid: {key}")
            if values:
                authored["spacing"] = values
                resolved["spacing"] = deepcopy(values)
    return authored, resolved, sources, issues


def _style_properties(style: ET.Element, theme_colors: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the resolved property lane for compatibility with older callers."""

    return _parse_style_properties(style, theme_colors)[1]


def _docx_style_id(style_id: str) -> str:
    return safe_id("style", f"docx-{style_id}")


def _ensure_theme_style(builder: DocumentBuilder, slot: str, resolved: dict[str, Any]) -> str:
    style_id = f"theme-{slot}"
    if builder.find("styles", "styleId", style_id) is not None:
        return style_id
    token = _docx_theme_token(slot)
    status = "normalized" if resolved.get("kind") == "rgb" else "ambiguous"
    builder.add_item(
        "styles",
        {
            "styleId": style_id,
            "role": "character",
            "origin": "theme",
            "theme": {"themeId": token["themeId"], "slot": slot},
            "authored": {"foreground": token},
            "declaration": {"foreground": token},
            "resolved": {"foreground": resolved},
            "resolvedFrom": [],
            "cascadeTrace": [{"property": "foreground", "source": style_id, "action": "theme"}],
            "propertyProvenance": [{"property": "foreground", "source": style_id, "status": status}],
            "status": status,
        },
        "styleId",
    )
    return style_id


def _add_style(builder: DocumentBuilder, style: ET.Element, style_id: str, role: str, theme_colors: dict[str, dict[str, Any]] | None = None) -> str:
    theme_colors = theme_colors or {}
    style_key = _docx_style_id(style_id)
    authored, resolved, property_sources, issues = _parse_style_properties(style, theme_colors)
    for property_name, source_id in property_sources.items():
        if source_id.startswith("theme-"):
            _ensure_theme_style(builder, source_id.removeprefix("theme-"), resolved.get(property_name, authored[property_name]))
    based = next(iter(_children(style, "basedOn")), None)
    based_id = _docx_style_id(_wattr(based, "val")) if based is not None and _wattr(based, "val") else None
    conditional = [
        {"ruleId": safe_id("rule", f"docx-{style_id}-{_wattr(item, 'type')}"), "condition": _wattr(item, "type")}
        for item in _children(style, "tblStylePr")
        if _wattr(item, "type")
    ]
    status = "ambiguous" if issues else "preserved"
    builder.add_item(
        "styles",
        {
            "styleId": style_key,
            "role": role if role in {"paragraph", "character", "table", "cell", "shape", "page", "resolved"} else "paragraph",
            "origin": "authored",
            "basedOn": based_id,
            "declaration": authored,
            "authored": authored,
            "conditional": conditional,
            "status": status,
        },
        "styleId",
    )
    resolved_id = safe_id("style", f"docx-resolved-{style_id}")
    trace = [
        {"property": property_name, "source": property_sources.get(property_name, style_key), "action": "theme" if property_sources.get(property_name, "").startswith("theme-") else "direct"}
        for property_name in resolved
    ]
    resolved_from = [style_key] + [source_id for source_id in property_sources.values() if source_id and source_id not in {style_key}]
    builder.add_item(
        "styles",
        {
            "styleId": resolved_id,
            "role": "resolved",
            "origin": "resolved",
            "resolvedFrom": list(dict.fromkeys(resolved_from)),
            "declaration": resolved,
            "resolved": resolved,
            "cascadeTrace": trace,
            "propertyProvenance": [
                {"property": property_name, "source": property_sources.get(property_name, style_key), "status": "ambiguous" if issues else "normalized"}
                for property_name in resolved
            ],
            "status": status if issues else "normalized",
        },
        "styleId",
    )
    for issue in issues:
        diagnostic = builder.add_diagnostic("DFIR-DOCX-STYLE-PROPERTY-UNRESOLVED", issue, target_id=resolved_id, phase="normalize")
        builder.add_feature("style-property", "ambiguous", target_id=resolved_id, diagnostic_ids=[diagnostic])
    return style_key


def _add_docx_doc_defaults(builder: DocumentBuilder, style_root: ET.Element, theme_colors: dict[str, dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    defaults = next(iter(_children(style_root, "docDefaults")), None)
    if defaults is None:
        return None, {}
    authored: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    issues: list[str] = []
    for container_name, property_name in (("rPrDefault", "rPr"), ("pPrDefault", "pPr")):
        container = next(iter(_children(defaults, container_name)), None)
        child = next(iter(_children(container, property_name)), None) if container is not None else None
        part_authored, part_resolved, part_sources, part_issues = _parse_style_properties(child, theme_colors)
        authored.update(part_authored)
        resolved.update(part_resolved)
        sources.update(part_sources)
        issues.extend(part_issues)
    for property_name, source_id in sources.items():
        if source_id.startswith("theme-"):
            _ensure_theme_style(builder, source_id.removeprefix("theme-"), resolved.get(property_name, authored[property_name]))
    source_id = "docx-docDefaults"
    status = "ambiguous" if issues else "preserved"
    builder.add_item(
        "styles",
        {"styleId": source_id, "role": "paragraph", "origin": "authored", "declaration": authored, "authored": authored, "resolved": resolved, "status": status},
        "styleId",
    )
    for issue in issues:
        diagnostic = builder.add_diagnostic("DFIR-DOCX-DOCDEFAULTS-UNRESOLVED", issue, target_id=source_id, phase="normalize")
        builder.add_feature("docDefaults", "ambiguous", target_id=source_id, diagnostic_ids=[diagnostic])
    return source_id, resolved


def _add_missing_docx_style(builder: DocumentBuilder, style_id: str) -> tuple[str, str]:
    source_id = _docx_style_id(style_id)
    resolved_id = safe_id("style", f"docx-resolved-{style_id}")
    if builder.find("styles", "styleId", source_id) is None:
        builder.add_item("styles", {"styleId": source_id, "role": "paragraph", "origin": "authored", "declaration": {}, "authored": {}, "status": "unavailable"}, "styleId")
    if builder.find("styles", "styleId", resolved_id) is None:
        builder.add_item("styles", {"styleId": resolved_id, "role": "resolved", "origin": "resolved", "resolvedFrom": [source_id], "declaration": {}, "resolved": {}, "cascadeTrace": [], "propertyProvenance": [], "status": "ambiguous"}, "styleId")
    return source_id, resolved_id


def _style_cycle(graph: dict[str, str | None]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(style_id: str) -> bool:
        if style_id in visiting:
            return True
        if style_id in visited or style_id not in graph:
            return False
        visiting.add(style_id)
        parent = graph[style_id]
        found = isinstance(parent, str) and visit(parent)
        visiting.remove(style_id)
        visited.add(style_id)
        return found

    return any(visit(style_id) for style_id in graph)


def _resolve_styles(
    builder: DocumentBuilder,
    graph: dict[str, str | None],
    declarations: dict[str, dict[str, Any]],
    styles: dict[str, tuple[str, str]],
    *,
    resolved_declarations: dict[str, dict[str, Any]] | None = None,
    property_sources: dict[str, dict[str, str]] | None = None,
    default_source: str | None = None,
    default_values: dict[str, Any] | None = None,
    missing_parents: set[str] | None = None,
) -> None:
    resolved_declarations = resolved_declarations or declarations
    property_sources = property_sources or {}
    default_values = default_values or {}
    missing_parents = missing_parents or set()
    cache: dict[str, tuple[dict[str, Any], dict[str, str], list[str], list[dict[str, Any]], str]] = {}

    def inherited_action(source_id: str) -> str:
        if source_id == default_source:
            return "default"
        if source_id.startswith("theme-"):
            return "theme"
        return "inherit"

    def resolve(style_id: str, visiting: set[str] | None = None) -> tuple[dict[str, Any], dict[str, str], list[str], list[dict[str, Any]], str]:
        if style_id in cache:
            return cache[style_id]
        visiting = set() if visiting is None else visiting
        if style_id in visiting:
            return {}, {}, [styles[style_id][0]] if style_id in styles else [], [], "ambiguous"
        visiting.add(style_id)
        merged: dict[str, Any] = deepcopy(default_values)
        provenance: dict[str, str] = {name: default_source for name in default_values if default_source}
        chain: list[str] = [default_source] if default_source else []
        trace: list[dict[str, Any]] = [
            {"property": name, "source": default_source, "action": "default"}
            for name in default_values
            if default_source
        ]
        status = "normalized"
        based_id = graph.get(style_id)
        if based_id:
            if based_id in graph:
                base_values, base_provenance, _base_chain, _base_trace, base_status = resolve(based_id, visiting)
                merged = deepcopy(base_values)
                provenance = dict(base_provenance)
                chain = list(_base_chain)
                trace = [
                    {"property": name, "source": source, "action": inherited_action(source)}
                    for name, source in provenance.items()
                ]
                if base_status != "normalized":
                    status = base_status
            else:
                status = "ambiguous"
        if style_id in missing_parents:
            status = "ambiguous"
        current_source = styles[style_id][0]
        own_values = resolved_declarations.get(style_id, {})
        own_sources = property_sources.get(style_id, {})
        for property_name, value in own_values.items():
            merged[property_name] = deepcopy(value)
            source = own_sources.get(property_name) or current_source
            provenance[property_name] = source
            action = "theme" if source.startswith("theme-") else "direct"
            trace.append({"property": property_name, "source": source, "action": action})
            if isinstance(value, dict) and value.get("kind") == "theme":
                status = "ambiguous"
            if source not in chain:
                chain.append(source)
        if current_source not in chain:
            chain.append(current_source)
        visiting.remove(style_id)
        result = (merged, provenance, chain, trace, status)
        cache[style_id] = result
        return result

    for style_id, (_, resolved_id) in styles.items():
        resolved_values, provenance, chain, trace, status = resolve(style_id)
        resolved_item = builder.find("styles", "styleId", resolved_id)
        if resolved_item is None:
            continue
        resolved_item["resolved"] = resolved_values
        resolved_item["declaration"] = resolved_values
        resolved_item["resolvedFrom"] = list(dict.fromkeys(chain))
        resolved_item["cascadeTrace"] = trace
        resolved_item["propertyProvenance"] = [
            {"property": name, "source": source, "status": "normalized" if status == "normalized" else "ambiguous"}
            for name, source in provenance.items()
        ]
        resolved_item["status"] = status
        # Style declarations are authored in word/styles.xml.  Keep an
        # explicit source map for the resolved lane as well, so the shared
        # package source-occurrence inventory can close the style-resolution
        # feature back to the XML occurrence that produced it.
        builder.add_source_map(
            resolved_id,
            {"part": "word/styles.xml", "path": f"style[{style_id}]"},
        )
        source_item = builder.find("styles", "styleId", styles[style_id][0])
        if source_item is not None and status != "normalized":
            source_item["status"] = status
        diagnostic_ids: list[str] = []
        if style_id in missing_parents:
            parent_id = graph.get(style_id)
            diagnostic_ids.append(
                builder.add_diagnostic(
                    "DFIR-DOCX-STYLE-PARENT-MISSING",
                    f"DOCX style basedOn parent is missing: {parent_id}",
                    target_id=resolved_id,
                    phase="normalize",
                )
            )
        builder.add_feature("style-resolution", status, target_id=resolved_id, diagnostic_ids=diagnostic_ids)


def _add_direct_style(
    builder: DocumentBuilder,
    element: ET.Element,
    theme_colors: dict[str, dict[str, Any]],
    *,
    base_style_ids: list[str] = (),
    base_resolved_id: str | None = None,
    key: str = "direct",
) -> tuple[str, str, str]:
    authored, resolved, property_sources, issues = _parse_style_properties(element, theme_colors)
    direct_index = sum(1 for item in builder.document.get("styles", []) if item.get("origin") == "direct")
    direct_id = "direct-formatting" if direct_index == 0 else f"direct-formatting-{direct_index + 1}"
    for property_name, source_id in property_sources.items():
        if source_id.startswith("theme-"):
            _ensure_theme_style(builder, source_id.removeprefix("theme-"), resolved.get(property_name, authored[property_name]))
    status = "ambiguous" if issues else "normalized"
    property_order = (
        "fontFamily",
        "fontSize",
        "weight",
        "italic",
        "underline",
        "strike",
        "foreground",
        "background",
        "paragraphAlignment",
        "spacing",
    )
    direct_property_names = [
        name for name in property_order if name in resolved
    ] + [name for name in resolved if name not in property_order]
    direct_item: dict[str, Any] = {
        "styleId": direct_id,
        "role": "character",
        "origin": "direct",
        "direct": resolved,
        "authored": authored,
        "declaration": resolved,
        "cascadeTrace": [
            {"property": property_name, "source": property_sources.get(property_name, direct_id), "action": "theme" if property_sources.get(property_name, "").startswith("theme-") else "direct"}
            for property_name in direct_property_names
        ],
        "propertyProvenance": [
            {"property": property_name, "source": property_sources.get(property_name, direct_id), "status": status}
            for property_name in resolved
        ],
        "resolved": resolved,
        "status": status,
    }
    builder.add_item("styles", direct_item, "styleId")
    base_item = builder.find("styles", "styleId", base_resolved_id) if base_resolved_id else None
    merged = deepcopy(base_item.get("resolved", {})) if base_item is not None else {}
    provenance: dict[str, str] = {}
    trace: list[dict[str, Any]] = []
    resolved_from = list(base_style_ids)
    if base_item is not None:
        provenance = {
            item["property"]: item["source"]
            for item in base_item.get("propertyProvenance", [])
            if isinstance(item, dict) and isinstance(item.get("property"), str) and isinstance(item.get("source"), str)
        }
        trace = deepcopy(base_item.get("cascadeTrace", []))
        resolved_from.append(base_resolved_id)
        resolved_from.extend(base_item.get("resolvedFrom", []))
        if base_item.get("status") != "normalized":
            status = "ambiguous"
    for property_name in direct_property_names:
        value = resolved[property_name]
        merged[property_name] = deepcopy(value)
        source = property_sources.get(property_name) or direct_id
        provenance[property_name] = source
        trace.append({"property": property_name, "source": source, "action": "theme" if source.startswith("theme-") else "direct"})
        resolved_from.append(source)
        if isinstance(value, dict) and value.get("kind") == "theme":
            status = "ambiguous"
    resolved_id = safe_id("style", f"docx-resolved-direct-{key}-{direct_index + 1}")
    builder.add_item(
        "styles",
        {
            "styleId": resolved_id,
            "role": "resolved",
            "origin": "resolved",
            "resolvedFrom": list(dict.fromkeys(resolved_from + [direct_id])),
            "declaration": merged,
            "resolved": merged,
            "cascadeTrace": trace,
            "propertyProvenance": [
                {"property": property_name, "source": source, "status": "normalized" if status == "normalized" else "ambiguous"}
                for property_name, source in provenance.items()
            ],
            "status": status,
        },
        "styleId",
    )
    for issue in issues:
        diagnostic = builder.add_diagnostic("DFIR-DOCX-DIRECT-STYLE-UNRESOLVED", issue, target_id=direct_id, phase="normalize")
        builder.add_feature("direct-formatting", "ambiguous", target_id=direct_id, diagnostic_ids=[diagnostic])
    return direct_id, resolved_id, status


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: ExtensionPayload, *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"docx-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item(
        "extensions",
        build_extension(
            extension_id=extension_id,
            target_id=target_id,
            namespace="urn:fdir:format:docx",
            extension_type=extension_type,
            payload=payload,
            criticality=criticality,
        ),
        "extensionId",
    )


def _add_text_run(
    builder: DocumentBuilder,
    parent_id: str,
    run: ET.Element,
    line_index: int,
    styles: dict[str, tuple[str, str]],
    *,
    part_name: str = "word/document.xml",
    theme_colors: dict[str, dict[str, Any]] | None = None,
    base_style_ids: list[str] | None = None,
    base_resolved_id: str | None = None,
) -> str:
    run_id = safe_id("node", f"docx-run-{line_index}-{len(builder.document['nodes'])}")
    style_ref: dict[str, Any] = {}
    rpr = next(iter(_children(run, "rPr")), None)
    if rpr is not None:
        direct, resolved_id, _status = _add_direct_style(
            builder,
            run,
            theme_colors or {},
            base_style_ids=base_style_ids or [],
            base_resolved_id=base_resolved_id,
            key=f"run-{line_index}-{len(builder.document['nodes'])}",
        )
        style_ref = {"styleIds": [*(base_style_ids or []), direct], "directStyleId": direct, "resolvedStyleId": resolved_id}
    elif base_style_ids or base_resolved_id:
        style_ref = {"styleIds": list(base_style_ids or []), "resolvedStyleId": base_resolved_id}
    builder.add_node("run", run_id, parent_id=parent_id, status="preserved", **style_ref)
    values: list[str] = []
    for item in run.iter():
        if _local(item.tag) in {"t", "delText", "instrText"} and item.text is not None:
            values.append(item.text)
    value = "".join(values)
    # An OOXML run may contain a break/tab/property-only construct with no
    # w:t.  Retain the empty authored text fact so the run is not silently
    # disconnected from the graph.
    text_id = safe_id("text", f"docx-text-{line_index}-{len(builder.document['texts'])}")
    builder.add_text(text_id, value, representation="source", provenance="authored")
    builder.link_text(run_id, text_id)
    builder.add_source_map(run_id, {"part": part_name, "path": f"paragraph[{line_index}]", "lineStart": max(1, line_index), "columnStart": 1, "lineEnd": max(1, line_index), "columnEnd": len(value) + 1})
    return run_id


def _docx_decimal_attr(element: ET.Element | None, key: str) -> str | None:
    raw = _attr(element, key)
    if not raw:
        return None
    return decimal(raw)


def _docx_bool_attr(element: ET.Element | None, key: str) -> bool:
    return _attr(element, key).strip().lower() in {"1", "true", "on", "yes"}


def _docx_angle_degrees(element: ET.Element | None) -> str:
    raw = _attr(element, "rot")
    if not raw:
        return "0"
    try:
        return decimal(Decimal(raw) / Decimal(60000))
    except (InvalidOperation, ValueError):
        raise AdapterError(f"invalid DrawingML rotation: {raw!r}")


def _docx_right_angle_matrix(
    angle: str,
    *,
    flip_h: bool = False,
    flip_v: bool = False,
) -> tuple[str, list[str]] | None:
    """Return an exact affine matrix when DrawingML rotation is orthogonal.

    Arbitrary trigonometric rotations cannot be represented exactly by the
    six-decimal IR transform without introducing a float approximation.  The
    caller therefore marks those cases ambiguous instead of pretending that
    an identity matrix preserved the source transform.
    """

    # A DrawingML flip is around the shape centre.  A sign-only matrix would
    # move the shape and silently lose that centre translation, so do not
    # label it affine-preserved without a source-sized transform lane.
    if flip_h or flip_v:
        return None
    try:
        value = Decimal(angle) % Decimal(360)
    except (InvalidOperation, ValueError):
        return None
    if value not in {Decimal(0), Decimal(90), Decimal(180), Decimal(270)}:
        return None
    matrices = {
        Decimal(0): ["1", "0", "0", "1", "0", "0"],
        Decimal(90): ["0", "1", "-1", "0", "0", "0"],
        Decimal(180): ["-1", "0", "0", "-1", "0", "0"],
        Decimal(270): ["0", "-1", "1", "0", "0", "0"],
    }
    matrix = matrices[value]
    a, b, c, d, e, f = matrix
    if flip_h:
        a, b = decimal(-Decimal(a)), decimal(-Decimal(b))
    if flip_v:
        c, d = decimal(-Decimal(c)), decimal(-Decimal(d))
    return "affine", [a, b, c, d, e, f]


def _geometry(
    builder: DocumentBuilder,
    target_id: str,
    extent: ET.Element | None,
    kind: str = "rectangle",
    *,
    x: str = "0",
    y: str = "0",
    rotation: str = "0",
    status: str = "preserved",
) -> str:
    geometry_id = safe_id("geometry", f"docx-{target_id}")
    cx = decimal(_attr(extent, "cx", "0") if extent is not None else "0")
    cy = decimal(_attr(extent, "cy", "0") if extent is not None else "0")
    space_id = "space-docx-page"
    if builder.find("coordinateSpaces", "coordinateSpaceId", space_id) is None:
        builder.add_item("coordinateSpaces", {"coordinateSpaceId": space_id, "unit": "emu", "origin": {"x": "0", "y": "0"}}, "coordinateSpaceId")
    primitive_kind = "rotatedRectangle" if rotation != "0" else "rectangle"
    primitive: dict[str, Any] = {
        "kind": primitive_kind,
        "x": decimal(x),
        "y": decimal(y),
        "width": {"value": cx, "unit": "emu"},
        "height": {"value": cy, "unit": "emu"},
    }
    if rotation != "0":
        primitive["rotation"] = {"value": decimal(rotation), "unit": "deg"}
    geometry_kind = kind if kind in {"rectangle", "rotatedRectangle", "bezier", "clippingPath", "cropRegion"} else "rectangle"
    builder.add_item(
        "geometries",
        {
            "geometryId": geometry_id,
            "spaceId": space_id,
            "kind": geometry_kind,
            "primitives": [primitive],
            "status": status,
        },
        "geometryId",
    )
    return geometry_id


def _docx_position_value(position: ET.Element | None) -> tuple[str, str | None, bool]:
    """Return an authored position token, numeric offset, and ambiguity flag."""

    if position is None:
        return "missing", None, True
    relative = _attr(position, "relativeFrom", "unknown")
    align = next(iter(_children(position, "align")), None)
    if align is not None:
        value = (align.text or "").strip()
        if value:
            return f"{relative}:align={value}", None, False
    offset = next(iter(_children(position, "posOffset")), None)
    if offset is not None and (offset.text or "").strip():
        value = decimal((offset.text or "").strip())
        return f"{relative}:posOffset={value}", value, False
    return f"{relative}:missing", None, True


def _docx_wrap(anchor: ET.Element | None) -> tuple[str | None, bool]:
    if anchor is None:
        return None, False
    names = {
        "wrapNone": "none",
        "wrapSquare": "square",
        "wrapTight": "tight",
        "wrapThrough": "through",
        "wrapTopAndBottom": "top-bottom",
    }
    for child in list(anchor):
        local = _local(child.tag)
        if local in names:
            return names[local], False
    return None, True


def _docx_drawing_parent(builder: DocumentBuilder, parent_id: str) -> str:
    parent = builder.find("nodes", "nodeId", parent_id)
    if isinstance(parent, dict) and parent.get("kind") == "run" and parent.get("parentId"):
        return str(parent["parentId"])
    return parent_id


def _docx_crop_geometry(
    builder: DocumentBuilder,
    target_id: str,
    crop: ET.Element,
) -> str:
    """Represent DrawingML source cropping as a percentage clip geometry."""

    def percentage(key: str) -> Decimal:
        raw = _attr(crop, key, "0")
        try:
            return Decimal(raw) / Decimal(1000)
        except (InvalidOperation, ValueError):
            raise AdapterError(f"invalid DrawingML crop percentage: {raw!r}")

    left, top, right, bottom = (percentage(key) for key in ("l", "t", "r", "b"))
    if any(value < 0 or value > 100 for value in (left, top, right, bottom)) or left + right > 100 or top + bottom > 100:
        raise AdapterError("DrawingML srcRect crop percentages are outside the representable image rectangle")
    geometry_id = safe_id("geometry", f"docx-{target_id}-crop")
    space_id = "space-docx-page"
    primitive = {
        "kind": "rectangle",
        "x": decimal(left),
        "y": decimal(top),
        "width": {"value": decimal(Decimal(100) - left - right), "unit": "percent"},
        "height": {"value": decimal(Decimal(100) - top - bottom), "unit": "percent"},
    }
    builder.add_item(
        "geometries",
        {"geometryId": geometry_id, "spaceId": space_id, "kind": "cropRegion", "primitives": [primitive], "status": "preserved"},
        "geometryId",
    )
    return geometry_id


def _drawing(
    builder: DocumentBuilder,
    parent_id: str,
    drawing: ET.Element,
    line_index: int,
    *,
    surface_id: str | None = None,
    relationships: dict[str, tuple[str, str, str]] | None = None,
    part_name: str = "word/document.xml",
) -> None:
    extent = next(iter(_descendants(drawing, "extent")), None)
    anchor_element = next(iter(_descendants(drawing, "anchor")), None)
    inline_element = next(iter(_descendants(drawing, "inline")), None)
    anchor_kind = "anchor" if anchor_element is not None else "inline" if inline_element is not None else "unknown"
    if _descendants(drawing, "cxnSp"):
        kind = "connector"
    elif _descendants(drawing, "chart"):
        kind = "chart"
    elif _descendants(drawing, "txbx") or _descendants(drawing, "wsp"):
        kind = "textBox" if _descendants(drawing, "txbx") else "shape"
    elif _descendants(drawing, "pic"):
        kind = "image"
    else:
        kind = "shape"
    resource_target = ""
    resource_id = ""
    blip = next(iter(_descendants(drawing, "blip")), None)
    chart_reference = next(iter(_descendants(drawing, "chart")), None)
    ole_reference = next(iter(_descendants(drawing, "OLEObject")), None)
    if blip is not None:
        relationship_id = _rattr(blip, "embed")
    elif chart_reference is not None:
        relationship_id = _rattr(chart_reference, "id")
    elif ole_reference is not None:
        relationship_id = _rattr(ole_reference, "id")
    else:
        relationship_id = ""
    relationship = relationships.get(relationship_id) if relationships and relationship_id else None
    if relationship is not None:
        target, _relationship_type, target_mode = relationship
        if str(target_mode or "").lower() == "external":
            resource_target = target
            resource_id = safe_id("resource", f"docx-external-{target}")
        else:
            resource_target = _relationship_target(part_name, target)
            resource_id = safe_id("resource", resource_target)
    transform_element = next(iter(_descendants(drawing, "xfrm")), None)
    off = next((item for item in list(transform_element) if _local(item.tag) == "off"), None) if transform_element is not None else None
    transform_extent = next((item for item in list(transform_element) if _local(item.tag) == "ext"), None) if transform_element is not None else None
    rotation = _docx_angle_degrees(transform_element)
    flip_h = _docx_bool_attr(transform_element, "flipH")
    flip_v = _docx_bool_attr(transform_element, "flipV")
    transform = _docx_right_angle_matrix(rotation, flip_h=flip_h, flip_v=flip_v)
    transform_status = "preserved" if transform is not None else "ambiguous"
    transform_payload = {
        "kind": transform[0] if transform is not None else "unavailable",
        "matrix": transform[1] if transform is not None else ["1", "0", "0", "1", "0", "0"],
    }
    doc_pr = next(iter(_descendants(drawing, "docPr")), None)
    doc_pr_id = _attr(doc_pr, "id")
    target_key = f"{part_name}:{doc_pr_id}" if doc_pr_id else ""
    target_map = getattr(builder, "_docx_drawing_targets", {})
    node_id = target_map.get(target_key) if target_key else None
    if node_id is None:
        node_id = safe_id("node", f"docx-drawing-{target_key}" if target_key else f"docx-{kind}-{line_index}-{len(builder.document['nodes'])}")
    if target_key:
        target_map[target_key] = node_id
        setattr(builder, "_docx_drawing_targets", target_map)

    local_x = _attr(off, "x", "0") if off is not None else "0"
    local_y = _attr(off, "y", "0") if off is not None else "0"
    geometry_id = _geometry(
        builder,
        node_id,
        extent,
        "rotatedRectangle" if rotation != "0" else "rectangle",
        x=local_x,
        y=local_y,
        rotation=rotation,
        status=transform_status,
    )
    layout_id = safe_id("layout", f"docx-{node_id}")
    position_h = next(iter(_children(anchor_element, "positionH")), None) if anchor_element is not None else None
    position_v = next(iter(_children(anchor_element, "positionV")), None) if anchor_element is not None else None
    h_token, h_offset, h_ambiguous = _docx_position_value(position_h)
    v_token, v_offset, v_ambiguous = _docx_position_value(position_v)
    simple_pos = next(iter(_children(anchor_element, "simplePos")), None) if anchor_element is not None else None
    simple_enabled = _attr(anchor_element, "simplePos", "0").strip().lower() in {"1", "true", "on"} if anchor_element is not None else False
    if simple_enabled and simple_pos is not None:
        h_offset = _attr(simple_pos, "x", "0")
        v_offset = _attr(simple_pos, "y", "0")
        h_token = f"simplePos={decimal(h_offset)}"
        v_token = f"simplePos={decimal(v_offset)}"
        h_ambiguous = v_ambiguous = False
    if anchor_kind == "inline":
        layout_anchor: dict[str, Any] = {"kind": "inline", "nodeId": parent_id}
        layout_status = transform_status
        placement = "inline"
    else:
        parent_node_id = _docx_drawing_parent(builder, parent_id)
        h_relative = _attr(position_h, "relativeFrom", "")
        v_relative = _attr(position_v, "relativeFrom", "")
        if h_relative == "page" or v_relative == "page":
            layout_anchor = {"kind": "page", "surfaceId": surface_id or safe_id("surface", "docx-page-1")}
        elif h_relative in {"paragraph", "character", "line"} or v_relative in {"paragraph", "character", "line"}:
            layout_anchor = {"kind": "paragraph", "nodeId": parent_node_id}
        else:
            layout_anchor = {"kind": "floating", "surfaceId": surface_id or safe_id("surface", "docx-page-1")}
        placement = "anchored"
        wrap, wrap_ambiguous = _docx_wrap(anchor_element)
        layout_status = "preserved" if not any((h_ambiguous, v_ambiguous, wrap_ambiguous)) and transform_status == "preserved" else "ambiguous"
    wrap, wrap_ambiguous = _docx_wrap(anchor_element)
    offset_parts = []
    if anchor_kind == "anchor":
        offset_parts.extend((f"h:{h_token}", f"v:{v_token}"))
        for key in ("distT", "distR", "distB", "distL"):
            raw = _attr(anchor_element, key)
            if raw:
                offset_parts.append(f"{key}={decimal(raw)}")
    offset = ";".join(offset_parts)
    z_raw = _attr(anchor_element, "relativeHeight") if anchor_element is not None else ""
    z_index: int | None = None
    z_ambiguous = False
    if z_raw:
        try:
            z_index = int(z_raw)
        except ValueError:
            z_ambiguous = True
    elif anchor_kind == "anchor":
        z_ambiguous = True
    # Keep the DrawingML shape transform separate from the Word anchor
    # placement.  ``a:xfrm/wp:off`` describes the shape's local geometry and
    # is independently observable from ``wp:positionH/V``.
    layout_transform: dict[str, Any] | None = None
    if transform_element is not None and off is not None:
        layout_transform = {"a": "1", "b": "0", "c": "0", "d": "1", "e": decimal(local_x), "f": decimal(local_y)}
    position_facts_present = anchor_kind == "anchor" and any(
        item is not None for item in (position_h, position_v, simple_pos)
    )
    position_ambiguous = position_facts_present and layout_anchor.get("kind") != "floating"
    effect_extent_present = anchor_element is not None and next(iter(_descendants(anchor_element, "effectExtent")), None) is not None
    connector_geometry_ambiguous = kind == "connector"
    drawing_status = "preserved" if transform_status == "preserved" and not wrap_ambiguous and not z_ambiguous and not position_ambiguous and not connector_geometry_ambiguous and not effect_extent_present and not (anchor_kind == "anchor" and (h_ambiguous or v_ambiguous)) else "ambiguous"
    if drawing_status != "preserved":
        layout_status = "ambiguous"
    builder.add_node(
        kind,
        node_id,
        parent_id=parent_id,
        geometryId=geometry_id,
        layoutIds=[layout_id],
        resourceIds=[resource_id] if resource_id else None,
        status=drawing_status,
    )
    builder.add_source_map(node_id, {"part": part_name, "path": f"drawing[{line_index}]"})
    layout_item: dict[str, Any] = {"layoutId": layout_id, "targetId": node_id, "placement": placement, "anchor": layout_anchor, "declaredGeometryId": geometry_id, "status": layout_status}
    # The common anchor discriminator only permits an offset on the
    # ``floating`` variant.  Paragraph/page anchors carry the authored
    # position in the layout transform instead; putting it on those anchor
    # objects would make the whole document invalid rather than preserving
    # the position fact.
    if offset and layout_item["anchor"].get("kind") == "floating":
        layout_item["anchor"]["offset"] = offset
    if z_index is not None:
        layout_item["zIndex"] = z_index
    if layout_transform is not None:
        layout_item["transform"] = layout_transform
    if wrap is not None:
        layout_item["wrap"] = wrap
    if surface_id is not None:
        layout_item["surfaceId"] = surface_id
        surface = builder.find("surfaces", "surfaceId", surface_id)
        if surface is not None:
            surface.setdefault("layoutIds", []).append(layout_id)
    crop = next(iter(_descendants(drawing, "srcRect")), None)
    if crop is not None:
        crop_id = _docx_crop_geometry(builder, node_id, crop)
        layout_item.setdefault("clipGeometryIds", []).append(crop_id)
    builder.add_item("layouts", layout_item, "layoutId")
    if effect_extent_present:
        diagnostic = builder.add_diagnostic(
            "DFIR-DOCX-EFFECT-EXTENT-UNREPRESENTED",
            "DOCX drawing effectExtent is present but the common geometry contract has no effect-boundary lane",
            target_id=node_id,
            phase="normalize",
        )
        builder.add_feature("drawing-effect-extent", "ambiguous", target_id=node_id, diagnostic_ids=[diagnostic])
        # ``drawing_status`` was computed before node emission so the node and
        # layout cannot accidentally claim preservation for this unsupported
        # effect boundary.
    if connector_geometry_ambiguous:
        diagnostic = builder.add_diagnostic(
            "DFIR-DOCX-CONNECTOR-GEOMETRY-UNRESOLVED",
            "DOCX connector endpoints are retained as relations, but its rendered path geometry is not derivable from the bounded source facts",
            target_id=node_id,
            phase="normalize",
        )
        builder.add_feature("connector-geometry", "ambiguous", target_id=node_id, diagnostic_ids=[diagnostic])
    for connector in _descendants(drawing, "cxnSp"):
        for endpoint, endpoint_kind in (("stCxn", "start"), ("endCxn", "to")):
            item = next(iter(_descendants(connector, endpoint)), None)
            if item is not None:
                target = _wattr(item, "id", "unknown")
                relation_id = safe_id("relation", f"docx-connector-{node_id}-{endpoint_kind}")
                target_node_id = target_map.get(f"{part_name}:{target}", safe_id("node", f"docx-drawing-{part_name}:{target}"))
                if builder.find("nodes", "nodeId", target_node_id) is None:
                    diagnostic = builder.add_diagnostic(
                        "DFIR-DOCX-CONNECTOR-TARGET-UNRESOLVED",
                        f"connector endpoint target is not represented by a parsed node: {target}",
                        target_id=node_id,
                        phase="normalize",
                    )
                    _extension(builder, node_id, "connector-target", {"endpoint": endpoint_kind, "sourceId": target}, criticality="non-critical")
                    builder.add_feature("connector-target", "ambiguous", target_id=node_id, diagnostic_ids=[diagnostic])
                    continue
                builder.add_item("relations", {"relationId": relation_id, "kind": "connectorTarget", "fromId": node_id, "toId": target_node_id, "endpoint": endpoint_kind, "status": "preserved"}, "relationId")
    for text_box in _descendants(drawing, "txbx"):
        for paragraph in _descendants(text_box, "p"):
            for run in _descendants(paragraph, "r"):
                _add_text_run(builder, node_id, run, line_index, {})
    _extension(
        builder,
        node_id,
        "drawing",
        {
            "kind": kind,
            "anchor": "floating" if anchor_kind == "anchor" else "inline",
            "extentEmu": {"cx": _attr(extent, "cx") if extent is not None else "0", "cy": _attr(extent, "cy") if extent is not None else "0"},
            "transform": transform_payload,
        },
    )
    if drawing_status != "preserved":
        diagnostic = builder.add_diagnostic(
            "DFIR-DOCX-DRAWING-ANCHOR-AMBIGUOUS",
            "DOCX drawing contains an anchor, transform, wrap, or z-order fact that is not fully representable in the common lanes",
            target_id=node_id,
            phase="normalize",
        )
        builder.add_feature("drawing", "ambiguous", target_id=node_id, diagnostic_ids=[diagnostic])
    else:
        builder.add_feature("drawing", "preserved", target_id=node_id)
    drawing_order = getattr(builder, "_docx_drawing_order", [])
    drawing_order.append(
        {
            "id": node_id,
            "ordinal": len(drawing_order),
            "anchorKind": anchor_kind,
            "zIndex": z_index,
            "status": drawing_status,
        }
    )
    setattr(builder, "_docx_drawing_order", drawing_order)


def _account_docx_unsupported(
    builder: DocumentBuilder,
    archive: zipfile.ZipFile,
    names: set[str],
    part_ids: dict[str, str],
    limits: AdapterLimits,
) -> None:
    """Account explicitly unsupported XML occurrences without preserving them."""

    seen: set[tuple[str, str]] = set()
    for name in sorted(names):
        if not name.endswith(".xml") or name == "[Content_Types].xml" or "/_rels/" in name or name == "_rels/.rels":
            continue
        try:
            root = _read_xml(archive, name, limits)
        except (AdapterError, ET.ParseError, KeyError, OSError):
            continue
        parent_by_id = {id(child): element for element in root.iter() for child in list(element)}
        target_id = part_ids.get(name, builder.root_id)
        for element in root.iter():
            local = _local(element.tag)
            tokens: list[str] = []
            if local in {"unknownBlock", "customChoice", "customUnsupported"}:
                tokens.append(local)
            elif local == "AlternateContent":
                tokens.append("AlternateContent")
            else:
                parent = parent_by_id.get(id(element))
                parent_local = _local(parent.tag) if parent is not None else ""
                if local in {"Choice", "Fallback"} and parent_local == "AlternateContent":
                    tokens.append(f"AlternateContent:{local}")
            for attribute in element.attrib:
                if attribute in {f"{{{NS_W}}}foo", f"{{{NS_W}}}mystery"}:
                    tokens.append(f"attribute:{attribute.rsplit('}', 1)[-1]}")
            for token in tokens:
                key = (name, token)
                if key in seen:
                    continue
                seen.add(key)
                diagnostic = builder.add_diagnostic(
                    "DFIR-DOCX-UNSUPPORTED-OCCURRENCE",
                    f"unsupported DOCX occurrence {token} in {name}",
                    target_id=target_id,
                    phase="normalize",
                )
                builder.add_feature(token, "unsupported", target_id=target_id, diagnostic_ids=[diagnostic])


def _docx_story_descriptor(part_name: str) -> tuple[str, str, int] | None:
    """Return the stable story identity for a Word story part."""

    normalized = part_name.replace("\\", "/")
    match = re.fullmatch(r"word/(header|footer)(\d+)\.xml", normalized)
    if match is not None:
        return match.group(1) + match.group(2), match.group(1), int(match.group(2))
    for story_type in ("footnote", "endnote", "comment"):
        if normalized == f"word/{story_type}s.xml":
            return story_type + "s", story_type, 1
    return None


def _docx_story_root(root: ET.Element, story_type: str) -> ET.Element:
    container_name = {"header": "hdr", "footer": "ftr"}.get(story_type, f"{story_type}s")
    return next(iter(_children(root, container_name)), root)


def _docx_text_content(element: ET.Element, names: set[str] | None = None) -> str:
    """Collect authored text in document order without normalizing whitespace."""

    accepted = names or {"t", "delText"}
    return "".join(item.text or "" for item in element.iter() if _local(item.tag) in accepted)


def _docx_inline_tokens(element: ET.Element, names: set[str] | None = None) -> list[str]:
    """Return text and inline control tokens in authored XML order."""

    accepted = names or {"t", "delText", "instrText"}
    tokens: list[str] = []
    for item in element.iter():
        local = _local(item.tag)
        if local in accepted:
            tokens.append(item.text or "")
        elif local == "tab":
            tokens.append("\t")
        elif local in {"br", "cr"}:
            tokens.append("\n")
    return tokens


def _docx_inline_has_control_tokens(element: ET.Element) -> bool:
    """Whether authored inline content contains a non-text control token."""
    return any(_local(item.tag) in {"tab", "br"} for item in element.iter())


def _add_docx_field(
    builder: DocumentBuilder,
    *,
    paragraph_id: str,
    paragraph_number: int,
    ordinal: int,
    kind: str,
    instruction: str,
    displayed_result: str,
    field_range: dict[str, Any],
    flags: list[str] | None = None,
    depth: int | None = None,
    events: list[dict[str, Any]] | None = None,
    status: str = "preserved",
    part_name: str = "word/document.xml",
    field_scope: str | None = None,
) -> None:
    scope_prefix = f"{field_scope}-" if field_scope else ""
    field_id = safe_id("field", f"docx-field-{scope_prefix}{paragraph_number}-{ordinal}")
    field: dict[str, Any] = {
        "fieldId": field_id,
        "ownerNodeId": paragraph_id,
        "kind": kind,
        "instruction": instruction,
        "displayedResult": displayed_result,
        "range": field_range,
        "status": status,
    }
    if flags:
        field["flags"] = list(dict.fromkeys(flags))
    builder.add_item("fields", field, "fieldId")
    field_node = safe_id("node", f"docx-field-node-{scope_prefix}{paragraph_number}-{ordinal}")
    builder.add_node("field", field_node, parent_id=paragraph_id, fieldId=field_id, status=status)
    builder.add_source_map(field_node, {"part": part_name, "path": f"paragraph[{paragraph_number}]/field[{ordinal}]"})
    if depth is not None and events is not None:
        _extension(builder, field_node, "field-sequence", {"depth": depth, "events": events})
        builder.add_feature("field-sequence", "preserved", target_id=field_node)


def _add_docx_paragraph_fields(
    builder: DocumentBuilder,
    paragraph: ET.Element,
    paragraph_id: str,
    paragraph_number: int,
    *,
    part_name: str = "word/document.xml",
    field_scope: str | None = None,
) -> None:
    """Parse simple and nested complex fields without flattening their ranges."""

    specs: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    ordinal = 0

    def append_event(record: dict[str, Any], event: dict[str, Any]) -> None:
        record.setdefault("events", []).append(event)

    for item in paragraph.iter():
        item_local = _local(item.tag)
        if item_local == "fldSimple":
            ordinal += 1
            instruction = (_wattr(item, "instr") or _attr(item, "instr")).strip()
            specs.append(
                {
                    "ordinal": ordinal,
                    "kind": "simple",
                    "instruction": " ".join(instruction.split()),
                    "displayedResult": _docx_text_content(item),
                    "range": {"balanced": True},
                    "depth": 0,
                    "events": [],
                    "flags": [],
                    "status": "preserved",
                }
            )
            continue
        if item_local == "fldChar":
            fld_type = _wattr(item, "fldCharType")
            if fld_type == "begin":
                for active in stack:
                    append_event(active, {"type": "begin", "depth": len(stack)})
                ordinal += 1
                depth = len(stack)
                flags: list[str] = []
                for attribute, flag in (("dirty", "dirty"), ("fldLock", "locked"), ("private", "private")):
                    if _wattr(item, attribute).lower() in {"1", "true", "on"}:
                        flags.append(flag)
                record = {
                    "ordinal": ordinal,
                    "kind": "complex",
                    "instructionValues": [],
                    "resultValues": [],
                    "range": {"begin": 1, "separate": 2, "end": 3, "balanced": False},
                    "depth": depth,
                    "events": [],
                    "flags": flags,
                    "status": "ambiguous",
                }
                append_event(record, {"type": "begin", "depth": depth})
                specs.append(record)
                stack.append(record)
            elif fld_type in {"separate", "end"} and stack:
                active = stack[-1]
                append_event(active, {"type": fld_type, "depth": active["depth"]})
                active["range"][fld_type] = 2 if fld_type == "separate" else 3
                if fld_type == "end":
                    active["range"]["balanced"] = "separate" in active["range"]
                    active["status"] = "preserved" if active["range"]["balanced"] else "ambiguous"
                    stack.pop()
            continue
        if item_local == "instrText" and stack:
            value = (item.text or "").strip()
            for active in stack:
                append_event(active, {"type": "instruction", "depth": active["depth"], "value": value})
                active.setdefault("instructionValues", []).append(value)
            continue
        if item_local in {"t", "delText"} and stack:
            value = item.text or ""
            for active in stack:
                append_event(active, {"type": "result", "depth": active["depth"], "value": value})
                active.setdefault("resultValues", []).append(value)

    for spec in sorted(specs, key=lambda item: item["ordinal"]):
        events = [
            event
            for event in spec.get("events", [])
            if event.get("type") != "begin" or event.get("depth") == spec.get("depth")
        ]
        _add_docx_field(
            builder,
            paragraph_id=paragraph_id,
            paragraph_number=paragraph_number,
            ordinal=spec["ordinal"],
            kind=spec["kind"],
            instruction=(
                " ".join(str(value) for value in spec.get("instructionValues", []) if value)
                or str(spec.get("instruction", ""))
            ),
            displayed_result="".join(spec.get("resultValues", [])) if spec["kind"] == "complex" else spec.get("displayedResult", ""),
            field_range=spec["range"],
            flags=spec.get("flags") or None,
            depth=spec.get("depth"),
            events=events,
            status=spec.get("status", "preserved"),
            part_name=part_name,
            field_scope=field_scope,
        )


def _parse_story_paragraph(
    builder: DocumentBuilder,
    paragraph: ET.Element,
    *,
    paragraph_id: str,
    parent_id: str,
    part_id: str,
    part_name: str,
    paragraph_number: int,
    styles: dict[str, tuple[str, str]],
    relationships: dict[str, tuple[str, str, str]] | None = None,
    surface_id: str | None = None,
) -> bool:
    """Emit one story paragraph and report whether an unsupported child occurred."""

    builder.add_node("paragraph", paragraph_id, parent_id=parent_id, part_id=part_id, status="preserved")
    builder.add_source_map(paragraph_id, {"part": part_name, "path": f"story/p[{paragraph_number}]"})
    _add_docx_paragraph_fields(
        builder,
        paragraph,
        paragraph_id,
        paragraph_number,
        part_name=part_name,
        field_scope=part_name,
    )
    unsupported = False
    for item in list(paragraph):
        item_local = _local(item.tag)
        if item_local in {"r", "ins", "del"}:
            runs = [item] if item_local == "r" else _children(item, "r")
            for run in runs:
                run_id = _add_text_run(builder, paragraph_id, run, paragraph_number, styles, part_name=part_name)
                for drawing in _children(run, "drawing"):
                    _drawing(
                        builder,
                        run_id,
                        drawing,
                        paragraph_number,
                        surface_id=surface_id,
                        relationships=relationships,
                        part_name=part_name,
                    )
                if item_local in {"ins", "del"}:
                    revision_kind = "insert" if item_local == "ins" else "delete"
                    _extension(
                        builder,
                        run_id,
                        "revision",
                        {
                            "kind": revision_kind,
                            "author": _wattr(item, "author"),
                            "revisionId": _wattr(item, "id"),
                            "range": f"paragraph:{paragraph_number}",
                        },
                    )
                    builder.add_feature("revision", "preserved", target_id=run_id)
        elif item_local == "hyperlink":
            nested_run_ids: list[str] = []
            for nested in item.iter():
                if _local(nested.tag) == "r":
                    nested_run_ids.append(_add_text_run(builder, paragraph_id, nested, paragraph_number, styles, part_name=part_name))
            relationship_id = _rattr(item, "id")
            relationship = relationships.get(relationship_id) if relationships and relationship_id else None
            target = ""
            if relationship is not None:
                target = relationship[0] if str(relationship[2] or "").lower() == "external" else _relationship_target(part_name, relationship[0])
            anchor_name = _wattr(item, "anchor")
            tokens = _docx_inline_tokens(item)
            content_anchor: dict[str, Any] = {"kind": "content", "resolved": bool(nested_run_ids)}
            if _docx_inline_has_control_tokens(item):
                content_anchor["tokens"] = tokens
            annotation_id = safe_id("annotation", f"docx-story-hyperlink-{part_name}-{paragraph_number}-{len(builder.document['annotations'])}")
            builder.add_item(
                "annotations",
                {
                    "annotationId": annotation_id,
                    "kind": "hyperlink",
                    "targetIds": [paragraph_id, *nested_run_ids],
                    "sourceSubtype": "w:hyperlink",
                    "action": {"kind": "relationship", "relationshipId": relationship_id} if relationship_id else {"kind": "anchor", "target": anchor_name},
                    "body": target or anchor_name,
                    "displayText": "".join(tokens),
                    "anchor": content_anchor,
                    "destination": target or anchor_name,
                    "status": "preserved",
                },
                "annotationId",
            )
        elif item_local != "pPr":
            diagnostic = builder.add_diagnostic(
                "DFIR-DOCX-STORY-CHILD-UNSUPPORTED",
                f"unsupported story paragraph child {item_local} in {part_name}",
                target_id=paragraph_id,
                phase="normalize",
            )
            builder.add_feature("story-child", "unsupported", target_id=paragraph_id, diagnostic_ids=[diagnostic])
            unsupported = True
    builder.add_feature("story-paragraph", "preserved", target_id=paragraph_id)
    return unsupported


def _parse_story_part(
    builder: DocumentBuilder,
    archive: zipfile.ZipFile,
    part_name: str,
    part_id: str,
    story_node_id: str,
    story_type: str,
    story_id: str,
    styles: dict[str, tuple[str, str]],
    limits: AdapterLimits | None = None,
    owner_surface_id: str | None = None,
    relationships: dict[str, tuple[str, str, str]] | None = None,
) -> bool:
    """Parse a DOCX story part under an explicit story node and surface."""

    del story_id
    root = _read_xml(archive, part_name, limits)
    story_root = _docx_story_root(root, story_type)
    story_part = builder.find("parts", "partId", part_id)
    if story_part is None:
        raise AdapterError(f"story part was not registered: {part_name}")
    root_node_ids: list[str] = []
    unsupported = False
    table_number = [len(builder.document.get("tables", []))]

    if story_type in {"header", "footer"}:
        paragraph_number = 0
        for child in list(story_root):
            local = _local(child.tag)
            if local == "p":
                paragraph_number += 1
                paragraph_id = safe_id("node", f"docx-story-paragraph-{part_name}-{paragraph_number}")
                root_node_ids.append(paragraph_id)
                unsupported = _parse_story_paragraph(
                    builder,
                    child,
                    paragraph_id=paragraph_id,
                    parent_id=story_node_id,
                    part_id=part_id,
                    part_name=part_name,
                    paragraph_number=paragraph_number,
                    styles=styles,
                    relationships=relationships,
                    surface_id=owner_surface_id,
                ) or unsupported
            elif local == "tbl":
                table_node_id, _table_id = _parse_docx_table(
                    builder,
                    child,
                    table_number=table_number,
                    parent_id=story_node_id,
                    part_id=part_id,
                    owner_surface_id=owner_surface_id,
                    owner_cell_id=None,
                    styles=styles,
                    theme_colors={},
                )
                root_node_ids.append(table_node_id)
            else:
                diagnostic = builder.add_diagnostic(
                    "DFIR-DOCX-STORY-ELEMENT-UNSUPPORTED",
                    f"unsupported story element {local} in {part_name}",
                    target_id=part_id,
                    phase="normalize",
                )
                builder.add_feature("story-element", "unsupported", target_id=part_id, diagnostic_ids=[diagnostic])
                unsupported = True
    else:
        item_name = story_type
        item_number = 0
        for item in _children(story_root, item_name):
            item_number += 1
            identifier = _wattr(item, "id", str(item_number))
            item_id = safe_id("node", f"docx-story-{story_type}-{identifier}")
            builder.add_node(story_type, item_id, parent_id=story_node_id, part_id=part_id, status="preserved")
            builder.add_source_map(item_id, {"part": part_name, "path": f"{item_name}[@w:id='{identifier}']"})
            root_node_ids.append(item_id)
            paragraph_number = 0
            for child in list(item):
                if _local(child.tag) == "p":
                    paragraph_number += 1
                    paragraph_id = safe_id("node", f"docx-story-{story_type}-{identifier}-paragraph-{paragraph_number}")
                    unsupported = _parse_story_paragraph(
                        builder,
                        child,
                        paragraph_id=paragraph_id,
                        parent_id=item_id,
                        part_id=part_id,
                        part_name=part_name,
                        paragraph_number=paragraph_number,
                        styles=styles,
                        relationships=relationships,
                        surface_id=owner_surface_id,
                    ) or unsupported
                elif _local(child.tag) not in {"pPr"}:
                    if _local(child.tag) == "tbl":
                        _parse_docx_table(
                            builder,
                            child,
                            table_number=table_number,
                            parent_id=item_id,
                            part_id=part_id,
                            owner_surface_id=owner_surface_id,
                            owner_cell_id=None,
                            styles=styles,
                            theme_colors={},
                        )
                    else:
                        diagnostic = builder.add_diagnostic(
                            "DFIR-DOCX-STORY-ELEMENT-UNSUPPORTED",
                            f"unsupported {item_name} child {_local(child.tag)} in {part_name}",
                            target_id=item_id,
                            phase="normalize",
                        )
                        builder.add_feature("story-element", "unsupported", target_id=item_id, diagnostic_ids=[diagnostic])
                        unsupported = True

    story_part["rootNodeIds"] = root_node_ids
    if unsupported:
        story_part["status"] = "ambiguous"
        diagnostic = builder.add_diagnostic(
            "DFIR-DOCX-STORY-PART-PARTIAL",
            f"DOCX story part contains explicitly unsupported constructs: {part_name}",
            target_id=part_id,
            phase="normalize",
        )
        builder.add_feature("story-part", "ambiguous", target_id=part_id, diagnostic_ids=[diagnostic])
    else:
        story_part["status"] = "preserved"
        builder.add_feature("story-part", "preserved", target_id=part_id)
    return not unsupported


def _docx_column_label(number: int) -> str:
    result = ""
    value = number
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result or "A"


def _docx_int_attr(element: ET.Element | None, key: str, default: int = 0) -> int:
    raw = _wattr(element, key) if element is not None else ""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_docx_table(
    builder: DocumentBuilder,
    table: ET.Element,
    *,
    table_number: list[int],
    parent_id: str,
    part_id: str,
    owner_surface_id: str | None,
    owner_cell_id: str | None,
    styles: dict[str, tuple[str, str]],
    theme_colors: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """Emit a DOCX table grid, including nested tables and vertical merges."""

    table_number[0] += 1
    number = table_number[0]
    node_id = safe_id("node", f"docx-table-{number}")
    table_id = safe_id("table", f"docx-{number}")
    builder.add_node("table", node_id, parent_id=parent_id, part_id=part_id, status="preserved")
    rows = _children(table, "tr")
    grid = next(iter(_children(table, "tblGrid")), None)
    grid_widths = [_wattr(item, "w") for item in _children(grid, "gridCol") if _wattr(item, "w")] if grid is not None else []
    grid_columns = len(grid_widths)
    grid_values = list(range(1, grid_columns + 1))
    row_ids: list[str] = []
    cell_ids: list[str] = []
    nested_table_ids: list[str] = []
    grid_before: dict[str, int] = {}
    grid_after: dict[str, int] = {}
    merge_by_column: dict[int, str] = {}
    merge_records: dict[str, dict[str, Any]] = {}
    merge_number = 0

    for row_number, row in enumerate(rows, start=1):
        row_id = safe_id("node", f"docx-table-{number}-row-{row_number}")
        builder.add_node("row", row_id, parent_id=node_id, status="preserved")
        row_ids.append(row_id)
        tr_pr = next(iter(_children(row, "trPr")), None)
        before = _docx_int_attr(next(iter(_children(tr_pr, "gridBefore")), None) if tr_pr is not None else None, "val")
        after = _docx_int_attr(next(iter(_children(tr_pr, "gridAfter")), None) if tr_pr is not None else None, "val")
        if before > 0:
            grid_before[row_id] = before
        if after > 0:
            grid_after[row_id] = after
        cells = _children(row, "tc")
        for column_number, cell in enumerate(cells, start=1):
            cell_id = safe_id("node", f"docx-table-{number}-cell-{row_number}-{column_number}")
            tc_pr = next(iter(_children(cell, "tcPr")), None)
            span = max(1, _docx_int_attr(next(iter(_children(tc_pr, "gridSpan")), None) if tc_pr is not None else None, "val", 1))
            vmerge = next(iter(_children(tc_pr, "vMerge")), None) if tc_pr is not None else None
            merge_value = _wattr(vmerge, "val") if vmerge is not None else ""
            merge_role: str | None = None
            merge_id: str | None = None
            if vmerge is not None:
                if merge_value in {"", "continue"} and column_number in merge_by_column:
                    merge_id = merge_by_column[column_number]
                    merge_role = "follower"
                    merge_records[merge_id]["followerCellIds"].append(cell_id)
                    merge_records[merge_id]["endRow"] = row_number
                else:
                    merge_number += 1
                    merge_id = f"docx-vmerge-{merge_number}"
                    merge_by_column[column_number] = merge_id
                    merge_role = "master"
                    merge_records[merge_id] = {
                        "range": None,
                        "masterCellId": cell_id,
                        "followerCellIds": [],
                        "policy": "master-plus-follower",
                        "column": column_number,
                        "startRow": row_number,
                        "endRow": row_number,
                    }
            else:
                merge_by_column.pop(column_number, None)
            fields: dict[str, Any] = {"address": {"row": row_number, "column": column_number}}
            if span > 1:
                fields["gridSpan"] = span
            if merge_role is not None:
                fields["mergeRole"] = merge_role
            if merge_id is not None:
                fields["mergeId"] = merge_id
            builder.add_node("cell", cell_id, parent_id=row_id, status="preserved", **fields)
            cell_ids.append(cell_id)
            paragraph_number = 0
            for child in list(cell):
                child_local = _local(child.tag)
                if child_local == "p":
                    paragraph_number += 1
                    cell_paragraph_id = safe_id(
                        "node",
                        f"docx-table-{number}-row-{row_number}-cell-{column_number}-paragraph-{paragraph_number}",
                    )
                    builder.add_node("paragraph", cell_paragraph_id, parent_id=cell_id, part_id=part_id, status="preserved")
                    for run in _children(child, "r"):
                        _add_text_run(
                            builder,
                            cell_paragraph_id,
                            run,
                            row_number + paragraph_number,
                            styles,
                            part_name="word/document.xml",
                            theme_colors=theme_colors,
                        )
                elif child_local == "tbl":
                    _nested_node_id, nested_table_id = _parse_docx_table(
                        builder,
                        child,
                        table_number=table_number,
                        parent_id=cell_id,
                        part_id=part_id,
                        owner_surface_id=None,
                        owner_cell_id=cell_id,
                        styles=styles,
                        theme_colors=theme_colors,
                    )
                    nested_table_ids.append(nested_table_id)

    if not grid_columns:
        grid_columns = max((len(_children(row, "tc")) for row in rows), default=1)
        grid_values = list(range(1, grid_columns + 1))
    column_ids: list[str] = []
    for column_number in range(1, grid_columns + 1):
        column_id = safe_id("node", f"docx-table-{number}-column-{column_number}")
        builder.add_node("column", column_id, parent_id=node_id, status="preserved")
        column_ids.append(column_id)

    merged_ranges: list[dict[str, Any]] = []
    for record in merge_records.values():
        start_row = int(record["startRow"])
        end_row = int(record["endRow"])
        column_number = int(record["column"])
        record["range"] = f"{_docx_column_label(column_number)}{start_row}:{_docx_column_label(column_number)}{end_row}"
        merged_ranges.append({key: record[key] for key in ("range", "masterCellId", "followerCellIds", "policy")})

    table_item: dict[str, Any] = {
        "tableId": table_id,
        "nodeId": node_id,
        "range": {"rowStart": 1, "rowEnd": len(rows), "columnStart": 1, "columnEnd": grid_columns},
        "gridColumns": grid_values,
        "rowIds": row_ids,
        "columnIds": column_ids,
        "cellIds": cell_ids,
        "mergedRanges": merged_ranges,
        "nestedTableIds": nested_table_ids,
        "gridBefore": grid_before,
        "gridAfter": grid_after,
        "status": "preserved",
    }
    if grid_widths:
        table_item["gridColumnWidths"] = grid_widths
    if owner_surface_id is not None:
        table_item["ownerSurfaceId"] = owner_surface_id
    if owner_cell_id is not None:
        table_item["ownerCellId"] = owner_cell_id
    builder.add_item("tables", table_item, "tableId")
    builder.add_item(
        "orders",
        {
            "orderId": safe_id("order", f"docx-table-{number}-grid"),
            "kind": "grid",
            "ownerId": node_id,
            "items": [{"id": row_id, "ordinal": ordinal} for ordinal, row_id in enumerate(row_ids)],
            "status": "preserved",
        },
        "orderId",
    )
    builder.add_feature("table", "preserved", target_id=node_id)
    return node_id, table_id


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "docx", "ECMA-376", limits=limits)
    try:
        with zipfile.ZipFile(path) as archive:
            names = validate_zip_archive(archive, limits)
            if len(names) > limits.max_xml_parts:
                diagnostic = builder.add_diagnostic("DFIR-DOCX-PACKAGE-LIMIT", f"package has {len(names)} parts; limit is {limits.max_xml_parts}", severity="error", phase="parse")
                builder.add_feature("package-validation", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            if "word/document.xml" not in names:
                diagnostic = builder.add_diagnostic("DFIR-DOCX-DOCUMENT-MISSING", "DOCX package lacks word/document.xml", severity="error", phase="parse")
                builder.add_feature("document", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            root = _read_xml(archive, "word/document.xml", limits)
            styles: dict[str, tuple[str, str]] = {}
            theme_colors: dict[str, dict[str, Any]] = {}
            default_source: str | None = None
            default_values: dict[str, Any] = {}
            if "word/styles.xml" in names:
                style_root = _read_xml(archive, "word/styles.xml", limits)
                if "word/theme/theme1.xml" in names:
                    theme_colors = _docx_theme_colors(_read_xml(archive, "word/theme/theme1.xml", limits))
                default_source, default_values = _add_docx_doc_defaults(builder, style_root, theme_colors)
                style_graph: dict[str, str | None] = {}
                style_declarations: dict[str, dict[str, Any]] = {}
                style_resolved_declarations: dict[str, dict[str, Any]] = {}
                style_property_sources: dict[str, dict[str, str]] = {}
                for style in _children(style_root, "style"):
                    style_id = _wattr(style, "styleId")
                    if style_id:
                        based = next(iter(_children(style, "basedOn")), None)
                        style_graph[style_id] = _wattr(based, "val") if based is not None and _wattr(based, "val") else None
                        authored_props, resolved_props, property_sources, _issues = _parse_style_properties(style, theme_colors)
                        style_declarations[style_id] = authored_props
                        style_resolved_declarations[style_id] = resolved_props
                        style_property_sources[style_id] = property_sources
                        authored = _add_style(builder, style, style_id, "paragraph" if _wattr(style, "type") == "paragraph" else "character", theme_colors)
                        styles[style_id] = (authored, safe_id("style", f"docx-resolved-{style_id}"))
                referenced_style_names = {
                    _wattr(pstyle, "val")
                    for paragraph in _descendants(root, "p")
                    for pstyle in _children(next(iter(_children(paragraph, "pPr")), ET.Element("pPr")), "pStyle")
                    if _wattr(pstyle, "val")
                }
                for referenced_style in sorted(referenced_style_names):
                    if referenced_style not in style_graph:
                        style_graph[referenced_style] = None
                        style_declarations[referenced_style] = {}
                        style_resolved_declarations[referenced_style] = {}
                        style_property_sources[referenced_style] = {}
                        styles[referenced_style] = _add_missing_docx_style(builder, referenced_style)
                missing_parents: set[str] = set()
                for child_id, parent_id in list(style_graph.items()):
                    if parent_id and parent_id not in style_graph:
                        style_graph[parent_id] = None
                        style_declarations[parent_id] = {}
                        style_resolved_declarations[parent_id] = {}
                        style_property_sources[parent_id] = {}
                        styles[parent_id] = _add_missing_docx_style(builder, parent_id)
                        missing_parents.add(child_id)
                if _style_cycle(style_graph):
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-STYLE-CYCLE", "style basedOn inheritance contains a cycle", severity="error", phase="validate")
                    builder.add_feature("style-inheritance", "failed", diagnostic_ids=[diagnostic])
                else:
                    _resolve_styles(
                        builder,
                        style_graph,
                        style_declarations,
                        styles,
                        resolved_declarations=style_resolved_declarations,
                        property_sources=style_property_sources,
                        default_source=default_source,
                        default_values=default_values,
                        missing_parents=missing_parents,
                    )
            content_types = _content_types(archive, limits)
            package_names = set(names)
            document_relationships = _rels(archive, "word/_rels/document.xml.rels", limits)
            comment_records: dict[str, dict[str, str]] = {}
            if "word/comments.xml" in names:
                comments_root = _read_xml(archive, "word/comments.xml", limits)
                for comment in _children(comments_root, "comment"):
                    comment_id = _wattr(comment, "id")
                    if comment_id:
                        comment_records[comment_id] = {
                            "body": _docx_text_content(comment),
                            "author": _wattr(comment, "author"),
                            "date": _wattr(comment, "date"),
                        }
            package_part_id = safe_id("part", "docx-package")
            builder.add_item("parts", {"partId": package_part_id, "kind": "package", "name": "OOXML package", "contentType": "application/vnd.openxmlformats-package", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
            part_id = safe_id("part", "docx-document")
            surface_id = safe_id("surface", "docx-page-1")
            builder.add_item("parts", {"partId": part_id, "kind": "document", "name": "word/document.xml", "contentType": content_types.get("word/document.xml", "application/xml"), "parentPartId": package_part_id, "rootNodeIds": [builder.root_id], "surfaceIds": [surface_id], "relationshipIds": [], "status": "preserved"}, "partId")
            part_ids: dict[str, str] = {"[package]": package_part_id, "word/document.xml": part_id}
            parsed_part_names = {"[Content_Types].xml", "word/document.xml", "word/styles.xml", "word/theme/theme1.xml"}
            for package_name in sorted(names):
                normalized_name = package_name.replace("\\", "/")
                if normalized_name == "word/document.xml":
                    continue
                package_part_id_for_name = safe_id("part", f"docx-{normalized_name}")
                part_ids[normalized_name] = package_part_id_for_name
                suffix = normalized_name.rsplit(".", 1)[-1].lower() if "." in normalized_name else ""
                kind = "relationships" if normalized_name.endswith(".rels") else "image" if normalized_name.startswith("word/media/") else "xml" if suffix == "xml" else "embeddedObject"
                story_descriptor = _docx_story_descriptor(normalized_name)
                if story_descriptor is not None:
                    kind = "story"
                    parsed_part_names.add(normalized_name)
                if normalized_name.endswith(".rels"):
                    parsed_part_names.add(normalized_name)
                part_status = "preserved" if normalized_name in parsed_part_names or normalized_name.startswith("word/media/") else "unsupported"
                part_item: dict[str, Any] = {"partId": package_part_id_for_name, "kind": kind, "name": normalized_name, "contentType": content_types.get(normalized_name, "application/octet-stream"), "parentPartId": part_id if story_descriptor is not None else package_part_id, "rootNodeIds": [], "relationshipIds": [], "status": part_status}
                if story_descriptor is not None:
                    part_item["storyType"] = story_descriptor[1]
                builder.add_item("parts", part_item, "partId")
                if part_status == "unsupported":
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-PART-UNPARSED", f"DOCX package part is inventory-visible but not parsed by the bounded adapter: {normalized_name}", target_id=package_part_id_for_name, phase="normalize")
                    builder.add_feature("package-part", "unsupported", target_id=package_part_id_for_name, diagnostic_ids=[diagnostic])
            _account_docx_unsupported(builder, archive, package_names, part_ids, limits)
            relationship_loss = False
            for relationship_file in sorted(name for name in names if name == "_rels/.rels" or "/_rels/" in name):
                source_name = _relationship_source(relationship_file)
                source_part_id = part_ids.get(source_name)
                if source_part_id is None:
                    source_part_id = safe_id("part", f"docx-missing-source-{source_name}")
                    part_ids[source_name] = source_part_id
                    builder.add_item("parts", {"partId": source_part_id, "kind": "unknown", "name": source_name, "external": True, "rootNodeIds": [], "relationshipIds": [], "status": "unavailable"}, "partId")
                source_part = builder.find("parts", "partId", source_part_id)
                for relationship_key, (target, relationship_type, target_mode) in _rels(archive, relationship_file, limits).items():
                    relation_id = safe_id("relation", f"docx-{relationship_file}-{relationship_key}")
                    source_occurrence_id = _docx_source_occurrence_id(relationship_file, relationship_key)
                    builder.add_source_map(
                        relation_id,
                        {"part": relationship_file, "path": f"Relationship[@Id='{relationship_key}']"},
                    )
                    normalized_target_mode = "external" if str(target_mode or "").lower() == "external" else "internal"
                    target_name = str(target)
                    if normalized_target_mode == "external":
                        target_id = safe_id("resource", f"docx-external-{target}")
                        if builder.find("resources", "resourceId", target_id) is None:
                            builder.add_item(
                                "resources",
                                {
                                    "resourceId": target_id,
                                    "kind": "linkedObject",
                                    "mediaType": "application/octet-stream",
                                    "availability": "unavailable",
                                    "derivedHandle": target,
                                    "externalTarget": target,
                                    "sourceRelationshipId": relation_id,
                                    "packagePresence": False,
                                    "rawPayloadAvailable": False,
                                    "decodability": "not-decodable",
                                    "embeddedOrExternal": "external",
                                    "networkAvailability": "unknown",
                                },
                                "resourceId",
                            )
                        relation_kind = "links"
                        relation_status = "unavailable"
                        relationship_loss = True
                    else:
                        target_name = _relationship_target(source_name, target)
                        target_id = part_ids.get(target_name)
                        relation_kind = "references"
                        relation_status = "preserved"
                        if target_id is None:
                            target_id = safe_id("resource", f"docx-missing-target-{target_name}")
                            if builder.find("resources", "resourceId", target_id) is None:
                                builder.add_item(
                                    "resources",
                                    {
                                        "resourceId": target_id,
                                        "kind": "linkedObject",
                                        "mediaType": _docx_media_type(target_name, content_types),
                                        "availability": "unavailable",
                                        "derivedHandle": target_name,
                                        "externalTarget": target_name,
                                        "sourceRelationshipId": relation_id,
                                        "packagePresence": False,
                                        "rawPayloadAvailable": False,
                                        "decodability": "not-decodable",
                                        "embeddedOrExternal": "embedded",
                                        "networkAvailability": "not-applicable",
                                    },
                                    "resourceId",
                                )
                            diagnostic = builder.add_diagnostic("DFIR-DOCX-RELATION-TARGET-MISSING", f"relationship target is missing from the package: {target_name}", target_id=source_part_id, phase="parse", related_ids=[relation_id])
                            builder.add_feature("package-relationship", "ambiguous", target_id=source_part_id, diagnostic_ids=[diagnostic])
                            relation_status = "unavailable"
                            relationship_loss = True
                        elif target_name.startswith("word/media/"):
                            resource_id = safe_id("resource", target_name)
                            resource = builder.find("resources", "resourceId", resource_id)
                            if resource is None:
                                media_type = _docx_media_type(target_name, content_types)
                                decodability = _docx_media_decodability(archive, target_name, media_type, package_names)
                                resource = builder.add_item(
                                    "resources",
                                    {
                                        "resourceId": resource_id,
                                        "kind": "image",
                                        "mediaType": media_type,
                                        "availability": _docx_resource_availability(package_presence=True, raw_payload_available=True, decodability=decodability),
                                        "derivedHandle": target_name,
                                        "sourceRelationshipId": relation_id,
                                        "packagePresence": True,
                                        "rawPayloadAvailable": True,
                                        "decodability": decodability,
                                        "embeddedOrExternal": "embedded",
                                        "networkAvailability": "not-applicable",
                                    },
                                    "resourceId",
                                )
                            else:
                                resource.setdefault("sourceRelationshipId", relation_id)
                            uses_resource_id = safe_id("relation", f"docx-resource-{relation_id}")
                            builder.add_item(
                                "relations",
                                {
                                    "relationId": uses_resource_id,
                                    "kind": "usesResource",
                                    "fromId": source_part_id,
                                    "toId": resource_id,
                                    "sourceRelationshipId": relationship_key,
                                    "sourceOccurrenceId": source_occurrence_id,
                                    "type": relationship_type,
                                    "targetMode": normalized_target_mode,
                                    "status": relation_status,
                                },
                                "relationId",
                            )
                        elif target_name.startswith("word/embeddings/"):
                            resource_id = safe_id("resource", target_name)
                            if builder.find("resources", "resourceId", resource_id) is None:
                                media_type = _docx_media_type(target_name, content_types)
                                decodability = _docx_media_decodability(archive, target_name, media_type, package_names)
                                builder.add_item(
                                    "resources",
                                    {
                                        "resourceId": resource_id,
                                        "kind": "embeddedObject",
                                        "mediaType": media_type,
                                        "availability": _docx_resource_availability(package_presence=True, raw_payload_available=True, decodability=decodability),
                                        "derivedHandle": target_name,
                                        "sourceRelationshipId": relation_id,
                                        "packagePresence": True,
                                        "rawPayloadAvailable": True,
                                        "decodability": decodability,
                                        "embeddedOrExternal": "embedded",
                                        "networkAvailability": "not-applicable",
                                    },
                                    "resourceId",
                                )
                            uses_resource_id = safe_id("relation", f"docx-resource-{relation_id}")
                            builder.add_item(
                                "relations",
                                {
                                    "relationId": uses_resource_id,
                                    "kind": "usesResource",
                                    "fromId": source_part_id,
                                    "toId": resource_id,
                                    "sourceRelationshipId": relationship_key,
                                    "sourceOccurrenceId": source_occurrence_id,
                                    "type": relationship_type,
                                    "targetMode": normalized_target_mode,
                                    "status": relation_status,
                                },
                                "relationId",
                            )
                        elif target_name.startswith("word/charts/") or target_name.startswith("word/diagrams/"):
                            resource_id = safe_id("resource", target_name)
                            if builder.find("resources", "resourceId", resource_id) is None:
                                media_type = _docx_media_type(target_name, content_types)
                                decodability = _docx_media_decodability(archive, target_name, media_type, package_names)
                                builder.add_item(
                                    "resources",
                                    {
                                        "resourceId": resource_id,
                                        "kind": "chart",
                                        "mediaType": media_type,
                                        "availability": _docx_resource_availability(package_presence=True, raw_payload_available=True, decodability=decodability),
                                        "derivedHandle": target_name,
                                        "sourceRelationshipId": relation_id,
                                        "packagePresence": True,
                                        "rawPayloadAvailable": True,
                                        "decodability": decodability,
                                        "embeddedOrExternal": "embedded",
                                        "networkAvailability": "not-applicable",
                                    },
                                    "resourceId",
                                )
                            uses_resource_id = safe_id("relation", f"docx-resource-{relation_id}")
                            builder.add_item(
                                "relations",
                                {
                                    "relationId": uses_resource_id,
                                    "kind": "usesResource",
                                    "fromId": source_part_id,
                                    "toId": resource_id,
                                    "sourceRelationshipId": relationship_key,
                                    "sourceOccurrenceId": source_occurrence_id,
                                    "type": relationship_type,
                                    "targetMode": normalized_target_mode,
                                    "status": relation_status,
                                },
                                "relationId",
                            )
                    builder.add_item(
                        "relations",
                        {
                            "relationId": relation_id,
                            "kind": relation_kind,
                            "fromId": source_part_id,
                            "toId": target_id,
                            "sourceOccurrenceId": source_occurrence_id,
                            "sourceRelationshipId": relationship_key,
                            "type": relationship_type,
                            "target": str(target),
                            "resolvedTarget": str(target_name),
                            "targetMode": normalized_target_mode,
                            "status": relation_status,
                        },
                        "relationId",
                    )
                    if source_part is not None:
                        source_part.setdefault("relationshipIds", []).append(relation_id)
            if relationship_loss:
                diagnostic = builder.add_diagnostic(
                    "DFIR-DOCX-RELATIONSHIP-CLOSURE-INCOMPLETE",
                    "DOCX OPC relationships include an external or missing target; relationship inventory is retained, but closure is not complete",
                    target_id=builder.root_id,
                    phase="normalize",
                )
                builder.add_feature("package-relationships", "ambiguous", target_id=builder.root_id, diagnostic_ids=[diagnostic])
            else:
                builder.add_feature("package-relationships", "preserved", target_id=builder.root_id)
            builder.add_item("surfaces", {"surfaceId": surface_id, "partId": part_id, "kind": "page", "ordinal": 0, "status": "preserved"}, "surfaceId")
            body = next(iter(_children(root, "body")), root)
            table_counter = [0]
            paragraph_number = 0
            source_block_ids: list[str] = []
            reading_order_ids: list[str] = []
            source_order_ambiguous = False
            for child in list(body):
                local = _local(child.tag)
                if local == "p":
                    paragraph_number += 1
                    ppr = next(iter(_children(child, "pPr")), None)
                    style_name = ""
                    if ppr is not None:
                        pstyle = next(iter(_children(ppr, "pStyle")), None)
                        style_name = _wattr(pstyle, "val") if pstyle is not None else ""
                    kind = "heading" if style_name.lower().startswith("heading") else "paragraph"
                    paragraph_id = safe_id("node", f"docx-paragraph-{paragraph_number}")
                    refs: dict[str, Any] = {}
                    base_style_ids: list[str] = []
                    base_resolved_id: str | None = None
                    if style_name in styles:
                        base_style_ids = [styles[style_name][0]]
                        base_resolved_id = styles[style_name][1]
                    direct_style_id: str | None = None
                    if ppr is not None and _parse_style_properties(ppr, theme_colors)[0]:
                        direct_style_id, base_resolved_id, _status = _add_direct_style(
                            builder,
                            ppr,
                            theme_colors,
                            base_style_ids=base_style_ids,
                            base_resolved_id=base_resolved_id,
                            key=f"paragraph-{paragraph_number}",
                        )
                    if base_style_ids or direct_style_id or base_resolved_id:
                        refs = {
                            "styleIds": [*base_style_ids, *([direct_style_id] if direct_style_id else [])],
                            "resolvedStyleId": base_resolved_id,
                        }
                        if direct_style_id:
                            refs["directStyleId"] = direct_style_id
                    builder.add_node(kind, paragraph_id, parent_id=builder.root_id, part_id=part_id, status="preserved", **refs)
                    builder.add_source_map(paragraph_id, {"part": "word/document.xml", "path": f"body/p[{paragraph_number}]"})
                    source_block_ids.append(paragraph_id)
                    reading_order_ids.append(paragraph_id)
                    _add_docx_paragraph_fields(builder, child, paragraph_id, paragraph_number, part_name="word/document.xml")
                    num_pr = next(iter(_children(ppr, "numPr")), None) if ppr is not None else None
                    if num_pr is not None:
                        _extension(builder, paragraph_id, "numbering", {"numId": _wattr(next(iter(_children(num_pr, "numId")), None), "val"), "ilvl": _wattr(next(iter(_children(num_pr, "ilvl")), None), "val")})
                    comment_ranges: dict[str, dict[str, str]] = {}
                    for item in list(child):
                        item_local = _local(item.tag)
                        if item_local in {"r", "ins", "del"}:
                            runs = [item] if item_local == "r" else _children(item, "r")
                            revision_run_ids: list[str] = []
                            for run in runs:
                                run_id = _add_text_run(
                                    builder,
                                    paragraph_id,
                                    run,
                                    paragraph_number,
                                    styles,
                                    theme_colors=theme_colors,
                                    base_style_ids=[*base_style_ids, *([direct_style_id] if direct_style_id else [])],
                                    base_resolved_id=base_resolved_id,
                                )
                                revision_run_ids.append(run_id)
                                if item_local == "ins":
                                    _extension(builder, run_id, "revision", {"kind": "insert", "author": _wattr(item, "author"), "revisionId": _wattr(item, "id"), "range": f"paragraph:{paragraph_number}"})
                                    builder.add_feature("revision", "preserved", target_id=run_id)
                                elif item_local == "del":
                                    _extension(builder, run_id, "revision", {"kind": "delete", "author": _wattr(item, "author"), "revisionId": _wattr(item, "id"), "range": f"paragraph:{paragraph_number}"})
                                    builder.add_feature("revision", "preserved", target_id=run_id)
                                for drawing in _children(run, "drawing"):
                                    _drawing(
                                        builder,
                                        run_id,
                                        drawing,
                                        paragraph_number,
                                        surface_id=surface_id,
                                        relationships=document_relationships,
                                    )
                                for marker in _descendants(run, "commentRangeStart"):
                                    identifier = _wattr(marker, "id")
                                    comment_ranges.setdefault(identifier, {})["start"] = f"commentRangeStart:{identifier}"
                                for marker in _descendants(run, "commentRangeEnd"):
                                    identifier = _wattr(marker, "id")
                                    comment_ranges.setdefault(identifier, {})["end"] = f"commentRangeEnd:{identifier}"
                                for reference in _descendants(run, "commentReference"):
                                    identifier = _wattr(reference, "id")
                                    record = comment_records.get(identifier, {})
                                    range_record = comment_ranges.get(identifier, {})
                                    builder.add_item(
                                        "annotations",
                                        {
                                            "annotationId": safe_id("annotation", f"docx-comment-{paragraph_number}-{identifier}"),
                                            "kind": "comment",
                                            "targetIds": [paragraph_id, run_id],
                                            "referenceId": identifier,
                                            "sourceSubtype": "w:commentRange",
                                            "body": record.get("body", ""),
                                            "author": record.get("author", ""),
                                            "date": record.get("date", ""),
                                            "anchor": {
                                                "kind": "range",
                                                "start": range_record.get("start", f"commentRangeStart:{identifier}"),
                                                "end": range_record.get("end", f"commentRangeEnd:{identifier}"),
                                                "balanced": "start" in range_record and "end" in range_record,
                                            },
                                            "status": "preserved" if record else "unavailable",
                                        },
                                        "annotationId",
                                    )
                            if item_local in {"ins", "del"}:
                                revision_id = _wattr(item, "id")
                                revision_type = "insert" if item_local == "ins" else "delete"
                                authored_text = _docx_text_content(item)
                                revision_annotation_id = safe_id("annotation", f"docx-revision-{revision_type}-{revision_id or paragraph_number}")
                                builder.add_item(
                                    "annotations",
                                    {
                                        "annotationId": revision_annotation_id,
                                        "kind": "revision",
                                        "targetIds": revision_run_ids,
                                        "revisionType": revision_type,
                                        "revisionId": revision_id,
                                        "author": _wattr(item, "author"),
                                        "date": _wattr(item, "date"),
                                        "range": {
                                            "start": f"{item_local}:{revision_id}",
                                            "end": f"{item_local}:{revision_id}",
                                            "balanced": bool(revision_id),
                                        },
                                        "before": authored_text if revision_type == "delete" else "",
                                        "after": authored_text if revision_type == "insert" else "",
                                        "status": "preserved" if revision_id else "ambiguous",
                                    },
                                    "annotationId",
                                )
                        elif item_local == "fldSimple":
                            # Simple fields are emitted by _add_docx_paragraph_fields
                            # so their range is represented exactly once.
                            continue
                        elif item_local == "drawing":
                            _drawing(builder, paragraph_id, item, paragraph_number, surface_id=surface_id, relationships=document_relationships)
                        elif item_local == "commentRangeStart":
                            identifier = _wattr(item, "id")
                            comment_ranges.setdefault(identifier, {})["start"] = f"commentRangeStart:{identifier}"
                        elif item_local == "commentRangeEnd":
                            identifier = _wattr(item, "id")
                            comment_ranges.setdefault(identifier, {})["end"] = f"commentRangeEnd:{identifier}"
                        elif item_local in {"commentReference", "footnoteReference", "endnoteReference", "hyperlink"}:
                            if item_local == "commentReference":
                                identifier = _wattr(item, "id")
                                record = comment_records.get(identifier, {})
                                range_record = comment_ranges.get(identifier, {})
                                builder.add_item(
                                    "annotations",
                                    {
                                        "annotationId": safe_id("annotation", f"docx-comment-{paragraph_number}-{identifier}"),
                                        "kind": "comment",
                                        "targetIds": [paragraph_id],
                                        "referenceId": identifier,
                                        "sourceSubtype": "w:commentRange",
                                        "body": record.get("body", ""),
                                        "author": record.get("author", ""),
                                        "date": record.get("date", ""),
                                        "anchor": {
                                            "kind": "range",
                                            "start": range_record.get("start", f"commentRangeStart:{identifier}"),
                                            "end": range_record.get("end", f"commentRangeEnd:{identifier}"),
                                            "balanced": "start" in range_record and "end" in range_record,
                                        },
                                        "status": "preserved" if record else "unavailable",
                                    },
                                    "annotationId",
                                )
                                continue
                            annotation_id = safe_id("annotation", f"docx-{item_local}-{paragraph_number}-{len(builder.document['annotations'])}")
                            kind_name = {"footnoteReference": "footnote", "endnoteReference": "endnote"}.get(item_local, "bookmark")
                            nested_run_ids: list[str] = []
                            if item_local == "hyperlink":
                                for nested in item.iter():
                                    if _local(nested.tag) == "r":
                                        nested_run_ids.append(
                                            _add_text_run(
                                                builder,
                                                paragraph_id,
                                                nested,
                                                paragraph_number,
                                                styles,
                                                theme_colors=theme_colors,
                                                base_style_ids=[*base_style_ids, *([direct_style_id] if direct_style_id else [])],
                                                base_resolved_id=base_resolved_id,
                                            )
                                        )
                                relationship_id = _rattr(item, "id")
                                relationship = document_relationships.get(relationship_id)
                                target = relationship[0] if relationship is not None else ""
                                anchor_name = _wattr(item, "anchor")
                                tokens = _docx_inline_tokens(item)
                                display_text = "".join(tokens)
                                content_anchor: dict[str, Any] = {"kind": "content", "resolved": bool(nested_run_ids)}
                                if _docx_inline_has_control_tokens(item):
                                    content_anchor["tokens"] = tokens
                                hyperlink_item: dict[str, Any] = {
                                    "annotationId": annotation_id,
                                    "kind": "hyperlink",
                                    "targetIds": [paragraph_id, *nested_run_ids],
                                    "sourceSubtype": "w:hyperlink",
                                    "action": {"kind": "relationship", "relationshipId": relationship_id} if relationship_id else {"kind": "anchor", "target": anchor_name},
                                    "body": target or anchor_name,
                                    "displayText": display_text,
                                    # Keep authored control tokens in the content
                                    # anchor when they carry information that cannot
                                    # be recovered from run text alone.
                                    "anchor": content_anchor,
                                    "destination": target or anchor_name,
                                    "status": "preserved",
                                }
                                builder.add_item("annotations", hyperlink_item, "annotationId")
                            else:
                                builder.add_item("annotations", {"annotationId": annotation_id, "kind": kind_name, "targetIds": [paragraph_id], "referenceId": _wattr(item, "id"), "body": _wattr(item, "id") or _wattr(item, "anchor") or _wattr(item, "id", ""), "status": "preserved"}, "annotationId")
                    builder.add_feature("paragraph", "preserved", target_id=paragraph_id)
                elif local == "tbl":
                    table_node_id, _table_id = _parse_docx_table(
                        builder,
                        child,
                        table_number=table_counter,
                        parent_id=builder.root_id,
                        part_id=part_id,
                        owner_surface_id=surface_id,
                        owner_cell_id=None,
                        styles=styles,
                        theme_colors=theme_colors,
                    )
                    source_block_ids.append(table_node_id)
                    reading_order_ids.append(table_node_id)
                elif local not in {"sectPr"}:
                    if local == "unknownBlock":
                        # _account_docx_unsupported already records this exact
                        # source occurrence against the document part.  Do
                        # not emit a second root-only feature that loses its
                        # package-part provenance.
                        continue
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-ELEMENT-UNSUPPORTED", f"unsupported body element: {local}", target_id=builder.root_id)
                    builder.add_feature(local, "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
                    source_order_ambiguous = True
            section_elements = [item for item in root.iter() if _local(item.tag) == "sectPr"]
            section_parents = {
                id(child): _local(parent.tag)
                for parent in root.iter()
                for child in list(parent)
                if _local(child.tag) == "sectPr"
            }
            paragraph_section_count = sum(1 for item in section_elements if section_parents.get(id(item)) == "pPr")
            # A body-level sectPr supplies the final properties for the last
            # paragraph-defined section.  When multiple paragraph section
            # boundaries already exist, it must not create a phantom page.
            if paragraph_section_count > 1 and section_elements and section_parents.get(id(section_elements[-1])) == "body":
                section_elements = section_elements[:-1]
            surface_ids = [surface_id]
            for section_number, section_element in enumerate(section_elements, start=1):
                current_surface_id = surface_id if section_number == 1 else safe_id("surface", f"docx-page-{section_number}")
                if section_number == 1:
                    current_surface = builder.find("surfaces", "surfaceId", current_surface_id)
                    if current_surface is not None:
                        current_surface["sectionBoundary"] = f"section-{section_number}"
                else:
                    builder.add_item(
                        "surfaces",
                        {
                            "surfaceId": current_surface_id,
                            "partId": part_id,
                            "kind": "page",
                            "ordinal": section_number - 1,
                            "sectionBoundary": f"section-{section_number}",
                            "status": "preserved",
                        },
                        "surfaceId",
                    )
                    surface_ids.append(current_surface_id)
                size = next(iter(_children(section_element, "pgSz")), None)
                margin = next(iter(_children(section_element, "pgMar")), None)
                columns = next(iter(_children(section_element, "cols")), None)
                line_numbers = next(iter(_children(section_element, "lnNumType")), None)
                page_properties = {
                    "pgSz": {
                        "w": _wattr(size, "w"),
                        "h": _wattr(size, "h"),
                        "orient": _wattr(size, "orient", "portrait"),
                    },
                    "pgMar": {
                        key: _wattr(margin, key)
                        for key in ("top", "right", "bottom", "left", "header", "footer", "gutter")
                        if _wattr(margin, key)
                    },
                    "columns": {"num": _wattr(columns, "num", "1"), "space": _wattr(columns, "space")},
                    "lineNumbering": {"countBy": _wattr(line_numbers, "countBy")} if line_numbers is not None else {},
                }
                section_node_id = safe_id("node", f"docx-section-{section_number}")
                builder.add_node("section", section_node_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
                builder.add_source_map(section_node_id, {"part": "word/document.xml", "path": f"section[{section_number}]"})
                builder.add_feature("section", "preserved", target_id=section_node_id)
                if size is not None or margin is not None or columns is not None or line_numbers is not None:
                    _extension(
                        builder,
                        section_node_id,
                        "section-page-properties",
                        {"pageProperties": page_properties},
                    )
                    builder.add_feature("section-page-properties", "preserved", target_id=section_node_id)
            document_part = builder.find("parts", "partId", part_id)
            if document_part is not None:
                document_part["surfaceIds"] = surface_ids
            story_parts = []
            story_priority = {"header": 0, "footer": 1, "footnote": 2, "endnote": 3, "comment": 4}
            for package_part in list(builder.document.get("parts", [])):
                descriptor = _docx_story_descriptor(str(package_part.get("name", "")))
                if descriptor is not None:
                    story_parts.append((story_priority[descriptor[1]], descriptor[2], descriptor, package_part))
            for _priority, _ordinal, descriptor, package_part in sorted(story_parts, key=lambda item: item[:2]):
                story_id, story_type, ordinal = descriptor
                story_node_id = safe_id("node", f"docx-story-{story_id}")
                builder.add_node("story", story_node_id, parent_id=builder.root_id, part_id=str(package_part["partId"]), status="preserved")
                story_surface_id = safe_id("surface", f"docx-{story_type}-{ordinal}")
                builder.add_item(
                    "surfaces",
                    {
                        "surfaceId": story_surface_id,
                        "partId": str(package_part["partId"]),
                        "kind": "story",
                        "ordinal": 0,
                        "storyId": story_id,
                        "status": "preserved",
                    },
                    "surfaceId",
                )
                package_part["surfaceIds"] = [story_surface_id]
                story_relationships = _rels(
                    archive,
                    _relationship_file_for_part(str(package_part["name"])),
                    limits,
                )
                _parse_story_part(
                    builder,
                    archive,
                    str(package_part["name"]),
                    str(package_part["partId"]),
                    story_node_id,
                    story_type,
                    story_id,
                    styles,
                    limits,
                    owner_surface_id=story_surface_id,
                    relationships=story_relationships,
                )
            for comment_part in ("word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"):
                if comment_part in names:
                    part_root = _read_xml(archive, comment_part, limits)
                    for item in _children(part_root, "comment") + _children(part_root, "footnote") + _children(part_root, "endnote"):
                        identifier = _wattr(item, "id")
                        body_text = "".join(text.text or "" for text in item.iter() if _local(text.tag) in {"t", "delText"})
                        _extension(
                            builder,
                            builder.root_id,
                            "annotation-body",
                            {
                                "part": comment_part,
                                "id": identifier,
                                "body": body_text,
                                "status": "preserved",
                                "sourceLocator": {"part": comment_part, "id": identifier},
                            },
                        )
            for media in sorted(name for name in names if name.startswith("word/media/")):
                resource_id = safe_id("resource", media)
                media_type = _docx_media_type(media, content_types)
                decodability = _docx_media_decodability(archive, media, media_type, package_names)
                availability = _docx_resource_availability(package_presence=True, raw_payload_available=True, decodability=decodability)
                resource = builder.find("resources", "resourceId", resource_id)
                if resource is None:
                    resource = builder.add_item(
                        "resources",
                        {
                            "resourceId": resource_id,
                            "kind": "image",
                            "mediaType": media_type,
                            "availability": availability,
                            "derivedHandle": media,
                            "packagePresence": True,
                            "rawPayloadAvailable": True,
                            "decodability": decodability,
                            "embeddedOrExternal": "embedded",
                            "networkAvailability": "not-applicable",
                        },
                        "resourceId",
                    )
                else:
                    resource.update(
                        {
                            "kind": "image",
                            "mediaType": media_type,
                            "availability": availability,
                            "derivedHandle": media,
                            "packagePresence": True,
                            "rawPayloadAvailable": True,
                            "decodability": decodability,
                            "embeddedOrExternal": "embedded",
                            "networkAvailability": "not-applicable",
                        }
                    )
            for resource in builder.document.get("resources", []):
                if isinstance(resource, dict) and resource.get("resourceId"):
                    resource["consumerCount"] = sum(
                        1
                        for relation in builder.document.get("relations", [])
                        if isinstance(relation, dict) and relation.get("toId") == resource.get("resourceId")
                    )
            if source_block_ids:
                builder.add_item(
                    "orders",
                    {
                        "orderId": safe_id("order", "docx-source"),
                        "kind": "source",
                        "ownerId": builder.root_id,
                        "items": [{"id": node_id, "ordinal": ordinal} for ordinal, node_id in enumerate(source_block_ids)],
                        "ordinalBase": 0,
                        "context": "DOCX body block authored order",
                        "status": "ambiguous" if source_order_ambiguous else "preserved",
                    },
                    "orderId",
                )
            if reading_order_ids:
                drawing_order = getattr(builder, "_docx_drawing_order", [])
                has_floating_layout = any(item.get("anchorKind") == "anchor" for item in drawing_order)
                has_ambiguous_drawing = any(item.get("status") != "preserved" for item in drawing_order)
                has_multiple_columns = any(
                    _wattr(next(iter(_children(section, "cols")), None), "num", "1") not in {"", "1"}
                    for section in section_elements
                )
                reading_status = "ambiguous" if source_order_ambiguous or has_floating_layout or has_ambiguous_drawing or has_multiple_columns else "preserved"
                builder.add_item(
                    "orders",
                    {
                        "orderId": safe_id("order", "docx-reading"),
                        "kind": "reading",
                        "ownerId": builder.root_id,
                        "items": [{"id": node_id, "ordinal": ordinal} for ordinal, node_id in enumerate(reading_order_ids)],
                        "ordinalBase": 0,
                        "context": "DOCX authored block order; visual reading order is unresolved for floating or multi-column layout",
                        "status": reading_status,
                    },
                    "orderId",
                )
            drawing_order = getattr(builder, "_docx_drawing_order", [])
            if drawing_order:
                builder.add_item(
                    "orders",
                    {
                        "orderId": safe_id("order", "docx-draw"),
                        "kind": "draw",
                        "ownerId": builder.root_id,
                        "items": [{"id": item["id"], "ordinal": ordinal} for ordinal, item in enumerate(drawing_order)],
                        "ordinalBase": 0,
                        "context": "DOCX DrawingML authored drawing order",
                        "status": "preserved" if all(item.get("status") == "preserved" for item in drawing_order) else "ambiguous",
                    },
                    "orderId",
                )
                floating_drawings = [item for item in drawing_order if item.get("anchorKind") == "anchor"]
                if floating_drawings:
                    z_status = "preserved" if all(item.get("zIndex") is not None and item.get("status") == "preserved" for item in floating_drawings) else "ambiguous"
                    z_sorted = sorted(
                        floating_drawings,
                        key=lambda item: (item.get("zIndex") if item.get("zIndex") is not None else 0, item["ordinal"]),
                    )
                    builder.add_item(
                        "orders",
                        {
                            "orderId": safe_id("order", "docx-z-order"),
                            "kind": "z-order",
                            "ownerId": builder.root_id,
                            "items": [{"id": item["id"], "ordinal": ordinal} for ordinal, item in enumerate(z_sorted)],
                            "ordinalBase": 0,
                            "context": "DOCX wp:anchor relativeHeight z-order; missing relativeHeight remains ambiguous",
                            "status": z_status,
                        },
                        "orderId",
                    )
            builder.add_item("orders", {"orderId": safe_id("order", "docx-structure"), "kind": "structure", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
            builder.add_feature("document", "preserved", target_id=builder.root_id)
            return builder.finish()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-DOCX-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("document", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
