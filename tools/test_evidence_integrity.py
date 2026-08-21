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
from typing import Any, Callable
import os
import uuid

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
    from qualification_evidence import selected_artifact_digest, selected_artifact_value
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
    from tools.qualification_evidence import selected_artifact_digest, selected_artifact_value


class IntegrityTestError(Exception):
    pass


ISSUE_88_EVIDENCE_ID = "issue-88-qualification-contract"
ISSUE_88_REQUIREMENT_ID = "QUAL-88-EVIDENCE-OBJECT"
ISSUE_88_BUNDLE_PREFIX = "artifacts/88"
ISSUE_88_INPUT_PATHS = (
    "machine/qualification-contract.json",
    "schemas/qualification-evidence.schema.json",
    "machine/strict-completion-contract.json",
    "tools/test_evidence_integrity.py",
)


def _json_digest(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _fixture_ref(bundle: Path, path: str, selector: dict[str, Any]) -> dict[str, Any]:
    target = bundle / Path(*path.split("/"))
    value = selected_artifact_value(target, selector)
    return {
        "path": path,
        "sha256": sha256_file(target),
        "selector": selector,
        "selectedSha256": selected_artifact_digest(value, selector),
    }


def _producer_fixture(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--bundle-prefix", required=True)
    parser.add_argument("--input-digest", action="append", required=True)
    parser.add_argument("--evidence-id", default="fixture-evidence")
    parser.add_argument("--requirement-id", default="fixture-requirement")
    args = parser.parse_args(argv)
    out_dir = args.out_dir if args.out_dir.is_absolute() else ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "authority.json": {"value": "expected"},
        "actual-positive.json": {"value": "expected"},
        "actual-negative.json": {"value": "mutated"},
        "input.json": {"case": "fixture"},
    }
    for name, value in values.items():
        _write_json(out_dir / name, value)
    support_records = [
        {"assertionId": "fixture:positive", "caseId": "fixture-positive", "actual": "expected", "target": {"entity": "fixture", "field": "value"}, "status": "passed"},
        {"assertionId": "fixture-positive", "caseId": "fixture-positive", "actual": "expected", "target": {"entity": "fixture", "field": "value"}, "status": "passed"},
        {"assertionId": "fixture:negative", "caseId": "fixture-negative", "actual": "mutated", "target": {"entity": "fixture", "field": "value"}, "status": "passed"},
        {"assertionId": "fixture-negative", "caseId": "fixture-negative", "actual": "mutated", "target": {"entity": "fixture", "field": "value"}, "status": "passed"},
    ]
    _write_json(out_dir / "support.json", {"records": support_records})
    source_sha = git_head(ROOT)
    if source_sha is None:
        return 1
    source_by_name = {name: out_dir / name for name in (*values, "support.json")}
    bundle_prefix = args.bundle_prefix.rstrip("/")

    def ref(name: str, pointer: str) -> dict[str, Any]:
        return _fixture_ref(out_dir, name, {"kind": "json-pointer", "pointer": pointer}) | {"path": f"{bundle_prefix}/{name}"}

    target = {"entity": "fixture", "field": "value"}
    authority = ref("authority.json", "/value")
    actual_positive = ref("actual-positive.json", "/value")
    actual_negative = ref("actual-negative.json", "/value")
    input_artifact = ref("input.json", "")
    support_positive = ref("support.json", "/records/0")
    support_positive_case = ref("support.json", "/records/1")
    support_negative = ref("support.json", "/records/2")
    support_negative_case = ref("support.json", "/records/3")

    def case(case_id: str, classification: str, actual: dict[str, Any], support: dict[str, Any], operator: str) -> dict[str, Any]:
        expected_value = "expected"
        actual_value = "expected" if classification == "positive" else "mutated"
        return {
            "caseId": case_id,
            "requirementId": args.requirement_id,
            "classification": classification,
            "inputArtifact": input_artifact,
            "authorityArtifact": authority,
            "actualArtifact": actual,
            "expected": expected_value,
            "actual": actual_value,
            "comparison": {"operator": operator},
            "result": "passed",
            "target": target,
            "diagnostic": {"code": "FIXTURE_CASE", "message": "independent fixture comparison completed"},
            "supportingArtifact": support,
        }

    cases = [
        case("fixture-positive", "positive", actual_positive, support_positive_case, "equal"),
        case("fixture-negative", "mutation", actual_negative, support_negative_case, "not-equal"),
    ]
    assertions = [
        {
            "assertionId": "fixture:positive",
            "requirementId": args.requirement_id,
            "assertionType": "json-value-equals",
            "testCaseId": "fixture-positive",
            "classification": "positive",
            "authorityArtifact": authority,
            "actualArtifact": actual_positive,
            "expected": "expected",
            "actual": "expected",
            "comparison": {"operator": "equal"},
            "status": "passed",
            "target": target,
            "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "authority and actual values match"},
            "supportingArtifact": support_positive,
        },
        {
            "assertionId": "fixture:negative",
            "requirementId": args.requirement_id,
            "assertionType": "negative-rejection",
            "testCaseId": "fixture-negative",
            "classification": "mutation",
            "authorityArtifact": authority,
            "actualArtifact": actual_negative,
            "expected": "expected",
            "actual": "mutated",
            "comparison": {"operator": "not-equal"},
            "status": "passed",
            "target": target,
            "diagnostic": {"code": "FIXTURE_ASSERTION", "message": "mutation differs from authority"},
            "supportingArtifact": support_negative,
        },
    ]
    report = {
        "schema": "fdir/qualification-producer-report",
        "version": "1.0.0",
        "evidenceId": args.evidence_id,
        "requirementIds": [args.requirement_id],
        "sourceSha": source_sha,
        "inputDigests": list(args.input_digest),
        "producerId": "fixture-producer",
        "authorityId": "fixture-oracle",
        "independence": {
            "producerComponentDigest": _json_digest({"component": "producer"}),
            "authorityComponentDigest": _json_digest({"component": "oracle"}),
            "evaluatorComponentDigest": _json_digest({"component": "evaluator"}),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [],
        },
        "assertions": assertions,
        "testCases": cases,
        "uncoveredItems": [],
        "unsupportedItems": [],
        "waivedItems": [],
        "status": "passed",
        "failureCount": 0,
    }
    _write_json(out_dir / "producer-report.json", report)
    return 0


def _issue_88_producer_path(output: Path | None) -> Path:
    if output is None:
        return ROOT / "e2e" / ".run" / "qualification-issue-88" / "producer-report.json"
    resolved = output if output.is_absolute() else ROOT / output
    return resolved.parent / resolved.stem / "producer-report.json"


def _issue_88_input_digests() -> list[str]:
    return [sha256_file(ROOT / Path(*item.split("/"))) for item in ISSUE_88_INPUT_PATHS]


def _issue_88_local_ref(local_path: Path, bundle_path: str, selector: dict[str, Any]) -> dict[str, Any]:
    value = selected_artifact_value(local_path, selector)
    return {
        "path": bundle_path,
        "sha256": sha256_file(local_path),
        "selector": selector,
        "selectedSha256": selected_artifact_digest(value, selector),
    }


def _issue_88_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    positive = next(
        (item for item in result.get("positive", []) if isinstance(item, dict) and item.get("id") == "positive-bundle"),
        {},
    )
    positive_ok = result.get("status") == "passed" and positive.get("status") == "passed"
    rows.append(
        {
            "caseId": "positive-bundle",
            "classification": "positive",
            "expected": {"outcome": "passed", "lane": "positive-bundle"},
            "actual": {
                "outcome": "passed" if positive_ok else "unavailable",
                "lane": "positive-bundle",
            },
            "evidence": positive,
            "target": {"issueNumber": 88, "lane": "positive-bundle"},
            "assertionType": "json-value-equals",
            "diagnosticCode": "ISSUE88_POSITIVE_BUNDLE",
        }
    )
    for section in ("negative", "forgery", "builderForgery"):
        entries = result.get(section, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            case_id = item["id"]
            expected_diagnostic = item.get("expectedDiagnostic")
            observed = item.get("observedDiagnostics", [])
            if not isinstance(observed, list):
                observed = []
            observed_diagnostic = next(
                (value for value in observed if value == expected_diagnostic),
                None,
            )
            if observed_diagnostic is None and section == "builderForgery":
                error = item.get("error")
                if isinstance(error, str):
                    candidate = error.split(":", 1)[0].strip()
                    if candidate == expected_diagnostic:
                        observed_diagnostic = candidate
            rejected = item.get("status") == "passed" and observed_diagnostic == expected_diagnostic
            rows.append(
                {
                    "caseId": case_id,
                    "classification": "mutation",
                    "expected": {"outcome": "rejected", "diagnosticCode": expected_diagnostic, "fixtureStatus": "passed"},
                    "actual": {
                        "outcome": "rejected" if rejected else "unavailable",
                        "diagnosticCode": observed_diagnostic,
                        "fixtureStatus": item.get("status"),
                    },
                    "evidence": item,
                    "target": {"issueNumber": 88, "lane": section, "caseId": case_id},
                    "assertionType": "json-value-equals",
                    "diagnosticCode": "EXPECTED_DIAGNOSTIC_OBSERVED" if rejected else "EXPECTED_DIAGNOSTIC_UNAVAILABLE",
                }
            )
    if len(rows) == 1 and result.get("status") != "passed":
        rows.append(
            {
                "caseId": "runner-failure",
                "classification": "mutation",
                "expected": {"outcome": "rejected", "fixtureStatus": "passed"},
                "actual": {"outcome": "unavailable", "fixtureStatus": "failed"},
                "evidence": {"error": result.get("error", "required integrity evidence was unavailable")},
                "target": {"issueNumber": 88, "lane": "runner"},
                "assertionType": "json-value-equals",
                "diagnosticCode": "ISSUE88_EVIDENCE_UNAVAILABLE",
            }
        )
    return rows


def _write_issue_88_producer_report(result: dict[str, Any], producer_path: Path) -> dict[str, Any]:
    """Persist the real --all semantic matrix as an issue-88 producer envelope."""

    producer_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = producer_path.parent / "cases"
    input_path = producer_path.parent / "input.json"
    input_digests = _issue_88_input_digests()
    source_sha = git_head(ROOT) or "0" * 40
    _write_json(input_path, {"issueNumber": 88, "inputDigests": input_digests, "sourceSha": source_sha})
    input_ref = _issue_88_local_ref(input_path, f"{ISSUE_88_BUNDLE_PREFIX}/input.json", {"kind": "json-pointer", "pointer": "/issueNumber"})
    rows = _issue_88_rows(result)
    cases: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    failure_count = 0
    authority_digest_payload: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row["caseId"])
        case_root = artifact_root / case_id
        case_root.mkdir(parents=True, exist_ok=True)
        authority_path = case_root / "authority.json"
        actual_path = case_root / "actual.json"
        support_path = case_root / "support.json"
        authority_doc = {"caseId": case_id, "qualification": row["expected"], "target": row["target"]}
        actual_doc = {"caseId": case_id, "qualification": row["actual"], "evidence": row["evidence"], "target": row["target"]}
        assertion_id = f"issue-88:{case_id}:semantic"
        support_doc = {
            "records": [
                {
                    "assertionId": case_id,
                    "caseId": case_id,
                    "actual": row["actual"],
                    "target": row["target"],
                    "status": "passed",
                },
                {
                    "assertionId": assertion_id,
                    "caseId": case_id,
                    "actual": row["actual"],
                    "target": row["target"],
                    "status": "passed" if row["actual"] == row["expected"] else "failed",
                },
            ]
        }
        _write_json(authority_path, authority_doc)
        _write_json(actual_path, actual_doc)
        _write_json(support_path, support_doc)
        authority_ref = _issue_88_local_ref(authority_path, f"{ISSUE_88_BUNDLE_PREFIX}/cases/{case_id}/authority.json", {"kind": "json-pointer", "pointer": "/qualification"})
        actual_ref = _issue_88_local_ref(actual_path, f"{ISSUE_88_BUNDLE_PREFIX}/cases/{case_id}/actual.json", {"kind": "json-pointer", "pointer": "/qualification"})
        support_ref = _issue_88_local_ref(support_path, f"{ISSUE_88_BUNDLE_PREFIX}/cases/{case_id}/support.json", {"kind": "json-pointer", "pointer": "/records/0"})
        assertion_support_ref = _issue_88_local_ref(support_path, f"{ISSUE_88_BUNDLE_PREFIX}/cases/{case_id}/support.json", {"kind": "json-pointer", "pointer": "/records/1"})
        case_status = "passed" if row["actual"] == row["expected"] else "failed"
        if case_status != "passed":
            failure_count += 1
        cases.append(
            {
                "caseId": case_id,
                "requirementId": ISSUE_88_REQUIREMENT_ID,
                "classification": row["classification"],
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": row["expected"],
                "actual": row["actual"],
                "comparison": {"operator": "equal"},
                "result": case_status,
                "target": row["target"],
                "diagnostic": {"code": row["diagnosticCode"], "message": "issue-88 semantic integrity result was independently materialized"},
                "supportingArtifact": support_ref,
            }
        )
        assertion = {
            "assertionId": assertion_id,
            "requirementId": ISSUE_88_REQUIREMENT_ID,
            "assertionType": row["assertionType"],
            "testCaseId": case_id,
            "classification": row["classification"],
            "authorityArtifact": authority_ref,
            "actualArtifact": actual_ref,
            "expected": row["expected"],
            "actual": row["actual"],
            "comparison": {"operator": "equal"},
            "status": case_status,
            "target": row["target"],
            "diagnostic": {"code": row["diagnosticCode"], "message": "semantic mutation/assertion data was compared"},
            "supportingArtifact": assertion_support_ref,
        }
        assertions.append(assertion)
        authority_digest_payload.append({"caseId": case_id, "expected": row["expected"]})
    status = "passed" if result.get("status") == "passed" and failure_count == 0 else "failed"
    if status != "passed":
        failure_count = max(1, failure_count)
    report = {
        "schema": "fdir/qualification-producer-report",
        "version": "1.0.0",
        "evidenceId": ISSUE_88_EVIDENCE_ID,
        "requirementIds": [ISSUE_88_REQUIREMENT_ID],
        "sourceSha": source_sha,
        "inputDigests": input_digests,
        "producerId": "issue-88-integrity-runner",
        "authorityId": "issue-88-independent-integrity-oracle",
        "independence": {
            "producerComponentDigest": sha256_file(Path(__file__).resolve()),
            "authorityComponentDigest": _json_digest({"issue": 88, "cases": authority_digest_payload}),
            "evaluatorComponentDigest": sha256_file(ROOT / "tools" / "validate_qualification_bundle.py"),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [],
        },
        "assertions": assertions,
        "testCases": cases,
        "uncoveredItems": [] if status == "passed" else ["issue-88-semantic-integrity"],
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failure_count,
    }
    _write_json(producer_path, report)
    return report


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


def _mutate_issue_binding(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["issueNumbers"] = [89]
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_generator(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["generator"] = "tools/not-a-generator.py"
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_empty_assertions(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["assertions"] = []
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_ci_url(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["ci"]["runUrl"] = "https://github.com/another-owner/another-repo/actions/runs/1"
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_waiver(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["waivers"] = [{"waiverId": "hide-survivor", "reason": "synthetic", "approvedBy": "synthetic"}]
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_missing_output(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    output_path = next(item["path"] for item in manifest["files"] if str(item["path"]).startswith("artifacts/"))
    (bundle / Path(*output_path.split("/"))).unlink()


def _mutate_missing_producer_report(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    producer_path = report["producerReport"]["path"]
    (bundle / Path(*producer_path.split("/"))).unlink()
    _refresh_manifest(bundle)


def _mutate_unresolved_evidence_id(bundle: Path) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["evidenceIds"] = [*manifest.get("evidenceIds", []), "issue-88-no-such-evidence"]
    _write_json(manifest_path, manifest)


def _mutate_command_generator(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["command"] = ["python", "tools/not-a-command.py"]
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _mutate_empty_test_cases(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    report = load_json(report_path)
    report["testCases"] = []
    _write_json(report_path, report)
    _refresh_manifest(bundle)


def _rebind_bundle_digests(bundle: Path) -> None:
    """Recompute every digest after a deliberate forged-report mutation."""

    manifest = load_json(bundle / "manifest.json")
    root_report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    root_report = load_json(root_report_path)
    producer_path = root_report["producerReport"]["path"]
    producer_file = bundle / Path(*producer_path.split("/"))
    producer = load_json(producer_file)

    def rebind(ref: dict[str, Any]) -> None:
        target = bundle / Path(*ref["path"].split("/"))
        value = selected_artifact_value(target, ref["selector"])
        ref["sha256"] = sha256_file(target)
        ref["selectedSha256"] = selected_artifact_digest(value, ref["selector"])

    for case in producer.get("testCases", []):
        for field in ("inputArtifact", "authorityArtifact", "actualArtifact", "supportingArtifact"):
            rebind(case[field])
    for assertion in producer.get("assertions", []):
        for field in ("authorityArtifact", "actualArtifact", "supportingArtifact"):
            rebind(assertion[field])
    _write_json(producer_file, producer)
    producer_digest = sha256_file(producer_file)
    root_report["producerReport"]["sha256"] = producer_digest
    output_digest_map: dict[str, str] = {}
    for output in root_report.get("outputs", []):
        target = bundle / Path(*output["path"].split("/"))
        if target.is_file():
            output["sha256"] = sha256_file(target)
            output_digest_map[output["path"]] = output["sha256"]
    for assertion in root_report.get("assertions", []):
        if assertion.get("assertionType") == "producer-report-digest":
            assertion["expected"] = producer_digest
            assertion["actual"] = producer_digest
        elif assertion.get("assertionType") == "output-digest-binding":
            assertion["expected"] = dict(output_digest_map)
            assertion["actual"] = dict(output_digest_map)
        elif assertion.get("assertionType") == "manifest-completeness":
            paths = sorted(output_digest_map)
            assertion["expected"] = paths
            assertion["actual"] = paths
    for case in root_report.get("testCases", []):
        if case.get("caseType") == "packaging":
            rebind(case["actualArtifact"])
            case["expected"] = producer_digest
            case["actual"] = producer_digest
    _write_json(root_report_path, root_report)
    _refresh_manifest(bundle)


def _mutate_simultaneous_self_attestation(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    root_report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    root_report = load_json(root_report_path)
    producer_path = bundle / Path(*root_report["producerReport"]["path"].split("/"))
    producer = load_json(producer_path)
    actual_path = bundle / Path("artifacts", "fixture", "actual-positive.json")
    actual = load_json(actual_path)
    actual["value"] = "forged"
    _write_json(actual_path, actual)
    producer["assertions"][0]["expected"] = "forged"
    producer["assertions"][0]["actual"] = "forged"
    producer["testCases"][0]["expected"] = "forged"
    producer["testCases"][0]["actual"] = "forged"
    support_path = bundle / Path("artifacts", "fixture", "support.json")
    support = load_json(support_path)
    support["records"][0]["actual"] = "forged"
    support["records"][1]["actual"] = "forged"
    _write_json(support_path, support)
    _write_json(producer_path, producer)
    _rebind_bundle_digests(bundle)


def _mutate_support_reassignment(bundle: Path) -> None:
    manifest = load_json(bundle / "manifest.json")
    root_report_path = bundle / Path("reports", f"{manifest['evidenceIds'][0]}.json")
    root_report = load_json(root_report_path)
    producer_path = bundle / Path(*root_report["producerReport"]["path"].split("/"))
    producer = load_json(producer_path)
    producer["assertions"][0]["supportingArtifact"] = deepcopy(producer["assertions"][1]["supportingArtifact"])
    _write_json(producer_path, producer)
    _rebind_bundle_digests(bundle)


def _forge_contract(base: dict[str, Any], *, command: list[str], output_path: str, output_role: str) -> Path:
    root = ROOT / "e2e" / ".run" / f"evidence-integrity-forgery-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    contract = deepcopy(base)
    first = deepcopy(base["defaultEvidence"][0])
    first["command"] = command
    first["outputs"] = [{"sourcePath": output_path, "path": "artifacts/forgery/output.json", "role": output_role}]
    contract["scope"] = {"issueNumbers": [88], "requiredEvidenceIds": [first["evidenceId"]], "requiredRequirementIds": list(first["requirementIds"])}
    contract["defaultEvidence"] = [first]
    path = root / "qualification-contract.json"
    _write_json(path, contract)
    return path


MUTATIONS: dict[str, tuple[str, Callable[[Path], None]]] = {
    "modified-output": ("OUTPUT_DIGEST_MISMATCH", _mutate_output),
    "different-source-sha": ("SOURCE_SHA_MISMATCH", _mutate_source_sha),
    "empty-report": ("EVIDENCE_REPORT_EMPTY", _mutate_empty_report),
    "ci-inconsistent": ("CI_SOURCE_SHA_MISMATCH", _mutate_ci),
    "duplicate-evidence-id": ("DUPLICATE_EVIDENCE_ID", _mutate_duplicate_id),
    "dirty-tree": ("DIRTY_TREE", _mutate_dirty_tree),
    "assertion-mismatch": ("ASSERTION_MISMATCH", _mutate_assertion),
    "manifest-digest": ("MANIFEST_DIGEST_MISMATCH", _mutate_manifest_digest),
    "issue-binding": ("ISSUE_BINDING_MISMATCH", _mutate_issue_binding),
    "generator-missing": ("GENERATOR_MISSING", _mutate_generator),
    "empty-assertions": ("ASSERTIONS_REQUIRED", _mutate_empty_assertions),
    "ci-url-mismatch": ("CI_URL_MISMATCH", _mutate_ci_url),
    "waiver-survivor": ("WAIVER_NOT_ALLOWED", _mutate_waiver),
    "missing-output": ("OUTPUT_MISSING", _mutate_missing_output),
    "unresolved-evidence-id": ("MANIFEST_EVIDENCE_IDS", _mutate_unresolved_evidence_id),
    "command-generator-missing": ("COMMAND_GENERATOR_PATH", _mutate_command_generator),
    "empty-test-cases": ("TEST_CASES_REQUIRED", _mutate_empty_test_cases),
}


FORGERY_MUTATIONS: dict[str, tuple[str, Callable[[Path], None]]] = {
    "expected-actual-same-forged-value": ("PRODUCER_EXPECTED_MISMATCH", _mutate_simultaneous_self_attestation),
    "supporting-artifact-reassigned": ("SUPPORT_SELECTOR_MISMATCH", _mutate_support_reassignment),
    "producer-report-output-missing": ("PRODUCER_REPORT_MISSING", _mutate_missing_producer_report),
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

    # The managed Windows image denies access to Python-created 0700 temp
    # directories.  Use the repository's ignored run area for this disposable
    # matrix and explicitly allow the bundle builder to write there.
    root = ROOT / "e2e" / ".run" / f"evidence-integrity-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        # The production contract contains all recovery evidence lanes.  The
        # integrity matrix is about bundle tamper resistance, so use a
        # one-lane disposable contract here instead of rerunning the costly
        # defect campaign for every mutation fixture.
        integrity_contract = deepcopy(contract)
        first_spec = deepcopy(contract["defaultEvidence"][0])
        fixture_dir = f"e2e/.run/evidence-integrity-fixture-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        first_spec["command"] = [
            "python",
            "tools/test_evidence_integrity.py",
            "--producer-fixture",
            "--out-dir",
            fixture_dir,
            "--bundle-prefix",
            "artifacts/fixture",
            "--evidence-id",
            first_spec["evidenceId"],
            "--requirement-id",
            first_spec["requirementIds"][0],
        ]
        first_spec["command"].extend(value for item in first_spec["inputs"] for value in ("--input-digest", sha256_file(ROOT / Path(*item["path"].split("/")))))
        first_spec["outputs"] = [
            # Exercise the contract-facing flag independently from the
            # normalized bundle role: the builder must copy this as
            # role=producer-report, while semantic reports remain separate.
            {"sourcePath": f"{fixture_dir}/producer-report.json", "path": "artifacts/fixture/producer-report.json", "role": "semantic-report", "producerReport": True},
            {"sourcePath": f"{fixture_dir}/authority.json", "path": "artifacts/fixture/authority.json", "role": "oracle"},
            {"sourcePath": f"{fixture_dir}/actual-positive.json", "path": "artifacts/fixture/actual-positive.json", "role": "behavioral-output"},
            {"sourcePath": f"{fixture_dir}/actual-negative.json", "path": "artifacts/fixture/actual-negative.json", "role": "mutation-output"},
            {"sourcePath": f"{fixture_dir}/input.json", "path": "artifacts/fixture/input.json", "role": "corpus-input"},
            {"sourcePath": f"{fixture_dir}/support.json", "path": "artifacts/fixture/support.json", "role": "supporting-record"},
        ]
        integrity_contract["scope"] = {
            "issueNumbers": list(first_spec["issueNumbers"]),
            "requiredEvidenceIds": [first_spec["evidenceId"]],
            "requiredRequirementIds": list(first_spec["requirementIds"]),
        }
        integrity_contract["defaultEvidence"] = [first_spec]
        integrity_contract_path = root / "qualification-contract.json"
        _write_json(integrity_contract_path, integrity_contract)
        positive_bundle = root / "positive"
        build_result = build_bundle(positive_bundle, source_sha=source_sha, contract_path=integrity_contract_path, allow_dirty=True, allow_repository_output=True)
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
        forgery: list[dict[str, Any]] = []
        for fixture_id, (expected_code, mutate) in FORGERY_MUTATIONS.items():
            case_bundle = _copy_case(positive_bundle, root, fixture_id)
            try:
                mutate(case_bundle)
                validation = validate_bundle(case_bundle / "manifest.json", repo_root=ROOT, contract_path=integrity_contract_path, allow_dirty=True)
                codes = [item.get("code") for item in validation.get("diagnostics", [])]
                passed = validation.get("status") == "failed" and expected_code in codes
                forgery.append({
                    "id": fixture_id,
                    "expectedDiagnostic": expected_code,
                    "status": "passed" if passed else "failed",
                    "observedDiagnostics": codes,
                })
            except Exception as exc:  # pragma: no cover - defensive fixture isolation
                forgery.append({
                    "id": fixture_id,
                    "expectedDiagnostic": expected_code,
                    "status": "failed",
                    "observedDiagnostics": [],
                    "error": f"{type(exc).__name__}: {exc}",
                })
        builder_forgery: list[dict[str, Any]] = []
        for fixture_id, command_args in {
            "no-op-command": ["--noop"],
            "exit-zero-success-string": ["--success-only"],
            "empty-stdout": ["--empty-stdout"],
            "existing-source-file": ["--noop"],
            "source-snapshot-only": ["--noop"],
        }.items():
            output_source = "tools/test_evidence_integrity.py" if fixture_id in {"existing-source-file", "source-snapshot-only"} else "schemas/qualification-evidence.schema.json"
            forged_contract = _forge_contract(
                contract,
                command=["python", "tools/test_evidence_integrity.py", *command_args],
                output_path=output_source,
                output_role="source-snapshot" if fixture_id in {"existing-source-file", "source-snapshot-only"} else "static-output",
            )
            try:
                build_bundle(root / fixture_id, source_sha=source_sha, contract_path=forged_contract, allow_dirty=True, allow_repository_output=True)
                builder_forgery.append({"id": fixture_id, "status": "failed", "expectedDiagnostic": "PRODUCER_REPORT_MISSING"})
            except Exception as exc:
                detail = str(exc)
                builder_forgery.append({
                    "id": fixture_id,
                    "status": "passed" if "PRODUCER_REPORT_MISSING" in detail else "failed",
                    "expectedDiagnostic": "PRODUCER_REPORT_MISSING",
                    "error": detail,
                })
        passed = positive["status"] == "passed" and all(item["status"] == "passed" for item in negative + forgery + builder_forgery)
        return {
            "schema": "fdir/evidence-integrity-report",
            "version": "1.0.0",
            "status": "passed" if passed else "failed",
            "sourceSha": source_sha,
            "assertions": [
                {"assertionId": item["id"], "expected": "passed", "actual": item["status"], "status": item["status"]}
                for item in negative
            ],
            "positive": [schema_case, positive],
            "negative": negative,
            "forgery": forgery,
            "builderForgery": builder_forgery,
            "positiveCount": 2,
            "negativeCount": len(negative),
            "forgeryCount": len(forgery),
            "builderForgeryCount": len(builder_forgery),
        }
    finally:
        # Keep the ignored directory available for post-failure inspection;
        # the workspace cleanup process owns eventual removal.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run positive and negative Evidence integrity fixtures.")
    parser.add_argument("--all", action="store_true", help="run every declared integrity fixture")
    parser.add_argument("--out", type=Path, help="also write the machine-readable integrity report")
    parser.add_argument("--producer-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--noop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--success-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--empty-stdout", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--out-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--bundle-prefix", help=argparse.SUPPRESS)
    parser.add_argument("--input-digest", action="append", help=argparse.SUPPRESS)
    parser.add_argument("--evidence-id", help=argparse.SUPPRESS)
    parser.add_argument("--requirement-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.producer_fixture:
        fixture_args = list(argv or sys.argv[1:])
        fixture_args = fixture_args[fixture_args.index("--producer-fixture") + 1 :]
        return _producer_fixture(fixture_args)
    if args.noop or args.success_only or args.empty_stdout:
        if args.success_only:
            print("success")
        return 0
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
    try:
        _write_issue_88_producer_report(result, _issue_88_producer_path(args.out))
    except Exception as exc:  # pragma: no cover - missing producer evidence must not become a pass
        result["status"] = "failed"
        result["error"] = f"producer-report generation failed: {type(exc).__name__}: {exc}"
    if args.out is not None:
        output = args.out if args.out.is_absolute() else ROOT / args.out
        _write_json(output, result)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(rendered.encode("utf-8"))
    else:
        sys.stdout.write(rendered)
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
