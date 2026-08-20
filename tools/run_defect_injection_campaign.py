"""Run source-level defect injection against disposable repository copies.

The runner deliberately treats a mutation as a small, source-managed
replacement that is rendered to a unified diff and applied with ``git apply``.
It never edits the checkout from which it was started.  A campaign is only
green when the clean base suite passes, the patched command supplies an
observable test/gate failure, and no required case is invalid, timed out, or
left undetected.

Only the Python standard library and the Git executable are used.  Commands
from the contract are argv arrays; no shell is involved.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "machine" / "defect-injection-contract.json"
RUNNER_VERSION = "1.0.0"
CLASSIFICATIONS = (
    "generated",
    "detected",
    "undetected",
    "equivalent",
    "invalid",
    "timeout",
    "infrastructure-error",
)
_PYTHON_NAMES = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}
_CRASH_MARKERS = (
    "fatal python error",
    "segmentation fault",
    "access violation",
    "stack overflow",
    "memoryerror",
    "killed by signal",
    "0xc0000005",
)


class CampaignError(RuntimeError):
    """A contract or disposable-checkout error that must fail closed."""


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    )


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot load JSON {path}: {exc}") from exc


def _read_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise CampaignError(f"cannot read UTF-8 source {path}: {exc}") from exc


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _contract_source(root: Path, path_text: str) -> str:
    """Read the committed source selector when the caller has a dirty tree."""

    try:
        result = _git_command(["show", "HEAD:" + _normalise_path(path_text)], root, 60)
    except Exception:
        result = None
    if isinstance(result, dict) and result.get("status") == "completed" and result.get("exit_code") == 0:
        return str(result.get("stdout", ""))
    return _read_text(_safe_relative(root, path_text))


def _normalise_path(value: str) -> str:
    return value.replace("\\", "/")


def _safe_relative(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CampaignError(f"path escapes checkout: {value}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise CampaignError(f"path escapes checkout: {value}") from exc
    return resolved


def _command_key(command: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in command)


def _validate_argv(command: Any, label: str) -> list[str]:
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise CampaignError(f"{label} must be a non-empty argv array")
    return list(command)


def _function_marker(source: str, function: str) -> bool:
    leaf = function.rsplit(".", 1)[-1]
    return bool(re.search(rf"^\s*def\s+{re.escape(leaf)}\s*\(", source, re.MULTILINE)) or bool(
        re.search(rf"\b{re.escape(leaf)}\b", source)
    )


def validate_contract(contract: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """Validate structure and source selectors without executing a campaign."""

    errors: list[str] = []
    warnings: list[str] = []
    if contract.get("schema") != "fdir/defect-injection-contract":
        errors.append("contract schema is not fdir/defect-injection-contract")
    if contract.get("version") != "1.0.0":
        errors.append("unsupported contract version")
    if contract.get("issue") != 89:
        errors.append("contract is not owned by Issue #89")
    policy = contract.get("policy")
    if not isinstance(policy, dict) or policy.get("commandsAreArgv") is not True or policy.get("thirdPartyDependencies") is not False:
        errors.append("contract must require argv commands and no third-party dependencies")
    selector_policy = policy.get("selectorPolicy") if isinstance(policy, dict) else None
    if not isinstance(selector_policy, dict) or selector_policy.get("kind") != "function-scoped-source-span":
        errors.append("contract must declare function-scoped source-span selectors")
    elif selector_policy.get("targetFileField") != "patch.changes[].path" or selector_policy.get("targetFunctionField") != "targetFunction" or selector_policy.get("astSelectorField") != "patch.changes[].old":
        errors.append("selector policy fields do not identify file, function, and AST/source selector")
    classifications = contract.get("classifications")
    if classifications != list(CLASSIFICATIONS):
        errors.append("classification order does not match the fail-closed report contract")
    for key in ("baseSuite", "syntaxCheck", "importCheck", "runner", "releaseProfiles", "cases", "invariantMatrix"):
        if key not in contract:
            errors.append(f"contract is missing {key}")
    support_files = contract.get("supportFiles", [])
    if not isinstance(support_files, list) or not all(isinstance(item, str) and item for item in support_files):
        errors.append("supportFiles must be an array of non-empty repository-relative paths")
    else:
        for item in support_files:
            try:
                path = _safe_relative(root, item)
            except CampaignError as exc:
                errors.append(f"supportFiles: {exc}")
                continue
            if not path.is_file():
                errors.append(f"support file is missing: {item}")
    base_suite = contract.get("baseSuite", {})
    if isinstance(base_suite, dict):
        try:
            _validate_argv(base_suite.get("command"), "baseSuite.command")
        except CampaignError as exc:
            errors.append(str(exc))
    profiles = contract.get("releaseProfiles")
    if not isinstance(profiles, dict) or not profiles:
        errors.append("releaseProfiles must be a non-empty object")
        profiles = {}
    else:
        for profile_id, profile in profiles.items():
            if not isinstance(profile, dict):
                errors.append(f"release profile is not an object: {profile_id}")
                continue
            try:
                _validate_argv(profile.get("command"), f"releaseProfiles.{profile_id}.command")
            except CampaignError as exc:
                errors.append(str(exc))
            if not isinstance(profile.get("timeoutSeconds"), (int, float)) or profile.get("timeoutSeconds", 0) <= 0:
                errors.append(f"release profile timeout is invalid: {profile_id}")
            if not isinstance(profile.get("minimumNonEquivalentCases"), int) or profile.get("minimumNonEquivalentCases", 0) < 1:
                errors.append(f"release profile minimumNonEquivalentCases is invalid: {profile_id}")

    cases = contract.get("cases")
    case_map: dict[str, dict[str, Any]] = {}
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        cases = []
    for case in cases:
        if not isinstance(case, dict):
            errors.append("case is not an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append("case has no id")
            continue
        if case_id in case_map:
            errors.append(f"duplicate case id: {case_id}")
            continue
        case_map[case_id] = case
        profile_id = case.get("releaseProfile")
        if profile_id not in profiles:
            errors.append(f"case {case_id} references an unknown release profile: {profile_id}")
        if not isinstance(case.get("detectingTestIds"), list) or not all(isinstance(item, str) for item in case.get("detectingTestIds", [])):
            errors.append(f"case {case_id} has malformed detectingTestIds")
        if case.get("expectedOutcome") == "non-equivalent" and not case.get("detectingTestIds"):
            errors.append(f"non-equivalent case {case_id} has no detecting test ID")
        patch = case.get("patch")
        if not isinstance(patch, dict) or patch.get("kind") != "replace" or not isinstance(patch.get("changes"), list) or not patch["changes"]:
            errors.append(f"case {case_id} does not declare replacement patch changes")
            continue
        seen_paths: set[str] = set()
        for change in patch["changes"]:
            if not isinstance(change, dict):
                errors.append(f"case {case_id} has a malformed patch change")
                continue
            path_text = change.get("path")
            old = change.get("old")
            new = change.get("new")
            count = change.get("count", 1)
            if not isinstance(path_text, str) or not isinstance(old, str) or not isinstance(new, str) or not isinstance(count, int) or count < 1 or old == new:
                errors.append(f"case {case_id} has an invalid replacement selector")
                continue
            if path_text in seen_paths:
                errors.append(f"case {case_id} changes one file more than once; use one combined replacement")
            seen_paths.add(path_text)
            try:
                path = _safe_relative(root, path_text)
                source = _contract_source(root, path_text)
            except CampaignError as exc:
                errors.append(f"case {case_id}: {exc}")
                continue
            occurrences = source.count(old)
            if occurrences != count:
                errors.append(f"case {case_id} selector count for {path_text} is {occurrences}, expected {count}")
            target_function = case.get("targetFunction")
            if not isinstance(target_function, str) or not target_function or not _function_marker(source, target_function):
                errors.append(f"case {case_id} targetFunction is not present in {path_text}")
        if case.get("expectedOutcome") not in {"non-equivalent", "equivalent"}:
            errors.append(f"case {case_id} has an unsupported expectedOutcome")

    matrix = contract.get("invariantMatrix")
    matrix_ids: set[str] = set()
    if not isinstance(matrix, list) or not matrix:
        errors.append("invariantMatrix must be a non-empty array")
        matrix = []
    for invariant in matrix:
        if not isinstance(invariant, dict):
            errors.append("invariant matrix entry is not an object")
            continue
        invariant_id = invariant.get("id")
        case_id = invariant.get("caseId")
        if not isinstance(invariant_id, str) or not invariant_id or invariant_id in matrix_ids:
            errors.append(f"invariant id is missing or duplicated: {invariant_id}")
        matrix_ids.add(str(invariant_id))
        if case_id not in case_map:
            errors.append(f"invariant {invariant_id} references an unknown case: {case_id}")
        if invariant.get("must") is not True:
            errors.append(f"invariant {invariant_id} is not marked must")
    missing_matrix_cases = sorted(set(case_map) - {item.get("caseId") for item in matrix if isinstance(item, dict)})
    if missing_matrix_cases:
        warnings.append("cases not owned by an invariant matrix row: " + ", ".join(missing_matrix_cases))

    profile_counts: dict[str, int] = {}
    for case in case_map.values():
        if case.get("expectedOutcome") == "non-equivalent":
            profile_counts[str(case.get("releaseProfile"))] = profile_counts.get(str(case.get("releaseProfile")), 0) + 1
    coverage_gaps: list[dict[str, int | str]] = []
    for profile_id, profile in profiles.items():
        minimum = int(profile.get("minimumNonEquivalentCases", 0)) if isinstance(profile, dict) else 0
        actual = profile_counts.get(profile_id, 0)
        if actual < minimum:
            coverage_gaps.append({"profile": profile_id, "actual": actual, "required": minimum, "missing": minimum - actual})
    if coverage_gaps:
        warnings.append("declared release profile case minimums are not yet met")

    return {
        "valid": not errors,
        "errors": sorted(errors),
        "warnings": sorted(warnings),
        "caseCount": len(case_map),
        "invariantCount": len(matrix),
        "profileCounts": {key: profile_counts[key] for key in sorted(profile_counts)},
        "coverageGaps": sorted(coverage_gaps, key=lambda item: str(item["profile"])),
    }


def _normalise_output(value: bytes | str | None, replacements: tuple[tuple[str, str], ...] = ()) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for old, new in replacements:
        if old:
            text = text.replace(old, new)
    return text


def _resolve_command(command: list[str]) -> list[str]:
    resolved = list(command)
    if resolved and Path(resolved[0]).name.casefold() in _PYTHON_NAMES:
        resolved[0] = sys.executable
        if len(resolved) > 1 and resolved[0] == sys.executable and command[0].casefold().startswith("py") and command[1] in {"-3", "-3.0"}:
            resolved.pop(1)
    return resolved


def _expand_command(command: list[str], target_files: list[str]) -> list[str]:
    expanded: list[str] = []
    for token in _validate_argv(command, "command"):
        if token == "{target_files}":
            expanded.extend(target_files)
        elif token == "{target}":
            if not target_files:
                raise CampaignError("{target} was used with no patch target")
            expanded.append(target_files[0])
        else:
            expanded.append(token)
    return expanded


def _command_environment(cwd: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    python_path = [str(cwd / "tools"), str(cwd)]
    existing = environment.get("PYTHONPATH")
    if existing:
        python_path.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def _process_result(
    command: list[str],
    resolved_command: list[str],
    return_code: int | None,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
    duration_ms: int,
    *,
    status: str,
    timed_out: bool = False,
    replacements: tuple[tuple[str, str], ...] = (),
    max_output_bytes: int = 262144,
) -> dict[str, Any]:
    stdout_text = _normalise_output(stdout, replacements)
    stderr_text = _normalise_output(stderr, replacements)
    stdout_bytes = stdout_text.encode("utf-8")
    stderr_bytes = stderr_text.encode("utf-8")
    stdout_truncated = len(stdout_bytes) > max_output_bytes
    stderr_truncated = len(stderr_bytes) > max_output_bytes
    if stdout_truncated:
        stdout_text = stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    if stderr_truncated:
        stderr_text = stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    return {
        "argv": list(command),
        "resolved_argv": [Path(item).name if index == 0 and Path(item).name.casefold() in _PYTHON_NAMES else item for index, item in enumerate(resolved_command)],
        "status": status,
        "timeout": timed_out,
        "exit_code": return_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_sha256": _digest_text(stdout_text),
        "stderr_sha256": _digest_text(stderr_text),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_ms": max(0, int(duration_ms)),
    }


def _run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
    *,
    root_for_output: Path,
    max_output_bytes: int,
) -> dict[str, Any]:
    command = _validate_argv(command, "command")
    resolved = _resolve_command(command)
    replacements = ((str(cwd.resolve()), "<checkout>"), (str(root_for_output.resolve()), "<root>"))
    started = time.monotonic_ns()
    process: subprocess.Popen[bytes] | None = None
    try:
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            resolved,
            cwd=str(cwd),
            env=_command_environment(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creation_flags,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            tail_stdout, tail_stderr = process.communicate()
            stdout = tail_stdout if tail_stdout else exc.output
            stderr = tail_stderr if tail_stderr else exc.stderr
            duration_ms = (time.monotonic_ns() - started) // 1_000_000
            return _process_result(command, resolved, None, stdout, stderr, duration_ms, status="timeout", timed_out=True, replacements=replacements, max_output_bytes=max_output_bytes)
        duration_ms = (time.monotonic_ns() - started) // 1_000_000
        return _process_result(command, resolved, process.returncode, stdout, stderr, duration_ms, status="completed", replacements=replacements, max_output_bytes=max_output_bytes)
    except (OSError, ValueError) as exc:
        duration_ms = (time.monotonic_ns() - started) // 1_000_000
        return _process_result(command, resolved, None, "", f"{type(exc).__name__}: {exc}", duration_ms, status="infrastructure-error", replacements=replacements, max_output_bytes=max_output_bytes)


def _git_command(args: list[str], cwd: Path, timeout_seconds: float = 120) -> dict[str, Any]:
    return _run_command(["git", "-c", "core.fsmonitor=false", *args], cwd, timeout_seconds, root_for_output=cwd, max_output_bytes=262144)


def _git_sha(root: Path) -> str:
    result = _git_command(["rev-parse", "HEAD"], root)
    if result["status"] != "completed" or result["exit_code"] != 0:
        raise CampaignError("cannot resolve base SHA: " + (result["stderr"] or result["stdout"]).strip())
    sha = result["stdout"].strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CampaignError(f"git rev-parse returned a non-SHA value: {sha!r}")
    return sha


def _git_dirty(root: Path) -> list[str]:
    result = _git_command(["status", "--porcelain=v1", "--untracked-files=all"], root)
    if result["status"] != "completed" or result["exit_code"] != 0:
        raise CampaignError("cannot inspect git status: " + (result["stderr"] or result["stdout"]).strip())
    return [line for line in result["stdout"].splitlines() if line]


def _create_archive(root: Path, sha: str, storage: Path) -> Path:
    archive = storage / "base.tar"
    result = _git_command(["archive", "--format=tar", "--output", str(archive), sha], root, 120)
    if result["status"] != "completed" or result["exit_code"] != 0 or not archive.is_file():
        detail = (result["stderr"] or result["stdout"]).strip()
        raise CampaignError(f"cannot create base archive: {detail}")
    return archive


def _extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    resolved_destination = destination.resolve()
    with tarfile.open(archive, mode="r") as stream:
        for member in stream.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(resolved_destination)
            except ValueError as exc:
                raise CampaignError(f"archive member escapes disposable checkout: {member.name}") from exc
            if member.issym() or member.islnk():
                raise CampaignError(f"archive contains a link, refusing unsafe extraction: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise CampaignError(f"archive contains unsupported member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = stream.extractfile(member)
            if source is None:
                raise CampaignError(f"archive member has no content: {member.name}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _overlay_support_files(root: Path, checkout: Path, support_files: Iterable[str]) -> None:
    """Copy committed-or-pending probe helpers into the disposable checkout.

    The campaign's source base remains the exact ``git archive`` SHA.  Small
    probe programs are declared explicitly in the contract and are overlaid
    only as test infrastructure; they are never patch targets and cannot
    change the product source under test.
    """

    for path_text in support_files:
        source = _safe_relative(root, path_text)
        destination = _safe_relative(checkout, path_text)
        if not source.is_file():
            raise CampaignError(f"declared support file is missing: {path_text}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _render_patch(base_checkout: Path, patch_spec: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if patch_spec.get("kind") != "replace" or not isinstance(patch_spec.get("changes"), list):
        raise CampaignError("only replace patch specifications are supported")
    before_by_path: dict[str, str] = {}
    after_by_path: dict[str, str] = {}
    details: list[dict[str, Any]] = []
    for change in patch_spec["changes"]:
        if not isinstance(change, dict):
            raise CampaignError("patch change is not an object")
        path_text = str(change.get("path", ""))
        path = _safe_relative(base_checkout, path_text)
        before = before_by_path.get(path_text)
        if before is None:
            before = _read_text(path)
        old = change.get("old")
        new = change.get("new")
        count = change.get("count", 1)
        if not isinstance(old, str) or not isinstance(new, str) or not isinstance(count, int) or count < 1:
            raise CampaignError(f"invalid replacement for {path_text}")
        occurrences = before.count(old)
        if occurrences != count:
            raise CampaignError(f"replacement selector count changed for {path_text}: {occurrences} != {count}")
        after = before.replace(old, new, count)
        before_by_path[path_text] = before
        after_by_path[path_text] = after
        details.append({
            "path": _normalise_path(path_text),
            "old_sha256": _digest_text(old),
            "new_sha256": _digest_text(new),
            "before_file_sha256": _digest_text(before),
            "after_file_sha256": _digest_text(after),
            "occurrences": count,
        })
    chunks: list[str] = []
    for path_text in sorted(before_by_path):
        diff = difflib.unified_diff(
            before_by_path[path_text].splitlines(keepends=True),
            after_by_path[path_text].splitlines(keepends=True),
            fromfile="a/" + _normalise_path(path_text),
            tofile="b/" + _normalise_path(path_text),
            n=3,
            lineterm="\n",
        )
        chunks.append("".join(diff))
    patch_text = "".join(chunks)
    if not patch_text:
        raise CampaignError("patch has no observable source change")
    return patch_text, details


def _verify_patch_targets(mutant_checkout: Path, patch_spec: dict[str, Any], patch_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    details_by_path = {str(item.get("path")): item for item in patch_details}
    for change in patch_spec.get("changes", []):
        path_text = str(change["path"])
        mutant_path = _safe_relative(mutant_checkout, path_text)
        after = _read_text(mutant_path)
        old = str(change["old"])
        new = str(change["new"])
        detail = details_by_path.get(_normalise_path(path_text), {})
        before_sha = detail.get("before_file_sha256")
        after_sha = _digest_text(after.replace("\r\n", "\n").replace("\r", "\n"))
        # Do not use substring absence as the proof: an equivalent mutation
        # may intentionally retain the old text as a comment or suffix.  The
        # renderer already calculated the complete expected file digest, so
        # verify the patched file against that digest instead.
        applied = (
            before_sha is not None
            and before_sha != after_sha
            and detail.get("after_file_sha256") == after_sha
        )
        targets.append({
            "path": _normalise_path(path_text),
            "function": "",
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "old_present_in_base": bool(detail),
            "new_present_in_mutant": new in after,
            "applied": applied,
        })
        if not applied:
            raise CampaignError(f"patch target verification failed: {path_text}")
    return targets


def _apply_patch(checkout: Path, patch_text: str, case_id: str, storage: Path, root: Path) -> dict[str, Any]:
    patch_path = storage / f"{case_id}.diff"
    _write_text(patch_path, patch_text)
    # ``git apply --no-index`` is intentionally used because the disposable
    # checkout is produced by ``git archive`` and has no .git directory.
    # Git's no-index mode is reliable for one file at a time on Windows; a
    # combined multi-file patch can reject a valid later hunk after applying
    # the first file's CR/LF-normalized context.  Validate and apply each
    # file patch independently, then retain the combined diff as the evidence
    # artifact.
    file_patches = [part for part in re.split(r"(?=^--- a/)", patch_text, flags=re.MULTILINE) if part.strip()]
    if not file_patches:
        raise CampaignError("patch contains no file hunks")
    per_file_paths: list[Path] = []
    checks: list[dict[str, Any]] = []
    for index, file_patch in enumerate(file_patches, start=1):
        file_path = storage / f"{case_id}-{index}.diff"
        _write_text(file_path, file_patch)
        per_file_paths.append(file_path)
        check = _git_command(["apply", "--no-index", "--ignore-whitespace", "--check", "--recount", "--whitespace=nowarn", str(file_path)], checkout, 60)
        checks.append(check)
        if check["status"] != "completed" or check["exit_code"] != 0:
            raise CampaignError("git apply --check failed: " + (check["stderr"] or check["stdout"]).strip())
    applied_results: list[dict[str, Any]] = []
    for file_path in per_file_paths:
        applied = _git_command(["apply", "--no-index", "--ignore-whitespace", "--recount", "--whitespace=nowarn", str(file_path)], checkout, 60)
        applied_results.append(applied)
        if applied["status"] != "completed" or applied["exit_code"] != 0:
            raise CampaignError("git apply failed: " + (applied["stderr"] or applied["stdout"]).strip())
    for result in [*checks, *applied_results]:
        for key in ("argv", "resolved_argv"):
            if isinstance(result.get(key), list):
                result[key] = [
                    "<patch>" if str(item) in {str(patch_path), *(str(path) for path in per_file_paths)} else item
                    for item in result[key]
                ]
    return {
        "path": _normalise_path(str(patch_path.relative_to(storage))),
        "sha256": _digest_text(patch_text),
        "text": patch_text,
        "check": checks[0] if len(checks) == 1 else {"status": "completed", "exit_code": 0, "files": checks},
        "apply": applied_results[0] if len(applied_results) == 1 else {"status": "completed", "exit_code": 0, "files": applied_results},
    }


def _is_crash(result: dict[str, Any]) -> bool:
    if isinstance(result.get("exit_code"), int) and result["exit_code"] < 0:
        return True
    combined = (str(result.get("stdout", "")) + "\n" + str(result.get("stderr", ""))).casefold()
    return any(marker in combined for marker in _CRASH_MARKERS)


def _same_observable(base_result: dict[str, Any], mutant_result: dict[str, Any]) -> bool:
    return (
        base_result.get("status") == mutant_result.get("status") == "completed"
        and base_result.get("exit_code") == mutant_result.get("exit_code")
        and base_result.get("stdout") == mutant_result.get("stdout")
        and base_result.get("stderr") == mutant_result.get("stderr")
    )


def _observable_digest(result: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, non-source-based description of a detector observation."""

    return {
        "status": result.get("status"),
        "timeout": result.get("timeout"),
        "exit_code": result.get("exit_code"),
        "stdout_sha256": result.get("stdout_sha256"),
        "stderr_sha256": result.get("stderr_sha256"),
    }


