"""Exercise the fail-closed release contract with executable negative cases.

This is a small control-plane qualification, not a replacement for the full
release gate.  Each case changes one in-memory manifest or claim and asserts
the exact guard that must reject it.  A later guard cannot mask a disabled
guard because the expected diagnostic is checked explicitly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_gate  # type: ignore


class QualificationError(AssertionError):
    pass


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationError(f"manifest is not an object: {path}")
    return value


def _runtime_commands() -> list[dict[str, Any]]:
    return [
        {
            "name": "evidence_bundle_self_test",
            "return_code": 0,
            "stdout": json.dumps({"status": "passed", "negativeCount": 8}),
        },
        {
            "name": "defect_injection_bounded",
            "return_code": 0,
            "stdout": json.dumps({
                "campaignStatus": "passed",
                "survivors": [],
                "counts": {"infrastructure-error": 0, "timeout": 0},
                "coverage": {"coverageStatus": "passed"},
            }),
        },
    ]


def _with_overrides(overrides: dict[Path, dict[str, Any]], callback: Callable[[], Any]) -> Any:
    original = release_gate.load_json
    normalized = {path.resolve(): copy.deepcopy(value) for path, value in overrides.items()}

    def patched(path: Path) -> dict[str, Any]:
        candidate = normalized.get(Path(path).resolve())
        return copy.deepcopy(candidate) if candidate is not None else original(path)

    release_gate.load_json = patched  # type: ignore[assignment]
    try:
        return callback()
    finally:
        release_gate.load_json = original  # type: ignore[assignment]


def _rejects(label: str, callback: Callable[[], Any], expected: str) -> dict[str, Any]:
    try:
        callback()
    except release_gate.GateError as exc:
        observed = str(exc)
        if expected not in observed:
            raise QualificationError(f"{label}: expected diagnostic containing {expected!r}, got {observed!r}") from exc
        return {"id": label, "status": "killed", "expected": expected, "observed": observed}
    raise QualificationError(f"{label}: weakened release guard was accepted")


def run() -> dict[str, Any]:
    plan = _load(release_gate.AUDIT_RECOVERY_PLAN_PATH)
    requirements = _load(release_gate.RELEASE_REQUIREMENTS_PATH)
    phase2 = _load(release_gate.PHASE2_ISSUE_PLAN_PATH)
    cases: list[dict[str, Any]] = []

    issue_state = copy.deepcopy(plan)
    issue_state.setdefault("liveState", {})["requiredAtGate"] = False
    cases.append(_rejects(
        "issue-state-required",
        lambda: _with_overrides(
            {release_gate.AUDIT_RECOVERY_PLAN_PATH: issue_state},
            lambda: release_gate.check_evidence_release_track(None, _runtime_commands()),
        ),
        "live GitHub state",
    ))

    missing_ci_command = copy.deepcopy(phase2)
    missing_ci_command.setdefault("policy", {})["requiredCommands"] = []
    cases.append(_rejects(
        "ci-required-check",
        lambda: _with_overrides(
            {release_gate.PHASE2_ISSUE_PLAN_PATH: missing_ci_command},
            release_gate.check_phase2_contracts,
        ),
        "qualification commands are incomplete",
    ))

    overclaimed_scope = copy.deepcopy(requirements)
    overclaimed_scope["releaseEligible"] = True
    cases.append(_rejects(
        "unsupported-scope-barrier",
        lambda: _with_overrides(
            {release_gate.RELEASE_REQUIREMENTS_PATH: overclaimed_scope},
            lambda: release_gate.check_evidence_release_track(None, _runtime_commands()),
        ),
        "overclaim",
    ))

    cases.append(_rejects(
        "clean-room-replay",
        lambda: release_gate.check_clean_room_claim({"cleanRoom": {"status": "passed", "runs": 2, "diffCount": 1}}),
        "clean-room replay evidence",
    ))

    return {
        "schema": "fdir/release-contract-qualification-report",
        "version": "1.0.0",
        "status": "passed",
        "cases": cases,
        "survivors": [],
        "negativeCount": len(cases),
    }


def main() -> int:
    try:
        report = run()
    except Exception as exc:
        report = {
            "schema": "fdir/release-contract-qualification-report",
            "version": "1.0.0",
            "status": "failed",
            "cases": [],
            "survivors": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" and report.get("survivors") == [] else 1


if __name__ == "__main__":
    raise SystemExit(main())
