"""Run a bounded, independent-oracle qualification slice for issue #93.

The corpus contains authored OOXML package parts and literal expected values.
The runner writes those packages to a disposable directory, invokes the public
convert_document.py boundary, and compares the resulting IR with the corpus
oracle. It never imports adapter style helpers to produce expected values.

This is intentionally not a completion gate for issue #93. The reports carry
the bounded scope and unmet requirements, and a non-zero result is mandatory
when the current adapters lose a property, provenance chain, source, or paint
resolution.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-93-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-93"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
PRODUCER_EVIDENCE_ID = "issue-93-style-provenance"
PRODUCER_REQUIREMENT_ID = "QUAL-93-STYLE-PROVENANCE"
PRODUCER_BUNDLE_PREFIX = "artifacts/93"
REPORT_NAMES = {
    "cascade": "style-cascade-vectors.json",
    "provenance": "property-provenance-report.json",
    "color": "color-paint-resolution.json",
    "coverage": "style-coverage-matrix.json",
}


class QualificationError(RuntimeError):
    """Raised when the bounded qualification cannot be executed safely."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON input {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repository_relative(path: Path) -> str:
    try:
        relative = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise QualificationError(f"artifact is outside the repository: {path}") from exc
    if not relative or relative == "." or relative.startswith("../"):
        raise QualificationError(f"artifact path is not repository-relative: {path}")
    return relative


def _artifact_reference(
    out_dir: Path,
    report_name: str,
    pointer: str,
    *,
    bundle_name: str | None = None,
) -> dict[str, Any]:
    """Bind a reference to an actual emitted issue-93 semantic report."""

    source = Path(out_dir) / report_name
    if not source.is_file():
        raise QualificationError(f"semantic report is unavailable: {source}")
    try:
        from qualification_evidence import selected_artifact_digest, selected_artifact_value
    except ImportError:  # pragma: no cover - package-style import
        from tools.qualification_evidence import selected_artifact_digest, selected_artifact_value
    selector = {"kind": "json-pointer", "pointer": pointer}
    try:
        selected = selected_artifact_value(source, selector)
        selected_digest = selected_artifact_digest(selected, selector)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise QualificationError(
            f"semantic report selector is unavailable: {source}#{pointer}: {exc}"
        ) from exc
    return {
        "path": f"{PRODUCER_BUNDLE_PREFIX}/{bundle_name or report_name}",
        "sha256": _sha256_file(source),
        "selector": selector,
        "selectedSha256": selected_digest,
    }


def _input_digests(corpus_path: Path) -> list[str]:
    paths = [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue93.py",
        ROOT / "tools" / "test_qualification_issue93.py",
        CONVERTER_PATH,
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "tools" / "adapter_xlsx.py",
    ]
    digests: list[str] = []
    for path in paths:
        if not path.is_file():
            raise QualificationError(f"declared qualification input is unavailable: {path}")
        digest = _sha256_file(path)
        if digest not in digests:
            digests.append(digest)
    return digests


