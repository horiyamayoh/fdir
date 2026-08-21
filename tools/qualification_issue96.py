"""Run the bounded, independent-oracle qualification lane for issue #96.

The corpus contains authored source bytes and literal source expectations.  The
runner invokes the public ``convert_document.py`` boundary, but it never
imports an adapter helper to manufacture an expected value.  Every report is
written even when the implementation is incomplete.  A surviving mismatch,
unaccounted occurrence, or undetected negative mutation returns exit status 1.

This is deliberately a qualification lane, not a whole-issue completion claim.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from typing import Any, Iterable, Literal, Sequence
import xml.etree.ElementTree as ET
import zipfile

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style imports.
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-96-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-96"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
REPORT_NAMES = {
    "relationships": "relationship-closure.json",
    "resources": "resource-availability.json",
    "annotations": "annotation-link-field-report.json",
    "revisions": "revision-range-report.json",
}
PROJECTIONS = ("edges", "resources", "annotations", "links", "fields", "revisions")
REQUIRED_NEGATIVE_CASES = {
    "relation-target-reassigned",
    "missing-target-preserved-available",
    "external-target-available",
    "hyperlink-content-traversal-disabled",
    "anchor-deleted",
    "pdf-annots-marker-fabrication",
    "pdf-subtype-action-ignored",
    "resource-payloadless-available",
    "external-availability-confused",
    "relation-reciprocity-deleted",
}
EVIDENCE_ID = "issue-96-relationship-closure"
REQUIREMENT_ID = "QUAL-96-RELATIONSHIP-CLOSURE"
Issue96EvaluatorType = Literal["relationship-closure", "mutation-killed"]
RELATIONSHIP_EVALUATOR: Issue96EvaluatorType = "relationship-closure"
MUTATION_EVALUATOR: Issue96EvaluatorType = "mutation-killed"
PRODUCER_ARTIFACT_REPORT_NAMES = (
    REPORT_NAMES["relationships"],
    REPORT_NAMES["resources"],
    REPORT_NAMES["annotations"],
    REPORT_NAMES["revisions"],
)
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_ATTR_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_OBJECT_RE = re.compile(rb"(?m)(\d+)\s+(\d+)\s+obj\b")
_REFERENCE_RE = re.compile(rb"(?<![A-Za-z0-9])([0-9]+)\s+([0-9]+)\s+R\b")


class QualificationError(RuntimeError):
    """Raised when the bounded lane cannot safely establish its evidence."""


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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        raise QualificationError(f"cannot obtain an exact 40-character source SHA: {value!r}")
    return value


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    raise QualificationError("corpus text payload must be a string or string list")


def _payload_bytes(spec: Any) -> bytes:
    if isinstance(spec, dict):
        encoding = spec.get("encoding", "utf-8")
        value = spec.get("value")
        if encoding == "base64":
            if not isinstance(value, str):
                raise QualificationError("base64 payload must contain a string value")
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise QualificationError(f"invalid base64 corpus payload: {exc}") from exc
        if encoding == "utf-8":
            return _as_text(value).encode("utf-8")
        raise QualificationError(f"unsupported corpus payload encoding: {encoding!r}")
    return _as_text(spec).encode("utf-8")


def _safe_member_name(name: Any) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise QualificationError(f"unsafe package member name: {name!r}")
    if name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
        raise QualificationError(f"unsafe package member name: {name!r}")
    return name


def _validate_case_lists(fixture: dict[str, Any]) -> None:
    expected = fixture.get("expected")
    if not isinstance(expected, dict):
        raise QualificationError(f"fixture has no expected projection: {fixture.get('fixtureId')}")
    for projection in PROJECTIONS:
        if not isinstance(expected.get(projection), list):
            raise QualificationError(f"fixture projection {projection!r} is not a list: {fixture.get('fixtureId')}")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise QualificationError(f"fixture has no qualification cases: {fixture.get('fixtureId')}")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
            raise QualificationError(f"invalid fixture case in {fixture.get('fixtureId')}")
        if case.get("projection") not in PROJECTIONS:
            raise QualificationError(f"invalid case projection: {case.get('caseId')}")


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict):
        raise QualificationError("issue #96 corpus root must be an object")
    if corpus.get("issueNumber") != 96:
        raise QualificationError("issue #96 corpus has the wrong issue number")
    if corpus.get("qualificationScope") != "bounded-independent-relationship-resource-annotation-field-revision-closure":
        raise QualificationError("issue #96 corpus is not marked as the bounded closure lane")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #96 corpus has no oracle declaration")
    if oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #96 corpus does not declare an independent expected-value oracle")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #96 corpus permits adapter-derived expected values")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #96 corpus has no forbidden derivation list")
    if corpus.get("reportNames") != list(REPORT_NAMES.values()):
        raise QualificationError("issue #96 corpus report names do not match the required four reports")
    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("issue #96 corpus has no fixtures")
    formats: set[str] = set()
    fixture_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("issue #96 fixture is not an object")
        fixture_id = fixture.get("fixtureId")
        format_name = fixture.get("format")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate fixture id: {fixture_id!r}")
        if format_name not in {"docx", "xlsx", "pdf", "markdown"}:
            raise QualificationError(f"unsupported issue #96 fixture format: {format_name!r}")
        if fixture.get("payloadKind") not in {"zip", "text"}:
            raise QualificationError(f"invalid fixture payload kind: {fixture_id}")
        if fixture["payloadKind"] == "zip" and not isinstance(fixture.get("parts"), dict):
            raise QualificationError(f"ZIP fixture has no parts: {fixture_id}")
        if fixture["payloadKind"] == "text" and not isinstance(fixture.get("value"), str):
            raise QualificationError(f"text fixture has no value: {fixture_id}")
        _validate_case_lists(fixture)
        formats.add(format_name)
        fixture_ids.add(fixture_id)
    if formats != {"docx", "xlsx", "pdf", "markdown"}:
        raise QualificationError(f"issue #96 corpus must cover all four formats, got {sorted(formats)}")
    negatives = corpus.get("negativeCases")
    if not isinstance(negatives, list):
        raise QualificationError("issue #96 corpus has no negative cases")
    negative_ids = {item.get("caseId") for item in negatives if isinstance(item, dict)}
    missing_negatives = sorted(REQUIRED_NEGATIVE_CASES - negative_ids)
    if missing_negatives:
        raise QualificationError(f"issue #96 corpus is missing negative cases: {missing_negatives}")
    for item in negatives:
        if not isinstance(item, dict) or not isinstance(item.get("caseId"), str):
            raise QualificationError("issue #96 negative case is malformed")
        if item.get("fixtureId") not in fixture_ids:
            raise QualificationError(f"negative case references an unknown fixture: {item.get('caseId')}")
        if item.get("projection") not in PROJECTIONS:
            raise QualificationError(f"negative case has an invalid projection: {item.get('caseId')}")
        mutation = item.get("mutation")
        if not isinstance(mutation, dict) or mutation.get("op") not in {"set", "delete", "append"}:
            raise QualificationError(f"negative case has an invalid mutation: {item.get('caseId')}")
    return corpus


def _fixture_ext(format_name: str) -> str:
    return {"docx": "docx", "xlsx": "xlsx", "pdf": "pdf", "markdown": "md"}[format_name]


def _materialize_fixture(fixture: dict[str, Any], work: Path) -> Path:
    fixture_id = str(fixture["fixtureId"])
    path = work / f"{fixture_id}.{_fixture_ext(str(fixture['format']))}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if fixture["payloadKind"] == "zip":
        parts = fixture.get("parts", {})
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for raw_name in sorted(parts):
                name = _safe_member_name(raw_name)
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, _payload_bytes(parts[raw_name]))
    else:
        path.write_bytes(_payload_bytes(fixture["value"]))
    return path


def _run_converter(fixture: dict[str, Any], source_path: Path, work: Path) -> dict[str, Any]:
    fixture_id = str(fixture["fixtureId"])
    output_path = work / f"{fixture_id}.json"
    evidence_path = work / f"{fixture_id}.evidence.json"
    command = [
        sys.executable,
        str(CONVERTER_PATH),
        "convert",
        str(source_path),
        "--format",
        str(fixture["format"]),
        "--out",
        str(output_path),
        "--evidence",
        str(evidence_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout[-2000:]
        stderr = result.stderr[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = 125
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
    document: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    if output_path.is_file():
        value = _read_json(output_path)
        if isinstance(value, dict):
            document = value
    if evidence_path.is_file():
        value = _read_json(evidence_path)
        if isinstance(value, dict):
            evidence = value
    if not document:
        stderr = f"{stderr}\nconverter produced no document".strip()
    return {
        "fixtureId": fixture_id,
        "format": fixture["format"],
        "sourceSha256": _sha256_file(source_path),
        "commandExitCode": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "document": document,
        "evidence": evidence,
    }


def _local_attr(element: ET.Element, name: str, default: str = "") -> str:
    for key, value in element.attrib.items():
        if key == name or key.rsplit("}", 1)[-1] == name:
            return value
    return default


def _opc_source_name(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return "[package]"
    parent = rels_name.rsplit("/", 1)[0]
    filename = rels_name.rsplit("/", 1)[-1]
    if not parent.endswith("/_rels") or not filename.endswith(".rels"):
        raise QualificationError(f"invalid OPC relationship part name: {rels_name}")
    return f"{parent[:-6]}/{filename[:-5]}".lstrip("/")


def _opc_resolved_target(source_name: str, target: str) -> str:
    if source_name == "[package]":
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_name), target))


def _opc_source_facts(source_path: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    with zipfile.ZipFile(source_path) as archive:
        names = set(archive.namelist())
        relationship_parts = sorted(name for name in names if name == "_rels/.rels" or "/_rels/" in name)
        for rels_name in relationship_parts:
            source_name = _opc_source_name(rels_name)
            root = ET.fromstring(archive.read(rels_name))
            for relationship in list(root):
                if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
                    continue
                relationship_id = _local_attr(relationship, "Id")
                raw_target = _local_attr(relationship, "Target")
                target_mode = _local_attr(relationship, "TargetMode", "Internal")
                external = target_mode.casefold() == "external"
                target = raw_target if external else _opc_resolved_target(source_name, raw_target)
                facts.append({
                    "relationshipFile": rels_name,
                    "relationshipId": relationship_id,
                    "owner": source_name,
                    "rawTarget": raw_target,
                    "target": target,
                    "targetMode": "external" if external else "internal",
                    "type": _local_attr(relationship, "Type"),
                    "status": "unavailable" if external or target not in names else "preserved",
                })
    return facts


def _pdf_source_facts(source_path: Path) -> list[dict[str, Any]]:
    raw = source_path.read_bytes()
    matches = list(_OBJECT_RE.finditer(raw))
    facts: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        object_number = int(match.group(1))
        generation = int(match.group(2))
        end = raw.find(b"endobj", match.end())
        if end < 0:
            end = len(raw)
        object_data = raw[match.end():end]
        for target_match in _REFERENCE_RE.finditer(object_data):
            target = (int(target_match.group(1)), int(target_match.group(2)))
            target_exists = any(int(item.group(1)) == target[0] and int(item.group(2)) == target[1] for item in matches)
            facts.append({
                "object": f"{object_number} {generation}",
                "owner": f"{object_number} {generation} obj",
                "reference": f"{target[0]} {target[1]} R",
                "target": f"{target[0]} {target[1]} obj" if target_exists else f"{target[0]} {target[1]} R",
                "targetMode": "internal",
                "type": "indirect-reference",
                "status": "preserved" if target_exists else "unavailable",
                "ordinal": len(facts),
            })
    return facts


def _markdown_source_check(fixture: dict[str, Any], source_path: Path) -> list[dict[str, Any]]:
    text = source_path.read_text(encoding="utf-8")
    facts: list[dict[str, Any]] = []
    for edge in fixture["expected"]["edges"]:
        locator = edge.get("sourceLocator", {})
        # Relationship edges synthesized from an authored definition (for
        # example, a reference-use -> bookmark relation) have no direct
        # source token.  They are checked by the typed relationship
        # projection, not counted as a second source occurrence.
        if not isinstance(locator, dict) or not locator.get("token") or not isinstance(locator.get("line"), int):
            continue
        token = locator.get("token")
        line = locator.get("line")
        found = isinstance(token, str) and token in text
        line_found = isinstance(line, int) and 1 <= line <= len(text.splitlines())
        facts.append({
            "sourceOccurrenceId": edge.get("sourceOccurrenceId"),
            "target": edge.get("target"),
            "line": line,
            "token": token,
            "found": found and line_found,
            "status": edge.get("status"),
        })
    return facts


def _source_facts_for_fixture(fixture: dict[str, Any], source_path: Path) -> dict[str, Any]:
    expected_edges = fixture["expected"]["edges"]
    format_name = fixture["format"]
    if format_name in {"docx", "xlsx"}:
        facts = _opc_source_facts(source_path)
        actual_by_locator = {(item["relationshipFile"], item["relationshipId"]): item for item in facts}
        mismatches: list[dict[str, Any]] = []
        accounted: set[tuple[str, str]] = set()
        for edge in expected_edges:
            locator = edge.get("sourceLocator", {})
            key = (locator.get("relationshipFile"), locator.get("relationshipId"))
            actual = actual_by_locator.get(key)
            if actual is None:
                mismatches.append({"kind": "source-occurrence-missing", "expected": edge, "actual": None})
                continue
            accounted.add(key)
            for field in ("owner", "target", "type", "targetMode", "status"):
                if actual.get(field) != edge.get(field):
                    mismatches.append({"kind": "source-fact-mismatch", "sourceOccurrenceId": edge.get("sourceOccurrenceId"), "field": field, "expected": edge.get(field), "actual": actual.get(field)})
        unexpected = [item for key, item in actual_by_locator.items() if key not in accounted]
        return {"facts": facts, "mismatches": mismatches, "unexpectedCount": len(unexpected), "expectedCount": len(expected_edges), "actualCount": len(facts)}
    if format_name == "pdf":
        facts = _pdf_source_facts(source_path)
        mismatches: list[dict[str, Any]] = []
        used: set[int] = set()
        for edge in expected_edges:
            locator = edge.get("sourceLocator", {})
            candidates = [
                (index, actual)
                for index, actual in enumerate(facts)
                if index not in used and actual.get("object") == locator.get("object") and actual.get("reference") == locator.get("reference")
            ]
            if not candidates:
                mismatches.append({"kind": "source-occurrence-missing", "expected": edge, "actual": None})
                continue
            index, actual = candidates[0]
            used.add(index)
            for field in ("owner", "target", "type", "targetMode", "status"):
                if actual.get(field) != edge.get(field):
                    mismatches.append({"kind": "source-fact-mismatch", "sourceOccurrenceId": edge.get("sourceOccurrenceId"), "field": field, "expected": edge.get(field), "actual": actual.get(field)})
        return {"facts": facts, "mismatches": mismatches, "unexpectedCount": len(facts) - len(used), "expectedCount": len(expected_edges), "actualCount": len(facts)}
    facts = _markdown_source_check(fixture, source_path)
    mismatches = [item for item in facts if item.get("found") is not True]
    return {"facts": facts, "mismatches": mismatches, "unexpectedCount": 0, "expectedCount": len(expected_edges), "actualCount": len(facts)}


def _collection_maps(document: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for collection, key in (("parts", "partId"), ("resources", "resourceId"), ("nodes", "nodeId"), ("texts", "textId"), ("annotations", "annotationId"), ("relations", "relationId"), ("fields", "fieldId"), ("formulas", "formulaId"), ("extensions", "extensionId"), ("sourceMaps", "sourceMapId")):
        result[collection] = {str(item[key]): item for item in document.get(collection, []) if isinstance(item, dict) and key in item}
    return result


def _part_symbol(item: dict[str, Any]) -> str:
    name = str(item.get("name", item.get("partId", "")))
    if name == "OOXML package":
        return "[package]"
    if name == "PDF document":
        return "pdf-document"
    return name


def _node_symbol(item: dict[str, Any], source_maps: dict[str, dict[str, Any]]) -> str:
    node_id = str(item.get("nodeId", ""))
    if node_id.startswith("node-"):
        return node_id[5:]
    if item.get("kind") == "run":
        return "markdown-run"
    locator = source_maps.get(node_id, {}).get("locator", {})
    if isinstance(locator, dict) and locator.get("lineStart"):
        return "markdown-run"
    return node_id


def _target_symbol(identifier: Any, maps: dict[str, dict[str, dict[str, Any]]]) -> str:
    value = str(identifier)
    if value in maps.get("parts", {}):
        return _part_symbol(maps["parts"][value])
    if value in maps.get("resources", {}):
        resource = maps["resources"][value]
        return str(resource.get("externalTarget") or resource.get("derivedHandle") or value)
    if value in maps.get("nodes", {}):
        return _node_symbol(maps["nodes"][value], maps.get("sourceMaps", {}))
    return value


def _actual_edges(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _collection_maps(document)
    rows: list[dict[str, Any]] = []
    for relation in document.get("relations", []):
        if not isinstance(relation, dict):
            continue
        # Resource-consumer observations are projected by the resource lane;
        # the edge lane is the authored package/document relationship graph.
        if relation.get("kind") == "usesResource":
            continue
        relation_id = relation.get("relationId")
        owner_id = relation.get("fromId")
        owner_item = maps.get("parts", {}).get(str(owner_id)) or maps.get("nodes", {}).get(str(owner_id))
        relationship_ids = owner_item.get("relationshipIds", []) if isinstance(owner_item, dict) else []
        rows.append({
            "relationId": relation_id,
            "sourceOccurrenceId": relation.get("sourceOccurrenceId") or relation.get("sourceRelationshipId"),
            "owner": _target_symbol(owner_id, maps),
            "target": _target_symbol(relation.get("toId"), maps),
            "type": relation.get("type") or relation.get("relationshipType") or relation.get("sourceType"),
            "targetMode": relation.get("targetMode"),
            "kind": relation.get("kind"),
            "status": relation.get("status"),
            "reciprocity": relation_id in relationship_ids if isinstance(relationship_ids, list) else False,
        })
    return rows


def _resource_handle(item: dict[str, Any]) -> str:
    return str(item.get("externalTarget") or item.get("derivedHandle") or item.get("resourceId", ""))


def _actual_resources(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _collection_maps(document)
    rows: list[dict[str, Any]] = []
    for resource in document.get("resources", []):
        if not isinstance(resource, dict):
            continue
        row = dict(resource)
        row["handle"] = _resource_handle(resource)
        row["resourceId"] = resource.get("resourceId")
        row["consumerCount"] = sum(1 for relation in document.get("relations", []) if isinstance(relation, dict) and relation.get("toId") == resource.get("resourceId"))
        row["sourceRelationshipIds"] = [relation.get("relationId") for relation in document.get("relations", []) if isinstance(relation, dict) and relation.get("toId") == resource.get("resourceId")]
        row["ownerSymbols"] = [_target_symbol(relation.get("fromId"), maps) for relation in document.get("relations", []) if isinstance(relation, dict) and relation.get("toId") == resource.get("resourceId")]
        rows.append(row)
    return rows


def _text_for_node(node: dict[str, Any], maps: dict[str, dict[str, dict[str, Any]]]) -> str:
    text_ids = node.get("textIds", [])
    values: list[tuple[str, str]] = []
    for text_id in text_ids if isinstance(text_ids, list) else []:
        text = maps.get("texts", {}).get(str(text_id))
        if not isinstance(text, dict):
            continue
        value = str(text.get("value", ""))
        representation = str(text.get("representation", "source"))
        values.append((representation, value))
    normalized = [value for representation, value in values if representation == "normalized"]
    if normalized:
        return "".join(normalized)
    return "".join(value for _representation, value in values)


def _annotation_display_text(annotation: dict[str, Any], maps: dict[str, dict[str, dict[str, Any]]]) -> str:
    values: list[str] = []
    for target_id in annotation.get("targetIds", []) if isinstance(annotation.get("targetIds"), list) else []:
        node = maps.get("nodes", {}).get(str(target_id))
        if isinstance(node, dict):
            values.append(_text_for_node(node, maps))
    return "".join(values)


def _actual_annotations(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _collection_maps(document)
    ordinals: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for annotation in document.get("annotations", []):
        if not isinstance(annotation, dict):
            continue
        kind = str(annotation.get("kind", ""))
        # Revisions have a dedicated projection below.  Keeping them in the
        # annotations collection is useful for the producer IR, but they are
        # not annotation occurrences in this lane's independent oracle.
        if kind == "revision":
            continue
        ordinals[kind] = ordinals.get(kind, 0) + 1
        row = dict(annotation)
        row["ordinal"] = ordinals[kind]
        row["displayText"] = annotation.get("displayText") if "displayText" in annotation else _annotation_display_text(annotation, maps)
        row["destination"] = str(annotation.get("destination", "") or "")
        body = annotation.get("body")
        if isinstance(body, str):
            if not row["destination"] and body.startswith("URI: "):
                row["destination"] = body[5:]
            elif not row["destination"] and body.startswith("Destination: "):
                row["destination"] = body[13:]
            elif not row["destination"] and body.startswith("GoToR: "):
                row["destination"] = body[7:]
            elif not row["destination"] and kind == "hyperlink":
                row["destination"] = body
        rows.append(row)
    return rows


def _actual_links(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for annotation in _actual_annotations(document):
        if annotation.get("kind") != "hyperlink":
            continue
        rows.append({
            "annotationId": annotation.get("annotationId"),
            "ordinal": annotation.get("ordinal"),
            "displayText": annotation.get("displayText", ""),
            "destination": annotation.get("destination", ""),
            "displayTextIndependent": bool(annotation.get("displayText")) and bool(annotation.get("destination")),
            "status": annotation.get("status"),
        })
    return rows


def _actual_fields(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _collection_maps(document)
    rows: list[dict[str, Any]] = []
    for field in document.get("fields", []):
        if not isinstance(field, dict):
            continue
        row = dict(field)
        if field.get("ownerNodeId"):
            row["owner"] = _target_symbol(field.get("ownerNodeId"), maps)
        rows.append(row)
    field_reference_ids = {
        str(field.get("referenceId"))
        for field in document.get("fields", [])
        if isinstance(field, dict) and field.get("referenceId")
    }
    for formula in document.get("formulas", []):
        if not isinstance(formula, dict):
            continue
        expression = formula.get("expression", {})
        values = formula.get("values", {})
        owner = maps.get("nodes", {}).get(str(formula.get("ownerCellId")), {})
        address = owner.get("address", {}) if isinstance(owner, dict) else {}
        row = {
            "fieldId": formula.get("formulaId"),
            "kind": formula.get("kind"),
            "instruction": expression.get("source") if isinstance(expression, dict) else None,
            "storedResult": ((values.get("stored") or {}).get("value") if isinstance(values, dict) else None),
            "computedResult": {"status": (values.get("computed") or {}).get("status")} if isinstance(values, dict) and isinstance(values.get("computed"), dict) else None,
            "range": formula.get("range"),
            "owner": formula.get("ownerAddress") or (f"{address.get('column')}" if isinstance(address, dict) and address.get("column") is not None else None),
            "status": formula.get("status"),
        }
        if not row["owner"] and isinstance(address, dict) and address.get("row") is not None and address.get("column") is not None:
            # The source fixture uses A1 notation; recover it from the cell node
            # when the adapter supplies a source map, otherwise retain a stable
            # row/column observation for the report.
            row["owner"] = str(owner.get("address", {}).get("raw", "")) or f"cell({address['row']},{address['column']})"
        rows.append(row)
    for annotation in _actual_annotations(document):
        if annotation.get("kind") != "form":
            continue
        if annotation.get("referenceId") in field_reference_ids:
            continue
        body = str(annotation.get("body", ""))
        instruction = body[7:] if body.startswith("Field: ") else body
        rows.append({"fieldId": annotation.get("annotationId"), "kind": "form", "instruction": instruction, "status": annotation.get("status")})
    return rows


def _actual_revisions(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for extension in document.get("extensions", []):
        if not isinstance(extension, dict) or extension.get("type") != "revision":
            continue
        payload = extension.get("payload") if isinstance(extension.get("payload"), dict) else {}
        row = {
            "revisionId": payload.get("revisionId"),
            "id": payload.get("revisionId"),
            "type": payload.get("kind"),
            "author": payload.get("author"),
            "date": payload.get("date"),
            "status": extension.get("status"),
        }
        revision_id = str(row.get("revisionId") or "")
        if revision_id not in by_id:
            by_id[revision_id] = row
            rows.append(row)
    # DOCX revisions are also represented as first-class annotated ranges so
    # the IR can carry before/after text and an exact balanced range without
    # overloading the legacy extension payload's string range.
    for annotation in document.get("annotations", []):
        if not isinstance(annotation, dict) or annotation.get("kind") != "revision":
            continue
        revision_id = str(annotation.get("revisionId") or "")
        row = by_id.get(revision_id)
        if row is None:
            row = {"revisionId": annotation.get("revisionId"), "id": annotation.get("revisionId")}
            by_id[revision_id] = row
            rows.append(row)
        for field, annotation_field in (
            ("type", "revisionType"),
            ("author", "author"),
            ("date", "date"),
            ("range", "range"),
            ("before", "before"),
            ("after", "after"),
            ("status", "status"),
        ):
            if annotation_field in annotation:
                row[field] = annotation.get(annotation_field)
    return rows


def _range_balanced(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("balanced") is True:
        return True
    if all(key in value for key in ("begin", "separate", "end")):
        try:
            return int(value["begin"]) < int(value["separate"]) < int(value["end"])
        except (TypeError, ValueError):
            return False
    return value.get("start") is not None and value.get("end") is not None


def _range_balance(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, int]:
    expected_ranges = [item.get("required", {}).get("range") for item in expected if isinstance(item.get("required"), dict) and "range" in item["required"]]
    actual_ranges = [item.get("range") for item in actual if "range" in item]
    return {
        "expectedRangeCount": len(expected_ranges),
        "expectedBalancedCount": sum(1 for value in expected_ranges if _range_balanced(value)),
        "actualRangeCount": len(actual_ranges),
        "actualBalancedCount": sum(1 for value in actual_ranges if _range_balanced(value)),
        "unbalancedOrMissingCount": sum(1 for value in expected_ranges if not _range_balanced(value)) + max(0, len(expected_ranges) - len(actual_ranges)),
    }


def _get_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    if not path:
        return True, current
    for token in path.split("."):
        if not isinstance(current, dict) or token not in current:
            return False, None
        current = current[token]
    return True, current


def _set_path(value: dict[str, Any], path: str, replacement: Any, *, delete: bool = False) -> None:
    tokens = path.split(".")
    current: Any = value
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            raise QualificationError(f"mutation path does not exist: {path}")
        current = current[token]
    if not isinstance(current, dict) or tokens[-1] not in current:
        raise QualificationError(f"mutation path does not exist: {path}")
    if delete:
        del current[tokens[-1]]
    else:
        current[tokens[-1]] = replacement


def _match_item(item: dict[str, Any], selector: dict[str, Any]) -> bool:
    return all(item.get(key) == value for key, value in selector.items())


def _apply_mutation(rows: list[dict[str, Any]], negative: dict[str, Any]) -> list[dict[str, Any]]:
    mutated = deepcopy(rows)
    target = negative.get("target", {})
    mutation = negative["mutation"]
    if target.get("append") is True:
        index = None
    else:
        index = next((i for i, item in enumerate(mutated) if isinstance(item, dict) and _match_item(item, target)), None)
        if index is None:
            raise QualificationError(f"negative mutation target not found: {negative.get('caseId')}")
    if mutation["op"] == "append":
        mutated.append(deepcopy(mutation.get("value")))
        return mutated
    assert index is not None
    item = mutated[index]
    if mutation["op"] == "set":
        _set_path(item, str(mutation["path"]), deepcopy(mutation.get("value")))
    elif mutation["op"] == "delete":
        _set_path(item, str(mutation["path"]), None, delete=True)
    return mutated


def _row_key(item: dict[str, Any], projection: str) -> tuple[Any, ...]:
    if projection == "edges":
        return (item.get("sourceOccurrenceId"), item.get("owner"), item.get("target"), item.get("type"))
    if projection == "resources":
        return (item.get("resourceKey"), item.get("handle"), item.get("resourceId"))
    if projection == "annotations":
        return (item.get("annotationKey"), item.get("referenceId"), item.get("kind"), item.get("ordinal"))
    if projection == "links":
        return (item.get("linkKey"), item.get("destination"), item.get("displayText"))
    if projection == "fields":
        return (item.get("fieldKey"), item.get("fieldId"), item.get("kind"), item.get("instruction"))
    return (item.get("revisionKey"), item.get("revisionId"), item.get("type"), item.get("id"))


def _projected_value(item: dict[str, Any], field: str) -> Any:
    """Read a field from either an adapter row or a literal oracle row."""

    if field in item:
        return item.get(field)
    required = item.get("required")
    if isinstance(required, dict) and field in required:
        return required.get(field)
    match = item.get("match")
    if isinstance(match, dict) and field in match:
        return match.get(field)
    return None


def _compare_projection(expected: list[dict[str, Any]], actual: list[dict[str, Any]], projection: str) -> dict[str, Any]:
    """Compare a literal projection without using any adapter implementation."""

    mismatches: list[dict[str, Any]] = []
    matched_actual: set[int] = set()
    if projection == "edges":
        for expected_item in expected:
            expected_relation_id = expected_item.get("expectedRelationId")
            candidates = [
                (index, item)
                for index, item in enumerate(actual)
                if index not in matched_actual and (
                    (expected_relation_id is not None and item.get("relationId") == expected_relation_id)
                    or (expected_relation_id is None and _row_key(expected_item, projection) == _row_key(item, projection))
                )
            ]
            if not candidates:
                candidates = [
                    (index, item)
                    for index, item in enumerate(actual)
                    if index not in matched_actual and _projected_value(item, "owner") == expected_item.get("owner") and _projected_value(item, "target") == expected_item.get("target") and _projected_value(item, "kind") == expected_item.get("kind")
                ]
            if not candidates:
                mismatches.append({"kind": "unaccounted-source-occurrence", "expected": expected_item, "actual": None})
                continue
            index, actual_item = candidates[0]
            matched_actual.add(index)
            for field in ("sourceOccurrenceId", "owner", "target", "type", "targetMode", "kind", "status", "reciprocity"):
                if _projected_value(actual_item, field) != expected_item.get(field):
                    mismatches.append({"kind": "edge-mismatch", "sourceOccurrenceId": expected_item.get("sourceOccurrenceId"), "field": field, "expected": expected_item.get(field), "actual": _projected_value(actual_item, field)})
        for index, item in enumerate(actual):
            if index not in matched_actual:
                mismatches.append({"kind": "unexpected-edge", "actual": item})
    else:
        for expected_item in expected:
            candidates = []
            for index, item in enumerate(actual):
                if index in matched_actual or not isinstance(item, dict):
                    continue
                if projection == "resources":
                    match = expected_item.get("match", {})
                    match_handle = match.get("handle")
                    match = match_handle is None or _projected_value(item, "handle") == match_handle
                elif projection == "annotations":
                    match = expected_item.get("match", {})
                    if match.get("referenceId") is not None:
                        match = _projected_value(item, "referenceId") == match.get("referenceId")
                    else:
                        match = _projected_value(item, "kind") == match.get("kind") and _projected_value(item, "ordinal") == match.get("ordinal")
                elif projection == "links":
                    match = expected_item.get("required", {}).get("destination") in {None, ""} or _projected_value(item, "destination") == expected_item.get("required", {}).get("destination")
                elif projection == "fields":
                    matcher = expected_item.get("match", {})
                    match = all(_projected_value(item, key) == value for key, value in matcher.items())
                else:
                    matcher = expected_item.get("match", {})
                    match = all(_projected_value(item, key) == value for key, value in matcher.items())
                if match:
                    candidates.append((index, item))
            if not candidates:
                mismatches.append({"kind": "expected-item-missing", "expected": expected_item})
                continue
            index, actual_item = candidates[0]
            matched_actual.add(index)
            required = expected_item.get("required", {})
            for field, expected_value in required.items():
                if field == "displayText" and expected_value is None:
                    continue
                if _projected_value(actual_item, field) != expected_value:
                    mismatches.append({"kind": "field-mismatch", "item": _row_key(expected_item, projection), "field": field, "expected": expected_value, "actual": _projected_value(actual_item, field)})
        for index, item in enumerate(actual):
            if index not in matched_actual and projection in {"annotations", "links", "fields", "revisions"}:
                mismatches.append({"kind": "unexpected-item", "actual": item})
    return {
        "projection": projection,
        "expectedCount": len(expected),
        "actualCount": len(actual),
        "mismatchCount": len(mismatches),
        "status": "passed" if not mismatches else "failed",
        "mismatches": mismatches,
    }


def _evaluate_fixture(fixture: dict[str, Any], execution: dict[str, Any], source_path: Path) -> dict[str, Any]:
    document = execution.get("document") if isinstance(execution.get("document"), dict) else {}
    expected = fixture["expected"]
    source = _source_facts_for_fixture(fixture, source_path)
    actual = {
        "edges": _actual_edges(document),
        "resources": _actual_resources(document),
        "annotations": _actual_annotations(document),
        "links": _actual_links(document),
        "fields": _actual_fields(document),
        "revisions": _actual_revisions(document),
    }
    projection_results = {
        projection: _compare_projection(expected[projection], actual[projection], projection)
        for projection in PROJECTIONS
    }
    for projection in PROJECTIONS:
        projection_results[projection]["expectedRows"] = deepcopy(expected[projection])
        projection_results[projection]["actualRows"] = deepcopy(actual[projection])
        if projection in {"fields", "revisions"}:
            projection_results[projection]["rangeBalance"] = _range_balance(expected[projection], actual[projection])
    projection_results["source"] = {
        "projection": "source",
        "expectedCount": source["expectedCount"],
        "actualCount": source["actualCount"],
        "mismatchCount": len(source["mismatches"]) + int(source.get("unexpectedCount", 0)),
        "status": "passed" if not source["mismatches"] and not source.get("unexpectedCount") else "failed",
        "mismatches": source["mismatches"],
        "unexpectedCount": source.get("unexpectedCount", 0),
    }
    case_results = []
    for case in fixture["cases"]:
        result = projection_results[str(case["projection"])]
        case_results.append({
            "caseId": case["caseId"],
            "category": case.get("category"),
            "projection": case["projection"],
            "status": result["status"],
            "mismatchCount": result["mismatchCount"],
        })
    return {
        "fixtureId": fixture["fixtureId"],
        "format": fixture["format"],
        "commandExitCode": execution.get("commandExitCode"),
        "conversionStatus": (document.get("conversion", {}) if isinstance(document, dict) else {}).get("status"),
        "evidenceOutcome": (execution.get("evidence", {}) if isinstance(execution.get("evidence"), dict) else {}).get("outcome"),
        "sourceSha256": execution.get("sourceSha256"),
        "sourceFacts": source,
        "projections": projection_results,
        "actual": actual,
        "cases": case_results,
    }


def _run_negative_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
    results: list[dict[str, Any]] = []
    for negative in corpus["negativeCases"]:
        fixture = fixtures[str(negative["fixtureId"])]
        projection = str(negative["projection"])
        baseline = fixture["expected"][projection]
        try:
            mutated = _apply_mutation(baseline, negative)
            comparison = _compare_projection(baseline, mutated, projection)
            detected = comparison["mismatchCount"] > 0
            error = None
        except Exception as exc:
            mutated = None
            comparison = {"mismatchCount": 0, "mismatches": []}
            detected = False
            error = f"{type(exc).__name__}: {exc}"
        results.append({
            "caseId": negative["caseId"],
            "defect": negative.get("defect"),
            "fixtureId": negative["fixtureId"],
            "projection": projection,
            "mutation": negative["mutation"],
            "oracleMutationDetected": detected,
            "oracleExpected": deepcopy(baseline),
            "oracleActual": deepcopy(mutated),
            "mismatchCount": comparison.get("mismatchCount", 0),
            "mismatches": comparison.get("mismatches", []),
            "error": error,
            "status": "passed" if detected else "failed",
        })
    return results


def _assertion(assertion_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if expected == actual else "failed",
    }


def _case_counts(corpus: dict[str, Any]) -> dict[str, Any]:
    fixtures = corpus["fixtures"]
    result: dict[str, Any] = {
        "fixtures": len(fixtures),
        "formats": {format_name: sum(1 for fixture in fixtures if fixture["format"] == format_name) for format_name in ("docx", "xlsx", "pdf", "markdown")},
        "fixtureCases": sum(len(fixture["cases"]) for fixture in fixtures),
        "relationships": sum(len(fixture["expected"]["edges"]) for fixture in fixtures),
        "resources": sum(len(fixture["expected"]["resources"]) for fixture in fixtures),
        "annotations": sum(len(fixture["expected"]["annotations"]) for fixture in fixtures),
        "links": sum(len(fixture["expected"]["links"]) for fixture in fixtures),
        "fields": sum(len(fixture["expected"]["fields"]) for fixture in fixtures),
        "revisions": sum(len(fixture["expected"]["revisions"]) for fixture in fixtures),
        "negativeCases": len(corpus["negativeCases"]),
    }
    return result


def _projection_failures(evaluations: list[dict[str, Any]], projection: str) -> tuple[int, int, int, list[dict[str, Any]]]:
    mismatch = 0
    expected_count = 0
    actual_count = 0
    details: list[dict[str, Any]] = []
    for evaluation in evaluations:
        result = evaluation["projections"][projection]
        mismatch += int(result.get("mismatchCount", 0))
        expected_count += int(result.get("expectedCount", 0))
        actual_count += int(result.get("actualCount", 0))
        details.append({"fixtureId": evaluation["fixtureId"], "format": evaluation["format"], **result})
    return mismatch, expected_count, actual_count, details


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    return [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue96.py",
        ROOT / "tools" / "test_qualification_issue96.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "machine" / "reference-registry.json",
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "tools" / "adapter_xlsx.py",
        ROOT / "tools" / "adapter_pdf.py",
        ROOT / "tools" / "adapter_markdown.py",
    ]


def _producer_rows(
    corpus: dict[str, Any] | None,
    evaluations: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    setup_error: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for evaluation in evaluations:
        fixture_id = str(evaluation.get("fixtureId", ""))
        format_name = evaluation.get("format")
        for case in evaluation.get("cases", []):
            projection = str(case.get("projection", ""))
            if not fixture_id or not projection:
                continue
            expected = {
                "fixtureId": fixture_id,
                "format": format_name,
                "projection": projection,
                "status": "passed",
                "mismatchCount": 0,
            }
            actual = {
                "fixtureId": fixture_id,
                "format": format_name,
                "projection": projection,
                "status": case.get("status"),
                "mismatchCount": case.get("mismatchCount", 0),
            }
            case_id = f"positive-{fixture_id}-{projection}"
            rows.append({
                "caseId": case_id,
                "classification": "positive",
                "evaluatorType": RELATIONSHIP_EVALUATOR,
                "input": {"fixtureId": fixture_id, "format": format_name, "projection": projection},
                "expected": expected,
                "actual": actual,
                "result": "passed" if expected == actual else "failed",
                "target": {"fixtureId": fixture_id, "format": format_name, "projection": projection, "dimension": "relationship-closure"},
                "diagnostic": {"code": "ISSUE-96-RELATIONSHIP-CLOSURE", "message": "authored relationship/resource/annotation/field/revision closure is compared with public-converter output"},
                "oracleEvidence": {"identity": "authored-independent-relationship-oracle", "expectedValuesAreRuntimeIndependent": True},
            })

    for result in negative_results:
        case_id = str(result.get("caseId", ""))
        if not case_id:
            continue
        expected_value = {
            "fixtureId": result.get("fixtureId"),
            "projection": result.get("projection"),
            "rows": deepcopy(result.get("oracleExpected")),
        }
        mutated_value = {
            "fixtureId": result.get("fixtureId"),
            "projection": result.get("projection"),
            "rows": deepcopy(result.get("oracleActual")),
        }
        detected = result.get("oracleMutationDetected") is True
        rows.append({
            "caseId": f"mutation-{case_id}",
            "classification": "mutation",
            "evaluatorType": MUTATION_EVALUATOR,
            "input": {"fixtureId": result.get("fixtureId"), "projection": result.get("projection"), "mutationCaseId": case_id},
            "expected": expected_value,
            "actual": mutated_value if detected else expected_value,
            "result": "passed" if detected else "failed",
            "target": {"mutationCaseId": case_id, "projection": result.get("projection"), "oracleMutationDetected": detected},
            "diagnostic": {"code": "ISSUE-96-MUTATION", "message": "authored relationship mutation must be detected by the independent oracle"},
            "oracleEvidence": {
                "oracleMutationDetected": detected,
                "oracleExpected": deepcopy(result.get("oracleExpected")),
                "oracleActual": deepcopy(result.get("oracleActual")),
                "mismatchCount": result.get("mismatchCount", 0),
            },
        })

    if setup_error or not rows:
        message = setup_error or "no issue #96 producer cases were generated"
        rows = [
            {
                "caseId": "setup-positive",
                "classification": "positive",
                "evaluatorType": RELATIONSHIP_EVALUATOR,
                "input": {"setup": "issue-96"},
                "expected": {"setup": "available"},
                "actual": {"setup": "unavailable", "error": message},
                "result": "failed",
                "target": {"phase": "qualification-setup"},
                "diagnostic": {"code": "ISSUE-96-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
            {
                "caseId": "setup-mutation",
                "classification": "mutation",
                "evaluatorType": MUTATION_EVALUATOR,
                "input": {"setup": "issue-96"},
                "expected": {"mutationDetected": True},
                "actual": {"mutationDetected": True},
                "result": "failed",
                "target": {"phase": "qualification-setup", "oracleMutationDetected": False},
                "diagnostic": {"code": "ISSUE-96-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
        ]
    return rows


def _write_producer_report(
    out_dir: Path,
    reports: dict[str, dict[str, Any]],
    corpus_path: Path,
    source_sha: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names=REPORT_NAMES,
        artifact_report_names=PRODUCER_ARTIFACT_REPORT_NAMES,
        issue_number=96,
        evidence_id=EVIDENCE_ID,
        requirement_id=REQUIREMENT_ID,
        source_sha=source_sha,
        input_paths=_producer_input_paths(corpus_path),
        producer_id="issue-96-relationship-closure-runner",
        authority_id="issue-96-authored-relationship-oracle",
        producer_component_path=Path(__file__),
        authority_component_path=Path(corpus_path),
        evaluator_component_path=ROOT / "tools" / "validate_qualification_contract.py",
        shared_component_paths=(ROOT / "tools" / "qualification_evidence.py",),
        rows=rows,
    )


def _make_report(
    report_kind: str,
    source_sha: str | None,
    corpus: dict[str, Any],
    evaluations: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    setup_failure: str | None = None,
    corpus_sha256: str | None = None,
) -> dict[str, Any]:
    projection_set = {
        "relationships": ("edges", "source"),
        "resources": ("resources",),
        "annotations": ("annotations", "links", "fields"),
        "revisions": ("revisions",),
    }[report_kind]
    details: dict[str, Any] = {}
    mismatch_count = 0
    expected_count = 0
    actual_count = 0
    unaccounted_occurrences = 0
    for projection in projection_set:
        if projection == "source":
            mismatch, expected_total, actual_total, projection_details = _projection_failures(evaluations, "source")
            unaccounted_occurrences += sum(item["sourceFacts"].get("unexpectedCount", 0) for item in evaluations)
        else:
            mismatch, expected_total, actual_total, projection_details = _projection_failures(evaluations, projection)
        mismatch_count += mismatch
        expected_count += expected_total
        actual_count += actual_total
        details[projection] = projection_details
    if report_kind == "relationships":
        details["exactEdgeLists"] = [
            {
                "fixtureId": evaluation["fixtureId"],
                "format": evaluation["format"],
                "expectedEdgeList": evaluation["projections"]["edges"].get("expectedRows", []),
                "actualEdgeList": evaluation["projections"]["edges"].get("actualRows", []),
                "sourceOccurrenceFacts": evaluation["sourceFacts"].get("facts", []),
            }
            for evaluation in evaluations
        ]
    if report_kind in {"annotations", "revisions"}:
        range_rows = [
            evaluation["projections"]["fields" if report_kind == "annotations" else "revisions"].get("rangeBalance", {})
            for evaluation in evaluations
        ]
        details["rangeBalance"] = {
            key: sum(int(row.get(key, 0)) for row in range_rows)
            for key in ("expectedRangeCount", "expectedBalancedCount", "actualRangeCount", "actualBalancedCount", "unbalancedOrMissingCount")
        }
    negative_failures = sum(1 for item in negative_results if item.get("status") != "passed")
    adapter_failures = sum(1 for item in evaluations if item.get("commandExitCode") != 0 or item.get("evidenceOutcome") != "success")
    setup_failure_count = 1 if setup_failure else 0
    failures: list[str] = []
    if setup_failure:
        failures.append(f"setup:{setup_failure}")
    if mismatch_count:
        failures.append(f"projection-mismatch-count={mismatch_count}")
    if unaccounted_occurrences:
        failures.append(f"unaccounted-occurrence-count={unaccounted_occurrences}")
    if negative_failures:
        failures.append(f"undetected-negative-defect-count={negative_failures}")
    if adapter_failures:
        failures.append(f"adapter-failure-count={adapter_failures}")
    assertions = [
        _assertion("source-sha-format", True, bool(re.fullmatch(r"[0-9a-f]{40}", source_sha or ""))),
        _assertion("authored-independent-oracle", False, corpus["oracle"].get("adapterHelpersUsedForExpected")),
        _assertion("bounded-case-count-positive", True, expected_count > 0 or report_kind == "revisions"),
        _assertion("projection-mismatch-zero", 0, mismatch_count),
        _assertion("unaccounted-occurrence-zero", 0, unaccounted_occurrences),
        _assertion("negative-defects-detected", 0, negative_failures),
        _assertion("whole-issue-completion-claim", False, False),
    ]
    if setup_failure:
        assertions.append(_assertion("qualification-setup", "available", "unavailable"))
    status = "passed" if not failures and all(item["status"] == "passed" for item in assertions) else "failed"
    unmet = list(corpus.get("unmetRequirements", []))
    if failures:
        unmet = failures + unmet
    compact_evaluations = [
        {
            "fixtureId": item["fixtureId"],
            "format": item["format"],
            "commandExitCode": item.get("commandExitCode"),
            "conversionStatus": item.get("conversionStatus"),
            "evidenceOutcome": item.get("evidenceOutcome"),
            "sourceSha256": item.get("sourceSha256"),
            "caseCount": len(item.get("cases", [])),
        }
        for item in evaluations
    ]
    return {
        "schema": "fdir/qualification-issue-96-report",
        "version": "1.0.0",
        "issueNumber": 96,
        "reportKind": REPORT_NAMES[report_kind].removesuffix(".json"),
        "qualificationScope": corpus["qualificationScope"],
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha256 or _sha256_file(DEFAULT_CORPUS_PATH),
        "status": status,
        "completionStatus": "incomplete-bounded-lane",
        "caseCounts": _case_counts(corpus),
        "reportExpectedCount": expected_count,
        "reportActualCount": actual_count,
        "mismatchCount": mismatch_count,
        "unaccountedOccurrenceCount": unaccounted_occurrences,
        "adapterFailureCount": adapter_failures,
        "setupFailureCount": setup_failure_count,
        "negativeDefectFailureCount": negative_failures,
        "rangeBalance": details.get("rangeBalance", {}),
        "assertions": assertions,
        "negativeDefectResults": negative_results,
        "fixtures": compact_evaluations,
        "details": details,
        "limitations": corpus.get("limitations", []),
        "unmetRequirements": unmet,
        "failureSummary": failures,
    }


def _fatal_report(report_kind: str, source_sha: str | None, corpus: dict[str, Any] | None, message: str, corpus_sha256: str | None = None) -> dict[str, Any]:
    base = corpus or {
        "qualificationScope": "bounded-independent-relationship-resource-annotation-field-revision-closure",
        "oracle": {"adapterHelpersUsedForExpected": False},
        "fixtures": [],
        "negativeCases": [],
        "limitations": ["Corpus could not be loaded."],
        "unmetRequirements": ["Qualification setup failed."],
    }
    report = _make_report(report_kind, source_sha, base, [], [], setup_failure=message, corpus_sha256=corpus_sha256)
    report["status"] = "failed"
    report["caseCounts"] = {"fixtures": 0, "fixtureCases": 0, "negativeCases": 0}
    report["failureSummary"] = [message]
    report["unmetRequirements"] = [message, *list(base.get("unmetRequirements", []))]
    return report


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    corpus: dict[str, Any] | None = None
    source_sha: str | None = None
    corpus_sha256: str | None = None
    evaluations: list[dict[str, Any]] = []
    negative_results: list[dict[str, Any]] = []
    try:
        source_sha = _source_sha()
        corpus_sha256 = _sha256_file(corpus_path)
        corpus = _load_corpus(corpus_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        work = out_dir / f"work-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
        executions: list[dict[str, Any]] = []
        for fixture in corpus["fixtures"]:
            source_path = _materialize_fixture(fixture, work)
            execution = _run_converter(fixture, source_path, work)
            executions.append(execution)
            evaluations.append(_evaluate_fixture(fixture, execution, source_path))
        negative_results = _run_negative_mutations(corpus)
        reports = {
            report_kind: _make_report(report_kind, source_sha, corpus, evaluations, negative_results, corpus_sha256=corpus_sha256)
            for report_kind in REPORT_NAMES
        }
        producer_report = _write_producer_report(
            out_dir,
            reports,
            Path(corpus_path),
            source_sha,
            _producer_rows(corpus, evaluations, negative_results),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        out_dir.mkdir(parents=True, exist_ok=True)
        reports = {
            report_kind: _fatal_report(report_kind, source_sha, corpus, message, corpus_sha256)
            for report_kind in REPORT_NAMES
        }
        _write_producer_report(
            out_dir,
            reports,
            Path(corpus_path),
            source_sha,
            _producer_rows(corpus, evaluations, negative_results, setup_error=message),
        )
        print(f"FAIL: issue #96 qualification setup: {message}", file=sys.stderr)
        return 1
    failed = [report_kind for report_kind, report in reports.items() if report.get("status") != "passed"]
    if producer_report.get("status") != "passed":
        failed.append("producer-report")
    if failed:
        failed_names = [REPORT_NAMES[item] if item in REPORT_NAMES else item for item in failed]
        print("FAIL: issue #96 bounded reports: " + ", ".join(failed_names), file=sys.stderr)
        return 1
    print("PASS: issue #96 bounded reports written: " + ", ".join(REPORT_NAMES.values()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_qualification(corpus_path=args.corpus.resolve(), out_dir=args.out_dir.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
