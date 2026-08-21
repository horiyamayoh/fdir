"""Independent-oracle qualification for issue #103.

The expected facts live in ``machine/qualification-issue-103-corpus.json`` and
are deliberately literal review data.  This runner never builds its expected
set by iterating ``COLLECTION_KEYS`` or by copying rows from either query
implementation.  It exercises the direct evaluator and the standalone
SQLite builder separately, then compares both with the authored facts and
with each other.

Every required report is written on success and failure.  A failed assertion
keeps the process at exit status 1; no report is allowed to turn an incomplete
implementation into a completion claim.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-103-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-103"

REPORT_NAMES = (
    "query-field-coverage.json",
    "direct-oracle-conformance.json",
    "index-oracle-conformance.json",
    "direct-index-parity.json",
    "reverse-reference-closure.json",
    "index-version-integrity.json",
    "query-resource-limits.json",
)

PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
EVIDENCE_ID = "issue-103-query-index"
REQUIREMENT_ID = "QUAL-103-QUERY-INDEX"
BUNDLE_PREFIX = "artifacts/103"
DECLARED_INPUTS = (
    "machine/qualification-issue-103-corpus.json",
    "machine/query-contract.json",
    "tools/generate_query_contract.py",
    "tools/query_ir.py",
    "tools/independent_index.py",
    "tools/qualification_issue103.py",
    "tools/test_query_surface.py",
    "tools/test_independent_index.py",
)
EVALUATOR_PATH = ROOT / "tools" / "validate_qualification_bundle.py"
SHARED_EVIDENCE_PATH = ROOT / "tools" / "qualification_evidence.py"

DOCUMENT_COLLECTION = "__document__"


class QualificationError(RuntimeError):
    """Raised when the #103 qualification input or lane is unsafe."""