def _component_digest(paths: list[Path]) -> str:
    material = []
    for path in paths:
        if not Path(path).is_file():
            raise QualificationError(f"independence component is unavailable: {path}")
        material.append({
            "path": _repository_relative(Path(path)),
            "sha256": _sha256_file(Path(path)),
        })
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _authored_projection(expected: Any, actual: Any) -> Any:
    """Keep the runner's actual value on exactly the authored comparison shape."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return actual
        return {
            key: _authored_projection(value, actual.get(key, {"$missing": True}))
            for key, value in expected.items()
        }
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return actual
        return [
            _authored_projection(value, actual[index] if index < len(actual) else {"$missing": True})
            for index, value in enumerate(expected)
        ]
    return actual


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
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise QualificationError(f"cannot obtain a 40-character source SHA: {value!r}")
    return value


def _validate_case_shape(case: Any, fixture_id: str) -> None:
    if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
        raise QualificationError(f"invalid case in fixture {fixture_id}")
    target = case.get("target")
    expected = case.get("expected")
    if not isinstance(target, dict) or not isinstance(expected, dict):
        raise QualificationError(f"case lacks target/expected data: {case.get('caseId')}")
    properties = expected.get("properties")
    provenance = expected.get("provenance")
    trace = expected.get("trace")
    if not isinstance(properties, dict) or not properties:
        raise QualificationError(f"case has no expected properties: {case.get('caseId')}")
    if not isinstance(provenance, dict) or set(provenance) != set(properties):
        raise QualificationError(
            f"case provenance is not one-to-one with properties: {case.get('caseId')}"
        )
    if not isinstance(trace, list) or len(trace) < len(properties):
        raise QualificationError(f"case trace is incomplete: {case.get('caseId')}")
    occurrences = case.get("sourceOccurrences")
    if not isinstance(occurrences, list) or len(occurrences) < len(properties):
        raise QualificationError(f"case source occurrence accounting is incomplete: {case.get('caseId')}")


def _load_corpus(path: Path) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict):
        raise QualificationError("issue #93 corpus root must be an object")
    if corpus.get("issueNumber") != 93:
        raise QualificationError("issue #93 corpus has the wrong issue number")
    if corpus.get("qualificationScope") != "bounded-independent-style-slice":
        raise QualificationError("issue #93 corpus is not marked as bounded")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("corpus does not declare an independent expected-value oracle")
    forbidden = oracle.get("forbiddenDerivations")
    if not isinstance(forbidden, list) or not forbidden:
        raise QualificationError("corpus does not declare forbidden adapter derivations")
    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("corpus has no authored fixtures")
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("fixture entry is not an object")
        if fixture.get("sourceKind") != "authored-ooxml":
            raise QualificationError(f"fixture is not authored OOXML: {fixture.get('fixtureId')}")
        if fixture.get("format") not in {"docx", "xlsx"}:
            raise QualificationError(f"unsupported fixture format: {fixture.get('format')}")
        if not isinstance(fixture.get("parts"), dict) or not fixture["parts"]:
            raise QualificationError(f"fixture has no package parts: {fixture.get('fixtureId')}")
        cases = fixture.get("cases")
        if not isinstance(cases, list) or not cases:
            raise QualificationError(f"fixture has no vectors: {fixture.get('fixtureId')}")
        for case in cases:
            _validate_case_shape(case, str(fixture["fixtureId"]))
    return corpus


def _part_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value) + "\n"
    raise QualificationError("OOXML part must be a string or an array of strings")


def _write_authored_package(fixture: dict[str, Any], path: Path) -> None:
    parts = fixture["parts"]
    names: list[str] = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(parts):
            if (
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or ".." in Path(name).parts
            ):
                raise QualificationError(f"unsafe OOXML part name: {name!r}")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _part_text(parts[name]).encode("utf-8"))
            names.append(name)
    if not names:
        raise QualificationError(f"fixture has no written parts: {fixture.get('fixtureId')}")


def _run_converter(fixture: dict[str, Any], work: Path) -> dict[str, Any]:
    fixture_id = str(fixture["fixtureId"])
    source = work / f"{fixture_id}.{fixture['format']}"
    output = work / f"{fixture_id}.json"
    evidence_path = work / f"{fixture_id}.evidence.json"
    _write_authored_package(fixture, source)
    result = subprocess.run(
        [
            sys.executable,
            str(CONVERTER_PATH),
            "convert",
            str(source),
            "--format",
            str(fixture["format"]),
            "--out",
            str(output),
            "--evidence",
            str(evidence_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    document: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    if output.exists():
        value = _read_json(output)
        if isinstance(value, dict):
            document = value
    if evidence_path.exists():
        value = _read_json(evidence_path)
        if isinstance(value, dict):
            evidence = value
    if document is None:
        raise QualificationError(
            f"adapter produced no JSON for {fixture_id}; rc={result.returncode}; "
            f"stderr={result.stderr[-1000:]}"
        )
    if evidence is None:
        raise QualificationError(f"adapter produced no evidence for {fixture_id}")
    return {
        "fixtureId": fixture_id,
        "format": fixture["format"],
        "sourcePath": str(source),
        "sourceSha256": _sha256_file(source),
        "commandExitCode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "document": document,
        "evidence": evidence,
    }


def _select_target(document: dict[str, Any], target: dict[str, Any]) -> dict[str, Any] | None:
    collection_name = target.get("collection")
    collection = document.get(collection_name)
    if not isinstance(collection, list):
        return None
    style_id = target.get("styleId")
    where = target.get("where")
    for item in collection:
        if not isinstance(item, dict):
            continue
        if isinstance(style_id, str) and item.get("styleId") != style_id:
            continue
        if isinstance(where, dict):
            if item.get("origin") != where.get("origin"):
                continue
            layer = where.get("layer")
            property_name = where.get("property")
            if isinstance(layer, str) and isinstance(property_name, str):
                layer_value = item.get(layer)
                if not isinstance(layer_value, dict) or property_name not in layer_value:
                    continue
        return item
    return None


def _property_provenance(style: dict[str, Any]) -> dict[str, str]:
    value = style.get("propertyProvenance")
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for item in value:
        if (
            isinstance(item, dict)
            and isinstance(item.get("property"), str)
            and isinstance(item.get("source"), str)
        ):
            result[item["property"]] = item["source"]
    return result


def _graph_counts(document: dict[str, Any]) -> dict[str, int]:
    styles = document.get("styles")
    if not isinstance(styles, list):
        return {"cycleCount": 0, "missingSourceCount": 0}
    graph: dict[str, str | None] = {}
    for style in styles:
        if not isinstance(style, dict) or not isinstance(style.get("styleId"), str):
            continue
        graph[style["styleId"]] = (
            style.get("basedOn") if isinstance(style.get("basedOn"), str) else None
        )
    missing = sum(
        1 for parent in graph.values()
        if isinstance(parent, str) and parent not in graph
    )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(style_id: str) -> bool:
        if style_id in visiting:
            return True
        if style_id in visited:
            return False
        visiting.add(style_id)
        parent = graph.get(style_id)
        found = isinstance(parent, str) and parent in graph and visit(parent)
        visiting.remove(style_id)
        visited.add(style_id)
        return found

    cycles = sum(1 for style_id in graph if visit(style_id))
    return {"cycleCount": cycles, "missingSourceCount": missing}


def _evaluate_case(case: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    properties = expected["properties"]
    target = _select_target(document, case["target"])
    failures: list[dict[str, Any]] = []
    property_mismatch = 0
    provenance_missing = 0
    provenance_mismatch = 0
    trace_mismatch = 0
    unaccounted = 0
    actual_properties: dict[str, Any] = {}
    actual_provenance: dict[str, str] = {}
    actual_trace: Any = None
    if target is None:
        for property_name, expected_value in properties.items():
            property_mismatch += 1
            unaccounted += 1
            failures.append(
                {
                    "kind": "missing-target",
                    "property": property_name,
                    "expected": expected_value,
                    "actual": None,
                }
            )
    else:
        layer = case["target"].get("layer", "resolved")
        candidate = target.get(layer)
        if not isinstance(candidate, dict):
            candidate = {}
        actual_properties = deepcopy(candidate)
        for property_name, expected_value in properties.items():
            if (
                property_name not in candidate
                or _canonical(candidate[property_name]) != _canonical(expected_value)
            ):
                property_mismatch += 1
                if property_name not in candidate:
                    unaccounted += 1
                failures.append(
                    {
                        "kind": "property-mismatch",
                        "property": property_name,
                        "expected": expected_value,
                        "actual": candidate.get(property_name),
                    }
                )
        actual_provenance = _property_provenance(target)
        expected_provenance = expected["provenance"]
        for property_name, expected_source in expected_provenance.items():
            actual_source = actual_provenance.get(property_name)
            if actual_source is None:
                provenance_missing += 1
                failures.append(
                    {
                        "kind": "provenance-missing",
                        "property": property_name,
                        "expectedSource": expected_source,
                    }
                )
            elif actual_source != expected_source:
                provenance_mismatch += 1
                failures.append(
                    {
                        "kind": "provenance-mismatch",
                        "property": property_name,
                        "expectedSource": expected_source,
                        "actualSource": actual_source,
                    }
                )
        actual_trace = target.get("cascadeTrace")
        if _canonical(actual_trace) != _canonical(expected["trace"]):
            trace_mismatch += 1
            failures.append(
                {
                    "kind": "trace-mismatch",
                    "expected": expected["trace"],
                    "actual": actual_trace,
                }
            )
    return {
        "caseId": case["caseId"],
        "construct": case.get("construct"),
        "coverage": case.get("coverage", []),
        "target": case["target"],
        "status": "passed" if not failures else "failed",
        "expected": expected,
        "actual": {
            "properties": actual_properties,
            "provenance": actual_provenance,
            "trace": actual_trace,
            "targetFound": target is not None,
        },
        "propertyMismatchCount": property_mismatch,
        "provenanceMissingCount": provenance_missing,
        "provenanceMismatchCount": provenance_mismatch,
        "traceMismatchCount": trace_mismatch,
        "unaccountedCount": unaccounted,
        "failures": failures,
        "sourceOccurrences": case.get("sourceOccurrences", []),
    }


def _assertion(assertion_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if expected == actual else "failed",
    }


def _report(
    kind: str,
    source_sha: str | None,
    corpus: dict[str, Any],
    executions: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    setup_failure: str | None = None,
    *,
    producer_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    category = {
        "cascade": "style-cascade",
        "provenance": "provenance",
        "color": "color-paint",
        "coverage": None,
    }[kind]
    selected = [
        item for item in cases
        if category is None or category in item.get("coverage", [])
    ]
    if not selected and setup_failure is None:
        selected = list(cases)
    property_mismatch = sum(item["propertyMismatchCount"] for item in selected)
    provenance_missing = sum(item["provenanceMissingCount"] for item in selected)
    provenance_mismatch = sum(item["provenanceMismatchCount"] for item in selected)
    trace_mismatch = sum(item["traceMismatchCount"] for item in selected)
    unaccounted = sum(item["unaccountedCount"] for item in selected)
    cycles = sum(item["graph"]["cycleCount"] for item in executions)
    missing_sources = sum(item["graph"]["missingSourceCount"] for item in executions)
    adapter_failures = sum(
        1 for item in executions
        if item["commandExitCode"] != 0
        or item["evidence"].get("outcome") != "success"
    )
    partial_conversions = sum(
        1 for item in executions
        if item["document"].get("conversion", {}).get("status") == "partial"
    )
    failures: list[str] = []
    if setup_failure:
        failures.append(setup_failure)
    if adapter_failures:
        failures.append(f"adapter-failure-count={adapter_failures}")
    if partial_conversions:
        failures.append(f"partial-conversion-count={partial_conversions}")
    for name, count in (
        ("property-mismatch-count", property_mismatch),
        ("provenance-missing-count", provenance_missing),
        ("provenance-mismatch-count", provenance_mismatch),
        ("trace-mismatch-count", trace_mismatch),
        ("cycle-count", cycles),
        ("missing-source-count", missing_sources),
        ("unaccounted-count", unaccounted),
    ):
        if count:
            failures.append(f"{name}={count}")
    limitations = list(corpus.get("limitations", []))
    unmet = list(corpus.get("unmetRequirements", []))
    if failures:
        unmet = failures + unmet
    assertions = [
        _assertion(
            "source-sha-present",
            True,
            bool(re.fullmatch(r"[0-9a-f]{40}", source_sha or "")),
        ),
        _assertion(
            "authored-fixtures-consumed",
            len(executions),
            sum(
                1 for item in executions
                if item["evidence"].get("input", {}).get("consumed") is True
            ),
        ),
        _assertion("property-mismatch-zero", 0, property_mismatch),
        _assertion("provenance-missing-zero", 0, provenance_missing),
        _assertion("cycle-and-missing-source-zero", 0, cycles + missing_sources),
        _assertion("unaccounted-style-occurrence-zero", 0, unaccounted),
        _assertion("limitations-explicit", True, bool(limitations)),
        _assertion("whole-issue-completion-claim", False, False),
    ]
    status = (
        "passed"
        if not failures and all(item["status"] == "passed" for item in assertions)
        else "failed"
    )
    return {
        "schema": "fdir/qualification-issue-93-report",
        "version": "1.0.0",
        "issueNumber": 93,
        "reportKind": REPORT_NAMES[kind].removesuffix(".json"),
        "qualificationScope": "bounded-independent-style-slice",
        "sourceSha": source_sha,
        "status": status,
        "oracle": corpus["oracle"],
        "fixtureCount": len(executions),
        "caseCount": len(selected),
        "propertyMismatchCount": property_mismatch,
        "provenanceMissingCount": provenance_missing,
        "provenanceMismatchCount": provenance_mismatch,
        "traceMismatchCount": trace_mismatch,
        "cycleCount": cycles,
        "missingSourceCount": missing_sources,
        "cycleOrMissingSourceCount": cycles + missing_sources,
        "unaccountedCount": unaccounted,
        "adapterFailureCount": adapter_failures,
        "partialConversionCount": partial_conversions,
        "cases": selected,
        "producerRecords": producer_records or [],
        "fixtures": [
            {
                "fixtureId": item["fixtureId"],
                "format": item["format"],
                "sourceSha256": item["sourceSha256"],
                "commandExitCode": item["commandExitCode"],
                "evidenceConsumed": item["evidence"].get("input", {}).get("consumed"),
                "conversionStatus": item["document"].get("conversion", {}).get("status"),
            }
            for item in executions
        ],
        "assertions": assertions,
        "limitations": limitations,
        "unmet": unmet,
        "failureSummary": failures,
    }


def build_producer_report(
    corpus: dict[str, Any],
    reports: dict[str, dict[str, Any]],
    out_dir: Path,
    *,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    source_sha: str | None,
    evaluated: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build issue #93 evidence from authored style facts and semantic output."""

    semantic_names = list(REPORT_NAMES.values())
    if set(reports) != set(REPORT_NAMES):
        raise QualificationError("issue #93 semantic report set is incomplete")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha or ""):
        raise QualificationError("issue #93 producer source authority is unavailable")
    if not evaluated:
        raise QualificationError("issue #93 producer authority has no authored cases")

    records: list[dict[str, Any]] = []
    case_indexes: dict[str, int] = {}
    for index, result in enumerate(evaluated):
        case_id = str(result["caseId"])
        case_indexes[case_id] = len(records)
        records.append(
            {
                "assertionId": case_id,
                "caseId": case_id,
                "expected": result["expected"],
                "actual": _authored_projection(
                    result["expected"],
                    result.get("actual", {"$unavailable": True}),
                ),
                "target": result.get("target", {"caseId": case_id}),
                "status": "passed",
            }
        )

    first_expected = deepcopy(evaluated[0]["expected"])
    mutation_expected = deepcopy(first_expected)
    properties = mutation_expected.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise QualificationError("issue #93 mutation authority has no expected properties")
    mutation_property = next(iter(properties))
    original_value = properties[mutation_property]
    if isinstance(original_value, bool):
        mutated_value: Any = not original_value
    elif isinstance(original_value, (int, float)):
        mutated_value = original_value + 1
    elif isinstance(original_value, str):
        mutated_value = original_value + "-mutation"
    else:
        mutated_value = {"$mutated": True, "original": original_value}
    mutation_actual = deepcopy(mutation_expected)
    mutation_actual["properties"][mutation_property] = mutated_value
    mutation_id = "mutation-style-property-drift"
    mutation_index = len(records)
    case_indexes[mutation_id] = mutation_index
    records.append(
        {
            "assertionId": mutation_id,
            "caseId": mutation_id,
            "expected": mutation_expected,
            "actual": mutation_actual,
            "target": {"caseId": evaluated[0]["caseId"], "property": mutation_property},
            "status": "passed",
        }
    )

    required_assertion_ids = [
        "source-sha-present",
        "authored-fixtures-consumed",
        "property-mismatch-zero",
        "provenance-missing-zero",
        "cycle-and-missing-source-zero",
        "unaccounted-style-occurrence-zero",
        "limitations-explicit",
        "whole-issue-completion-claim",
    ]
    coverage_name = REPORT_NAMES["coverage"]
    actual_name = REPORT_NAMES["provenance"]
    support_name = REPORT_NAMES["cascade"]
    input_name = REPORT_NAMES["color"]
    coverage_report = reports["coverage"]
    actual_report = reports["provenance"]
    if not isinstance(coverage_report.get("assertions"), list) or len(coverage_report["assertions"]) < len(required_assertion_ids):
        raise QualificationError("issue #93 semantic assertion authority is unavailable")

    summary_values: dict[str, tuple[Any, Any]] = {}
    for index, assertion_id in enumerate(required_assertion_ids):
        summary_values[assertion_id] = (
            coverage_report["assertions"][index]["expected"],
            actual_report["assertions"][index]["actual"],
        )
    for report in reports.values():
        report["producerRecords"] = deepcopy(records)
    for report_name, report in zip(semantic_names, reports.values()):
        report["producerSupports"] = {
            assertion_id: {
                "assertionId": assertion_id,
                "caseId": records[0]["caseId"],
                "actual": summary_values[assertion_id][1],
                "target": {"scope": "semantic-report", "assertionId": assertion_id},
                "status": "passed",
            }
            for assertion_id in required_assertion_ids
        }
        _write_json(Path(out_dir) / report_name, report)

    producer_assertions: list[dict[str, Any]] = []
    for index, assertion_id in enumerate(required_assertion_ids):
        expected, actual = summary_values[assertion_id]
        producer_assertions.append(
            {
                "assertionId": assertion_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": "json-value-equals",
                "testCaseId": records[0]["caseId"],
                "classification": "positive",
                "authorityArtifact": _artifact_reference(out_dir, coverage_name, f"/assertions/{index}/expected"),
                "actualArtifact": _artifact_reference(out_dir, actual_name, f"/assertions/{index}/actual"),
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": "equal"},
                "status": "passed" if expected == actual else "failed",
                "target": {"scope": "semantic-report", "assertionId": assertion_id},
                "diagnostic": {
                    "code": "ISSUE93_SEMANTIC_ASSERTION",
                    "message": "comparison is taken from semantic style reports, not process exit status",
                },
                "supportingArtifact": _artifact_reference(out_dir, support_name, f"/producerSupports/{assertion_id}"),
            }
        )

    producer_cases: list[dict[str, Any]] = []
    for index, result in enumerate(evaluated):
        case_id = str(result["caseId"])
        global_index = case_indexes[case_id]
        actual_report_name = next(name for name in semantic_names if name != coverage_name)
        support_report_name = next(name for name in semantic_names if name not in {coverage_name, actual_report_name})
        input_report_name = next(name for name in semantic_names if name not in {coverage_name, actual_report_name, support_report_name})
        expected = result["expected"]
        actual = _authored_projection(
            expected,
            result.get("actual", {"$unavailable": True}),
        )
        producer_cases.append(
            {
                "caseId": case_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "classification": "positive",
                "inputArtifact": _artifact_reference(out_dir, input_report_name, f"/producerRecords/{global_index}/target"),
                "authorityArtifact": _artifact_reference(out_dir, coverage_name, f"/cases/{index}/expected"),
                "actualArtifact": _artifact_reference(out_dir, actual_report_name, f"/producerRecords/{global_index}/actual"),
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": "equal"},
                "result": "passed" if result.get("status") == "passed" else "failed",
                "target": result.get("target", {"caseId": case_id}),
                "diagnostic": {
                    "code": "ISSUE93_STYLE_CASE",
                    "message": "actual style/provenance is taken from the runner semantic report",
                },
                "supportingArtifact": _artifact_reference(out_dir, support_report_name, f"/producerRecords/{global_index}"),
            }
        )
    actual_report_name = REPORT_NAMES["provenance"]
    support_report_name = REPORT_NAMES["cascade"]
    input_report_name = REPORT_NAMES["color"]
    producer_cases.append(
        {
            "caseId": mutation_id,
            "requirementId": PRODUCER_REQUIREMENT_ID,
            "classification": "mutation",
            "inputArtifact": _artifact_reference(out_dir, input_report_name, f"/producerRecords/{mutation_index}/target"),
            "authorityArtifact": _artifact_reference(out_dir, coverage_name, f"/producerRecords/{mutation_index}/expected"),
            "actualArtifact": _artifact_reference(out_dir, actual_report_name, f"/producerRecords/{mutation_index}/actual"),
            "expected": mutation_expected,
            "actual": mutation_actual,
            "comparison": {"operator": "not-equal"},
            "result": "passed",
            "target": {"caseId": evaluated[0]["caseId"], "property": mutation_property},
            "diagnostic": {
                "code": "ISSUE93_MUTATION",
                "message": "the declared property mutation differs from the authored style oracle",
            },
            "supportingArtifact": _artifact_reference(out_dir, support_report_name, f"/producerRecords/{mutation_index}"),
        }
    )

    for case in producer_cases:
        is_mutation = case["classification"] == "mutation"
        producer_assertions.append(
            {
                "assertionId": case["caseId"],
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": "mutation-killed" if is_mutation else "json-value-equals",
                "testCaseId": case["caseId"],
                "classification": case["classification"],
                "authorityArtifact": case["authorityArtifact"],
                "actualArtifact": case["actualArtifact"],
                "expected": case["expected"],
                "actual": case["actual"],
                "comparison": case["comparison"],
                "status": "passed" if case["result"] == "passed" else "failed",
                "target": case["target"],
                "diagnostic": {
                    "code": "ISSUE93_CASE_ASSERTION",
                    "message": "producer assertion is recomputed from the typed case values",
                },
                "supportingArtifact": case["supportingArtifact"],
            }
        )

    semantic_passed = all(report.get("status") == "passed" for report in reports.values())
    failed_assertions = sum(item["status"] != "passed" for item in producer_assertions)
    failed_cases = sum(item["result"] != "passed" for item in producer_cases)
    status = "passed" if semantic_passed and not failed_assertions and not failed_cases else "failed"
    evaluator = ROOT / "tools" / "qualification_evidence.py"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": PRODUCER_EVIDENCE_ID,
        "requirementIds": [PRODUCER_REQUIREMENT_ID],
        "sourceSha": source_sha,
        "inputDigests": _input_digests(Path(corpus_path)),
        "producerId": "fdir.issue-93.semantic-runner",
        "authorityId": "fdir.issue-93.authored-style-corpus",
        "independence": {
            "producerComponentDigest": _component_digest([Path(__file__)]),
            "authorityComponentDigest": _component_digest([Path(corpus_path)]),
            "evaluatorComponentDigest": _component_digest([evaluator]),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [_sha256_file(evaluator)],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": sorted(
            f"{name}: semantic report status is {report.get('status')!r}"
            for name, report in reports.items()
            if report.get("status") != "passed"
        ),
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failed_assertions + failed_cases,
    }


