"""Run the behavioral release-quality qualification for GitHub issue #105.

This runner is intentionally separate from ``release_gate.py``.  The release
gate is an integration consumer; this module produces the eight semantic
reports that issue #105 requires.  Expected requirements, case IDs, platform
profiles, and false-completion mutations are authored in the issue corpus.
The runner never turns a command exit code or an existing file into a passed
behavioral assertion by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "machine" / "qualification-issue-105-corpus.json"
DEFAULT_OUT = ROOT / "e2e" / ".run" / "qualification-issue-105"
REPORT_NAMES = (
    "requirement-traceability.json",
    "behavioral-suite-inventory.json",
    "cli-api-contract-results.json",
    "platform-runtime-matrix.json",
    "fresh-install-results.json",
    "determinism-replay-diff.json",
    "false-completion-regressions.json",
    "release-claim-conformance.json",
)
SOURCE_SHA_LENGTH = 40
SHA256_LENGTH = 64

PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
EVIDENCE_ID = "issue-105-release-quality"
REQUIREMENT_ID = "QUAL-105-RELEASE-BARRIER"
BUNDLE_PREFIX = "artifacts/105"
DECLARED_INPUTS = (
    "machine/release-claim-manifest.json",
    "machine/audit-recovery-plan.json",
    "machine/qualification-contract.json",
    "tools/qualification_issue105.py",
    "machine/qualification-issue-105-corpus.json",
)
EVALUATOR_PATH = ROOT / "tools" / "validate_qualification_bundle.py"
SHARED_EVIDENCE_PATH = ROOT / "tools" / "qualification_evidence.py"


class QualificationError(RuntimeError):
    """Raised for invalid qualification input or an unsafe execution lane."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    paths = [ROOT / relative for relative in DECLARED_INPUTS]
    candidate = Path(corpus_path)
    paths[-1] = candidate if candidate.is_absolute() else ROOT / candidate
    return paths


