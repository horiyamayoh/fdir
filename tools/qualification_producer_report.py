"""Issue-local producer-report envelope support for qualification lanes #95-#98.

This module is intentionally narrower than the shared bundle code.  The four
callers provide typed, issue-specific evaluator names and independently
computed expected/actual values.  This helper only binds those values to the
semantic report artifacts that the existing qualification contract already
declares.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"invalid JSON Pointer: {pointer!r}")
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _producer_pointer(section: str, case_id: str) -> str:
    return f"/producerEvidence/{section}/{_pointer_token(case_id)}"


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _artifact_reference(
    out_dir: Path,
    local_report_name: str,
    bundle_report_path: str,
    pointer: str,
) -> dict[str, Any]:
    local_path = Path(out_dir) / local_report_name
    document = _read_json(local_path)
    selected = _json_pointer(document, pointer)
    return {
        "path": bundle_report_path,
        "sha256": _sha256_file(local_path),
        "selector": {"kind": "json-pointer", "pointer": pointer},
        "selectedSha256": _sha256_bytes(_canonical_json_bytes(selected)),
    }


def _input_digests(paths: Sequence[Path]) -> list[str]:
    values: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file():
            values.add(_sha256_file(path))
        else:
            # A missing input is still represented by a valid digest so a
            # setup-failed report remains closed and inspectable.  The bundle
            # builder separately rejects the missing declared input.
            values.add(_sha256_bytes(f"missing-input:{path}".encode("utf-8")))
    if not values:
        values.add(_sha256_bytes(b"qualification-input-set-empty"))
    return sorted(values)


def _component_digest(path: Path | None, label: str) -> str:
    if path is not None and Path(path).is_file():
        return _sha256_file(Path(path))
    return _sha256_bytes(f"missing-component:{label}".encode("utf-8"))


def attach_producer_evidence(
    reports: Mapping[str, dict[str, Any]],
    rows: Sequence[dict[str, Any]],
) -> None:
    """Add closed, pointer-addressable authority/actual/support records.

    The records are copied into every semantic report because the existing
    contract declares those reports as the only non-source output artifacts.
    The producer envelope then references different report files for input,
    authority, actual, and support, preserving artifact separation after the
    bundle builder copies them.
    """

    evidence: dict[str, dict[str, Any]] = {
        "input": {},
        "expected": {},
        "actual": {},
        "support": {},
    }
    seen: set[str] = set()
    for row in rows:
        case_id = row.get("caseId")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"producer evidence caseId is missing or duplicated: {case_id!r}")
        seen.add(case_id)
        expected = deepcopy(row.get("expected"))
        actual = deepcopy(row.get("actual"))
        target = deepcopy(row.get("target"))
        evidence["input"][case_id] = deepcopy(row.get("input", {"caseId": case_id}))
        evidence["expected"][case_id] = expected
        evidence["actual"][case_id] = actual
        support: dict[str, Any] = {
            "assertionId": case_id,
            "caseId": case_id,
            "actual": actual,
            "target": target,
            "status": "passed",
        }
        if "oracleEvidence" in row:
            support["oracleEvidence"] = deepcopy(row["oracleEvidence"])
        evidence["support"][case_id] = support

    for report in reports.values():
        report["producerEvidence"] = deepcopy(evidence)


def write_producer_report(
    *,
    out_dir: Path,
    reports: Mapping[str, dict[str, Any]],
    report_names: Mapping[str, str],
    artifact_report_names: Sequence[str],
    issue_number: int,
    evidence_id: str,
    requirement_id: str,
    source_sha: str | None,
    input_paths: Sequence[Path],
    producer_id: str,
    authority_id: str,
    producer_component_path: Path,
    authority_component_path: Path,
    evaluator_component_path: Path,
    rows: Sequence[dict[str, Any]],
    shared_component_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Write semantic reports and their issue-specific producer envelope."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    semantic_names = list(report_names.values())
    selected_names = list(artifact_report_names)
    if len(selected_names) != 4 or len(set(selected_names)) != 4:
        raise ValueError("producer artifact binding requires four distinct semantic reports")
    if any(name not in semantic_names for name in selected_names):
        raise ValueError("producer artifact binding references an undeclared semantic report")
    if not rows:
        raise ValueError("producer report requires at least one typed test case")

    attach_producer_evidence(reports, rows)
    for report_kind, report_name in report_names.items():
        if report_kind not in reports:
            raise ValueError(f"semantic report is missing: {report_kind}")
        _write_json(out_dir / report_name, reports[report_kind])

    input_report, authority_report, actual_report, support_report = selected_names
    bundle_root = f"artifacts/{issue_number}"
    producer_cases: list[dict[str, Any]] = []
    producer_assertions: list[dict[str, Any]] = []
    failed_count = 0
    for row in rows:
        case_id = str(row["caseId"])
        classification = str(row["classification"])
        evaluator_type = str(row["evaluatorType"])
        expected = deepcopy(row.get("expected"))
        actual = deepcopy(row.get("actual"))
        comparison = {"operator": "not-equal" if evaluator_type == "mutation-killed" else "equal"}
        result = str(row.get("result", "failed"))
        if result not in {"passed", "failed"}:
            result = "failed"
        if result != "passed":
            failed_count += 2
        target = deepcopy(row.get("target"))
        diagnostic = deepcopy(row.get("diagnostic"))
        if not isinstance(target, dict) or not target:
            raise ValueError(f"producer target is required: {case_id}")
        if not isinstance(diagnostic, dict) or not diagnostic.get("code") or not diagnostic.get("message"):
            raise ValueError(f"producer diagnostic is required: {case_id}")
        refs = {
            "inputArtifact": _artifact_reference(out_dir, input_report, f"{bundle_root}/{input_report}", _producer_pointer("input", case_id)),
            "authorityArtifact": _artifact_reference(out_dir, authority_report, f"{bundle_root}/{authority_report}", _producer_pointer("expected", case_id)),
            "actualArtifact": _artifact_reference(out_dir, actual_report, f"{bundle_root}/{actual_report}", _producer_pointer("actual", case_id)),
            "supportingArtifact": _artifact_reference(out_dir, support_report, f"{bundle_root}/{support_report}", _producer_pointer("support", case_id)),
        }
        common = {
            "caseId": case_id,
            "requirementId": requirement_id,
            "classification": classification,
            **refs,
            "expected": expected,
            "actual": actual,
            "comparison": comparison,
            "target": target,
            "diagnostic": diagnostic,
        }
        producer_cases.append({**common, "result": result})
        producer_assertions.append({
            "assertionId": case_id,
            "requirementId": requirement_id,
            "assertionType": evaluator_type,
            "testCaseId": case_id,
            "classification": classification,
            "authorityArtifact": refs["authorityArtifact"],
            "actualArtifact": refs["actualArtifact"],
            "expected": expected,
            "actual": actual,
            "comparison": comparison,
            "status": result,
            "target": target,
            "diagnostic": diagnostic,
            "supportingArtifact": refs["supportingArtifact"],
        })

    component_paths = [Path(path) for path in shared_component_paths]
    producer_report = {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": evidence_id,
        "requirementIds": [requirement_id],
        "sourceSha": source_sha or "",
        "inputDigests": _input_digests(input_paths),
        "producerId": producer_id,
        "authorityId": authority_id,
        "independence": {
            "producerComponentDigest": _component_digest(producer_component_path, "producer"),
            "authorityComponentDigest": _component_digest(authority_component_path, "authority"),
            "evaluatorComponentDigest": _component_digest(evaluator_component_path, "evaluator"),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [
                _component_digest(path, f"shared-{index}")
                for index, path in enumerate(component_paths)
            ],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": [],
        "unsupportedItems": [],
        "waivedItems": [],
        "status": "passed" if failed_count == 0 else "failed",
        "failureCount": failed_count,
    }
    _write_json(out_dir / "producer-report.json", producer_report)
    return producer_report
