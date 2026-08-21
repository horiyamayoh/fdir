"""Build a commit-bound qualification bundle for the FDIR release gates.

Every evidence definition in ``machine/qualification-contract.json`` is
executed independently.  The resulting bundle contains the exact command,
inputs, outputs, console streams, assertions, issue indexes, and canonical
manifest needed to replay and audit the qualification result.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

try:
    from qualification_evidence import (
        ARTIFACT_REFERENCE_REQUIRED_FIELDS,
        CLASSIFICATIONS,
        PRODUCER_REPORT_SCHEMA,
        PRODUCER_REPORT_OUTPUT_ROLE,
        PRODUCER_REPORT_VERSION,
        allowed_producer_assertion_types,
        is_producer_report_output,
        selected_artifact_digest,
        selected_artifact_value,
        validate_producer_report_shape,
    )
except ImportError:  # pragma: no cover - package-style imports
    from tools.qualification_evidence import (
        ARTIFACT_REFERENCE_REQUIRED_FIELDS,
        CLASSIFICATIONS,
        PRODUCER_REPORT_SCHEMA,
        PRODUCER_REPORT_OUTPUT_ROLE,
        PRODUCER_REPORT_VERSION,
        allowed_producer_assertion_types,
        is_producer_report_output,
        selected_artifact_digest,
        selected_artifact_value,
        validate_producer_report_shape,
    )


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
SCHEMA_PATH = ROOT / "schemas" / "qualification-evidence.schema.json"
REPOSITORY = "horiyamayoh/fdir"
VERSION = "1.0.0"


class BundleBuildError(RuntimeError):
    """Raised when a bundle cannot be built safely."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise BundleBuildError(f"cannot execute git: {exc}") from exc
    if completed.returncode != 0:
        raise BundleBuildError(f"git {' '.join(args)} failed: {(completed.stdout + completed.stderr).strip()}")
    return completed.stdout.strip()


def current_head() -> str:
    value = git("rev-parse", "HEAD")
    if len(value) != 40 or value.lower() != value or any(char not in "0123456789abcdef" for char in value):
        raise BundleBuildError(f"git HEAD is not a 40-character lowercase SHA: {value!r}")
    return value


