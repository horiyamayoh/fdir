"""Run the hand-authored, non-generated fidelity corpus through public APIs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from typing import Any

try:
    from ir_validation import validate_document
except ImportError:  # pragma: no cover
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "e2e" / "corpus"
MANIFEST = CORPUS / "manifest.json"
CONVERTER = ROOT / "tools" / "convert_document.py"
CANONICALIZER = ROOT / "tools" / "canonicalize_ir.py"


class CorpusFailure(RuntimeError):
    pass


def _all_strings(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_all_strings(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_all_strings(child) for child in value)
    return str(value)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)


def _package(case: dict[str, Any], workspace: Path) -> Path:
    source = CORPUS / str(case["path"])
    if case.get("kind") == "file":
        if not source.is_file():
            raise CorpusFailure(f"corpus source is missing: {source}")
        destination = workspace / source.name
        shutil.copyfile(source, destination)
        return destination
    if case.get("kind") != "ooxml-parts" or not source.is_dir():
        raise CorpusFailure(f"invalid corpus case source: {case.get('id')}")
    destination = workspace / f"{case['id']}.{case['format']}"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part in sorted(item for item in source.rglob("*") if item.is_file()):
            archive.write(part, part.relative_to(source).as_posix())
    return destination


def _convert(case: dict[str, Any], input_path: Path, workspace: Path) -> dict[str, Any]:
    output = workspace / f"{case['id']}.json"
    evidence = workspace / f"{case['id']}.evidence.json"
    result = _run([str(CONVERTER), "convert", str(input_path), "--format", case["format"], "--out", str(output), "--evidence", str(evidence)])
    if result.returncode != 0:
        raise CorpusFailure(f"{case['id']} conversion failed: {result.stdout}; {result.stderr}")
    document = json.loads(output.read_text(encoding="utf-8"))
    warnings = validate_document(document)
    if document.get("conversion", {}).get("status") == "failed":
        raise CorpusFailure(f"{case['id']} converted as failed")
    source_text = _all_strings(document)
    missing = [token for token in case.get("expected", []) if token not in source_text]
    if missing:
        raise CorpusFailure(f"{case['id']} lost source-derived tokens: {missing}")
    canonical = _run([str(CANONICALIZER), str(output), "--digest"])
    digest = canonical.stdout.strip()
    if canonical.returncode != 0 or len(digest) != 64:
        raise CorpusFailure(f"{case['id']} canonical digest failed: {canonical.stdout}; {canonical.stderr}")
    evidence_value = json.loads(evidence.read_text(encoding="utf-8"))
    if evidence_value.get("input", {}).get("consumed") is not True:
        raise CorpusFailure(f"{case['id']} evidence does not prove input consumption")
    return {
        "id": case["id"],
        "format": case["format"],
        "status": document["conversion"]["status"],
        "documentId": document["documentId"],
        "canonicalDigest": digest,
        "nodes": len(document.get("nodes", [])),
        "diagnostics": len(document.get("diagnostics", [])),
        "warnings": warnings,
    }


def _check_resource_limit(case: dict[str, Any], input_path: Path, workspace: Path) -> dict[str, Any]:
    output = workspace / f"{case['id']}-limited.json"
    evidence = workspace / f"{case['id']}-limited.evidence.json"
    result = _run([str(CONVERTER), "convert", str(input_path), "--format", case["format"], "--out", str(output), "--evidence", str(evidence), "--max-input-bytes", "1"])
    if result.returncode == 0:
        raise CorpusFailure("resource-limit negative case unexpectedly succeeded")
    document = json.loads(output.read_text(encoding="utf-8"))
    validate_document(document)
    if document.get("conversion", {}).get("status") != "failed":
        raise CorpusFailure("resource-limit negative case did not fail closed")
    return {"id": "resource-limit", "status": document["conversion"]["status"], "diagnostics": len(document.get("diagnostics", []))}


def run() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("independent") is not True:
        raise CorpusFailure("independent corpus manifest is not marked independent")
    workspace = ROOT / "e2e" / ".run" / f"independent-{os.getpid()}"
    workspace.mkdir(parents=True, exist_ok=True)
    cases = []
    packaged: dict[str, Path] = {}
    for case in manifest.get("cases", []):
        packaged[case["id"]] = _package(case, workspace)
        cases.append(_convert(case, packaged[case["id"]], workspace))
    first_case = manifest["cases"][0]
    negative = _check_resource_limit(first_case, packaged[first_case["id"]], workspace)
    return {
        "schema": "fdir/independent-fidelity-corpus-report",
        "version": "1.0.0",
        "status": "passed",
        "independent": True,
        "cases": cases,
        "negativeChecks": [negative],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run()
    except Exception as exc:
        report = {"schema": "fdir/independent-fidelity-corpus-report", "version": "1.0.0", "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
