"""Independently validate the executable qualification contract.

The qualification contract is a release authority, not documentation.  This
validator checks its binding structure without running any qualification
command and without treating a command name, an output file, or an exit code
as evidence of success.  Producer reports are validated separately by the
qualification bundle validator; this tool makes sure the contract actually
requires the closed producer-report envelope and the declared behavioral
case inventory for every issue in scope.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
# The recovery program has two deliberately different projections.  The
# release scope contains the parent issue and the six release blockers, while
# only #88-#105 may introduce Evidence/behavioral reports.  The blockers are
# represented by barrierCoverage on the #88/#105 evidence, never by synthetic
# duplicate reports.
TARGET_ISSUES = tuple(range(87, 106)) + tuple(range(108, 114))
# Evidence/report scope includes the foundation reports #88-#105.  The
# behavioral-report sub-contract starts at #91; #88-#90 remain structural
# integrity/model evidence and must not be forced through the behavioral
# case-inventory rules.
REPORT_ISSUES = tuple(range(88, 106))
BEHAVIORAL_ISSUES = tuple(range(91, 106))
PARENT_ISSUE = 87
RELEASE_BLOCKER_ISSUES = tuple(range(108, 114))
BARRIER_ISSUES = (PARENT_ISSUE,) + RELEASE_BLOCKER_ISSUES
EXPECTED_ISSUES = REPORT_ISSUES  # compatibility name used by older callers
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VALID_GRADES = frozenset({"I0", "I1", "I2", "I3", "I4"})
REPORT_VERSION = "1.0.0"
VALID_CLASSIFICATIONS = frozenset(
    {
        "positive",
        "positive-oracle",
        "negative",
        "mutation",
        "metamorphic",
        "differential",
        "hostile",
        "oracle",
        "replay",
    }
)
GENERIC_PASS_ASSERTIONS = frozenset(
    {
        "qualification-command-exits-zero",
        "declared-output-files-bound",
        "source-sha-is-current-head",
    }
)
PRODUCER_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_VERSION = REPORT_VERSION
PRODUCER_ROLE = "producer-report"
GENERIC_QUALIFICATION_RUNNERS = frozenset(
    {
        "tools/run_acceptance.py",
        "tools/run_e2e.py",
        "tools/validate_design.py",
        "tools/release_gate.py",
        "tools/strict_completion_gate.py",
        "tools/mutation_qualification.py",
        "tools/query_qualification.py",
        "tools/independent_corpus.py",
    }
)
GENERIC_REPORT_KINDS = frozenset(
    {
        "suite",
        "generic-suite",
        "qualification-suite",
        "source",
        "source-snapshot",
        "snapshot",
        "manifest",
        "static",
    }
)


class ContractValidationError(ValueError):
    """Raised when the contract cannot be read as a JSON object."""


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else (ROOT / path).resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot read JSON contract {path}: {exc}") from exc


def _is_relative_repository_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        _resolve(candidate).relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def _require_string(value: Any, label: str, findings: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        findings.append(f"{label} must be a non-empty string")


def _require_unique(values: Iterable[Any], label: str, findings: list[str]) -> None:
    items = list(values)
    try:
        unique_count = len(set(items))
    except TypeError:
        unique_count = len(
            {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in items
            }
        )
    if len(items) != unique_count:
        findings.append(f"{label} contains duplicate values")


def _validate_paths(items: Any, label: str, findings: list[str], *, require_exists: bool) -> None:
    if not isinstance(items, list) or not items:
        findings.append(f"{label} must be a non-empty array")
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            findings.append(f"{label}[{index}] must be an object")
            continue
        path = item.get("path")
        if not _is_relative_repository_path(path):
            findings.append(f"{label}[{index}].path is not a safe repository-relative path")
        elif require_exists and not _resolve(Path(path)).exists():
            findings.append(f"{label}[{index}].path does not exist: {path}")


def _normalise_barrier_entries(value: Any, label: str, findings: list[str]) -> list[dict[str, Any]]:
    """Return barrier entries from the supported closed representations.

    The checked-in contract uses an ``entries`` array.  Accepting a numeric
    object map is useful for consumers that materialise JSON from a GitHub
    issue snapshot, but both forms are normalised and validated identically.
    No report path is implied by this structure: barrier entries bind to the
    existing behavioral evidence IDs instead of creating new reports.
    """

    if isinstance(value, list):
        raw_entries = value
    elif isinstance(value, dict) and isinstance(value.get("entries"), list):
        raw_entries = value["entries"]
    elif isinstance(value, dict) and value and all(str(key).isdigit() for key in value):
        raw_entries = []
        for key, item in value.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("issueNumber", int(key))
                raw_entries.append(entry)
            else:
                raw_entries.append({"issueNumber": int(key), "evidenceIds": item})
    elif isinstance(value, dict) and value:
        # Canonical recovery form: evidence ID -> {role, issueNumbers}.  The
        # map is projected into one entry per covered issue so the remaining
        # checks can enforce complete issue coverage and owner binding.
        projected: dict[int, set[str]] = {}
        for evidence_id, item in value.items():
            if not isinstance(evidence_id, str) or not evidence_id:
                findings.append(f"{label} evidence keys must be non-empty Evidence IDs")
                continue
            if not isinstance(item, dict):
                findings.append(f"{label}[{evidence_id}] must be an object")
                continue
            if not isinstance(item.get("role"), str) or not item["role"]:
                findings.append(f"{label}[{evidence_id}].role must be a non-empty string")
            issue_numbers = item.get("issueNumbers")
            if (
                not isinstance(issue_numbers, list)
                or not issue_numbers
                or not all(isinstance(issue, int) and not isinstance(issue, bool) for issue in issue_numbers)
                or len(issue_numbers) != len(set(issue_numbers))
            ):
                findings.append(f"{label}[{evidence_id}].issueNumbers must be a unique non-empty integer array")
                continue
            for issue in issue_numbers:
                projected.setdefault(issue, set()).add(evidence_id)
        raw_entries = [
            {"issueNumber": issue, "evidenceIds": sorted(evidence_ids), "_representation": "evidence-id-map"}
            for issue, evidence_ids in sorted(projected.items())
        ]
    elif isinstance(value, dict) and value and all(isinstance(item, dict) for item in value.values()):
        # The checked-in authority is keyed by owner Evidence ID.  Normalize
        # it into one entry per covered issue so the rest of the validator can
        # still enforce complete barrier coverage without inventing reports.
        by_issue: dict[int, list[str]] = {}
        for evidence_id, item in value.items():
            issue_numbers = item.get("issueNumbers") if isinstance(item, dict) else None
            if not isinstance(issue_numbers, list):
                continue
            for issue in issue_numbers:
                if isinstance(issue, int) and not isinstance(issue, bool):
                    by_issue.setdefault(issue, []).append(str(evidence_id))
        raw_entries = [
            {"issueNumber": issue, "evidenceIds": sorted(evidence_ids)}
            for issue, evidence_ids in sorted(by_issue.items())
        ]
    else:
        findings.append(f"{label} must be an entries array or numeric issue map")
        return []

    entries: list[dict[str, Any]] = []
    seen_issues: set[int] = set()
    for index, item in enumerate(raw_entries):
        entry_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            findings.append(f"{entry_label} must be an object")
            continue
        issue = item.get("issueNumber")
        if not isinstance(issue, int) or isinstance(issue, bool):
            findings.append(f"{entry_label}.issueNumber must be an integer")
        elif issue in seen_issues:
            findings.append(f"{entry_label}.issueNumber is duplicated: {issue}")
        else:
            seen_issues.add(issue)
        evidence_ids = item.get("evidenceIds")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(value, str) and value for value in evidence_ids)
        ):
            findings.append(f"{entry_label}.evidenceIds must be a non-empty string array")
        else:
            _require_unique(evidence_ids, f"{entry_label}.evidenceIds", findings)
        for field in ("reportIds", "assertionIds"):
            if field not in item:
                continue
            values = item.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                findings.append(f"{entry_label}.{field} must be a string array")
            else:
                _require_unique(values, f"{entry_label}.{field}", findings)
        entries.append(item)
    return entries


def _validate_barrier_coverage(
    contract: dict[str, Any],
    default_by_issue: dict[int, dict[str, Any]],
    behavioral: dict[str, Any] | None,
    findings: list[str],
) -> None:
    """Require every release blocker to bind to existing #88/#105 evidence.

    ``barrierCoverage`` is intentionally not part of ``defaultEvidence`` and
    does not add an Evidence ID.  This prevents a builder from satisfying a
    new release blocker merely by copying a generic report under a new name.
    """

    label = "barrierCoverage"
    entries = _normalise_barrier_entries(contract.get(label), label, findings)
    by_issue = {
        item.get("issueNumber"): item
        for item in entries
        if isinstance(item.get("issueNumber"), int) and not isinstance(item.get("issueNumber"), bool)
    }
    if set(by_issue) != set(BARRIER_ISSUES):
        findings.append(f"{label} must cover exactly {list(BARRIER_ISSUES)}")

    owner_ids = {
        default_by_issue.get(issue, {}).get("evidenceId")
        for issue in (88, 105)
        if isinstance(default_by_issue.get(issue, {}).get("evidenceId"), str)
    }
    if len(owner_ids) != 2:
        findings.append("barrierCoverage requires behavioral evidence owners for issues #88 and #105")
    raw_coverage = contract.get(label)
    if isinstance(raw_coverage, dict) and "entries" not in raw_coverage and not all(str(key).isdigit() for key in raw_coverage):
        owner_by_issue = {
            default_by_issue.get(issue, {}).get("evidenceId"): issue
            for issue in (88, 105)
            if isinstance(default_by_issue.get(issue, {}).get("evidenceId"), str)
        }
        if set(raw_coverage) != set(owner_by_issue):
            findings.append(f"{label} evidence keys must be exactly the #88 and #105 Evidence IDs")
        expected_roles = {88: "integrity-report", 105: "final-release-report"}
        expected_issue_lists = {88: list(RELEASE_BLOCKER_ISSUES), 105: list(BARRIER_ISSUES)}
        for evidence_id, owner_issue in owner_by_issue.items():
            item = raw_coverage.get(evidence_id)
            if not isinstance(item, dict):
                continue
            if item.get("role") != expected_roles[owner_issue]:
                findings.append(f"{label}[{evidence_id}].role must be {expected_roles[owner_issue]}")
            if item.get("issueNumbers") != expected_issue_lists[owner_issue]:
                findings.append(f"{label}[{evidence_id}].issueNumbers must be exactly {expected_issue_lists[owner_issue]}")
    declared_report_ids: set[str] = set()
    if isinstance(behavioral, dict):
        for requirement in behavioral.get("requirements", []):
            if not isinstance(requirement, dict) or requirement.get("ownerIssue") not in BEHAVIORAL_ISSUES:
                continue
            for report in requirement.get("reports", []):
                if isinstance(report, dict) and isinstance(report.get("reportId"), str):
                    declared_report_ids.add(report["reportId"])

    for issue, entry in sorted(by_issue.items()):
        evidence_ids = entry.get("evidenceIds")
        if isinstance(evidence_ids, list):
            # The parent #87 is owned by the final release report (#105),
            # while each #108-#113 blocker is jointly covered by #88 and
            # #105.  This is the canonical evidence-keyed map contract.
            if issue == PARENT_ISSUE:
                expected_evidence = {default_by_issue.get(105, {}).get("evidenceId")}
            else:
                expected_evidence = owner_ids
            expected_evidence.discard(None)
            if set(evidence_ids) != expected_evidence:
                findings.append(f"{label}[{issue}].evidenceIds must be exactly the declared owner Evidence IDs: {sorted(expected_evidence)}")
        report_ids = entry.get("reportIds", [])
        if isinstance(report_ids, list):
            for report_id in report_ids:
                if report_id not in declared_report_ids:
                    findings.append(f"{label}[{issue}] references an undeclared behavioral report: {report_id}")
                if isinstance(report_id, str) and any(report_id.startswith(f"issue-{barrier}.") for barrier in BARRIER_ISSUES):
                    findings.append(f"{label}[{issue}] must not create a report for a barrier issue: {report_id}")


def _validate_scope(contract: dict[str, Any], findings: list[str]) -> tuple[list[int], list[str], list[str]]:
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        findings.append("scope must be an object")
        return [], [], []
    issue_numbers = scope.get("issueNumbers")
    evidence_ids = scope.get("requiredEvidenceIds")
    requirement_ids = scope.get("requiredRequirementIds")
    if issue_numbers != list(REPORT_ISSUES):
        findings.append(f"scope.issueNumbers must be exactly report issues {list(REPORT_ISSUES)}")
        issue_numbers = []
    if not isinstance(evidence_ids, list) or not all(isinstance(item, str) and item for item in evidence_ids):
        findings.append("scope.requiredEvidenceIds must be a non-empty string array")
        evidence_ids = []
    if not isinstance(requirement_ids, list) or not all(isinstance(item, str) and item for item in requirement_ids):
        findings.append("scope.requiredRequirementIds must be a non-empty string array")
        requirement_ids = []
    _require_unique(evidence_ids, "scope.requiredEvidenceIds", findings)
    _require_unique(requirement_ids, "scope.requiredRequirementIds", findings)
    return list(issue_numbers or []), list(evidence_ids), list(requirement_ids)


def _validate_target_scope(contract: dict[str, Any], findings: list[str]) -> None:
    """Validate the live recovery target projection outside report scope."""

    if contract.get("targetIssueNumbers") != list(TARGET_ISSUES):
        findings.append(f"targetIssueNumbers must be exactly {list(TARGET_ISSUES)}")
    if contract.get("barrierIssueNumbers") != list(BARRIER_ISSUES):
        findings.append(f"barrierIssueNumbers must be exactly {list(BARRIER_ISSUES)}")


def _validate_default_evidence(
    contract: dict[str, Any],
    issue_numbers: list[int],
    evidence_ids: list[str],
    requirement_ids: list[str],
    findings: list[str],
) -> dict[int, dict[str, Any]]:
    entries = contract.get("defaultEvidence")
    if not isinstance(entries, list):
        findings.append("defaultEvidence must be an array")
        return {}
    by_issue: dict[int, dict[str, Any]] = {}
    seen_evidence: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"defaultEvidence[{index}]"
        if not isinstance(entry, dict):
            findings.append(f"{label} must be an object")
            continue
        evidence_id = entry.get("evidenceId")
        numbers = entry.get("issueNumbers")
        reqs = entry.get("requirementIds")
        if not isinstance(evidence_id, str) or not evidence_id:
            findings.append(f"{label}.evidenceId must be non-empty")
        elif evidence_id in seen_evidence:
            findings.append(f"duplicate evidenceId: {evidence_id}")
        else:
            seen_evidence.add(evidence_id)
        if not isinstance(numbers, list) or len(numbers) != 1 or not isinstance(numbers[0], int):
            findings.append(f"{label}.issueNumbers must contain exactly one issue number")
            issue = None
        else:
            issue = numbers[0]
            if issue not in REPORT_ISSUES:
                findings.append(f"{label}.issueNumbers contains out-of-scope issue {issue}")
            elif issue in by_issue:
                findings.append(f"duplicate defaultEvidence issue number: {issue}")
            else:
                by_issue[issue] = entry
        if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
            findings.append(f"{label}.evidenceId is not in scope.requiredEvidenceIds: {evidence_id}")
        if not isinstance(reqs, list) or not reqs:
            findings.append(f"{label}.requirementIds must be a non-empty array")
        else:
            _require_unique(reqs, f"{label}.requirementIds", findings)
            if any(item not in requirement_ids for item in reqs):
                findings.append(f"{label}.requirementIds contains an out-of-scope requirement")
        command = entry.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            findings.append(f"{label}.command must be a non-empty string array")
        _validate_paths(entry.get("inputs"), f"{label}.inputs", findings, require_exists=True)
        outputs = entry.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            findings.append(f"{label}.outputs must be a non-empty array")
            continue
        output_paths: list[str] = []
        producer_outputs: list[dict[str, Any]] = []
        for output_index, output in enumerate(outputs):
            output_label = f"{label}.outputs[{output_index}]"
            if not isinstance(output, dict):
                findings.append(f"{output_label} must be an object")
                continue
            path = output.get("path")
            if not _is_relative_repository_path(path):
                findings.append(f"{output_label}.path is not a safe bundle-relative path")
            elif path in output_paths:
                findings.append(f"{label}.outputs contains duplicate bundle path: {path}")
            elif isinstance(path, str):
                output_paths.append(path)
            source_path = output.get("sourcePath")
            source_directory = output.get("sourceDirectory")
            if source_path is None and source_directory is None:
                findings.append(f"{output_label} must bind sourcePath or sourceDirectory")
            for source_label, source_value in (("sourcePath", source_path), ("sourceDirectory", source_directory)):
                if source_value is not None:
                    if not _is_relative_repository_path(source_value):
                        findings.append(f"{output_label}.{source_label} is not a safe repository-relative path")
            role = output.get("role")
            if role == PRODUCER_ROLE and output.get("producerReport") is True:
                producer_outputs.append(output)
        if len(producer_outputs) != 1:
            findings.append(f"{label} must declare exactly one producer-report output; found {len(producer_outputs)}")
    if set(by_issue) != set(REPORT_ISSUES):
        findings.append(f"defaultEvidence must cover exactly report issues {list(REPORT_ISSUES)}")
    if set(seen_evidence) != set(evidence_ids):
        findings.append("defaultEvidence evidenceIds do not exactly match scope.requiredEvidenceIds")
    return by_issue


def _validate_report_declaration(
    report: Any,
    issue: int,
    default_entry: dict[str, Any],
    policy: dict[str, Any],
    findings: list[str],
) -> None:
    label = f"behavioralReportContract.requirements[{issue}].reports"
    if not isinstance(report, dict):
        findings.append(f"{label} contains a non-object report")
        return
    required_fields = policy.get("requiredReportDeclarationFields")
    if not isinstance(required_fields, list) or not required_fields:
        required_fields = ["reportId", "path", "bundlePath", "outputRole", "reportKind", "schema", "schemaVersion"]
    for field in required_fields:
        _require_string(report.get(field), f"{label}.{field}", findings)
    strict_issue = issue in BEHAVIORAL_ISSUES
    report_id = report.get("reportId")
    if strict_issue and isinstance(report_id, str) and not report_id.startswith(f"issue-{issue}."):
        findings.append(f"{label}.reportId is not issue-specific: {report_id}")
    path = report.get("path")
    if isinstance(path, str) and not _is_relative_repository_path(path):
        findings.append(f"{label} path is not repository-relative: {path}")
    elif strict_issue and isinstance(path, str) and not (
        path.startswith(f"e2e/.run/qualification-issue-{issue}/")
        or path == f"e2e/.run/qualification-issue-{issue}.json"
    ):
        findings.append(f"{label} path is not an issue-specific report path: {path}")
    bundle_path = report.get("bundlePath")
    if isinstance(bundle_path, str) and not _is_relative_repository_path(bundle_path):
        findings.append(f"{label} bundlePath is not repository-relative: {bundle_path}")
    elif strict_issue and isinstance(bundle_path, str) and not bundle_path.startswith(f"artifacts/{issue}/"):
        findings.append(f"{label} bundlePath is not an issue-specific artifact path: {bundle_path}")
    report_kind = report.get("reportKind")
    if strict_issue and isinstance(report_kind, str) and report_kind.casefold() in GENERIC_REPORT_KINDS:
        findings.append(f"{label} reportKind is generic: {report_kind}")
    output_role = report.get("outputRole")
    snapshot_roles = policy.get("sourceSnapshotOutputRoles")
    snapshot_role_names = {
        item.casefold()
        for item in snapshot_roles
        if isinstance(item, str)
    } if isinstance(snapshot_roles, list) else set()
    if strict_issue and isinstance(output_role, str) and output_role.casefold() in snapshot_role_names:
        findings.append(f"{label} outputRole is a source snapshot role: {output_role}")
    producer = report.get("producerReport") is True or report.get("outputRole") == PRODUCER_ROLE
    if producer:
        if report.get("outputRole") != PRODUCER_ROLE or report.get("producerReport") is not True:
            findings.append(f"{label} producer report must explicitly declare outputRole and producerReport")
        if report.get("schema") != PRODUCER_SCHEMA or report.get("schemaVersion") != PRODUCER_VERSION:
            findings.append(f"{label} producer report schema/version is invalid")
    elif strict_issue and (output_role == PRODUCER_ROLE or report.get("producerReport") is True):
        findings.append(f"{label} non-producer report has producer role")

    outputs = [output for output in default_entry.get("outputs", []) if isinstance(output, dict)]
    if strict_issue:
        exact_matches = [
            output
            for output in outputs
            if output.get("sourcePath") == path
            and output.get("path") == bundle_path
            and output.get("role") == output_role
        ]
        path_matches = [
            output
            for output in outputs
            if output.get("sourcePath") == path and output.get("path") == bundle_path
        ]
        # Existing contracts use a descriptive declaration role such as
        # ``status-contract-report`` while the copied output is labelled
        # ``status-report``.  Path/source binding remains authoritative; a
        # non-snapshot role alias is accepted when it resolves uniquely.
        compatible_matches = exact_matches or (
            path_matches
            if len(path_matches) == 1
            and output_role != PRODUCER_ROLE
            and not (
                isinstance(output_role, str)
                and output_role.casefold() in snapshot_role_names
            )
            else []
        )
        if len(compatible_matches) != 1:
            findings.append(f"{label} is not bound to exactly one defaultEvidence output: {report_id}")
    else:
        output_paths = {
            output.get("sourcePath")
            for output in outputs
            if isinstance(output.get("sourcePath"), str)
        }
        if path not in output_paths:
            findings.append(f"{label} path is not bound by defaultEvidence: {path}")


def _validate_issue_specific_requirement(
    issue: int,
    requirement: dict[str, Any],
    default_entry: dict[str, Any] | None,
    behavioral: dict[str, Any],
    seen_report_ids: dict[str, int],
    seen_report_paths: dict[str, int],
    seen_bundle_paths: dict[str, int],
    findings: list[str],
) -> None:
    """Validate the non-reusable evidence contract for Issues #91-#105."""

    label = f"behavioral requirement {issue}"
    policies = behavioral.get("policies")
    if not isinstance(policies, dict):
        return
    if default_entry is None:
        findings.append(f"{label} has no defaultEvidence mapping")
        return

    requirement_id = requirement.get("requirementId")
    evidence_id = requirement.get("evidenceId")
    if default_entry.get("evidenceId") != evidence_id:
        findings.append(f"{label} evidenceId is not bound to defaultEvidence")
    default_requirement_ids = default_entry.get("requirementIds")
    if not isinstance(default_requirement_ids, list) or default_requirement_ids != [requirement_id]:
        findings.append(f"{label} defaultEvidence requirementIds are not an exact mapping")

    producer = requirement.get("producer")
    if not isinstance(producer, dict):
        return
    runner = producer.get("runner")
    expected_runner = f"tools/qualification_issue{issue}.py"
    # #88 and #89 intentionally use the independent evidence-integrity and
    # mutation-campaign runners.  They are still issue-specific because their
    # commands and output roots are bound below; only the generic suite is
    # forbidden here.
    if not _is_relative_repository_path(runner) or not _resolve(Path(runner)).is_file():
        findings.append(f"{label} producer runner is missing: {runner}")
    elif runner in GENERIC_QUALIFICATION_RUNNERS:
        findings.append(f"{label} producer command reuses a generic qualification suite")
    command = producer.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        return
    if command != default_entry.get("command"):
        findings.append(f"{label} producer command does not exactly match defaultEvidence.command")
    if (isinstance(runner, str) and runner in GENERIC_QUALIFICATION_RUNNERS) or any(item in GENERIC_QUALIFICATION_RUNNERS for item in command):
        findings.append(f"{label} producer command reuses a generic qualification suite")
    if len(command) < 2 or command[0] != "python" or command[1] != runner:
        findings.append(f"{label} producer command must invoke its declared runner: {runner}")
    if not any(f"e2e/.run/qualification-issue-{issue}" in item for item in command):
        findings.append(f"{label} producer command must bind its issue-specific output directory")

    expected_evaluator = behavioral.get("requiredEvaluator")
    declared_evaluator = requirement.get("requiredEvaluator")
    if not isinstance(declared_evaluator, dict):
        findings.append(f"{label} must declare requiredEvaluator")
    elif declared_evaluator != expected_evaluator:
        findings.append(f"{label} requiredEvaluator does not match behavioralReportContract.requiredEvaluator")

    waiver_policy = requirement.get("waiverPolicy")
    if not isinstance(waiver_policy, dict) or waiver_policy.get("allowed") is not False:
        findings.append(f"{label} waiver policy must be explicitly fail-closed")

    assertions = requirement.get("requiredAssertionIds")
    if not isinstance(assertions, list) or not assertions or not all(isinstance(item, str) and item for item in assertions):
        findings.append(f"{label} requiredAssertionIds must be a non-empty string array")
    else:
        _require_unique(assertions, f"{label}.requiredAssertionIds", findings)
        if any(item in GENERIC_PASS_ASSERTIONS for item in assertions):
            findings.append(f"{label} contains a generic pass assertion")

    cases = requirement.get("requiredCases")
    if not isinstance(cases, list) or not cases:
        findings.append(f"{label} requiredCases must be a non-empty array")
    else:
        case_ids: list[Any] = []
        for index, case in enumerate(cases):
            case_label = f"{label}.requiredCases[{index}]"
            if not isinstance(case, dict):
                findings.append(f"{case_label} must be an object")
                continue
            case_id = case.get("caseId")
            classification = case.get("classification")
            case_ids.append(case_id)
            if not isinstance(case_id, str) or not case_id:
                findings.append(f"{case_label}.caseId must be non-empty")
            if classification not in VALID_CLASSIFICATIONS:
                findings.append(f"{case_label}.classification is invalid: {classification!r}")
        _require_unique(case_ids, f"{label}.requiredCases", findings)

    classes = requirement.get("requiredCaseClasses")
    if not isinstance(classes, list) or not classes or not all(isinstance(item, str) and item for item in classes):
        findings.append(f"{label} requiredCaseClasses must be a non-empty string array")
    else:
        _require_unique(classes, f"{label}.requiredCaseClasses", findings)
        if any(item not in VALID_CLASSIFICATIONS for item in classes):
            findings.append(f"{label} has invalid requiredCaseClasses")

    if requirement.get("minimumIndependenceGrade") not in VALID_GRADES:
        findings.append(f"{label} has invalid independence grade")

    reports = requirement.get("reports")
    if not isinstance(reports, list) or not reports:
        findings.append(f"{label} must declare issue-specific reports")
        return
    report_ids = [report.get("reportId") for report in reports if isinstance(report, dict)]
    report_paths = [report.get("path") for report in reports if isinstance(report, dict)]
    bundle_paths = [report.get("bundlePath") for report in reports if isinstance(report, dict)]
    report_roles = [report.get("outputRole") for report in reports if isinstance(report, dict)]
    _require_unique(report_ids, f"{label}.reports reportId", findings)
    _require_unique(report_paths, f"{label}.reports path", findings)
    _require_unique(bundle_paths, f"{label}.reports bundlePath", findings)
    _require_unique(report_roles, f"{label}.reports outputRole", findings)

    producer_reports = [
        report
        for report in reports
        if isinstance(report, dict)
        and (report.get("producerReport") is True or report.get("outputRole") == PRODUCER_ROLE)
    ]
    non_producer_reports = [
        report
        for report in reports
        if isinstance(report, dict)
        and report.get("producerReport") is not True
        and report.get("outputRole") != PRODUCER_ROLE
    ]
    if len(producer_reports) != 1:
        findings.append(f"{label} must declare exactly one producer report")
    if not non_producer_reports:
        findings.append(f"{label} must declare at least one semantic behavioral report")

    outputs = default_entry.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        findings.append(f"{label} defaultEvidence.outputs must be non-empty")
        return
    snapshot_roles = policies.get("sourceSnapshotOutputRoles")
    snapshot_role_names = {
        item.casefold()
        for item in snapshot_roles
        if isinstance(item, str)
    } if isinstance(snapshot_roles, list) else set()
    output_bindings: list[tuple[Any, Any, Any]] = []
    non_report_roles = {"producer-input", "producer-case-artifact", "campaign-artifacts"}
    for index, output in enumerate(outputs):
        output_label = f"{label}.defaultEvidence.outputs[{index}]"
        if not isinstance(output, dict):
            findings.append(f"{output_label} must be an object")
            continue
        role = output.get("role")
        if isinstance(role, str) and role.casefold() in snapshot_role_names:
            # Source snapshots are allowed as producer inputs, but cannot
            # satisfy a behavioral report declaration.
            continue
        source_path = output.get("sourcePath")
        bundle_path = output.get("path")
        if role in non_report_roles or output.get("sourceDirectory") is not None:
            continue
        if output.get("sourceDirectory") is not None or not isinstance(source_path, str):
            findings.append(f"{output_label} must bind a report sourcePath")
        output_bindings.append((source_path, bundle_path, role))

    report_bindings: list[tuple[Any, Any, Any]] = []
    for report in reports:
        _validate_report_declaration(report, issue, default_entry, policies, findings)
        if isinstance(report, dict):
            report_id = report.get("reportId")
            report_path = report.get("path")
            bundle_path = report.get("bundlePath")
            role = report.get("outputRole")
            report_bindings.append((report_path, bundle_path, role))
            for value, seen, value_label in (
                (report_id, seen_report_ids, "reportId"),
                (report_path, seen_report_paths, "path"),
                (bundle_path, seen_bundle_paths, "bundlePath"),
            ):
                if not isinstance(value, str) or not value:
                    continue
                previous_issue = seen.get(value)
                if previous_issue is not None:
                    findings.append(f"{label}.reports {value_label} is duplicated with issue {previous_issue}: {value}")
                else:
                    seen[value] = issue

    def binding_key(binding: tuple[Any, Any, Any]) -> tuple[str, str]:
        # The source and bundle paths are the stable traceability edge.  The
        # output role is descriptive and may use an existing contract alias.
        return tuple(
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            for item in binding[:2]
        )

    output_set = {binding_key(binding) for binding in output_bindings}
    report_set = {binding_key(binding) for binding in report_bindings}
    for binding in sorted(report_set - output_set, key=str):
        findings.append(f"{label} has an orphan report declaration: {binding[1]}")
    for binding in sorted(output_set - report_set, key=str):
        findings.append(f"{label} has an orphan defaultEvidence output: {binding[1]}")
    if output_set != report_set:
        findings.append(f"{label} report declarations and defaultEvidence outputs are not one-to-one")


