"""Executable positive/negative tests for qualification Evidence integrity.

Fixtures are created in a temporary directory so this test adds no permanent
fixture files.  Every negative case starts from an independently built bundle;
the expected diagnostic must be present, and a negative mutation that validates
successfully is itself a test failure.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Callable

try:
    from build_qualification_bundle import build_bundle
    from validate_qualification_bundle import (
        CONTRACT_PATH,
        ROOT,
        canonical_json_bytes,
        git_head,
        load_json,
        sha256_bytes,
        sha256_file,
        validate_bundle,
        validate_schema_document,
    )
except ImportError:  # pragma: no cover - supports package-style imports
    from tools.build_qualification_bundle import build_bundle
    from tools.validate_qualification_bundle import (
        CONTRACT_PATH,
        ROOT,
        canonical_json_bytes,
        git_head,
        load_json,
        sha256_bytes,
        sha256_file,
        validate_bundle,
        validate_schema_document,
    )


class IntegrityTestError(Exception):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _payload_metadata(bundle: Path, relative: str, reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target = bundle / Path(*relative.split("/"))
    evidence_ids: set[str] = set()
    issue_numbers: set[int] = set()
    for evidence_id, report in reports.items():
        outputs = {item.get("path") for item in report.get("outputs", []) if isinstance(item, dict)}
        if relative == f"reports/{evidence_id}.json" or relative in outputs:
            evidence_ids.add(evidence_id)
            issue_numbers.update(item for item in report.get("issueNumbers", []) if isinstance(item, int))
    if relative.startswith("issues/"):
        try:
            issue_numbers.add(int(Path(relative).stem))
        except ValueError:
            pass
    return {
        "path": relative,
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
        "evidenceIds": sorted(evidence_ids),
        "issueNumbers": sorted(issue_numbers),
        "ordinal": 0,
    }


def _refresh_manifest(bundle: Path) -> None:
    """Refresh payload metadata for mutations that should reach deeper checks."""

    manifest_path = bundle / "manifest.json"
    manifest = load_json(manifest_path)
    reports: dict[str, dict[str, Any]] = {}
    for path in sorted((bundle / "reports").glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("evidenceId"), str):
            reports[value["evidenceId"]] = value
    paths = sorted(
        item.relative_to(bundle).as_posix()
        for item in bundle.rglob("*")
        if item.is_file() and item.resolve() != manifest_path.resolve()
    )
    files = [_payload_metadata(bundle, relative, reports) for relative in paths]
    for ordinal, entry in enumerate(files, start=1):
        entry["ordinal"] = ordinal
    manifest["files"] = files
    manifest["manifestDigest"] = sha256_bytes(canonical_json_bytes({key: value for key, value in manifest.items() if key != "manifestDigest"}))
    _write_json(manifest_path, manifest)


def _copy_case(source: Path, root: Path, name: str) -> Path:
    target = root / name
    shutil.copytree(source, target)
    return target


def _different_sha(value: str) -> str:
    return ("0" if value[0] != "0" else "1") + value[1:]


def _mutate_output(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    output_path = next(item["path"] for item in manifest["files"] if str(item["path"]).startswith("artifacts/"))
    target = bundle / Path(*output_path.split("/"))
    with target.open("ab") as stream:
        stream.write(b"\nmutation")


def _mutate_source_sha(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["sourceSha"] = _different_sha(report["sourceSha"])
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_empty_report(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    _write_json(report_path, {})
    _refresh_manifest(bundle)


def _mutate_ci(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["ci"]["sourceSha"] = _different_sha(report["sourceSha"])
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_duplicate_id(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    evidence_id = manifest["evidenceIds"][0]
    source = bundle / Path("reports", f"{evidence_id}.json")
    shutil.copyfile(source, bundle / "reports" / "duplicate-report.json")
    _refresh_manifest(bundle)


def _mutate_dirty_tree(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    manifest["dirtyTree"] = True
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["dirtyTree"] = True
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_assertion(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["assertions"][0]["expected"] = not report["assertions"][0]["expected"]
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_manifest_digest(bundle: Path) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["issueNumbers"] = [*manifest.get("issueNumbers", []), 999]
    _write_json(manifest_path, manifest)


MUTATIONS: dict[str, tuple[str, Callable[[Path], None]]] = {
    "modified-output": ("OUTPUT_DIGEST_MISMATCH", _mutate_output),
    "different-source-sha": ("SOURCE_SHA_MISMATCH", _mutate_source_sha),
    "empty-report": ("EVIDENCE_REPORT_EMPTY", _mutate_empty_report),
    "ci-inconsistent": ("CI_SOURCE_SHA_MISMATCH", _mutate_ci),
    "duplicate-evidence-id": ("DUPLICATE_EVIDENCE_ID", _mutate_duplicate_id),
    "dirty-tree": ("DIRTY_TREE", _mutate_dirty_tree),
    "assertion-mismatch": ("ASSERTION_MISMATCH", _mutate_assertion),
    "manifest-digest": ("MANIFEST_DIGEST_MISMATCH", _mutate_manifest_digest),
}


def _schema_case() -> dict[str, Any]:
    schema = load_json(ROOT / "schemas" / "qualification-evidence.schema.json")
    diagnostics = validate_schema_document(schema)
    return {
        "id": "schema-contract",
        "status": "passed" if not diagnostics else "failed",
        "diagnostics": diagnostics,
    }


def run_all() -> dict[str, Any]:
    contract = load_json(CONTRACT_PATH)
    declared = {item.get("id"): item.get("expectedDiagnostic") for item in contract.get("negativeFixtures", []) if isinstance(item, dict)}
    missing_mutations = sorted(set(MUTATIONS) - set(declared))
    if missing_mutations:
        raise IntegrityTestError("contract is missing negative fixtures: " + ", ".join(missing_mutations))
    mismatched_expectations = sorted(
        fixture_id
        for fixture_id, (expected_code, _) in MUTATIONS.items()
        if declared.get(fixture_id) != expected_code
    )
    if mismatched_expectations:
        raise IntegrityTestError("contract negative fixture diagnostics disagree: " + ", ".join(mismatched_expectations))
    source_sha = git_head(ROOT)
    if source_sha is None:
        raise IntegrityTestError("cannot resolve current git HEAD")
    schema_case = _schema_case()
    if schema_case["status"] != "passed":
        return {"schema": "fdir/evidence-integrity-report", "version": "1.0.0", "status": "failed", "positive": [schema_case], "negative": [], "positiveCount": 0, "negativeCount": 0}

    with tempfile.TemporaryDirectory(prefix="fdir-evidence-integrity-") as temporary:
        root = Path(temporary)
        # The production contract contains all recovery evidence lanes.  The
        # integrity matrix is about bundle tamper resistance, so use a
        # one-lane disposable contract here instead of rerunning the costly
        # defect campaign for every mutation fixture.
        integrity_contract = deepcopy(contract)
        first_spec = deepcopy(contract["defaultEvidence"][0])
        integrity_contract["scope"] = {
            "issueNumbers": list(first_spec["issueNumbers"]),
            "requiredEvidenceIds": [first_spec["evidenceId"]],
            "requiredRequirementIds": list(first_spec["requirementIds"]),
        }
        integrity_contract["defaultEvidence"] = [first_spec]
        integrity_contract_path = root / "qualification-contract.json"
        _write_json(integrity_contract_path, integrity_contract)
        positive_bundle = root / "positive"
        build_result = build_bundle(positive_bundle, source_sha=source_sha, contract_path=integrity_contract_path, allow_dirty=True)
        positive_validation = validate_bundle(positive_bundle / "manifest.json", repo_root=ROOT, contract_path=integrity_contract_path, allow_dirty=True)
        positive = {
            "id": "positive-bundle",
            "status": "passed" if build_result.get("schema") == "fdir/qualification-bundle-manifest" and positive_validation.get("status") == "passed" else "failed",
            "build": {"status": build_result.get("status"), "sourceSha": build_result.get("sourceSha"), "manifestDigest": build_result.get("manifestDigest")},
            "validation": positive_validation,
        }
        negative: list[dict[str, Any]] = []
        for fixture_id, (expected_code, mutate) in MUTATIONS.items():
            case_bundle = _copy_case(positive_bundle, root, fixture_id)
            try:
                mutate(case_bundle)
                validation = validate_bundle(case_bundle / "manifest.json", repo_root=ROOT, contract_path=integrity_contract_path, allow_dirty=False)
                codes = [item.get("code") for item in validation.get("diagnostics", [])]
                passed = validation.get("status") == "failed" and expected_code in codes
                negative.append({
                    "id": fixture_id,
                    "expectedDiagnostic": expected_code,
                    "status": "passed" if passed else "failed",
                    "observedDiagnostics": codes,
                })
            except Exception as exc:  # pragma: no cover - defensive fixture isolation
                negative.append({
                    "id": fixture_id,
                    "expectedDiagnostic": expected_code,
                    "status": "failed",
                    "observedDiagnostics": [],
                    "error": f"{type(exc).__name__}: {exc}",
                })
        passed = positive["status"] == "passed" and all(item["status"] == "passed" for item in negative)
        return {
            "schema": "fdir/evidence-integrity-report",
            "version": "1.0.0",
            "status": "passed" if passed else "failed",
            "positive": [schema_case, positive],
            "negative": negative,
            "positiveCount": 2,
            "negativeCount": len(negative),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run positive and negative Evidence integrity fixtures.")
    parser.add_argument("--all", action="store_true", help="run every declared integrity fixture")
    args = parser.parse_args(argv)
    if not args.all:
        parser.error("--all is required")
    try:
        result = run_all()
    except Exception as exc:  # pragma: no cover - fail closed with a machine-readable report
        result = {
            "schema": "fdir/evidence-integrity-report",
            "version": "1.0.0",
            "status": "failed",
            "positive": [],
            "negative": [],
            "positiveCount": 0,
            "negativeCount": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(rendered.encode("utf-8"))
    else:
        sys.stdout.write(rendered)
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
