"""Build and verify commit-bound qualification evidence.

The bundle is deliberately a small execution ledger, not a claim generator.
It records the exact source revision, executed argv, exit status, stdout/stderr
digests, and every file digest used by the run.  Verification reads the bytes
again; labels, counts, and file existence alone cannot make a bundle valid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "qualification-evidence.schema.json"
REQUIREMENTS_PATH = ROOT / "machine" / "release-requirements.json"
AUDIT_PATH = ROOT / "machine" / "audit-recovery-plan.json"
REGRESSIONS_PATH = ROOT / "machine" / "false-completion-regressions.json"
HEX64 = set("0123456789abcdef")
DEFAULT_COMMAND_TIMEOUT_SECONDS = 900
LIVE_ISSUE_NUMBERS = tuple(range(87, 106))


class EvidenceError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PersistentDirectory:
    """Create an inherited-ACL scratch directory without tempfile's 0700 ACL."""

    def __init__(self, parent: Path, prefix: str):
        parent.mkdir(parents=True, exist_ok=True)
        candidate = parent / f"{prefix}{os.getpid()}"
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = parent / f"{prefix}{os.getpid()}-{counter}"
        candidate.mkdir()
        self.path = candidate

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_: Any) -> bool:
        return False


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest_file(path: Path) -> tuple[str, int]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EvidenceError("ARTIFACT_UNREADABLE", f"{path}: {exc}") from exc
    return digest_bytes(data), len(data)


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise EvidenceError("PATH_OUTSIDE_ROOT", str(path)) from exc


def root_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate_issue_snapshot(value: Any) -> dict[str, Any]:
    """Validate the live GitHub issue state bound into final evidence."""

    if not isinstance(value, dict):
        raise EvidenceError("ISSUE_SNAPSHOT_SCHEMA", "live issue snapshot is not an object")
    if value.get("schema") != "fdir/github-issue-state-snapshot" or value.get("version") != "1.0.0":
        raise EvidenceError("ISSUE_SNAPSHOT_SCHEMA", "unsupported live issue snapshot schema")
    if value.get("repository") != "horiyamayoh/fdir" or value.get("source") != "github-issue-api":
        raise EvidenceError("ISSUE_SNAPSHOT_SOURCE", "live issue snapshot is not bound to the GitHub issue API")
    issues = value.get("issues")
    if not isinstance(issues, list):
        raise EvidenceError("ISSUE_SNAPSHOT_CASES", "live issue snapshot has no issue list")
    numbers = [item.get("number") for item in issues if isinstance(item, dict)]
    if set(numbers) != set(LIVE_ISSUE_NUMBERS) or len(numbers) != len(set(numbers)):
        raise EvidenceError("ISSUE_SNAPSHOT_SCOPE", "live issue snapshot must cover exactly #87-#105")
    for item in issues:
        if not isinstance(item, dict) or item.get("state") != "closed" or item.get("stateReason") != "completed":
            raise EvidenceError("ISSUE_SNAPSHOT_NOT_COMPLETE", f"issue #{item.get('number') if isinstance(item, dict) else '<unknown>'} is not closed as completed")
    return {
        "status": "passed",
        "source": "github-issue-api",
        "issueNumbers": list(LIVE_ISSUE_NUMBERS),
        "closedCompleted": len(issues),
    }


def load_issue_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("ISSUE_SNAPSHOT_UNREADABLE", str(exc)) from exc
    summary = validate_issue_snapshot(value)
    sha, size = digest_file(path)
    return value, {**summary, "path": relative(path), "sha256": sha, "bytes": size}


def git(*args: str, check: bool = True) -> str:
    completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if check and completed.returncode != 0:
        raise EvidenceError("GIT_FAILED", (completed.stderr or completed.stdout).strip())
    return completed.stdout.strip()


def source_state() -> dict[str, Any]:
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain", "--untracked-files=all")
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", check=True)
    hasher = hashlib.sha256()
    names_bytes = names.encode("utf-8", "surrogatepass")
    for name in sorted(item for item in names_bytes.split(b"\0") if item):
        path = ROOT / name.decode("utf-8")
        if not path.is_file():
            raise EvidenceError("SOURCE_FILE_MISSING", name.decode("utf-8"))
        data = path.read_bytes()
        hasher.update(len(name).to_bytes(8, "big"))
        hasher.update(name)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return {
        "headSha": head,
        "treeSha": tree,
        "trackedDigest": hasher.hexdigest(),
        "workingTreeClean": not bool(status),
        "binding": "exact-commit" if not status else "dirty-working-tree",
    }