def _result_diagnostic(code: str, result: dict[str, Any] | None = None, **details: Any) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {"code": code}
    if result is not None:
        diagnostic["observation"] = _observable_digest(result)
    diagnostic.update(details)
    return diagnostic


def _target_files(case: dict[str, Any]) -> list[str]:
    return sorted({_normalise_path(str(change["path"])) for change in case["patch"]["changes"]})


def _normalise_patch_targets(checkout: Path, patch_spec: dict[str, Any]) -> None:
    """Make disposable patch targets LF-stable before rendering a diff.

    Windows repositories may contain CRLF blobs.  A unified diff generated
    from preserved CRLF line endings embeds carriage returns in its context,
    which ``git apply`` rejects when the patch file itself is LF-delimited.
    This normalization is limited to the disposable checkout and only to
    UTF-8 text targets; the source checkout is never modified.
    """

    seen: set[str] = set()
    for change in patch_spec.get("changes", []):
        path_text = str(change.get("path", ""))
        if path_text in seen:
            continue
        seen.add(path_text)
        path = _safe_relative(checkout, path_text)
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if normalized != text:
            path.write_text(normalized, encoding="utf-8", newline="\n")


def _case_command(case: dict[str, Any], profiles: dict[str, Any]) -> tuple[list[str], float]:
    if isinstance(case.get("gateCommand"), list):
        command = _validate_argv(case["gateCommand"], f"case {case.get('id')}.gateCommand")
        timeout = float(case.get("timeoutSeconds", 300))
    else:
        profile = profiles.get(case.get("releaseProfile"))
        if not isinstance(profile, dict):
            raise CampaignError(f"case {case.get('id')} has no usable release profile")
        command = _validate_argv(profile.get("command"), f"releaseProfiles.{case.get('releaseProfile')}.command")
        timeout = float(profile.get("timeoutSeconds", 300))
    return command, timeout


