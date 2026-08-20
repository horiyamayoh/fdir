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
INDEX_VERSION = "1.0.0"
SQLITE_USER_VERSION = 1
CANONICALIZATION = "FDIR-IIDX-C14N-1"
BUILDER_NAME = "fdir.independent_index"
BUILDER_VERSION = "1.0.0"
MANIFEST_SUFFIX = ".manifest.json"

TABLES = {"metadata", "entities", "fields", "reverse_references"}
MANIFEST_CORE_KEYS = {
    "schema",
    "indexVersion",
    "canonicalization",
    "source",
    "bindings",
    "capabilityProfileIds",
    "applicableCapabilityProfileIds",
    "indexSchemaVersion",
    "builder",
    "counts",
    "integrity",
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
        "contractVersions": {
            "irSchema": schema.get("$id", schema.get("version")),
            "model": model_contract.get("version"),
            "referenceRegistry": reference_registry.get("version"),
            "extensionRegistry": extension_registry.get("version"),
            "queryContract": query_contract.get("version"),
            "capabilityProfile": capability_profile.get("version"),
        },
        "profileIds": profile_ids,
        "applicableProfileIds": [applicable],
        "bindings": bindings,
    }
    return collection_contract, contract_metadata, {"schema": schema, "model": model_contract}


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
) -> dict[str, list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    known_ids = _known_ids(document, collection_contract)

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
    _, contract_metadata, _ = _contract_context(
        document,
        schema_path=schema_path,
        model_contract_path=model_contract_path,
        reference_registry_path=reference_registry_path,
        extension_registry_path=extension_registry_path,
        query_contract_path=query_contract_path,
        capability_profile_path=capability_profile_path,
    )
    records = _extract_records(document, collection_contract)
    context = {
        "document": document,
        "sourceFileSha256": source_file_sha256,
        "sourceCanonicalDigest": source_canonical_digest,
        "collectionContract": collection_contract,
        "contractMetadata": contract_metadata,
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
        "capabilityProfileIds": contract_metadata["profileIds"],
        "applicableCapabilityProfileIds": contract_metadata["applicableProfileIds"],
        "indexSchemaVersion": SQLITE_USER_VERSION,
        "builder": {"name": BUILDER_NAME, "version": BUILDER_VERSION},
        "counts": _counts(records, collection_contract),
        "integrity": {"recordsDigest": context["recordsDigest"]},
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
    if core["capabilityProfileIds"] != context["contractMetadata"]["profileIds"]:
        raise IndependentIndexError("index capability profile registry binding is stale")
    if core["applicableCapabilityProfileIds"] != context["contractMetadata"]["applicableProfileIds"]:
        raise IndependentIndexError("index applicable capability profile binding is stale")
    if core["counts"] != expected_counts:
        raise IndependentIndexError("index row counts do not match current IR")
    if core["integrity"] != {"recordsDigest": context["recordsDigest"]}:
        raise IndependentIndexError("index records digest does not match current IR")
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
    ) -> None:
        self._connection = connection
        self.index_path = index_path
        self.manifest = manifest
        self.collection_contract = collection_contract

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
        if collection not in self.collection_contract:
            raise IndependentIndexError(f"unknown indexed collection: {collection}")

    def list_entities(
        self,
        collection: str | None = None,
        *,
        kind: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_live()
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
    ) -> list[dict[str, str]]:
        """Return entities whose stored field equals a typed JSON value."""

        if collection is not None:
            self._check_collection(collection)
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise IndependentIndexError("field pointer must be empty or start with '/'")
        value_json = _canonical_json(value)
        clauses = ["pointer = ?", "value_sha256 = ?", "value_type = ?"]
        parameters: list[Any] = [
            pointer,
            _sha256_bytes(value_json.encode("utf-8")),
            _value_type(value),
        ]
        if collection is not None:
            clauses.append("collection = ?")
            parameters.append(collection)
        self._assert_live()
        try:
            rows = self._connection.execute(
                """
                SELECT collection, entity_id FROM fields
                WHERE """ + " AND ".join(clauses) + """
                ORDER BY collection, entity_id
                """,
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"typed field query failed: {exc}") from exc
        return [{"collection": row[0], "id": row[1]} for row in rows]

    def reverse_references(self, target_id: str) -> list[dict[str, Any]]:
        self._assert_live()
        try:
            rows = self._connection.execute(
                """
                SELECT source_collection, source_id, source_pointer,
                       target_id, target_collection, reference_kind, ordinal
                FROM reverse_references WHERE target_id = ?
                ORDER BY source_collection, source_id, source_pointer,
                         reference_kind, COALESCE(ordinal, -1)
                """,
                (target_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndependentIndexError(f"reverse-reference query failed: {exc}") from exc
        return [
            {
                "sourceCollection": row[0], "sourceId": row[1], "sourcePointer": row[2],
                "targetId": row[3], "targetCollection": row[4],
                "referenceKind": row[5], "ordinal": row[6],
            }
            for row in rows
        ]


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
        return PersistentIndex(connection, index_path, manifest, collection_contract)
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
