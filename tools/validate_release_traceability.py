#!/usr/bin/env python3
"""Fail-closed validation for the frozen FDIR 2.1 release scope."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

AUTHORITY = (
    "release/claim-manifest.yaml",
    "release/traceability.yaml",
    "release/deferred-capabilities.yaml",
    "release/blocker-policy.yaml",
    "release/change-control.md",
)
OWNER_FIELDS = (
    "implementationOwnerIssues", "coreDependencyIssues",
    "positiveEvidenceOwnerIssues", "negativeEvidenceOwnerIssues",
    "ambiguousPartialEvidenceOwnerIssues", "reliabilityQualificationOwnerIssues",
    "securityPrivacyQualificationOwnerIssues", "packagingOwnerIssues",
    "documentationOwnerIssues", "qualificationReportOwnerIssues",
)
REQ_OWNER_FIELDS = (
    "implementationOwnerIssues", "verificationOwnerIssues", "qualificationOwnerIssues"
)
INITIAL_TUPLES = {
    "markdown.recorded-information.recorded-information-core",
    "docx.recorded-information.recorded-information-core",
    "xlsx.recorded-information.recorded-information-core",
    "pdf.recorded-information.recorded-information-core",
}
COMPLETION_STATES = {"planned", "implemented", "verified", "qualified", "complete"}
EVIDENCE_STATES = COMPLETION_STATES - {"planned"}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def duplicates(values: list[Any]) -> list[Any]:
    seen, dup = set(), set()
    for value in values:
        (dup if value in seen else seen).add(value)
    return sorted(dup, key=str)


def owner_list(entry: dict[str, Any], field: str, label: str,
               known: set[int], failures: list[str]) -> None:
    values = entry.get(field)
    if not isinstance(values, list) or not values:
        failures.append(f"missing owner: {label}.{field}")
        return
    for value in values:
        if not isinstance(value, int) or value not in known:
            failures.append(f"invalid owner issue: {label}.{field}={value!r}")


def evidence(entry: dict[str, Any], label: str, root: Path,
             failures: list[str], paths: bool) -> None:
    state = entry.get("completionState")
    if state not in COMPLETION_STATES:
        failures.append(f"invalid completion state: {label}={state!r}")
    values = entry.get("evidencePaths")
    if not isinstance(values, list):
        failures.append(f"evidencePaths must be a list: {label}")
        return
    if state in EVIDENCE_STATES and not values:
        failures.append(f"missing evidence: {label} is {state}")
    if paths:
        for relative in values:
            if not isinstance(relative, str) or not (root / relative).is_file():
                failures.append(f"missing evidence file: {label} -> {relative}")


def load_models(root: Path) -> list[dict[str, Any]]:
    return [load(root / path) for path in (
        "machine/requirements.yaml", "machine/acceptance-tests.yaml",
        "machine/profiles.yaml", "machine/capabilities.yaml",
        "release/claim-manifest.yaml", "release/deferred-capabilities.yaml",
        "release/traceability.yaml", "release/blocker-policy.yaml",
        "release/scope-approvals.yaml",
    )]


def validate_models(root: Path, models: list[dict[str, Any]], *,
                    paths: bool, approval_blobs: bool) -> list[str]:
    req_doc, test_doc, profile_doc, cap_doc, claim, deferred, trace, blockers, approvals = models
    failures: list[str] = []

    lines = {item.get("releaseLine") for item in (claim, deferred, trace, blockers, approvals)}
    revisions = {item.get("scopeRevision") for item in (claim, deferred, trace, blockers, approvals)}
    if lines != {"2.1.x"}:
        failures.append(f"release line mismatch: {sorted(map(repr, lines))}")
    if len(revisions) != 1 or not all(isinstance(item, int) and item > 0 for item in revisions):
        failures.append(f"scope revision mismatch: {sorted(map(repr, revisions))}")
    revision = claim.get("scopeRevision")

    registry = claim.get("issueRegistry", [])
    issue_values = [item.get("issue") for item in registry if isinstance(item, dict)]
    for value in duplicates(issue_values):
        failures.append(f"duplicate issue registry entry: #{value}")
    known = {value for value in issue_values if isinstance(value, int)}
    if not set(range(1, 27)).issubset(known):
        failures.append("claim manifest issueRegistry must include issues #1 through #26")

    requirements = req_doc.get("requirements", [])
    tests = test_doc.get("tests", [])
    req_ids = [item.get("id") for item in requirements if isinstance(item, dict)]
    test_ids = [item.get("id") for item in tests if isinstance(item, dict)]
    for value in duplicates(req_ids):
        failures.append(f"duplicate normative requirement: {value}")
    for value in duplicates(test_ids):
        failures.append(f"duplicate normative acceptance test: {value}")
    req_set = {value for value in req_ids if isinstance(value, str)}
    test_set = {value for value in test_ids if isinstance(value, str)}
    expected: dict[str, set[str]] = {value: set() for value in req_set}
    for item in tests:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        linked = item.get("requirements")
        if not isinstance(linked, list) or not linked:
            failures.append(f"acceptance test has no requirements: {item['id']}")
            continue
        for req_id in linked:
            if req_id not in req_set:
                failures.append(f"acceptance test references unknown requirement: {item['id']} -> {req_id}")
            else:
                expected[req_id].add(item["id"])

    tr_reqs = trace.get("requirements", [])
    tr_tests = trace.get("acceptanceTests", [])
    tr_req_ids = [item.get("requirementId") for item in tr_reqs if isinstance(item, dict)]
    tr_test_ids = [item.get("acceptanceTestId") for item in tr_tests if isinstance(item, dict)]
    for value in duplicates(tr_req_ids):
        failures.append(f"duplicate release requirement trace: {value}")
    for value in duplicates(tr_test_ids):
        failures.append(f"duplicate release acceptance-test trace: {value}")
    tr_req = {item["requirementId"]: item for item in tr_reqs
              if isinstance(item, dict) and isinstance(item.get("requirementId"), str)}
    tr_test = {item["acceptanceTestId"]: item for item in tr_tests
               if isinstance(item, dict) and isinstance(item.get("acceptanceTestId"), str)}
    for value in sorted(req_set - set(tr_req)):
        failures.append(f"orphan normative requirement: {value}")
    for value in sorted(set(tr_req) - req_set):
        failures.append(f"orphan traceability requirement: {value}")
    for value in sorted(test_set - set(tr_test)):
        failures.append(f"orphan normative acceptance test: {value}")
    for value in sorted(set(tr_test) - test_set):
        failures.append(f"orphan traceability acceptance test: {value}")

    adrs = trace.get("adrs", [])
    schemas = trace.get("schemas", [])
    adr_by_path = {item.get("path"): item for item in adrs if isinstance(item, dict)}
    schema_by_path = {item.get("path"): item for item in schemas if isinstance(item, dict)}
    for name, items in (("ADR", adrs), ("schema", schemas)):
        for value in duplicates([item.get("path") for item in items if isinstance(item, dict)]):
            failures.append(f"duplicate {name} trace: {value}")

    for req_id, item in tr_req.items():
        label = f"requirement {req_id}"
        for field in REQ_OWNER_FIELDS:
            owner_list(item, field, label, known, failures)
        evidence(item, label, root, failures, paths)
        actual = set(item.get("acceptanceTestIds", []))
        if actual != expected.get(req_id, set()):
            failures.append(f"acceptance-test mapping mismatch for {req_id}: expected {sorted(expected.get(req_id, set()))}, got {sorted(actual)}")
        for field, catalog in (("adrPaths", adr_by_path), ("schemaPaths", schema_by_path)):
            values = item.get(field)
            if not isinstance(values, list):
                failures.append(f"{field} must be a list: {req_id}")
                continue
            for relative in values:
                if relative not in catalog:
                    failures.append(f"unregistered {field}: {req_id} -> {relative}")
                elif req_id not in catalog[relative].get("requirementIds", []):
                    failures.append(f"asymmetric {field}: {req_id} -> {relative}")
                if paths and not (root / relative).is_file():
                    failures.append(f"missing mapped file: {req_id} -> {relative}")
        fixtures = item.get("fixturePaths")
        if not isinstance(fixtures, list):
            failures.append(f"fixturePaths must be a list: {req_id}")
        elif paths:
            for relative in fixtures:
                if not (root / relative).is_file():
                    failures.append(f"missing mapped fixture: {req_id} -> {relative}")

    for test_id, item in tr_test.items():
        owner_list(item, "executionOwnerIssues", f"acceptance test {test_id}", known, failures)
        evidence(item, f"acceptance test {test_id}", root, failures, paths)

    for name, items, field in (("ADR", adrs, "adrPaths"), ("schema", schemas, "schemaPaths")):
        for item in items:
            if not isinstance(item, dict):
                failures.append(f"invalid {name} registry entry")
                continue
            relative = item.get("path")
            owner_list(item, "ownerIssues", f"{name} {relative}", known, failures)
            linked = item.get("requirementIds")
            if not isinstance(linked, list) or not linked:
                failures.append(f"orphan {name} mapping: {relative}")
                continue
            for req_id in linked:
                if req_id not in tr_req or relative not in tr_req[req_id].get(field, []):
                    failures.append(f"asymmetric {name.lower()} mapping: {relative} -> {req_id}")
            if paths and (not isinstance(relative, str) or not (root / relative).is_file()):
                failures.append(f"missing registered {name.lower()}: {relative}")

    artifacts = trace.get("releaseArtifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        failures.append("releaseArtifacts registry is missing")
        artifacts = []
    for item in artifacts:
        if not isinstance(item, dict):
            failures.append("invalid release artifact entry")
            continue
        relative = item.get("path")
        owner_list(item, "ownerIssues", f"release artifact {relative}", known, failures)
        if not isinstance(item.get("purpose"), str) or not item["purpose"].strip():
            failures.append(f"release artifact purpose is missing: {relative}")
        if paths and (not isinstance(relative, str) or not (root / relative).is_file()):
            failures.append(f"missing release artifact: {relative}")

    profiles = {item.get("id") for item in profile_doc.get("profiles", []) if isinstance(item, dict)}
    capabilities = {(item.get("format"), item.get("capability"), item.get("profile"))
                    for item in cap_doc.get("capabilities", []) if isinstance(item, dict)}
    policy = claim.get("claimPolicy", {})
    qualified = policy.get("qualifiedState")
    valid_states = set(policy.get("unqualifiedStates", [])) | ({qualified} if isinstance(qualified, str) else set())
    tuples = claim.get("formatTuples", [])
    tuple_ids = [item.get("id") for item in tuples if isinstance(item, dict)]
    if revision == 1 and set(tuple_ids) != INITIAL_TUPLES:
        failures.append(f"scope revision 1 tuple set changed: {sorted(tuple_ids, key=str)}")
    for item in tuples:
        if not isinstance(item, dict):
            failures.append("invalid claim tuple entry")
            continue
        tuple_id = item.get("id")
        label = f"claim tuple {tuple_id}"
        expected_id = f"{item.get('format')}.{item.get('capability')}.{item.get('profile')}"
        if tuple_id != expected_id:
            failures.append(f"claim tuple id mismatch: {tuple_id!r} != {expected_id!r}")
        if (item.get("format"), item.get("capability"), item.get("profile")) not in capabilities:
            failures.append(f"claim tuple is absent from machine capabilities: {tuple_id}")
        if item.get("profile") not in profiles:
            failures.append(f"claim tuple references unknown profile: {tuple_id}")
        supporting = item.get("requiredSupportingProfiles")
        if not isinstance(supporting, list) or not supporting or any(value not in profiles for value in supporting):
            failures.append(f"claim tuple has invalid supporting profiles: {tuple_id}")
        if not isinstance(item.get("scopeItems"), list) or not item["scopeItems"]:
            failures.append(f"claim tuple has no bounded scope items: {tuple_id}")
        for field in OWNER_FIELDS:
            owner_list(item, field, label, known, failures)
        required = item.get("requiredRequirementIds")
        if not isinstance(required, list) or not required:
            failures.append(f"claim tuple has no normative requirements: {tuple_id}")
        elif any(value not in req_set for value in required):
            failures.append(f"claim tuple references unknown requirement: {tuple_id}")
        if item.get("state") not in valid_states:
            failures.append(f"invalid claim state: {tuple_id}={item.get('state')!r}")
        if item.get("productionReady") is not False:
            if item.get("state") != qualified:
                failures.append(f"production-ready tuple is not qualified: {tuple_id}")
            if not item.get("qualificationEvidencePaths"):
                failures.append(f"production-ready tuple has no qualification evidence: {tuple_id}")
    if claim.get("developmentStatus") == "development-unqualified" and claim.get("productionReady") is not False:
        failures.append("development-unqualified release cannot be productionReady")

    functions = claim.get("releaseFunctions", [])
    if not isinstance(functions, list) or not functions:
        failures.append("releaseFunctions registry is missing")
    for item in functions:
        if not isinstance(item, dict):
            failures.append("invalid release function entry")
            continue
        function_id = item.get("id")
        owner_list(item, "ownerIssues", f"release function {function_id}", known, failures)
        required = item.get("requiredRequirementIds")
        if not isinstance(required, list) or not required or any(value not in req_set for value in required):
            failures.append(f"release function has invalid normative requirements: {function_id}")
        if item.get("productionReady") is not False and item.get("state") != qualified:
            failures.append(f"production-ready release function is not qualified: {function_id}")
    wide = claim.get("releaseWideEvidenceOwners")
    if not isinstance(wide, dict) or not wide:
        failures.append("releaseWideEvidenceOwners is missing")
    else:
        for name, values in wide.items():
            owner_list({"owners": values}, "owners", f"release-wide evidence {name}", known, failures)

    approval_policy = claim.get("approval", {})
    required_controls = {"auditable-manifest-diff", "scopeRevision-increment",
                         "roadmap-and-traceability-update", "qualification-impact-review"}
    if approval_policy.get("futureSemanticScopeChangesRequireOwnerApproval") is not True:
        failures.append("future semantic scope changes do not require owner approval")
    if not required_controls.issubset(set(approval_policy.get("futureChangeRequirements", []))):
        failures.append("claim manifest approval policy omits mandatory change-control gates")

    deferred_items = deferred.get("capabilities", [])
    if not isinstance(deferred_items, list) or not deferred_items:
        failures.append("deferred capability registry is missing")
    for item in deferred_items:
        if not isinstance(item, dict):
            failures.append("invalid deferred capability entry")
            continue
        deferred_id = item.get("id")
        if not isinstance(deferred_id, str) or not deferred_id:
            failures.append("deferred capability id is missing")
        if not isinstance(item.get("rationale"), str) or len(item["rationale"].strip()) < 20:
            failures.append(f"deferred capability rationale is missing: {deferred_id}")
        if not isinstance(item.get("futureOwner"), str) or not item["futureOwner"].strip():
            failures.append(f"deferred capability future owner is missing: {deferred_id}")
        if item.get("productionReady") is not False:
            failures.append(f"deferred capability is productionReady: {deferred_id}")

    severity_expected = {"critical": True, "high": True, "medium": "conditional",
                         "low": False, "informational": False}
    severity_items = blockers.get("severities", [])
    severity = {item.get("id"): item for item in severity_items if isinstance(item, dict)}
    if set(severity) != set(severity_expected):
        failures.append(f"release blocker severity set is incomplete: {sorted(severity)}")
    for name, expected_value in severity_expected.items():
        item = severity.get(name, {})
        if item.get("releaseBlocking") != expected_value:
            failures.append(f"release blocker policy mismatch: {name}.releaseBlocking")
        if not isinstance(item.get("resolutionPolicy"), str) or not item["resolutionPolicy"].strip():
            failures.append(f"release blocker resolution policy is missing: {name}")
    expected_gap = {
        "roadmapIssue": 4, "scopeIssue": 5,
        "newRequirementRequiresStableId": True, "newRequirementRequiresOwningIssue": True,
        "roadmapUpdateRequired": True, "traceabilityUpdateRequired": True,
        "scopeImpactRequiresRevisionIncrement": True,
        "scopeImpactRequiresProjectOwnerApproval": True, "closureRequiresEvidence": True,
    }
    gap = blockers.get("gapIntake", {})
    for field, expected_value in expected_gap.items():
        if gap.get(field) != expected_value:
            failures.append(f"gap-intake policy mismatch: {field}")

    if paths:
        change = (root / "release/change-control.md").read_text(encoding="utf-8").lower()
        for token_value in ("editorial clarification", "claim narrowing", "claim expansion",
                            "normative semantic change", "project-owner approval", "roadmap #4",
                            "release blockers", "deferred capabilities"):
            if token_value not in change:
                failures.append(f"release change-control policy omits: {token_value}")

    if approvals.get("projectOwner") != "horiyamayoh":
        failures.append("scope approval project owner must be horiyamayoh")
    decisions = approvals.get("decisions", [])
    current = next((item for item in decisions
                    if isinstance(item, dict) and item.get("id") == approvals.get("currentDecision")), None)
    if not isinstance(current, dict):
        failures.append("current scope approval decision is missing")
    else:
        checks = (("scopeRevision", revision), ("status", "approved"),
                  ("approvedBy", approvals.get("projectOwner")), ("issue", 5))
        for field, expected_value in checks:
            if current.get(field) != expected_value:
                failures.append(f"scope approval mismatch: {field}")
        if not isinstance(current.get("pullRequest"), int) or current["pullRequest"] <= 0:
            failures.append("current scope approval must cite a pull request")
        shas = current.get("authorityBlobShas")
        if not isinstance(shas, dict) or set(shas) != set(AUTHORITY):
            failures.append("scope approval does not bind every release-scope authority file")
        elif approval_blobs:
            for relative in AUTHORITY:
                actual = blob_sha(root / relative)
                if shas.get(relative) != actual:
                    failures.append(f"scope approval is stale for {relative}: expected {shas.get(relative)}, actual {actual}")
    return failures


def generated_failures(root: Path) -> list[str]:
    generator = tool(root / "tools/generate_traceability.py", "fdir_traceability_generator")
    failures = []
    for relative, content in generator.outputs(root).items():
        path = root / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            failures.append(f"stale generated mapping: {relative}")
    return failures


def validate_repository(root: Path, *, generated: bool = True) -> list[str]:
    required = (
        "machine/requirements.yaml", "machine/acceptance-tests.yaml",
        "machine/profiles.yaml", "machine/capabilities.yaml",
        "release/claim-manifest.yaml", "release/deferred-capabilities.yaml",
        "release/traceability.yaml", "release/blocker-policy.yaml",
        "release/scope-approvals.yaml", "release/change-control.md",
        "tools/generate_traceability.py",
    )
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        return [f"missing release traceability input: {path}" for path in missing]
    try:
        models = load_models(root)
        failures = validate_models(root, models, paths=True, approval_blobs=True)
        if generated:
            failures.extend(generated_failures(root))
        return sorted(set(failures))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        return [f"cannot validate release traceability: {error}"]


def self_tests(root: Path) -> list[str]:
    try:
        models = load_models(root)
    except (OSError, json.JSONDecodeError) as error:
        return [f"self-test setup failed: {error}"]
    failures: list[str] = []

    def expect(name: str, mutate: Callable[[list[dict[str, Any]]], None], fragment: str,
               *, paths: bool = False, approval: bool = False) -> None:
        copies = copy.deepcopy(models)
        mutate(copies)
        actual = validate_models(root, copies, paths=paths, approval_blobs=approval)
        if not any(fragment in item for item in actual):
            failures.append(f"self-test {name} did not detect {fragment!r}")

    expect("orphan requirement", lambda d: d[6]["requirements"].pop(0),
           "orphan normative requirement")
    expect("orphan test", lambda d: d[6]["acceptanceTests"].pop(0),
           "orphan normative acceptance test")
    expect("missing owner", lambda d: d[6]["requirements"][0].__setitem__("implementationOwnerIssues", []),
           "missing owner")
    def due_evidence(d: list[dict[str, Any]]) -> None:
        d[6]["requirements"][0]["completionState"] = "verified"
        d[6]["requirements"][0]["evidencePaths"] = []
    expect("missing evidence", due_evidence, "missing evidence")
    expect("deferred leak", lambda d: d[5]["capabilities"][0].__setitem__("productionReady", True),
           "deferred capability is productionReady")
    expect("scope revision", lambda d: d[7].__setitem__("scopeRevision", 999),
           "scope revision mismatch")
    expect("unqualified tuple", lambda d: d[4]["formatTuples"][0].__setitem__("productionReady", True),
           "production-ready tuple is not qualified")
    def stale(d: list[dict[str, Any]]) -> None:
        current = next(item for item in d[8]["decisions"] if item["id"] == d[8]["currentDecision"])
        current["authorityBlobShas"][AUTHORITY[0]] = "0" * 40
    expect("stale approval", stale, "scope approval is stale", paths=True, approval=True)
    return failures


def summary(root: Path, failures: list[str], self_failures: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok" if not failures and not self_failures else "failed",
        "unresolvedGapCount": len(failures), "selfTestFailureCount": len(self_failures),
    }
    try:
        reqs, tests, _, _, claim, deferred, _, _, approvals = load_models(root)
        result.update({"releaseLine": claim.get("releaseLine"),
                       "scopeRevision": claim.get("scopeRevision"),
                       "requirements": len(reqs.get("requirements", [])),
                       "acceptanceTests": len(tests.get("tests", [])),
                       "formatTuples": len(claim.get("formatTuples", [])),
                       "releaseFunctions": len(claim.get("releaseFunctions", [])),
                       "deferredCapabilities": len(deferred.get("capabilities", [])),
                       "approvalDecision": approvals.get("currentDecision")})
    except (OSError, json.JSONDecodeError):
        pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    failures = validate_repository(root)
    self_failures = self_tests(root) if args.self_test else []
    result = summary(root, failures, self_failures)
    if args.json:
        result.update({"failures": failures, "selfTestFailures": self_failures})
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    elif failures or self_failures:
        print("FDIR 2.1 release traceability validation failed:")
        for item in failures + self_failures:
            print(f"  - {item}")
    else:
        print("FDIR 2.1 release traceability passed: "
              f"{result.get('requirements', 0)} requirements, "
              f"{result.get('acceptanceTests', 0)} acceptance tests, "
              f"{result.get('formatTuples', 0)} format tuples, "
              f"{result.get('deferredCapabilities', 0)} deferred capabilities, "
              "unresolved gaps 0")
        if args.self_test:
            print("fail-closed self-tests: ok")
    return 1 if failures or self_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
