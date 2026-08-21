"""Focused single-defect tests for the #91 occurrence accounting lane.

The construct-level policy is now bound in the checked-in capability profile.
The tests still require the CLI to fail closed when no accounting payload is
provided, and independently verify that structural accounting defects cannot
be hidden by a passing profile lookup.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from occurrence_qualification import (  # noqa: E402
    REQUIRED_OCCURRENCE_FIELDS,
    enumerate_source,
    profile_for_format,
    validate_accounting,
)


CORPUS = ROOT / "e2e" / "corpus"
CASES = {
    "docx": CORPUS / "docx-independent",
    "xlsx": CORPUS / "xlsx-independent",
    "pdf": CORPUS / "pdf-independent.pdf",
    "markdown": CORPUS / "markdown-independent.md",
}


def _codes(result: dict[str, Any]) -> set[str]:
    return {str(item.get("code")) for item in result.get("failures", []) if isinstance(item, dict)}


def _enumerate_all() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for format_name, path in CASES.items():
        result = enumerate_source(path, format_name, evidence_case_id=f"{format_name}-focused")
        occurrences = result["sourceOccurrences"]
        if not occurrences:
            raise AssertionError(f"{format_name} source occurrence list is empty")
        ids = [item["sourceOccurrenceId"] for item in occurrences]
        if len(ids) != len(set(ids)):
            raise AssertionError(f"{format_name} occurrence IDs are not unique")
        for item in occurrences:
            missing = [field for field in REQUIRED_OCCURRENCE_FIELDS if field not in item]
            if missing:
                raise AssertionError(f"{format_name} occurrence is missing fields: {missing}")
            if item["sourceDigest"] != result["sourceSha"]:
                raise AssertionError(f"{format_name} source digest is not bound to the enumeration")
        if result["enumerator"]["adapterModulesImported"] != []:
            raise AssertionError(f"{format_name} enumerator declares adapter coupling")
        results[format_name] = result
    return results


def _enumerate_checked_in_manifest() -> int:
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    cases = list(manifest.get("cases", [])) + list(manifest.get("negativeCases", []))
    if len(cases) < 10:
        raise AssertionError("checked-in corpus manifest unexpectedly has too few cases")
    for case in cases:
        format_name = case["format"]
        path = CORPUS / case["path"]
        result = enumerate_source(path, format_name, evidence_case_id=case["id"])
        if result["sourceOccurrenceCount"] < 1 or len(result["sourceOccurrences"]) != result["sourceOccurrenceCount"]:
            raise AssertionError(f"{case['id']} did not produce a bounded source occurrence record")
    return len(cases)


def _account_everything(enumeration: dict[str, Any]) -> list[dict[str, Any]]:
    profile = profile_for_format(enumeration["format"])
    rules = {item["constructId"]: item for item in profile["occurrenceAccounting"]["rules"]}
    accounting: list[dict[str, Any]] = []
    for source in enumeration["sourceOccurrences"]:
        rule = rules.get(source["constructId"])
        if rule is None:
            # This is deliberately not a successful fallback.  The caller
            # should see UNKNOWN_CONSTRUCT for an observed construct absent
            # from the profile.
            disposition = "failed"
            diagnostic_ids = [f"diagnostic:{source['sourceOccurrenceId']}"]
            target_ids: list[str] = []
        else:
            allowed = list(rule["allowedDispositions"])
            disposition = "preserved" if "preserved" in allowed else "normalized" if "normalized" in allowed else allowed[0]
            target_ids = [f"target:{source['sourceOccurrenceId']}"] if disposition in {"preserved", "normalized"} and rule.get("targetRequired") else []
            diagnostic_ids = []
            if disposition != "omitted-by-policy" and disposition not in {"preserved", "normalized"} and rule.get("diagnosticRequired"):
                diagnostic_ids = [f"diagnostic:{source['sourceOccurrenceId']}"]
        entry = {field: source[field] for field in REQUIRED_OCCURRENCE_FIELDS}
        entry.update({"disposition": disposition, "targetIds": target_ids, "diagnosticIds": diagnostic_ids})
        accounting.append(entry)
    return accounting


def _assert_defect(enumeration: dict[str, Any], accounting: list[dict[str, Any]], expected_code: str) -> None:
    result = validate_accounting(enumeration, accounting)
    if expected_code not in _codes(result):
        raise AssertionError(f"expected {expected_code}, got {sorted(_codes(result))}")


def _run_structural_negative_cases(enumeration: dict[str, Any]) -> int:
    baseline = _account_everything(enumeration)
    profile = profile_for_format(enumeration["format"])
    baseline_result = validate_accounting(enumeration, baseline)
    baseline_codes = _codes(baseline_result)
    if "CAPABILITY_PROFILE_NOT_CONSTRUCT_CLOSED" in baseline_codes:
        raise AssertionError("the checked-in capability profile is not construct-closed")
    forbidden = {"UNACCOUNTED_OCCURRENCE", "DUPLICATE_OCCURRENCE_ACCOUNTING", "UNKNOWN_OCCURRENCE_ID", "UNKNOWN_CONSTRUCT"}
    if baseline_codes.intersection(forbidden):
        raise AssertionError(f"baseline accounting has an unexpected structural defect: {sorted(baseline_codes)}")

    cases = 0
    _assert_defect(enumeration, baseline[1:], "UNACCOUNTED_OCCURRENCE")
    cases += 1

    duplicated = deepcopy(baseline)
    duplicated.append(deepcopy(duplicated[0]))
    _assert_defect(enumeration, duplicated, "DUPLICATE_OCCURRENCE_ACCOUNTING")
    cases += 1

    unknown_id = deepcopy(baseline)
    unknown_id[0]["sourceOccurrenceId"] = "source-occurrence:not-in-source"
    _assert_defect(enumeration, unknown_id, "UNKNOWN_OCCURRENCE_ID")
    cases += 1

    locator_swapped = deepcopy(baseline)
    second_locator_index = next(
        (index for index in range(1, len(locator_swapped)) if locator_swapped[index]["sourceLocator"] != locator_swapped[0]["sourceLocator"]),
        None,
    )
    if second_locator_index is None:
        raise AssertionError("fixture has no pair of distinct source locators")
    locator_swapped[0]["sourceLocator"], locator_swapped[second_locator_index]["sourceLocator"] = locator_swapped[second_locator_index]["sourceLocator"], locator_swapped[0]["sourceLocator"]
    _assert_defect(enumeration, locator_swapped, "ACCOUNTING_SOURCE_IDENTITY_MISMATCH")
    cases += 1

    handler_disabled = deepcopy(baseline)
    rules_by_construct = {
        item["constructId"]: item
        for item in profile["occurrenceAccounting"]["rules"]
        if isinstance(item, dict) and isinstance(item.get("constructId"), str)
    }
    target_required_index = next(
        (
            index
            for index, source in enumerate(enumeration["sourceOccurrences"])
            if rules_by_construct.get(source.get("constructId"), {}).get("targetRequired") is True
            and baseline[index].get("targetIds")
        ),
        None,
    )
    if target_required_index is None:
        raise AssertionError("fixture has no target-required occurrence")
    handler_disabled[target_required_index]["targetIds"] = []
    _assert_defect(enumeration, handler_disabled, "TARGET_BINDING_REQUIRED")
    cases += 1

    unknown_construct = deepcopy(enumeration)
    injected = deepcopy(unknown_construct["sourceOccurrences"][0])
    injected["sourceOccurrenceId"] = "source-occurrence:injected-unknown-construct"
    injected["constructId"] = "format.unknown-construct"
    injected["policyRuleId"] = "unknown-construct"
    unknown_construct["sourceOccurrences"].append(injected)
    unknown_construct["sourceOccurrenceCount"] += 1
    unknown_accounting = deepcopy(baseline)
    unknown_entry = {field: injected[field] for field in REQUIRED_OCCURRENCE_FIELDS}
    unknown_entry.update({"disposition": "failed", "targetIds": [], "diagnosticIds": ["diagnostic:unknown-construct"]})
    unknown_accounting.append(unknown_entry)
    _assert_defect(unknown_construct, unknown_accounting, "UNKNOWN_CONSTRUCT")
    cases += 1

    empty = validate_accounting(enumeration, [])
    if empty.get("status") != "failed" or len(empty.get("unaccountedOccurrenceIds", [])) != enumeration["sourceOccurrenceCount"]:
        raise AssertionError("missing accounting payload did not fail closed for every occurrence")
    cases += 1
    return cases


def _run_stability_case(workspace: Path, markdown: dict[str, Any]) -> None:
    original = CASES["markdown"].read_text(encoding="utf-8")
    changed_path = workspace / "markdown-with-unrelated-insertion.md"
    changed_path.write_text(original + "\nindependent unrelated source sentence\n", encoding="utf-8", newline="\n")
    changed = enumerate_source(changed_path, "markdown", evidence_case_id="markdown-stability")
    original_ids = {item["sourceOccurrenceId"] for item in markdown["sourceOccurrences"]}
    changed_ids = {item["sourceOccurrenceId"] for item in changed["sourceOccurrences"]}
    if not original_ids.issubset(changed_ids):
        raise AssertionError("unrelated source insertion changed existing occurrence IDs")


def _run_cli_fail_closed(workspace: Path) -> None:
    output = workspace / "reports"
    command = [
        sys.executable,
        str(ROOT / "tools" / "occurrence_qualification.py"),
        "qualify",
        str(CASES["markdown"]),
        "--format",
        "markdown",
        "--out-dir",
        str(output),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode == 0:
        raise AssertionError("qualification without an accounting payload unexpectedly passed")
    reports = [json.loads((output / name).read_text(encoding="utf-8")) for name in ("source-occurrence-accounting.json", "capability-profile-coverage.json", "status-aggregation.json", "unaccounted-occurrences.json")]
    for report in reports:
        if report.get("status") != "failed":
            raise AssertionError(f"fail-closed report was not failed: {report.get('reportKind')}")
        if report.get("sourceSha") != markdown_source_sha:
            raise AssertionError("report source digest is not commit/input bound")


def main() -> int:
    global markdown_source_sha
    workspace = ROOT / "e2e" / ".run" / f"occurrence-focused-{os.getpid()}"
    workspace.mkdir(parents=True, exist_ok=True)
    enumerations = _enumerate_all()
    manifest_cases = _enumerate_checked_in_manifest()
    markdown_source_sha = enumerations["markdown"]["sourceSha"]
    negative_cases = sum(_run_structural_negative_cases(enumeration) for enumeration in enumerations.values())
    _run_stability_case(workspace, enumerations["markdown"])
    _run_cli_fail_closed(workspace)
    result = {
        "schema": "fdir/occurrence-qualification-focused-test",
        "status": "passed",
        "formats": sorted(enumerations),
        "sourceOccurrences": {name: value["sourceOccurrenceCount"] for name, value in sorted(enumerations.items())},
        "singleDefectNegativeCases": negative_cases,
        "checkedInManifestCases": manifest_cases,
        "sharedCapabilityProfileBound": True,
        "qualificationStillFailClosed": True,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