def artifact(path: Path, kind: str, artifact_id: str, command_id: str | None = None) -> dict[str, Any]:
    sha, size = digest_file(path)
    value: dict[str, Any] = {"id": artifact_id, "path": relative(path), "kind": kind, "sha256": sha, "bytes": size}
    if command_id:
        value["commandId"] = command_id
    return value


def digest_ref(path: Path) -> dict[str, Any]:
    sha, size = digest_file(path)
    return {"path": relative(path), "sha256": sha, "bytes": size}


def index_for(bundle: dict[str, Any]) -> dict[str, Any]:
    entries = []
    commands = bundle.get("commands", [])
    for item in sorted(bundle.get("artifacts", []), key=lambda value: value["id"]):
        command_ids = [command["id"] for command in commands if any(ref.get("path") == item["path"] for ref in command.get("outputDigests", []))]
        entries.append({
            "evidenceId": item["id"],
            "artifactId": item["id"],
            "sha256": item["sha256"],
            "sourceHeadSha": bundle["source"]["headSha"],
            "commandIds": sorted(command_ids),
        })
    result = {"schema": "fdir/qualification-evidence-index", "version": "1.0.0", "entries": entries}
    result["digest"] = digest_bytes(canonical(result))
    return result


def bundle_digest(bundle: dict[str, Any]) -> str:
    copy = json.loads(json.dumps(bundle))
    copy.get("integrity", {}).pop("bundleDigest", None)
    return digest_bytes(canonical(copy))


def write_bundle(bundle: dict[str, Any], output: Path, index_path: Path) -> None:
    index = index_for(bundle)
    bundle["index"] = {"schema": index["schema"], "version": index["version"], "path": relative(index_path), "entries": index["entries"], "digest": index["digest"]}
    bundle.setdefault("integrity", {})["indexDigest"] = index["digest"]
    bundle["integrity"]["algorithm"] = "sha256"
    bundle["integrity"]["bundleDigest"] = bundle_digest(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical(bundle))
    index_path.write_bytes(canonical(index))


def _hash_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def validate_shape(bundle: dict[str, Any]) -> None:
    required = {"schema", "version", "repository", "source", "commands", "artifacts", "index", "barrier", "status"}
    if not isinstance(bundle, dict) or not required <= set(bundle):
        raise EvidenceError("BUNDLE_SCHEMA", "required evidence bundle fields are missing")
    if bundle.get("schema") != "fdir/qualification-evidence-bundle" or bundle.get("version") != "1.0.0":
        raise EvidenceError("BUNDLE_SCHEMA", "unsupported evidence bundle version")
    source = bundle.get("source", {})
    if not isinstance(source, dict) or not isinstance(source.get("headSha"), str) or len(source["headSha"]) != 40 or not isinstance(source.get("treeSha"), str) or len(source["treeSha"]) != 40:
        raise EvidenceError("SOURCE_SHA_INVALID", "source head/tree SHA is not a full SHA-1")
    if source.get("binding") not in {"exact-commit", "dirty-working-tree"}:
        raise EvidenceError("SOURCE_BINDING_INVALID", "unknown source binding")
    for item in bundle.get("artifacts", []):
        if not isinstance(item, dict) or not _hash_ok(item.get("sha256")):
            raise EvidenceError("ARTIFACT_DIGEST_INVALID", "artifact digest is not SHA-256")
    for item in bundle.get("commands", []):
        if not isinstance(item, dict) or not _hash_ok(item.get("stdoutDigest")) or not _hash_ok(item.get("stderrDigest")):
            raise EvidenceError("COMMAND_DIGEST_INVALID", "command output digest is not SHA-256")
    issue_state = bundle.get("issueState")
    if issue_state is not None:
        if not isinstance(issue_state, dict) or issue_state.get("status") != "passed" or issue_state.get("source") != "github-issue-api" or set(issue_state.get("issueNumbers", [])) != set(LIVE_ISSUE_NUMBERS):
            raise EvidenceError("ISSUE_STATE_INVALID", "bundle issue state is not a complete live GitHub snapshot")
    if bundle.get("barrier", {}).get("releaseEligible"):
        if issue_state is None:
            raise EvidenceError("ISSUE_STATE_REQUIRED", "release-eligible evidence has no live issue state")
        clean_room = bundle.get("cleanRoom")
        if not isinstance(clean_room, dict) or clean_room.get("status") != "passed" or clean_room.get("runs", 0) < 2 or clean_room.get("diffCount") != 0:
            raise EvidenceError("CLEAN_ROOM_REQUIRED", "release-eligible evidence has no passed clean-room replay")


