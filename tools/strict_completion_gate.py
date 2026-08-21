"""Fail-closed completion gate for the phase-2 GitHub issue contract.

This gate is deliberately stricter than the compatibility/release smoke gate.
It requires machine-readable evidence produced by the qualification commands;
exit status and file presence alone never satisfy a completion claim.
"""

from __future__ import annotations

import argparse
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


def _bundle_issue_reports(manifest: Path) -> list[dict[str, Any]]:
    """Materialize one strict-report entry for every recovery issue.

    A list of issue numbers is not sufficient evidence for #113: reviewers
    need to see which Evidence report, outputs, assertions, and cases were
    actually resolved for each issue.  This helper is intentionally tolerant
    of an invalid candidate so a blocked report still explains missing
    material instead of hiding it behind a top-level validator error.
    """

    issue_numbers = list(range(88, 106))
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
            if issue in value.get("issueNumbers", [])
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
        _require(mutation.get("status") == "passed", "MUTATION_STATUS", "mutation report is not marked passed", blockers)
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
        _require(corpus.get("status") == "passed", "CORPUS_STATUS", "independent corpus report is not marked passed", blockers)
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
        _require(query.get("status") == "passed", "QUERY_STATUS", "query qualification report is not marked passed", blockers)
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
        _require(real_input.get("status") == "passed", "E2E_STATUS", "real-input E2E report is not marked passed", blockers)
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


def run_bundle(manifest: Path, *, allow_dirty: bool = False) -> dict[str, Any]:
    """Run the commit-bound Evidence validator for every recovery child.

    The old phase-2 report is intentionally not a release result.  A bundle
    invocation is the only strict completion path and its issue scope is the
    full #88--#105 recovery contract, not just #88.
    """

    try:
        from validate_qualification_bundle import validate_bundle
    except ImportError:  # pragma: no cover
        from tools.validate_qualification_bundle import validate_bundle

    validation = validate_bundle(manifest, repo_root=ROOT, allow_dirty=allow_dirty)
    blockers = list(validation.get("diagnostics", []))
    bundle_checks: dict[str, Any] | None = None
    if validation.get("status") == "passed":
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
    issues = list(range(88, 106))
    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "passed" if validation.get("status") == "passed" and not blockers else "blocked",
        "mode": "candidate",
        "releaseReady": False,
        "issues": issues,
        "blockers": blockers,
        "reportsChecked": ["qualification-bundle", *[f"issue-{issue}" for issue in issues]],
        "issueReports": _bundle_issue_reports(manifest),
        "bundleValidation": validation,
        "bundleChecks": bundle_checks,
    }


def run_attestation(attestation: Path, *, bundle: Path | None = None) -> dict[str, Any]:
    """Validate a final attestation without re-entering the legacy gate."""

    try:
        try:
            from release_attestation import load_and_validate_attestation
        except ImportError:  # pragma: no cover
            from tools.release_attestation import load_and_validate_attestation
        actions = os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
        expected_attempt: Any = os.environ.get("GITHUB_RUN_ATTEMPT") if actions else None
        if isinstance(expected_attempt, str) and expected_attempt.isdigit():
            expected_attempt = int(expected_attempt)
        result = load_and_validate_attestation(
            attestation,
            bundle_manifest_path=bundle,
            expected_run_id=os.environ.get("GITHUB_RUN_ID") if actions else None,
            expected_attempt=expected_attempt,
        )
    except Exception as exc:
        return {
            "schema": "fdir/strict-completion-gate-report",
            "version": "1.2.0",
            "status": "blocked",
            "mode": "final-attestation",
            "releaseReady": False,
            "issues": list(range(88, 106)),
            "blockers": [{"code": getattr(exc, "code", "ATTESTATION_INVALID"), "detail": str(exc)}],
            "reportsChecked": ["final-attestation"],
            "issueReports": _bundle_issue_reports(bundle) if bundle is not None else [],
        }
    return {
        "schema": "fdir/strict-completion-gate-report",
        "version": "1.2.0",
        "status": "passed",
        "mode": "final-attestation",
        "releaseReady": True,
        "issues": list(range(88, 106)),
        "blockers": [],
        "reportsChecked": ["qualification-bundle", "final-attestation", *[f"issue-{issue}" for issue in range(88, 106)]],
        "issueReports": _bundle_issue_reports(bundle) if bundle is not None else [],
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
        "issues": list(range(88, 106)),
        "blockers": [{
            "code": "LEGACY_COMPLETION_PATH_DISABLED",
            "detail": "strict completion release qualification requires --bundle or --attestation; the legacy report path is development-only",
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
        report = {"schema": "fdir/strict-completion-gate-report", "version": "1.2.0", "status": "blocked", "mode": "release" if args.mode == "release" else "smoke", "releaseReady": False, "issues": list(range(88, 106)), "blockers": [{"code": "GATE_ERROR", "detail": f"{type(exc).__name__}: {exc}"}]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
