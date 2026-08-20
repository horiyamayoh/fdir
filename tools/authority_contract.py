"""Shared IR authority contract and executable field/graph census.

This module deliberately owns only cross-format IR rules.  Format adapters and
release evidence remain separate.  The model contract is normative; the
reference registry and canonical/query metadata are checked projections.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "machine" / "model-contract.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
REFERENCE_PATH = ROOT / "machine" / "reference-registry.json"
EXTENSION_PATH = ROOT / "machine" / "extension-registry.json"
COLLECTION_KEYS = {
    "parts": "partId", "surfaces": "surfaceId", "nodes": "nodeId", "texts": "textId",
    "tables": "tableId", "styles": "styleId", "layouts": "layoutId",
    "coordinateSpaces": "coordinateSpaceId", "geometries": "geometryId",
    "resources": "resourceId", "formulas": "formulaId", "fields": "fieldId",
    "annotations": "annotationId", "relations": "relationId", "orders": "orderId",
    "observations": "observationId", "extensions": "extensionId",
    "sourceMaps": "sourceMapId", "diagnostics": "diagnosticId",
}
CONTRACT_DIGEST = "98b4856e5f53f4f0a2da5052b852d0fdf2caa9a44547d275fd2d379981232375"


class AuthorityContractError(ValueError):
    """Raised when a shared authority rule or its derived artifact drifts."""

    def __init__(self, message: str, code: str = "DFIR-AUTHORITY-CONTRACT") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityContractError(f"cannot load {path}: {exc}", "DFIR-AUTHORITY-LOAD") from exc
    if not isinstance(value, dict):
        raise AuthorityContractError(f"authority artifact is not an object: {path}", "DFIR-AUTHORITY-ROOT")
    return value


def load_contract() -> dict[str, Any]:
    contract = _load(MODEL_PATH)
    if contract.get("schema") != "fdir/document-form-model-contract":
        raise AuthorityContractError("wrong model contract schema", "DFIR-AUTHORITY-SCHEMA")
    return contract


def canonical_contract_bytes(contract: dict[str, Any] | None = None) -> bytes:
    value = contract if contract is not None else load_contract()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contract_digest(contract: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_contract_bytes(contract)).hexdigest()


def _schema_id_fields(schema: dict[str, Any]) -> dict[str, str]:
    properties = schema.get("properties", {})
    result: dict[str, str] = {}
    if not isinstance(properties, dict):
        return result
    for collection, value in properties.items():
        if collection not in COLLECTION_KEYS or not isinstance(value, dict):
            continue
        result[collection] = COLLECTION_KEYS[collection]
    return result


def _normal_ref(entry: dict[str, Any]) -> dict[str, Any]:
    """Project the legacy registry row into the model-contract census shape."""

    owner = entry.get("owner")
    target = entry.get("targetCollections", [])
    return {
        "owner": owner,
        "targetCollections": list(target) if isinstance(target, list) else [],
        "cardinality": entry.get("cardinality"),
        "reciprocal": entry.get("reciprocal"),
    }


def reference_census() -> dict[str, Any]:
    contract = load_contract()
    registry = _load(REFERENCE_PATH)
    model_rows = contract.get("referenceFields")
    registry_rows = registry.get("references")
    if not isinstance(model_rows, list) or not isinstance(registry_rows, list):
        raise AuthorityContractError("reference contract is not an array", "DFIR-REFERENCE-CENSUS-SHAPE")
    model = {row.get("owner"): _normal_ref(row) for row in model_rows if isinstance(row, dict)}
    generated = {row.get("owner"): _normal_ref(row) for row in registry_rows if isinstance(row, dict)}
    if None in model or None in generated:
        raise AuthorityContractError("reference row lacks owner", "DFIR-REFERENCE-CENSUS-OWNER")
    missing = sorted(set(model) - set(generated))
    extra = sorted(set(generated) - set(model))
    mismatches = sorted(key for key in set(model) & set(generated) if model[key] != generated[key])
    authority = registry.get("authority", {})
    if authority.get("sourcePath") != "machine/model-contract.json" or authority.get("sourceDigest") != contract_digest(contract):
        raise AuthorityContractError("reference registry source digest does not match model contract", "DFIR-REFERENCE-DRIFT")
    if missing or extra or mismatches:
        raise AuthorityContractError(f"reference registry drift missing={missing} extra={extra} mismatches={mismatches}", "DFIR-REFERENCE-DRIFT")
    required = contract.get("referenceRegistry", {}).get("requiredFields", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise AuthorityContractError("reference registry required field census is malformed", "DFIR-REFERENCE-CENSUS-CONTRACT")
    return {
        "status": "passed",
        "modelCount": len(model_rows),
        "registryCount": len(registry_rows),
        "missing": missing,
        "extra": extra,
        "mismatches": mismatches,
        "requiredFields": required,
        "contractDigest": contract_digest(contract),
    }


def _schema_enum(schema: dict[str, Any], definition: str, field: str) -> set[Any]:
    definition_schema = schema.get("$defs", {}).get(definition, {})
    if field == "" and isinstance(definition_schema, dict):
        enum = definition_schema.get("enum", [])
        return set(enum) if isinstance(enum, list) else set()
    value = definition_schema.get("properties", {}).get(field, {}) if isinstance(definition_schema, dict) else {}
    enum = value.get("enum", []) if isinstance(value, dict) else []
    return set(enum) if isinstance(enum, list) else set()


def extension_emission_census() -> dict[str, Any]:
    registry = _load(EXTENSION_PATH)
    entries = registry.get("entries", [])
    registered = {(item.get("namespace"), item.get("type"), item.get("schemaVersion")) for item in entries if isinstance(item, dict)}
    literal_types: set[str] = set()
    dynamic_sites: list[str] = []
    for path in sorted((ROOT / "tools").glob("adapter_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
                if "namespace" not in keys or "schemaVersion" not in keys:
                    continue
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "type":
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            literal_types.add(value.value)
                        elif isinstance(value, ast.Name):
                            dynamic_sites.append(f"{path.relative_to(ROOT)}:{node.lineno}:type={value.id}")
    registered_types = {item[1] for item in registered}
    return {
        "status": "passed" if not (literal_types - registered_types) else "failed",
        "registeredTypeCount": len(registered),
        "literalEmissionTypes": sorted(literal_types),
        "unregisteredLiteralTypes": sorted(literal_types - registered_types),
        "dynamicEmissionSites": sorted(dynamic_sites),
        "dynamicPolicy": "dynamic type values must flow through registry-validated constructor; AST census records sites for qualification",
    }


def validate_authority_artifacts() -> dict[str, Any]:
    contract = load_contract()
    schema = _load(SCHEMA_PATH)
    digest = contract_digest(contract)
    if digest != CONTRACT_DIGEST:
        raise AuthorityContractError(f"model contract digest changed without updating generated metadata: {digest}", "DFIR-AUTHORITY-DIGEST")
    schema_ref = schema.get("$id", "")
    if not schema_ref.endswith("/1.0.0"):
        raise AuthorityContractError("schema version does not match model contract", "DFIR-AUTHORITY-VERSION")
    if _schema_id_fields(schema) != {key: value["idField"] for key, value in contract["collections"].items()}:
        raise AuthorityContractError("collection id-field census differs from schema", "DFIR-COLLECTION-CENSUS")
    if _schema_enum(schema, "relation", "kind") != set(contract["relationKinds"]):
        raise AuthorityContractError("relation kind census differs from schema", "DFIR-RELATION-CENSUS")
    if _schema_enum(schema, "order", "kind") != set(contract["orders"]["kinds"]):
        raise AuthorityContractError("order kind census differs from schema", "DFIR-ORDER-CENSUS")
    statuses = contract["statusContract"]
    if _schema_enum(schema, "status", "") != set(statuses["entityStatuses"]):
        raise AuthorityContractError("status census differs from schema", "DFIR-STATUS-CENSUS")
    conversions = schema.get("$defs", {}).get("conversionReport", {}).get("properties", {}).get("status", {}).get("enum", [])
    if set(conversions) != set(statuses["conversionStatuses"]):
        raise AuthorityContractError("conversion status census differs from schema", "DFIR-CONVERSION-CENSUS")
    references = reference_census()
    extensions = extension_emission_census()
    return {"status": "passed", "contractDigest": digest, "referenceCensus": references, "extensionCensus": extensions}


def _collection_index(document: dict[str, Any]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    by_id: dict[str, str] = {}
    items: dict[str, dict[str, Any]] = {}
    for collection, id_field in COLLECTION_KEYS.items():
        for item in document.get(collection, []) or []:
            if isinstance(item, dict) and isinstance(item.get(id_field), str):
                by_id[item[id_field]] = collection
                items[item[id_field]] = item
    return by_id, items


def validate_runtime_contract(document: dict[str, Any], ids: dict[str, str] | None = None) -> None:
    """Validate semantic rules not expressible by the closed JSON schema."""

    contract = load_contract()
    by_id, items = _collection_index(document)
    if ids:
        by_id.update(ids)
    kinds = {identifier: item.get("kind") for identifier, item in items.items()}
    relation_rules = contract["relationKinds"]
    for relation in document.get("relations", []) or []:
        kind = relation.get("kind")
        rule = relation_rules.get(kind)
        if rule is None:
            raise AuthorityContractError(f"unknown relation kind: {kind}", "DFIR-RELATION-KIND")
        from_id, to_id = relation.get("fromId"), relation.get("toId")
        from_collection, to_collection = by_id.get(from_id), by_id.get(to_id)
        if from_collection not in rule["fromCollections"] or to_collection not in rule["toCollections"]:
            raise AuthorityContractError(f"relation {relation.get('relationId')} has invalid endpoint collections", "DFIR-RELATION-ENDPOINT")
        from_kind, to_kind = kinds.get(from_id, "*"), kinds.get(to_id, "*")
        if rule.get("fromKinds") != ["*"] and from_kind not in rule["fromKinds"]:
            raise AuthorityContractError(f"relation {relation.get('relationId')} has invalid source kind {from_kind}", "DFIR-RELATION-SOURCE-KIND")
        if rule.get("toKinds") != ["*"] and to_kind not in rule["toKinds"]:
            raise AuthorityContractError(f"relation {relation.get('relationId')} has invalid target kind {to_kind}", "DFIR-RELATION-TARGET-KIND")
    styles = {item.get("styleId"): item for item in document.get("styles", []) or [] if isinstance(item, dict)}
    for style_id, style in styles.items():
        resolved = style.get("resolved")
        if not isinstance(resolved, dict) or style.get("status") != "normalized":
            continue
        provenance = style.get("propertyProvenance")
        if not isinstance(provenance, list):
            raise AuthorityContractError(f"resolved style {style_id} has no property provenance", "DFIR-STYLE-PROVENANCE-MISSING")
        provenance_by_property = {item.get("property"): item for item in provenance if isinstance(item, dict)}
        missing = sorted(set(resolved) - set(provenance_by_property))
        if missing:
            raise AuthorityContractError(f"resolved style {style_id} lacks provenance for {', '.join(missing)}", "DFIR-STYLE-PROVENANCE-MISSING")
        for property_name, item in provenance_by_property.items():
            source_id = item.get("source")
            if property_name in resolved and source_id not in styles:
                raise AuthorityContractError(f"resolved style {style_id} property {property_name} has dangling provenance source", "DFIR-STYLE-PROVENANCE-REF")
    statuses = contract["statusContract"]
    complete = document.get("conversion", {}).get("status") == "complete"
    if complete:
        forbidden = set(statuses["completeEligibility"]["forbiddenEntityStatuses"])
        for collection, entries in document.items():
            if collection in {"diagnostics", "sourceMaps", "conversion"} or not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and item.get("status") in forbidden:
                    raise AuthorityContractError(f"complete conversion contains {item.get('status')} in {collection}", "DFIR-COMPLETE-STATUS")
        forbidden_severity = set(statuses["completeEligibility"]["forbiddenDiagnosticSeverities"])
        if any(item.get("severity") in forbidden_severity for item in document.get("diagnostics", []) if isinstance(item, dict)):
            raise AuthorityContractError("complete conversion contains an error diagnostic", "DFIR-COMPLETE-DIAGNOSTIC")
    for order in document.get("orders", []) or []:
        ordinals = [item.get("ordinal") for item in order.get("items", []) if isinstance(item, dict)]
        if any(not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0 for ordinal in ordinals):
            raise AuthorityContractError(f"order {order.get('orderId')} contains a negative or non-integer ordinal", "DFIR-ORDER-ORDINAL")
        if len(set(ordinals)) != len(ordinals):
            raise AuthorityContractError(f"order {order.get('orderId')} has duplicate ordinals", "DFIR-ORDER-ORDINAL")
