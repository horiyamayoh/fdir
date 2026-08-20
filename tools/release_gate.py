"""Fail-closed, reproducible integration gate for the Document Form IR design.

The gate deliberately has no third-party dependencies.  It runs the two
authoritative commands first, then checks the machine-readable release
inventory and the product boundary that those commands cannot fully express.
The only successful result is a zero exit status together with a JSON summary
whose command logs can be replayed from the repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from qualification_evidence import validate_source_feature_closure
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_source_feature_closure


ROOT = Path(__file__).resolve().parents[1]

REQUIREMENTS_PATH = ROOT / "machine" / "requirements.json"
ACCEPTANCE_PATH = ROOT / "machine" / "acceptance-tests.json"
ISSUE_PLAN_PATH = ROOT / "machine" / "issue-plan.json"
GITHUB_ISSUE_MAP_PATH = ROOT / "machine" / "github-issue-map.json"
PHASE2_ISSUE_PLAN_PATH = ROOT / "machine" / "phase2-issue-plan.json"
CAPABILITY_PROFILE_PATH = ROOT / "machine" / "capability-profile.json"
REFERENCE_REGISTRY_PATH = ROOT / "machine" / "reference-registry.json"
EXTENSION_REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
CANONICALIZATION_PATH = ROOT / "machine" / "canonicalization.json"
QUERY_CONTRACT_PATH = ROOT / "machine" / "query-contract.json"
RELEASE_CLAIM_MANIFEST_PATH = ROOT / "machine" / "release-claim-manifest.json"
INDEPENDENT_CORPUS_MANIFEST_PATH = ROOT / "e2e" / "corpus" / "manifest.json"
STRICT_COMPLETION_CONTRACT_PATH = ROOT / "machine" / "strict-completion-contract.json"
TRACEABILITY_PATH = ROOT / "machine" / "traceability.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
EXAMPLES_PATH = ROOT / "examples"

EXPECTED_REQUIREMENTS = 134
EXPECTED_FAMILIES = 16
EXPECTED_CASES = 134
EXPECTED_LEAF_ISSUES = 20
EXPECTED_UMBRELLA_ISSUE = 47
EXPECTED_LEAF_ISSUE_RANGE = range(48, 68)
EXPECTED_E2E_ISSUE = 68

REQUIRED_EXAMPLES = {
    "callout.json",
    "cell-formula.json",
    "markdown-authoring.json",
    "partial-conversion.json",
    "pdf-observation.json",
    "style-resolution.json",
}

NORMATIVE_DOCUMENTS = (
    "README.md",
    "docs/01-product-definition.md",
    "docs/02-architecture.md",
    "docs/03-logical-model.md",
    "docs/04-format-mapping.md",
    "docs/05-serialization-and-extensions.md",
    "docs/06-interfaces-and-implementation.md",
    "docs/07-verification-and-issues.md",
    "docs/08-review-and-reset.md",
)

REQUIRED_SCHEMA_FIELDS = {
    "schema",
    "documentId",
    "sourceFormat",
    "rootNodeId",
    "nodes",
    "conversion",
}

# These are product-model concepts, not words that are forbidden in a
# boundary document.  The key scan therefore applies to the schema and JSON
# examples, while the documentation scan below requires explicit boundary
# language instead of rejecting the documented non-goals themselves.
FORBIDDEN_PRODUCT_KEY_WORDS = (
    "semantic",
    "predicate",
    "assertion",
    "equivalence",
    "rawbyte",
    "sourcebyte",
    "sourcestore",
    "sourcearchive",
    "contentaddressedsource",
    "evidencestore",
    "forensic",
    "accounting",
    "census",
    "lineage",
)

FORBIDDEN_PRODUCT_VALUE_PATTERNS = (
    re.compile(r"\bsemantic\s+(?:ir|equivalence|predicate|assertion|interpretation)\b", re.I),
    re.compile(r"\b(?:raw|source)[ -]?bytes?\b", re.I),
    re.compile(r"\b(?:content[- ]addressed\s+)?source\s+(?:store|archive)\b", re.I),
    re.compile(r"\bforensic(?:\s+(?:evidence|archive|accounting))?\b", re.I),
    re.compile(r"\b(?:accounting\s+(?:closure|item|census)|byte\s+census)\b", re.I),
    re.compile(r"\b(?:lineage\s+certificate|cross[- ]revision\s+lineage)\b", re.I),
    re.compile(r"\bpredicate\b", re.I),
)


class GateError(Exception):
    """A deterministic, user-facing release-gate failure."""


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def check_source_closure_report(report: dict[str, Any], label: str) -> None:
    """Recompute source closure from each report's emitted IR document."""

    cases = list(report.get("cases", []))
    cases.extend(item for item in report.get("negativeChecks", []) if isinstance(item, dict))
    require(cases, f"{label} has no source closure cases")
    for case in cases:
        require(isinstance(case, dict), f"{label} contains a malformed source closure case")
        document_path = case.get("documentPath") or case.get("output")
        require(isinstance(document_path, str) and Path(document_path).is_file(), f"{label} case has no emitted IR document path")
        document = load_json(Path(document_path))
        closure = validate_source_feature_closure(document, case)
        require(closure.get("status") == "passed" and closure.get("mismatches") == [], f"{label} source closure failed: {json.dumps(closure.get('mismatches', []), ensure_ascii=False)}")
        reported = case.get("sourceClosure")
        require(isinstance(reported, dict) and reported.get("status") == "passed" and reported.get("mismatches") == [], f"{label} did not report a passed source closure")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateError(f"missing JSON artifact: {relative(path)}") from exc
    except UnicodeDecodeError as exc:
        raise GateError(f"invalid UTF-8 in {relative(path)}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid JSON in {relative(path)}: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc


def compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def find_product_boundary_leaks(value: Any, label: str, *, scan_values: bool) -> list[str]:
    leaks: list[str] = []

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                compact = compact_key(key_text)
                if any(word in compact for word in FORBIDDEN_PRODUCT_KEY_WORDS):
                    leaks.append(f"{label}{''.join(f'[{part!r}]' for part in path)}[{key_text!r}]")
                visit(child, path + (key_text,))
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, path + (str(index),))
            return
        if scan_values and isinstance(node, str):
            if any(pattern.search(node) for pattern in FORBIDDEN_PRODUCT_VALUE_PATTERNS):
                leaks.append(f"{label}{''.join(f'[{part!r}]' for part in path)}")

    visit(value, ())
    return leaks


