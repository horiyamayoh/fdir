"""Typed, non-semantic queries over a Document Form IR JSON document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from canonicalize_ir import canonical_digest, canonical_value_digest
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from tools.canonicalize_ir import canonical_digest, canonical_value_digest


class QueryError(ValueError):
    """Raised for an invalid query or malformed IR input."""


INDEX_SCHEMA = "fdir/document-form-index"
INDEX_VERSION = "1.1.0"
REPRESENTATIONS = {"source", "normalized", "stored", "computed", "displayed", "rendered", "observed"}


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {token}")


def _ensure_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise QueryError("IR must be an object")
    try:
        validate_document(document)
    except IRValidationError as exc:
        raise QueryError(f"IR failed authority validation: {exc}") from exc
    return document


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


def list_nodes(document: dict[str, Any], kind: str | None = None, part_id: str | None = None,
               status: str | None = None) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    result = _items(document, "nodes")
    return [
        node for node in result
        if (kind is None or node.get("kind") == kind)
        and (part_id is None or node.get("partId") == part_id)
        and (status is None or node.get("status") == status)
    ]


def get_text(document: dict[str, Any], node_id: str, representation: str = "source") -> list[dict[str, Any]]:
    document = _ensure_document(document)
    if representation not in REPRESENTATIONS:
        raise QueryError(f"unknown text representation: {representation}")
    node = next((item for item in _items(document, "nodes") if item.get("nodeId") == node_id), None)
    if node is None:
        raise QueryError(f"unknown node: {node_id}")
    text_ids = list(node.get("textIds", []))
    for descendant in descendants(document, node_id):
        text_ids.extend(descendant.get("textIds", []))
    if not isinstance(text_ids, list):
        raise QueryError(f"node textIds is not an array: {node_id}")
    texts = _items(document, "texts")
    return [
        text for text in texts
        if text.get("textId") in text_ids and text.get("representation") == representation
    ]


def _collection_name(collection: str) -> str:
    if collection not in COLLECTION_KEYS:
        raise QueryError(f"unknown entity collection: {collection}")
    return collection


def list_entities(document: dict[str, Any], collection: str, *, kind: str | None = None, status: str | None = None,
                  identifier: str | None = None, offset: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    collection = _collection_name(collection)
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


def get_entity(document: dict[str, Any], collection: str, identifier: str) -> dict[str, Any]:
    document = _ensure_document(document)
    values = list_entities(document, collection, identifier=identifier)
    if not values:
        raise QueryError(f"unknown {collection} entity: {identifier}")
    return values[0]


def descendants(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    nodes = {item.get("nodeId"): item for item in _items(document, "nodes")}
    if node_id not in nodes:
        raise QueryError(f"unknown node: {node_id}")
    result: list[dict[str, Any]] = []
    pending = list(nodes[node_id].get("childIds", []))
    while pending:
        child_id = pending.pop(0)
        child = nodes[child_id]
        result.append(child)
        pending.extend(child.get("childIds", []))
    return result


def ancestors(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    document = _ensure_document(document)
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


def _reference_pairs(item: dict[str, Any], identifier_key: str, known_ids: set[str]) -> Iterable[tuple[str, str]]:
    """Yield nested ID references without treating the index as authority."""

    def walk(value: Any, field_path: str) -> Iterable[tuple[str, str]]:
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{field_path}.{key}" if field_path else key
                if key.endswith("Id") and key != identifier_key and isinstance(child, str) and child in known_ids:
                    yield path, child
                elif key.endswith("Ids") and isinstance(child, list):
                    for target in child:
                        if isinstance(target, str) and target in known_ids:
                            yield path, target
                yield from walk(child, path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, f"{field_path}[{index}]")

    for field, value in item.items():
        if field in {identifier_key, "schemaId", "documentId"}:
            continue
        yield from walk(value, field)


def _build_index(document: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, non-authoritative projection from validated IR."""

    entities: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    reverse: list[dict[str, Any]] = []
    known_ids = {
        item[identifier_key]
        for collection, identifier_key in COLLECTION_KEYS.items()
        for item in _items(document, collection)
    }
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in sorted(_items(document, collection), key=lambda value: str(value.get(identifier_key, ""))):
            identifier = item[identifier_key]
            entities.append({"id": identifier, "collection": collection, "kind": item.get("kind"), "status": item.get("status")})
            facts.append({"collection": collection, "id": identifier, "digest": canonical_value_digest(item)})
            reverse.extend(
                {"fromId": identifier, "field": field, "toId": target}
                for field, target in _reference_pairs(item, identifier_key, known_ids)
            )
    entities.sort(key=lambda value: (value["collection"], value["id"]))
    facts.sort(key=lambda value: (value["collection"], value["id"]))
    reverse.sort(key=lambda value: (value["toId"], value["fromId"], value["field"]))
    return {
        "schema": INDEX_SCHEMA,
        "version": INDEX_VERSION,
        "authority": {
            "documentId": document["documentId"],
            "canonicalDigest": canonical_digest(document),
            "projection": "source-map-excluded",
            "schema": document["schema"],
        },
        "entities": entities,
        "facts": facts,
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
    for field in ("entities", "facts", "reverseReferences"):
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
    direct_entity_count = sum(len(_items(document, collection)) for collection in COLLECTION_KEYS)
    fact_count = direct_entity_count
    return {
        "status": "passed",
        "directEntityCount": direct_entity_count,
        "indexEntityCount": len(candidate["entities"]),
        "directFactCount": fact_count,
        "indexFactCount": len(candidate["facts"]),
        "reverseReferenceCount": len(candidate["reverseReferences"]),
        "operations": ["list-entities", "get-entity", "rebuild-index", "validate-index"],
        "unqueryableFacts": [],
        "mismatches": [],
    }


def _filter(items: Iterable[dict[str, Any]], **criteria: str | None) -> list[dict[str, Any]]:
    return [item for item in items if all(value is None or item.get(key) == value for key, value in criteria.items())]


def find_relations(document: dict[str, Any], kind: str | None = None,
                   from_id: str | None = None, to_id: str | None = None) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    return _filter(_items(document, "relations"), kind=kind, fromId=from_id, toId=to_id)


def find_extensions(document: dict[str, Any], namespace: str | None = None,
                    extension_type: str | None = None, target_id: str | None = None) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    return _filter(_items(document, "extensions"), namespace=namespace, type=extension_type, targetId=target_id)


def find_observations(document: dict[str, Any], target_id: str | None = None,
                      observation_kind: str | None = None) -> list[dict[str, Any]]:
    document = _ensure_document(document)
    return _filter(_items(document, "observations"), targetId=target_id, kind=observation_kind)


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

    lookup = sub.add_parser("get-entity")
    lookup.add_argument("collection")
    lookup.add_argument("identifier")

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

    observation = sub.add_parser("find-observations")
    observation.add_argument("--target-id")
    observation.add_argument("--kind")
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
            result = find_extensions(document, args.namespace, args.type, args.target_id)
        elif args.operation == "find-observations":
            result = find_observations(document, args.target_id, args.kind)
        elif args.operation == "list-entities":
            result = list_entities(document, args.collection, kind=args.kind, status=args.status, identifier=args.id, offset=args.offset, limit=args.limit)
        elif args.operation == "get-entity":
            result = get_entity(document, args.collection, args.identifier)
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
