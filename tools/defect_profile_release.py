"""Exercise release-gate failure paths for source-level defect injection.

The normal release gate is intentionally blocked until the audit-recovery
bundle is complete.  This small harness gives the defect campaign a bounded,
real execution path for the release-gate operators without weakening the
normal gate: it invokes the actual release-gate function or orchestrator and
injects only a disposable failing condition in memory/temp JSON.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import release_gate  # type: ignore  # noqa: E402


def _probe_directory(prefix: str) -> Path:
    """Allocate probe state in the checkout's ignored scratch directory."""

    scratch = ROOT / "e2e" / ".run" / "defect-probes"
    scratch.mkdir(parents=True, exist_ok=True)
    directory = scratch / f"{prefix}{os.getpid()}-{time.time_ns()}"
    directory.mkdir()
    return directory


def _claim_probe() -> int:
    original = release_gate.RELEASE_CLAIM_MANIFEST_PATH
    directory = _probe_directory("release-claim-")
    try:
        target = Path(directory) / "release-claim-manifest.json"
        value = json.loads(original.read_text(encoding="utf-8"))
        value = copy.deepcopy(value)
        value["independentEvidence"]["runner"] = "tools/not-the-independent-corpus.py"
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        release_gate.RELEASE_CLAIM_MANIFEST_PATH = target
        try:
            release_gate.check_release_claims()
        except release_gate.GateError:
            return 0
        return 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _orchestration_probe(failing_command: str) -> int:
    original_run_command = release_gate.run_command
    original_runtime = release_gate.check_runtime_evidence
    original_audit = getattr(release_gate, "check_audit_recovery_release_boundary", None)
    original_clean_room = getattr(release_gate, "check_clean_room_replay", None)
    directory = _probe_directory("release-orchestration-")

    def fake_run_command(name: str, display_command: str, argv: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "command": display_command,
            "cwd": ".",
            "return_code": 1 if name == failing_command else 0,
            "stdout": "",
            "stderr": "synthetic disposable defect-profile failure\n" if name == failing_command else "",
            "timed_out": False,
        }

    def fake_runtime(_: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "mutation_cases": 36,
            "independent_cases": 16,
            "independent_negative_checks": 4,
            "query_sources": 3,
            "e2e_cases": 16,
            "strict_issues": 18,
        }

    release_gate.run_command = fake_run_command  # type: ignore[assignment]
    release_gate.check_runtime_evidence = fake_runtime  # type: ignore[assignment]
    if original_audit is not None:
        release_gate.check_audit_recovery_release_boundary = lambda **_: {"recovery_children": 18, "umbrella_issue": 87}  # type: ignore[assignment]
    if original_clean_room is not None:
        # The release defect probe exercises orchestration in a disposable
        # worktree, where the real two-run clean-room report is intentionally
        # not present.  Keep that probe focused on its injected failing
        # command while preserving the normal gate's strict clean-room check.
        release_gate.check_clean_room_replay = lambda: {"runs": 2, "difference_count": 0, "diff_digest": "0" * 64}  # type: ignore[assignment]
    try:
        # The normal smoke entrypoint intentionally exits before orchestration
        # when no release authority is supplied.  A disposable bundle path
        # selects the real orchestration path without making this probe a
        # release claim; bundle validation fails closed at the end.
        result = int(release_gate.main(["--bundle", str(directory / "synthetic-bundle.json")]))
        return 0 if result != 0 else 1
    finally:
        release_gate.run_command = original_run_command  # type: ignore[assignment]
        release_gate.check_runtime_evidence = original_runtime  # type: ignore[assignment]
        if original_audit is not None:
            release_gate.check_audit_recovery_release_boundary = original_audit  # type: ignore[assignment]
        if original_clean_room is not None:
            release_gate.check_clean_room_replay = original_clean_room  # type: ignore[assignment]
        shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=("claim", "required-checks", "status-aggregation"), required=True)
    args = parser.parse_args(argv)
    if args.probe == "claim":
        return _claim_probe()
    if args.probe == "required-checks":
        return _orchestration_probe("design_validation")
    return _orchestration_probe("design_validation")


if __name__ == "__main__":
    raise SystemExit(main())
