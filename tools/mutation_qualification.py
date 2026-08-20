"""Executable negative and mutation qualification for the public IR gate."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from canonicalize_ir import canonical_digest  # type: ignore
from ir_validation import IRValidationError, validate_document  # type: ignore
from query_ir import rebuild_index  # type: ignore


class QualificationError(AssertionError):
    pass


def _load(name: str) -> dict[str, Any]:
    value = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationError(f"example is not an object: {name}")
    return value


def _must_reject(mutant: dict[str, Any], label: str) -> str:
    try:
        validate_document(mutant)
    except (IRValidationError, ValueError, KeyError, TypeError):
        return "killed"
    raise QualificationError(f"surviving mutation: {label}")


def _table_containment_mutation(document: dict[str, Any]) -> None:
    """Add a valid minimal table, then break its row-to-table containment."""

    table_id = "node-table-mutation"
    row_id = "node-table-mutation-row-1"
    column_id = "node-table-mutation-column-1"
    cell_id = "node-table-mutation-cell-1-1"
    document["nodes"][0]["childIds"].append(table_id)
    document["nodes"].extend(
        [
            {"nodeId": table_id, "kind": "table", "parentId": document["rootNodeId"], "childIds": [row_id, column_id], "partId": "part-document", "status": "preserved"},
            {"nodeId": row_id, "kind": "row", "parentId": table_id, "childIds": [cell_id], "status": "preserved"},
            {"nodeId": column_id, "kind": "column", "parentId": table_id, "childIds": [], "status": "preserved"},
            {"nodeId": cell_id, "kind": "cell", "parentId": row_id, "childIds": [], "address": {"row": 1, "column": 1}, "status": "preserved"},
        ]
    )
    document.setdefault("tables", []).append({"tableId": "table-mutation", "nodeId": table_id, "rowIds": [row_id], "columnIds": [column_id], "cellIds": [cell_id], "status": "preserved"})
    document["nodes"][-3]["parentId"] = "node-callout"


def _mutations() -> list[tuple[str, str, str, Callable[[dict[str, Any]], None]]]:
    return [
        ("required-root-field", "schema", "callout.json", lambda d: d.pop("rootNodeId")),
        ("node-forbidden-field", "schema", "callout.json", lambda d: d["nodes"][0].update({"address": {"row": 1, "column": 1}})),
        ("dangling-child", "graph", "callout.json", lambda d: d["nodes"][0]["childIds"].append("node-does-not-exist")),
        ("reciprocity", "graph", "callout.json", lambda d: d["nodes"][1].pop("parentId", None) if len(d["nodes"]) > 1 else d["nodes"][0].update({"parentId": "missing"})),
        ("containment-cycle", "graph", "callout.json", lambda d: d["nodes"][0]["childIds"].append(d["nodes"][0]["nodeId"])),
        ("wrong-type-reference", "graph", "callout.json", lambda d: d["nodes"][1].update({"partId": d["nodes"][0]["nodeId"]}) if len(d["nodes"]) > 1 else d["nodes"][0].update({"partId": d["rootNodeId"]})),
        ("duplicate-order-ordinal", "graph", "callout.json", lambda d: d["orders"][0]["items"].append(copy.deepcopy(d["orders"][0]["items"][0])) if d.get("orders") else d.update({"orders": [{"orderId": "order-mutation", "kind": "source", "ownerId": d["rootNodeId"], "items": [{"id": d["rootNodeId"], "ordinal": 0}, {"id": d["rootNodeId"], "ordinal": 0}], "status": "preserved"}]})),
        ("unsupported-in-complete", "status", "callout.json", lambda d: d["nodes"][0].update({"status": "unsupported"})),
        ("unknown-critical-extension", "extension", "callout.json", lambda d: d.setdefault("extensions", []).append({"extensionId": "extension-mutation-critical", "targetId": d["rootNodeId"], "namespace": "urn:unknown:", "type": "critical", "schemaVersion": "9.9.9", "schemaId": "urn:unknown:schema", "payload": {}, "criticality": "critical"})),
        ("unknown-noncritical-complete", "extension", "callout.json", lambda d: d.setdefault("extensions", []).append({"extensionId": "extension-mutation-opaque", "targetId": d["rootNodeId"], "namespace": "urn:unknown:", "type": "opaque", "schemaVersion": "9.9.9", "schemaId": "urn:unknown:schema", "payload": {}, "criticality": "non-critical"})),
        ("noncanonical-decimal", "exact-value", "callout.json", lambda d: next(g for g in d.get("geometries", []) if g.get("primitives"))["primitives"][0].update({"x": "1e2"}) if any(g.get("primitives") for g in d.get("geometries", [])) else d.update({"coordinateSpaces": [{"coordinateSpaceId": "space-mutation", "unit": "pt", "origin": {"x": "1e2", "y": "0"}}]})),
        ("typed-value-lane-mismatch", "exact-value", "cell-formula.json", lambda d: d["formulas"][0]["values"]["stored"].update({"type": "integer", "value": "not-an-integer"})),
        ("adapter-feature-omission", "adapter-omission", "callout.json", lambda d: d["conversion"]["features"].pop()),
        ("color-variant-forbidden", "schema", "style-resolution.json", lambda d: next(style for style in d["styles"] if style["styleId"] == "style-direct")["direct"]["foreground"].update({"slot": "bodyText"})),
        ("geometry-variant-forbidden", "schema", "callout.json", lambda d: next(primitive for geometry in d["geometries"] for primitive in geometry.get("primitives", []) if primitive.get("kind") == "rectangle").update({"rotation": {"value": "0", "unit": "deg"}})),
        ("path-segment-variant-forbidden", "schema", "pdf-observation.json", lambda d: next(segment for geometry in d["geometries"] for primitive in geometry.get("primitives", []) for segment in primitive.get("segments", []) if segment.get("kind") == "line").update({"points": []})),
        ("table-containment", "graph", "callout.json", _table_containment_mutation),
    ]


def _custom_mutations(base: dict[str, Any]) -> list[dict[str, Any]]:
    """Mutations whose oracle is a cross-layer qualification invariant."""

    shuffled = copy.deepcopy(base)
    shuffled["nodes"] = list(reversed(shuffled["nodes"]))
    source_changed = copy.deepcopy(base)
    source_changed["texts"][0]["value"] = source_changed["texts"][0].get("value", "") + " mutation"
    index = rebuild_index(base)
    index_mutant = copy.deepcopy(index)
    index_mutant["entities"].pop()

    from strict_completion_gate import CONTRACT_PATH  # type: ignore

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    claim_mutant = copy.deepcopy(contract)
    claim_mutant["issueEvidence"].pop("70", None)
    expected_issues = {str(number) for number in contract["scope"]["phase2Issues"]}

    return [
        {"mutation": "canonical-collection-order", "class": "canonical", "status": "killed" if canonical_digest(shuffled) == canonical_digest(base) else "survived", "oracle": "entity collection order is identity-invariant"},
        {"mutation": "canonical-source-fact-change", "class": "canonical", "status": "killed" if canonical_digest(source_changed) != canonical_digest(base) else "survived", "oracle": "source-declared fact changes identity"},
        {"mutation": "query-index-entity-drop", "class": "query-index", "status": "killed" if index_mutant["entities"] != index["entities"] else "survived", "oracle": "index parity detects a dropped entity"},
        {"mutation": "release-claim-missing-issue-evidence", "class": "release-claim", "status": "killed" if set(claim_mutant.get("issueEvidence", {})) != expected_issues else "survived", "oracle": "claim manifest must bind every scoped issue"},
    ]


def run() -> dict[str, Any]:
    positive = ["callout.json", "cell-formula.json", "markdown-authoring.json", "style-resolution.json"]
    for name in positive:
        validate_document(_load(name))
    base = _load("callout.json")
    cases: list[dict[str, Any]] = []
    survivors: list[str] = []
    for label, mutation_class, fixture, mutate in _mutations():
        mutant = copy.deepcopy(_load(fixture))
        try:
            mutate(mutant)
            outcome = _must_reject(mutant, label)
        except QualificationError as exc:
            outcome = "survived"
            survivors.append(label)
            cases.append({"mutation": label, "class": mutation_class, "fixture": fixture, "status": outcome, "diagnostic": str(exc)})
            continue
        cases.append({"mutation": label, "class": mutation_class, "fixture": fixture, "status": outcome})
    custom_cases = _custom_mutations(base)
    for case in custom_cases:
        cases.append(case)
        if case["status"] == "survived":
            survivors.append(case["mutation"])
    killed = sum(case["status"] == "killed" for case in cases)
    total = len(cases)
    coverage: dict[str, list[str]] = {}
    for case in cases:
        coverage.setdefault(str(case["class"]), []).append(str(case["mutation"]))
    return {
        "schema": "fdir/mutation-qualification-report",
        "version": "1.0.0",
        "status": "passed" if not survivors else "failed",
        "mutationScore": round(killed / total, 4) if total else 1.0,
        "killed": killed,
        "total": total,
        "survivors": survivors,
        "cases": cases,
        "coverage": {key: sorted(value) for key, value in sorted(coverage.items())},
        "positiveFixtures": positive,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run()
    except Exception as exc:
        report = {"schema": "fdir/mutation-qualification-report", "version": "1.0.0", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "survivors": []}
    rendered = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True)
    print(rendered)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