def dirty_tree() -> bool:
    return bool(git("status", "--porcelain", "--untracked-files=all"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dependency_lock_digest() -> str:
    lock_path = ROOT / "requirements-qualification.txt"
    if not lock_path.is_file():
        raise BundleBuildError(f"qualification dependency lock is missing: {lock_path}")
    return sha256_bytes(b"fdir-qualification-dependencies-v1\n" + lock_path.read_bytes())


def run_qualification_command(command: list[str], timeout_seconds: int) -> tuple[int, str, str]:
    """Execute the declared argv and capture the exact console streams."""

    if not command or any(not isinstance(item, str) or not item for item in command):
        raise BundleBuildError("qualification command must be a non-empty argv array")
    argv = [sys.executable, *command[1:]] if command[0].casefold() in {"python", "python3", "py"} else list(command)
    child_environment = os.environ.copy()
    child_environment["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, stdout, stderr + f"\ncommand timed out after {timeout_seconds} seconds\n"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}\n"
    return completed.returncode, completed.stdout, completed.stderr


def _repository_relative(path_value: str) -> str:
    path = Path(path_value)
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise BundleBuildError(f"path is outside the repository: {path_value}") from exc


def repository_input(path_value: str, role: str) -> dict[str, str]:
    relative = _repository_relative(path_value)
    path = ROOT / Path(*relative.split("/"))
    if not path.is_file():
        raise BundleBuildError(f"qualification input is missing: {relative}")
    return {"path": relative, "sha256": sha256_file(path), "role": role}


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    return {
        ".json": "application/json",
        ".py": "text/x-python",
        ".yml": "text/yaml",
        ".yaml": "text/yaml",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".diff": "text/x-diff",
    }.get(suffix, "application/octet-stream")


def copy_payload(source: Path, bundle_root: Path, target: str, role: str) -> dict[str, Any]:
    if not source.is_file():
        raise BundleBuildError(f"declared qualification output is missing: {source}")
    normalized_target = target.replace("\\", "/")
    target_parts = Path(normalized_target).parts
    if not normalized_target or normalized_target.startswith("/") or ".." in target_parts or normalized_target == "manifest.json":
        raise BundleBuildError(f"declared qualification output path is unsafe: {target}")
    destination = bundle_root / Path(*target.replace("\\", "/").split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {
        "path": target.replace("\\", "/"),
        "mediaType": _media_type(target),
        "sha256": sha256_file(destination),
        "role": role,
    }


def _artifact_reference(
    bundle_root: Path,
    path: str,
    selector: dict[str, Any],
    output_roles: dict[str, str],
    *,
    label: str,
) -> Any:
    """Resolve and digest a producer-declared artifact selector.

    This is deliberately limited to files copied as declared outputs.  A
    producer cannot point a claim at the source checkout, a log placeholder,
    or an unlisted file and have the builder package it as evidence.
    """

    if not isinstance(path, str) or not path or path not in output_roles:
        raise BundleBuildError(f"{label} references an undeclared bundle output: {path!r}")
    target = bundle_root / Path(*path.replace("\\", "/").split("/"))
    if not target.is_file():
        raise BundleBuildError(f"{label} artifact is missing: {path}")
    try:
        value = selected_artifact_value(target, selector)
        selected_digest = selected_artifact_digest(value, selector)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"{label} selector cannot be resolved: {path}: {exc}") from exc
    return {
        "path": path,
        "sha256": sha256_file(target),
        "selector": selector,
        "selectedSha256": selected_digest,
    }


def _forbidden_artifact_role(role: str) -> bool:
    lowered = role.casefold()
    return any(token in lowered for token in ("snapshot", "source", "manifest", "schema", "contract", "workflow", "stdout", "stderr", "static", "code"))


def _validate_producer_report(
    report: Any,
    *,
    evidence_id: str,
    issue_numbers: list[int],
    requirement_ids: list[str],
    source_sha: str,
    input_digests: set[str],
    bundle_root: Path,
    output_roles: dict[str, str],
) -> None:
    """Reject a producer report before it can enter the bundle.

    The builder checks the closed envelope and provenance bindings only.  It
    does not turn a command exit code, a file's existence, or a producer's
    expected/actual pair into a requirement assertion.
    """

    errors = validate_producer_report_shape(report)
    if errors:
        raise BundleBuildError("PRODUCER_REPORT_SCHEMA: " + "; ".join(errors))
    if report.get("evidenceId") != evidence_id:
        raise BundleBuildError("PRODUCER_REPORT_EVIDENCE_ID: producer report evidenceId does not match the contract")
    if set(report.get("requirementIds", [])) != set(requirement_ids):
        raise BundleBuildError("PRODUCER_REPORT_REQUIREMENTS: producer report requirementIds do not match the contract")
    if report.get("sourceSha") != source_sha:
        raise BundleBuildError("PRODUCER_REPORT_SOURCE_SHA: producer report sourceSha does not match the current HEAD")
    if set(report.get("inputDigests", [])) != input_digests:
        raise BundleBuildError("PRODUCER_REPORT_INPUT_DIGESTS: producer report does not account for the declared inputs")
    if report.get("status") != "passed":
        raise BundleBuildError("PRODUCER_REPORT_STATUS: producer report is not passed")
    if report.get("uncoveredItems") or report.get("unsupportedItems") or report.get("waivedItems"):
        raise BundleBuildError("PRODUCER_REPORT_COVERAGE: producer report contains uncovered, unsupported, or waived items")
    allowed_types = allowed_producer_assertion_types(issue_numbers)
    cases = report.get("testCases", [])
    case_ids: set[str] = set()
    classifications: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise BundleBuildError(f"PRODUCER_CASE_ENTRY: test case {index} is not an object")
        missing = ARTIFACT_REFERENCE_REQUIRED_FIELDS
        for field in ("inputArtifact", "authorityArtifact", "actualArtifact", "supportingArtifact"):
            ref = case.get(field)
            if not isinstance(ref, dict) or not missing.issubset(ref):
                raise BundleBuildError(f"PRODUCER_CASE_ARTIFACT: {field} is incomplete in case {case.get('caseId')!r}")
            resolved = _artifact_reference(bundle_root, ref["path"], ref["selector"], output_roles, label=f"case {case.get('caseId')} {field}")
            if resolved["sha256"] != ref.get("sha256") or resolved["selectedSha256"] != ref.get("selectedSha256"):
                raise BundleBuildError(f"PRODUCER_CASE_DIGEST: {field} digest is not bound in case {case.get('caseId')!r}")
        case_id = case.get("caseId")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise BundleBuildError(f"PRODUCER_CASE_ID: caseId is missing or duplicated: {case_id!r}")
        case_ids.add(case_id)
        classification = case.get("classification")
        if classification not in CLASSIFICATIONS:
            raise BundleBuildError(f"PRODUCER_CASE_CLASSIFICATION: invalid classification in case {case_id!r}")
        classifications.add(classification)
        if case.get("requirementId") not in requirement_ids:
            raise BundleBuildError(f"PRODUCER_CASE_REQUIREMENT: case {case_id!r} is not bound to this requirement")
        if case["authorityArtifact"]["path"] == case["actualArtifact"]["path"]:
            raise BundleBuildError(f"PRODUCER_CASE_SAME_ARTIFACT: authority and actual are the same artifact in case {case_id!r}")
        if case["supportingArtifact"]["path"] in {case["authorityArtifact"]["path"], case["actualArtifact"]["path"]}:
            raise BundleBuildError(f"PRODUCER_CASE_SUPPORT_SAME_ARTIFACT: support is not a separate artifact in case {case_id!r}")
        for field in ("authorityArtifact", "actualArtifact", "inputArtifact"):
            role = output_roles.get(case[field]["path"], "")
            if _forbidden_artifact_role(role):
                raise BundleBuildError(f"PRODUCER_CASE_SOURCE_SNAPSHOT: {field} uses forbidden role {role!r} in case {case_id!r}")
    if "positive" not in classifications or not ({"negative", "mutation"} & classifications):
        raise BundleBuildError("PRODUCER_CASE_COVERAGE: producer report must contain positive and negative/mutation cases")
    assertion_ids: set[str] = set()
    for index, assertion in enumerate(report.get("assertions", [])):
        if not isinstance(assertion, dict):
            raise BundleBuildError(f"PRODUCER_ASSERTION_ENTRY: assertion {index} is not an object")
        assertion_id = assertion.get("assertionId")
        if not isinstance(assertion_id, str) or not assertion_id or assertion_id in assertion_ids:
            raise BundleBuildError(f"PRODUCER_ASSERTION_ID: assertionId is missing or duplicated: {assertion_id!r}")
        assertion_ids.add(assertion_id)
        if assertion.get("assertionType") not in allowed_types:
            raise BundleBuildError(f"PRODUCER_ASSERTION_TYPE: unknown or non-semantic assertion type: {assertion.get('assertionType')!r}")
        if assertion.get("testCaseId") not in case_ids:
            raise BundleBuildError(f"PRODUCER_ASSERTION_CASE: assertion {assertion_id!r} has no producer test case")
        if assertion.get("requirementId") not in requirement_ids:
            raise BundleBuildError(f"PRODUCER_ASSERTION_REQUIREMENT: assertion {assertion_id!r} is not requirement-specific")
        if assertion.get("classification") not in CLASSIFICATIONS:
            raise BundleBuildError(f"PRODUCER_ASSERTION_CLASSIFICATION: invalid classification: {assertion_id!r}")
        for field in ("authorityArtifact", "actualArtifact", "supportingArtifact"):
            ref = assertion.get(field)
            if not isinstance(ref, dict) or not ARTIFACT_REFERENCE_REQUIRED_FIELDS.issubset(ref):
                raise BundleBuildError(f"PRODUCER_ASSERTION_ARTIFACT: {field} is incomplete in {assertion_id!r}")
            resolved = _artifact_reference(bundle_root, ref["path"], ref["selector"], output_roles, label=f"assertion {assertion_id} {field}")
            if resolved["sha256"] != ref.get("sha256") or resolved["selectedSha256"] != ref.get("selectedSha256"):
                raise BundleBuildError(f"PRODUCER_ASSERTION_DIGEST: {field} digest is not bound in {assertion_id!r}")
        if assertion["authorityArtifact"]["path"] == assertion["actualArtifact"]["path"]:
            raise BundleBuildError(f"PRODUCER_ASSERTION_SAME_ARTIFACT: authority and actual are the same artifact in {assertion_id!r}")
        if assertion["supportingArtifact"]["path"] in {assertion["authorityArtifact"]["path"], assertion["actualArtifact"]["path"]}:
            raise BundleBuildError(f"PRODUCER_ASSERTION_SUPPORT_SAME_ARTIFACT: support is not a separate artifact in {assertion_id!r}")
        for field in ("authorityArtifact", "actualArtifact"):
            role = output_roles.get(assertion[field]["path"], "")
            if _forbidden_artifact_role(role):
                raise BundleBuildError(f"PRODUCER_ASSERTION_SOURCE_SNAPSHOT: {field} uses forbidden role {role!r} in {assertion_id!r}")
        if not isinstance(assertion.get("target"), dict) or not assertion["target"]:
            raise BundleBuildError(f"PRODUCER_ASSERTION_TARGET: target is required in {assertion_id!r}")
        if not isinstance(assertion.get("diagnostic"), dict) or not assertion["diagnostic"].get("code") or not assertion["diagnostic"].get("message"):
            raise BundleBuildError(f"PRODUCER_ASSERTION_DIAGNOSTIC: diagnostic is required in {assertion_id!r}")


def _normalise_stream(value: str, empty_label: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if normalized else f"{empty_label}\n"


def _ci_metadata(source_sha: str) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS", "").casefold() == "true":
        repository = os.environ.get("GITHUB_REPOSITORY", REPOSITORY)
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        job_id = os.environ.get("GITHUB_JOB", "qualification")
        attempt_text = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        try:
            attempt = max(1, int(attempt_text))
        except ValueError:
            attempt = 1
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return {
            "provider": "github-actions",
            "repository": repository,
            "sourceSha": source_sha,
            "runId": run_id or f"actions-{source_sha[:12]}",
            "runUrl": f"{server}/{repository}/actions/runs/{run_id}" if run_id else f"{server}/{repository}/actions/runs/unknown",
            "jobId": job_id,
            "attempt": attempt,
            "status": "completed",
        }
    return {
        "provider": "local",
        "repository": REPOSITORY,
        "sourceSha": source_sha,
        "runId": f"local-{source_sha[:12]}",
        "runUrl": "local://qualification",
        "jobId": "local-qualification",
        "attempt": 1,
        "status": "completed",
    }


def _load_contract(contract_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        contract = json.loads(contract_path.resolve().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"cannot load qualification contract: {exc}") from exc
    if not isinstance(contract, dict) or contract.get("schema") != "fdir/qualification-contract" or contract.get("version") != VERSION:
        raise BundleBuildError("qualification contract schema/version is invalid")
    defaults = contract.get("defaultEvidence")
    scope = contract.get("scope")
    if not isinstance(defaults, list) or not defaults or not all(isinstance(item, dict) for item in defaults):
        raise BundleBuildError("qualification contract must contain default evidence definitions")
    if not isinstance(scope, dict):
        raise BundleBuildError("qualification contract scope is missing")
    expected_ids = set(scope.get("requiredEvidenceIds", []))
    actual_ids = [item.get("evidenceId") for item in defaults]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
        raise BundleBuildError("default evidence IDs do not exactly match qualification scope")
    return contract, defaults


def build_bundle(
    output: Path,
    source_sha: str | None = None,
    *,
    contract_path: Path = CONTRACT_PATH,
    allow_dirty: bool = False,
    allow_repository_output: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    if timeout_seconds < 1:
        raise BundleBuildError("qualification command timeout must be positive")
    contract, evidence_specs = _load_contract(contract_path)
    try:
        schema_value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"cannot load qualification Evidence schema: {exc}") from exc
    if not isinstance(schema_value, dict) or schema_value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise BundleBuildError("qualification Evidence schema is not Draft 2020-12")

    output = output.resolve()
    if output == ROOT or (ROOT in output.parents and not allow_repository_output):
        raise BundleBuildError("bundle output must not be the repository root or a repository ancestor")
    if output.exists() and any(output.iterdir()):
        raise BundleBuildError(f"refusing to overwrite a non-empty bundle directory: {output}")

    head = current_head()
    if source_sha is not None and source_sha != head:
        raise BundleBuildError(f"requested source SHA {source_sha} does not match current HEAD {head}")
    source_sha = head
    is_dirty = dirty_tree()
    if is_dirty and contract.get("sourcePolicy", {}).get("releaseEvidenceMustBeClean", True) and not allow_dirty:
        raise BundleBuildError("working tree is dirty; pass --allow-dirty only for local development")

    generated_at = utc_now()
    output.mkdir(parents=True, exist_ok=True)
    ci = _ci_metadata(source_sha)
    reports: list[dict[str, Any]] = []
    report_paths: dict[str, str] = {}
    issue_to_evidence: dict[int, list[str]] = {}

    for spec in evidence_specs:
        evidence_id = str(spec["evidenceId"])
        issue_numbers = [int(item) for item in spec.get("issueNumbers", [])]
        requirements = [str(item) for item in spec.get("requirementIds", [])]
        command = list(spec.get("command", []))
        if not issue_numbers or not requirements or not command:
            raise BundleBuildError(f"evidence definition is incomplete: {evidence_id}")

        inputs: list[dict[str, str]] = []
        for item in spec.get("inputs", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("role"), str):
                raise BundleBuildError(f"invalid input definition in {evidence_id}")
            inputs.append(repository_input(item["path"], item["role"]))
        if not inputs:
            raise BundleBuildError(f"evidence definition has no inputs: {evidence_id}")

        return_code, stdout, stderr = run_qualification_command(command, timeout_seconds)
        stdout_path = output / "logs" / f"{evidence_id}.stdout.txt"
        stderr_path = output / "logs" / f"{evidence_id}.stderr.txt"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(_normalise_stream(stdout, "(no stdout)"), encoding="utf-8", newline="\n")
        stderr_path.write_text(_normalise_stream(stderr, "(no stderr)"), encoding="utf-8", newline="\n")
        outputs: list[dict[str, Any]] = [
            {"path": f"logs/{evidence_id}.stdout.txt", "mediaType": "text/plain", "sha256": sha256_file(stdout_path), "role": "stdout"},
            {"path": f"logs/{evidence_id}.stderr.txt", "mediaType": "text/plain", "sha256": sha256_file(stderr_path), "role": "stderr"},
        ]
        missing_outputs: list[str] = []
        producer_candidates: list[str] = []
        output_roles: dict[str, str] = {
            str(item["path"]): str(item["role"])
            for item in outputs
            if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("role"), str)
        }
        for item in spec.get("outputs", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("role"), str):
                raise BundleBuildError(f"invalid output definition in {evidence_id}")
            if isinstance(item.get("sourcePath"), str):
                source = ROOT / Path(*_repository_relative(item["sourcePath"]).split("/"))
                if not source.is_file():
                    missing_outputs.append(item["sourcePath"])
                    continue
                copied_role = PRODUCER_REPORT_OUTPUT_ROLE if is_producer_report_output(item) else item["role"]
                copied = copy_payload(source, output, item["path"], copied_role)
                outputs.append(copied)
                output_roles[copied["path"]] = copied["role"]
                if copied_role == PRODUCER_REPORT_OUTPUT_ROLE:
                    producer_candidates.append(copied["path"])
                continue
            if isinstance(item.get("sourceDirectory"), str):
                source_directory = ROOT / Path(*_repository_relative(item["sourceDirectory"]).split("/"))
                if not source_directory.is_dir():
                    missing_outputs.append(item["sourceDirectory"])
                    continue
                directory_files = sorted(item for item in source_directory.rglob("*") if item.is_file())
                if not directory_files:
                    missing_outputs.append(item["sourceDirectory"])
                    continue
                target_root = item["path"].rstrip("/")
                for source in directory_files:
                    relative = source.relative_to(source_directory).as_posix()
                    copied_role = PRODUCER_REPORT_OUTPUT_ROLE if is_producer_report_output(item) else item["role"]
                    copied = copy_payload(source, output, f"{target_root}/{relative}", copied_role)
                    outputs.append(copied)
                    output_roles[copied["path"]] = copied["role"]
                    if copied_role == PRODUCER_REPORT_OUTPUT_ROLE:
                        producer_candidates.append(copied["path"])
                continue
            raise BundleBuildError(f"output definition needs sourcePath or sourceDirectory: {evidence_id}")

        if return_code != 0:
            raise BundleBuildError(f"PRODUCER_COMMAND_FAILED: {evidence_id} command exited with {return_code}")
        if missing_outputs:
            raise BundleBuildError(f"PRODUCER_OUTPUT_MISSING: {evidence_id}: {', '.join(missing_outputs)}")
        if len(producer_candidates) != 1:
            raise BundleBuildError(
                f"PRODUCER_REPORT_MISSING: {evidence_id} requires exactly one output with role producer-report; found {producer_candidates}"
            )
        producer_report_path = producer_candidates[0]
        producer_report_file = output / Path(*producer_report_path.split("/"))
        try:
            producer_report = json.loads(producer_report_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BundleBuildError(f"PRODUCER_REPORT_JSON: cannot load {producer_report_path}: {exc}") from exc
        _validate_producer_report(
            producer_report,
            evidence_id=evidence_id,
            issue_numbers=issue_numbers,
            requirement_ids=requirements,
            source_sha=source_sha,
            input_digests={item["sha256"] for item in inputs},
            bundle_root=output,
            output_roles=output_roles,
        )
        producer_digest = sha256_file(producer_report_file)
        producer_source_selector = {"kind": "json-pointer", "pointer": "/sourceSha"}
        producer_source_ref = _artifact_reference(output, producer_report_path, producer_source_selector, output_roles, label="producer report sourceSha")
        producer_whole_ref = _artifact_reference(output, producer_report_path, {"kind": "whole-file"}, output_roles, label="producer report")
        declared_output_digests = {str(item["path"]): str(item["sha256"]) for item in outputs}
        declared_output_paths = sorted(declared_output_digests)
        assertions = [
            {
                "assertionId": f"{evidence_id}:producer-report-digest",
                "assertionType": "producer-report-digest",
                "expected": producer_digest,
                "actual": producer_digest,
                "status": "passed",
                "supportingOutput": producer_whole_ref,
            },
            {
                "assertionId": f"{evidence_id}:producer-report-source-binding",
                "assertionType": "producer-report-source-binding",
                "expected": source_sha,
                "actual": producer_report["sourceSha"],
                "status": "passed",
                "supportingOutput": producer_source_ref,
            },
            {
                "assertionId": f"{evidence_id}:output-digest-binding",
                "assertionType": "output-digest-binding",
                "expected": declared_output_digests,
                "actual": declared_output_digests,
                "status": "passed",
                "supportingOutput": producer_whole_ref,
            },
            {
                "assertionId": f"{evidence_id}:manifest-completeness",
                "assertionType": "manifest-completeness",
                "expected": declared_output_paths,
                "actual": declared_output_paths,
                "status": "passed",
                "supportingOutput": producer_whole_ref,
            },
            {
                "assertionId": f"{evidence_id}:command-metadata",
                "assertionType": "command-metadata",
                "expected": command,
                "actual": command,
                "status": "passed",
                "supportingOutput": producer_whole_ref,
            },
        ]
        test_cases = [
            {
                "caseId": f"{evidence_id}:packaging",
                "caseType": "packaging",
                "inputDigest": inputs[0]["sha256"],
                "actualArtifact": producer_whole_ref,
                "expected": producer_digest,
                "actual": producer_digest,
                "comparison": {"operator": "equal"},
                "result": "passed",
            }
        ]
        assertion_failures = 0
        testcase_failures = 0
        status = "passed"
        report = {
            "schema": "fdir/qualification-evidence",
            "version": VERSION,
            "evidenceId": evidence_id,
            "issueNumbers": issue_numbers,
            "requirementIds": requirements,
            "sourceSha": source_sha,
            "dirtyTree": is_dirty,
            "generatedAt": generated_at,
            "generator": "tools/build_qualification_bundle.py",
            "command": command,
            "workingDirectory": ".",
            "environment": {
                "os": platform.platform(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "runtime": sys.implementation.name,
                "dependencyLockDigest": dependency_lock_digest(),
            },
            "inputs": inputs,
            "outputs": outputs,
            "producerReport": {
                "path": producer_report_path,
                "sha256": producer_digest,
                "schema": PRODUCER_REPORT_SCHEMA,
                "version": PRODUCER_REPORT_VERSION,
                "evidenceId": evidence_id,
                "requirementIds": requirements,
            },
            "assertions": assertions,
            "testCases": test_cases,
            "status": status,
            "failureCount": assertion_failures + testcase_failures,
            "waivers": [],
            "ci": dict(ci),
        }
        report_path = output / "reports" / f"{evidence_id}.json"
        write_json(report_path, report)
        reports.append(report)
        report_paths[evidence_id] = f"reports/{evidence_id}.json"
        for issue_number in issue_numbers:
            issue_to_evidence.setdefault(issue_number, []).append(evidence_id)

    for issue_number in sorted(issue_to_evidence):
        issue_index = {
            "schema": "fdir/qualification-issue-index",
            "version": VERSION,
            "issueNumber": issue_number,
            "sourceSha": source_sha,
            "evidenceIds": sorted(issue_to_evidence[issue_number]),
            "reportPaths": [report_paths[item] for item in sorted(issue_to_evidence[issue_number])],
            "status": "generated",
        }
        write_json(output / "issues" / f"{issue_number}.json", issue_index)

    owners: dict[str, set[str]] = {}
    issue_owners: dict[str, set[int]] = {}
    for report in reports:
        evidence_id = str(report["evidenceId"])
        report_relative = f"reports/{evidence_id}.json"
        owners.setdefault(report_relative, set()).add(evidence_id)
        issue_owners.setdefault(report_relative, set()).update(int(item) for item in report["issueNumbers"])
        for item in report["outputs"]:
            path = str(item["path"])
            owners.setdefault(path, set()).add(evidence_id)
            issue_owners.setdefault(path, set()).update(int(value) for value in report["issueNumbers"])
    for issue_number, evidence_ids in issue_to_evidence.items():
        path = f"issues/{issue_number}.json"
        owners.setdefault(path, set()).update(evidence_ids)
        issue_owners.setdefault(path, set()).add(issue_number)

    payload_paths = sorted(
        item.relative_to(output).as_posix()
        for item in output.rglob("*")
        if item.is_file() and item.resolve() != (output / "manifest.json").resolve()
    )
    files: list[dict[str, Any]] = []
    for ordinal, relative in enumerate(payload_paths, start=1):
        target = output / Path(*relative.split("/"))
        files.append(
            {
                "path": relative,
                "size": target.stat().st_size,
                "sha256": sha256_file(target),
                "evidenceIds": sorted(owners.get(relative, set())),
                "issueNumbers": sorted(issue_owners.get(relative, set())),
                "ordinal": ordinal,
            }
        )

    evidence_ids = sorted(str(item["evidenceId"]) for item in reports)
    issue_numbers = sorted(int(item) for item in contract["scope"]["issueNumbers"])
    manifest: dict[str, Any] = {
        "schema": "fdir/qualification-bundle-manifest",
        "version": VERSION,
        "repository": REPOSITORY,
        "sourceSha": source_sha,
        "dirtyTree": is_dirty,
        "generatedAt": generated_at,
        "manifestDigest": "",
        "files": files,
        "evidenceIds": evidence_ids,
        "issueNumbers": issue_numbers,
    }
    manifest["manifestDigest"] = sha256_bytes(canonical_json({key: value for key, value in manifest.items() if key != "manifestDigest"}))
    write_json(output / "manifest.json", manifest)
    result = dict(manifest)
    result["status"] = "passed" if all(item.get("status") == "passed" for item in reports) else "failed"
    result["reportCount"] = len(reports)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    parser.add_argument("--allow-dirty", action="store_true", help="record a dirty tree for local development")
    parser.add_argument("--timeout", type=int, default=120, help="timeout per declared qualification command")
    args = parser.parse_args(argv)
    try:
        manifest = build_bundle(
            args.out,
            args.source_sha,
            contract_path=args.contract,
            allow_dirty=args.allow_dirty,
            timeout_seconds=args.timeout,
        )
    except (BundleBuildError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BUNDLE BUILD ERROR: {exc}", file=sys.stderr)
        return 1
    status = manifest.get("status", "failed")
    print(
        json.dumps(
            {
                "status": status,
                "manifest": str(args.out / "manifest.json"),
                "sourceSha": manifest["sourceSha"],
                "dirtyTree": manifest["dirtyTree"],
                "manifestDigest": manifest["manifestDigest"],
                "reportCount": manifest.get("reportCount"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