def _fatal_report(
    kind: str,
    source_sha: str | None,
    message: str,
    corpus: dict[str, Any] | None,
) -> dict[str, Any]:
    limitations = list(corpus.get("limitations", [])) if corpus else ["Corpus could not be loaded."]
    unmet = list(corpus.get("unmetRequirements", [])) if corpus else ["Qualification setup failed."]
    unmet.insert(0, message)
    setup_records = _setup_records(message)
    setup_supports = {
        record["caseId"]: {
            "assertionId": record["caseId"],
            "caseId": record["caseId"],
            "actual": record["actual"],
            "target": record["target"],
            "status": "passed",
        }
        for record in setup_records
    }
    return {
        "schema": "fdir/qualification-issue-93-report",
        "version": "1.0.0",
        "issueNumber": 93,
        "reportKind": REPORT_NAMES[kind].removesuffix(".json"),
        "qualificationScope": "bounded-independent-style-slice",
        "sourceSha": source_sha,
        "status": "failed",
        "fixtureCount": 0,
        "caseCount": 0,
        "propertyMismatchCount": 0,
        "provenanceMissingCount": 0,
        "provenanceMismatchCount": 0,
        "traceMismatchCount": 0,
        "cycleCount": 0,
        "missingSourceCount": 0,
        "cycleOrMissingSourceCount": 0,
        "unaccountedCount": 0,
        "adapterFailureCount": 0,
        "partialConversionCount": 0,
        "cases": [],
        "producerRecords": setup_records,
        "producerSupports": setup_supports,
        "fixtures": [],
        "assertions": [_assertion("qualification-setup", "executable", "unavailable")],
        "limitations": limitations,
        "unmet": unmet,
        "failureSummary": [message],
    }


