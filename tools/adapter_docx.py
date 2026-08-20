"""Bounded stdlib DOCX adapter for Document Form IR.

The adapter reads the OOXML package and maps recorded structure and authoring
facts.  It does not preserve the package byte stream or infer document meaning.
"""

from __future__ import annotations

from pathlib import Path
import mimetypes
import posixpath
import re
from typing import Any
import zipfile
import xml.etree.ElementTree as ET

try:
    from adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id, validate_zip_members
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterError, AdapterLimits, DocumentBuilder, decimal, input_limit_check, safe_id, validate_zip_members


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


def _attr(element: ET.Element, key: str, default: str = "") -> str:
    return element.attrib.get(key, default)


def _wattr(element: ET.Element, key: str, default: str = "") -> str:
    return element.attrib.get(f"{{{NS_W}}}{key}", default) or element.attrib.get(key, default)


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(archive.read(name))


def _rels(archive: zipfile.ZipFile, name: str) -> dict[str, tuple[str, str, str]]:
    if name not in archive.namelist():
        return {}
    root = _read_xml(archive, name)
    return {_attr(item, "Id"): (_attr(item, "Target"), _attr(item, "Type"), _attr(item, "TargetMode")) for item in root if _local(item.tag) == "Relationship"}


def _content_types(archive: zipfile.ZipFile) -> dict[str, str]:
    if "[Content_Types].xml" not in archive.namelist():
        return {}
    root = _read_xml(archive, "[Content_Types].xml")
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
            result[normalized] = defaults.get(normalized.rsplit(".", 1)[-1].lower(), "application/octet-stream") if "." in normalized else "application/octet-stream"
    return result