def verify_bundle(bundle_path: Path, *, index_path: Path | None = None, require_clean: bool = True, check_source_digest: bool = True) -> dict[str, Any]:
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("BUNDLE_UNREADABLE", str(exc)) from exc
    validate_shape(bundle)
    source = source_state()
    if bundle["source"]["headSha"] != source["headSha"]:
        raise EvidenceError("SOURCE_SHA_MISMATCH", "bundle was generated from a different commit")
    if check_source_digest and bundle["source"]["trackedDigest"] != source["trackedDigest"]:
        raise EvidenceError("SOURCE_DIGEST_MISMATCH", "workspace bytes differ from the bundled source digest")
    if require_clean and (not source["workingTreeClean"] or bundle["source"].get("binding") != "exact-commit"):
        raise EvidenceError("DIRTY_SOURCE", "exact release evidence requires a clean commit-bound workspace")
    if bundle.get("integrity", {}).get("bundleDigest") != bundle_digest(bundle):
        raise EvidenceError("BUNDLE_DIGEST_MISMATCH", "bundle bytes or integrity fields were changed")

    artifacts = {item["id"]: item for item in bundle.get("artifacts", [])}
    for item in artifacts.values():
        path = root_path(item["path"])
        sha, size = digest_file(path)
        if sha != item["sha256"] or size != item["bytes"]:
            raise EvidenceError("ARTIFACT_DIGEST_MISMATCH", f"artifact bytes changed: {item['path']}")
    issue_state = bundle.get("issueState")
    if isinstance(issue_state, dict):
        artifact_id = issue_state.get("artifactId")
        item = artifacts.get(artifact_id)
        if not isinstance(item, dict):
            raise EvidenceError("ISSUE_STATE_ARTIFACT_MISSING", "live issue state artifact is not indexed")
        snapshot_path = root_path(item["path"])
        _snapshot, snapshot_summary = load_issue_snapshot(snapshot_path)
        if snapshot_summary["sha256"] != item["sha256"] or snapshot_summary["bytes"] != item["bytes"]:
            raise EvidenceError("ISSUE_STATE_DIGEST_MISMATCH", "live issue snapshot digest does not match its artifact")
        if snapshot_summary["issueNumbers"] != issue_state.get("issueNumbers"):
            raise EvidenceError("ISSUE_STATE_SCOPE_MISMATCH", "live issue snapshot scope differs from bundle metadata")
    for command in bundle.get("commands", []):
        for ref in command.get("outputDigests", []):
            sha, size = digest_file(root_path(ref["path"]))
            if sha != ref["sha256"] or size != ref["bytes"]:
                raise EvidenceError("COMMAND_OUTPUT_DIGEST_MISMATCH", f"command output bytes changed: {ref['path']}")
        if command.get("status") == "passed" and command.get("exitCode") != 0:
            raise EvidenceError("COMMAND_STATUS_MISMATCH", command.get("id", "<unknown>"))

    index = index_path
    if index is None and isinstance(bundle.get("index"), dict) and bundle["index"].get("path"):
        index = root_path(bundle["index"]["path"])
    if index is not None and index.is_file():
        try:
            external_index = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EvidenceError("INDEX_UNREADABLE", str(exc)) from exc
        expected_index = index_for(bundle)
        if external_index != expected_index:
            raise EvidenceError("INDEX_CONTENT_MISMATCH", "external index differs from bundle index")
    expected_index = index_for(bundle)
    bundle_index_core = {key: bundle["index"].get(key) for key in ("schema", "version", "entries", "digest")}
    if bundle_index_core != expected_index:
        raise EvidenceError("INDEX_DIGEST_MISMATCH", "index entries do not match artifact and command bindings")
    if bundle.get("barrier", {}).get("releaseEligible") and (not source["workingTreeClean"] or bundle["source"].get("binding") != "exact-commit"):
        raise EvidenceError("BARRIER_FALSE_RELEASE", "dirty or non-commit evidence cannot claim release eligibility")
    return {"status": "passed", "sourceHeadSha": source["headSha"], "bundleDigest": bundle["integrity"]["bundleDigest"], "indexDigest": bundle["index"]["digest"], "artifactCount": len(artifacts)}