def _case_report_base(case: dict[str, Any], base_sha: str, patch_text: str | None = None) -> dict[str, Any]:
    patch_digest = _digest_text(patch_text) if patch_text is not None else None
    return {
        "id": case.get("id"),
        "operator_id": case.get("operatorId"),
        "category": case.get("category"),
        "owner_issues": sorted(case.get("ownerIssues", [])),
        "requirement_ids": sorted(case.get("requirementIds", [])),
        "target_function": case.get("targetFunction"),
        "release_profile": case.get("releaseProfile"),
        "must": case.get("must", True),
        "expected_outcome": case.get("expectedOutcome"),
        "detecting_test_ids": sorted(case.get("detectingTestIds", [])),
        "target_selectors": [
            {
                "path": _normalise_path(str(change.get("path"))),
                "function": case.get("targetFunction"),
                "kind": "function-scoped-source-span",
                "source_span_sha256": _digest_text(str(change.get("old", ""))),
                "occurrences": change.get("count", 1),
            }
            for change in case.get("patch", {}).get("changes", [])
            if isinstance(change, dict)
        ],
        "base_sha": base_sha,
        "classification": "generated",
        "patch_sha256": patch_digest,
        "patch": patch_text,
        "stage": "generated",
    }


def _run_case(
    case: dict[str, Any],
    archive: Path,
    root: Path,
    base_sha: str,
    profiles: dict[str, Any],
    syntax_spec: dict[str, Any],
    import_spec: dict[str, Any],
    baseline_results: dict[tuple[str, ...], dict[str, Any]],
    max_output_bytes: int,
    artifact_dir: Path | None = None,
    support_files: Iterable[str] = (),
) -> dict[str, Any]:
    case_id = str(case["id"])
    work_dir = Path(tempfile.mkdtemp(prefix=f"fdir-defect-campaign-{case_id}-"))
    report: dict[str, Any] = _case_report_base(case, base_sha)
    patch_storage = work_dir / "patches"
    try:
        base_checkout = work_dir / "base"
        _extract_archive(archive, base_checkout)
        _overlay_support_files(root, base_checkout, support_files)
        _normalise_patch_targets(base_checkout, case["patch"])
        patch_text, patch_details = _render_patch(base_checkout, case["patch"])
        report["patch"] = patch_text
        report["patch_sha256"] = _digest_text(patch_text)
        report["patch_changes"] = patch_details
        apply_info = _apply_patch(base_checkout, patch_text, case_id, patch_storage, root)
        mutant_checkout = base_checkout
        report["patch_application"] = apply_info
        report["targets"] = _verify_patch_targets(mutant_checkout=mutant_checkout, patch_spec=case["patch"], patch_details=patch_details)
        for target in report["targets"]:
            target["function"] = case.get("targetFunction", "")
        report["stage"] = "patch-applied"
        target_files = _target_files(case)
        syntax_command = case.get("syntaxCommand", syntax_spec.get("command"))
        syntax_timeout = float(case.get("syntaxTimeoutSeconds", syntax_spec.get("timeoutSeconds", 60)))
        expanded_syntax = _expand_command(_validate_argv(syntax_command, f"case {case_id}.syntaxCommand"), target_files)
        syntax_result = _run_command(expanded_syntax, mutant_checkout, syntax_timeout, root_for_output=root, max_output_bytes=max_output_bytes)
        report["syntax_check"] = syntax_result
        if syntax_result["status"] == "timeout":
            report["classification"] = "timeout"
            report["stage"] = "syntax-timeout"
            report["diagnostic"] = _result_diagnostic("syntax-check-timeout", syntax_result)
            return report
        if syntax_result["status"] == "infrastructure-error":
            report["classification"] = "infrastructure-error"
            report["stage"] = "syntax-infrastructure-error"
            report["diagnostic"] = _result_diagnostic("syntax-check-infrastructure-error", syntax_result)
            return report
        if syntax_result.get("exit_code") != syntax_spec.get("expectedExitCode", 0):
            report["classification"] = "invalid"
            report["stage"] = "syntax-invalid"
            report["diagnostic"] = _result_diagnostic(
                "patched-source-failed-syntax-check",
                syntax_result,
                expected_exit_code=syntax_spec.get("expectedExitCode", 0),
            )
            return report
        import_command = case.get("importCommand", import_spec.get("command"))
        import_timeout = float(case.get("importTimeoutSeconds", import_spec.get("timeoutSeconds", 60)))
        expanded_import = _expand_command(_validate_argv(import_command, f"case {case_id}.importCommand"), target_files)
        import_result = _run_command(expanded_import, mutant_checkout, import_timeout, root_for_output=root, max_output_bytes=max_output_bytes)
        report["import_check"] = import_result
        if import_result["status"] == "timeout":
            report["classification"] = "timeout"
            report["stage"] = "import-timeout"
            report["diagnostic"] = _result_diagnostic("import-check-timeout", import_result)
            return report
        if import_result["status"] == "infrastructure-error":
            report["classification"] = "infrastructure-error"
            report["stage"] = "import-infrastructure-error"
            report["diagnostic"] = _result_diagnostic("import-check-infrastructure-error", import_result)
            return report
        if import_result.get("exit_code") != import_spec.get("expectedExitCode", 0):
            report["classification"] = "invalid"
            report["stage"] = "import-invalid"
            report["diagnostic"] = _result_diagnostic(
                "patched-source-failed-import-check",
                import_result,
                expected_exit_code=import_spec.get("expectedExitCode", 0),
            )
            return report
        gate_command, gate_timeout = _case_command(case, profiles)
        gate_result = _run_command(gate_command, mutant_checkout, gate_timeout, root_for_output=root, max_output_bytes=max_output_bytes)
        report["gate"] = gate_result
        baseline = baseline_results.get(_command_key(gate_command))
        report["baseline_gate"] = baseline
        if baseline is None or baseline.get("status") != "completed" or baseline.get("exit_code") != 0:
            report["classification"] = "infrastructure-error"
            report["stage"] = "baseline-gate-invalid"
            report["diagnostic"] = _result_diagnostic(
                "detector-baseline-not-passing",
                baseline,
                expected_exit_code=0,
            )
            return report
        if gate_result["status"] == "timeout":
            report["classification"] = "timeout"
            report["stage"] = "gate-timeout"
            report["diagnostic"] = _result_diagnostic("detector-gate-timeout", gate_result)
            return report
        if gate_result["status"] == "infrastructure-error" or _is_crash(gate_result):
            report["classification"] = "infrastructure-error"
            report["stage"] = "gate-infrastructure-error"
            report["diagnostic"] = _result_diagnostic(
                "detector-gate-crash-or-infrastructure-error",
                gate_result,
                crash=_is_crash(gate_result),
            )
            return report
        if case.get("expectedOutcome") == "equivalent":
            observable_unchanged = _same_observable(baseline, gate_result)
            report["classification"] = "equivalent" if observable_unchanged else "undetected"
            report["stage"] = "gate-equivalence-observed"
            report["observable_delta"] = not observable_unchanged
            report["equivalence"] = {
                "observable_unchanged": observable_unchanged,
                "basis": ["status", "timeout", "exit_code", "stdout", "stderr"],
                "base": _observable_digest(baseline),
                "mutant": _observable_digest(gate_result),
            }
            report["diagnostic"] = _result_diagnostic(
                "equivalent-observable-unchanged" if observable_unchanged else "equivalent-case-observable-changed",
                gate_result,
                baseline=_observable_digest(baseline),
            )
            return report
        accepted_exit_codes = case.get("acceptedMutantExitCodes", [1])
        if not isinstance(accepted_exit_codes, list) or not all(isinstance(code, int) for code in accepted_exit_codes):
            accepted_exit_codes = [1]
        detected = gate_result.get("exit_code") in accepted_exit_codes and gate_result.get("exit_code") != baseline.get("exit_code")
        report["classification"] = "detected" if detected else "undetected"
        report["stage"] = "gate-failure-observed" if detected else "gate-pass-survived"
        report["detector_observation"] = {
            "base_exit_code": baseline.get("exit_code"),
            "mutant_exit_code": gate_result.get("exit_code"),
            "accepted_mutant_exit_codes": accepted_exit_codes,
            "exit_code_changed": gate_result.get("exit_code") != baseline.get("exit_code"),
            "observable_failure": detected,
        }
        report["diagnostic"] = _result_diagnostic(
            "baseline-pass-mutant-fail" if detected else "detector-did-not-fail",
            gate_result,
            oracle="baseline-pass-mutant-fail",
            baseline=_observable_digest(baseline),
            accepted_mutant_exit_codes=accepted_exit_codes,
        )
        if detected:
            report["detection"] = {
                "test_ids": sorted(case.get("detectingTestIds", [])),
                "diagnostic": report["diagnostic"],
            }
        return report
    except CampaignError as exc:
        report["classification"] = "infrastructure-error"
        report["stage"] = "infrastructure-error"
        report["diagnostic"] = {"code": "campaign-infrastructure-error", "message": str(exc)}
        return report
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        report["classification"] = "infrastructure-error"
        report["stage"] = "unexpected-error"
        report["diagnostic"] = {"code": "unexpected-runner-error", "message": f"unexpected {type(exc).__name__}: {exc}"}
        return report
    finally:
        if artifact_dir is not None:
            _save_case_artifacts(report, artifact_dir)
        shutil.rmtree(work_dir, ignore_errors=True)


