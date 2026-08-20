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

from ir_validation import IRValidationError, validate_document  # type: ignore


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


def _mutations() -> list[tuple[str, Callable[[dict[str, Any]], None]]]:
    return [
        ("required-root-field", lambda d: d.pop("rootNodeId")),
        ("node-forbidden-field", lambda d: d["nodes"][0].update({"address": {"row": 1, "column": 1}})),
        ("dangling-child", lambda d: d["nodes"][0]["childIds"].append("node-does-not-exist")),
        ("reciprocity", lambda d: d["nodes"][1].pop("parentId", None) if len(d["nodes"]) > 1 else d["nodes"][0].update({"parentId": "missing"})),
        ("containment-cycle", lambda d: d["nodes"][0]["childIds"].append(d["nodes"][0]["nodeId"])),
        ("duplicate-order-ordinal", lambda d: d["orders"][0]["items"].append(copy.deepcopy(d["orders"][0]["items"][0])) if d.get("orders") else d.update({"orders": [{"orderId": "order-mutation", "kind": "source", "ownerId": d["rootNodeId"], "items": [{"id": d["rootNodeId"], "ordinal": 0}, {"id": d["rootNodeId"], "ordinal": 0}], "status": "preserved"}]})),
        ("unknown-critical-extension", lambda d: d.setdefault("extensions", []).append({"extensionId": "extension-mutation-critical", "targetId": d["rootNodeId"], "namespace": "urn:unknown:", "type": "critical", "schemaVersion": "9.9.9", "schemaId": "urn:unknown:schema", "payload": {}, "criticality": "critical"})),
        ("unknown-noncritical-complete", lambda d: d.setdefault("extensions", []).append({"extensionId": "extension-mutation-opaque", "targetId": d["rootNodeId"], "namespace": "urn:unknown:", "type": "opaque", "schemaVersion": "9.9.9", "schemaId": "urn:unknown:schema", "payload": {}, "criticality": "non-critical"})),
        ("noncanonical-decimal", lambda d: next(g for g in d.get("geometries", []) if g.get("primitives"))["primitives"][0].update({"x": "1e2"}) if any(g.get("primitives") for g in d.get("geometries", [])) else d.update({"coordinateSpaces": [{"coordinateSpaceId": "space-mutation", "unit": "pt", "origin": {"x": "1e2", "y": "0"}}]})),
        ("unsupported-in-complete", lambda d: d["nodes"][0].update({"status": "unsupported"})),
    ]


def run() -> dict[str, Any]:
    positive = ["callout.json", "cell-formula.json", "markdown-authoring.json", "style-resolution.json"]
    for name in positive:
        validate_document(_load(name))
    base = _load("callout.json")
    cases: list[dict[str, Any]] = []
    survivors: list[str] = []
    for label, mutate in _mutations():
        mutant = copy.deepcopy(base)
        try:
            mutate(mutant)
            outcome = _must_reject(mutant, label)
        except QualificationError as exc:
            outcome = "survived"
            survivors.append(label)
            cases.append({"mutation": label, "status": outcome, "diagnostic": str(exc)})
            continue
        cases.append({"mutation": label, "status": outcome})
    killed = sum(case["status"] == "killed" for case in cases)
    total = len(cases)
    return {
        "schema": "fdir/mutation-qualification-report",
        "version": "1.0.0",
        "status": "passed" if not survivors else "failed",
        "mutationScore": round(killed / total, 4) if total else 1.0,
        "killed": killed,
        "total": total,
        "survivors": survivors,
        "cases": cases,
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
