"""Fail-closed completion gate for the phase-2 GitHub issue contract.

This gate is deliberately stricter than the compatibility/release smoke gate.
It requires machine-readable evidence produced by the qualification commands;
exit status and file presence alone never satisfy a completion claim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from qualification_evidence import validate_source_feature_closure
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_source_feature_closure


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "machine" / "strict-completion-contract.json"


def _run(command: list[str]) -> tuple[int, str, str]:
    child_environment = os.environ.copy()
    child_environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, *command],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_environment,
        capture_output=True,
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _json_report(command: list[str], blockers: list[dict[str, str]]) -> dict[str, Any] | None:
    returncode, stdout, stderr = _run(command)
    if returncode != 0:
        blockers.append({"code": "COMMAND_FAILED", "command": "python " + " ".join(command), "detail": (stdout + stderr).strip()[-1000:]})
        return None
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        blockers.append({"code": "REPORT_NOT_JSON", "command": "python " + " ".join(command), "detail": str(exc)})
        return None
    if not isinstance(report, dict):
        blockers.append({"code": "REPORT_NOT_OBJECT", "command": "python " + " ".join(command), "detail": "report root is not an object"})
        return None
    return report


def _require(condition: bool, code: str, detail: str, blockers: list[dict[str, str]]) -> None:
    if not condition:
        blockers.append({"code": code, "detail": detail})


def _check_source_closure(report: dict[str, Any], label: str, blockers: list[dict[str, str]]) -> None:
    cases = list(report.get("cases", []))
    cases.extend(item for item in report.get("negativeChecks", []) if isinstance(item, dict))
    for case in cases:
        if not isinstance(case, dict):
            _require(False, "SOURCE_CLOSURE_CASE_MALFORMED", f"{label} contains a malformed case", blockers)
            continue
        document_path = case.get("documentPath") or case.get("output")
        if not isinstance(document_path, str) or not Path(document_path).is_file():
            _require(False, "SOURCE_CLOSURE_DOCUMENT_MISSING", f"{label}/{case.get('id', case.get('format', '<unknown>'))} does not identify the converted IR document", blockers)
            continue
        try:
            document = json.loads(Path(document_path).read_text(encoding="utf-8"))
            closure = validate_source_feature_closure(document, case)
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            _require(False, "SOURCE_CLOSURE_EXECUTION", f"{label}/{case.get('id', case.get('format', '<unknown>'))} closure execution failed: {exc}", blockers)
            continue
        _require(closure.get("status") == "passed", "SOURCE_CLOSURE_CONTENT", f"{label}/{case.get('id', case.get('format', '<unknown>'))} source occurrence closure failed: {json.dumps(closure.get('mismatches', []), ensure_ascii=False)}", blockers)


def run() -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    blockers: list[dict[str, str]] = []
    reports = contract["reports"]

    mutation = _json_report(["tools/mutation_qualification.py", "--json"], blockers)
    if mutation is not None:
        specification = reports["mutation"]
        _require(mutation.get("schema") == specification["schema"], "MUTATION_SCHEMA", "mutation report schema is not the strict contract schema", blockers)
        _require(isinstance(mutation.get("coverage"), dict), "MUTATION_COVERAGE_MISSING", "mutation report must identify covered mutation classes", blockers)
        covered = set(mutation.get("coverage", {}).keys()) if isinstance(mutation.get("coverage"), dict) else set()
        _require(set(specification["requiredMutationClasses"]).issubset(covered), "MUTATION_CLASSES_INCOMPLETE", "required mutation classes are missing: " + ", ".join(sorted(set(specification["requiredMutationClasses"]) - covered)), blockers)
        _require(mutation.get("survivors") == [], "MUTATION_SURVIVORS", "strict completion requires zero surviving mutations", blockers)
        for field in specification.get("requiredFields", []):
            _require(field in mutation, "MUTATION_FIELD_MISSING", f"mutation report lacks {field}", blockers)
        _require(isinstance(mutation.get("killed"), int) and isinstance(mutation.get("total"), int) and mutation.get("total", 0) > 0, "MUTATION_COUNTS", "mutation report killed/total counts are invalid", blockers)
        _require(mutation.get("killed") == mutation.get("total"), "MUTATION_SCORE", "mutation report is not fully killed", blockers)
        _require(len(mutation.get("survivors", [])) <= specification.get("maximumSurvivors", 0), "MUTATION_SURVIVOR_LIMIT", "mutation survivor limit exceeded", blockers)

    corpus = _json_report(["tools/independent_corpus.py", "--json"], blockers)
    if corpus is not None:
        specification = reports["independentCorpus"]
        _require(corpus.get("schema") == specification["schema"], "CORPUS_SCHEMA", "independent corpus report schema is not the strict contract schema", blockers)
        formats = {case.get("format") for case in corpus.get("cases", []) if isinstance(case, dict)}
        _require(formats == set(specification["requiredFormats"]), "CORPUS_FORMAT_MATRIX", "independent corpus does not cover exactly the required formats", blockers)
        case_classes = {case.get("caseClass") for case in corpus.get("cases", []) if isinstance(case, dict)} | {item.get("id") for item in corpus.get("negativeChecks", []) if isinstance(item, dict)}
        _require(set(specification["requiredCaseClasses"]).issubset(case_classes), "CORPUS_NEGATIVE_MATRIX", "independent corpus lacks required case classes: " + ", ".join(sorted(set(specification["requiredCaseClasses"]) - case_classes)), blockers)
        for case in corpus.get("cases", []):
            if isinstance(case, dict):
                for field in specification["requiredFieldsPerCase"]:
                    _require(field in case, "CORPUS_CASE_EVIDENCE", f"{case.get('id', '<unknown>')} lacks {field}", blockers)
                _require(isinstance(case.get("sourceFeatureIds"), list) and bool(case.get("sourceFeatureIds")), "CORPUS_SOURCE_EVIDENCE", f"{case.get('id', '<unknown>')} source inventory is empty", blockers)
                _require(case.get("queryParity", {}).get("status") == "passed", "CORPUS_QUERY_PARITY", f"{case.get('id', '<unknown>')} query parity is not passed", blockers)
                _require(isinstance(case.get("dispositions"), list) and isinstance(case.get("featureInventory"), list), "CORPUS_DISPOSITIONS", f"{case.get('id', '<unknown>')} disposition evidence is malformed", blockers)
        _check_source_closure(corpus, "independent-corpus", blockers)
        for negative in corpus.get("negativeChecks", []):
            if isinstance(negative, dict):
                _require(isinstance(negative.get("dispositions"), list) and isinstance(negative.get("featureInventory"), list), "CORPUS_NEGATIVE_EVIDENCE", f"{negative.get('id', '<unknown>')} negative evidence is malformed", blockers)
                _require(isinstance(negative.get("sourceFeatureIds"), list) and bool(negative.get("sourceFeatureIds")), "CORPUS_NEGATIVE_SOURCE_EVIDENCE", f"{negative.get('id', '<unknown>')} source inventory is empty", blockers)

    query = _json_report(["tools/query_qualification.py"], blockers)
    if query is not None:
        specification = reports["query"]
        _require(query.get("schema") == specification["schema"], "QUERY_SCHEMA", "query report schema is not the strict contract schema", blockers)
        _require(set(specification["requiredSources"]).issubset(set(query.get("sources", []))), "QUERY_SOURCES_INCOMPLETE", "query qualification must include examples, real-input E2E, and independent corpus", blockers)
        _require(query.get("unqueryableFacts") == [], "QUERY_UNQUERYABLE_FACTS", "strict completion forbids unqueryable authoritative facts", blockers)
        _require(query.get("parity", {}).get("status") == "passed", "QUERY_PARITY", "direct/index parity must be reported as passed", blockers)
        for field in specification.get("requiredFields", []):
            _require(field in query, "QUERY_FIELD_MISSING", f"query report lacks {field}", blockers)
        _require(isinstance(query.get("operations"), list) and query.get("operations"), "QUERY_OPERATIONS", "query report has no executed operations", blockers)

    real_input = _json_report(["tools/run_e2e.py", "--all", "--json"], blockers)
    if real_input is not None:
        specification = reports["realInput"]
        _require(real_input.get("schema") == specification["schema"], "E2E_SCHEMA", "real-input report schema is not the strict contract schema", blockers)
        formats = set(real_input.get("formats", []))
        _require(formats == set(specification["requiredFormats"]), "E2E_FORMAT_MATRIX", "real-input E2E does not cover exactly the required formats", blockers)
        for case in real_input.get("cases", []):
            if isinstance(case, dict):
                for field in specification["requiredFieldsPerCase"]:
                    _require(field in case, "E2E_CASE_EVIDENCE", f"{case.get('format', '<unknown>')} case lacks {field}", blockers)
                _require(case.get("queryParity", {}).get("status") == "passed", "E2E_QUERY_PARITY", f"{case.get('format', '<unknown>')} query parity is not passed", blockers)
                _require(isinstance(case.get("sourceFeatureIds"), list) and bool(case.get("sourceFeatureIds")), "E2E_SOURCE_EVIDENCE", f"{case.get('format', '<unknown>')} source evidence is empty", blockers)
                _require(isinstance(case.get("dispositions"), list) and isinstance(case.get("residuals"), list), "E2E_DISPOSITIONS", f"{case.get('format', '<unknown>')} disposition evidence is malformed", blockers)
        _check_source_closure(real_input, "real-input-e2e", blockers)

    issue_evidence = contract.get("issueEvidence", {})
    _require(set(issue_evidence) == {str(number) for number in contract["scope"]["phase2Issues"]}, "ISSUE_EVIDENCE_MATRIX", "strict contract must define evidence for every phase-2 issue", blockers)
    for issue_number, evidence_ids in issue_evidence.items():
        _require(isinstance(evidence_ids, list) and all(isinstance(item, str) and item for item in evidence_ids), "ISSUE_EVIDENCE_EMPTY", f"issue {issue_number} has empty evidence binding", blockers)
    _require(contract["closurePolicy"].get("closedStateIsNotEvidence") is True, "CLOSURE_POLICY", "closed issue state must never be sufficient evidence", blockers)

    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.0.0",
        "status": "passed" if not blockers else "blocked",
        "issues": contract["scope"]["phase2Issues"],
        "blockers": blockers,
        "reportsChecked": ["mutation", "independentCorpus", "query", "realInput"],
    }


def main() -> int:
    try:
        report = run()
    except Exception as exc:
        report = {"schema": "fdir/strict-completion-gate-report", "version": "1.0.0", "status": "blocked", "blockers": [{"code": "GATE_ERROR", "detail": f"{type(exc).__name__}: {exc}"}]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