def check_design_catalog() -> dict[str, int]:
    requirements = load_json(REQUIREMENTS_PATH)
    acceptance = load_json(ACCEPTANCE_PATH)
    require(isinstance(requirements, dict), "requirements root must be an object")
    require(isinstance(acceptance, dict), "acceptance-tests root must be an object")

    requirement_entries = requirements.get("requirements")
    family_entries = acceptance.get("families")
    require(isinstance(requirement_entries, list), "requirements must be an array")
    require(isinstance(family_entries, list), "acceptance families must be an array")
    require(len(requirement_entries) == EXPECTED_REQUIREMENTS, f"expected {EXPECTED_REQUIREMENTS} requirements, got {len(requirement_entries)}")
    require(len(family_entries) == EXPECTED_FAMILIES, f"expected {EXPECTED_FAMILIES} acceptance families, got {len(family_entries)}")

    requirement_ids: set[str] = set()
    for requirement in requirement_entries:
        require(isinstance(requirement, dict), "requirement entry is not an object")
        requirement_id = requirement.get("id")
        require(isinstance(requirement_id, str) and requirement_id, "requirement has no id")
        require(requirement_id not in requirement_ids, f"duplicate requirement id: {requirement_id}")
        requirement_ids.add(requirement_id)

    expected_cases: list[str] = []
    family_ids: set[str] = set()
    for family in family_entries:
        require(isinstance(family, dict), "acceptance family entry is not an object")
        family_id = family.get("id")
        count = family.get("count")
        prefix = family.get("requirementPrefix")
        require(isinstance(family_id, str) and family_id, "acceptance family has no id")
        require(family_id not in family_ids, f"duplicate acceptance family id: {family_id}")
        family_ids.add(family_id)
        require(isinstance(count, int) and count > 0, f"invalid acceptance count: {family_id}")
        require(isinstance(prefix, str) and prefix, f"acceptance family has no requirement prefix: {family_id}")
        matching = [entry for entry in requirement_entries if isinstance(entry, dict) and str(entry.get("id", "")).startswith(prefix)]
        require(len(matching) == count, f"acceptance family count mismatch: {family_id} expected {count} got {len(matching)}")
        expected_cases.extend(f"{family_id}-{number:03d}" for number in range(1, count + 1))

    require(len(expected_cases) == EXPECTED_CASES, f"expected {EXPECTED_CASES} acceptance cases, got {len(expected_cases)}")
    require(len(set(expected_cases)) == EXPECTED_CASES, "acceptance case IDs are not unique")

    actual_cases: list[str] = []
    for requirement in requirement_entries:
        tests = requirement.get("acceptanceTests") if isinstance(requirement, dict) else None
        requirement_id = requirement.get("id") if isinstance(requirement, dict) else "<unknown>"
        require(isinstance(tests, list) and len(tests) == 1, f"requirement must map to exactly one acceptance case: {requirement_id}")
        case_id = tests[0]
        require(isinstance(case_id, str) and case_id, f"requirement has invalid acceptance case: {requirement_id}")
        actual_cases.append(case_id)

    require(len(actual_cases) == EXPECTED_CASES, f"expected {EXPECTED_CASES} mapped acceptance cases, got {len(actual_cases)}")
    require(len(set(actual_cases)) == EXPECTED_CASES, "acceptance cases are mapped more than once")
    missing = sorted(set(expected_cases) - set(actual_cases))
    extra = sorted(set(actual_cases) - set(expected_cases))
    require(not missing and not extra, f"acceptance catalog mismatch: missing={missing} extra={extra}")

    return {
        "requirements": len(requirement_entries),
        "acceptance_families": len(family_entries),
        "acceptance_cases": len(expected_cases),
    }


