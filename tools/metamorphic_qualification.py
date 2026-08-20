"""Metamorphic, repeatability, and hostile-input qualification.

This runner complements the independent source oracle.  It checks properties
that must remain true when a source representation changes without changing
the authored document-form facts, and it checks that unsafe package members
and resource-limit inputs fail closed.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import zipfile
from typing import Any

try:
    from adapter_common import AdapterLimits
    from canonicalize_ir import canonical_digest
    from convert_document import convert_path
    from ir_validation import COLLECTION_KEYS, validate_document
except ImportError:  # pragma: no cover
    from tools.adapter_common import AdapterLimits
    from tools.canonicalize_ir import canonical_digest
    from tools.convert_document import convert_path
    from tools.ir_validation import COLLECTION_KEYS, validate_document


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "e2e" / "corpus"
MANIFEST = CORPUS / "manifest.json"


class MetamorphicFailure(AssertionError):
    pass


def _package(case: dict[str, Any], workspace: Path, *, reverse: bool = False, compression: int = zipfile.ZIP_DEFLATED) -> Path:
    source = CORPUS / str(case["path"])
    if case.get("kind") == "file":
        destination = workspace / (f"{case['id']}-variant.{case['format']}" if case["format"] != "markdown" else f"{case['id']}-variant.md")
        shutil.copyfile(source, destination)
        if case["format"] == "markdown":
            text = destination.read_text(encoding="utf-8")
            destination.write_text(text.replace("\r\n", "\n").replace("\n", "\r\n"), encoding="utf-8", newline="")
        return destination
    destination = workspace / f"{case['id']}-variant.{case['format']}"
    names = sorted(item for item in source.rglob("*") if item.is_file())
    if reverse:
        names.reverse()
    with zipfile.ZipFile(destination, "w", compression=compression) as archive:
        for part in names:
            archive.write(part, part.relative_to(source).as_posix())
    return destination


def _convert(path: Path, format_name: str) -> dict[str, Any]:
    document, evidence = convert_path(path, format_name)
    validate_document(document)
    if document.get("conversion", {}).get("status") == "failed":
        raise MetamorphicFailure(f"valid metamorphic input failed: {path.name}")
    return {"document": document, "evidence": evidence, "digest": canonical_digest(document)}


def _entity_order_variant(document: dict[str, Any]) -> dict[str, Any]:
    variant = copy.deepcopy(document)
    for collection in COLLECTION_KEYS:
        values = variant.get(collection)
        if isinstance(values, list):
            values.reverse()
    validate_document(variant)
    return variant


def _hostile_zip(case: dict[str, Any], workspace: Path) -> Path:
    source = CORPUS / str(case["path"])
    if not source.is_dir():
        raise MetamorphicFailure("hostile package requires an OOXML directory case")
    destination = workspace / f"{case['id']}-traversal.{case['format']}"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part in sorted(item for item in source.rglob("*") if item.is_file()):
            archive.write(part, part.relative_to(source).as_posix())
        archive.writestr("../escape.xml", b"not allowed")
    return destination


def run() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    positive = list(manifest.get("cases", []))
    workspace = ROOT / "e2e" / ".run" / f"metamorphic-{os.getpid()}"
    workspace.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for case in positive:
        original_path = _package(case, workspace, reverse=False)
        variant_path = _package(case, workspace, reverse=True, compression=zipfile.ZIP_STORED)
        original = _convert(original_path, str(case["format"]))
        variant = _convert(variant_path, str(case["format"]))
        if original["digest"] != variant["digest"]:
            raise MetamorphicFailure(f"source-representation digest drift: {case['id']}")
        repeated = _convert(original_path, str(case["format"]))
        if original["digest"] != repeated["digest"]:
            raise MetamorphicFailure(f"repeat conversion digest drift: {case['id']}")
        reordered = _entity_order_variant(original["document"])
        if canonical_digest(reordered) != original["digest"]:
            raise MetamorphicFailure(f"entity collection order changed identity: {case['id']}")
        cases.append({
            "id": case["id"],
            "format": case["format"],
            "status": "passed",
            "properties": ["container-order-and-compression-invariance", "repeatability", "entity-array-order-invariance"],
            "originalDigest": original["digest"],
            "variantDigest": variant["digest"],
            "repeatDigest": repeated["digest"],
            "sourceDigests": [original["evidence"].get("input", {}).get("sha256"), variant["evidence"].get("input", {}).get("sha256")],
        })

    hostile: list[dict[str, Any]] = []
    for case in positive:
        if case.get("kind") != "ooxml-parts":
            continue
        path = _hostile_zip(case, workspace)
        document, evidence = convert_path(path, str(case["format"]))
        validate_document(document)
        diagnostics = " ".join(str(item) for item in document.get("diagnostics", []))
        passed = document.get("conversion", {}).get("status") == "failed" and "unsafe ZIP member path" in diagnostics
        if not passed:
            raise MetamorphicFailure(f"ZIP traversal input was not rejected: {case['id']}")
        hostile.append({"id": case["id"], "format": case["format"], "case": "zip-path-traversal", "status": "killed", "diagnostics": len(document.get("diagnostics", []))})

    first = positive[0]
    resource_path = _package(first, workspace, reverse=False)
    limited, _ = convert_path(resource_path, str(first["format"]), AdapterLimits(max_input_bytes=1))
    validate_document(limited)
    if limited.get("conversion", {}).get("status") != "failed":
        raise MetamorphicFailure("resource limit survived metamorphic qualification")
    hostile.append({"id": "resource-limit", "format": first["format"], "case": "max-input-bytes", "status": "killed"})
    return {
        "schema": "fdir/metamorphic-qualification-report",
        "version": "1.0.0",
        "status": "passed",
        "source": "independent fidelity corpus",
        "cases": cases,
        "hostileCases": hostile,
        "survivors": [],
        "differential": {"status": "passed", "mismatches": [], "compared": len(cases)},
    }


def main() -> int:
    try:
        report = run()
    except Exception as exc:
        report = {"schema": "fdir/metamorphic-qualification-report", "version": "1.0.0", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "survivors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
