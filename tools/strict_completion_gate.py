"""Fail-closed completion gate for the phase-2 GitHub issue contract.

This gate is deliberately stricter than the compatibility/release smoke gate.
It requires machine-readable evidence produced by the qualification commands;
exit status and file presence alone never satisfy a completion claim.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Keep the live issue-state scope separate from the qualification-report
# scope.  #87 is the umbrella and #108--#113 are release-barrier issues; none
# of them gets a duplicate qualification report.  ``RECOVERY_ISSUES`` remains
# as a compatibility alias for callers that use the old name for reports.
LIVE_ISSUES = tuple(range(87, 106)) + tuple(range(108, 114))
QUALIFICATION_ISSUES = tuple(range(88, 106))
BARRIER_ISSUES = tuple(range(108, 114))
RECOVERY_ISSUES = QUALIFICATION_ISSUES
RECOVERY_ISSUE_SET = set(QUALIFICATION_ISSUES)
LIVE_ISSUE_SET = set(LIVE_ISSUES)
CONTRACT_PATH = ROOT / "machine" / "strict-completion-contract.json"


def _declared_issue_numbers(value: Any) -> list[int] | None:
    if not isinstance(value, dict):
        return None
    numbers = value.get("issueNumbers")
    if not isinstance(numbers, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in numbers):
        return None
    return list(numbers)


def _load_bundle_scope(
    manifest: Path,
) -> tuple[dict[str, Any] | None, list[tuple[str, dict[str, Any]]], list[dict[str, str]]]:
    """Load the candidate bundle and independently enforce its issue scope.

    ``validate_qualification_bundle.py`` is the authoritative bundle
    validator, but this gate must not silently trust a validator result that a
    caller has replaced or that was produced from a stale contract.  The
    release boundary is therefore checked here as well: the manifest declares
    exactly #88--#105 and the union of report bindings is neither narrower nor
    wider than that set.  The umbrella and release-barrier issues are checked
    from the final GitHub snapshot, not fabricated as report entries.
    """

    blockers: list[dict[str, str]] = []
    manifest_path = manifest.resolve()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        blockers.append({"code": "BUNDLE_MANIFEST_UNREADABLE", "detail": f"cannot load {manifest_path}: {exc}"})
        return None, [], blockers
    if not isinstance(value, dict):
        blockers.append({"code": "BUNDLE_MANIFEST_SCHEMA", "detail": "bundle manifest root is not an object"})
        return None, [], blockers

    expected = list(QUALIFICATION_ISSUES)
    if _declared_issue_numbers(value) != expected:
        blockers.append({
            "code": "BUNDLE_ISSUE_SCOPE",
            "detail": f"bundle manifest issueNumbers must be exactly {expected}",
        })

    reports: list[tuple[str, dict[str, Any]]] = []
    reports_dir = manifest_path.parent / "reports"
    try:
        report_paths = sorted(path for path in reports_dir.glob("*.json") if path.is_file())
    except OSError as exc:
        report_paths = []
        blockers.append({"code": "BUNDLE_REPORTS_UNREADABLE", "detail": f"cannot enumerate {reports_dir}: {exc}"})
    if not report_paths:
        blockers.append({"code": "BUNDLE_REPORTS_MISSING", "detail": f"bundle has no reports/*.json files: {reports_dir}"})

    seen_issue_numbers: set[int] = set()
    for report_path in report_paths:
        relative = f"reports/{report_path.name}"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            blockers.append({"code": "BUNDLE_REPORT_UNREADABLE", "detail": f"cannot load {relative}: {exc}"})
            continue
        if not isinstance(report, dict):
            blockers.append({"code": "BUNDLE_REPORT_SCOPE", "detail": f"{relative} is not an object"})
            continue
        reports.append((relative, report))
        numbers = _declared_issue_numbers(report)
        if not numbers:
            blockers.append({"code": "BUNDLE_REPORT_SCOPE", "detail": f"{relative} has no valid issueNumbers binding"})
            continue
        if len(numbers) != len(set(numbers)):
            blockers.append({"code": "BUNDLE_REPORT_SCOPE", "detail": f"{relative} repeats an issue number: {numbers}"})
        unexpected = sorted(set(numbers) - RECOVERY_ISSUE_SET)
        if unexpected:
            blockers.append({
                "code": "BUNDLE_REPORT_SCOPE",
                "detail": f"{relative} binds out-of-scope issues: {unexpected}; expected only {expected}",
            })
        seen_issue_numbers.update(number for number in numbers if number in RECOVERY_ISSUE_SET)

    if seen_issue_numbers != RECOVERY_ISSUE_SET:
        missing = sorted(RECOVERY_ISSUE_SET - seen_issue_numbers)
        unexpected = sorted(seen_issue_numbers - RECOVERY_ISSUE_SET)
        blockers.append({
            "code": "BUNDLE_REPORT_SCOPE",
            "detail": f"bundle reports must cover exactly #88-#105; missing={missing}, unexpected={unexpected}",
        })
    return value, reports, blockers


def _static_release_claim_blockers(label: str, value: Any) -> list[dict[str, str]]:
    """Reject a candidate artifact that publishes release authority itself."""

    if not isinstance(value, dict):
        return []
    locations: list[tuple[str, dict[str, Any]]] = [(label, value)]
    for key in ("release", "releaseState", "releaseClaim", "finalRelease"):
        nested = value.get(key)
        if isinstance(nested, dict):
            locations.append((f"{label}.{key}", nested))

    blockers: list[dict[str, str]] = []
    for location, claim in locations:
        conflict = (
            claim.get("releaseReady") is True
            or claim.get("releaseEligible") is True
            or claim.get("releaseBlocked") is False
            or claim.get("status") == "release-ready"
            or claim.get("claimStatus") == "release-ready"
        )
        if conflict:
            blockers.append({
                "code": "STATIC_RELEASE_READY_CONTRADICTION",
                "detail": f"{location} contains a release-ready claim; only a final external attestation may claim release readiness",
            })
    return blockers


def _normalise_command_token(value: Any) -> str:
    return str(value).strip().strip("\"'").replace("\\", "/").casefold()


def _bundle_output_json(bundle_root: Path, report: dict[str, Any], basename: str) -> tuple[Path | None, list[dict[str, str]]]:
    outputs = report.get("outputs")
    if not isinstance(outputs, list):
        return None, [{"code": "CIRCULAR_105_EVIDENCE", "detail": f"#105 report has no outputs list for {basename}"}]
    matches = [
        item.get("path")
        for item in outputs
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and PurePosixPath(item["path"].replace("\\", "/")).name == basename
    ]
    if len(matches) != 1:
        return None, [{
            "code": "CIRCULAR_105_EVIDENCE",
            "detail": f"#105 report must bind exactly one {basename}; found {matches}",
        }]
    relative = PurePosixPath(str(matches[0]).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return None, [{"code": "CIRCULAR_105_EVIDENCE", "detail": f"#105 output path escapes the bundle: {matches[0]}"}]
    target = (bundle_root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(bundle_root.resolve())
    except ValueError:
        return None, [{"code": "CIRCULAR_105_EVIDENCE", "detail": f"#105 output path escapes the bundle: {matches[0]}"}]
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [{"code": "CIRCULAR_105_EVIDENCE", "detail": f"#105 producer report is unreadable: {exc}"}]
    if not isinstance(value, dict):
        return None, [{"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 producer report is not a JSON object"}]
    return target, _static_release_claim_blockers("#105 producer report", value)


def _candidate_105_blockers(
    manifest: dict[str, Any] | None,
    reports: list[tuple[str, dict[str, Any]]],
    bundle_root: Path,
) -> list[dict[str, str]]:
    """Apply the non-circular candidate rules before any release check runs."""

    blockers = _static_release_claim_blockers("bundle manifest", manifest)
    for relative, report in reports:
        blockers.extend(_static_release_claim_blockers(relative, report))

    candidates = [
        (relative, report)
        for relative, report in reports
        if report.get("evidenceId") == "issue-105-release-quality"
    ]
    if len(candidates) != 1:
        blockers.append({
            "code": "BUNDLE_105_EVIDENCE_MISSING" if not candidates else "BUNDLE_105_EVIDENCE_SCOPE",
            "detail": f"bundle must contain exactly one issue-105-release-quality report; found {len(candidates)}",
        })
        return blockers

    relative, candidate = candidates[0]
    command = candidate.get("command")
    if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
        blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": f"{relative} command is not a string array produced by the #105 behavioral runner"})
    else:
        tokens = [_normalise_command_token(item) for item in command]
        has_issue_runner = any(PurePosixPath(token).name == "qualification_issue105.py" for token in tokens)
        if not has_issue_runner:
            blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 candidate evidence must be produced by tools/qualification_issue105.py"})
        forbidden_authorities = {"release_gate.py", "release_attestation.py", "strict_completion_gate.py"}
        if any(PurePosixPath(token).name in forbidden_authorities for token in tokens):
            blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 candidate evidence invokes a release or strict-completion authority"})
        module_command = " ".join(tokens).replace("/", ".")
        if any(name in module_command for name in ("tools.release_gate", "tools.release_attestation", "tools.strict_completion_gate")):
            blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 candidate evidence invokes a release or strict-completion module"})
        if any(token == "--bundle" or token.startswith("--bundle=") or token == "--attestation" or token.startswith("--attestation=") for token in tokens):
            blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 candidate evidence must not invoke bundle or attestation qualification"})
        if any(token in {"--release", "--release-ready"} or token.startswith("--release-ready=") for token in tokens):
            blockers.append({"code": "CIRCULAR_105_EVIDENCE", "detail": "#105 candidate evidence must not request release readiness"})

    producer_path, producer_blockers = _bundle_output_json(bundle_root, candidate, "producer-report.json")
    del producer_path  # The path is diagnostic material; the JSON claim is what matters here.
    blockers.extend(producer_blockers)
    return blockers


def _bundle_issue_reports(manifest: Path) -> list[dict[str, Any]]:
    """Materialize one strict-report entry for every recovery issue.

    A list of issue numbers is not sufficient evidence for #113: reviewers
    need to see which Evidence report, outputs, assertions, and cases were
    actually resolved for each issue.  This helper is intentionally tolerant
    of an invalid candidate so a blocked report still explains missing
    material instead of hiding it behind a top-level validator error.
    """

    issue_numbers = list(RECOVERY_ISSUES)
    try:
        bundle_root = manifest.resolve().parent
        json.loads(manifest.resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [
            {
                "issueNumber": issue,
                "status": "blocked",
                "evidenceIds": [],
                "reportPaths": [],
                "reports": [],
                "outputPaths": [],
                "assertionCount": 0,
                "testCaseCount": 0,
                "sourceSha": None,
                "blockers": [{"code": "BUNDLE_UNREADABLE", "detail": str(manifest)}],
                "liveState": "pending-final-attestation",
            }
            for issue in issue_numbers
        ]

    parsed_reports: list[tuple[str, dict[str, Any]]] = []
    reports_dir = bundle_root / "reports"
    try:
        report_paths = sorted(reports_dir.glob("*.json"))
    except OSError:
        report_paths = []
    for report_path in report_paths:
        try:
            value = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            parsed_reports.append((f"reports/{report_path.name}", value))

    entries: list[dict[str, Any]] = []
    for issue in issue_numbers:
        matching = [
            (report_path, value)
            for report_path, value in parsed_reports
            if issue in (_declared_issue_numbers(value) or [])
        ]
        blockers: list[dict[str, str]] = []
        if not matching:
            blockers.append({"code": "ISSUE_EVIDENCE_MISSING", "detail": f"issue #{issue} has no report bound by issueNumbers"})

        evidence_ids: list[str] = []
        report_paths: list[str] = []
        output_paths: list[str] = []
        report_records: list[dict[str, Any]] = []
        source_shas: set[str] = set()
        assertion_count = 0
        test_case_count = 0
        for report_path, value in matching:
            report_paths.append(report_path)
            evidence_id = value.get("evidenceId")
            if isinstance(evidence_id, str) and evidence_id:
                evidence_ids.append(evidence_id)
            source_sha = value.get("sourceSha")
            if isinstance(source_sha, str) and source_sha:
                source_shas.add(source_sha)
            outputs = value.get("outputs", [])
            report_output_paths: list[str] = []
            if isinstance(outputs, list):
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    output_path = output.get("path")
                    if isinstance(output_path, str) and output_path:
                        report_output_paths.append(output_path)
                        output_paths.append(output_path)
            status = value.get("status")
            failure_count = value.get("failureCount")
            if status != "passed" or failure_count != 0:
                blockers.append({"code": "ISSUE_EVIDENCE_NOT_PASSED", "detail": f"{report_path} is {status!r} with failureCount={failure_count!r}"})
            report_assertions = value.get("assertions", [])
            report_cases = value.get("cases", [])
            assertion_count += len(report_assertions) if isinstance(report_assertions, list) else 0
            test_case_count += len(report_cases) if isinstance(report_cases, list) else 0
            report_records.append({
                "evidenceId": evidence_id,
                "reportPath": report_path,
                "status": status,
                "failureCount": failure_count,
                "sourceSha": source_sha,
                "assertionCount": len(report_assertions) if isinstance(report_assertions, list) else 0,
                "testCaseCount": len(report_cases) if isinstance(report_cases, list) else 0,
                "outputPaths": sorted(set(report_output_paths)),
            })

        entries.append({
            "issueNumber": issue,
            "status": "passed" if matching and not blockers else "blocked",
            "evidenceIds": sorted(set(evidence_ids)),
            "reportPaths": sorted(set(report_paths)),
            "reports": report_records,
            "outputPaths": sorted(set(output_paths)),
            "assertionCount": assertion_count,
            "testCaseCount": test_case_count,
            "sourceSha": next(iter(source_shas)) if len(source_shas) == 1 else None,
            "blockers": blockers,
            "liveState": "pending-final-attestation",
        })
    return entries


def _live_issue_state(snapshot: Any) -> dict[str, Any]:
    """Render live issue scope separately from qualification report scope."""

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("issues"), list):
        return {
            "status": "blocked",
            "issueNumbers": list(LIVE_ISSUES),
            "snapshotDigest": snapshot.get("snapshotDigest") if isinstance(snapshot, dict) else None,
            "blockers": [{"code": "ISSUE_STATE_MISSING", "detail": "final attestation has no verified issue snapshot"}],
        }
    issue_numbers = [
        item.get("issueNumber")
        for item in snapshot["issues"]
        if isinstance(item, dict)
    ]
    blockers: list[dict[str, str]] = []
    if issue_numbers != list(LIVE_ISSUES):
        blockers.append({
            "code": "ISSUE_STATE_SCOPE",
            "detail": f"final issue snapshot must be ordered as {list(LIVE_ISSUES)}",
        })
    by_number = {
        item.get("issueNumber"): item
        for item in snapshot["issues"]
        if isinstance(item, dict) and isinstance(item.get("issueNumber"), int)
    }
    missing = sorted(LIVE_ISSUE_SET - set(by_number))
    if missing:
        blockers.append({"code": "ISSUE_STATE_SCOPE", "detail": f"final issue snapshot is missing {missing}"})
    incomplete = [
        number
        for number in LIVE_ISSUES
        if number in by_number
        and not (
            by_number[number].get("state") == "closed"
            and by_number[number].get("stateReason") == "completed"
            and by_number[number].get("closedAt") is not None
        )
    ]
    if incomplete:
        blockers.append({"code": "ISSUE_NOT_COMPLETED", "detail": f"final issue snapshot has incomplete issues: {incomplete}"})
    return {
        "status": "verified" if not blockers else "blocked",
        "issueNumbers": list(LIVE_ISSUES),
        "snapshotDigest": snapshot.get("snapshotDigest"),
        "blockers": blockers,
    }


def _attested_issue_reports(bundle: Path | None, snapshot: Any) -> list[dict[str, Any]]:
    """Bind candidate issue evidence to the externally verified live state."""

    entries = _bundle_issue_reports(bundle) if bundle is not None else []
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("issues"), list):
        return entries
    snapshot_digest = snapshot.get("snapshotDigest")
    live_by_issue = {
        item.get("issueNumber"): item
        for item in snapshot["issues"]
        if isinstance(item, dict) and isinstance(item.get("issueNumber"), int)
    }
    for entry in entries:
        issue = entry.get("issueNumber")
        live = live_by_issue.get(issue)
        if not isinstance(live, dict):
            entry["status"] = "blocked"
            entry["liveState"] = "blocked"
            entry.setdefault("blockers", []).append({"code": "ISSUE_STATE_MISSING", "detail": f"final snapshot has no issue #{issue}"})
            continue
        entry.update({
            "state": live.get("state"),
            "stateReason": live.get("stateReason"),
            "closedAt": live.get("closedAt"),
            "updatedAt": live.get("updatedAt"),
            "snapshotDigest": snapshot_digest,
        })
        completed = live.get("state") == "closed" and live.get("stateReason") == "completed" and live.get("closedAt") is not None
        if completed and entry.get("status") == "passed":
            entry["liveState"] = "verified"
        else:
            entry["status"] = "blocked"
            entry["liveState"] = "blocked"
            entry.setdefault("blockers", []).append({"code": "ISSUE_NOT_COMPLETED", "detail": f"issue #{issue} is not completed in the final live snapshot"})
    return entries


def run() -> dict[str, Any]:
    """Compatibility entry point for the old no-bundle development smoke.

    Completion is intentionally not available through this API.  The only
    passing paths are ``run_bundle`` (candidate qualification) and
    ``run_attestation`` (final external authority).
    """

    return _legacy_path_report("smoke")


def run_bundle(manifest: Path, *, allow_dirty: bool = False) -> dict[str, Any]:
    """Run the commit-bound Evidence validator for every recovery child.

    The old phase-2 report is intentionally not a release result.  A bundle
    invocation is the only strict completion path and its issue scope is the
    full #88--#105 recovery contract, not just #88.
    """

    manifest_value, bundle_reports, scope_blockers = _load_bundle_scope(manifest)
    blockers: list[dict[str, Any]] = list(scope_blockers)
    blockers.extend(_candidate_105_blockers(manifest_value, bundle_reports, manifest.resolve().parent))

    try:
        try:
            from validate_qualification_bundle import validate_bundle
        except ImportError:  # pragma: no cover
            from tools.validate_qualification_bundle import validate_bundle
        validation = validate_bundle(manifest, repo_root=ROOT, allow_dirty=allow_dirty)
    except Exception as exc:
        validation = {
            "schema": "fdir/qualification-validation-report",
            "status": "failed",
            "diagnostics": [{"code": "BUNDLE_VALIDATOR_ERROR", "detail": f"{type(exc).__name__}: {exc}"}],
        }
    if not isinstance(validation, dict):
        validation = {
            "schema": "fdir/qualification-validation-report",
            "status": "failed",
            "diagnostics": [{"code": "BUNDLE_VALIDATOR_REPORT", "detail": "bundle validator did not return an object"}],
        }
    diagnostics = validation.get("diagnostics", [])
    if isinstance(diagnostics, list):
        blockers.extend(item for item in diagnostics if isinstance(item, dict))
    else:
        blockers.append({"code": "BUNDLE_VALIDATOR_REPORT", "detail": "bundle validator diagnostics are not a list"})
    bundle_checks: dict[str, Any] | None = None
    if validation.get("status") == "passed" and not blockers:
        try:
            try:
                from release_gate import check_qualification_bundle
            except ImportError:  # pragma: no cover
                from tools.release_gate import check_qualification_bundle
            bundle_checks = check_qualification_bundle(manifest)
        except Exception as exc:
            blockers.append({"code": getattr(exc, "code", "QUALIFICATION_BUNDLE_CONTENT_INVALID"), "detail": str(exc)})
        if not blockers:
            try:
                try:
                    from release_attestation import validate_candidate_bundle
                except ImportError:  # pragma: no cover
                    from tools.release_attestation import validate_candidate_bundle
                validate_candidate_bundle(manifest, allow_dirty=allow_dirty)
            except Exception as exc:
                blockers.append({"code": getattr(exc, "code", "CANDIDATE_BUNDLE_INVALID"), "detail": str(exc)})
    issues = list(LIVE_ISSUES)
    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "passed" if validation.get("status") == "passed" and not blockers else "blocked",
        "mode": "candidate",
        "releaseReady": False,
        "issues": issues,
        "qualificationIssues": list(QUALIFICATION_ISSUES),
        "barrierIssues": list(BARRIER_ISSUES),
        "blockers": blockers,
        "reportsChecked": ["qualification-bundle", *[f"issue-{issue}" for issue in QUALIFICATION_ISSUES]],
        "issueReports": _bundle_issue_reports(manifest),
        "scope": {
            "issueNumbers": issues,
            "qualificationIssueNumbers": list(QUALIFICATION_ISSUES),
            "barrierIssueNumbers": list(BARRIER_ISSUES),
            "exact": not any(item.get("code") in {"BUNDLE_ISSUE_SCOPE", "BUNDLE_REPORT_SCOPE"} for item in blockers),
        },
        "bundleValidation": validation,
        "bundleChecks": bundle_checks,
    }


def _blocked_report(code: str, detail: str, *, mode: str = "final-attestation", bundle: Path | None = None, snapshot: Any = None) -> dict[str, Any]:
    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "blocked",
        "mode": mode,
        "releaseReady": False,
        "issues": list(LIVE_ISSUES),
        "qualificationIssues": list(QUALIFICATION_ISSUES),
        "barrierIssues": list(BARRIER_ISSUES),
        "blockers": [{"code": code, "detail": detail}],
        "reportsChecked": ["final-attestation"],
        "issueReports": _bundle_issue_reports(bundle) if bundle is not None else [],
        "liveIssueState": _live_issue_state(snapshot),
    }


def run_attestation(attestation: Path, *, bundle: Path | None = None) -> dict[str, Any]:
    """Validate a final attestation without re-entering the legacy gate."""

    if bundle is None:
        return _blocked_report(
            "BUNDLE_REQUIRED",
            "final attestation validation is diagnostic-only without the exact candidate bundle; pass --bundle and --attestation together",
        )
    if os.environ.get("GITHUB_ACTIONS", "").casefold() != "true":
        return _blocked_report(
            "ATTESTATION_CI_PROVIDER",
            "final attestation authority must be validated in GitHub Actions",
            bundle=bundle,
        )
    try:
        try:
            from release_attestation import load_and_validate_attestation
        except ImportError:  # pragma: no cover
            from tools.release_attestation import load_and_validate_attestation
        actions = True
        try:
            from release_gate import current_head
        except ImportError:  # pragma: no cover
            from tools.release_gate import current_head
        source_sha = current_head()
        if os.environ.get("GITHUB_SHA") != source_sha:
            raise RuntimeError("GITHUB_SHA does not match the inspected checkout HEAD")
        for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
            if not os.environ.get(name):
                raise RuntimeError(f"GitHub Actions identity is missing {name}")
        expected_attempt: Any = os.environ.get("GITHUB_RUN_ATTEMPT")
        if isinstance(expected_attempt, str) and expected_attempt.isdigit():
            expected_attempt = int(expected_attempt)
        result = load_and_validate_attestation(
            attestation,
            bundle_manifest_path=bundle,
            expected_source_sha=source_sha,
            expected_run_id=os.environ.get("GITHUB_RUN_ID") if actions else None,
            expected_attempt=expected_attempt,
        )
    except Exception as exc:
        return _blocked_report(
            getattr(exc, "code", "ATTESTATION_INVALID"),
            str(exc),
            bundle=bundle,
        )
    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "passed",
        "mode": "final-attestation",
        "releaseReady": True,
        "issues": list(LIVE_ISSUES),
        "qualificationIssues": list(QUALIFICATION_ISSUES),
        "barrierIssues": list(BARRIER_ISSUES),
        "blockers": [],
        "reportsChecked": ["qualification-bundle", "final-attestation", *[f"issue-{issue}" for issue in QUALIFICATION_ISSUES]],
        "issueReports": _attested_issue_reports(bundle, result.get("snapshot")),
        "liveIssueState": _live_issue_state(result.get("snapshot")),
        "attestation": result,
    }


def _legacy_path_report(mode: str = "smoke") -> dict[str, Any]:
    """Return a non-release report instead of allowing the old path to pass."""

    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "blocked",
        "mode": mode,
        "releaseReady": False,
        "issues": list(LIVE_ISSUES),
        "qualificationIssues": list(QUALIFICATION_ISSUES),
        "barrierIssues": list(BARRIER_ISSUES),
        "blockers": [{
            "code": "LEGACY_COMPLETION_PATH_DISABLED",
            "detail": "strict completion release qualification requires --bundle and --attestation; the bundleless legacy path is diagnostic-only",
        }],
        "reportsChecked": [],
        "issueReports": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="validate a commit-bound qualification manifest")
    parser.add_argument("--attestation", type=Path, help="validate a final release attestation")
    parser.add_argument("--mode", choices=("smoke", "release"), default=None)
    parser.add_argument("--allow-dirty", action="store_true", help="permit a dirty bundle for local development")
    args = parser.parse_args(argv)
    try:
        if args.bundle is not None:
            report = run_bundle(args.bundle, allow_dirty=args.allow_dirty)
            if args.attestation is not None and report.get("status") == "passed":
                report = run_attestation(args.attestation, bundle=args.bundle)
        elif args.attestation is not None:
            report = run_attestation(args.attestation)
        else:
            report = _legacy_path_report("release" if args.mode == "release" else "smoke")
    except Exception as exc:
        report = {"schema": "fdir/strict-completion-gate-report", "version": "1.2.0", "status": "blocked", "mode": "release" if args.mode == "release" else "smoke", "releaseReady": False, "issues": list(LIVE_ISSUES), "qualificationIssues": list(QUALIFICATION_ISSUES), "barrierIssues": list(BARRIER_ISSUES), "blockers": [{"code": "GATE_ERROR", "detail": f"{type(exc).__name__}: {exc}"}]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