def check_issue_plan() -> dict[str, int]:
    issue_plan = load_json(ISSUE_PLAN_PATH)
    github_map = load_json(GITHUB_ISSUE_MAP_PATH)
    require(isinstance(issue_plan, dict), "issue-plan root must be an object")
    require(isinstance(github_map, dict), "github issue map root must be an object")

    entries = issue_plan.get("issues")
    require(isinstance(entries, list), "issue plan issues must be an array")
    issue_by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        require(isinstance(entry, dict), "issue plan entry is not an object")
        issue_id = entry.get("id")
        require(isinstance(issue_id, str) and issue_id, "issue plan entry has no id")
        require(issue_id not in issue_by_id, f"duplicate issue-plan id: {issue_id}")
        issue_by_id[issue_id] = entry

    umbrella = issue_by_id.get("DFIR-I-000")
    require(umbrella is not None and umbrella.get("kind") == "umbrella", "issue plan umbrella DFIR-I-000 is missing")
    expected_leaf_ids = {f"DFIR-I-{number:03d}" for number in range(1, EXPECTED_LEAF_ISSUES + 1)}
    actual_leaf_ids = set(issue_by_id) - {"DFIR-I-000"}
    require(actual_leaf_ids == expected_leaf_ids, "issue plan leaf IDs are not exactly DFIR-I-001 through DFIR-I-020")
    require(len(actual_leaf_ids) == EXPECTED_LEAF_ISSUES, f"expected {EXPECTED_LEAF_ISSUES} leaf issues, got {len(actual_leaf_ids)}")

    require(github_map.get("repository") == "horiyamayoh/fdir", "github issue map points to the wrong repository")
    mapped_umbrella = github_map.get("umbrella")
    require(isinstance(mapped_umbrella, dict), "github issue map umbrella is missing")
    require(mapped_umbrella.get("key") == "DFIR-I-000", "github issue map umbrella key is wrong")
    require(mapped_umbrella.get("issueNumber") == EXPECTED_UMBRELLA_ISSUE, "github issue map umbrella must be issue #47")

    mapped_leaves = github_map.get("issues")
    require(isinstance(mapped_leaves, list), "github issue map issues must be an array")
    mapped_by_key: dict[str, dict[str, Any]] = {}
    for entry in mapped_leaves:
        require(isinstance(entry, dict), "github issue map entry is not an object")
        key = entry.get("key")
        number = entry.get("issueNumber")
        require(isinstance(key, str) and key, "github issue map entry has no key")
        require(key not in mapped_by_key, f"duplicate github issue map key: {key}")
        mapped_by_key[key] = entry
        require(isinstance(number, int), f"github issue map has invalid number: {key}")

    require(set(mapped_by_key) == expected_leaf_ids, "github issue map does not cover exactly the 20 leaf issues")
    expected_numbers = set(EXPECTED_LEAF_ISSUE_RANGE)
    actual_numbers = {entry["issueNumber"] for entry in mapped_by_key.values()}
    require(actual_numbers == expected_numbers, "github issue map does not cover issues #48 through #67")
    for number in EXPECTED_LEAF_ISSUE_RANGE:
        key = f"DFIR-I-{number - 47:03d}"
        require(mapped_by_key[key]["issueNumber"] == number, f"github issue mapping is not contiguous at #{number}")
    blocker = github_map.get("releaseBlocker")
    require(isinstance(blocker, dict), "github issue map has no E2E release blocker")
    require(blocker.get("issueNumber") == EXPECTED_E2E_ISSUE, "github issue map E2E blocker must be issue #68")
    require(blocker.get("key") == "DFIR-E2E-001", "github issue map E2E blocker key is wrong")

    return {
        "issue_plan_entries": len(entries),
        "leaf_issues": len(actual_leaf_ids),
        "umbrella_issue": EXPECTED_UMBRELLA_ISSUE,
        "first_leaf_issue": min(expected_numbers),
        "last_leaf_issue": max(expected_numbers),
        "e2e_issue": EXPECTED_E2E_ISSUE,
    }


