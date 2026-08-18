
#!/usr/bin/env python3
"""Deterministic, standard-library-only validation for the FDIR 2.1 baseline."""
from __future__ import annotations

import importlib.util
import json
import py_compile
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT_REQUIRED = {
    "baseline.yaml", "README.md", ".gitignore", ".gitattributes",
    "machine/logical-model.yaml", "machine/requirements.yaml", "machine/acceptance-tests.yaml",
    "machine/profiles.yaml", "machine/capabilities.yaml",
    "tools/generate_contracts.py", "tools/generate_traceability.py", "tools/canonical_json.py",
    "schemas/fdir.schema.json", "schemas/fdir.cddl", "schemas/fdir.sql", "schemas/context.jsonld",
    "schemas/generated-manifest.json", "spec/generated/logical-model.md",
    "matrices/requirements-tests.md", "matrices/requirements-tests.csv",
    "references/packaging.md", "diagrams/authority.mmd", "diagrams/model.mmd",
}

ENTITY_ARRAYS = {
    "artifacts": ("artifactId", "Artifact"), "carriers": ("carrierId", "Carrier"),
    "surfaces": ("surfaceId", "Surface"), "geometries": ("geometryId", "Geometry"),
    "selectors": ("selectorId", "Selector"), "occurrences": ("occurrenceId", "Occurrence"),
    "observations": ("observationId", "Observation"),
    "inventoryDomains": ("inventoryDomainId", "InventoryDomain"),
    "accountingItems": ("accountingItemId", "AccountingItem"),
    "censusReceipts": ("receiptId", "IndependentCensusReceipt"),
    "units": ("unitId", "InformationUnit"), "assertions": ("assertionId", "RecordAssertion"),
    "relations": ("relationId", "InformationRelation"),
    "acceptedProjections": ("projectionId", "AcceptedProjection"),
    "interpretationContexts": ("contextId", "InterpretationContext"),
    "guaranteeStatuses": ("guaranteeStatusId", "GuaranteeStatus"),
    "equivalenceCertificates": ("certificateId", "EquivalenceCertificate"),
    "lineageCertificates": ("lineageCertificateId", "LineageCertificate"),
    "diagnostics": ("diagnosticId", "Diagnostic"),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def import_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_document(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document.get("fdirVersion") != "2.1.0":
        errors.append("VERSION_MISMATCH")
    required_arrays = [
        "artifacts", "carriers", "selectors", "occurrences", "units", "assertions", "relations",
        "inventoryDomains", "accountingItems", "guaranteeStatuses", "diagnostics",
    ]
    for key in required_arrays:
        if not isinstance(document.get(key), list):
            errors.append(f"MISSING_ARRAY:{key}")

    ids: dict[str, set[str]] = defaultdict(set)
    all_ids: set[str] = set()
    for key, (id_key, _) in ENTITY_ARRAYS.items():
        values = document.get(key, [])
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str) or not item[id_key]:
                errors.append(f"INVALID_ID:{key}")
                continue
            value = item[id_key]
            if value in seen or value in all_ids:
                errors.append(f"DUPLICATE_ID:{value}")
            seen.add(value)
            all_ids.add(value)
            ids[key].add(value)

    artifacts = ids["artifacts"]
    carriers = ids["carriers"]
    selectors = ids["selectors"]
    occurrences = ids["occurrences"]
    units = ids["units"]
    assertions = ids["assertions"]
    contexts = ids["interpretationContexts"]
    diagnostics = ids["diagnostics"]
    guarantees = ids["guaranteeStatuses"]
    inventory_domains = ids["inventoryDomains"]

    for carrier in document.get("carriers", []):
        if carrier.get("artifactId") not in artifacts:
            errors.append("UNKNOWN_ARTIFACT_REF")
    for occurrence in document.get("occurrences", []):
        if occurrence.get("carrierId") not in carriers:
            errors.append("UNKNOWN_CARRIER_REF")
        for selector_id in occurrence.get("selectorIds", []):
            if selector_id not in selectors:
                errors.append("UNKNOWN_SELECTOR_REF")
    accepted_assertions_by_unit: Counter[str] = Counter()
    all_assertions_by_unit: Counter[str] = Counter()
    for assertion in document.get("assertions", []):
        unit_id = assertion.get("unitId")
        if unit_id not in units:
            errors.append("UNKNOWN_UNIT_REF")
        else:
            all_assertions_by_unit[unit_id] += 1
            if assertion.get("status") == "accepted":
                accepted_assertions_by_unit[unit_id] += 1
        occurrence_ids = assertion.get("occurrenceIds")
        if not isinstance(occurrence_ids, list):
            errors.append("ASSERTION_OCCURRENCES_REQUIRED")
        elif assertion.get("status") == "accepted" and not occurrence_ids:
            related = set(assertion.get("diagnosticIds", []))
            if not related.intersection(diagnostics):
                errors.append("ACCEPTED_ASSERTION_WITHOUT_EVIDENCE")
        for occurrence_id in occurrence_ids or []:
            if occurrence_id not in occurrences:
                errors.append("UNKNOWN_OCCURRENCE_REF")
        context_id = assertion.get("contextId")
        if context_id is not None and context_id not in contexts:
            errors.append("UNKNOWN_CONTEXT_REF")
    for unit_id in units:
        if all_assertions_by_unit[unit_id] == 0:
            errors.append("UNIT_WITHOUT_ASSERTION")

    for relation in document.get("relations", []):
        if relation.get("sourceUnitId") not in units or relation.get("targetUnitId") not in units:
            errors.append("UNKNOWN_RELATION_UNIT_REF")
    for projection in document.get("acceptedProjections", []):
        if projection.get("unitId") not in units:
            errors.append("UNKNOWN_PROJECTION_UNIT_REF")
        for assertion_id in projection.get("assertionIds", []):
            if assertion_id not in assertions:
                errors.append("UNKNOWN_PROJECTION_ASSERTION_REF")
        allowed = {
            a["assertionId"] for a in document.get("assertions", [])
            if a.get("status") == "accepted" and a.get("unitId") == projection.get("unitId")
        }
        if not set(projection.get("assertionIds", [])).issubset(allowed):
            errors.append("PROJECTION_USES_NON_ACCEPTED_ASSERTION")

    source_keys: dict[str, set[str]] = defaultdict(set)
    dispositions = {"represented", "residual", "unsupported", "unreadable", "policy-excluded", "duplicate"}
    for item in document.get("accountingItems", []):
        domain_id = item.get("inventoryDomainId")
        source_key = item.get("sourceKey")
        if domain_id not in inventory_domains:
            errors.append("UNKNOWN_INVENTORY_DOMAIN_REF")
        if not isinstance(source_key, str) or not source_key:
            errors.append("ACCOUNTING_SOURCE_KEY_REQUIRED")
        elif source_key in source_keys[domain_id]:
            errors.append("ACCOUNTING_DUPLICATE_SOURCE_KEY")
        else:
            source_keys[domain_id].add(source_key)
        if item.get("disposition") not in dispositions:
            errors.append("ACCOUNTING_DISPOSITION_INVALID")
        for unit_id in item.get("unitIds", []):
            if unit_id not in units:
                errors.append("UNKNOWN_ACCOUNTING_UNIT_REF")
    domains_by_id = {item.get("inventoryDomainId"): item for item in document.get("inventoryDomains", [])}
    for domain_id, domain in domains_by_id.items():
        expected = domain.get("expectedCount")
        if isinstance(expected, int) and expected != len(source_keys.get(domain_id, set())):
            errors.append("ACCOUNTING_COUNT_MISMATCH")
    for receipt in document.get("censusReceipts", []):
        domain_id = receipt.get("inventoryDomainId")
        if domain_id not in inventory_domains:
            errors.append("UNKNOWN_RECEIPT_DOMAIN_REF")
        expected = domains_by_id.get(domain_id, {}).get("expectedCount")
        if isinstance(expected, int) and receipt.get("observedCount") != expected:
            errors.append("CENSUS_COUNT_MISMATCH")

    guarantee_by_id = {item.get("guaranteeStatusId"): item for item in document.get("guaranteeStatuses", [])}
    for certificate in document.get("equivalenceCertificates", []):
        coverage = [guarantee_by_id.get(item) for item in certificate.get("coverageStatusIds", [])]
        if not coverage or any(item is None for item in coverage):
            errors.append("EQUIVALENCE_COVERAGE_REF_INVALID")
            continue
        if certificate.get("outcome") == "equivalent" and any(item.get("state") != "complete" for item in coverage if item):
            errors.append("EQUIVALENCE_INSUFFICIENT_COVERAGE")

    claims = document.get("claims", {})
    if claims.get("productionReady") is True or claims.get("qualified") is True:
        errors.append("FALSE_PRODUCTION_CLAIM")
    return sorted(set(errors))


def validate_root(root: Path) -> list[str]:
    failures: list[str] = []
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    for required in sorted(ROOT_REQUIRED):
        if required not in actual:
            failures.append(f"missing required file: {required}")
    if any(path.startswith(".bootstrap/") or path.startswith(".issue2/") for path in actual):
        failures.append("bootstrap staging remains in normative tree")

    baseline = load_json(root / "baseline.yaml")
    if baseline.get("version") != "2.1.0" or baseline.get("status") != "final":
        failures.append("baseline metadata is not final 2.1.0")
    if baseline.get("productImplementationClaim") is not False:
        failures.append("baseline falsely claims product implementation")
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    for token in ("dist/", "reports/", "indexes/", "qualification/**/*.bin"):
        if token not in ignored:
            failures.append(f"missing ignore policy: {token}")
    packaging = (root / "references/packaging.md").read_text(encoding="utf-8").lower()
    for token in ("pdf", "docx", "png", "binary qualification", "non-canonical"):
        if token not in packaging:
            failures.append(f"packaging note missing: {token}")

    contract_tool = import_tool(root / "tools/generate_contracts.py", "fdir_generate_contracts")
    if contract_tool.run(root, True) != 0:
        failures.append("generated contract parity failed")
    trace_tool = import_tool(root / "tools/generate_traceability.py", "fdir_generate_traceability")
    if trace_tool.run(root, True) != 0:
        failures.append("traceability generation parity failed")

    requirements = load_json(root / "machine/requirements.yaml")["requirements"]
    tests = load_json(root / "machine/acceptance-tests.yaml")["tests"]
    requirement_ids = [item["id"] for item in requirements]
    if len(requirement_ids) != len(set(requirement_ids)):
        failures.append("duplicate requirement id")
    coverage = Counter()
    for test in tests:
        for requirement_id in test.get("requirements", []):
            if requirement_id not in set(requirement_ids):
                failures.append(f"unknown requirement in {test['id']}: {requirement_id}")
            coverage[requirement_id] += 1
        fixture = test.get("fixture")
        if fixture and not (root / fixture).is_file():
            failures.append(f"missing fixture referenced by {test['id']}: {fixture}")
    for requirement_id in requirement_ids:
        if coverage[requirement_id] == 0:
            failures.append(f"uncovered requirement: {requirement_id}")

    for path in sorted((root / "examples").glob("*.json")) + sorted((root / "fixtures/positive").glob("*.json")):
        errors = validate_document(load_json(path))
        if errors:
            failures.append(f"positive document failed {path.relative_to(root)}: {', '.join(errors)}")

    negative_manifest = load_json(root / "fixtures/negative/manifest.json")
    for item in negative_manifest["fixtures"]:
        path = root / item["path"]
        errors = validate_document(load_json(path))
        if item["expectedCode"] not in errors:
            failures.append(f"negative fixture did not produce {item['expectedCode']}: {item['path']} ({errors})")

    canonical_tool = import_tool(root / "tools/canonical_json.py", "fdir_canonical_json")
    vector = load_json(root / "fixtures/canonical/vector.json")
    actual_canonical = canonical_tool.canonical_bytes(vector["value"]).decode("utf-8")
    actual_digest = canonical_tool.sha256_digest(vector["value"])
    if actual_canonical != vector["canonical"] or actual_digest != vector["digest"]:
        failures.append("canonical JSON vector mismatch")

    schema = load_json(root / "schemas/fdir.schema.json")
    if schema.get("$ref") != "#/$defs/Snapshot" or "RecordAssertion" not in schema.get("$defs", {}):
        failures.append("generated JSON schema lacks Snapshot or RecordAssertion")
    if "InformationUnit" not in schema.get("$defs", {}) or set(schema["$defs"]["InformationUnit"]["properties"]) != {"unitId"}:
        failures.append("InformationUnit contains substantive fields")

    capabilities = load_json(root / "machine/capabilities.yaml")
    if any(item.get("status") not in {"design-only", "unimplemented"} for item in capabilities["capabilities"]):
        failures.append("capability registry makes an implementation or qualification claim")

    identity_text = (root / "spec/05-canonicalization-and-identity.md").read_text(encoding="utf-8").lower()
    for phrase in ("unit identity", "cross-format equivalence", "cross-revision continuity"):
        if phrase not in identity_text:
            failures.append(f"identity separation missing: {phrase}")

    for path in sorted((root / "tools").glob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as error:
            failures.append(f"python compile failed {path.name}: {error.msg}")
    for cache in root.rglob("__pycache__"):
        for child in cache.iterdir():
            child.unlink()
        cache.rmdir()
    return failures


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0] if argv else ".").resolve()
    failures = validate_root(root)
    if failures:
        print("FDIR 2.1 baseline validation failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    positive_count = len(list((root / "fixtures/positive").glob("*.json")))
    negative_count = len(load_json(root / "fixtures/negative/manifest.json")["fixtures"])
    requirement_count = len(load_json(root / "machine/requirements.yaml")["requirements"])
    test_count = len(load_json(root / "machine/acceptance-tests.yaml")["tests"])
    print(
        "FDIR 2.1 baseline validation passed: "
        f"{requirement_count} requirements, {test_count} acceptance tests, "
        f"{positive_count} positive fixtures, {negative_count} negative fixtures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
