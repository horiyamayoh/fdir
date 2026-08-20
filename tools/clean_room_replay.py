"""Run the real-input qualification twice from clean trees and compare it.

The release gate must prove more than that one checkout can pass.  This
runner archives the current commit, extracts two independent clean trees, and
executes the public E2E command in each tree.  Only path-normalised reports
are compared; source-derived bytes, canonical digests, feature inventories,
diagnostics, and query results remain part of the comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_PATH_RE = re.compile(r"(?:^|/)e2e/\.run/run-\d+(?:/(.*))?$", re.IGNORECASE)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stdout + result.stderr).strip()}")
    return result.stdout.strip()


def safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"git archive contains an unsafe path: {member.name}")
    # Do not ask tarfile to restore Git's POSIX mode bits: this managed
    # Windows workspace can make a directory inaccessible when tarfile
    # applies them.  The archive is from our own commit and contains only
    # regular files/directories; reject anything else explicitly.
    for member in archive.getmembers():
        target = destination / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"git archive member has no data: {member.name}")
            with target.open("wb") as stream:
                stream.write(source.read())
        else:
            raise RuntimeError(f"git archive contains unsupported member: {member.name}")


def archive_head(destination: Path) -> None:
    archive = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "archive", "--format=tar", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        detail = archive.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git archive failed: {detail.strip()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as stream:
        safe_extract(stream, destination)


def normalize_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize_paths(child) for key, child in value.items()}
    if isinstance(value, list):
        return [normalize_paths(child) for child in value]
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/")
    match = RUN_PATH_RE.search(normalized)
    if match:
        suffix = match.group(1)
        return "<clean-room>" if not suffix else "<clean-room>/" + suffix
    return normalized


def diff_values(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": path, "left": left, "right": right, "reason": "type"}]
    if isinstance(left, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}"
            if key not in left:
                differences.append({"path": child_path, "right": right[key], "reason": "missing-left"})
            elif key not in right:
                differences.append({"path": child_path, "left": left[key], "reason": "missing-right"})
            else:
                differences.extend(diff_values(left[key], right[key], child_path))
            if len(differences) >= 100:
                return differences[:100]
        return differences
    if isinstance(left, list):
        differences = []
        if len(left) != len(right):
            differences.append({"path": path, "leftLength": len(left), "rightLength": len(right), "reason": "length"})
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(diff_values(left_item, right_item, f"{path}[{index}]"))
            if len(differences) >= 100:
                return differences[:100]
        return differences[:100]
    if left != right:
        return [{"path": path, "left": left, "right": right, "reason": "value"}]
    return []


def run_e2e(clean_root: Path, run_number: int) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONHASHSEED"] = "0"
    try:
        result = subprocess.run(
            [sys.executable, "tools/run_e2e.py", "--all", "--json"],
            cwd=clean_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        return {
            "run": run_number,
            "status": "failed",
            "returnCode": 124,
            "timedOut": True,
            "stdoutSha256": sha256_bytes(stdout.encode("utf-8")),
            "stderrSha256": sha256_bytes(stderr.encode("utf-8")),
            "error": "clean-room E2E timed out after 300 seconds",
        }

    result_record: dict[str, Any] = {
        "run": run_number,
        "status": "failed",
        "returnCode": result.returncode,
        "timedOut": False,
        "stdoutSha256": sha256_bytes(result.stdout.encode("utf-8")),
        "stderrSha256": sha256_bytes(result.stderr.encode("utf-8")),
    }
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        result_record["error"] = f"E2E command did not emit JSON: {exc}"
        return result_record
    if not isinstance(report, dict):
        result_record["error"] = "E2E report root is not an object"
        return result_record
    normalized = normalize_paths(report)
    normalized_bytes = canonical_bytes(normalized)
    result_record.update(
        {
            "status": "passed" if result.returncode == 0 and report.get("status") == "passed" else "failed",
            "reportDigest": sha256_bytes(normalized_bytes),
            "report": normalized,
        }
    )
    return result_record


def build_report(source_sha: str, output: Path) -> dict[str, Any]:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise RuntimeError(f"source SHA is not a 40-character lowercase SHA: {source_sha!r}")
    runs: list[dict[str, Any]] = []
    # Managed Windows workstations can deny access to Python's 0700 temporary
    # directories.  Use the repository's ignored run area, whose ACL is
    # intentionally writable, and retain it for post-failure inspection.
    scratch_parent = ROOT / "e2e" / ".run"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    root = scratch_parent / f"clean-room-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    for run_number in (1, 2):
        clean_root = root / f"run-{run_number}"
        clean_root.mkdir()
        archive_head(clean_root)
        runs.append(run_e2e(clean_root, run_number))

    differences: list[dict[str, Any]] = []
    if len(runs) == 2 and "report" in runs[0] and "report" in runs[1]:
        differences = diff_values(runs[0]["report"], runs[1]["report"])
    else:
        differences = [{"path": "$.runs", "reason": "missing-report"}]
    diff_digest = sha256_bytes(canonical_bytes(differences))
    status = "passed" if all(item.get("status") == "passed" for item in runs) and not differences else "failed"
    report = {
        "schema": "fdir/clean-room-replay-report",
        "version": "1.0.0",
        "sourceSha": source_sha,
        "command": "python tools/run_e2e.py --all --json",
        "runs": [
            {
                key: value
                for key, value in item.items()
                if key != "report"
            }
            for item in runs
        ],
        "comparison": {
            "status": "passed" if not differences else "failed",
            "differenceCount": len(differences),
            "differences": differences,
            "diffDigest": diff_digest,
        },
        "status": status,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("e2e/.run/clean-room-replay.json"))
    parser.add_argument("--source-sha")
    args = parser.parse_args(argv)
    try:
        source_sha = args.source_sha or git("rev-parse", "HEAD")
        output = args.out if args.out.is_absolute() else ROOT / args.out
        report = build_report(source_sha, output.resolve())
    except Exception as exc:
        print(f"CLEAN ROOM REPLAY ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "sourceSha": report["sourceSha"], "diffDigest": report["comparison"]["diffDigest"], "differenceCount": report["comparison"]["differenceCount"], "path": str(output.resolve())}, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
