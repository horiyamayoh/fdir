"""Aggregate integration runner for GitHub issue #91.

This runner supplies the integration boundary: all checked-in independent
corpus cases are enumerated by ``occurrence_qualification.py`` and converted
through the public adapter boundary.  The four issue-91 reports bind each
independent occurrence to actual emitted IR IDs and diagnostics.  Exploded OPC
trees are zipped only as an adapter transport artifact; source digests and
occurrence identity remain based on the hand-authored source tree.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Sequence
import zipfile

try:
    from convert_document import convert_path
    from qualification_evidence import (
        _attach_mappings,
        feature_dispositions,
        selected_artifact_digest,
        selected_artifact_value,
        source_feature_inventory,
    )
except ImportError:  # pragma: no cover - package-style imports
    from tools.convert_document import convert_path
    from tools.qualification_evidence import (
        _attach_mappings,
        feature_dispositions,
        selected_artifact_digest,
        selected_artifact_value,
        source_feature_inventory,
    )


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from occurrence_qualification import (  # noqa: E402
    COMPLETE_DISPOSITIONS,
    OccurrenceQualificationError,
    REPORT_NAMES as OCCURRENCE_REPORT_NAMES,
    REQUIRED_OCCURRENCE_FIELDS,
    enumerate_source,
    profile_for_format,
    validate_accounting,
)


REPORT_NAMES = tuple(OCCURRENCE_REPORT_NAMES)
FORMAT_NAMES = ("docx", "xlsx", "pdf", "markdown")
MANIFEST_PATH = ROOT / "e2e" / "corpus" / "manifest.json"
REPORT_SCHEMA_PATH = ROOT / "schemas" / "qualification-issue-91-report.schema.json"
REPORT_SCHEMA = "fdir/qualification-issue-91-aggregate-report"
REPORT_VERSION = "1.0.0"
PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
PRODUCER_EVIDENCE_ID = "issue-91-occurrence-accounting"
PRODUCER_REQUIREMENT_ID = "QUAL-91-OCCURRENCE-ACCOUNTING"
PRODUCER_BUNDLE_PREFIX = "artifacts/91"
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class Issue91QualificationError(ValueError):
    """Raised when the checked-in corpus cannot be safely integrated."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Issue91QualificationError(f"value is not canonical JSON: {exc}") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise Issue91QualificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _repository_relative(path: Path) -> str:
    try:
        relative = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise Issue91QualificationError(f"artifact is outside the repository: {path}") from exc
    if not relative or relative == "." or relative.startswith("../"):
        raise Issue91QualificationError(f"artifact path is not repository-relative: {path}")
    return relative


def _artifact_reference(
    out_dir: Path,
    report_name: str,
    pointer: str,
    *,
    bundle_name: str | None = None,
) -> dict[str, Any]:
    """Bind a producer reference to a real emitted semantic report.

    The stored path is the path used after bundle packaging.  Digests are
    calculated from the runner output before packaging; the builder copies
    those exact bytes to the stored path.
    """

    source = Path(out_dir) / report_name
    if not source.is_file():
        raise Issue91QualificationError(f"semantic report is unavailable: {source}")
    selector = {"kind": "json-pointer", "pointer": pointer}
    try:
        selected = selected_artifact_value(source, selector)
        selected_digest = selected_artifact_digest(selected, selector)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise Issue91QualificationError(
            f"semantic report selector is unavailable: {source}#{pointer}: {exc}"
        ) from exc
    return {
        "path": f"{PRODUCER_BUNDLE_PREFIX}/{bundle_name or report_name}",
        "sha256": _sha256_file(source),
        "selector": selector,
        "selectedSha256": selected_digest,
    }


def _input_digests(manifest_path: Path) -> list[str]:
    """Hash every declared issue-91 input, including evaluator components."""

    paths = [
        ROOT / "machine" / "capability-profile.json",
        ROOT / "tools" / "occurrence_qualification.py",
        ROOT / "tools" / "qualification_issue91.py",
        ROOT / "tools" / "test_qualification_issue91.py",
        REPORT_SCHEMA_PATH,
        Path(manifest_path),
        ROOT / "tools" / "qualification_evidence.py",
        ROOT / "tools" / "validate_qualification_contract.py",
    ]
    digests: list[str] = []
    for path in paths:
        if not path.is_file():
            raise Issue91QualificationError(f"declared qualification input is unavailable: {path}")
        digest = _sha256_file(path)
        if digest not in digests:
            digests.append(digest)
    return digests


def _component_digest(paths: Iterable[Path]) -> str:
    material = []
    for path in paths:
        if not Path(path).is_file():
            raise Issue91QualificationError(f"independence component is unavailable: {path}")
        material.append({"path": _repository_relative(Path(path)), "sha256": _sha256_file(Path(path))})
    return _sha256(_canonical(material))


