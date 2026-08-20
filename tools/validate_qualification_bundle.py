"""Fail-closed validation for commit-bound FDIR qualification bundles.

The validator intentionally uses only the Python standard library.  The JSON
Schema is the normative shape, while this module performs the checks that a
generic schema validator cannot express: Git binding, clean-tree policy,
recomputed file digests, assertion equality, issue resolution, CI identity,
and the canonical manifest digest.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "qualification-evidence.schema.json"
CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
EVIDENCE_SCHEMA_NAME = "fdir/qualification-evidence"
BUNDLE_SCHEMA_NAME = "fdir/qualification-bundle-manifest"
VERSION = "1.0.0"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REPOSITORY = "horiyamayoh/fdir"

SCHEMA_REQUIRED = {
    "schema",
    "version",
    "evidenceId",
    "issueNumbers",
    "requirementIds",
    "sourceSha",
    "dirtyTree",
    "generatedAt",
    "generator",
    "command",
    "workingDirectory",
    "environment",
    "inputs",
    "outputs",
    "assertions",
    "testCases",
    "status",
    "failureCount",
    "waivers",
    "ci",
}
SCHEMA_PROPERTIES = SCHEMA_REQUIRED
SCHEMA_DEFS = {
    "sha256",
    "repositoryPath",
    "environment",
    "input",
    "output",
    "supportingOutput",
    "assertion",
    "testCase",
    "waiver",
    "ci",
}


def canonical_json_bytes(value: Any) -> bytes:
    """Return the byte representation used by manifestDigest."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    """Load UTF-8 JSON, tolerating the BOM used by some Windows tools."""

    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _safe_relative(value: Any, *, allow_dot: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    if value == ".":
        return allow_dot
    parts = PurePosixPath(value).parts
    return bool(parts) and "." not in parts and ".." not in parts


def _resolve_relative(root: Path, value: str) -> Path:
    return root.joinpath(*PurePosixPath(value).parts)


def _diagnostic(code: str, detail: str, path: str | None = None) -> dict[str, str]:
    result = {"code": code, "detail": detail}
    if path:
        result["path"] = path
    return result


def _add(diagnostics: list[dict[str, str]], code: str, detail: str, path: str | None = None) -> None:
    diagnostics.append(_diagnostic(code, detail, path))


def _same_json(left: Any, right: Any) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _date_time(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _line_count(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return max(1, len(text.splitlines()))


def _git(repo_root: Path, *args: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return (127, "", str(exc))
    return (completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def git_head(repo_root: Path) -> str | None:
    code, output, _ = _git(repo_root, "rev-parse", "HEAD")
    return output if code == 0 and SHA40.fullmatch(output) else None


def git_dirty(repo_root: Path, *, exclude: Path | None = None) -> bool | None:
    code, output, _ = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return None
    if exclude is None:
        return output != ""
    try:
        excluded = exclude.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        return output != ""
    for line in output.splitlines():
        path_text = line[3:] if len(line) >= 4 else ""
        if " -> " in path_text:
            path_text = path_text.rsplit(" -> ", 1)[-1]
        if path_text.replace("\\", "/").strip("/") != excluded and not path_text.replace("\\", "/").startswith(excluded + "/"):
            return True
    return False


def validate_schema_document(schema: Any) -> list[dict[str, str]]:
    """Validate the normative schema without importing jsonschema."""

    diagnostics: list[dict[str, str]] = []
    if not isinstance(schema, dict):
        return [_diagnostic("SCHEMA_ROOT", "qualification schema root must be an object")]
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        _add(diagnostics, "SCHEMA_DRAFT", "qualification schema must pin Draft 2020-12")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        _add(diagnostics, "SCHEMA_ROOT_CLOSED", "qualification schema root must be a closed object")
    required = schema.get("required")
    if not isinstance(required, list) or set(required) != SCHEMA_REQUIRED:
        _add(diagnostics, "SCHEMA_REQUIRED_FIELDS", "qualification schema required fields do not match the Evidence contract")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != SCHEMA_PROPERTIES:
        _add(diagnostics, "SCHEMA_PROPERTIES", "qualification schema properties do not match the Evidence contract")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or not SCHEMA_DEFS.issubset(definitions):
        _add(diagnostics, "SCHEMA_DEFS", "qualification schema is missing one or more closed definitions")
    elif any(
        not isinstance(definitions[name], dict) or definitions[name].get("additionalProperties") is not False
        for name in SCHEMA_DEFS
        if name not in {"sha256", "repositoryPath"}
    ):
        _add(diagnostics, "SCHEMA_DEFS_OPEN", "qualification schema contains an open typed definition")
    if schema.get("$id") != "https://github.com/horiyamayoh/fdir/schemas/qualification-evidence.schema.json/1.0.0":
        _add(diagnostics, "SCHEMA_ID", "qualification schema id/version is not pinned")
    return diagnostics


def _validate_contract_shape(contract: Any) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if not isinstance(contract, dict):
        return [_diagnostic("CONTRACT_ROOT", "qualification contract root must be an object")]
    if contract.get("schema") != "fdir/qualification-contract" or contract.get("version") != VERSION:
        _add(diagnostics, "CONTRACT_SCHEMA", "qualification contract schema/version is invalid")
    if contract.get("repository") != REPOSITORY:
        _add(diagnostics, "CONTRACT_REPOSITORY", "qualification contract repository is not horiyamayoh/fdir")
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        _add(diagnostics, "CONTRACT_SCOPE", "qualification contract scope is missing")
    else:
        for field in ("issueNumbers", "requiredEvidenceIds", "requiredRequirementIds"):
            values = scope.get(field)
            if not isinstance(values, list) or not values:
                _add(diagnostics, "CONTRACT_SCOPE", f"qualification contract scope.{field} must be a non-empty array")
    source_policy = contract.get("sourcePolicy")
    if not isinstance(source_policy, dict) or source_policy.get("shaFormat") != "git-40-lowercase-hex":
        _add(diagnostics, "CONTRACT_SOURCE_POLICY", "qualification contract source policy is invalid")
    bundle_policy = contract.get("bundlePolicy")
    if not isinstance(bundle_policy, dict) or bundle_policy.get("manifestName") != "manifest.json":
        _add(diagnostics, "CONTRACT_BUNDLE_POLICY", "qualification contract manifest policy is invalid")
    defaults = contract.get("defaultEvidence")
    if not isinstance(defaults, list) or not defaults:
        _add(diagnostics, "CONTRACT_EVIDENCE", "qualification contract has no default evidence definition")
    negative = contract.get("negativeFixtures")
    if not isinstance(negative, list) or not negative:
        _add(diagnostics, "CONTRACT_NEGATIVE_FIXTURES", "qualification contract has no negative fixture definitions")
    return diagnostics


def _contract_evidence_spec(contract: dict[str, Any], evidence_id: str | None) -> dict[str, Any] | None:
    if not isinstance(evidence_id, str):
        return None
    for item in contract.get("defaultEvidence", []):
        if isinstance(item, dict) and item.get("evidenceId") == evidence_id:
            return item
    return None


def _validate_manifest_header(manifest: Any, diagnostics: list[dict[str, str]]) -> None:
    if not isinstance(manifest, dict):
        _add(diagnostics, "MANIFEST_ROOT", "manifest root must be an object")
        return
    if manifest.get("schema") != BUNDLE_SCHEMA_NAME or manifest.get("version") != VERSION:
        _add(diagnostics, "MANIFEST_SCHEMA", "bundle manifest schema/version is invalid")
    required = {"schema", "version", "repository", "sourceSha", "dirtyTree", "generatedAt", "manifestDigest", "files", "evidenceIds", "issueNumbers"}
    missing = sorted(required - set(manifest))
    if missing:
        _add(diagnostics, "MANIFEST_REQUIRED_FIELDS", "manifest is missing: " + ", ".join(missing))
    if manifest.get("repository") != REPOSITORY:
        _add(diagnostics, "MANIFEST_REPOSITORY", "manifest repository is invalid")
    if not SHA40.fullmatch(str(manifest.get("sourceSha", ""))):
        _add(diagnostics, "MANIFEST_SOURCE_SHA", "manifest sourceSha is not 40 lowercase hex characters")
    if not isinstance(manifest.get("dirtyTree"), bool):
        _add(diagnostics, "MANIFEST_DIRTY_TREE", "manifest dirtyTree must be boolean")
    if not _date_time(manifest.get("generatedAt")):
        _add(diagnostics, "MANIFEST_GENERATED_AT", "manifest generatedAt must be an RFC 3339 date-time")
    if not SHA256.fullmatch(str(manifest.get("manifestDigest", ""))):
        _add(diagnostics, "MANIFEST_DIGEST", "manifestDigest is not 64 lowercase hex characters")


def _validate_manifest_digest(manifest: dict[str, Any], diagnostics: list[dict[str, str]]) -> None:
    declared = manifest.get("manifestDigest")
    if not isinstance(declared, str):
        return
    normalized = deepcopy(manifest)
    normalized.pop("manifestDigest", None)
    actual = sha256_bytes(canonical_json_bytes(normalized))
    if actual != declared:
        _add(diagnostics, "MANIFEST_DIGEST_MISMATCH", f"manifestDigest declares {declared} but canonical manifest hashes to {actual}")


def _validate_file_table(bundle_root: Path, manifest: dict[str, Any], diagnostics: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        _add(diagnostics, "MANIFEST_FILES", "manifest files must be a non-empty array")
        return {}
    by_path: dict[str, dict[str, Any]] = {}
    for ordinal, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            _add(diagnostics, "MANIFEST_FILE_ENTRY", f"manifest file entry {ordinal} is not an object")
            continue
        path = entry.get("path")
        if not _safe_relative(path):
            _add(diagnostics, "MANIFEST_FILE_PATH", f"manifest file path is not safe: {path!r}")
            continue
        if path == "manifest.json":
            _add(diagnostics, "MANIFEST_SELF_ENTRY", "manifest.json is excluded from the payload file table")
        if path in by_path:
            _add(diagnostics, "DUPLICATE_MANIFEST_PATH", f"manifest lists a payload path more than once: {path}")
            continue
        by_path[path] = entry
        if not SHA256.fullmatch(str(entry.get("sha256", ""))):
            _add(diagnostics, "MANIFEST_FILE_DIGEST", f"payload digest is invalid: {path}", path)
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            _add(diagnostics, "MANIFEST_FILE_SIZE", f"payload size is invalid: {path}", path)
        for field in ("evidenceIds", "issueNumbers"):
            values = entry.get(field)
            unique = isinstance(values, list) and all(isinstance(value, (str, int)) and not isinstance(value, bool) for value in values) and len(values) == len(set(values))
            if not unique:
                _add(diagnostics, "MANIFEST_FILE_METADATA", f"payload {field} metadata is invalid: {path}", path)
        target = _resolve_relative(bundle_root, path)
        if not target.is_file():
            _add(diagnostics, "MANIFEST_FILE_MISSING", f"manifest payload is missing: {path}", path)
            continue
        if isinstance(entry.get("size"), int) and target.stat().st_size != entry["size"]:
            _add(diagnostics, "FILE_SIZE_MISMATCH", f"{path} declares size {entry['size']} but actual size is {target.stat().st_size}", path)
        actual = sha256_file(target)
        if actual != entry.get("sha256"):
            _add(diagnostics, "FILE_DIGEST_MISMATCH", f"{path} declares {entry.get('sha256')} but actual digest is {actual}", path)

    actual_paths = {
        item.relative_to(bundle_root).as_posix()
        for item in bundle_root.rglob("*")
        if item.is_file() and item.resolve() != (bundle_root / "manifest.json").resolve()
    }
    listed_paths = set(by_path)
    for path in sorted(actual_paths - listed_paths):
        _add(diagnostics, "UNLISTED_PAYLOAD", f"bundle file is not listed in manifest: {path}", path)
    for path in sorted(listed_paths - actual_paths):
        _add(diagnostics, "MANIFEST_FILE_MISSING", f"manifest lists a file that is not present: {path}", path)
    return by_path


def _validate_environment(environment: Any, diagnostics: list[dict[str, str]], report_path: str) -> None:
    if not isinstance(environment, dict):
        _add(diagnostics, "EVIDENCE_ENVIRONMENT", "environment must be an object", report_path)
        return
    extra = sorted(set(environment) - {"os", "architecture", "python", "runtime", "dependencyLockDigest"})
    if extra:
        _add(diagnostics, "EVIDENCE_ENVIRONMENT", "environment has unknown fields: " + ", ".join(extra), report_path)
    required = {"os", "architecture", "python", "runtime", "dependencyLockDigest"}
    for field in sorted(required):
        if not isinstance(environment.get(field), str) or not environment[field]:
            _add(diagnostics, "EVIDENCE_ENVIRONMENT", f"environment.{field} is required", report_path)
    if not SHA256.fullmatch(str(environment.get("dependencyLockDigest", ""))):
        _add(diagnostics, "DEPENDENCY_LOCK_DIGEST", "environment dependencyLockDigest is invalid", report_path)


def _validate_ci(ci: Any, report: dict[str, Any], contract: dict[str, Any], diagnostics: list[dict[str, str]], report_path: str) -> None:
    if not isinstance(ci, dict):
        _add(diagnostics, "CI_OBJECT", "ci must be an object", report_path)
        return
    required = {"provider", "repository", "sourceSha", "runId", "runUrl", "jobId", "attempt", "status"}
    extra = sorted(set(ci) - required)
    if extra:
        _add(diagnostics, "CI_UNKNOWN_FIELDS", "ci has unknown fields: " + ", ".join(extra), report_path)
    missing = sorted(required - set(ci))
    if missing:
        _add(diagnostics, "CI_REQUIRED_FIELDS", "ci is missing: " + ", ".join(missing), report_path)
    provider = ci.get("provider")
    allowed = contract.get("ciPolicy", {}).get("allowedProviders", ["github-actions", "local"])
    if provider not in allowed:
        _add(diagnostics, "CI_PROVIDER", f"ci provider is not allowed: {provider!r}", report_path)
    if ci.get("repository") != contract.get("repository", REPOSITORY):
        _add(diagnostics, "CI_REPOSITORY_MISMATCH", "ci repository does not match the qualification repository", report_path)
    source_sha = report.get("sourceSha")
    if not SHA40.fullmatch(str(ci.get("sourceSha", ""))):
        _add(diagnostics, "CI_SOURCE_SHA_INVALID", "ci sourceSha is invalid", report_path)
    elif ci.get("sourceSha") != source_sha:
        _add(diagnostics, "CI_SOURCE_SHA_MISMATCH", "ci sourceSha does not match the Evidence sourceSha", report_path)
    if not isinstance(ci.get("runId"), str) or not ci["runId"] or not isinstance(ci.get("jobId"), str) or not ci["jobId"]:
        _add(diagnostics, "CI_RUN_ID", "ci runId and jobId are required", report_path)
    if not isinstance(ci.get("attempt"), int) or ci["attempt"] < 1:
        _add(diagnostics, "CI_ATTEMPT", "ci attempt must be a positive integer", report_path)
    if ci.get("status") != contract.get("ciPolicy", {}).get("releaseStatus", "completed"):
        _add(diagnostics, "CI_STATUS", "ci status is not a completed release result", report_path)
    run_url = ci.get("runUrl")
    if provider == "local" and (not isinstance(run_url, str) or not run_url.startswith("local://")):
        _add(diagnostics, "CI_URL_MISMATCH", "local CI evidence must use a local:// runUrl", report_path)
    if provider == "github-actions":
        prefix = f"https://github.com/{contract.get('repository', REPOSITORY)}/actions/runs/"
        if not isinstance(run_url, str) or not run_url.startswith(prefix):
            _add(diagnostics, "CI_URL_MISMATCH", "GitHub Actions runUrl belongs to another repository or URL shape", report_path)


def _validate_evidence(
    report: Any,
    report_path: str,
    bundle_root: Path,
    repo_root: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    file_table: dict[str, dict[str, Any]],
    diagnostics: list[dict[str, str]],
    *,
    allow_dirty: bool,
) -> str | None:
    if report == {}:
        _add(diagnostics, "EVIDENCE_REPORT_EMPTY", "Evidence report is an empty object", report_path)
        return None
    if not isinstance(report, dict):
        _add(diagnostics, "EVIDENCE_ROOT", "Evidence report root must be an object", report_path)
        return None
    extra = sorted(set(report) - SCHEMA_PROPERTIES)
    if extra:
        _add(diagnostics, "EVIDENCE_UNKNOWN_FIELDS", "Evidence report has unknown fields: " + ", ".join(extra), report_path)
    missing = sorted(SCHEMA_REQUIRED - set(report))
    if missing:
        _add(diagnostics, "EVIDENCE_REQUIRED_FIELDS", "Evidence report is missing: " + ", ".join(missing), report_path)
    if report.get("schema") != EVIDENCE_SCHEMA_NAME or report.get("version") != VERSION:
        _add(diagnostics, "EVIDENCE_SCHEMA", "Evidence report schema/version is invalid", report_path)
    evidence_id = report.get("evidenceId")
    if not isinstance(evidence_id, str) or not ID.fullmatch(evidence_id):
        _add(diagnostics, "EVIDENCE_ID", "Evidence evidenceId is invalid", report_path)
        evidence_id = None
    issue_numbers = report.get("issueNumbers")
    if not isinstance(issue_numbers, list) or not issue_numbers or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in issue_numbers) or len(issue_numbers) != len(set(issue_numbers)):
        _add(diagnostics, "ISSUE_BINDING", "Evidence issueNumbers must be a non-empty unique integer array", report_path)
        issue_numbers = []
    allowed_issues = set(contract.get("scope", {}).get("issueNumbers", []))
    if allowed_issues and not set(issue_numbers).issubset(allowed_issues):
        _add(diagnostics, "ISSUE_BINDING_MISMATCH", "Evidence binds an issue outside the qualification contract", report_path)
    required_evidence_ids = set(contract.get("scope", {}).get("requiredEvidenceIds", []))
    requirement_ids = report.get("requirementIds")
    if not isinstance(requirement_ids, list) or not requirement_ids or any(not isinstance(item, str) or not ID.fullmatch(item) for item in requirement_ids) or len(requirement_ids) != len(set(requirement_ids)):
        _add(diagnostics, "REQUIREMENT_BINDING", "Evidence requirementIds must be a non-empty unique ID array", report_path)
        requirement_ids = []
    evidence_spec = _contract_evidence_spec(contract, evidence_id)
    if evidence_id in required_evidence_ids and evidence_spec is None:
        _add(diagnostics, "EVIDENCE_SPEC_MISSING", f"required Evidence ID has no default evidence definition: {evidence_id}", report_path)
    if evidence_spec is not None:
        expected_issues = set(evidence_spec.get("issueNumbers", []))
        if set(issue_numbers) != expected_issues:
            _add(diagnostics, "ISSUE_BINDING_MISMATCH", f"Evidence issueNumbers do not match its contract definition: {evidence_id}", report_path)
        expected_requirements = set(evidence_spec.get("requirementIds", []))
        if set(requirement_ids) != expected_requirements:
            _add(diagnostics, "REQUIREMENT_BINDING_MISMATCH", f"Evidence requirementIds do not match its contract definition: {evidence_id}", report_path)
    source_sha = report.get("sourceSha")
    if not SHA40.fullmatch(str(source_sha)):
        _add(diagnostics, "SOURCE_SHA_INVALID", "Evidence sourceSha is not 40 lowercase hex characters", report_path)
    elif source_sha != manifest.get("sourceSha"):
        _add(diagnostics, "SOURCE_SHA_MISMATCH", "Evidence sourceSha does not match the bundle sourceSha", report_path)
    if report.get("dirtyTree") != manifest.get("dirtyTree"):
        _add(diagnostics, "DIRTY_TREE_MISMATCH", "Evidence dirtyTree does not match the bundle dirtyTree", report_path)
    if report.get("dirtyTree") is True and contract.get("sourcePolicy", {}).get("releaseEvidenceMustBeClean", True) and not allow_dirty:
        _add(diagnostics, "DIRTY_TREE", "release Evidence cannot be generated from a dirty working tree", report_path)
    if not _date_time(report.get("generatedAt")):
        _add(diagnostics, "EVIDENCE_GENERATED_AT", "Evidence generatedAt must be an RFC 3339 date-time", report_path)
    generator = report.get("generator")
    if not _safe_relative(generator):
        _add(diagnostics, "GENERATOR_PATH", "Evidence generator must be a repository-relative path", report_path)
    elif not _resolve_relative(repo_root, generator).is_file():
        _add(diagnostics, "GENERATOR_MISSING", f"Evidence generator is missing: {generator}", report_path)
    command = report.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
        _add(diagnostics, "COMMAND_ARGV", "Evidence command must be a non-empty argv array", report_path)
    else:
        scripts = [item for item in command if item.endswith(".py") and not item.startswith("-")]
        if not scripts or any(not _safe_relative(item) or not _resolve_relative(repo_root, item).is_file() for item in scripts):
            _add(diagnostics, "COMMAND_GENERATOR_PATH", "Evidence command must name an existing repository-relative Python executable", report_path)
    working_directory = report.get("workingDirectory")
    if not _safe_relative(working_directory, allow_dot=True) or not _resolve_relative(repo_root, working_directory if isinstance(working_directory, str) else ".").is_dir():
        _add(diagnostics, "WORKING_DIRECTORY", "Evidence workingDirectory must be an existing repository-relative directory", report_path)
    _validate_environment(report.get("environment"), diagnostics, report_path)

    input_digests: set[str] = set()
    inputs = report.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        _add(diagnostics, "INPUTS_REQUIRED", "Evidence inputs must be a non-empty array", report_path)
        inputs = []
    input_paths: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict):
            _add(diagnostics, "INPUT_ENTRY", "Evidence input is not an object", report_path)
            continue
        missing = sorted({"path", "sha256", "role"} - set(item))
        if missing:
            _add(diagnostics, "INPUT_REQUIRED_FIELDS", "Evidence input is missing: " + ", ".join(missing), report_path)
        extra = sorted(set(item) - {"path", "sha256", "role"})
        if extra:
            _add(diagnostics, "INPUT_UNKNOWN_FIELDS", "Evidence input has unknown fields: " + ", ".join(extra), report_path)
        path = item.get("path")
        if not _safe_relative(path):
            _add(diagnostics, "INPUT_PATH", f"Evidence input path is not safe: {path!r}", report_path)
            continue
        if path in input_paths:
            _add(diagnostics, "DUPLICATE_INPUT", f"Evidence input is listed more than once: {path}", report_path)
        input_paths.add(path)
        target = _resolve_relative(repo_root, path)
        if not target.is_file():
            _add(diagnostics, "INPUT_MISSING", f"Evidence input is missing: {path}", report_path)
            continue
        declared = item.get("sha256")
        if not SHA256.fullmatch(str(declared)):
            _add(diagnostics, "INPUT_DIGEST_INVALID", f"Evidence input digest is invalid: {path}", report_path)
            continue
        actual = sha256_file(target)
        input_digests.add(actual)
        if declared != actual:
            _add(diagnostics, "INPUT_DIGEST_MISMATCH", f"{path} declares {declared} but actual digest is {actual}", report_path)
        if not isinstance(item.get("role"), str) or not item["role"]:
            _add(diagnostics, "INPUT_ROLE", f"Evidence input role is missing: {path}", report_path)

    output_digests: set[str] = set()
    output_paths: set[str] = set()
    outputs = report.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        _add(diagnostics, "OUTPUTS_REQUIRED", "Evidence outputs must be a non-empty array", report_path)
        outputs = []
    for item in outputs:
        if not isinstance(item, dict):
            _add(diagnostics, "OUTPUT_ENTRY", "Evidence output is not an object", report_path)
            continue
        missing = sorted({"path", "mediaType", "sha256", "role"} - set(item))
        if missing:
            _add(diagnostics, "OUTPUT_REQUIRED_FIELDS", "Evidence output is missing: " + ", ".join(missing), report_path)
        extra = sorted(set(item) - {"path", "mediaType", "sha256", "role"})
        if extra:
            _add(diagnostics, "OUTPUT_UNKNOWN_FIELDS", "Evidence output has unknown fields: " + ", ".join(extra), report_path)
        path = item.get("path")
        if not _safe_relative(path) or path == "manifest.json":
            _add(diagnostics, "OUTPUT_PATH", f"Evidence output path is not a safe bundle path: {path!r}", report_path)
            continue
        if path in output_paths:
            _add(diagnostics, "DUPLICATE_OUTPUT", f"Evidence output is listed more than once: {path}", report_path)
        output_paths.add(path)
        target = _resolve_relative(bundle_root, path)
        if not target.is_file():
            _add(diagnostics, "OUTPUT_MISSING", f"Evidence output is missing: {path}", report_path)
            continue
        declared = item.get("sha256")
        if not SHA256.fullmatch(str(declared)):
            _add(diagnostics, "OUTPUT_DIGEST_INVALID", f"Evidence output digest is invalid: {path}", report_path)
            continue
        actual = sha256_file(target)
        output_digests.add(actual)
        if declared != actual:
            _add(diagnostics, "OUTPUT_DIGEST_MISMATCH", f"{path} declares {declared} but actual digest is {actual}", report_path)
        if path not in file_table:
            _add(diagnostics, "OUTPUT_NOT_IN_MANIFEST", f"Evidence output is not listed in manifest: {path}", report_path)
        elif file_table[path].get("sha256") != actual:
            _add(diagnostics, "OUTPUT_MANIFEST_DIGEST_MISMATCH", f"manifest digest does not match Evidence output: {path}", report_path)
        if not isinstance(item.get("mediaType"), str) or not item["mediaType"] or not isinstance(item.get("role"), str) or not item["role"]:
            _add(diagnostics, "OUTPUT_METADATA", f"Evidence output mediaType and role are required: {path}", report_path)

    assertions = report.get("assertions")
    assertion_failures = 0
    if not isinstance(assertions, list) or not assertions:
        _add(diagnostics, "ASSERTIONS_REQUIRED", "Evidence assertions must be a non-empty array", report_path)
        assertions = []
    assertion_ids: set[str] = set()
    for item in assertions:
        if not isinstance(item, dict):
            _add(diagnostics, "ASSERTION_ENTRY", "Evidence assertion is not an object", report_path)
            assertion_failures += 1
            continue
        missing = sorted({"assertionId", "expected", "actual", "status", "supportingOutput"} - set(item))
        if missing:
            _add(diagnostics, "ASSERTION_REQUIRED_FIELDS", "Evidence assertion is missing: " + ", ".join(missing), report_path)
            assertion_failures += len(missing)
        extra = sorted(set(item) - {"assertionId", "expected", "actual", "status", "supportingOutput"})
        if extra:
            _add(diagnostics, "ASSERTION_UNKNOWN_FIELDS", "Evidence assertion has unknown fields: " + ", ".join(extra), report_path)
            assertion_failures += 1
        assertion_id = item.get("assertionId")
        if not isinstance(assertion_id, str) or not ID.fullmatch(assertion_id):
            _add(diagnostics, "ASSERTION_ID", "Evidence assertionId is invalid", report_path)
        elif assertion_id in assertion_ids:
            _add(diagnostics, "DUPLICATE_ASSERTION_ID", f"assertionId is repeated: {assertion_id}", report_path)
        assertion_ids.add(str(assertion_id))
        if not _same_json(item.get("expected"), item.get("actual")):
            _add(diagnostics, "ASSERTION_MISMATCH", f"assertion does not match expected value: {assertion_id}", report_path)
            assertion_failures += 1
        if item.get("status") != "passed":
            _add(diagnostics, "ASSERTION_FAILED", f"assertion is not passed: {assertion_id}", report_path)
            assertion_failures += 1
        supporting = item.get("supportingOutput")
        if not isinstance(supporting, dict):
            _add(diagnostics, "ASSERTION_SUPPORT", f"assertion has no supporting output: {assertion_id}", report_path)
            assertion_failures += 1
            continue
        support_path = supporting.get("path")
        if support_path not in output_paths:
            _add(diagnostics, "ASSERTION_SUPPORT", f"assertion support is not a declared output: {support_path}", report_path)
            assertion_failures += 1
            continue
        support_file = _resolve_relative(bundle_root, support_path)
        start, end = supporting.get("lineStart"), supporting.get("lineEnd")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > _line_count(support_file):
            _add(diagnostics, "ASSERTION_OUTPUT_RANGE", f"assertion support range is invalid: {support_path}", report_path)
            assertion_failures += 1

    test_cases = report.get("testCases")
    testcase_failures = 0
    if not isinstance(test_cases, list) or not test_cases:
        _add(diagnostics, "TEST_CASES_REQUIRED", "Evidence testCases must be a non-empty array", report_path)
        test_cases = []
    testcase_ids: set[str] = set()
    for item in test_cases:
        if not isinstance(item, dict):
            _add(diagnostics, "TEST_CASE_ENTRY", "Evidence testCase is not an object", report_path)
            testcase_failures += 1
            continue
        missing = sorted({"caseId", "oracle", "inputDigest", "result"} - set(item))
        if missing:
            _add(diagnostics, "TEST_CASE_REQUIRED_FIELDS", "Evidence testCase is missing: " + ", ".join(missing), report_path)
            testcase_failures += len(missing)
        extra = sorted(set(item) - {"caseId", "oracle", "inputDigest", "result"})
        if extra:
            _add(diagnostics, "TEST_CASE_UNKNOWN_FIELDS", "Evidence testCase has unknown fields: " + ", ".join(extra), report_path)
            testcase_failures += 1
        case_id = item.get("caseId")
        if not isinstance(case_id, str) or not ID.fullmatch(case_id):
            _add(diagnostics, "TEST_CASE_ID", "testCase caseId is invalid", report_path)
        elif case_id in testcase_ids:
            _add(diagnostics, "DUPLICATE_TEST_CASE_ID", f"caseId is repeated: {case_id}", report_path)
        testcase_ids.add(str(case_id))
        if not isinstance(item.get("oracle"), str) or not item["oracle"]:
            _add(diagnostics, "TEST_CASE_ORACLE", f"testCase oracle is missing: {case_id}", report_path)
        digest = item.get("inputDigest")
        if not SHA256.fullmatch(str(digest)):
            _add(diagnostics, "TEST_CASE_INPUT_DIGEST", f"testCase inputDigest is invalid: {case_id}", report_path)
        elif digest not in input_digests:
            _add(diagnostics, "TEST_CASE_INPUT_DIGEST_MISMATCH", f"testCase inputDigest is not one of the recomputed input digests: {case_id}", report_path)
        if item.get("result") != "passed":
            _add(diagnostics, "TEST_CASE_FAILED", f"testCase is not passed: {case_id}", report_path)
            testcase_failures += 1

    waivers = report.get("waivers")
    if not isinstance(waivers, list):
        _add(diagnostics, "WAIVERS", "waivers must be an array", report_path)
    else:
        if waivers and not contract.get("policies", {}).get("waiversAllowed", False):
            _add(diagnostics, "WAIVER_NOT_ALLOWED", "qualification contract does not permit release waivers", report_path)
        for waiver in waivers:
            if not isinstance(waiver, dict) or {"waiverId", "reason", "approvedBy"} - set(waiver) or set(waiver) - {"waiverId", "reason", "approvedBy"}:
                _add(diagnostics, "WAIVER_FIELDS", "waiver must contain exactly waiverId, reason, and approvedBy", report_path)
    expected_failure_count = assertion_failures + testcase_failures
    if not isinstance(report.get("failureCount"), int) or report.get("failureCount") != expected_failure_count:
        _add(diagnostics, "FAILURE_COUNT", f"failureCount is {report.get('failureCount')!r}; recomputed count is {expected_failure_count}", report_path)
    if report.get("status") != "passed" or report.get("failureCount") != 0:
        _add(diagnostics, "EVIDENCE_STATUS", "release Evidence must be passed with zero failures", report_path)
    _validate_ci(report.get("ci"), report, contract, diagnostics, report_path)
    return evidence_id


def _validate_issue_indexes(
    bundle_root: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    report_by_id: dict[str, tuple[str, dict[str, Any]]],
    diagnostics: list[dict[str, str]],
) -> None:
    issue_files = sorted(item for item in bundle_root.glob("issues/*.json") if item.is_file())
    expected_issues = set(contract.get("scope", {}).get("issueNumbers", []))
    seen: set[int] = set()
    for path in issue_files:
        try:
            issue_number = int(path.stem)
        except ValueError:
            _add(diagnostics, "ISSUE_INDEX_PATH", f"issue index filename is not numeric: {path.name}", path.relative_to(bundle_root).as_posix())
            continue
        if issue_number in seen:
            _add(diagnostics, "DUPLICATE_ISSUE_INDEX", f"issue index is repeated: {issue_number}")
        seen.add(issue_number)
        try:
            index = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            _add(diagnostics, "ISSUE_INDEX_JSON", f"cannot load issue index {path.name}: {exc}")
            continue
        if not isinstance(index, dict) or index.get("schema") != "fdir/qualification-issue-index" or index.get("issueNumber") != issue_number:
            _add(diagnostics, "ISSUE_INDEX_SCHEMA", f"issue index is malformed: {path.name}")
            continue
        if index.get("sourceSha") != manifest.get("sourceSha"):
            _add(diagnostics, "ISSUE_INDEX_SOURCE_SHA", f"issue index sourceSha mismatch: {path.name}")
        ids = index.get("evidenceIds")
        if not isinstance(ids, list) or len(ids) != len(set(ids)):
            _add(diagnostics, "ISSUE_INDEX_EVIDENCE", f"issue index evidenceIds are invalid: {path.name}")
            ids = []
        expected_ids = {evidence_id for evidence_id, (_, report) in report_by_id.items() if issue_number in report.get("issueNumbers", [])}
        if set(ids) != expected_ids:
            _add(diagnostics, "ISSUE_INDEX_BINDING", f"issue index evidenceIds do not resolve to reports: {path.name}")
        if not all(item in report_by_id for item in ids):
            _add(diagnostics, "UNRESOLVED_EVIDENCE_ID", f"issue index references an unknown Evidence ID: {path.name}")
    if seen != expected_issues:
        _add(diagnostics, "ISSUE_INDEX_SCOPE", f"issue indexes {sorted(seen)} do not match contract issues {sorted(expected_issues)}")


def validate_bundle(
    manifest_path: Path,
    *,
    repo_root: Path = ROOT,
    contract_path: Path = CONTRACT_PATH,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    diagnostics: list[dict[str, str]] = []
    manifest_path = manifest_path.resolve()
    bundle_root = manifest_path.parent
    try:
        schema = load_json(repo_root / "schemas" / "qualification-evidence.schema.json")
    except (OSError, json.JSONDecodeError) as exc:
        _add(diagnostics, "SCHEMA_LOAD", f"cannot load qualification schema: {exc}")
        schema = None
    if schema is not None:
        diagnostics.extend(validate_schema_document(schema))
    try:
        contract = load_json(contract_path.resolve())
    except (OSError, json.JSONDecodeError) as exc:
        _add(diagnostics, "CONTRACT_LOAD", f"cannot load qualification contract: {exc}")
        contract = {}
    diagnostics.extend(_validate_contract_shape(contract))
    try:
        manifest = load_json(manifest_path)
    except FileNotFoundError:
        _add(diagnostics, "MANIFEST_MISSING", f"manifest does not exist: {manifest_path}")
        return _result(manifest_path, None, diagnostics)
    except (OSError, json.JSONDecodeError) as exc:
        _add(diagnostics, "MANIFEST_JSON", f"cannot load manifest: {exc}")
        return _result(manifest_path, None, diagnostics)
    _validate_manifest_header(manifest, diagnostics)
    if not isinstance(manifest, dict):
        return _result(manifest_path, None, diagnostics)
    _validate_manifest_digest(manifest, diagnostics)
    file_table = _validate_file_table(bundle_root, manifest, diagnostics)

    actual_head = git_head(repo_root)
    if actual_head is None:
        _add(diagnostics, "GIT_HEAD_UNAVAILABLE", "cannot resolve git HEAD")
    elif manifest.get("sourceSha") != actual_head:
        _add(diagnostics, "SOURCE_SHA_MISMATCH", f"bundle sourceSha {manifest.get('sourceSha')} is not current HEAD {actual_head}")
    actual_dirty = git_dirty(repo_root, exclude=bundle_root)
    if actual_dirty is None:
        _add(diagnostics, "GIT_STATUS_UNAVAILABLE", "cannot determine working-tree dirtiness")
    elif manifest.get("dirtyTree") != actual_dirty:
        _add(diagnostics, "DIRTY_TREE_MISMATCH", f"bundle dirtyTree {manifest.get('dirtyTree')} does not match current working tree {actual_dirty}")
    if manifest.get("dirtyTree") is True and contract.get("sourcePolicy", {}).get("releaseEvidenceMustBeClean", True) and not allow_dirty:
        _add(diagnostics, "DIRTY_TREE", "release bundle is bound to a dirty working tree")

    report_paths = sorted(path for path in bundle_root.glob("reports/*.json") if path.is_file())
    if not report_paths:
        _add(diagnostics, "REPORTS_MISSING", "bundle has no reports/*.json files")
    report_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in report_paths:
        relative = path.relative_to(bundle_root).as_posix()
        try:
            report = load_json(path)
        except FileNotFoundError:
            _add(diagnostics, "EVIDENCE_REPORT_MISSING", f"Evidence report is missing: {relative}", relative)
            continue
        except (OSError, json.JSONDecodeError) as exc:
            _add(diagnostics, "EVIDENCE_REPORT_EMPTY" if path.stat().st_size == 0 else "EVIDENCE_REPORT_JSON", f"cannot load Evidence report {relative}: {exc}", relative)
            continue
        evidence_id = _validate_evidence(report, relative, bundle_root, repo_root, manifest, contract, file_table, diagnostics, allow_dirty=allow_dirty)
        if evidence_id is not None:
            if evidence_id in report_by_id:
                _add(diagnostics, "DUPLICATE_EVIDENCE_ID", f"Evidence ID is defined by more than one report: {evidence_id}", relative)
            else:
                report_by_id[evidence_id] = (relative, report)
            entry = file_table.get(relative)
            if entry is None:
                _add(diagnostics, "REPORT_NOT_IN_MANIFEST", f"Evidence report is not listed in manifest: {relative}", relative)
            elif evidence_id not in entry.get("evidenceIds", []):
                _add(diagnostics, "REPORT_MANIFEST_BINDING", f"manifest does not bind report Evidence ID {evidence_id}: {relative}", relative)

    actual_ids = set(report_by_id)
    declared_ids = set(manifest.get("evidenceIds", [])) if isinstance(manifest.get("evidenceIds"), list) else set()
    required_ids = set(contract.get("scope", {}).get("requiredEvidenceIds", []))
    if actual_ids != declared_ids:
        _add(diagnostics, "MANIFEST_EVIDENCE_IDS", f"manifest evidenceIds {sorted(declared_ids)} do not match report IDs {sorted(actual_ids)}")
    if not required_ids.issubset(actual_ids):
        _add(diagnostics, "UNRESOLVED_EVIDENCE_ID", f"required Evidence IDs are missing: {sorted(required_ids - actual_ids)}")
    expected_issue_numbers = set(contract.get("scope", {}).get("issueNumbers", []))
    declared_issue_numbers = set(manifest.get("issueNumbers", [])) if isinstance(manifest.get("issueNumbers"), list) else set()
    if declared_issue_numbers != expected_issue_numbers:
        _add(diagnostics, "MANIFEST_ISSUE_SCOPE", f"manifest issueNumbers {sorted(declared_issue_numbers)} do not match contract {sorted(expected_issue_numbers)}")
    _validate_issue_indexes(bundle_root, manifest, contract, report_by_id, diagnostics)
    return _result(manifest_path, manifest, diagnostics, report_by_id)


def _result(
    manifest_path: Path,
    manifest: dict[str, Any] | None,
    diagnostics: list[dict[str, str]],
    report_by_id: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in diagnostics:
        key = (item.get("code", ""), item.get("detail", ""), item.get("path", ""))
        if key not in seen:
            unique.append(item)
            seen.add(key)
    result: dict[str, Any] = {
        "schema": "fdir/qualification-validation-report",
        "version": VERSION,
        "status": "passed" if not unique else "failed",
        "manifest": str(manifest_path),
        "diagnostics": unique,
    }
    if isinstance(manifest, dict):
        result["sourceSha"] = manifest.get("sourceSha")
        result["manifestDigest"] = manifest.get("manifestDigest")
    if report_by_id is not None:
        result["evidenceIds"] = sorted(report_by_id)
    return result


def _emit(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(rendered.encode("utf-8"))
    else:
        sys.stdout.write(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a commit-bound FDIR qualification bundle.")
    parser.add_argument("manifest", nargs="?", type=Path, help="path to qualification bundle manifest.json")
    parser.add_argument("--repo-root", type=Path, default=ROOT, help="repository root used for Git and input resolution")
    parser.add_argument("--contract", type=Path, default=None, help="qualification contract JSON path")
    parser.add_argument("--allow-dirty", action="store_true", help="permit dirty-tree evidence for local development; still verify its value")
    parser.add_argument("--schema-only", action="store_true", help="validate only the Draft 2020-12 Evidence schema")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.schema_only:
        try:
            schema = load_json(repo_root / "schemas" / "qualification-evidence.schema.json")
        except (OSError, json.JSONDecodeError) as exc:
            result = {"schema": "fdir/qualification-schema-report", "version": VERSION, "status": "failed", "diagnostics": [_diagnostic("SCHEMA_LOAD", str(exc))]}
        else:
            diagnostics = validate_schema_document(schema)
            result = {"schema": "fdir/qualification-schema-report", "version": VERSION, "status": "passed" if not diagnostics else "failed", "diagnostics": diagnostics}
        _emit(result)
        return 0 if result["status"] == "passed" else 1
    if args.manifest is None:
        parser.error("manifest is required unless --schema-only is used")
    result = validate_bundle(args.manifest, repo_root=repo_root, contract_path=(args.contract.resolve() if args.contract else repo_root / "machine" / "qualification-contract.json"), allow_dirty=args.allow_dirty)
    _emit(result)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
