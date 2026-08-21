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
REQUIRED_ISSUES = tuple(range(88, 106))
CANDIDATE_ISSUES = tuple(range(88, 105))
SOURCE_SHA_RE = SHA40


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


def _contract_evidence_ids() -> set[str]:
    contract = _load_json(QUALIFICATION_CONTRACT_PATH)
    scope = contract.get("scope") if isinstance(contract, dict) else None
    issue_numbers = scope.get("issueNumbers") if isinstance(scope, dict) else None
    evidence_ids = scope.get("requiredEvidenceIds") if isinstance(scope, dict) else None
    if issue_numbers != list(REQUIRED_ISSUES) or not isinstance(evidence_ids, list) or not evidence_ids:
        raise AttestationError("QUALIFICATION_SCOPE", "qualification contract is not exactly #88-#105")
    return {str(item) for item in evidence_ids}


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
    expected_ids = _contract_evidence_ids()
    reports_dir = bundle_root / "reports"
    if not reports_dir.is_dir():
        raise AttestationError("BUNDLE_REPORTS_MISSING", "bundle has no reports directory")
    reports: dict[str, dict[str, Any]] = {}
    for path in sorted(reports_dir.glob("*.json")):
        value = _load_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("evidenceId"), str):
            raise AttestationError("BUNDLE_REPORT_INVALID", f"invalid evidence report: {path.name}")
        evidence_id = value["evidenceId"]
        if evidence_id in reports or evidence_id not in expected_ids:
            raise AttestationError("BUNDLE_REPORT_SCOPE", f"unexpected or duplicate evidence report: {evidence_id}")
        if value.get("sourceSha") != manifest.get("sourceSha") or value.get("status") != "passed" or value.get("failureCount") != 0:
            raise AttestationError("BUNDLE_REPORT_NOT_PASSED", f"evidence report is not passed and SHA-bound: {evidence_id}")
        reports[evidence_id] = value
    if set(reports) != expected_ids:
        raise AttestationError("BUNDLE_REPORT_SCOPE", f"bundle evidence IDs do not exactly cover #88-#105: {sorted(set(expected_ids) - set(reports))}")
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


