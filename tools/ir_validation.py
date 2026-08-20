"""Fail-closed schema, graph, status, and extension validation for FDIR."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


FORBIDDEN_KEYS = {
    "sourceBytes", "sourceByteStore", "contentAddressedSource", "semanticEquivalence",
    "EquivalenceCertificate", "LineageCertificate", "AccountingItem", "predicate",
}
COLLECTION_KEYS = {
    "parts": "partId", "surfaces": "surfaceId", "nodes": "nodeId", "texts": "textId",
    "tables": "tableId", "styles": "styleId", "layouts": "layoutId", "coordinateSpaces": "coordinateSpaceId",
    "geometries": "geometryId", "resources": "resourceId", "formulas": "formulaId", "fields": "fieldId",
    "annotations": "annotationId", "relations": "relationId", "orders": "orderId", "observations": "observationId",
    "extensions": "extensionId", "sourceMaps": "sourceMapId", "diagnostics": "diagnosticId",
}
STATUS_VALUES = {
    "preserved", "normalized", "approximated", "ambiguous", "unsupported", "omitted-by-policy", "unavailable", "failed",
}
PARTIAL_STATUSES = {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}
NODE_KINDS = {"document", "section", "paragraph", "run", "heading", "list", "table", "row", "column", "cell", "shape", "textBox", "connector", "image", "chart", "glyph", "path", "field", "annotation", "resource"}
VISUAL_NODE_KINDS = {"shape", "textBox", "connector", "image", "chart", "glyph", "path"}


class IRValidationError(ValueError):
    """Raised when a document is not a valid authoritative IR."""

    def __init__(self, message: str, code: str = "DFIR-IR-INVALID") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "document-form-ir.schema.json"
CAPABILITY_PROFILE_PATH = Path(__file__).resolve().parents[1] / "machine" / "capability-profile.json"


def _load_schema() -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IRValidationError(f"cannot load normative IR schema: {exc}", "DFIR-SCHEMA-LOAD") from exc
    if not isinstance(schema, dict):
        raise IRValidationError("normative IR schema root must be an object", "DFIR-SCHEMA-ROOT")
    return schema


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    if not reference.startswith("#/"):
        raise IRValidationError(f"unsupported schema reference: {reference}", "DFIR-SCHEMA-REF")
    current: Any = root
    for part in reference[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise IRValidationError(f"unresolved schema reference: {reference}", "DFIR-SCHEMA-REF")
        current = current[part]
    if not isinstance(current, dict):
        raise IRValidationError(f"schema reference is not an object: {reference}", "DFIR-SCHEMA-REF")
    return current


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_matches(value: Any, expected: str) -> bool:
    actual = _json_type(value)
    if expected == "number":
        return actual in {"integer", "number"}
    return actual == expected


def _condition_matches(value: Any, condition: dict[str, Any]) -> bool:
    """Evaluate the explicit discriminator conditions used by this schema."""

    if not isinstance(value, dict):
        return False
    properties = condition.get("properties")
    if not isinstance(properties, dict):
        return False
    for key, rule in properties.items():
        if key not in value or not isinstance(rule, dict):
            return False
        if "const" in rule and value[key] != rule["const"]:
            return False
        if "enum" in rule and value[key] not in rule["enum"]:
            return False
    required = condition.get("required", [])
    return isinstance(required, list) and all(key in value for key in required)


def _schema_matches(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> bool:
    try:
        _validate_schema(value, schema, root, path)
    except IRValidationError:
        return False
    return True


def _validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    """Validate the Draft 2020-12 subset used by the normative schema."""

    schema = _resolve_schema(schema, root)
    if "const" in schema and value != schema["const"]:
        raise IRValidationError(f"{path} does not equal schema const", "DFIR-SCHEMA-CONST")
    if "enum" in schema and value not in schema["enum"]:
        raise IRValidationError(f"{path} is not one of the allowed values", "DFIR-SCHEMA-ENUM")
    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(isinstance(item, str) and _type_matches(value, item) for item in expected_types):
            raise IRValidationError(f"{path} has type {_json_type(value)}, expected {expected}", "DFIR-SCHEMA-TYPE")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise IRValidationError(f"{path} is shorter than minLength {minimum}", "DFIR-SCHEMA-LENGTH")
        if isinstance(maximum, int) and len(value) > maximum:
            raise IRValidationError(f"{path} is longer than maxLength {maximum}", "DFIR-SCHEMA-LENGTH")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise IRValidationError(f"{path} does not match schema pattern", "DFIR-SCHEMA-PATTERN")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise IRValidationError(f"{path} is non-finite", "DFIR-SCHEMA-NONFINITE")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise IRValidationError(f"{path} is below minimum {minimum}", "DFIR-SCHEMA-RANGE")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise IRValidationError(f"{path} is above maximum {maximum}", "DFIR-SCHEMA-RANGE")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise IRValidationError(f"{path} has invalid schema properties", "DFIR-SCHEMA-PROPERTIES")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise IRValidationError(f"{path} has invalid schema required list", "DFIR-SCHEMA-REQUIRED")
        missing = [key for key in required if key not in value]
        if missing:
            raise IRValidationError(f"{path} is missing required fields: {', '.join(missing)}", "DFIR-SCHEMA-REQUIRED")
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            raise IRValidationError(f"{path} has too few properties", "DFIR-SCHEMA-PROPERTIES")
        if isinstance(maximum, int) and len(value) > maximum:
            raise IRValidationError(f"{path} has too many properties", "DFIR-SCHEMA-PROPERTIES")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                _validate_schema(child, properties[key], root, f"{path}.{key}")
            elif additional is False:
                raise IRValidationError(f"{path} contains unknown field: {key}", "DFIR-SCHEMA-CLOSED")
            elif isinstance(additional, dict):
                _validate_schema(child, additional, root, f"{path}.{key}")
        dependent = schema.get("dependentRequired", {})
        if isinstance(dependent, dict):
            for key, dependencies in dependent.items():
                if key in value and isinstance(dependencies, list):
                    missing_dependencies = [item for item in dependencies if item not in value]
                    if missing_dependencies:
                        raise IRValidationError(f"{path}.{key} requires {', '.join(missing_dependencies)}", "DFIR-SCHEMA-DEPENDENCY")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise IRValidationError(f"{path} has fewer than minItems {minimum}", "DFIR-SCHEMA-ITEMS")
        if isinstance(maximum, int) and len(value) > maximum:
            raise IRValidationError(f"{path} has more than maxItems {maximum}", "DFIR-SCHEMA-ITEMS")
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in value]
            if len(set(encoded)) != len(encoded):
                raise IRValidationError(f"{path} contains duplicate items", "DFIR-SCHEMA-UNIQUE")
        if isinstance(schema.get("items"), dict):
            for index, child in enumerate(value):
                _validate_schema(child, schema["items"], root, f"{path}[{index}]")
        contains = schema.get("contains")
        if isinstance(contains, dict) and not any(_schema_matches(child, contains, root, f"{path}[{index}]") for index, child in enumerate(value)):
            raise IRValidationError(f"{path} does not contain a required item", "DFIR-SCHEMA-CONTAINS")
    for combinator in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(combinator)
        if branches is None:
            continue
        if not isinstance(branches, list) or not all(isinstance(branch, dict) for branch in branches):
            raise IRValidationError(f"{path} has an invalid {combinator}", "DFIR-SCHEMA-COMBINATOR")
        matches = sum(_schema_matches(value, branch, root, path) for branch in branches)
        if combinator == "allOf" and matches != len(branches):
            raise IRValidationError(f"{path} failed allOf", "DFIR-SCHEMA-ALLOF")
        if combinator == "anyOf" and matches == 0:
            raise IRValidationError(f"{path} failed anyOf", "DFIR-SCHEMA-ANYOF")
        if combinator == "oneOf" and matches != 1:
            raise IRValidationError(f"{path} matched {matches} oneOf branches", "DFIR-SCHEMA-ONEOF")
    condition = schema.get("if")
    if isinstance(condition, dict) and _condition_matches(value, condition):
        branch = schema.get("then")
        if isinstance(branch, dict):
            _validate_schema(value, branch, root, path)
    elif isinstance(condition, dict):
        branch = schema.get("else")
        if isinstance(branch, dict):
            _validate_schema(value, branch, root, path)
    negative = schema.get("not")
    if isinstance(negative, dict) and _schema_matches(value, negative, root, path):
        raise IRValidationError(f"{path} matches a forbidden schema", "DFIR-SCHEMA-NOT")


def validate_normative_schema(document: dict[str, Any]) -> None:
    schema = _load_schema()
    _validate_schema(document, schema, schema, "$")


def _validate_capability_contract(document: dict[str, Any], source: dict[str, Any], conversion: dict[str, Any]) -> None:
    profile_id = conversion.get("capabilityProfile")
    if not isinstance(profile_id, str) or not profile_id:
        raise IRValidationError("conversion.capabilityProfile is required", "DFIR-CAPABILITY-PROFILE-MISSING")
    try:
        catalog = json.loads(CAPABILITY_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IRValidationError(f"cannot load capability profile catalog: {exc}", "DFIR-CAPABILITY-CATALOG") from exc
    profiles = catalog.get("profiles") if isinstance(catalog, dict) else None
    profile = next((item for item in profiles or [] if isinstance(item, dict) and item.get("id") == profile_id), None)
    if profile is None:
        raise IRValidationError(f"unknown capability profile: {profile_id}", "DFIR-CAPABILITY-PROFILE-UNKNOWN")
    if profile.get("format") != source.get("name") or profile.get("version") != source.get("version"):
        raise IRValidationError("capability profile does not match sourceFormat", "DFIR-CAPABILITY-PROFILE-MISMATCH")
    features = conversion.get("features", [])
    inventory = conversion.get("featureInventory")
    if not isinstance(features, list) or not isinstance(inventory, list):
        raise IRValidationError("conversion feature inventory must be arrays", "DFIR-CAPABILITY-INVENTORY-TYPE")
    expected: dict[tuple[str, str], int] = {}
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("feature"), str) or not isinstance(feature.get("status"), str):
            raise IRValidationError("conversion feature is malformed", "DFIR-CAPABILITY-FEATURE-MALFORMED")
        key = (feature["feature"], feature["status"])
        expected[key] = expected.get(key, 0) + 1
    observed: dict[tuple[str, str], int] = {}
    for entry in inventory:
        if not isinstance(entry, dict) or not isinstance(entry.get("feature"), str) or not isinstance(entry.get("occurrences"), int):
            raise IRValidationError("feature inventory entry is malformed", "DFIR-CAPABILITY-INVENTORY-MALFORMED")
        status = entry.get("status")
        if not isinstance(status, str):
            raise IRValidationError(f"feature inventory lacks status: {entry.get('feature')}", "DFIR-CAPABILITY-INVENTORY-STATUS")
        key = (entry["feature"], status)
        observed[key] = observed.get(key, 0) + entry["occurrences"]
        disposition = entry.get("disposition")
        if status in PARTIAL_STATUSES and disposition != "non-preserved":
            raise IRValidationError(f"non-preserved feature has disposition {disposition}: {entry['feature']}", "DFIR-CAPABILITY-DISPOSITION")
        if status == "unavailable" and disposition != "observation":
            raise IRValidationError(f"unavailable feature is not observation-only: {entry['feature']}", "DFIR-CAPABILITY-DISPOSITION")
    if observed != expected:
        raise IRValidationError("featureInventory does not aggregate conversion.features exactly", "DFIR-CAPABILITY-INVENTORY-MISMATCH")


def _walk(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise IRValidationError(f"forbidden key at {path}: {key}", "DFIR-BOUNDARY-FORBIDDEN")
            if not isinstance(key, str):
                raise IRValidationError(f"object key is not a string at {path}", "DFIR-JSON-KEY")
            _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]")
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise IRValidationError(f"non-finite number at {path}", "DFIR-JSON-NONFINITE")


def _id_ref(value: Any, ids: dict[str, str], targets: set[str], path: str, *, optional: bool = True, target_kinds: set[str] | None = None, kind_by_id: dict[str, str] | None = None) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or value not in ids or ids[value] not in targets:
        raise IRValidationError(f"{path} references an invalid target: {value}", "DFIR-REF-DANGLING")
    if target_kinds is not None and kind_by_id is not None and value in kind_by_id and kind_by_id[value] not in target_kinds:
        raise IRValidationError(f"{path} references wrong target kind: {kind_by_id[value]}", "DFIR-REF-WRONG-TYPE")


def _check_cycle(graph: dict[str, str | None], code: str, label: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise IRValidationError(f"{label} cycle at {identifier}", code)
        if identifier in visited:
            return
        visiting.add(identifier)
        parent = graph.get(identifier)
        if parent is not None and parent in graph:
            visit(parent)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)


def _status_of(item: Any) -> str | None:
    return item.get("status") if isinstance(item, dict) else None


def _typed_value_check(value: dict[str, Any], path: str) -> None:
    value_type = value.get("type")
    raw = value.get("value")
    if value_type == "blank" and raw is not None:
        raise IRValidationError(f"{path} blank value must be null", "DFIR-TYPED-VALUE-MISMATCH")
    if value_type == "boolean" and not isinstance(raw, bool):
        raise IRValidationError(f"{path} boolean value is not boolean", "DFIR-TYPED-VALUE-MISMATCH")
    if value_type in {"integer", "number", "decimal"}:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise IRValidationError(f"{path} numeric value has the wrong type", "DFIR-TYPED-VALUE-MISMATCH")
        if isinstance(raw, float) and (raw != raw or raw in (float("inf"), float("-inf"))):
            raise IRValidationError(f"{path} numeric value is non-finite", "DFIR-TYPED-VALUE-MISMATCH")
        if value_type == "decimal" and (not isinstance(raw, str) or re.fullmatch(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", raw) is None):
            raise IRValidationError(f"{path} decimal is not canonical", "DFIR-DECIMAL-NONCANONICAL")
        if value_type == "integer" and isinstance(raw, str) and re.fullmatch(r"^-?(0|[1-9][0-9]*)$", raw) is None:
            raise IRValidationError(f"{path} integer is not exact", "DFIR-TYPED-VALUE-MISMATCH")
    if value_type in {"string", "date", "datetime", "error"} and raw is not None and not isinstance(raw, str):
        raise IRValidationError(f"{path} textual value is not a string", "DFIR-TYPED-VALUE-MISMATCH")


def validate_document(document: dict[str, Any]) -> list[str]:
    """Validate a complete IR and return non-fatal warnings."""

    if not isinstance(document, dict):
        raise IRValidationError("IR root must be an object", "DFIR-IR-ROOT")
    _walk(document)
    validate_normative_schema(document)
    if document.get("schema") != {"name": "fdir/document-form", "version": "1.0.0"}:
        raise IRValidationError("unsupported IR schema", "DFIR-SCHEMA-VERSION")
    source = document.get("sourceFormat")
    if not isinstance(source, dict) or source.get("name") not in {"docx", "xlsx", "pdf", "markdown"}:
        raise IRValidationError("invalid sourceFormat", "DFIR-SOURCE-FORMAT")

    ids: dict[str, str] = {}
    items_by_collection: dict[str, dict[str, dict[str, Any]]] = {}
    for collection, id_key in COLLECTION_KEYS.items():
        items = document.get(collection, [])
        if not isinstance(items, list):
            raise IRValidationError(f"{collection} must be an array", "DFIR-COLLECTION-TYPE")
        current: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str):
                raise IRValidationError(f"{collection} contains an item without {id_key}", "DFIR-ID-MISSING")
            item_id = item[id_key]
            if item_id in ids:
                raise IRValidationError(f"duplicate entity id: {item_id}", "DFIR-ID-DUPLICATE")
            ids[item_id] = collection
            current[item_id] = item
            status = _status_of(item)
            if status is not None and status not in STATUS_VALUES:
                raise IRValidationError(f"{collection}.{item_id} has invalid status", "DFIR-STATUS-INVALID")
        items_by_collection[collection] = current

    nodes = items_by_collection["nodes"]
    if document["rootNodeId"] not in nodes:
        raise IRValidationError("rootNodeId does not identify a node", "DFIR-ROOT-MISSING")
    root = nodes[document["rootNodeId"]]
    if root.get("kind") != "document":
        raise IRValidationError("rootNodeId must identify the document variant", "DFIR-ROOT-KIND")
    if root.get("parentId") is not None:
        raise IRValidationError("document root cannot have a parent", "DFIR-GRAPH-ROOT-PARENT")

    kind_by_id = {identifier: item.get("kind") for identifier, item in nodes.items() if isinstance(item.get("kind"), str)}
    parent_of: dict[str, str] = {}
    for node_id, node in nodes.items():
        kind = node.get("kind")
        if kind not in NODE_KINDS:
            raise IRValidationError(f"node {node_id} has unknown kind {kind}", "DFIR-NODE-KIND")
        child_ids = node.get("childIds", [])
        if len(set(child_ids)) != len(child_ids):
            raise IRValidationError(f"node {node_id} has duplicate child membership", "DFIR-GRAPH-DUPLICATE-MEMBER")
        for child_id in child_ids:
            _id_ref(child_id, ids, {"nodes"}, f"node {node_id}.childIds", optional=False)
            if child_id in parent_of and parent_of[child_id] != node_id:
                raise IRValidationError(f"node {child_id} has multiple parents", "DFIR-GRAPH-MULTIPLE-PARENT")
            parent_of[child_id] = node_id
        parent_id = node.get("parentId")
        if parent_id is not None:
            _id_ref(parent_id, ids, {"nodes"}, f"node {node_id}.parentId", optional=False)
            if parent_id == node_id:
                raise IRValidationError(f"node {node_id} is its own parent", "DFIR-GRAPH-CYCLE")
            if node_id not in nodes[parent_id].get("childIds", []):
                raise IRValidationError(f"node {node_id} parent/child reciprocity is broken", "DFIR-GRAPH-RECIPROCITY")
            if node_id in parent_of and parent_of[node_id] != parent_id:
                raise IRValidationError(f"node {node_id} has multiple parents", "DFIR-GRAPH-MULTIPLE-PARENT")
            parent_of[node_id] = parent_id
        elif node_id in parent_of:
            raise IRValidationError(f"node {node_id} child lacks reciprocal parentId", "DFIR-GRAPH-RECIPROCITY")
        for field, targets in {
            "partId": {"parts"}, "geometryId": {"geometries"}, "formulaId": {"formulas"}, "formulaFieldId": {"formulas"},
            "fieldId": {"fields"}, "directStyleId": {"styles"}, "resolvedStyleId": {"styles"},
        }.items():
            if field in node:
                _id_ref(node[field], ids, targets, f"node {node_id}.{field}", optional=False)
        for field, target in {"textIds":"texts", "styleIds":"styles", "layoutIds":"layouts", "annotationIds":"annotations", "resourceIds":"resources"}.items():
            values = node.get(field, [])
            if len(set(values)) != len(values):
                raise IRValidationError(f"node {node_id}.{field} has duplicates", "DFIR-GRAPH-DUPLICATE-MEMBER")
            for value in values:
                _id_ref(value, ids, {target}, f"node {node_id}.{field}", optional=False)
        if kind == "run" and not node.get("textIds") and node.get("status") not in {"unsupported", "omitted-by-policy"}:
            raise IRValidationError(f"run {node_id} has no text", "DFIR-RUN-TEXT-MISSING")
        if kind == "cell" and "address" not in node:
            raise IRValidationError(f"cell {node_id} has no address", "DFIR-CELL-ADDRESS-MISSING")
        if kind in VISUAL_NODE_KINDS and "geometryId" not in node:
            raise IRValidationError(f"visual node {node_id} has no geometry", "DFIR-GEOMETRY-MISSING")
        if isinstance(node.get("value"), dict):
            _typed_value_check(node["value"], f"node {node_id}.value")
        forbidden_by_kind = {
            "document": {"address", "geometryId", "formulaId", "fieldId", "directStyleId", "resolvedStyleId"},
            "paragraph": {"address", "geometryId", "formulaId"}, "heading": {"address", "geometryId", "formulaId"},
            "run": {"address", "geometryId", "formulaId", "fieldId"}, "row": {"address", "geometryId", "formulaId"},
            "column": {"address", "geometryId", "formulaId", "fieldId"}, "table": {"address", "geometryId", "formulaId"},
        }
        forbidden = forbidden_by_kind.get(kind, set())
        if forbidden.intersection(node):
            raise IRValidationError(f"node {node_id} has fields forbidden for kind {kind}", "DFIR-NODE-VARIANT")

    if set(parent_of) - {document["rootNodeId"]} != set(nodes) - {document["rootNodeId"]}:
        missing = sorted((set(nodes) - {document["rootNodeId"]}) - set(parent_of))
        raise IRValidationError(f"unreachable/orphan nodes: {', '.join(missing)}", "DFIR-GRAPH-ORPHAN")
    reachable: set[str] = set()
    active: set[str] = set()

    def walk_node(node_id: str) -> None:
        if node_id in active:
            raise IRValidationError(f"containment cycle at {node_id}", "DFIR-GRAPH-CYCLE")
        if node_id in reachable:
            return
        active.add(node_id)
        reachable.add(node_id)
        for child_id in nodes[node_id].get("childIds", []):
            walk_node(child_id)
        active.remove(node_id)

    walk_node(document["rootNodeId"])
    if reachable != set(nodes):
        raise IRValidationError("not all nodes are reachable from root", "DFIR-GRAPH-UNREACHABLE")

    for part_id, part in items_by_collection["parts"].items():
        for node_id in part.get("rootNodeIds", []):
            _id_ref(node_id, ids, {"nodes"}, f"part {part_id}.rootNodeIds", optional=False)
        for surface_id in part.get("surfaceIds", []):
            _id_ref(surface_id, ids, {"surfaces"}, f"part {part_id}.surfaceIds", optional=False)
        for relationship_id in part.get("relationshipIds", []):
            _id_ref(relationship_id, ids, {"relations"}, f"part {part_id}.relationshipIds", optional=False)
        if part.get("parentPartId") is not None:
            _id_ref(part["parentPartId"], ids, {"parts"}, f"part {part_id}.parentPartId", optional=False)
    _check_cycle({identifier: item.get("parentPartId") for identifier, item in items_by_collection["parts"].items()}, "DFIR-PART-CYCLE", "part")

    for surface_id, surface in items_by_collection["surfaces"].items():
        _id_ref(surface.get("partId"), ids, {"parts"}, f"surface {surface_id}.partId", optional=False)
        for field, target in {"coordinateSpaceId": "coordinateSpaces", "gridId": "nodes"}.items():
            if surface.get(field) is not None:
                _id_ref(surface[field], ids, {target}, f"surface {surface_id}.{field}", optional=False)
        for layout_id in surface.get("layoutIds", []):
            _id_ref(layout_id, ids, {"layouts"}, f"surface {surface_id}.layoutIds", optional=False)

    for table_id, table in items_by_collection["tables"].items():
        _id_ref(table.get("nodeId"), ids, {"nodes"}, f"table {table_id}.nodeId", optional=False, target_kinds={"table", "section"}, kind_by_id=kind_by_id)
        for field, kinds in (("rowIds", {"row"}), ("columnIds", {"column"}), ("cellIds", {"cell"})):
            values = table.get(field, [])
            if len(set(values)) != len(values):
                raise IRValidationError(f"table {table_id}.{field} has duplicates", "DFIR-TABLE-DUPLICATE-MEMBER")
            for value in values:
                _id_ref(value, ids, {"nodes"}, f"table {table_id}.{field}", optional=False, target_kinds=kinds, kind_by_id=kind_by_id)

    for style_id, style in items_by_collection["styles"].items():
        if style.get("basedOn") is not None:
            _id_ref(style["basedOn"], ids, {"styles"}, f"style {style_id}.basedOn", optional=False)
        for source_id in style.get("resolvedFrom", []):
            _id_ref(source_id, ids, {"styles"}, f"style {style_id}.resolvedFrom", optional=False)
        for step in style.get("cascadeTrace", []):
            _id_ref(step.get("source"), ids, {"styles"}, f"style {style_id}.cascadeTrace.source", optional=False)
        for item in style.get("propertyProvenance", []):
            _id_ref(item.get("source"), ids, {"styles"}, f"style {style_id}.propertyProvenance.source", optional=False)
    _check_cycle({identifier: item.get("basedOn") for identifier, item in items_by_collection["styles"].items()}, "DFIR-STYLE-CYCLE", "style")

    for layout_id, layout in items_by_collection["layouts"].items():
        _id_ref(layout.get("targetId"), ids, {"nodes"}, f"layout {layout_id}.targetId", optional=False)
        for field, target in (("surfaceId", "surfaces"), ("declaredGeometryId", "geometries"), ("resolvedGeometryId", "geometries")):
            if layout.get(field) is not None:
                _id_ref(layout[field], ids, {target}, f"layout {layout_id}.{field}", optional=False)
        for geometry_id in layout.get("clipGeometryIds", []):
            _id_ref(geometry_id, ids, {"geometries"}, f"layout {layout_id}.clipGeometryIds", optional=False)
        anchor = layout.get("anchor", {})
        for field, target in (("surfaceId", "surfaces"), ("nodeId", "nodes"), ("gridId", "nodes")):
            if anchor.get(field) is not None:
                _id_ref(anchor[field], ids, {target}, f"layout {layout_id}.anchor.{field}", optional=False)
    for space_id, space in items_by_collection["coordinateSpaces"].items():
        if space.get("parentSpaceId") is not None:
            _id_ref(space["parentSpaceId"], ids, {"coordinateSpaces"}, f"coordinateSpace {space_id}.parentSpaceId", optional=False)
    _check_cycle({identifier: item.get("parentSpaceId") for identifier, item in items_by_collection["coordinateSpaces"].items()}, "DFIR-SPACE-CYCLE", "coordinate space")
    for geometry_id, geometry in items_by_collection["geometries"].items():
        _id_ref(geometry.get("spaceId"), ids, {"coordinateSpaces"}, f"geometry {geometry_id}.spaceId", optional=False)
    for resource_id, resource in items_by_collection["resources"].items():
        if resource.get("sourceRelationshipId") is not None:
            _id_ref(resource["sourceRelationshipId"], ids, {"relations"}, f"resource {resource_id}.sourceRelationshipId", optional=False)

    for formula_id, formula in items_by_collection["formulas"].items():
        if formula.get("ownerCellId") is not None:
            _id_ref(formula["ownerCellId"], ids, {"nodes"}, f"formula {formula_id}.ownerCellId", optional=False, target_kinds={"cell"}, kind_by_id=kind_by_id)
        for name, typed in formula.get("values", {}).items():
            if isinstance(typed, dict) and name != "displayed":
                _typed_value_check(typed, f"formula {formula_id}.values.{name}")
    for field_id, field in items_by_collection["fields"].items():
        if field.get("ownerNodeId") is not None:
            _id_ref(field["ownerNodeId"], ids, {"nodes"}, f"field {field_id}.ownerNodeId", optional=False)
        for key in ("instructionTextId", "resultTextId"):
            if field.get(key) is not None:
                _id_ref(field[key], ids, {"texts"}, f"field {field_id}.{key}", optional=False)
    for annotation_id, annotation in items_by_collection["annotations"].items():
        for target_id in annotation.get("targetIds", []):
            _id_ref(target_id, ids, {"nodes", "resources"}, f"annotation {annotation_id}.targetIds", optional=False)
        for key, target in (("bodyNodeId", "nodes"), ("bodyTextId", "texts")):
            if annotation.get(key) is not None:
                _id_ref(annotation[key], ids, {target}, f"annotation {annotation_id}.{key}", optional=False)

    for relation_id, relation in items_by_collection["relations"].items():
        _id_ref(relation.get("fromId"), ids, set(COLLECTION_KEYS), f"relation {relation_id}.fromId", optional=False)
        _id_ref(relation.get("toId"), ids, set(COLLECTION_KEYS), f"relation {relation_id}.toId", optional=False)
    for order_id, order in items_by_collection["orders"].items():
        _id_ref(order.get("ownerId"), ids, {"parts", "surfaces", "nodes"}, f"order {order_id}.ownerId", optional=False)
        seen_ids: set[str] = set()
        seen_ordinals: set[int] = set()
        for item in order.get("items", []):
            item_id = item.get("id")
            ordinal = item.get("ordinal")
            if item_id in seen_ids or ordinal in seen_ordinals:
                raise IRValidationError(f"order {order_id} has duplicate member or ordinal", "DFIR-ORDER-DUPLICATE")
            seen_ids.add(item_id)
            seen_ordinals.add(ordinal)
            _id_ref(item_id, ids, set(COLLECTION_KEYS) - {"diagnostics", "sourceMaps"}, f"order {order_id}.items.id", optional=False)
    for observation_id, observation in items_by_collection["observations"].items():
        _id_ref(observation.get("targetId"), ids, {"nodes", "texts", "geometries", "resources"}, f"observation {observation_id}.targetId", optional=False)
        for key, target in (("textId", "texts"), ("geometryId", "geometries")):
            if observation.get(key) is not None:
                _id_ref(observation[key], ids, {target}, f"observation {observation_id}.{key}", optional=False)

    try:
        from extension_registry import validate_extension
    except ImportError:  # pragma: no cover - package-style import
        from tools.extension_registry import validate_extension
    for extension in items_by_collection["extensions"].values():
        validate_extension(extension, document, ids, kind_by_id)
    for source_map_id, source_map in items_by_collection["sourceMaps"].items():
        _id_ref(source_map.get("targetId"), ids, set(COLLECTION_KEYS) - {"diagnostics", "sourceMaps"}, f"sourceMap {source_map_id}.targetId", optional=False)
        if source_map.get("format", {}).get("name") != source.get("name"):
            raise IRValidationError(f"sourceMap {source_map_id} format does not match sourceFormat", "DFIR-SOURCEMAP-FORMAT")
    diagnostic_ids = set(items_by_collection["diagnostics"])
    for diagnostic_id, diagnostic in items_by_collection["diagnostics"].items():
        if diagnostic.get("targetId") is not None:
            _id_ref(diagnostic["targetId"], ids, set(COLLECTION_KEYS) - {"diagnostics", "sourceMaps"}, f"diagnostic {diagnostic_id}.targetId", optional=False)
        if diagnostic.get("sourceMapId") is not None:
            _id_ref(diagnostic["sourceMapId"], ids, {"sourceMaps"}, f"diagnostic {diagnostic_id}.sourceMapId", optional=False)
        for related_id in diagnostic.get("relatedIds", []):
            _id_ref(related_id, ids, set(COLLECTION_KEYS), f"diagnostic {diagnostic_id}.relatedIds", optional=False)

    conversion = document.get("conversion")
    if not isinstance(conversion, dict) or conversion.get("status") not in {"complete", "partial", "failed"}:
        raise IRValidationError("invalid conversion report", "DFIR-CONVERSION-STATUS")
    _validate_capability_contract(document, source, conversion)
    for diagnostic_id in conversion.get("diagnostics", []):
        if diagnostic_id not in diagnostic_ids:
            raise IRValidationError(f"conversion references missing diagnostic {diagnostic_id}", "DFIR-DIAGNOSTIC-MISSING")
    for warning_id in conversion.get("warnings", []):
        if warning_id not in diagnostic_ids:
            raise IRValidationError(f"conversion references missing warning {warning_id}", "DFIR-DIAGNOSTIC-MISSING")
    for feature in conversion.get("features", []):
        status = feature.get("status")
        if status not in STATUS_VALUES:
            raise IRValidationError(f"feature has invalid status: {status}", "DFIR-STATUS-INVALID")
        if feature.get("targetId") is not None:
            _id_ref(feature["targetId"], ids, set(COLLECTION_KEYS) - {"diagnostics", "sourceMaps"}, "conversion feature targetId", optional=False)
        for diagnostic_id in feature.get("diagnosticIds", []):
            if diagnostic_id not in diagnostic_ids:
                raise IRValidationError(f"feature references missing diagnostic {diagnostic_id}", "DFIR-DIAGNOSTIC-MISSING")
    for inventory in conversion.get("featureInventory", []):
        for diagnostic_id in inventory.get("diagnosticIds", []):
            if diagnostic_id not in diagnostic_ids:
                raise IRValidationError(f"feature inventory references missing diagnostic {diagnostic_id}", "DFIR-DIAGNOSTIC-MISSING")

    statuses = [_status_of(item) for collection in items_by_collection.values() for item in collection.values()]
    statuses.extend(feature.get("status") for feature in conversion.get("features", []))
    hard_loss = any(status in PARTIAL_STATUSES for status in statuses)
    has_error = any(item.get("severity") in {"error", "fatal"} for item in items_by_collection["diagnostics"].values())
    if conversion["status"] == "complete" and (hard_loss or has_error):
        raise IRValidationError("complete conversion contains a non-preserved or error outcome", "DFIR-COMPLETE-CLAIM")
    if conversion["status"] == "failed" and not conversion.get("diagnostics"):
        raise IRValidationError("failed conversion has no diagnostic", "DFIR-FAILED-NO-DIAGNOSTIC")
    if conversion["status"] == "partial" and not conversion.get("diagnostics") and not hard_loss:
        raise IRValidationError("partial conversion has no loss evidence", "DFIR-PARTIAL-NO-EVIDENCE")
    return [f"{item.get('code')}: {item.get('message')}" for item in items_by_collection["diagnostics"].values() if item.get("severity") in {"info", "warning"}]