def _save_case_artifacts(report: dict[str, Any], artifact_dir: Path) -> None:
    case_dir = artifact_dir / "cases" / str(report.get("id"))
    case_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(report.get("patch"), str):
        _write_text(case_dir / "patch.diff", report["patch"])
    for key, name in (("syntax_check", "syntax"), ("import_check", "import"), ("gate", "gate"), ("baseline_gate", "baseline-gate")):
        result = report.get(key)
        if isinstance(result, dict):
            _write_text(case_dir / f"{name}.stdout.txt", str(result.get("stdout", "")))
            _write_text(case_dir / f"{name}.stderr.txt", str(result.get("stderr", "")))


def _run_base_command(
    archive: Path,
    root: Path,
    command: list[str],
    timeout_seconds: float,
    max_output_bytes: int,
    support_files: Iterable[str] = (),
) -> dict[str, Any]:
    work_dir = Path(tempfile.mkdtemp(prefix="fdir-defect-campaign-base-"))
    try:
        checkout = work_dir / "base"
        _extract_archive(archive, checkout)
        _overlay_support_files(root, checkout, support_files)
        return _run_command(command, checkout, timeout_seconds, root_for_output=root, max_output_bytes=max_output_bytes)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _prepare_baselines(
    archive: Path,
    root: Path,
    cases: list[dict[str, Any]],
    profiles: dict[str, Any],
    max_output_bytes: int,
    support_files: Iterable[str] = (),
) -> tuple[dict[tuple[str, ...], dict[str, Any]], list[dict[str, Any]]]:
    commands: dict[tuple[str, ...], tuple[list[str], float]] = {}
    for case in cases:
        command, timeout_seconds = _case_command(case, profiles)
        commands.setdefault(_command_key(command), (command, timeout_seconds))
    results: dict[tuple[str, ...], dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for key in sorted(commands):
        command, timeout_seconds = commands[key]
        result = _run_base_command(archive, root, command, timeout_seconds, max_output_bytes, support_files)
        results[key] = result
        if result.get("status") != "completed" or result.get("exit_code") != 0:
            failures.append({"argv": command, "result": result})
    return results, failures


def _coverage_report(contract: dict[str, Any], cases: list[dict[str, Any]], *, enforced: bool) -> dict[str, Any]:
    profiles = contract.get("releaseProfiles", {})
    declared: dict[str, int] = {}
    for case in cases:
        if case.get("expectedOutcome") == "non-equivalent":
            profile = str(case.get("releaseProfile"))
            declared[profile] = declared.get(profile, 0) + 1
    entries: list[dict[str, Any]] = []
    complete = True
    for profile_id in sorted(profiles):
        profile = profiles[profile_id]
        required = int(profile.get("minimumNonEquivalentCases", 0)) if isinstance(profile, dict) else 0
        actual = declared.get(profile_id, 0)
        missing = max(0, required - actual)
        if missing:
            complete = False
        entries.append({"profile": profile_id, "actualNonEquivalentCases": actual, "requiredNonEquivalentCases": required, "missing": missing})
    return {
        "enforced": enforced,
        "complete": complete if enforced else None,
        "profiles": entries,
        "invariantCount": len(contract.get("invariantMatrix", [])),
        "invariantCoverage": "declared-case-matrix" if contract.get("invariantMatrix") else "missing",
    }


def _invariant_coverage(contract: dict[str, Any], case_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reports_by_id = {str(item.get("id")): item for item in case_reports}
    rows: list[dict[str, Any]] = []
    for invariant in sorted(contract.get("invariantMatrix", []), key=lambda item: str(item.get("id"))):
        if not isinstance(invariant, dict):
            continue
        case_id = str(invariant.get("caseId"))
        case_report = reports_by_id.get(case_id)
        rows.append(
            {
                "id": invariant.get("id"),
                "operator_id": invariant.get("operatorId"),
                "case_id": case_id,
                "owner_issues": sorted(invariant.get("ownerIssues", [])),
                "requirement_ids": sorted(invariant.get("requirementIds", [])),
                "release_profile": invariant.get("releaseProfile"),
                "must": invariant.get("must") is True,
                "classification": case_report.get("classification", "not-run") if case_report else "not-run",
                "detecting_test_ids": sorted(case_report.get("detection", {}).get("test_ids", [])) if case_report else [],
            }
        )
    return rows


def _classification_case_ids(case_reports: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        classification: sorted(
            str(item.get("id"))
            for item in case_reports
            if item.get("classification") == classification
        )
        for classification in CLASSIFICATIONS
    }


def _resource_usage(
    base_suite: dict[str, Any] | None,
    detector_baselines: list[dict[str, Any]],
    case_reports: list[dict[str, Any]],
    jobs: int,
) -> dict[str, Any]:
    result_keys = ("syntax_check", "import_check", "gate", "baseline_gate")
    case_durations = [
        sum(
            int(item[key].get("duration_ms", 0))
            for key in result_keys
            if isinstance(item.get(key), dict) and isinstance(item[key].get("duration_ms", 0), int)
        )
        for item in case_reports
    ]
    return {
        "duration_unit": "ms",
        "jobs": jobs,
        "base_suite_duration_ms": int(base_suite.get("duration_ms", 0)) if isinstance(base_suite, dict) else 0,
        "detector_baseline_duration_ms": sum(int(item.get("duration_ms", 0)) for item in detector_baselines if isinstance(item.get("duration_ms", 0), int)),
        "case_duration_ms": sum(case_durations),
        "max_case_duration_ms": max(case_durations) if case_durations else 0,
        "case_count": len(case_reports),
    }


def _stable_projection(value: Any) -> Any:
    """Remove measurements that naturally vary between executions."""

    if isinstance(value, dict):
        return {
            key: _stable_projection(child)
            for key, child in value.items()
            if key != "report_digest" and not key.endswith("duration_ms")
        }
    if isinstance(value, list):
        return [_stable_projection(child) for child in value]
    return value


def _report_digest(report: dict[str, Any]) -> str:
    return _digest_bytes(_json_bytes(_stable_projection(report)))


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is not None:
        _write_text(path, rendered)
    output = getattr(sys.stdout, "buffer", None)
    if output is not None:
        output.write(rendered.encode("utf-8"))
    else:  # pragma: no cover - StringIO callers
        sys.stdout.write(rendered)


def _base_report(contract: dict[str, Any], contract_check: dict[str, Any], base_sha: str | None) -> dict[str, Any]:
    contract_digest = _digest_bytes(_json_bytes(contract))
    return {
        "schema": contract.get("runner", {}).get("reportSchema", "fdir/defect-injection-campaign-report"),
        "version": RUNNER_VERSION,
        "framework": {"name": "fdir-defect-injection-runner", "version": RUNNER_VERSION, "dependencies": ["python-stdlib", "git"]},
        "framework_version": RUNNER_VERSION,
        "status": "failed",
        "campaign_calculated": False,
        "base_sha": base_sha,
        "contract_digest": contract_digest,
        "config_digest": contract_digest,
        "runner": {"path": "tools/run_defect_injection_campaign.py", "version": RUNNER_VERSION},
        "contract_validation": contract_check,
        "counts": {key: 0 for key in CLASSIFICATIONS},
        "declared_case_count": len(contract.get("cases", [])) if isinstance(contract.get("cases"), list) else 0,
        "cases": [],
        "undetected": [],
        "waivers": [],
        "classification_case_ids": {key: [] for key in CLASSIFICATIONS},
        "invariant_coverage": [],
        "resource_usage": _resource_usage(None, [], [], 1),
    }


def run_campaign(
    contract: dict[str, Any],
    *,
    root: Path = ROOT,
    case_ids: set[str] | None = None,
    categories: set[str] | None = None,
    limit: int | None = None,
    jobs: int = 1,
    allow_dirty_base: bool = False,
    allow_incomplete_matrix: bool = False,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    contract_check = validate_contract(contract, root)
    base_sha: str | None = None
    try:
        base_sha = _git_sha(root)
    except CampaignError as exc:
        report = _base_report(contract, contract_check, None)
        report["failure_reason"] = str(exc)
        report["report_digest"] = _report_digest(report)
        return report
    report = _base_report(contract, contract_check, base_sha)
    if not contract_check["valid"]:
        report["failure_reason"] = "contract-validation-failed"
        report["report_digest"] = _report_digest(report)
        return report
    if jobs < 1 or jobs > int(contract.get("runner", {}).get("maxJobs", 8)):
        report["failure_reason"] = f"jobs must be between 1 and {contract.get('runner', {}).get('maxJobs', 8)}"
        report["report_digest"] = _report_digest(report)
        return report
    dirty: list[str]
    try:
        dirty = _git_dirty(root)
    except CampaignError as exc:
        report["failure_reason"] = str(exc)
        report["report_digest"] = _report_digest(report)
        return report
    report["base_worktree"] = {"dirty": bool(dirty), "entries": dirty, "allowed": allow_dirty_base}
    if dirty and not allow_dirty_base:
        report["failure_reason"] = "base checkout is dirty; pass --allow-dirty-base only for non-release diagnostics"
        report["report_digest"] = _report_digest(report)
        return report

    all_cases = sorted(contract["cases"], key=lambda item: str(item["id"]))
    selected = [
        case
        for case in all_cases
        if (case_ids is None or str(case["id"]) in case_ids)
        and (categories is None or str(case.get("category")) in categories)
    ]
    if limit is not None:
        selected = selected[: max(0, limit)]
    report["selection"] = {
        "case_ids": sorted(case_ids) if case_ids is not None else None,
        "categories": sorted(categories) if categories is not None else None,
        "limit": limit,
        "jobs": jobs,
        "selected_case_count": len(selected),
    }
    scope_is_full = len(selected) == len(all_cases) and case_ids is None and categories is None and limit is None
    report["coverage"] = _coverage_report(contract, selected, enforced=scope_is_full and not allow_incomplete_matrix)
    report["invariant_coverage"] = _invariant_coverage(contract, [])
    if not selected:
        report["failure_reason"] = "no cases selected"
        report["report_digest"] = _report_digest(report)
        return report

    temporary_root = Path(tempfile.mkdtemp(prefix="fdir-defect-campaign-archive-"))
    try:
        try:
            archive = _create_archive(root, base_sha, temporary_root)
        except CampaignError as exc:
            report["failure_reason"] = str(exc)
            report["report_digest"] = _report_digest(report)
            return report
        base_suite = contract["baseSuite"]
        base_command = _validate_argv(base_suite["command"], "baseSuite.command")
        support_files = contract.get("supportFiles", [])
        base_result = _run_base_command(archive, root, base_command, float(base_suite.get("timeoutSeconds", 900)), int(contract["runner"].get("maxOutputBytes", 262144)), support_files)
        report["base_suite"] = base_result
        report["resource_usage"] = _resource_usage(base_result, [], [], jobs)
        if base_result.get("status") != "completed" or base_result.get("exit_code") != base_suite.get("expectedExitCode", 0):
            report["failure_reason"] = "base suite did not complete with its declared passing exit code"
            report["report_digest"] = _report_digest(report)
            return report

        baselines, baseline_failures = _prepare_baselines(archive, root, selected, contract["releaseProfiles"], int(contract["runner"].get("maxOutputBytes", 262144)), support_files)
        report["detector_baselines"] = [baselines[key] for key in sorted(baselines)]
        report["resource_usage"] = _resource_usage(base_result, report["detector_baselines"], [], jobs)
        if baseline_failures:
            report["failure_reason"] = "one or more detector commands failed on the clean base"
            report["baseline_failures"] = baseline_failures
            report["report_digest"] = _report_digest(report)
            return report

        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            _write_text(artifact_dir / "base-suite.stdout.txt", str(base_result.get("stdout", "")))
            _write_text(artifact_dir / "base-suite.stderr.txt", str(base_result.get("stderr", "")))
        max_output_bytes = int(contract["runner"].get("maxOutputBytes", 262144))
        syntax_spec = contract["syntaxCheck"]
        import_spec = contract["importCheck"]
        if jobs == 1:
            case_reports = [
                _run_case(case, archive, root, base_sha, contract["releaseProfiles"], syntax_spec, import_spec, baselines, max_output_bytes, artifact_dir, support_files)
                for case in selected
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="defect-case") as executor:
                futures = [
                    executor.submit(_run_case, case, archive, root, base_sha, contract["releaseProfiles"], syntax_spec, import_spec, baselines, max_output_bytes, artifact_dir, support_files)
                    for case in selected
                ]
                case_reports = [future.result() for future in futures]
        case_reports.sort(key=lambda item: str(item.get("id")))
        report["cases"] = case_reports
        report["campaign_calculated"] = True
        counts = {key: 0 for key in CLASSIFICATIONS}
        for case_report in case_reports:
            classification = case_report.get("classification")
            if classification in counts:
                counts[classification] += 1
        counts["generated"] = len(case_reports)
        report["counts"] = counts
        report["classification_case_ids"] = _classification_case_ids(case_reports)
        report["invariant_coverage"] = _invariant_coverage(contract, case_reports)
        report["resource_usage"] = _resource_usage(base_result, report["detector_baselines"], case_reports, jobs)
        report["undetected"] = [
            {
                "id": item.get("id"),
                "operator_id": item.get("operator_id"),
                "release_profile": item.get("release_profile"),
                "must": item.get("must"),
                "classification": item.get("classification"),
            }
            for item in case_reports
            if item.get("classification") == "undetected" or (item.get("must") and item.get("classification") in {"invalid", "timeout", "infrastructure-error"})
        ]
        must_failures = [
            item.get("id")
            for item in case_reports
            if item.get("must") and (item.get("expected_outcome") == "non-equivalent" and item.get("classification") != "detected")
        ]
        coverage_ok = bool(report["coverage"].get("complete") is not False)
        report["completion"] = {
            "must_undetected_zero": not must_failures,
            "must_failure_ids": sorted(str(item) for item in must_failures),
            "coverage_complete": coverage_ok,
            "release_eligible": not must_failures and coverage_ok and not report["base_worktree"]["dirty"],
        }
        report["status"] = "passed" if not must_failures and coverage_ok and not report["undetected"] and report["base_worktree"]["dirty"] is False else "failed"
        if not coverage_ok and not report.get("failure_reason"):
            report["failure_reason"] = "declared release profile case minimums are incomplete"
        elif report["undetected"] and not report.get("failure_reason"):
            report["failure_reason"] = "one or more cases were not detected without an allowed waiver"
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    report["report_digest"] = _report_digest(report)
    return report


def _self_test_script(value: str) -> str:
    return value.replace("\r\n", "\n")


def _init_self_test_repo() -> tuple[Path, Path, str]:
    parent = Path(tempfile.mkdtemp(prefix="fdir-defect-self-test-"))
    repo = parent / "repo"
    repo.mkdir()
    _write_text(repo / "target.py", "VALUE = 1\n")
    _write_text(repo / "detector.py", "import target\nraise SystemExit(1 if target.VALUE == 0 else 0)\n")
    init = _git_command(["init", "--quiet"], repo)
    if init.get("status") != "completed" or init.get("exit_code") != 0:
        raise CampaignError("self-test git init failed")
    add = _git_command(["add", "target.py", "detector.py"], repo)
    if add.get("status") != "completed" or add.get("exit_code") != 0:
        raise CampaignError("self-test git add failed")
    commit = _run_command(
        ["git", "-c", "user.name=FDIR self-test", "-c", "user.email=fdir-self-test@example.invalid", "commit", "--quiet", "-m", "base"],
        repo,
        60,
        root_for_output=repo,
        max_output_bytes=262144,
    )
    if commit.get("status") != "completed" or commit.get("exit_code") != 0:
        raise CampaignError("self-test git commit failed: " + str(commit.get("stderr")))
    sha = _git_sha(repo)
    return parent, repo, sha


def _self_case(case_id: str, patch_changes: list[dict[str, Any]], command: list[str], expected: str = "non-equivalent", timeout: float = 10) -> dict[str, Any]:
    return {
        "id": case_id,
        "operatorId": "self-test-" + case_id,
        "category": "meta",
        "ownerIssues": [89],
        "requirementIds": ["DFIR-QA-008"],
        "targetFunction": "VALUE",
        "releaseProfile": "self",
        "detectingTestIds": ["META-" + case_id] if expected == "non-equivalent" else [],
        "expectedOutcome": expected,
        "gateCommand": command,
        "timeoutSeconds": timeout,
        "syntaxCommand": [sys.executable, "-m", "py_compile", "{target_files}"],
        "importCommand": [sys.executable, "-m", "py_compile", "{target_files}"],
        "patch": {"kind": "replace", "changes": patch_changes},
    }


def _self_contract(base_command: list[str], cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "fdir/defect-injection-contract",
        "version": "1.0.0",
        "issue": 89,
        "repository": "self-test",
        "policy": {
            "baseRef": "HEAD",
            "checkout": "git-archive-disposable-copy",
            "commandsAreArgv": True,
            "thirdPartyDependencies": False,
            "cleanBaseRequired": True,
            "patchKind": "declarative-replacement-rendered-as-unified-diff",
            "patchApplyFailureClassification": "infrastructure-error",
            "invalidCasesStayInDenominator": True,
            "timeoutIsNotDetection": True,
            "crashIsNotDetection": True,
            "detectedOracle": "the same detector command passes on the base copy and exits with an accepted test/gate failure on the patched copy",
            "equivalentOracle": "the declared equivalent patch leaves the detector observable result unchanged",
            "sourceComparisonIsNeverDetection": True,
            "selectorPolicy": {
                "kind": "function-scoped-source-span",
                "targetFileField": "patch.changes[].path",
                "targetFunctionField": "targetFunction",
                "astSelectorField": "patch.changes[].old",
                "verification": "self-test exact source-span selector",
            },
        },
        "baseSuite": {"id": "self-base", "command": base_command, "timeoutSeconds": 10, "expectedExitCode": 0},
        "syntaxCheck": {"command": [sys.executable, "-m", "py_compile", "{target_files}"], "timeoutSeconds": 10, "expectedExitCode": 0},
        "importCheck": {"command": [sys.executable, "-m", "py_compile", "{target_files}"], "timeoutSeconds": 10, "expectedExitCode": 0},
        "runner": {
            "id": "self-test-runner",
            "version": RUNNER_VERSION,
            "reportSchema": "fdir/defect-injection-campaign-report",
            "jsonEncoding": "UTF-8",
            "lineEnding": "LF",
            "sortKeys": True,
            "maxOutputBytes": 262144,
            "defaultJobs": 2,
            "maxJobs": 8,
            "worktreePrefix": "fdir-defect-self-test-",
        },
        "classifications": list(CLASSIFICATIONS),
        "releaseProfiles": {"self": {"command": [sys.executable, "detector.py"], "timeoutSeconds": 10, "minimumNonEquivalentCases": 1}},
        "cases": cases,
        "invariantMatrix": [
            {
                "id": "META-" + str(case["id"]).upper(),
                "ownerIssues": [89],
                "requirementIds": ["DFIR-QA-008"],
                "operatorId": case["operatorId"],
                "caseId": case["id"],
                "releaseProfile": "self",
                "must": True,
            }
            for case in cases
        ],
    }


def run_self_test() -> dict[str, Any]:
    parent: Path | None = None
    try:
        parent, repo, sha = _init_self_test_repo()
        archive_storage = parent / "archive"
        archive_storage.mkdir()
        archive = _create_archive(repo, sha, archive_storage)
        common_profile = {"command": [sys.executable, "detector.py"], "timeoutSeconds": 10}
        profiles = {"self": common_profile}
        syntax_spec = {"command": [sys.executable, "-m", "py_compile", "{target_files}"], "timeoutSeconds": 10, "expectedExitCode": 0}
        import_spec = {"command": [sys.executable, "-m", "py_compile", "{target_files}"], "timeoutSeconds": 10, "expectedExitCode": 0}
        detected = _self_case("detected", [{"path": "target.py", "old": "VALUE = 1", "new": "VALUE = 0", "count": 1}], [sys.executable, "detector.py"])
        disabled = _self_case("detector-disabled", [{"path": "target.py", "old": "VALUE = 1", "new": "VALUE = 0", "count": 1}, {"path": "detector.py", "old": "raise SystemExit(1 if target.VALUE == 0 else 0)", "new": "raise SystemExit(0)", "count": 1}], [sys.executable, "detector.py"])
        equivalent = _self_case("equivalent-output", [{"path": "target.py", "old": "VALUE = 1", "new": "VALUE = 1  # equivalent", "count": 1}], [sys.executable, "detector.py"], expected="equivalent")
        invalid = _self_case("invalid-syntax", [{"path": "target.py", "old": "VALUE = 1", "new": "VALUE = (", "count": 1}], [sys.executable, "detector.py"])
        timeout_case = _self_case("timeout", [{"path": "detector.py", "old": "import target", "new": "import time; time.sleep(2)\nimport target", "count": 1}], [sys.executable, "detector.py"], timeout=0.05)
        missing = _self_case("missing-command", [{"path": "target.py", "old": "VALUE = 1", "new": "VALUE = 0", "count": 1}], ["fdir-command-does-not-exist"])
        not_applicable = _self_case("patch-not-applicable", [{"path": "target.py", "old": "MISSING = 1", "new": "MISSING = 0", "count": 1}], [sys.executable, "detector.py"])
        selected = [detected, disabled, equivalent, invalid, timeout_case, missing, not_applicable]
        baseline_cases = [detected, disabled, equivalent, invalid, timeout_case]
        baselines, failures = _prepare_baselines(archive, repo, baseline_cases, profiles, 262144)
        if failures:
            raise CampaignError("self-test detector baseline failed")
        first = _run_case(detected, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        disabled_result = _run_case(disabled, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        equivalent_result = _run_case(equivalent, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        invalid_result = _run_case(invalid, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        timeout_result = _run_case(timeout_case, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        missing_result = _run_case(missing, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        not_applicable_result = _run_case(not_applicable, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144)
        expected = {
            "detected": "detected",
            "detector-disabled": "undetected",
            "equivalent-output": "equivalent",
            "invalid-syntax": "invalid",
            "timeout": "timeout",
            "missing-command": "infrastructure-error",
            "patch-not-applicable": "infrastructure-error",
        }
        observed = {item["id"]: item["classification"] for item in [first, disabled_result, equivalent_result, invalid_result, timeout_result, missing_result, not_applicable_result]}
        assertions = [{"id": key, "expected": expected[key], "actual": observed.get(key), "status": "passed" if expected[key] == observed.get(key) else "failed"} for key in sorted(expected)]
        parallel_cases = [detected, equivalent]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_run_case, item, archive, repo, sha, profiles, syntax_spec, import_spec, baselines, 262144) for item in parallel_cases]
            parallel_results = [future.result() for future in futures]
        parallel_results.sort(key=lambda item: item["id"])
        parallel_ok = [item["classification"] for item in parallel_results] == ["detected", "equivalent"] and len({item["patch_sha256"] for item in parallel_results}) == 2
        assertions.append({"id": "parallel-isolation", "expected": "deterministic-independent-case-results", "actual": [item["classification"] for item in parallel_results], "status": "passed" if parallel_ok else "failed"})

        campaign_contract = _self_contract([sys.executable, "-c", "raise SystemExit(0)"], baseline_cases)
        campaign_first = run_campaign(campaign_contract, root=repo, jobs=2)
        campaign_second = run_campaign(campaign_contract, root=repo, jobs=2)
        campaign_classifications = {
            str(item.get("id")): item.get("classification")
            for item in campaign_first.get("cases", [])
        }
        campaign_ok = (
            campaign_first.get("campaign_calculated") is True
            and campaign_classifications.get("detected") == "detected"
            and campaign_classifications.get("detector-disabled") == "undetected"
            and campaign_classifications.get("equivalent-output") == "equivalent"
            and campaign_classifications.get("invalid-syntax") == "invalid"
            and campaign_classifications.get("timeout") == "timeout"
            and campaign_first.get("base_sha") == sha
        )
        assertions.append({
            "id": "campaign-execution",
            "expected": "base-gate-and-all-meta-classifications",
            "actual": campaign_classifications,
            "status": "passed" if campaign_ok else "failed",
        })
        deterministic_ok = (
            campaign_first.get("report_digest") == campaign_second.get("report_digest")
            and campaign_first.get("base_sha") == campaign_second.get("base_sha")
            and campaign_first.get("contract_digest") == campaign_second.get("contract_digest")
            and [item.get("id") for item in campaign_first.get("cases", [])] == [item.get("id") for item in campaign_second.get("cases", [])]
        )
        assertions.append({
            "id": "same-sha-config-determinism",
            "expected": "same-report-digest-and-case-order",
            "actual": {"first": campaign_first.get("report_digest"), "second": campaign_second.get("report_digest")},
            "status": "passed" if deterministic_ok else "failed",
        })

        failing_contract = _self_contract([sys.executable, "-c", "raise SystemExit(1)"], baseline_cases)
        failing_campaign = run_campaign(failing_contract, root=repo, jobs=1)
        base_fail_ok = (
            failing_campaign.get("campaign_calculated") is False
            and failing_campaign.get("cases") == []
            and failing_campaign.get("failure_reason") == "base suite did not complete with its declared passing exit code"
        )
        assertions.append({
            "id": "base-suite-failure",
            "expected": "nonzero-base-stops-before-case-results",
            "actual": {"campaign_calculated": failing_campaign.get("campaign_calculated"), "case_count": len(failing_campaign.get("cases", []))},
            "status": "passed" if base_fail_ok else "failed",
        })
        return {
            "schema": "fdir/defect-injection-self-test-report",
            "version": RUNNER_VERSION,
            "status": "passed" if all(item["status"] == "passed" for item in assertions) else "failed",
            "assertions": assertions,
            "observed_classifications": observed,
            "campaign_classifications": campaign_classifications,
            "campaign_report_digest": campaign_first.get("report_digest"),
            "base_sha": sha,
            "third_party_dependencies": False,
        }
    except Exception as exc:
        return {
            "schema": "fdir/defect-injection-self-test-report",
            "version": RUNNER_VERSION,
            "status": "failed",
            "assertions": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if parent is not None:
            shutil.rmtree(parent, ignore_errors=True)


def _parse_set(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    return {value for item in values for value in item.split(",") if value}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH, help="machine-readable defect-injection contract")
    parser.add_argument("--out", type=Path, help="also write the deterministic JSON report to this path")
    parser.add_argument("--artifact-dir", type=Path, help="write per-case patch/stdout/stderr artifacts here")
    parser.add_argument("--case-id", action="append", help="run only these case IDs; may be comma-separated")
    parser.add_argument("--category", action="append", help="run only these categories; may be comma-separated")
    parser.add_argument("--limit", type=int, help="run at most this many cases after deterministic sorting")
    parser.add_argument("--jobs", type=int, default=1, help="parallel disposable cases (1-8)")
    parser.add_argument("--allow-dirty-base", action="store_true", help="diagnostic mode; never release-eligible")
    parser.add_argument("--allow-incomplete-matrix", action="store_true", help="do not fail the selected campaign for declared count gaps")
    parser.add_argument("--validate-contract", action="store_true", help="validate selectors and print a contract report without running commands")
    parser.add_argument("--self-test", action="store_true", help="run runner meta self-tests in a disposable repository")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_path = args.out if args.out is None or args.out.is_absolute() else ROOT / args.out
    if args.limit is not None and args.limit < 0:
        report = {"schema": "fdir/defect-injection-campaign-report", "version": RUNNER_VERSION, "status": "failed", "failure_reason": "--limit must be non-negative"}
        _write_report(out_path, report)
        return 1
    if args.self_test:
        report = run_self_test()
        report["report_digest"] = _report_digest(report)
        _write_report(out_path, report)
        return 0 if report.get("status") == "passed" else 1
    try:
        contract_path = args.contract if args.contract.is_absolute() else ROOT / args.contract
        contract = _load_json(contract_path)
        if not isinstance(contract, dict):
            raise CampaignError("contract root must be an object")
        if args.validate_contract:
            check = validate_contract(contract, ROOT)
            try:
                contract_path_display = _normalise_path(str(contract_path.relative_to(ROOT)))
            except ValueError:
                contract_path_display = _normalise_path(str(contract_path))
            report = {
                "schema": "fdir/defect-injection-contract-validation-report",
                "version": RUNNER_VERSION,
                "status": "passed" if check["valid"] else "failed",
                "contract_path": contract_path_display,
                "contract_digest": _digest_bytes(_json_bytes(contract)),
                "validation": check,
            }
            report["report_digest"] = _report_digest(report)
            _write_report(out_path, report)
            return 0 if report["status"] == "passed" else 1
        report = run_campaign(
            contract,
            root=ROOT,
            case_ids=_parse_set(args.case_id),
            categories=_parse_set(args.category),
            limit=args.limit,
            jobs=args.jobs,
            allow_dirty_base=args.allow_dirty_base,
            allow_incomplete_matrix=args.allow_incomplete_matrix,
            artifact_dir=(args.artifact_dir if args.artifact_dir is None or args.artifact_dir.is_absolute() else ROOT / args.artifact_dir),
        )
    except (CampaignError, OSError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "schema": "fdir/defect-injection-campaign-report",
            "version": RUNNER_VERSION,
            "status": "failed",
            "campaign_calculated": False,
            "failure_reason": f"{type(exc).__name__}: {exc}",
        }
    report["report_digest"] = _report_digest(report)
    _write_report(out_path, report)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
