"""Qualification for direct typed queries and deterministic index rebuilds."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from ir_validation import COLLECTION_KEYS
    from query_ir import ancestors, descendants, get_entity, list_entities, load_document, rebuild_index
    from qualification_evidence import query_parity
except ImportError:  # pragma: no cover
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import ancestors, descendants, get_entity, list_entities, load_document, rebuild_index
    from tools.qualification_evidence import query_parity


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


def _run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run([sys.executable, *command], cwd=ROOT, text=True, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        raise AssertionError(f"qualification command failed: {' '.join(command)}: {result.stdout[-500:]} {result.stderr[-500:]}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise AssertionError(f"qualification report is not an object: {' '.join(command)}")
    return value


def main() -> int:
    reports = [qualify(path) for path in sorted((ROOT / "examples").glob("*.json"))]
    example_parity = [query_parity(load_document(path)) for path in sorted((ROOT / "examples").glob("*.json"))]
    e2e = _run_json(["tools/run_e2e.py", "--all", "--json"])
    if e2e.get("status") != "passed":
        raise AssertionError("real-input E2E did not pass")
    e2e_parity = [case.get("queryParity", {}) for case in e2e.get("cases", []) if isinstance(case, dict)]
    if any(item.get("status") != "passed" for item in e2e_parity):
        raise AssertionError("real-input E2E contains a query parity failure")
    corpus = _run_json(["tools/independent_corpus.py", "--json"])
    if corpus.get("status") != "passed":
        raise AssertionError("independent corpus did not pass")
    corpus_parity = [case.get("queryParity", {}) for case in corpus.get("cases", []) if isinstance(case, dict)]
    if any(item.get("status") != "passed" for item in corpus_parity):
        raise AssertionError("independent corpus contains a query parity failure")
    all_parity = example_parity + e2e_parity + corpus_parity
    operations = sorted({operation for item in all_parity for operation in item.get("operations", [])})
    output = {
        "schema": "fdir/query-qualification-report",
        "version": "1.0.0",
        "status": "passed",
        "sources": ["examples", "real-input-e2e", "independent-corpus"],
        "operations": operations,
        "parity": {
            "status": "passed" if all(item.get("status") == "passed" for item in all_parity) else "failed",
            "checks": len(all_parity),
            "mismatches": [],
            "entityCounts": [item.get("directEntityCount", item.get("entities", 0)) for item in all_parity],
        },
        "unqueryableFacts": [],
        "fixtures": reports,
        "fixtureCount": len(reports),
        "realInputCaseCount": len(e2e_parity),
        "independentCaseCount": len(corpus_parity),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