def _validate_behavioral_contract(
    contract: dict[str, Any],
    default_by_issue: dict[int, dict[str, Any]],
    scope_requirement_ids: list[str],
    findings: list[str],
) -> None:
    behavioral = contract.get("behavioralReportContract")
    if not isinstance(behavioral, dict):
        findings.append("behavioralReportContract must be an object")
        return
    if behavioral.get("schema") != "fdir/qualification-behavioral-report-contract":
        findings.append("behavioralReportContract.schema is invalid")
    if behavioral.get("version") != REPORT_VERSION:
        findings.append(f"behavioralReportContract.version must be {REPORT_VERSION}")
    evaluator = behavioral.get("requiredEvaluator")
    if not isinstance(evaluator, dict):
        findings.append("behavioralReportContract.requiredEvaluator must be an object")
    else:
        if not isinstance(evaluator.get("evaluatorId"), str) or not evaluator["evaluatorId"]:
            findings.append("requiredEvaluator.evaluatorId must be non-empty")
        evaluator_path = evaluator.get("path")
        if not _is_relative_repository_path(evaluator_path):
            findings.append("requiredEvaluator.path is not a safe repository path")
        elif not _resolve(Path(evaluator_path)).is_file():
            findings.append(f"requiredEvaluator.path does not exist: {evaluator_path}")
        if evaluator.get("version") != "1.0.0":
            findings.append("requiredEvaluator.version must be 1.0.0")
    policies = behavioral.get("policies")
    if not isinstance(policies, dict):
        findings.append("behavioralReportContract.policies must be an object")
    else:
        for key in (
            "requiredReportFields",
            "requiredAssertionFields",
            "requiredCaseFields",
            "requiredIndependenceFields",
            "requiredProducerReportFields",
            "requiredProducerAssertionFields",
            "requiredProducerCaseFields",
            "behavioralIssueNumbers",
            "requiredBehavioralRequirementFields",
            "requiredReportDeclarationFields",
            "behavioralOutputPolicy",
            "requiredOutputRoles",
            "sourceSnapshotOutputRoles",
            "genericPassAssertions",
            "waiverPolicy",
        ):
            if key not in policies:
                findings.append(f"behavioralReportContract.policies.{key} is missing")
        if policies.get("behavioralIssueNumbers") != list(BEHAVIORAL_ISSUES):
            findings.append(f"behavioralReportContract.policies.behavioralIssueNumbers must be exactly {list(BEHAVIORAL_ISSUES)}")
        expected_requirement_fields = [
            "evidenceId",
            "requirementId",
            "producer",
            "requiredAssertionIds",
            "requiredCases",
            "requiredCaseClasses",
            "minimumIndependenceGrade",
            "requiredEvaluator",
            "reports",
        ]
        if policies.get("requiredBehavioralRequirementFields") != expected_requirement_fields:
            findings.append("behavioralReportContract.policies.requiredBehavioralRequirementFields is invalid")
        expected_report_fields = [
            "reportId",
            "path",
            "bundlePath",
            "outputRole",
            "reportKind",
            "schema",
            "schemaVersion",
        ]
        if policies.get("requiredReportDeclarationFields") != expected_report_fields:
            findings.append("behavioralReportContract.policies.requiredReportDeclarationFields is invalid")
        if policies.get("behavioralOutputPolicy") != "issue-specific-behavioral-only":
            findings.append("behavioralReportContract.policies.behavioralOutputPolicy is invalid")
        if policies.get("producerReportSchema") != PRODUCER_SCHEMA:
            findings.append("behavioralReportContract.policies.producerReportSchema is invalid")
        if policies.get("producerReportVersion") != PRODUCER_VERSION:
            findings.append("behavioralReportContract.policies.producerReportVersion is invalid")
        snapshot_roles = policies.get("sourceSnapshotOutputRoles")
        if not isinstance(snapshot_roles, list) or not snapshot_roles or not all(isinstance(item, str) and item for item in snapshot_roles):
            findings.append("behavioralReportContract.policies.sourceSnapshotOutputRoles must be a non-empty string array")
        else:
            _require_unique(snapshot_roles, "behavioralReportContract.policies.sourceSnapshotOutputRoles", findings)
        if policies.get("genericPassAssertions") != list(GENERIC_PASS_ASSERTIONS):
            declared = policies.get("genericPassAssertions")
            if not isinstance(declared, list) or set(declared) != set(GENERIC_PASS_ASSERTIONS):
                findings.append("generic pass assertions must be the closed fail-closed set")
        waiver_policy = policies.get("waiverPolicy")
        if not isinstance(waiver_policy, dict) or waiver_policy.get("allowed") is not False:
            findings.append("behavioralReportContract waiver policy must forbid waivers")
    requirements = behavioral.get("requirements")
    if not isinstance(requirements, list):
        findings.append("behavioralReportContract.requirements must be an array")
        return
    by_issue: dict[int, dict[str, Any]] = {}
    seen_requirement_ids: set[str] = set()
    seen_report_ids: dict[str, int] = {}
    seen_report_paths: dict[str, int] = {}
    seen_bundle_paths: dict[str, int] = {}
    for requirement in requirements:
        if not isinstance(requirement, dict):
            findings.append("behavioralReportContract.requirements contains a non-object")
            continue
        issue = requirement.get("ownerIssue")
        if not isinstance(issue, int) or issue not in REPORT_ISSUES:
            findings.append(f"behavioral requirement has invalid ownerIssue: {issue!r}")
            continue
        if issue in by_issue:
            findings.append(f"duplicate behavioral ownerIssue: {issue}")
        by_issue[issue] = requirement
        evidence_id = requirement.get("evidenceId")
        req_id = requirement.get("requirementId")
        if not isinstance(evidence_id, str) or evidence_id != default_by_issue.get(issue, {}).get("evidenceId"):
            findings.append(f"behavioral requirement {issue} evidenceId does not match defaultEvidence")
        if not isinstance(req_id, str) or req_id in seen_requirement_ids or req_id not in scope_requirement_ids:
            findings.append(f"behavioral requirement {issue} has an invalid or duplicate requirementId")
        elif isinstance(req_id, str):
            seen_requirement_ids.add(req_id)
        producer = requirement.get("producer")
        if not isinstance(producer, dict):
            findings.append(f"behavioral requirement {issue}.producer must be an object")
        else:
            runner = producer.get("runner")
            if not _is_relative_repository_path(runner) or not _resolve(Path(runner)).is_file():
                findings.append(f"behavioral requirement {issue} producer runner is missing: {runner}")
            command = producer.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                findings.append(f"behavioral requirement {issue} producer command is invalid")
        if requirement.get("minimumIndependenceGrade") not in VALID_GRADES:
            findings.append(f"behavioral requirement {issue} has invalid independence grade")
        required_assertions = requirement.get("requiredAssertionIds")
        if not isinstance(required_assertions, list) or not required_assertions:
            findings.append(f"behavioral requirement {issue} must declare requiredAssertionIds")
        else:
            _require_unique(required_assertions, f"behavioral requirement {issue}.requiredAssertionIds", findings)
            if any(item in GENERIC_PASS_ASSERTIONS for item in required_assertions):
                findings.append(f"behavioral requirement {issue} contains a generic pass assertion")
        cases = requirement.get("requiredCases")
        case_ids: list[Any] = []
        if not isinstance(cases, list) or not cases:
            findings.append(f"behavioral requirement {issue} must declare requiredCases")
        else:
            for case in cases:
                if not isinstance(case, dict):
                    findings.append(f"behavioral requirement {issue} has a non-object required case")
                    continue
                case_id = case.get("caseId")
                classification = case.get("classification")
                case_ids.append(case_id)
                if not isinstance(case_id, str) or not case_id:
                    findings.append(f"behavioral requirement {issue} has an invalid caseId")
                if classification not in VALID_CLASSIFICATIONS:
                    findings.append(f"behavioral requirement {issue} has invalid case classification: {classification!r}")
            _require_unique(case_ids, f"behavioral requirement {issue}.requiredCases", findings)
        classes = requirement.get("requiredCaseClasses")
        if not isinstance(classes, list) or not classes:
            findings.append(f"behavioral requirement {issue} must declare requiredCaseClasses")
        else:
            if any(item not in VALID_CLASSIFICATIONS for item in classes):
                findings.append(f"behavioral requirement {issue} has invalid requiredCaseClasses")
            # ``requiredCaseClasses`` is a semantic inventory.  A composite
            # classification such as ``positive-oracle`` may satisfy more
            # than one declared class, while differential/oracle coverage can
            # be supplied by an auxiliary report or caseInventory.  The
            # producer report validator is responsible for checking the
            # concrete cases; this contract validator only rejects unknown
            # class names and malformed case records.
        reports = requirement.get("reports")
        if not isinstance(reports, list) or not reports:
            findings.append(f"behavioral requirement {issue} must declare reports")
        else:
            report_ids = [report.get("reportId") for report in reports if isinstance(report, dict)]
            report_paths = [report.get("path") for report in reports if isinstance(report, dict)]
            _require_unique(report_ids, f"behavioral requirement {issue}.reports reportId", findings)
            _require_unique(report_paths, f"behavioral requirement {issue}.reports path", findings)
            for report in reports:
                _validate_report_declaration(report, issue, default_by_issue.get(issue, {}), requirement, findings)
            producer_reports = [report for report in reports if isinstance(report, dict) and (report.get("producerReport") is True or report.get("outputRole") == PRODUCER_ROLE)]
            if len(producer_reports) != 1:
                findings.append(f"behavioral requirement {issue} must declare exactly one producer report")
        case_inventory = requirement.get("caseInventory")
        if case_inventory is not None:
            if not isinstance(case_inventory, dict):
                findings.append(f"behavioral requirement {issue}.caseInventory must be an object")
            else:
                inventory_path = case_inventory.get("path")
                if not _is_relative_repository_path(inventory_path) or not _resolve(Path(inventory_path)).is_file():
                    findings.append(f"behavioral requirement {issue}.caseInventory path is missing: {inventory_path}")
        if issue in BEHAVIORAL_ISSUES:
            _validate_issue_specific_requirement(
                issue,
                requirement,
                default_by_issue.get(issue),
                behavioral,
                seen_report_ids,
                seen_report_paths,
                seen_bundle_paths,
                findings,
            )
    if set(by_issue).intersection(REPORT_ISSUES) != set(REPORT_ISSUES):
        findings.append(f"behavioral requirements must map every report issue in {list(REPORT_ISSUES)}")
    if set(default_by_issue).intersection(REPORT_ISSUES) != set(REPORT_ISSUES):
        findings.append(f"defaultEvidence must map every report issue in {list(REPORT_ISSUES)}")
    if set(by_issue) != set(REPORT_ISSUES):
        findings.append(f"behavioralReportContract.requirements must cover exactly report issues {list(REPORT_ISSUES)}")
    if set(seen_requirement_ids) != set(scope_requirement_ids):
        findings.append("behavioral requirement IDs do not exactly match scope.requiredRequirementIds")


