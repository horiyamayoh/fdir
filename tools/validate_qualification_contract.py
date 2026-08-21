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
EXPECTED_ISSUES = tuple(range(88, 106))
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VALID_GRADES = frozenset({"I0", "I1", "I2", "I3", "I4"})
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
PRODUCER_VERSION = "1.0.0"
PRODUCER_ROLE = "producer-report"


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
    if len(items) != len(set(items)):
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


def _validate_scope(contract: dict[str, Any], findings: list[str]) -> tuple[list[int], list[str], list[str]]:
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        findings.append("scope must be an object")
        return [], [], []
    issue_numbers = scope.get("issueNumbers")
    evidence_ids = scope.get("requiredEvidenceIds")
    requirement_ids = scope.get("requiredRequirementIds")
    if issue_numbers != list(EXPECTED_ISSUES):
        findings.append(f"scope.issueNumbers must be exactly {list(EXPECTED_ISSUES)}")
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
            if issue not in EXPECTED_ISSUES:
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
    if set(by_issue) != set(EXPECTED_ISSUES):
        findings.append(f"defaultEvidence must cover exactly {list(EXPECTED_ISSUES)}")
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
    for field in ("reportId", "path", "bundlePath", "outputRole", "reportKind", "schema", "schemaVersion"):
        _require_string(report.get(field), f"{label}.{field}", findings)
    path = report.get("path")
    if isinstance(path, str) and not _is_relative_repository_path(path):
        findings.append(f"{label} path is not repository-relative: {path}")
    output_paths = {
        output.get("sourcePath")
        for output in default_entry.get("outputs", [])
        if isinstance(output, dict) and isinstance(output.get("sourcePath"), str)
    }
    if path not in output_paths:
        findings.append(f"{label} path is not bound by defaultEvidence: {path}")
    producer = report.get("producerReport") is True or report.get("outputRole") == PRODUCER_ROLE
    if producer:
        if report.get("outputRole") != PRODUCER_ROLE or report.get("producerReport") is not True:
            findings.append(f"{label} producer report must explicitly declare outputRole and producerReport")
        if report.get("schema") != PRODUCER_SCHEMA or report.get("schemaVersion") != PRODUCER_VERSION:
            findings.append(f"{label} producer report schema/version is invalid")


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
        for key in ("requiredReportFields", "requiredAssertionFields", "requiredCaseFields", "requiredIndependenceFields", "requiredProducerReportFields", "requiredProducerAssertionFields", "requiredProducerCaseFields", "requiredOutputRoles", "sourceSnapshotOutputRoles", "genericPassAssertions", "waiverPolicy"):
            if key not in policies:
                findings.append(f"behavioralReportContract.policies.{key} is missing")
        if policies.get("producerReportSchema") != PRODUCER_SCHEMA:
            findings.append("behavioralReportContract.policies.producerReportSchema is invalid")
        if policies.get("producerReportVersion") != PRODUCER_VERSION:
            findings.append("behavioralReportContract.policies.producerReportVersion is invalid")
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
    for requirement in requirements:
        if not isinstance(requirement, dict):
            findings.append("behavioralReportContract.requirements contains a non-object")
            continue
        issue = requirement.get("ownerIssue")
        if not isinstance(issue, int) or issue not in EXPECTED_ISSUES:
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
    if set(by_issue) != set(EXPECTED_ISSUES):
        findings.append(f"behavioralReportContract.requirements must cover exactly {list(EXPECTED_ISSUES)}")
    if set(seen_requirement_ids) != set(scope_requirement_ids):
        findings.append("behavioral requirement IDs do not exactly match scope.requiredRequirementIds")


def validate_contract_document(contract: Any, *, root: Path = ROOT) -> list[str]:
    """Return structural findings for a parsed qualification contract."""

    findings: list[str] = []
    if not isinstance(contract, dict):
        return ["contract root must be an object"]
    for key in ("schema", "version", "repository", "evidenceSchema", "scope", "defaultEvidence", "behavioralReportContract"):
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
    issues, evidence_ids, requirement_ids = _validate_scope(contract, findings)
    default_by_issue = _validate_default_evidence(contract, issues, evidence_ids, requirement_ids, findings)
    _validate_behavioral_contract(contract, default_by_issue, requirement_ids, findings)
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