def git_head_sha(repo_root: Path = ROOT) -> str:
    """Return the exact commit identity used by the evidence contract."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=Path(repo_root),
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise Issue91QualificationError(f"cannot resolve Git HEAD: {exc}") from exc
    source_sha = result.stdout.strip().lower()
    if not GIT_SHA_PATTERN.fullmatch(source_sha):
        raise Issue91QualificationError(f"Git HEAD is not a 40-character lowercase SHA: {source_sha!r}")
    return source_sha


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Issue91QualificationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Issue91QualificationError(f"JSON root must be an object: {path}")
    return value


def _safe_source_path(corpus_root: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise Issue91QualificationError(f"corpus source path must be relative: {raw_path}")
    candidate = (corpus_root / relative).resolve()
    try:
        candidate.relative_to(corpus_root.resolve())
    except ValueError as exc:
        raise Issue91QualificationError(f"corpus source escapes corpus root: {raw_path}") from exc
    if not candidate.exists():
        raise Issue91QualificationError(f"corpus source does not exist: {raw_path}")
    if not (candidate.is_file() or candidate.is_dir()):
        raise Issue91QualificationError(f"corpus source is not a file or directory: {raw_path}")
    return candidate


def load_corpus_cases(manifest_path: Path = MANIFEST_PATH) -> list[dict[str, Any]]:
    """Load and validate every positive and negative checked-in corpus case."""

    manifest_path = Path(manifest_path).resolve()
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != "fdir/independent-fidelity-corpus":
        raise Issue91QualificationError("independent corpus manifest schema is invalid")
    if manifest.get("independent") is not True:
        raise Issue91QualificationError("corpus manifest is not marked independent")

    raw_cases: list[Any] = []
    for key in ("cases", "negativeCases"):
        values = manifest.get(key)
        if not isinstance(values, list):
            raise Issue91QualificationError(f"corpus manifest field {key!r} must be a list")
        raw_cases.extend(values)

    corpus_root = manifest_path.parent
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise Issue91QualificationError("corpus manifest contains a non-object case")
        case_id = raw.get("id")
        format_name = raw.get("format")
        raw_path = raw.get("path")
        if not isinstance(case_id, str) or not case_id:
            raise Issue91QualificationError("corpus case has no non-empty id")
        if case_id in seen_ids:
            raise Issue91QualificationError(f"duplicate corpus case id: {case_id}")
        if format_name not in FORMAT_NAMES:
            raise Issue91QualificationError(f"unsupported corpus format: {format_name!r}")
        if not isinstance(raw_path, str) or not raw_path:
            raise Issue91QualificationError(f"corpus case {case_id} has no path")
        source_path = _safe_source_path(corpus_root, raw_path)
        seen_ids.add(case_id)
        cases.append(
            {
                "id": case_id,
                "format": format_name,
                "path": source_path,
                "relativePath": source_path.relative_to(ROOT.resolve()).as_posix(),
                "caseClass": raw.get("caseClass"),
                "expectedStatus": raw.get("expectedStatus"),
            }
        )

    formats = {case["format"] for case in cases}
    missing_formats = sorted(set(FORMAT_NAMES) - formats)
    if missing_formats:
        raise Issue91QualificationError(f"corpus is missing required formats: {missing_formats}")
    if not cases:
        raise Issue91QualificationError("corpus manifest has no cases")
    return cases


def _case_occurrence_key(case_id: str, source_occurrence_id: str) -> str:
    """Namespace a lane-local occurrence ID without changing its identity."""

    return f"{case_id}::{source_occurrence_id}"


def _safe_artifact_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "case"


def _zip_package_tree(source: Path, destination: Path) -> Path:
    """Materialize an exploded OPC corpus case for the public adapter boundary.

    The independent enumerator continues to read ``source`` directly.  The ZIP
    is only an adapter transport artifact and is written with deterministic
    member ordering; it is never used as the source digest or source identity.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for child in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.relative_to(source).as_posix()):
            name = child.relative_to(source).as_posix()
            archive.writestr(name, child.read_bytes())
    return destination


def _adapter_input(case: dict[str, Any], artifact_root: Path) -> Path:
    source = Path(case["path"])
    if not source.is_dir():
        return source
    return _zip_package_tree(source, artifact_root / f"{_safe_artifact_name(case['id'])}.zip")


def _bare_ids(references: Iterable[Any]) -> list[str]:
    """Convert qualified ``collection/id`` references to actual IR IDs."""

    result: list[str] = []
    for value in references:
        if not isinstance(value, str) or not value:
            continue
        identifier = value.split("/", 1)[1] if "/" in value else value
        if identifier and identifier not in result:
            result.append(identifier)
    return sorted(result)


def _diagnostic_ids_for_targets(document: dict[str, Any], target_ids: Iterable[str]) -> list[str]:
    targets = set(value for value in target_ids if isinstance(value, str))
    result: list[str] = []
    for item in document.get("diagnostics", []):
        if not isinstance(item, dict) or not isinstance(item.get("diagnosticId"), str):
            continue
        if item.get("targetId") in targets or item.get("sourceMapId") in targets:
            result.append(item["diagnosticId"])
    for feature in document.get("conversion", {}).get("features", []):
        if not isinstance(feature, dict) or feature.get("targetId") not in targets:
            continue
        result.extend(value for value in feature.get("diagnosticIds", []) if isinstance(value, str))
    return sorted(set(result))


def _source_record_matches(occurrence: dict[str, Any], record: dict[str, Any]) -> bool:
    """Match an independent occurrence to a coarser emitted source record.

    The independent enumerator owns occurrence identity.  This matcher is only
    a binding bridge: package XML descendants bind to their package part,
    PDF tokens bind to their object/operator record, and Markdown tokens bind
    to their source line.  It never creates an occurrence or changes its
    digest/locator fields.
    """

    construct = str(occurrence.get("constructId", ""))
    locator = occurrence.get("sourceLocator", {})
    if not isinstance(locator, dict):
        return False
    kind = str(record.get("sourceKind", ""))
    record_locator = record.get("sourceLocator", {})
    if not isinstance(record_locator, dict):
        return False
    signature = occurrence.get("sourceSignature", {})
    if not isinstance(signature, dict):
        signature = {}
    record_signature = record.get("sourceSignature", {})
    if not isinstance(record_signature, dict):
        record_signature = {}

    if construct.startswith("opc."):
        part = locator.get("part")
        if not isinstance(part, str):
            return kind == "package-container"
        if kind == "package-part" and record_locator.get("path") == part:
            return True
        if kind == "package-relationship-part" and record_locator.get("path") == part:
            return True
        if kind == "package-relationship":
            return (
                record_locator.get("path") == part
                and (
                    not locator.get("relationshipId")
                    or record_locator.get("relationshipId") == locator.get("relationshipId")
                )
            )
        return False

    if construct.startswith("pdf."):
        obj = locator.get("object")
        if construct == "pdf.indirect-object":
            return kind == "pdf-object" and record_locator.get("object") == obj
        if construct == "pdf.indirect-reference":
            return kind == "pdf-reference" and record_locator.get("fromObject") == locator.get("fromObject") and record_locator.get("toObject") == locator.get("toObject")
        if construct == "pdf.page":
            return kind == "pdf-object" and record_locator.get("object") == obj
        if construct == "pdf.font-cmap":
            return kind in {"pdf-object", "pdf-font-cmap"} and record_locator.get("object", record_locator.get("fontObject")) == obj
        if construct in {"pdf.resource-entry", "pdf.annotation-action", "pdf.encryption-filter", "pdf.invalid-sequence"}:
            return kind == "pdf-object" and record_locator.get("object") == obj
        if construct in {"pdf.content-operator", "pdf.inline-image", "pdf.unknown-operator"}:
            return kind == "pdf-operator" and record_locator.get("object") == obj and record_signature.get("operator") == occurrence.get("operator")
        if construct == "pdf.revision":
            return False
        if construct == "pdf.xref-entry":
            return False
        return False

    if construct.startswith("markdown."):
        line = locator.get("lineStart")
        return kind == "markdown-line-token" and isinstance(line, int) and record_locator.get("lineStart") == line
    return False


