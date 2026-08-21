"""Standalone, persistent SQLite index for a bounded #103 query slice.

This module intentionally does not import :mod:`query_ir` or
:mod:`query_qualification`.  It rebuilds a projection from the validated IR
file, stores every entity field at a JSON-pointer path, and binds the SQLite
file to the source and contract files that were used to build it.

The sidecar manifest and the database are replaced independently with
``os.replace``.  A reader therefore treats a pair observed during the small
replacement window as invalid (the database digest and manifest digest do
not agree) instead of accepting a partially replaced index.  Readers keep a
read-only SQLite connection and re-check the database digest before each
operation; a caller may retry after a concurrent replacement.

This is a bounded slice of Issue #103.  It provides persistent full-field
storage, exact typed field lookup, entity listing, and reverse-reference
lookup.  It does not claim to implement the complete query surface or the
issue's full independent qualification reports.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
DEFAULT_MODEL_CONTRACT_PATH = ROOT / "machine" / "model-contract.json"
DEFAULT_REFERENCE_REGISTRY_PATH = ROOT / "machine" / "reference-registry.json"
DEFAULT_EXTENSION_REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
DEFAULT_QUERY_CONTRACT_PATH = ROOT / "machine" / "query-contract.json"
DEFAULT_CAPABILITY_PROFILE_PATH = ROOT / "machine" / "capability-profile.json"

INDEX_SCHEMA = "fdir/independent-sqlite-index"
INDEX_VERSION = "1.1.0"
SQLITE_USER_VERSION = 2
CANONICALIZATION = "FDIR-IIDX-C14N-1"
BUILDER_NAME = "fdir.independent_index"
BUILDER_VERSION = "1.1.0"
MANIFEST_SUFFIX = ".manifest.json"
DOCUMENT_COLLECTION = "__document__"
_MISSING = object()

TABLES = {"metadata", "entities", "fields", "reverse_references"}
MANIFEST_CORE_KEYS = {
    "schema",
    "indexVersion",
    "canonicalization",
    "source",
    "bindings",
    "contractVersions",
    "capabilityProfileIds",
    "applicableCapabilityProfileIds",
    "indexSchemaVersion",
    "builder",
    "counts",
    "integrity",
    "querySurface",
    "build",
}
MANIFEST_KEYS = MANIFEST_CORE_KEYS | {"databaseSha256", "integrityChecksum"}


class IndependentIndexError(RuntimeError):
    """Raised when an index cannot be safely built, opened, or queried."""


class IndexFieldNotFound(IndependentIndexError):
    """Raised for a missing field path, distinct from a JSON null value."""


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {token}")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IndependentIndexError(f"value is not canonical JSON: {exc}") from exc


def _encode_query_cursor(value: dict[str, Any]) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_query_cursor(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise IndependentIndexError("query cursor must be a non-empty string")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        result = json.loads(decoded.decode("utf-8"), object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndependentIndexError("query cursor is malformed") from exc
    if not isinstance(result, dict):
        raise IndependentIndexError("query cursor is not an object")
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, UnicodeError) as exc:
        raise IndependentIndexError(f"cannot hash {path}: {exc}") from exc


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise IndependentIndexError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IndependentIndexError(f"{label} must be a JSON object: {path}")
    return value


def _pointer_segments(pointer: str) -> list[str]:
    if pointer in {"", "/"}:
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise IndependentIndexError("field pointer must be empty or start with '/'")
    return pointer[1:].split("/")


def _pointer_matches(template: str, pointer: str) -> bool:
    return _pointer_segments_match(_pointer_segments(template), _pointer_segments(pointer))


def _pointer_segments_match(template_segments: tuple[str, ...] | list[str], pointer_segments: tuple[str, ...] | list[str]) -> bool:
    index = 0
    for segment in template_segments:
        if segment == "**":
            return index <= len(pointer_segments)
        if index >= len(pointer_segments):
            return False
        if segment != "*" and segment != pointer_segments[index]:
            return False
        index += 1
    return index == len(pointer_segments)


def _extension_identity_matches(
    field: dict[str, Any],
    extension_type: str | None,
    extension_namespace: str | None,
    extension_version: str | None,
) -> bool:
    metadata = field.get("extension")
    if not isinstance(metadata, dict):
        return True
    return (
        (extension_type is None or metadata.get("type") == extension_type)
        and (extension_namespace is None or metadata.get("namespace") == extension_namespace)
        and (extension_version is None or metadata.get("schemaVersion") == extension_version)
    )


class _IndexedFieldRegistry(dict[str, list[dict[str, Any]]]):
    """Dictionary-compatible field registry with constant-time path buckets."""

    def __init__(self) -> None:
        super().__init__()
        self.all_candidates: dict[str, list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self.by_first: dict[tuple[str, str], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self.exact: dict[tuple[str, str], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self.wildcards: dict[tuple[str, str, int | None], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}


def _query_contract_sources_valid(query_contract: dict[str, Any], root: Path) -> None:
    generated = query_contract.get("generated")
    sources = generated.get("sources") if isinstance(generated, dict) else None
    if not isinstance(sources, list) or not sources:
        raise IndependentIndexError("query contract has no generated source bindings")
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
            raise IndependentIndexError("query contract source binding is malformed")
        path = (root / source["path"]).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise IndependentIndexError("query contract source escapes repository root") from exc
        if _file_sha256(path) != source["sha256"]:
            raise IndependentIndexError(f"query contract source is stale: {source['path']}")


def _query_field_registry(query_contract: dict[str, Any]) -> _IndexedFieldRegistry:
    groups = (
        query_contract.get("fieldPaths"),
        query_contract.get("documentFieldPaths"),
        query_contract.get("extensionFieldPaths"),
    )
    if not all(isinstance(group, list) for group in groups):
        raise IndependentIndexError("query contract field registry is incomplete")
    total = sum(len(group) for group in groups if isinstance(group, list))
    if query_contract.get("fieldPathCount") != total:
        raise IndependentIndexError("query contract field path count is inconsistent")
    result = _IndexedFieldRegistry()
    seen: set[str] = set()
    ordinal = 0
    for group in groups:
        assert isinstance(group, list)
        for field in group:
            if not isinstance(field, dict) or not isinstance(field.get("fieldId"), str) or not isinstance(field.get("ownerCollection"), str) or not isinstance(field.get("path"), str):
                raise IndependentIndexError("query contract contains a malformed field")
            if field["fieldId"] in seen:
                raise IndependentIndexError(f"query contract contains duplicate field id: {field['fieldId']}")
            seen.add(field["fieldId"])
            collection = field["ownerCollection"]
            path = str(field["path"])
            result.setdefault(collection, []).append(field)
            segments = tuple(_pointer_segments(path))
            candidate = (ordinal, segments, field)
            result.all_candidates.setdefault(collection, []).append(candidate)
            first = segments[0] if segments else ""
            result.by_first.setdefault((collection, first), []).append(candidate)
            if "*" not in segments and "**" not in segments:
                result.exact.setdefault((collection, path), []).append(candidate)
            else:
                length = None if "**" in segments else len(segments)
                result.wildcards.setdefault((collection, first, length), []).append(candidate)
            ordinal += 1
    return result


def _registered_query_field(
    registry: dict[str, list[dict[str, Any]]],
    collection: str,
    pointer: str,
    extension_type: str | None = None,
    extension_namespace: str | None = None,
    extension_version: str | None = None,
) -> dict[str, Any] | None:
    if pointer in {"", "/"}:
        return None
    if not isinstance(registry, _IndexedFieldRegistry):
        return next(
            (
                field for field in registry.get(collection, [])
                if _extension_identity_matches(field, extension_type, extension_namespace, extension_version)
                and _pointer_matches(str(field["path"]), pointer)
            ),
            None,
        )
    pointer_segments = tuple(_pointer_segments(pointer))
    first = pointer_segments[0] if pointer_segments else ""
    candidates = list(registry.exact.get((collection, pointer), ()))
    for template_first in (first, "*", "**"):
        for length in (len(pointer_segments), None):
            candidates.extend(registry.wildcards.get((collection, template_first, length), ()))
    for _, template_segments, field in sorted(candidates, key=lambda item: item[0]):
        if _extension_identity_matches(field, extension_type, extension_namespace, extension_version) and _pointer_segments_match(template_segments, pointer_segments):
            return field
    return None


def _matching_query_fields(
    registry: dict[str, list[dict[str, Any]]],
    collection: str,
    pointer: str,
    extension_type: str | None = None,
    extension_namespace: str | None = None,
    extension_version: str | None = None,
) -> list[dict[str, Any]]:
    """Return query-template matches without scanning unrelated fields."""

    if not isinstance(registry, _IndexedFieldRegistry):
        return [
            field for field in registry.get(collection, [])
            if _extension_identity_matches(field, extension_type, extension_namespace, extension_version)
            and (_pointer_matches(str(field["path"]), pointer) or _pointer_matches(pointer, str(field["path"])))
        ]
    pointer_segments = tuple(_pointer_segments(pointer))
    first = pointer_segments[0] if pointer_segments else ""
    if "*" in pointer_segments or "**" in pointer_segments:
        if first in {"*", "**"}:
            candidates = list(registry.all_candidates.get(collection, ()))
        else:
            candidates = []
            for candidate_first in (first, "*", "**"):
                candidates.extend(registry.by_first.get((collection, candidate_first), ()))
    else:
        candidates = list(registry.exact.get((collection, pointer), ()))
        for candidate_first in (first, "*", "**"):
            for length in (len(pointer_segments), None):
                candidates.extend(registry.wildcards.get((collection, candidate_first, length), ()))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, template_segments, field in sorted(candidates, key=lambda item: item[0]):
        field_id = str(field["fieldId"])
        if field_id in seen or not _extension_identity_matches(field, extension_type, extension_namespace, extension_version):
            continue
        if _pointer_segments_match(template_segments, pointer_segments) or _pointer_segments_match(pointer_segments, template_segments):
            seen.add(field_id)
            result.append(field)
    return result


def manifest_path_for(index_path: Path) -> Path:
    """Return the sidecar manifest path for an SQLite index path."""

    return index_path.with_name(index_path.name + MANIFEST_SUFFIX)


def _derive_collection_contract(schema: dict[str, Any]) -> dict[str, str]:
    """Derive collection identifiers from the normative schema itself.

    A new top-level array with no unambiguous entity identifier is rejected.
    This prevents a schema/model addition from silently disappearing from
    this independent builder.
    """

    properties = schema.get("properties")
    definitions = schema.get("$defs")
    if not isinstance(properties, dict) or not isinstance(definitions, dict):
        raise IndependentIndexError("normative schema has no usable properties/$defs")
    result: dict[str, str] = {}
    for collection, specification in properties.items():
        if not isinstance(specification, dict) or specification.get("type") != "array":
            continue
        items = specification.get("items")
        reference = items.get("$ref") if isinstance(items, dict) else None
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise IndependentIndexError(f"collection has no local item definition: {collection}")
        definition = definitions.get(reference[len("#/$defs/"):])
        required = definition.get("required") if isinstance(definition, dict) else None
        if collection.endswith("ies"):
            singular = collection[:-3] + "y"
        elif collection.endswith("s"):
            singular = collection[:-1]
        else:
            singular = collection
        identifiers = [
            item for item in required or []
            if isinstance(item, str) and item == f"{singular}Id"
        ]
        if len(identifiers) != 1:
            raise IndependentIndexError(
                f"collection identifier mapping is ambiguous in schema: {collection}"
            )
        result[collection] = identifiers[0]
    if "nodes" not in result:
        raise IndependentIndexError("normative schema has no nodes collection")
    return dict(sorted(result.items()))


def _validate_model_collection_contract(
    model_contract: dict[str, Any],
    collection_contract: dict[str, str],
) -> None:
    collections = model_contract.get("collections")
    if not isinstance(collections, list):
        raise IndependentIndexError("model contract has no collections list")
    model_mapping: dict[str, str] = {}
    for item in collections:
        if not isinstance(item, dict):
            raise IndependentIndexError("model contract contains a non-object collection")
        name = item.get("name")
        identifier = item.get("idField")
        if not isinstance(name, str) or not isinstance(identifier, str):
            raise IndependentIndexError("model collection is missing name/idField")
        if name in model_mapping:
            raise IndependentIndexError(f"duplicate model collection: {name}")
        model_mapping[name] = identifier
    if model_mapping != collection_contract:
        raise IndependentIndexError(
            "schema/model collection mapping drift; refusing to build an incomplete index"
        )


def _validate_document_surface(
    document: dict[str, Any],
    collection_contract: dict[str, str],
) -> None:
    """Perform independent shape checks before invoking the authority validator."""

    for required in ("schema", "documentId", "sourceFormat", "rootNodeId", "nodes", "conversion"):
        if required not in document:
            raise IndependentIndexError(f"IR is missing required top-level field: {required}")
    if not isinstance(document.get("documentId"), str) or not document["documentId"]:
        raise IndependentIndexError("IR documentId must be a non-empty string")
    if not isinstance(document.get("schema"), dict) or not isinstance(document["schema"].get("version"), str):
        raise IndependentIndexError("IR schema reference is malformed")
    if not isinstance(document.get("sourceFormat"), dict):
        raise IndependentIndexError("IR sourceFormat must be an object")

    known_top_level = {
        "schema", "documentId", "sourceFormat", "rootNodeId", "conversion",
        *collection_contract,
    }
    for key, value in document.items():
        if isinstance(value, list) and key not in known_top_level:
            raise IndependentIndexError(
                f"unsupported top-level array is not indexed: {key}"
            )

    all_ids: dict[str, str] = {}
    for collection, identifier_field in collection_contract.items():
        values = document.get(collection, [])
        if not isinstance(values, list):
            raise IndependentIndexError(f"IR collection is not an array: {collection}")
        for item in values:
            if not isinstance(item, dict):
                raise IndependentIndexError(f"IR collection item is not an object: {collection}")
            identifier = item.get(identifier_field)
            if not isinstance(identifier, str) or not identifier:
                raise IndependentIndexError(
                    f"IR item has no string identifier: {collection}.{identifier_field}"
                )
            if identifier in all_ids:
                raise IndependentIndexError(f"duplicate IR entity identifier: {identifier}")
            all_ids[identifier] = collection

    # Reuse only the authority validator; the query/index implementation is
    # deliberately not imported.  A source that the normative validator
    # rejects must never become an index input.
    try:
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        from ir_validation import IRValidationError, validate_document

        validate_document(document)
    except ImportError as exc:  # pragma: no cover - repository packaging failure
        raise IndependentIndexError(f"authority validator is unavailable: {exc}") from exc
    except IRValidationError as exc:
        raise IndependentIndexError(f"IR failed authority validation: {exc}") from exc
    except Exception as exc:
        raise IndependentIndexError(f"IR authority validation failed: {exc}") from exc


def _load_document(
    source_path: Path,
    collection_contract: dict[str, str],
) -> tuple[dict[str, Any], str, str]:
    document = _load_json(source_path, "IR")
    _validate_document_surface(document, collection_contract)
    return document, _file_sha256(source_path), _sha256_json(document)


def _contract_context(
    document: dict[str, Any],
    *,
    schema_path: Path,
    model_contract_path: Path,
    reference_registry_path: Path,
    extension_registry_path: Path,
    query_contract_path: Path,
    capability_profile_path: Path,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    schema = _load_json(schema_path, "IR schema")
    collection_contract = _derive_collection_contract(schema)
    model_contract = _load_json(model_contract_path, "model contract")
    _validate_model_collection_contract(model_contract, collection_contract)
    reference_registry = _load_json(reference_registry_path, "reference registry")
    extension_registry = _load_json(extension_registry_path, "extension registry")
    if not isinstance(extension_registry.get("entries"), list) or not extension_registry["entries"]:
        raise IndependentIndexError("extension registry has no entries")
    query_contract = _load_json(query_contract_path, "query contract")
    if query_contract.get("schema") != "fdir/document-form-query-contract":
        raise IndependentIndexError("query contract schema is invalid")
    _query_contract_sources_valid(query_contract, ROOT)
    field_registry = _query_field_registry(query_contract)
    contract_collections = {
        item.get("name"): item.get("idField")
        for item in query_contract.get("collections", [])
        if isinstance(item, dict)
    }
    if contract_collections != collection_contract:
        raise IndependentIndexError("query contract collection mapping is stale or incomplete")
    if any(collection not in field_registry for collection in collection_contract):
        raise IndependentIndexError("query contract has no field paths for a model collection")
    if DOCUMENT_COLLECTION not in field_registry:
        raise IndependentIndexError("query contract has no document field paths")
    capability_profile = _load_json(capability_profile_path, "capability profile")
    profiles = capability_profile.get("profiles")
    if not isinstance(profiles, list):
        raise IndependentIndexError("capability profile has no profiles list")
    profile_ids = sorted(
        item["id"] for item in profiles
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    if not profile_ids:
        raise IndependentIndexError("capability profile has no profile ids")
    applicable = document.get("conversion", {}).get("capabilityProfile")
    if not isinstance(applicable, str) or applicable not in profile_ids:
        raise IndependentIndexError(
            f"source capability profile is not registered: {applicable!r}"
        )

    bindings = {
        "irSchemaSha256": _file_sha256(schema_path),
        "modelContractSha256": _file_sha256(model_contract_path),
        "referenceRegistrySha256": _file_sha256(reference_registry_path),
        "extensionRegistrySha256": _file_sha256(extension_registry_path),
        "queryContractSha256": _file_sha256(query_contract_path),
        "capabilityProfileSha256": _file_sha256(capability_profile_path),
    }
    contract_metadata = {
        "profileIds": profile_ids,
        "applicableProfileIds": [applicable],
        "bindings": bindings,
        "contractVersions": {
            "irSchema": schema.get("$id", schema.get("version")),
            "model": model_contract.get("version"),
            "referenceRegistry": reference_registry.get("version"),
            "extensionRegistry": extension_registry.get("version"),
            "queryContract": query_contract.get("version"),
            "capabilityProfile": capability_profile.get("version"),
        },
        "queryContractVersion": query_contract.get("version"),
        "registeredFieldPathCount": query_contract.get("fieldPathCount"),
    }
    return collection_contract, contract_metadata, {
        "schema": schema,
        "model": model_contract,
        "queryContract": query_contract,
        "fieldRegistry": field_registry,
    }


def _escape_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk_fields(value: Any, pointer: str = "") -> Iterable[tuple[str, Any]]:
    yield pointer, value
    if isinstance(value, dict):
        for key in sorted(value):
            child_pointer = f"{pointer}/{_escape_pointer_segment(key)}"
            yield from _walk_fields(value[key], child_pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_fields(child, f"{pointer}/{index}")


def _value_type(value: Any) -> str:
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
        return "list"
    if isinstance(value, dict):
        return "object"
    raise IndependentIndexError(f"unsupported JSON value type: {type(value).__name__}")


def _typed_equal(left: Any, right: Any) -> bool:
    # Keep bool, integer, and number lanes distinct; Python's ``True == 1``
    # is not the query contract's typed equality.
    if _value_type(left) != _value_type(right):
        return False
    return left == right


def _typed_compare(left: Any, right: Any, operator: str) -> bool:
    if operator in {"eq", "neq"}:
        equal = _typed_equal(left, right)
        return equal if operator == "eq" else not equal
    if operator in {"lt", "lte", "gt", "gte"}:
        if _value_type(left) not in {"integer", "number", "string"} or _value_type(right) not in {"integer", "number", "string"}:
            raise IndependentIndexError(f"{operator} requires comparable scalar values")
        if _value_type(left) != _value_type(right):
            raise IndependentIndexError("typed comparison cannot mix scalar lanes")
        if operator == "lt":
            return left < right
        if operator == "lte":
            return left <= right
        if operator == "gt":
            return left > right
        return left >= right
    if operator == "prefix":
        return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
    if operator == "contains":
        if isinstance(left, str) and isinstance(right, str):
            return right in left
        if isinstance(left, list):
            return any(_typed_equal(item, right) for item in left)
        return False
    raise IndependentIndexError(f"unknown field operator: {operator}")


def _known_ids(document: dict[str, Any], collection_contract: dict[str, str]) -> dict[str, str]:
    return {
        item[identifier_field]: collection
        for collection, identifier_field in collection_contract.items()
        for item in document.get(collection, [])
    }


def _reference_rows_for_entity(
    entity: dict[str, Any],
    *,
    collection: str,
    identifier: str,
    identifier_field: str,
    known_ids: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(pointer: str, target: str, kind: str, ordinal: int | None = None) -> None:
        if target in known_ids and not (
            pointer == f"/{_escape_pointer_segment(identifier_field)}"
        ):
            rows.append({
                "sourceCollection": collection,
                "sourceId": identifier,
                "sourcePointer": pointer,
                "targetId": target,
                "targetCollection": known_ids[target],
                "referenceKind": kind,
                "ordinal": ordinal,
            })

    def walk(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                child = value[key]
                child_pointer = f"{pointer}/{_escape_pointer_segment(key)}"
                if key.endswith("Id") and isinstance(child, str):
                    add(child_pointer, child, "id-field")
                elif key.endswith("Ids") and isinstance(child, list):
                    for ordinal, target in enumerate(child):
                        if isinstance(target, str):
                            add(f"{child_pointer}/{ordinal}", target, "id-list", ordinal)
                if key == "id" and isinstance(child, str) and "type" in value:
                    add(child_pointer, child, "typed-object")
                walk(child, child_pointer)
        elif isinstance(value, list):
            for ordinal, child in enumerate(value):
                walk(child, f"{pointer}/{ordinal}")

    walk(entity, "")
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        identity = tuple(row[key] for key in (
            "sourceCollection", "sourceId", "sourcePointer", "targetId", "referenceKind", "ordinal"
        ))
        unique[identity] = row
    return sorted(
        unique.values(),
        key=lambda row: (
            row["sourceCollection"], row["sourceId"], row["sourcePointer"],
            row["targetId"], row["referenceKind"], -1 if row["ordinal"] is None else row["ordinal"],
        ),
    )


def _extract_records(
    document: dict[str, Any],
    collection_contract: dict[str, str],
    field_registry: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    known_ids = _known_ids(document, collection_contract)
    known_ids[document["documentId"]] = DOCUMENT_COLLECTION

    for collection, identifier_field in collection_contract.items():
        for ordinal, entity in enumerate(document.get(collection, [])):
            identifier = entity[identifier_field]
            payload_json = _canonical_json(entity)
            entities.append({
                "collection": collection,
                "entityId": identifier,
                "kind": entity.get("kind") if isinstance(entity.get("kind"), str) else None,
                "status": entity.get("status") if isinstance(entity.get("status"), str) else None,
                "ordinal": ordinal,
                "payloadJson": payload_json,
                "payloadSha256": _sha256_bytes(payload_json.encode("utf-8")),
            })
            for pointer, value in _walk_fields(entity):
                extension_type = entity.get("type") if collection == "extensions" and isinstance(entity.get("type"), str) else None
                extension_namespace = entity.get("namespace") if collection == "extensions" and isinstance(entity.get("namespace"), str) else None
                extension_version = entity.get("schemaVersion") if collection == "extensions" and isinstance(entity.get("schemaVersion"), str) else None
                if pointer and _registered_query_field(field_registry, collection, pointer, extension_type, extension_namespace, extension_version) is None:
                    raise IndependentIndexError(
                        f"observed field path is absent from query contract: {collection}{pointer}"
                    )
                value_json = _canonical_json(value)
                fields.append({
                    "collection": collection,
                    "entityId": identifier,
                    "pointer": pointer,
                    "valueJson": value_json,
                    "valueSha256": _sha256_bytes(value_json.encode("utf-8")),
                    "valueType": _value_type(value),
                    "isNull": 1 if value is None else 0,
                })
            references.extend(_reference_rows_for_entity(
                entity,
                collection=collection,
                identifier=identifier,
                identifier_field=identifier_field,
                known_ids=known_ids,
            ))

    document_identifier = str(document["documentId"])
    document_names = {
        str(field["path"]).split("/", 2)[1].replace("~1", "/").replace("~0", "~")
        for field in field_registry.get(DOCUMENT_COLLECTION, [])
        if isinstance(field, dict) and isinstance(field.get("path"), str) and str(field["path"]).startswith("/")
    }
    document_surface = {
        name: document[name]
        for name in sorted(document_names)
        if name in document
    }
    document_payload = {"documentId": document_identifier, **document_surface}
    for pointer, value in _walk_fields(document_payload):
        if pointer and _registered_query_field(field_registry, DOCUMENT_COLLECTION, pointer) is None:
            raise IndependentIndexError(
                f"observed document field path is absent from query contract: {pointer}"
            )
        value_json = _canonical_json(value)
        fields.append({
            "collection": DOCUMENT_COLLECTION,
            "entityId": document_identifier,
            "pointer": pointer,
            "valueJson": value_json,
            "valueSha256": _sha256_bytes(value_json.encode("utf-8")),
            "valueType": _value_type(value),
            "isNull": 1 if value is None else 0,
        })
    references.extend(_reference_rows_for_entity(
        document_payload,
        collection=DOCUMENT_COLLECTION,
        identifier=document_identifier,
        identifier_field="documentId",
        known_ids=known_ids,
    ))

    entities.sort(key=lambda row: (row["collection"], row["entityId"]))
    fields.sort(key=lambda row: (row["collection"], row["entityId"], row["pointer"]))
    references.sort(key=lambda row: (
        row["sourceCollection"], row["sourceId"], row["sourcePointer"],
        row["targetId"], row["referenceKind"], -1 if row["ordinal"] is None else row["ordinal"],
    ))
    return {"entities": entities, "fields": fields, "references": references}


def _records_digest(records: dict[str, list[dict[str, Any]]]) -> str:
    payload = {
        "entities": [
            [row[key] for key in ("collection", "entityId", "kind", "status", "ordinal", "payloadSha256")]
            for row in records["entities"]
        ],
        "fields": [
            [row[key] for key in ("collection", "entityId", "pointer", "valueSha256", "valueType", "isNull")]
            for row in records["fields"]
        ],
        "references": [
            [row[key] for key in (
                "sourceCollection", "sourceId", "sourcePointer", "targetId",
                "targetCollection", "referenceKind", "ordinal",
            )]
            for row in records["references"]
        ],
    }
    return _sha256_json(payload)


def _counts(
    records: dict[str, list[dict[str, Any]]],
    collection_contract: dict[str, str],
) -> dict[str, Any]:
    return {
        "entities": len(records["entities"]),
        "fields": len(records["fields"]),
        "documentFields": sum(1 for row in records["fields"] if row["collection"] == DOCUMENT_COLLECTION),
        "entityFields": sum(1 for row in records["fields"] if row["collection"] != DOCUMENT_COLLECTION),
        "references": len(records["references"]),
        "collections": {
            collection: sum(1 for row in records["entities"] if row["collection"] == collection)
            for collection in sorted(collection_contract)
        },
    }


def _build_context(
    source_path: Path,
    *,
    schema_path: Path,
    model_contract_path: Path,
    reference_registry_path: Path,
    extension_registry_path: Path,
    query_contract_path: Path,
    capability_profile_path: Path,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    schema = _load_json(schema_path, "IR schema")
    collection_contract = _derive_collection_contract(schema)
    document, source_file_sha256, source_canonical_digest = _load_document(
        source_path, collection_contract
    )
    _, contract_metadata, contract_artifacts = _contract_context(
        document,
        schema_path=schema_path,
        model_contract_path=model_contract_path,
        reference_registry_path=reference_registry_path,
        extension_registry_path=extension_registry_path,
        query_contract_path=query_contract_path,
        capability_profile_path=capability_profile_path,
    )
    records = _extract_records(document, collection_contract, contract_artifacts["fieldRegistry"])
    context = {
        "document": document,
        "sourceFileSha256": source_file_sha256,
        "sourceCanonicalDigest": source_canonical_digest,
        "collectionContract": collection_contract,
        "contractMetadata": contract_metadata,
        "fieldRegistry": contract_artifacts["fieldRegistry"],
        "queryContract": contract_artifacts["queryContract"],
        "recordsDigest": _records_digest(records),
    }
    return context, collection_contract, contract_metadata, records


def _manifest_core(
    context: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    *,
    build_timestamp: str,
) -> dict[str, Any]:
    document = context["document"]
    contract_metadata = context["contractMetadata"]
    collection_contract = context["collectionContract"]
    return {
        "schema": INDEX_SCHEMA,
        "indexVersion": INDEX_VERSION,
        "canonicalization": CANONICALIZATION,
        "source": {
            "documentId": document["documentId"],
            "sourceFormat": document["sourceFormat"],
            "sourceFileSha256": context["sourceFileSha256"],
            "canonicalDigest": context["sourceCanonicalDigest"],
        },
        "bindings": contract_metadata["bindings"],
        "contractVersions": contract_metadata["contractVersions"],
        "capabilityProfileIds": contract_metadata["profileIds"],
        "applicableCapabilityProfileIds": contract_metadata["applicableProfileIds"],
        "indexSchemaVersion": SQLITE_USER_VERSION,
        "builder": {"name": BUILDER_NAME, "version": BUILDER_VERSION},
        "counts": _counts(records, collection_contract),
        "integrity": {"recordsDigest": context["recordsDigest"]},
        "querySurface": {
            "contractVersion": contract_metadata["queryContractVersion"],
            "registeredFieldPathCount": contract_metadata["registeredFieldPathCount"],
        },
        "build": {
            "timestampUtc": build_timestamp,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }


def _manifest_checksum(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "integrityChecksum"}
    return _sha256_json(payload)


def _create_sqlite(
    path: Path,
    core: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    collection_contract: dict[str, str],
) -> None:
    try:
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA user_version = {SQLITE_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE entities (
                collection TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_kind TEXT,
                entity_status TEXT,
                source_ordinal INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                PRIMARY KEY (collection, entity_id)
            );
            CREATE TABLE fields (
                collection TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                pointer TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_sha256 TEXT NOT NULL,
                value_type TEXT NOT NULL,
                is_null INTEGER NOT NULL CHECK (is_null IN (0, 1)),
                PRIMARY KEY (collection, entity_id, pointer)
            );
            CREATE TABLE reverse_references (
                source_collection TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_pointer TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_collection TEXT NOT NULL,
                reference_kind TEXT NOT NULL,
                ordinal INTEGER,
                PRIMARY KEY (
                    source_collection, source_id, source_pointer,
                    target_id, reference_kind, ordinal
                )
            );
            CREATE INDEX fields_by_value ON fields(value_sha256, value_type);
            CREATE INDEX references_by_target ON reverse_references(target_id, target_collection);
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("manifestCoreJson", _canonical_json(core)),
                ("manifestCoreSha256", _sha256_json(core)),
                ("recordsDigest", core["integrity"]["recordsDigest"]),
                ("collectionContract", _canonical_json(collection_contract)),
            ],
        )
        connection.executemany(
            """
            INSERT INTO entities(
                collection, entity_id, entity_kind, entity_status,
                source_ordinal, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["collection"], row["entityId"], row["kind"], row["status"],
                    row["ordinal"], row["payloadJson"], row["payloadSha256"],
                )
                for row in records["entities"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO fields(
                collection, entity_id, pointer, value_json,
                value_sha256, value_type, is_null
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["collection"], row["entityId"], row["pointer"], row["valueJson"],
                    row["valueSha256"], row["valueType"], row["isNull"],
                )
                for row in records["fields"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO reverse_references(
                source_collection, source_id, source_pointer, target_id,
                target_collection, reference_kind, ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["sourceCollection"], row["sourceId"], row["sourcePointer"],
                    row["targetId"], row["targetCollection"], row["referenceKind"], row["ordinal"],
                )
                for row in records["references"]
            ],
        )
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise IndependentIndexError(f"new SQLite index failed integrity_check: {result}")
    except sqlite3.Error as exc:
        raise IndependentIndexError(f"cannot build SQLite index: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    data = (_canonical_json(value) + "\n").encode("utf-8")
    try:
        with path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise IndependentIndexError(f"cannot write manifest {path}: {exc}") from exc


def _stage_path(parent: Path, name: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".tmp", dir=str(parent)
    )
    os.close(descriptor)
    return Path(raw_path)


def build_index(
    source_path: Path,
    index_path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    model_contract_path: Path = DEFAULT_MODEL_CONTRACT_PATH,
    reference_registry_path: Path = DEFAULT_REFERENCE_REGISTRY_PATH,
    extension_registry_path: Path = DEFAULT_EXTENSION_REGISTRY_PATH,
    query_contract_path: Path = DEFAULT_QUERY_CONTRACT_PATH,
    capability_profile_path: Path = DEFAULT_CAPABILITY_PROFILE_PATH,
) -> dict[str, Any]:
    """Build and atomically replace a persistent independent index."""

    source_path = Path(source_path)
    index_path = Path(index_path)
    if source_path.resolve() == index_path.resolve():
        raise IndependentIndexError("source IR and index path must be different")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    context, collection_contract, _, records = _build_context(
        source_path,
        schema_path=Path(schema_path),
        model_contract_path=Path(model_contract_path),
        reference_registry_path=Path(reference_registry_path),
        extension_registry_path=Path(extension_registry_path),
        query_contract_path=Path(query_contract_path),
        capability_profile_path=Path(capability_profile_path),
    )
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    core = _manifest_core(context, records, build_timestamp=timestamp)
    staged_index = _stage_path(index_path.parent, index_path.name)
    staged_manifest = _stage_path(index_path.parent, manifest_path_for(index_path).name)
    try:
        _create_sqlite(staged_index, core, records, collection_contract)
        database_sha256 = _file_sha256(staged_index)
        manifest = dict(core)
        manifest["databaseSha256"] = database_sha256
        manifest["integrityChecksum"] = _manifest_checksum(manifest)
        _write_json_atomically(staged_manifest, manifest)
        os.replace(staged_index, index_path)
        os.replace(staged_manifest, manifest_path_for(index_path))
        return manifest
    except Exception:
        for path in (staged_index, staged_manifest):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _read_manifest(index_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path_for(index_path)
    manifest = _load_json(manifest_path, "index manifest")
    if set(manifest) != MANIFEST_KEYS:
        raise IndependentIndexError("index manifest has unexpected or missing fields")
    if manifest.get("schema") != INDEX_SCHEMA or manifest.get("indexVersion") != INDEX_VERSION:
        raise IndependentIndexError("unsupported index manifest schema/version")
    if manifest.get("indexSchemaVersion") != SQLITE_USER_VERSION:
        raise IndependentIndexError("unsupported newer SQLite index schema version")
    if not isinstance(manifest.get("databaseSha256"), str):
        raise IndependentIndexError("index manifest has no database digest")
    if manifest.get("integrityChecksum") != _manifest_checksum(manifest):
        raise IndependentIndexError("index manifest integrity checksum mismatch")
    core = {key: manifest[key] for key in MANIFEST_CORE_KEYS}
    if not isinstance(core.get("source"), dict) or not isinstance(core.get("bindings"), dict):
        raise IndependentIndexError("index manifest core is malformed")
    return manifest


def _verify_sqlite_shape(connection: sqlite3.Connection) -> None:
    try:
        if connection.execute("PRAGMA user_version").fetchone() != (SQLITE_USER_VERSION,):
            raise IndependentIndexError("SQLite user_version is unsupported")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise IndependentIndexError("SQLite quick_check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != TABLES:
            raise IndependentIndexError("SQLite index has unexpected or missing tables")
    except sqlite3.Error as exc:
        raise IndependentIndexError(f"SQLite index cannot be opened safely: {exc}") from exc


def _read_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    except sqlite3.Error as exc:
        raise IndependentIndexError(f"SQLite metadata cannot be read: {exc}") from exc
    metadata = {key: value for key, value in rows}
    if set(metadata) != {"manifestCoreJson", "manifestCoreSha256", "recordsDigest", "collectionContract"}:
        raise IndependentIndexError("SQLite metadata is incomplete or has extra keys")
    return metadata


def _records_from_sqlite(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    try:
        entities = [
            {
                "collection": row[0], "entityId": row[1], "kind": row[2], "status": row[3],
                "ordinal": row[4], "payloadJson": row[5], "payloadSha256": row[6],
            }
            for row in connection.execute(
                """
                SELECT collection, entity_id, entity_kind, entity_status,
                       source_ordinal, payload_json, payload_sha256
                FROM entities ORDER BY collection, entity_id
                """
            )
        ]
        fields = [
            {
                "collection": row[0], "entityId": row[1], "pointer": row[2],
                "valueJson": row[3], "valueSha256": row[4], "valueType": row[5], "isNull": row[6],
            }
            for row in connection.execute(
                """
                SELECT collection, entity_id, pointer, value_json,
                       value_sha256, value_type, is_null
                FROM fields ORDER BY collection, entity_id, pointer
                """
            )
        ]
        references = [
            {
                "sourceCollection": row[0], "sourceId": row[1], "sourcePointer": row[2],
                "targetId": row[3], "targetCollection": row[4], "referenceKind": row[5],
                "ordinal": row[6],
            }
            for row in connection.execute(
                """
                SELECT source_collection, source_id, source_pointer, target_id,
                       target_collection, reference_kind, ordinal
                FROM reverse_references
                ORDER BY source_collection, source_id, source_pointer,
                         target_id, reference_kind, COALESCE(ordinal, -1)
                """
            )
        ]
    except sqlite3.Error as exc:
        raise IndependentIndexError(f"SQLite index rows cannot be read: {exc}") from exc
    return {"entities": entities, "fields": fields, "references": references}


def _validate_records_self_integrity(records: dict[str, list[dict[str, Any]]]) -> None:
    """Validate stored JSON against each row's own digest and type lane."""

    for row in records["entities"]:
        try:
            payload = json.loads(
                row["payloadJson"],
                object_pairs_hook=_unique_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise IndependentIndexError("SQLite entity payload is not canonical JSON") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != row["payloadJson"]:
            raise IndependentIndexError("SQLite entity payload canonicalization mismatch")
        if _sha256_bytes(row["payloadJson"].encode("utf-8")) != row["payloadSha256"]:
            raise IndependentIndexError("SQLite entity payload digest mismatch")
        payload_kind = payload.get("kind") if isinstance(payload.get("kind"), str) else None
        payload_status = payload.get("status") if isinstance(payload.get("status"), str) else None
        if payload_kind != row["kind"] or payload_status != row["status"]:
            raise IndependentIndexError("SQLite entity summary does not match payload")

    for row in records["fields"]:
        try:
            value = json.loads(
                row["valueJson"],
                object_pairs_hook=_unique_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise IndependentIndexError("SQLite field value is not canonical JSON") from exc
        if _canonical_json(value) != row["valueJson"]:
            raise IndependentIndexError("SQLite field value canonicalization mismatch")
        if _sha256_bytes(row["valueJson"].encode("utf-8")) != row["valueSha256"]:
            raise IndependentIndexError("SQLite field value digest mismatch")
        if _value_type(value) != row["valueType"] or int(value is None) != row["isNull"]:
            raise IndependentIndexError("SQLite field type/null lane mismatch")


def _check_manifest_against_source(
    manifest: dict[str, Any],
    context: dict[str, Any],
    records: dict[str, list[dict[str, Any]]],
    collection_contract: dict[str, str],
) -> None:
    core = {key: manifest[key] for key in MANIFEST_CORE_KEYS}
    expected_counts = _counts(records, collection_contract)
    expected_source = {
        "documentId": context["document"]["documentId"],
        "sourceFormat": context["document"]["sourceFormat"],
        "sourceFileSha256": context["sourceFileSha256"],
        "canonicalDigest": context["sourceCanonicalDigest"],
    }
    expected_bindings = context["contractMetadata"]["bindings"]
    if core["source"] != expected_source:
        raise IndependentIndexError("index source binding does not match current IR")
    if core["bindings"] != expected_bindings:
        raise IndependentIndexError("index contract binding does not match current contracts")
    if core["contractVersions"] != context["contractMetadata"]["contractVersions"]:
        raise IndependentIndexError("index contract versions are stale")
    if core["capabilityProfileIds"] != context["contractMetadata"]["profileIds"]:
        raise IndependentIndexError("index capability profile registry binding is stale")
    if core["applicableCapabilityProfileIds"] != context["contractMetadata"]["applicableProfileIds"]:
        raise IndependentIndexError("index applicable capability profile binding is stale")
    if core["counts"] != expected_counts:
        raise IndependentIndexError("index row counts do not match current IR")
    if core["integrity"] != {"recordsDigest": context["recordsDigest"]}:
        raise IndependentIndexError("index records digest does not match current IR")
    expected_query_surface = {
        "contractVersion": context["contractMetadata"]["queryContractVersion"],
        "registeredFieldPathCount": context["contractMetadata"]["registeredFieldPathCount"],
    }
    if core["querySurface"] != expected_query_surface:
        raise IndependentIndexError("index query surface binding is stale")
    if core["canonicalization"] != CANONICALIZATION:
        raise IndependentIndexError("unsupported index canonicalization")
    if core["builder"] != {"name": BUILDER_NAME, "version": BUILDER_VERSION}:
        raise IndependentIndexError("unsupported index builder version")
    if not isinstance(core["build"].get("timestampUtc"), str):
        raise IndependentIndexError("index build timestamp is missing")


class PersistentIndex:
    """Read-only handle to a validated independent index."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        index_path: Path,
        manifest: dict[str, Any],
        collection_contract: dict[str, str],
        field_registry: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._connection = connection
        self.index_path = index_path
        self.manifest = manifest
        self.collection_contract = collection_contract
        self.field_registry = field_registry

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PersistentIndex":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _assert_live(self) -> None:
        if _file_sha256(self.index_path) != self.manifest["databaseSha256"]:
            raise IndependentIndexError(
                "index file changed after validation; refusing to query"
            )

    def _check_collection(self, collection: str) -> None:
        if collection != DOCUMENT_COLLECTION and collection not in self.collection_contract:
            raise IndependentIndexError(f"unknown indexed collection: {collection}")

    def _check_field(
        self,
        collection: str,
        pointer: str,
        extension_type: str | None = None,
        extension_namespace: str | None = None,
        extension_version: str | None = None,
    ) -> None:
        if pointer in {"", "/"}:
            raise IndependentIndexError("the entity/document root is not a field path")
        if _registered_query_field(
            self.field_registry,
            collection,
            pointer,
            extension_type,
            extension_namespace,
            extension_version,
        ) is None:
            raise IndependentIndexError(f"field pointer is not registered: {collection}{pointer}")

    def list_entities(
        self,
        collection: str | None = None,
        *,
        kind: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_live()
        if profile is not None:
            if profile not in set(self.manifest.get("capabilityProfileIds", [])):
                raise IndependentIndexError(f"unknown capability profile: {profile}")
            if profile not in self.manifest.get("applicableCapabilityProfileIds", []):
                return []
        if collection is not None:
            self._check_collection(collection)
        if offset < 0 or (limit is not None and limit < 0):
            raise IndependentIndexError("offset and limit must be non-negative")
        clauses: list[str] = []
        parameters: list[Any] = []
        if collection is not None:
            clauses.append("collection = ?")
            parameters.append(collection)
        if kind is not None:
            clauses.append("entity_kind = ?")
            parameters.append(kind)
        if status is not None:
            clauses.append("entity_status = ?")
            parameters.append(status)
        query = (
            "SELECT payload_json FROM entities"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY collection, entity_id LIMIT ? OFFSET ?"
        )
        parameters.extend([limit if limit is not None else -1, offset])
        try:
            return [json.loads(row[0]) for row in self._connection.execute(query, parameters)]
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"entity query failed: {exc}") from exc

    def list_entities_page(
        self,
        collection: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic entity page bound to this index digest."""

        self._check_collection(collection)
        max_page_size = 1000
        if limit < 1 or limit > max_page_size:
            raise IndependentIndexError(f"limit must be between 1 and {max_page_size}")
        profile_id = profile or (self.manifest.get("applicableCapabilityProfileIds") or [None])[0]
        if profile_id not in set(self.manifest.get("capabilityProfileIds", [])):
            raise IndependentIndexError(f"unknown capability profile: {profile_id}")
        digest = self.manifest["source"]["canonicalDigest"]
        last_id: str | None = None
        if cursor is not None:
            payload = _decode_query_cursor(cursor)
            expected = {
                "schema": "fdir/query-cursor",
                "queryContractVersion": self.manifest["querySurface"]["contractVersion"],
                "indexDigest": digest,
                "collection": collection,
                "kind": kind,
                "status": status,
                "profileId": profile_id,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise IndependentIndexError("query cursor is stale or belongs to another query/index")
            if not isinstance(payload.get("lastId"), str):
                raise IndependentIndexError("query cursor has no last entity ID")
            last_id = payload["lastId"]
        values = self.list_entities(collection, kind=kind, status=status, profile=profile)
        if last_id is not None:
            identifier_field = self.collection_contract[collection]
            values = [item for item in values if str(item[identifier_field]) > last_id]
        items = values[:limit]
        next_cursor = None
        if len(values) > limit and items:
            identifier_field = self.collection_contract[collection]
            next_cursor = _encode_query_cursor({
                "schema": "fdir/query-cursor",
                "queryContractVersion": self.manifest["querySurface"]["contractVersion"],
                "indexDigest": digest,
                "collection": collection,
                "kind": kind,
                "status": status,
                "profileId": profile_id,
                "lastId": str(items[-1][identifier_field]),
            })
        return {
            "queryContractVersion": self.manifest["querySurface"]["contractVersion"],
            "profileId": profile_id,
            "indexDigest": digest,
            "collection": collection,
            "items": items,
            "nextCursor": next_cursor,
        }

    def get_entity(self, collection: str, identifier: str) -> dict[str, Any]:
        self._check_collection(collection)
        self._assert_live()
        try:
            row = self._connection.execute(
                "SELECT payload_json FROM entities WHERE collection = ? AND entity_id = ?",
                (collection, identifier),
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"entity query failed: {exc}") from exc
        if row is None:
            raise IndependentIndexError(f"unknown entity: {collection}/{identifier}")
        return json.loads(row[0])

    def get_field(self, collection: str, identifier: str, pointer: str) -> Any:
        self._check_collection(collection)
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise IndependentIndexError("field pointer must be empty or start with '/'")
        self._assert_live()
        try:
            extension_type = None
            extension_namespace = None
            extension_version = None
            if collection != DOCUMENT_COLLECTION:
                entity_row = self._connection.execute(
                    "SELECT payload_json FROM entities WHERE collection = ? AND entity_id = ?",
                    (collection, identifier),
                ).fetchone()
                if entity_row is None:
                    raise IndependentIndexError(f"unknown entity: {collection}/{identifier}")
                if collection == "extensions":
                    payload = json.loads(entity_row[0], object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
                    extension_type = payload.get("type") if isinstance(payload, dict) and isinstance(payload.get("type"), str) else None
                    extension_namespace = payload.get("namespace") if isinstance(payload, dict) and isinstance(payload.get("namespace"), str) else None
                    extension_version = payload.get("schemaVersion") if isinstance(payload, dict) and isinstance(payload.get("schemaVersion"), str) else None
            self._check_field(collection, pointer, extension_type, extension_namespace, extension_version)
            row = self._connection.execute(
                """
                SELECT value_json FROM fields
                WHERE collection = ? AND entity_id = ? AND pointer = ?
                """,
                (collection, identifier, pointer),
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"field query failed: {exc}") from exc
        if row is None:
            raise IndexFieldNotFound(f"missing field: {collection}/{identifier}{pointer}")
        return json.loads(row[0])

    def find_field_equals(
        self,
        pointer: str,
        value: Any,
        *,
        collection: str | None = None,
        namespace: str | None = None,
        extension_type: str | None = None,
        schema_version: str | None = None,
    ) -> list[dict[str, str]]:
        """Return entities whose stored field equals a typed JSON value."""
        return [
            {"collection": row["collection"], "id": row["id"]}
            for row in self.query_fields(
                pointer,
                value,
                operator="eq",
                collection=collection,
                namespace=namespace,
                extension_type=extension_type,
                schema_version=schema_version,
            )
        ]

    def query_fields(
        self,
        pointer: str,
        value: Any = _MISSING,
        *,
        operator: str = "eq",
        collection: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        profile: str | None = None,
        namespace: str | None = None,
        extension_type: str | None = None,
        schema_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a typed field query over the persistent field rows."""

        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise IndependentIndexError("query field pointer must start with '/'")
        if collection is not None:
            self._check_collection(collection)
            matching_registry = _matching_query_fields(
                self.field_registry,
                collection,
                pointer,
                extension_type if collection == "extensions" else None,
                namespace if collection == "extensions" else None,
                schema_version if collection == "extensions" else None,
            )
            if not matching_registry:
                raise IndependentIndexError(f"field pointer is not registered: {collection}{pointer}")
            if not any(operator in field.get("filterOperators", []) for field in matching_registry):
                raise IndependentIndexError(f"operator {operator} is not registered for {collection}{pointer}")
        else:
            matching_registry = [
                field
                for owner_collection in self.field_registry
                for field in _matching_query_fields(self.field_registry, owner_collection, pointer)
            ]
            if not matching_registry:
                raise IndependentIndexError(f"field pointer is not registered: {pointer}")
            if not any(operator in field.get("filterOperators", []) for field in matching_registry):
                raise IndependentIndexError(f"operator {operator} is not registered for {pointer}")
        if operator not in {"eq", "neq", "lt", "lte", "gt", "gte", "prefix", "contains", "exists", "is-null", "is-missing"}:
            raise IndependentIndexError(f"unknown field operator: {operator}")
        if operator not in {"exists", "is-null", "is-missing"} and value is _MISSING:
            raise IndependentIndexError(f"operator {operator} requires a value")
        if profile is not None:
            profiles = set(self.manifest.get("capabilityProfileIds", []))
            if profile not in profiles:
                raise IndependentIndexError(f"unknown capability profile: {profile}")
            if profile not in self.manifest.get("applicableCapabilityProfileIds", []):
                return []
        response_metadata = {
            "queryContractVersion": self.manifest["querySurface"]["contractVersion"],
            "profileId": profile or (self.manifest.get("applicableCapabilityProfileIds") or [None])[0],
        }
        self._assert_live()
        clauses: list[str] = []
        parameters: list[Any] = []
        if collection is not None:
            clauses.append("f.collection = ?")
            parameters.append(collection)
        if "*" not in pointer:
            clauses.append("f.pointer = ?")
            parameters.append(pointer)
        sql = (
            "SELECT f.collection, f.entity_id, f.pointer, f.value_json, "
            "f.value_type, e.entity_kind, e.entity_status, e.payload_json "
            "FROM fields f LEFT JOIN entities e "
            "ON e.collection = f.collection AND e.entity_id = f.entity_id"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY f.collection, f.entity_id, f.pointer"
        )
        try:
            rows = self._connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"typed field query failed: {exc}") from exc
        results: list[dict[str, Any]] = []
        for row in rows:
            row_collection, identifier, actual_pointer = row[0], row[1], row[2]
            if "*" in pointer and not _pointer_matches(pointer, actual_pointer):
                continue
            if kind is not None and row[5] != kind:
                continue
            if status is not None and row[6] != status:
                continue
            if row_collection == "extensions":
                try:
                    payload = json.loads(row[7], object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    raise IndependentIndexError("stored extension entity JSON cannot be decoded") from exc
                row_extension_type = payload.get("type") if isinstance(payload, dict) and isinstance(payload.get("type"), str) else None
                row_namespace = payload.get("namespace") if isinstance(payload, dict) and isinstance(payload.get("namespace"), str) else None
                row_schema_version = payload.get("schemaVersion") if isinstance(payload, dict) and isinstance(payload.get("schemaVersion"), str) else None
                if namespace is not None and row_namespace != namespace:
                    continue
                if extension_type is not None and row_extension_type != extension_type:
                    continue
                if schema_version is not None and row_schema_version != schema_version:
                    continue
                if _registered_query_field(
                    self.field_registry,
                    row_collection,
                    actual_pointer,
                    row_extension_type,
                    row_namespace,
                    row_schema_version,
                ) is None:
                    continue
            try:
                actual = json.loads(row[3], object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise IndependentIndexError("stored field JSON cannot be decoded") from exc
            if operator == "exists":
                matches = True
            elif operator == "is-null":
                matches = actual is None
            elif operator == "is-missing":
                matches = False
            else:
                matches = _typed_compare(actual, value, operator)
            if matches:
                results.append({
                    **response_metadata,
                    "collection": row_collection,
                    "id": identifier,
                    "pointer": actual_pointer,
                    "presence": "null" if actual is None else "value",
                    "value": actual,
                    "status": row[6],
                })

        if operator == "is-missing" and "*" not in pointer:
            try:
                candidate_sql = "SELECT collection, entity_id, entity_kind, entity_status FROM entities"
                candidate_params: list[Any] = []
                if collection is not None and collection != DOCUMENT_COLLECTION:
                    candidate_sql += " WHERE collection = ?"
                    candidate_params.append(collection)
                candidate_sql += " ORDER BY collection, entity_id"
                candidates = self._connection.execute(candidate_sql, candidate_params).fetchall()
                if collection == DOCUMENT_COLLECTION:
                    candidates = [(DOCUMENT_COLLECTION, self.manifest["source"]["documentId"], None, None)]
            except sqlite3.Error as exc:
                raise IndependentIndexError(f"missing-field candidate query failed: {exc}") from exc
            present = {(row[0], row[1]) for row in rows}
            for candidate in candidates:
                if (candidate[0], candidate[1]) in present:
                    continue
                if kind is not None and candidate[2] != kind:
                    continue
                if status is not None and candidate[3] != status:
                    continue
                results.append({
                    **response_metadata,
                    "collection": candidate[0], "id": candidate[1], "pointer": pointer,
                    "presence": "missing", "value": None, "status": candidate[3],
                })
        results.sort(key=lambda item: (item["collection"], item["id"], item["pointer"]))
        return results

    def find_references(
        self,
        target_id: str | None = None,
        *,
        source_id: str | None = None,
        source_collection: str | None = None,
        pointer: str | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_live()
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (("target_id", target_id), ("source_id", source_id), ("source_collection", source_collection), ("source_pointer", pointer)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        try:
            rows = self._connection.execute(
                "SELECT source_collection, source_id, source_pointer, target_id, target_collection, reference_kind, ordinal FROM reverse_references"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY target_collection, target_id, source_collection, source_id, source_pointer, reference_kind, COALESCE(ordinal, -1)",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"reference query failed: {exc}") from exc
        return [
            {
                "sourceCollection": row[0], "sourceId": row[1], "sourcePointer": row[2],
                "targetId": row[3], "targetCollection": row[4], "referenceKind": row[5], "ordinal": row[6],
            }
            for row in rows
        ]

    def reverse_references(self, target_id: str) -> list[dict[str, Any]]:
        return self.find_references(target_id)


def open_index(
    source_path: Path,
    index_path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    model_contract_path: Path = DEFAULT_MODEL_CONTRACT_PATH,
    reference_registry_path: Path = DEFAULT_REFERENCE_REGISTRY_PATH,
    extension_registry_path: Path = DEFAULT_EXTENSION_REGISTRY_PATH,
    query_contract_path: Path = DEFAULT_QUERY_CONTRACT_PATH,
    capability_profile_path: Path = DEFAULT_CAPABILITY_PROFILE_PATH,
) -> PersistentIndex:
    """Open only an index whose source, contracts, rows, and file are intact."""

    source_path = Path(source_path)
    index_path = Path(index_path)
    if not index_path.is_file():
        raise IndependentIndexError(f"index file does not exist: {index_path}")
    manifest = _read_manifest(index_path)
    if _file_sha256(index_path) != manifest["databaseSha256"]:
        raise IndependentIndexError("index database digest mismatch")
    context, collection_contract, contract_metadata, expected_records = _build_context(
        source_path,
        schema_path=Path(schema_path),
        model_contract_path=Path(model_contract_path),
        reference_registry_path=Path(reference_registry_path),
        extension_registry_path=Path(extension_registry_path),
        query_contract_path=Path(query_contract_path),
        capability_profile_path=Path(capability_profile_path),
    )
    if manifest["bindings"] != contract_metadata["bindings"]:
        raise IndependentIndexError("index contract digest is stale or wrong")
    try:
        connection = sqlite3.connect(str(index_path))
        connection.execute("PRAGMA query_only = ON")
        _verify_sqlite_shape(connection)
        metadata = _read_metadata(connection)
        core = {key: manifest[key] for key in MANIFEST_CORE_KEYS}
        if metadata["manifestCoreJson"] != _canonical_json(core):
            raise IndependentIndexError("SQLite manifest core does not match sidecar manifest")
        if metadata["manifestCoreSha256"] != _sha256_json(core):
            raise IndependentIndexError("SQLite manifest core checksum mismatch")
        if metadata["collectionContract"] != _canonical_json(collection_contract):
            raise IndependentIndexError("SQLite collection contract is stale or corrupt")
        if metadata["recordsDigest"] != core["integrity"]["recordsDigest"]:
            raise IndependentIndexError("SQLite records digest metadata mismatch")
        actual_records = _records_from_sqlite(connection)
        _validate_records_self_integrity(actual_records)
        if _records_digest(actual_records) != core["integrity"]["recordsDigest"]:
            raise IndependentIndexError("SQLite row integrity digest mismatch")
        actual_counts = {
            "entities": len(actual_records["entities"]),
            "fields": len(actual_records["fields"]),
            "documentFields": sum(1 for row in actual_records["fields"] if row["collection"] == DOCUMENT_COLLECTION),
            "entityFields": sum(1 for row in actual_records["fields"] if row["collection"] != DOCUMENT_COLLECTION),
            "references": len(actual_records["references"]),
            "collections": {
                collection: sum(
                    1 for row in actual_records["entities"] if row["collection"] == collection
                )
                for collection in sorted(collection_contract)
            },
        }
        if actual_counts != core["counts"]:
            raise IndependentIndexError("SQLite row counts mismatch manifest")
        if _records_digest(expected_records) != core["integrity"]["recordsDigest"]:
            raise IndependentIndexError("index content digest does not match current IR")
        _check_manifest_against_source(
            manifest, context, expected_records, collection_contract
        )
        return PersistentIndex(connection, index_path, manifest, collection_contract, context["fieldRegistry"])
    except Exception:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        raise


def _path_argument(value: str) -> Path:
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    build = sub.add_parser("build")
    build.add_argument("source", type=_path_argument)
    build.add_argument("index", type=_path_argument)
    verify = sub.add_parser("verify")
    verify.add_argument("source", type=_path_argument)
    verify.add_argument("index", type=_path_argument)
    field = sub.add_parser("get-field")
    field.add_argument("source", type=_path_argument)
    field.add_argument("index", type=_path_argument)
    field.add_argument("collection")
    field.add_argument("identifier")
    field.add_argument("pointer")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "build":
            result = build_index(args.source, args.index)
        elif args.operation == "verify":
            with open_index(args.source, args.index) as index:
                result = {
                    "status": "passed",
                    "schema": index.manifest["schema"],
                    "indexVersion": index.manifest["indexVersion"],
                    "counts": index.manifest["counts"],
                    "databaseSha256": index.manifest["databaseSha256"],
                }
        else:
            with open_index(args.source, args.index) as index:
                result = index.get_field(args.collection, args.identifier, args.pointer)
        sys.stdout.write(_canonical_json(result) + "\n")
        return 0
    except (IndependentIndexError, OSError, sqlite3.Error, ValueError) as exc:
        sys.stderr.write(f"INDEPENDENT INDEX ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
