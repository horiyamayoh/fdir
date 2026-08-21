"""Run the bounded, independent topology qualification for issue #95.

The checked-in corpus is the oracle.  It contains authored package/text
fixtures and literal parts, story, surface, containment, and table-grid
expectations.  The runner invokes the public converter only to obtain an
actual document to inspect; it never imports an adapter or derives expected
values from adapter output.

This lane is deliberately fail-closed.  A report is ``passed`` only when all
authored positive topology vectors match, every source occurrence is
accounted for, the actual graph is reciprocal, and every authored mutation is
detected by the independent oracle.  A bounded report can therefore be a
useful negative result while the target implementation is still incomplete.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Literal, Sequence
import uuid
import zipfile

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style imports.
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-95-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-95"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
REPORT_NAMES = {
    "partSurfaceClosure": "part-surface-closure.json",
    "containmentReciprocity": "containment-reciprocity.json",
    "tableGridTopology": "table-grid-topology.json",
    "storyCoverage": "story-coverage.json",
}
FORMAT_NAMES = ("docx", "xlsx", "pdf", "markdown")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_ID = "issue-95-topology"
REQUIREMENT_ID = "QUAL-95-TOPOLOGY"
Issue95EvaluatorType = Literal["topology", "mutation-killed"]
TOPOLOGY_EVALUATOR: Issue95EvaluatorType = "topology"
MUTATION_EVALUATOR: Issue95EvaluatorType = "mutation-killed"
PRODUCER_ARTIFACT_REPORT_NAMES = (
    REPORT_NAMES["partSurfaceClosure"],
    REPORT_NAMES["containmentReciprocity"],
    REPORT_NAMES["tableGridTopology"],
    REPORT_NAMES["storyCoverage"],
)


class QualificationError(RuntimeError):
    """Raised when issue #95 qualification setup is not safe to run."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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
        raise QualificationError(f"value is not canonical JSON: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise QualificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _source_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise QualificationError(f"cannot execute git: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or SOURCE_SHA_RE.fullmatch(value) is None:
        raise QualificationError(f"cannot obtain a 40-character source SHA: {value!r}")
    return value


def _id_key(collection: str) -> str:
    return {
        "parts": "partId",
        "surfaces": "surfaceId",
        "nodes": "nodeId",
        "tables": "tableId",
        "orders": "orderId",
        "stories": "storyId",
    }.get(collection, "")


def _records(value: Any, collection: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    key = _id_key(collection)
    result: dict[str, dict[str, Any]] = {}
    for item in value.get(collection, []):
        if isinstance(item, dict) and key and isinstance(item.get(key), str):
            result[item[key]] = item
    return result


def _fixture_by_id(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["fixtureId"]): item for item in corpus["fixtures"]}


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    """Load and structurally validate the independent issue #95 corpus."""

    corpus = _read_json(Path(path))
    if not isinstance(corpus, dict):
        raise QualificationError("issue #95 corpus root must be an object")
    if corpus.get("schema") != "fdir/qualification-issue-95-corpus":
        raise QualificationError("issue #95 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 95:
        raise QualificationError("issue #95 corpus version or issue binding is invalid")
    if corpus.get("qualificationScope") != "bounded-independent-parts-stories-surfaces-containment-grid-topology":
        raise QualificationError("issue #95 corpus is not the bounded topology lane")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #95 corpus has no oracle declaration")
    for key in ("expectedTopologyIsAuthored", "expectedValuesAreRuntimeIndependent", "adapterHelpersUsedForExpected"):
        if oracle.get(key) is not (key != "adapterHelpersUsedForExpected"):
            raise QualificationError(f"issue #95 oracle flag {key!r} is unsafe")
    forbidden = oracle.get("forbiddenDerivations")
    if not isinstance(forbidden, list) or not forbidden or not all(isinstance(item, str) for item in forbidden):
        raise QualificationError("issue #95 oracle has no forbidden adapter derivations")

    formats = corpus.get("requiredFormats")
    if formats != list(FORMAT_NAMES):
        raise QualificationError("issue #95 required format list is incomplete or reordered")
    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise QualificationError("issue #95 corpus has no fixtures")
    fixture_ids: set[str] = set()
    seen_formats: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("issue #95 fixture is not an object")
        fixture_id = fixture.get("fixtureId")
        format_name = fixture.get("format")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate issue #95 fixture id: {fixture_id!r}")
        if format_name not in FORMAT_NAMES:
            raise QualificationError(f"unsupported issue #95 fixture format: {format_name!r}")
        source = fixture.get("source")
        expected = fixture.get("expected")
        if not isinstance(source, dict) or not isinstance(expected, dict):
            raise QualificationError(f"fixture {fixture_id} lacks source or expected topology")
        if source.get("type") not in {"zip-parts", "text", "pdf-text"}:
            raise QualificationError(f"fixture {fixture_id} has an unsupported source type")
        if source.get("type") == "zip-parts" and not isinstance(source.get("parts"), dict):
            raise QualificationError(f"fixture {fixture_id} has no authored package parts")
        if source.get("type") != "zip-parts" and not isinstance(source.get("text"), str):
            raise QualificationError(f"fixture {fixture_id} has no authored text source")
        for key in ("parts", "stories", "surfaces", "nodes", "tables", "orders", "occurrences"):
            if not isinstance(expected.get(key), list):
                raise QualificationError(f"fixture {fixture_id} expected.{key} must be a list")
        for collection in ("parts", "stories", "surfaces", "nodes", "tables", "orders"):
            ids: set[str] = set()
            key = _id_key(collection)
            for item in expected[collection]:
                if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
                    raise QualificationError(f"fixture {fixture_id} has an invalid expected {collection} record")
                if item[key] in ids:
                    raise QualificationError(f"fixture {fixture_id} duplicates expected {collection} id {item[key]}")
                ids.add(item[key])
        occurrence_ids: set[str] = set()
        for occurrence in expected["occurrences"]:
            if not isinstance(occurrence, dict) or not isinstance(occurrence.get("occurrenceId"), str):
                raise QualificationError(f"fixture {fixture_id} has an invalid occurrence")
            if occurrence["occurrenceId"] in occurrence_ids:
                raise QualificationError(f"fixture {fixture_id} duplicates an occurrence")
            occurrence_ids.add(occurrence["occurrenceId"])
        fixture_ids.add(fixture_id)
        seen_formats.add(str(format_name))
    missing_formats = sorted(set(FORMAT_NAMES) - seen_formats)
    if missing_formats:
        raise QualificationError(f"issue #95 corpus is missing formats: {missing_formats}")

    negatives = corpus.get("negativeCases")
    required_negative_names = corpus.get("requiredNegativeDefects")
    if not isinstance(negatives, list) or not negatives:
        raise QualificationError("issue #95 corpus has no negative cases")
    if not isinstance(required_negative_names, list) or not required_negative_names:
        raise QualificationError("issue #95 corpus has no required negative defect list")
    negative_ids: set[str] = set()
    negative_names: set[str] = set()
    for case in negatives:
        if not isinstance(case, dict):
            raise QualificationError("issue #95 negative case is not an object")
        case_id = case.get("id")
        fixture_id = case.get("fixtureId")
        mutation = case.get("mutation")
        expected_code = case.get("expectedDefectCode")
        if not isinstance(case_id, str) or not case_id or case_id in negative_ids:
            raise QualificationError(f"invalid or duplicate issue #95 negative id: {case_id!r}")
        if fixture_id not in fixture_ids or not isinstance(mutation, dict):
            raise QualificationError(f"negative case {case_id} references an invalid fixture or mutation")
        if not isinstance(case.get("defect"), str) or not isinstance(expected_code, str) or not expected_code:
            raise QualificationError(f"negative case {case_id} lacks defect authority")
        negative_ids.add(case_id)
        negative_names.add(str(case["defect"]))
    missing_negative_names = sorted(set(map(str, required_negative_names)) - negative_names)
    if missing_negative_names:
        raise QualificationError(f"issue #95 required negative coverage is missing: {missing_negative_names}")
    return corpus


def _resolve_parent_token(token: Any, expected: dict[str, Any]) -> str | None:
    if token is None:
        return None
    if token == "root":
        return str(expected.get("rootNodeId"))
    if not isinstance(token, str) or ":" not in token:
        return None
    prefix, value = token.split(":", 1)
    if prefix in {"node", "cell", "row", "column", "textbox"}:
        return value
    if prefix == "table":
        for table in expected.get("tables", []):
            if isinstance(table, dict) and table.get("tableId") == value:
                return str(table.get("nodeId"))
    if prefix == "story":
        for node in expected.get("nodes", []):
            if isinstance(node, dict) and node.get("nodeId") == f"node-docx-story-{value}":
                return str(node["nodeId"])
        for story in expected.get("stories", []):
            if isinstance(story, dict) and story.get("storyId") == value:
                for node in expected.get("nodes", []):
                    if isinstance(node, dict) and node.get("partId") == story.get("partId") and node.get("kind") == "story":
                        return str(node.get("nodeId"))
    return None


def _range_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, dict):
        try:
            return (
                int(value["rowStart"]),
                int(value["rowEnd"]),
                int(value["columnStart"]),
                int(value["columnEnd"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", value.upper())
    if match is None:
        return None
    def column_number(token: str) -> int:
        result = 0
        for character in token:
            result = result * 26 + ord(character) - ord("A") + 1
        return result
    return (int(match.group(2)), int(match.group(4)), column_number(match.group(1)), column_number(match.group(3)))


def _address_in_range(address: Any, bounds: tuple[int, int, int, int]) -> bool:
    if isinstance(address, str):
        match = re.fullmatch(r"([A-Z]+)(\d+)", address.upper())
        if match is None:
            return False
        column = 0
        for character in match.group(1):
            column = column * 26 + ord(character) - ord("A") + 1
        row = int(match.group(2))
    elif isinstance(address, dict):
        try:
            row = int(address["row"])
            column = int(address["column"])
        except (KeyError, TypeError, ValueError):
            return False
    else:
        return False
    row_start, row_end, column_start, column_end = bounds
    return row_start <= row <= row_end and column_start <= column <= column_end


def _manifest_graph_findings(fixture: dict[str, Any]) -> list[str]:
    """Validate the authored topology without looking at a target document."""

    expected = fixture["expected"]
    nodes = {item["nodeId"]: item for item in expected["nodes"]}
    tables = {item["tableId"]: item for item in expected["tables"]}
    findings: set[str] = set()

    parent_by_node: dict[str, str | None] = {}
    for node_id, node in nodes.items():
        multiple = node.get("parentIds", node.get("parents"))
        if isinstance(multiple, list) and len(multiple) != 1:
            findings.add("MULTI-PARENT")
        token = node.get("parent")
        if token is None:
            if node_id != expected.get("rootNodeId"):
                findings.add("ORPHAN")
            parent_by_node[node_id] = None
            continue
        parent_id = _resolve_parent_token(token, expected)
        if parent_id is None:
            findings.add("ORPHAN")
        elif parent_id not in nodes:
            findings.add("ORPHAN")
        elif parent_id == node_id:
            findings.add("CYCLE")
        parent_by_node[node_id] = parent_id

    for node_id, node in nodes.items():
        children = node.get("children")
        if not isinstance(children, list):
            continue
        if len(children) != len(set(children)):
            findings.add("RECIPROCITY-MISMATCH")
        for child_id in children:
            if child_id not in nodes:
                findings.add("ORPHAN")
                continue
            if parent_by_node.get(child_id) != node_id:
                findings.add("RECIPROCITY-MISMATCH")

    for node_id, parent_id in parent_by_node.items():
        if parent_id is None:
            continue
        parent_node = nodes.get(parent_id)
        if isinstance(parent_node, dict) and isinstance(parent_node.get("children"), list) and node_id not in parent_node["children"]:
            findings.add("RECIPROCITY-MISMATCH")

    for node_id in nodes:
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None:
            if current in seen:
                findings.add("CYCLE")
                break
            seen.add(current)
            current = parent_by_node.get(current)

    for table_id, table in tables.items():
        table_node = table.get("nodeId")
        if table_node not in nodes:
            findings.add("ORPHAN")
        row_ids = table.get("rowIds", [])
        column_ids = table.get("columnIds", [])
        cell_ids = table.get("cellIds", [])
        if len(row_ids) != len(set(row_ids)) or len(column_ids) != len(set(column_ids)) or len(cell_ids) != len(set(cell_ids)):
            findings.add("RECIPROCITY-MISMATCH")
        for row_id in row_ids:
            row = nodes.get(row_id)
            if row is not None and _resolve_parent_token(row.get("parent"), expected) != table_node:
                findings.add("CONTAINMENT-PARENT-MISMATCH")
        for column_id in column_ids:
            column = nodes.get(column_id)
            if column is not None and _resolve_parent_token(column.get("parent"), expected) != table_node:
                findings.add("CONTAINMENT-PARENT-MISMATCH")
        row_set = set(row_ids)
        for cell_id in cell_ids:
            cell = nodes.get(cell_id)
            if cell is not None:
                parent = _resolve_parent_token(cell.get("parent"), expected)
                if parent not in row_set:
                    findings.add("CONTAINMENT-PARENT-MISMATCH")
                bounds = _range_bounds(table.get("range"))
                if bounds is not None and not _address_in_range(cell.get("address"), bounds):
                    findings.add("TABLE-RANGE-MEMBER-MISMATCH")
        bounds = _range_bounds(table.get("range"))
        for address in table.get("memberAddresses", []):
            if bounds is not None and not _address_in_range(address, bounds):
                findings.add("TABLE-RANGE-MEMBER-MISMATCH")
        if table.get("scope") == "whole-sheet" and table.get("scope") != "sheet-grid":
            findings.add("XLSX-WHOLE-SHEET-TABLE")
        for merged in table.get("mergedRanges", []):
            if not isinstance(merged, dict):
                findings.add("MERGED-FOLLOWER-MASTER")
                continue
            master = nodes.get(str(merged.get("masterCellId")))
            if master is not None and master.get("mergeRole") not in {None, "master"}:
                findings.add("MERGED-FOLLOWER-MASTER")
            for follower_id in merged.get("followerCellIds", []):
                follower = nodes.get(str(follower_id))
                if follower is not None and follower.get("mergeRole") == "master":
                    findings.add("MERGED-FOLLOWER-MASTER")

    for story in expected.get("stories", []):
        if not isinstance(story, dict):
            continue
        root_parent = story.get("rootParent")
        for root_id in story.get("rootNodeIds", []):
            node = nodes.get(str(root_id))
            if node is None:
                findings.add("ORPHAN")
            elif node.get("parent") != root_parent:
                findings.add("STORY-ROOT-WRONG-PARENT")
        if story.get("type") not in {"document", "sheet"} and root_parent == "root":
            findings.add("STORY-ROOT-WRONG-PARENT")

    for table in expected.get("tables", []):
        if fixture.get("format") == "markdown":
            separators = set(table.get("separatorLines", []))
            if separators.intersection(set(table.get("rowSourceLines", []))):
                findings.add("MARKDOWN-SEPARATOR-DATA")

    occurrence_ids = {
        item.get("occurrenceId")
        for item in expected.get("occurrences", [])
        if isinstance(item, dict)
    }
    for occurrence in expected.get("occurrences", []):
        if isinstance(occurrence, dict) and occurrence.get("requiredStyle") and occurrence.get("occurrenceId") not in occurrence_ids:
            findings.add("XLSX-EMPTY-STYLED-OMITTED")
    required_occurrences = expected.get("requiredOccurrenceIds", [])
    if not isinstance(required_occurrences, list):
        required_occurrences = []
    required_occurrences.extend(
        item.get("occurrenceId")
        for item in expected.get("occurrences", [])
        if isinstance(item, dict) and item.get("requiredStyle")
    )
    for required_id in required_occurrences:
        if required_id not in occurrence_ids:
            findings.add("XLSX-EMPTY-STYLED-OMITTED")

    if fixture.get("format") == "pdf":
        actual_order = [
            item.get("sourceObject")
            for item in sorted(expected.get("surfaces", []), key=lambda value: value.get("ordinal", 0))
            if isinstance(item, dict) and item.get("sourceObject") is not None
        ]
        if expected.get("pageTreeOrder") != actual_order:
            findings.add("PDF-PAGE-TREE-ORDER")

    for part in expected.get("parts", []):
        if isinstance(part, dict) and part.get("statusClass") == "unparsed" and part.get("status") == "preserved":
            findings.add("UNPARSED-PART-PRESERVED")
    return sorted(findings)


def _apply_mutation(fixture: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(fixture)
    expected = candidate["expected"]
    kind = mutation.get("kind")
    if kind == "set-node-parent":
        node = next((item for item in expected["nodes"] if item.get("nodeId") == mutation.get("nodeId")), None)
        if node is None:
            raise QualificationError(f"mutation node is missing: {mutation}")
        node["parent"] = mutation.get("parent")
    elif kind == "set-node-field":
        node = next((item for item in expected["nodes"] if item.get("nodeId") == mutation.get("nodeId")), None)
        if node is None:
            raise QualificationError(f"mutation node is missing: {mutation}")
        node[str(mutation["field"])] = deepcopy(mutation.get("value"))
    elif kind == "append-table-cell":
        table = next((item for item in expected["tables"] if item.get("tableId") == mutation.get("tableId")), None)
        if table is None:
            raise QualificationError(f"mutation table is missing: {mutation}")
        table.setdefault("cellIds", []).append(str(mutation["cellId"]))
        table.setdefault("memberAddresses", []).append(str(mutation["address"]))
    elif kind == "append-separator-row":
        table = next((item for item in expected["tables"] if item.get("tableId") == mutation.get("tableId")), None)
        if table is None:
            raise QualificationError(f"mutation table is missing: {mutation}")
        table.setdefault("rowIds", []).append(str(mutation["rowId"]))
        table.setdefault("rowSourceLines", []).append(int(mutation["sourceLine"]))
    elif kind == "set-table-scope":
        table = next((item for item in expected["tables"] if item.get("tableId") == mutation.get("tableId")), None)
        if table is None:
            raise QualificationError(f"mutation table is missing: {mutation}")
        table["scope"] = str(mutation["scope"])
    elif kind == "drop-occurrence":
        expected["occurrences"] = [
            item for item in expected["occurrences"]
            if item.get("occurrenceId") != mutation.get("occurrenceId")
        ]
    elif kind == "set-page-order":
        expected["pageTreeOrder"] = list(mutation.get("order", []))
    elif kind == "set-part-status":
        part = next((item for item in expected["parts"] if item.get("partId") == mutation.get("partId")), None)
        if part is None:
            raise QualificationError(f"mutation part is missing: {mutation}")
        part["status"] = str(mutation["status"])
    elif kind == "drop-child":
        node = next((item for item in expected["nodes"] if item.get("nodeId") == mutation.get("nodeId")), None)
        if node is None:
            raise QualificationError(f"mutation node is missing: {mutation}")
        node["children"] = [child for child in node.get("children", []) if child != mutation.get("childId")]
    elif kind == "set-node-parents":
        node = next((item for item in expected["nodes"] if item.get("nodeId") == mutation.get("nodeId")), None)
        if node is None:
            raise QualificationError(f"mutation node is missing: {mutation}")
        node["parentIds"] = list(mutation.get("parents", []))
    else:
        raise QualificationError(f"unsupported issue #95 mutation kind: {kind!r}")
    return candidate


def run_oracle_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute all negative mutations against authored topology only."""

    fixtures = _fixture_by_id(corpus)
    results: list[dict[str, Any]] = []
    for case in corpus["negativeCases"]:
        fixture = fixtures[str(case["fixtureId"])]
        candidate = _apply_mutation(fixture, case["mutation"])
        defects = _manifest_graph_findings(candidate)
        expected_code = str(case["expectedDefectCode"])
        detected = expected_code in defects
        results.append({
            "caseId": case["id"],
            "fixtureId": case["fixtureId"],
            "defect": case["defect"],
            "expectedDefectCode": expected_code,
            "detectedDefectCodes": defects,
            "detected": detected,
            "oracleExpected": deepcopy(fixture["expected"]),
            "oracleActual": deepcopy(candidate["expected"]),
            "status": "passed" if detected else "failed",
        })
    return results


def _write_source(fixture: dict[str, Any], directory: Path) -> Path:
    source = fixture["source"]
    suffix = {"docx": ".docx", "xlsx": ".xlsx", "pdf": ".pdf", "markdown": ".md"}[fixture["format"]]
    path = directory / f"{fixture['fixtureId']}{suffix}"
    source_type = source["type"]
    if source_type == "zip-parts":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in sorted(source["parts"]):
                if not isinstance(name, str) or not name or name.startswith("/") or ".." in Path(name).parts:
                    raise QualificationError(f"unsafe authored package part: {name!r}")
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.flag_bits = 0x800
                archive.writestr(info, str(source["parts"][name]).encode("utf-8"))
    elif source_type == "pdf-text":
        path.write_bytes(str(source["text"]).encode("latin-1"))
    else:
        path.write_text(str(source["text"]), encoding="utf-8", newline="\n")
    return path


def _run_converter(fixture: dict[str, Any], directory: Path) -> dict[str, Any]:
    try:
        source_path = _write_source(fixture, directory)
    except (OSError, QualificationError) as exc:
        return {
            "returnCode": None,
            "error": f"fixture materialization failed: {type(exc).__name__}: {exc}",
            "document": None,
        }
    output_path = directory / f"{fixture['fixtureId']}.ir.json"
    evidence_path = directory / f"{fixture['fixtureId']}.evidence.json"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(CONVERTER_PATH),
                "convert",
                str(source_path),
                "--format",
                str(fixture["format"]),
                *(["--profile", str(fixture["dialect"])] if fixture["format"] == "markdown" and isinstance(fixture.get("dialect"), str) else []),
                "--out",
                str(output_path),
                "--evidence",
                str(evidence_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returnCode": None, "error": f"converter execution failed: {type(exc).__name__}: {exc}", "document": None}
    document: dict[str, Any] | None = None
    if output_path.is_file():
        try:
            value = _read_json(output_path)
            if isinstance(value, dict):
                document = value
        except QualificationError as exc:
            return {"returnCode": result.returncode, "error": str(exc), "document": None}
    return {
        "returnCode": result.returncode,
        "stderr": result.stderr[-1200:],
        "document": document,
        "evidencePresent": evidence_path.is_file(),
    }


def _create_workspace_workdir() -> Path:
    """Create a unique workdir below the repository, avoiding OS temp ACLs."""

    candidates = [ROOT / "e2e" / ".run", ROOT]
    last_error: OSError | None = None
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            for _ in range(8):
                candidate = base / f"qualification-issue95-run-{os.getpid()}-{uuid.uuid4().hex[:12]}"
                try:
                    candidate.mkdir()
                    return candidate
                except FileExistsError:
                    continue
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise QualificationError(f"cannot create workspace qualification directory: {last_error}")
    raise QualificationError("cannot create workspace qualification directory")


def _cleanup_workspace_workdir(path: Path) -> str | None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _actual_story_projection(document: dict[str, Any], expected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parts = _records(document, "parts")
    surfaces = _records(document, "surfaces")
    nodes = _records(document, "nodes")
    result: dict[str, dict[str, Any]] = {}
    for story in expected.get("stories", []):
        story_id = str(story["storyId"])
        part = parts.get(str(story.get("partId")))
        surface = surfaces.get(str(story.get("surfaceId")))
        roots = list(part.get("rootNodeIds", [])) if part is not None else []
        root_parents = [nodes.get(str(root), {}).get("parentId") for root in roots]
        result[story_id] = {
            "storyId": story_id,
            "type": part.get("storyType") if part else None,
            "partId": part.get("partId") if part else None,
            # A document root part owns itself semantically but has no
            # parentPartId edge.  Keep the projection explicit so the
            # closure check does not mistake a valid root for an orphan.
            "ownerPartId": (
                part.get("parentPartId")
                if part and part.get("parentPartId") is not None
                else (part.get("partId") if part else None)
            ),
            "surfaceId": surface.get("surfaceId") if surface else None,
            "rootNodeIds": roots,
            "rootParentIds": root_parents,
        }
    return result


def _actual_surface_context(document: dict[str, Any], surface_id: str) -> dict[str, Any]:
    surfaces = _records(document, "surfaces")
    surface = surfaces.get(surface_id, {})
    context = dict(surface)
    nodes = _records(document, "nodes")
    maps = [item for item in document.get("sourceMaps", []) if isinstance(item, dict)]
    page_number_match = re.search(r"-page-(\d+)$", surface_id)
    if page_number_match:
        page_id = f"node-pdf-page-{page_number_match.group(1)}"
        for source_map in maps:
            if source_map.get("targetId") == page_id:
                locator = source_map.get("locator", {})
                if isinstance(locator, dict) and locator.get("object") is not None:
                    context["sourceObject"] = f"{locator.get('object')} 0"
                break
        if page_id in nodes:
            context["pageNodeId"] = page_id
    return context


def _append_mismatch(mismatches: list[dict[str, Any]], code: str, path: str, expected: Any, actual: Any) -> None:
    mismatches.append({
        "code": code,
        "path": path,
        "expected": expected,
        "actual": actual,
    })


def _compare_record(
    mismatches: list[dict[str, Any]],
    expected: dict[str, Any],
    actual: dict[str, Any] | None,
    path: str,
    *,
    expected_node_ids: set[str] | None = None,
    expected_context: dict[str, Any] | None = None,
) -> None:
    if actual is None:
        _append_mismatch(mismatches, "MISSING-ENTITY", path, expected, None)
        return
    expected_context = expected_context or {}
    for key, expected_value in expected.items():
        if key in {"acceptedStatuses", "statusClass", "parent", "children", "sourceLine", "storyType", "ownerPartId", "rootParent"}:
            continue
        actual_value = actual.get(key, "__missing__")
        if actual_value == "__missing__":
            _append_mismatch(mismatches, "MISSING-FIELD", f"{path}/{key}", expected_value, None)
        elif actual_value != expected_value:
            _append_mismatch(mismatches, "VALUE-MISMATCH", f"{path}/{key}", expected_value, actual_value)
    if "acceptedStatuses" in expected:
        if actual.get("status") not in expected["acceptedStatuses"]:
            code = "UNPARSED-PART-PRESERVED" if expected.get("statusClass") == "unparsed" and actual.get("status") == "preserved" else "STATUS-MISMATCH"
            _append_mismatch(mismatches, code, f"{path}/status", expected["acceptedStatuses"], actual.get("status"))
    elif "statusClass" not in expected and "status" in expected and actual.get("status") != expected["status"]:
        _append_mismatch(mismatches, "STATUS-MISMATCH", f"{path}/status", expected["status"], actual.get("status"))

    if "parent" in expected:
        actual_parent = actual.get("parentId")
        wanted_parent = _resolve_parent_token(expected.get("parent"), expected_context)
        if expected.get("parent") is None:
            wanted_parent = None
        if actual_parent != wanted_parent:
            _append_mismatch(mismatches, "CONTAINMENT-PARENT-MISMATCH", f"{path}/parent", expected.get("parent"), actual_parent)
    if isinstance(expected.get("children"), list):
        actual_children = actual.get("childIds", [])
        if expected_node_ids is not None:
            actual_children = [item for item in actual_children if item in expected_node_ids]
        if actual_children != expected["children"]:
            _append_mismatch(mismatches, "RECIPROCITY-MISMATCH", f"{path}/children", expected["children"], actual_children)


def _compare_fixture(fixture: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    expected = fixture["expected"]
    document = run.get("document")
    mismatches: list[dict[str, Any]] = []
    target_defects: list[str] = []
    if not isinstance(document, dict):
        _append_mismatch(mismatches, "TARGET-OUTPUT-MISSING", "$", "IR object", run.get("error", "no document"))
        return {
            "caseId": fixture["fixtureId"],
            "format": fixture["format"],
            "targetReturnCode": run.get("returnCode"),
            "targetConversionStatus": None,
            "mismatches": mismatches,
            "targetContainmentDefects": target_defects,
            "status": "failed",
        }
    target_status = document.get("conversion", {}).get("status")
    actual_maps = {collection: _records(document, collection) for collection in ("parts", "surfaces", "nodes", "tables", "orders")}
    expected_ids = {item[key] for collection in actual_maps for item in expected.get(collection, []) if isinstance(item, dict) for key in [_id_key(collection)] if isinstance(item.get(key), str)}

    for item in expected["parts"]:
        _compare_record(mismatches, item, actual_maps["parts"].get(item["partId"]), f"parts/{item['partId']}", expected_context=expected)
    for item in expected["surfaces"]:
        actual = _actual_surface_context(document, item["surfaceId"])
        if item["surfaceId"] not in actual_maps["surfaces"]:
            actual = None
        _compare_record(mismatches, item, actual, f"surfaces/{item['surfaceId']}", expected_context=expected)
    for item in expected["nodes"]:
        _compare_record(mismatches, item, actual_maps["nodes"].get(item["nodeId"]), f"nodes/{item['nodeId']}", expected_node_ids=set(expected_ids), expected_context=expected)
    for item in expected["tables"]:
        actual = actual_maps["tables"].get(item["tableId"])
        _compare_record(mismatches, item, actual, f"tables/{item['tableId']}", expected_context=expected)
        if actual is not None:
            if item.get("scope") == "structured-table":
                expected_members = set(item.get("cellIds", []))
                actual_members = set(actual.get("cellIds", []))
                if not actual_members.issubset(expected_members) or actual_members != expected_members:
                    _append_mismatch(mismatches, "XLSX-WHOLE-SHEET-TABLE", f"tables/{item['tableId']}/cellIds", sorted(expected_members), sorted(actual_members))
            if fixture["format"] == "markdown" and item.get("separatorIsMetadata"):
                actual_rows = actual.get("rowIds", [])
                if any("separator" in str(row).casefold() for row in actual_rows):
                    _append_mismatch(mismatches, "MARKDOWN-SEPARATOR-DATA", f"tables/{item['tableId']}/rowIds", item.get("rowIds"), actual_rows)
            for member in item.get("memberTopology", []):
                member_id = member.get("nodeId")
                actual_node = actual_maps["nodes"].get(str(member_id))
                if actual_node is None:
                    _append_mismatch(mismatches, "MISSING-CELL-MEMBER", f"tables/{item['tableId']}/memberTopology/{member_id}", member, None)
                    continue
                if actual_node.get("address") != member.get("address"):
                    _append_mismatch(mismatches, "CELL-ADDRESS-MISMATCH", f"tables/{item['tableId']}/memberTopology/{member_id}/address", member.get("address"), actual_node.get("address"))
                expected_parent = _resolve_parent_token(member.get("parent"), expected)
                if actual_node.get("parentId") != expected_parent:
                    _append_mismatch(mismatches, "CONTAINMENT-PARENT-MISMATCH", f"tables/{item['tableId']}/memberTopology/{member_id}/parent", member.get("parent"), actual_node.get("parentId"))
                if isinstance(member.get("order"), int) and member_id in actual.get("cellIds", []):
                    actual_order = actual["cellIds"].index(member_id)
                    if actual_order != member["order"]:
                        _append_mismatch(mismatches, "ORDER-MISMATCH", f"tables/{item['tableId']}/memberTopology/{member_id}/order", member.get("order"), actual_order)
    expected_table_ids = {item["tableId"] for item in expected["tables"]}
    for table_id in sorted(set(actual_maps["tables"]) - expected_table_ids):
        _append_mismatch(mismatches, "UNEXPECTED-TABLE", f"tables/{table_id}", None, actual_maps["tables"][table_id])

    for item in expected["orders"]:
        actual = actual_maps["orders"].get(item["orderId"])
        if actual is None:
            _append_mismatch(mismatches, "MISSING-ORDER", f"orders/{item['orderId']}", item, None)
            continue
        if actual.get("kind") != item.get("kind") or actual.get("ownerId") != item.get("ownerId"):
            _append_mismatch(mismatches, "ORDER-OWNER-MISMATCH", f"orders/{item['orderId']}", {"kind": item.get("kind"), "ownerId": item.get("ownerId")}, {"kind": actual.get("kind"), "ownerId": actual.get("ownerId")})
        actual_items = [entry.get("id") for entry in actual.get("items", []) if isinstance(entry, dict)]
        expected_items = list(item.get("items", []))
        scoped_actual = [entry for entry in actual_items if entry in set(expected_items)]
        if scoped_actual != expected_items:
            _append_mismatch(mismatches, "ORDER-MISMATCH", f"orders/{item['orderId']}/items", expected_items, scoped_actual)

    actual_stories = _actual_story_projection(document, expected)
    for story in expected["stories"]:
        actual = actual_stories.get(story["storyId"])
        if actual is None:
            _append_mismatch(mismatches, "MISSING-STORY", f"stories/{story['storyId']}", story, None)
            continue
        for key in ("type", "partId", "ownerPartId", "surfaceId", "rootNodeIds"):
            if actual.get(key) != story.get(key):
                _append_mismatch(mismatches, "STORY-CLOSURE-MISMATCH", f"stories/{story['storyId']}/{key}", story.get(key), actual.get(key))
        expected_parent = story.get("rootParent")
        if expected_parent is not None:
            owner_id = _resolve_parent_token(expected_parent, expected)
            if actual.get("rootParentIds") != [owner_id for _ in actual.get("rootNodeIds", [])]:
                _append_mismatch(mismatches, "STORY-ROOT-WRONG-PARENT", f"stories/{story['storyId']}/rootParent", expected_parent, actual.get("rootParentIds"))

    actual_occurrences = 0
    for occurrence in expected["occurrences"]:
        target = occurrence.get("target", {})
        collection = target.get("collection")
        target_id = target.get("id")
        found: Any = None
        if collection in actual_maps:
            found = actual_maps[collection].get(target_id)
        elif collection == "stories":
            found = actual_stories.get(str(target_id))
        if target.get("kind"):
            found = next((node for node in actual_maps["nodes"].values() if node.get("kind") == target.get("kind")), None)
        if found is None:
            _append_mismatch(mismatches, "OCCURRENCE-UNACCOUNTED", f"occurrences/{occurrence['occurrenceId']}", target, None)
            continue
        actual_occurrences += 1
        if occurrence.get("disposition") == "unparsed" and found.get("status") == "preserved":
            _append_mismatch(mismatches, "UNPARSED-PART-PRESERVED", f"occurrences/{occurrence['occurrenceId']}", "opaque-or-unsupported", found.get("status"))
        if occurrence.get("mustNotBecome") == "table" and actual_maps["tables"]:
            _append_mismatch(mismatches, "MARKDOWN-DIALECT-TABLE", f"occurrences/{occurrence['occurrenceId']}", "no-table", sorted(actual_maps["tables"]))
        if occurrence.get("mustNotBecome") == "data-row" and actual_maps["tables"]:
            table = next(iter(actual_maps["tables"].values()))
            if len(table.get("rowIds", [])) > 3:
                _append_mismatch(mismatches, "MARKDOWN-SEPARATOR-DATA", f"occurrences/{occurrence['occurrenceId']}", "separator-metadata", table.get("rowIds"))

    target_defects = _actual_containment_findings(document)
    return {
        "caseId": fixture["fixtureId"],
        "format": fixture["format"],
        "targetReturnCode": run.get("returnCode"),
        "targetConversionStatus": target_status,
        "targetEvidencePresent": bool(run.get("evidencePresent")),
        "expectedOccurrenceCount": len(expected["occurrences"]),
        "accountedOccurrenceCount": actual_occurrences,
        "targetContainmentDefects": target_defects,
        "mismatchCount": len(mismatches),
        "mismatches": mismatches,
        "status": "passed" if not mismatches and not target_defects else "failed",
    }


def _actual_containment_findings(document: dict[str, Any]) -> list[str]:
    nodes = _records(document, "nodes")
    findings: set[str] = set()
    reverse_children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        parent_id = node.get("parentId")
        if parent_id is not None and parent_id not in nodes:
            findings.add("ORPHAN")
        if isinstance(node.get("parentIds"), list) and len(node["parentIds"]) > 1:
            findings.add("MULTI-PARENT")
        for child_id in node.get("childIds", []):
            reverse_children.setdefault(str(child_id), []).append(node_id)
            if child_id not in nodes or nodes[child_id].get("parentId") != node_id:
                findings.add("RECIPROCITY-MISMATCH")
    for child_id, parents in reverse_children.items():
        if len(parents) > 1:
            findings.add("MULTI-PARENT")
        if child_id not in nodes:
            findings.add("ORPHAN")
    for node_id in nodes:
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current in nodes:
            if current in seen:
                findings.add("CYCLE")
                break
            seen.add(current)
            current = nodes[current].get("parentId")
    return sorted(findings)


def _assertion(assertion_id: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "assertionId": assertion_id,
        "expected": expected,
        "actual": actual,
        "status": "passed" if actual == expected else "failed",
    }


def _producer_input_paths(corpus_path: Path) -> list[Path]:
    return [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue95.py",
        ROOT / "tools" / "test_qualification_issue95.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_docx.py",
        ROOT / "tools" / "adapter_xlsx.py",
        ROOT / "tools" / "adapter_pdf.py",
        ROOT / "tools" / "adapter_markdown.py",
    ]


def _producer_rows(
    corpus: dict[str, Any] | None,
    case_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    setup_error: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fixtures = {
        str(item.get("fixtureId")): item
        for item in (corpus or {}).get("fixtures", [])
        if isinstance(item, dict) and isinstance(item.get("fixtureId"), str)
    }
    for result in case_results:
        case_id = str(result.get("caseId", ""))
        fixture = fixtures.get(case_id)
        if not case_id or fixture is None:
            continue
        expected_count = len(fixture.get("expected", {}).get("occurrences", []))
        expected = {
            "fixtureId": case_id,
            "format": fixture.get("format"),
            "status": "passed",
            "expectedOccurrenceCount": expected_count,
            "accountedOccurrenceCount": expected_count,
            "mismatchCount": 0,
            "targetContainmentDefects": [],
        }
        actual = {
            "fixtureId": case_id,
            "format": result.get("format"),
            "status": result.get("status"),
            "expectedOccurrenceCount": result.get("expectedOccurrenceCount", expected_count),
            "accountedOccurrenceCount": result.get("accountedOccurrenceCount", 0),
            "mismatchCount": result.get("mismatchCount", 0),
            "targetContainmentDefects": result.get("targetContainmentDefects", []),
        }
        rows.append({
            "caseId": f"positive-{case_id}",
            "classification": "positive",
            "evaluatorType": TOPOLOGY_EVALUATOR,
            "input": {"fixtureId": case_id, "format": fixture.get("format")},
            "expected": expected,
            "actual": actual,
            "result": "passed" if expected == actual else "failed",
            "target": {"fixtureId": case_id, "format": fixture.get("format"), "dimension": "topology"},
            "diagnostic": {"code": "ISSUE-95-TOPOLOGY", "message": "authored topology is compared with public-converter topology"},
            "oracleEvidence": {"identity": "authored-independent-topology", "expectedValuesAreRuntimeIndependent": True},
        })

    for result in negative_results:
        case_id = str(result.get("caseId", ""))
        if not case_id:
            continue
        expected_value = {
            "fixtureId": result.get("fixtureId"),
            "topology": deepcopy(result.get("oracleExpected")),
        }
        mutated_value = {
            "fixtureId": result.get("fixtureId"),
            "topology": deepcopy(result.get("oracleActual")),
        }
        detected = result.get("detected") is True
        rows.append({
            "caseId": f"mutation-{case_id}",
            "classification": "mutation",
            "evaluatorType": MUTATION_EVALUATOR,
            "input": {"fixtureId": result.get("fixtureId"), "mutationCaseId": case_id},
            "expected": expected_value,
            # If the oracle did not detect the mutation, keep the producer
            # comparison equal so the typed mutation evaluator also fails.
            "actual": mutated_value if detected else expected_value,
            "result": "passed" if detected else "failed",
            "target": {
                "mutationCaseId": case_id,
                "expectedDefectCode": result.get("expectedDefectCode"),
                "oracleMutationDetected": detected,
            },
            "diagnostic": {"code": "ISSUE-95-MUTATION", "message": "authored topology mutation must be detected by the independent oracle"},
            "oracleEvidence": {
                "detectedDefectCodes": result.get("detectedDefectCodes", []),
                "oracleExpected": deepcopy(result.get("oracleExpected")),
                "oracleActual": deepcopy(result.get("oracleActual")),
                "detected": detected,
            },
        })

    if setup_error or not rows:
        message = setup_error or "no issue #95 producer cases were generated"
        rows = [
            {
                "caseId": "setup-positive",
                "classification": "positive",
                "evaluatorType": TOPOLOGY_EVALUATOR,
                "input": {"setup": "issue-95"},
                "expected": {"setup": "available"},
                "actual": {"setup": "unavailable", "error": message},
                "result": "failed",
                "target": {"phase": "qualification-setup"},
                "diagnostic": {"code": "ISSUE-95-SETUP", "message": message},
                "oracleEvidence": {"setupError": message},
            },
            {
                "caseId": "setup-mutation",
                "classification": "mutation",
                "evaluatorType": MUTATION_EVALUATOR,
                "input": {"setup": "issue-95"},
                "expected": {"mutationDetected": True},
                "actual": {"mutationDetected": True},
                "result": "failed",
                "target": {"phase": "qualification-setup", "oracleMutationDetected": False},
                "diagnostic": {"code": "ISSUE-95-SETUP", "message": message},
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
        issue_number=95,
        evidence_id=EVIDENCE_ID,
        requirement_id=REQUIREMENT_ID,
        source_sha=source_sha,
        input_paths=_producer_input_paths(corpus_path),
        producer_id="issue-95-topology-runner",
        authority_id="issue-95-authored-topology-oracle",
        producer_component_path=Path(__file__),
        authority_component_path=Path(corpus_path),
        evaluator_component_path=ROOT / "tools" / "validate_qualification_contract.py",
        shared_component_paths=(ROOT / "tools" / "qualification_evidence.py",),
        rows=rows,
    )


def _fatal_report(kind: str, source_sha: str | None, message: str) -> dict[str, Any]:
    return {
        "schema": "fdir/qualification-issue-95-report",
        "version": "1.0.0",
        "issueNumber": 95,
        "reportKind": kind,
        "sourceSha": source_sha,
        "status": "failed",
        "caseCounts": {"total": 0, "positive": 0, "negative": 0, "targetPassed": 0, "targetFailed": 0, "negativeDetected": 0, "negativeUndetected": 0},
        "caseCount": 0,
        "positiveCaseCount": 0,
        "negativeCaseCount": 0,
        "assertions": [_assertion("qualification-setup", "executable", "unavailable")],
        "negativeDefectResults": [],
        "cases": [],
        "failure": {"code": "QUALIFICATION-SETUP-FAILED", "message": message},
        "limitations": ["No topology result is valid when setup fails."],
    }


def _build_report(
    kind: str,
    corpus: dict[str, Any],
    source_sha: str,
    corpus_sha: str,
    case_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    authored_defects: list[str],
) -> dict[str, Any]:
    target_failed = sum(1 for item in case_results if item.get("status") != "passed")
    target_passed = len(case_results) - target_failed
    negative_undetected = sum(1 for item in negative_results if not item.get("detected"))
    format_counts: dict[str, dict[str, int]] = {}
    for item in case_results:
        entry = format_counts.setdefault(item["format"], {"total": 0, "passed": 0, "failed": 0})
        entry["total"] += 1
        entry["passed" if item.get("status") == "passed" else "failed"] += 1
    assertions = [
        _assertion("source-sha-is-commit-bound", True, bool(SOURCE_SHA_RE.fullmatch(source_sha))),
        _assertion("authored-positive-oracle-has-no-defects", 0, len(authored_defects)),
        _assertion("all-qualified-fixtures-match-exact-topology", 0, target_failed),
        _assertion("all-negative-mutations-detected", 0, negative_undetected),
        _assertion("target-oracle-occurrences-accounted", 0, sum(int(item.get("expectedOccurrenceCount", 0)) - int(item.get("accountedOccurrenceCount", 0)) for item in case_results)),
        _assertion("target-orphan-cycle-multi-parent-reciprocity", 0, sum(len(item.get("targetContainmentDefects", [])) for item in case_results)),
        _assertion("required-format-coverage", sorted(FORMAT_NAMES), sorted(format_counts)),
    ]
    status = "passed" if all(item["status"] == "passed" for item in assertions) else "failed"
    return {
        "schema": "fdir/qualification-issue-95-report",
        "version": "1.0.0",
        "issueNumber": 95,
        "reportKind": kind,
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "bounded": True,
        "oracle": {
            "identity": corpus["oracle"]["identity"],
            "expectedTopologyIsAuthored": corpus["oracle"]["expectedTopologyIsAuthored"],
            "adapterHelpersUsedForExpected": corpus["oracle"]["adapterHelpersUsedForExpected"],
            "forbiddenDerivations": corpus["oracle"]["forbiddenDerivations"],
        },
        "caseCounts": {"total": len(case_results) + len(negative_results), "positive": len(case_results), "negative": len(negative_results), "targetPassed": target_passed, "targetFailed": target_failed, "negativeDetected": len(negative_results) - negative_undetected, "negativeUndetected": negative_undetected},
        "caseCount": len(case_results) + len(negative_results),
        "positiveCaseCount": len(case_results),
        "negativeCaseCount": len(negative_results),
        "formatCaseCounts": format_counts,
        "assertions": assertions,
        "negativeDefectResults": negative_results,
        "cases": case_results,
        "status": status,
        "limitations": [
            "This is a finite, bounded corpus; it is not a proof of every package construct.",
            "PDF table inference is intentionally not qualified as a source-declared table fact.",
            "A failed target result is retained as evidence and never converted to pass by coverage counts.",
        ],
    }


def run_qualification(
    *,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> int:
    """Run all four reports and return 1 whenever the lane is not complete."""

    source_sha: str | None = None
    corpus: dict[str, Any] | None = None
    case_results: list[dict[str, Any]] = []
    negative_results: list[dict[str, Any]] = []
    try:
        source_sha = _source_sha()
        corpus = load_corpus(corpus_path)
        authored_defects = sorted({defect for fixture in corpus["fixtures"] for defect in _manifest_graph_findings(fixture)})
        if authored_defects:
            raise QualificationError("authored positive topology is invalid: " + ", ".join(authored_defects))
        negative_results = run_oracle_mutations(corpus)
        corpus_sha = _sha256_file(Path(corpus_path))
        work = _create_workspace_workdir()
        cleanup_error: str | None = None
        try:
            for fixture in corpus["fixtures"]:
                run = _run_converter(fixture, work)
                case_results.append(_compare_fixture(fixture, run))
        finally:
            cleanup_error = _cleanup_workspace_workdir(work)
        if cleanup_error and case_results:
            case_results[0]["mismatches"].append({
                "code": "WORKDIR-CLEANUP-FAILED",
                "path": "runtime/workdir",
                "expected": "cleanup succeeds",
                "actual": cleanup_error,
            })
            case_results[0]["mismatchCount"] = len(case_results[0]["mismatches"])
            case_results[0]["status"] = "failed"
        reports = {
            kind: _build_report(kind, corpus, source_sha, corpus_sha, case_results, negative_results, authored_defects)
            for kind in REPORT_NAMES
        }
        producer_report = _write_producer_report(
            Path(out_dir),
            reports,
            Path(corpus_path),
            source_sha,
            _producer_rows(corpus, case_results, negative_results),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        reports = {kind: _fatal_report(kind, source_sha, message) for kind in REPORT_NAMES}
        _write_producer_report(
            Path(out_dir),
            reports,
            Path(corpus_path),
            source_sha,
            _producer_rows(corpus, case_results, negative_results, setup_error=message),
        )
        print(f"FAIL: issue #95 qualification setup: {message}", file=sys.stderr)
        return 1

    failed = [kind for kind, report in reports.items() if report.get("status") != "passed"]
    if producer_report.get("status") != "passed":
        failed.append("producer-report")
    if failed:
        print("FAIL: issue #95 qualification reports: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("PASS: issue #95 qualification reports written: " + ", ".join(REPORT_NAMES.values()))
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
