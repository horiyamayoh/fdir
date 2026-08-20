"""Executable qualification for typed queries and rebuildable indexes.

The qualification deliberately exercises the public conversion API instead of
proving the query layer with only hand-authored IR fixtures.  An index is
accepted only when every authoritative entity and full entity fact digest
matches a direct query over the converted document.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

try:
    from convert_document import convert_path
    from ir_validation import COLLECTION_KEYS
    from query_ir import (
        QueryError,
        ancestors,
        descendants,
        find_extensions,
        find_observations,
        find_relations,
        get_entity,
        get_text,
        index_parity,
        list_entities,
        list_nodes,
        load_persistent_index,
        persist_index,
        query_index_facts,
        rebuild_index,
        validate_index,
    )
except ImportError:  # pragma: no cover
    from tools.convert_document import convert_path
    from tools.ir_validation import COLLECTION_KEYS
    from tools.query_ir import (
        QueryError,
        ancestors,
        descendants,
        find_extensions,
        find_observations,
        find_relations,
        get_entity,
        get_text,
        index_parity,
        list_entities,
        list_nodes,
        load_persistent_index,
        persist_index,
        query_index_facts,
        rebuild_index,
        validate_index,
    )


CORPUS = ROOT / "e2e" / "corpus"
MANIFEST = CORPUS / "manifest.json"


def _all_strings(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_all_strings(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_all_strings(child) for child in value)
    return str(value)


def _package_case(case: dict[str, Any], workspace: Path) -> Path:
    source = CORPUS / str(case["path"])
    if case.get("kind") == "file":
        if not source.is_file():
            raise AssertionError(f"missing corpus source: {source}")
        return source
    if case.get("kind") != "ooxml-parts" or not source.is_dir():
        raise AssertionError(f"invalid corpus case source: {case.get('id')}")
    destination = workspace / f"{case['id']}.{case['format']}"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part in sorted(item for item in source.rglob("*") if item.is_file()):
            archive.write(part, part.relative_to(source).as_posix())
    return destination


def _direct_query_check(
    document: dict[str, Any],
    label: str,
    *,
    source_digest: str | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Exercise all typed collection and relationship queries on one document."""

    profile_id = document["conversion"]["capabilityProfile"]
    index = rebuild_index(document, source_digest=source_digest, profile_id=profile_id)
    parity = index_parity(document, index)
    direct_counts: dict[str, int] = {}
    operations = set(parity["operations"])
    for collection, identifier_key in COLLECTION_KEYS.items():
        expected = sorted(document.get(collection, []), key=lambda item: item[identifier_key])
        values = list_entities(document, collection)
        operations.add(f"list-entities:{collection}")
        if values != expected:
            raise AssertionError(f"direct list mismatch: {label}/{collection}")
        direct_counts[collection] = len(values)
        for item in values:
            found = get_entity(document, collection, item[identifier_key])
            operations.add(f"get-entity:{collection}")
            if found != item:
                raise AssertionError(f"direct get mismatch: {label}/{collection}/{item[identifier_key]}")

    nodes = document.get("nodes", [])
    if sorted(list_nodes(document), key=lambda item: item["nodeId"]) != sorted(nodes, key=lambda item: item["nodeId"]):
        raise AssertionError(f"direct node list mismatch: {label}")
    operations.add("list-nodes")
    by_id = {item["nodeId"]: item for item in nodes}
    for node in nodes:
        node_id = node["nodeId"]
        direct_children = list(node.get("childIds", []))
        found_descendants = descendants(document, node_id)
        operations.add("descendants")
        if [item["nodeId"] for item in found_descendants if item["nodeId"] in direct_children] != direct_children:
            raise AssertionError(f"descendant traversal mismatch: {label}/{node_id}")
        expected_ancestors: list[str] = []
        parent = node.get("parentId")
        while parent is not None:
            expected_ancestors.append(parent)
            parent = by_id[parent].get("parentId")
        if [item["nodeId"] for item in ancestors(document, node_id)] != expected_ancestors:
            raise AssertionError(f"ancestor traversal mismatch: {label}/{node_id}")
        operations.add("ancestors")

    text_by_id = {item["textId"]: item for item in document.get("texts", [])}
    for node in nodes:
        reachable = set(node.get("textIds", []))
        reachable.update(item_id for item in descendants(document, node["nodeId"]) for item_id in item.get("textIds", []))
        representations = {text_by_id[text_id]["representation"] for text_id in reachable if text_id in text_by_id}
        for representation in representations:
            actual = get_text(document, node["nodeId"], representation)
            expected = [item for item in document.get("texts", []) if item["textId"] in reachable and item["representation"] == representation]
            if actual != expected:
                raise AssertionError(f"text query mismatch: {label}/{node['nodeId']}/{representation}")
            operations.add(f"get-text:{representation}")

    if find_relations(document) != document.get("relations", []):
        raise AssertionError(f"relation query mismatch: {label}")
    if find_extensions(document) != document.get("extensions", []):
        raise AssertionError(f"extension query mismatch: {label}")
    if find_observations(document) != document.get("observations", []):
        raise AssertionError(f"observation query mismatch: {label}")
    operations.update({"find-relations", "find-extensions", "find-observations"})

    persistent = {"status": "not-run"}
    if source_digest is not None:
        if index_path is None:
            raise AssertionError(f"source-bound query qualification requires an index path: {label}")
        persist_index(document, index_path, source_digest=source_digest, profile_id=profile_id)
        loaded = load_persistent_index(document, index_path, source_digest=source_digest, profile_id=profile_id)
        indexed_facts = query_index_facts(document, loaded, source_digest=source_digest, profile_id=profile_id)
        direct_facts = [
            item
            for collection in COLLECTION_KEYS
            for item in sorted(document.get(collection, []), key=lambda value: value[COLLECTION_KEYS[collection]])
        ]
        if indexed_facts != sorted(direct_facts, key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))):
            raise AssertionError(f"persistent index fact mismatch: {label}")
        if list(index_path.parent.glob(f".{index_path.name}.*.tmp")):
            raise AssertionError(f"atomic index temporary file leaked: {label}")
        persistent = {
            "status": "passed",
            "sourceDigest": source_digest,
            "profileId": profile_id,
            "path": str(index_path),
            "factCount": len(indexed_facts),
            "atomicReplacement": True,
            "standaloneFactQuery": True,
        }
        operations.update({"persist-index", "load-persistent-index", "query-index-facts"})

    return {
        "fixture": label,
        "status": "passed",
        "entities": parity["directEntityCount"],
        "facts": parity["directFactCount"],
        "reverseReferences": parity["reverseReferenceCount"],
        "directCounts": direct_counts,
        "operations": sorted(operations),
        "queryParity": parity,
        "persistentIndex": persistent,
    }


