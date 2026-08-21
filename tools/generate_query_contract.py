"""Generate the typed query contract from the normative model sources.

The query contract is a derived artifact.  It must not become a second,
hand-maintained list of fields: every collection field is expanded from the
IR schema, and every registered extension payload is expanded from the
extension schema named by the extension registry.  The generated templates
use ``*`` for one list/map segment; callers resolve those templates against
concrete RFC 6901 JSON pointers at query time.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "machine" / "query-contract.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
MODEL_PATH = ROOT / "machine" / "model-contract.json"
REFERENCE_PATH = ROOT / "machine" / "reference-registry.json"
EXTENSION_REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
EXTENSION_SCHEMA_PATH = ROOT / "schemas" / "extensions" / "format-extensions.schema.json"
GENERATOR_VERSION = "1.1.0"
CONTRACT_VERSION = "1.3.0"
SCHEMA_NAME = "fdir/document-form-query-contract"
DOCUMENT_COLLECTION = "__document__"


class QueryContractGenerationError(ValueError):
    """Raised when an authoritative query surface cannot be generated."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise QueryContractGenerationError(f"cannot read {path}: {exc}") from exc


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueryContractGenerationError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QueryContractGenerationError(f"{label} must be an object: {path}")
    return value


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _ref_name(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return ref[len("#/$defs/") :]
    return None


def _resolve(node: Any, definitions: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    current = node
    seen: set[str] = set()
    first_ref: str | None = None
    while True:
        name = _ref_name(current)
        if name is None:
            if not isinstance(current, dict):
                raise QueryContractGenerationError("schema node resolved to a non-object")
            return current, first_ref
        if name in seen:
            raise QueryContractGenerationError(f"cyclic schema reference at #/$defs/{name}")
        seen.add(name)
        if first_ref is None:
            first_ref = name
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            raise QueryContractGenerationError(f"unresolved schema reference #/$defs/{name}")
        current = definition


def _merge_schema_nodes(node: dict[str, Any]) -> dict[str, Any]:
    """Merge only structural schema keys used for field expansion."""

    merged: dict[str, Any] = {}
    branches: list[dict[str, Any]] = []
    for keyword in ("allOf", "oneOf", "anyOf"):
        values = node.get(keyword)
        if isinstance(values, list):
            branches.extend(value for value in values if isinstance(value, dict))
    if branches:
        for branch in branches:
            branch_merged = _merge_schema_nodes(branch)
            for key, value in branch_merged.items():
                if key == "properties":
                    merged.setdefault("properties", {}).update(value)
                elif key == "required":
                    merged.setdefault("required", [])
                    for name in value:
                        if name not in merged["required"]:
                            merged["required"].append(name)
                elif key not in merged:
                    merged[key] = copy.deepcopy(value)
    for key, value in node.items():
        if key in {"allOf", "oneOf", "anyOf"}:
            continue
        if key == "properties" and isinstance(value, dict):
            merged.setdefault("properties", {}).update(copy.deepcopy(value))
        elif key == "required" and isinstance(value, list):
            merged.setdefault("required", [])
            for name in value:
                if name not in merged["required"]:
                    merged["required"].append(name)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _schema_type(node: dict[str, Any], definitions: dict[str, Any]) -> tuple[str, str | None]:
    resolved, ref_name = _resolve(node, definitions)
    merged = _merge_schema_nodes(resolved)
    if merged.get("type") == "array":
        return "list", ref_name
    if merged.get("type") == "object" or "properties" in merged or "additionalProperties" in merged:
        if isinstance(merged.get("additionalProperties"), dict) and not merged.get("properties"):
            return "map", ref_name
        return "object", ref_name
    type_value = merged.get("type")
    if isinstance(type_value, list):
        if "number" in type_value or "integer" in type_value:
            return "number", ref_name
        if "string" in type_value:
            return "string", ref_name
        if "boolean" in type_value:
            return "boolean", ref_name
        if "null" in type_value:
            return "null", ref_name
    if isinstance(type_value, str):
        return type_value, ref_name
    if "enum" in merged or "const" in merged:
        return "scalar", ref_name
    return "unknown", ref_name


def _query_type(node: dict[str, Any], definitions: dict[str, Any], path: str) -> str:
    resolved, ref_name = _resolve(node, definitions)
    if ref_name == "decimal":
        return "decimal-string"
    if ref_name == "id":
        return "id-string"
    if ref_name == "status":
        return "status"
    if ref_name == "typedValue":
        return "typed-value"
    value_type, _ = _schema_type(resolved, definitions)
    if value_type == "number" and path.lower().endswith(("/x", "/y", "/width", "/height", "/rotation", "/score")):
        return "number"
    return value_type


def _walk_schema(
    node: dict[str, Any],
    *,
    path: str,
    definitions: dict[str, Any],
    owner: dict[str, Any],
    add: Any,
    active_refs: tuple[str, ...] = (),
) -> None:
    resolved, ref_name = _resolve(node, definitions)
    if ref_name is not None and ref_name in active_refs:
        return
    refs = active_refs + ((ref_name,) if ref_name is not None else ())
    merged = _merge_schema_nodes(resolved)
    branches: list[dict[str, Any]] = []
    for keyword in ("oneOf", "anyOf"):
        values = resolved.get(keyword)
        if isinstance(values, list):
            branches.extend(value for value in values if isinstance(value, dict))
    if branches:
        for branch in branches:
            _walk_schema(
                branch,
                path=path,
                definitions=definitions,
                owner=owner,
                add=add,
                active_refs=refs,
            )
    # A union made only of references has no fields of its own.  Its branches
    # above are the authoritative expansion (for example cellRange versus a
    # DOCX merged-range extension), so do not add an ``unknown`` placeholder.
    if path and (not branches or merged.get("type") or merged.get("properties") or merged.get("additionalProperties")):
        add(path, resolved, owner, definitions)
    properties = merged.get("properties")
    if isinstance(properties, dict):
        required = set(item for item in merged.get("required", []) if isinstance(item, str))
        for name in sorted(properties):
            child = properties[name]
            child_path = f"{path}/{_escape(name)}"
            _walk_schema(
                child,
                path=child_path,
                definitions=definitions,
                owner={**owner, "requiredParent": name in required},
                add=add,
                active_refs=refs,
            )
    if merged.get("type") == "array" and isinstance(merged.get("items"), dict):
        _walk_schema(
            merged["items"],
            path=f"{path}/*",
            definitions=definitions,
            owner={**owner, "listItem": True},
            add=add,
            active_refs=refs,
        )
    additional = merged.get("additionalProperties")
    if isinstance(additional, dict):
        _walk_schema(
            additional,
            path=f"{path}/*",
            definitions=definitions,
            owner={**owner, "mapValue": True},
            add=add,
            active_refs=refs,
        )
    elif (
        (merged.get("type") == "object" or "properties" in merged)
        and ("additionalProperties" not in merged or additional is True)
    ):
        # JSON Schema leaves additionalProperties open by default.  Such an
        # object is authoritative even when the current input happens to use
        # only one key; registering only the named properties would turn a
        # valid future/input key into a false unqueryable fact.  ``**`` is an
        # opaque, arbitrary-depth template and is still resolved against
        # concrete observed pointers by the direct and persistent indexes.
        add(f"{path}/**", resolved, owner, definitions)


def _pointer_segments(path: str) -> list[str]:
    return path.lstrip("/").split("/") if path else []


def _owner_field(owner_definition: str, path: str) -> str:
    segments = [segment for segment in _pointer_segments(path) if segment != "*"]
    return f"{owner_definition}.{'/'.join(segments)}"


def _reference_entry_for(
    owner_definition: str,
    path: str,
    references: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidate = _owner_field(owner_definition, path).replace("/", ".")
    # The registry uses ``order.items.id`` while the concrete path contains
    # ``/items/*/id``.  Removing list markers makes both vocabularies compare
    # mechanically without assuming a collection-specific field name.
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("owner"), str):
            continue
        if reference["owner"] == candidate:
            return reference
    return None


def _classification(path: str, query_type: str, reference: dict[str, Any] | None, extension: bool) -> str:
    if extension:
        return "extension-payload"
    if reference is not None or path.rsplit("/", 1)[-1].endswith(("Id", "Ids")):
        return "reference" if query_type not in {"list", "object", "map"} else "reference-container"
    leaf = path.rsplit("/", 1)[-1]
    if leaf in {"status", "provenance", "laneProvenance", "propertyProvenance", "sourceMapId", "sourceRange", "locator"}:
        return "provenance-status"
    if query_type in {"list", "object", "map"}:
        return query_type
    return "scalar"


def _operator_set(query_type: str, classification: str) -> list[str]:
    operators = ["eq", "neq", "exists", "is-null", "is-missing"]
    if query_type in {"integer", "number", "decimal-string"}:
        operators.extend(["lt", "lte", "gt", "gte"])
    if query_type in {"string", "id-string", "status"}:
        operators.extend(["prefix", "contains"])
    if classification.startswith("reference"):
        operators.extend(["incoming", "outgoing"])
    return operators


def _field_id(prefix: str, collection: str, path: str) -> str:
    raw = f"{prefix}:{collection}:{path}"
    safe = re.sub(r"[^A-Za-z0-9_.:/*-]", "_", raw)
    return safe


def _add_field(
    fields: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    *,
    collection: str,
    definition: str,
    path: str,
    node: dict[str, Any],
    definitions: dict[str, Any],
    references: list[dict[str, Any]],
    extension: dict[str, Any] | None = None,
    required: bool = False,
) -> None:
    key = (collection, path, extension.get("type", "") if extension else "")
    if key in seen:
        return
    seen.add(key)
    query_type = _query_type(node, definitions, path)
    reference = _reference_entry_for(definition, path, references)
    is_extension = extension is not None
    classification = _classification(path, query_type, reference, is_extension)
    entry: dict[str, Any] = {
        "fieldId": _field_id(
            f"extension:{extension['type']}" if is_extension and extension is not None else "model",
            collection,
            path,
        ),
        "ownerCollection": collection,
        "ownerDefinition": definition,
        "path": path,
        "queryType": query_type,
        "classification": classification,
        "cardinality": "1" if required else "0..1",
        "filterOperators": _operator_set(query_type, classification),
        "ordering": "canonical-json" if query_type in {"object", "list", "map"} else "typed",
        "projection": "json-pointer-value",
        "indexStrategy": "field-value-row",
        "nullPolicy": "missing-distinct-from-null",
        "applicability": {"schemaVersion": "1.0.0", "profileIds": "*"},
        "requiredPositiveCaseIds": [f"query-positive:{_field_id('case', collection, path)}"],
        "requiredNegativeCaseIds": [f"query-negative:{_field_id('case', collection, path)}:missing"],
    }
    if reference is not None:
        entry["reference"] = {
            "targetCollections": copy.deepcopy(reference.get("targetCollections", [])),
            "cardinality": reference.get("cardinality"),
            "owner": reference.get("owner"),
        }
    if extension is not None:
        entry["extension"] = {
            "namespace": extension["namespace"],
            "type": extension["type"],
            "schemaVersion": extension["schemaVersion"],
            "schemaId": extension["schemaId"],
            "registrySchemaPath": extension["schemaPath"],
        }
    fields.append(entry)


def _extension_schema(schema: dict[str, Any], schema_path: str) -> dict[str, Any]:
    marker = "#"
    if marker not in schema_path:
        raise QueryContractGenerationError(f"extension schemaPath has no fragment: {schema_path}")
    file_name, fragment = schema_path.split(marker, 1)
    expected = Path(file_name).name
    if expected != EXTENSION_SCHEMA_PATH.name:
        raise QueryContractGenerationError(f"extension schemaPath is outside the registered schema: {schema_path}")
    if not fragment.startswith("/$defs/"):
        raise QueryContractGenerationError(f"unsupported extension schema fragment: {schema_path}")
    name = fragment[len("/$defs/") :]
    value = schema.get("$defs", {}).get(name)
    if not isinstance(value, dict):
        raise QueryContractGenerationError(f"extension schema definition is missing: {schema_path}")
    return value


def _top_level_fields(
    schema: dict[str, Any],
    definitions: dict[str, Any],
    collections: set[str],
) -> list[dict[str, Any]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise QueryContractGenerationError("IR schema has no top-level properties")
    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for name in sorted(properties):
        if name in collections:
            continue
        node = properties[name]
        owner = {
            "ownerCollection": DOCUMENT_COLLECTION,
            "ownerDefinition": "document",
            "path": f"/{_escape(name)}",
        }
        _walk_schema(
            node,
            path=f"/{_escape(name)}",
            definitions=definitions,
            owner=owner,
            add=lambda path, value, _owner, defs: _add_field(
                fields,
                seen,
                collection=DOCUMENT_COLLECTION,
                definition="document",
                path=path,
                node=value,
                definitions=defs,
                references=[],
                required=name in set(schema.get("required", [])),
            ),
        )
    return fields


def generate_contract() -> dict[str, Any]:
    schema = _load_json(SCHEMA_PATH, "IR schema")
    model = _load_json(MODEL_PATH, "model contract")
    references_doc = _load_json(REFERENCE_PATH, "reference registry")
    extension_registry = _load_json(EXTENSION_REGISTRY_PATH, "extension registry")
    extension_schema = _load_json(EXTENSION_SCHEMA_PATH, "extension schema")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise QueryContractGenerationError("IR schema.$defs is missing")
    references = references_doc.get("references")
    if not isinstance(references, list):
        raise QueryContractGenerationError("reference registry has no references list")
    model_collections = model.get("collections")
    if not isinstance(model_collections, list) or not model_collections:
        raise QueryContractGenerationError("model contract has no collections")
    ext_entries = extension_registry.get("entries")
    if not isinstance(ext_entries, list):
        raise QueryContractGenerationError("extension registry has no entries")

    collections = []
    collection_names: set[str] = set()
    field_paths: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    schema_properties = schema.get("properties", {})
    for model_collection in sorted(model_collections, key=lambda value: str(value.get("name"))):
        if not isinstance(model_collection, dict):
            raise QueryContractGenerationError("model collection is not an object")
        collection = model_collection.get("name")
        definition_name = model_collection.get("definition")
        id_field = model_collection.get("idField")
        if not all(isinstance(value, str) for value in (collection, definition_name, id_field)):
            raise QueryContractGenerationError("model collection has incomplete identity")
        if collection in collection_names:
            raise QueryContractGenerationError(f"duplicate model collection: {collection}")
        collection_names.add(collection)
        property_schema = schema_properties.get(collection)
        if not isinstance(property_schema, dict) or _ref_name(property_schema.get("items")) != definition_name:
            raise QueryContractGenerationError(f"schema/model collection drift: {collection}")
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            raise QueryContractGenerationError(f"missing model definition: {definition_name}")
        collections.append({
            "name": collection,
            "definition": definition_name,
            "idField": id_field,
            "statusField": model_collection.get("statusField"),
            "kind": copy.deepcopy(model_collection.get("kind")),
        })

        def add(path: str, node: dict[str, Any], _owner: dict[str, Any], defs: dict[str, Any]) -> None:
            required = path.split("/")[1] in set(definition.get("required", [])) if path.count("/") == 1 else False
            _add_field(
                field_paths,
                seen,
                collection=collection,
                definition=definition_name,
                path=path,
                node=node,
                definitions=defs,
                references=[item for item in references if isinstance(item, dict)],
                required=required,
            )

        _walk_schema(
            definition,
            path="",
            definitions=definitions,
            owner={"ownerCollection": collection, "ownerDefinition": definition_name},
            add=add,
        )

    document_fields = _top_level_fields(schema, definitions, collection_names)
    # Core extension metadata is already part of the model definition.  The
    # payload is intentionally expanded independently from the core schema,
    # because the core payload is an opaque object and the registry is its
    # normative vocabulary.
    extension_fields: list[dict[str, Any]] = []
    extension_seen: set[tuple[str, str, str]] = set()
    for entry in sorted(ext_entries, key=lambda value: (str(value.get("namespace")), str(value.get("type")))):
        if not isinstance(entry, dict):
            raise QueryContractGenerationError("extension registry entry is not an object")
        required_keys = ("namespace", "type", "schemaVersion", "schemaId", "schemaPath")
        if not all(isinstance(entry.get(key), str) for key in required_keys):
            raise QueryContractGenerationError("extension registry entry has incomplete schema identity")
        payload_schema = _extension_schema(extension_schema, entry["schemaPath"])

        def add_extension(path: str, node: dict[str, Any], _owner: dict[str, Any], defs: dict[str, Any]) -> None:
            _add_field(
                extension_fields,
                extension_seen,
                collection="extensions",
                definition="extension",
                path=path,
                node=node,
                definitions=defs,
                references=[item for item in references if isinstance(item, dict)],
                extension=entry,
                required=path == "/payload" or (path.count("/") == 2 and path.split("/")[-1] in set(payload_schema.get("required", []))),
            )

        _walk_schema(
            payload_schema,
            path="/payload",
            definitions=extension_schema.get("$defs", {}),
            owner={"ownerCollection": "extensions", "ownerDefinition": "extension", "extension": entry},
            add=add_extension,
        )
        # An extension payload is also allowed to be opaque by the IR schema.
        # Keep a wildcard registration so an opaque value remains queryable;
        # the concrete schema fields above still carry typed metadata.
        if not any(item["path"] == "/payload/**" and item.get("extension", {}).get("type") == entry["type"] for item in extension_fields):
            _add_field(
                extension_fields,
                extension_seen,
                collection="extensions",
                definition="extension",
                path="/payload/**",
                node={"type": ["string", "number", "integer", "boolean", "null", "object", "array"]},
                definitions=extension_schema.get("$defs", {}),
                references=[],
                extension=entry,
                required=False,
            )

    field_paths.sort(key=lambda item: (item["ownerCollection"], item["path"], item["fieldId"]))
    extension_fields.sort(key=lambda item: (item["extension"]["namespace"], item["extension"]["type"], item["path"]))
    all_fields = document_fields + field_paths + extension_fields
    profile_ids = sorted(
        item["id"]
        for item in _load_json(ROOT / "machine" / "capability-profile.json", "capability profile").get("profiles", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    sources = [
        {"role": "ir-schema", "path": SCHEMA_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(SCHEMA_PATH)},
        {"role": "model-contract", "path": MODEL_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(MODEL_PATH)},
        {"role": "reference-registry", "path": REFERENCE_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(REFERENCE_PATH)},
        {"role": "extension-registry", "path": EXTENSION_REGISTRY_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(EXTENSION_REGISTRY_PATH)},
        {"role": "extension-schema", "path": EXTENSION_SCHEMA_PATH.relative_to(ROOT).as_posix(), "sha256": _sha256_file(EXTENSION_SCHEMA_PATH)},
        {"role": "generator", "path": Path(__file__).relative_to(ROOT).as_posix(), "sha256": _sha256_file(Path(__file__))},
    ]
    contract = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": SCHEMA_NAME,
        "version": CONTRACT_VERSION,
        "generated": {
            "generator": {"path": Path(__file__).relative_to(ROOT).as_posix(), "version": GENERATOR_VERSION},
            "sourceDigestAlgorithm": "sha256(canonical-json(sources))",
            "sourceDigest": _sha256_bytes(_canonical(sources)),
            "sources": sources,
        },
        "authority": {
            "kind": "derived-query-contract",
            "normativeInputs": [
                "schemas/document-form-ir.schema.json",
                "machine/model-contract.json",
                "machine/reference-registry.json",
                "machine/extension-registry.json",
                "schemas/extensions/format-extensions.schema.json",
            ],
            "generatedArtifact": "machine/query-contract.json",
            "unregisteredFieldPolicy": "fail-closed",
        },
        "acceptedSchema": {"name": "fdir/document-form", "version": "1.0.0"},
        "collections": collections,
        "documentCollection": {"name": DOCUMENT_COLLECTION, "idField": "documentId"},
        "profiles": profile_ids,
        "fieldPaths": field_paths,
        "documentFieldPaths": document_fields,
        "extensionFieldPaths": extension_fields,
        "referenceRegistry": {
            "path": REFERENCE_PATH.relative_to(ROOT).as_posix(),
            "version": references_doc.get("version"),
            "references": copy.deepcopy(references),
        },
        "policies": {
            "pointerSyntax": "RFC6901-concrete-or-template",
            "templateListSegment": "*",
            "nullMissing": "distinct",
            "defaultOrdering": "collection,id,pointer,ordinal",
            "typedComparison": "canonical-json-type-and-value",
            "paginationCursor": "index-digest-bound",
            "maxPageSize": 1000,
        },
        "operations": [
            "list-entities", "list-entities-page", "get-entity", "get-field", "get-document-field", "query-fields",
            "field-coverage", "list-nodes", "get-text", "ancestors", "descendants",
            "find-relations", "find-extensions", "find-observations", "find-references",
            "rebuild-index", "validate-index",
        ],
        "representations": ["source", "normalized", "stored", "computed", "displayed", "rendered", "observed"],
        "index": {
            "schema": "fdir/independent-sqlite-index",
            "version": "1.1.0",
            "authority": "non-authoritative deterministic projection",
            "requiredFields": [
                "schema", "indexVersion", "canonicalization", "source", "bindings",
                "contractVersions", "querySurface",
                "capabilityProfileIds", "applicableCapabilityProfileIds", "indexSchemaVersion",
                "builder", "counts", "integrity", "build", "databaseSha256", "integrityChecksum",
            ],
            "factParity": "every registered concrete entity/document field is represented by a typed field row",
            "validation": "stale, corrupt, incomplete, unsupported-newer, and extra index rows fail closed",
        },
        "fieldPathCount": len(all_fields),
    }
    return contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    value = generate_contract()
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.check:
        try:
            current = args.output.read_bytes()
        except OSError as exc:
            raise SystemExit(f"error: cannot read {args.output}: {exc}")
        if current != encoded:
            raise SystemExit(f"error: generated query contract drift detected: {args.output}; regenerate it")
        print(f"query contract valid: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    print(f"generated query contract: {args.output} ({len(value['fieldPaths']) + len(value['documentFieldPaths']) + len(value['extensionFieldPaths'])} field paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