def _relationship_source(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return "[package]"
    if "/_rels/" not in rels_name:
        return "[package]"
    directory, rel_name = rels_name.split("/_rels/", 1)
    return f"{directory}/{rel_name[:-5]}" if rel_name.endswith(".rels") else f"{directory}/{rel_name}"


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
        names = validate_zip_members(archive, limits)
        if "word/document.xml" not in names:
            raise AdapterError("DOCX package lacks word/document.xml")
        root = _read_xml(archive, "word/document.xml")
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


def _style_properties(style: ET.Element) -> dict[str, Any]:
    props: dict[str, Any] = {}
    rpr = next(iter(_children(style, "rPr")), None)
    ppr = next(iter(_children(style, "pPr")), None)
    if rpr is not None:
        fonts = next(iter(_children(rpr, "rFonts")), None)
        color = next(iter(_children(rpr, "color")), None)
        size = next(iter(_children(rpr, "sz")), None)
        if fonts is not None and _wattr(fonts, "ascii"):
            props["fontFamily"] = _wattr(fonts, "ascii")
        if color is not None and _wattr(color, "val") and _wattr(color, "val") != "auto":
            value = _wattr(color, "val")
            if len(value) == 6:
                props["foreground"] = {"kind": "rgb", "r": int(value[0:2], 16), "g": int(value[2:4], 16), "b": int(value[4:6], 16), "a": 1}
        if size is not None and _wattr(size, "val"):
            props["fontSize"] = {"value": decimal(int(_wattr(size, "val")) / 2), "unit": "pt"}
        if _children(rpr, "b"):
            props["weight"] = 700
        if _children(rpr, "i"):
            props["italic"] = True
        if _children(rpr, "u"):
            props["underline"] = _wattr(_children(rpr, "u")[0], "val", "single")
    if ppr is not None:
        alignment = next(iter(_children(ppr, "jc")), None)
        if alignment is not None and _wattr(alignment, "val") in {"left", "center", "right", "both"}:
            props["paragraphAlignment"] = "justify" if _wattr(alignment, "val") == "both" else _wattr(alignment, "val")
        spacing = next(iter(_children(ppr, "spacing")), None)
        if spacing is not None:
            props["spacing"] = {key: {"value": decimal(int(_wattr(spacing, key, "0")) / 20), "unit": "pt"} for key in ("before", "after") if _wattr(spacing, key)}
    return props


def _add_style(builder: DocumentBuilder, style: ET.Element, style_id: str, role: str) -> str:
    style_key = safe_id("style", f"docx-{style_id}")
    declaration = _style_properties(style)
    based = next(iter(_children(style, "basedOn")), None)
    based_id = safe_id("style", f"docx-{_wattr(based, 'val')}") if based is not None and _wattr(based, "val") else None
    builder.add_item("styles", {"styleId": style_key, "role": role if role in {"paragraph", "character", "table", "cell", "shape", "page", "resolved"} else "paragraph", "origin": "authored", "basedOn": based_id, "declaration": declaration, "authored": declaration, "status": "preserved"}, "styleId")
    resolved_id = safe_id("style", f"docx-resolved-{style_id}")
    builder.add_item("styles", {"styleId": resolved_id, "role": "resolved", "origin": "resolved", "resolvedFrom": [style_key] + ([based_id] if based_id else []), "declaration": declaration, "resolved": declaration, "status": "preserved"}, "styleId")
    return style_key


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
) -> None:
    cache: dict[str, tuple[dict[str, Any], dict[str, str], list[str], list[dict[str, Any]]]] = {}

    def resolve(style_id: str, visiting: set[str] | None = None) -> tuple[dict[str, Any], dict[str, str], list[str], list[dict[str, Any]]]:
        if style_id in cache:
            return cache[style_id]
        visiting = set() if visiting is None else visiting
        if style_id in visiting:
            return {}, {}, [style_id], []
        visiting.add(style_id)
        merged: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        chain: list[str] = []
        trace: list[dict[str, Any]] = []
        based_id = graph.get(style_id)
        if based_id and based_id in graph:
            base_values, base_provenance, base_chain, base_trace = resolve(based_id, visiting)
            merged.update(base_values)
            provenance.update(base_provenance)
            chain.extend(base_chain)
            trace.extend(base_trace)
            for property_name in base_values:
                trace.append({"property": property_name, "source": styles[based_id][0], "action": "inherit"})
        authored = declarations.get(style_id, {})
        merged.update(authored)
        for property_name in authored:
            provenance[property_name] = styles[style_id][0]
            trace.append({"property": property_name, "source": styles[style_id][0], "action": "direct"})
        chain.append(style_id)
        visiting.remove(style_id)
        result = (merged, provenance, chain, trace)
        cache[style_id] = result
        return result

    for style_id, (_, resolved_id) in styles.items():
        resolved_values, provenance, chain, trace = resolve(style_id)
        resolved_item = builder.find("styles", "styleId", resolved_id)
        if resolved_item is None:
            continue
        resolved_item["resolved"] = resolved_values
        resolved_item["declaration"] = resolved_values
        resolved_item["resolvedFrom"] = [styles[item][0] for item in chain if item in styles]
        resolved_item["cascadeTrace"] = trace
        resolved_item["propertyProvenance"] = [{"property": name, "source": source, "status": "normalized"} for name, source in sorted(provenance.items())]
        resolved_item["status"] = "normalized"


def _extension(builder: DocumentBuilder, target_id: str, extension_type: str, payload: dict[str, Any], *, criticality: str = "non-critical") -> None:
    extension_id = safe_id("extension", f"docx-{extension_type}-{len(builder.document['extensions'])}")
    builder.add_item("extensions", {"extensionId": extension_id, "targetId": target_id, "namespace": "urn:fdir:format:docx", "type": extension_type, "schemaVersion": "1.0.0", "schemaId": f"urn:fdir:schema:docx-{extension_type}", "payload": payload, "criticality": criticality}, "extensionId")


def _add_text_run(
    builder: DocumentBuilder,
    parent_id: str,
    run: ET.Element,
    line_index: int,
    styles: dict[str, tuple[str, str]],
    *,
    part_name: str = "word/document.xml",
) -> str:
    run_id = safe_id("node", f"docx-run-{line_index}-{len(builder.document['nodes'])}")
    style_ref: dict[str, Any] = {}
    rpr = next(iter(_children(run, "rPr")), None)
    if rpr is not None:
        direct = safe_id("style", f"docx-direct-{line_index}-{len(builder.document['styles'])}")
        declaration = _style_properties(run)
        builder.add_item("styles", {"styleId": direct, "role": "character", "origin": "direct", "direct": declaration, "declaration": declaration, "status": "preserved"}, "styleId")
        style_ref = {"styleIds": [direct], "directStyleId": direct, "resolvedStyleId": direct}
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


