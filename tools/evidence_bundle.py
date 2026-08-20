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
import re
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
COMMAND_IDS = (
    "design-validation",
    "acceptance",
    "e2e",
    "mutation-qualification",
    "query-qualification",
    "independent-corpus",
    "format-qualification",
    "metamorphic-qualification",
    "strict-completion",
    "evidence-self-test",
    "release-contract-qualification",
    "defect-injection",
)

# Each audit requirement resolves to a real command result and the source
# files that define the qualified boundary.  These are bindings, not claims:
# the command status and every digest are rechecked before a bundle is valid.
ISSUE_EVIDENCE_SPECS: dict[str, dict[str, Any]] = {
    "bundle.source": {"commands": ["design-validation"], "paths": ["machine/audit-recovery-plan.json"]},
    "bundle.command-digests": {"commands": list(COMMAND_IDS), "paths": []},
    "bundle.artifact-digests": {"commands": list(COMMAND_IDS), "paths": []},
    "bundle.index-integrity": {"commands": ["evidence-self-test"], "paths": ["schemas/qualification-evidence.schema.json"]},
    "defect.campaign": {"commands": ["defect-injection"], "paths": ["tools/run_defect_injection_campaign.py", "machine/defect-injection-contract.json"]},
    "defect.negative-self-test": {"commands": ["evidence-self-test", "release-contract-qualification"], "paths": ["tools/evidence_bundle.py"]},
    "defect.no-undetected-must": {"commands": ["defect-injection", "strict-completion"], "paths": ["machine/strict-completion-contract.json"]},
    "normative-model": {"commands": ["design-validation", "acceptance"], "paths": ["machine/model-contract.json", "schemas/document-form-ir.schema.json", "tools/ir_validation.py", "machine/reference-registry.json"]},
    "source-accounting": {"commands": ["e2e", "independent-corpus"], "paths": ["tools/qualification_evidence.py", "tools/adapter_common.py", "tools/run_e2e.py", "tools/independent_corpus.py"]},
    "exact-value-provenance": {"commands": ["e2e", "independent-corpus", "mutation-qualification"], "paths": ["tools/qualification_evidence.py", "tools/adapter_xlsx.py", "tools/adapter_pdf.py"]},
    "style-provenance": {"commands": ["e2e", "format-qualification"], "paths": ["tools/adapter_docx.py", "tools/adapter_common.py", "examples/style-resolution.json"]},
    "geometry-provenance": {"commands": ["e2e", "format-qualification"], "paths": ["tools/adapter_docx.py", "tools/adapter_pdf.py", "tools/qualification_evidence.py"]},
    "structure-topology": {"commands": ["e2e", "independent-corpus"], "paths": ["tools/adapter_docx.py", "tools/adapter_xlsx.py", "tools/adapter_markdown.py", "machine/model-contract.json"]},
    "relationship-resource-closure": {"commands": ["e2e", "independent-corpus", "mutation-qualification"], "paths": ["machine/reference-registry.json", "tools/adapter_docx.py", "tools/adapter_xlsx.py", "tools/adapter_pdf.py", "schemas/extensions/format-extensions.schema.json"]},
    "extension-registry-closure": {"commands": ["design-validation", "mutation-qualification"], "paths": ["machine/extension-registry.json", "schemas/extensions/format-extensions.schema.json", "tools/extension_registry.py"]},
    "canonical-migration": {"commands": ["e2e", "query-qualification", "metamorphic-qualification"], "paths": ["machine/canonicalization.json", "tools/canonicalize_ir.py", "tools/adapter_common.py"]},
    "docx-qualified-profile": {"commands": ["e2e", "format-qualification", "independent-corpus"], "paths": ["machine/format-qualified-profiles.json", "tools/adapter_docx.py", "tools/format_qualification.py", "e2e/corpus/manifest.json"]},
    "xlsx-qualified-profile": {"commands": ["e2e", "format-qualification", "independent-corpus"], "paths": ["machine/format-qualified-profiles.json", "tools/adapter_xlsx.py", "tools/format_qualification.py", "e2e/corpus/manifest.json"]},
    "pdf-qualified-profile": {"commands": ["e2e", "format-qualification", "independent-corpus"], "paths": ["machine/format-qualified-profiles.json", "tools/adapter_pdf.py", "tools/format_qualification.py", "e2e/corpus/manifest.json"]},
    "markdown-qualified-profile": {"commands": ["e2e", "format-qualification", "independent-corpus"], "paths": ["machine/format-qualified-profiles.json", "tools/adapter_markdown.py", "tools/format_qualification.py", "e2e/corpus/manifest.json"]},
    "query-completeness": {"commands": ["query-qualification", "e2e", "independent-corpus"], "paths": ["machine/query-contract.json", "tools/query_ir.py", "tools/query_qualification.py"]},
    "independent-corpus": {"commands": ["independent-corpus"], "paths": ["e2e/corpus/manifest.json", "tools/independent_corpus.py", "tools/independent_oracle.py"]},
    "differential": {"commands": ["metamorphic-qualification", "independent-corpus"], "paths": ["tools/metamorphic_qualification.py", "tools/independent_oracle.py", "machine/independent-corpus-contract.json"]},
    "metamorphic": {"commands": ["metamorphic-qualification"], "paths": ["tools/metamorphic_qualification.py"]},
    "hostile": {"commands": ["metamorphic-qualification", "defect-injection"], "paths": ["tools/run_defect_injection_campaign.py", "machine/defect-injection-contract.json"]},
    "release-barrier": {"commands": ["release-contract-qualification", "strict-completion"], "paths": ["tools/release_gate.py", "machine/release-requirements.json", "machine/release-claim-manifest.json"]},
    "clean-room": {"commands": ["evidence-self-test"], "paths": ["tools/evidence_bundle.py"]},
    "claim-conformance": {"commands": ["release-contract-qualification", "format-qualification"], "paths": ["machine/release-claim-manifest.json", "machine/capability-profile.json", "tools/release_gate.py"]},
    "all-child-evidence": {"commands": list(COMMAND_IDS), "paths": ["machine/audit-recovery-plan.json"]},
}


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