def _fallback_target_ids(document: dict[str, Any], occurrence: dict[str, Any]) -> list[str]:
    """Resolve a conservative real IR target when a source record is absent."""

    construct = str(occurrence.get("constructId", ""))
    locator = occurrence.get("sourceLocator", {})
    if not isinstance(locator, dict):
        return []
    result: list[str] = []
    if construct.startswith("opc."):
        part_name = locator.get("part")
        for item in document.get("parts", []):
            if isinstance(item, dict) and item.get("name") == part_name and isinstance(item.get("partId"), str):
                result.append(item["partId"])
    elif construct.startswith("pdf."):
        object_name = locator.get("object")
        if isinstance(object_name, str):
            for item in document.get("parts", []):
                if isinstance(item, dict) and item.get("name") == f"{object_name} obj" and isinstance(item.get("partId"), str):
                    result.append(item["partId"])
        if construct == "pdf.page":
            page = locator.get("page")
            for item in document.get("sourceMaps", []):
                if isinstance(item, dict) and item.get("targetId") and isinstance(item.get("locator"), dict) and item["locator"].get("page") == page:
                    result.append(str(item["targetId"]))
    elif construct.startswith("markdown."):
        line = locator.get("lineStart")
        for item in document.get("sourceMaps", []):
            if isinstance(item, dict) and isinstance(item.get("targetId"), str) and isinstance(item.get("locator"), dict):
                item_locator = item["locator"]
                if item_locator.get("lineStart") == line:
                    result.append(item["targetId"])
        if not result and isinstance(document.get("rootNodeId"), str):
            result.append(document["rootNodeId"])
    return sorted(set(result))