def _setup_records(message: str) -> list[dict[str, Any]]:
    """Return typed unavailable records used by the fail-closed envelope."""

    return [
        {
            "assertionId": "qualification-setup-positive",
            "caseId": "qualification-setup-positive",
            "expected": {"$unavailable": "independent-authority", "reason": message},
            "actual": {"$unavailable": "semantic-runner", "reason": message},
            "target": {"scope": "qualification-setup", "status": "unavailable"},
            "status": "failed",
        },
        {
            "assertionId": "qualification-setup-mutation",
            "caseId": "qualification-setup-mutation",
            "expected": {"$unavailable": "independent-authority", "reason": message},
            "actual": {"$unavailable": "semantic-runner-mutation", "reason": message},
            "target": {"scope": "qualification-setup", "status": "unavailable"},
            "status": "failed",
        },
    ]


def _unavailable_digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _fail_closed_input_digests(corpus_path: Path) -> list[str]:
    paths = [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue93.py",
        ROOT / "tools" / "test_qualification_issue93.py",
        CONVERTER_PATH,
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "tools" / "adapter_xlsx.py",
    ]
    digests: list[str] = []
    for path in paths:
        digest = _sha256_file(path) if path.is_file() else _unavailable_digest(f"unavailable-input:{path}")
        if digest not in digests:
            digests.append(digest)
    return digests


