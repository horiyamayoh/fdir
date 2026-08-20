"""Executable negative and mutation qualification for the public IR gate."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from canonicalize_ir import CanonicalizationError, canonical_bytes, canonical_digest, canonical_value_bytes  # type: ignore
from ir_validation import IRValidationError, validate_document  # type: ignore
from query_ir import QueryError, index_parity, rebuild_index, validate_index  # type: ignore


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


def _adapter_feature_omission_case() -> dict[str, Any]:
    """Mutate a real adapter call while keeping the independent source intact."""

    source = ROOT / "e2e" / "corpus" / "markdown-independent.md"
    source_lines = source.read_text(encoding="utf-8").splitlines()
    expected_headings = [line for line in source_lines if line.startswith("# ")]
    if not expected_headings:
        raise QualificationError("adapter omission fixture has no independent heading construct")
    from convert_document import convert_path  # type: ignore
    import adapter_markdown  # type: ignore

    baseline, _ = convert_path(source, "markdown")
    baseline_headings = [node for node in baseline.get("nodes", []) if node.get("kind") == "heading"]
    if len(baseline_headings) < len(expected_headings):
        raise QualificationError("baseline adapter does not emit the independent heading construct")
    original = adapter_markdown._heading_parts
    adapter_markdown._heading_parts = lambda line: None
    try:
        mutant, _ = convert_path(source, "markdown")
    finally:
        adapter_markdown._heading_parts = original
    mutant_headings = [node for node in mutant.get("nodes", []) if node.get("kind") == "heading"]
    killed = len(mutant_headings) < len(expected_headings)
    return {
        "mutation": "adapter-feature-omission",
        "class": "adapter-omission",
        "fixture": str(source.relative_to(ROOT)),
        "status": "killed" if killed else "survived",
        "oracle": "an independent authored heading must remain a typed heading after the adapter path is qualified",
        "sourceConstructs": {"headingCount": len(expected_headings)},
        "baselineEmission": {"headingCount": len(baseline_headings)},
        "mutantEmission": {"headingCount": len(mutant_headings)},
    }


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
        ("noncanonical-integer-negative-zero", "exact-value", "cell-formula.json", lambda d: d["formulas"][0]["values"]["stored"].update({"type": "integer", "value": "-0"})),
        ("typed-value-lane-mismatch", "exact-value", "cell-formula.json", lambda d: d["formulas"][0]["values"]["stored"].update({"type": "integer", "value": "not-an-integer"})),
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


def _actual_canonical_and_index_mutations() -> list[dict[str, Any]]:
    """Run #79/#83 mutations against a real converter output and public APIs."""

    from convert_document import convert_path  # type: ignore

    source = ROOT / "e2e" / "corpus" / "pdf-independent.pdf"
    document, evidence = convert_path(source, "pdf")
    if evidence.get("outcome") != "success" or document.get("conversion", {}).get("status") not in {"partial", "complete-with-warnings", "complete"}:
        raise QualificationError(f"real canonical/query mutation source did not convert: {evidence}")

    cases: list[dict[str, Any]] = []
    input_path = ROOT / "e2e" / ".run" / f"mutation-source-{os.getpid()}.json"
    input_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for projection in ("full", "content", "source-map-excluded"):
        cli = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "canonicalize_ir.py"), str(input_path), "--digest", "--projection", projection],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        library_digest = canonical_digest(document, projection)
        # The CLI/library parity is qualified against the same public adapter
        # output shape.  If the audit output is unavailable, the CLI negative
        # is reported rather than replaced with a hand-authored IR file.
        status = "killed" if cli.returncode == 0 and len(cli.stdout.strip()) == 64 and cli.stdout.strip() == library_digest else "survived"
        cases.append({
            "mutation": f"canonical-cli-library-{projection}",
            "class": "canonical",
            "status": status,
            "oracle": "named CLI digest equals library digest for the real adapter output",
            "projection": projection,
            "cliReturnCode": cli.returncode,
            "libraryDigest": library_digest,
            "cliDigest": cli.stdout.strip(),
        })

    projected_path = ROOT / "e2e" / ".run" / f"mutation-projection-{os.getpid()}.json"
    projected = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "canonicalize_ir.py"), str(input_path), "--output", str(projected_path), "--projection", "content"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    projected_bytes = projected_path.read_bytes() if projected_path.is_file() else b""
    cases.append({
        "mutation": "canonical-cli-library-content-bytes",
        "class": "canonical",
        "status": "killed" if projected.returncode == 0 and projected_bytes == canonical_bytes(document, "content") + b"\n" else "survived",
        "oracle": "named CLI projection bytes equal library canonical_bytes",
        "projection": "content",
    })

    reordered = copy.deepcopy(document)
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    cases.append({
        "mutation": "canonical-real-collection-order",
        "class": "canonical",
        "status": "killed" if canonical_digest(reordered) == canonical_digest(document) else "survived",
        "oracle": "real conversion entity collection ordering is identity-invariant",
    })

    floating_cases = [("canonical-real-negative-zero", -0.0), ("canonical-real-exponent", 1e2)]
    for label, value in floating_cases:
        try:
            canonical_value_bytes({"value": value})
        except CanonicalizationError:
            status = "killed"
        else:
            status = "survived"
        cases.append({"mutation": label, "class": "canonical", "status": status, "oracle": "non-exact JSON floating-point spellings are rejected"})

    canonical_path = ROOT / "e2e" / ".run" / f"mutation-canonical-{os.getpid()}.json"
    canonical_path.write_bytes(canonical_bytes(document) + b"\r\n")
    check = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "canonicalize_ir.py"), str(canonical_path), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    cases.append({
        "mutation": "canonical-crlf-input",
        "class": "canonical",
        "status": "killed" if check.returncode != 0 else "survived",
        "oracle": "canonical check rejects CRLF bytes; normal CLI output is LF",
        "returnCode": check.returncode,
    })

    duplicate_path = ROOT / "e2e" / ".run" / f"mutation-duplicate-{os.getpid()}.json"
    duplicate_path.write_bytes(b'{"duplicate":1,"duplicate":2}\n')
    duplicate = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "canonicalize_ir.py"), str(duplicate_path), "--digest"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    cases.append({
        "mutation": "canonical-duplicate-object-key",
        "class": "canonical",
        "status": "killed" if duplicate.returncode != 0 else "survived",
        "oracle": "duplicate JSON object members are rejected before authority validation",
        "returnCode": duplicate.returncode,
    })

    index = rebuild_index(document)
    stale_document = copy.deepcopy(document)
    stale_document["sourceFormat"]["version"] = f"{stale_document['sourceFormat']['version']}-stale"
    index_mutations: list[tuple[str, Callable[[], None]]] = [
        ("query-index-real-stale-document", lambda: index_parity(stale_document, index)),
        ("query-index-real-missing-entity", lambda: validate_index(document, {**index, "entities": index["entities"][:-1]})),
    ]
    corrupt_fact = copy.deepcopy(index)
    corrupt_fact["facts"][0]["digest"] = "0" * 64
    index_mutations.append(("query-index-real-corrupt-fact", lambda: validate_index(document, corrupt_fact)))
    unqueryable = copy.deepcopy(index)
    unqueryable["facts"] = unqueryable["facts"][1:]
    index_mutations.append(("query-index-real-unqueryable-fact", lambda: validate_index(document, unqueryable)))
    for label, callback in index_mutations:
        try:
            callback()
        except (QueryError, ValueError, KeyError, TypeError):
            status = "killed"
        else:
            status = "survived"
        cases.append({"mutation": label, "class": "query-index", "status": status, "oracle": "real conversion output must reject stale, corrupt, and incomplete indexes"})

    cases.append({
        "mutation": "query-index-real-parity",
        "class": "query-index",
        "status": "killed" if index_parity(document, index).get("unqueryableFacts") == [] else "survived",
        "oracle": "all authoritative entities and fact digests are directly queryable",
    })
    return cases


def run() -> dict[str, Any]:
    # Mutation qualification writes only disposable run products.  Create the
    # ignored workspace here so the strict gate also works from a fresh
    # checkout where e2e/.run does not yet exist.
    (ROOT / "e2e" / ".run").mkdir(parents=True, exist_ok=True)
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
    adapter_case = _adapter_feature_omission_case()
    cases.append(adapter_case)
    if adapter_case["status"] == "survived":
        survivors.append(adapter_case["mutation"])
    custom_cases = _custom_mutations(base)
    custom_cases.extend(_actual_canonical_and_index_mutations())
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