def _accounting_for_case(
    enumeration: dict[str, Any],
    document: dict[str, Any],
    source_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build accounting entries from independent occurrences and actual IR."""

    profile = profile_for_format(enumeration["format"])
    rules = {
        item["constructId"]: item
        for item in profile["occurrenceAccounting"]["rules"]
        if isinstance(item, dict) and isinstance(item.get("constructId"), str)
    }
    entries: list[dict[str, Any]] = []
    for occurrence in enumeration["sourceOccurrences"]:
        construct = str(occurrence.get("constructId"))
        rule = rules.get(construct)
        candidates = [record for record in source_records if _source_record_matches(occurrence, record)]
        qualified_refs = [reference for record in candidates for reference in record.get("mapping", {}).get("emittedEntityIds", [])]
        target_ids = _bare_ids(qualified_refs)
        if not target_ids:
            target_ids = _fallback_target_ids(document, occurrence)
        diagnostic_ids = sorted({
            value
            for record in candidates
            for value in record.get("mapping", {}).get("diagnosticIds", [])
            if isinstance(value, str)
        })
        diagnostic_ids = sorted(set(diagnostic_ids).union(_diagnostic_ids_for_targets(document, target_ids)))
        source_dispositions = [
            str(record.get("semanticDisposition", record.get("sourceDisposition", "non-preserved")))
            for record in candidates
        ]
        if rule is None:
            disposition = "unsupported"
        elif "omitted-by-policy" in source_dispositions:
            if "normalized" in rule.get("allowedDispositions", []) and not target_ids:
                disposition = "normalized"
            elif "omitted-by-policy" in rule.get("allowedDispositions", []):
                disposition = "omitted-by-policy"
            else:
                disposition = "failed"
        elif any(value in {"non-preserved", "approximated", "unsupported", "failed"} for value in source_dispositions) and construct not in {"markdown.block", "markdown.inline-token", "markdown.delimiter", "markdown.escape", "markdown.source-span"}:
            if construct == "pdf.font-cmap" and "unavailable-observation" in rule.get("allowedDispositions", []):
                disposition = "unavailable-observation"
            else:
                disposition = next(
                    (value for value in ("approximated", "unsupported", "ambiguous", "unavailable-observation", "failed") if value in rule.get("allowedDispositions", [])),
                    "failed",
                )
        elif target_ids and not any(value in {"unsupported", "failed"} for value in source_dispositions) and any(value in {"normalized", "preserved"} for value in rule.get("allowedDispositions", [])):
            disposition = next((value for value in ("normalized", "preserved") if value in rule.get("allowedDispositions", [])), "failed")
        elif any(value == "observation" for value in source_dispositions):
            disposition = next((value for value in ("unavailable-observation", "normalized", "preserved") if value in rule.get("allowedDispositions", [])), "failed")
        else:
            disposition = next((value for value in ("normalized", "preserved") if value in rule.get("allowedDispositions", [])), "failed")
        if rule is not None and rule.get("targetRequired") is True and disposition in COMPLETE_DISPOSITIONS and not target_ids:
            disposition = next((value for value in ("unsupported", "approximated", "ambiguous", "failed") if value in rule.get("allowedDispositions", [])), disposition)
        if rule is not None and rule.get("diagnosticRequired") is True and disposition not in COMPLETE_DISPOSITIONS and disposition != "omitted-by-policy" and not diagnostic_ids:
            diagnostic_ids = _diagnostic_ids_for_targets(document, [document.get("rootNodeId")])
        if not diagnostic_ids and construct == "pdf.invalid-sequence":
            diagnostic_ids = sorted(
                item["diagnosticId"]
                for item in document.get("diagnostics", [])
                if isinstance(item, dict)
                and isinstance(item.get("diagnosticId"), str)
                and any(token in str(item.get("code", "")).upper() for token in ("STREAM", "LENGTH", "ENDSTREAM"))
            )
        entry = {field: occurrence[field] for field in REQUIRED_OCCURRENCE_FIELDS}
        entry.update({"disposition": disposition, "targetIds": target_ids, "diagnosticIds": diagnostic_ids})
        entries.append(entry)
    return entries


def _profile_coverage(format_name: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    profile = profile_for_format(format_name)
    policy = profile.get("occurrenceAccounting")
    if not isinstance(policy, dict):
        policy = {}
    rules = policy.get("rules")
    if not isinstance(rules, list):
        rules = []
    rule_by_construct = {
        rule.get("constructId"): rule
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("constructId"), str)
    }
    observed = Counter(str(row.get("constructId")) for row in rows)
    unknown = sorted(construct for construct in observed if construct not in rule_by_construct)
    return {
        "format": format_name,
        "profileId": profile.get("id"),
        "profileVersion": profile.get("version"),
        "profileBound": policy.get("profileBound") is True,
        "occurrenceAccountingVersion": policy.get("version"),
        "observedConstructCount": len(observed),
        "observedOccurrenceCount": len(rows),
        "unknownConstructs": unknown,
        "rules": [
            {
                "policyRuleId": rule.get("id"),
                "constructId": rule.get("constructId"),
                "support": rule.get("support"),
                "allowedDispositions": rule.get("allowedDispositions", []),
                "observedCount": observed.get(rule.get("constructId"), 0),
            }
            for rule in rules
            if isinstance(rule, dict)
        ],
    }


def _blocker(code: str, message: str, *, count: int | None = None, case_ids: Iterable[str] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message, "severity": "blocker"}
    if count is not None:
        result["count"] = count
    ids = sorted(set(case_ids))
    if ids:
        result["caseIds"] = ids
    return result


def _assertion(assertion_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if actual == expected else "failed",
    }


def _case_failure_codes(validation: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item.get("code"))
            for item in validation.get("failures", [])
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        }
    )


def _enumerate_case(case: dict[str, Any]) -> dict[str, Any]:
    enumeration = enumerate_source(
        case["path"],
        case["format"],
        evidence_case_id=case["id"],
    )
    artifact_root = Path(case.get("artifactRoot", ROOT / ".qualification-issue-91-run"))
    adapter_path = _adapter_input(case, artifact_root)
    document, adapter_evidence = convert_path(adapter_path, case["format"])
    if not isinstance(document, dict):
        raise Issue91QualificationError(f"adapter returned a non-object for case {case['id']}")
    dispositions = feature_dispositions(document)
    source_records = source_feature_inventory(adapter_path, case["format"], document)
    _attach_mappings(document, source_records, dispositions)
    accounting = _accounting_for_case(enumeration, document, source_records)
    validation = validate_accounting(enumeration, accounting, ir=document, require_ir=True)
    rows: list[dict[str, Any]] = []
    for source_row in validation.get("occurrenceRows", []):
        row = dict(source_row)
        source_id = row.get("sourceOccurrenceId")
        if not isinstance(source_id, str):
            raise Issue91QualificationError(f"case {case['id']} has an occurrence without a stable ID")
        row["caseId"] = case["id"]
        row["aggregateOccurrenceKey"] = _case_occurrence_key(case["id"], source_id)
        rows.append(row)
    if len(rows) != enumeration.get("sourceOccurrenceCount"):
        raise Issue91QualificationError(f"case {case['id']} occurrence count is inconsistent")
    aggregate = validation.get("aggregate", {})
    adapter_conversion_status = document.get("conversion", {}).get("status")
    if adapter_conversion_status == "failed":
        conversion_status = "failed"
    elif aggregate.get("status") != "complete" and adapter_conversion_status in {"complete", "complete-with-warnings"}:
        # The public adapter status is a self-report.  An independent source
        # occurrence with a non-eligible disposition is stronger evidence and
        # must prevent a complete claim in this qualification lane.
        conversion_status = "partial"
    else:
        conversion_status = adapter_conversion_status
    expected_status = case.get("expectedStatus")
    source_fact_partial = any(
        isinstance(feature, dict)
        and feature.get("feature") not in {"renderer-observation", "ocr-observation"}
        and feature.get("status") in {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed", "unavailable"}
        for feature in document.get("conversion", {}).get("features", [])
    )
    if expected_status == "complete":
        case_qualified = conversion_status == "complete" and aggregate.get("status") == "complete" and not validation.get("unknownConstructOccurrenceIds")
    elif expected_status == "complete-with-warnings":
        case_qualified = conversion_status == "complete-with-warnings" and aggregate.get("completeAllowed") is True
    elif expected_status == "partial":
        case_qualified = conversion_status == "partial" and (aggregate.get("completeAllowed") is False or not source_fact_partial)
    elif expected_status == "failed":
        case_qualified = conversion_status == "failed" and aggregate.get("completeAllowed") is False
    else:
        case_qualified = False
    return {
        "case": case,
        "enumeration": enumeration,
        "validation": validation,
        "rows": rows,
        "coverage": _profile_coverage(case["format"], rows),
        "document": document,
        "adapterEvidence": adapter_evidence,
        "sourceRecords": source_records,
        "adapterReportedConversionStatus": adapter_conversion_status,
        "expectedStatus": expected_status,
        "conversionStatus": conversion_status,
        "caseQualified": bool(case_qualified),
    }


def _aggregate_case_source_digest(case_results: Sequence[dict[str, Any]]) -> str:
    material = [
        {
            "caseId": result["case"]["id"],
            "format": result["case"]["format"],
            "relativePath": result["case"]["relativePath"],
            "sourceSha": result["enumeration"]["sourceSha"],
            "sourceOccurrenceCount": result["enumeration"]["sourceOccurrenceCount"],
        }
        for result in case_results
    ]
    return _sha256(_canonical(material))


def _build_aggregate(case_results: Sequence[dict[str, Any]], source_sha: str) -> dict[str, Any]:
    if not case_results:
        raise Issue91QualificationError("cannot build an empty issue-91 aggregate")
    if not GIT_SHA_PATTERN.fullmatch(source_sha):
        raise Issue91QualificationError("aggregate sourceSha must be the 40-character Git HEAD SHA")
    rows = [row for result in case_results for row in result["rows"]]
    case_source_digest = _aggregate_case_source_digest(case_results)
    case_ids = [result["case"]["id"] for result in case_results]
    formats = sorted({result["case"]["format"] for result in case_results})
    unaccounted = [row["aggregateOccurrenceKey"] for row in rows if row.get("accountingState") == "unaccounted"]
    duplicate_source_ids = sorted(
        _case_occurrence_key(result["case"]["id"], source_id)
        for result in case_results
        for source_id in result["validation"].get("duplicateOccurrenceIds", [])
    )
    unknown_constructs = sorted(
        {
            str(row.get("constructId"))
            for row in rows
            if str(row.get("constructId")) in {item for result in case_results for item in result["coverage"]["unknownConstructs"]}
        }
    )
    source_binding_count = sum(len(row.get("targetIds", [])) for row in rows)
    diagnostic_binding_count = sum(len(row.get("diagnosticIds", [])) for row in rows)
    lane_failures = [
        {
            "caseId": result["case"]["id"],
            "format": result["case"]["format"],
            "codes": _case_failure_codes(result["validation"]),
        }
        for result in case_results
    ]
    blockers: list[dict[str, Any]] = []
    if not all(isinstance(result.get("document"), dict) for result in case_results):
        blockers.append(_blocker("ADAPTER_IR_UNAVAILABLE", "A corpus case did not produce an adapter IR document.", count=len(case_results), case_ids=case_ids))
    if not all(isinstance(result.get("validation"), dict) for result in case_results):
        blockers.append(_blocker("ACCOUNTING_INPUT_UNAVAILABLE", "A corpus case did not produce an accounting validation result.", count=len(case_results), case_ids=case_ids))
    if unaccounted:
        blockers.append(_blocker("UNACCOUNTED_SOURCE_OCCURRENCES", "At least one independently enumerated source occurrence has no accounting entry.", count=len(unaccounted), case_ids=case_ids))
    if duplicate_source_ids:
        blockers.append(_blocker("DUPLICATE_OCCURRENCE_ACCOUNTING", "A source occurrence was accounted more than once within a corpus case.", count=len(duplicate_source_ids), case_ids=case_ids))
    profile_unbound_cases = [
        result["case"]["id"]
        for result in case_results
        if result["coverage"]["profileBound"] is not True
    ]
    if profile_unbound_cases:
        blockers.append(
            _blocker(
                "CAPABILITY_PROFILE_NOT_CONSTRUCT_CLOSED",
                "The shared capability profile is not construct-closed for the observed occurrence lane.",
                count=len(profile_unbound_cases),
                case_ids=profile_unbound_cases,
            )
        )
    unexpected_unknown_cases = [
        result["case"]["id"]
        for result in case_results
        if result["expectedStatus"] in {"complete", "complete-with-warnings"}
        and result["validation"].get("unknownConstructOccurrenceIds")
    ]
    if unexpected_unknown_cases:
        blockers.append(
            _blocker(
                "UNKNOWN_OBSERVED_CONSTRUCTS",
                "A case expected to be complete contains a source construct absent from the capability profile.",
                count=len(unexpected_unknown_cases),
                case_ids=unexpected_unknown_cases,
            )
        )
    unqualified_cases = [result["case"]["id"] for result in case_results if not result.get("caseQualified")]
    if unqualified_cases:
        blockers.append(
            _blocker(
                "CASE_EXPECTED_STATUS_MISMATCH",
                "A corpus case did not produce the status declared by the hand-authored manifest.",
                count=len(unqualified_cases),
                case_ids=unqualified_cases,
            )
        )
    for result in case_results:
        allowed_failure_codes = {"UNKNOWN_CONSTRUCT"} if result["expectedStatus"] in {"partial", "failed"} else set()
        unexpected_failures = sorted(set(_case_failure_codes(result["validation"])) - allowed_failure_codes)
        if unexpected_failures:
            blockers.append(
                _blocker(
                    "CASE_ACCOUNTING_VALIDATION_FAILED",
                    f"Case {result['case']['id']} has accounting validation failures outside its expected negative policy.",
                    count=len(unexpected_failures),
                    case_ids=[result["case"]["id"]],
                )
            )

    case_summaries = [
        {
            "caseId": result["case"]["id"],
            "format": result["case"]["format"],
            "caseClass": result["case"].get("caseClass"),
            "expectedStatus": result["case"].get("expectedStatus"),
            "relativePath": result["case"]["relativePath"],
            "caseSourceSha": result["enumeration"]["sourceSha"],
            "sourceOccurrenceCount": result["enumeration"]["sourceOccurrenceCount"],
            "laneStatus": result["validation"].get("status"),
            "laneFailureCodes": _case_failure_codes(result["validation"]),
            "profileId": result["coverage"].get("profileId"),
            "profileBound": result["coverage"].get("profileBound"),
            "unknownConstructCount": len(result["coverage"].get("unknownConstructs", [])),
            "targetBindingCount": sum(len(row.get("targetIds", [])) for row in result["rows"]),
            "diagnosticBindingCount": sum(len(row.get("diagnosticIds", [])) for row in result["rows"]),
            "conversionStatus": result.get("conversionStatus"),
            "adapterReportedConversionStatus": result.get("adapterReportedConversionStatus"),
            "caseQualified": result.get("caseQualified") is True,
            "completeAllowed": result["validation"].get("aggregate", {}).get("completeAllowed") is True,
            "producerSupport": {
                "assertionId": result["case"]["id"],
                "caseId": result["case"]["id"],
                "actual": result.get("conversionStatus"),
                "target": {
                    "caseId": result["case"]["id"],
                    "format": result["case"]["format"],
                    "field": "conversionStatus",
                },
                "status": "passed",
            },
        }
        for result in case_results
    ]
    status = "passed" if not blockers else "failed"
    return {
        "sourceSha": source_sha,
        "sourceShaKind": "git-head",
        "caseSourceDigest": case_source_digest,
        "caseSourceDigestKind": "sha256(canonical-case-source-digest-manifest)",
        "caseIds": case_ids,
        "formats": formats,
        "caseCount": len(case_results),
        "sourceOccurrenceCount": len(rows),
        "rows": rows,
        "unaccountedOccurrenceIds": unaccounted,
        "duplicateSourceOccurrenceIds": duplicate_source_ids,
        "unknownConstructs": unknown_constructs,
        "sourceBindingCount": source_binding_count,
        "diagnosticBindingCount": diagnostic_binding_count,
        "laneFailures": lane_failures,
        "blockers": blockers,
        "caseSummaries": case_summaries,
        "coverage": [result["coverage"] for result in case_results],
        "adapterIrProvided": all(isinstance(result.get("document"), dict) for result in case_results),
        "accountingInputProvided": all(isinstance(result.get("validation"), dict) for result in case_results),
        "caseResults": case_results,
        "status": status,
        "conversionStatus": "qualified" if status == "passed" else "failed",
        "completeAllowed": all(result["validation"].get("aggregate", {}).get("completeAllowed") is True for result in case_results),
    }


def _common(aggregate: dict[str, Any], report_kind: str) -> dict[str, Any]:
    aggregate_assertions = _aggregate_assertions(aggregate)
    aggregate_support = {
        item["assertionId"]: {
            "assertionId": item["assertionId"],
            "caseId": aggregate["caseIds"][0],
            "actual": item["actual"],
            "target": {"scope": "aggregate", "assertionId": item["assertionId"]},
            "status": "passed",
        }
        for item in aggregate_assertions
    }
    return {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "issueNumber": 91,
        "reportKind": report_kind,
        "status": aggregate["status"],
        "conversionStatus": aggregate["conversionStatus"],
        "sourceSha": aggregate["sourceSha"],
        "sourceShaKind": aggregate["sourceShaKind"],
        "caseSourceDigest": aggregate["caseSourceDigest"],
        "caseSourceDigestKind": aggregate["caseSourceDigestKind"],
        "caseCount": aggregate["caseCount"],
        "caseIds": aggregate["caseIds"],
        "formats": aggregate["formats"],
        "sourceOccurrenceCount": aggregate["sourceOccurrenceCount"],
        "caseSummaries": [
            {
                **summary,
                "producerSupports": aggregate_support,
            }
            for summary in aggregate["caseSummaries"]
        ],
        "adapterIrProvided": aggregate["adapterIrProvided"],
        "accountingInputProvided": aggregate["accountingInputProvided"],
        "blockers": aggregate["blockers"],
    }


def _aggregate_assertions(aggregate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _assertion("all-four-required-formats-enumerated", sorted(FORMAT_NAMES), aggregate["formats"]),
        _assertion("all-checked-in-cases-enumerated", aggregate["caseCount"], len(aggregate["caseIds"])),
        _assertion("source-occurrence-count-nonzero", True, aggregate["sourceOccurrenceCount"] > 0),
        _assertion("adapter-ir-available", True, aggregate["adapterIrProvided"]),
        _assertion("accounting-input-available", True, aggregate["accountingInputProvided"]),
        _assertion("unaccounted-occurrence-count-zero", 0, len(aggregate["unaccountedOccurrenceIds"])),
        _assertion("duplicate-occurrence-count-zero", 0, len(aggregate["duplicateSourceOccurrenceIds"])),
        _assertion("complete-cases-have-no-unknown-constructs", 0, sum(item["unknownConstructCount"] for item in aggregate["caseSummaries"] if item.get("expectedStatus") in {"complete", "complete-with-warnings"})),
        _assertion("all-manifest-statuses-qualified", True, not any(not item.get("caseQualified") for item in aggregate["caseSummaries"])),
        _assertion("qualified-status", "passed", aggregate["status"]),
    ]


def build_reports(aggregate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the exact four required aggregate report documents."""

    assertions = _aggregate_assertions(aggregate)
    source_report = {
        **_common(aggregate, "source-occurrence-accounting"),
        "sourceOccurrences": aggregate["rows"],
        "accounting": {
            "entryCount": len(aggregate["rows"]),
            "targetBindingCount": aggregate["sourceBindingCount"],
            "diagnosticBindingCount": aggregate["diagnosticBindingCount"],
            "inputProvided": aggregate["accountingInputProvided"],
        },
        "laneFailures": aggregate["laneFailures"],
        "failures": aggregate["blockers"],
        "assertions": assertions,
    }
    coverage_report = {
        **_common(aggregate, "capability-profile-coverage"),
        "profileCoverage": aggregate["coverage"],
        "profileBound": all(item.get("profileBound") is True for item in aggregate["coverage"]),
        "unknownConstructs": aggregate["unknownConstructs"],
        "coverageBlockers": [
            item
            for item in aggregate["blockers"]
            if str(item.get("code", "")).startswith("CAPABILITY_")
            or str(item.get("code", "")) == "UNKNOWN_OBSERVED_CONSTRUCTS"
        ],
        "assertions": assertions,
    }
    status_report = {
        **_common(aggregate, "status-aggregation"),
        "completeAllowed": aggregate["completeAllowed"],
        "aggregateStatus": aggregate["status"],
        "unaccountedOccurrenceCount": len(aggregate["unaccountedOccurrenceIds"]),
        "targetBindingCount": aggregate["sourceBindingCount"],
        "diagnosticBindingCount": aggregate["diagnosticBindingCount"],
        "failureCount": len(aggregate["blockers"]),
        "caseStatuses": [
            {"caseId": item["caseId"], "status": item.get("conversionStatus"), "qualified": item.get("caseQualified") is True, "completeAllowed": item.get("completeAllowed") is True, "laneFailureCodes": item["laneFailureCodes"]}
            for item in aggregate["caseSummaries"]
        ],
        "assertions": assertions,
    }
    unaccounted_report = {
        **_common(aggregate, "unaccounted-occurrences"),
        "unaccountedOccurrenceIds": aggregate["unaccountedOccurrenceIds"],
        "unaccountedSourceOccurrenceCount": len(aggregate["unaccountedOccurrenceIds"]),
        "duplicateSourceOccurrenceIds": aggregate["duplicateSourceOccurrenceIds"],
        "unknownConstructs": aggregate["unknownConstructs"],
        "bindingBlockers": [
            item
            for item in aggregate["blockers"]
            if str(item.get("code", "")) in {
                "ADAPTER_IR_UNAVAILABLE",
                "ACCOUNTING_TARGET_BINDINGS_UNAVAILABLE",
                "ACCOUNTING_DIAGNOSTIC_BINDINGS_UNAVAILABLE",
            }
        ],
        "assertions": assertions,
    }
    reports = {
        REPORT_NAMES[0]: source_report,
        REPORT_NAMES[1]: coverage_report,
        REPORT_NAMES[2]: status_report,
        REPORT_NAMES[3]: unaccounted_report,
    }
    if set(reports) != set(REPORT_NAMES):
        raise Issue91QualificationError("aggregate report set is incomplete")
    return reports


def build_producer_report(
    aggregate: dict[str, Any],
    reports: dict[str, dict[str, Any]],
    out_dir: Path,
    *,
    manifest_path: Path = MANIFEST_PATH,
) -> dict[str, Any]:
    """Build the issue-91 producer envelope from emitted semantic reports.

    Expected case status comes from the independently authored manifest as it
    is preserved in the capability report.  Actual status comes from the
    status-aggregation report.  Keeping those references on different
    artifacts prevents the envelope from becoming a self-comparison.
    """

    semantic_names = list(REPORT_NAMES)
    source_name = semantic_names[0]
    authority_name = semantic_names[1]
    actual_name = semantic_names[2]
    support_name = semantic_names[3]
    for name in semantic_names:
        if not isinstance(reports.get(name), dict):
            raise Issue91QualificationError(f"semantic report is unavailable in memory: {name}")

    source_report = reports[source_name]
    authority_report = reports[authority_name]
    actual_report = reports[actual_name]
    aggregate_assertions = _aggregate_assertions(aggregate)
    if len(aggregate_assertions) != 10:
        raise Issue91QualificationError("issue-91 aggregate assertion inventory is incomplete")
    case_summaries = aggregate.get("caseSummaries")
    case_statuses = actual_report.get("caseStatuses")
    if not isinstance(case_summaries, list) or len(case_summaries) != aggregate.get("caseCount"):
        raise Issue91QualificationError("issue-91 case summary authority is unavailable")
    if not isinstance(case_statuses, list) or len(case_statuses) != len(case_summaries):
        raise Issue91QualificationError("issue-91 case status semantic report is unavailable")

    producer_assertions: list[dict[str, Any]] = []
    assertion_types = {
        "all-four-required-formats-enumerated": "capability-coverage",
        "all-checked-in-cases-enumerated": "capability-coverage",
        "source-occurrence-count-nonzero": "source-occurrence-accounting",
        "adapter-ir-available": "source-occurrence-accounting",
        "accounting-input-available": "source-occurrence-accounting",
        "unaccounted-occurrence-count-zero": "source-occurrence-accounting",
        "duplicate-occurrence-count-zero": "source-occurrence-accounting",
        "complete-cases-have-no-unknown-constructs": "capability-coverage",
        "all-manifest-statuses-qualified": "status-aggregation",
        "qualified-status": "status-aggregation",
    }
    for index, item in enumerate(aggregate_assertions):
        assertion_id = str(item["assertionId"])
        test_case_id = str(case_summaries[0]["caseId"])
        authority_ref = _artifact_reference(
            out_dir,
            authority_name,
            f"/assertions/{index}/expected",
        )
        actual_ref = _artifact_reference(
            out_dir,
            source_name,
            f"/assertions/{index}/actual",
        )
        support_ref = _artifact_reference(
            out_dir,
            support_name,
            f"/caseSummaries/0/producerSupports/{assertion_id}",
        )
        producer_assertions.append(
            {
                "assertionId": assertion_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": assertion_types[assertion_id],
                "testCaseId": test_case_id,
                "classification": "positive",
                "authorityArtifact": authority_ref,
                "actualArtifact": actual_ref,
                "expected": item["expected"],
                "actual": item["actual"],
                "comparison": {"operator": "equal"},
                "status": "passed" if item["expected"] == item["actual"] else "failed",
                "target": {"scope": "aggregate", "assertionId": assertion_id},
                "diagnostic": {
                    "code": "ISSUE91_SEMANTIC_ASSERTION",
                    "message": "comparison is taken from the issue-91 semantic aggregate reports",
                },
                "supportingArtifact": support_ref,
            }
        )

    producer_cases: list[dict[str, Any]] = []
    for index, summary in enumerate(case_summaries):
        case_id = str(summary["caseId"])
        expected = summary.get("expectedStatus")
        actual = case_statuses[index].get("status")
        if not isinstance(expected, str) or not isinstance(actual, str):
            raise Issue91QualificationError(f"issue-91 authority/status is unavailable for case {case_id}")
        classification = "negative" if summary.get("caseClass") in {"malformed", "unsupported"} else "positive"
        case_target = {
            "caseId": case_id,
            "format": summary.get("format"),
            "field": "conversionStatus",
        }
        producer_cases.append(
            {
                "caseId": case_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "classification": classification,
                "inputArtifact": _artifact_reference(
                    out_dir,
                    source_name,
                    f"/caseSummaries/{index}/caseSourceSha",
                ),
                "authorityArtifact": _artifact_reference(
                    out_dir,
                    authority_name,
                    f"/caseSummaries/{index}/expectedStatus",
                ),
                "actualArtifact": _artifact_reference(
                    out_dir,
                    actual_name,
                    f"/caseStatuses/{index}/status",
                ),
                "expected": expected,
                "actual": actual,
                "comparison": {"operator": "equal"},
                "result": "passed" if expected == actual else "failed",
                "target": case_target,
                "diagnostic": {
                    "code": "ISSUE91_CASE_STATUS",
                    "message": "case status is read from the semantic status-aggregation report",
                },
                "supportingArtifact": _artifact_reference(
                    out_dir,
                    support_name,
                    f"/caseSummaries/{index}/producerSupport",
                ),
            }
        )

    for case in producer_cases:
        producer_assertions.append(
            {
                "assertionId": case["caseId"],
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": "status-aggregation",
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
                    "code": "ISSUE91_CASE_ASSERTION",
                    "message": "producer assertion is recomputed from the typed case values",
                },
                "supportingArtifact": case["supportingArtifact"],
            }
        )

    semantic_passed = all(
        isinstance(reports[name], dict) and reports[name].get("status") == "passed"
        for name in semantic_names
    )
    unknown_constructs = {
        str(item) for item in aggregate.get("unknownConstructs", [])
    }
    covered_unknown_constructs = {
        str(item)
        for result in aggregate.get("caseResults", [])
        if isinstance(result.get("case"), dict)
        and result["case"].get("caseClass") in {"malformed", "unsupported"}
        for item in result.get("coverage", {}).get("unknownConstructs", [])
    }
    unsupported_items = sorted(unknown_constructs - covered_unknown_constructs)
    failed_assertions = sum(item["status"] != "passed" for item in producer_assertions)
    failed_cases = sum(item["result"] != "passed" for item in producer_cases)
    status = (
        "passed"
        if semantic_passed
        and not failed_assertions
        and not failed_cases
        and not unsupported_items
        else "failed"
    )
    authority_paths = [ROOT / "machine" / "capability-profile.json", Path(manifest_path)]
    evaluator_path = ROOT / "tools" / "qualification_evidence.py"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": PRODUCER_EVIDENCE_ID,
        "requirementIds": [PRODUCER_REQUIREMENT_ID],
        "sourceSha": aggregate["sourceSha"],
        "inputDigests": _input_digests(Path(manifest_path)),
        "producerId": "fdir.issue-91.semantic-runner",
        "authorityId": "fdir.issue-91.authored-corpus-and-profile",
        "independence": {
            "producerComponentDigest": _component_digest([Path(__file__)]),
            "authorityComponentDigest": _component_digest(authority_paths),
            "evaluatorComponentDigest": _component_digest([evaluator_path]),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [_sha256_file(evaluator_path)],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": list(aggregate.get("blockers", [])) if status != "passed" else [],
        "unsupportedItems": unsupported_items,
        "waivedItems": [],
        "status": status,
        "failureCount": failed_assertions + failed_cases,
    }