def source_artifact_id(path: str) -> str:
    return f"source-{digest_bytes(path.encode('utf-8'))[:20]}"


def issue_source_paths() -> list[str]:
    paths = {path for spec in ISSUE_EVIDENCE_SPECS.values() for path in spec.get("paths", [])}
    return sorted(paths)


def source_artifacts() -> list[dict[str, Any]]:
    result = []
    for path in issue_source_paths():
        resolved = root_path(path)
        if not resolved.is_file():
            raise EvidenceError("ISSUE_EVIDENCE_SOURCE_MISSING", f"required issue evidence source is missing: {path}")
        result.append(artifact(resolved, "source", source_artifact_id(path)))
    return result


def _command_artifact_ids(command_id: str, artifacts: list[dict[str, Any]]) -> list[str]:
    return sorted(item["id"] for item in artifacts if item.get("commandId") == command_id)


def build_issue_evidence(source: dict[str, Any], commands: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve every audit-plan evidence ID to commands and digested sources."""

    try:
        plan = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("ISSUE_EVIDENCE_PLAN_UNREADABLE", str(exc)) from exc
    command_ids = {item.get("id") for item in commands}
    artifact_ids = {item.get("id") for item in artifacts}
    entries = [{"issueNumber": 87, "requiredEvidenceIds": ["all-child-evidence"], "bindings": []}]
    for plan_item in plan.get("children", []):
        issue_number = plan_item.get("issueNumber")
        bindings = []
        for evidence_id in plan_item.get("requiredEvidenceIds", []):
            spec = ISSUE_EVIDENCE_SPECS.get(evidence_id)
            if spec is None:
                raise EvidenceError("ISSUE_EVIDENCE_SPEC_MISSING", f"no binding specification for {evidence_id}")
            commands_for_binding = list(spec.get("commands", []))
            paths_for_binding = list(spec.get("paths", []))
            missing_commands = sorted(set(commands_for_binding) - command_ids)
            missing_paths = sorted(set(paths_for_binding) - {item.get("path") for item in artifacts})
            if missing_commands or missing_paths:
                raise EvidenceError("ISSUE_EVIDENCE_UNRESOLVED", f"{evidence_id}: commands={missing_commands} paths={missing_paths}")
            resolved_artifacts = set()
            for command_id in commands_for_binding:
                resolved_artifacts.update(_command_artifact_ids(command_id, artifacts))
            resolved_artifacts.update(source_artifact_id(path) for path in paths_for_binding)
            if not resolved_artifacts.issubset(artifact_ids):
                raise EvidenceError("ISSUE_EVIDENCE_ARTIFACT_UNRESOLVED", evidence_id)
            bindings.append({
                "evidenceId": evidence_id,
                "commandIds": commands_for_binding,
                "sourcePaths": paths_for_binding,
                "artifactIds": sorted(resolved_artifacts),
            })
        entries.append({"issueNumber": issue_number, "requiredEvidenceIds": list(plan_item.get("requiredEvidenceIds", [])), "bindings": bindings})
    all_artifacts = sorted(artifact_ids)
    entries[0]["bindings"] = [{
        "evidenceId": "all-child-evidence",
        "commandIds": list(COMMAND_IDS),
        "sourcePaths": ["machine/audit-recovery-plan.json"],
        "artifactIds": all_artifacts,
    }]
    return {
        "schema": "fdir/issue-evidence-bindings",
        "version": "1.0.0",
        "sourceHeadSha": source["headSha"],
        "entries": entries,
    }


def validate_issue_evidence(bundle: dict[str, Any]) -> None:
    value = bundle.get("issueEvidence")
    if not isinstance(value, dict) or value.get("schema") != "fdir/issue-evidence-bindings" or value.get("version") != "1.0.0":
        raise EvidenceError("ISSUE_EVIDENCE_SCHEMA", "issue-specific evidence bindings are missing")
    if value.get("sourceHeadSha") != bundle.get("source", {}).get("headSha"):
        raise EvidenceError("ISSUE_EVIDENCE_SOURCE_MISMATCH", "issue evidence is bound to a different source commit")
    by_issue = {item.get("issueNumber"): item for item in value.get("entries", []) if isinstance(item, dict)}
    if set(by_issue) != set(LIVE_ISSUE_NUMBERS):
        raise EvidenceError("ISSUE_EVIDENCE_SCOPE", "issue evidence must cover exactly #87-#105")
    commands = {item.get("id"): item for item in bundle.get("commands", []) if isinstance(item, dict)}
    artifacts = {item.get("id"): item for item in bundle.get("artifacts", []) if isinstance(item, dict)}
    try:
        plan = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("ISSUE_EVIDENCE_PLAN_UNREADABLE", str(exc)) from exc
    expected = {87: ["all-child-evidence"]}
    expected.update({item["issueNumber"]: item.get("requiredEvidenceIds", []) for item in plan.get("children", [])})
    for issue_number, required_ids in expected.items():
        entry = by_issue[issue_number]
        if entry.get("requiredEvidenceIds") != required_ids:
            raise EvidenceError("ISSUE_EVIDENCE_REQUIREMENTS", f"issue #{issue_number} evidence IDs do not match the audit plan")
        bindings = {item.get("evidenceId"): item for item in entry.get("bindings", []) if isinstance(item, dict)}
        if set(bindings) != set(required_ids):
            raise EvidenceError("ISSUE_EVIDENCE_BINDINGS", f"issue #{issue_number} has unresolved required evidence")
        for evidence_id in required_ids:
            binding = bindings[evidence_id]
            for command_id in binding.get("commandIds", []):
                command = commands.get(command_id)
                if not isinstance(command, dict):
                    raise EvidenceError("ISSUE_EVIDENCE_COMMAND_MISSING", f"{evidence_id}: {command_id}")
                if bundle.get("barrier", {}).get("releaseEligible") and (command.get("status") != "passed" or command.get("exitCode") != 0):
                    raise EvidenceError("ISSUE_EVIDENCE_COMMAND_FAILED", f"{evidence_id}: {command_id}")
            for path in binding.get("sourcePaths", []):
                source_id = source_artifact_id(path)
                item = artifacts.get(source_id)
                if not isinstance(item, dict) or item.get("path") != path:
                    raise EvidenceError("ISSUE_EVIDENCE_SOURCE_UNRESOLVED", f"{evidence_id}: {path}")
            for artifact_id in binding.get("artifactIds", []):
                if artifact_id not in artifacts:
                    raise EvidenceError("ISSUE_EVIDENCE_ARTIFACT_UNRESOLVED", f"{evidence_id}: {artifact_id}")


def validate_clean_room_bindings(bundle: dict[str, Any]) -> None:
    clean_room = bundle.get("cleanRoom")
    if not isinstance(clean_room, dict):
        raise EvidenceError("CLEAN_ROOM_REQUIRED", "release-eligible evidence has no clean-room report")
    if clean_room.get("sourceHeadSha") != bundle.get("source", {}).get("headSha") or clean_room.get("sourceTrackedDigest") != bundle.get("source", {}).get("trackedDigest"):
        raise EvidenceError("CLEAN_ROOM_SOURCE_MISMATCH", "clean-room report is bound to a different source")
    inputs = clean_room.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2 or len({item.get("path") for item in inputs if isinstance(item, dict)}) != 2:
        raise EvidenceError("CLEAN_ROOM_INPUTS_REQUIRED", "clean-room report must bind two distinct raw replay bundles")
    artifacts = {item.get("path"): item for item in bundle.get("artifacts", []) if isinstance(item, dict)}
    for entry in inputs:
        if not isinstance(entry, dict):
            raise EvidenceError("CLEAN_ROOM_INPUTS_REQUIRED", "clean-room input is malformed")
        raw = artifacts.get(entry.get("path"))
        index = artifacts.get(entry.get("indexPath"))
        if not isinstance(raw, dict) or raw.get("sha256") != entry.get("sha256") or raw.get("bytes") != entry.get("bytes"):
            raise EvidenceError("CLEAN_ROOM_INPUT_DIGEST_MISMATCH", str(entry.get("path")))
        if not isinstance(index, dict) or index.get("sha256") != entry.get("indexSha256") or index.get("bytes") != entry.get("indexBytes"):
            raise EvidenceError("CLEAN_ROOM_INDEX_DIGEST_MISMATCH", str(entry.get("indexPath")))


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
    if bundle.get("issueEvidence") is not None:
        validate_issue_evidence(bundle)
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
    if bundle.get("barrier", {}).get("releaseEligible") and (not source["workingTreeClean"] or bundle["source"].get("binding") != "exact-commit"):
        raise EvidenceError("BARRIER_FALSE_RELEASE", "dirty or non-commit evidence cannot claim release eligibility")
    if bundle.get("integrity", {}).get("bundleDigest") != bundle_digest(bundle):
        raise EvidenceError("BUNDLE_DIGEST_MISMATCH", "bundle bytes or integrity fields were changed")

    artifacts = {item["id"]: item for item in bundle.get("artifacts", [])}
    for item in artifacts.values():
        path = root_path(item["path"])
        sha, size = digest_file(path)
        if sha != item["sha256"] or size != item["bytes"]:
            raise EvidenceError("ARTIFACT_DIGEST_MISMATCH", f"artifact bytes changed: {item['path']}")
    if bundle.get("barrier", {}).get("releaseEligible"):
        if bundle.get("issueEvidence") is None:
            raise EvidenceError("ISSUE_EVIDENCE_REQUIRED", "release-eligible evidence has no issue-specific bindings")
        validate_clean_room_bindings(bundle)
    if bundle.get("issueEvidence") is not None:
        validate_issue_evidence(bundle)
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
    return {"status": "passed", "sourceHeadSha": source["headSha"], "bundleDigest": bundle["integrity"]["bundleDigest"], "indexDigest": bundle["index"]["digest"], "artifactCount": len(artifacts)}


def _normalize_runtime_string(value: str) -> str:
    root_text = str(ROOT.resolve())
    normalized = value.replace(root_text, "<repo>").replace(root_text.replace("\\", "/"), "<repo>")
    normalized = re.sub(r"e2e[\\/]\.run[\\/][^\"'\\s]+", "e2e/.run/<run>", normalized)
    return normalized


def _normalize_runtime_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if str(key).casefold().endswith("durationmilliseconds") or str(key).casefold() in {"duration", "elapsedmilliseconds"}:
                continue
            result[key] = _normalize_runtime_value(child)
        return result
    if isinstance(value, list):
        return [_normalize_runtime_value(child) for child in value]
    if isinstance(value, str):
        return _normalize_runtime_string(value)
    return value


def _normalize_campaign_report(value: dict[str, Any]) -> dict[str, Any]:
    """Keep campaign outcomes while excluding nested subprocess log digests.

    The defect campaign deliberately creates disposable mutation workspaces.
    Their subprocess logs contain run-specific paths, so the campaign's own
    stdout/stderr digests are execution evidence rather than a stable result.
    Mutation identity, classification, detection, and all gate counts remain
    compared below.
    """

    normalized = _normalize_runtime_value(value)
    for suite in normalized.get("baseSuite", []):
        if isinstance(suite, dict):
            suite.pop("stdoutDigest", None)
            suite.pop("stderrDigest", None)
    for case in normalized.get("cases", []):
        if isinstance(case, dict) and isinstance(case.get("execution"), dict):
            case["execution"].pop("stdoutDigest", None)
            case["execution"].pop("stderrDigest", None)
    return normalized


def _normalized_log_digest(path: Path) -> tuple[str, int]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("CLEAN_ROOM_OUTPUT_UNREADABLE", str(exc)) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        normalized = _normalize_runtime_string(raw).replace("\r\n", "\n")
        data = normalized.encode("utf-8")
    else:
        if isinstance(parsed, dict) and parsed.get("schema") == "fdir/defect-injection-campaign-report":
            parsed = _normalize_campaign_report(parsed)
        else:
            parsed = _normalize_runtime_value(parsed)
        data = canonical(parsed)
    return digest_bytes(data), len(data)


def _clean_room_projection(bundle: dict[str, Any]) -> dict[str, Any]:
    """Remove only declared volatile execution fields before replay comparison."""

    value = json.loads(json.dumps(bundle))
    value.pop("integrity", None)
    value.pop("environment", None)
    value.get("index", {}).pop("path", None)
    for command in value.get("commands", []):
        command.pop("durationMilliseconds", None)
        normalized_outputs = []
        for reference in command.get("outputDigests", []):
            normalized_sha, normalized_size = _normalized_log_digest(root_path(reference["path"]))
            reference["sha256"] = normalized_sha
            reference["bytes"] = normalized_size
            normalized_outputs.append((normalized_sha, normalized_size))
        if normalized_outputs:
            command["stdoutDigest"] = normalized_outputs[0][0]
            if len(normalized_outputs) > 1:
                command["stderrDigest"] = normalized_outputs[1][0]
        for collection in ("inputDigests", "outputDigests"):
            for reference in command.get(collection, []):
                reference.pop("path", None)
    normalized_artifacts = {}
    for item in value.get("artifacts", []):
        if item.get("kind") == "log":
            normalized_sha, normalized_size = _normalized_log_digest(root_path(item["path"]))
            item["sha256"] = normalized_sha
            item["bytes"] = normalized_size
        normalized_artifacts[item.get("id")] = item.get("sha256")
        item.pop("path", None)
    index = value.get("index", {})
    for entry in index.get("entries", []):
        if entry.get("artifactId") in normalized_artifacts:
            entry["sha256"] = normalized_artifacts[entry["artifactId"]]
    index_core = {key: index.get(key) for key in ("schema", "version", "entries")}
    index["digest"] = digest_bytes(canonical(index_core))
    return value


def compare_clean_room(first_path: Path, second_path: Path) -> dict[str, Any]:
    try:
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = json.loads(second_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("CLEAN_ROOM_UNREADABLE", str(exc)) from exc
    validate_shape(first)
    validate_shape(second)
    first_index_path = root_path(first["index"]["path"])
    second_index_path = root_path(second["index"]["path"])
    first_sha, first_size = digest_file(first_path)
    second_sha, second_size = digest_file(second_path)
    first_index_sha, first_index_size = digest_file(first_index_path)
    second_index_sha, second_index_size = digest_file(second_index_path)
    if first["source"] != second["source"]:
        differences = ["source binding differs"]
    else:
        left = _clean_room_projection(first)
        right = _clean_room_projection(second)
        differences = [] if left == right else ["normalized evidence bundle differs"]
    diff_digest = digest_bytes(canonical(differences))
    return {
        "schema": "fdir/clean-room-replay-report",
        "version": "1.0.0",
        "status": "passed" if not differences else "failed",
        "runs": 2,
        "sourceHeadSha": first.get("source", {}).get("headSha"),
        "sourceTrackedDigest": first.get("source", {}).get("trackedDigest"),
        "inputs": [
            {"path": relative(first_path), "sha256": first_sha, "bytes": first_size, "indexPath": relative(first_index_path), "indexSha256": first_index_sha, "indexBytes": first_index_size, "bundleDigest": first.get("integrity", {}).get("bundleDigest"), "indexDigest": first.get("index", {}).get("digest")},
            {"path": relative(second_path), "sha256": second_sha, "bytes": second_size, "indexPath": relative(second_index_path), "indexSha256": second_index_sha, "indexBytes": second_index_size, "bundleDigest": second.get("integrity", {}).get("bundleDigest"), "indexDigest": second.get("index", {}).get("digest")},
        ],
        "volatileFields": ["integrity", "environment", "commands[*].durationMilliseconds", "commands[*].*.path", "artifacts[*].path", "defect-injection-report.baseSuite[*].*Digest", "defect-injection-report.cases[*].execution.*Digest"],
        "diffCount": len(differences),
        "diffDigest": diff_digest,
        "differences": differences,
    }


def load_clean_room_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("CLEAN_ROOM_UNREADABLE", str(exc)) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "fdir/clean-room-replay-report"
        or value.get("status") != "passed"
        or value.get("runs", 0) < 2
        or not _hash_ok(value.get("diffDigest"))
        or not isinstance(value.get("sourceHeadSha"), str)
        or len(value.get("sourceHeadSha", "")) != 40
        or not _hash_ok(value.get("sourceTrackedDigest"))
        or not isinstance(value.get("inputs"), list)
        or len(value.get("inputs", [])) != 2
        or any(not isinstance(item, dict) or not _hash_ok(item.get("sha256")) or not _hash_ok(item.get("indexSha256")) or not _hash_ok(item.get("bundleDigest")) or not _hash_ok(item.get("indexDigest")) for item in value.get("inputs", []))
        or value.get("diffCount") != 0
    ):
        raise EvidenceError("CLEAN_ROOM_REQUIRED", "clean-room replay is not passed with zero differences")
    return value


def finalize_bundle(bundle_path: Path, index_path: Path, clean_room_report_path: Path, output_path: Path, output_index_path: Path) -> dict[str, Any]:
    """Create a final bundle without overwriting either raw replay input."""

    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceError("BUNDLE_UNREADABLE", str(exc)) from exc
    validate_shape(bundle)
    clean_room = load_clean_room_report(clean_room_report_path)
    source = source_state()
    if not source["workingTreeClean"] or source["binding"] != "exact-commit":
        raise EvidenceError("DIRTY_SOURCE", "finalize requires an exact clean commit")
    if bundle.get("source") != source:
        raise EvidenceError("SOURCE_STATE_MISMATCH", "collected bundle is not from the current exact commit")
    if clean_room.get("sourceHeadSha") != source["headSha"] or clean_room.get("sourceTrackedDigest") != source["trackedDigest"]:
        raise EvidenceError("CLEAN_ROOM_SOURCE_MISMATCH", "clean-room replay is not bound to the current source")
    if bundle.get("issueState", {}).get("status") != "passed":
        raise EvidenceError("ISSUE_STATE_REQUIRED", "finalize requires a complete live GitHub issue snapshot")
    inputs = clean_room.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2:
        raise EvidenceError("CLEAN_ROOM_INPUTS_REQUIRED", "clean-room report must bind both raw replay bundles")
    input_by_path = {item.get("path"): item for item in inputs if isinstance(item, dict)}
    raw_paths = [bundle_path, root_path(inputs[1].get("path")) if inputs[0].get("path") == relative(bundle_path) else root_path(inputs[0].get("path"))]
    if len(set(raw_paths)) != 2:
        raise EvidenceError("CLEAN_ROOM_INPUTS_REQUIRED", "clean-room replay inputs must be two distinct bundles")
    for raw_path in raw_paths:
        entry = input_by_path.get(relative(raw_path))
        if not isinstance(entry, dict):
            raise EvidenceError("CLEAN_ROOM_INPUT_MISSING", f"clean-room report does not bind {relative(raw_path)}")
        sha, size = digest_file(raw_path)
        if sha != entry.get("sha256") or size != entry.get("bytes"):
            raise EvidenceError("CLEAN_ROOM_INPUT_DIGEST_MISMATCH", relative(raw_path))
        raw_index = root_path(entry.get("indexPath", ""))
        index_sha, index_size = digest_file(raw_index)
        if index_sha != entry.get("indexSha256") or index_size != entry.get("indexBytes"):
            raise EvidenceError("CLEAN_ROOM_INDEX_DIGEST_MISMATCH", relative(raw_index))
    blockers = [item for item in bundle.get("barrier", {}).get("blockers", []) if item.get("code") != "CLEAN_ROOM_REQUIRED"]
    if blockers or any(item.get("status") != "passed" for item in bundle.get("commands", [])):
        raise EvidenceError("BUNDLE_BLOCKED", "command or live-state blockers remain before clean-room finalization")
    bundle["cleanRoom"] = clean_room
    bundle["artifacts"].append(artifact(clean_room_report_path, "report", "clean-room-replay"))
    for label, raw_path in zip(("a", "b"), raw_paths):
        entry = input_by_path[relative(raw_path)]
        raw_index = root_path(entry["indexPath"])
        bundle["artifacts"].append(artifact(raw_path, "report", f"clean-room-run-{label}"))
        bundle["artifacts"].append(artifact(raw_index, "index", f"clean-room-run-{label}-index"))
    bundle["barrier"] = {"releaseEligible": True, "claimMode": "release-candidate", "productReleaseEligible": False, "productClaimMode": "experimental-bounded-subset", "blockers": []}
    bundle["status"] = "passed"
    write_bundle(bundle, output_path, output_index_path)
    verified = verify_bundle(output_path, index_path=output_index_path, require_clean=True)
    return {"status": "passed", "bundle": relative(output_path), "index": relative(output_index_path), "cleanRoom": clean_room, "bundleDigest": verified["bundleDigest"], "indexDigest": verified["indexDigest"]}


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
    artifacts.extend(source_artifacts())
    issue_evidence = build_issue_evidence(source, commands, artifacts)
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
            clean_room = load_clean_room_report(clean_room_report_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            blockers.append({"code": "CLEAN_ROOM_UNREADABLE", "detail": str(exc)})
        except EvidenceError as exc:
            blockers.append({"code": exc.code, "detail": exc.detail})
        else:
            artifacts.append(artifact(clean_room_report_path, "report", "clean-room-replay"))
    bundle: dict[str, Any] = {
        "schema": "fdir/qualification-evidence-bundle", "version": "1.0.0", "repository": "horiyamayoh/fdir",
        "source": source, "commands": commands, "artifacts": sorted(artifacts, key=lambda value: value["id"]),
        "index": {}, "barrier": {"releaseEligible": not blockers, "claimMode": "release-candidate" if not blockers else "experimental-bounded-subset", "productReleaseEligible": False, "productClaimMode": "experimental-bounded-subset", "blockers": blockers},
        "status": "passed" if not blockers else "blocked", "integrity": {},
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "issueEvidence": issue_evidence,
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
        clean_room = {
            "schema": "fdir/clean-room-replay-report",
            "version": "1.0.0",
            "status": "passed",
            "runs": 2,
            "sourceHeadSha": "0" * 40,
            "sourceTrackedDigest": "0" * 64,
            "inputs": [
                {"path": "raw-a.json", "sha256": "0" * 64, "bytes": 1, "indexPath": "raw-a.index.json", "indexSha256": "0" * 64, "indexBytes": 1, "bundleDigest": "0" * 64, "indexDigest": "0" * 64},
                {"path": "raw-b.json", "sha256": "0" * 64, "bytes": 1, "indexPath": "raw-b.index.json", "indexSha256": "0" * 64, "indexBytes": 1, "bundleDigest": "0" * 64, "indexDigest": "0" * 64},
            ],
            "volatileFields": [],
            "diffCount": 0,
            "diffDigest": "0" * 64,
        }
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
    compare_parser.add_argument("--output", type=Path)
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("bundle", type=Path)
    finalize_parser.add_argument("--index", type=Path, required=True)
    finalize_parser.add_argument("--clean-room-report", type=Path, required=True)
    finalize_parser.add_argument("--output", type=Path, required=True, help="final bundle path; raw replay input is never overwritten")
    finalize_parser.add_argument("--output-index", type=Path, required=True, help="final bundle index path")
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
            if args.output:
                output_path = root_path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(canonical(result))
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["status"] == "passed" else 1
        if args.operation == "finalize":
            result = finalize_bundle(root_path(args.bundle), root_path(args.index), root_path(args.clean_room_report), root_path(args.output), root_path(args.output_index))
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        result = self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except EvidenceError as exc:
        print(json.dumps({"schema":"fdir/evidence-bundle-error","status":"failed","code":exc.code,"detail":exc.detail}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
