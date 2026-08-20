"""Qualification for direct typed queries and deterministic index rebuilds."""

from __future__ import annotations

import json
from pathlib import Path
import sys

try:
    from ir_validation import COLLECTION_KEYS
    from query_ir import ancestors, descendants, get_entity, list_entities, load_document, rebuild_index
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import ancestors, descendants, get_entity, list_entities, load_document, rebuild_index


ROOT = Path(__file__).resolve().parents[1]


def qualify(path: Path) -> dict[str, int | str]:
    document = load_document(path)
    index = rebuild_index(document)
    expected_ids = sorted((collection, item[identifier]) for collection, identifier in COLLECTION_KEYS.items() for item in document.get(collection, []))
    actual_ids = sorted((item["collection"], item["id"]) for item in index["entities"])
    if expected_ids != actual_ids:
        raise AssertionError(f"index entity mismatch for {path.name}")
    for collection, identifier in COLLECTION_KEYS.items():
        values = list_entities(document, collection)
        if len(values) != len(document.get(collection, [])):
            raise AssertionError(f"typed list mismatch: {collection}")
        for item in values:
            if get_entity(document, collection, item[identifier])[identifier] != item[identifier]:
                raise AssertionError(f"typed lookup mismatch: {collection}/{item[identifier]}")
    nodes = document.get("nodes", [])
    for node in nodes:
        node_id = node["nodeId"]
        direct_children = node.get("childIds", [])
        if [item["nodeId"] for item in descendants(document, node_id) if item["nodeId"] in direct_children] != direct_children:
            raise AssertionError(f"descendant traversal mismatch: {node_id}")
        expected_ancestors: list[str] = []
        parent = node.get("parentId")
        by_id = {item["nodeId"]: item for item in nodes}
        while parent is not None:
            expected_ancestors.append(parent)
            parent = by_id[parent].get("parentId")
        if [item["nodeId"] for item in ancestors(document, node_id)] != expected_ancestors:
            raise AssertionError(f"ancestor traversal mismatch: {node_id}")
    return {"fixture": path.name, "entities": len(index["entities"]), "reverseReferences": len(index["reverseReferences"]), "status": "passed"}


def main() -> int:
    reports = [qualify(path) for path in sorted((ROOT / "examples").glob("*.json"))]
    output = {"schema": "fdir/query-qualification-report", "version": "1.0.0", "status": "passed", "fixtures": reports, "fixtureCount": len(reports)}
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
