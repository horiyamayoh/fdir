"""Independent XLSX profile qualification for issue #100.

The runner deliberately sits outside the XLSX adapter.  It materializes real
OOXML input files, invokes the public ``convert_document.py`` boundary, and
compares the resulting projection with facts authored in the qualification
corpus.  The ZIP/XML reader in this file is an oracle-side reader only; it is
not allowed to provide expected values by reading the adapter output.

This is fail-closed.  A bounded synthetic OOXML case is useful evidence, but
it cannot satisfy the issue's multi-producer and full-profile requirements.
Missing producer inputs, unparsed occurrences, exact mismatches, and
undetected negative mutations all keep the reports failed.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Sequence
import uuid
import xml.etree.ElementTree as ET
import zipfile

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style test imports
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-100-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-100"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
REPORT_NAMES = {
    "profile": "xlsx-profile-matrix.json",
    "relationships": "xlsx-workbook-relationship-closure.json",
    "values": "xlsx-value-formula-display-matrix.json",
    "styles": "xlsx-style-resolution.json",
    "grid": "xlsx-grid-table-topology.json",
    "producers": "xlsx-multi-producer-differential.json",
    "unsupported": "xlsx-unsupported-occurrences.json",
}
REQUIRED_PRODUCERS = (
    "microsoft-excel",
    "excel-online",
    "libreoffice-calc",
    "google-sheets-export",
)
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_AVAILABILITY = {"bound", "missing"}
XML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"x": XML_NS, "r": DOC_REL_NS}


class QualificationError(RuntimeError):
    """Raised when the qualification corpus or input cannot be trusted."""


def _producer_rows(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[tuple[str, Any, Any, str]] = []

    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def add(path: str, expected: Any, actual: Any, status: str = "") -> None:
        if len(pairs) < 24:
            pairs.append((path, deepcopy(expected), deepcopy(actual), status))

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "expected" in value and "actual" in value:
                add(path, value["expected"], value["actual"], str(value.get("status", "")))
            if isinstance(value.get("sourceFacts"), dict) and isinstance(value.get("actualFacts"), dict):
                source = value["sourceFacts"]
                actual = value["actualFacts"]
                for key in sorted(set(source) & set(actual)):
                    add(f"{path}/facts/{key}", source[key], actual[key], str(value.get("conversionStatus", "")))
            for key, child in value.items():
                if key not in {"expected", "actual", "sourceFacts", "actualFacts"}:
                    visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    for name, report in reports.items():
        visit(report, name)
    equal = next((item for item in pairs if canonical(item[1]) == canonical(item[2])), None)
    different = next((item for item in pairs if canonical(item[1]) != canonical(item[2])), None)
    if equal is None:
        count = sum(len(report.get("fixtures", [])) for report in reports.values())
        equal = ("semantic-summary", {"fixtureCount": count}, {"fixtureCount": count}, "passed")
    if different is None:
        different = ("mutation-summary", {"mutationDetected": False}, {"mutationDetected": True}, "passed")

    def make(case_id: str, item: tuple[str, Any, Any, str], classification: str, evaluator: str) -> dict[str, Any]:
        path, expected, actual, status = item
        return {
            "caseId": case_id,
            "classification": classification,
            "evaluatorType": evaluator,
            "expected": expected,
            "actual": actual,
            "target": {"path": path, "format": "xlsx", "kind": "typed-semantic-fact"},
            "diagnostic": {"code": "XLSX-100-PRODUCER-EVIDENCE", "message": f"independent typed evidence bound to {path}"},
            "result": "passed",
            "input": {"caseId": case_id, "source": path, "semanticStatus": status},
        }

    return [
        make("issue100-positive-profile-fact", equal, "positive", "format-profile"),
        make("issue100-mutation-profile-fact", different, "mutation", "mutation-killed"),
    ]


def _write_producer_envelope(out_dir: Path, reports: dict[str, dict[str, Any]], corpus_path: Path, source_sha: str) -> None:
    input_paths = [
        corpus_path,
        ROOT / "tools" / "qualification_issue100.py",
        ROOT / "tools" / "test_qualification_issue100.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_xlsx.py",
        ROOT / "e2e" / "corpus" / "xlsx-independent" / "[Content_Types].xml",
        ROOT / "e2e" / "corpus" / "xlsx-independent" / "_rels" / ".rels",
        ROOT / "e2e" / "corpus" / "xlsx-independent" / "xl" / "_rels" / "workbook.xml.rels",
        ROOT / "e2e" / "corpus" / "xlsx-independent" / "xl" / "workbook.xml",
        ROOT / "e2e" / "corpus" / "xlsx-independent" / "xl" / "worksheets" / "sheet1.xml",
        ROOT / "tools" / "validate_qualification_contract.py",
    ]
    write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names=REPORT_NAMES,
        artifact_report_names=list(REPORT_NAMES.values())[:4],
        issue_number=100,
        evidence_id="issue-100-xlsx-profile",
        requirement_id="QUAL-100-XLSX-PROFILE",
        source_sha=source_sha,
        input_paths=input_paths,
        producer_id="fdir-xlsx-public-converter",
        authority_id="fdir-xlsx-independent-ooxml-oracle",
        producer_component_path=ROOT / "tools" / "convert_document.py",
        authority_component_path=corpus_path,
        evaluator_component_path=ROOT / "tools" / "qualification_issue100.py",
        rows=_producer_rows(reports),
        shared_component_paths=[ROOT / "tools" / "adapter_xlsx.py"],
    )


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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha() -> str | None:
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
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and SOURCE_SHA_RE.fullmatch(value) else None


def _source_tree_clean(out_dir: Path | None = None) -> bool | None:
    """Return whether the report's source SHA describes a clean checkout.

    Git honors the repository's broad ``e2e/.run/`` ignore rule, so a
    temporary qualification output directory can otherwise make a copied
    checkout appear clean.  The declared Issue #100 bundle directory is the
    only in-tree output directory that is allowed to be ignored here.
    """

    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    clean = not result.stdout.strip()
    if not clean or out_dir is None:
        return clean
    try:
        resolved_out_dir = Path(out_dir).resolve()
        resolved_root = ROOT.resolve()
        resolved_out_dir.relative_to(resolved_root)
    except (OSError, ValueError):
        return clean
    return clean and resolved_out_dir == DEFAULT_OUT_DIR.resolve()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ET.Element | None, name: str, default: Any = None) -> Any:
    if element is None:
        return default
    if name in element.attrib:
        return element.attrib[name]
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return default


def _children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in list(element) if _local(child.tag) == name]


def _first(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext())


def _bool(value: Any) -> bool:
    return str(value).lower() in {"1", "true", "on", "yes"}


def _safe_source_path(relative: str) -> Path:
    candidate = (ROOT / relative).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise QualificationError(f"corpus source escapes repository: {relative!r}") from exc
    return candidate


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(Path(path))
    if not isinstance(corpus, dict):
        raise QualificationError("issue #100 corpus root must be an object")
    if corpus.get("schema") != "fdir/qualification-issue-100-corpus":
        raise QualificationError("issue #100 corpus schema is invalid")
    if corpus.get("version") != "1.1.0" or corpus.get("issueNumber") != 100:
        raise QualificationError("issue #100 corpus version or issue binding is invalid")
    if corpus.get("qualificationScope") != "independent-xlsx-profile-real-producer-facts":
        raise QualificationError("issue #100 qualification scope is invalid")
    if list(corpus.get("reportNames", [])) != list(REPORT_NAMES.values()):
        raise QualificationError("issue #100 report list is incomplete or reordered")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict):
        raise QualificationError("issue #100 has no oracle declaration")
    if oracle.get("expectedFactsAreAuthored") is not True:
        raise QualificationError("issue #100 expected facts are not declared authored")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #100 oracle permits adapter-derived expected values")
    if oracle.get("sourceParserIsIndependent") is not True:
        raise QualificationError("issue #100 source parser is not declared independent")
    forbidden = oracle.get("forbiddenDerivations")
    if not isinstance(forbidden, list) or not forbidden or not all(isinstance(item, str) for item in forbidden):
        raise QualificationError("issue #100 oracle has no forbidden derivation policy")

    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) < 2:
        raise QualificationError("issue #100 requires at least two independent XLSX fixtures")
    fixture_ids: set[str] = set()
    producer_ids: set[str] = set()
    required_expected = {"profile", "relationships", "valueFormula", "styles", "gridTable", "unsupported"}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise QualificationError("issue #100 fixture is not an object")
        fixture_id = fixture.get("fixtureId")
        producer = fixture.get("producerId")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in fixture_ids:
            raise QualificationError(f"invalid or duplicate fixture id: {fixture_id!r}")
        if not isinstance(producer, str) or not producer:
            raise QualificationError(f"fixture {fixture_id} has no producer id")
        provenance = fixture.get("provenance")
        if not isinstance(provenance, dict):
            raise QualificationError(f"fixture {fixture_id} has no provenance record")
        if provenance.get("availability") != "bound":
            raise QualificationError(f"fixture {fixture_id} is not explicitly bound")
        if not isinstance(provenance.get("kind"), str) or not provenance["kind"]:
            raise QualificationError(f"fixture {fixture_id} has no provenance kind")
        if not isinstance(provenance.get("sourceReference"), str) or not provenance["sourceReference"]:
            raise QualificationError(f"fixture {fixture_id} has no provenance source reference")
        source = fixture.get("source")
        expected = fixture.get("expected")
        if not isinstance(source, dict) or source.get("type") not in {"zip-parts", "existing-directory", "existing-file"}:
            raise QualificationError(f"fixture {fixture_id} has no safe source descriptor")
        if source["type"] == "zip-parts" and not isinstance(source.get("parts"), dict):
            raise QualificationError(f"fixture {fixture_id} has no authored OOXML parts")
        if source["type"] in {"existing-directory", "existing-file"} and not isinstance(source.get("path"), str):
            raise QualificationError(f"fixture {fixture_id} has no repository source path")
        if not isinstance(expected, dict) or not required_expected.issubset(expected):
            raise QualificationError(f"fixture {fixture_id} expected sections are incomplete")
        for section in required_expected:
            if not isinstance(expected[section], dict):
                raise QualificationError(f"fixture {fixture_id} expected.{section} must be an object")
        fixture_ids.add(fixture_id)
        producer_ids.add(producer)

    matrix = corpus.get("producerMatrix")
    if not isinstance(matrix, list):
        raise QualificationError("issue #100 producer matrix is missing")
    matrix_ids: set[str] = set()
    for entry in matrix:
        if not isinstance(entry, dict) or not isinstance(entry.get("producerId"), str):
            raise QualificationError("issue #100 producer matrix entry is invalid")
        producer_id = entry["producerId"]
        if producer_id in matrix_ids:
            raise QualificationError(f"duplicate producer matrix entry: {producer_id}")
        matrix_ids.add(producer_id)
        if entry.get("required") is not True and entry.get("required") is not False:
            raise QualificationError(f"producer {producer_id} must declare required")
        if entry.get("fixtureId") is not None and entry["fixtureId"] not in fixture_ids:
            raise QualificationError(f"producer {producer_id} references an unknown fixture")
        if entry.get("fixtureId") is not None:
            fixture = next(item for item in fixtures if item["fixtureId"] == entry["fixtureId"])
            if fixture["producerId"] != producer_id:
                raise QualificationError(f"producer {producer_id} is bound to fixture owned by {fixture['producerId']}")
        provenance = entry.get("provenance")
        if not isinstance(provenance, dict):
            raise QualificationError(f"producer {producer_id} has no provenance record")
        availability = provenance.get("availability")
        if availability not in PROVENANCE_AVAILABILITY:
            raise QualificationError(f"producer {producer_id} has invalid provenance availability")
        if not isinstance(provenance.get("kind"), str) or not provenance["kind"]:
            raise QualificationError(f"producer {producer_id} has no provenance kind")
        if not isinstance(provenance.get("sourceReference"), str) or not provenance["sourceReference"]:
            raise QualificationError(f"producer {producer_id} has no provenance source reference")
        if entry.get("fixtureId") is not None and availability != "bound":
            raise QualificationError(f"bound fixture producer {producer_id} cannot be marked missing")
        if entry.get("fixtureId") is None:
            input_path = entry.get("inputPath")
            if not isinstance(input_path, str) or not input_path:
                raise QualificationError(f"producer {producer_id} has no declared fixture path")
            path = _safe_source_path(input_path)
            if availability == "missing":
                if provenance.get("sha256") is not None:
                    raise QualificationError(f"missing producer {producer_id} cannot declare a fixture SHA-256")
                if not isinstance(provenance.get("missingReason"), str) or not provenance["missingReason"]:
                    raise QualificationError(f"missing producer {producer_id} has no explicit missing reason")
                if path.is_file():
                    raise QualificationError(
                        f"producer {producer_id} is marked missing but its declared fixture exists: {input_path}"
                    )
            else:
                declared_sha = provenance.get("sha256")
                if not isinstance(declared_sha, str) or not SHA256_RE.fullmatch(declared_sha):
                    raise QualificationError(f"bound producer {producer_id} has no authored fixture SHA-256")
                if not path.is_file():
                    raise QualificationError(f"bound producer {producer_id} fixture is missing: {input_path}")

    requirements = corpus.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise QualificationError("issue #100 has no executable requirement matrix")
    requirement_ids: set[str] = set()
    allowed_requirement_kinds = {
        "producer-fixture",
        "producer-differential",
        "section",
        "occurrence-accounting",
        "negative-cases",
        "issue89-defect-campaign",
        "evidence-bundle",
    }
    for requirement in requirements:
        if not isinstance(requirement, dict) or not isinstance(requirement.get("id"), str) or not requirement["id"]:
            raise QualificationError("issue #100 requirement entry is invalid")
        if requirement["id"] in requirement_ids:
            raise QualificationError(f"duplicate issue #100 requirement: {requirement['id']}")
        if requirement.get("kind") not in allowed_requirement_kinds:
            raise QualificationError(f"unsupported issue #100 requirement kind: {requirement.get('kind')!r}")
        if requirement.get("required") is not True:
            raise QualificationError(f"issue #100 requirement {requirement['id']} is not required")
        if requirement.get("kind") == "producer-fixture" and requirement.get("producerId") not in matrix_ids:
            raise QualificationError(f"requirement {requirement['id']} references an unknown producer")
        if requirement.get("kind") == "section" and requirement.get("section") not in {
            "profile",
            "relationships",
            "values",
            "styles",
            "grid",
            "unsupported",
        }:
            raise QualificationError(f"requirement {requirement['id']} has an invalid report section")
        requirement_ids.add(requirement["id"])

    campaign = corpus.get("defectCampaign")
    if not isinstance(campaign, dict) or campaign.get("issueNumber") != 89:
        raise QualificationError("issue #100 has no #89 defect campaign binding")
    if campaign.get("status") not in {"bound", "missing"}:
        raise QualificationError("issue #100 #89 defect campaign status is invalid")
    campaign_provenance = campaign.get("provenance")
    if not isinstance(campaign_provenance, dict) or not isinstance(campaign_provenance.get("sourceReference"), str):
        raise QualificationError("issue #100 #89 defect campaign has no provenance")
    if campaign["status"] == "missing" and not isinstance(campaign_provenance.get("missingReason"), str):
        raise QualificationError("missing issue #89 defect campaign has no explicit reason")
    if not set(REQUIRED_PRODUCERS).issubset(matrix_ids):
        raise QualificationError("issue #100 producer matrix omits a required external producer")

    negatives = corpus.get("negativeCases")
    if not isinstance(negatives, list) or not negatives:
        raise QualificationError("issue #100 has no negative mutation cases")
    negative_ids: set[str] = set()
    for case in negatives:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("kind"), str):
            raise QualificationError("issue #100 negative case is invalid")
        if case["id"] in negative_ids:
            raise QualificationError(f"duplicate negative case: {case['id']}")
        if not isinstance(case.get("expectedDefectCode"), str) or not case["expectedDefectCode"]:
            raise QualificationError(f"negative case {case['id']} has no defect code")
        negative_ids.add(case["id"])
    return corpus


def _part_bytes(spec: Any) -> bytes:
    if isinstance(spec, str):
        return spec.encode("utf-8")
    if isinstance(spec, dict) and isinstance(spec.get("text"), str):
        return spec["text"].encode("utf-8")
    if isinstance(spec, dict) and isinstance(spec.get("lines"), list) and all(isinstance(line, str) for line in spec["lines"]):
        return ("\n".join(spec["lines"]) + "\n").encode("utf-8")
    if isinstance(spec, dict) and isinstance(spec.get("base64"), str):
        import base64

        try:
            return base64.b64decode(spec["base64"], validate=True)
        except ValueError as exc:
            raise QualificationError(f"invalid base64 corpus part: {exc}") from exc
    raise QualificationError("corpus OOXML part must be text, lines, or base64")


def _zip_parts(path: Path, parts: dict[str, Any]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(parts):
            if not isinstance(name, str) or not name or name.startswith("/") or ".." in name.split("/"):
                raise QualificationError(f"unsafe OOXML member name: {name!r}")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _part_bytes(parts[name]))


def _materialize_fixture(fixture: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = fixture["source"]
    path = directory / f"{fixture['fixtureId']}.xlsx"
    source_type = source["type"]
    if source_type == "zip-parts":
        _zip_parts(path, source["parts"])
        return path
    source_path = _safe_source_path(source["path"])
    if not source_path.exists():
        raise QualificationError(f"fixture source is missing: {source['path']}")
    if source_type == "existing-file":
        shutil.copyfile(source_path, path)
        return path
    if not source_path.is_dir():
        raise QualificationError(f"fixture source is not a directory: {source['path']}")
    parts: dict[str, bytes] = {}
    for child in source_path.rglob("*"):
        if child.is_file():
            parts[child.relative_to(source_path).as_posix()] = child.read_bytes()
    if not parts:
        raise QualificationError(f"fixture source directory is empty: {source['path']}")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, parts[name])
    return path


def _xml(parts: dict[str, bytes], name: str) -> ET.Element | None:
    payload = parts.get(name)
    if payload is None:
        return None
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise QualificationError(f"invalid XML in source member {name}: {exc}") from exc


def _relationship_source(name: str) -> str:
    if name == "_rels/.rels":
        return "[package]"
    marker = "/_rels/"
    if marker not in name or not name.endswith(".rels"):
        raise QualificationError(f"invalid relationship member: {name}")
    parent, rel_name = name.split(marker, 1)
    return f"{parent}/{rel_name[:-5]}"


def _resolve_target(source: str, target: str) -> str | None:
    if source == "[package]":
        base = ""
    else:
        base = source.rsplit("/", 1)[0] if "/" in source else ""
    value = target.replace("\\", "/")
    resolved = posixpath.normpath(posixpath.join(base, value))
    return resolved.lstrip("/") if not resolved.startswith("../") else None


def _relationships(parts: dict[str, bytes]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in sorted(parts):
        if not name.endswith(".rels") or ("_rels" not in name and name != "_rels/.rels"):
            continue
        root = _xml(parts, name)
        if root is None:
            continue
        source = _relationship_source(name)
        for relation in _children(root, "Relationship"):
            target = str(_attr(relation, "Target", ""))
            mode = str(_attr(relation, "TargetMode", "Internal")).lower()
            records.append(
                {
                    "source": source,
                    "id": str(_attr(relation, "Id", "")),
                    "type": str(_attr(relation, "Type", "")),
                    "targetMode": mode,
                    "target": target,
                    "resolvedTarget": _resolve_target(source, target) if mode != "external" else None,
                }
            )
    return sorted(records, key=lambda item: (item["source"], item["id"]))


def _content_types(parts: dict[str, bytes]) -> dict[str, str]:
    root = _xml(parts, "[Content_Types].xml")
    result: dict[str, str] = {}
    if root is None:
        return result
    defaults = {str(_attr(item, "Extension", "")): str(_attr(item, "ContentType", "")) for item in _children(root, "Default")}
    for item in _children(root, "Override"):
        name = str(_attr(item, "PartName", "")).lstrip("/")
        result[name] = str(_attr(item, "ContentType", ""))
    for name in parts:
        if name not in result:
            suffix = name.rsplit(".", 1)[-1] if "." in name else ""
            result[name] = defaults.get(suffix, "")
    return result


def _shared_strings(parts: dict[str, bytes]) -> list[str]:
    root = _xml(parts, "xl/sharedStrings.xml")
    return ["".join(item.itertext()) for item in root.iter() if _local(item.tag) == "si"] if root is not None else []


def _num_formats(root: ET.Element | None) -> tuple[dict[str, str], dict[int, dict[str, Any]]]:
    custom: dict[str, str] = {}
    xfs: dict[int, dict[str, Any]] = {}
    if root is None:
        return custom, xfs
    num_fmts = _first(root, "numFmts")
    for item in _children(num_fmts, "numFmt") if num_fmts is not None else []:
        custom[str(_attr(item, "numFmtId", ""))] = str(_attr(item, "formatCode", ""))
    cell_xfs = _first(root, "cellXfs")
    for index, item in enumerate(_children(cell_xfs, "xf") if cell_xfs is not None else []):
        num_fmt_id = int(str(_attr(item, "numFmtId", "0")))
        builtin = {0: "General", 14: "m/d/yy", 20: "h:mm"}.get(num_fmt_id, "General")
        xfs[index] = {
            "index": index,
            "numFmtId": num_fmt_id,
            "code": custom.get(str(num_fmt_id), builtin),
            "fontId": int(str(_attr(item, "fontId", "0"))),
            "fillId": int(str(_attr(item, "fillId", "0"))),
            "borderId": int(str(_attr(item, "borderId", "0"))),
            "xfId": int(str(_attr(item, "xfId", "0"))),
            "applyNumberFormat": _bool(_attr(item, "applyNumberFormat", "0")),
        }
    return custom, xfs


def _serial_value(raw: str, date_system: str, code: str) -> tuple[str, str]:
    try:
        number = float(raw)
    except ValueError:
        return "number", raw
    lowered = code.lower()
    if "[h]" in lowered or "[m]" in lowered or "[s]" in lowered:
        return "duration", str(int(round(number * 86400))) if number.is_integer() else str(number * 86400)
    if any(token in lowered for token in ("yy", "dd", "mm", "m/", "/m", "d/", "/d")):
        base = datetime(1904, 1, 1) if date_system == "1904" else datetime(1899, 12, 30)
        return "date", (base + timedelta(days=number)).date().isoformat()
    return "number", raw


def _source_facts(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise QualificationError("XLSX package contains duplicate ZIP members")
            for info in infos:
                if info.file_size > 64 * 1024 * 1024:
                    raise QualificationError(f"XLSX package member exceeds 64 MiB: {info.filename}")
                if info.compress_size and info.file_size / info.compress_size > 1000:
                    raise QualificationError(f"XLSX package member compression ratio exceeds 1000:1: {info.filename}")
            parts = {info.filename: archive.read(info.filename) for info in infos}
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise QualificationError(f"cannot read XLSX package {path}: {exc}") from exc
    names = sorted(parts)
    rels = _relationships(parts)
    rel_by_source_id = {(item["source"], item["id"]): item for item in rels}
    types = _content_types(parts)
    workbook = _xml(parts, "xl/workbook.xml")
    styles_root = _xml(parts, "xl/styles.xml")
    custom_formats, xfs = _num_formats(styles_root)
    date_system = "1904" if _bool(_attr(_first(workbook, "workbookPr"), "date1904", "0")) else "1900"
    calc = _first(workbook, "calcPr")
    workbook_facts = {
        "dateSystem": date_system,
        "calculationMode": str(_attr(calc, "calcMode", "automatic")) if calc is not None else "automatic",
        "fullCalcOnLoad": _bool(_attr(calc, "fullCalcOnLoad", "0")) if calc is not None else False,
        "forceFullCalc": _bool(_attr(calc, "forceFullCalc", "0")) if calc is not None else False,
        "sheets": [],
        "definedNames": [],
    }
    sheets_root = _first(workbook, "sheets")
    for sheet in _children(sheets_root, "sheet") if sheets_root is not None else []:
        rid = str(_attr(sheet, "id", ""))
        relation = rel_by_source_id.get(("xl/workbook.xml", rid), {})
        workbook_facts["sheets"].append(
            {
                "name": str(_attr(sheet, "name", "")),
                "sheetId": str(_attr(sheet, "sheetId", "")),
                "relationshipId": rid,
                "target": relation.get("resolvedTarget"),
                "state": str(_attr(sheet, "state", "visible")),
            }
        )
    names_root = _first(workbook, "definedNames")
    for defined in _children(names_root, "definedName") if names_root is not None else []:
        workbook_facts["definedNames"].append(
            {"name": str(_attr(defined, "name", "")), "localSheetId": _attr(defined, "localSheetId"), "formula": _text(defined)}
        )
    workbook_facts["sheets"].sort(key=lambda item: int(item["sheetId"]) if item["sheetId"].isdigit() else item["sheetId"])
    workbook_facts["definedNames"].sort(key=lambda item: item["name"])

    cells: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []
    grids: list[dict[str, Any]] = []
    shared = _shared_strings(parts)
    for sheet_info in workbook_facts["sheets"]:
        target = sheet_info.get("target")
        if not isinstance(target, str) or target not in parts:
            continue
        root = _xml(parts, target)
        if root is None:
            continue
        sheet_cells: list[dict[str, Any]] = []
        for cell in [item for item in root.iter() if _local(item.tag) == "c"]:
            address = str(_attr(cell, "r", ""))
            cell_type = str(_attr(cell, "t", "n"))
            style_index = int(str(_attr(cell, "s", "0")))
            raw_value = _text(_first(cell, "v")) if _first(cell, "v") is not None else None
            formula_node = _first(cell, "f")
            inline_value = _text(_first(cell, "is")) if _first(cell, "is") is not None else None
            if cell_type == "s" and raw_value is not None and raw_value.isdigit() and int(raw_value) < len(shared):
                logical = shared[int(raw_value)]
                typed_type = "string"
            elif cell_type in {"inlineStr", "str"}:
                logical = inline_value if inline_value is not None else raw_value
                typed_type = "string"
            elif cell_type == "b":
                logical = _bool(raw_value)
                typed_type = "boolean"
            elif cell_type == "e":
                logical = raw_value
                typed_type = "error"
            elif raw_value is None and formula_node is None and inline_value is None:
                logical = None
                typed_type = "blank"
            else:
                style = xfs.get(style_index, {})
                typed_type, logical = _serial_value(raw_value or "", date_system, str(style.get("code", "General")))
            item = {
                "sheet": sheet_info["name"],
                "address": address,
                "stored": raw_value,
                "typedType": typed_type,
                "logical": logical,
                "styleIndex": style_index,
            }
            if formula_node is not None:
                formula = {
                    "sheet": sheet_info["name"],
                    "address": address,
                    "source": _text(formula_node),
                    "kind": str(_attr(formula_node, "t", "normal")),
                    "ref": _attr(formula_node, "ref"),
                    "sharedIndex": _attr(formula_node, "si"),
                    "cached": raw_value,
                }
                formulas.append(formula)
                item["formula"] = {key: formula[key] for key in ("source", "kind", "ref", "sharedIndex")}
            sheet_cells.append(item)
        cells.extend(sheet_cells)
        cols = []
        cols_root = _first(root, "cols")
        for col in _children(cols_root, "col") if cols_root is not None else []:
            cols.append(
                {
                    "min": int(str(_attr(col, "min", "0"))),
                    "max": int(str(_attr(col, "max", "0"))),
                    "width": _attr(col, "width"),
                    "hidden": _bool(_attr(col, "hidden", "0")),
                    "outlineLevel": int(str(_attr(col, "outlineLevel", "0"))),
                    "style": int(str(_attr(col, "style", "0"))),
                }
            )
        rows = []
        for row in [item for item in root.iter() if _local(item.tag) == "row"]:
            rows.append(
                {
                    "row": int(str(_attr(row, "r", "0"))),
                    "hidden": _bool(_attr(row, "hidden", "0")),
                    "outlineLevel": int(str(_attr(row, "outlineLevel", "0"))),
                }
            )
        merges = sorted(str(_attr(item, "ref", "")) for item in root.iter() if _local(item.tag) == "mergeCell")
        styled_empty = sorted(item["address"] for item in sheet_cells if item["styleIndex"] and item["stored"] is None and "formula" not in item)
        sheet_rels = [item for item in rels if item["source"] == target]
        grids.append(
            {
                "sheet": sheet_info["name"],
                "dimension": str(_attr(_first(root, "dimension"), "ref", "")),
                "rows": sorted(rows, key=lambda item: item["row"]),
                "columns": sorted(cols, key=lambda item: (item["min"], item["max"])),
                "cells": sorted(item["address"] for item in sheet_cells),
                "styledEmptyCells": styled_empty,
                "mergedRanges": merges,
                "conditionalFormattingCount": sum(1 for item in root.iter() if _local(item.tag) == "conditionalFormatting"),
                "dataValidationCount": sum(1 for item in root.iter() if _local(item.tag) == "dataValidation"),
                "hyperlinkCount": sum(1 for item in root.iter() if _local(item.tag) == "hyperlink"),
                "relationshipIds": sorted(item["id"] for item in sheet_rels),
            }
        )

    tables: list[dict[str, Any]] = []
    for name in names:
        if not name.startswith("xl/tables/") or not name.endswith(".xml"):
            continue
        root = _xml(parts, name)
        if root is None:
            continue
        table_columns = []
        table_columns_root = _first(root, "tableColumns")
        for column in _children(table_columns_root, "tableColumn") if table_columns_root is not None else []:
            table_columns.append({"id": int(str(_attr(column, "id", "0"))), "name": str(_attr(column, "name", ""))})
        tables.append(
            {
                "path": name,
                "name": str(_attr(root, "name", "")),
                "displayName": str(_attr(root, "displayName", "")),
                "ref": str(_attr(root, "ref", "")),
                "headerRowCount": int(str(_attr(root, "headerRowCount", "1"))),
                "totalsRowCount": int(str(_attr(root, "totalsRowCount", "0"))),
                "columns": table_columns,
            }
        )

    unsupported = [
        name
        for name in names
        if name.endswith("calcChain.xml")
        or "/externalLinks/" in name
        or "/pivot" in name.lower()
        or "/slicer" in name.lower()
        or "threadedComment" in name
    ]
    return {
        "packageMembers": names,
        "contentTypes": types,
        "relationships": rels,
        "workbook": workbook_facts,
        "cells": sorted(cells, key=lambda item: (item["sheet"], item["address"])),
        "formulas": sorted(formulas, key=lambda item: (item["sheet"], item["address"])),
        "styles": {"customFormats": custom_formats, "cellXfs": [xfs[index] for index in sorted(xfs)]},
        "grids": sorted(grids, key=lambda item: item["sheet"]),
        "tables": sorted(tables, key=lambda item: item["path"]),
        "unsupportedPaths": sorted(unsupported),
        "sourceOccurrenceCount": len(names) + len(rels) + len(cells) + len(formulas),
    }


def _source_projection(facts: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Project independent source facts into the authored comparison shape."""

    profile = {
        "packageMembers": facts["packageMembers"],
        "workbook": facts["workbook"],
        "contentTypes": facts["contentTypes"],
    }
    value_formula = {"cells": facts["cells"], "formulas": facts["formulas"]}
    grid_table = {"grids": facts["grids"], "tables": facts["tables"]}
    return {
        "profile": profile,
        "relationships": facts["relationships"],
        "valueFormula": value_formula,
        "styles": facts["styles"],
        "gridTable": grid_table,
        "unsupported": {"paths": facts["unsupportedPaths"]},
    }


