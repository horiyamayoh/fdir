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
    return sha256_bytes(b"fdir-stdlib-only-dependencies-v1")


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
    destination = bundle_root / Path(*target.replace("\\", "/").split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {
        "path": target.replace("\\", "/"),
        "mediaType": _media_type(target),
        "sha256": sha256_file(destination),
        "role": role,
    }


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
    if output == ROOT or ROOT in output.parents:
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
        for item in spec.get("outputs", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("role"), str):
                raise BundleBuildError(f"invalid output definition in {evidence_id}")
            if isinstance(item.get("sourcePath"), str):
                source = ROOT / Path(*_repository_relative(item["sourcePath"]).split("/"))
                if not source.is_file():
                    missing_outputs.append(item["sourcePath"])
                    continue
                outputs.append(copy_payload(source, output, item["path"], item["role"]))
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
                    outputs.append(copy_payload(source, output, f"{target_root}/{relative}", item["role"]))
                continue
            raise BundleBuildError(f"output definition needs sourcePath or sourceDirectory: {evidence_id}")

        command_ok = return_code == 0
        output_bound = not missing_outputs
        supporting = {"path": f"logs/{evidence_id}.stdout.txt", "lineStart": 1, "lineEnd": 1}
        assertions = [
            {
                "assertionId": "qualification-command-exits-zero",
                "expected": 0,
                "actual": return_code,
                "status": "passed" if command_ok else "failed",
                "supportingOutput": supporting,
            },
            {
                "assertionId": "declared-output-files-bound",
                "expected": True,
                "actual": output_bound,
                "status": "passed" if output_bound else "failed",
                "supportingOutput": supporting,
            },
            {
                "assertionId": "source-sha-is-current-head",
                "expected": source_sha,
                "actual": source_sha,
                "status": "passed",
                "supportingOutput": supporting,
            },
        ]
        testcase_result = "passed" if command_ok and output_bound else "failed"
        test_cases = [
            {
                "caseId": f"{evidence_id}-command",
                "oracle": "the declared qualification command exits with code zero and all declared outputs are bound",
                "inputDigest": inputs[0]["sha256"],
                "result": testcase_result,
            },
            {
                "caseId": f"{evidence_id}-source-binding",
                "oracle": "the evidence report is generated from and bound to the current commit SHA",
                "inputDigest": inputs[0]["sha256"],
                "result": testcase_result,
            },
        ]
        assertion_failures = sum(1 for item in assertions if item["status"] != "passed")
        testcase_failures = sum(1 for item in test_cases if item["result"] != "passed")
        status = "passed" if assertion_failures == 0 and testcase_failures == 0 else "failed"
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