def _geometry(builder: DocumentBuilder, target_id: str, extent: ET.Element | None, kind: str = "rectangle") -> str:
    geometry_id = safe_id("geometry", f"docx-{target_id}")
    cx = decimal(int(_attr(extent, "cx", "0")) if extent is not None else 0)
    cy = decimal(int(_attr(extent, "cy", "0")) if extent is not None else 0)
    space_id = "space-docx-page"
    if builder.find("coordinateSpaces", "coordinateSpaceId", space_id) is None:
        builder.add_item("coordinateSpaces", {"coordinateSpaceId": space_id, "unit": "emu", "origin": {"x": "0", "y": "0"}}, "coordinateSpaceId")
    builder.add_item("geometries", {"geometryId": geometry_id, "spaceId": space_id, "kind": kind if kind in {"rectangle", "rotatedRectangle", "bezier", "clippingPath"} else "rectangle", "primitives": [{"kind": "rectangle", "x": "0", "y": "0", "width": {"value": cx, "unit": "emu"}, "height": {"value": cy, "unit": "emu"}}], "status": "preserved"}, "geometryId")
    return geometry_id


def _drawing(builder: DocumentBuilder, parent_id: str, drawing: ET.Element, line_index: int) -> None:
    extent = next(iter(_descendants(drawing, "extent")), None)
    if _descendants(drawing, "cxnSp"):
        kind = "connector"
    elif _descendants(drawing, "txbx") or _descendants(drawing, "wsp"):
        kind = "textBox" if _descendants(drawing, "txbx") else "shape"
    elif _descendants(drawing, "pic"):
        kind = "image"
    else:
        kind = "shape"
    node_id = safe_id("node", f"docx-{kind}-{line_index}-{len(builder.document['nodes'])}")
    geometry_id = _geometry(builder, node_id, extent, "rectangle")
    builder.add_node(kind, node_id, parent_id=parent_id, geometryId=geometry_id, status="preserved")
    builder.add_item("layouts", {"layoutId": safe_id("layout", f"docx-{node_id}"), "targetId": node_id, "placement": "inline", "anchor": {"kind": "inline", "nodeId": parent_id}, "declaredGeometryId": geometry_id, "zIndex": line_index, "status": "preserved"}, "layoutId")
    for connector in _descendants(drawing, "cxnSp"):
        for endpoint, endpoint_kind in (("stCxn", "start"), ("endCxn", "to")):
            item = next(iter(_descendants(connector, endpoint)), None)
            if item is not None:
                target = _wattr(item, "id", "unknown")
                relation_id = safe_id("relation", f"docx-connector-{node_id}-{endpoint_kind}")
                target_node_id = safe_id("node", target)
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
                builder.add_item("relations", {"relationId": relation_id, "kind": "connectorTarget", "fromId": node_id, "toId": target_node_id, "endpoint": endpoint_kind, "status": "ambiguous"}, "relationId")
    for text_box in _descendants(drawing, "txbx"):
        for paragraph in _descendants(text_box, "p"):
            for run in _descendants(paragraph, "r"):
                _add_text_run(builder, node_id, run, line_index, {})
    _extension(builder, node_id, "drawing", {"kind": kind, "anchor": "inline", "extentEmu": {"cx": _attr(extent, "cx") if extent is not None else "0", "cy": _attr(extent, "cy") if extent is not None else "0"}})


