"""Build and validate the non-circular final release attestation.

The qualification bundle is a candidate produced before the final release
decision.  This module supplies the separate Phase-C attestation: it binds the
candidate manifest and every report to the exact source SHA, CI attempt,
uploaded artifact, and verified GitHub issue-state snapshot.  The #105 report
inside a candidate bundle is accepted only as a pending behavioral candidate;
it can never be the authority that makes itself release-ready.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

try:
    from github_issue_state import (
        AUDIT_ISSUE_NUMBERS,
        IssueStateError,
        REPOSITORY,
        SHA256,
        SHA40,
        canonical_json_bytes,
        derive_release_boundary,
        evidence_close_time_blockers,
        fetch_live_issue_state,
        load_snapshot,
        parse_datetime,
        validate_snapshot,
    )
except ImportError:  # pragma: no cover - package-style imports
    from tools.github_issue_state import (
        AUDIT_ISSUE_NUMBERS,
        IssueStateError,
        REPOSITORY,
        SHA256,
        SHA40,
        canonical_json_bytes,
        derive_release_boundary,
        evidence_close_time_blockers,
        fetch_live_issue_state,
        load_snapshot,
        parse_datetime,
        validate_snapshot,
    )


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAME = "fdir/release-attestation"
VERSION = "1.0.0"
QUALIFICATION_CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
LIVE_ISSUES = tuple(range(87, 106)) + tuple(range(108, 114))
QUALIFICATION_ISSUES = tuple(range(88, 106))
BARRIER_ISSUES = tuple(range(108, 114))
REQUIRED_ISSUES = QUALIFICATION_ISSUES
CANDIDATE_ISSUES = tuple(range(88, 105))
UMBRELLA_ISSUE = 87
FINAL_QUALIFICATION_ISSUE = 105
SOURCE_SHA_RE = SHA40
GITHUB_SERVER_URL = "https://github.com"
WORKFLOW_PATH = ".github/workflows/design.yml"
EVIDENCE_SCHEMA_NAME = "fdir/github-actions-release-evidence"
EVIDENCE_VERSION = "1.0.0"
QUALIFICATION_JOB_PROFILE = "qualification"
QUALIFICATION_JOB_NAME = "Qualification bundle"
QUALIFICATION_BUNDLE_PREFIX = "fdir-qualification-bundle-"
QUALIFICATION_RECEIPT_PREFIX = "fdir-qualification-receipt-"
UPLOAD_BUNDLE_STEP = "Upload qualification bundle"
UPLOAD_RECEIPT_STEP = "Upload qualification upload receipt"
REQUIRED_QUALIFICATION_STEPS = (
    "Validate workflow identity",
    "Validate design authority",
    "Validate generated model contract",
    "Validate qualification contract",
    "Validate qualification schema",
    "Run Evidence integrity matrix",
    "Run clean-room replay",
    "Prepare commit-bound bundle path",
    "Build commit-bound qualification bundle",
    "Validate commit-bound qualification bundle",
    "Run candidate qualification gate",
    "Publish qualification digest",
    "Run real-input adapter E2E",
    UPLOAD_BUNDLE_STEP,
    "Write qualification upload receipt",
    UPLOAD_RECEIPT_STEP,
)
PLACEHOLDER_PROVENANCE_VALUES = frozenset({
    "",
    "local",
    "synthetic",
    "fake",
    "unknown",
    "none",
    "null",
    "n/a",
})


class AttestationError(Exception):
    """A fail-closed attestation diagnostic."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise AttestationError("BUNDLE_FILE_UNAVAILABLE", f"cannot read {path}: {exc}") from exc


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AttestationError("ATTESTATION_MISSING", f"JSON file does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError("ATTESTATION_JSON", f"cannot load {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _now(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_sha(value: Any, *, field: str = "sourceSha") -> str:
    if not isinstance(value, str) or SOURCE_SHA_RE.fullmatch(value) is None:
        raise AttestationError("ATTESTATION_SHA", f"{field} must be a 40-character lowercase SHA")
    return value


def _is_relative_repository_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = Path(*value.replace("\\", "/").split("/"))
    return not value.startswith(("/", "\\")) and not re.match(r"^[A-Za-z]:", value) and "." not in path.parts and ".." not in path.parts


def _manifest_without_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifestDigest"}


def _manifest_digest(manifest: dict[str, Any]) -> str:
    value = manifest.get("manifestDigest")
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise AttestationError("BUNDLE_MANIFEST_DIGEST", "bundle manifestDigest is invalid")
    expected = sha256_bytes(canonical_json_bytes(_manifest_without_digest(manifest)))
    if value != expected:
        raise AttestationError("BUNDLE_MANIFEST_DIGEST_MISMATCH", "bundle manifestDigest does not match its content")
    return value


def _contract_scope() -> tuple[set[str], dict[str, list[int]]]:
    contract = _load_json(QUALIFICATION_CONTRACT_PATH)
    if not isinstance(contract, dict):
        raise AttestationError("QUALIFICATION_SCOPE", "qualification contract is not an object")
    if contract.get("targetIssueNumbers") != list(LIVE_ISSUES) or contract.get("recoveryChildIssueNumbers") != list(QUALIFICATION_ISSUES):
        raise AttestationError("QUALIFICATION_SCOPE", "qualification contract target scope is not #87-#105 and #108-#113 with reports #88-#105")
    if contract.get("barrierIssueNumbers") != [UMBRELLA_ISSUE, *BARRIER_ISSUES]:
        raise AttestationError("BARRIER_COVERAGE_SCOPE", "qualification contract barrier issue scope is invalid")
    ci_policy = contract.get("ciPolicy")
    if not isinstance(ci_policy, dict) or ci_policy.get("allowedProviders") != ["github-actions"]:
        raise AttestationError("ATTESTATION_CI_PROVIDER", "qualification contract permits a non-GitHub final provider")
    scope = contract.get("scope")
    issue_numbers = scope.get("issueNumbers") if isinstance(scope, dict) else None
    evidence_ids = scope.get("requiredEvidenceIds") if isinstance(scope, dict) else None
    if issue_numbers != list(QUALIFICATION_ISSUES) or not isinstance(evidence_ids, list) or not evidence_ids:
        raise AttestationError("QUALIFICATION_SCOPE", "qualification contract is not exactly #88-#105")
    expected_coverage = {
        "issue-88-qualification-contract": list(BARRIER_ISSUES),
        "issue-105-release-quality": [UMBRELLA_ISSUE, *BARRIER_ISSUES],
    }
    coverage = contract.get("barrierCoverage")
    if not isinstance(coverage, dict) or set(coverage) != set(expected_coverage):
        raise AttestationError("BARRIER_COVERAGE_SCOPE", "barrierCoverage must be owned by the #88 and #105 reports")
    for evidence_id, expected_numbers in expected_coverage.items():
        record = coverage.get(evidence_id)
        if not isinstance(record, dict) or record.get("issueNumbers") != expected_numbers or not isinstance(record.get("role"), str) or not record.get("role"):
            raise AttestationError("BARRIER_COVERAGE_SCOPE", f"barrierCoverage is invalid for {evidence_id}")
    return {str(item) for item in evidence_ids}, expected_coverage


def _contract_evidence_ids() -> set[str]:
    evidence_ids, _ = _contract_scope()
    return evidence_ids


def _relative_or_external(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        # A CI artifact can live outside the checkout.  The exact local path is
        # not an authority, but the manifest file digest is; retain a stable
        # marker instead of smuggling an absolute workstation path into the
        # attestation.
        return "external-bundle/manifest.json"


def _reports(bundle_root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expected_ids, barrier_coverage = _contract_scope()
    reports_dir = bundle_root / "reports"
    if not reports_dir.is_dir():
        raise AttestationError("BUNDLE_REPORTS_MISSING", "bundle has no reports directory")
    reports: dict[str, dict[str, Any]] = {}
    covered_issue_numbers: set[int] = set()
    for path in sorted(reports_dir.glob("*.json")):
        value = _load_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("evidenceId"), str):
            raise AttestationError("BUNDLE_REPORT_INVALID", f"invalid evidence report: {path.name}")
        evidence_id = value["evidenceId"]
        if evidence_id in reports or evidence_id not in expected_ids:
            raise AttestationError("BUNDLE_REPORT_SCOPE", f"unexpected or duplicate evidence report: {evidence_id}")
        issue_numbers = value.get("issueNumbers")
        if not isinstance(issue_numbers, list) or not issue_numbers or any(isinstance(item, bool) or not isinstance(item, int) for item in issue_numbers):
            raise AttestationError("BUNDLE_REPORT_SCOPE", f"qualification report issue scope is invalid: {evidence_id}")
        if len(issue_numbers) != len(set(issue_numbers)) or not set(issue_numbers) <= set(QUALIFICATION_ISSUES):
            raise AttestationError("BUNDLE_REPORT_SCOPE", f"qualification report binds duplicate or live-only issues: {evidence_id}")
        if covered_issue_numbers.intersection(issue_numbers):
            raise AttestationError("BUNDLE_REPORT_SCOPE", f"qualification report issue coverage is duplicated: {evidence_id}")
        covered_issue_numbers.update(issue_numbers)
        if value.get("sourceSha") != manifest.get("sourceSha") or value.get("status") != "passed" or value.get("failureCount") != 0:
            raise AttestationError("BUNDLE_REPORT_NOT_PASSED", f"evidence report is not passed and SHA-bound: {evidence_id}")
        reports[evidence_id] = value
    if set(reports) != expected_ids:
        raise AttestationError("BUNDLE_REPORT_SCOPE", f"bundle evidence IDs do not exactly cover #88-#105: {sorted(set(expected_ids) - set(reports))}")
    if covered_issue_numbers != set(QUALIFICATION_ISSUES):
        raise AttestationError("BUNDLE_REPORT_SCOPE", "bundle qualification reports must cover exactly #88-#105")
    if set(barrier_coverage) - set(reports):
        raise AttestationError("BARRIER_COVERAGE_SCOPE", "barrierCoverage refers to a report absent from the candidate bundle")
    return reports


def _output_path(report: dict[str, Any], basename: str) -> str:
    matches = sorted(
        str(item.get("path"))
        for item in report.get("outputs", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str) and (Path(item["path"]).name == basename or Path(item["path"]).name.endswith("." + basename))
    )
    if len(matches) != 1:
        raise AttestationError("BUNDLE_OUTPUT_BINDING", f"{report.get('evidenceId')} must bind exactly one {basename}: {matches}")
    return matches[0]


def _candidate_105_receipt(bundle_root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Accept only the issue-specific Phase-A #105 report.

    The candidate bundle may contain the behavioral #105 report, but it must
    not run ``release_gate.py`` or ``release_attestation.py`` from inside the
    bundle.  Those commands are Phase-C authorities and using either one as a
    producer would make #105 self-qualifying.
    """

    command = report.get("command")
    if not isinstance(command, list) or "tools/qualification_issue105.py" not in command:
        raise AttestationError(
            "CIRCULAR_105_EVIDENCE",
            "#105 candidate evidence must be produced by tools/qualification_issue105.py",
        )
    if any(item in command for item in ("tools/release_gate.py", "tools/release_attestation.py")):
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 candidate evidence must not invoke a release authority")
    if "--bundle" in command or "--attestation" in command:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 evidence must not invoke a bundle or attestation from inside the candidate bundle")
    producer_path = bundle_root / Path(*_output_path(report, "producer-report.json").split("/"))
    try:
        value = _load_json(producer_path)
    except AttestationError as exc:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 producer report is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != "fdir/qualification-producer-report":
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 candidate output is not a producer report")
    if value.get("evidenceId") != "issue-105-release-quality" or value.get("requirementIds") != ["QUAL-105-RELEASE-BARRIER"]:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 producer report binding is invalid")
    independence = value.get("independence")
    if not isinstance(independence, dict) or independence.get("expectedDerivedFromActual") is not False:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "#105 producer report lacks an independent expected-value binding")
    if value.get("status") != "passed" or value.get("failureCount") != 0:
        raise AttestationError("CANDIDATE_105_NOT_PASSED", "#105 behavioral candidate report is not passed")
    return {"status": "pending-attestation", "command": command, "releaseReady": False, "producerReport": _output_path(report, "producer-report.json")}


def _bundle_metadata(bundle_manifest: Path, *, repo_root: Path, expected_source_sha: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    bundle_manifest = bundle_manifest.resolve()
    bundle_root = bundle_manifest.parent
    manifest = _load_json(bundle_manifest)
    if not isinstance(manifest, dict) or manifest.get("schema") != "fdir/qualification-bundle-manifest":
        raise AttestationError("BUNDLE_MANIFEST_SCHEMA", "bundle manifest schema is invalid")
    source_sha = _source_sha(manifest.get("sourceSha"))
    if source_sha != expected_source_sha:
        raise AttestationError("BUNDLE_SOURCE_SHA_MISMATCH", f"bundle source SHA {source_sha} does not match {expected_source_sha}")
    if manifest.get("dirtyTree") is not False:
        raise AttestationError("BUNDLE_DIRTY", "final attestation requires a clean qualification bundle")
    manifest_digest = _manifest_digest(manifest)
    reports = _reports(bundle_root, manifest)
    _candidate_105_receipt(bundle_root, reports["issue-105-release-quality"])
    report_metadata = []
    for evidence_id in sorted(reports):
        report = reports[evidence_id]
        report_metadata.append({
            "evidenceId": evidence_id,
            "issueNumbers": report.get("issueNumbers"),
            "sourceSha": report.get("sourceSha"),
            "generatedAt": report.get("generatedAt"),
            "status": report.get("status"),
            "sha256": sha256_file(bundle_root / "reports" / f"{evidence_id}.json"),
        })
    metadata = {
        "manifestPath": _relative_or_external(bundle_manifest, repo_root),
        "manifestDigest": manifest_digest,
        "manifestFileDigest": sha256_file(bundle_manifest),
        "sourceSha": source_sha,
        "dirtyTree": False,
        "evidenceIds": sorted(reports),
        "reports": report_metadata,
    }
    return metadata, reports


def validate_candidate_bundle(bundle_manifest: Path, *, repo_root: Path = ROOT, allow_dirty: bool = False) -> dict[str, Any]:
    """Validate the non-circular candidate shape used by strict completion.

    ``allow_dirty`` is accepted for API symmetry with the generic bundle
    validator, but a final candidate still has to advertise ``dirtyTree:``
    false.  A local caller may validate a separately copied clean fixture
    without changing the release policy.
    """

    try:
        try:
            from validate_qualification_bundle import validate_bundle
        except ImportError:  # pragma: no cover
            from tools.validate_qualification_bundle import validate_bundle
        validation = validate_bundle(bundle_manifest, repo_root=repo_root, allow_dirty=allow_dirty)
    except Exception as exc:
        if isinstance(exc, AttestationError):
            raise
        raise AttestationError("BUNDLE_VALIDATION_ERROR", str(exc)) from exc
    if validation.get("status") != "passed":
        diagnostics = validation.get("diagnostics", [])
        raise AttestationError(
            "BUNDLE_VALIDATION_FAILED",
            json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
        )
    manifest = _load_json(bundle_manifest.resolve())
    if not isinstance(manifest, dict):
        raise AttestationError("BUNDLE_MANIFEST_SCHEMA", "bundle manifest is not an object")
    source_sha = _source_sha(manifest.get("sourceSha"))
    metadata, _ = _bundle_metadata(bundle_manifest, repo_root=repo_root, expected_source_sha=source_sha)
    return metadata


def _non_synthetic_text(value: Any, *, field: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.casefold() in PLACEHOLDER_PROVENANCE_VALUES or value.casefold().startswith("local://"):
        raise AttestationError(code, f"{field} is missing, synthetic, or local-only")
    return value


def _positive_numeric_text(value: Any, *, field: str, code: str) -> str:
    if isinstance(value, bool):
        raise AttestationError(code, f"{field} must be a positive numeric GitHub identity")
    if isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = value.strip()
    else:
        raise AttestationError(code, f"{field} must be a positive numeric GitHub identity")
    if re.fullmatch(r"[1-9][0-9]*", rendered) is None:
        raise AttestationError(code, f"{field} must be a positive numeric GitHub identity")
    return rendered


def _attempt(value: Any, *, field: str = "attempt") -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AttestationError("ATTESTATION_ATTEMPT", f"{field} must be a positive integer")
    return value


def _artifact_digest(value: Any, *, field: str = "artifact.digest") -> str:
    if not isinstance(value, str):
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST", f"{field} must be a SHA-256 digest")
    rendered = value.strip()
    if rendered.startswith("sha256:"):
        rendered = rendered[7:]
    if SHA256.fullmatch(rendered) is None:
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST", f"{field} must be a lowercase SHA-256 digest")
    return rendered


def _run_url(repository: str, run_id: str) -> str:
    return f"{GITHUB_SERVER_URL}/{repository}/actions/runs/{run_id}"


def _attempt_url(repository: str, run_id: str, attempt: int) -> str:
    return f"{_run_url(repository, run_id)}/attempts/{attempt}"


def _artifact_url(repository: str, run_id: str, artifact_id: str) -> str:
    return f"{_run_url(repository, run_id)}/artifacts/{artifact_id}"


def _parse_attestation_datetime(value: Any, *, field: str) -> datetime:
    try:
        return parse_datetime(value, field=field)
    except IssueStateError as exc:
        raise AttestationError("ATTESTATION_TIMESTAMP", exc.detail) from exc


def _validate_retention(record: dict[str, Any], *, field: str, now: datetime | None) -> str:
    if record.get("expired") is not False:
        raise AttestationError("ATTESTATION_ARTIFACT_RETENTION", f"{field} is expired or has no explicit non-expired state")
    expires_at = _parse_attestation_datetime(record.get("expiresAt"), field=f"{field}.expiresAt")
    if expires_at <= _now(now):
        raise AttestationError("ATTESTATION_ARTIFACT_RETENTION", f"{field} retention has expired")
    size = record.get("sizeInBytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise AttestationError("ATTESTATION_ARTIFACT_EMPTY", f"{field} must contain a non-empty uploaded artifact")
    return record["expiresAt"]


def _validate_run_record(
    run: Any,
    *,
    repository: str,
    source_sha: str,
    expected_run_id: str,
    expected_attempt: int,
) -> dict[str, Any]:
    if not isinstance(run, dict):
        raise AttestationError("ATTESTATION_WORKFLOW_MISSING", "GitHub Actions workflow run provenance is missing")
    run_id = _positive_numeric_text(run.get("id"), field="workflow.run.id", code="ATTESTATION_RUN_ID")
    if run_id != expected_run_id:
        raise AttestationError("ATTESTATION_RUN_ID_MISMATCH", "workflow run ID does not match the selected target run")
    attempt = _attempt(run.get("attempt"), field="workflow.run.attempt")
    if attempt != expected_attempt:
        raise AttestationError("ATTESTATION_ATTEMPT_MISMATCH", "workflow run attempt does not match the selected target attempt")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise AttestationError("ATTESTATION_WORKFLOW_NOT_SUCCESS", "workflow run is not completed successfully")
    if _source_sha(run.get("headSha"), field="workflow.run.headSha") != source_sha:
        raise AttestationError("ATTESTATION_SHA_MISMATCH", "workflow run head SHA does not match the attestation source SHA")
    if run.get("path") != WORKFLOW_PATH:
        raise AttestationError("ATTESTATION_WORKFLOW_MISMATCH", "workflow run is not the checked-in design workflow")
    expected_url = _run_url(repository, run_id)
    expected_attempt_url = _attempt_url(repository, run_id, attempt)
    if run.get("url") != expected_url:
        raise AttestationError("ATTESTATION_RUN_URL", "workflow run URL is not the canonical GitHub URL")
    if run.get("attemptUrl") != expected_attempt_url:
        raise AttestationError("ATTESTATION_ATTEMPT_URL", "workflow attempt URL is not the canonical GitHub URL")
    return {
        "id": run_id,
        "attempt": attempt,
        "status": "completed",
        "conclusion": "success",
        "headSha": source_sha,
        "path": WORKFLOW_PATH,
        "url": expected_url,
        "attemptUrl": expected_attempt_url,
    }


def _validate_step_record(
    step: Any,
    *,
    repository: str,
    run_id: str,
    job_id: str,
    expected_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise AttestationError("ATTESTATION_STEP_MISSING", "workflow step provenance is missing")
    number = step.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise AttestationError("ATTESTATION_STEP", "workflow step number is invalid")
    name = _non_synthetic_text(step.get("name"), field="workflow.step.name", code="ATTESTATION_STEP")
    if expected_name is not None and name != expected_name:
        raise AttestationError("ATTESTATION_STEP_MISMATCH", f"expected workflow step {expected_name!r}, got {name!r}")
    if step.get("status") != "completed" or step.get("conclusion") != "success":
        raise AttestationError("ATTESTATION_STEP_NOT_SUCCESS", f"workflow step {name!r} is not completed successfully")
    url = _non_synthetic_text(step.get("url"), field="workflow.step.url", code="ATTESTATION_STEP_URL")
    prefix = f"{_run_url(repository, run_id)}/job/{job_id}"
    if not url.startswith(prefix) or f"#step:{number}" not in url:
        raise AttestationError("ATTESTATION_STEP_URL", "workflow step URL is not bound to the selected run and job")
    return {"number": number, "name": name, "status": "completed", "conclusion": "success", "url": url}


def _validate_job_record(
    job: Any,
    *,
    repository: str,
    source_sha: str,
    expected_run_id: str,
    expected_attempt: int,
    expected_profile: str = QUALIFICATION_JOB_PROFILE,
    expected_name: str = QUALIFICATION_JOB_NAME,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(job, dict):
        raise AttestationError("ATTESTATION_JOB_MISSING", "required qualification job provenance is missing")
    job_id = _positive_numeric_text(job.get("id"), field="workflow.job.id", code="ATTESTATION_JOB")
    if job.get("name") != expected_name or job.get("profile") != expected_profile:
        raise AttestationError("ATTESTATION_JOB_MISMATCH", "workflow job is not the qualification job")
    job_run_id = _positive_numeric_text(job.get("runId"), field="workflow.job.runId", code="ATTESTATION_JOB")
    if job_run_id != expected_run_id:
        raise AttestationError("ATTESTATION_JOB_RUN_MISMATCH", "qualification job is bound to a different workflow run")
    job_attempt = _attempt(job.get("attempt"), field="workflow.job.attempt")
    if job_attempt != expected_attempt:
        raise AttestationError("ATTESTATION_JOB_ATTEMPT_MISMATCH", "qualification job is bound to a different workflow attempt")
    if _source_sha(job.get("headSha"), field="workflow.job.headSha") != source_sha:
        raise AttestationError("ATTESTATION_JOB_SHA_MISMATCH", "qualification job SHA does not match the attestation source SHA")
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise AttestationError("ATTESTATION_JOB_NOT_SUCCESS", "qualification job is not completed successfully")
    job_url = _non_synthetic_text(job.get("url"), field="workflow.job.url", code="ATTESTATION_JOB_URL")
    if not job_url.startswith(f"{_run_url(repository, expected_run_id)}/job/{job_id}"):
        raise AttestationError("ATTESTATION_JOB_URL", "qualification job URL is not bound to the selected run")
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        raise AttestationError("ATTESTATION_STEP_MISSING", "qualification job has no step provenance")
    steps: dict[str, dict[str, Any]] = {}
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            raise AttestationError("ATTESTATION_STEP", "qualification job contains malformed step provenance")
        name = raw_step.get("name")
        if not isinstance(name, str) or not name:
            raise AttestationError("ATTESTATION_STEP", "qualification job contains an unnamed step")
        if name in steps:
            raise AttestationError("ATTESTATION_STEP", f"qualification job contains duplicate step {name!r}")
        steps[name] = _validate_step_record(raw_step, repository=repository, run_id=expected_run_id, job_id=job_id)
    for required_name in REQUIRED_QUALIFICATION_STEPS:
        if required_name not in steps:
            raise AttestationError("ATTESTATION_STEP_MISSING", f"required qualification step is missing: {required_name}")
        _validate_step_record(steps[required_name], repository=repository, run_id=expected_run_id, job_id=job_id, expected_name=required_name)
    normalized_job = {
        "id": job_id,
        "name": expected_name,
        "profile": expected_profile,
        "runId": expected_run_id,
        "attempt": expected_attempt,
        "headSha": source_sha,
        "status": "completed",
        "conclusion": "success",
        "url": job_url,
        "steps": list(steps.values()),
    }
    return normalized_job, steps


def _validate_evidence_artifact(
    record: Any,
    *,
    repository: str,
    source_sha: str,
    run_id: str,
    attempt: int,
    expected_name: str,
    expected_id: str | None,
    expected_digest: str | None,
    expected_url: str | None,
    expected_producer_step: str,
    now: datetime | None,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise AttestationError("ATTESTATION_ARTIFACT_MISSING", f"missing artifact evidence for {expected_name}")
    if record.get("status") != "uploaded":
        raise AttestationError("ATTESTATION_ARTIFACT_NOT_UPLOADED", f"artifact {expected_name} is not uploaded")
    if record.get("name") != expected_name:
        raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", f"artifact name is not bound to the selected SHA and attempt: {expected_name}")
    artifact_id = _positive_numeric_text(record.get("id"), field=f"artifact {expected_name}.id", code="ATTESTATION_ARTIFACT_ID")
    if expected_id is not None and artifact_id != _positive_numeric_text(expected_id, field="artifact.id", code="ATTESTATION_ARTIFACT_ID"):
        raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", f"artifact {expected_name} ID does not match the upload receipt")
    digest = _artifact_digest(record.get("digest"), field=f"artifact {expected_name}.digest")
    if record.get("sha256") is not None and _artifact_digest(record.get("sha256"), field=f"artifact {expected_name}.sha256") != digest:
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST_MISMATCH", f"artifact {expected_name} digest fields differ")
    if expected_digest is not None and digest != _artifact_digest(expected_digest, field="artifact.digest"):
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST_MISMATCH", f"artifact {expected_name} digest does not match the upload receipt")
    url = _non_synthetic_text(record.get("url"), field=f"artifact {expected_name}.url", code="ATTESTATION_ARTIFACT_URL")
    canonical_url = _artifact_url(repository, run_id, artifact_id)
    if url != canonical_url or (expected_url is not None and url != expected_url):
        raise AttestationError("ATTESTATION_ARTIFACT_URL_MISMATCH", f"artifact {expected_name} URL is not bound to its run and ID")
    if record.get("producerJob") != QUALIFICATION_JOB_NAME:
        raise AttestationError("ATTESTATION_ARTIFACT_JOB_MISMATCH", f"artifact {expected_name} producer job is not the qualification job")
    if record.get("producerStep") != expected_producer_step:
        raise AttestationError("ATTESTATION_ARTIFACT_STEP_MISMATCH", f"artifact {expected_name} producer step is incorrect")
    workflow_run = record.get("workflowRun")
    if not isinstance(workflow_run, dict):
        raise AttestationError("ATTESTATION_ARTIFACT_RUN_MISMATCH", f"artifact {expected_name} has no workflow-run binding")
    workflow_run_id = _positive_numeric_text(workflow_run.get("id"), field=f"artifact {expected_name}.workflowRun.id", code="ATTESTATION_ARTIFACT_RUN_MISMATCH")
    if workflow_run_id != run_id or _source_sha(workflow_run.get("head_sha"), field=f"artifact {expected_name}.workflowRun.head_sha") != source_sha:
        raise AttestationError("ATTESTATION_ARTIFACT_RUN_MISMATCH", f"artifact {expected_name} is bound to a different workflow run or SHA")
    expires_at = _validate_retention(record, field=f"artifact {expected_name}", now=now)
    return {
        "status": "uploaded",
        "id": artifact_id,
        "name": expected_name,
        "digest": digest,
        "sha256": digest,
        "url": url,
        "sizeInBytes": record["sizeInBytes"],
        "expired": False,
        "expiresAt": expires_at,
        "producerJob": QUALIFICATION_JOB_NAME,
        "producerStep": expected_producer_step,
        "workflowRun": {"id": run_id, "head_sha": source_sha},
    }


def _receipt_without_digest(receipt: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "receiptDigest"}


def _validate_upload_receipt(
    receipt: Any,
    *,
    repository: str,
    source_sha: str,
    run_id: str,
    attempt: int,
    job_id: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_url: str,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_MISSING", "qualification upload receipt is missing")
    if receipt.get("schema") != "fdir/qualification-upload-receipt" or receipt.get("version") != "1.0.0":
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_SCHEMA", "qualification upload receipt schema/version is invalid")
    if receipt.get("provider") != "github-actions":
        raise AttestationError("ATTESTATION_CI_PROVIDER", "qualification upload receipt must come from GitHub Actions")
    if receipt.get("repository") != repository or receipt.get("sourceSha") != source_sha:
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_BINDING", "qualification upload receipt repository or SHA is inconsistent")
    receipt_run_id = _positive_numeric_text(receipt.get("runId"), field="uploadReceipt.runId", code="ATTESTATION_RUN_ID")
    if receipt_run_id != run_id:
        raise AttestationError("ATTESTATION_RUN_ID_MISMATCH", "qualification upload receipt belongs to a different workflow run")
    if _attempt(receipt.get("attempt"), field="uploadReceipt.attempt") != attempt:
        raise AttestationError("ATTESTATION_ATTEMPT_MISMATCH", "qualification upload receipt belongs to a different workflow attempt")
    receipt_job = receipt.get("job")
    if not isinstance(receipt_job, dict) or _non_synthetic_text(receipt_job.get("id"), field="uploadReceipt.job.id", code="ATTESTATION_JOB") != job_id or receipt_job.get("name") != QUALIFICATION_JOB_NAME:
        raise AttestationError("ATTESTATION_JOB_MISMATCH", "qualification upload receipt job binding is invalid")
    expected_artifact_name = f"{QUALIFICATION_BUNDLE_PREFIX}{source_sha}-attempt-{attempt}"
    receipt_artifact = receipt.get("artifact")
    if not isinstance(receipt_artifact, dict) or receipt_artifact.get("status") != "uploaded" or receipt_artifact.get("name") != expected_artifact_name:
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_ARTIFACT", "qualification upload receipt has no valid bundle artifact")
    receipt_artifact_id = _positive_numeric_text(receipt_artifact.get("id"), field="uploadReceipt.artifact.id", code="ATTESTATION_ARTIFACT_ID")
    if receipt_artifact_id != artifact_id:
        raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", "qualification upload receipt artifact ID differs from the selected artifact")
    if _artifact_digest(receipt_artifact.get("digest"), field="uploadReceipt.artifact.digest") != artifact_digest:
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST_MISMATCH", "qualification upload receipt artifact digest differs from the selected artifact")
    if receipt_artifact.get("url") != artifact_url:
        raise AttestationError("ATTESTATION_ARTIFACT_URL_MISMATCH", "qualification upload receipt artifact URL differs from the selected artifact")
    links = receipt.get("links")
    if not isinstance(links, dict) or links.get("source") != f"{GITHUB_SERVER_URL}/{repository}/commit/{source_sha}" or links.get("run") != _run_url(repository, run_id) or links.get("attempt") != _attempt_url(repository, run_id, attempt):
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_LINKS", "qualification upload receipt links are not bound to the selected run")
    digest = receipt.get("receiptDigest")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None or digest != sha256_bytes(canonical_json_bytes(_receipt_without_digest(receipt))):
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_DIGEST", "qualification upload receipt digest is invalid")
    return receipt


def _validate_actions_evidence(
    evidence: Any,
    *,
    repository: str,
    source_sha: str,
    run_id: str,
    attempt: int,
    job_id: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_url: str,
    upload_receipt: Any,
    now: datetime | None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise AttestationError("ATTESTATION_PROVENANCE_MISSING", "GitHub Actions release evidence is missing")
    if evidence.get("schema") != EVIDENCE_SCHEMA_NAME or evidence.get("version") != EVIDENCE_VERSION:
        raise AttestationError("ATTESTATION_PROVENANCE_SCHEMA", "GitHub Actions release evidence schema/version is invalid")
    if evidence.get("provider") != "github-actions" or evidence.get("repository") != repository or evidence.get("sourceSha") != source_sha:
        raise AttestationError("ATTESTATION_PROVENANCE_BINDING", "GitHub Actions release evidence provider, repository, or SHA is inconsistent")
    if evidence.get("candidateReady") is not True or evidence.get("releaseReady") is not False or evidence.get("status") != "pending-attestation":
        raise AttestationError("ATTESTATION_PROVENANCE_NOT_READY", "GitHub Actions release evidence is not a successful candidate awaiting final attestation")
    diagnostics = evidence.get("diagnostics")
    if not isinstance(diagnostics, list) or diagnostics:
        raise AttestationError("ATTESTATION_PROVENANCE_DIAGNOSTICS", "GitHub Actions release evidence contains failures or warnings")
    run = _validate_run_record(evidence.get("run"), repository=repository, source_sha=source_sha, expected_run_id=run_id, expected_attempt=attempt)
    jobs = evidence.get("jobs")
    if not isinstance(jobs, list):
        raise AttestationError("ATTESTATION_JOB_MISSING", "GitHub Actions release evidence has no jobs")
    matching_jobs = [item for item in jobs if isinstance(item, dict) and item.get("name") == QUALIFICATION_JOB_NAME and item.get("profile") == QUALIFICATION_JOB_PROFILE]
    if len(matching_jobs) != 1:
        raise AttestationError("ATTESTATION_JOB_MISSING", "GitHub Actions release evidence does not contain exactly one qualification job")
    job, steps = _validate_job_record(matching_jobs[0], repository=repository, source_sha=source_sha, expected_run_id=run_id, expected_attempt=attempt)
    bundle_name = f"{QUALIFICATION_BUNDLE_PREFIX}{source_sha}-attempt-{attempt}"
    receipt_name = f"{QUALIFICATION_RECEIPT_PREFIX}{source_sha}-attempt-{attempt}"
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list):
        raise AttestationError("ATTESTATION_ARTIFACT_MISSING", "GitHub Actions release evidence has no artifact list")
    bundle_records = [item for item in artifacts if isinstance(item, dict) and item.get("name") == bundle_name]
    receipt_records = [item for item in artifacts if isinstance(item, dict) and item.get("name") == receipt_name]
    if len(bundle_records) != 1 or len(receipt_records) != 1:
        raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_MISSING", "qualification bundle or upload receipt artifact is missing")
    bundle_artifact = _validate_evidence_artifact(bundle_records[0], repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, expected_name=bundle_name, expected_id=artifact_id, expected_digest=artifact_digest, expected_url=artifact_url, expected_producer_step=UPLOAD_BUNDLE_STEP, now=now)
    receipt = _validate_upload_receipt(upload_receipt, repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, job_id=job_id, artifact_id=bundle_artifact["id"], artifact_digest=bundle_artifact["digest"], artifact_url=bundle_artifact["url"])
    receipt_artifact_payload = receipt["artifact"]
    receipt_artifact = _validate_evidence_artifact(receipt_records[0], repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, expected_name=receipt_name, expected_id=receipt_artifact_payload["id"], expected_digest=receipt_artifact_payload["digest"], expected_url=None, expected_producer_step=UPLOAD_RECEIPT_STEP, now=now)
    return {
        "evidenceSchema": EVIDENCE_SCHEMA_NAME,
        "evidenceVersion": EVIDENCE_VERSION,
        "repository": repository,
        "sourceSha": source_sha,
        "candidateReady": True,
        "status": "pending-attestation",
        "run": run,
        "job": job,
        "steps": list(steps.values()),
        "step": steps[UPLOAD_BUNDLE_STEP],
        "artifact": bundle_artifact,
        "receiptArtifact": receipt_artifact,
        "uploadReceipt": receipt,
    }


def _validate_ci_provenance(
    provenance: Any,
    *,
    repository: str,
    source_sha: str,
    run_id: str,
    attempt: int,
    job_id: str,
    now: datetime | None,
) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise AttestationError("ATTESTATION_PROVENANCE_MISSING", "final attestation requires GitHub Actions provenance")
    if provenance.get("evidenceSchema") != EVIDENCE_SCHEMA_NAME or provenance.get("evidenceVersion") != EVIDENCE_VERSION or provenance.get("repository") != repository or provenance.get("sourceSha") != source_sha or provenance.get("candidateReady") is not True or provenance.get("status") != "pending-attestation":
        raise AttestationError("ATTESTATION_PROVENANCE_BINDING", "embedded GitHub Actions provenance is incomplete or inconsistent")
    run = _validate_run_record(provenance.get("run"), repository=repository, source_sha=source_sha, expected_run_id=run_id, expected_attempt=attempt)
    job, steps_by_name = _validate_job_record(provenance.get("job"), repository=repository, source_sha=source_sha, expected_run_id=run_id, expected_attempt=attempt)
    if job_id != QUALIFICATION_JOB_PROFILE:
        raise AttestationError("ATTESTATION_JOB_MISMATCH", "attestation job identity is not the qualification job")
    top_steps = provenance.get("steps")
    if not isinstance(top_steps, list) or {item.get("name") for item in top_steps if isinstance(item, dict)} != set(steps_by_name):
        raise AttestationError("ATTESTATION_STEP_MISMATCH", "embedded step catalog does not match the qualification job")
    selected_step = _validate_step_record(provenance.get("step"), repository=repository, run_id=run_id, job_id=job["id"], expected_name=UPLOAD_BUNDLE_STEP)
    if selected_step != steps_by_name[UPLOAD_BUNDLE_STEP]:
        raise AttestationError("ATTESTATION_STEP_MISMATCH", "embedded artifact upload step is not the verified qualification step")
    bundle_name = f"{QUALIFICATION_BUNDLE_PREFIX}{source_sha}-attempt-{attempt}"
    receipt_name = f"{QUALIFICATION_RECEIPT_PREFIX}{source_sha}-attempt-{attempt}"
    artifact_value = provenance.get("artifact")
    artifact_id = _positive_numeric_text(artifact_value.get("id") if isinstance(artifact_value, dict) else None, field="ci.provenance.artifact.id", code="ATTESTATION_ARTIFACT_ID")
    artifact_digest = _artifact_digest(artifact_value.get("digest") if isinstance(artifact_value, dict) else None, field="ci.provenance.artifact.digest")
    artifact_url = _non_synthetic_text(artifact_value.get("url") if isinstance(artifact_value, dict) else None, field="ci.provenance.artifact.url", code="ATTESTATION_ARTIFACT_URL")
    artifact = _validate_evidence_artifact(artifact_value, repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, expected_name=bundle_name, expected_id=artifact_id, expected_digest=artifact_digest, expected_url=artifact_url, expected_producer_step=UPLOAD_BUNDLE_STEP, now=now)
    receipt = provenance.get("uploadReceipt")
    _validate_upload_receipt(receipt, repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, job_id=job_id, artifact_id=artifact["id"], artifact_digest=artifact["digest"], artifact_url=artifact["url"])
    receipt_payload = receipt.get("artifact") if isinstance(receipt, dict) else None
    receipt_artifact = _validate_evidence_artifact(provenance.get("receiptArtifact"), repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt, expected_name=receipt_name, expected_id=receipt_payload.get("id") if isinstance(receipt_payload, dict) else None, expected_digest=receipt_payload.get("digest") if isinstance(receipt_payload, dict) else None, expected_url=None, expected_producer_step=UPLOAD_RECEIPT_STEP, now=now)
    if receipt_artifact["id"] == artifact["id"]:
        raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", "qualification bundle and upload receipt are the same artifact")
    return {
        "evidenceSchema": EVIDENCE_SCHEMA_NAME,
        "evidenceVersion": EVIDENCE_VERSION,
        "repository": repository,
        "sourceSha": source_sha,
        "candidateReady": True,
        "status": "pending-attestation",
        "run": run,
        "job": job,
        "steps": list(steps_by_name.values()),
        "step": selected_step,
        "artifact": artifact,
        "receiptArtifact": receipt_artifact,
        "uploadReceipt": receipt,
    }


def _validate_supply_chain(
    value: Any,
    *,
    source_sha: str,
    run_id: str,
    attempt: int,
    bundle: dict[str, Any],
    artifact: dict[str, Any],
    now: datetime | None,
) -> dict[str, Any]:
    """Validate independently uploaded package and provenance bindings.

    These records are deliberately supplied by the CI evidence producer. The
    attestation builder never invents a package, SBOM, signature, or
    provenance record from local files or from the attestation itself.
    """

    if not isinstance(value, dict):
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_MISSING", "final attestation requires independent supply-chain evidence")
    candidate = value.get("candidateBundle")
    if not isinstance(candidate, dict):
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_MISSING", "supply-chain evidence has no candidate bundle binding")
    expected_candidate = {
        "kind": "candidate-bundle",
        "provider": "github-actions",
        "sourceSha": source_sha,
        "runId": run_id,
        "attempt": attempt,
        "jobId": QUALIFICATION_JOB_PROFILE,
        "artifactId": artifact.get("id"),
        "artifactName": artifact.get("name"),
        "artifactDigest": artifact.get("digest"),
        "artifactUrl": artifact.get("url"),
        "manifestDigest": bundle.get("manifestDigest"),
        "manifestFileDigest": bundle.get("manifestFileDigest"),
    }
    for field, expected in expected_candidate.items():
        actual = candidate.get(field)
        if field in {"artifactDigest", "manifestDigest", "manifestFileDigest"}:
            if not isinstance(actual, str) or SHA256.fullmatch(actual) is None or actual != expected:
                raise AttestationError("ATTESTATION_SUPPLY_CHAIN_BINDING", f"candidate bundle {field} is not bound to the selected artifact")
        elif actual != expected:
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_BINDING", f"candidate bundle {field} is not bound to the selected run")
    retention = candidate.get("retention")
    if not isinstance(retention, dict) or retention.get("expired") is not False:
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_RETENTION", "candidate bundle retention is missing or expired")
    _parse_attestation_datetime(retention.get("expiresAt"), field="supplyChain.candidateBundle.retention.expiresAt")
    if _parse_attestation_datetime(retention["expiresAt"], field="supplyChain.candidateBundle.retention.expiresAt") <= _now(now):
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_RETENTION", "candidate bundle retention has expired")
    verification = candidate.get("verification")
    if verification != {"status": "verified", "method": "independent-ci"}:
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_VERIFICATION", "candidate bundle must have independent CI verification")
    if candidate.get("selfAttested") is not False or candidate.get("circular") is not False:
        raise AttestationError("ATTESTATION_SUPPLY_CHAIN_CIRCULAR", "candidate bundle cannot be self-attested or circular")

    expected_records = {
        "package": ("package", "independent-ci"),
        "sbom": ("sbom", "independent-ci"),
        "dependencyLock": ("dependency-lock", "independent-ci"),
        "signature": ("signature", "signature-verification"),
        "provenance": ("provenance", "provenance-verification"),
    }
    for field, (kind, method) in expected_records.items():
        record = value.get(field)
        if not isinstance(record, dict):
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_MISSING", f"supply-chain record is missing: {field}")
        if record.get("kind") != kind or record.get("provider") != "github-actions" or record.get("sourceSha") != source_sha:
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_BINDING", f"supply-chain record is not GitHub Actions-bound: {field}")
        path = record.get("path")
        if not isinstance(path, str) or not path or Path(path).is_absolute() or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_PATH", f"supply-chain path is not repository-relative: {field}")
        if re.search(r"(?:^|/)(?:release[_-]attestation|release[_-]gate|strict[_-]completion[_-]gate)(?:[-_.][A-Za-z0-9_-]+)*(?:\.[A-Za-z0-9]+)?$", path):
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_CIRCULAR", f"supply-chain path is a release gate or attestation: {path}")
        digest = record.get("sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_DIGEST", f"supply-chain digest is invalid: {field}")
        record_verification = record.get("verification")
        if not isinstance(record_verification, dict) or record_verification.get("status") != "verified" or record_verification.get("method") != method:
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_VERIFICATION", f"supply-chain verification is invalid: {field}")
        if record.get("selfAttested") is not False or record.get("circular") is not False:
            raise AttestationError("ATTESTATION_SUPPLY_CHAIN_CIRCULAR", f"supply-chain record is self-attested or circular: {field}")
    return value


def _load_object(value: Any, *, missing_code: str, label: str) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, Path):
        try:
            return _load_json(value)
        except AttestationError as exc:
            raise AttestationError(missing_code, f"cannot load {label}: {exc.detail}") from exc
    if isinstance(value, str) and value:
        try:
            return _load_json(Path(value))
        except AttestationError as exc:
            raise AttestationError(missing_code, f"cannot load {label}: {exc.detail}") from exc
    raise AttestationError(missing_code, f"{label} is missing")


def _discover_upload_receipt(bundle_manifest: Path, environment: dict[str, str]) -> Path:
    configured = environment.get("QUALIFICATION_UPLOAD_RECEIPT_PATH") or environment.get("UPLOAD_RECEIPT_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    runner_temp = environment.get("RUNNER_TEMP")
    if runner_temp:
        candidates.append(Path(runner_temp) / "candidate-receipt" / "qualification-upload-receipt.json")
    for parent in bundle_manifest.resolve().parents:
        candidates.append(parent / "candidate-receipt" / "qualification-upload-receipt.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AttestationError("ATTESTATION_UPLOAD_RECEIPT_MISSING", "qualification upload receipt was not found")


def _ci_metadata(
    source_sha: str,
    *,
    artifact_id: str | None,
    artifact_digest: str | None,
    artifact_url: str | None,
    environment: dict[str, str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = environment or os.environ
    if not isinstance(artifact_id, str) or not artifact_id:
        raise AttestationError("ATTESTATION_ARTIFACT_MISSING", "final attestation requires an uploaded artifact ID")
    normalized_artifact_id = _positive_numeric_text(artifact_id, field="artifact.id", code="ATTESTATION_ARTIFACT_ID")
    normalized_artifact_digest = _artifact_digest(artifact_digest, field="artifact.digest")
    if not isinstance(artifact_url, str) or not artifact_url:
        raise AttestationError("ATTESTATION_ARTIFACT_URL", "final attestation requires an artifact URL")
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    if not actions:
        raise AttestationError("ATTESTATION_CI_PROVIDER", "final attestation authority must be GitHub Actions")
    repository = _non_synthetic_text(env.get("GITHUB_REPOSITORY"), field="GITHUB_REPOSITORY", code="ATTESTATION_REPOSITORY_MISMATCH")
    if repository != REPOSITORY:
        raise AttestationError("ATTESTATION_REPOSITORY_MISMATCH", f"CI repository is {repository!r}, not {REPOSITORY!r}")
    declared_sha = _source_sha(env.get("GITHUB_SHA"), field="GITHUB_SHA")
    if declared_sha != source_sha:
        raise AttestationError("ATTESTATION_SHA_MISMATCH", "CI GITHUB_SHA does not match the candidate source SHA")
    run_id = _positive_numeric_text(env.get("GITHUB_RUN_ID"), field="GITHUB_RUN_ID", code="ATTESTATION_RUN_ID")
    attempt_value = _attempt(env.get("GITHUB_RUN_ATTEMPT"), field="GITHUB_RUN_ATTEMPT")
    job_id = _non_synthetic_text(env.get("GITHUB_JOB"), field="GITHUB_JOB", code="ATTESTATION_JOB")
    if job_id != QUALIFICATION_JOB_PROFILE:
        raise AttestationError("ATTESTATION_JOB_MISMATCH", "final attestation must be produced for the qualification job")
    if provenance is None:
        raise AttestationError("ATTESTATION_PROVENANCE_MISSING", "final attestation requires GitHub Actions run, job, step, and artifact evidence")
    provenance_value = _validate_ci_provenance(provenance, repository=repository, source_sha=source_sha, run_id=run_id, attempt=attempt_value, job_id=job_id, now=None)
    artifact = provenance_value["artifact"]
    if normalized_artifact_id != artifact["id"] or normalized_artifact_digest != artifact["digest"] or artifact_url != artifact["url"]:
        raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", "CI artifact inputs do not match the verified GitHub Actions artifact")
    ci = {
        "provider": "github-actions",
        "repository": repository,
        "sourceSha": source_sha,
        "runId": run_id,
        "runUrl": _run_url(repository, run_id),
        "jobId": job_id,
        "attempt": attempt_value,
        "status": "completed",
        "conclusion": "success",
        "artifact": artifact,
        "provenance": provenance_value,
    }
    return ci


def _without_attestation_digest(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "attestationDigest"}


def _validate_final_ci_provider(ci: dict[str, Any]) -> None:
    if ci.get("provider") != "github-actions":
        raise AttestationError("ATTESTATION_CI_PROVIDER", "final attestation authority must be GitHub Actions")


def _load_snapshot_for_attestation(
    snapshot: Any,
    *,
    source_sha: str,
    expected_run_id: str | None,
    expected_attempt: int | None,
    now: datetime | None,
) -> dict[str, Any]:
    try:
        validate_snapshot(snapshot, expected_repository=REPOSITORY, expected_source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=expected_attempt, now=now)
    except IssueStateError as exc:
        raise AttestationError(exc.code, exc.detail) from exc
    return snapshot


def _validate_live_issue_scope(snapshot: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("issues"), list):
        raise AttestationError("ISSUE_STATE_SCOPE", "final attestation has no live GitHub issue list")
    numbers = [item.get("issueNumber") for item in snapshot["issues"] if isinstance(item, dict)]
    if numbers != list(LIVE_ISSUES):
        raise AttestationError("ISSUE_STATE_SCOPE", "final issue snapshot must contain #87-#105 and #108-#113 in order")
    by_number = {
        int(item["issueNumber"]): item
        for item in snapshot["issues"]
        if isinstance(item, dict) and isinstance(item.get("issueNumber"), int)
    }
    if set(by_number) != set(LIVE_ISSUES):
        raise AttestationError("ISSUE_STATE_SCOPE", "final issue snapshot does not cover the complete live issue target")
    return by_number


def _validate_umbrella_closed_last(live_by_number: dict[int, dict[str, Any]]) -> None:
    umbrella_closed_at = live_by_number.get(UMBRELLA_ISSUE, {}).get("closedAt")
    if not isinstance(umbrella_closed_at, str):
        raise AttestationError("CLOSURE_ORDER", "umbrella issue #87 has no close timestamp")
    umbrella_time = _parse_attestation_datetime(umbrella_closed_at, field="issue #87.closedAt")
    for issue_number, issue in live_by_number.items():
        if issue_number == UMBRELLA_ISSUE:
            continue
        closed_at = issue.get("closedAt")
        if not isinstance(closed_at, str):
            raise AttestationError("ISSUE_STATE_BLOCKED", f"issue #{issue_number} has no completed close timestamp")
        if umbrella_time <= _parse_attestation_datetime(closed_at, field=f"issue #{issue_number}.closedAt"):
            raise AttestationError("CLOSURE_ORDER", "umbrella issue #87 must be closed after every child and release-barrier issue")


def build_attestation(
    bundle_manifest: Path,
    *,
    output: Path | None = None,
    source_sha: str,
    issue_snapshot: Path | None = None,
    snapshot: dict[str, Any] | None = None,
    artifact_id: str | None,
    artifact_digest: str | None,
    artifact_url: str | None,
    repo_root: Path = ROOT,
    environment: dict[str, str] | None = None,
    now: datetime | None = None,
    actions_evidence: dict[str, Any] | Path | None = None,
    upload_receipt: dict[str, Any] | Path | None = None,
    supply_chain: dict[str, Any] | Path | None = None,
) -> dict[str, Any]:
    """Create a final attestation from a passed candidate bundle."""

    source_sha = _source_sha(source_sha)
    validate_candidate_bundle(bundle_manifest, repo_root=repo_root)
    bundle, reports = _bundle_metadata(bundle_manifest, repo_root=repo_root, expected_source_sha=source_sha)
    env = environment or os.environ
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    if not actions:
        raise AttestationError("ATTESTATION_CI_PROVIDER", "final attestation authority must be GitHub Actions")
    expected_run_id = env.get("GITHUB_RUN_ID") if actions else None
    attempt_value: Any = env.get("GITHUB_RUN_ATTEMPT") if actions else None
    if isinstance(attempt_value, str) and attempt_value.isdigit():
        attempt_value = int(attempt_value)
    if not isinstance(artifact_id, str) or not artifact_id:
        raise AttestationError("ATTESTATION_ARTIFACT_MISSING", "final attestation requires an uploaded artifact ID")
    normalized_artifact_id = _positive_numeric_text(artifact_id, field="artifact.id", code="ATTESTATION_ARTIFACT_ID")
    normalized_artifact_digest = _artifact_digest(artifact_digest, field="artifact.digest")
    if not isinstance(artifact_url, str) or not artifact_url:
        raise AttestationError("ATTESTATION_ARTIFACT_URL", "final attestation requires an artifact URL")
    generated_at = _now(now)
    evidence_source = actions_evidence if actions_evidence is not None else env.get("EVIDENCE_PATH")
    evidence = _load_object(evidence_source, missing_code="ATTESTATION_PROVENANCE_MISSING", label="GitHub Actions release evidence")
    receipt_source: Any = upload_receipt
    if receipt_source is None:
        receipt_source = _discover_upload_receipt(bundle_manifest, env)
    receipt = _load_object(receipt_source, missing_code="ATTESTATION_UPLOAD_RECEIPT_MISSING", label="qualification upload receipt")
    provenance = _validate_actions_evidence(
        evidence,
        repository=REPOSITORY,
        source_sha=source_sha,
        run_id=_positive_numeric_text(expected_run_id, field="GITHUB_RUN_ID", code="ATTESTATION_RUN_ID"),
        attempt=_attempt(attempt_value, field="GITHUB_RUN_ATTEMPT"),
        job_id=_non_synthetic_text(env.get("GITHUB_JOB"), field="GITHUB_JOB", code="ATTESTATION_JOB"),
        artifact_id=normalized_artifact_id,
        artifact_digest=normalized_artifact_digest,
        artifact_url=artifact_url,
        upload_receipt=receipt,
        now=generated_at,
    )
    supply_chain_source: Any = supply_chain
    if supply_chain_source is None:
        supply_chain_source = evidence.get("supplyChain")
    supply_chain_value = _load_object(
        supply_chain_source,
        missing_code="ATTESTATION_SUPPLY_CHAIN_MISSING",
        label="independent supply-chain evidence",
    )
    _validate_supply_chain(
        supply_chain_value,
        source_sha=source_sha,
        run_id=_positive_numeric_text(expected_run_id, field="GITHUB_RUN_ID", code="ATTESTATION_RUN_ID"),
        attempt=_attempt(attempt_value, field="GITHUB_RUN_ATTEMPT"),
        bundle=bundle,
        artifact=provenance["artifact"],
        now=generated_at,
    )
    if snapshot is None:
        if issue_snapshot is not None:
            snapshot = load_snapshot(issue_snapshot, expected_repository=REPOSITORY, expected_source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=attempt_value, now=now)
        else:
            try:
                snapshot = fetch_live_issue_state(source_sha=source_sha, environment=env, now=now)
            except IssueStateError as exc:
                raise AttestationError(exc.code, exc.detail) from exc
    snapshot = _load_snapshot_for_attestation(snapshot, source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=attempt_value, now=now)
    live_by_number = _validate_live_issue_scope(snapshot)
    boundary = derive_release_boundary(snapshot)
    if boundary.get("releaseBlocked") is True:
        raise AttestationError("ISSUE_STATE_BLOCKED", "final attestation cannot be created while GitHub recovery issues are blocked")
    _validate_umbrella_closed_last(live_by_number)
    evidence_times: dict[int, str] = {}
    for report in reports.values():
        generated_at = report.get("generatedAt")
        if not isinstance(generated_at, str):
            raise AttestationError("BUNDLE_REPORT_TIMESTAMP", f"evidence report has no generatedAt: {report.get('evidenceId')}")
        for issue_number in report.get("issueNumbers", []):
            if isinstance(issue_number, int) and issue_number in REQUIRED_ISSUES:
                existing = evidence_times.get(issue_number)
                if existing is None or parse_datetime(generated_at, field="generatedAt") > parse_datetime(existing, field="generatedAt"):
                    evidence_times[issue_number] = generated_at
    close_blockers = evidence_close_time_blockers(snapshot, evidence_times)
    if close_blockers:
        raise AttestationError("GITHUB_ISSUE_CLOSED_BEFORE_EVIDENCE", json.dumps(close_blockers, ensure_ascii=False, sort_keys=True))
    ci = _ci_metadata(source_sha, artifact_id=artifact_id, artifact_digest=artifact_digest, artifact_url=artifact_url, environment=env, provenance=provenance)
    attestation: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "version": VERSION,
        "repository": REPOSITORY,
        "sourceSha": source_sha,
        "generatedAt": _format_datetime(generated_at),
        "phase": "final",
        "bundle": bundle,
        "issueState": snapshot,
        "releaseState": boundary,
        "ci": ci,
        "supplyChain": supply_chain_value,
        "closure": {
            "candidateIssueNumbers": list(CANDIDATE_ISSUES),
            "finalIssueNumber": 105,
            "selfReference": False,
            "selfQualifyingIssueNumbers": [],
            "attestationOutsideCandidateBundle": True,
        },
    }
    attestation["attestationDigest"] = sha256_bytes(canonical_json_bytes(attestation))
    validate_attestation(attestation, bundle_manifest_path=bundle_manifest, expected_source_sha=source_sha, repo_root=repo_root, now=generated_at, require_artifact=True)
    if output is not None:
        _write_json(output, attestation)
    return attestation


def _validate_report_metadata(bundle: dict[str, Any], bundle_manifest_path: Path | None, *, repo_root: Path, source_sha: str) -> None:
    if bundle.get("sourceSha") != source_sha or bundle.get("dirtyTree") is not False:
        raise AttestationError("ATTESTATION_BUNDLE_BINDING", "attestation bundle source or dirtyTree binding is invalid")
    if not isinstance(bundle.get("manifestPath"), str) or not bundle["manifestPath"]:
        raise AttestationError("ATTESTATION_BUNDLE_BINDING", "attestation bundle manifestPath is missing")
    for field in ("manifestDigest", "manifestFileDigest"):
        if not isinstance(bundle.get(field), str) or SHA256.fullmatch(bundle[field]) is None:
            raise AttestationError("ATTESTATION_BUNDLE_BINDING", f"attestation bundle {field} is invalid")
    ids = bundle.get("evidenceIds")
    if ids != sorted(_contract_evidence_ids()):
        raise AttestationError("ATTESTATION_BUNDLE_SCOPE", "attestation bundle evidence scope is not exactly #88-#105")
    reports = bundle.get("reports")
    if not isinstance(reports, list) or len(reports) != len(REQUIRED_ISSUES):
        raise AttestationError("ATTESTATION_BUNDLE_SCOPE", "attestation report digest catalog is incomplete")
    seen: set[str] = set()
    actual_root: Path | None = None
    if bundle_manifest_path is not None:
        actual_root = bundle_manifest_path.resolve().parent
        manifest = _load_json(bundle_manifest_path.resolve())
        if not isinstance(manifest, dict):
            raise AttestationError("BUNDLE_MANIFEST_SCHEMA", "bound bundle manifest is not an object")
        if _manifest_digest(manifest) != bundle["manifestDigest"]:
            raise AttestationError("ATTESTATION_BUNDLE_DIGEST_MISMATCH", "attestation manifestDigest does not match the bound bundle")
        if sha256_file(bundle_manifest_path.resolve()) != bundle["manifestFileDigest"]:
            raise AttestationError("ATTESTATION_BUNDLE_DIGEST_MISMATCH", "attestation manifestFileDigest does not match the bound bundle")
    for record in reports:
        if not isinstance(record, dict):
            raise AttestationError("ATTESTATION_REPORT_DIGEST", "attestation report catalog contains a malformed entry")
        evidence_id = record.get("evidenceId")
        if not isinstance(evidence_id, str) or evidence_id in seen:
            raise AttestationError("ATTESTATION_REPORT_DIGEST", "attestation report catalog has a duplicate or invalid evidence ID")
        if evidence_id not in _contract_evidence_ids() or record.get("sourceSha") != source_sha or record.get("status") != "passed":
            raise AttestationError("ATTESTATION_REPORT_DIGEST", f"attestation report binding is invalid: {evidence_id}")
        if not isinstance(record.get("sha256"), str) or SHA256.fullmatch(record["sha256"]) is None:
            raise AttestationError("ATTESTATION_REPORT_DIGEST", f"report digest is invalid: {evidence_id}")
        if actual_root is not None:
            target = actual_root / "reports" / f"{evidence_id}.json"
            if sha256_file(target) != record["sha256"]:
                raise AttestationError("ATTESTATION_REPORT_DIGEST_MISMATCH", f"report digest does not match bundle: {evidence_id}")
        seen.add(evidence_id)
    if seen != _contract_evidence_ids():
        raise AttestationError("ATTESTATION_REPORT_DIGEST", "attestation reports do not cover the qualification contract")


def validate_attestation(
    attestation: Any,
    *,
    bundle_manifest_path: Path | None = None,
    expected_source_sha: str | None = None,
    expected_repository: str = REPOSITORY,
    expected_run_id: str | None = None,
    expected_attempt: int | None = None,
    repo_root: Path = ROOT,
    now: datetime | None = None,
    require_artifact: bool = True,
) -> dict[str, Any]:
    """Verify a final attestation and return its verified release state."""

    if not isinstance(attestation, dict):
        raise AttestationError("ATTESTATION_ROOT", "attestation root must be an object")
    if attestation.get("schema") != SCHEMA_NAME or attestation.get("version") != VERSION:
        raise AttestationError("ATTESTATION_SCHEMA", "attestation schema/version is invalid")
    repository = attestation.get("repository")
    if repository != expected_repository:
        raise AttestationError("ATTESTATION_REPOSITORY_MISMATCH", f"attestation repository {repository!r} does not match {expected_repository!r}")
    source_sha = _source_sha(attestation.get("sourceSha"))
    if expected_source_sha is not None and source_sha != _source_sha(expected_source_sha):
        raise AttestationError("ATTESTATION_SHA_MISMATCH", "attestation source SHA does not match the inspected checkout")
    generated_at = parse_datetime(attestation.get("generatedAt"), field="attestation.generatedAt")
    digest = attestation.get("attestationDigest")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise AttestationError("ATTESTATION_DIGEST", "attestationDigest is invalid")
    if digest != sha256_bytes(canonical_json_bytes(_without_attestation_digest(attestation))):
        raise AttestationError("ATTESTATION_DIGEST_MISMATCH", "attestationDigest does not match attestation content")
    if attestation.get("phase") != "final":
        raise AttestationError("ATTESTATION_PHASE", "only final attestations are release authority")
    bundle = attestation.get("bundle")
    if not isinstance(bundle, dict):
        raise AttestationError("ATTESTATION_BUNDLE_BINDING", "attestation has no bundle binding")
    _validate_report_metadata(bundle, bundle_manifest_path, repo_root=repo_root, source_sha=source_sha)
    snapshot = attestation.get("issueState")
    try:
        validate_snapshot(snapshot, expected_repository=expected_repository, expected_source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=expected_attempt, now=now)
    except IssueStateError as exc:
        raise AttestationError(exc.code, exc.detail) from exc
    live_by_number = _validate_live_issue_scope(snapshot)
    snapshot_ci = snapshot.get("ci") if isinstance(snapshot, dict) else None
    ci = attestation.get("ci")
    if not isinstance(snapshot_ci, dict) or not isinstance(ci, dict):
        raise AttestationError("ATTESTATION_CI_BINDING", "attestation and issue-state CI bindings are required")
    _validate_final_ci_provider(ci)
    if snapshot_ci.get("provider") != "github-actions":
        raise AttestationError("ATTESTATION_CI_PROVIDER", "issue-state snapshot for final evidence must come from GitHub Actions")
    if snapshot_ci.get("repository") != expected_repository or snapshot_ci.get("sourceSha") != source_sha:
        raise AttestationError("ATTESTATION_CI_BINDING", "issue-state CI binding does not match attestation")
    snapshot_run_id = _positive_numeric_text(snapshot_ci.get("runId"), field="issueState.ci.runId", code="ATTESTATION_RUN_ID")
    snapshot_attempt = _attempt(snapshot_ci.get("attempt"), field="issueState.ci.attempt")
    snapshot_job = _non_synthetic_text(snapshot_ci.get("job"), field="issueState.ci.job", code="ATTESTATION_JOB")
    if snapshot_run_id != ci.get("runId") or snapshot_attempt != ci.get("attempt") or snapshot_job != ci.get("jobId"):
        raise AttestationError("ATTESTATION_CI_BINDING", "issue-state and attestation run identity/attempt differ")
    boundary = derive_release_boundary(snapshot)
    release_state = attestation.get("releaseState")
    if not isinstance(release_state, dict) or release_state.get("releaseBlocked") is not False or release_state.get("status") != "release-ready":
        raise AttestationError("ATTESTATION_RELEASE_STATE", "final attestation is not release-ready")
    if release_state.get("blockingIssues") != boundary.get("blockingIssues") or release_state.get("snapshotDigest") != snapshot.get("snapshotDigest"):
        raise AttestationError("ATTESTATION_RELEASE_STATE_CONTRADICTION", "attestation release state is not derived from issue state")
    _validate_umbrella_closed_last(live_by_number)
    if generated_at < parse_datetime(snapshot.get("retrievedAt"), field="issueState.retrievedAt"):
        raise AttestationError("ATTESTATION_TIME_ORDER", "attestation predates the issue-state retrieval")
    closure = attestation.get("closure")
    if not isinstance(closure, dict) or closure.get("candidateIssueNumbers") != list(CANDIDATE_ISSUES) or closure.get("finalIssueNumber") != 105 or closure.get("selfReference") is not False or closure.get("selfQualifyingIssueNumbers") != [] or closure.get("attestationOutsideCandidateBundle") is not True:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "final attestation has a circular or incomplete #105 closure")
    if ci.get("repository") != expected_repository or ci.get("sourceSha") != source_sha or ci.get("status") != "completed" or ci.get("conclusion") != "success":
        raise AttestationError("ATTESTATION_CI_BINDING", "attestation CI binding is incomplete or inconsistent")
    run_id = _positive_numeric_text(ci.get("runId"), field="ci.runId", code="ATTESTATION_RUN_ID")
    attempt = _attempt(ci.get("attempt"), field="ci.attempt")
    if ci.get("runUrl") != _run_url(expected_repository, run_id):
        raise AttestationError("ATTESTATION_RUN_URL", "attestation CI run URL is not bound to the selected GitHub run")
    job_id = _non_synthetic_text(ci.get("jobId"), field="ci.jobId", code="ATTESTATION_JOB")
    if job_id != QUALIFICATION_JOB_PROFILE:
        raise AttestationError("ATTESTATION_JOB_MISMATCH", "attestation CI job is not the qualification job")
    if expected_run_id is not None and ci.get("runId") != expected_run_id:
        raise AttestationError("ATTESTATION_RUN_ID_MISMATCH", "attestation CI run ID does not match the current run")
    if expected_attempt is not None and attempt != expected_attempt:
        raise AttestationError("ATTESTATION_ATTEMPT_MISMATCH", "attestation CI attempt does not match the current run")
    provenance = _validate_ci_provenance(ci.get("provenance"), repository=expected_repository, source_sha=source_sha, run_id=run_id, attempt=attempt, job_id=job_id, now=generated_at)
    _validate_supply_chain(
        attestation.get("supplyChain"),
        source_sha=source_sha,
        run_id=run_id,
        attempt=attempt,
        bundle=bundle,
        artifact=provenance["artifact"],
        now=generated_at,
    )
    artifact = ci.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("status") != "uploaded":
        raise AttestationError("ATTESTATION_ARTIFACT_NOT_UPLOADED", "final attestation requires an uploaded qualification artifact")
    provenance_artifact = provenance["artifact"]
    for field in ("id", "name", "digest", "sha256", "url", "sizeInBytes", "expired", "expiresAt", "producerJob", "producerStep"):
        if artifact.get(field) != provenance_artifact.get(field):
            raise AttestationError("ATTESTATION_ARTIFACT_MISMATCH", f"attestation artifact field {field!r} differs from verified GitHub Actions provenance")
    if not require_artifact:
        # Kept for callers that used the old API.  A final attestation is never
        # allowed to opt out of the uploaded-artifact and receipt checks.
        raise AttestationError("ATTESTATION_ARTIFACT_NOT_UPLOADED", "final attestation artifact validation cannot be disabled")
    return {"status": "passed", "sourceSha": source_sha, "repository": repository, "snapshot": snapshot, "releaseState": boundary, "attestationDigest": digest}


def load_and_validate_attestation(path: Path, **kwargs: Any) -> dict[str, Any]:
    return validate_attestation(_load_json(path.resolve()), **kwargs)


def _emit(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(rendered.encode("utf-8"))
    else:
        sys.stdout.write(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="candidate qualification bundle manifest")
    parser.add_argument("--attestation", type=Path, help="validate an existing final attestation")
    parser.add_argument("--out", type=Path, help="write a new final attestation")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--issue-snapshot", type=Path)
    parser.add_argument("--artifact-id")
    parser.add_argument("--artifact-digest")
    parser.add_argument("--artifact-url")
    parser.add_argument("--supply-chain", type=Path, help="independently produced package/SBOM/lock/signature/provenance evidence")
    args = parser.parse_args(argv)
    try:
        if args.attestation is not None:
            result = load_and_validate_attestation(args.attestation, bundle_manifest_path=args.bundle, expected_source_sha=args.source_sha)
            _emit({"schema": "fdir/release-attestation-result", "version": VERSION, **result})
            return 0
        if args.bundle is None or args.out is None:
            raise AttestationError("ATTESTATION_ARGUMENTS", "creating an attestation requires --bundle and --out")
        value = build_attestation(args.bundle, output=args.out, source_sha=args.source_sha, issue_snapshot=args.issue_snapshot, artifact_id=args.artifact_id, artifact_digest=args.artifact_digest, artifact_url=args.artifact_url, supply_chain=args.supply_chain)
        _emit({"schema": "fdir/release-attestation-result", "version": VERSION, "status": "passed", "attestation": value})
        return 0
    except (AttestationError, IssueStateError) as exc:
        _emit({"schema": "fdir/release-attestation-result", "version": VERSION, "status": "failed", "diagnostics": [{"code": getattr(exc, "code", "ATTESTATION_ERROR"), "detail": getattr(exc, "detail", str(exc))}]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