def _load_tools() -> tuple[Any, Any]:
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    try:
        import independent_index
        import query_ir
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise QualificationError(f"query/index modules are unavailable: {exc}") from exc
    return query_ir, independent_index


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON input {path}: {exc}") from exc


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _manifest_checksum(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "integrityChecksum"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _equal(left: Any, right: Any) -> bool:
    return _canonical(left) == _canonical(right)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    paths = [ROOT / relative for relative in DECLARED_INPUTS]
    paths[0] = Path(corpus_path)
    return paths


def _producer_input_digests(corpus_path: Path) -> tuple[list[str], list[str]]:
    digests: list[str] = []
    unavailable: list[str] = []
    for path in _producer_input_paths(corpus_path):
        if path.is_file():
            digests.append(_sha256_file(path))
        else:
            unavailable.append(str(path))
            digests.append(_sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
    return sorted(set(digests)), unavailable


def _producer_artifact_reference(local_path: Path, bundle_path: str, pointer: str) -> dict[str, Any]:
    value = _pointer_value(_read_json(local_path), pointer)
    selector = {"kind": "json-pointer", "pointer": pointer}
    return {
        "path": bundle_path,
        "sha256": _sha256_file(local_path),
        "selector": selector,
        "selectedSha256": _sha256_bytes(_canonical(value).encode("utf-8")),
    }


def _append_producer_record(report: dict[str, Any], key: str, value: dict[str, Any]) -> str:
    records = report.setdefault(key, [])
    pointer = f"/{key}/{len(records)}"
    records.append(value)
    return pointer


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise QualificationError(f"cannot obtain exact source SHA: {value!r}")
    return value


def _validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("issueNumber") != 103:
        raise QualificationError("issue-103 corpus has the wrong issue number")
    if corpus.get("qualificationScope") != "bounded-independent-query-index-oracle":
        raise QualificationError("issue-103 corpus is not marked as the independent query/index lane")
    if tuple(corpus.get("reportNames", [])) != REPORT_NAMES:
        raise QualificationError("issue-103 corpus report names are incomplete or reordered")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue-103 corpus has no oracle declaration")
    for key in (
        "expectedValuesAreRuntimeIndependent",
        "expectedFactsAreHandReviewed",
        "expectedFactsGeneratedFromCollectionKeys",
        "directAndIndexUsedToGenerateExpected",
    ):
        if key not in oracle:
            raise QualificationError(f"issue-103 oracle declaration is missing {key}")
    if not oracle["expectedValuesAreRuntimeIndependent"] or not oracle["expectedFactsAreHandReviewed"]:
        raise QualificationError("issue-103 expected facts are not marked independent and reviewed")
    if oracle["expectedFactsGeneratedFromCollectionKeys"] or oracle["directAndIndexUsedToGenerateExpected"]:
        raise QualificationError("issue-103 expected facts are coupled to implementation metadata")
    fixtures = corpus.get("fixtures")
    facts = corpus.get("expectedFacts")
    references = corpus.get("expectedReferences")
    if not isinstance(fixtures, list) or not fixtures or not isinstance(facts, list) or not facts:
        raise QualificationError("issue-103 corpus has no fixture or expected fact table")
    if not isinstance(references, list) or not references:
        raise QualificationError("issue-103 corpus has no expected reference table")
    fixture_ids = {item.get("id") for item in fixtures if isinstance(item, dict)}
    if None in fixture_ids or any(item.get("fixture") not in fixture_ids for item in facts + references if isinstance(item, dict)):
        raise QualificationError("issue-103 expected fact references an unknown fixture")


def _pointer_value(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise QualificationError(f"invalid fixture mutation pointer: {pointer!r}")
    current = value
    for raw in pointer[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise QualificationError(f"fixture mutation pointer is missing: {pointer}")
    return current


def _set_pointer(value: Any, pointer: str, replacement: Any) -> None:
    if not pointer.startswith("/"):
        raise QualificationError(f"invalid fixture mutation pointer: {pointer!r}")
    segments = pointer[1:].split("/")
    current = value
    for raw in segments[:-1]:
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise QualificationError(f"fixture mutation parent is missing: {pointer}")
    leaf = segments[-1].replace("~1", "/").replace("~0", "~")
    if isinstance(current, dict):
        current[leaf] = replacement
    elif isinstance(current, list) and leaf.isdigit() and int(leaf) < len(current):
        current[int(leaf)] = replacement
    else:
        raise QualificationError(f"fixture mutation target is missing: {pointer}")


def _load_fixtures(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    for fixture in corpus["fixtures"]:
        fixture_id = str(fixture["id"])
        path = ROOT / str(fixture["path"])
        value = _read_json(path)
        if not isinstance(value, dict):
            raise QualificationError(f"fixture is not an object: {path}")
        if value.get("documentId") != fixture.get("expectedDocumentId"):
            raise QualificationError(f"fixture document ID drift: {fixture_id}")
        mutation = fixture.get("mutation")
        if mutation is not None:
            node_id = mutation.get("nodeId")
            node = next((item for item in value.get("nodes", []) if item.get("nodeId") == node_id), None)
            if not isinstance(node, dict):
                raise QualificationError(f"fixture mutation node is missing: {fixture_id}/{node_id}")
            pointer = str(mutation.get("pointer"))
            _set_pointer(node, pointer, copy.deepcopy(mutation.get("value")))
        fixtures[fixture_id] = value
    return fixtures


def _base_report(source_sha: str, *, status: str, assertions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": "fdir/qualification-issue-103-report",
        "version": "1.0.0",
        "issueNumber": 103,
        "sourceSha": source_sha,
        "status": status,
        "completionStatus": "qualified-focused-lane" if status == "passed" else "incomplete-focused-lane",
        "assertions": assertions or [],
        "cases": [],
        "mismatches": [],
    }


def _failed_report(source_sha: str, error: str) -> dict[str, Any]:
    report = _base_report(source_sha, status="failed")
    report["mismatches"] = [{"code": "QUALIFICATION_EXECUTION_ERROR", "error": error}]
    return report


def _fact_value(query_ir: Any, document: dict[str, Any], fact: dict[str, Any]) -> Any:
    if fact["collection"] == DOCUMENT_COLLECTION:
        return query_ir.get_document_field(document, str(fact["pointer"]))
    return query_ir.get_field(document, str(fact["collection"]), str(fact["entityId"]), str(fact["pointer"]))


def _index_fact_value(index: Any, fact: dict[str, Any]) -> Any:
    return index.get_field(str(fact["collection"]), str(fact["entityId"]), str(fact["pointer"]))


def _direct_fact_cases(query_ir: Any, fixtures: dict[str, dict[str, Any]], facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for fact in facts:
        document = fixtures[str(fact["fixture"])]
        case: dict[str, Any] = {
            "caseId": fact["caseId"],
            "fixture": fact["fixture"],
            "field": {
                "collection": fact["collection"],
                "entityId": fact["entityId"],
                "pointer": fact["pointer"],
            },
            "expected": fact.get("value"),
        }
        try:
            actual = _fact_value(query_ir, document, fact)
            query_rows = query_ir.query_fields(
                document,
                str(fact["pointer"]),
                fact.get("value"),
                operator="eq",
                collection=str(fact["collection"]),
            )
            row_match = any(
                row.get("id") == fact["entityId"]
                and row.get("pointer") == fact["pointer"]
                and _equal(row.get("value"), fact.get("value"))
                for row in query_rows
            )
            case.update({"actual": actual, "queryRowMatch": row_match, "status": "passed" if _equal(actual, fact.get("value")) and row_match else "failed"})
            if case["status"] != "passed":
                mismatches.append(case)
        except Exception as exc:
            case.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            mismatches.append(case)
        cases.append(case)
    return cases, mismatches


def _coverage_report(query_ir: Any, contract: dict[str, Any], fixtures: dict[str, dict[str, Any]], source_sha: str) -> dict[str, Any]:
    report = _base_report(source_sha, status="passed")
    report["contract"] = {
        "version": contract.get("version"),
        "fieldPathCount": contract.get("fieldPathCount"),
        "generatedSourceDigest": contract.get("generated", {}).get("sourceDigest"),
    }
    coverage_cases: list[dict[str, Any]] = []
    for fixture_id, document in fixtures.items():
        coverage = query_ir.query_field_coverage(document)
        item = {
            "fixture": fixture_id,
            "status": coverage.get("status"),
            "checkedFactCount": coverage.get("checkedFactCount"),
            "registeredFieldPathCount": coverage.get("registeredFieldPathCount"),
            "observedRegisteredFactCounts": coverage.get("observedRegisteredFactCounts", {}),
            "unqueryableFacts": coverage.get("unqueryableFacts", []),
        }
        coverage_cases.append(item)
        if coverage.get("status") != "passed" or coverage.get("unqueryableFacts"):
            report["mismatches"].extend(coverage.get("unqueryableFacts", []))
    report["cases"] = coverage_cases
    report["observedFactCount"] = sum(int(item.get("checkedFactCount") or 0) for item in coverage_cases)
    report["status"] = "passed" if coverage_cases and not report["mismatches"] else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "generated-authoritative-registry", "status": "passed" if int(contract.get("fieldPathCount") or 0) > 0 else "failed"},
        {"id": "observed-facts-are-explored", "status": "passed" if report["observedFactCount"] > 0 else "failed"},
        {"id": "unqueryable-facts-zero", "status": "passed" if not report["mismatches"] else "failed"},
    ]
    if any(item["status"] == "failed" for item in report["assertions"]):
        report["status"] = "failed"
    return report


def _direct_oracle_report(query_ir: Any, corpus: dict[str, Any], fixtures: dict[str, dict[str, Any]], source_sha: str) -> dict[str, Any]:
    cases, mismatches = _direct_fact_cases(query_ir, fixtures, corpus["expectedFacts"])
    report = _base_report(source_sha, status="passed")
    report["cases"] = cases
    report["mismatches"] = mismatches
    report["expectedFactCount"] = len(cases)
    report["directMismatchCount"] = len(mismatches)
    negative_cases: list[dict[str, Any]] = []
    for negative in corpus.get("typedNegativeCases", []):
        document = fixtures[str(negative["fixture"])]
        item = {"caseId": negative["caseId"], "status": "passed"}
        try:
            if negative.get("operator") == "get-field":
                _fact_value(query_ir, document, negative)
                item.update({"status": "failed", "survived": True})
            else:
                rows = query_ir.query_fields(
                    document,
                    negative["pointer"],
                    negative.get("value"),
                    operator=negative.get("operator", "eq"),
                    collection=negative.get("collection"),
                )
                relevant_rows = [
                    row for row in rows
                    if negative.get("entityId") is None or row.get("id") == negative.get("entityId")
                ]
                survived = bool(relevant_rows) if negative.get("expectedMatch") is False else not bool(relevant_rows)
                item.update({"rows": len(rows), "status": "failed" if survived else "passed", "survived": survived})
        except Exception as exc:
            expected_rejection = bool(negative.get("expectedRejection"))
            item.update({"status": "passed" if expected_rejection else "failed", "error": f"{type(exc).__name__}: {exc}"})
        negative_cases.append(item)
    report["negativeCases"] = negative_cases
    report["mismatches"].extend(item for item in negative_cases if item["status"] != "passed")
    report["status"] = "passed" if not report["mismatches"] else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "literal-fact-table", "status": "passed" if cases else "failed"},
        {"id": "direct-oracle-mismatch-zero", "status": "passed" if not mismatches else "failed"},
        {"id": "typed-negative-survivors-zero", "status": "passed" if not report["mismatches"] else "failed"},
    ]
    return report


def _write_source(path: Path, document: dict[str, Any]) -> None:
    path.write_text(_canonical(document) + "\n", encoding="utf-8", newline="\n")


def _build_indexes(independent_index: Any, fixtures: dict[str, dict[str, Any]], workspace: Path) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    workspace.mkdir(parents=True, exist_ok=True)
    for fixture_id, document in fixtures.items():
        source = workspace / f"{fixture_id}.json"
        index_path = workspace / f"{fixture_id}.sqlite"
        _write_source(source, document)
        manifest = independent_index.build_index(source, index_path)
        contexts[fixture_id] = {"source": source, "indexPath": index_path, "manifest": manifest}
    return contexts


def _index_oracle_report(independent_index: Any, corpus: dict[str, Any], fixtures: dict[str, dict[str, Any]], contexts: dict[str, dict[str, Any]], source_sha: str) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for fact in corpus["expectedFacts"]:
        fixture_id = str(fact["fixture"])
        context = contexts[fixture_id]
        case: dict[str, Any] = {
            "caseId": fact["caseId"],
            "fixture": fixture_id,
            "field": {"collection": fact["collection"], "entityId": fact["entityId"], "pointer": fact["pointer"]},
            "expected": fact.get("value"),
        }
        try:
            with independent_index.open_index(context["source"], context["indexPath"]) as index:
                actual = _index_fact_value(index, fact)
                rows = index.query_fields(fact["pointer"], fact.get("value"), collection=fact["collection"])
                row_match = any(
                    row.get("id") == fact["entityId"]
                    and row.get("pointer") == fact["pointer"]
                    and _equal(row.get("value"), fact.get("value"))
                    for row in rows
                )
            case.update({"actual": actual, "queryRowMatch": row_match, "status": "passed" if _equal(actual, fact.get("value")) and row_match else "failed"})
            if case["status"] != "passed":
                mismatches.append(case)
        except Exception as exc:
            case.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            mismatches.append(case)
        cases.append(case)
    negative_cases: list[dict[str, Any]] = []
    for negative in corpus.get("typedNegativeCases", []):
        fixture_id = str(negative["fixture"])
        context = contexts[fixture_id]
        item: dict[str, Any] = {"caseId": negative["caseId"], "status": "passed"}
        try:
            with independent_index.open_index(context["source"], context["indexPath"]) as index:
                if negative.get("operator") == "get-field":
                    _index_fact_value(index, negative)
                    survived = True
                else:
                    rows = index.query_fields(
                        negative["pointer"],
                        negative.get("value"),
                        operator=negative.get("operator", "eq"),
                        collection=negative.get("collection"),
                    )
                    relevant_rows = [
                        row for row in rows
                        if negative.get("entityId") is None or row.get("id") == negative.get("entityId")
                    ]
                    survived = bool(relevant_rows) if negative.get("expectedMatch") is False else not bool(relevant_rows)
            item.update({"survived": survived, "status": "failed" if survived else "passed"})
        except Exception as exc:
            expected_rejection = bool(negative.get("expectedRejection"))
            item.update({
                "survived": not expected_rejection,
                "status": "passed" if expected_rejection else "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        negative_cases.append(item)
    report = _base_report(source_sha, status="passed")
    report["cases"] = cases
    report["mismatches"] = mismatches
    report["negativeCases"] = negative_cases
    report["mismatches"].extend(item for item in negative_cases if item["status"] != "passed")
    report["expectedFactCount"] = len(cases)
    report["indexMismatchCount"] = len(mismatches)
    report["manifests"] = {
        fixture: {
            "indexVersion": context["manifest"]["indexVersion"],
            "contractVersions": context["manifest"]["contractVersions"],
            "bindings": context["manifest"]["bindings"],
            "counts": context["manifest"]["counts"],
            "querySurface": context["manifest"]["querySurface"],
        }
        for fixture, context in contexts.items()
    }
    report["status"] = "passed" if not mismatches else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "independent-persistent-backend", "status": "passed" if contexts else "failed"},
        {"id": "index-oracle-mismatch-zero", "status": "passed" if not mismatches else "failed"},
        {"id": "typed-negative-survivors-zero", "status": "passed" if not report["mismatches"] else "failed"},
    ]
    return report


def _reference_key(row: dict[str, Any], *, direct: bool) -> tuple[Any, ...]:
    if direct:
        return (
            row["fromCollection"], row["fromId"], row["field"],
            row["toCollection"], row["toId"], row["ordinal"],
        )
    return (
        row["sourceCollection"], row["sourceId"], row["sourcePointer"],
        row["targetCollection"], row["targetId"], row["ordinal"],
    )


def _parity_report(query_ir: Any, independent_index: Any, corpus: dict[str, Any], fixtures: dict[str, dict[str, Any]], contexts: dict[str, dict[str, Any]], source_sha: str) -> dict[str, Any]:
    report = _base_report(source_sha, status="passed")
    mismatches: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for fixture_id, document in fixtures.items():
        coverage = query_ir.query_field_coverage(document)
        context = contexts[fixture_id]
        with independent_index.open_index(context["source"], context["indexPath"]) as persistent:
            direct_index_mismatches: list[dict[str, Any]] = []
            for fact in coverage.get("checked", []):
                try:
                    if fact["collection"] == DOCUMENT_COLLECTION:
                        direct = query_ir.get_document_field(document, fact["pointer"])
                    else:
                        direct = query_ir.get_field(document, fact["collection"], fact["id"], fact["pointer"])
                    indexed = persistent.get_field(fact["collection"], fact["id"], fact["pointer"])
                    if not _equal(direct, indexed):
                        direct_index_mismatches.append({"fact": fact, "direct": direct, "index": indexed})
                except Exception as exc:
                    direct_index_mismatches.append({"fact": fact, "error": f"{type(exc).__name__}: {exc}"})
            direct_refs = {
                _reference_key(row, direct=True)
                for row in query_ir.rebuild_index(document)["reverseReferences"]
            }
            indexed_refs = {
                _reference_key(row, direct=False)
                for row in persistent.find_references()
            }
            if direct_refs != indexed_refs:
                direct_index_mismatches.append({
                    "code": "DIRECT_INDEX_REFERENCE_MISMATCH",
                    "directOnly": sorted(direct_refs - indexed_refs),
                    "indexOnly": sorted(indexed_refs - direct_refs),
                })
            item = {
                "fixture": fixture_id,
                "checkedFactCount": len(coverage.get("checked", [])),
                "directReferenceCount": len(direct_refs),
                "indexReferenceCount": len(indexed_refs),
                "mismatches": direct_index_mismatches,
                "status": "passed" if not direct_index_mismatches else "failed",
            }
            cases.append(item)
            mismatches.extend(direct_index_mismatches)
    report["cases"] = cases
    report["mismatches"] = mismatches
    report["status"] = "passed" if not mismatches else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "direct-index-field-parity", "status": "passed" if not mismatches else "failed"},
        {"id": "direct-index-reference-parity", "status": "passed" if not mismatches else "failed"},
    ]
    return report