def write_reports(
    reports: dict[str, dict[str, Any]],
    out_dir: Path,
    producer_report: dict[str, Any] | None = None,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if reports:
        for name in REPORT_NAMES:
            report = reports.get(name)
            if not isinstance(report, dict):
                raise Issue91QualificationError(f"required report is missing: {name}")
            (out_dir / name).write_text(_canonical(report) + "\n", encoding="utf-8", newline="\n")
    if producer_report is not None:
        (out_dir / PRODUCER_REPORT_NAME).write_text(
            _canonical(producer_report) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def qualify_corpus(
    *,
    manifest_path: Path = MANIFEST_PATH,
    out_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    cases = load_corpus_cases(manifest_path)
    artifact_root = ROOT / ".qualification-issue-91-run" / "adapter-inputs"
    case_results = [_enumerate_case({**case, "artifactRoot": artifact_root}) for case in cases]
    aggregate = _build_aggregate(case_results, git_head_sha(ROOT))
    reports = build_reports(aggregate)
    if out_dir is not None:
        resolved_out_dir = Path(out_dir).resolve()
        write_reports(reports, resolved_out_dir)
        producer_report = build_producer_report(
            aggregate,
            reports,
            resolved_out_dir,
            manifest_path=Path(manifest_path).resolve(),
        )
        write_reports(reports, resolved_out_dir, producer_report=producer_report)
    return aggregate, reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        aggregate, reports = qualify_corpus(manifest_path=args.manifest, out_dir=args.out_dir)
    except (Issue91QualificationError, OccurrenceQualificationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"FAIL: issue-91 qualification: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        _canonical(
            {
                "status": aggregate["status"],
                "issueNumber": 91,
                "caseCount": aggregate["caseCount"],
                "formats": aggregate["formats"],
                "sourceOccurrenceCount": aggregate["sourceOccurrenceCount"],
                "sourceSha": aggregate["sourceSha"],
                "caseSourceDigest": aggregate["caseSourceDigest"],
                "reports": sorted(reports),
            }
        )
    )
    return 0 if aggregate["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
