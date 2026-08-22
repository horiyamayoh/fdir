"""Validate the product design contract used by local regression tests.

It validates only the product requirements, acceptance matrix, schema,
examples, and executable helpers.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQ_PATH = ROOT / "machine" / "requirements.json"
TEST_PATH = ROOT / "machine" / "acceptance-tests.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"


class DesignError(Exception):
    """A product contract violation."""


def load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DesignError(f"missing product artifact: {path.relative_to(ROOT)}") from exc
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


def validate_families(tests: dict, requirements: dict[str, dict]) -> dict[str, dict]:
    families = tests.get("families")
    require(isinstance(families, list), "acceptance test families must be an array")
    require(len(families) == 16, f"acceptance family count mismatch: expected 16 got {len(families)}")
    family_map = ids(families, "id", "acceptance family")
    for family in families:
        family_id = family["id"]
        count = family.get("count")
        require(isinstance(count, int) and count > 0, f"invalid test count: {family_id}")
        prefix = family.get("requirementPrefix")
        require(isinstance(prefix, str) and prefix, f"test family has no requirement prefix: {family_id}")
        require(isinstance(family.get("command"), str) and family["command"].strip(), f"test family has no command: {family_id}")
        require(isinstance(family.get("expected"), str) and family["expected"].strip(), f"test family has no expected result: {family_id}")
        matching = [key for key in requirements if key.startswith(prefix)]
        require(len(matching) == count, f"test family count mismatch: {family_id} expected {count} got {len(matching)}")
    return family_map


def validate_requirements(requirements: dict, families: dict[str, dict]) -> dict[str, dict]:
    entries = requirements.get("requirements")
    require(isinstance(entries, list), "requirements must be an array")
    expected = requirements.get("expectedRequirementCount")
    require(isinstance(expected, int) and len(entries) == expected, f"requirement count mismatch: expected {expected} got {len(entries)}")
    minimum = requirements.get("minimumRequirementCount", expected)
    require(isinstance(minimum, int) and len(entries) >= minimum, "requirement count is below the product baseline")
    requirement_map = ids(entries, "id", "requirement")
    if not families:
        return requirement_map
    for requirement in entries:
        identifier = requirement["id"]
        require(requirement.get("priority") == "must", f"requirement is not must-level: {identifier}")
        require(isinstance(requirement.get("statement"), str) and requirement["statement"].strip(), f"requirement has no statement: {identifier}")
        tests = requirement.get("acceptanceTests")
        require(isinstance(tests, list) and len(tests) == 1, f"requirement must map to one acceptance test: {identifier}")
        test_id = tests[0]
        require(isinstance(test_id, str) and re.fullmatch(r"AT-[A-Z]+-\d{3}", test_id), f"invalid acceptance test id: {identifier} -> {test_id}")
        family_id = test_id.rsplit("-", 1)[0]
        family = families.get(family_id)
        require(family is not None, f"acceptance family does not exist: {identifier} -> {test_id}")
        require(test_id.startswith(family["id"] + "-"), f"acceptance test does not match family: {identifier} -> {test_id}")
        require(identifier.startswith(family["requirementPrefix"]), f"acceptance family does not cover requirement: {identifier} -> {test_id}")
    return requirement_map


def validate_schema(schema: dict) -> None:
    require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", "IR schema draft is not pinned")
    require(isinstance(schema.get("$id"), str) and schema["$id"].endswith("/1.0.0"), "IR schema version is not pinned")
    required = set(schema.get("required", []))
    require({"schema", "documentId", "sourceFormat", "rootNodeId", "nodes", "conversion"}.issubset(required), "IR schema authority fields are incomplete")
    definitions = schema.get("$defs")
    require(isinstance(definitions, dict), "IR schema has no typed definitions")
    for name in ("node", "text", "style", "layout", "geometry", "relation", "order", "observation", "extension", "diagnostic", "conversionReport"):
        require(name in definitions, f"IR schema is missing typed definition: {name}")
    for name, definition in definitions.items():
        if isinstance(definition, dict) and definition.get("type") == "object" and name not in {"extension", "styleProperties", "extensionPayload", "opaqueExtensionPayloadObject"}:
            require(definition.get("additionalProperties") is False, f"typed definition is open: {name}")
    raw = json.dumps(schema, ensure_ascii=False)
    for token in ("sourceBytes", "sourceByteStore", "contentAddressedSource", "RecordAssertion", "EquivalenceCertificate", "LineageCertificate", "AccountingItem", "predicate", "semanticEquivalence"):
        require(token not in raw, f"forbidden product concept leaked into schema: {token}")
    require("criticality" in raw and "extension" in raw.lower(), "extension compatibility contract is incomplete")


def validate_examples() -> None:
    examples = sorted((ROOT / "examples").glob("*.json"))
    require(len(examples) >= 6, "concrete IR examples are missing")
    for path in examples:
        value = load(path)
        require(isinstance(value, dict), f"example is not an object: {path.name}")
        require(value.get("schema", {}).get("name") == "fdir/document-form", f"example has wrong schema: {path.name}")
        require(isinstance(value.get("documentId"), str) and value["documentId"], f"example has no documentId: {path.name}")
        require(isinstance(value.get("nodes"), list) and value["nodes"], f"example has no nodes: {path.name}")
        require(isinstance(value.get("conversion"), dict), f"example has no conversion report: {path.name}")
        encoded = json.dumps(value, ensure_ascii=False)
        require("predicate" not in encoded and "semanticEquivalence" not in encoded, f"semantic predicate leaked into example: {path.name}")


def validate_docs() -> None:
    required = [
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
    for relative in required:
        require((ROOT / relative).is_file(), f"missing product document: {relative}")
    text = "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in required)
    for phrase in ("Parser / Adapter", "Document Form IR", "Semantic IR", "source map", "property bag"):
        require(phrase.casefold() in text.casefold(), f"documentation is missing boundary phrase: {phrase}")


def validate_runtime_files() -> None:
    for relative in (
        "tools/run_acceptance.py",
        "tools/run_e2e.py",
        "tools/canonicalize_ir.py",
        "tools/query_ir.py",
        "tools/convert_document.py",
        "tools/generate_e2e_fixtures.py",
        "machine/model-contract.json",
        "machine/query-contract.json",
        "machine/capability-profile.json",
        "machine/extension-registry.json",
    ):
        require((ROOT / relative).is_file(), f"product file is missing: {relative}")


def main() -> int:
    try:
        requirements = load(REQ_PATH)
        tests = load(TEST_PATH)
        schema = load(SCHEMA_PATH)
        require(isinstance(requirements, dict), "requirements root must be an object")
        require(isinstance(tests, dict), "acceptance tests root must be an object")
        require(isinstance(schema, dict), "schema root must be an object")
        requirement_map = validate_requirements(requirements, {})
        family_map = validate_families(tests, requirement_map)
        validate_requirements(requirements, family_map)
        validate_schema(schema)
        validate_examples()
        validate_docs()
        validate_runtime_files()
    except DesignError as exc:
        print(f"DESIGN INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"design valid: {len(requirement_map)} requirements, {len(family_map)} acceptance families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
