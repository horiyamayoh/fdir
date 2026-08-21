"""Typed extension registry and payload validation for FDIR."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any, TypeAlias


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Adapter emission helpers accept only this typed payload boundary.  The
# registry/schema remains the runtime authority for the concrete vocabulary;
# Mapping avoids the former free-form ``dict[str, Any]`` constructor contract.
ExtensionPayload: TypeAlias = Mapping[str, object]

COLLECTION_KEYS = {
    "parts": "partId", "surfaces": "surfaceId", "nodes": "nodeId", "texts": "textId",
    "tables": "tableId", "styles": "styleId", "layouts": "layoutId", "coordinateSpaces": "coordinateSpaceId",
    "geometries": "geometryId", "resources": "resourceId", "formulas": "formulaId", "fields": "fieldId",
    "annotations": "annotationId", "relations": "relationId", "orders": "orderId", "observations": "observationId",
    "extensions": "extensionId", "sourceMaps": "sourceMapId", "diagnostics": "diagnosticId",
}


class ExtensionValidationError(ValueError):
    """Raised when an extension violates the registry contract."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtensionValidationError(f"cannot load extension artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExtensionValidationError(f"extension artifact is not an object: {path}")
    return value


def load_registry() -> dict[str, Any]:
    registry = _load_json(REGISTRY_PATH)
    entries = registry.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ExtensionValidationError("extension registry has no entries")
    compatibility = registry.get("compatibility")
    if not isinstance(compatibility, dict) or any(not isinstance(compatibility.get(name), dict) for name in ("patch", "minor", "major")):
        raise ExtensionValidationError("extension registry compatibility rules are not executable objects")
    unknown_policy = registry.get("unknownPolicy")
    if not isinstance(unknown_policy, dict) or unknown_policy.get("critical") != "reject-or-partial" or unknown_policy.get("nonCritical") != "opaque-and-diagnose" or unknown_policy.get("completeClaim") != "forbidden":
        raise ExtensionValidationError("extension registry unknown policy is incomplete")
    keys: set[tuple[str, str, str]] = set()
    schema_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExtensionValidationError("extension registry contains a non-object entry")
        key = (str(entry.get("namespace")), str(entry.get("type")), str(entry.get("schemaVersion")))
        if key in keys:
            raise ExtensionValidationError(f"extension registry collision: {key}")
        keys.add(key)
        schema_id = entry.get("schemaId")
        if not isinstance(schema_id, str) or not schema_id or schema_id in schema_ids:
            raise ExtensionValidationError(f"extension registry schema id collision: {schema_id}")
        schema_ids.add(schema_id)
        if not isinstance(entry.get("schemaPath"), str) or "#/$defs/" not in entry["schemaPath"]:
            raise ExtensionValidationError(f"extension registry schema path is invalid: {key}")
        if not isinstance(entry.get("targetCollections"), list) or not entry["targetCollections"] or not all(isinstance(item, str) and item for item in entry["targetCollections"]):
            raise ExtensionValidationError(f"extension registry target collection contract is invalid: {key}")
        if not isinstance(entry.get("targetKinds"), list) or not entry["targetKinds"] or not all(isinstance(item, str) and item for item in entry["targetKinds"]):
            raise ExtensionValidationError(f"extension registry target kind contract is invalid: {key}")
        if entry.get("criticality") not in {"critical", "non-critical"}:
            raise ExtensionValidationError(f"extension registry criticality is invalid: {key}")
        schema_path = ROOT / entry["schemaPath"].split("#", 1)[0]
        try:
            digest = hashlib.sha256(schema_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ExtensionValidationError(f"cannot digest extension schema for {key}: {exc}") from exc
        if entry.get("schemaDigest") != digest:
            raise ExtensionValidationError(f"extension registry schema digest does not match schema for {key}")
        semantic_required = entry.get("semanticRequiredFields")
        if not isinstance(semantic_required, list) or not all(isinstance(item, str) and item for item in semantic_required):
            raise ExtensionValidationError(f"extension registry semantic required fields are invalid: {key}")
        schema_required = _payload_schema(entry).get("required", [])
        if not isinstance(schema_required, list) or not set(semantic_required).issubset(schema_required):
            raise ExtensionValidationError(f"extension registry semantic required fields exceed payload schema: {key}")
        version_range = entry.get("versionRange")
        if not isinstance(version_range, dict) or version_range.get("current") != entry.get("schemaVersion") or not isinstance(version_range.get("policy"), str):
            raise ExtensionValidationError(f"extension registry version range is incomplete: {key}")
        if not isinstance(entry.get("consumerCapabilities"), list) or not all(isinstance(item, str) and item for item in entry["consumerCapabilities"]):
            raise ExtensionValidationError(f"extension registry consumer capabilities are invalid: {key}")
        if not isinstance(entry.get("payloadReferences"), list):
            raise ExtensionValidationError(f"extension registry payload references are invalid: {key}")
        for reference in entry["payloadReferences"]:
            if not isinstance(reference, dict) or not isinstance(reference.get("pointer"), str) or not reference["pointer"].startswith("/"):
                raise ExtensionValidationError(f"extension registry payload reference is invalid: {key}")
            if not isinstance(reference.get("targetCollections"), list) or not reference["targetCollections"] or not all(isinstance(item, str) and item for item in reference["targetCollections"]):
                raise ExtensionValidationError(f"extension registry payload reference target is incomplete: {key}")
        for metadata_name in ("query", "canonicalization", "migration", "downgrade", "unknownFieldPolicy"):
            if not isinstance(entry.get(metadata_name), dict):
                raise ExtensionValidationError(f"extension registry metadata {metadata_name} is missing: {key}")
        if entry.get("readOnly") is True and (not isinstance(entry.get("readOnlyReason"), str) or not entry["readOnlyReason"]):
            raise ExtensionValidationError(f"read-only extension entry has no reason: {key}")
    return registry


def _entry_map(registry: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (entry["namespace"], entry["type"], entry["schemaVersion"]): entry
        for entry in registry["entries"]
        if isinstance(entry, dict)
    }


def _resolve_schema_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ExtensionValidationError(f"unsupported extension schema ref: {ref}")
    current: Any = schema
    for part in ref[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            raise ExtensionValidationError(f"unresolved extension schema ref: {ref}")
        current = current[part]
    if not isinstance(current, dict):
        raise ExtensionValidationError(f"extension schema ref is not an object: {ref}")
    return current


def _payload_schema(entry: dict[str, Any]) -> dict[str, Any]:
    path_text, fragment = entry["schemaPath"].split("#", 1)
    schema = _load_json(ROOT / path_text)
    return _resolve_schema_ref(schema, "#" + fragment)


def _semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = SEMVER_RE.fullmatch(value)
    return tuple(int(item) for item in match.groups()) if match else None


def _reference_registry(document: dict[str, Any], ids: dict[str, str], node_kinds: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Build the global reference-registry view consumed by extension refs."""

    result: dict[str, dict[str, Any]] = {
        identifier: {"collection": collection, "kind": node_kinds.get(identifier)}
        for identifier, collection in ids.items()
    }
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in document.get(collection, []):
            if isinstance(item, dict) and isinstance(item.get(identifier_key), str):
                identifier = item[identifier_key]
                result[identifier] = {"collection": collection, "kind": item.get("kind")}
    return result


def _pointer_value(value: Any, pointer: str) -> Any:
    current = value
    for raw_segment in pointer[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(pointer)
        current = current[segment]
    return current


def _compatible_entry(registry: dict[str, Any], extension: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Resolve an exact entry or an explicitly executable compatibility lane."""

    exact = _entry_map(registry).get((extension["namespace"], extension["type"], extension["schemaVersion"]))
    if exact is not None:
        return exact, "exact"
    incoming = _semver(extension.get("schemaVersion"))
    candidates = [
        entry for entry in registry.get("entries", [])
        if isinstance(entry, dict)
        and entry.get("namespace") == extension.get("namespace")
        and entry.get("type") == extension.get("type")
        and _semver(entry.get("schemaVersion")) is not None
    ]
    if incoming is None or not candidates:
        return None, "unknown"
    current_entry = max(candidates, key=lambda entry: _semver(entry["schemaVersion"]))
    current = _semver(current_entry["schemaVersion"])
    assert current is not None
    if incoming[0] != current[0]:
        return current_entry, "major"
    if incoming[1] != current[1]:
        return current_entry, "minor"
    if incoming[2] != current[2]:
        return current_entry, "patch"
    return current_entry, "unknown"


def _validate_payload(payload: Any, entry: dict[str, Any], path: str) -> None:
    if not isinstance(payload, dict):
        raise ExtensionValidationError(f"{path} must be an object")
    try:
        from ir_validation import _validate_schema  # type: ignore  # imported lazily to avoid a module cycle
    except ImportError:  # pragma: no cover - package-style import
        from tools.ir_validation import _validate_schema  # type: ignore
    schema = _load_json(ROOT / entry["schemaPath"].split("#", 1)[0])
    payload_schema = _resolve_schema_ref(schema, "#" + entry["schemaPath"].split("#", 1)[1])
    _validate_schema(payload, payload_schema, schema, path)


def _validate_payload_references(
    extension: dict[str, Any],
    entry: dict[str, Any],
    ids: dict[str, str],
    reference_registry: dict[str, dict[str, Any]],
) -> None:
    for reference in entry.get("payloadReferences", []):
        pointer = reference["pointer"]
        try:
            referenced_id = _pointer_value(extension["payload"], pointer)
        except KeyError:
            if reference.get("required", False):
                raise ExtensionValidationError(f"extension {extension['extensionId']} is missing required payload reference {pointer}")
            continue
        if not isinstance(referenced_id, str) or referenced_id not in ids:
            raise ExtensionValidationError(f"extension {extension['extensionId']} payload reference {pointer} is dangling")
        target = reference_registry.get(referenced_id, {"collection": ids[referenced_id], "kind": None})
        if target.get("collection") not in set(reference.get("targetCollections", [])):
            raise ExtensionValidationError(
                f"extension {extension['extensionId']} payload reference {pointer} targets collection {target.get('collection')}"
            )
        target_kinds = set(reference.get("targetKinds", []))
        if target_kinds and target.get("kind") not in target_kinds:
            raise ExtensionValidationError(
                f"extension {extension['extensionId']} payload reference {pointer} targets kind {target.get('kind')}"
            )


def validate_extension(
    extension: dict[str, Any],
    document: dict[str, Any],
    ids: dict[str, str],
    node_kinds: dict[str, str],
    *,
    reference_registry: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Validate one extension and return ``known``, ``compatible`` or ``opaque``."""

    required = ("extensionId", "targetId", "namespace", "type", "schemaVersion", "schemaId", "payload", "criticality")
    missing = [key for key in required if key not in extension]
    if missing:
        raise ExtensionValidationError(f"extension {extension.get('extensionId')} missing: {', '.join(missing)}")
    target_id = extension["targetId"]
    if target_id not in ids:
        raise ExtensionValidationError(f"extension {extension['extensionId']} references missing target {target_id}")
    if extension["criticality"] not in {"critical", "non-critical"}:
        raise ExtensionValidationError(f"extension {extension['extensionId']} has invalid criticality")
    registry = load_registry()
    entry, compatibility_lane = _compatible_entry(registry, extension)
    if entry is None:
        if extension["criticality"] == "critical":
            raise ExtensionValidationError(f"unknown critical extension: {extension['namespace']}:{extension['type']}:{extension['schemaVersion']}")
        if document.get("conversion", {}).get("status") in {"complete", "complete-with-warnings"}:
            raise ExtensionValidationError(
                "unknown non-critical extension cannot claim a complete conversion: "
                f"{extension['extensionId']}"
            )
        return "opaque"
    if extension["schemaId"] != entry["schemaId"]:
        raise ExtensionValidationError(f"extension {extension['extensionId']} schemaId does not match registry")
    if extension["criticality"] != entry.get("criticality"):
        raise ExtensionValidationError(f"extension {extension['extensionId']} criticality does not match registry")
    if compatibility_lane == "major":
        raise ExtensionValidationError(f"extension {extension['extensionId']} requires a major-version migration")
    if compatibility_lane == "patch" and extension.get("schemaDigest") != entry.get("schemaDigest"):
        raise ExtensionValidationError(f"extension {extension['extensionId']} patch version is not schema-digest compatible")
    if compatibility_lane == "minor" and extension.get("migrationReceipt") is not None and not isinstance(extension.get("migrationReceipt"), dict):
        raise ExtensionValidationError(f"extension {extension['extensionId']} has an invalid minor migration receipt")

    reference_registry = reference_registry or _reference_registry(document, ids, node_kinds)
    target_collection = ids[target_id]
    allowed_collections = set(entry.get("targetCollections", []))
    if target_collection not in allowed_collections:
        raise ExtensionValidationError(
            f"extension {extension['extensionId']} target collection {target_collection} is not allowed"
        )
    allowed_kinds = set(entry.get("targetKinds", []))
    target_kind = reference_registry.get(target_id, {}).get("kind") or node_kinds.get(target_id)
    if allowed_kinds and target_kind not in allowed_kinds:
        raise ExtensionValidationError(f"extension {extension['extensionId']} target kind {target_kind} is not allowed")
    _validate_payload(extension["payload"], entry, f"$.extensions[{extension['extensionId']}].payload")
    _validate_payload_references(extension, entry, ids, reference_registry)
    if extension.get("compatibility", {}).get("unknownVersion") == "ignore":
        raise ExtensionValidationError(f"extension {extension['extensionId']} cannot ignore compatibility policy")
    return "known" if compatibility_lane == "exact" else "compatible"


def build_extension(
    *,
    extension_id: str,
    target_id: str,
    namespace: str,
    extension_type: str,
    payload: ExtensionPayload,
    criticality: str = "non-critical",
    schema_version: str = "1.0.0",
) -> dict[str, Any]:
    """Construct a registry-backed extension envelope for adapter emission."""

    registry = load_registry()
    entry = _entry_map(registry).get((namespace, extension_type, schema_version))
    if entry is None:
        raise ExtensionValidationError(f"cannot construct unregistered extension: {namespace}:{extension_type}:{schema_version}")
    if not isinstance(payload, Mapping):
        raise ExtensionValidationError(f"payload for {namespace}:{extension_type} must be a typed mapping")
    payload_dict = dict(payload)
    _validate_payload(payload_dict, entry, f"extension {extension_id}.payload")
    return {
        "extensionId": extension_id,
        "targetId": target_id,
        "namespace": namespace,
        "type": extension_type,
        "schemaVersion": schema_version,
        "schemaId": entry["schemaId"],
        "payload": payload_dict,
        "criticality": criticality,
    }


def validate_registry_integrity() -> dict[str, int]:
    registry = load_registry()
    for entry in registry["entries"]:
        _payload_schema(entry)
    return {"entries": len(registry["entries"]), "schemas": len({entry["schemaPath"] for entry in registry["entries"]})}