def check_traceability() -> dict[str, int]:
    traceability = load_json(TRACEABILITY_PATH)
    require(isinstance(traceability, dict), "traceability root must be an object")
    authoritative = traceability.get("authoritativeInputs")
    require(isinstance(authoritative, list), "traceability authoritativeInputs must be an array")
    required_inputs = {
        "machine/requirements.json",
        "machine/acceptance-tests.json",
        "machine/issue-plan.json",
        "machine/github-issue-map.json",
        "schemas/document-form-ir.schema.json",
    }
    require(required_inputs.issubset(set(authoritative)), "traceability omits a release authority")
    derivation = traceability.get("derivation")
    require(isinstance(derivation, dict), "traceability derivation is missing")
    for key in ("requirementToIssue", "requirementToAcceptance", "acceptanceFamilyExpansion"):
        require(isinstance(derivation.get(key), str) and derivation[key], f"traceability derivation is missing: {key}")
    return {"authoritative_inputs": len(authoritative), "derivation_rules": len(derivation)}


def check_phase2_contracts() -> dict[str, int]:
    plan = load_json(PHASE2_ISSUE_PLAN_PATH)
    require(isinstance(plan, dict), "phase2 issue plan root must be an object")
    policy = plan.get("policy")
    entries = plan.get("issues")
    require(isinstance(policy, dict) and isinstance(entries, list), "phase2 issue plan is incomplete")
    numbers = [entry.get("issueNumber") for entry in entries if isinstance(entry, dict)]
    expected_numbers = list(range(69, 85)) + [86]
    require(numbers == expected_numbers, "phase2 issue plan must cover issues #69 through #84 and #86 in order")
    require(policy.get("activeIssueNumbers") == expected_numbers, "phase2 active issue numbers are incomplete")
    required_commands = policy.get("requiredCommands")
    require(isinstance(required_commands, list) and "python tools/mutation_qualification.py --json" in required_commands and "python tools/query_qualification.py" in required_commands and "python tools/independent_corpus.py --json" in required_commands and "python tools/strict_completion_gate.py" in required_commands and "python tools/release_gate.py" in required_commands, "phase2 qualification commands are incomplete")

    capability = load_json(CAPABILITY_PROFILE_PATH)
    profiles = capability.get("profiles") if isinstance(capability, dict) else None
    require(isinstance(profiles, list) and {item.get("format") for item in profiles if isinstance(item, dict)} == {"docx", "xlsx", "pdf", "markdown"}, "capability profiles do not cover all formats")
    for profile in profiles:
        require(isinstance(profile, dict) and isinstance(profile.get("id"), str) and isinstance(profile.get("supportedFeatures"), list), "capability profile is malformed")
        require(profile.get("disposition") == "preserve-or-diagnose" and profile.get("unknownConstructPolicy") == "partial-with-diagnostic", "capability profile policy is not fail-closed")
    status_contract = capability.get("statusContract") if isinstance(capability, dict) else None
    require(isinstance(status_contract, dict) and status_contract.get("normalizedIsComplete") is True and "unavailable" in status_contract.get("observationOnlyStatuses", []), "capability status contract is incomplete")

    reference = load_json(REFERENCE_REGISTRY_PATH)
    references = reference.get("references") if isinstance(reference, dict) else None
    require(isinstance(references, list) and references, "reference registry is empty")
    owners = [item.get("owner") for item in references if isinstance(item, dict)]
    require(len(owners) == len(set(owners)), "reference registry contains duplicate owners")

    canonicalization = load_json(CANONICALIZATION_PATH)
    require(isinstance(canonicalization.get("entityCollections"), dict), "canonicalization registry lacks entity collections")
    require(canonicalization.get("defaultDigestProjection") == "source-map-excluded", "canonicalization default projection is wrong")
    require({item.get("name") for item in canonicalization.get("projections", []) if isinstance(item, dict)} >= {"full", "content", "source-map-excluded"}, "canonicalization projections are incomplete")

    query_contract = load_json(QUERY_CONTRACT_PATH)
    operations = query_contract.get("operations") if isinstance(query_contract, dict) else None
    require(isinstance(operations, list) and {"list-entities", "get-entity", "rebuild-index", "get-text"}.issubset(set(operations)), "typed query contract is incomplete")
    require(query_contract.get("index", {}).get("schema") == "fdir/document-form-index", "query index contract is missing")

    try:
        from extension_registry import validate_registry_integrity
    except ImportError:  # pragma: no cover
        from tools.extension_registry import validate_registry_integrity
    extension_details = validate_registry_integrity()
    return {"phase2_issues": len(numbers), "capability_profiles": len(profiles), "references": len(references), "extension_entries": extension_details["entries"], "canonical_entity_collections": len(canonicalization["entityCollections"]), "query_operations": len(operations)}


