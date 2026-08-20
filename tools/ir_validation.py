"""Fail-closed structural validation for generated Document Form IR."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


FORBIDDEN_KEYS = {
    "sourceBytes",
    "sourceByteStore",
    "contentAddressedSource",
    "semanticEquivalence",
    "EquivalenceCertificate",
    "LineageCertificate",
    "AccountingItem",
    "predicate",
}
COLLECTION_KEYS = {
    "parts": "partId",
    "surfaces": "surfaceId",
    "nodes": "nodeId",
    "texts": "textId",
    "tables": "tableId",
    "styles": "styleId",
    "layouts": "layoutId",
    "coordinateSpaces": "coordinateSpaceId",
    "geometries": "geometryId",
    "resources": "resourceId",
    "formulas": "formulaId",
    "fields": "fieldId",
    "annotations": "annotationId",
    "relations": "relationId",
    "orders": "orderId",
    "observations": "observationId",
    "extensions": "extensionId",
    "sourceMaps": "sourceMapId",
    "diagnostics": "diagnosticId",
}


class IRValidationError(ValueError):
    pass


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "document-form-ir.schema.json"


def _load_schema() -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IRValidationError(f"cannot load normative IR schema: {exc}") from exc
    if not isinstance(schema, dict):
        raise IRValidationError("normative IR schema root must be an object")
    return schema


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    if not reference.startswith("#/"):
        raise IRValidationError(f"unsupported schema reference: {reference}")
    current: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            raise IRValidationError(f"unresolved schema reference: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise IRValidationError(f"schema reference is not an object: {reference}")
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


def _condition_matches(value: Any, condition: dict[str, Any], root: dict[str, Any]) -> bool:
    """Evaluate the small property/const/enum condition used by this schema."""

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
    return True


def _validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    schema = _resolve_schema(schema, root)
    if "const" in schema and value != schema["const"]:
        raise IRValidationError(f"{path} does not equal schema const")
    if "enum" in schema and value not in schema["enum"]:
        raise IRValidationError(f"{path} is not one of the allowed values")
    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(isinstance(item, str) and _type_matches(value, item) for item in expected_types):
            raise IRValidationError(f"{path} has type {_json_type(value)}, expected {expected}")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise IRValidationError(f"{path} is shorter than minLength {minimum}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise IRValidationError(f"{path} does not match schema pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise IRValidationError(f"{path} is below minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise IRValidationError(f"{path} is above maximum {maximum}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise IRValidationError(f"{path} has invalid schema properties")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise IRValidationError(f"{path} has invalid schema required list")
        missing = [key for key in required if key not in value]
        if missing:
            raise IRValidationError(f"{path} is missing required fields: {', '.join(missing)}")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                _validate_schema(child, properties[key], root, f"{path}.{key}")
            elif additional is False:
                raise IRValidationError(f"{path} contains unknown field: {key}")
            elif isinstance(additional, dict):
                _validate_schema(child, additional, root, f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, child in enumerate(value):
            _validate_schema(child, schema["items"], root, f"{path}[{index}]")
    for branch in schema.get("allOf", []):
        if not isinstance(branch, dict):
            raise IRValidationError(f"{path} has an invalid allOf branch")
        condition = branch.get("if")
        if isinstance(condition, dict) and _condition_matches(value, condition, root):
            then = branch.get("then")
            if isinstance(then, dict):
                _validate_schema(value, then, root, path)
    negative = schema.get("not")
    if isinstance(negative, dict):
        try:
            _validate_schema(value, negative, root, path)
        except IRValidationError:
            pass
        else:
            raise IRValidationError(f"{path} matches a forbidden schema")


def validate_normative_schema(document: dict[str, Any]) -> None:
    schema = _load_schema()
    _validate_schema(document, schema, schema, "$")


def _walk(value: Any, path: str = "$ ") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise IRValidationError(f"forbidden key at {path}: {key}")
            _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]")


def validate_document(document: dict[str, Any]) -> list[str]:
    """Validate core shape/references and return warnings.

    JSON Schema remains the normative contract.  This validator supplies the
    runtime checks needed by the stdlib-only E2E path and intentionally fails
    when an adapter emits an apparently valid but internally disconnected IR.
    """

    if not isinstance(document, dict):
        raise IRValidationError("IR root must be an object")
    _walk(document)
    validate_normative_schema(document)
    required = {"schema", "documentId", "sourceFormat", "rootNodeId", "nodes", "conversion"}
    missing = sorted(required - set(document))
    if missing:
        raise IRValidationError(f"missing root fields: {', '.join(missing)}")
    if document.get("schema") != {"name": "fdir/document-form", "version": "1.0.0"}:
        raise IRValidationError("unsupported IR schema")
    source = document.get("sourceFormat")
    if not isinstance(source, dict) or source.get("name") not in {"docx", "xlsx", "pdf", "markdown"}:
        raise IRValidationError("invalid sourceFormat")
    ids: dict[str, str] = {}
    for collection, id_key in COLLECTION_KEYS.items():
        items = document.get(collection, [])
        if not isinstance(items, list):
            raise IRValidationError(f"{collection} must be an array")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str):
                raise IRValidationError(f"{collection} contains an item without {id_key}")
            item_id = item[id_key]
            if item_id in ids:
                raise IRValidationError(f"duplicate entity id: {item_id}")
            ids[item_id] = collection
    nodes = document["nodes"]
    if not any(node.get("nodeId") == document["rootNodeId"] for node in nodes):
        raise IRValidationError("rootNodeId does not identify a node")
    warnings: list[str] = []
    for node in nodes:
        node_id = node["nodeId"]
        if not isinstance(node.get("childIds"), list):
            raise IRValidationError(f"node {node_id} childIds must be an array")
        for child_id in node["childIds"]:
            if child_id not in ids or ids[child_id] != "nodes":
                raise IRValidationError(f"node {node_id} references missing child {child_id}")
        parent_id = node.get("parentId")
        if parent_id and (parent_id not in ids or ids[parent_id] != "nodes"):
            raise IRValidationError(f"node {node_id} references missing parent {parent_id}")
        for ref_key in ("textIds", "styleIds", "layoutIds", "annotationIds", "resourceIds"):
            for ref in node.get(ref_key, []):
                collection = {"textIds":"texts", "styleIds":"styles", "layoutIds":"layouts", "annotationIds":"annotations", "resourceIds":"resources"}[ref_key]
                if ref not in ids or ids[ref] != collection:
                    raise IRValidationError(f"node {node_id} references missing {ref_key} item {ref}")
        if node.get("kind") == "run" and not node.get("textIds"):
            warnings.append(f"run {node_id} has no text")
    for relation in document.get("relations", []):
        for key in ("fromId", "toId"):
            if relation.get(key) not in ids:
                raise IRValidationError(f"relation {relation.get('relationId')} references missing {key}")
    for source_map in document.get("sourceMaps", []):
        if source_map.get("targetId") not in ids:
            raise IRValidationError(f"source map references missing target {source_map.get('targetId')}")
    diagnostic_ids = {item["diagnosticId"] for item in document.get("diagnostics", [])}
    conversion = document.get("conversion")
    if not isinstance(conversion, dict) or conversion.get("status") not in {"complete", "partial", "failed"}:
        raise IRValidationError("invalid conversion report")
    for diagnostic_id in conversion.get("diagnostics", []):
        if diagnostic_id not in diagnostic_ids:
            raise IRValidationError(f"conversion references missing diagnostic {diagnostic_id}")
    return warnings