def _ci_metadata(
    source_sha: str,
    *,
    artifact_id: str | None,
    artifact_digest: str | None,
    artifact_url: str | None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = environment or os.environ
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    repository = env.get("GITHUB_REPOSITORY", REPOSITORY)
    if repository != REPOSITORY:
        raise AttestationError("ATTESTATION_REPOSITORY_MISMATCH", f"CI repository is {repository!r}, not {REPOSITORY!r}")
    declared_sha = env.get("GITHUB_SHA") if actions else source_sha
    if declared_sha != source_sha:
        raise AttestationError("ATTESTATION_SHA_MISMATCH", "CI GITHUB_SHA does not match the candidate source SHA")
    run_id = env.get("GITHUB_RUN_ID") if actions else "local"
    if not isinstance(run_id, str) or not run_id or (actions and re.fullmatch(r"[1-9][0-9]*", run_id) is None):
        raise AttestationError("ATTESTATION_RUN_ID", "CI run ID is missing or invalid")
    attempt_value: Any = env.get("GITHUB_RUN_ATTEMPT", "1")
    if isinstance(attempt_value, str) and attempt_value.isdigit():
        attempt_value = int(attempt_value)
    if isinstance(attempt_value, bool) or not isinstance(attempt_value, int) or attempt_value < 1:
        raise AttestationError("ATTESTATION_ATTEMPT", "CI run attempt must be a positive integer")
    if actions and env.get("GITHUB_JOB") is None:
        raise AttestationError("ATTESTATION_JOB", "GitHub Actions job identity is missing")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise AttestationError("ATTESTATION_ARTIFACT_MISSING", "final attestation requires an uploaded artifact ID")
    if not isinstance(artifact_digest, str) or SHA256.fullmatch(artifact_digest) is None:
        raise AttestationError("ATTESTATION_ARTIFACT_DIGEST", "final attestation requires a SHA-256 artifact digest")
    if not isinstance(artifact_url, str) or not artifact_url:
        raise AttestationError("ATTESTATION_ARTIFACT_URL", "final attestation requires an artifact URL")
    return {
        "provider": "github-actions" if actions else "local",
        "repository": repository,
        "sourceSha": source_sha,
        "runId": run_id,
        "runUrl": f"https://github.com/{repository}/actions/runs/{run_id}" if actions else "local://release-attestation",
        "jobId": env.get("GITHUB_JOB", "local"),
        "attempt": attempt_value,
        "status": "completed",
        "artifact": {"status": "uploaded", "id": artifact_id, "digest": artifact_digest, "url": artifact_url},
    }


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
) -> dict[str, Any]:
    """Create a final attestation from a passed candidate bundle."""

    source_sha = _source_sha(source_sha)
    validate_candidate_bundle(bundle_manifest, repo_root=repo_root)
    bundle, reports = _bundle_metadata(bundle_manifest, repo_root=repo_root, expected_source_sha=source_sha)
    env = environment or os.environ
    actions = env.get("GITHUB_ACTIONS", "").casefold() == "true"
    expected_run_id = env.get("GITHUB_RUN_ID") if actions else None
    attempt_value: Any = env.get("GITHUB_RUN_ATTEMPT") if actions else None
    if isinstance(attempt_value, str) and attempt_value.isdigit():
        attempt_value = int(attempt_value)
    if snapshot is None:
        if issue_snapshot is not None:
            snapshot = load_snapshot(issue_snapshot, expected_repository=REPOSITORY, expected_source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=attempt_value, now=now)
        else:
            try:
                snapshot = fetch_live_issue_state(source_sha=source_sha, environment=env, now=now)
            except IssueStateError as exc:
                raise AttestationError(exc.code, exc.detail) from exc
    snapshot = _load_snapshot_for_attestation(snapshot, source_sha=source_sha, expected_run_id=expected_run_id, expected_attempt=attempt_value, now=now)
    boundary = derive_release_boundary(snapshot)
    if boundary.get("releaseBlocked") is True:
        raise AttestationError("ISSUE_STATE_BLOCKED", "final attestation cannot be created while GitHub recovery issues are blocked")
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
    ci = _ci_metadata(source_sha, artifact_id=artifact_id, artifact_digest=artifact_digest, artifact_url=artifact_url, environment=env)
    generated_at = _now(now)
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
    snapshot_ci = snapshot.get("ci") if isinstance(snapshot, dict) else None
    ci = attestation.get("ci")
    if not isinstance(snapshot_ci, dict) or not isinstance(ci, dict):
        raise AttestationError("ATTESTATION_CI_BINDING", "attestation and issue-state CI bindings are required")
    _validate_final_ci_provider(ci)
    if snapshot_ci.get("repository") != expected_repository or snapshot_ci.get("sourceSha") != source_sha:
        raise AttestationError("ATTESTATION_CI_BINDING", "issue-state CI binding does not match attestation")
    if snapshot_ci.get("runId") != ci.get("runId") or snapshot_ci.get("attempt") != ci.get("attempt"):
        raise AttestationError("ATTESTATION_CI_BINDING", "issue-state and attestation run identity/attempt differ")
    boundary = derive_release_boundary(snapshot)
    release_state = attestation.get("releaseState")
    if not isinstance(release_state, dict) or release_state.get("releaseBlocked") is not False or release_state.get("status") != "release-ready":
        raise AttestationError("ATTESTATION_RELEASE_STATE", "final attestation is not release-ready")
    if release_state.get("blockingIssues") != boundary.get("blockingIssues") or release_state.get("snapshotDigest") != snapshot.get("snapshotDigest"):
        raise AttestationError("ATTESTATION_RELEASE_STATE_CONTRADICTION", "attestation release state is not derived from issue state")
    if generated_at < parse_datetime(snapshot.get("retrievedAt"), field="issueState.retrievedAt"):
        raise AttestationError("ATTESTATION_TIME_ORDER", "attestation predates the issue-state retrieval")
    closure = attestation.get("closure")
    if not isinstance(closure, dict) or closure.get("candidateIssueNumbers") != list(CANDIDATE_ISSUES) or closure.get("finalIssueNumber") != 105 or closure.get("selfReference") is not False or closure.get("selfQualifyingIssueNumbers") != [] or closure.get("attestationOutsideCandidateBundle") is not True:
        raise AttestationError("CIRCULAR_105_EVIDENCE", "final attestation has a circular or incomplete #105 closure")
    if ci.get("repository") != expected_repository or ci.get("sourceSha") != source_sha or ci.get("status") != "completed":
        raise AttestationError("ATTESTATION_CI_BINDING", "attestation CI binding is incomplete or inconsistent")
    attempt = ci.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise AttestationError("ATTESTATION_ATTEMPT", "attestation CI attempt is invalid")
    if expected_run_id is not None and ci.get("runId") != expected_run_id:
        raise AttestationError("ATTESTATION_RUN_ID_MISMATCH", "attestation CI run ID does not match the current run")
    if expected_attempt is not None and attempt != expected_attempt:
        raise AttestationError("ATTESTATION_ATTEMPT_MISMATCH", "attestation CI attempt does not match the current run")
    artifact = ci.get("artifact")
    if require_artifact:
        if not isinstance(artifact, dict) or artifact.get("status") != "uploaded" or not isinstance(artifact.get("id"), str) or not artifact.get("id") or not isinstance(artifact.get("url"), str) or not artifact.get("url") or not isinstance(artifact.get("digest"), str) or SHA256.fullmatch(artifact["digest"]) is None:
            raise AttestationError("ATTESTATION_ARTIFACT_NOT_UPLOADED", "final attestation requires uploaded artifact ID, URL, and digest")
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
    args = parser.parse_args(argv)
    try:
        if args.attestation is not None:
            result = load_and_validate_attestation(args.attestation, bundle_manifest_path=args.bundle, expected_source_sha=args.source_sha)
            _emit({"schema": "fdir/release-attestation-result", "version": VERSION, **result})
            return 0
        if args.bundle is None or args.out is None:
            raise AttestationError("ATTESTATION_ARGUMENTS", "creating an attestation requires --bundle and --out")
        value = build_attestation(args.bundle, output=args.out, source_sha=args.source_sha, issue_snapshot=args.issue_snapshot, artifact_id=args.artifact_id, artifact_digest=args.artifact_digest, artifact_url=args.artifact_url)
        _emit({"schema": "fdir/release-attestation-result", "version": VERSION, "status": "passed", "attestation": value})
        return 0
    except (AttestationError, IssueStateError) as exc:
        _emit({"schema": "fdir/release-attestation-result", "version": VERSION, "status": "failed", "diagnostics": [{"code": getattr(exc, "code", "ATTESTATION_ERROR"), "detail": getattr(exc, "detail", str(exc))}]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