def check_release_claims() -> dict[str, int]:
    manifest = load_json(RELEASE_CLAIM_MANIFEST_PATH)
    require(manifest.get("schema") == "fdir/document-form-release-claim-manifest", "release claim manifest schema is missing")
    release = manifest.get("release")
    require(isinstance(release, dict) and release.get("policy") == "fail-closed", "release claim policy is not fail-closed")
    claims = manifest.get("issueClaims")
    plan = load_json(PHASE2_ISSUE_PLAN_PATH)
    plan_numbers = {entry.get("issueNumber") for entry in plan.get("issues", []) if isinstance(entry, dict)}
    require(isinstance(claims, list), "release claim issue claims are missing")
    claim_numbers = {claim.get("issueNumber") for claim in claims if isinstance(claim, dict)}
    require(claim_numbers == plan_numbers - {69}, "release claims do not cover every phase2 child issue")
    for claim in claims:
        require(isinstance(claim, dict) and isinstance(claim.get("claim"), str) and claim.get("claim"), "release claim is malformed")
        for command in claim.get("evidenceCommands", []):
            require(isinstance(command, str) and command, "release claim evidence command is malformed")
        for relative in claim.get("evidencePaths", []):
            require((ROOT / relative).is_file(), f"release claim evidence path is missing: {relative}")
    capability_claims = manifest.get("capabilityClaims")
    capability = load_json(CAPABILITY_PROFILE_PATH)
    profile_ids = {profile.get("id") for profile in capability.get("profiles", []) if isinstance(profile, dict)}
    require(isinstance(capability_claims, list) and {item.get("profileId") for item in capability_claims if isinstance(item, dict)} == profile_ids, "capability claims do not cover every profile")
    corpus = load_json(INDEPENDENT_CORPUS_MANIFEST_PATH)
    require(corpus.get("independent") is True and len(corpus.get("cases", [])) >= 4, "independent corpus is incomplete")
    required_formats = {"docx", "xlsx", "pdf", "markdown"}
    require({case.get("format") for case in corpus.get("cases", []) if isinstance(case, dict)} == required_formats, "independent corpus format matrix is incomplete")
    all_corpus_cases = [case for case in corpus.get("cases", []) + corpus.get("negativeCases", []) if isinstance(case, dict)]
    require({case.get("caseClass") for case in all_corpus_cases} >= {"positive", "malformed", "unsupported"}, "independent corpus lacks a positive/malformed/unsupported matrix")
    require({case.get("format") for case in all_corpus_cases} == required_formats, "independent negative corpus format matrix is incomplete")
    require(isinstance(corpus.get("negativeChecks"), list) and any(item.get("id") == "resource-limit" for item in corpus["negativeChecks"] if isinstance(item, dict)), "independent resource-limit evidence is missing")
    require(manifest.get("independentEvidence", {}).get("runner") == "tools/independent_corpus.py", "independent corpus runner is not claimed")
    strict_contract = load_json(STRICT_COMPLETION_CONTRACT_PATH)
    require(strict_contract.get("schema") == "fdir/document-form-strict-completion-contract", "strict completion contract is missing")
    require(strict_contract.get("closurePolicy", {}).get("closedStateIsNotEvidence") is True and strict_contract.get("closurePolicy", {}).get("fileExistenceIsNotEvidence") is True, "strict completion closure policy is weak")
    strict_claim = manifest.get("strictCompletionContract")
    require(isinstance(strict_claim, dict) and strict_claim.get("path") == "machine/strict-completion-contract.json" and strict_claim.get("gate") == "tools/strict_completion_gate.py" and strict_claim.get("requiredReportStatus") == "passed", "release claim does not bind the strict completion gate")
    strict_issue_evidence = strict_contract.get("issueEvidence", {})
    require(set(strict_issue_evidence) == {str(number) for number in strict_contract.get("scope", {}).get("phase2Issues", [])}, "strict issue evidence does not cover the declared phase2 scope")
    return {"child_claims": len(claims), "capability_claims": len(capability_claims), "independent_positive_cases": len(corpus["cases"]), "independent_negative_cases": len(corpus.get("negativeCases", [])), "strict_issue_bindings": len(strict_issue_evidence)}


