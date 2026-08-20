"""Run the real-input FDIR adapter end-to-end qualification.

The runner deliberately invokes the public ``convert_document.py`` process
for every case.  A hand-authored IR fixture, a missing adapter, or an adapter
that ignores its input cannot satisfy the evidence and source-derived content
checks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from generate_e2e_fixtures import write_fixtures
    from ir_validation import validate_document
except ImportError:  # pragma: no cover
    from tools.generate_e2e_fixtures import write_fixtures
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "tools" / "convert_document.py"
CANONICALIZER = ROOT / "tools" / "canonicalize_ir.py"
QUERY = ROOT / "tools" / "query_ir.py"
FORMATS = ("docx", "xlsx", "pdf", "markdown")


class E2EFailure(RuntimeError):
    pass


def run_command(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise E2EFailure(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise E2EFailure(f"JSON root is not an object: {path}")
    return value


def all_strings(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {all_strings(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(all_strings(child) for child in value)
    return str(value)


def check_case(format_name: str, input_path: Path, work: Path, expected: tuple[str, ...]) -> dict[str, Any]:
    inspect_result = run_command(
        [str(CONVERTER), "inspect", str(input_path), "--format", format_name],
        cwd=ROOT,
    )
    if inspect_result.returncode != 0:
        raise E2EFailure(f"{format_name} inspect failed: rc={inspect_result.returncode}; stdout={inspect_result.stdout}; stderr={inspect_result.stderr}")
    try:
        inspect_report = json.loads(inspect_result.stdout)
    except json.JSONDecodeError as exc:
        raise E2EFailure(f"{format_name} inspect did not return JSON: {inspect_result.stdout}") from exc
    if inspect_report.get("format") != format_name:
        raise E2EFailure(f"{format_name} inspect format mismatch")
    if inspect_report.get("bytes") != input_path.stat().st_size:
        raise E2EFailure(f"{format_name} inspect byte count mismatch")

    output = work / f"{format_name}.json"
    evidence_path = work / f"{format_name}.evidence.json"
    result = run_command(
        [str(CONVERTER), "convert", str(input_path), "--format", format_name, "--out", str(output), "--evidence", str(evidence_path)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise E2EFailure(f"{format_name} valid input failed: rc={result.returncode}; stdout={result.stdout}; stderr={result.stderr}")
    document = load_json(output)
    warnings = validate_document(document)
    evidence = load_json(evidence_path)
    if evidence.get("input", {}).get("consumed") is not True:
        raise E2EFailure(f"{format_name} evidence does not prove input consumption")
    if evidence.get("input", {}).get("format") != format_name:
        raise E2EFailure(f"{format_name} evidence format mismatch")
    if evidence.get("adapter", {}).get("operation") != "convert":
        raise E2EFailure(f"{format_name} did not execute convert operation")
    if evidence.get("documentId") != document.get("documentId"):
        raise E2EFailure(f"{format_name} evidence/document identity mismatch")
    if document.get("conversion", {}).get("status") == "failed":
        raise E2EFailure(f"{format_name} valid input converted as failed")

    validation = run_command([str(CONVERTER), "validate", str(output)], cwd=ROOT)
    if validation.returncode != 0:
        raise E2EFailure(f"{format_name} public validate failed: {validation.stdout}; {validation.stderr}")
    try:
        validation_report = json.loads(validation.stdout)
    except json.JSONDecodeError as exc:
        raise E2EFailure(f"{format_name} public validate did not return JSON") from exc
    if validation_report.get("status") != "valid":
        raise E2EFailure(f"{format_name} public validate did not report valid")

    digest = run_command([str(CANONICALIZER), str(output), "--digest"], cwd=ROOT)
    canonical_digest = digest.stdout.strip()
    if digest.returncode != 0 or len(canonical_digest) != 64 or any(character not in "0123456789abcdef" for character in canonical_digest):
        raise E2EFailure(f"{format_name} canonicalization failed: {digest.stdout}; {digest.stderr}")

    query_kind = {"docx": "paragraph", "xlsx": "cell", "pdf": "glyph", "markdown": "paragraph"}[format_name]
    query = run_command([str(QUERY), str(output), "list-nodes", "--kind", query_kind], cwd=ROOT)
    if query.returncode != 0:
        raise E2EFailure(f"{format_name} query failed: {query.stdout}; {query.stderr}")
    try:
        query_nodes = json.loads(query.stdout)
    except json.JSONDecodeError as exc:
        raise E2EFailure(f"{format_name} query did not return JSON") from exc
    if not isinstance(query_nodes, list) or not query_nodes:
        raise E2EFailure(f"{format_name} query returned no {query_kind} nodes")

    strings = all_strings(document)
    for token in expected:
        if token not in strings:
            raise E2EFailure(f"{format_name} output lacks source-derived token {token!r}")
    if len(document.get("nodes", [])) < 2:
        raise E2EFailure(f"{format_name} output has no parsed child nodes")
    return {
        "format": format_name,
        "input": str(input_path),
        "inputBytes": evidence["input"].get("bytes"),
        "inputSha256": evidence["input"].get("sha256"),
        "inspect": {"format": inspect_report.get("format"), "bytes": inspect_report.get("bytes")},
        "validation": validation_report,
        "canonicalDigest": canonical_digest,
        "query": {"kind": query_kind, "count": len(query_nodes)},
        "output": str(output),
        "evidence": str(evidence_path),
        "conversionStatus": document["conversion"]["status"],
        "nodes": len(document.get("nodes", [])),
        "texts": len(document.get("texts", [])),
        "diagnostics": len(document.get("diagnostics", [])),
        "warnings": warnings,
    }


def check_malformed(format_name: str, input_path: Path, work: Path) -> dict[str, Any]:
    output = work / f"malformed-{format_name}.json"
    evidence_path = work / f"malformed-{format_name}.evidence.json"
    result = run_command(
        [str(CONVERTER), "convert", str(input_path), "--format", format_name, "--out", str(output), "--evidence", str(evidence_path)],
        cwd=ROOT,
    )
    if result.returncode == 0:
        raise E2EFailure(f"{format_name} malformed input unexpectedly succeeded")
    document = load_json(output)
    validate_document(document)
    evidence = load_json(evidence_path)
    if evidence.get("input", {}).get("consumed") is not True:
        raise E2EFailure(f"{format_name} malformed evidence does not prove input consumption")
    if document.get("conversion", {}).get("status") not in {"failed", "partial"}:
        raise E2EFailure(f"{format_name} malformed input was not marked failed/partial")
    if not document.get("diagnostics"):
        raise E2EFailure(f"{format_name} malformed input has no diagnostic")
    return {
        "format": format_name,
        "input": str(input_path),
        "case": "malformed",
        "commandExitCode": result.returncode,
        "conversionStatus": document["conversion"]["status"],
        "diagnostics": [item.get("code") for item in document.get("diagnostics", [])],
    }


def check_resource_limit(format_name: str, input_path: Path, work: Path) -> dict[str, Any]:
    output = work / f"limited-{format_name}.json"
    evidence_path = work / f"limited-{format_name}.evidence.json"
    result = run_command(
        [str(CONVERTER), "convert", str(input_path), "--format", format_name, "--out", str(output), "--evidence", str(evidence_path), "--max-input-bytes", "1"],
        cwd=ROOT,
    )
    if result.returncode == 0:
        raise E2EFailure(f"{format_name} resource limit was not enforced")
    document = load_json(output)
    validate_document(document)
    evidence = load_json(evidence_path)
    if evidence.get("input", {}).get("consumed") is not True:
        raise E2EFailure(f"{format_name} resource-limit evidence does not prove input consumption")
    if document.get("conversion", {}).get("status") != "failed":
        raise E2EFailure(f"{format_name} resource limit did not produce failed status")
    return {"format": format_name, "case": "resource-limit", "commandExitCode": result.returncode, "diagnostics": len(document.get("diagnostics", []))}


def run_all(keep: Path | None = None) -> dict[str, Any]:
    if keep is None:
        # Some managed Windows workstations deny chmod/rmtree in generated
        # temporary directories.  Use an ignored, per-process workspace so
        # concurrent acceptance/release commands cannot race while writing
        # fixtures, and a failed gate leaves inspectable evidence behind.
        work = ROOT / "e2e" / ".run" / f"run-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(keep).resolve()
        work.mkdir(parents=True, exist_ok=True)
    fixture_dir = work / "fixtures"
    paths = write_fixtures(fixture_dir)
    cases: list[dict[str, Any]] = []
    expected = {
        "docx": ("FDIR DOCX E2E", "bold"),
        "xlsx": ("Alpha", "SUM(B2:B3)"),
        "pdf": ("FDIR PDF E2E",),
        "markdown": ("FDIR Markdown E2E", "bold", "authoring-facts"),
    }
    for format_name in FORMATS:
        cases.append(check_case(format_name, paths[format_name], work, expected[format_name]))
    for format_name in FORMATS:
        cases.append(check_malformed(format_name, paths[f"malformed_{format_name}"], work))
    cases.append(check_resource_limit("markdown", paths["markdown"], work))
    report = {
        "schema": "fdir/e2e-report",
        "version": "1.0.0",
        "status": "passed",
        "realInput": True,
        "formats": list(FORMATS),
        "cases": cases,
        "workdir": str(work),
    }
    if keep is not None:
        (work / "e2e-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", required=True)
    parser.add_argument("--keep", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_all(args.keep)
    except Exception as exc:
        payload = {"schema": "fdir/e2e-report", "version": "1.0.0", "status": "failed", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"e2e valid: {len(report['cases'])} real-input cases across {len(report['formats'])} formats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
