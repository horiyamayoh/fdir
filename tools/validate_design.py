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
    require(isinstance(commands, list) and "python tools/validate_design.py" in commands and "python tools/run_acceptance.py --all" in commands and "python tools/run_e2e.py --all" in commands,
            "release gate commands are incomplete")
    for relative in manifest.get("requiredAdapters", []):
        require((ROOT / relative).is_file(), f"release gate adapter is missing: {relative}")
    e2e_issue = manifest.get("e2eIssue")
    require(isinstance(e2e_issue, dict) and e2e_issue.get("number") == 68 and e2e_issue.get("releaseBlocker") is True,
            "release gate E2E issue metadata is incomplete")
    require(set(manifest.get("requiredFormats", [])) == {"docx", "xlsx", "pdf", "markdown"}, "release gate formats are incomplete")
    for relative in manifest.get("requiredExamples", []):
        require((ROOT / relative).is_file(), f"release gate example is missing: {relative}")
    require(len(manifest.get("checks", [])) >= 9 and "real-input-e2e" in manifest.get("checks", []), "release gate checks are incomplete")


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
        schema = load(SCHEMA_PATH)
        require(isinstance(requirements, dict), "requirements root must be an object")
        require(isinstance(tests, dict), "tests root must be an object")
        require(isinstance(issue_plan, dict), "issue plan root must be an object")
        require(isinstance(github_map, dict), "GitHub issue map root must be an object")
        require(isinstance(release_manifest, dict), "release gate manifest root must be an object")
        require(isinstance(schema, dict), "schema root must be an object")
        requirement_map = ids(requirements.get("requirements", []), "id", "requirement")
        family_map = validate_families(tests, requirement_map)
        issue_map = validate_issues(issue_plan, requirements["requirements"], family_map)
        validate_github_issue_map(github_map, issue_map)
        validate_release_gate_manifest(release_manifest)
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