def check_schema() -> dict[str, int]:
    schema = load_json(SCHEMA_PATH)
    require(isinstance(schema, dict), "IR schema root must be an object")
    require(schema.get("type") == "object", "IR schema root must be an object type")
    require(schema.get("additionalProperties") is False, "IR schema root must be closed")
    required = schema.get("required")
    properties = schema.get("properties")
    defs = schema.get("$defs")
    require(isinstance(required, list), "IR schema required must be an array")
    require(REQUIRED_SCHEMA_FIELDS.issubset(set(required)), "IR schema is missing a required core field")
    require(isinstance(properties, dict), "IR schema properties are missing")
    require(REQUIRED_SCHEMA_FIELDS.issubset(set(properties)), "IR schema properties omit a required core field")
    require(isinstance(defs, dict) and defs, "IR schema definitions are missing")
    require("extension" in defs, "IR schema has no extension definition")
    require("criticality" in json.dumps(schema, ensure_ascii=False), "IR schema has no extension criticality")

    open_definitions: list[str] = []
    for name, definition in defs.items():
        if isinstance(definition, dict) and "properties" in definition and definition.get("additionalProperties") is not False:
            open_definitions.append(str(name))
    require(not open_definitions, "IR schema has open typed definitions: " + ", ".join(sorted(open_definitions)))

    leaks = find_product_boundary_leaks(schema, "schema", scan_values=False)
    require(not leaks, "product-boundary concept leaked into schema: " + ", ".join(leaks[:5]))
    for token in (
        "sourceBytes",
        "sourceByteStore",
        "contentAddressedSource",
        "RecordAssertion",
        "EquivalenceCertificate",
        "LineageCertificate",
        "AccountingItem",
        "semanticEquivalence",
    ):
        require(token not in json.dumps(schema, ensure_ascii=False), f"forbidden schema token: {token}")

    return {"required_core_fields": len(REQUIRED_SCHEMA_FIELDS), "typed_definitions": len(defs)}


def check_examples() -> dict[str, int]:
    require(EXAMPLES_PATH.is_dir(), "examples directory is missing")
    paths = sorted(EXAMPLES_PATH.glob("*.json"))
    require(len(paths) >= 6, f"expected at least 6 JSON examples, got {len(paths)}")
    actual_names = {path.name for path in paths}
    missing_examples = sorted(REQUIRED_EXAMPLES - actual_names)
    require(not missing_examples, "required examples are missing: " + ", ".join(missing_examples))
    formats: set[str] = set()
    for path in paths:
        data = load_json(path)
        require(isinstance(data, dict), f"example is not an object: {path.name}")
        schema_ref = data.get("schema")
        source_format = data.get("sourceFormat")
        require(isinstance(schema_ref, dict) and schema_ref.get("name") == "fdir/document-form", f"example has wrong schema: {path.name}")
        require(isinstance(data.get("documentId"), str) and data["documentId"], f"example has no documentId: {path.name}")
        require(isinstance(source_format, dict) and isinstance(source_format.get("name"), str), f"example has no source format: {path.name}")
        require(isinstance(data.get("nodes"), list) and data["nodes"], f"example has no nodes: {path.name}")
        conversion = data.get("conversion")
        require(isinstance(conversion, dict) and isinstance(conversion.get("status"), str), f"example has no conversion report: {path.name}")
        formats.add(source_format["name"])
        leaks = find_product_boundary_leaks(data, f"example:{path.name}", scan_values=True)
        require(not leaks, "product-boundary concept leaked into example: " + ", ".join(leaks[:5]))

    require({"docx", "xlsx", "pdf", "markdown"}.issubset(formats), "examples do not cover DOCX, XLSX, PDF, and Markdown")
    return {"json_examples": len(paths), "source_formats": len(formats)}


def check_real_input_e2e_assets() -> dict[str, int]:
    """Ensure the release contains a real-input path for every required format."""

    required = {
        "tools/adapter_common.py",
        "tools/adapter_docx.py",
        "tools/adapter_xlsx.py",
        "tools/adapter_pdf.py",
        "tools/adapter_markdown.py",
        "tools/ir_validation.py",
        "tools/convert_document.py",
        "tools/generate_e2e_fixtures.py",
        "tools/run_e2e.py",
        "e2e/README.md",
        "e2e/fixtures/README.md",
    }
    missing = sorted(relative(ROOT / item) for item in required if not (ROOT / item).is_file())
    require(not missing, "real-input E2E assets are missing: " + ", ".join(missing))
    generator = (ROOT / "tools" / "generate_e2e_fixtures.py").read_text(encoding="utf-8")
    runner = (ROOT / "tools" / "run_e2e.py").read_text(encoding="utf-8")
    for phrase in ("write_zip", "pdf_bytes", "MARKDOWN"):
        require(phrase in generator, f"E2E fixture generator lacks real {phrase} implementation")
    for phrase in ("real-input", "evidence", "validate", "canonical", "query", "malformed", "unsupported", "resource-limit", "consumed"):
        require(phrase.casefold() in runner.casefold(), f"E2E runner lacks required check: {phrase}")
    return {"required_assets": len(required), "formats": 4, "e2e_issue": EXPECTED_E2E_ISSUE}


