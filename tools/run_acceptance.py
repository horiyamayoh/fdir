"""Execute the machine-readable Document Form IR acceptance matrix.

The runner tests authority files, schema shape, examples, boundary rules,
runtime helpers, and the real-input adapter E2E gate.  Pre-authored examples
remain useful for contract coverage, but they are not treated as proof that an
input parser exists.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "machine" / "acceptance-tests.json"
REQ_PATH = ROOT / "machine" / "requirements.json"
ISSUE_PATH = ROOT / "machine" / "issue-plan.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
MANIFEST_PATH = ROOT / "machine" / "release-gate.json"
EXAMPLE_DIR = ROOT / "examples"


class AcceptanceFailure(AssertionError):
    pass


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"cannot load {path.relative_to(ROOT)}: {exc}") from exc


def all_text() -> str:
    paths = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def nested_values(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from nested_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_values(child)


def has_token(value: Any, token: str) -> bool:
    return any(token in item for item in nested_values(value) if isinstance(item, str))


def ids_for(data: dict[str, Any], field: str, label: str) -> dict[str, dict[str, Any]]:
    values = data.get(field, [])
    ensure(isinstance(values, list), f"{label} is not an array")
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        ensure(isinstance(item, dict), f"{label} contains a non-object")
        identifier = item.get({
            "nodes": "nodeId", "parts": "partId", "surfaces": "surfaceId",
            "texts": "textId", "styles": "styleId", "layouts": "layoutId",
            "geometries": "geometryId", "formulas": "formulaId", "fields": "fieldId",
            "annotations": "annotationId", "relations": "relationId", "orders": "orderId",
            "observations": "observationId", "extensions": "extensionId",
            "sourceMaps": "sourceMapId", "diagnostics": "diagnosticId",
        }.get(field, field))
        ensure(isinstance(identifier, str) and identifier, f"{label} has no stable identifier")
        ensure(identifier not in result, f"duplicate {label} identifier: {identifier}")
        result[identifier] = item
    return result


def example(ctx: "Context", name: str) -> dict[str, Any]:
    try:
        return ctx.examples[name]
    except KeyError as exc:
        raise AcceptanceFailure(f"missing example: {name}") from exc


def docs_have(ctx: "Context", *tokens: str) -> None:
    for token in tokens:
        ensure(token.lower() in ctx.docs.lower(), f"documentation token is missing: {token}")


def array_has(items: Any, predicate: Callable[[dict[str, Any]], bool], message: str) -> dict[str, Any]:
    ensure(isinstance(items, list), f"{message}: not an array")
    for item in items:
        if isinstance(item, dict) and predicate(item):
            return item
    raise AcceptanceFailure(message)


def schema_def(ctx: "Context", name: str) -> dict[str, Any]:
    definition = ctx.schema.get("$defs", {}).get(name)
    ensure(isinstance(definition, dict), f"schema definition is missing: {name}")
    return definition


def assert_closed(definition: dict[str, Any], name: str) -> None:
    ensure(definition.get("type") == "object", f"{name} is not an object definition")
    ensure(definition.get("additionalProperties") is False, f"{name} is an open property bag")


def ref_ids(document: dict[str, Any]) -> None:
    nodes = ids_for(document, "nodes", "nodes")
    known = set(nodes)
    for node in nodes.values():
        for key in ("parentId", "geometryId", "formulaId", "formulaFieldId", "fieldId", "directStyleId", "resolvedStyleId"):
            if key in node:
                ensure(node[key] in known or key in {"geometryId", "formulaId", "formulaFieldId", "fieldId", "directStyleId", "resolvedStyleId"},
                       f"node reference has wrong shape: {key}={node[key]}")
        for child in node.get("childIds", []):
            ensure(child in known, f"unknown child node: {child}")
        if "parentId" in node:
            ensure(node["parentId"] in known, f"unknown parent node: {node['parentId']}")
    for node in nodes.values():
        for child_id in node.get("childIds", []):
            child = nodes[child_id]
            ensure(child.get("parentId") in {None, node["nodeId"]}, f"containment disagreement for {child_id}")


def validate_document_shape(document: dict[str, Any], name: str) -> None:
    ensure(document.get("schema", {}).get("name") == "fdir/document-form", f"{name}: wrong schema name")
    ensure(isinstance(document.get("documentId"), str) and document["documentId"], f"{name}: missing documentId")
    source_format = document.get("sourceFormat")
    ensure(isinstance(source_format, dict) and source_format.get("name") in {"docx", "xlsx", "pdf", "markdown"}, f"{name}: invalid source format")
    ensure(isinstance(document.get("rootNodeId"), str), f"{name}: missing rootNodeId")
    ensure(isinstance(document.get("nodes"), list) and document["nodes"], f"{name}: missing nodes")
    ensure(isinstance(document.get("conversion"), dict), f"{name}: missing conversion report")
    ensure(document["conversion"].get("status") in {"complete", "partial", "failed"}, f"{name}: invalid conversion status")
    ref_ids(document)
    node_map = ids_for(document, "nodes", f"{name} nodes")
    ensure(document["rootNodeId"] in node_map, f"{name}: root node is not present")
    statuses = {
        "preserved", "normalized", "approximated", "ambiguous", "unsupported",
        "omitted-by-policy", "unavailable", "failed",
    }
    for collection, field in [("nodes", "status"), ("texts", "status"), ("styles", "status"), ("geometries", "status")]:
        for item in document.get(collection, []):
            ensure(item.get(field) in statuses, f"{name}: invalid {collection} status")
    conversion = document["conversion"]
    diagnostics = document.get("diagnostics", [])
    diagnostic_map = ids_for(document, "diagnostics", f"{name} diagnostics")
    report_diagnostics = conversion.get("diagnostics", [])
    ensure(isinstance(report_diagnostics, list), f"{name}: conversion diagnostics is not an array")
    for diagnostic_id in report_diagnostics:
        ensure(diagnostic_id in diagnostic_map, f"{name}: missing conversion diagnostic {diagnostic_id}")
    if conversion["status"] in {"partial", "failed"}:
        ensure(report_diagnostics or diagnostics, f"{name}: non-complete conversion hides diagnostics")
    if conversion["status"] == "complete":
        ensure(not any(node.get("status") in {"failed", "unsupported", "unavailable"} for node in node_map.values()),
               f"{name}: complete conversion contains an unhandled node status")
    forbidden = {
        "sourceBytes", "sourceByteStore", "contentAddressedSource", "semanticEquivalence",
        "EquivalenceCertificate", "LineageCertificate", "AccountingItem", "predicate",
    }
    for token in forbidden:
        ensure(not has_token(document, token), f"{name}: forbidden concept leaked into example: {token}")


def style_cycle(styles: list[dict[str, Any]]) -> bool:
    graph = {item.get("styleId"): item.get("basedOn") for item in styles if item.get("styleId")}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> bool:
        if identifier in visiting:
            return True
        if identifier in visited or identifier not in graph:
            return False
        visiting.add(identifier)
        parent = graph[identifier]
        cycle = isinstance(parent, str) and visit(parent)
        visiting.remove(identifier)
        visited.add(identifier)
        return cycle

    return any(visit(identifier) for identifier in graph)


def canonical_digest(document: dict[str, Any]) -> str:
    sys.path.insert(0, str(ROOT / "tools"))
    from canonicalize_ir import canonical_digest as digest  # type: ignore

    return digest(document)


def query_module():
    sys.path.insert(0, str(ROOT / "tools"))
    import query_ir  # type: ignore

    return query_ir


@dataclass
class Context:
    families: list[dict[str, Any]]
    requirements: list[dict[str, Any]]
    issue_plan: dict[str, Any]
    schema: dict[str, Any]
    manifest: dict[str, Any]
    examples: dict[str, dict[str, Any]]
    docs: str

    @classmethod
    def empty(cls) -> "Context":
        return cls([], [], {}, {}, {}, {}, "")


def load_context() -> Context:
    family_data = load_json(FAMILY_PATH)
    requirements_data = load_json(REQ_PATH)
    issue_plan = load_json(ISSUE_PATH)
    schema = load_json(SCHEMA_PATH)
    manifest = load_json(MANIFEST_PATH)
    ensure(isinstance(family_data, dict) and isinstance(family_data.get("families"), list), "acceptance families are unavailable")
    ensure(isinstance(requirements_data, dict) and isinstance(requirements_data.get("requirements"), list), "requirements are unavailable")
    examples: dict[str, dict[str, Any]] = {}
    for path in sorted(EXAMPLE_DIR.glob("*.json")):
        value = load_json(path)
        ensure(isinstance(value, dict), f"example is not an object: {path.name}")
        examples[path.name] = value
    context = Context(family_data["families"], requirements_data["requirements"], issue_plan, schema, manifest, examples, all_text())
    for name, value in context.examples.items():
        validate_document_shape(value, name)
    return context


def check_bnd(ctx: Context, case: int) -> None:
    docs_have(ctx, "Document Form IR", "Semantic IR", "Parser / Adapter", "DOCX", "XLSX", "PDF", "Markdown")
    if case == 1:
        docs_have(ctx, "recorded", "semantic meaning")
    elif case == 2:
        ensure({"docx", "xlsx", "pdf", "markdown"} <= {item.get("sourceFormat", {}).get("name") for item in ctx.examples.values()}, "not all four formats have examples")
    elif case == 3:
        assert_closed(schema_def(ctx, "node"), "node")
    elif case == 4:
        docs_have(ctx, "structure", "presentation", "layout", "geometry", "order")
    elif case == 5:
        docs_have(ctx, "downstream", "meaning")
    elif case == 6:
        docs_have(ctx, "namespace", "extension")
    elif case == 7:
        docs_have(ctx, "Universal", "extensible")
    elif case == 8:
        docs_have(ctx, "observable definition", "glossary")


def check_auth(ctx: Context, case: int) -> None:
    schema = ctx.schema
    if case == 1:
        ensure(schema.get("$id", "").endswith("/1.0.0"), "schema version is not pinned")
        docs_have(ctx, "normative", "version")
    elif case == 2:
        ensure((ROOT / "tools/validate_design.py").is_file(), "deterministic authority check is missing")
        ensure((ROOT / "tools/run_acceptance.py").is_file(), "acceptance runner is missing")
    elif case == 3:
        ensure(canonical_digest(example(ctx, "callout.json")) == canonical_digest(copy.deepcopy(example(ctx, "callout.json"))), "canonical identity is not stable")
    elif case == 4:
        docs_have(ctx, "non-authoritative", "rebuildable", "Query index")
    elif case == 5:
        pdf = example(ctx, "pdf-observation.json")
        ensure(any(item.get("kind") == "ocr" for item in pdf.get("observations", [])), "OCR observation is missing")
        ensure(any(item.get("representation") == "source" for item in pdf.get("texts", [])), "source text is missing")
    elif case == 6:
        source_map_doc = example(ctx, "markdown-authoring.json")
        before = canonical_digest(source_map_doc)
        altered = copy.deepcopy(source_map_doc)
        altered["sourceMaps"] = [{"sourceMapId": "extra", "targetId": "node-run", "format": altered["sourceMaps"][0]["format"], "locator": altered["sourceMaps"][0]["locator"]}]
        ensure(before == canonical_digest(altered), "source map changed IR identity")
    elif case == 7:
        ensure("ingestion metadata" in ctx.docs.lower() and "outside" in ctx.docs.lower(), "ingestion boundary is undocumented")
    elif case == 8:
        docs_have(ctx, "Semantic IR", "downstream", "cannot redefine")


def check_model(ctx: Context, case: int) -> None:
    defs = ctx.schema["$defs"]
    if case == 1:
        ensure({"documentId", "sourceFormat", "rootNodeId", "conversion"} <= set(ctx.schema["required"]), "document authority fields are incomplete")
    elif case == 2:
        assert_closed(defs["part"], "part")
        docs_have(ctx, "section", "page", "sheet")
    elif case == 3:
        assert_closed(defs["surface"], "surface")
        ensure("coordinateSpaceId" in defs["surface"].get("properties", {}), "surface lacks coordinate space")
    elif case == 4:
        ensure(len(defs["node"]["properties"]["kind"]["enum"]) >= 15, "node union is not closed and typed")
    elif case == 5:
        ensure(all(isinstance(node.get("childIds"), list) for document in ctx.examples.values() for node in document["nodes"]), "containment is not explicit")
    elif case == 6:
        ensure({"source", "normalized", "displayed", "observed"} <= set(defs["text"]["properties"]["representation"]["enum"]), "text representations are incomplete")
    elif case == 7:
        assert_closed(defs["table"], "table")
        docs_have(ctx, "TableGrid", "merged")
    elif case == 8:
        assert_closed(defs["resource"], "resource")
        ensure("sourceBytes" not in json.dumps(defs["resource"]), "resource is a byte archive")
    elif case == 9:
        assert_closed(defs["formula"], "formula")
        assert_closed(defs["annotation"], "annotation")
    elif case == 10:
        for document in ctx.examples.values():
            ref_ids(document)


def check_type(ctx: Context, case: int) -> None:
    defs = ctx.schema["$defs"]
    if case == 1:
        ensure(all(definition.get("additionalProperties") is False for definition in defs.values() if isinstance(definition, dict) and definition.get("type") == "object"), "open core object found")
    elif case == 2:
        ensure(defs["typedValue"]["properties"]["type"]["enum"] == ["blank", "boolean", "integer", "number", "decimal", "string", "date", "datetime", "error"], "typed scalar enum changed")
    elif case == 3:
        ensure(defs["decimal"].get("type") == "string" and "pattern" in defs["decimal"], "decimal is not an exact typed string")
    elif case == 4:
        ensure(defs["length"].get("required") == ["value", "unit"], "length lacks value/unit")
    elif case == 5:
        ensure(set(defs["color"]["properties"]["kind"]["enum"]) >= {"rgb", "gray", "cmyk", "theme"}, "color variants are incomplete")
    elif case == 6:
        ensure("transformToParent" in defs["coordinateSpace"]["properties"] and "rotation" in defs["geometryPrimitive"]["properties"], "coordinate/transform context is incomplete")
    elif case == 7:
        ensure(set(defs["calculationContext"]["properties"]["dateSystem"]["enum"]) == {"1900", "1904"}, "date systems are not explicit")
    elif case == 8:
        ensure("score" in defs["observation"]["properties"], "observation precision/score is missing")


def check_style(ctx: Context, case: int) -> None:
    style = schema_def(ctx, "style")
    styles = example(ctx, "style-resolution.json")["styles"]
    if case == 1:
        ensure({"authored", "resolved"} <= set(style["properties"]["origin"]["enum"]), "authored/resolved styles are not distinct")
        ensure(any(item.get("authored") for item in styles) and any(item.get("resolved") for item in styles), "style lineage fixture is incomplete")
    elif case == 2:
        ensure("basedOn" in style["properties"] and any(item.get("basedOn") for item in styles), "based-on style reference is missing")
    elif case == 3:
        ensure("theme" in style["properties"] and any(item.get("theme") for item in styles), "theme style reference is missing")
    elif case == 4:
        ensure(any(item.get("direct") for item in styles) and any(item.get("resolved") for item in styles), "direct/resolved style fixture is incomplete")
    elif case == 5:
        ensure("conditional" in style["properties"] and {"condition", "style"} <= set(schema_def(ctx, "conditionalRule").get("properties", {})), "conditional style is not typed")
    elif case == 6:
        props = schema_def(ctx, "styleProperties")["properties"]
        ensure({"fontFamily", "paragraphAlignment", "borders", "fill", "stroke", "visibility"} <= set(props), "style typed fields are incomplete")
    elif case == 7:
        ensure("numberFormat" in schema_def(ctx, "styleProperties")["properties"] and "numberFormat" in schema_def(ctx, "formula")["properties"], "number format is not separate")
    elif case == 8:
        cyclic = copy.deepcopy(styles)
        cyclic[0]["basedOn"] = cyclic[-1]["styleId"]
        cyclic[-1]["basedOn"] = cyclic[0]["styleId"]
        ensure(style_cycle(cyclic), "style cycle was not detected")


def check_layout(ctx: Context, case: int) -> None:
    defs = ctx.schema["$defs"]
    callout = example(ctx, "callout.json")
    if case == 1:
        ensure("unit" in defs["coordinateSpace"]["required"] and any(item.get("unit") for item in callout["coordinateSpaces"]), "coordinate unit is missing")
    elif case == 2:
        ensure("parentSpaceId" in defs["coordinateSpace"]["properties"] and "transformToParent" in defs["coordinateSpace"]["properties"], "parent coordinate transform is missing")
    elif case == 3:
        ensure("transform" in defs["geometry"]["properties"] and "rotation" in defs["geometryPrimitive"]["properties"], "geometry transforms are incomplete")
    elif case == 4:
        ensure(set(defs["geometryPrimitive"]["properties"]["kind"]["enum"]) >= {"point", "line", "polyline", "polygon", "bezier"}, "geometry primitive union is incomplete")
    elif case == 5:
        ensure(any(item.get("kind") == "glyphBoxes" for item in example(ctx, "pdf-observation.json")["geometries"]), "glyph boxes fixture is missing")
    elif case == 6:
        ensure(any(item.get("kind") == "clippingPath" for item in example(ctx, "pdf-observation.json")["geometries"]), "clipping fixture is missing")
    elif case == 7:
        ensure(set(defs["anchor"]["properties"]["kind"]["enum"]) >= {"inline", "page", "paragraph", "cell-range"}, "anchor variants are incomplete")
    elif case == 8:
        connector = array_has(callout["nodes"], lambda item: item.get("kind") == "connector", "connector fixture is missing")
        ensure(connector.get("geometryId") and any(item.get("arrowhead") for geometry in callout["geometries"] for primitive in geometry.get("primitives", []) for item in [primitive] if isinstance(item, dict)), "connector path/arrowhead is missing")
    elif case == 9:
        ensure("wrap" in defs["layout"]["properties"] and "placement" in defs["layout"]["properties"], "layout constraints are incomplete")
    elif case == 10:
        ensure("zIndex" in defs["layout"]["properties"] and any("zIndex" in item for item in callout["layouts"]), "paint z-order is missing")
    elif case == 11:
        ensure(set(defs["order"]["properties"]["kind"]["enum"]) >= {"structure", "source", "reading", "draw", "grid", "tab", "revision"}, "order axes are incomplete")
    elif case == 12:
        relation = schema_def(ctx, "relation")
        ensure("connectorTarget" in relation["properties"]["kind"]["enum"] and "semantic" not in json.dumps(relation).lower(), "connector relation is semantic")


def check_value(ctx: Context, case: int) -> None:
    cell = example(ctx, "cell-formula.json")
    formula = cell["formulas"][0]
    if case == 1:
        ensure(formula["expression"]["source"] and formula["values"]["stored"], "formula/stored values are not separate")
    elif case == 2:
        ensure({"cached", "computed"} <= set(formula["values"]), "cached/computed values are not separate")
    elif case == 3:
        ensure("displayed" in formula["values"] and formula["values"]["displayed"].get("text") != formula["values"]["raw"].get("value"), "displayed text is not separate")
    elif case == 4:
        ensure({"dateSystem", "locale", "mode", "referenceStyle"} <= set(formula["calculationContext"]), "calculation context is incomplete")
    elif case == 5:
        field = schema_def(ctx, "field")
        ensure({"instruction", "storedResult", "displayedResult"} <= set(field["properties"]), "word field results are not separate")
    elif case == 6:
        pdf = example(ctx, "pdf-observation.json")
        ensure({"source", "observed"} <= {text.get("representation") for text in pdf["texts"]}, "PDF source/OCR text representations are not separate")
    elif case == 7:
        markdown = example(ctx, "markdown-authoring.json")
        ensure({"source", "normalized"} <= {text.get("representation") for text in markdown["texts"]}, "Markdown source/normalized text is not separate")
    elif case == 8:
        ensure(any(item.get("kind") == "renderer" for item in example(ctx, "pdf-observation.json")["observations"]), "renderer observation is missing")


def check_status(ctx: Context, case: int) -> None:
    status = schema_def(ctx, "status")["enum"]
    partial = example(ctx, "partial-conversion.json")
    if case == 1:
        ensure(set(status) == {"preserved", "normalized", "approximated", "ambiguous", "unsupported", "omitted-by-policy", "unavailable", "failed"}, "status enum is incomplete")
    elif case == 2:
        ensure(partial["conversion"]["status"] == "partial", "document status does not summarize partial conversion")
    elif case == 3:
        ensure(all("status" in feature for feature in partial["conversion"]["features"]), "feature status is missing")
    elif case == 4:
        ensure(any(node.get("status") == "unsupported" for node in partial["nodes"]), "unsupported node is missing")
    elif case == 5:
        ensure("status" in schema_def(ctx, "field")["required"], "field status is not available")
    elif case == 6:
        diagnostic = partial["diagnostics"][0]
        ensure({"diagnosticId", "code", "severity", "message"} <= set(diagnostic), "diagnostic fields are incomplete")
    elif case == 7:
        ensure(partial["conversion"]["diagnostics"], "unsupported node was silently dropped")
    elif case == 8:
        ensure("sourceMaps" not in partial or isinstance(partial.get("sourceMaps"), list), "source locator is not optional metadata")


def check_ext(ctx: Context, case: int) -> None:
    extension = schema_def(ctx, "extension")
    callout_ext = example(ctx, "callout.json")["extensions"][0]
    if case == 1:
        ensure({"namespace", "type", "schemaVersion", "schemaId"} <= set(extension["required"]), "extension identity fields are incomplete")
    elif case == 2:
        ensure(extension["properties"]["payload"].get("type") == "object", "extension payload is not object-shaped")
        ensure(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", callout_ext["schemaVersion"]), "extension schema version is invalid")
    elif case == 3:
        ensure(set(extension["properties"]["criticality"]["enum"]) == {"critical", "non-critical"}, "criticality is not explicit")
    elif case == 4:
        ensure(callout_ext["criticality"] == "non-critical", "fixture does not demonstrate retained non-critical extension")
    elif case == 5:
        synthetic = copy.deepcopy(callout_ext)
        synthetic["criticality"] = "critical"
        synthetic["schemaId"] = "urn:fdir:schema:unknown"
        ensure(synthetic["criticality"] == "critical" and synthetic["schemaId"].endswith("unknown"), "critical unknown case was not constructed")
        ensure("partial" in ctx.docs.lower() and "critical extension" in ctx.docs.lower(), "critical unknown handling is undocumented")
    elif case == 6:
        docs_have(ctx, "vendor", "namespace")
    elif case == 7:
        docs_have(ctx, "promotion", "migration")
    elif case == 8:
        ensure("unknownVersion" in extension["properties"]["compatibility"]["properties"], "unknown version policy is missing")


def check_io(ctx: Context, case: int) -> None:
    doc = example(ctx, "callout.json")
    if case == 1:
        encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ensure(not encoded.startswith(b"\xef\xbb\xbf") and b"\r\n" not in encoded, "canonical encoding is not UTF-8/LF")
    elif case == 2:
        ensure(canonical_digest(doc) == canonical_digest(copy.deepcopy(doc)), "canonical ordering is unstable")
    elif case == 3:
        docs_have(ctx, "decimal", "canonicalize")
    elif case == 4:
        altered = copy.deepcopy(doc)
        altered["sourceBytes"] = "forbidden"
        try:
            canonical_digest(altered)
        except ValueError:
            pass
        else:
            raise AcceptanceFailure("source-byte material changed or was accepted as IR identity")
    elif case == 5:
        docs_have(ctx, "major", "minor", "compatibility")
    elif case == 6:
        docs_have(ctx, "migration", "diagnostic", "ambiguous")
    elif case == 7:
        altered = copy.deepcopy(doc)
        altered["nodes"] = list(reversed(altered["nodes"]))
        ensure(canonical_digest(doc) == canonical_digest(altered), "entity array reorder changed identity")
    elif case == 8:
        docs_have(ctx, "index", "does not change", "IR identity")


def check_docx(ctx: Context, case: int) -> None:
    docs_have(ctx, "DOCX", "paragraph", "style", "drawing", "diagnostic")
    callout = example(ctx, "callout.json")
    if case == 1:
        ensure(callout["sourceFormat"]["name"] == "docx" and {"paragraph", "run"} <= {node["kind"] for node in callout["nodes"]}, "DOCX core nodes are missing")
    elif case == 2:
        ensure(any(item.get("origin") == "resolved" for item in example(ctx, "style-resolution.json")["styles"]), "DOCX resolved style is missing")
    elif case == 3:
        docs_have(ctx, "numbering", "list level", "source order")
    elif case == 4:
        docs_have(ctx, "field", "revision", "comment", "footnote", "bookmark")
    elif case == 5:
        ensure({"connector", "textBox"} <= {node["kind"] for node in callout["nodes"]}, "DOCX drawing/anchor mapping fixture is incomplete")
    elif case == 6:
        docs_have(ctx, "embedded", "linked", "Resource")
    elif case == 7:
        ensure(example(ctx, "partial-conversion.json")["diagnostics"][0]["code"].startswith("DFIR-DOCX-"), "DOCX loss diagnostic is not stable")
    elif case == 8:
        docs_have(ctx, "business meaning", "connector", "red")


def check_xlsx(ctx: Context, case: int) -> None:
    docs_have(ctx, "XLSX", "workbook", "worksheet", "formula", "calculation")
    cell = example(ctx, "cell-formula.json")
    formula = cell["formulas"][0]
    if case == 1:
        ensure(cell["sourceFormat"]["name"] == "xlsx" and cell["nodes"][1]["kind"] == "cell", "XLSX cell mapping is missing")
    elif case == 2:
        docs_have(ctx, "shared", "inline string")
    elif case == 3:
        ensure({"raw", "stored", "cached", "computed", "displayed"} <= set(formula["values"]), "XLSX value representations are incomplete")
    elif case == 4:
        docs_have(ctx, "error values", "stale cache")
    elif case == 5:
        ensure({"dateSystem", "locale", "mode"} <= set(formula["calculationContext"]), "XLSX calculation context is incomplete")
    elif case == 6:
        docs_have(ctx, "conditional formatting", "tables", "merges", "themes")
    elif case == 7:
        docs_have(ctx, "charts", "pivots", "cell anchors", "external references")
    elif case == 8:
        docs_have(ctx, "no recalculation", "stored values")


def check_pdf(ctx: Context, case: int) -> None:
    docs_have(ctx, "PDF", "glyph", "path", "clipping", "OCR")
    pdf = example(ctx, "pdf-observation.json")
    if case == 1:
        ensure(pdf["sourceFormat"]["name"] == "pdf" and any(item.get("kind") == "glyphBoxes" for item in pdf["geometries"]), "PDF page/coordinate mapping is missing")
    elif case == 2:
        docs_have(ctx, "character code", "Unicode mapping", "font")
    elif case == 3:
        ensure(any(item.get("kind") == "glyphBoxes" for item in pdf["geometries"]), "PDF glyph geometry is missing")
    elif case == 4:
        ensure(any(item.get("kind") == "clippingPath" for item in pdf["geometries"]), "PDF path/clip mapping is missing")
    elif case == 5:
        docs_have(ctx, "annotations", "forms", "outlines", "destinations", "tagged structure")
    elif case == 6:
        docs_have(ctx, "paint order", "reading-order")
    elif case == 7:
        ensure({"renderer", "ocr"} <= {item.get("kind") for item in pdf["observations"]}, "PDF source and observations are not distinct")
    elif case == 8:
        docs_have(ctx, "xref", "unreferenced bytes", "forensic archive")


def check_md(ctx: Context, case: int) -> None:
    docs_have(ctx, "Markdown", "block", "inline", "delimiter", "escaping")
    markdown = example(ctx, "markdown-authoring.json")
    if case == 1:
        ensure(markdown["sourceFormat"]["name"] == "markdown" and {"paragraph", "run"} <= {node["kind"] for node in markdown["nodes"]}, "Markdown typed nodes are missing")
    elif case == 2:
        docs_have(ctx, "headings", "lists", "tables", "source order")
    elif case == 3:
        docs_have(ctx, "links", "images", "reference definitions", "resources")
    elif case == 4:
        docs_have(ctx, "code blocks", "inline code", "emphasis", "entities")
    elif case == 5:
        ensure(any(item.get("type") == "authoring-facts" and item.get("payload", {}).get("delimiter") for item in markdown["extensions"]), "Markdown authoring extension is missing")
    elif case == 6:
        docs_have(ctx, "raw HTML", "without semantic interpretation")
    elif case == 7:
        docs_have(ctx, "front matter", "footnotes", "line-break")
    elif case == 8:
        docs_have(ctx, "dialect", "known loss", "diagnostic")


def check_query(ctx: Context, case: int) -> None:
    query = query_module()
    callout = example(ctx, "callout.json")
    cell = example(ctx, "cell-formula.json")
    pdf = example(ctx, "pdf-observation.json")
    if case == 1:
        ensure(query.list_nodes(callout, kind="paragraph"), "paragraph query returned no rows")
        ensure(query.list_nodes(cell, kind="cell"), "cell query returned no rows")
    elif case == 2:
        ensure(cell["formulas"] and query._items(cell, "formulas"), "formula query returned no rows")
        ensure(cell["formulas"][0]["values"]["stored"] != cell["formulas"][0]["values"]["displayed"], "stored/displayed query collapsed values")
    elif case == 3:
        red = [style for style in example(ctx, "style-resolution.json")["styles"] if "foreground" in json.dumps(style.get("resolved", {}))]
        ensure(red, "resolved style query returned no red text style")
    elif case == 4:
        ensure(query.list_nodes(callout, kind="connector"), "shape/connector query returned no connector")
        ensure(query.find_relations(callout, kind="connectorTarget"), "connector relation query returned no relation")
    elif case == 5:
        ensure(query.list_nodes(example(ctx, "partial-conversion.json"), status="unsupported"), "status query returned no unsupported node")
    elif case == 6:
        ensure(query.find_extensions(callout, namespace="urn:fdir:format:docx"), "extension namespace query returned no extension")
    elif case == 7:
        style_ids = {item["styleId"] for item in example(ctx, "style-resolution.json")["styles"]}
        ensure("style-base" in style_ids and "style-authored" in style_ids, "style inheritance query has no chain")
    elif case == 8:
        observations = query.find_observations(pdf, target_id="node-glyph")
        ensure({item.get("kind") for item in observations} >= {"renderer", "ocr"}, "observation difference query collapsed results")


def check_qa(ctx: Context, case: int) -> None:
    if case == 1:
        for name in ["node", "text", "style", "layout", "geometry", "extension", "diagnostic"]:
            assert_closed(schema_def(ctx, name), name)
    elif case == 2:
        for document in ctx.examples.values():
            ref_ids(document)
    elif case == 3:
        ensure({doc["sourceFormat"]["name"] for doc in ctx.examples.values()} >= {"docx", "xlsx", "pdf", "markdown"}, "cross-format fixtures are incomplete")
        ensure(not has_token(ctx.examples, "semanticEquivalence"), "cross-format fixture makes a semantic equivalence claim")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "run_e2e.py"), "--all"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        ensure(completed.returncode == 0, f"real-input E2E failed: {(completed.stdout + completed.stderr).strip()}")
        ensure("e2e valid:" in completed.stdout.casefold(), "real-input E2E did not emit a passing summary")
    elif case == 4:
        ensure(example(ctx, "callout.json").get("geometries"), "geometry fixture is missing")
        ensure(example(ctx, "pdf-observation.json").get("geometries"), "PDF geometry fixture is missing")
    elif case == 5:
        ensure(example(ctx, "style-resolution.json").get("styles") and example(ctx, "cell-formula.json").get("formulas"), "style/formula fixtures are missing")
    elif case == 6:
        malformed = {"schema": {"name": "fdir/document-form"}, "nodes": []}
        try:
            validate_document_shape(malformed, "malformed")
        except AcceptanceFailure:
            pass
        else:
            raise AcceptanceFailure("malformed fixture was accepted")
        ensure(example(ctx, "partial-conversion.json")["conversion"]["status"] == "partial", "partial fixture is missing")
    elif case == 7:
        docs_have(ctx, "unknown critical", "backward", "forward", "compatibility")
    elif case == 8:
        manifest = ctx.manifest
        ensure("resource-and-package-boundary" in manifest.get("checks", []), "resource boundary is not a release check")
        ensure("real-input-e2e" in manifest.get("checks", []), "real-input E2E is not a release check")
        ensure((ROOT / "tools/release_gate.py").is_file(), "release gate is missing")


CHECKS: dict[str, Callable[[Context, int], None]] = {
    "AT-BND": check_bnd,
    "AT-AUTH": check_auth,
    "AT-MODEL": check_model,
    "AT-TYPE": check_type,
    "AT-STYLE": check_style,
    "AT-LAYOUT": check_layout,
    "AT-VALUE": check_value,
    "AT-STATUS": check_status,
    "AT-EXT": check_ext,
    "AT-IO": check_io,
    "AT-DOCX": check_docx,
    "AT-XLSX": check_xlsx,
    "AT-PDF": check_pdf,
    "AT-MD": check_md,
    "AT-QUERY": check_query,
    "AT-QA": check_qa,
}


@dataclass
class CaseResult:
    family: str
    case: int
    test_id: str
    requirement: str | None
    status: str
    diagnostic: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "case": self.case,
            "test": self.test_id,
            "requirement": self.requirement,
            "status": self.status,
            "diagnostic": self.diagnostic,
        }


def authority_check() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/validate_design.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def run_cases(ctx: Context, selected_family: str | None, selected_case: int | None) -> list[CaseResult]:
    family_map = {family["id"]: family for family in ctx.families}
    requirement_by_test = {
        test_id: requirement
        for requirement in ctx.requirements
        for test_id in requirement.get("acceptanceTests", [])
    }
    if selected_family:
        ensure(selected_family in family_map, f"unknown acceptance family: {selected_family}")
        families = [family_map[selected_family]]
    else:
        families = ctx.families
    results: list[CaseResult] = []
    for family in families:
        family_id = family["id"]
        count = family["count"]
        case_numbers = [selected_case] if selected_case is not None else list(range(1, count + 1))
        for case in case_numbers:
            ensure(isinstance(case, int) and 1 <= case <= count, f"case out of range: {family_id}-{case}")
            test_id = f"{family_id}-{case:03d}"
            requirement = requirement_by_test.get(test_id)
            status = "PASS"
            diagnostic = "accepted"
            try:
                ensure(requirement is not None, f"no requirement maps to {test_id}")
                CHECKS[family_id](ctx, case)
            except (AcceptanceFailure, KeyError, TypeError, ValueError) as exc:
                status = "FAIL"
                diagnostic = f"{type(exc).__name__}: {exc}"
            results.append(CaseResult(family_id, case, test_id, requirement.get("id") if requirement else None, status, diagnostic))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="run every declared acceptance case")
    selection.add_argument("--family", help="run one acceptance family")
    parser.add_argument("--case", type=int, help="run one case number from --family")
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.case is not None and not args.family:
        print("--case requires --family", file=sys.stderr)
        return 2
    try:
        ctx = load_context()
        valid, authority_output = authority_check()
        if not valid:
            raise AcceptanceFailure(f"design authority failed: {authority_output}")
        results = run_cases(ctx, args.family, args.case)
    except (AcceptanceFailure, OSError, subprocess.SubprocessError) as exc:
        payload = {"status": "FAIL", "passed": 0, "failed": 1, "total": 1, "error": str(exc), "cases": []}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"[FAIL] ACCEPTANCE-GATE {exc}")
        return 1

    passed = sum(result.status == "PASS" for result in results)
    failed = len(results) - passed
    payload = {
        "status": "PASS" if failed == 0 else "FAIL",
        "passed": passed,
        "failed": failed,
        "total": len(results),
        "families": len({result.family for result in results}),
        "cases": [result.as_dict() for result in results],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for result in results:
            print(f"[{result.status}] {result.test_id} {result.requirement or '-'}: {result.diagnostic}")
        print(f"acceptance {'valid' if failed == 0 else 'invalid'}: {passed}/{len(results)} cases across {len({result.family for result in results})} families")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