def _clean_room_projection(bundle: dict[str, Any]) -> dict[str, Any]:
    """Remove only declared volatile execution fields before replay comparison."""

    value = json.loads(json.dumps(bundle))
    value.pop("integrity", None)
    value.pop("environment", None)
    for command in value.get("commands", []):
        command.pop("durationMilliseconds", None)
        for collection in ("inputDigests", "outputDigests"):
            for reference in command.get(collection, []):
                reference.pop("path", None)
    for item in value.get("artifacts", []):
        item.pop("path", None)
    return value


def compare_clean_room(first_path: Path, second_path: Path) -> dict[str, Any]:
    try:
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = json.loads(second_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("CLEAN_ROOM_UNREADABLE", str(exc)) from exc
    validate_shape(first)
    validate_shape(second)
    if first["source"] != second["source"]:
        differences = ["source binding differs"]
    else:
        left = _clean_room_projection(first)
        right = _clean_room_projection(second)
        differences = [] if left == right else ["normalized evidence bundle differs"]
    diff_digest = digest_bytes(canonical(differences))
    return {"schema":"fdir/clean-room-replay-report","version":"1.0.0","status":"passed" if not differences else "failed","runs":2,"volatileFields":["integrity","environment","commands[*].durationMilliseconds","commands[*].*.path","artifacts[*].path"],"diffCount":len(differences),"diffDigest":diff_digest,"differences":differences}


COMMANDS = [
    ("design-validation", ["tools/validate_design.py"]),
    ("acceptance", ["tools/run_acceptance.py", "--all"]),
    ("e2e", ["tools/run_e2e.py", "--all", "--json"]),
    ("mutation-qualification", ["tools/mutation_qualification.py", "--json"]),
    ("query-qualification", ["tools/query_qualification.py"]),
    ("independent-corpus", ["tools/independent_corpus.py", "--json"]),
    ("format-qualification", ["tools/format_qualification.py"]),
    ("metamorphic-qualification", ["tools/metamorphic_qualification.py"]),
    ("strict-completion", ["tools/strict_completion_gate.py"]),
    ("evidence-self-test", ["tools/evidence_bundle.py", "self-test"]),
    ("release-contract-qualification", ["tools/release_contract_qualification.py"]),
    ("defect-injection", ["tools/run_defect_injection_campaign.py", "--json"]),
]
CONFIG_INPUTS = [
    SCHEMA_PATH,
    REQUIREMENTS_PATH,
    AUDIT_PATH,
    REGRESSIONS_PATH,
    ROOT / "machine" / "format-qualified-profiles.json",
    ROOT / "machine" / "defect-injection-contract.json",
    ROOT / "tools" / "independent_oracle.py",
    ROOT / "tools" / "run_defect_injection_campaign.py",
    ROOT / "tools" / "release_contract_qualification.py",
]


def run_command(command_id: str, argv: list[str], log_dir: Path, timeout: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.monotonic()
    out = log_dir / f"{command_id}.stdout.txt"
    err = log_dir / f"{command_id}.stderr.txt"
    try:
        completed = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        status = "passed" if completed.returncode == 0 else "failed"
        code = completed.returncode
        stdout, stderr = completed.stdout.replace("\r\n", "\n"), completed.stderr.replace("\r\n", "\n")
    except subprocess.TimeoutExpired as exc:
        status, code = "timeout", -1
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        status, code, stdout, stderr = "infrastructure-error", -1, "", str(exc)
    out.write_text(stdout, encoding="utf-8", newline="\n")
    err.write_text(stderr, encoding="utf-8", newline="\n")
    command = {
        "id": command_id, "argv": argv, "cwd": ".", "exitCode": code, "status": status,
        "stdoutDigest": digest_bytes(stdout.encode("utf-8")), "stderrDigest": digest_bytes(stderr.encode("utf-8")),
        "durationMilliseconds": round((time.monotonic() - started) * 1000, 3),
        "inputDigests": [digest_ref(path) for path in CONFIG_INPUTS],
        "outputDigests": [digest_ref(out), digest_ref(err)],
    }
    artifacts = [artifact(out, "log", f"{command_id}.stdout", command_id), artifact(err, "log", f"{command_id}.stderr", command_id)]
    return command, artifacts


def collect(
    output: Path,
    index_path: Path,
    timeout: int,
    allow_dirty: bool,
    issue_snapshot_path: Path | None = None,
    clean_room_report_path: Path | None = None,
) -> dict[str, Any]:
    source = source_state()
    log_dir = output.parent / (output.stem + "-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    commands, artifacts = [], []
    for command_id, argv in COMMANDS:
        command, produced = run_command(command_id, argv, log_dir, timeout)
        commands.append(command)
        artifacts.extend(produced)
    blockers = []
    if not source["workingTreeClean"] and not allow_dirty:
        blockers.append({"code": "DIRTY_SOURCE", "detail": "collect requires a clean exact candidate; use --allow-dirty only for a blocked diagnostic report"})
    if not source["workingTreeClean"]:
        blockers.append({"code": "DIRTY_SOURCE", "detail": "working tree is not an exact release candidate"})
    if any(item["status"] != "passed" for item in commands):
        blockers.append({"code": "COMMAND_FAILURE", "detail": "one or more recorded public commands did not pass"})
    issue_state = None
    if issue_snapshot_path is None:
        blockers.append({"code": "LIVE_ISSUE_STATE_REQUIRED", "detail": "final release evidence must bind live GitHub state for #87-#105", "issueNumbers": list(LIVE_ISSUE_NUMBERS)})
    else:
        try:
            _snapshot, issue_state = load_issue_snapshot(issue_snapshot_path)
        except EvidenceError as exc:
            blockers.append({"code": exc.code, "detail": exc.detail})
        else:
            artifacts.append(artifact(issue_snapshot_path, "report", "live-issue-state"))

    clean_room = None
    if clean_room_report_path is None:
        blockers.append({"code": "CLEAN_ROOM_REQUIRED", "detail": "final release evidence must bind a passed two-run clean-room replay"})
    else:
        try:
            clean_room = json.loads(clean_room_report_path.read_text(encoding="utf-8"))
            if not isinstance(clean_room, dict) or clean_room.get("status") != "passed" or clean_room.get("runs", 0) < 2 or clean_room.get("diffCount") != 0:
                raise EvidenceError("CLEAN_ROOM_REQUIRED", "clean-room replay is not passed with zero differences")
            if not _hash_ok(clean_room.get("diffDigest")):
                raise EvidenceError("CLEAN_ROOM_SCHEMA", "clean-room replay has no SHA-256 diff digest")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            blockers.append({"code": "CLEAN_ROOM_UNREADABLE", "detail": str(exc)})
        except EvidenceError as exc:
            blockers.append({"code": exc.code, "detail": exc.detail})
        else:
            artifacts.append(artifact(clean_room_report_path, "report", "clean-room-replay"))
    bundle: dict[str, Any] = {
        "schema": "fdir/qualification-evidence-bundle", "version": "1.0.0", "repository": "horiyamayoh/fdir",
        "source": source, "commands": commands, "artifacts": sorted(artifacts, key=lambda value: value["id"]),
        "index": {}, "barrier": {"releaseEligible": not blockers, "claimMode": "release-candidate" if not blockers else "experimental-bounded-subset", "blockers": blockers},
        "status": "passed" if not blockers else "blocked", "integrity": {},
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
    }
    if issue_state is not None:
        issue_state["artifactId"] = "live-issue-state"
        bundle["issueState"] = issue_state
    if clean_room is not None:
        bundle["cleanRoom"] = clean_room
    write_bundle(bundle, output, index_path)
    return {"status": bundle["status"], "bundle": relative(output), "index": relative(index_path), "sourceHeadSha": source["headSha"], "sourceBinding": source["binding"], "blockers": blockers, "commandCount": len(commands), "artifactCount": len(artifacts), "bundleDigest": bundle["integrity"]["bundleDigest"], "indexDigest": bundle["index"]["digest"]}


def self_test() -> dict[str, Any]:
    root = ROOT / "reports"
    root.mkdir(parents=True, exist_ok=True)
    with PersistentDirectory(root, "evidence-self-test-") as temp:
        folder = Path(temp)
        input_path = folder / "input.txt"
        output_path = folder / "output.json"
        stdout_path = folder / "stdout.txt"
        stderr_path = folder / "stderr.txt"
        input_path.write_text("fixture-input\n", encoding="utf-8")
        output_path.write_text("{\"observed\":true}\n", encoding="utf-8")
        stdout_path.write_text("{\"status\":\"passed\"}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        issue_path = folder / "issues.json"
        issue_path.write_text(json.dumps({
            "schema": "fdir/github-issue-state-snapshot",
            "version": "1.0.0",
            "repository": "horiyamayoh/fdir",
            "source": "github-issue-api",
            "issues": [{"number": number, "state": "closed", "stateReason": "completed"} for number in LIVE_ISSUE_NUMBERS],
        }, sort_keys=True) + "\n", encoding="utf-8")
        _issue_snapshot, issue_state = load_issue_snapshot(issue_path)
        issue_state["artifactId"] = "live-issue-state"
        clean_room = {"schema": "fdir/clean-room-replay-report", "version": "1.0.0", "status": "passed", "runs": 2, "volatileFields": [], "diffCount": 0, "diffDigest": "0" * 64}
        clean_path = folder / "clean-room.json"
        clean_path.write_text(json.dumps(clean_room, sort_keys=True) + "\n", encoding="utf-8")
        source = source_state()
        bundle: dict[str, Any] = {
            "schema": "fdir/qualification-evidence-bundle", "version": "1.0.0", "repository": "horiyamayoh/fdir", "source": source,
            "commands": [{"id":"self-test", "argv":["tools/evidence_bundle.py","self-test"], "cwd":".", "exitCode":0, "status":"passed", "stdoutDigest":digest_file(stdout_path)[0], "stderrDigest":digest_file(stderr_path)[0], "durationMilliseconds":1.0, "inputDigests":[digest_ref(input_path)], "outputDigests":[digest_ref(stdout_path),digest_ref(stderr_path)]}],
            "artifacts":[artifact(input_path,"input","self.input"),artifact(output_path,"output","self.output"),artifact(stdout_path,"report","self.stdout"),artifact(stderr_path,"log","self.stderr"),artifact(issue_path,"report","live-issue-state"),artifact(clean_path,"report","clean-room-replay")],
            "index": {}, "barrier": {"releaseEligible":False,"claimMode":"experimental-bounded-subset","blockers":[{"code":"SELF_TEST","detail":"synthetic negative test"}]}, "status":"blocked", "integrity":{},
            "issueState": issue_state, "cleanRoom": clean_room,
        }
        bundle_path = folder / "bundle.json"
        index_path = folder / "index.json"
        write_bundle(bundle, bundle_path, index_path)
        checks = []
        verify_bundle(bundle_path, index_path=index_path, require_clean=False, check_source_digest=False)
        checks.append({"id":"base-bundle","status":"passed"})
        mutations = [
            ("bundle-tamper-artifact", output_path, lambda: output_path.write_text("tampered\n", encoding="utf-8"), "ARTIFACT_DIGEST_MISMATCH"),
            ("bundle-tamper-source", None, lambda: bundle_path.write_text(bundle_path.read_text(encoding="utf-8").replace(source["headSha"], "0" * 40), encoding="utf-8"), "SOURCE_SHA_MISMATCH"),
            ("bundle-tamper-command", None, lambda: bundle_path.write_text(bundle_path.read_text(encoding="utf-8").replace(bundle["commands"][0]["stdoutDigest"], "f" * 64), encoding="utf-8"), "BUNDLE_DIGEST_MISMATCH"),
        ]
        for name, target, mutate, expected in mutations:
            # Recreate a clean fixture for each negative assertion.
            input_path.write_text("fixture-input\n", encoding="utf-8")
            output_path.write_text("{\"observed\":true}\n", encoding="utf-8")
            stdout_path.write_text("{\"status\":\"passed\"}\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            write_bundle(bundle, bundle_path, index_path)
            if target is not None:
                target.write_text("tampered\n", encoding="utf-8")
            else:
                mutate()
            if target is not None:
                pass
            else:
                pass
            try:
                verify_bundle(bundle_path, index_path=index_path, require_clean=False, check_source_digest=False)
            except EvidenceError as exc:
                checks.append({"id": name, "status": "passed", "observed": exc.code, "expected": expected})
                if exc.code != expected:
                    raise EvidenceError("SELF_TEST_ORACLE", f"{name} expected {expected} got {exc.code}")
            else:
                raise EvidenceError("SELF_TEST_SURVIVED", name)
        # The external index is a separate artifact.  A verifier that only
        # checks the embedded copy would incorrectly accept this mutation.
        write_bundle(bundle, bundle_path, index_path)
        external = json.loads(index_path.read_text(encoding="utf-8"))
        external["entries"][0]["sha256"] = "f" * 64
        index_path.write_bytes(canonical(external))
        try:
            verify_bundle(bundle_path, index_path=index_path, require_clean=False, check_source_digest=False)
        except EvidenceError as exc:
            if exc.code != "INDEX_CONTENT_MISMATCH":
                raise EvidenceError("SELF_TEST_ORACLE", f"bundle-tamper-index expected INDEX_CONTENT_MISMATCH got {exc.code}")
            checks.append({"id":"bundle-tamper-index","status":"passed","observed":exc.code,"expected":"INDEX_CONTENT_MISMATCH"})
        else:
            raise EvidenceError("SELF_TEST_SURVIVED", "bundle-tamper-index")
        # Change the embedded index and re-sign only the outer bundle.  The
        # embedded index digest must still reject it even when no external
        # index file is supplied.
        write_bundle(bundle, bundle_path, index_path)
        embedded = json.loads(bundle_path.read_text(encoding="utf-8"))
        embedded["index"]["entries"][0]["sha256"] = "f" * 64
        embedded["integrity"]["bundleDigest"] = bundle_digest(embedded)
        bundle_path.write_bytes(canonical(embedded))
        try:
            verify_bundle(bundle_path, require_clean=False, check_source_digest=False)
        except EvidenceError as exc:
            if exc.code != "INDEX_DIGEST_MISMATCH":
                raise EvidenceError("SELF_TEST_ORACLE", f"bundle-tamper-embedded-index expected INDEX_DIGEST_MISMATCH got {exc.code}")
            checks.append({"id":"bundle-tamper-embedded-index","status":"passed","observed":exc.code,"expected":"INDEX_DIGEST_MISMATCH"})
        else:
            raise EvidenceError("SELF_TEST_SURVIVED", "bundle-tamper-embedded-index")
        # Re-signing a tampered barrier must still be rejected when it makes a
        # dirty/non-commit source claim release eligibility.
        write_bundle(bundle, bundle_path, index_path)
        bundle["source"]["workingTreeClean"] = False
        bundle["source"]["binding"] = "dirty-working-tree"
        bundle["barrier"]["releaseEligible"] = True
        write_bundle(bundle, bundle_path, index_path)
        try:
            verify_bundle(bundle_path, index_path=index_path, require_clean=False, check_source_digest=False)
        except EvidenceError as exc:
            if exc.code != "BARRIER_FALSE_RELEASE":
                raise EvidenceError("SELF_TEST_ORACLE", f"barrier-dirty-source expected BARRIER_FALSE_RELEASE got {exc.code}")
            checks.append({"id":"barrier-dirty-source","status":"passed","observed":exc.code,"expected":"BARRIER_FALSE_RELEASE"})
        else:
            raise EvidenceError("SELF_TEST_SURVIVED", "barrier-dirty-source")
        return {"schema":"fdir/evidence-bundle-self-test-report","version":"1.0.0","status":"passed","checks":checks,"negativeCount":len(checks)-1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--index", type=Path, required=True)
    collect_parser.add_argument("--timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_SECONDS)
    collect_parser.add_argument("--allow-dirty", action="store_true")
    collect_parser.add_argument("--issue-snapshot", type=Path, help="validated live GitHub issue-state snapshot for #87-#105")
    collect_parser.add_argument("--clean-room-report", type=Path, help="passed two-run clean-room comparison report")
    collect_parser.add_argument("--allow-blocked-report", action="store_true", help="write a blocked diagnostic report and return zero; never changes its blocked status")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("bundle", type=Path)
    verify_parser.add_argument("--index", type=Path)
    sub.add_parser("self-test")
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "collect":
            result = collect(
                root_path(args.output),
                root_path(args.index),
                args.timeout,
                args.allow_dirty,
                root_path(args.issue_snapshot) if args.issue_snapshot else None,
                root_path(args.clean_room_report) if args.clean_room_report else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["status"] == "passed" or (args.allow_blocked_report and result["status"] == "blocked") else 1
        if args.operation == "verify":
            result = verify_bundle(root_path(args.bundle), index_path=root_path(args.index) if args.index else None, require_clean=True)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.operation == "compare":
            result = compare_clean_room(root_path(args.first), root_path(args.second))
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["status"] == "passed" else 1
        result = self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except EvidenceError as exc:
        print(json.dumps({"schema":"fdir/evidence-bundle-error","status":"failed","code":exc.code,"detail":exc.detail}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