def check_documents() -> dict[str, int]:
    texts: list[str] = []
    for document in NORMATIVE_DOCUMENTS:
        path = ROOT / document
        require(path.is_file(), f"missing normative document: {document}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise GateError(f"document is not UTF-8: {document}") from exc
        require(content.strip(), f"normative document is empty: {document}")
        texts.append(content)

    all_text = "\n".join(texts)
    required_phrases = (
        "Parser / Adapter",
        "Document Form IR",
        "Semantic IR",
        "source map",
        "property bag",
    )
    for phrase in required_phrases:
        require(phrase.casefold() in all_text.casefold(), f"documentation is missing boundary phrase: {phrase}")

    boundary_terms = (
        r"semantic\s+IR",
        r"raw[- ]byte",
        r"source[- ]byte",
        r"(?:source|raw\s+byte)\s+store",
        r"forensic",
        r"accounting",
        r"lineage",
    )
    for term in boundary_terms:
        require(re.search(term, all_text, re.I), f"documentation is missing boundary term: {term}")

    # A boundary document may name an excluded concept, but it must also state
    # the direction or exclusion.  This prevents a positive product claim from
    # being hidden behind otherwise adequate documentation coverage.
    boundary_markers = (
        "downstream",
        "out of scope",
        "outside",
        "not ",
        "no ",
        "without",
        "excluded",
        "removed",
        "範囲外",
        "除外",
        "廃棄",
    )
    marked_lines = [line.casefold() for line in all_text.splitlines() if any(re.search(term, line, re.I) for term in boundary_terms)]
    require(any(any(marker in line for marker in boundary_markers) for line in marked_lines), "boundary terms are not accompanied by an exclusion or downstream marker")

    return {"normative_documents": len(NORMATIVE_DOCUMENTS), "boundary_marked_lines": len(marked_lines)}


def run_command(name: str, display_command: str, argv: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "command": display_command,
        "cwd": ".",
    }
    try:
        child_environment = os.environ.copy()
        # Runtime reports contain absolute paths under the Japanese workspace.
        # The Windows console default is often CP932, while this gate decodes
        # captured JSON as UTF-8; force the child Python process to use the
        # same encoding so paths cannot be silently replaced with U+FFFD.
        child_environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, *argv],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
            check=False,
        )
    except OSError as exc:
        result.update({"return_code": None, "stdout": "", "stderr": str(exc)})
        return result

    result.update(
        {
            "return_code": completed.returncode,
            "stdout": completed.stdout.replace("\r\n", "\n"),
            "stderr": completed.stderr.replace("\r\n", "\n"),
        }
    )
    return result