def _reverse_reference_report(query_ir: Any, independent_index: Any, corpus: dict[str, Any], fixtures: dict[str, dict[str, Any]], contexts: dict[str, dict[str, Any]], source_sha: str) -> dict[str, Any]:
    report = _base_report(source_sha, status="passed")
    cases: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for expected in corpus["expectedReferences"]:
        fixture_id = str(expected["fixture"])
        document = fixtures[fixture_id]
        context = contexts[fixture_id]
        direct_rows = [
            row for row in query_ir.find_references(
                document,
                target_id=expected["targetId"],
                source_id=expected["sourceId"],
                source_collection=expected["sourceCollection"],
                pointer=expected["pointer"],
            )
            if row["toCollection"] == expected["targetCollection"] and row["ordinal"] == expected["ordinal"]
        ]
        with independent_index.open_index(context["source"], context["indexPath"]) as persistent:
            index_rows = [
                row for row in persistent.find_references(
                    expected["targetId"],
                    source_id=expected["sourceId"],
                    source_collection=expected["sourceCollection"],
                    pointer=expected["pointer"],
                )
                if row["targetCollection"] == expected["targetCollection"] and row["ordinal"] == expected["ordinal"]
            ]
        case = {
            "caseId": expected["caseId"],
            "expected": expected,
            "directCount": len(direct_rows),
            "indexCount": len(index_rows),
            "status": "passed" if direct_rows and index_rows else "failed",
        }
        if case["status"] != "passed":
            mismatches.append(case)
        cases.append(case)
    report["cases"] = cases
    report["mismatches"] = mismatches
    report["status"] = "passed" if not mismatches else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "literal-reference-closure", "status": "passed" if cases and not mismatches else "failed"},
        {"id": "list-ordinal-preserved", "status": "passed" if any(item["expected"].get("ordinal") is not None for item in cases) else "failed"},
        {"id": "nested-map-payload-reference-query", "status": "passed" if any(item["expected"].get("pointer") == "/anchor/surfaceId" for item in cases) else "failed"},
    ]
    return report


