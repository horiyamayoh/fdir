"""Typed, non-semantic queries over a Document Form IR JSON document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from canonicalize_ir import canonical_digest
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS, IRValidationError, validate_document
    from tools.canonicalize_ir import canonical_digest


class QueryError(ValueError):
    """Raised for an invalid query or malformed IR input."""


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryError(f"cannot load IR: {exc}") from exc
    if not isinstance(value, dict):
        raise QueryError("IR document must be an object")
    try:
        validate_document(value)
    except IRValidationError as exc:
        raise QueryError(f"IR failed authority validation: {exc}") from exc
    return value


def _items(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = document.get(key, [])
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise QueryError(f"IR field {key!r} must be an array of objects")
    return values


def list_nodes(document: dict[str, Any], kind: str | None = None, part_id: str | None = None,
               status: str | None = None) -> list[dict[str, Any]]:
    result = _items(document, "nodes")
    return [
        node for node in result
        if (kind is None or node.get("kind") == kind)
        and (part_id is None or node.get("partId") == part_id)
        and (status is None or node.get("status") == status)
    ]


def get_text(document: dict[str, Any], node_id: str, representation: str = "source") -> list[dict[str, Any]]:
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
    collection = _collection_name(collection)
    identifier_key = COLLECTION_KEYS[collection]
    items = _items(document, collection)
    result = [item for item in items if (identifier is None or item.get(identifier_key) == identifier) and (kind is None or item.get("kind") == kind) and (status is None or item.get("status") == status)]
    result.sort(key=lambda item: str(item.get(identifier_key, "")))
    result = result[max(0, offset):]
    return result if limit is None else result[:max(0, limit)]


def get_entity(document: dict[str, Any], collection: str, identifier: str) -> dict[str, Any]:
    values = list_entities(document, collection, identifier=identifier)
    if not values:
        raise QueryError(f"unknown {collection} entity: {identifier}")
    return values[0]


def descendants(document: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
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


def rebuild_index(document: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, non-authoritative projection from validated IR."""

    entities: list[dict[str, Any]] = []
    reverse: list[dict[str, Any]] = []
    for collection, identifier_key in COLLECTION_KEYS.items():
        for item in sorted(_items(document, collection), key=lambda value: str(value.get(identifier_key, ""))):
            identifier = item[identifier_key]
            entities.append({"id": identifier, "collection": collection, "kind": item.get("kind"), "status": item.get("status")})
            for field, value in item.items():
                if field in {identifier_key, "schemaId", "documentId"}:
                    continue
                if field.endswith("Id") and isinstance(value, str):
                    reverse.append({"fromId": identifier, "field": field, "toId": value})
                elif field.endswith("Ids") and isinstance(value, list):
                    reverse.extend({"fromId": identifier, "field": field, "toId": target} for target in value if isinstance(target, str))
    entities.sort(key=lambda value: (value["collection"], value["id"]))
    reverse.sort(key=lambda value: (value["toId"], value["fromId"], value["field"]))
    return {
        "schema": "fdir/document-form-index",
        "version": "1.0.0",
        "authority": {"documentId": document["documentId"], "canonicalDigest": canonical_digest(document), "schema": document["schema"]},
        "entities": entities,
        "reverseReferences": reverse,
    }


def _filter(items: Iterable[dict[str, Any]], **criteria: str | None) -> list[dict[str, Any]]:
    return [item for item in items if all(value is None or item.get(key) == value for key, value in criteria.items())]


def find_relations(document: dict[str, Any], kind: str | None = None,
                   from_id: str | None = None, to_id: str | None = None) -> list[dict[str, Any]]:
    return _filter(_items(document, "relations"), kind=kind, fromId=from_id, toId=to_id)


def find_extensions(document: dict[str, Any], namespace: str | None = None,
                    extension_type: str | None = None, target_id: str | None = None) -> list[dict[str, Any]]:
    return _filter(_items(document, "extensions"), namespace=namespace, type=extension_type, targetId=target_id)


def find_observations(document: dict[str, Any], target_id: str | None = None,
                      observation_kind: str | None = None) -> list[dict[str, Any]]:
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