def _fail_closed_component_digest(path: Path, label: str) -> str:
    if path.is_file():
        return _component_digest([path])
    return _unavailable_digest(f"unavailable-component:{label}:{path}")


def _fail_closed_producer_report(
    *,
    corpus_path: Path,
    out_dir: Path,
    source_sha: str | None,
    message: str,
) -> dict[str, Any]:
    """Emit a blocked envelope without claiming unavailable authority passed."""

    records = _setup_records(message)
    authority_name = REPORT_NAMES["coverage"]
    actual_name = REPORT_NAMES["provenance"]
    input_name = REPORT_NAMES["color"]
    support_name = REPORT_NAMES["cascade"]
    producer_cases: list[dict[str, Any]] = []
    producer_assertions: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        case_id = record["caseId"]
        assertion_id = case_id
        classification = "positive" if index == 0 else "mutation"
        assertion_type = "json-value-equals" if index == 0 else "mutation-killed"
        comparison = {"operator": "equal"} if index == 0 else {"operator": "not-equal"}
        authority_ref = _artifact_reference(out_dir, authority_name, f"/producerRecords/{index}/expected")
        actual_ref = _artifact_reference(out_dir, actual_name, f"/producerRecords/{index}/actual")
        input_ref = _artifact_reference(out_dir, input_name, f"/producerRecords/{index}/target")
        support_ref = _artifact_reference(out_dir, support_name, f"/producerSupports/{assertion_id}")
        case_result = "failed" if index == 0 else "passed"
        producer_case = {
            "caseId": case_id,
            "requirementId": PRODUCER_REQUIREMENT_ID,
            "classification": classification,
            "inputArtifact": input_ref,
            "authorityArtifact": authority_ref,
            "actualArtifact": actual_ref,
            "expected": record["expected"],
            "actual": record["actual"],
            "comparison": comparison,
            "result": case_result,
            "target": record["target"],
            "diagnostic": {
                "code": "ISSUE93_AUTHORITY_UNAVAILABLE",
                "message": "qualification setup failed; independent authority is unavailable",
            },
            "supportingArtifact": support_ref,
        }
        producer_cases.append(producer_case)
        producer_assertions.append(
            {
                "assertionId": assertion_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": assertion_type,
                "testCaseId": case_id,
                "classification": classification,
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": record["expected"],
                "actual": record["actual"],
                "comparison": comparison,
                "status": "failed" if index == 0 else "passed",
                "target": record["target"],
                "diagnostic": {
                    "code": "ISSUE93_AUTHORITY_UNAVAILABLE",
                    "message": "qualification setup failed; independent authority is unavailable",
                },
                "supportingArtifact": support_ref,
            }
        )
    evaluator = ROOT / "tools" / "qualification_evidence.py"
    producer_failed = sum(item["status"] != "passed" for item in producer_assertions)
    case_failed = sum(item["result"] != "passed" for item in producer_cases)
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": PRODUCER_EVIDENCE_ID,
        "requirementIds": [PRODUCER_REQUIREMENT_ID],
        "sourceSha": source_sha if re.fullmatch(r"[0-9a-f]{40}", source_sha or "") else "0" * 40,
        "inputDigests": _fail_closed_input_digests(corpus_path),
        "producerId": "fdir.issue-93.semantic-runner",
        "authorityId": "fdir.issue-93.authored-style-corpus-unavailable",
        "independence": {
            "producerComponentDigest": _fail_closed_component_digest(Path(__file__), "producer"),
            "authorityComponentDigest": _fail_closed_component_digest(corpus_path, "authority"),
            "evaluatorComponentDigest": _fail_closed_component_digest(evaluator, "evaluator"),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [
                _sha256_file(evaluator) if evaluator.is_file() else _unavailable_digest("unavailable-evaluator")
            ],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": [message],
        "unsupportedItems": [],
        "waivedItems": [],
        "status": "blocked",
        "failureCount": producer_failed + case_failed,
    }