def _copy_index(source: Path, index_path: Path, destination: Path, independent_index: Any) -> tuple[Path, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied_index = destination.with_suffix(".sqlite")
    shutil.copyfile(index_path, copied_index)
    shutil.copyfile(independent_index.manifest_path_for(index_path), independent_index.manifest_path_for(copied_index))
    return source, copied_index


def _expect_rejection(case_id: str, callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        callback()
    except (Exception,) as exc:  # the lane records the concrete fail-closed diagnostic
        return {"caseId": case_id, "status": "killed", "diagnostic": f"{type(exc).__name__}: {exc}"}
    return {"caseId": case_id, "status": "survived", "diagnostic": "operation unexpectedly succeeded"}


def _integrity_report(independent_index: Any, corpus: dict[str, Any], fixtures: dict[str, dict[str, Any]], contexts: dict[str, dict[str, Any]], workspace: Path, source_sha: str) -> dict[str, Any]:
    fixture_id = "callout"
    context = contexts[fixture_id]
    source = context["source"]
    index_path = context["indexPath"]
    workspace.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    source_value = _read_json(source)

    stale_source = workspace / "stale-source.json"
    stale_value = copy.deepcopy(source_value)
    stale_value["documentId"] = "doc-callout-stale"
    _write_source(stale_source, stale_value)
    results.append(_expect_rejection("stale-source-digest", lambda: independent_index.open_index(stale_source, index_path)))

    wrong_profile = workspace / "wrong-profile.json"
    wrong_profile_value = copy.deepcopy(source_value)
    wrong_profile_value["conversion"]["capabilityProfile"] = "__wrong-profile__"
    _write_source(wrong_profile, wrong_profile_value)
    results.append(_expect_rejection("wrong-capability-profile", lambda: independent_index.open_index(wrong_profile, index_path)))

    altered_contract = workspace / "altered-query-contract.json"
    altered_contract_value = _read_json(ROOT / "machine" / "query-contract.json")
    altered_contract_value["version"] = "1.3.1"
    _write_json(altered_contract, altered_contract_value)
    altered_index = workspace / "wrong-contract.sqlite"
    independent_index.build_index(source, altered_index, query_contract_path=altered_contract)
    results.append(_expect_rejection("wrong-query-contract", lambda: independent_index.open_index(source, altered_index)))

    newer_index = workspace / "newer-version.sqlite"
    _copy_index(source, index_path, newer_index, independent_index)
    newer_manifest_path = independent_index.manifest_path_for(newer_index)
    newer_manifest = _read_json(newer_manifest_path)
    newer_manifest["indexVersion"] = "99.0.0"
    _write_json(newer_manifest_path, newer_manifest)
    results.append(_expect_rejection("unsupported-newer-index-version", lambda: independent_index.open_index(source, newer_index)))

    bad_digest = workspace / "bad-digest.sqlite"
    _copy_index(source, index_path, bad_digest, independent_index)
    bad_manifest_path = independent_index.manifest_path_for(bad_digest)
    bad_manifest = _read_json(bad_manifest_path)
    bad_manifest["databaseSha256"] = "0" * 64
    bad_manifest["integrityChecksum"] = _manifest_checksum(bad_manifest)
    _write_json(bad_manifest_path, bad_manifest)
    results.append(_expect_rejection("manifest-digest-corruption", lambda: independent_index.open_index(source, bad_digest)))

    deleted_row = workspace / "deleted-row.sqlite"
    _copy_index(source, index_path, deleted_row, independent_index)
    connection = sqlite3.connect(str(deleted_row))
    try:
        connection.execute(
            "DELETE FROM fields WHERE collection = ? AND entity_id = ? AND pointer = ?",
            ("extensions", "extension-docx-callout", "/payload/presetGeometry"),
        )
        connection.commit()
    finally:
        connection.close()
    results.append(_expect_rejection("deleted-index-row", lambda: independent_index.open_index(source, deleted_row)))

    partial = workspace / "partial.sqlite"
    _copy_index(source, index_path, partial, independent_index)
    partial.write_bytes(partial.read_bytes()[: max(1, partial.stat().st_size // 4)])
    results.append(_expect_rejection("partial-index-write", lambda: independent_index.open_index(source, partial)))

    deleted_registration = workspace / "deleted-registration.json"
    deleted_contract = _read_json(ROOT / "machine" / "query-contract.json")
    removed = False
    for group_name in ("fieldPaths", "documentFieldPaths", "extensionFieldPaths"):
        group = deleted_contract[group_name]
        retained = []
        for field in group:
            is_deleted_extension_path = (
                field.get("ownerCollection") == "extensions"
                and (
                    field.get("extension", {}).get("type") == "drawingml-callout"
                    or not field.get("extension")
                )
                and field.get("path") in {"/payload", "/payload/presetGeometry", "/payload/**"}
            )
            if is_deleted_extension_path:
                removed = True
            else:
                retained.append(field)
        deleted_contract[group_name] = retained
    if not removed:
        raise QualificationError("could not construct deleted query registration mutation")
    deleted_contract["fieldPathCount"] = sum(
        len(deleted_contract[group_name])
        for group_name in ("fieldPaths", "documentFieldPaths", "extensionFieldPaths")
    )
    _write_json(deleted_registration, deleted_contract)
    results.append(_expect_rejection(
        "deleted-query-field-registration",
        lambda: independent_index.build_index(source, workspace / "deleted-registration.sqlite", query_contract_path=deleted_registration),
    ))

    changed_reference = workspace / "changed-reference.sqlite"
    _copy_index(source, index_path, changed_reference, independent_index)
    connection = sqlite3.connect(str(changed_reference))
    try:
        connection.execute(
            "UPDATE reverse_references SET ordinal = ? WHERE source_collection = ? AND source_id = ? AND source_pointer = ?",
            (99, "nodes", "node-callout", "/layoutIds/0"),
        )
        connection.commit()
    finally:
        connection.close()
    results.append(_expect_rejection("changed-reference-ordinal", lambda: independent_index.open_index(source, changed_reference)))

    report = _base_report(source_sha, status="passed")
    expected_diagnostics = {
        item["caseId"]: item.get("expectedDiagnostic")
        for item in corpus.get("negativeCases", [])
        if isinstance(item, dict)
    }
    for result in results:
        expected = expected_diagnostics.get(result["caseId"])
        result["expectedDiagnostic"] = expected
        result["diagnosticMatched"] = (
            result["status"] == "killed"
            and (not expected or str(expected) in str(result.get("diagnostic", "")))
        )
    report["cases"] = results
    report["mismatches"] = [item for item in results if not item["diagnosticMatched"]]
    report["negativeCaseCount"] = len(results)
    report["survivorCount"] = len(report["mismatches"])
    report["status"] = "passed" if not report["mismatches"] else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "stale-corrupt-partial-all-rejected", "status": "passed" if not report["mismatches"] else "failed"},
        {"id": "query-contract-deletion-rejected", "status": "passed" if any(item["caseId"] == "deleted-query-field-registration" and item["status"] == "killed" for item in results) else "failed"},
        {"id": "reference-ordinal-tamper-rejected", "status": "passed" if any(item["caseId"] == "changed-reference-ordinal" and item["status"] == "killed" for item in results) else "failed"},
    ]
    return report


def _resource_report(query_ir: Any, independent_index: Any, contract: dict[str, Any], fixtures: dict[str, dict[str, Any]], contexts: dict[str, dict[str, Any]], corpus: dict[str, Any], source_sha: str) -> dict[str, Any]:
    report = _base_report(source_sha, status="passed")
    policy = corpus["resourcePolicy"]
    cases: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for fixture_id, document in fixtures.items():
        started = time.perf_counter()
        direct_first = query_ir.query_fields(document, "/kind", operator="exists", collection="nodes")
        direct_second = query_ir.query_fields(document, "/kind", operator="exists", collection="nodes")
        direct_seconds = time.perf_counter() - started
        context = contexts[fixture_id]
        with independent_index.open_index(context["source"], context["indexPath"]) as persistent:
            index_first = persistent.query_fields("/kind", operator="exists", collection="nodes")
            index_second = persistent.query_fields("/kind", operator="exists", collection="nodes")
        deterministic = _equal(direct_first, direct_second) and _equal(index_first, index_second)
        bounded = max(len(direct_first), len(index_first)) <= int(policy["maxPageSize"])
        item = {
            "fixture": fixture_id,
            "directResultCount": len(direct_first),
            "indexResultCount": len(index_first),
            "maxPageSize": policy["maxPageSize"],
            "deterministic": deterministic,
            "directElapsedSeconds": round(direct_seconds, 6),
            "status": "passed" if deterministic and bounded else "failed",
        }
        cases.append(item)
        if item["status"] != "passed":
            mismatches.append(item)
    report["policy"] = policy
    report["contractVersion"] = contract.get("version")
    report["cases"] = cases
    report["mismatches"] = mismatches
    report["status"] = "passed" if not mismatches else "failed"
    report["completionStatus"] = "qualified-focused-lane" if report["status"] == "passed" else "incomplete-focused-lane"
    report["assertions"] = [
        {"id": "repeated-query-determinism", "status": "passed" if not mismatches else "failed"},
        {"id": "bounded-fixture-results", "status": "passed" if all(item["status"] == "passed" for item in cases) else "failed"},
    ]
    return report


def _producer_report(
    corpus: dict[str, Any] | None,
    reports: dict[str, dict[str, Any]],
    *,
    corpus_path: Path,
    out_dir: Path,
    source_sha: str,
) -> dict[str, Any]:
    """Build the issue-specific producer envelope from semantic report records.

    The authority records are copied from the authored corpus into the direct
    oracle report, while actual records come from the persistent-index,
    integrity, reference, and resource reports.  The envelope never treats a
    command return code, a report's existence, or an aggregate report status as
    a qualification assertion.
    """

    coverage = reports.get("query-field-coverage.json")
    direct = reports.get("direct-oracle-conformance.json")
    indexed = reports.get("index-oracle-conformance.json")
    parity = reports.get("direct-index-parity.json")
    references = reports.get("reverse-reference-closure.json")
    integrity = reports.get("index-version-integrity.json")
    resources = reports.get("query-resource-limits.json")
    report_by_name = {
        name: reports.get(name)
        for name in REPORT_NAMES
        if isinstance(reports.get(name), dict)
    }
    touched: set[str] = set()
    case_specs: list[dict[str, Any]] = []

    def records(report_name: str, key: str) -> list[dict[str, Any]]:
        report = reports.setdefault(report_name, _failed_report(source_sha, f"semantic report unavailable: {report_name}"))
        touched.add(report_name)
        return report.setdefault(key, [])

    def add_support(report_name: str, case_id: str, assertion_id: str, actual: Any, target: dict[str, Any]) -> str:
        pointer = _append_producer_record(
            reports.setdefault(report_name, _failed_report(source_sha, f"semantic report unavailable: {report_name}")),
            "producerSupport",
            {
                "assertionId": assertion_id,
                "caseId": case_id,
                "actual": actual,
                "target": target,
                "status": "passed",
            },
        )
        touched.add(report_name)
        return pointer

    def add_case(
        *,
        case_id: str,
        classification: str,
        assertion_type: str,
        authority_report: str,
        authority_pointer: str,
        actual_report: str,
        actual_pointer: str,
        input_report: str,
        target: dict[str, Any],
        diagnostic: dict[str, str],
    ) -> None:
        assertion_id = f"issue-103:{case_id}"
        input_pointer = _append_producer_record(
            reports.setdefault(input_report, _failed_report(source_sha, f"semantic report unavailable: {input_report}")),
            "producerInput",
            {"caseId": case_id, "target": target, "status": "passed"},
        )
        support_actual = _pointer_value(reports[actual_report], actual_pointer)
        support_case = add_support("direct-index-parity.json", case_id, case_id, support_actual, target)
        support_assertion = add_support("direct-index-parity.json", case_id, assertion_id, support_actual, target)
        case_specs.append({
            "caseId": case_id,
            "classification": classification,
            "assertionType": assertion_type,
            "assertionId": assertion_id,
            "authorityReport": authority_report,
            "authorityPointer": authority_pointer,
            "actualReport": actual_report,
            "actualPointer": actual_pointer,
            "inputReport": input_report,
            "inputPointer": input_pointer,
            "supportCasePointer": support_case,
            "supportAssertionPointer": support_assertion,
            "target": target,
            "diagnostic": diagnostic,
        })

    if isinstance(corpus, dict):
        expected_facts = corpus.get("expectedFacts", [])
        direct_cases = direct.get("cases", []) if isinstance(direct, dict) else []
        index_cases = indexed.get("cases", []) if isinstance(indexed, dict) else []
        direct_authority = records("direct-oracle-conformance.json", "producerAuthority")
        index_actual = records("index-oracle-conformance.json", "producerActual")
        for index, fact in enumerate(expected_facts):
            target = {
                "collection": fact.get("collection"),
                "entityId": fact.get("entityId"),
                "pointer": fact.get("pointer"),
            }
            expected = fact.get("value")
            actual = index_cases[index].get("actual") if index < len(index_cases) else None
            authority_index = len(direct_authority)
            direct_authority.append({"caseId": fact["caseId"], "expected": expected, "target": target, "status": "passed"})
            actual_index = len(index_actual)
            index_actual.append({"caseId": fact["caseId"], "actual": actual, "target": target, "status": "passed" if index < len(index_cases) else "failed"})
            add_case(
                case_id=fact["caseId"],
                classification="positive",
                assertion_type="differential-equality",
                authority_report="direct-oracle-conformance.json",
                authority_pointer=f"/producerAuthority/{authority_index}/expected",
                actual_report="index-oracle-conformance.json",
                actual_pointer=f"/producerActual/{actual_index}/actual",
                input_report="query-field-coverage.json",
                target=target,
                diagnostic={"code": "QUERY_INDEX_FACT", "message": "authored field fact agrees with the independent persistent index"},
            )

        typed_authority = records("direct-oracle-conformance.json", "producerNegativeAuthority")
        typed_actual = records("index-oracle-conformance.json", "producerNegativeActual")
        index_negative_cases = indexed.get("negativeCases", []) if isinstance(indexed, dict) else []
        for index, negative in enumerate(corpus.get("typedNegativeCases", [])):
            target = {
                "fixture": negative.get("fixture"),
                "collection": negative.get("collection"),
                "entityId": negative.get("entityId"),
                "pointer": negative.get("pointer"),
                "operator": negative.get("operator"),
            }
            expected = {"survived": False}
            actual = {"survived": bool(index_negative_cases[index].get("survived"))} if index < len(index_negative_cases) else {"survived": True}
            authority_index = len(typed_authority)
            typed_authority.append({"caseId": negative["caseId"], "expected": expected, "target": target, "status": "passed"})
            actual_index = len(typed_actual)
            typed_actual.append({"caseId": negative["caseId"], "actual": actual, "target": target, "status": "passed" if index < len(index_negative_cases) else "failed"})
            add_case(
                case_id=negative["caseId"],
                classification="negative",
                assertion_type="differential-equality",
                authority_report="direct-oracle-conformance.json",
                authority_pointer=f"/producerNegativeAuthority/{authority_index}/expected",
                actual_report="index-oracle-conformance.json",
                actual_pointer=f"/producerNegativeActual/{actual_index}/actual",
                input_report="query-field-coverage.json",
                target=target,
                diagnostic={"code": "QUERY_INDEX_NEGATIVE", "message": "the independent index rejects the authored invalid query or type"},
            )

        mutation_authority = records("query-field-coverage.json", "producerMutationAuthority")
        mutation_actual = records("index-version-integrity.json", "producerActual")
        integrity_cases = integrity.get("cases", []) if isinstance(integrity, dict) else []
        for index, negative in enumerate(corpus.get("negativeCases", [])):
            expected = {"killed": True, "diagnosticMatched": True}
            observed = integrity_cases[index] if index < len(integrity_cases) else {}
            actual = {
                "killed": observed.get("status") == "killed",
                "diagnosticMatched": observed.get("diagnosticMatched") is True,
            }
            target = {"caseId": negative.get("caseId"), "expectedDiagnostic": negative.get("expectedDiagnostic")}
            authority_index = len(mutation_authority)
            mutation_authority.append({"caseId": negative["caseId"], "expected": expected, "target": target, "status": "passed"})
            actual_index = len(mutation_actual)
            mutation_actual.append({"caseId": negative["caseId"], "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"mutation-{negative['caseId']}",
                classification="mutation",
                assertion_type="differential-equality",
                authority_report="query-field-coverage.json",
                authority_pointer=f"/producerMutationAuthority/{authority_index}/expected",
                actual_report="index-version-integrity.json",
                actual_pointer=f"/producerActual/{actual_index}/actual",
                input_report="query-field-coverage.json",
                target=target,
                diagnostic={"code": "QUERY_INDEX_MUTATION", "message": "index integrity tampering was rejected with the authored diagnostic"},
            )

        reference_authority = records("query-field-coverage.json", "producerReferenceAuthority")
        reference_actual = records("reverse-reference-closure.json", "producerActual")
        reference_cases = references.get("cases", []) if isinstance(references, dict) else []
        for index, expected_reference in enumerate(corpus.get("expectedReferences", [])):
            expected = {"closed": True, "ordinal": expected_reference.get("ordinal")}
            observed = reference_cases[index] if index < len(reference_cases) else {}
            actual = {
                "closed": bool(observed.get("directCount")) and bool(observed.get("indexCount")),
                "ordinal": expected_reference.get("ordinal"),
            }
            target = {
                "sourceCollection": expected_reference.get("sourceCollection"),
                "sourceId": expected_reference.get("sourceId"),
                "pointer": expected_reference.get("pointer"),
                "targetCollection": expected_reference.get("targetCollection"),
                "targetId": expected_reference.get("targetId"),
            }
            authority_index = len(reference_authority)
            reference_authority.append({"caseId": expected_reference["caseId"], "expected": expected, "target": target, "status": "passed"})
            actual_index = len(reference_actual)
            reference_actual.append({"caseId": expected_reference["caseId"], "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"reference-{expected_reference['caseId']}",
                classification="positive",
                assertion_type="query-index-parity",
                authority_report="query-field-coverage.json",
                authority_pointer=f"/producerReferenceAuthority/{authority_index}/expected",
                actual_report="reverse-reference-closure.json",
                actual_pointer=f"/producerActual/{actual_index}/actual",
                input_report="query-field-coverage.json",
                target=target,
                diagnostic={"code": "QUERY_INDEX_REFERENCE", "message": "direct and persistent reverse-reference closure agrees"},
            )

        resource_authority = records("query-field-coverage.json", "producerResourceAuthority")
        resource_actual = records("query-resource-limits.json", "producerActual")
        resource_cases = resources.get("cases", []) if isinstance(resources, dict) else []
        for index, fixture_id in enumerate(sorted({str(item.get("fixture")) for item in resource_cases if isinstance(item, dict)})):
            observed = next((item for item in resource_cases if item.get("fixture") == fixture_id), {})
            expected = {"deterministic": True, "bounded": True}
            actual = {"deterministic": observed.get("deterministic") is True, "bounded": observed.get("status") == "passed"}
            target = {"fixture": fixture_id, "policy": corpus.get("resourcePolicy", {})}
            authority_index = len(resource_authority)
            resource_authority.append({"caseId": f"resource-{fixture_id}", "expected": expected, "target": target, "status": "passed"})
            actual_index = len(resource_actual)
            resource_actual.append({"caseId": f"resource-{fixture_id}", "actual": actual, "target": target, "status": "passed" if actual == expected else "failed"})
            add_case(
                case_id=f"resource-{fixture_id}",
                classification="positive",
                assertion_type="query-index-parity",
                authority_report="query-field-coverage.json",
                authority_pointer=f"/producerResourceAuthority/{authority_index}/expected",
                actual_report="query-resource-limits.json",
                actual_pointer=f"/producerActual/{actual_index}/actual",
                input_report="query-field-coverage.json",
                target=target,
                diagnostic={"code": "QUERY_INDEX_RESOURCE", "message": "repeated queries are deterministic and bounded by the authored policy"},
            )

    if not case_specs:
        authority = records("query-field-coverage.json", "producerSetupAuthority")
        actual = records("direct-oracle-conformance.json", "producerSetupActual")
        target = {"lane": "issue-103", "evidence": "semantic qualification setup"}
        authority_index = len(authority)
        actual_index = len(actual)
        authority.append({"caseId": "setup-unavailable", "expected": {"available": True}, "target": target, "status": "passed"})
        actual.append({"caseId": "setup-unavailable", "actual": {"available": False}, "target": target, "status": "passed"})
        add_case(
            case_id="setup-unavailable",
            classification="negative",
            assertion_type="differential-equality",
            authority_report="query-field-coverage.json",
            authority_pointer=f"/producerSetupAuthority/{authority_index}/expected",
            actual_report="direct-oracle-conformance.json",
            actual_pointer=f"/producerSetupActual/{actual_index}/actual",
            input_report="query-field-coverage.json",
            target=target,
            diagnostic={"code": "QUERY_INDEX_SETUP_UNAVAILABLE", "message": "semantic query/index evidence was unavailable"},
        )

    for name in REPORT_NAMES:
        if name in touched or name in report_by_name:
            _write_json(out_dir / name, reports[name])

    input_digests, unavailable_inputs = _producer_input_digests(corpus_path)
    uncovered = list(unavailable_inputs)
    if not isinstance(corpus, dict):
        uncovered.append("issue-103 authored corpus is unavailable")
    for name in REPORT_NAMES:
        if not (out_dir / name).is_file():
            uncovered.append(f"semantic report unavailable: {name}")
    failures = 0
    producer_cases: list[dict[str, Any]] = []
    producer_assertions: list[dict[str, Any]] = []
    for spec in case_specs:
        try:
            authority_local = out_dir / spec["authorityReport"]
            actual_local = out_dir / spec["actualReport"]
            input_local = out_dir / spec["inputReport"]
            authority_ref = _producer_artifact_reference(authority_local, f"{BUNDLE_PREFIX}/{spec['authorityReport']}", spec["authorityPointer"])
            actual_ref = _producer_artifact_reference(actual_local, f"{BUNDLE_PREFIX}/{spec['actualReport']}", spec["actualPointer"])
            input_ref = _producer_artifact_reference(input_local, f"{BUNDLE_PREFIX}/{spec['inputReport']}", spec["inputPointer"])
            support_report = out_dir / "direct-index-parity.json"
            support_case_ref = _producer_artifact_reference(support_report, f"{BUNDLE_PREFIX}/direct-index-parity.json", spec["supportCasePointer"])
            support_assertion_ref = _producer_artifact_reference(support_report, f"{BUNDLE_PREFIX}/direct-index-parity.json", spec["supportAssertionPointer"])
            expected = _pointer_value(_read_json(authority_local), spec["authorityPointer"])
            actual_value = _pointer_value(_read_json(actual_local), spec["actualPointer"])
            passed = _equal(expected, actual_value)
            case = {
                "caseId": spec["caseId"],
                "requirementId": REQUIREMENT_ID,
                "classification": spec["classification"],
                "inputArtifact": input_ref,
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual_value,
                "comparison": {"operator": "equal"},
                "result": "passed" if passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_case_ref,
            }
            assertion = {
                "assertionId": spec["assertionId"],
                "requirementId": REQUIREMENT_ID,
                "assertionType": spec["assertionType"],
                "testCaseId": spec["caseId"],
                "classification": spec["classification"],
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": expected,
                "actual": actual_value,
                "comparison": {"operator": "equal"},
                "status": "passed" if passed else "failed",
                "target": spec["target"],
                "diagnostic": spec["diagnostic"],
                "supportingArtifact": support_assertion_ref,
            }
            producer_cases.append(case)
            producer_assertions.append(assertion)
            if not passed:
                failures += 2
        except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError, QualificationError) as exc:
            failures += 2
            uncovered.append(f"{spec['caseId']}: semantic artifact could not be resolved ({type(exc).__name__}: {exc})")

    component_paths = [ROOT / "tools" / "qualification_issue103.py", Path(corpus_path), EVALUATOR_PATH]
    component_digests: list[str] = []
    for path in component_paths:
        if path.is_file():
            component_digests.append(_sha256_file(path))
        else:
            uncovered.append(f"independence component unavailable: {path}")
            component_digests.append(_sha256_bytes(f"missing:{path.as_posix()}".encode("utf-8")))
    shared_digest = _sha256_file(SHARED_EVIDENCE_PATH) if SHARED_EVIDENCE_PATH.is_file() else _sha256_bytes(b"missing:qualification_evidence")
    if not SHARED_EVIDENCE_PATH.is_file():
        uncovered.append("shared artifact-reference evaluator unavailable")
    status = "failed" if failures else "blocked" if uncovered else "passed"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": EVIDENCE_ID,
        "requirementIds": [REQUIREMENT_ID],
        "sourceSha": source_sha,
        "inputDigests": input_digests,
        "producerId": "issue-103-query-index-runner",
        "authorityId": "issue-103-hand-reviewed-corpus",
        "independence": {
            "producerComponentDigest": component_digests[0],
            "authorityComponentDigest": component_digests[1],
            "evaluatorComponentDigest": component_digests[2],
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [shared_digest],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": sorted(set(uncovered)),
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failures,
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    source_sha = _source_sha()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports: dict[str, dict[str, Any]] = {}
    try:
        corpus = _read_json(Path(corpus_path))
        if not isinstance(corpus, dict):
            raise QualificationError("issue-103 corpus is not an object")
        _validate_corpus(corpus)
        fixtures = _load_fixtures(corpus)
        query_ir, independent_index = _load_tools()
        contract = query_ir.query_contract()
        contexts = _build_indexes(independent_index, fixtures, out_dir / "index-work")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        for name in REPORT_NAMES:
            reports[name] = _failed_report(source_sha, error)
        for name, report in reports.items():
            _write_json(out_dir / name, report)
        _write_json(out_dir / PRODUCER_REPORT_NAME, _producer_report(None, reports, corpus_path=Path(corpus_path), out_dir=out_dir, source_sha=source_sha))
        return 1

    def execute(name: str, callback: Callable[[], dict[str, Any]]) -> None:
        try:
            reports[name] = callback()
        except Exception as exc:
            reports[name] = _failed_report(source_sha, f"{type(exc).__name__}: {exc}")

    execute("query-field-coverage.json", lambda: _coverage_report(query_ir, contract, fixtures, source_sha))
    execute("direct-oracle-conformance.json", lambda: _direct_oracle_report(query_ir, corpus, fixtures, source_sha))
    execute("index-oracle-conformance.json", lambda: _index_oracle_report(independent_index, corpus, fixtures, contexts, source_sha))
    execute("direct-index-parity.json", lambda: _parity_report(query_ir, independent_index, corpus, fixtures, contexts, source_sha))
    execute("reverse-reference-closure.json", lambda: _reverse_reference_report(query_ir, independent_index, corpus, fixtures, contexts, source_sha))
    execute("index-version-integrity.json", lambda: _integrity_report(independent_index, corpus, fixtures, contexts, out_dir / "integrity-work", source_sha))
    execute("query-resource-limits.json", lambda: _resource_report(query_ir, independent_index, contract, fixtures, contexts, corpus, source_sha))
    for name in REPORT_NAMES:
        _write_json(out_dir / name, reports[name])
    producer = _producer_report(corpus, reports, corpus_path=Path(corpus_path), out_dir=out_dir, source_sha=source_sha)
    _write_json(out_dir / PRODUCER_REPORT_NAME, producer)
    return 0 if producer["status"] == "passed" and all(report.get("status") == "passed" for report in reports.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