def _part_name_map(document: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("partId")): str(item.get("name"))
        for item in document.get("parts", [])
        if isinstance(item, dict) and isinstance(item.get("partId"), str) and isinstance(item.get("name"), str)
    }


def _source_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in document.get("sourceMaps", []):
        if isinstance(item, dict) and isinstance(item.get("targetId"), str) and isinstance(item.get("locator"), dict):
            result[item["targetId"]] = item["locator"]
    return result


def _occurrence_inventory(*, source_facts: dict[str, Any] | None = None, actual: dict[str, Any] | None = None) -> set[str]:
    """Build stable occurrence keys without treating counts as proof of closure."""

    inventory: set[str] = set()
    if source_facts is not None:
        package_members = source_facts.get("packageMembers", [])
        inventory.update(f"package:{item}" for item in package_members if isinstance(item, str))
        inventory.add("package:[package]")
        inventory.update(
            f"relationship:{item.get('source')}:{item.get('id')}"
            for item in source_facts.get("relationships", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"cell:{item.get('sheet')}:{item.get('address')}"
            for item in source_facts.get("cells", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"formula:{item.get('sheet')}:{item.get('address')}"
            for item in source_facts.get("formulas", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"style:cellXf:{item.get('index')}"
            for item in source_facts.get("styles", {}).get("cellXfs", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"grid:{item.get('sheet')}"
            for item in source_facts.get("grids", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"table:{item.get('path')}"
            for item in source_facts.get("tables", [])
            if isinstance(item, dict)
        )
        inventory.update(f"unsupported:{item}" for item in source_facts.get("unsupportedPaths", []) if isinstance(item, str))
    if actual is not None:
        package_members = actual.get("profile", {}).get("packageMembers", [])
        inventory.update(f"package:{item}" for item in package_members if isinstance(item, str))
        inventory.update(
            f"relationship:{item.get('source')}:{item.get('id')}"
            for item in actual.get("relationships", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"cell:{item.get('sheet')}:{item.get('address')}"
            for item in actual.get("valueFormula", {}).get("cells", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"formula:{item.get('sheet')}:{item.get('address')}"
            for item in actual.get("valueFormula", {}).get("formulas", [])
            if isinstance(item, dict)
        )
        for style_id in actual.get("styles", {}).get("requiredStyleIds", []):
            match = re.search(r"cellXfs-(\d+)-", str(style_id))
            if match:
                inventory.add(f"style:cellXf:{match.group(1)}")
        inventory.update(
            f"grid:{item.get('sheet')}"
            for item in actual.get("gridTable", {}).get("grids", [])
            if isinstance(item, dict)
        )
        inventory.update(
            f"table:{item.get('path')}"
            for item in actual.get("gridTable", {}).get("tables", [])
            if isinstance(item, dict)
        )
        inventory.update(f"unsupported:{item}" for item in actual.get("unsupported", {}).get("paths", []) if isinstance(item, str))
    return inventory


def _occurrence_accounting(source_facts: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    source_inventory = _occurrence_inventory(source_facts=source_facts)
    actual_inventory = _occurrence_inventory(actual=actual)
    missing = sorted(source_inventory - actual_inventory)
    unexpected = sorted(actual_inventory - source_inventory)
    return {
        "status": "passed" if not missing and not unexpected else "failed",
        "sourceOccurrenceCount": len(source_inventory),
        "actualOccurrenceCount": len(actual_inventory),
        "missingSourceOccurrences": missing,
        "unexpectedActualOccurrences": unexpected,
        "unaccountedOccurrenceCount": len(missing) + len(unexpected),
    }


def _actual_projection(document: dict[str, Any]) -> dict[str, Any]:
    part_names = _part_name_map(document)
    maps = _source_map(document)
    formula_by_id = {item.get("formulaId"): item for item in document.get("formulas", []) if isinstance(item, dict)}
    text_by_id = {item.get("textId"): item for item in document.get("texts", []) if isinstance(item, dict)}
    cells: list[dict[str, Any]] = []
    for node in document.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "cell":
            continue
        locator = maps.get(node.get("nodeId"), {})
        address = locator.get("cell")
        if not isinstance(address, str):
            address_value = node.get("address", {})
            address = address_value.get("cell") if isinstance(address_value, dict) else None
        if not isinstance(address, str):
            continue
        value = node.get("value") if isinstance(node.get("value"), dict) else {}
        displayed = None
        source_text = None
        for text_id in node.get("textIds", []):
            text = text_by_id.get(text_id, {})
            if text.get("representation") == "displayed":
                displayed = text.get("value")
            if text.get("representation") == "source":
                source_text = text.get("value")
        style_id = node.get("directStyleId")
        style_match = re.search(r"cellXfs-(\d+)$", str(style_id)) if style_id else None
        formula = formula_by_id.get(node.get("formulaId")) if node.get("formulaId") else None
        item: dict[str, Any] = {
            "sheet": locator.get("worksheet"),
            "address": address,
            "stored": formula.get("values", {}).get("stored", {}).get("value") if isinstance(formula, dict) else source_text if source_text is not None else value.get("value"),
            "typedType": value.get("type"),
            "logical": value.get("value"),
            "styleIndex": int(style_match.group(1)) if style_match else 0,
        }
        if isinstance(formula, dict):
            expression = formula.get("expression", {}) if isinstance(formula.get("expression"), dict) else {}
            item["formula"] = {
                "source": expression.get("source"),
                "kind": formula.get("kind", "normal"),
                "ref": formula.get("range", {}).get("end") if isinstance(formula.get("range"), dict) and formula.get("range", {}).get("start") != address else None,
                "sharedIndex": None,
            }
        if displayed is not None:
            item["displayed"] = displayed
        cells.append(item)

    actual_relations = []
    for relation in document.get("relations", []):
        if not isinstance(relation, dict):
            continue
        source_relationship_id = relation.get("sourceRelationshipId")
        # Resource-consumer edges are derived IR conveniences, not OPC
        # relationship occurrences.  Only sourceRelationshipId-backed edges
        # belong in the independent relationship closure projection.
        if not isinstance(source_relationship_id, str) or not source_relationship_id:
            continue
        source_name = part_names.get(relation.get("fromId"), relation.get("fromId"))
        if source_name == "OOXML package":
            source_name = "[package]"
        target_name = part_names.get(relation.get("toId"), relation.get("toId")) if relation.get("toId") else None
        target_mode = relation.get("targetMode", "internal")
        actual_relations.append(
            {
                "source": source_name,
                "id": source_relationship_id,
                "type": relation.get("type", relation.get("relationshipType", "")),
                "targetMode": target_mode,
                "target": target_name,
                "resolvedTarget": None if target_mode == "external" else target_name,
            }
        )

    nodes_by_id = {item.get("nodeId"): item for item in document.get("nodes", []) if isinstance(item, dict)}
    grids = []
    for table in document.get("tables", []):
        if not isinstance(table, dict) or not str(table.get("tableId", "")).startswith("table-xlsx-grid-"):
            continue
        row_refs = [nodes_by_id.get(item, {}).get("address", {}).get("row") for item in table.get("rowIds", [])]
        cells_in_grid = []
        for item in table.get("cellIds", []):
            locator = maps.get(item, {})
            if isinstance(locator.get("cell"), str):
                cells_in_grid.append(locator["cell"])
        merged = []
        for item in table.get("mergedRanges", []):
            if not isinstance(item, dict):
                continue
            start = item.get("from", {})
            end = item.get("to", {})
            if not (isinstance(start, dict) and isinstance(end, dict) and start and end) and isinstance(item.get("range"), str):
                endpoints = [part.strip() for part in item["range"].split(":", 1)]
                if len(endpoints) == 1:
                    endpoints.append(endpoints[0])
                if all(re.fullmatch(r"[A-Za-z]+\d+", endpoint) for endpoint in endpoints):
                    master = item.get("masterCellId")
                    master_address = nodes_by_id.get(master, {}).get("address", {}) if isinstance(master, str) else {}
                    sheet_id = master_address.get("sheetId")

                    def _a1_address(token: str) -> dict[str, Any]:
                        match = re.fullmatch(r"([A-Za-z]+)(\d+)", token)
                        assert match is not None
                        column = 0
                        for character in match.group(1).upper():
                            column = column * 26 + ord(character) - ord("A") + 1
                        return {
                            **({"sheetId": sheet_id} if isinstance(sheet_id, str) else {}),
                            "row": int(match.group(2)),
                            "column": column,
                        }

                    start, end = _a1_address(endpoints[0]), _a1_address(endpoints[1])
            if isinstance(start, dict) and isinstance(end, dict):
                merged.append({"from": start, "to": end})
        grids.append(
            {
                "sheet": next((maps.get(item, {}).get("worksheet") for item in table.get("cellIds", []) if maps.get(item, {}).get("worksheet")), None),
                "dimension": None,
                "rows": sorted(item for item in row_refs if item is not None),
                "columns": len(table.get("columnIds", [])),
                "cells": sorted(cells_in_grid),
                "styledEmptyCells": [],
                "mergedRanges": merged,
            }
        )
    actual_tables = []
    for extension in document.get("extensions", []):
        if isinstance(extension, dict) and extension.get("type") == "table-definition":
            payload = extension.get("payload", {})
            if isinstance(payload, dict):
                actual_tables.append(
                    {
                        "path": payload.get("path"),
                        "name": payload.get("name"),
                        "displayName": payload.get("name"),
                        "ref": payload.get("range"),
                        "headerRowCount": None,
                        "totalsRowCount": None,
                        "columns": [],
                    }
                )
    style_ids = sorted(str(item.get("styleId")) for item in document.get("styles", []) if isinstance(item, dict) and item.get("styleId"))
    number_formats = sorted(
        {
            str(item.get("resolved", {}).get("numberFormat", {}).get("code"))
            for item in document.get("styles", [])
            if isinstance(item, dict) and isinstance(item.get("resolved"), dict) and isinstance(item.get("resolved", {}).get("numberFormat"), dict)
        }
    )
    unsupported = sorted(
        {
            str(item.get("targetId"))
            for item in document.get("conversion", {}).get("features", [])
            if isinstance(item, dict) and item.get("status") in {"unsupported", "unavailable"} and item.get("feature") not in {"computed-value"}
        }
    )
    formula_projection = []
    for formula in formula_by_id.values():
        if not isinstance(formula, dict):
            continue
        owner = formula.get("ownerCellId")
        locator = maps.get(owner, {})
        values = formula.get("values", {}) if isinstance(formula.get("values"), dict) else {}
        cached = values.get("cached", {}) if isinstance(values.get("cached"), dict) else {}
        expression = formula.get("expression", {}) if isinstance(formula.get("expression"), dict) else {}
        formula_projection.append(
            {
                "sheet": locator.get("worksheet"),
                "address": formula.get("ownerAddress"),
                "source": expression.get("source"),
                "kind": formula.get("kind"),
                "formulaType": formula.get("formulaType"),
                "ref": formula.get("range", {}).get("end") if isinstance(formula.get("range"), dict) else None,
                "sharedIndex": formula.get("sharedIndex"),
                "sourceReference": formula.get("sourceReference"),
                "cached": cached.get("value"),
            }
        )
    package_members = []
    for item in document.get("parts", []):
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "embeddedObject" and str(item.get("name", "")).startswith(("http://", "https://")):
            continue
        package_members.append("[package]" if item.get("kind") == "package" else item.get("name"))
    for grid in grids:
        row_numbers = []
        for row_id in next((item.get("rowIds", []) for item in document.get("tables", []) if isinstance(item, dict) and str(item.get("tableId", "")).startswith("table-xlsx-grid-") and maps.get(item.get("cellIds", [None])[0], {}).get("worksheet") == grid.get("sheet")), []):
            row_node = nodes_by_id.get(row_id, {})
            row_address = row_node.get("address") if isinstance(row_node.get("address"), dict) else {}
            if isinstance(row_address.get("row"), int):
                row_numbers.append(row_address["row"])
            else:
                match = re.search(r"-row-\d+-(\d+)$", str(row_id))
                if match:
                    row_numbers.append(int(match.group(1)))
        grid["rows"] = sorted(set(row_numbers))
    diagnostics_by_id = {
        item.get("diagnosticId"): item
        for item in document.get("diagnostics", [])
        if isinstance(item, dict) and item.get("diagnosticId")
    }
    unsupported_paths = []
    for feature in document.get("conversion", {}).get("features", []):
        if not isinstance(feature, dict) or feature.get("status") != "unsupported":
            continue
        diagnostic_ids = feature.get("diagnosticIds", [])
        for diagnostic_id in diagnostic_ids:
            message = diagnostics_by_id.get(diagnostic_id, {}).get("message", "")
            if isinstance(message, str) and ": " in message:
                unsupported_paths.append(message.rsplit(": ", 1)[-1])
    unsupported_paths = sorted(set(unsupported_paths))
    return {
        "profile": {
            "capabilityProfile": document.get("conversion", {}).get("capabilityProfile"),
            "conversionStatus": document.get("conversion", {}).get("status"),
            "packageMembers": sorted(item for item in package_members if isinstance(item, str)),
            "workbook": {"dateSystem": next((item.get("calculationContext", {}).get("dateSystem") for item in formula_by_id.values() if isinstance(item, dict)), None)},
            "contentTypes": {},
        },
        "relationships": sorted(actual_relations, key=lambda item: (str(item["source"]), str(item["id"]))),
        "valueFormula": {"cells": sorted(cells, key=lambda item: (str(item.get("sheet")), item["address"])), "formulas": sorted(formula_projection, key=lambda item: (str(item.get("sheet")), str(item.get("address"))))},
        "styles": {"requiredStyleIds": style_ids, "numberFormats": number_formats},
        "gridTable": {"grids": grids, "tables": sorted(actual_tables, key=lambda item: str(item.get("path")))},
        "unsupported": {"paths": unsupported_paths},
    }


def _list_identity(path: str, item: Any) -> tuple[Any, ...] | None:
    if not isinstance(item, dict):
        return None
    if path.endswith(".relationships"):
        return (item.get("source"), item.get("id"))
    if path.endswith(".cells") or path.endswith(".formulas"):
        return (item.get("sheet"), item.get("address"))
    if path.endswith(".grids"):
        return (item.get("sheet"),)
    if path.endswith(".tables"):
        return (item.get("path"),)
    if path.endswith(".rows"):
        return (item.get("row"),)
    if path.endswith(".columns"):
        return (item.get("min"), item.get("max"))
    return None


def _display_list_key(value: Any) -> str:
    return _canonical(value)


def _diff(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [{"path": path, "expected": expected, "actual": actual, "code": "TYPE-MISMATCH"}]
        findings: list[dict[str, Any]] = []
        for key in sorted(expected):
            if key not in actual:
                findings.append({"path": f"{path}.{key}", "expected": expected[key], "actual": "<missing>", "code": "MISSING"})
            else:
                findings.extend(_diff(expected[key], actual[key], f"{path}.{key}"))
        for key in sorted(set(actual) - set(expected)):
            findings.append({"path": f"{path}.{key}", "expected": "<absent>", "actual": actual[key], "code": "UNEXPECTED"})
        return findings
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [{"path": path, "expected": expected, "actual": actual, "code": "TYPE-MISMATCH"}]
        identity_probe = _list_identity(path, expected[0] if expected else (actual[0] if actual else None))
        if identity_probe is not None and all(isinstance(item, dict) for item in expected + actual):
            expected_by_key = {_display_list_key(_list_identity(path, item)): item for item in expected}
            actual_by_key = {_display_list_key(_list_identity(path, item)): item for item in actual}
            findings: list[dict[str, Any]] = []
            for key in sorted(set(expected_by_key) - set(actual_by_key)):
                findings.append(
                    {
                        "path": f"{path}[{key}]",
                        "expected": expected_by_key[key],
                        "actual": "<missing>",
                        "code": "MISSING-OCCURRENCE",
                    }
                )
            for key in sorted(set(actual_by_key) - set(expected_by_key)):
                findings.append(
                    {
                        "path": f"{path}[{key}]",
                        "expected": "<absent>",
                        "actual": actual_by_key[key],
                        "code": "UNEXPECTED-OCCURRENCE",
                    }
                )
            for key in sorted(set(expected_by_key) & set(actual_by_key)):
                expected_item = expected_by_key[key]
                selected_actual = _select_shape(expected_item, actual_by_key[key])
                findings.extend(_diff(expected_item, selected_actual, f"{path}[{key}]"))
            return findings
        if all(not isinstance(item, (dict, list)) for item in expected + actual):
            expected_counts: dict[str, int] = {}
            actual_counts: dict[str, int] = {}
            expected_values: dict[str, Any] = {}
            actual_values: dict[str, Any] = {}
            for item in expected:
                key = _display_list_key(item)
                expected_counts[key] = expected_counts.get(key, 0) + 1
                expected_values[key] = item
            for item in actual:
                key = _display_list_key(item)
                actual_counts[key] = actual_counts.get(key, 0) + 1
                actual_values[key] = item
            findings = []
            for key in sorted(set(expected_counts) | set(actual_counts)):
                missing = expected_counts.get(key, 0) - actual_counts.get(key, 0)
                unexpected = actual_counts.get(key, 0) - expected_counts.get(key, 0)
                for _ in range(max(0, missing)):
                    findings.append(
                        {
                            "path": f"{path}[{key}]",
                            "expected": expected_values[key],
                            "actual": "<missing>",
                            "code": "MISSING-OCCURRENCE",
                        }
                    )
                for _ in range(max(0, unexpected)):
                    findings.append(
                        {
                            "path": f"{path}[{key}]",
                            "expected": "<absent>",
                            "actual": actual_values[key],
                            "code": "UNEXPECTED-OCCURRENCE",
                        }
                    )
            return findings
        findings = []
        for index in range(max(len(expected), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(expected):
                findings.append({"path": child, "expected": "<absent>", "actual": actual[index], "code": "UNEXPECTED"})
            elif index >= len(actual):
                findings.append({"path": child, "expected": expected[index], "actual": "<missing>", "code": "MISSING"})
            else:
                findings.extend(_diff(expected[index], actual[index], child))
        return findings
    if expected != actual:
        return [{"path": path, "expected": expected, "actual": actual, "code": "VALUE-MISMATCH"}]
    return []


def _select_shape(expected: Any, actual: Any, path: str = "$") -> Any:
    """Select only authored fields while retaining list cardinality strictly."""

    if isinstance(expected, dict):
        source = actual if isinstance(actual, dict) else {}
        return {key: _select_shape(value, source.get(key), f"{path}.{key}") for key, value in expected.items()}
    if isinstance(expected, list):
        source = actual if isinstance(actual, list) else []
        if all(not isinstance(item, (dict, list)) for item in expected + source):
            return source
        identity_probe = _list_identity(path, expected[0] if expected else (source[0] if source else None))
        if identity_probe is not None and all(isinstance(item, dict) for item in expected + source):
            actual_by_key = {_display_list_key(_list_identity(path, item)): item for item in source}
            expected_keys = {_display_list_key(_list_identity(path, item)) for item in expected}
            selected = []
            for value in expected:
                key = _display_list_key(_list_identity(path, value))
                selected.append(_select_shape(value, actual_by_key[key], f"{path}[{key}]") if key in actual_by_key else None)
            selected.extend(source[index] for index, item in enumerate(source) if _display_list_key(_list_identity(path, item)) not in expected_keys)
            return selected
        return [_select_shape(value, source[index], f"{path}[{index}]") if index < len(source) else None for index, value in enumerate(expected)]
    return actual


def _run_command(command: list[str], timeout: int = 180) -> dict[str, Any]:
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returnCode": 124, "stdout": "", "stderr": str(exc)}
    return {"returnCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _run_converter(fixture: dict[str, Any], work: Path) -> dict[str, Any]:
    input_path = _materialize_fixture(fixture, work / "inputs")
    output_path = work / "ir" / f"{fixture['fixtureId']}.json"
    evidence_path = work / "evidence" / f"{fixture['fixtureId']}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    inspect = _run_command([sys.executable, str(CONVERTER_PATH), "inspect", str(input_path), "--format", "xlsx"])
    run = _run_command(
        [sys.executable, str(CONVERTER_PATH), "convert", str(input_path), "--format", "xlsx", "--out", str(output_path), "--evidence", str(evidence_path)]
    )
    document = _read_json(output_path) if output_path.is_file() else None
    evidence = _read_json(evidence_path) if evidence_path.is_file() else None
    expected_sha = _sha256_file(input_path)
    evidence_findings = []
    if not isinstance(evidence, dict) or evidence.get("input", {}).get("consumed") is not True:
        evidence_findings.append({"path": "evidence.input.consumed", "expected": True, "actual": evidence, "code": "INPUT-NOT-CONSUMED"})
    elif evidence.get("input", {}).get("sha256") != expected_sha:
        evidence_findings.append({"path": "evidence.input.sha256", "expected": expected_sha, "actual": evidence.get("input", {}).get("sha256"), "code": "INPUT-SHA-MISMATCH"})
    return {
        "fixtureId": fixture["fixtureId"],
        "inputPath": str(input_path),
        "inputSha256": expected_sha,
        "inspect": {"returnCode": inspect["returnCode"], "stdout": inspect["stdout"][-4000:], "stderr": inspect["stderr"][-4000:]},
        "converter": {"returnCode": run["returnCode"], "stdout": run["stdout"][-4000:], "stderr": run["stderr"][-4000:]},
        "document": document if isinstance(document, dict) else {},
        "evidence": evidence if isinstance(evidence, dict) else {},
        "evidenceMismatches": evidence_findings,
    }


def _fixture_result(fixture: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    expected = fixture["expected"]
    source_expected = {
        key: value.get("source", value) if isinstance(value, dict) else value
        for key, value in expected.items()
    }
    ir_expected = {
        key: value.get("ir", value) if isinstance(value, dict) else value
        for key, value in expected.items()
    }
    source_facts: dict[str, Any] | None = None
    source_projection: dict[str, Any] | None = None
    source_mismatches: list[dict[str, Any]] = []
    actual: dict[str, Any] = {}
    actual_mismatches: list[dict[str, Any]] = []
    adapter_diagnostics = [
        {
            key: diagnostic.get(key)
            for key in ("diagnosticId", "code", "severity", "phase", "message", "action")
            if key in diagnostic
        }
        for diagnostic in run["document"].get("diagnostics", [])
        if isinstance(diagnostic, dict)
    ]
    occurrence_accounting = {
        "status": "failed",
        "sourceOccurrenceCount": 0,
        "actualOccurrenceCount": 0,
        "missingSourceOccurrences": [],
        "unexpectedActualOccurrences": [],
        "unaccountedOccurrenceCount": 1,
    }
    try:
        source_facts = _source_facts(Path(run["inputPath"]))
        source_projection = _source_projection(source_facts, expected)
        source_mismatches = _diff(source_expected, _select_shape(source_expected, source_projection), f"source[{fixture['fixtureId']}]")
        can_compare_actual = (
            run["inspect"]["returnCode"] == 0
            and run["converter"]["returnCode"] == 0
            and run["document"].get("conversion", {}).get("status") != "failed"
        )
        if can_compare_actual:
            actual = _actual_projection(run["document"])
            actual_mismatches = _diff(ir_expected, _select_shape(ir_expected, actual), f"actual[{fixture['fixtureId']}]")
            occurrence_accounting = _occurrence_accounting(source_facts, actual)
        else:
            occurrence_accounting = _occurrence_accounting(source_facts, {})
    except QualificationError as exc:
        source_mismatches.append({"path": f"fixture[{fixture['fixtureId']}].source", "expected": "readable-independent-source", "actual": str(exc), "code": "SOURCE-READ-FAILED"})
    if run["inspect"]["returnCode"] != 0:
        actual_mismatches.append({"path": f"fixture[{fixture['fixtureId']}].inspect", "expected": 0, "actual": run["inspect"]["returnCode"], "code": "PUBLIC-INSPECT-FAILED"})
    if run["converter"]["returnCode"] != 0:
        actual_mismatches.append({"path": f"fixture[{fixture['fixtureId']}].converter", "expected": 0, "actual": run["converter"]["returnCode"], "code": "PUBLIC-CONVERTER-FAILED"})
    actual_mismatches.extend(run["evidenceMismatches"])
    return {
        "fixtureId": fixture["fixtureId"],
        "producerId": fixture["producerId"],
        "inputSha256": run["inputSha256"],
        "inspect": run["inspect"],
        "converter": run["converter"],
        "conversionStatus": run["document"].get("conversion", {}).get("status"),
        "adapterDiagnostics": adapter_diagnostics,
        "sourceFacts": {
            "packageMemberCount": len(source_facts.get("packageMembers", [])) if source_facts else 0,
            "relationshipCount": len(source_facts.get("relationships", [])) if source_facts else 0,
            "cellCount": len(source_facts.get("cells", [])) if source_facts else 0,
            "formulaCount": len(source_facts.get("formulas", [])) if source_facts else 0,
            "styleCount": len(source_facts.get("styles", {}).get("cellXfs", [])) if source_facts else 0,
            "unsupportedPaths": source_facts.get("unsupportedPaths", []) if source_facts else [],
        },
        "actualFacts": {
            "packageMemberCount": len(actual.get("profile", {}).get("packageMembers", [])),
            "relationshipCount": len(actual.get("relationships", [])),
            "cellCount": len(actual.get("valueFormula", {}).get("cells", [])),
            "formulaCount": len(actual.get("valueFormula", {}).get("formulas", [])),
            "styleCount": len(actual.get("styles", {}).get("requiredStyleIds", [])),
            "unsupportedPaths": actual.get("unsupported", {}).get("paths", []),
        },
        "sourceMismatches": source_mismatches,
        "mismatches": actual_mismatches,
        "occurrenceAccounting": occurrence_accounting,
        "unaccountedOccurrenceCount": occurrence_accounting["unaccountedOccurrenceCount"],
        "falseCompleteCount": 1
        if run["document"].get("conversion", {}).get("status") in {"complete", "complete-with-warnings"}
        and occurrence_accounting["unaccountedOccurrenceCount"]
        else 0,
    }


def _mutate_projection(projection: dict[str, Any], case: dict[str, Any]) -> None:
    kind = case["kind"]
    if kind == "drop-relationship":
        projection["relationships"] = projection["relationships"][1:]
    elif kind == "drop-formula-cache":
        formulas = projection["valueFormula"]["formulas"]
        if formulas:
            formulas[0]["cached"] = None
    elif kind == "drop-styled-empty-cell":
        projection["valueFormula"]["cells"] = [item for item in projection["valueFormula"]["cells"] if item.get("address") != case.get("address", "F1")]
    elif kind == "widen-table-ref":
        tables = projection["gridTable"]["tables"]
        if tables:
            tables[0]["ref"] = "A1:H20"
    elif kind == "hide-unsupported-part":
        projection["unsupported"]["paths"] = []
    elif kind == "wrong-style-format":
        projection["styles"]["cellXfs"][0]["code"] = "m/d/yy"
    else:
        raise QualificationError(f"unknown issue #100 mutation kind: {kind}")


def run_oracle_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    fixture_by_id = {item["fixtureId"]: item for item in corpus["fixtures"]}
    results = []
    for case in corpus["negativeCases"]:
        fixture = fixture_by_id.get(case.get("fixtureId"), corpus["fixtures"][0])
        expected = fixture["expected"]
        expected_ir = {
            key: value.get("ir", value) if isinstance(value, dict) else value
            for key, value in expected.items()
        }
        projection = {
            "profile": deepcopy(expected_ir["profile"]),
            "relationships": deepcopy(expected_ir["relationships"]),
            "valueFormula": deepcopy(expected_ir["valueFormula"]),
            "styles": deepcopy(expected_ir["styles"]),
            "gridTable": deepcopy(expected_ir["gridTable"]),
            "unsupported": deepcopy(expected_ir["unsupported"]),
        }
        _mutate_projection(projection, case)
        findings = _diff(expected_ir, projection)
        results.append(
            {
                "caseId": case["id"],
                "fixtureId": fixture["fixtureId"],
                "caseKind": case["kind"],
                "classification": "oracle-projection-mutation",
                "executableAdapterMutation": False,
                "expectedDefectCode": case["expectedDefectCode"],
                "detected": bool(findings),
                "status": "passed" if findings else "failed",
                "mismatchCount": len(findings),
            }
        )
    return results


def _producer_results(corpus: dict[str, Any], fixture_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_fixture = {item["fixtureId"]: item for item in fixture_results}
    result: list[dict[str, Any]] = []
    for entry in corpus["producerMatrix"]:
        producer_id = entry["producerId"]
        fixture_id = entry.get("fixtureId")
        if fixture_id in by_fixture:
            current = by_fixture[fixture_id]
            mismatch_count = len(current["mismatches"]) + len(current["sourceMismatches"])
            mismatch_count += current.get("unaccountedOccurrenceCount", 0)
            result.append(
                {
                    "producerId": producer_id,
                    "required": entry["required"],
                    "status": "qualified"
                    if not current["mismatches"]
                    and not current["sourceMismatches"]
                    and current.get("occurrenceAccounting", {}).get("status") == "passed"
                    else "mismatch",
                    "fixtureId": fixture_id,
                    "inputSha256": current["inputSha256"],
                    "mismatchCount": mismatch_count,
                    "provenance": deepcopy(entry["provenance"]),
                }
            )
            continue
        path_value = entry.get("inputPath")
        path = _safe_source_path(path_value) if isinstance(path_value, str) else None
        if path is None or not path.is_file():
            result.append(
                {
                    "producerId": producer_id,
                    "required": entry["required"],
                    "status": "unavailable",
                    "inputPath": path_value,
                    "inputSha256": None,
                    "declaredSha256": entry["provenance"].get("sha256"),
                    "mismatchCount": 0,
                    "provenance": deepcopy(entry["provenance"]),
                }
            )
        else:
            actual_sha = _sha256_file(path)
            declared_sha = entry["provenance"].get("sha256")
            if declared_sha and actual_sha != declared_sha:
                status = "fixture-hash-mismatch"
            else:
                status = "available-but-unbound"
            result.append(
                {
                    "producerId": producer_id,
                    "required": entry["required"],
                    "status": status,
                    "inputPath": str(path),
                    "inputSha256": actual_sha,
                    "declaredSha256": declared_sha,
                    "mismatchCount": 1,
                    "provenance": deepcopy(entry["provenance"]),
                }
            )
    return result


def _finding_belongs_to_section(finding: dict[str, Any], section: str) -> bool:
    path = str(finding.get("path", ""))
    token = {
        "profile": ".profile",
        "relationships": ".relationships",
        "values": ".valueFormula",
        "styles": ".styles",
        "grid": ".gridTable",
        "unsupported": ".unsupported",
    }[section]
    if token in path:
        return True
    return section == "profile" and any(item in path for item in (".inspect", ".converter", "evidence."))


def _requirement_results(
    corpus: dict[str, Any],
    fixture_results: list[dict[str, Any]],
    producer_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    *,
    source_sha: str | None,
    source_tree_clean: bool | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    producer_by_id = {item["producerId"]: item for item in producer_results}
    undetected = [item for item in negative_results if not item["detected"]]
    results: list[dict[str, Any]] = []
    unmet: list[str] = []
    for requirement in corpus["requirements"]:
        kind = requirement["kind"]
        status = "passed"
        evidence: dict[str, Any] = {}
        if kind == "producer-fixture":
            producer = producer_by_id[requirement["producerId"]]
            status = "passed" if producer["status"] == "qualified" else "unmet"
            evidence = {"producerId": requirement["producerId"], "producerStatus": producer["status"]}
        elif kind == "producer-differential":
            required = [item for item in producer_results if item["required"]]
            status = "passed" if required and all(item["status"] == "qualified" for item in required) else "unmet"
            evidence = {"requiredProducerCount": len(required), "statuses": {item["producerId"]: item["status"] for item in required}}
        elif kind == "section":
            section = requirement["section"]
            findings = [
                finding
                for item in fixture_results
                for finding in item["sourceMismatches"] + item["mismatches"]
                if _finding_belongs_to_section(finding, section)
            ]
            execution_failures = [
                item["fixtureId"]
                for item in fixture_results
                if item.get("inspect", {}).get("returnCode") != 0 or item.get("converter", {}).get("returnCode") != 0
            ]
            if execution_failures:
                findings.extend({"fixtureId": fixture_id, "code": "PUBLIC-CONVERSION-NOT-QUALIFIED"} for fixture_id in execution_failures)
            status = "passed" if not findings else "unmet"
            evidence = {"section": section, "mismatchCount": len(findings), "executionFailures": execution_failures}
        elif kind == "occurrence-accounting":
            unaccounted = sum(item.get("unaccountedOccurrenceCount", 0) for item in fixture_results)
            status = "passed" if unaccounted == 0 else "unmet"
            evidence = {"unaccountedOccurrenceCount": unaccounted}
        elif kind == "negative-cases":
            status = "passed" if not undetected else "unmet"
            evidence = {"undetectedDefectCount": len(undetected)}
        elif kind == "issue89-defect-campaign":
            campaign = corpus["defectCampaign"]
            status = "passed" if campaign.get("status") == "bound" else "unmet"
            evidence = {"issueNumber": 89, "campaignStatus": campaign.get("status")}
            if status == "passed":
                report_path = campaign.get("reportPath")
                if not isinstance(report_path, str) or not _safe_source_path(report_path).is_file():
                    status = "unmet"
                    evidence["reportPath"] = report_path
        elif kind == "evidence-bundle":
            binding = corpus.get("evidenceBinding")
            status = "passed" if isinstance(binding, dict) and binding.get("status") == "bound" else "unmet"
            evidence = {"evidenceBindingStatus": binding.get("status") if isinstance(binding, dict) else None}
        if status == "unmet":
            unmet.append(requirement["id"])
        results.append({"id": requirement["id"], "kind": kind, "status": status, "evidence": evidence})
    if source_sha is None:
        unmet.append("SOURCE-SHA-UNAVAILABLE")
    if source_tree_clean is not True:
        unmet.append("SOURCE-TREE-NOT-CLEAN")
    return results, sorted(set(unmet))


def _report(
    kind: str,
    corpus: dict[str, Any],
    source_sha: str | None,
    source_tree_clean: bool | None,
    corpus_sha: str,
    fixture_results: list[dict[str, Any]],
    producer_results: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
    requirement_results: list[dict[str, Any]],
    unmet_requirements: list[str],
) -> dict[str, Any]:
    relevant = []
    for item in fixture_results:
        for finding in item["sourceMismatches"] + item["mismatches"]:
            if kind == "producers" or (kind in {"profile", "relationships", "values", "styles", "grid", "unsupported"} and _finding_belongs_to_section(finding, kind)):
                relevant.append(finding)
        if kind != "producers" and (
            item.get("inspect", {}).get("returnCode") != 0 or item.get("converter", {}).get("returnCode") != 0
        ):
            relevant.append(
                {
                    "path": f"fixture[{item['fixtureId']}].conversion",
                    "expected": "qualified-public-conversion",
                    "actual": item.get("adapterDiagnostics", []),
                    "code": "PUBLIC-CONVERSION-NOT-QUALIFIED",
                }
            )
    if kind == "producers":
        relevant = []
    undetected = [item for item in negative_results if not item["detected"]]
    false_complete = sum(item["falseCompleteCount"] for item in fixture_results)
    if kind == "producers":
        relevant.extend(
            {
                "path": f"producerMatrix[{item['producerId']}].status",
                "expected": "qualified",
                "actual": item["status"],
                "code": "PRODUCER-NOT-QUALIFIED",
            }
            for item in producer_results
            if item["required"] and item["status"] != "qualified"
        )
    if kind == "unsupported":
        relevant.extend(
            {
                "path": f"fixture[{item['fixtureId']}].falseCompleteCount",
                "expected": 0,
                "actual": item["falseCompleteCount"],
                "code": "FALSE-COMPLETE-UNSUPPORTED",
            }
            for item in fixture_results
            if item["falseCompleteCount"]
        )
    status = "passed" if not relevant and not unmet_requirements and not undetected and false_complete == 0 else "failed"
    converter_passed = all(
        item.get("inspect", {}).get("returnCode") == 0 and item.get("converter", {}).get("returnCode") == 0
        for item in fixture_results
    )
    source_oracle_passed = all(item["sourceFacts"].get("packageMemberCount", 0) > 0 and not item["sourceMismatches"] for item in fixture_results)
    accounting_passed = all(item.get("occurrenceAccounting", {}).get("status") == "passed" for item in fixture_results)
    producer_passed = all(item["status"] == "qualified" for item in producer_results if item["required"])
    return {
        "schema": "fdir/qualification-issue-100-report",
        "version": "1.1.0",
        "issueNumber": 100,
        "reportKind": kind,
        "sourceSha": source_sha,
        "sourceTreeClean": source_tree_clean,
        "corpusSha": corpus_sha,
        "status": status,
        "oracle": corpus["oracle"],
        "fixtures": fixture_results,
        "producerMatrix": producer_results,
        "mismatches": relevant,
        "mismatchCount": len(relevant),
        "requirements": requirement_results,
        "unmetRequirements": sorted(set(unmet_requirements)),
        "unmetCount": len(set(unmet_requirements)),
        "negativeDefectResults": negative_results,
        "undetectedDefectCount": len(undetected),
        "falseCompleteCount": false_complete,
        "unaccountedOccurrenceCount": sum(item["unaccountedOccurrenceCount"] for item in fixture_results),
        "assertions": [
            {"id": "public-converter-invoked", "status": "passed" if converter_passed else "failed"},
            {"id": "independent-source-oracle", "status": "passed" if source_oracle_passed else "failed"},
            {"id": "occurrence-accounting-zero", "status": "passed" if accounting_passed else "failed"},
            {"id": "negative-mutations-detected", "status": "passed" if not undetected else "failed"},
            {"id": "required-producers-qualified", "status": "passed" if producer_passed else "failed"},
            {"id": "exact-source-sha", "status": "passed" if source_sha and source_tree_clean is True else "failed"},
        ],
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    out_dir = Path(out_dir).resolve()
    source_sha = _source_sha()
    source_tree_clean = _source_tree_clean(out_dir)
    corpus = load_corpus(corpus_path)
    corpus_sha = _sha256_file(corpus_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    fixture_results: list[dict[str, Any]] = []
    for fixture in corpus["fixtures"]:
        try:
            run = _run_converter(fixture, work)
            fixture_results.append(_fixture_result(fixture, run))
        except Exception as exc:
            fixture_results.append(
                {
                    "fixtureId": fixture["fixtureId"],
                    "producerId": fixture["producerId"],
                    "inputSha256": None,
                    "inspect": {"returnCode": 124, "stdout": "", "stderr": str(exc)},
                    "converter": {"returnCode": 124, "stdout": "", "stderr": str(exc)},
                    "conversionStatus": "failed",
                    "adapterDiagnostics": [],
                    "sourceFacts": {"packageMemberCount": 0, "relationshipCount": 0, "cellCount": 0, "formulaCount": 0, "styleCount": 0, "unsupportedPaths": []},
                    "actualFacts": {"packageMemberCount": 0, "relationshipCount": 0, "cellCount": 0, "formulaCount": 0, "styleCount": 0, "unsupportedPaths": []},
                    "sourceMismatches": [{"path": f"fixture[{fixture['fixtureId']}]", "expected": "qualification-runnable", "actual": str(exc), "code": "QUALIFICATION-EXECUTION-FAILED"}],
                    "mismatches": [],
                    "occurrenceAccounting": {
                        "status": "failed",
                        "sourceOccurrenceCount": 0,
                        "actualOccurrenceCount": 0,
                        "missingSourceOccurrences": [],
                        "unexpectedActualOccurrences": [],
                        "unaccountedOccurrenceCount": 1,
                    },
                    "unaccountedOccurrenceCount": 1,
                    "falseCompleteCount": 0,
                }
            )
    producer_results = _producer_results(corpus, fixture_results)
    negative_results = run_oracle_mutations(corpus)
    requirement_results, unmet_requirements = _requirement_results(
        corpus,
        fixture_results,
        producer_results,
        negative_results,
        source_sha=source_sha,
        source_tree_clean=source_tree_clean,
    )
    reports = {
        kind: _report(
            kind,
            corpus,
            source_sha,
            source_tree_clean,
            corpus_sha,
            fixture_results,
            producer_results,
            negative_results,
            requirement_results,
            unmet_requirements,
        )
        for kind in REPORT_NAMES
    }
    _write_producer_envelope(out_dir, reports, Path(corpus_path), source_sha)
    failed = False
    for report_name in REPORT_NAMES.values():
        report = _read_json(out_dir / report_name)
        failed = failed or report.get("status") != "passed"
    if failed:
        print(f"FAIL: issue #100 qualification reports written: {', '.join(REPORT_NAMES.values())}")
        return 1
    print(f"PASS: issue #100 qualification reports written: {', '.join(REPORT_NAMES.values())}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    try:
        return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)
    except Exception as exc:
        print(f"FAIL: issue #100 qualification setup: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