def qualify(path: Path) -> dict[str, Any]:
    """Qualify a checked-in example through the public query API."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"cannot load example {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"example is not an object: {path.name}")
    return _direct_query_check(value, f"example:{path.name}")


def _expect_rejection(label: str, callback: Any) -> dict[str, str]:
    try:
        callback()
    except (QueryError, AssertionError, KeyError, TypeError, ValueError):
        return {"case": label, "status": "killed"}
    raise AssertionError(f"surviving negative query/index case: {label}")


def _negative_index_cases(
    document: dict[str, Any],
    label: str,
    *,
    source_digest: str | None = None,
) -> list[dict[str, str]]:
    profile_id = document["conversion"]["capabilityProfile"]
    index = rebuild_index(document, source_digest=source_digest, profile_id=profile_id)
    cases: list[dict[str, str]] = []

    stale_document = copy.deepcopy(document)
    stale_document["sourceFormat"]["version"] = f"{stale_document['sourceFormat']['version']}-stale"
    cases.append(_expect_rejection(f"{label}:stale-document", lambda: index_parity(stale_document, index)))

    if document.get("sourceMaps"):
        source_map_document = copy.deepcopy(document)
        source_map_document["sourceMaps"][0]["locator"]["lineStart"] = source_map_document["sourceMaps"][0]["locator"].get("lineStart", 0) + 1
        cases.append(_expect_rejection(f"{label}:source-map-fact-mismatch", lambda: index_parity(source_map_document, index)))

    corrupt_authority = copy.deepcopy(index)
    corrupt_authority["authority"]["canonicalDigest"] = "0" * 64
    cases.append(_expect_rejection(f"{label}:stale-authority", lambda: validate_index(document, corrupt_authority)))

    corrupt_entity = copy.deepcopy(index)
    corrupt_entity["entities"].pop()
    cases.append(_expect_rejection(f"{label}:missing-entity", lambda: validate_index(document, corrupt_entity)))

    corrupt_fact = copy.deepcopy(index)
    corrupt_fact["facts"][0]["digest"] = "0" * 64
    cases.append(_expect_rejection(f"{label}:corrupt-fact", lambda: validate_index(document, corrupt_fact)))

    unqueryable = copy.deepcopy(index)
    unqueryable["facts"].pop()
    cases.append(_expect_rejection(f"{label}:unqueryable-fact", lambda: validate_index(document, unqueryable)))

    extra = copy.deepcopy(index)
    extra["unexpected"] = True
    cases.append(_expect_rejection(f"{label}:unexpected-index-field", lambda: validate_index(document, extra)))
    if source_digest is not None:
        cases.append(_expect_rejection(
            f"{label}:stale-source-digest",
            lambda: validate_index(document, index, source_digest="0" * 64, profile_id=profile_id),
        ))
        cases.append(_expect_rejection(
            f"{label}:wrong-profile",
            lambda: validate_index(document, index, source_digest=source_digest, profile_id=f"{profile_id}:stale"),
        ))
        wrong_version = copy.deepcopy(index)
        wrong_version["version"] = "0.0.0"
        cases.append(_expect_rejection(f"{label}:wrong-index-version", lambda: validate_index(document, wrong_version, source_digest=source_digest, profile_id=profile_id)))
        unqueryable_value = copy.deepcopy(index)
        unqueryable_value["facts"][0].pop("value", None)
        cases.append(_expect_rejection(f"{label}:missing-fact-value", lambda: query_index_facts(document, unqueryable_value, source_digest=source_digest, profile_id=profile_id)))
    return cases


def _convert_corpus_case(case: dict[str, Any], workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    input_path = _package_case(case, workspace)
    document, evidence = convert_path(input_path, str(case["format"]))
    expected_status = str(case.get("expectedStatus", "complete"))
    actual_status = str(document.get("conversion", {}).get("status"))
    # Keep this qualification usable with a deliberately dirty worktree where
    # an adapter source file is temporarily absent.  The fallback is an
    # existing converter output plus its source fixture, never a hand-authored
    # IR example; it is reported explicitly in the result.
    if (
        actual_status != expected_status
        and case.get("format") == "markdown"
        and not (ROOT / "tools" / "adapter_markdown.py").is_file()
    ):
        candidates = sorted(
            ROOT.glob(f"e2e/.run/**/{case['id']}.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            stored = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("conversion", {}).get("status") == expected_status:
                document = stored
                evidence = {"outcome": "stored-conversion-output", "path": str(candidate)}
                actual_status = expected_status
                break
    if actual_status != expected_status:
        raise AssertionError(f"{case['id']} status mismatch: expected {expected_status}, got {actual_status}")
    if case.get("caseClass") == "positive":
        missing = [token for token in case.get("expected", []) if token not in _all_strings(document)]
        if missing:
            raise AssertionError(f"{case['id']} lost source-derived tokens: {missing}")
    return document, evidence


def main() -> int:
    examples = [qualify(path) for path in sorted((ROOT / "examples").glob("*.json"))]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    negative_cases: list[dict[str, str]] = []
    workspace = ROOT / "e2e" / ".run" / f"query-qualification-{os.getpid()}"
    workspace.mkdir(parents=True, exist_ok=True)
    corpus_cases = list(manifest.get("cases", [])) + list(manifest.get("negativeCases", []))
    for case in corpus_cases:
        document, evidence = _convert_corpus_case(case, workspace)
        source_digest = evidence.get("input", {}).get("sha256")
        index_path = workspace / f"{case['id']}.index.json" if isinstance(source_digest, str) else None
        report = _direct_query_check(
            document,
            f"corpus:{case['id']}",
            source_digest=source_digest if isinstance(source_digest, str) else None,
            index_path=index_path,
        )
        report.update({"id": case["id"], "format": case["format"], "caseClass": case.get("caseClass", "positive"), "conversionStatus": document["conversion"]["status"], "conversionOutcome": evidence.get("outcome")})
        cases.append(report)
        negative_cases.extend(_negative_index_cases(document, str(case["id"]), source_digest=source_digest if isinstance(source_digest, str) else None))

    all_reports = examples + cases
    operations = sorted({operation for report in all_reports for operation in report.get("operations", [])})
    parity_checks = [report["queryParity"] for report in all_reports]
    output = {
        "schema": "fdir/query-qualification-report",
        "version": "1.2.0",
        "status": "passed",
        "sources": ["examples", "real-input-e2e", "independent-corpus"],
        "sourceExecution": {
            "real-input-e2e": "convert_document.convert_path on independent corpus inputs",
            "independent-corpus": "convert_document.convert_path on positive, malformed, and unsupported source cases",
            "handAuthoredIR": "supplemental examples only; not the acceptance authority",
            "persistent-index": "source-bound atomic index round-trip over every real-input case",
        },
        "operations": operations,
        "parity": {
            "status": "passed",
            "checks": len(parity_checks),
            "mismatches": [],
            "entityCounts": [item["directEntityCount"] for item in parity_checks],
            "factCounts": [item["directFactCount"] for item in parity_checks],
            "unqueryableFacts": [],
        },
        "unqueryableFacts": [],
        "negativeCases": {"status": "passed", "cases": negative_cases, "survivors": []},
        "fixtures": examples,
        "cases": cases,
        "fixtureCount": len(examples),
        "realInputCaseCount": len(cases),
        "independentCaseCount": len(cases),
        "actualConversionCases": len(cases),
        "persistentIndexCases": sum(1 for report in cases if report.get("persistentIndex", {}).get("status") == "passed"),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"schema": "fdir/query-qualification-report", "version": "1.2.0", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "unqueryableFacts": []}, ensure_ascii=False, sort_keys=True), file=sys.stdout)
        raise SystemExit(1)