def check_runtime_evidence(commands: list[dict[str, Any]]) -> dict[str, int]:
    """Validate the content of the qualification reports produced this run."""

    by_name = {item.get("name"): item for item in commands if isinstance(item, dict)}

    def report(name: str) -> dict[str, Any]:
        item = by_name.get(name)
        require(isinstance(item, dict) and item.get("return_code") == 0, f"runtime command did not pass: {name}")
        try:
            value = json.loads(str(item.get("stdout", "")))
        except json.JSONDecodeError as exc:
            raise GateError(f"runtime report is not JSON: {name}: {exc}") from exc
        require(isinstance(value, dict), f"runtime report is not an object: {name}")
        return value

    mutation = report("mutation_qualification")
    corpus = report("independent_corpus")
    query = report("query_qualification")
    e2e = report("real_input_e2e")
    strict = report("strict_completion")
    require(mutation.get("status") == "passed" and mutation.get("survivors") == [] and mutation.get("killed") == mutation.get("total"), "mutation report is not fully green")
    require(corpus.get("status") == "passed" and len(corpus.get("cases", [])) >= 4, "independent corpus report is incomplete")
    check_source_closure_report(corpus, "independent corpus")
    require(query.get("status") == "passed" and query.get("parity", {}).get("status") == "passed" and query.get("unqueryableFacts") == [], "query report is not fully green")
    require(e2e.get("status") == "passed" and set(e2e.get("formats", [])) == {"docx", "xlsx", "pdf", "markdown"}, "real-input E2E report is not fully green")
    check_source_closure_report(e2e, "real-input E2E")
    require(strict.get("status") == "passed" and strict.get("blockers") == [], "strict completion report is not fully green")
    return {
        "mutation_cases": int(mutation.get("total", 0)),
        "independent_cases": len(corpus.get("cases", [])),
        "independent_negative_checks": len(corpus.get("negativeChecks", [])),
        "query_sources": len(query.get("sources", [])),
        "e2e_cases": len(e2e.get("cases", [])),
        "strict_issues": len(strict.get("issues", [])),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FDIR design and acceptance release gate.")
    parser.add_argument(
        "--summary",
        type=Path,
        metavar="PATH",
        help="also write the deterministic JSON summary to PATH (relative to the repository root)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []

    for name, display, command in (
        ("design_validation", "python tools/validate_design.py", ["tools/validate_design.py"]),
        ("acceptance_all", "python tools/run_acceptance.py --all", ["tools/run_acceptance.py", "--all"]),
        ("real_input_e2e", "python tools/run_e2e.py --all --json", ["tools/run_e2e.py", "--all", "--json"]),
        ("mutation_qualification", "python tools/mutation_qualification.py --json", ["tools/mutation_qualification.py", "--json"]),
        ("query_qualification", "python tools/query_qualification.py", ["tools/query_qualification.py"]),
        ("independent_corpus", "python tools/independent_corpus.py --json", ["tools/independent_corpus.py", "--json"]),
        ("strict_completion", "python tools/strict_completion_gate.py", ["tools/strict_completion_gate.py"]),
    ):
        command_result = run_command(name, display, command)
        commands.append(command_result)
        passed = command_result.get("return_code") == 0
        checks.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "details": {
                    "command": display,
                    "return_code": command_result.get("return_code"),
                },
            }
        )

    try:
        runtime_details = check_runtime_evidence(commands)
    except GateError as exc:
        checks.append({"name": "runtime_evidence", "status": "failed", "error": str(exc)})
        runtime_details = {"mutation_cases": 0, "independent_cases": 0, "independent_negative_checks": 0, "query_sources": 0, "e2e_cases": 0, "strict_issues": 0}
    else:
        checks.append({"name": "runtime_evidence", "status": "passed", "details": runtime_details})

    check_functions: tuple[tuple[str, Callable[[], dict[str, int]]], ...] = (
        ("design_catalog", check_design_catalog),
        ("issue_plan_and_github_map", check_issue_plan),
        ("phase2_contracts", check_phase2_contracts),
        ("release_claims", check_release_claims),
        ("traceability", check_traceability),
        ("schema", check_schema),
        ("examples", check_examples),
        ("real_input_e2e_assets", check_real_input_e2e_assets),
        ("documents_and_boundaries", check_documents),
    )
    for name, function in check_functions:
        try:
            details = function()
        except GateError as exc:
            checks.append({"name": name, "status": "failed", "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            checks.append({"name": name, "status": "failed", "error": f"unexpected {type(exc).__name__}: {exc}"})
        else:
            checks.append({"name": name, "status": "passed", "details": details})

    passed = all(check["status"] == "passed" for check in checks)
    summary: dict[str, Any] = {
        "schema": "fdir/release-gate-summary",
        "version": "1.0.0",
        "status": "passed" if passed else "failed",
        "exit_code": 0 if passed else 1,
        "reproducibility": {
            "repository_root": ".",
            "python_command": "python",
            "logs_are_normalized": True,
        },
        "counts": {
            "requirements": EXPECTED_REQUIREMENTS,
            "acceptance_families": EXPECTED_FAMILIES,
            "acceptance_cases": EXPECTED_CASES,
            "leaf_issues": EXPECTED_LEAF_ISSUES,
            "umbrella_issue": EXPECTED_UMBRELLA_ISSUE,
            "leaf_issue_first": min(EXPECTED_LEAF_ISSUE_RANGE),
            "leaf_issue_last": max(EXPECTED_LEAF_ISSUE_RANGE),
            "e2e_issue": EXPECTED_E2E_ISSUE,
            "phase2_issues": len(list(range(69, 85)) + [85, 86]),
            "phase2_issue_first": 69,
            "phase2_issue_last": 86,
            "phase2_duplicate_issue": 85,
            **runtime_details,
        },
        "checks": checks,
        "commands": commands,
    }

    if args.summary is not None:
        summary_path = args.summary if args.summary.is_absolute() else ROOT / args.summary
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            summary["status"] = "failed"
            summary["exit_code"] = 1
            summary["checks"].append({"name": "summary_output", "status": "failed", "error": str(exc)})
            passed = False

    rendered_summary = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # CI is UTF-8, but local Windows shells can still expose a legacy code
    # page.  Writing the JSON bytes directly keeps the summary valid and
    # reproducible regardless of the console locale.
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(rendered_summary.encode("utf-8"))
    else:  # pragma: no cover - useful when main() is called with StringIO
        sys.stdout.write(rendered_summary)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