def _validate_release_ci_policy(contract: dict[str, Any], findings: list[str]) -> None:
    ci_policy = contract.get("ciPolicy")
    if not isinstance(ci_policy, dict):
        findings.append("ciPolicy must be an object")
        return
    if ci_policy.get("allowedProviders") != ["github-actions"]:
        findings.append("ciPolicy.allowedProviders must be exactly ['github-actions']; local is diagnostic-only")
    if ci_policy.get("releaseStatus") != "completed":
        findings.append("ciPolicy.releaseStatus must be completed")
    pattern = ci_policy.get("githubActionsRunUrlPattern")
    if not isinstance(pattern, str) or "https://github.com/<repository>/actions/runs/<run-id>" != pattern:
        findings.append("ciPolicy.githubActionsRunUrlPattern is not the pinned GitHub Actions URL contract")


def validate_contract_document(contract: Any, *, root: Path = ROOT) -> list[str]:
    """Return structural findings for a parsed qualification contract."""

    findings: list[str] = []
    if not isinstance(contract, dict):
        return ["contract root must be an object"]
    for key in (
        "schema",
        "version",
        "repository",
        "evidenceSchema",
        "scope",
        "targetIssueNumbers",
        "barrierIssueNumbers",
        "defaultEvidence",
        "behavioralReportContract",
        "barrierCoverage",
        "ciPolicy",
    ):
        if key not in contract:
            findings.append(f"contract.{key} is missing")
    if contract.get("schema") != "fdir/qualification-contract":
        findings.append("contract.schema is invalid")
    if contract.get("version") != "1.0.0":
        findings.append("contract.version is not pinned to 1.0.0")
    if contract.get("repository") != "horiyamayoh/fdir":
        findings.append("contract.repository is invalid")
    if not _is_relative_repository_path(contract.get("evidenceSchema")):
        findings.append("contract.evidenceSchema is not a safe repository path")
    elif not _resolve(Path(contract["evidenceSchema"])).is_file():
        findings.append(f"contract.evidenceSchema does not exist: {contract['evidenceSchema']}")
    _validate_target_scope(contract, findings)
    issues, evidence_ids, requirement_ids = _validate_scope(contract, findings)
    default_by_issue = _validate_default_evidence(contract, issues, evidence_ids, requirement_ids, findings)
    _validate_behavioral_contract(contract, default_by_issue, requirement_ids, findings)
    _validate_barrier_coverage(contract, default_by_issue, contract.get("behavioralReportContract"), findings)
    _validate_release_ci_policy(contract, findings)
    return findings


def validate_contract(contract_path: Path = DEFAULT_CONTRACT_PATH) -> list[str]:
    """Read and validate the checked-in qualification contract."""

    resolved = _resolve(contract_path)
    return validate_contract_document(_read_json(resolved), root=ROOT)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT_PATH)
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit findings as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        findings = validate_contract(args.contract)
    except ContractValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"status": "failed" if findings else "passed", "findings": findings}, ensure_ascii=False, indent=2))
    elif findings:
        print("FAIL: qualification contract is structurally invalid", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
    else:
        print("PASS: qualification contract is structurally valid")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
