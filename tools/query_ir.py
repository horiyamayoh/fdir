"""Typed, non-semantic queries over a Document Form IR JSON document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


class QueryError(ValueError):
    """Raised for an invalid query or malformed IR input."""


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryError(f"cannot load IR: {exc}") from exc
    if not isinstance(value, dict):
        raise QueryError("IR document must be an object")
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
    text_ids = node.get("textIds", [])
    if not isinstance(text_ids, list):
        raise QueryError(f"node textIds is not an array: {node_id}")
    texts = _items(document, "texts")
    return [
        text for text in texts
        if text.get("textId") in text_ids and text.get("representation") == representation
    ]


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
    text.add_argument("--representation", default="source", choices=["source", "normalized", "displayed", "observed"])

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
            result = get_text(document, args.node_id, args.representation)
        elif args.operation == "find-relations":
            result = find_relations(document, args.kind, args.from_id, args.to_id)
        elif args.operation == "find-extensions":
            result = find_extensions(document, args.namespace, args.type, args.target_id)
        elif args.operation == "find-observations":
            result = find_observations(document, args.target_id, args.kind)
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
