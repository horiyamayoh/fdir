"""Qualify the shared IR authority against real converted inputs.

The runner mutates IR produced from generated DOCX/XLSX/PDF/Markdown files;
it does not use hand-authored IR as its positive authority.  Schema-only
mutants are compared with the independent Draft 2020-12 validator, while
graph/status/extension rules are reported as runtime semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

try:
    from generate_e2e_fixtures import write_fixtures
    from convert_document import convert_path
    from ir_validation import validate_document, IRValidationError
    from authority_contract import validate_authority_artifacts, contract_digest
    from canonicalize_ir import canonical_value_bytes
except ImportError:  # pragma: no cover
    from tools.generate_e2e_fixtures import write_fixtures
    from tools.convert_document import convert_path
    from tools.ir_validation import validate_document, IRValidationError
    from tools.authority_contract import validate_authority_artifacts, contract_digest
    from tools.canonicalize_ir import canonical_value_bytes


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "machine" / "authority-negative-corpus.json"


class QualificationError(RuntimeError):
    pass


def _first(document: dict[str, Any], collection: str, predicate: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
    for item in document.get(collection, []) or []:
        if isinstance(item, dict) and (predicate is None or predicate(item)):
            return item
    return None


def _mutate(document: dict[str, Any], mutation: str) -> dict[str, Any]:
    mutant = copy.deepcopy(document)
    if mutation == "remove-root-node":
        mutant.pop("rootNodeId", None)
    elif mutation == "add-unknown-root-field":
        mutant["authorityLeak"] = True
    elif mutation == "duplicate-node-id":
        mutant["nodes"].append(copy.deepcopy(mutant["nodes"][1]))
    elif mutation == "replace-child-id":
        node = _first(mutant, "nodes", lambda item: bool(item.get("childIds")))
        if node is None:
            raise QualificationError("fixture has no node child edge for dangling-reference case")
        node["childIds"][0] = "node-does-not-exist"
    elif mutation == "remove-parent-child-reciprocity":
        node = _first(mutant, "nodes", lambda item: item.get("parentId") is not None)
        if node is None:
            raise QualificationError("fixture has no parent edge for reciprocity case")
        parent = _first(mutant, "nodes", lambda item: node["nodeId"] in item.get("childIds", []))
        if parent is None:
            raise QualificationError("fixture parent edge is already inconsistent")
        parent["childIds"].remove(node["nodeId"])
    elif mutation == "references-to-connector-target":
        relation = _first(mutant, "relations", lambda item: item.get("kind") == "usesResource")
        if relation is None:
            relation = _first(mutant, "relations")
        if relation is None:
            raise QualificationError("fixture has no relation")
        relation["kind"] = "connectorTarget"
    elif mutation == "row-member-becomes-cell":
        table = _first(mutant, "tables", lambda item: bool(item.get("rowIds")) and bool(item.get("cellIds")))
        if table is None:
            raise QualificationError("fixture has no table topology")
        table["rowIds"][0] = table["cellIds"][0]
    elif mutation == "duplicate-order-ordinal":
        order = _first(mutant, "orders", lambda item: len(item.get("items", [])) >= 2)
        if order is None:
            raise QualificationError("fixture has no multi-item order")
        order["items"][1]["ordinal"] = order["items"][0]["ordinal"]
    elif mutation == "numeric-wire-number":
        node = _first(mutant, "nodes", lambda item: isinstance(item.get("value"), dict) and item["value"].get("type") in {"integer", "number", "decimal"})
        if node is None:
            formula = _first(mutant, "formulas", lambda item: isinstance(item.get("values", {}).get("stored"), dict))
            if formula is None:
                raise QualificationError("fixture has no numeric typed value")
            formula["values"]["stored"]["value"] = 1
        else:
            node["value"]["value"] = 1
    elif mutation == "promote-partial-to-complete":
        mutant["conversion"]["status"] = "complete"
    elif mutation == "flip-extension-criticality":
        extension = _first(mutant, "extensions")
        if extension is None:
            raise QualificationError("fixture has no extension")
        extension["criticality"] = "critical"
    elif mutation == "change-source-map-format":
        source_map = _first(mutant, "sourceMaps")
        if source_map is None:
            raise QualificationError("fixture has no source map")
        source_map["format"]["name"] = "xlsx"
    else:
        raise QualificationError(f"unknown mutation {mutation}")
    return mutant


def _error_code(exc: Exception) -> str:
    return getattr(exc, "code", "").split(":", 1)[0] or str(exc).split(":", 1)[0]


def _schema_differential(document: dict[str, Any]) -> dict[str, Any]:
    import jsonschema
    schema = json.loads((ROOT / "schemas" / "document-form-ir.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    return {"valid": not errors, "errorCount": len(errors), "firstPath": list(errors[0].absolute_path) if errors else []}


def _canonical_vectors() -> dict[str, Any]:
    manifest = json.loads((ROOT / "machine" / "canonical-cross-language-vectors.json").read_text(encoding="utf-8"))
    python_results = []
    for vector in manifest.get("vectors", []):
        encoded = canonical_value_bytes(vector["value"])
        python_results.append({"id": vector["id"], "hex": encoded.hex(), "sha256": hashlib.sha256(encoded).hexdigest()})
    js_script = r'''
const crypto = require("crypto");
const vectors = JSON.parse(process.argv[1]);
function normalize(value, field) {
  if (Array.isArray(value)) {
    const rows = value.map(item => normalize(item, undefined));
    if (field === "nodes" && rows.every(item => item && typeof item.nodeId === "string")) rows.sort((a,b) => a.nodeId < b.nodeId ? -1 : a.nodeId > b.nodeId ? 1 : 0);
    return rows;
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = normalize(value[key], key);
    return out;
  }
  return value;
}
for (const vector of vectors) {
  const bytes = Buffer.from(JSON.stringify(normalize(vector.value, undefined)), "utf8");
  console.log(JSON.stringify({id: vector.id, hex: bytes.toString("hex"), sha256: crypto.createHash("sha256").update(bytes).digest("hex")}));
}
'''
    js_results: list[dict[str, Any]] = []
    try:
        result = subprocess.run(["node", "-e", js_script, json.dumps(manifest["vectors"], ensure_ascii=False)], capture_output=True, text=True, check=False, timeout=10)
        if result.returncode == 0:
            js_results = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        js_results = []
    expected = [{"id": item["id"], "hex": item["canonicalUtf8Hex"], "sha256": item["sha256"]} for item in manifest["vectors"]]
    return {"status": "passed" if python_results == expected and js_results == expected else "failed", "vectorCount": len(expected), "python": python_results, "javascript": js_results, "expected": expected}


def main() -> int:
    manifest = json.loads(CORPUS.read_text(encoding="utf-8"))
    authority = validate_authority_artifacts()
    vectors = _canonical_vectors()
    cases: list[dict[str, Any]] = []
    work_root = ROOT / "e2e" / ".run" / f"authority-qualification-{os.getpid()}"
    work_root.mkdir(parents=True, exist_ok=True)
    fixtures = write_fixtures(work_root)
    try:
        documents: dict[str, dict[str, Any]] = {}
        for format_name in ("docx", "xlsx", "pdf", "markdown"):
            document, evidence = convert_path(fixtures[format_name], format_name)
            if document.get("conversion", {}).get("status") == "failed":
                raise QualificationError(f"real {format_name} fixture failed conversion: {evidence}")
            validate_document(document)
            documents[format_name] = document
        for specification in manifest.get("cases", []):
            base = documents[specification["format"]]
            mutant = _mutate(base, specification["mutation"])
            schema_result = _schema_differential(mutant)
            try:
                validate_document(mutant)
            except Exception as exc:
                actual = _error_code(exc)
                passed = actual == specification["expectedCode"] or specification["expectedCode"] == "DFIR-IR-INVALID" and actual.startswith("DFIR-EXT")
                cases.append({"id": specification["id"], "format": specification["format"], "mutation": specification["mutation"], "status": "passed" if passed else "failed", "expectedCode": specification["expectedCode"], "actualCode": actual, "schemaDifferential": schema_result})
            else:
                cases.append({"id": specification["id"], "format": specification["format"], "mutation": specification["mutation"], "status": "failed", "expectedCode": specification["expectedCode"], "actualCode": None, "schemaDifferential": schema_result})
    finally:
        # The run directory is intentionally retained as an evidence anchor;
        # the repository's ignored-run policy can reap it outside the gate.
        pass
    passed = sum(item["status"] == "passed" for item in cases)
    report = {"schema":"fdir/authority-qualification-report","version":"1.0.0","status":"passed" if passed == len(cases) and vectors["status"] == "passed" else "failed","contractDigest":contract_digest(),"authority":authority,"realInputFormats":sorted(documents),"caseCount":len(cases),"passedCases":passed,"failedCases":len(cases)-passed,"schemaDifferential":"schema constraints are compared with jsonschema; graph semantics are runtime-only","canonicalVectors":vectors,"cases":cases}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