def _parse_story_part(
    builder: DocumentBuilder,
    archive: zipfile.ZipFile,
    part_name: str,
    part_id: str,
    styles: dict[str, tuple[str, str]],
) -> bool:
    """Parse header/footer story roots into the same typed node graph.

    A story is a real OOXML text surface, not merely package metadata.  The
    bounded parser accepts paragraphs and runs (including revision wrappers)
    and records any other element as an explicit residual on the story part.
    """

    root = _read_xml(archive, part_name)
    story_candidates = _children(root, "hdr") or _children(root, "ftr")
    story_root = story_candidates[0] if story_candidates else root
    story_part = builder.find("parts", "partId", part_id)
    if story_part is None:
        raise AdapterError(f"story part was not registered: {part_name}")
    root_node_ids: list[str] = []
    unsupported = False
    paragraph_number = 0
    for child in list(story_root):
        local = _local(child.tag)
        if local != "p":
            if local == "tbl":
                diagnostic = builder.add_diagnostic(
                    "DFIR-DOCX-STORY-TABLE-UNSUPPORTED",
                    f"DOCX story table is retained only as a package part: {part_name}",
                    target_id=part_id,
                    phase="normalize",
                )
                builder.add_feature("story-table", "unsupported", target_id=part_id, diagnostic_ids=[diagnostic])
            else:
                diagnostic = builder.add_diagnostic(
                    "DFIR-DOCX-STORY-ELEMENT-UNSUPPORTED",
                    f"unsupported story element {local} in {part_name}",
                    target_id=part_id,
                    phase="normalize",
                )
                builder.add_feature("story-element", "unsupported", target_id=part_id, diagnostic_ids=[diagnostic])
            unsupported = True
            continue
        paragraph_number += 1
        paragraph_id = safe_id("node", f"docx-story-paragraph-{part_name}-{paragraph_number}")
        builder.add_node("paragraph", paragraph_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
        builder.add_source_map(paragraph_id, {"part": part_name, "path": f"story/p[{paragraph_number}]"})
        root_node_ids.append(paragraph_id)
        for item in list(child):
            item_local = _local(item.tag)
            if item_local in {"r", "ins", "del"}:
                runs = [item] if item_local == "r" else _children(item, "r")
                for run in runs:
                    run_id = _add_text_run(builder, paragraph_id, run, paragraph_number, styles, part_name=part_name)
                    if item_local in {"ins", "del"}:
                        revision_kind = "insert" if item_local == "ins" else "delete"
                        _extension(builder, run_id, "revision", {"kind": revision_kind, "author": _wattr(item, "author"), "revisionId": _wattr(item, "id")})
                        builder.add_feature("revision", "preserved", target_id=run_id)
            elif item_local == "hyperlink":
                annotation_id = safe_id("annotation", f"docx-story-hyperlink-{part_name}-{paragraph_number}-{len(builder.document['annotations'])}")
                builder.add_item("annotations", {"annotationId": annotation_id, "kind": "hyperlink", "targetIds": [paragraph_id], "body": _wattr(item, "anchor") or _wattr(item, "id", ""), "status": "preserved"}, "annotationId")
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


def convert(path: Path, *, limits: AdapterLimits | None = None) -> dict[str, Any]:
    path = Path(path)
    limits = input_limit_check(path, limits)
    builder = DocumentBuilder(path, "docx", "ECMA-376", limits=limits)
    try:
        with zipfile.ZipFile(path) as archive:
            names = validate_zip_members(archive, limits)
            if len(names) > limits.max_xml_parts:
                diagnostic = builder.add_diagnostic("DFIR-DOCX-PACKAGE-LIMIT", f"package has {len(names)} parts; limit is {limits.max_xml_parts}", severity="error", phase="parse")
                builder.add_feature("package-validation", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            if "word/document.xml" not in names:
                diagnostic = builder.add_diagnostic("DFIR-DOCX-DOCUMENT-MISSING", "DOCX package lacks word/document.xml", severity="error", phase="parse")
                builder.add_feature("document", "failed", diagnostic_ids=[diagnostic])
                return builder.finish(status="failed")
            root = _read_xml(archive, "word/document.xml")
            styles: dict[str, tuple[str, str]] = {}
            if "word/styles.xml" in names:
                style_root = _read_xml(archive, "word/styles.xml")
                style_graph: dict[str, str | None] = {}
                style_declarations: dict[str, dict[str, Any]] = {}
                for style in _children(style_root, "style"):
                    style_id = _wattr(style, "styleId")
                    if style_id:
                        based = next(iter(_children(style, "basedOn")), None)
                        style_graph[style_id] = _wattr(based, "val") if based is not None and _wattr(based, "val") else None
                        style_declarations[style_id] = _style_properties(style)
                        authored = _add_style(builder, style, style_id, "paragraph" if _wattr(style, "type") == "paragraph" else "character")
                        styles[style_id] = (authored, safe_id("style", f"docx-resolved-{style_id}"))
                if _style_cycle(style_graph):
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-STYLE-CYCLE", "style basedOn inheritance contains a cycle", severity="error", phase="validate")
                    builder.add_feature("style-inheritance", "failed", diagnostic_ids=[diagnostic])
                else:
                    _resolve_styles(builder, style_graph, style_declarations, styles)
            content_types = _content_types(archive)
            package_part_id = safe_id("part", "docx-package")
            builder.add_item("parts", {"partId": package_part_id, "kind": "package", "name": "OOXML package", "contentType": "application/vnd.openxmlformats-package", "rootNodeIds": [builder.root_id], "status": "preserved"}, "partId")
            part_id = safe_id("part", "docx-document")
            surface_id = safe_id("surface", "docx-page-1")
            builder.add_item("parts", {"partId": part_id, "kind": "document", "name": "word/document.xml", "contentType": content_types.get("word/document.xml", "application/xml"), "parentPartId": package_part_id, "rootNodeIds": [builder.root_id], "surfaceIds": [surface_id], "relationshipIds": [], "status": "preserved"}, "partId")
            part_ids: dict[str, str] = {"[package]": package_part_id, "word/document.xml": part_id}
            parsed_part_names = {"word/document.xml", "word/styles.xml", "word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"}
            for package_name in sorted(names):
                normalized_name = package_name.replace("\\", "/")
                if normalized_name in {"[Content_Types].xml", "word/document.xml"} or normalized_name.endswith(".rels"):
                    continue
                package_part_id_for_name = safe_id("part", f"docx-{normalized_name}")
                part_ids[normalized_name] = package_part_id_for_name
                suffix = normalized_name.rsplit(".", 1)[-1].lower() if "." in normalized_name else ""
                kind = "image" if normalized_name.startswith("word/media/") else "xml" if suffix == "xml" else "embeddedObject"
                if re.fullmatch(r"word/(header|footer)\d+\.xml", normalized_name):
                    parsed_part_names.add(normalized_name)
                part_status = "preserved" if normalized_name in parsed_part_names or normalized_name.startswith("word/media/") else "unsupported"
                builder.add_item("parts", {"partId": package_part_id_for_name, "kind": kind, "name": normalized_name, "contentType": content_types.get(normalized_name, "application/octet-stream"), "parentPartId": package_part_id, "rootNodeIds": [], "relationshipIds": [], "status": part_status}, "partId")
                if part_status == "unsupported":
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-PART-UNPARSED", f"DOCX package part is inventory-visible but not parsed by the bounded adapter: {normalized_name}", target_id=package_part_id_for_name, phase="normalize")
                    builder.add_feature("package-part", "unsupported", target_id=package_part_id_for_name, diagnostic_ids=[diagnostic])
            relationship_loss = False
            for relationship_file in sorted(name for name in names if name == "_rels/.rels" or "/_rels/" in name):
                source_name = _relationship_source(relationship_file)
                source_part_id = part_ids.get(source_name)
                if source_part_id is None:
                    source_part_id = safe_id("part", f"docx-missing-source-{source_name}")
                    part_ids[source_name] = source_part_id
                    builder.add_item("parts", {"partId": source_part_id, "kind": "unknown", "name": source_name, "external": True, "rootNodeIds": [], "relationshipIds": [], "status": "unavailable"}, "partId")
                source_part = builder.find("parts", "partId", source_part_id)
                for relationship_key, (target, relationship_type, target_mode) in _rels(archive, relationship_file).items():
                    relation_id = safe_id("relation", f"docx-{relationship_file}-{relationship_key}")
                    if target_mode == "External":
                        target_id = safe_id("resource", f"docx-external-{target}")
                        if builder.find("resources", "resourceId", target_id) is None:
                            builder.add_item("resources", {"resourceId": target_id, "kind": "linkedObject", "mediaType": "application/octet-stream", "availability": "unavailable", "externalTarget": target, "sourceRelationshipId": relation_id}, "resourceId")
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
                                builder.add_item("resources", {"resourceId": target_id, "kind": "linkedObject", "mediaType": "application/octet-stream", "availability": "unavailable", "externalTarget": target_name, "sourceRelationshipId": relation_id}, "resourceId")
                            diagnostic = builder.add_diagnostic("DFIR-DOCX-RELATION-TARGET-MISSING", f"relationship target is missing from the package: {target_name}", target_id=source_part_id, phase="parse", related_ids=[relation_id])
                            builder.add_feature("package-relationship", "ambiguous", target_id=source_part_id, diagnostic_ids=[diagnostic])
                            relation_status = "unavailable"
                            relationship_loss = True
                    builder.add_item("relations", {"relationId": relation_id, "kind": relation_kind, "fromId": source_part_id, "toId": target_id, "status": relation_status}, "relationId")
                    if source_part is not None:
                        source_part.setdefault("relationshipIds", []).append(relation_id)
            if not relationship_loss:
                builder.add_feature("package-relationships", "preserved", target_id=builder.root_id)
            for package_part in list(builder.document.get("parts", [])):
                part_name = str(package_part.get("name", ""))
                if re.fullmatch(r"word/(header|footer)\d+\.xml", part_name):
                    _parse_story_part(builder, archive, part_name, str(package_part["partId"]), styles)
            builder.add_item("surfaces", {"surfaceId": surface_id, "partId": part_id, "kind": "page", "ordinal": 0, "status": "preserved"}, "surfaceId")
            body = next(iter(_children(root, "body")), root)
            table_entities: list[dict[str, Any]] = []
            paragraph_number = 0
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
                    if style_name in styles:
                        refs = {"styleIds": [styles[style_name][0]], "directStyleId": styles[style_name][0], "resolvedStyleId": styles[style_name][1]}
                    builder.add_node(kind, paragraph_id, parent_id=builder.root_id, part_id=part_id, status="preserved", **refs)
                    builder.add_source_map(paragraph_id, {"part": "word/document.xml", "path": f"body/p[{paragraph_number}]"})
                    num_pr = next(iter(_children(ppr, "numPr")), None) if ppr is not None else None
                    if num_pr is not None:
                        _extension(builder, paragraph_id, "numbering", {"numId": _wattr(next(iter(_children(num_pr, "numId")), None), "val"), "ilvl": _wattr(next(iter(_children(num_pr, "ilvl")), None), "val")})
                    for item in list(child):
                        item_local = _local(item.tag)
                        if item_local in {"r", "ins", "del"}:
                            runs = [item] if item_local == "r" else _children(item, "r")
                            for run in runs:
                                run_id = _add_text_run(builder, paragraph_id, run, paragraph_number, styles)
                                if item_local == "ins":
                                    _extension(builder, run_id, "revision", {"kind": "insert", "author": _wattr(item, "author"), "revisionId": _wattr(item, "id")})
                                    builder.add_feature("revision", "preserved", target_id=run_id)
                                elif item_local == "del":
                                    _extension(builder, run_id, "revision", {"kind": "delete", "author": _wattr(item, "author"), "revisionId": _wattr(item, "id")})
                                    builder.add_feature("revision", "preserved", target_id=run_id)
                        elif item_local == "fldSimple":
                            field_id = safe_id("field", f"docx-field-{paragraph_number}-{len(builder.document['fields'])}")
                            instruction = _wattr(item, "instr") or _attr(item, "instr")
                            result = "".join(text.text or "" for text in item.iter() if _local(text.tag) in {"t", "instrText"})
                            builder.add_item("fields", {"fieldId": field_id, "ownerNodeId": paragraph_id, "kind": "simple", "instruction": instruction.strip(), "displayedResult": result, "status": "preserved"}, "fieldId")
                            field_node = safe_id("node", f"docx-field-node-{paragraph_number}")
                            builder.add_node("field", field_node, parent_id=paragraph_id, fieldId=field_id, status="preserved")
                        elif item_local == "drawing":
                            _drawing(builder, paragraph_id, item, paragraph_number)
                        elif item_local in {"commentRangeStart", "commentRangeEnd", "commentReference", "footnoteReference", "endnoteReference", "hyperlink"}:
                            annotation_id = safe_id("annotation", f"docx-{item_local}-{paragraph_number}-{len(builder.document['annotations'])}")
                            kind_name = {"commentReference": "comment", "footnoteReference": "footnote", "endnoteReference": "endnote", "hyperlink": "hyperlink"}.get(item_local, "bookmark")
                            builder.add_item("annotations", {"annotationId": annotation_id, "kind": kind_name, "targetIds": [paragraph_id], "body": _wattr(item, "id") or _wattr(item, "anchor") or _wattr(item, "id", ""), "status": "preserved"}, "annotationId")
                    builder.add_feature("paragraph", "preserved", target_id=paragraph_id)
                elif local == "tbl":
                    table_number = len(table_entities) + 1
                    table_id = safe_id("node", f"docx-table-{table_number}")
                    builder.add_node("table", table_id, parent_id=builder.root_id, part_id=part_id, status="preserved")
                    row_ids: list[str] = []
                    cell_ids: list[str] = []
                    column_count = 0
                    for row_number, row in enumerate(_children(child, "tr"), start=1):
                        row_id = safe_id("node", f"docx-table-{table_number}-row-{row_number}")
                        builder.add_node("row", row_id, parent_id=table_id, status="preserved")
                        row_ids.append(row_id)
                        cells = _children(row, "tc")
                        column_count = max(column_count, len(cells))
                        for column_number, cell in enumerate(cells, start=1):
                            cell_id = safe_id("node", f"docx-table-{table_number}-cell-{row_number}-{column_number}")
                            builder.add_node("cell", cell_id, parent_id=row_id, status="preserved", address={"row": row_number, "column": column_number})
                            cell_ids.append(cell_id)
                            for paragraph in _children(cell, "p"):
                                for run in _children(paragraph, "r"):
                                    _add_text_run(builder, cell_id, run, paragraph_number + row_number, styles)
                    column_ids: list[str] = []
                    for column_number in range(1, column_count + 1):
                        column_id = safe_id("node", f"docx-table-{table_number}-column-{column_number}")
                        builder.add_node("column", column_id, parent_id=table_id, status="preserved")
                        column_ids.append(column_id)
                    builder.add_item("tables", {"tableId": safe_id("table", f"docx-{table_number}"), "nodeId": table_id, "rowIds": row_ids, "columnIds": column_ids, "cellIds": cell_ids, "status": "preserved"}, "tableId")
                    builder.add_feature("table", "preserved", target_id=table_id)
                elif local not in {"sectPr"}:
                    diagnostic = builder.add_diagnostic("DFIR-DOCX-ELEMENT-UNSUPPORTED", f"unsupported body element: {local}", target_id=builder.root_id)
                    builder.add_feature(local, "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
            for comment_part in ("word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"):
                if comment_part in names:
                    part_root = _read_xml(archive, comment_part)
                    for item in _children(part_root, "comment") + _children(part_root, "footnote") + _children(part_root, "endnote"):
                        identifier = _wattr(item, "id")
                        body_text = "".join(text.text or "" for text in item.iter() if _local(text.tag) in {"t", "delText"})
                        _extension(builder, builder.root_id, "annotation-body", {"part": comment_part, "id": identifier, "body": body_text})
            for media in sorted(name for name in names if name.startswith("word/media/")):
                resource_id = safe_id("resource", media)
                builder.add_item("resources", {"resourceId": resource_id, "kind": "image", "mediaType": "application/octet-stream", "availability": "available", "derivedHandle": media}, "resourceId")
            builder.add_item("orders", {"orderId": safe_id("order", "docx-structure"), "kind": "structure", "ownerId": builder.root_id, "items": [{"id": node["nodeId"], "ordinal": index} for index, node in enumerate(builder.document["nodes"][1:])], "status": "preserved"}, "orderId")
            builder.add_feature("document", "preserved", target_id=builder.root_id)
            return builder.finish()
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError, AdapterError) as exc:
        diagnostic = builder.add_diagnostic("DFIR-DOCX-PARSE-FAILED", str(exc), severity="error", phase="parse", target_id=builder.root_id)
        builder.add_feature("document", "failed", target_id=builder.root_id, diagnostic_ids=[diagnostic])
        return builder.finish(status="failed")
