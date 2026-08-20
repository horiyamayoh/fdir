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
    require(len(entries) >= requirements.get("minimumRequirementCount", 120), "requirement count is below the restored baseline")
    req_map = ids(entries, "id", "requirement")
    for req in entries:
        require(req.get("priority") == "must", f"requirement is not must-level: {req.get('id')}")
        require(isinstance(req.get("statement"), str) and req["statement"].strip(), f"requirement has no statement: {req.get('id')}")
        owner = req.get("ownerIssue")
        require(owner in issue_map, f"requirement owner does not exist: {req.get('id')} -> {owner}")
        tests = req.get("acceptanceTests")
        require(isinstance(tests, list) and tests, f"requirement has no acceptance test: {req.get('id')}")
        for test_id in tests:
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
    family_map = ids(families, "id", "acceptance family")
    for family in families:
        require(isinstance(family.get("count"), int) and family["count"] > 0, f"invalid test count: {family.get('id')}")
        require(isinstance(family.get("requirementPrefix"), str), f"test family has no requirement prefix: {family.get('id')}")
        matching = [key for key in requirement_map if key.startswith(family["requirementPrefix"])]
        require(len(matching) == family["count"], f"test family count mismatch: {family['id']} expected {family['count']} got {len(matching)}")
    return family_map


def validate_issues(issue_plan: dict, requirements: list[dict], families: dict[str, dict]) -> dict[str, dict]:
    entries = issue_plan.get("issues")
    require(isinstance(entries, list), "issues must be an array")
    issue_map = ids(entries, "id", "issue")
    require(len(issue_map) >= issue_plan.get("policy", {}).get("targetLeafIssueCount", 20), "issue plan is too small")
    for issue in entries:
        for dependency in issue.get("dependsOn", []):
            require(dependency in issue_map, f"issue dependency does not exist: {issue['id']} -> {dependency}")
        for family_id in issue.get("acceptanceFamilies", []):
            require(family_id in families, f"issue acceptance family does not exist: {issue['id']} -> {family_id}")
        require(issue.get("paths"), f"issue has no owned paths: {issue['id']}")
        require(issue.get("deliverables"), f"issue has no deliverables: {issue['id']}")
    for req in requirements:
        owner = issue_map[req["ownerIssue"]]
        prefixes = owner.get("requirementPrefixes", [])
        require(any(req["id"].startswith(prefix) for prefix in prefixes), f"owner issue does not declare requirement prefix: {req['id']} -> {req['ownerIssue']}")
    return issue_map


def validate_schema(schema: dict) -> None:
    require(schema.get("type") == "object", "IR schema root must be an object")
    require("documentId" in schema.get("required", []), "IR schema must require documentId")
    require("conversion" in schema.get("required", []), "IR schema must require conversion")
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
        schema = load(SCHEMA_PATH)
        require(isinstance(requirements, dict), "requirements root must be an object")
        require(isinstance(tests, dict), "tests root must be an object")
        require(isinstance(issue_plan, dict), "issue plan root must be an object")
        require(isinstance(schema, dict), "schema root must be an object")
        requirement_map = ids(requirements.get("requirements", []), "id", "requirement")
        family_map = validate_families(tests, requirement_map)
        issue_map = validate_issues(issue_plan, requirements["requirements"], family_map)
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
