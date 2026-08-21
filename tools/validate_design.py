"""Fail-closed validation for the Document Form IR design artifacts.

This validator is intentionally standard-library-only. It validates the design
authority and its traceability; it is not a document converter.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQ_PATH = ROOT / "machine" / "requirements.json"
TEST_PATH = ROOT / "machine" / "acceptance-tests.json"
ISSUE_PATH = ROOT / "machine" / "issue-plan.json"
GITHUB_MAP_PATH = ROOT / "machine" / "github-issue-map.json"
RELEASE_GATE_PATH = ROOT / "machine" / "release-gate.json"
AUDIT_RECOVERY_PATH = ROOT / "machine" / "audit-recovery-plan.json"
QUALIFICATION_CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"


class DesignError(Exception):
    pass


def load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DesignError(f"missing design artifact: {path.relative_to(ROOT)}") from exc
    except json.JSONDecodeError as exc:
        raise DesignError(f"invalid JSON in {path.relative_to(ROOT)}: {exc}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DesignError(message)


def ids(items: list[dict], field: str, label: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in items:
        value = item.get(field)
        require(isinstance(value, str) and value, f"{label} has an invalid {field}")
        require(value not in result, f"duplicate {label} id: {value}")
        result[value] = item
    return result


def validate_requirements(requirements: dict, issue_map: dict, families: dict) -> None:
    entries = requirements.get("requirements")
    require(isinstance(entries, list), "requirements must be an array")
    expected_count = requirements.get("expectedRequirementCount", 134)
    require(isinstance(expected_count, int) and len(entries) == expected_count,
            f"requirement count mismatch: expected {expected_count} got {len(entries)}")
    require(len(entries) >= requirements.get("minimumRequirementCount", 120), "requirement count is below the restored baseline")
    req_map = ids(entries, "id", "requirement")
    for req in entries:
        require(req.get("priority") == "must", f"requirement is not must-level: {req.get('id')}")
        require(isinstance(req.get("statement"), str) and req["statement"].strip(), f"requirement has no statement: {req.get('id')}")
        owner = req.get("ownerIssue")
        require(owner in issue_map, f"requirement owner does not exist: {req.get('id')} -> {owner}")
        tests = req.get("acceptanceTests")
        require(isinstance(tests, list) and tests, f"requirement has no acceptance test: {req.get('id')}")
        require(len(tests) == 1, f"requirement must map to exactly one acceptance test: {req.get('id')}")
        for test_id in tests:
            require(isinstance(test_id, str) and re.fullmatch(r"AT-[A-Z]+-\d{3}", test_id),
                    f"requirement has an invalid acceptance test id: {req.get('id')} -> {test_id}")
            prefix = test_id.rsplit("-", 1)[0]
            family = families.get(prefix)
            require(family is not None, f"acceptance family does not exist for {req.get('id')}: {test_id}")
            require(test_id.startswith(family["id"] + "-"), f"acceptance test does not match family: {test_id}")
            expected_prefix = family["requirementPrefix"]
            require(req["id"].startswith(expected_prefix), f"acceptance family does not cover requirement: {req['id']} -> {test_id}")
    return None


def validate_families(tests: dict, requirement_map: dict) -> dict[str, dict]:
    families = tests.get("families")
    require(isinstance(families, list), "acceptance test families must be an array")
    require(len(families) == 16, f"acceptance family count mismatch: expected 16 got {len(families)}")
    family_map = ids(families, "id", "acceptance family")
    for family in families:
        require(isinstance(family.get("count"), int) and family["count"] > 0, f"invalid test count: {family.get('id')}")
        require(isinstance(family.get("requirementPrefix"), str), f"test family has no requirement prefix: {family.get('id')}")
        require(isinstance(family.get("command"), str) and family["command"].strip(), f"test family has no command: {family.get('id')}")
        require(isinstance(family.get("expected"), str) and family["expected"].strip(), f"test family has no expected result: {family.get('id')}")
        matching = [key for key in requirement_map if key.startswith(family["requirementPrefix"])]
        require(len(matching) == family["count"], f"test family count mismatch: {family['id']} expected {family['count']} got {len(matching)}")
    return family_map


def validate_issues(issue_plan: dict, requirements: list[dict], families: dict[str, dict]) -> dict[str, dict]:
    entries = issue_plan.get("issues")
    require(isinstance(entries, list), "issues must be an array")
    issue_map = ids(entries, "id", "issue")
    leaf_ids = {f"DFIR-I-{number:03d}" for number in range(1, 21)}
    require(set(issue_map) == {"DFIR-I-000", *leaf_ids}, "issue plan must contain exactly the umbrella and 20 leaf issues")
    require(sum(issue.get("kind") != "umbrella" for issue in entries) == 20, "issue plan must contain 20 leaf issues")
    require(issue_plan.get("policy", {}).get("targetLeafIssueCount") == 20, "issue plan target leaf count must be 20")
    for issue in entries:
        for dependency in issue.get("dependsOn", []):
            require(dependency in issue_map, f"issue dependency does not exist: {issue['id']} -> {dependency}")
        for family_id in issue.get("acceptanceFamilies", []):
            require(family_id in families, f"issue acceptance family does not exist: {issue['id']} -> {family_id}")
        require(issue.get("paths"), f"issue has no owned paths: {issue['id']}")
        require(issue.get("deliverables"), f"issue has no deliverables: {issue['id']}")
        require(isinstance(issue.get("requirementPrefixes"), list), f"issue has invalid requirement prefixes: {issue['id']}")
        require(isinstance(issue.get("acceptanceFamilies"), list), f"issue has invalid acceptance families: {issue['id']}")
        for relative in issue["paths"]:
            require(isinstance(relative, str) and relative and (ROOT / relative).exists(),
                    f"issue owned path is missing: {issue['id']} -> {relative}")
    for req in requirements:
        owner = issue_map[req["ownerIssue"]]
        prefixes = owner.get("requirementPrefixes", [])
        require(any(req["id"].startswith(prefix) for prefix in prefixes), f"owner issue does not declare requirement prefix: {req['id']} -> {req['ownerIssue']}")
    return issue_map


def validate_github_issue_map(github_map: dict, issue_plan: dict[str, dict]) -> None:
    require(github_map.get("repository") == "horiyamayoh/fdir", "GitHub issue map has the wrong repository")
    umbrella = github_map.get("umbrella")
    require(isinstance(umbrella, dict) and umbrella.get("key") == "DFIR-I-000" and umbrella.get("issueNumber") == 47,
            "GitHub issue map must map DFIR-I-000 to issue #47")
    entries = github_map.get("issues")
    require(isinstance(entries, list) and len(entries) == 20, "GitHub issue map must contain 20 leaf issues")
    seen_keys: set[str] = set()
    seen_numbers: set[int] = set()
    for entry in entries:
        require(isinstance(entry, dict), "GitHub issue map contains a non-object entry")
        key = entry.get("key")
        number = entry.get("issueNumber")
        require(isinstance(key, str) and re.fullmatch(r"DFIR-I-0(?:0[1-9]|1[0-9]|20)", key), f"invalid GitHub issue key: {key}")
        require(isinstance(number, int) and 48 <= number <= 67, f"invalid GitHub issue number: {number}")
        require(key not in seen_keys and number not in seen_numbers, f"duplicate GitHub issue mapping: {key} / {number}")
        require(entry.get("url") == f"https://github.com/horiyamayoh/fdir/issues/{number}", f"invalid issue URL: {key}")
        require(key in issue_plan, f"GitHub issue mapping is not in issue plan: {key}")
        seen_keys.add(key)
        seen_numbers.add(number)
    require(seen_keys == {f"DFIR-I-{number:03d}" for number in range(1, 21)}, "GitHub issue map keys are incomplete")
    require(seen_numbers == set(range(48, 68)), "GitHub issue map numbers are incomplete")


def validate_release_gate_manifest(manifest: dict) -> None:
    expected = manifest.get("expected")
    require(isinstance(expected, dict), "release gate manifest has no expected counts")
    for key, value in {"requirements": 134, "acceptanceFamilies": 16, "acceptanceCases": 134, "leafIssues": 20, "e2eIssue": 68}.items():
        require(expected.get(key) == value, f"release gate expected count mismatch: {key}")
    commands = manifest.get("commands")
    require(isinstance(commands, list) and "python tools/validate_design.py" in commands and "python tools/run_acceptance.py --all" in commands and "python tools/run_e2e.py --all" in commands and "python tools/mutation_qualification.py --json" in commands and "python tools/query_qualification.py" in commands and "python tools/independent_corpus.py --json" in commands and "python tools/strict_completion_gate.py" in commands and "python tools/validate_model_contract.py --check" in commands and "python tools/test_evidence_integrity.py --all" in commands and "python tools/validate_qualification_bundle.py --schema-only" in commands,
            "release gate commands are incomplete")
    for relative in manifest.get("requiredAdapters", []):
        require((ROOT / relative).is_file(), f"release gate adapter is missing: {relative}")
    e2e_issue = manifest.get("e2eIssue")
    require(isinstance(e2e_issue, dict) and e2e_issue.get("number") == 68 and e2e_issue.get("releaseBlocker") is True,
            "release gate E2E issue metadata is incomplete")
    require(set(manifest.get("requiredFormats", [])) == {"docx", "xlsx", "pdf", "markdown"}, "release gate formats are incomplete")
    for relative in manifest.get("requiredExamples", []):
        require((ROOT / relative).is_file(), f"release gate example is missing: {relative}")
    for relative in ("machine/phase2-issue-plan.json", "machine/capability-profile.json", "machine/reference-registry.json", "machine/extension-registry.json", "machine/canonicalization.json", "machine/query-contract.json", "machine/release-claim-manifest.json", "machine/strict-completion-contract.json", "machine/audit-recovery-plan.json", "machine/qualification-contract.json", "machine/recovery-report-contract.json", "machine/model-contract.json", "machine/defect-injection-contract.json", "schemas/qualification-evidence.schema.json", "schemas/qualification-issue-91-report.schema.json", "schemas/qualification-issue-104-corpus.schema.json", "schemas/qualification-issue-104-report.schema.json", "schemas/qualification-issue-104-summary.schema.json", "schemas/qualification-issue-105-report.schema.json", "schemas/github-issue-state.schema.json", "schemas/release-attestation.schema.json", "requirements-qualification.txt", "e2e/corpus/manifest.json", "tools/mutation_qualification.py", "tools/query_qualification.py", "tools/generate_query_contract.py", "tools/independent_index.py", "tools/test_independent_index.py", "tools/test_query_surface.py", "tools/qualification_evidence.py", "tools/validate_qualification_contract.py", "tools/test_qualification_contract.py", "tools/qualification_issue103.py", "tools/test_qualification_issue103.py", "machine/qualification-issue-103-corpus.json", "tools/independent_corpus.py", "tools/strict_completion_gate.py", "tools/build_qualification_bundle.py", "tools/validate_qualification_bundle.py", "tools/test_evidence_integrity.py", "tools/generate_model_contract.py", "tools/validate_model_contract.py", "tools/run_defect_injection_campaign.py", "tools/github_issue_state.py", "tools/release_attestation.py", "tools/qualification_issue90.py", "tools/test_qualification_issue90.py", "machine/qualification-issue-90-corpus.json", "tools/occurrence_qualification.py", "tools/qualification_issue91.py", "tools/test_qualification_issue91.py", "machine/qualification-issue-92-corpus.json", "tools/qualification_issue92.py", "tools/test_qualification_issue92.py", "machine/qualification-issue-93-corpus.json", "tools/qualification_issue93.py", "tools/test_qualification_issue93.py", "machine/qualification-issue-94-corpus.json", "tools/qualification_issue94.py", "tools/test_qualification_issue94.py", "machine/qualification-issue-95-corpus.json", "tools/qualification_issue95.py", "tools/test_qualification_issue95.py", "machine/qualification-issue-96-corpus.json", "tools/qualification_issue96.py", "tools/test_qualification_issue96.py", "machine/qualification-issue-97-corpus.json", "tools/qualification_issue97.py", "tools/test_qualification_issue97.py", "machine/qualification-issue-98-corpus.json", "tools/canonical_issue98_node.mjs", "tools/qualification_issue98.py", "tools/test_qualification_issue98.py", "machine/qualification-issue-99-corpus.json", "tools/qualification_issue99.py", "tools/test_qualification_issue99.py", "machine/qualification-issue-100-corpus.json", "tools/qualification_issue100.py", "tools/test_qualification_issue100.py", "machine/qualification-issue-101-corpus.json", "tools/qualification_issue101.py", "tools/test_qualification_issue101.py", "machine/qualification-issue-102-corpus.json", "tools/qualification_issue102.py", "tools/test_qualification_issue102.py", "machine/qualification-issue-104-corpus.json", "tools/qualification_issue104.py", "tools/test_qualification_issue104.py", "machine/qualification-issue-105-corpus.json", "tools/qualification_issue105.py", "tools/test_qualification_issue105.py"):
        require((ROOT / relative).is_file(), f"phase2 release artifact is missing: {relative}")
    require(len(manifest.get("checks", [])) >= 13 and {"real-input-e2e", "audit-recovery", "audit-recovery-release-boundary", "qualification-evidence", "model-contract", "defect-injection"}.issubset(set(manifest.get("checks", []))), "release gate checks are incomplete")


def validate_audit_recovery_plan(plan: dict) -> None:
    """Validate the recovery DAG and its explicit release-boundary state."""

    require(plan.get("schema") == "fdir/audit-recovery-plan", "audit recovery plan schema is wrong")
    require(plan.get("version") == "1.0.0", "audit recovery plan version is not pinned")
    require(plan.get("repository") == "horiyamayoh/fdir", "audit recovery plan repository is wrong")
    require(plan.get("umbrellaIssue") == 87 and isinstance(plan.get("releaseBlocked"), bool),
            "audit recovery plan must bind issue #87 and a boolean release state")
    audited_closed = plan.get("auditedClosedIssues")
    audited_open = plan.get("auditedOpenProgramIssues")
    require(isinstance(audited_closed, list) and isinstance(audited_open, list),
            "audit recovery issue sets are missing")
    require(all(isinstance(number, int) and number > 0 for number in audited_closed + audited_open),
            "audit recovery issue sets contain an invalid number")
    children = plan.get("children")
    require(isinstance(children, list), "audit recovery children are missing")
    child_by_number: dict[int, dict] = {}
    for child in children:
        require(isinstance(child, dict), "audit recovery child is not an object")
        number = child.get("issueNumber")
        require(isinstance(number, int) and 88 <= number <= 105,
                f"audit recovery child number is outside #88-#105: {number}")
        require(number not in child_by_number, f"duplicate audit recovery child: #{number}")
        require(isinstance(child.get("title"), str) and child["title"].strip(),
                f"audit recovery child has no title: #{number}")
        require(isinstance(child.get("dependsOn"), list) and isinstance(child.get("auditsClaimsFrom"), list),
                f"audit recovery child metadata is incomplete: #{number}")
        child_by_number[number] = child
    require(set(child_by_number) == set(range(88, 106)),
            "audit recovery plan must cover every child issue #88-#105 exactly once")
    for number, child in child_by_number.items():
        for dependency in child["dependsOn"]:
            require(isinstance(dependency, int) and dependency in child_by_number and dependency != number,
                    f"audit recovery dependency is not a child or is self-referential: #{number} -> {dependency}")
        for source_issue in child["auditsClaimsFrom"]:
            require(isinstance(source_issue, int) and source_issue > 0,
                    f"audit recovery claim source is invalid: #{number} -> {source_issue}")
        require(child.get("statusSource") == "github-issue-api",
                f"audit recovery child must use live GitHub state: #{number}")
        if plan.get("releaseBlocked") is False:
            require(child.get("status") == "completed",
                    f"release-ready recovery child is not completed: #{number}")
            require(child.get("evidenceIds") == [f"issue-{number}-" + {
                88: "qualification-contract", 89: "defect-injection", 90: "model-contract",
                91: "occurrence-accounting", 92: "exact-values", 93: "style-provenance",
                94: "geometry-order", 95: "topology", 96: "relationship-closure",
                97: "extension-registry", 98: "canonical-identity", 99: "docx-profile",
                100: "xlsx-profile", 101: "pdf-profile", 102: "markdown-profile",
                103: "query-index", 104: "independent-corpus", 105: "release-quality",
            }[number]], f"release-ready recovery child evidence binding is invalid: #{number}")

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(number: int) -> None:
        if number in visiting:
            raise DesignError(f"audit recovery dependency cycle includes #{number}")
        if number in visited:
            return
        visiting.add(number)
        for dependency in child_by_number[number]["dependsOn"]:
            visit(dependency)
        visiting.remove(number)
        visited.add(number)

    for number in child_by_number:
        visit(number)
    close_policy = plan.get("closePolicy")
    require(isinstance(close_policy, dict), "audit recovery close policy is missing")
    for key in ("forbidStringOnlyEvidence", "forbidClosedStateAsEvidence", "forbidReleaseClaimsUntilChildrenComplete"):
        require(close_policy.get(key) is True, f"audit recovery close policy is weak: {key}")
    forbidden_claims = plan.get("forbiddenReleaseClaims")
    require(isinstance(forbidden_claims, list) and forbidden_claims and all(isinstance(item, str) and item for item in forbidden_claims),
            "audit recovery forbidden release claims are missing")
    if plan.get("releaseBlocked") is False:
        qualification = plan.get("qualificationEvidence")
        require(isinstance(qualification, dict), "release-ready audit plan has no qualification evidence binding")
        require(qualification.get("status") == "passed", "release-ready audit qualification is not passed")
        require(qualification.get("manifestPath") == "qualification/<source-sha>/manifest.json",
                "audit qualification manifest path must be source-SHA templated")
        require(qualification.get("sourceShaPolicy") == "exact-bundle-manifest",
                "audit qualification source SHA policy is not exact-bundle-manifest")
        contract = load(QUALIFICATION_CONTRACT_PATH)
        expected_ids = set(contract.get("scope", {}).get("requiredEvidenceIds", [])) if isinstance(contract, dict) else set()
        require(set(qualification.get("requiredEvidenceIds", [])) == expected_ids,
                "audit qualification evidence IDs do not match the contract")


def validate_release_claim_boundary(claims: dict, recovery: dict) -> None:
    """Keep release claims synchronized with the recovery evidence boundary."""

    require(isinstance(recovery.get("releaseBlocked"), bool), "release claim boundary has no boolean audit state")
    release = claims.get("release")
    require(isinstance(release, dict), "release claim manifest has no release state")
    if recovery.get("releaseBlocked") is True:
        require(release.get("releaseBlocked") is True and release.get("status") == "release-blocked",
                "blocked audit must publish a release-blocked claim")
    else:
        require(release.get("releaseBlocked") is False and release.get("status") == "release-ready",
                "release-ready audit must publish a release-ready claim")
        binding = release.get("qualificationBinding")
        require(isinstance(binding, dict) and binding.get("status") == "passed",
                "release-ready claim has no passed qualification binding")
        require(binding.get("manifestPath") == "qualification/<source-sha>/manifest.json",
                "release qualification manifest path must be source-SHA templated")
        require(binding.get("sourceShaPolicy") == "exact-bundle-manifest",
                "release qualification source SHA policy is not exact-bundle-manifest")
        contract = load(QUALIFICATION_CONTRACT_PATH)
        expected_ids = set(contract.get("scope", {}).get("requiredEvidenceIds", [])) if isinstance(contract, dict) else set()
        require(set(binding.get("requiredEvidenceIds", [])) == expected_ids,
                "release qualification evidence IDs do not match the contract")
    forbidden = {str(item).casefold() for item in recovery.get("forbiddenReleaseClaims", [])}
    claim_texts: list[str] = []
    for section in (claims.get("capabilityClaims", []), claims.get("issueClaims", [])):
        if isinstance(section, list):
            claim_texts.extend(str(item.get("claim", "")) for item in section if isinstance(item, dict))
    for text in claim_texts:
        for phrase in forbidden:
            # Match a claim term, not a longer word such as
            # ``completeness`` when the forbidden term is ``complete``.
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(phrase)}(?![A-Za-z0-9_])"
            require(re.search(pattern, text.casefold()) is None,
                    f"blocked release claim remains published: {phrase}")


def validate_schema(schema: dict) -> None:
    require(schema.get("type") == "object", "IR schema root must be an object")
    require("documentId" in schema.get("required", []), "IR schema must require documentId")
    require("conversion" in schema.get("required", []), "IR schema must require conversion")
    require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", "IR schema draft is not pinned")
    require(isinstance(schema.get("$id"), str) and schema["$id"].endswith("/1.0.0"), "IR schema version is not pinned")
    required_properties = {"schema", "documentId", "sourceFormat", "rootNodeId", "nodes", "conversion"}
    require(required_properties.issubset(set(schema.get("required", []))), "IR schema required authority fields are incomplete")
    definitions = schema.get("$defs")
    require(isinstance(definitions, dict), "IR schema has no typed definitions")
    for name in ["node", "text", "style", "layout", "geometry", "relation", "order", "observation", "extension", "diagnostic", "conversionReport"]:
        require(name in definitions, f"IR schema is missing typed definition: {name}")
    for name, definition in definitions.items():
        if isinstance(definition, dict) and definition.get("type") == "object" and name not in {"extension", "styleProperties"}:
            require(definition.get("additionalProperties") is False, f"typed definition is an open property bag: {name}")
    raw = json.dumps(schema, ensure_ascii=False)
    forbidden = [
        "sourceBytes",
        "sourceByteStore",
        "contentAddressedSource",
        "RecordAssertion",
        "EquivalenceCertificate",
        "LineageCertificate",
        "AccountingItem",
        "predicate",
        "semanticEquivalence",
    ]
    for token in forbidden:
        require(token not in raw, f"forbidden concept leaked into schema: {token}")
    require("extension" in raw.lower(), "schema has no extension envelope")
    require("criticality" in raw, "schema has no extension criticality")


def validate_examples() -> None:
    example_dir = ROOT / "examples"
    examples = sorted(example_dir.glob("*.json"))
    require(len(examples) >= 6, "expected concrete IR examples are missing")
    for path in examples:
        data = load(path)
        require(isinstance(data, dict), f"example is not an object: {path.name}")
        require(data.get("schema", {}).get("name") == "fdir/document-form", f"example has wrong schema: {path.name}")
        require(isinstance(data.get("documentId"), str), f"example has no documentId: {path.name}")
        require(isinstance(data.get("nodes"), list), f"example has no nodes array: {path.name}")
        require(isinstance(data.get("conversion"), dict), f"example has no conversion report: {path.name}")
        encoded = json.dumps(data, ensure_ascii=False)
        require("predicate" not in encoded and "semanticEquivalence" not in encoded, f"semantic predicate leaked into example: {path.name}")


def validate_docs() -> None:
    required_docs = [
        "README.md",
        "docs/01-product-definition.md",
        "docs/02-architecture.md",
        "docs/03-logical-model.md",
        "docs/04-format-mapping.md",
        "docs/05-serialization-and-extensions.md",
        "docs/06-interfaces-and-implementation.md",
        "docs/07-verification-and-issues.md",
        "docs/08-review-and-reset.md",
    ]
    for relative in required_docs:
        require((ROOT / relative).is_file(), f"missing normative document: {relative}")
    all_text = "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in required_docs)
    for phrase in ["Parser / Adapter", "Document Form IR", "Semantic IR", "source map", "property bag"]:
        require(phrase.lower() in all_text.lower(), f"documentation is missing boundary phrase: {phrase}")
    for relative in [
        "tools/run_acceptance.py",
        "tools/release_gate.py",
        "tools/canonicalize_ir.py",
        "tools/query_ir.py",
        "tools/convert_document.py",
        "tools/run_e2e.py",
        "tools/generate_e2e_fixtures.py",
    ]:
        require((ROOT / relative).is_file(), f"executable release artifact is missing: {relative}")


def validate_forbidden_legacy_paths() -> None:
    forbidden_paths = [
        "crates/fdir-semantics",
        "crates/fdir-accounting",
        "crates/fdir-storage",
        "machine/logical-model.yaml",
        "machine/requirements.yaml",
        "machine/acceptance-tests.yaml",
        "release/claim-manifest.yaml",
        "schemas/context.jsonld",
    ]
    present = [path for path in forbidden_paths if (ROOT / path).exists()]
    require(not present, "legacy design paths still present: " + ", ".join(present))


def main() -> int:
    try:
        requirements = load(REQ_PATH)
        tests = load(TEST_PATH)
        issue_plan = load(ISSUE_PATH)
        github_map = load(GITHUB_MAP_PATH)
        release_manifest = load(RELEASE_GATE_PATH)
        release_claims = load(ROOT / "machine" / "release-claim-manifest.json")
        audit_recovery = load(AUDIT_RECOVERY_PATH)
        schema = load(SCHEMA_PATH)
        require(isinstance(requirements, dict), "requirements root must be an object")
        require(isinstance(tests, dict), "tests root must be an object")
        require(isinstance(issue_plan, dict), "issue plan root must be an object")
        require(isinstance(github_map, dict), "GitHub issue map root must be an object")
        require(isinstance(release_manifest, dict), "release gate manifest root must be an object")
        require(isinstance(release_claims, dict), "release claim manifest root must be an object")
        require(isinstance(audit_recovery, dict), "audit recovery plan root must be an object")
        require(isinstance(schema, dict), "schema root must be an object")
        requirement_map = ids(requirements.get("requirements", []), "id", "requirement")
        family_map = validate_families(tests, requirement_map)
        issue_map = validate_issues(issue_plan, requirements["requirements"], family_map)
        validate_github_issue_map(github_map, issue_map)
        validate_release_gate_manifest(release_manifest)
        validate_audit_recovery_plan(audit_recovery)
        validate_release_claim_boundary(release_claims, audit_recovery)
        validate_requirements(requirements, issue_map, family_map)
        validate_schema(schema)
        validate_examples()
        validate_docs()
        validate_forbidden_legacy_paths()
    except DesignError as exc:
        print(f"DESIGN INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"design valid: {len(requirement_map)} requirements, {len(family_map)} acceptance families, {len(issue_map)} issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