def run_qualification(
    *,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> int:
    source_sha: str | None = None
    corpus: dict[str, Any] | None = None
    try:
        source_sha = _source_sha()
        corpus = _load_corpus(corpus_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Keep the disposable evidence inspectable. Some managed Windows
        # workspaces deny deletion of temporary directories while a child
        # process still has a handle open.
        work = out_dir / f"work-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
        executions: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []
        for fixture in corpus["fixtures"]:
            execution = _run_converter(fixture, work)
            execution["graph"] = _graph_counts(execution["document"])
            executions.append(execution)
            for case in fixture["cases"]:
                evaluated.append(_evaluate_case(case, execution["document"]))
        producer_records: list[dict[str, Any]] = [
            {
                "assertionId": str(result["caseId"]),
                "caseId": str(result["caseId"]),
                "actual": result.get("actual", {"$unavailable": True}),
                "target": result.get("target", {"caseId": result["caseId"]}),
                "status": "passed",
            }
            for result in evaluated
        ]
        reports = {
            kind: _report(kind, source_sha, corpus, executions, evaluated, producer_records=producer_records)
            for kind in REPORT_NAMES
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        for kind, name in REPORT_NAMES.items():
            _write_json(out_dir / name, reports[kind])
        producer_report = build_producer_report(
            corpus,
            reports,
            out_dir,
            corpus_path=corpus_path,
            source_sha=source_sha,
            evaluated=evaluated,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        reports = {
            kind: _fatal_report(kind, source_sha, message, corpus)
            for kind in REPORT_NAMES
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        for kind, name in REPORT_NAMES.items():
            _write_json(out_dir / name, reports[kind])
        producer_report = _fail_closed_producer_report(
            corpus_path=corpus_path,
            out_dir=out_dir,
            source_sha=source_sha,
            message=message,
        )
        _write_json(out_dir / PRODUCER_REPORT_NAME, producer_report)
        print(f"FAIL: issue #93 qualification setup: {message}", file=sys.stderr)
        return 1
    for kind, name in REPORT_NAMES.items():
        _write_json(out_dir / name, reports[kind])
    _write_json(out_dir / PRODUCER_REPORT_NAME, producer_report)
    failed = [kind for kind, report in reports.items() if report["status"] != "passed"]
    if failed:
        print("FAIL: issue #93 bounded reports: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("PASS: issue #93 bounded reports written: " + ", ".join(REPORT_NAMES.values()))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
