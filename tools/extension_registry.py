"""Typed extension registry and payload validation for FDIR."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"


class ExtensionValidationError(ValueError):
    """Raised when an extension violates the registry contract."""

    code = "DFIR-EXTENSION-INVALID"


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


def validate_extension(extension: dict[str, Any], document: dict[str, Any], ids: dict[str, str], node_kinds: dict[str, str]) -> str:
    """Validate one extension and return ``known`` or ``opaque``.

    Unknown non-critical extensions are retained as opaque only for partial
    documents.  This keeps forward-compatible reads explicit and prevents an
    unknown payload from silently supporting a complete claim.
    """

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
    entry = _entry_map(registry).get((extension["namespace"], extension["type"], extension["schemaVersion"]))
    if entry is None:
        if extension["criticality"] == "critical":
            raise ExtensionValidationError(f"unknown critical extension: {extension['namespace']}:{extension['type']}:{extension['schemaVersion']}")
        if document.get("conversion", {}).get("status") == "complete":
            raise ExtensionValidationError(f"unknown non-critical extension cannot be complete: {extension['extensionId']}")
        return "opaque"
    if extension["criticality"] != entry.get("criticality"):
        raise ExtensionValidationError(
            f"extension {extension['extensionId']} criticality does not match registry policy",
        )
    if extension["schemaId"] != entry["schemaId"]:
        raise ExtensionValidationError(f"extension {extension['extensionId']} schemaId does not match registry")
    allowed_collections = entry.get("targetCollections")
    if not isinstance(allowed_collections, list):
        # Current registry entries target node variants; keeping this default
        # explicit preserves compatibility while the contract census reports
        # the collection-level target policy.
        allowed_collections = ["nodes"] if entry.get("targetKinds") else []
    if allowed_collections and ids.get(target_id) not in allowed_collections:
        raise ExtensionValidationError(f"extension {extension['extensionId']} target collection {ids.get(target_id)} is not allowed")
    allowed = entry.get("targetKinds", [])
    target_kind = node_kinds.get(target_id)
    if allowed and target_kind not in allowed and target_id in node_kinds:
        raise ExtensionValidationError(f"extension {extension['extensionId']} target kind {target_kind} is not allowed")
    try:
        from ir_validation import _validate_schema  # type: ignore  # imported lazily to avoid a module cycle
    except ImportError:  # pragma: no cover - package-style import
        from tools.ir_validation import _validate_schema  # type: ignore

    schema = _load_json(ROOT / entry["schemaPath"].split("#", 1)[0])
    payload_schema = _resolve_schema_ref(schema, "#" + entry["schemaPath"].split("#", 1)[1])
    _validate_schema(extension["payload"], payload_schema, schema, f"$.extensions[{extension['extensionId']}].payload")
    if extension.get("compatibility", {}).get("unknownVersion") == "ignore":
        raise ExtensionValidationError(f"extension {extension['extensionId']} cannot ignore compatibility policy")
    return "known"


def validate_registry_integrity() -> dict[str, int]:
    registry = load_registry()
    for entry in registry["entries"]:
        _payload_schema(entry)
    return {"entries": len(registry["entries"]), "schemas": len({entry["schemaPath"] for entry in registry["entries"]})}