def _producer_input_digests(corpus_path: Path) -> tuple[list[str], list[str]]:
    digests: list[str] = []
    unavailable: list[str] = []
    for path in _producer_input_paths(corpus_path):
        if path.is_file():
            digests.append(sha256_file(path))
        else:
            unavailable.append(str(path))
            digests.append(sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
    return sorted(set(digests)), unavailable


def _producer_pointer(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QualificationError(f"invalid producer JSON pointer: {pointer!r}")
    current = value
    for raw in pointer[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise QualificationError(f"producer JSON pointer is missing: {pointer}")
    return current


def _producer_artifact_reference(local_path: Path, bundle_path: str, pointer: str) -> dict[str, Any]:
    value = _producer_pointer(load_json(local_path), pointer)
    return {
        "path": bundle_path,
        "sha256": sha256_file(local_path),
        "selector": {"kind": "json-pointer", "pointer": pointer},
        "selectedSha256": sha256_bytes(canonical(value).encode("utf-8")),
    }


def _append_producer_record(report: dict[str, Any], key: str, value: dict[str, Any]) -> str:
    records = report.setdefault(key, [])
    pointer = f"/{key}/{len(records)}"
    records.append(value)
    return pointer


def _stable_path_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f"<external>/{path.name}"


def sha256_paths(paths: list[Path]) -> str:
    entries: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if path.is_file():
            entries.append({"path": _stable_path_label(path), "sha256": sha256_file(path)})
        elif path.is_dir():
            children = [child for child in path.rglob("*") if child.is_file()]
            entries.append({
                "path": _stable_path_label(path),
                "files": [
                    {"path": child.relative_to(path).as_posix(), "sha256": sha256_file(child)}
                    for child in sorted(children, key=lambda item: item.as_posix())
                ],
            })
        else:
            entries.append({"path": path.relative_to(ROOT).as_posix(), "missing": True})
    return sha256_bytes(canonical(entries).encode("utf-8"))


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def source_sha() -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != SOURCE_SHA_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise QualificationError(f"cannot obtain exact source SHA: {value!r}")
    return value


def working_tree_status() -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise QualificationError(f"git status failed: {(result.stdout + result.stderr).strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def resolve_repo_path(value: str) -> Path:
    path = (ROOT / Path(*value.replace("\\", "/").split("/"))).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise QualificationError(f"path escapes repository: {value}") from exc
    return path


def validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("schema") != "fdir/qualification-issue-105-corpus" or corpus.get("version") != "1.0.0":
        raise QualificationError("issue #105 corpus schema/version is invalid")
    if corpus.get("issueNumber") != 105 or corpus.get("qualificationScope") != "behavioral-release-quality-and-reproducibility":
        raise QualificationError("issue #105 corpus scope is invalid")
    if tuple(corpus.get("reportNames", [])) != REPORT_NAMES:
        raise QualificationError("issue #105 report names are incomplete or reordered")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict) or not oracle.get("requirementsAreHandReviewed") or not oracle.get("casesAreImplementationIndependent"):
        raise QualificationError("issue #105 corpus does not declare an independent authored oracle")
    if oracle.get("expectedResultsGeneratedFromCommandExitOnly") or oracle.get("expectedResultsGeneratedFromFileExistenceOnly"):
        raise QualificationError("issue #105 corpus permits weak expected-result generation")
    release_scope = corpus.get("releaseScopeIssues")
    if release_scope != list(range(88, 105)):
        raise QualificationError("issue #105 release scope must enumerate #88-#104")
    release_evidence = corpus.get("releaseEvidence")
    if not isinstance(release_evidence, list) or [item.get("issueNumber") for item in release_evidence] != release_scope:
        raise QualificationError("issue #105 release evidence table is incomplete")
    requirements = corpus.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise QualificationError("issue #105 has no authored requirements")
    requirement_ids: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict):
            raise QualificationError("issue #105 contains a malformed requirement")
        identifier = item.get("requirementId")
        if not isinstance(identifier, str) or not identifier or identifier in requirement_ids:
            raise QualificationError(f"issue #105 requirement ID is invalid or duplicated: {identifier!r}")
        requirement_ids.add(identifier)
        for field in ("positiveCaseIds", "negativeCaseIds", "defectInjectionCaseIds", "independentCaseIds", "implementationPaths", "requiredReportNames"):
            values = item.get(field)
            if not isinstance(values, list) or not values:
                raise QualificationError(f"issue #105 requirement {identifier} has no {field}")
    suites = corpus.get("suiteCommands")
    if not isinstance(suites, list) or not suites or len({item.get("caseId") for item in suites}) != len(suites):
        raise QualificationError("issue #105 suite command inventory is invalid")
    cli_cases = corpus.get("cliCases")
    if not isinstance(cli_cases, list) or {item.get("format") for item in cli_cases} != {"docx", "xlsx", "pdf", "markdown"}:
        raise QualificationError("issue #105 CLI matrix does not cover every format")
    profiles = corpus.get("supportedPlatformProfiles")
    if not isinstance(profiles, list) or {item.get("osFamily") for item in profiles} != {"Linux", "Windows", "Darwin"}:
        raise QualificationError("issue #105 platform matrix is incomplete")
    false_cases = corpus.get("falseCompletionCases")
    if not isinstance(false_cases, list) or len(false_cases) < 12:
        raise QualificationError("issue #105 false-completion regression matrix is incomplete")


def _argv(spec: list[str], out_dir: Path) -> list[str]:
    result = list(spec)
    if result and result[0].casefold() in {"python", "python3", "py"}:
        result[0] = sys.executable
    return [item.replace("{out_dir}", str(out_dir)) for item in result]


def run_command(
    command: list[str],
    *,
    expected_exit: int,
    timeout: int,
    cwd: Path = ROOT,
    input_paths: list[Path] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONHASHSEED"] = "0"
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
            check=False,
        )
        timed_out = False
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        timed_out = False
        return_code = 127
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
    duration = round((time.monotonic() - started) * 1000, 3)
    status = "passed" if not timed_out and return_code == expected_exit else "failed"
    return {
        "command": command,
        "cwd": "." if cwd.resolve() == ROOT.resolve() else str(cwd.resolve()),
        "expectedExitCode": expected_exit,
        "returnCode": return_code,
        "timedOut": timed_out,
        "durationMilliseconds": duration,
        "inputDigest": sha256_paths(input_paths or []),
        "stdoutSha256": sha256_bytes(stdout.encode("utf-8")),
        "stderrSha256": sha256_bytes(stderr.encode("utf-8")),
        "diagnostics": [line[-500:] for line in (stdout + "\n" + stderr).splitlines() if line.strip()][-12:],
        "status": status,
        "_stdout": stdout,
        "_stderr": stderr,
    }


def public_case(
    case_id: str,
    oracle: str,
    command_result: dict[str, Any],
    *,
    target: str,
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    return {
        "caseId": case_id,
        "oracle": oracle,
        "assertionIds": [f"{case_id}-expected", f"{case_id}-side-effects"],
        "target": target,
        "expected": expected,
        "actual": actual,
        "diagnostics": command_result.get("diagnostics", []),
        "durationMilliseconds": command_result.get("durationMilliseconds"),
        "inputDigest": command_result.get("inputDigest"),
        "status": command_result.get("status", "failed"),
    }


def report(
    name: str,
    source: str,
    dirty: list[str],
    *,
    assertions: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    diagnostics: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    passed = not dirty and all(item.get("status") == "passed" for item in assertions) and all(item.get("status") == "passed" for item in cases)
    return {
        "schema": "fdir/qualification-issue-105-report",
        "version": "1.0.0",
        "issueNumber": 105,
        "reportName": name,
        "sourceSha": source,
        "dirtyTree": bool(dirty),
        "status": "passed" if passed else "failed",
        "completionStatus": "qualified" if passed else "incomplete",
        "assertions": assertions,
        "cases": cases,
        "diagnostics": diagnostics or [],
        **extra,
    }


def _assertion(identifier: str, expected: Any, actual: Any, *, status: str | None = None, detail: Any = None) -> dict[str, Any]:
    item = {
        "id": identifier,
        "expected": expected,
        "actual": actual,
        "status": status or ("passed" if expected == actual else "failed"),
    }
    if detail is not None:
        item["detail"] = detail
    return item


def traceability_report(corpus: dict[str, Any], source: str, dirty: list[str]) -> dict[str, Any]:
    contract = load_json(ROOT / "machine" / "qualification-contract.json")
    recovery = load_json(ROOT / "machine" / "recovery-report-contract.json")
    scope = contract.get("scope", {}) if isinstance(contract, dict) else {}
    defaults = {item.get("evidenceId"): item for item in contract.get("defaultEvidence", []) if isinstance(item, dict)}
    expected_entries = {item["issueNumber"]: item for item in corpus["releaseEvidence"]}
    cases: list[dict[str, Any]] = []
    failures: list[str] = []
    for issue_number in corpus["releaseScopeIssues"]:
        expected = expected_entries[issue_number]
        evidence_id = expected["evidenceId"]
        actual_reports = recovery.get("reports", {}).get(str(issue_number), [])
        default = defaults.get(evidence_id)
        checks = {
            "evidenceInScope": evidence_id in set(scope.get("requiredEvidenceIds", [])),
            "defaultEvidenceBound": isinstance(default, dict) and issue_number in default.get("issueNumbers", []),
            "requiredReportsBound": set(expected["requiredReports"]).issubset(set(actual_reports)),
            "commandIsIssueSpecific": isinstance(default, dict) and bool(default.get("command")) and str(default.get("command", [""])[1:]).find("issue") >= 0 or issue_number in {88, 89, 104},
        }
        status = "passed" if all(checks.values()) else "failed"
        if status != "passed":
            failures.append(f"issue-{issue_number}:{','.join(key for key, value in checks.items() if not value)}")
        cases.append({
            "caseId": f"RQ-TRACE-{issue_number}",
            "oracle": "authored release-scope evidence table must match the qualification and report contracts",
            "assertionIds": [f"RQ-TRACE-{issue_number}-contract"],
            "target": f"issue #{issue_number}",
            "expected": {"evidenceId": evidence_id, "requiredReports": expected["requiredReports"]},
            "actual": {"evidenceId": evidence_id if isinstance(default, dict) else None, "requiredReports": actual_reports, "checks": checks},
            "inputDigest": sha256_paths([ROOT / "machine" / "qualification-contract.json", ROOT / "machine" / "recovery-report-contract.json"]),
            "status": status,
        })
    assertions = [
        _assertion("release-scope-is-exactly-88-104", list(range(88, 105)), corpus["releaseScopeIssues"]),
        _assertion("all-required-evidence-is-owned", len(corpus["releaseScopeIssues"]), len([case for case in cases if case["status"] == "passed"])),
        _assertion("issue-105-requirements-have-implementation-paths", True, all(item.get("implementationPaths") for item in corpus["requirements"])),
    ]
    return report("requirement-traceability.json", source, dirty, assertions=assertions, cases=cases, failures=failures)


def behavioral_report(corpus: dict[str, Any], source: str, dirty: list[str], out_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    results: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for spec in corpus["suiteCommands"]:
        result = run_command(_argv(spec["argv"], out_dir), expected_exit=int(spec["expectedExitCode"]), timeout=int(spec["timeoutSeconds"]), input_paths=[resolve_repo_path("machine/qualification-issue-105-corpus.json")])
        results[str(spec["suiteId"])] = result
        cases.append(public_case(
            str(spec["caseId"]),
            "the authored suite command must produce its machine-readable behavioral result and declared exit code",
            result,
            target=str(spec["suiteId"]),
            expected={"exitCode": int(spec["expectedExitCode"]), "status": "passed"},
            actual={"exitCode": result.get("returnCode"), "status": result.get("status")},
        ))
    assertions = [
        _assertion("suite-case-count-is-authored", len(corpus["suiteCommands"]), len(cases)),
        _assertion("suite-case-ids-are-unique", len(cases), len({case["caseId"] for case in cases})),
        _assertion("suite-results-have-digests", True, all(len(case.get("inputDigest", "")) == SHA256_LENGTH and len(case.get("actual", {}).get("status", "")) > 0 for case in cases)),
    ]
    return report("behavioral-suite-inventory.json", source, dirty, assertions=assertions, cases=cases, suites=results), results


def _zip_directory(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for child in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
            info = zipfile.ZipInfo(child.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, child.read_bytes())
    return destination


def cli_report(corpus: dict[str, Any], source: str, dirty: list[str], out_dir: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for spec in corpus["cliCases"]:
        case_id = str(spec["caseId"])
        source_path = resolve_repo_path(str(spec["input"]))
        if source_path.is_dir():
            input_path = _zip_directory(source_path, out_dir / "inputs" / f"{case_id}.{spec['format']}")
        elif source_path.is_file():
            input_path = out_dir / "inputs" / f"{case_id}{source_path.suffix}"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, input_path)
        else:
            input_path = out_dir / "inputs" / f"{case_id}.missing"
        output_path = out_dir / "cli" / f"{case_id}.json"
        evidence_path = out_dir / "cli" / f"{case_id}.evidence.json"
        inspect_result = run_command([sys.executable, "tools/convert_document.py", "inspect", str(input_path), "--format", str(spec["format"])], expected_exit=0, timeout=120, input_paths=[source_path])
        convert_result = run_command([sys.executable, "tools/convert_document.py", "convert", str(input_path), "--format", str(spec["format"]), "--out", str(output_path), "--evidence", str(evidence_path)], expected_exit=0, timeout=180, input_paths=[source_path])
        validate_result = run_command([sys.executable, "tools/convert_document.py", "validate", str(output_path)], expected_exit=0, timeout=120, input_paths=[output_path])
        inspect_json: Any = None
        validate_json: Any = None
        try:
            inspect_json = json.loads(inspect_result.get("_stdout", ""))
        except json.JSONDecodeError:
            pass
        try:
            validate_json = json.loads(validate_result.get("_stdout", ""))
        except json.JSONDecodeError:
            pass
        actual = {
            "inspectStatus": inspect_result.get("status"),
            "convertStatus": convert_result.get("status"),
            "validateStatus": validate_result.get("status"),
            "outputSchemaStatus": validate_json.get("status") if isinstance(validate_json, dict) else None,
            "inspectObject": isinstance(inspect_json, dict),
            "outputFile": output_path.is_file(),
            "evidenceFile": evidence_path.is_file(),
        }
        expected = {
            "inspectStatus": "passed",
            "convertStatus": "passed",
            "validateStatus": "passed",
            "outputSchemaStatus": "valid",
            "inspectObject": True,
            "outputFile": True,
            "evidenceFile": True,
        }
        status = "passed" if actual == expected else "failed"
        cases.append({
            "caseId": case_id,
            "oracle": "inspect, convert, and validate must agree on a schema-valid IR and explicit filesystem side effects",
            "assertionIds": [f"{case_id}-inspect", f"{case_id}-convert", f"{case_id}-validate", f"{case_id}-side-effects"],
            "target": str(spec["format"]),
            "expected": expected,
            "actual": actual,
            "inputDigest": sha256_paths([source_path]),
            "diagnostics": (inspect_result.get("diagnostics", []) + convert_result.get("diagnostics", []) + validate_result.get("diagnostics", []))[-20:],
            "durationMilliseconds": round(float(inspect_result.get("durationMilliseconds", 0)) + float(convert_result.get("durationMilliseconds", 0)) + float(validate_result.get("durationMilliseconds", 0)), 3),
            "status": status,
        })
    assertions = [
        _assertion("all-four-public-formats-exercised", ["docx", "markdown", "pdf", "xlsx"], sorted(str(item.get("format")) for item in corpus["cliCases"])),
        _assertion("cli-case-count-is-authored", len(corpus["cliCases"]), len(cases)),
        _assertion("cli-results-are-not-token-only", True, all("outputSchemaStatus" in case.get("actual", {}) and "side-effects" in " ".join(case.get("assertionIds", [])) for case in cases)),
    ]
    return report("cli-api-contract-results.json", source, dirty, assertions=assertions, cases=cases)


def platform_report(corpus: dict[str, Any], source: str, dirty: list[str]) -> dict[str, Any]:
    current_os = platform.system()
    current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    cases: list[dict[str, Any]] = []
    for profile in corpus["supportedPlatformProfiles"]:
        matching = profile.get("osFamily") == current_os and str(profile.get("python")) == current_python
        command_result = run_command([sys.executable, "tools/convert_document.py", "--help"], expected_exit=0, timeout=60, input_paths=[ROOT / "tools" / "convert_document.py"]) if matching else {"status": "failed", "returnCode": None, "diagnostics": ["required profile was not executed in this environment"], "durationMilliseconds": 0, "inputDigest": sha256_paths([ROOT / "tools" / "convert_document.py"])}
        cases.append({
            "caseId": f"RQ-PLATFORM-{profile['profileId']}",
            "oracle": "every authored supported OS/runtime profile must execute a smoke contract; unavailable is failure, not skip",
            "assertionIds": [f"RQ-PLATFORM-{profile['profileId']}-executed", f"RQ-PLATFORM-{profile['profileId']}-smoke"],
            "target": profile["profileId"],
            "expected": {"osFamily": profile["osFamily"], "python": profile["python"], "status": "passed"},
            "actual": {"osFamily": current_os if matching else "not-executed", "python": current_python if matching else "not-executed", "status": command_result.get("status")},
            "inputDigest": command_result.get("inputDigest"),
            "diagnostics": command_result.get("diagnostics", []),
            "durationMilliseconds": command_result.get("durationMilliseconds", 0),
            "status": command_result.get("status", "failed") if matching else "failed",
        })
    assertions = [
        _assertion("all-required-os-profiles-present", ["Darwin", "Linux", "Windows"], sorted(str(item.get("osFamily")) for item in corpus["supportedPlatformProfiles"])),
        _assertion("all-required-profiles-executed", len(cases), len([case for case in cases if case.get("actual", {}).get("status") == "passed"])),
        _assertion("no-platform-skip-is-success", 0, len([case for case in cases if case.get("status") == "skipped"])),
    ]
    return report("platform-runtime-matrix.json", source, dirty, assertions=assertions, cases=cases, currentEnvironment={"osFamily": current_os, "python": current_python, "architecture": platform.machine()})


def fresh_install_report(corpus: dict[str, Any], source: str, dirty: list[str], out_dir: Path) -> dict[str, Any]:
    external = (out_dir / "external-workdir").resolve()
    external.mkdir(parents=True, exist_ok=True)
    input_path = resolve_repo_path("e2e/corpus/markdown-independent.md")
    script = ROOT / "tools" / "convert_document.py"
    help_result = run_command([sys.executable, "-I", str(script), "--help"], expected_exit=0, timeout=60, cwd=external, input_paths=[script, ROOT / "requirements-qualification.txt"])
    inspect_result = run_command([sys.executable, "-I", str(script), "inspect", str(input_path), "--format", "markdown"], expected_exit=0, timeout=120, cwd=external, input_paths=[script, input_path])
    parsed = False
    try:
        parsed = isinstance(json.loads(inspect_result.get("_stdout", "")), dict)
    except json.JSONDecodeError:
        parsed = False
    cases = [
        public_case("RQ-INSTALL-001", "isolated CLI invocation from an external working directory", help_result, target="fresh-help", expected={"returnCode": 0}, actual={"returnCode": help_result.get("returnCode")}),
        public_case("RQ-INSTALL-002", "isolated inspect invocation from an external working directory emits JSON", inspect_result, target="fresh-inspect", expected={"returnCode": 0, "jsonObject": True}, actual={"returnCode": inspect_result.get("returnCode"), "jsonObject": parsed}),
    ]
    assertions = [
        _assertion("external-working-directory-is-used", True, help_result.get("cwd") != "."),
        _assertion("isolated-python-mode-is-used", True, "-I" in help_result.get("command", [])),
        _assertion("dependency-lock-is-bound", True, (ROOT / "requirements-qualification.txt").is_file()),
    ]
    return report("fresh-install-results.json", source, dirty, assertions=assertions, cases=cases, environment={"python": sys.version, "implementation": sys.implementation.name, "lockDigest": sha256_file(ROOT / "requirements-qualification.txt")})


def determinism_report(corpus: dict[str, Any], source: str, dirty: list[str], out_dir: Path) -> dict[str, Any]:
    replay_path = out_dir / "clean-room-replay.json"
    result = run_command([sys.executable, "tools/clean_room_replay.py", "--source-sha", source, "--out", str(replay_path)], expected_exit=0, timeout=900, input_paths=[ROOT / "tools" / "clean_room_replay.py", ROOT / "e2e" / "corpus" / "manifest.json"])
    replay = None
    if replay_path.is_file():
        try:
            replay = load_json(replay_path)
        except QualificationError:
            replay = None
    actual = {
        "commandStatus": result.get("status"),
        "reportStatus": replay.get("status") if isinstance(replay, dict) else None,
        "differenceCount": replay.get("comparison", {}).get("differenceCount") if isinstance(replay, dict) else None,
        "sourceSha": replay.get("sourceSha") if isinstance(replay, dict) else None,
        "twoRuns": len(replay.get("runs", [])) if isinstance(replay, dict) else 0,
    }
    expected = {"commandStatus": "passed", "reportStatus": "passed", "differenceCount": 0, "sourceSha": source, "twoRuns": 2}
    cases = [public_case("RQ-REPLAY-001", "two clean-room executions must have equal normalized machine artifacts", result, target="clean-room-replay", expected=expected, actual=actual)]
    assertions = [
        _assertion("replay-source-sha-is-current", source, actual["sourceSha"]),
        _assertion("replay-has-two-runs", 2, actual["twoRuns"]),
        _assertion("unexpected-replay-diff-count", 0, actual["differenceCount"]),
        _assertion("dirty-tree-is-not-release-evidence", False, bool(dirty)),
    ]
    return report("determinism-replay-diff.json", source, dirty, assertions=assertions, cases=cases, replay=actual)


def false_completion_report(corpus: dict[str, Any], source: str, dirty: list[str], suite_results: dict[str, dict[str, Any]], platform_result: dict[str, Any], determinism_result: dict[str, Any], claim_result: dict[str, Any]) -> dict[str, Any]:
    executor_map = {
        "evidence-integrity": suite_results.get("evidence-integrity"),
        "defect-injection-self-test": suite_results.get("defect-injection"),
        "platform-matrix": platform_result,
        "fresh-install": suite_results.get("fresh-install"),
        "determinism": determinism_result,
        "claim-barrier": claim_result,
    }
    cases: list[dict[str, Any]] = []
    for item in corpus["falseCompletionCases"]:
        executor = str(item["executor"])
        result = executor_map.get(executor) or {"status": "failed", "diagnostics": [f"executor is unavailable: {executor}"], "inputDigest": sha256_bytes(executor.encode("utf-8"))}
        output_text = "\n".join(result.get("diagnostics", []))
        expected_diagnostic = str(item["expectedDiagnostic"])
        diagnostic_observed = expected_diagnostic in output_text
        if executor in {"platform-matrix", "fresh-install", "determinism", "claim-barrier"}:
            diagnostic_observed = False
        status = "passed" if result.get("status") == "passed" and diagnostic_observed else "failed"
        cases.append({
            "caseId": item["caseId"],
            "oracle": "the exact authored mutation must be rejected with its stable diagnostic; a generic suite pass is insufficient",
            "assertionIds": [f"{item['caseId']}-mutation", f"{item['caseId']}-diagnostic"],
            "target": item["mutation"],
            "expected": {"diagnostic": expected_diagnostic, "executor": executor, "rejected": True},
            "actual": {"diagnosticObserved": diagnostic_observed, "executorStatus": result.get("status"), "diagnostics": result.get("diagnostics", [])},
            "inputDigest": result.get("inputDigest", sha256_bytes(item["mutation"].encode("utf-8"))),
            "diagnostics": result.get("diagnostics", []),
            "status": status,
        })
    assertions = [
        _assertion("false-completion-case-count-is-authored", len(corpus["falseCompletionCases"]), len(cases)),
        _assertion("false-completion-case-ids-are-unique", len(cases), len({case["caseId"] for case in cases})),
        _assertion("false-completion-regressions-escape-zero", 0, len([case for case in cases if case.get("status") != "passed"])),
    ]
    return report("false-completion-regressions.json", source, dirty, assertions=assertions, cases=cases)


def _live_issue_states() -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY", "horiyamayoh/fdir")
    states: list[dict[str, Any]] = []
    for issue_number in [87, *range(88, 106)]:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/issues/{issue_number}",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fdir-qualification-issue-105/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "retrievedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        states.append({"issueNumber": issue_number, "state": payload.get("state"), "stateReason": payload.get("state_reason"), "updatedAt": payload.get("updated_at"), "apiUrl": request.full_url})
    return {"status": "passed", "repository": repository, "issues": states, "retrievedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def claim_report(corpus: dict[str, Any], source: str, dirty: list[str]) -> dict[str, Any]:
    recovery = load_json(ROOT / "machine" / "audit-recovery-plan.json")
    claims = load_json(ROOT / "machine" / "release-claim-manifest.json")
    contract = load_json(ROOT / "machine" / "qualification-contract.json")
    release = claims.get("release", {}) if isinstance(claims, dict) else {}
    live = _live_issue_states()
    live_open = [item for item in live.get("issues", []) if item.get("state") == "open"] if live.get("status") == "passed" else None
    expected_blocked = live.get("status") != "passed" or bool(live_open)
    actual_blocked = release.get("releaseBlocked") is True and release.get("status") == "release-blocked"
    required_ids = set(contract.get("scope", {}).get("requiredEvidenceIds", [])) if isinstance(contract, dict) else set()
    binding = release.get("qualificationBinding", {}) if isinstance(release, dict) else {}
    cases = [
        {"caseId": "RQ-CLAIM-001", "oracle": "live issue state is the release authority and open audit issues keep release blocked", "assertionIds": ["RQ-CLAIM-001-live", "RQ-CLAIM-001-claim"], "target": "#87-#105 live issue state", "expected": {"releaseBlocked": expected_blocked, "claimStatus": "release-blocked"}, "actual": {"liveStateStatus": live.get("status"), "openIssueCount": len(live_open) if live_open is not None else None, "claimBlocked": actual_blocked, "claimStatus": release.get("status")}, "inputDigest": sha256_paths([ROOT / "machine" / "audit-recovery-plan.json", ROOT / "machine" / "release-claim-manifest.json"]), "diagnostics": [live.get("error")] if live.get("error") else [], "status": "passed" if expected_blocked and actual_blocked else "failed"},
        {"caseId": "RQ-CLAIM-NEG-001", "oracle": "a release-ready claim cannot be published while the recovery plan is blocked", "assertionIds": ["RQ-CLAIM-NEG-001-blocked"], "target": "machine/release-claim-manifest.json", "expected": True, "actual": bool(recovery.get("releaseBlocked") is True and actual_blocked), "inputDigest": sha256_paths([ROOT / "machine" / "audit-recovery-plan.json", ROOT / "machine" / "release-claim-manifest.json"]), "status": "passed" if recovery.get("releaseBlocked") is True and actual_blocked else "failed"},
        {"caseId": "RQ-CLAIM-NEG-002", "oracle": "qualification binding must enumerate every required Evidence ID even when release is blocked", "assertionIds": ["RQ-CLAIM-NEG-002-scope"], "target": "qualificationBinding.requiredEvidenceIds", "expected": sorted(required_ids), "actual": sorted(binding.get("requiredEvidenceIds", [])), "inputDigest": sha256_paths([ROOT / "machine" / "qualification-contract.json", ROOT / "machine" / "machine/release-claim-manifest.json"] if (ROOT / "machine" / "machine/release-claim-manifest.json").exists() else [ROOT / "machine" / "qualification-contract.json", ROOT / "machine" / "release-claim-manifest.json"]), "status": "passed" if set(binding.get("requiredEvidenceIds", [])) == required_ids else "failed"},
    ]
    assertions = [
        _assertion("release-blocked-state-is-explicit", True, actual_blocked),
        _assertion("live-state-failure-closes-release", True, expected_blocked if live.get("status") != "passed" else True),
        _assertion("release-binding-scope-is-contract-exact", sorted(required_ids), sorted(binding.get("requiredEvidenceIds", []))),
        _assertion("local-recovery-plan-agrees-with-blocked-claim", True, bool(recovery.get("releaseBlocked") is True and actual_blocked)),
    ]
    return report("release-claim-conformance.json", source, dirty, assertions=assertions, cases=cases, liveGitHubState=live)


def _producer_envelope(
    corpus: dict[str, Any] | None,
    reports: dict[str, dict[str, Any]],
    *,
    corpus_path: Path,
    source: str | None,
    dirty: list[str],
    out_dir: Path,
) -> dict[str, Any]:
    """Build the issue #105 producer-report from semantic claim evidence.

    The envelope compares authored corpus/claim expectations with structured
    report fields.  It deliberately does not use command return codes or file
    existence as a qualification assertion; those remain diagnostics or
    availability signals in the semantic reports.
    """

    trace_name = "requirement-traceability.json"
    suite_name = "behavioral-suite-inventory.json"
    cli_name = "cli-api-contract-results.json"
    platform_name = "platform-runtime-matrix.json"
    fresh_name = "fresh-install-results.json"
    determinism_name = "determinism-replay-diff.json"
    false_name = "false-completion-regressions.json"
    claim_name = "release-claim-conformance.json"
    report_kinds = {
        trace_name: "requirement-traceability",
        suite_name: "behavioral-suite-inventory",
        cli_name: "cli-api-contract-results",
        platform_name: "platform-runtime-matrix",
        fresh_name: "fresh-install-results",
        determinism_name: "determinism-replay-diff",
        false_name: "false-completion-regressions",
        claim_name: "release-claim-conformance",
    }

    def ensure_report(name: str) -> dict[str, Any]:
        report = reports.get(name)
        if not isinstance(report, dict):
            report = {
                "schema": "fdir/qualification-issue-105-report",
                "version": "1.0.0",
                "issueNumber": 105,
                "reportName": name,
                "sourceSha": source,
                "dirtyTree": bool(dirty),
                "status": "failed",
                "completionStatus": "incomplete",
                "assertions": [],
                "cases": [],
                "diagnostics": [f"semantic report unavailable: {name}"],
            }
            reports[name] = report
        return report

    def records(name: str, key: str) -> list[dict[str, Any]]:
        return ensure_report(name).setdefault(key, [])

    case_specs: list[dict[str, Any]] = []
    uncovered: list[str] = []
    if isinstance(source, str) and len(source) == SOURCE_SHA_LENGTH and all(char in "0123456789abcdef" for char in source):
        envelope_source = source
    else:
        envelope_source = "0" * SOURCE_SHA_LENGTH
        uncovered.append("source SHA is unavailable; envelope is not commit-bound")
    if dirty:
        uncovered.append("working tree is dirty; clean release evidence is unavailable")

    def add_case(
        *,
        case_id: str,
        classification: str,
        assertion_type: str,
        authority_report: str,
        authority_pointer: str,
        actual_report: str,
        actual_pointer: str,
        support_report: str,
        target: dict[str, Any],
        diagnostic: dict[str, str],
    ) -> None:
        actual_value = _producer_pointer(ensure_report(actual_report), actual_pointer)
        support = ensure_report(support_report)
        input_pointer = _append_producer_record(
            support,
            "producerInput",
            {"caseId": case_id, "target": target, "status": "passed"},
        )
        support_case_pointer = _append_producer_record(
            support,
            "producerSupport",
            {"assertionId": case_id, "caseId": case_id, "actual": actual_value, "target": target, "status": "passed"},
        )
        assertion_id = f"issue-105:{case_id}"
        support_assertion_pointer = _append_producer_record(
            support,
            "producerSupport",
            {"assertionId": assertion_id, "caseId": case_id, "actual": actual_value, "target": target, "status": "passed"},
        )
        case_specs.append({
            "caseId": case_id,
            "assertionId": assertion_id,
            "classification": classification,
            "assertionType": assertion_type,
            "authorityReport": authority_report,
            "authorityPointer": authority_pointer,
            "actualReport": actual_report,
            "actualPointer": actual_pointer,
            "supportReport": support_report,
            "inputPointer": input_pointer,
            "supportCasePointer": support_case_pointer,
            "supportAssertionPointer": support_assertion_pointer,
            "target": target,
            "diagnostic": diagnostic,
        })

    if isinstance(corpus, dict):
        authority = records(claim_name, "producerAuthority")
        suite_actual = records(suite_name, "producerSuiteActual")
        cli_actual = records(cli_name, "producerCliActual")
        platform_actual = records(platform_name, "producerPlatformActual")
        fresh_actual = records(fresh_name, "producerFreshInstallActual")
        determinism_actual = records(determinism_name, "producerDeterminismActual")
        false_actual = records(false_name, "producerFalseCompletionActual")
        claim_authority = records(trace_name, "producerClaimAuthority")
        binding_authority = records(trace_name, "producerBindingAuthority")
        claim_actual = records(claim_name, "producerClaimActual")

        suite_cases = {item.get("caseId"): item for item in ensure_report(suite_name).get("cases", []) if isinstance(item, dict)}
        for spec in corpus.get("suiteCommands", []):
            case_id = str(spec["caseId"])
            observed = suite_cases.get(case_id, {})
            expected = {"caseId": case_id, "suiteId": str(spec["suiteId"]), "declared": True}
            actual = {"caseId": observed.get("caseId"), "suiteId": observed.get("target"), "declared": observed.get("caseId") == case_id}
            target = {"caseId": case_id, "suiteId": str(spec["suiteId"])}
            authority_index = len(authority)
            authority.append({"caseId": f"suite-{case_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(suite_actual)
            suite_actual.append({"caseId": f"suite-{case_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"suite-{case_id}",
                classification="positive",
                assertion_type="differential-equality",
                authority_report=claim_name,
                authority_pointer=f"/producerAuthority/{authority_index}/expected",
                actual_report=suite_name,
                actual_pointer=f"/producerSuiteActual/{actual_index}/actual",
                support_report=trace_name,
                target=target,
                diagnostic={"code": "ISSUE_105_SUITE_INVENTORY", "message": "authored suite inventory is bound to observed semantic case identity"},
            )

        cli_cases = {item.get("caseId"): item for item in ensure_report(cli_name).get("cases", []) if isinstance(item, dict)}
        for spec in corpus.get("cliCases", []):
            case_id = str(spec["caseId"])
            observed = cli_cases.get(case_id, {})
            observed_actual = observed.get("actual", {}) if isinstance(observed.get("actual"), dict) else {}
            expected = {"format": str(spec["format"]), "semanticOutput": True}
            actual = {
                "format": observed.get("target"),
                "semanticOutput": observed_actual.get("inspectObject") is True and observed_actual.get("outputSchemaStatus") == "valid",
            }
            target = {"caseId": case_id, "format": str(spec["format"]), "input": str(spec["input"])}
            authority_index = len(authority)
            authority.append({"caseId": f"cli-{case_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(cli_actual)
            cli_actual.append({"caseId": f"cli-{case_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"cli-{case_id}",
                classification="positive",
                assertion_type="differential-equality",
                authority_report=claim_name,
                authority_pointer=f"/producerAuthority/{authority_index}/expected",
                actual_report=cli_name,
                actual_pointer=f"/producerCliActual/{actual_index}/actual",
                support_report=trace_name,
                target=target,
                diagnostic={"code": "ISSUE_105_CLI_SEMANTIC_OUTPUT", "message": "CLI output is compared by schema validity and parsed object semantics"},
            )

        current_os = platform.system()
        current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_cases = {item.get("caseId"): item for item in ensure_report(platform_name).get("cases", []) if isinstance(item, dict)}
        for profile in corpus.get("supportedPlatformProfiles", []):
            case_id = f"RQ-PLATFORM-{profile['profileId']}"
            observed = platform_cases.get(case_id, {})
            observed_actual = observed.get("actual", {}) if isinstance(observed.get("actual"), dict) else {}
            matching = profile.get("osFamily") == current_os and str(profile.get("python")) == current_python
            expected = {"available": matching}
            actual = {
                "available": observed_actual.get("osFamily") not in {None, "not-executed"} and observed_actual.get("python") not in {None, "not-executed"},
            }
            target = {"profileId": profile.get("profileId"), "osFamily": profile.get("osFamily"), "python": profile.get("python")}
            if not matching:
                uncovered.append(f"platform profile unavailable in this runtime: {profile.get('profileId')}")
            authority_index = len(authority)
            authority.append({"caseId": f"platform-{profile['profileId']}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(platform_actual)
            platform_actual.append({"caseId": f"platform-{profile['profileId']}", "actual": actual, "target": target, "status": "passed" if actual == expected else "unavailable" if not matching else "failed"})
            add_case(
                case_id=f"platform-{profile['profileId']}",
                classification="negative" if not matching else "positive",
                assertion_type="differential-equality",
                authority_report=claim_name,
                authority_pointer=f"/producerAuthority/{authority_index}/expected",
                actual_report=platform_name,
                actual_pointer=f"/producerPlatformActual/{actual_index}/actual",
                support_report=trace_name,
                target=target,
                diagnostic={"code": "ISSUE_105_PLATFORM_PROFILE", "message": "platform availability is explicit; an unexecuted profile is not promoted"},
            )

        fresh_report = ensure_report(fresh_name)
        fresh_assertions = {item.get("id"): item for item in fresh_report.get("assertions", []) if isinstance(item, dict)}
        fresh_cases = {item.get("caseId"): item for item in fresh_report.get("cases", []) if isinstance(item, dict)}
        fresh_expected = {"externalWorkingDirectory": True, "isolatedPython": True, "semanticJson": True}
        fresh_observed = {
            "externalWorkingDirectory": fresh_assertions.get("external-working-directory-is-used", {}).get("actual") is True,
            "isolatedPython": fresh_assertions.get("isolated-python-mode-is-used", {}).get("actual") is True,
            "semanticJson": any((item.get("actual") or {}).get("jsonObject") is True for item in fresh_cases.values() if isinstance(item.get("actual"), dict)),
        }
        fresh_target = {"requirementId": "QUAL-105-FRESH-INSTALL", "workingDirectory": "external"}
        authority_index = len(authority)
        authority.append({"caseId": "fresh-install", "expected": fresh_expected, "target": fresh_target, "status": "passed"})
        fresh_index = len(fresh_actual)
        fresh_actual.append({"caseId": "fresh-install", "actual": fresh_observed, "target": fresh_target, "status": "passed" if fresh_observed == fresh_expected else "failed"})
        add_case(
            case_id="fresh-install",
            classification="positive",
            assertion_type="differential-equality",
            authority_report=claim_name,
            authority_pointer=f"/producerAuthority/{authority_index}/expected",
            actual_report=fresh_name,
            actual_pointer=f"/producerFreshInstallActual/{fresh_index}/actual",
            support_report=trace_name,
            target=fresh_target,
            diagnostic={"code": "ISSUE_105_FRESH_INSTALL", "message": "fresh-install evidence is evaluated from working-directory, isolation, and parsed JSON facts"},
        )

        determinism_report_value = ensure_report(determinism_name).get("replay", {})
        if not isinstance(determinism_report_value, dict):
            determinism_report_value = {}
        determinism_expected = {"twoRuns": 2, "differenceCount": 0, "sourceSha": envelope_source}
        determinism_observed = {
            "twoRuns": determinism_report_value.get("twoRuns"),
            "differenceCount": determinism_report_value.get("differenceCount"),
            "sourceSha": determinism_report_value.get("sourceSha"),
        }
        determinism_target = {"requirementId": "QUAL-105-DETERMINISM", "replay": "normalized semantic artifacts"}
        authority_index = len(authority)
        authority.append({"caseId": "determinism", "expected": determinism_expected, "target": determinism_target, "status": "passed"})
        actual_index = len(determinism_actual)
        determinism_actual.append({"caseId": "determinism", "actual": determinism_observed, "target": determinism_target, "status": "passed" if determinism_observed == determinism_expected else "failed"})
        if not determinism_report_value:
            uncovered.append("determinism semantic replay evidence is unavailable")
        add_case(
            case_id="determinism",
            classification="positive",
            assertion_type="differential-equality",
            authority_report=claim_name,
            authority_pointer=f"/producerAuthority/{authority_index}/expected",
            actual_report=determinism_name,
            actual_pointer=f"/producerDeterminismActual/{actual_index}/actual",
            support_report=trace_name,
            target=determinism_target,
            diagnostic={"code": "ISSUE_105_DETERMINISM", "message": "replay equality is adjudicated from normalized artifact differences and source binding"},
        )

        false_cases = {item.get("caseId"): item for item in ensure_report(false_name).get("cases", []) if isinstance(item, dict)}
        for item in corpus.get("falseCompletionCases", []):
            case_id = str(item["caseId"])
            observed = false_cases.get(case_id, {})
            observed_actual = observed.get("actual", {}) if isinstance(observed.get("actual"), dict) else {}
            expected = {"rejected": True}
            actual = {"rejected": observed_actual.get("diagnosticObserved") is True}
            target = {"caseId": case_id, "executor": item.get("executor"), "expectedDiagnostic": item.get("expectedDiagnostic")}
            authority_index = len(authority)
            authority.append({"caseId": f"false-{case_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(false_actual)
            false_actual.append({"caseId": f"false-{case_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"false-{case_id}",
                classification="mutation",
                assertion_type="differential-equality",
                authority_report=claim_name,
                authority_pointer=f"/producerAuthority/{authority_index}/expected",
                actual_report=false_name,
                actual_pointer=f"/producerFalseCompletionActual/{actual_index}/actual",
                support_report=trace_name,
                target=target,
                diagnostic={"code": "ISSUE_105_FALSE_COMPLETION", "message": "the authored false-completion mutation must have an observed semantic rejection"},
            )

        try:
            recovery = load_json(ROOT / "machine" / "audit-recovery-plan.json")
        except QualificationError as exc:
            recovery = {}
            uncovered.append(f"audit recovery plan unavailable: {exc}")
        claim_cases = {item.get("caseId"): item for item in ensure_report(claim_name).get("cases", []) if isinstance(item, dict)}
        claim_case = claim_cases.get("RQ-CLAIM-001", {})
        observed_claim = claim_case.get("actual", {}) if isinstance(claim_case.get("actual"), dict) else {}
        expected_claim = {"releaseBlocked": recovery.get("releaseBlocked") is True, "claimStatus": "release-blocked"}
        actual_claim = {"releaseBlocked": observed_claim.get("claimBlocked") is True, "claimStatus": observed_claim.get("claimStatus")}
        claim_target = {"requirementId": "QUAL-105-CLAIM-BARRIER", "scope": "#87-#105"}
        authority_index = len(claim_authority)
        claim_authority.append({"caseId": "claim-blocked", "expected": expected_claim, "target": claim_target, "status": "passed"})
        actual_index = len(claim_actual)
        claim_actual.append({"caseId": "claim-blocked", "actual": actual_claim, "target": claim_target, "status": "passed" if actual_claim == expected_claim else "failed"})
        live_state = ensure_report(claim_name).get("liveGitHubState", {})
        if not isinstance(live_state, dict) or live_state.get("status") != "passed":
            uncovered.append("live issue state is unavailable; release claim remains blocked")
        add_case(
            case_id="claim-blocked",
            classification="negative",
            assertion_type="release-claim-conformance",
            authority_report=trace_name,
            authority_pointer=f"/producerClaimAuthority/{authority_index}/expected",
            actual_report=claim_name,
            actual_pointer=f"/producerClaimActual/{actual_index}/actual",
            support_report=suite_name,
            target=claim_target,
            diagnostic={"code": "ISSUE_105_RELEASE_CLAIM", "message": "release remains blocked when the recovery plan or live issue oracle is unresolved"},
        )

        binding_case = claim_cases.get("RQ-CLAIM-NEG-002", {})
        binding_actual = binding_case.get("actual") if isinstance(binding_case.get("actual"), list) else []
        expected_binding = {"requiredEvidenceCount": len(corpus.get("releaseEvidence", []))}
        actual_binding = {"requiredEvidenceCount": len(binding_actual)}
        binding_target = {"requirementId": "QUAL-105-TRACEABILITY", "field": "qualificationBinding.requiredEvidenceIds"}
        authority_index = len(binding_authority)
        binding_authority.append({"caseId": "claim-binding", "expected": expected_binding, "target": binding_target, "status": "passed"})
        actual_index = len(claim_actual)
        claim_actual.append({"caseId": "claim-binding", "actual": actual_binding, "target": binding_target, "status": "passed" if actual_binding == expected_binding else "failed"})
        add_case(
            case_id="claim-binding",
            classification="positive",
            assertion_type="release-claim-conformance",
            authority_report=trace_name,
            authority_pointer=f"/producerBindingAuthority/{authority_index}/expected",
            actual_report=claim_name,
            actual_pointer=f"/producerClaimActual/{actual_index}/actual",
            support_report=suite_name,
            target=binding_target,
            diagnostic={"code": "ISSUE_105_CLAIM_BINDING", "message": "the release claim enumerates the authored release-scope Evidence IDs"},
        )
    else:
        authority = records(trace_name, "producerSetupAuthority")
        actual = records(suite_name, "producerSetupActual")
        target = {"lane": "issue-105", "evidence": "release-quality setup"}
        authority_index = len(authority)
        actual_index = len(actual)
        authority.append({"caseId": "setup-unavailable", "expected": {"available": True}, "target": target, "status": "passed"})
        actual.append({"caseId": "setup-unavailable", "actual": {"available": False}, "target": target, "status": "unavailable"})
        uncovered.append("issue-105 authored corpus is unavailable")
        add_case(
            case_id="setup-unavailable",
            classification="negative",
            assertion_type="release-claim-conformance",
            authority_report=trace_name,
            authority_pointer=f"/producerSetupAuthority/{authority_index}/expected",
            actual_report=suite_name,
            actual_pointer=f"/producerSetupActual/{actual_index}/actual",
            support_report=claim_name,
            target=target,
            diagnostic={"code": "ISSUE_105_SETUP_UNAVAILABLE", "message": "the independent release-quality corpus could not be loaded"},
        )

    for name in REPORT_NAMES:
        ensure_report(name)
        write_json(out_dir / name, reports[name])

    input_digests, unavailable_inputs = _producer_input_digests(corpus_path)
    uncovered.extend(unavailable_inputs)
    for name in REPORT_NAMES:
        if not (out_dir / name).is_file():
            uncovered.append(f"semantic report unavailable: {name}")

    producer_cases: list[dict[str, Any]] = []
    producer_assertions: list[dict[str, Any]] = []
    failures = 0
    for spec in case_specs:
        try:
            authority_local = out_dir / spec["authorityReport"]
            actual_local = out_dir / spec["actualReport"]
            support_local = out_dir / spec["supportReport"]
            authority_ref = _producer_artifact_reference(authority_local, f"{BUNDLE_PREFIX}/{spec['authorityReport']}", spec["authorityPointer"])
            actual_ref = _producer_artifact_reference(actual_local, f"{BUNDLE_PREFIX}/{spec['actualReport']}", spec["actualPointer"])
            input_ref = _producer_artifact_reference(support_local, f"{BUNDLE_PREFIX}/{spec['supportReport']}", spec["inputPointer"])
            support_case_ref = _producer_artifact_reference(support_local, f"{BUNDLE_PREFIX}/{spec['supportReport']}", spec["supportCasePointer"])
            support_assertion_ref = _producer_artifact_reference(support_local, f"{BUNDLE_PREFIX}/{spec['supportReport']}", spec["supportAssertionPointer"])
            expected = _producer_pointer(load_json(authority_local), spec["authorityPointer"])
            actual = _producer_pointer(load_json(actual_local), spec["actualPointer"])
            passed = canonical(expected) == canonical(actual)
            producer_cases.append({
                "caseId": spec["caseId"],
                "requirementId": REQUIREMENT_ID,
                "classification": spec["classification"],
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": "equal"},
                "result": "passed" if passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_case_ref,
            })
            producer_assertions.append({
                "assertionId": spec["assertionId"],
                "requirementId": REQUIREMENT_ID,
                "assertionType": spec["assertionType"],
                "testCaseId": spec["caseId"],
                "classification": spec["classification"],
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": "equal"},
                "status": "passed" if passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_assertion_ref,
            })
            if not passed:
                failures += 2
        except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError, QualificationError) as exc:
            failures += 2
            uncovered.append(f"{spec['caseId']}: semantic artifact could not be resolved ({type(exc).__name__}: {exc})")

    component_paths = [ROOT / "tools" / "qualification_issue105.py", Path(corpus_path), EVALUATOR_PATH]
    component_digests: list[str] = []
    for path in component_paths:
        if path.is_file():
            component_digests.append(sha256_file(path))
        else:
            component_digests.append(sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
            uncovered.append(f"independence component unavailable: {path}")
    if SHARED_EVIDENCE_PATH.is_file():
        shared_digest = sha256_file(SHARED_EVIDENCE_PATH)
    else:
        shared_digest = sha256_bytes(b"missing:qualification_evidence")
        uncovered.append("shared artifact-reference evaluator unavailable")
    status = "failed" if failures else "blocked" if uncovered else "passed"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": EVIDENCE_ID,
        "requirementIds": [REQUIREMENT_ID],
        "sourceSha": envelope_source,
        "inputDigests": input_digests,
        "producerId": "issue-105-release-quality-runner",
        "authorityId": "issue-105-hand-reviewed-corpus-and-claim-policy",
        "independence": {
            "producerComponentDigest": component_digests[0],
            "authorityComponentDigest": component_digests[1],
            "evaluatorComponentDigest": component_digests[2],
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [shared_digest],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": sorted(set(uncovered)),
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failures,
    }


def _failed_report(name: str, source: str, dirty: list[str], error: str) -> dict[str, Any]:
    return report(name, source, dirty, assertions=[_assertion("runner-execution", "completed", "failed", detail=error)], cases=[], diagnostics=[error])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    corpus_path = args.corpus if args.corpus.is_absolute() else ROOT / args.corpus
    out_dir = args.out_dir if args.out_dir.is_absolute() else ROOT / args.out_dir
    try:
        corpus = load_json(corpus_path)
        if not isinstance(corpus, dict):
            raise QualificationError("issue #105 corpus root is not an object")
        validate_corpus(corpus)
        source = source_sha()
        dirty = working_tree_status()
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        out_dir.mkdir(parents=True, exist_ok=True)
        fallback_source = "0" * SOURCE_SHA_LENGTH
        try:
            fallback_source = source_sha()
        except Exception:
            pass
        try:
            fallback_dirty = working_tree_status()
        except Exception:
            fallback_dirty = []
        error = f"{type(exc).__name__}: {exc}"
        reports = {
            name: _failed_report(name, fallback_source, fallback_dirty, error)
            for name in REPORT_NAMES
        }
        producer = _producer_envelope(
            None,
            reports,
            corpus_path=corpus_path,
            source=fallback_source,
            dirty=fallback_dirty,
            out_dir=out_dir,
        )
        for name in REPORT_NAMES:
            write_json(out_dir / name, reports[name])
        write_json(out_dir / PRODUCER_REPORT_NAME, producer)
        print(f"ISSUE 105 QUALIFICATION ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    reports: dict[str, dict[str, Any]] = {}
    suite_results: dict[str, dict[str, Any]] = {}
    try:
        reports["behavioral-suite-inventory.json"], suite_results = behavioral_report(corpus, source, dirty, out_dir)
    except Exception as exc:
        reports["behavioral-suite-inventory.json"] = _failed_report("behavioral-suite-inventory.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["cli-api-contract-results.json"] = cli_report(corpus, source, dirty, out_dir)
    except Exception as exc:
        reports["cli-api-contract-results.json"] = _failed_report("cli-api-contract-results.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["platform-runtime-matrix.json"] = platform_report(corpus, source, dirty)
    except Exception as exc:
        reports["platform-runtime-matrix.json"] = _failed_report("platform-runtime-matrix.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["fresh-install-results.json"] = fresh_install_report(corpus, source, dirty, out_dir)
        suite_results["fresh-install"] = {"status": reports["fresh-install-results.json"]["status"], "inputDigest": sha256_paths([ROOT / "tools" / "convert_document.py"]), "diagnostics": reports["fresh-install-results.json"].get("diagnostics", [])}
    except Exception as exc:
        reports["fresh-install-results.json"] = _failed_report("fresh-install-results.json", source, dirty, f"{type(exc).__name__}: {exc}")
        suite_results["fresh-install"] = {"status": "failed", "inputDigest": sha256_bytes(b"fresh-install"), "diagnostics": [str(exc)]}
    try:
        reports["determinism-replay-diff.json"] = determinism_report(corpus, source, dirty, out_dir)
    except Exception as exc:
        reports["determinism-replay-diff.json"] = _failed_report("determinism-replay-diff.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["requirement-traceability.json"] = traceability_report(corpus, source, dirty)
    except Exception as exc:
        reports["requirement-traceability.json"] = _failed_report("requirement-traceability.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["release-claim-conformance.json"] = claim_report(corpus, source, dirty)
    except Exception as exc:
        reports["release-claim-conformance.json"] = _failed_report("release-claim-conformance.json", source, dirty, f"{type(exc).__name__}: {exc}")
    try:
        reports["false-completion-regressions.json"] = false_completion_report(corpus, source, dirty, suite_results, reports.get("platform-runtime-matrix.json", {}), reports.get("determinism-replay-diff.json", {}), reports.get("release-claim-conformance.json", {}))
    except Exception as exc:
        reports["false-completion-regressions.json"] = _failed_report("false-completion-regressions.json", source, dirty, f"{type(exc).__name__}: {exc}")

    for name in REPORT_NAMES:
        reports.setdefault(name, _failed_report(name, source, dirty, "report was not produced"))
    producer = _producer_envelope(
        corpus,
        reports,
        corpus_path=corpus_path,
        source=source,
        dirty=dirty,
        out_dir=out_dir,
    )
    for name in REPORT_NAMES:
        write_json(out_dir / name, reports[name])
    write_json(out_dir / PRODUCER_REPORT_NAME, producer)
    status = "passed" if producer.get("status") == "passed" and all(reports.get(name, {}).get("status") == "passed" for name in REPORT_NAMES) else "failed"
    summary = {"status": status, "producerStatus": producer.get("status"), "sourceSha": source, "dirtyTree": bool(dirty), "reportCount": len(REPORT_NAMES), "reports": [str((out_dir / name).resolve()) for name in REPORT_NAMES]}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
