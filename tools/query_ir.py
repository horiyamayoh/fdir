"""Typed, non-semantic queries over a Document Form IR JSON document."""

from __future__ import annotations

import argparse
import base64
from collections import deque
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    from ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from canonicalize_ir import canonical_digest, canonical_value_digest
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from tools.canonicalize_ir import canonical_digest, canonical_value_digest


class QueryError(ValueError):
    """Raised for an invalid query or malformed IR input."""


INDEX_SCHEMA = "fdir/document-form-index"
INDEX_VERSION = "1.3.0"
REPRESENTATIONS = {"source", "normalized", "stored", "computed", "displayed", "rendered", "observed"}
QUERY_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "machine" / "query-contract.json"
DOCUMENT_COLLECTION = "__document__"
_MISSING = object()
_QUERY_CONTRACT_CACHE: tuple[int, int, dict[str, Any]] | None = None
_QUERY_FIELD_INDEX_CACHE: tuple[int, int, _CompiledQueryFields] | None = None


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {token}")


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise QueryError(f"cannot read query contract source {path}: {exc}") from exc


def _load_query_contract() -> dict[str, Any]:
    """Load and verify the generated authoritative field registry."""

    global _QUERY_CONTRACT_CACHE
    try:
        stat = QUERY_CONTRACT_PATH.stat()
    except OSError as exc:
        raise QueryError(f"query contract is unavailable: {exc}") from exc
    cache_key = (stat.st_mtime_ns, stat.st_size)
    if _QUERY_CONTRACT_CACHE is not None and _QUERY_CONTRACT_CACHE[:2] == cache_key:
        return _QUERY_CONTRACT_CACHE[2]
    try:
        value = json.loads(
            QUERY_CONTRACT_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise QueryError(f"cannot load query contract: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "fdir/document-form-query-contract":
        raise QueryError("query contract has an invalid schema")
    generated = value.get("generated")
    sources = generated.get("sources") if isinstance(generated, dict) else None
    if not isinstance(sources, list) or not sources:
        raise QueryError("query contract has no generated source bindings")
    root = QUERY_CONTRACT_PATH.parents[1].resolve()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
            raise QueryError("query contract source binding is malformed")
        path = (root / source["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise QueryError("query contract source escapes repository root") from exc
        if _sha256_file(path) != source["sha256"]:
            raise QueryError(f"query contract is stale for source: {source['path']}")
    field_groups = (
        value.get("fieldPaths"),
        value.get("documentFieldPaths"),
        value.get("extensionFieldPaths"),
    )
    if not all(isinstance(group, list) for group in field_groups):
        raise QueryError("query contract field registry is incomplete")
    total = sum(len(group) for group in field_groups if isinstance(group, list))
    if value.get("fieldPathCount") != total:
        raise QueryError("query contract field path count is inconsistent")
    seen_ids: set[str] = set()
    for group in field_groups:
        assert isinstance(group, list)
        for field in group:
            if not isinstance(field, dict) or not isinstance(field.get("fieldId"), str) or not isinstance(field.get("path"), str):
                raise QueryError("query contract contains a malformed field entry")
            if field["fieldId"] in seen_ids:
                raise QueryError(f"query contract contains duplicate field id: {field['fieldId']}")
            seen_ids.add(field["fieldId"])
    collections = value.get("collections")
    if not isinstance(collections, list):
        raise QueryError("query contract has no collection registry")
    contract_mapping = {
        item.get("name"): item.get("idField")
        for item in collections
        if isinstance(item, dict)
    }
    if contract_mapping != COLLECTION_KEYS:
        raise QueryError("query contract collection mapping is stale or incomplete")
    _QUERY_CONTRACT_CACHE = (stat.st_mtime_ns, stat.st_size, value)
    return value


def query_contract() -> dict[str, Any]:
    """Return the verified generated query contract."""

    return _load_query_contract()


def _contract_fields(
    collection: str,
    *,
    document: bool = False,
    extension_type: str | None = None,
    extension_namespace: str | None = None,
    extension_version: str | None = None,
) -> list[dict[str, Any]]:
    return _compiled_query_fields().fields(
        collection,
        document=document,
        extension_type=extension_type,
        extension_namespace=extension_namespace,
        extension_version=extension_version,
    )


def _pointer_segments(pointer: str) -> list[str]:
    if pointer in {"", "/"}:
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QueryError("field pointer must be empty or start with '/'")
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


class _CompiledQueryFields:
    """Indexed view of the generated field registry.

    Coverage and index building visit one registry path for every observed
    JSON pointer.  Scanning the complete generated contract for each pointer
    made that operation O(observed-facts * registered-fields).  Exact paths
    use a dictionary and wildcard paths are bucketed by their first segment
    and, when possible, their length.  The original contract ordinal is kept
    so overlapping templates retain generated-registry precedence.
    """

    def __init__(self, contract: dict[str, Any]) -> None:
        self._fields: dict[tuple[bool, str], list[dict[str, Any]]] = {}
        self._all_candidates: dict[tuple[bool, str], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self._by_first: dict[tuple[bool, str, str], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self._exact: dict[tuple[bool, str, str], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        self._wildcards: dict[tuple[bool, str, int | None], list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        ordinal = 0
        groups = (
            (True, contract["documentFieldPaths"]),
            (False, contract["fieldPaths"]),
            (False, contract["extensionFieldPaths"]),
        )
        for document, group in groups:
            for field in group:
                collection = str(field["ownerCollection"])
                path = str(field["path"])
                segments = tuple(_pointer_segments(path))
                candidate = (ordinal, segments, field)
                self._fields.setdefault((document, collection), []).append(field)
                self._all_candidates.setdefault((document, collection), []).append(candidate)
                first = segments[0] if segments else ""
                self._by_first.setdefault((document, collection, first), []).append(candidate)
                if "*" not in segments and "**" not in segments:
                    self._exact.setdefault((document, collection, path), []).append(candidate)
                else:
                    length = None if "**" in segments else len(segments)
                    self._wildcards.setdefault((document, collection, first, length), []).append(candidate)
                ordinal += 1

    def fields(
        self,
        collection: str,
        *,
        document: bool,
        extension_type: str | None,
        extension_namespace: str | None,
        extension_version: str | None,
    ) -> list[dict[str, Any]]:
        return [
            field
            for field in self._fields.get((document, collection), [])
            if _extension_identity_matches(field, extension_type, extension_namespace, extension_version)
        ]

    def lookup(
        self,
        collection: str,
        pointer: str,
        *,
        document: bool,
        extension_type: str | None,
        extension_namespace: str | None,
        extension_version: str | None,
    ) -> dict[str, Any] | None:
        pointer_segments = tuple(_pointer_segments(pointer))
        first = pointer_segments[0] if pointer_segments else ""
        candidates = list(self._exact.get((document, collection, pointer), ()))
        for template_first in (first, "*", "**"):
            for length in (len(pointer_segments), None):
                candidates.extend(self._wildcards.get((document, collection, template_first, length), ()))
        for _, template_segments, field in sorted(candidates, key=lambda item: item[0]):
            if _extension_identity_matches(field, extension_type, extension_namespace, extension_version) and _pointer_segments_match(template_segments, pointer_segments):
                return field
        return None

    def matching(
        self,
        collection: str,
        pointer: str,
        *,
        document: bool,
        extension_type: str | None,
        extension_namespace: str | None,
        extension_version: str | None,
    ) -> list[dict[str, Any]]:
        """Return all registry entries that can match a query template.

        Concrete pointers use the same exact/first-segment buckets as
        :meth:`lookup`.  A caller-supplied wildcard in the first segment is
        inherently broad and falls back to the collection bucket; normal
        concrete field queries therefore never scan the full registry.
        """

        pointer_segments = tuple(_pointer_segments(pointer))
        first = pointer_segments[0] if pointer_segments else ""
        if "*" in pointer_segments or "**" in pointer_segments:
            if first in {"*", "**"}:
                candidates = list(self._all_candidates.get((document, collection), ()))
            else:
                candidates = []
                for candidate_first in (first, "*", "**"):
                    candidates.extend(self._by_first.get((document, collection, candidate_first), ()))
        else:
            candidates = list(self._exact.get((document, collection, pointer), ()))
            for candidate_first in (first, "*", "**"):
                for length in (len(pointer_segments), None):
                    candidates.extend(self._wildcards.get((document, collection, candidate_first, length), ()))
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


def _compiled_query_fields() -> _CompiledQueryFields:
    global _QUERY_FIELD_INDEX_CACHE
    try:
        stat = QUERY_CONTRACT_PATH.stat()
    except OSError as exc:
        raise QueryError(f"query contract is unavailable: {exc}") from exc
    cache_key = (stat.st_mtime_ns, stat.st_size)
    if _QUERY_FIELD_INDEX_CACHE is not None and _QUERY_FIELD_INDEX_CACHE[:2] == cache_key:
        return _QUERY_FIELD_INDEX_CACHE[2]
    compiled = _CompiledQueryFields(_load_query_contract())
    _QUERY_FIELD_INDEX_CACHE = (stat.st_mtime_ns, stat.st_size, compiled)
    return compiled


def _registered_field(
    collection: str,
    pointer: str,
    *,
    document: bool = False,
    extension_type: str | None = None,
    extension_namespace: str | None = None,
    extension_version: str | None = None,
) -> dict[str, Any] | None:
    if pointer in {"", "/"}:
        return None
    return _compiled_query_fields().lookup(
        collection,
        pointer,
        document=document,
        extension_type=extension_type,
        extension_namespace=extension_namespace,
        extension_version=extension_version,
    )


def _require_registered_field(
    collection: str,
    pointer: str,
    *,
    document: bool = False,
    extension_type: str | None = None,
    extension_namespace: str | None = None,
    extension_version: str | None = None,
) -> dict[str, Any]:
    if pointer in {"", "/"}:
        raise QueryError("the entity/document root is not a field path")
    field = _registered_field(
        collection,
        pointer,
        document=document,
        extension_type=extension_type,
        extension_namespace=extension_namespace,
        extension_version=extension_version,
    )
    if field is None:
        raise QueryError(f"field pointer is not registered: {collection}{pointer}")
    return field


def _ensure_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise QueryError("IR must be an object")
    try:
        validate_document(document)
    except IRValidationError as exc:
        raise QueryError(f"IR failed authority validation: {exc}") from exc
    return document


class _ValidatedDocument:
    """One authority validation bound to an immutable document snapshot.

    Public query functions continue to call :func:`_ensure_document` on every
    invocation.  Regression tests can use this private path when they perform
    a batch of read-only queries over one document.  The canonical
    digest is checked after the batch, so an in-flight mutation cannot turn a
    validated snapshot into an accepted stale result.
    """

    __slots__ = ("document", "_digest")

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = _ensure_document(document)
        self._digest = canonical_digest(self.document)

    def assert_current(self) -> None:
        try:
            current = canonical_digest(self.document)
        except Exception as exc:  # pragma: no cover - authority validation normally catches this first
            raise QueryError(f"validated IR became unreadable: {exc}") from exc
        if current != self._digest:
            raise QueryError("validated IR was mutated during the query batch")


def _validated_document(document: dict[str, Any]) -> _ValidatedDocument:
    return _ValidatedDocument(document)


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError, UnicodeError) as exc:
        raise QueryError(f"cannot load IR: {exc}") from exc
    if not isinstance(value, dict):
        raise QueryError("IR document must be an object")
    return _ensure_document(value)


def load_index(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError, UnicodeError) as exc:
        raise QueryError(f"cannot load index: {exc}") from exc
    if not isinstance(value, dict):
        raise QueryError("index must be an object")
    return value


def _items(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = document.get(key, [])
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise QueryError(f"IR field {key!r} must be an array of objects")
    return values


def _list_nodes_validated(document: dict[str, Any], kind: str | None = None, part_id: str | None = None,
                          status: str | None = None) -> list[dict[str, Any]]:
    result = _items(document, "nodes")
    return [
        node for node in result
        if (kind is None or node.get("kind") == kind)
        and (part_id is None or node.get("partId") == part_id)
        and (status is None or node.get("status") == status)
    ]


def list_nodes(document: dict[str, Any], kind: str | None = None, part_id: str | None = None,
               status: str | None = None) -> list[dict[str, Any]]:
    return _list_nodes_validated(_ensure_document(document), kind, part_id, status)


def _get_text_validated(document: dict[str, Any], node_id: str, representation: str = "source") -> list[dict[str, Any]]:
    if representation not in REPRESENTATIONS:
        raise QueryError(f"unknown text representation: {representation}")
    node = next((item for item in _items(document, "nodes") if item.get("nodeId") == node_id), None)
    if node is None:
        raise QueryError(f"unknown node: {node_id}")
    text_ids = list(node.get("textIds", []))
    for descendant in _descendants_validated(document, node_id):
        text_ids.extend(descendant.get("textIds", []))
    if not isinstance(text_ids, list):
        raise QueryError(f"node textIds is not an array: {node_id}")
    texts = _items(document, "texts")
    return [
        text for text in texts
        if text.get("textId") in text_ids and text.get("representation") == representation
    ]


def get_text(document: dict[str, Any], node_id: str, representation: str = "source") -> list[dict[str, Any]]:
    return _get_text_validated(_ensure_document(document), node_id, representation)


def _collection_name(collection: str) -> str:
    if collection not in COLLECTION_KEYS:
        raise QueryError(f"unknown entity collection: {collection}")
    return collection


def _list_entities_validated(document: dict[str, Any], collection: str, *, kind: str | None = None, status: str | None = None,
                             identifier: str | None = None, offset: int = 0, limit: int | None = None,
                             profile: str | None = None) -> list[dict[str, Any]]:
    collection = _collection_name(collection)
    contract = _load_query_contract()
    if profile is not None:
        if profile not in set(contract.get("profiles", [])):
            raise QueryError(f"unknown capability profile: {profile}")
        if document.get("conversion", {}).get("capabilityProfile") != profile:
            return []
    if offset < 0:
        raise QueryError("offset must be non-negative")
    if limit is not None and limit < 0:
        raise QueryError("limit must be non-negative")
    identifier_key = COLLECTION_KEYS[collection]
    items = _items(document, collection)
    result = [item for item in items if (identifier is None or item.get(identifier_key) == identifier) and (kind is None or item.get("kind") == kind) and (status is None or item.get("status") == status)]
    result.sort(key=lambda item: str(item.get(identifier_key, "")))
    result = result[max(0, offset):]
    return result if limit is None else result[:max(0, limit)]


def list_entities(document: dict[str, Any], collection: str, *, kind: str | None = None, status: str | None = None,
                  identifier: str | None = None, offset: int = 0, limit: int | None = None,
                  profile: str | None = None) -> list[dict[str, Any]]:
    return _list_entities_validated(
        _ensure_document(document),
        collection,
        kind=kind,
        status=status,
        identifier=identifier,
        offset=offset,
        limit=limit,
        profile=profile,
    )


def list_entities_page(
    document: dict[str, Any],
    collection: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic entity page bound to the current IR digest."""

    document = _ensure_document(document)
    collection = _collection_name(collection)
    contract = _load_query_contract()
    max_page_size = int(contract.get("policies", {}).get("maxPageSize", 1000))
    if limit < 1 or limit > max_page_size:
        raise QueryError(f"limit must be between 1 and {max_page_size}")
    profile_id = profile or document.get("conversion", {}).get("capabilityProfile")
    if profile_id not in set(contract.get("profiles", [])):
        raise QueryError(f"unknown capability profile: {profile_id}")
    digest = canonical_digest(document)
    last_id: str | None = None
    if cursor is not None:
        payload = _decode_query_cursor(cursor)
        expected = {
            "schema": "fdir/query-cursor",
            "queryContractVersion": contract["version"],
            "indexDigest": digest,
            "collection": collection,
            "kind": kind,
            "status": status,
            "profileId": profile_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise QueryError("query cursor is stale or belongs to another query/index")
        if not isinstance(payload.get("lastId"), str):
            raise QueryError("query cursor has no last entity ID")
        last_id = payload["lastId"]
    values = list_entities(document, collection, kind=kind, status=status, profile=profile)
    if last_id is not None:
        values = [item for item in values if str(item[COLLECTION_KEYS[collection]]) > last_id]
    items = values[:limit]
    next_cursor = None
    if len(values) > limit and items:
        next_cursor = _encode_query_cursor({
            "schema": "fdir/query-cursor",
            "queryContractVersion": contract["version"],
            "indexDigest": digest,
            "collection": collection,
            "kind": kind,
            "status": status,
            "profileId": profile_id,
            "lastId": str(items[-1][COLLECTION_KEYS[collection]]),
        })
    return {
        "queryContractVersion": contract["version"],
        "profileId": profile_id,
        "indexDigest": digest,
        "collection": collection,
        "items": items,
        "nextCursor": next_cursor,
    }


def _get_entity_validated(document: dict[str, Any], collection: str, identifier: str) -> dict[str, Any]:
    values = _list_entities_validated(document, collection, identifier=identifier)
    if not values:
        raise QueryError(f"unknown {collection} entity: {identifier}")
    return values[0]


def get_entity(document: dict[str, Any], collection: str, identifier: str) -> dict[str, Any]:
    return _get_entity_validated(_ensure_document(document), collection, identifier)


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_pointer_unescape(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _encode_query_cursor(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_query_cursor(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise QueryError("query cursor must be a non-empty string")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        result = json.loads(decoded.decode("utf-8"), object_pairs_hook=_unique_object_pairs, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueryError("query cursor is malformed") from exc
    if not isinstance(result, dict):
        raise QueryError("query cursor is not an object")
    return result


def _field_value(entity: dict[str, Any], pointer: str) -> Any:
    if pointer in {"", "/"}:
        return entity
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QueryError("field pointer must be empty or start with '/'")
    current: Any = entity
    for raw_segment in pointer[1:].split("/"):
        segment = _json_pointer_unescape(raw_segment)
        if isinstance(current, dict):
            if segment not in current:
                raise QueryError(f"unknown field pointer: {pointer}")
            current = current[segment]
        elif isinstance(current, list):
            if segment == "-" or not segment.isdigit():
                raise QueryError(f"invalid array field pointer: {pointer}")
            index = int(segment)
            if index >= len(current):
                raise QueryError(f"array field pointer is out of range: {pointer}")
            current = current[index]
        else:
            raise QueryError(f"field pointer traverses a scalar: {pointer}")
    return current


def get_field(document: dict[str, Any], collection: str, identifier: str, pointer: str) -> Any:
    """Return one authoritative entity field using an RFC 6901-style pointer.

    Entity lookup alone is not a field-level query contract: callers could
    receive the full object while the index omitted a nested value.  This
    operation makes every serialized field addressable for the product query
    surface.
    """

    return _get_field_validated(_ensure_document(document), collection, identifier, pointer)


def _get_field_validated(document: dict[str, Any], collection: str, identifier: str, pointer: str) -> Any:
    entity = _get_entity_validated(document, collection, identifier)
    extension_type = entity.get("type") if collection == "extensions" and isinstance(entity.get("type"), str) else None
    extension_namespace = entity.get("namespace") if collection == "extensions" and isinstance(entity.get("namespace"), str) else None
    extension_version = entity.get("schemaVersion") if collection == "extensions" and isinstance(entity.get("schemaVersion"), str) else None
    _require_registered_field(
        collection,
        pointer,
        extension_type=extension_type,
        extension_namespace=extension_namespace,
        extension_version=extension_version,
    )
    return _field_value(entity, pointer)


def get_document_field(document: dict[str, Any], pointer: str) -> Any:
    """Return a registered top-level document field.

    Conversion status/features and source format are document facts rather
    than members of an entity collection.  Keeping this operation separate
    makes that distinction explicit while retaining the same missing-vs-null
    behavior as :func:`get_field`.
    """

    return _get_document_field_validated(_ensure_document(document), pointer)


def _get_document_field_validated(document: dict[str, Any], pointer: str) -> Any:
    _require_registered_field(DOCUMENT_COLLECTION, pointer, document=True)
    return _field_value(document, pointer)


def _field_pointers(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key in sorted(value):
            pointer = f"{prefix}/{_json_pointer_escape(key)}"
            yield pointer
            yield from _field_pointers(value[key], pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            pointer = f"{prefix}/{index}"
            yield pointer
            yield from _field_pointers(child, pointer)


def _document_surface(document: dict[str, Any]) -> dict[str, Any]:
    contract = _load_query_contract()
    names = {
        field["path"].split("/", 2)[1].replace("~1", "/").replace("~0", "~")
        for field in contract["documentFieldPaths"]
        if isinstance(field, dict) and isinstance(field.get("path"), str) and field["path"].count("/") >= 1
    }
    return {name: document[name] for name in sorted(names) if name in document}


def _query_field_coverage_validated(document: dict[str, Any]) -> dict[str, Any]:
    """Explore actual facts against the generated authoritative field registry.

    The result is deliberately based on observed concrete pointers.  It does
    not manufacture ``unqueryableFacts: []`` and it does not treat matching
    collection names as field coverage.
    """

    checked: list[dict[str, str]] = []
    unqueryable: list[dict[str, str]] = []
    registered_counts: dict[str, int] = {}
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in _items(document, collection):
            identifier = item[identifier_key]
            for pointer in _field_pointers(item):
                try:
                    _field_value(item, pointer)
                except QueryError as exc:
                    unqueryable.append({
                        "collection": collection,
                        "id": identifier,
                        "pointer": pointer,
                        "error": str(exc),
                    })
                else:
                    extension_type = item.get("type") if collection == "extensions" and isinstance(item.get("type"), str) else None
                    extension_namespace = item.get("namespace") if collection == "extensions" and isinstance(item.get("namespace"), str) else None
                    extension_version = item.get("schemaVersion") if collection == "extensions" and isinstance(item.get("schemaVersion"), str) else None
                    if _registered_field(
                        collection,
                        pointer,
                        extension_type=extension_type,
                        extension_namespace=extension_namespace,
                        extension_version=extension_version,
                    ) is None:
                        unqueryable.append({
                            "collection": collection,
                            "id": identifier,
                            "pointer": pointer,
                            "error": "observed field path is absent from generated query contract",
                        })
                    else:
                        checked.append({"collection": collection, "id": identifier, "pointer": pointer})
                        registered_counts[collection] = registered_counts.get(collection, 0) + 1
    document_id = str(document.get("documentId"))
    surface = _document_surface(document)
    for pointer in _field_pointers(surface):
        try:
            _field_value(surface, pointer)
        except QueryError as exc:
            unqueryable.append({"collection": DOCUMENT_COLLECTION, "id": document_id, "pointer": pointer, "error": str(exc)})
        else:
            if _registered_field(DOCUMENT_COLLECTION, pointer, document=True) is None:
                unqueryable.append({
                    "collection": DOCUMENT_COLLECTION,
                    "id": document_id,
                    "pointer": pointer,
                    "error": "observed document field path is absent from generated query contract",
                })
            else:
                checked.append({"collection": DOCUMENT_COLLECTION, "id": document_id, "pointer": pointer})
                registered_counts[DOCUMENT_COLLECTION] = registered_counts.get(DOCUMENT_COLLECTION, 0) + 1
    contract = _load_query_contract()
    return {
        "status": "passed" if not unqueryable else "failed",
        "checked": checked,
        "unqueryableFacts": unqueryable,
        "checkedFactCount": len(checked),
        "registeredFieldPathCount": contract["fieldPathCount"],
        "observedRegisteredFactCounts": registered_counts,
    }


def query_field_coverage(document: dict[str, Any]) -> dict[str, Any]:
    return _query_field_coverage_validated(_ensure_document(document))


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
        return "list"
    if isinstance(value, dict):
        return "object"
    raise QueryError(f"unsupported query value type: {type(value).__name__}")


def _typed_equal(left: Any, right: Any) -> bool:
    # Python considers True == 1.  The IR query contract does not: the JSON
    # type lane is part of equality and keeps scalar filters exact.
    if _json_type(left) != _json_type(right):
        return False
    return left == right


def _compare_typed(left: Any, right: Any, operator: str) -> bool:
    if operator in {"eq", "neq"}:
        equal = _typed_equal(left, right)
        return equal if operator == "eq" else not equal
    if operator in {"lt", "lte", "gt", "gte"}:
        if _json_type(left) not in {"integer", "number", "string"} or _json_type(right) not in {"integer", "number", "string"}:
            raise QueryError(f"{operator} requires comparable scalar values")
        if _json_type(left) != _json_type(right):
            raise QueryError("typed comparison cannot mix scalar lanes")
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
    raise QueryError(f"unknown field operator: {operator}")


def _query_target_collections(collection: str | None) -> list[str]:
    if collection is None:
        return [DOCUMENT_COLLECTION, *COLLECTION_KEYS]
    if collection == DOCUMENT_COLLECTION or collection in COLLECTION_KEYS:
        return [collection]
    raise QueryError(f"unknown entity collection: {collection}")


def _entity_status(item: dict[str, Any], collection: str) -> str | None:
    value = item.get("status")
    return value if isinstance(value, str) else None


def query_fields(
    document: dict[str, Any],
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
    """Query registered concrete field paths with typed semantics.

    ``pointer`` may contain ``*``/``**`` only when the caller wants a
    projection over a registered template.  Missing fields are returned with
    ``presence=missing`` only for ``exists``/``is-missing``/``is-null``;
    ``null`` remains a real stored value.
    """

    document = _ensure_document(document)
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QueryError("query field pointer must start with '/'")
    targets = _query_target_collections(collection)
    contract = _load_query_contract()
    response_metadata = {
        "queryContractVersion": contract["version"],
        "profileId": profile or document.get("conversion", {}).get("capabilityProfile"),
    }
    if profile is not None:
        registered_profiles = set(contract.get("profiles", []))
        if profile not in registered_profiles:
            raise QueryError(f"unknown capability profile: {profile}")
        if document.get("conversion", {}).get("capabilityProfile") != profile:
            return []
    if operator not in {"eq", "neq", "lt", "lte", "gt", "gte", "prefix", "contains", "exists", "is-null", "is-missing"}:
        raise QueryError(f"unknown field operator: {operator}")
    if operator not in {"exists", "is-null", "is-missing"} and value is _MISSING:
        raise QueryError(f"operator {operator} requires a value")
    results: list[dict[str, Any]] = []
    matched_targets = 0
    for target_collection in targets:
        is_document = target_collection == DOCUMENT_COLLECTION
        matching_registry = _compiled_query_fields().matching(
            target_collection,
            pointer,
            document=is_document,
            extension_type=extension_type if target_collection == "extensions" else None,
            extension_namespace=namespace if target_collection == "extensions" else None,
            extension_version=schema_version if target_collection == "extensions" else None,
        )
        if not matching_registry:
            if collection is None:
                continue
            raise QueryError(f"field pointer is not registered: {target_collection}{pointer}")
        matched_targets += 1
        if not any(operator in field.get("filterOperators", []) for field in matching_registry):
            raise QueryError(f"operator {operator} is not registered for {target_collection}{pointer}")
        if is_document:
            records: Iterable[tuple[str, dict[str, Any], str | None]] = [
                (str(document["documentId"]), _document_surface(document), None)
            ]
        else:
            identifier_key = COLLECTION_KEYS[target_collection]
            records = [
                (str(item[identifier_key]), item, _entity_status(item, target_collection))
                for item in _items(document, target_collection)
                if (kind is None or item.get("kind") == kind)
                and (status is None or _entity_status(item, target_collection) == status)
                and (target_collection != "extensions" or namespace is None or item.get("namespace") == namespace)
                and (target_collection != "extensions" or extension_type is None or item.get("type") == extension_type)
                and (target_collection != "extensions" or schema_version is None or item.get("schemaVersion") == schema_version)
            ]
        for identifier, entity, entity_status in records:
            actual_pointers = list(_field_pointers(entity))
            matching = [candidate for candidate in actual_pointers if _pointer_matches(pointer, candidate) or _pointer_matches(candidate, pointer)]
            if not matching:
                if operator == "is-missing":
                    results.append({**response_metadata, "collection": target_collection, "id": identifier, "pointer": pointer, "presence": "missing", "value": None, "status": entity_status})
                elif operator == "exists":
                    continue
                elif operator == "is-null":
                    continue
                continue
            for actual_pointer in matching:
                extension_type = entity.get("type") if target_collection == "extensions" and isinstance(entity.get("type"), str) else None
                extension_namespace = entity.get("namespace") if target_collection == "extensions" and isinstance(entity.get("namespace"), str) else None
                extension_version = entity.get("schemaVersion") if target_collection == "extensions" and isinstance(entity.get("schemaVersion"), str) else None
                if _registered_field(
                    target_collection,
                    actual_pointer,
                    document=is_document,
                    extension_type=extension_type,
                    extension_namespace=extension_namespace,
                    extension_version=extension_version,
                ) is None:
                    continue
                actual = _field_value(entity, actual_pointer)
                matches = (
                    actual is not _MISSING and operator == "exists"
                ) or (
                    actual is None and operator == "is-null"
                ) or (
                    actual is not _MISSING and operator not in {"exists", "is-null", "is-missing"} and _compare_typed(actual, value, operator)
                )
                if matches:
                    results.append({**response_metadata, "collection": target_collection, "id": identifier, "pointer": actual_pointer, "presence": "null" if actual is None else "value", "value": actual, "status": entity_status})
    if matched_targets == 0:
        raise QueryError(f"field pointer is not registered: {pointer}")
    results.sort(key=lambda item: (item["collection"], item["id"], item["pointer"]))
    return results


def find_field_equals(
    document: dict[str, Any],
    pointer: str,
    value: Any,
    *,
    collection: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    profile: str | None = None,
    namespace: str | None = None,
    extension_type: str | None = None,
    schema_version: str | None = None,
) -> list[dict[str, str]]:
    """Return compact typed equality results compatible with the index API."""

    return [
        {"collection": item["collection"], "id": item["id"]}
        for item in query_fields(
            document,
            pointer,
            value,
            operator="eq",
            collection=collection,
            kind=kind,
            status=status,
            profile=profile,
            namespace=namespace,
            extension_type=extension_type,
            schema_version=schema_version,
        )
    ]


def _descendants_validated(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    nodes = {item.get("nodeId"): item for item in _items(document, "nodes")}
    if node_id not in nodes:
        raise QueryError(f"unknown node: {node_id}")
    result: list[dict[str, Any]] = []
    pending = deque(nodes[node_id].get("childIds", []))
    while pending:
        child_id = pending.popleft()
        child = nodes[child_id]
        result.append(child)
        pending.extend(child.get("childIds", []))
    return result


def descendants(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return _descendants_validated(_ensure_document(document), node_id)


def _ancestors_validated(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    nodes = {item.get("nodeId"): item for item in _items(document, "nodes")}
    if node_id not in nodes:
        raise QueryError(f"unknown node: {node_id}")
    result: list[dict[str, Any]] = []
    current = nodes[node_id].get("parentId")
    while current is not None:
        parent = nodes[current]
        result.append(parent)
        current = parent.get("parentId")
    return result


def ancestors(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return _ancestors_validated(_ensure_document(document), node_id)


def _reference_pairs(
    item: dict[str, Any],
    identifier_key: str,
    known_ids: dict[str, str],
) -> Iterable[tuple[str, str, str, int | None]]:
    """Yield nested references as concrete JSON pointers.

    Traversal is recursive over objects, lists, and map values.  List ordinals
    are part of the pointer; a list-valued reference is never collapsed to a
    single top-level field name.
    """

    def walk(value: Any, field_path: str, *, root: bool = False) -> Iterable[tuple[str, str, str, int | None]]:
        if isinstance(value, dict):
            for key, child in value.items():
                if root and key in {identifier_key, "schemaId", "documentId"}:
                    continue
                path = f"{field_path}/{_json_pointer_escape(key)}" if field_path else f"/{_json_pointer_escape(key)}"
                if key.endswith("Id") and key != identifier_key and isinstance(child, str) and child in known_ids:
                    yield path, child, known_ids[child], None
                elif key.endswith("Ids") and isinstance(child, list):
                    for ordinal, target in enumerate(child):
                        if isinstance(target, str) and target in known_ids:
                            yield f"{path}/{ordinal}", target, known_ids[target], ordinal
                elif key == "id" and isinstance(child, str) and "type" in value and child in known_ids:
                    yield path, child, known_ids[child], None
                yield from walk(child, path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, f"{field_path}/{index}")

    yield from walk(item, "", root=True)


def _field_fact_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a digest-only projection of every observed registered fact."""

    rows: list[dict[str, Any]] = []

    def add(
        collection: str,
        identifier: str,
        value: Any,
        pointer: str,
        *,
        document_scope: bool = False,
        extension_type: str | None = None,
        extension_namespace: str | None = None,
        extension_version: str | None = None,
    ) -> None:
        if _registered_field(
            collection,
            pointer,
            document=document_scope,
            extension_type=extension_type,
            extension_namespace=extension_namespace,
            extension_version=extension_version,
        ) is None:
            raise QueryError(f"observed field path is not registered: {collection}{pointer}")
        rows.append({
            "collection": collection,
            "id": identifier,
            "pointer": pointer,
            "digest": canonical_value_digest(value),
            "type": _json_type(value),
            "presence": "null" if value is None else "value",
        })

    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in _items(document, collection):
            identifier = str(item[identifier_key])
            for pointer in _field_pointers(item):
                extension_type = item.get("type") if collection == "extensions" and isinstance(item.get("type"), str) else None
                extension_namespace = item.get("namespace") if collection == "extensions" and isinstance(item.get("namespace"), str) else None
                extension_version = item.get("schemaVersion") if collection == "extensions" and isinstance(item.get("schemaVersion"), str) else None
                add(
                    collection,
                    identifier,
                    _field_value(item, pointer),
                    pointer,
                    extension_type=extension_type,
                    extension_namespace=extension_namespace,
                    extension_version=extension_version,
                )
    document_id = str(document["documentId"])
    surface = _document_surface(document)
    for pointer in _field_pointers(surface):
        add(DOCUMENT_COLLECTION, document_id, _field_value(surface, pointer), pointer, document_scope=True)
    rows.sort(key=lambda row: (row["collection"], row["id"], row["pointer"]))
    return rows


def _build_index(document: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, non-authoritative projection from validated IR."""

    entities: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    field_facts = _field_fact_rows(document)
    reverse: list[dict[str, Any]] = []
    known_ids = {
        item[identifier_key]: collection
        for collection, identifier_key in COLLECTION_KEYS.items()
        for item in _items(document, collection)
    }
    known_ids[str(document["documentId"])] = DOCUMENT_COLLECTION
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in sorted(_items(document, collection), key=lambda value: str(value.get(identifier_key, ""))):
            identifier = item[identifier_key]
            entities.append({"id": identifier, "collection": collection, "kind": item.get("kind"), "status": item.get("status")})
            facts.append({"collection": collection, "id": identifier, "digest": canonical_value_digest(item)})
            reverse.extend(
                {
                    "fromCollection": collection,
                    "fromId": identifier,
                    "field": field,
                    "toCollection": target_collection,
                    "toId": target,
                    "ordinal": ordinal,
                }
                for field, target, target_collection, ordinal in _reference_pairs(item, identifier_key, known_ids)
            )
    document_payload = {"documentId": str(document["documentId"]), **_document_surface(document)}
    reverse.extend(
        {
            "fromCollection": DOCUMENT_COLLECTION,
            "fromId": str(document["documentId"]),
            "field": field,
            "toCollection": target_collection,
            "toId": target,
            "ordinal": ordinal,
        }
        for field, target, target_collection, ordinal in _reference_pairs(
            document_payload, "documentId", known_ids
        )
    )
    entities.sort(key=lambda value: (value["collection"], value["id"]))
    facts.sort(key=lambda value: (value["collection"], value["id"]))
    reverse.sort(key=lambda value: (
        value["toCollection"], value["toId"], value["fromCollection"],
        value["fromId"], value["field"], -1 if value["ordinal"] is None else value["ordinal"],
    ))
    contract = _load_query_contract()
    return {
        "schema": INDEX_SCHEMA,
        "version": INDEX_VERSION,
        "authority": {
            "documentId": document["documentId"],
            "canonicalDigest": canonical_digest(document),
            "projection": "source-map-excluded",
            "schema": document["schema"],
            "queryContractVersion": contract["version"],
            "queryContractSourceDigest": contract["generated"]["sourceDigest"],
            "registeredFieldPathCount": contract["fieldPathCount"],
        },
        "entities": entities,
        "facts": facts,
        "fieldFacts": field_facts,
        "reverseReferences": reverse,
    }


def rebuild_index(document: dict[str, Any]) -> dict[str, Any]:
    return _build_index(_ensure_document(document))


def validate_index(document: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when an index is stale, corrupt, incomplete, or unqueryable."""

    document = _ensure_document(document)
    if not isinstance(index, dict):
        raise QueryError("index must be an object")
    expected = _build_index(document)
    if index.get("schema") != INDEX_SCHEMA or index.get("version") != INDEX_VERSION:
        raise QueryError("index is corrupt: schema or version is invalid")
    if index.get("authority") != expected["authority"]:
        raise QueryError("index is stale or corrupt: authority fingerprint does not match IR")
    for field in ("entities", "facts", "fieldFacts", "reverseReferences"):
        if index.get(field) != expected[field]:
            raise QueryError(f"index is stale or corrupt: {field} does not match authoritative IR")
    if set(index) != set(expected):
        raise QueryError("index is corrupt: unexpected or missing index fields")
    return {"status": "passed", "validated": True}


def index_parity(document: dict[str, Any], index: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare every entity and full entity fact digest with a derived index."""

    document = _ensure_document(document)
    candidate = rebuild_index(document) if index is None else index
    validate_index(document, candidate)
    field_coverage = query_field_coverage(document)
    direct_entity_count = sum(len(_items(document, collection)) for collection in COLLECTION_KEYS)
    return {
        "status": "passed",
        "directEntityCount": direct_entity_count,
        "indexEntityCount": len(candidate["entities"]),
        "directFactCount": len(field_coverage["checked"]),
        "indexFactCount": len(candidate["fieldFacts"]),
        "reverseReferenceCount": len(candidate["reverseReferences"]),
        "operations": ["list-entities", "get-entity", "get-field", "field-coverage", "rebuild-index", "validate-index"],
        "unqueryableFacts": field_coverage["unqueryableFacts"],
        "fieldCoverage": field_coverage,
        "mismatches": [],
    }


def _filter(items: Iterable[dict[str, Any]], **criteria: str | None) -> list[dict[str, Any]]:
    return [item for item in items if all(value is None or item.get(key) == value for key, value in criteria.items())]


def _find_relations_validated(document: dict[str, Any], kind: str | None = None,
                              from_id: str | None = None, to_id: str | None = None) -> list[dict[str, Any]]:
    return _filter(_items(document, "relations"), kind=kind, fromId=from_id, toId=to_id)


def find_relations(document: dict[str, Any], kind: str | None = None,
                   from_id: str | None = None, to_id: str | None = None) -> list[dict[str, Any]]:
    return _find_relations_validated(_ensure_document(document), kind, from_id, to_id)


def _find_extensions_validated(document: dict[str, Any], namespace: str | None = None,
                               extension_type: str | None = None, target_id: str | None = None,
                               schema_version: str | None = None) -> list[dict[str, Any]]:
    return _filter(
        _items(document, "extensions"),
        namespace=namespace,
        type=extension_type,
        targetId=target_id,
        schemaVersion=schema_version,
    )


def find_extensions(document: dict[str, Any], namespace: str | None = None,
                    extension_type: str | None = None, target_id: str | None = None,
                    schema_version: str | None = None) -> list[dict[str, Any]]:
    return _find_extensions_validated(_ensure_document(document), namespace, extension_type, target_id, schema_version)


def _find_observations_validated(document: dict[str, Any], target_id: str | None = None,
                                 observation_kind: str | None = None) -> list[dict[str, Any]]:
    return _filter(_items(document, "observations"), targetId=target_id, kind=observation_kind)


def find_observations(document: dict[str, Any], target_id: str | None = None,
                      observation_kind: str | None = None) -> list[dict[str, Any]]:
    return _find_observations_validated(_ensure_document(document), target_id, observation_kind)


def find_references(
    document: dict[str, Any],
    *,
    target_id: str | None = None,
    source_id: str | None = None,
    source_collection: str | None = None,
    pointer: str | None = None,
) -> list[dict[str, Any]]:
    """Query nested and typed references with concrete list ordinals."""

    document = _ensure_document(document)
    return _find_references_validated(
        document,
        target_id=target_id,
        source_id=source_id,
        source_collection=source_collection,
        pointer=pointer,
    )


def _find_references_validated(
    document: dict[str, Any],
    *,
    index: dict[str, Any] | None = None,
    target_id: str | None = None,
    source_id: str | None = None,
    source_collection: str | None = None,
    pointer: str | None = None,
) -> list[dict[str, Any]]:
    references = (index if index is not None else _build_index(document))["reverseReferences"]
    result = [
        row for row in references
        if (target_id is None or row["toId"] == target_id)
        and (source_id is None or row["fromId"] == source_id)
        and (source_collection is None or row["fromCollection"] == source_collection)
        and (pointer is None or row["field"] == pointer)
    ]
    result.sort(key=lambda row: (
        row["toCollection"], row["toId"], row["fromCollection"], row["fromId"],
        row["field"], -1 if row["ordinal"] is None else row["ordinal"],
    ))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)

    nodes = sub.add_parser("list-nodes")
    nodes.add_argument("--kind")
    nodes.add_argument("--part-id")
    nodes.add_argument("--status")

    text = sub.add_parser("get-text")
    text.add_argument("node_id")
    text.add_argument("--representation", default="source", choices=["source", "normalized", "stored", "computed", "displayed", "rendered", "observed"])
    text.add_argument("--scope", default="descendants", choices=["direct", "descendants"])

    entity = sub.add_parser("list-entities")
    entity.add_argument("collection")
    entity.add_argument("--kind")
    entity.add_argument("--status")
    entity.add_argument("--id")
    entity.add_argument("--offset", type=int, default=0)
    entity.add_argument("--limit", type=int)

    entity_page = sub.add_parser("list-entities-page")
    entity_page.add_argument("collection")
    entity_page.add_argument("--kind")
    entity_page.add_argument("--status")
    entity_page.add_argument("--limit", type=int, default=100)
    entity_page.add_argument("--cursor")
    entity_page.add_argument("--profile")

    lookup = sub.add_parser("get-entity")
    lookup.add_argument("collection")
    lookup.add_argument("identifier")

    field = sub.add_parser("get-field")
    field.add_argument("collection")
    field.add_argument("identifier")
    field.add_argument("pointer")

    document_field = sub.add_parser("get-document-field")
    document_field.add_argument("pointer")

    query = sub.add_parser("query-fields")
    query.add_argument("pointer")
    query.add_argument("--value")
    query.add_argument("--operator", default="eq")
    query.add_argument("--collection")
    query.add_argument("--kind")
    query.add_argument("--status")
    query.add_argument("--profile")
    query.add_argument("--namespace")
    query.add_argument("--extension-type")
    query.add_argument("--schema-version")

    coverage = sub.add_parser("field-coverage")

    descendant = sub.add_parser("descendants")
    descendant.add_argument("node_id")

    ancestor = sub.add_parser("ancestors")
    ancestor.add_argument("node_id")

    index = sub.add_parser("rebuild-index")
    index.add_argument("--out", type=Path)

    validate_index_parser = sub.add_parser("validate-index")
    validate_index_parser.add_argument("index", type=Path)

    relation = sub.add_parser("find-relations")
    relation.add_argument("--kind")
    relation.add_argument("--from-id")
    relation.add_argument("--to-id")

    extension = sub.add_parser("find-extensions")
    extension.add_argument("--namespace")
    extension.add_argument("--type")
    extension.add_argument("--target-id")
    extension.add_argument("--schema-version")

    observation = sub.add_parser("find-observations")
    observation.add_argument("--target-id")
    observation.add_argument("--kind")

    references = sub.add_parser("find-references")
    references.add_argument("--target-id")
    references.add_argument("--source-id")
    references.add_argument("--source-collection")
    references.add_argument("--pointer")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = load_document(args.input)
        if args.operation == "list-nodes":
            result = list_nodes(document, args.kind, args.part_id, args.status)
        elif args.operation == "get-text":
            if args.scope == "direct":
                node = get_entity(document, "nodes", args.node_id)
                direct_ids = set(node.get("textIds", []))
                result = [text for text in _items(document, "texts") if text.get("textId") in direct_ids and text.get("representation") == args.representation]
            else:
                result = get_text(document, args.node_id, args.representation)
        elif args.operation == "find-relations":
            result = find_relations(document, args.kind, args.from_id, args.to_id)
        elif args.operation == "find-extensions":
            result = find_extensions(document, args.namespace, args.type, args.target_id, args.schema_version)
        elif args.operation == "find-observations":
            result = find_observations(document, args.target_id, args.kind)
        elif args.operation == "find-references":
            result = find_references(
                document,
                target_id=args.target_id,
                source_id=args.source_id,
                source_collection=args.source_collection,
                pointer=args.pointer,
            )
        elif args.operation == "list-entities":
            result = list_entities(document, args.collection, kind=args.kind, status=args.status, identifier=args.id, offset=args.offset, limit=args.limit)
        elif args.operation == "list-entities-page":
            result = list_entities_page(
                document,
                args.collection,
                kind=args.kind,
                status=args.status,
                limit=args.limit,
                cursor=args.cursor,
                profile=args.profile,
            )
        elif args.operation == "get-entity":
            result = get_entity(document, args.collection, args.identifier)
        elif args.operation == "get-field":
            result = get_field(document, args.collection, args.identifier, args.pointer)
        elif args.operation == "get-document-field":
            result = get_document_field(document, args.pointer)
        elif args.operation == "query-fields":
            parsed_value: Any = _MISSING
            if args.value is not None:
                try:
                    parsed_value = json.loads(args.value, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise QueryError(f"--value must be canonical JSON: {exc}") from exc
            result = query_fields(
                document,
                args.pointer,
                parsed_value,
                operator=args.operator,
                collection=args.collection,
                kind=args.kind,
                status=args.status,
                profile=args.profile,
                namespace=args.namespace,
                extension_type=args.extension_type,
                schema_version=args.schema_version,
            )
        elif args.operation == "field-coverage":
            result = query_field_coverage(document)
        elif args.operation == "descendants":
            result = descendants(document, args.node_id)
        elif args.operation == "ancestors":
            result = ancestors(document, args.node_id)
        elif args.operation == "rebuild-index":
            result = rebuild_index(document)
            if args.out:
                args.out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        elif args.operation == "validate-index":
            result = index_parity(document, load_index(args.index))
        else:  # pragma: no cover - argparse enforces the operation
            raise QueryError(f"unknown operation: {args.operation}")
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
    except QueryError as exc:
        print(f"QUERY ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
